"""Plan C: cold start and loop hygiene. The meta.db indexes and their one-time
migration, pinned query plans, the numpy ghost diff, off-loop collection
construction, rowid vector sampling, memory release, and the index-job pre-work
order. The checks are structural (EXPLAIN plans, SQL traces, thread identity), not
timings, so the suite stays deterministic on Windows and Linux."""
import asyncio
import ctypes
import logging
import sqlite3
import threading
import zlib
from pathlib import Path

import httpx
import numpy as np
import pytest

import raggio.store as store
from raggio.app import create_app
from raggio.config import Settings
from raggio.store import (
    Collection,
    CollectionConfig,
    CollectionManager,
    IdMapIndex,
    _IvfIndex,
    open_meta_db,
)

DIM = 8
ROOT = {"x-api-key": "root-key"}
LOG = "uvicorn.error"  # the logger plan C logs through (uvicorn's handler in the container)


class FakeEmbedder:
    """Deterministic: same text -> same unit vector."""

    async def embed(self, texts):
        out = []
        for t in texts:
            v = np.random.default_rng(zlib.crc32(t.encode())).standard_normal(DIM)
            out.append((v / np.linalg.norm(v)).tolist())
        return out

    async def aclose(self):
        pass


def make_collection(tmp_path, embedder=None) -> Collection:
    return Collection(
        CollectionConfig("t", DIM, 4, None, None, None), Path(tmp_path),
        lambda: embedder or FakeEmbedder(),
    )


def rowvec(i):
    v = np.random.default_rng(1000 + i).standard_normal(DIM)
    return (v / np.linalg.norm(v)).tolist()


def ingest(col, n, start=0, meta=None):
    docs = [
        {"doc_id": f"d{i}", "chunks": [
            {"id": f"c{i}", "text": f"chunk {i}", "vector": rowvec(i),
             "metadata": (meta or {}).get(i)},
        ]}
        for i in range(start, start + n)
    ]
    asyncio.run(col._process_job({"documents": docs}))


def plan(db, sql, params=()) -> str:
    """EXPLAIN QUERY PLAN details joined with ' | ' (rows are id, parent, notused, detail)."""
    return " | ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, list(params)))


class Recorder:
    """Proxy for a sqlite3 connection that records every execute() as (sql, params)."""

    def __init__(self, db, log):
        self._db, self.log = db, log

    def execute(self, sql, params=()):
        self.log.append((sql, list(params)))
        return self._db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._db, name)


def record_sql(col) -> list:
    """Trace every statement the collection runs on its read connections."""
    log, real = [], col._rdb
    col._rdb = lambda: Recorder(real(), log)
    return log


def make_manager(tmp_path, monkeypatch) -> CollectionManager:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    return CollectionManager(Settings(), lambda cfg: FakeEmbedder())


@pytest.fixture(autouse=True)
def small_ivf(monkeypatch):
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)


@pytest.fixture(autouse=True)
def uvicorn_logs_reach_caplog(monkeypatch):
    # any test in the session that builds a uvicorn.Config applies uvicorn's
    # LOGGING_CONFIG, which sets propagate=False on "uvicorn"; caplog listens on the
    # root logger, so without this the caplog tests would depend on test order
    for name in ("uvicorn", LOG):
        monkeypatch.setattr(logging.getLogger(name), "propagate", True)


def index_names(db) -> set:
    # sql IS NULL filters out sqlite_autoindex_* (the external_id UNIQUE constraint)
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    )}


def make_legacy_db(path: Path, rows: int = 50) -> None:
    """A meta.db in the pre-plan-C layout: idx_records_doc, neither new index."""
    db = open_meta_db(path)
    db.execute("DROP INDEX IF EXISTS idx_records_doc_type")
    db.execute("DROP INDEX IF EXISTS idx_jobs_open")
    db.execute("CREATE INDEX IF NOT EXISTS idx_records_doc ON records(doc_id)")
    db.executemany(
        "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
        " VALUES (?,?,?,?,?,?,?,1)",
        [(i, f"c{i}", f"d{i % 7}", "chunk", i, f"chunk {i}", "{}") for i in range(1, rows + 1)],
    )
    db.commit()
    db.close()


# ---- Task 1: indexes and the one-time migration (D9) ----


def test_fresh_meta_db_has_the_covering_and_partial_indexes(tmp_path):
    db = open_meta_db(tmp_path / "meta.db")
    names = index_names(db)
    assert {"idx_records_doc_type", "idx_jobs_open"} <= names
    assert "idx_records_doc" not in names  # its (doc_id) prefix lives on in the covering index
    cols = [r[2] for r in db.execute("PRAGMA index_info(idx_records_doc_type)")]
    assert cols == ["doc_id", "type", "indexed"]
    partial = {r[1]: r[4] for r in db.execute("PRAGMA index_list(jobs)")}
    assert partial["idx_jobs_open"] == 1
    db.close()


def test_legacy_db_migrates_once_and_logs(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOG)
    path = tmp_path / "meta.db"
    make_legacy_db(path)
    db = open_meta_db(path)
    assert "one-time migration" in caplog.text
    assert "built idx_records_doc_type" in caplog.text
    assert "idx_records_doc" not in index_names(db)
    assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 50
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()
    caplog.clear()
    open_meta_db(path).close()  # already migrated: a silent no-op
    assert "idx_records_doc_type" not in caplog.text


def test_interrupted_migration_drops_the_old_index_on_reopen(tmp_path):
    path = tmp_path / "meta.db"
    make_legacy_db(path)
    db = sqlite3.connect(path)  # the process died after CREATE INDEX, before DROP INDEX
    db.execute("CREATE INDEX idx_records_doc_type ON records(doc_id, type, indexed)")
    db.close()
    db = open_meta_db(path)
    names = index_names(db)
    assert "idx_records_doc" not in names
    assert {"idx_records_doc_type", "idx_jobs_open"} <= names
    db.close()


def test_migration_heartbeat_logs_progress(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(store, "MIGRATION_LOG_EVERY_S", 0.0)
    monkeypatch.setattr(store, "MIGRATION_PROGRESS_OPS", 100)
    caplog.set_level(logging.INFO, logger=LOG)
    path = tmp_path / "meta.db"
    make_legacy_db(path, rows=2000)
    open_meta_db(path).close()
    assert "still building idx_records_doc_type" in caplog.text


def test_open_job_queries_use_the_partial_index(tmp_path):
    col = make_collection(tmp_path)
    with col.db_lock:
        col.db.executemany(
            "INSERT INTO jobs(id, payload, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            [(i, "{}", "done", "", "") for i in range(1, 51)]
            + [(51, "{}", "processing", "", ""), (52, "{}", "pending", "", ""),
               (53, "{}", "error", "", "")],
        )
        col.db.commit()
    db = col._rdb()
    # the literal predicate D's claim query, pending_jobs and resume_pending keep (§4.1)
    count = "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
    claim = ("SELECT id, payload FROM jobs WHERE status IN ('pending','processing')"
             " ORDER BY id LIMIT 1")
    assert "idx_jobs_open" in plan(db, count), plan(db, count)
    assert "idx_jobs_open" in plan(db, claim), plan(db, claim)
    assert col.pending_jobs() == 2
    assert col._claim_next()[0] == 51  # lowest open id, 'processing' replays first


# ---- Task 2: pinned query plans (D9) ----

LIST_META = {i: {"year": 2000 + i % 5, "src": "wiki" if i % 2 else "arxiv"} for i in range(30)}


@pytest.mark.parametrize("scope", ["chunks", "summaries", "both"])
@pytest.mark.parametrize("sort", [None, "year", "-year"])
@pytest.mark.parametrize("filt", [None, {"src": "wiki"}])
def test_listing_page_query_never_uses_the_covering_index(tmp_path, scope, sort, filt):
    # p1: with idx_records_doc_type the planner walks the index in doc_id order and
    # fetches every row by rowid (0.91 -> 2.73 s warm at 2.55M rows); EXPLAIN the SQL
    # list_records really runs, for every page shape
    col = make_collection(tmp_path)
    ingest(col, 30, meta=LIST_META)
    log = record_sql(col)
    col.list_records(scope, filt, sort, 20, 0)
    page = [(s, p) for s, p in log if "LIMIT ? OFFSET ?" in s]
    assert len(page) == 1
    detail = plan(col.db, *page[0])
    assert "idx_records_doc_type" not in detail, detail


def test_records_scans_use_the_covering_index(tmp_path):
    # regression pins (brief 2b §3, SQLite 3.47.1, no sqlite_stat1)
    col = make_collection(tmp_path)
    ingest(col, 30, meta=LIST_META)
    db = col.db
    for sql in (
        "SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type",  # Collection.__init__
        "SELECT type, COUNT(*) FROM records GROUP BY type",  # stats()
        "SELECT COUNT(DISTINCT doc_id) FROM records",  # stats()
        "SELECT id FROM records WHERE indexed=1",  # the live-id scan (_live_ids, Task 3)
        "SELECT COUNT(*) FROM records WHERE indexed = 1",  # unfiltered list count
    ):
        assert "COVERING INDEX idx_records_doc_type" in plan(db, sql), (sql, plan(db, sql))
    for sql in (
        "SELECT id FROM records WHERE doc_id=? AND type='chunk' AND indexed=1",  # siblings
        "SELECT id FROM records WHERE doc_id=? AND type='summary'",  # summary expansion
        "SELECT id FROM records WHERE doc_id=? ORDER BY type DESC, position, id",  # get_document
        "SELECT id, indexed, type FROM records WHERE doc_id=?",  # _delete_doc_rows
    ):
        detail = plan(db, sql, ["d1"])
        assert "idx_records_doc_type (doc_id=?" in detail, (sql, detail)
    filtered = "SELECT COUNT(*) FROM records WHERE indexed = 1 AND json_extract(metadata, ?) = ?"
    assert "idx_records_doc_type" not in plan(db, filtered, ["$.src", "wiki"])
    fts = (
        "SELECT r.id, -bm25(records_fts) FROM records_fts"
        " JOIN records r ON r.id = records_fts.rowid"
        " WHERE records_fts MATCH ? AND doc_id=? AND type='chunk' AND indexed=1"
        " ORDER BY bm25(records_fts) LIMIT ?"
    )
    # ADR 0003: records_fts must stay the outer loop (never a rowid-restricted MATCH)
    assert plan(db, fts, ['"chunk"', "d1", 5]).startswith("SCAN records_fts VIRTUAL TABLE")


# ---- Task 3: numpy id-set diffs ----


def seed(tmp_path, record_ids, index_ids):
    """A collection dir whose meta.db holds `record_ids` and whose synced flat index
    holds `index_ids`: the state a crash between a db commit and index.sync leaves."""
    db = open_meta_db(tmp_path / "meta.db")
    db.executemany(
        "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
        " VALUES (?,?,'d1','chunk',0,'hello','{}',1)",
        [(i, f"c{i}") for i in record_ids],
    )
    db.commit()
    db.close()
    idx = IdMapIndex(dim=DIM, bit_width=4)
    if index_ids:
        idx.add_with_ids(
            np.random.default_rng(0).standard_normal((len(index_ids), DIM)).astype(np.float32),
            np.array(index_ids, dtype=np.uint64),
        )
    idx.sync(str(tmp_path / "index.tvim"))


def test_live_ids_is_uint64_and_handles_empty(tmp_path):
    col = make_collection(tmp_path)
    empty = store._live_ids(col.db)
    assert empty.dtype == np.uint64 and len(empty) == 0
    ingest(col, 5)
    live = store._live_ids(col.db)
    assert live.dtype == np.uint64
    assert sorted(live.tolist()) == [1, 2, 3, 4, 5]


def test_reconcile_reads_live_ids_once_through_the_helper(tmp_path, monkeypatch):
    seed(tmp_path, [1, 2], [1, 2, 3])
    calls, real = [], store._live_ids
    monkeypatch.setattr(store, "_live_ids", lambda db: calls.append(1) or real(db))
    col = make_collection(tmp_path)
    assert calls == [1]
    assert len(col.index) == 2 and not col.index.contains(3)


def test_reconcile_keeps_live_ids_above_2_32(tmp_path):
    big = [2**40 + 1, 2**40 + 2, 2**40 + 3]  # a int64/uint64 mix would promote to float64
    seed(tmp_path, big[:2], big)
    col = make_collection(tmp_path)
    assert len(col.index) == 2
    assert col.index.contains(big[0]) and col.index.contains(big[1])
    assert not col.index.contains(big[2])


def test_reconcile_without_ghosts_does_not_sync(tmp_path, monkeypatch):
    seed(tmp_path, [1, 2, 3], [1, 2, 3])
    synced = []
    monkeypatch.setattr(Collection, "_sync_index", lambda self: synced.append(1))
    col = make_collection(tmp_path)
    assert len(col.index) == 3 and synced == []


def test_reconcile_all_ghosts_empties_the_index(tmp_path):
    seed(tmp_path, [], [5, 6, 7])  # empty records, non-empty index
    col = make_collection(tmp_path)
    assert len(col.index) == 0


def test_reconcile_never_re_adds(tmp_path):
    seed(tmp_path, [1, 2, 3, 4], [1, 2])  # records the index lacks stay out (§4.3)
    col = make_collection(tmp_path)
    assert len(col.index) == 2
    assert not col.index.contains(3) and not col.index.contains(4)


@pytest.mark.parametrize("seed_", range(5))
def test_reconcile_matches_the_set_semantics(tmp_path, seed_):
    rng = np.random.default_rng(seed_)
    records = sorted(rng.choice(np.arange(1, 200), size=int(rng.integers(0, 150)), replace=False).tolist())
    in_index = sorted(rng.choice(np.arange(1, 200), size=int(rng.integers(1, 150)), replace=False).tolist())
    seed(tmp_path, records, in_index)
    col = make_collection(tmp_path)
    expect = set(in_index) & set(records)  # the pre-plan-C set logic
    assert len(col.index) == len(expect)
    assert all(col.index.contains(i) == (i in expect) for i in in_index)


def test_rows_deleted_during_a_build_are_diffed_out(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    real = col._iter_vec_blocks
    victims = iter(["c7", "c8"])

    def stream_then_delete():
        yield from real()
        with col.db_lock:  # a delete that commits while the build streams
            col.db.execute("DELETE FROM records WHERE external_id=?", (next(victims),))
            col.db.commit()

    col._iter_vec_blocks = stream_then_delete
    asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    assert isinstance(col.index, _IvfIndex) and len(col.index) == 299
    asyncio.run(col._process_job({"op": "detach_index"}))
    assert isinstance(col.index, IdMapIndex) and len(col.index) == 298


# ---- Task 4: off-loop, cancel-safe construction (§5.1) ----

GATE_TIMEOUT = 3.0


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.done = {"n": 0, "thread": None}


@pytest.fixture
def gated(monkeypatch):
    """Swap store.Collection for a subclass whose construction parks on `release`,
    so a test can hold a load mid-flight and see which thread built it."""
    gate, real = Gate(), store.Collection

    class GatedCollection(real):
        def __init__(self, *args, **kwargs):
            gate.entered.set()
            if not gate.release.wait(GATE_TIMEOUT):
                raise RuntimeError("gate never released")
            super().__init__(*args, **kwargs)
            gate.done["n"] += 1
            gate.done["thread"] = threading.get_ident()

    monkeypatch.setattr(store, "Collection", GatedCollection)
    return gate


def test_touch_builds_the_collection_off_the_event_loop(tmp_path, monkeypatch, gated):
    m = make_manager(tmp_path, monkeypatch)
    gated.release.set()

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        return threading.get_ident(), c

    loop_thread, c = asyncio.run(run())
    assert gated.done["n"] == 1
    assert gated.done["thread"] != loop_thread
    assert m.resident["x"] is c and c._worker is not None


def test_off_loop_built_collection_serves_ingest_and_search(tmp_path, monkeypatch):
    # its asyncio.Condition/Event were created on a worker thread: they must still work
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        await c.enqueue({"documents": [
            {"doc_id": "d1", "chunks": [{"id": "c1", "text": "one", "vector": rowvec(1)}]},
        ]})
        for _ in range(500):
            if c.pending_jobs() == 0:
                break
            await asyncio.sleep(0.01)
        q = np.array([rowvec(1)], dtype=np.float32)
        hits = await c.search("vector", q, None, 5, "chunks", None, None)
        return c.pending_jobs(), [h["id"] for h in hits]

    assert asyncio.run(run()) == (0, ["c1"])


def test_healthz_answers_while_a_collection_loads(tmp_path, monkeypatch, gated):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    app = create_app(embedder_factory=lambda cfg: FakeEmbedder())

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post("/collections", headers=ROOT, json={"name": "x"})
                assert r.status_code == 201, r.text
                load = asyncio.create_task(c.get("/collections/x", headers=ROOT))
                await asyncio.to_thread(gated.entered.wait, GATE_TIMEOUT)
                h = await asyncio.wait_for(c.get("/healthz"), 1.0)
                gated.release.set()
                r = await load
                return (h.status_code, h.json()["resident_collections"],
                        gated.entered.is_set(), r.status_code)

    assert asyncio.run(run()) == (200, [], True, 200)


def test_resident_collection_is_served_while_another_loads(tmp_path, monkeypatch, gated):
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        for n in ("x", "y"):
            await m.create_collection(n, DIM, 4, None, None, None)
        gated.release.set()
        x = await m.touch("x")
        gated.release.clear()
        gated.entered.clear()
        load = asyncio.create_task(m.touch("y"))
        await asyncio.to_thread(gated.entered.wait, GATE_TIMEOUT)
        again = await asyncio.wait_for(m.touch("x"), 1.0)  # must not queue behind y's load
        gated.release.set()
        y = await load
        return again is x, m.resident["y"] is y

    assert asyncio.run(run()) == (True, True)


def test_cancelled_load_still_registers_the_built_collection(tmp_path, monkeypatch, gated):
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        first = asyncio.create_task(m.touch("x"))
        await asyncio.to_thread(gated.entered.wait, GATE_TIMEOUT)
        first.cancel()  # the client disconnected mid-load
        await asyncio.sleep(0)
        second = asyncio.create_task(m.touch("x"))
        await asyncio.sleep(0.05)  # second is now queued on _load_lock
        gated.release.set()
        c = await second
        with pytest.raises(asyncio.CancelledError):
            await first
        worker_live = c._worker is not None and not c._worker.done()
        return m.resident.get("x") is c, gated.done["n"], worker_live

    assert asyncio.run(run()) == (True, 1, True)


def test_resident_touch_does_not_wait_for_another_collections_delete(tmp_path, monkeypatch):
    """B's delete holds _load_lock through stop(). A resident collection must not
    queue behind it; only loads of non-resident collections still do."""
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        for n in ("x", "y"):
            await m.create_collection(n, DIM, 4, None, None, None)
        x = await m.touch("x")
        y = await m.touch("y")
        held, release = asyncio.Event(), asyncio.Event()
        real_stop = y.stop

        async def slow_stop():
            held.set()
            await release.wait()
            await real_stop()

        y.stop = slow_stop
        delete = asyncio.create_task(m.delete_collection("y"))
        await asyncio.wait_for(held.wait(), GATE_TIMEOUT)
        locked = m._load_lock.locked()
        again = await asyncio.wait_for(m.touch("x"), 1.0)  # must not queue behind y's delete
        release.set()
        await delete
        return locked, again is x, "y" in m.resident

    assert asyncio.run(run()) == (True, True, False)


# ---- Task 5: records scans on request paths stay off the loop (§5.2) ----


def _off_loop() -> bool:
    """True inside an asyncio.to_thread worker: no running loop in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def api_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    return create_app(embedder_factory=lambda cfg: FakeEmbedder())


async def wait_job(c, name, job_id):
    for _ in range(500):
        r = await c.get(f"/collections/{name}/jobs/{job_id}", headers=ROOT)
        if r.json()["status"] in ("done", "error"):
            return r.json()
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished")


def api_docs(n):
    return [
        {"doc_id": f"d{i}", "chunks": [{
            "id": f"c{i}", "text": f"chunk {i}", "vector": rowvec(i),
            "metadata": {"src": "wiki" if i % 2 else "arxiv", "year": 2000 + i},
        }]}
        for i in range(n)
    ]


def test_collection_info_runs_stats_off_the_loop(tmp_path, monkeypatch):
    where, real = [], store.Collection.stats

    def spy(self):
        where.append(_off_loop())
        return real(self)

    monkeypatch.setattr(store.Collection, "stats", spy)
    app = api_app(tmp_path, monkeypatch)

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await c.post("/collections", headers=ROOT, json={"name": "x"})
                r = await c.post("/collections/x/documents", headers=ROOT,
                                 json={"documents": api_docs(1)})
                assert (await wait_job(c, "x", r.json()["job_id"]))["status"] == "done"
                return (await c.get("/collections/x", headers=ROOT)).json()

    info = asyncio.run(run())
    assert where == [True]
    assert (info["documents"], info["chunks"], info["summaries"], info["pending_jobs"]) == (1, 1, 0, 0)
    assert info["name"] == "x" and info["index"] == {"type": "flat"}


def test_request_paths_that_scan_records_run_off_the_loop(tmp_path, monkeypatch):
    # §5.2 audit: filtered/sorted listings and filtered allowlists take 43-54 s cold at
    # 4 GiB, so every request-path _filter_sql caller must run in a worker thread
    where, real = [], store._filter_sql

    def spy(scope, filt):
        where.append(_off_loop())
        return real(scope, filt)

    monkeypatch.setattr(store, "_filter_sql", spy)
    app = api_app(tmp_path, monkeypatch)

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await c.post("/collections", headers=ROOT, json={"name": "x"})
                r = await c.post("/collections/x/documents", headers=ROOT,
                                 json={"documents": api_docs(6)})
                assert (await wait_job(c, "x", r.json()["job_id"]))["status"] == "done"
                listing = await c.get("/collections/x/documents", headers=ROOT, params={
                    "scope": "chunks", "filter": '{"src": "wiki"}', "sort": "-year"})
                vector = await c.post("/collections/x/search", headers=ROOT, json={
                    "query": {"vector": rowvec(1)}, "filter": {"src": "wiki"}, "k": 3})
                text = await c.post("/collections/x/search", headers=ROOT, json={
                    "query": {"text": "chunk"}, "mode": "text", "filter": {"src": "wiki"}, "k": 3})
                return [r.status_code for r in (listing, vector, text)]

    assert asyncio.run(run()) == [200, 200, 200]
    assert len(where) >= 3 and all(where), where
