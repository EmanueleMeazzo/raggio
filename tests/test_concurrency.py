"""Concurrency correctness (ADR 0001, addendum 2026-09): the shared catalog connection,
delete_collection vs a racing touch(), Collection.stop() vs in-flight threads,
copy-on-write indexed_counts, and a mixed-workload stress run checked after
quiescence. Races that can't be forced deterministically under the GIL are pinned
structurally (lock spies, identity checks) instead of by timing."""
import ast
import asyncio
import collections
import os
import random
import re
import sqlite3
import sys
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

import raggio.store as store
from raggio.app import create_app
from raggio.config import Settings
from raggio.store import Collection, CollectionConfig, CollectionDeletedError, CollectionManager

DIM = 8
ROOT = {"x-api-key": "root-key"}


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


def vec(seed):
    return np.random.default_rng(seed).standard_normal(DIM).tolist()


def make_collection(tmp_path) -> Collection:
    return Collection(CollectionConfig("t", DIM, 4, None, None, None), Path(tmp_path), FakeEmbedder)


def make_manager(tmp_path, monkeypatch) -> CollectionManager:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    return CollectionManager(Settings(), lambda cfg: FakeEmbedder())


@pytest.fixture
def busy_switching():
    """Force a thread switch every ~1us so unlocked shared state interleaves at
    statement granularity instead of once per 5ms default switch interval."""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


# ---- R1: the shared catalog connection ----


class _LockAsserting:
    """Wraps a sqlite3 connection (and the cursors it returns): every call must run
    with `lock` held: pysqlite objects are not safe to share across threads."""

    def __init__(self, inner, lock):
        self._inner, self._lock = inner, lock

    def _check(self):
        assert self._lock.locked(), "catalog touched outside _catalog_lock"

    def execute(self, *args):
        self._check()
        return _LockAsserting(self._inner.execute(*args), self._lock)

    def fetchone(self):
        self._check()
        return self._inner.fetchone()

    def fetchall(self):
        self._check()
        return self._inner.fetchall()

    def commit(self):
        self._check()
        return self._inner.commit()

    def close(self):
        self._check()
        return self._inner.close()

    def __iter__(self):
        return self

    def __next__(self):
        self._check()
        return next(self._inner)


def test_every_catalog_statement_runs_under_the_catalog_lock(tmp_path, monkeypatch):
    m = make_manager(tmp_path, monkeypatch)
    m.catalog = _LockAsserting(m.catalog, m._catalog_lock)

    async def run():
        await m.create_collection("a", DIM, 4, None, None, "key-a")
        assert m.get_config("a").name == "a"
        assert m.get_config("missing") is None
        assert m.list_collections() == ["a"]
        m.set_index_config("a", {"nlist": 2, "nprobe": 1})
        await m.touch("a")
        await m.delete_collection("a")
        assert m.list_collections() == []
        await m.shutdown()

    asyncio.run(run())


def test_concurrent_catalog_reads_return_the_requested_row(tmp_path, monkeypatch, busy_switching):
    # require_collection is a sync dependency: FastAPI runs it on threadpool threads,
    # one per in-flight request, all reading the manager's single catalog connection
    m = make_manager(tmp_path, monkeypatch)
    names = [f"c{i}" for i in range(8)]

    async def create():
        for n in names:
            await m.create_collection(n, DIM, 4, None, None, f"key-{n}")

    asyncio.run(create())
    bad = []
    start = threading.Barrier(16)

    def hammer(t):
        start.wait()
        deadline = time.monotonic() + 1.0
        k = 0
        while time.monotonic() < deadline and len(bad) < 20:
            n = names[(t + k) % len(names)]
            k += 1
            try:
                cfg = m.get_config(n)
                if cfg is None or cfg.name != n or cfg.key_hash != store.hash_key(f"key-{n}"):
                    bad.append((n, cfg and cfg.name))
                if k % 8 == 0 and m.list_collections() != names:
                    bad.append(("list_collections", n))
            except Exception as e:  # InterfaceError / IndexError / SystemError on main
                bad.append((n, repr(e)))

    threads = [threading.Thread(target=hammer, args=(t,)) for t in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    asyncio.run(m.shutdown())
    assert bad == []


def test_concurrent_collection_key_checks_are_consistent(tmp_path, monkeypatch, busy_switching):
    # end to end: collection B's key must get exactly 401 on A and 200 on B, never a
    # 500 (InterfaceError) or a decision made against the other collection's row
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    app = create_app(embedder_factory=lambda cfg: FakeEmbedder())

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                for n in ("A", "B"):
                    r = await c.post("/collections", headers=ROOT,
                                     json={"name": n, "collection_key": f"key{n}"})
                    assert r.status_code == 201, r.text
                seen = collections.Counter()

                async def hit(i):
                    target = "A" if i % 2 else "B"
                    r = await c.get(f"/collections/{target}", headers={"x-api-key": "keyB"})
                    seen[f"{target}:{r.status_code}"] += 1

                # 16 concurrent requests x 80 rounds: c=16, as in p4's R1 hammer on gn100
                for _ in range(80):
                    await asyncio.gather(*(hit(i) for i in range(16)))
        return seen

    assert asyncio.run(run()) == {"A:401": 640, "B:200": 640}


def test_no_threading_lock_is_held_across_an_await():
    # _catalog_lock and db_lock are threading locks the loop thread takes too: an
    # await inside one parks every pool thread behind a suspended coroutine, and a
    # second coroutine taking it on the loop deadlocks the server (not reentrant)
    tree = ast.parse(Path(store.__file__).read_text(encoding="utf-8"))
    sites, awaits = collections.Counter(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        locks = [i.context_expr.attr for i in node.items
                 if isinstance(i.context_expr, ast.Attribute)
                 and i.context_expr.attr in ("_catalog_lock", "db_lock")]
        if locks:
            sites.update(locks)
            awaits += [(locks[0], n.lineno) for stmt in node.body for n in ast.walk(stmt)
                       if isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor))]
    assert sites["_catalog_lock"] and sites["db_lock"]  # the scan still sees the locks
    assert awaits == []


# ---- delete_collection vs a racing touch() ----


def test_delete_collection_is_not_resurrected_by_a_racing_touch(tmp_path, monkeypatch):
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        in_stop, release = asyncio.Event(), asyncio.Event()
        real_stop = c.stop

        async def slow_stop():  # eviction yields to the loop (sync, worker cancel)
            in_stop.set()
            await release.wait()
            await real_stop()

        c.stop = slow_stop
        deleter = asyncio.create_task(m.delete_collection("x"))
        await in_stop.wait()
        toucher = asyncio.create_task(m.touch("x"))  # a request arriving mid-delete
        await asyncio.sleep(0.05)
        release.set()
        await deleter
        with pytest.raises(KeyError):
            await toucher  # the collection is gone: 404, never a reload of its files
        assert "x" not in m.resident
        await m.shutdown()

    asyncio.run(run())


def test_delete_waits_for_an_in_flight_load_of_the_same_collection(tmp_path, monkeypatch):
    # touch() yields mid-load while it LRU-evicts another collection (and, once
    # plan C builds Collection off the loop, while it constructs): a delete landing
    # there must wait, or the load registers a collection whose row and files are gone
    monkeypatch.setenv("MAX_RESIDENT_COLLECTIONS", "1")
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        for n in ("x", "y"):
            await m.create_collection(n, DIM, 4, None, None, None)
        x = await m.touch("x")
        in_stop, release = asyncio.Event(), asyncio.Event()
        real_stop = x.stop

        async def slow_stop():
            in_stop.set()
            await release.wait()
            await real_stop()

        x.stop = slow_stop
        loader = asyncio.create_task(m.touch("y"))  # budget 1: evicts x first
        await in_stop.wait()
        deleter = asyncio.create_task(m.delete_collection("y"))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(loader, deleter, return_exceptions=True)
        state = ("y" in m.resident, m._dir("y").exists(), m.get_config("y"),
                 loader.exception(), deleter.exception())
        await m.shutdown()
        return state

    assert asyncio.run(run()) == (False, False, None, None, None)


# ---- R3: Collection.stop() vs in-flight threads ----


def test_stop_does_not_cancel_the_worker_inside_its_write_section(tmp_path):
    # cancelling first released the write lock mid-upsert while the orphaned to_thread
    # body went on to commit indexed=1 rows the index never received: a filtered search
    # in that window built an allowlist with ids the index lacks (turbovec KeyError)
    col = make_collection(tmp_path)
    inside, release = threading.Event(), threading.Event()
    real_upsert = col._upsert_rows

    def slow_upsert(rows, mat):
        inside.set()
        release.wait(5)
        return real_upsert(rows, mat)

    col._upsert_rows = slow_upsert

    async def run():
        col.start_worker()
        await col.enqueue({"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": vec(1)}]}]})
        assert await asyncio.to_thread(inside.wait, 5)
        stopper = asyncio.create_task(col.stop())
        await asyncio.sleep(0.1)
        cancelled_mid_write = col._worker.done()
        release.set()
        await asyncio.wait_for(stopper, 5)
        return cancelled_mid_write

    assert asyncio.run(run()) is False  # stop() waited for upsert -> add -> sync
    reopened = make_collection(tmp_path)  # the row reached the index before the close
    try:
        n = reopened._rdb().execute("SELECT COUNT(*) FROM records WHERE indexed=1").fetchone()[0]
        assert (n, len(reopened.index)) == (1, 1)
    finally:
        asyncio.run(reopened.stop())


def test_stop_waits_for_a_write_transaction_its_cancelled_worker_left_running(tmp_path):
    col = make_collection(tmp_path)
    inside, release = threading.Event(), threading.Event()
    outcome = {}

    def slow_finish(job_id, status, error):  # the worker's to_thread body, still running
        with col.db_lock:  # once stop() cancels the task that awaited it
            inside.set()
            release.wait(5)
            try:
                col.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
                col.db.commit()
                outcome["committed"] = status
            except sqlite3.ProgrammingError as e:  # "Cannot operate on a closed database"
                outcome["error"] = e

    col._finish_job = slow_finish

    async def run():
        col.start_worker()
        await col.enqueue({"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": vec(1)}]}]})
        assert await asyncio.to_thread(inside.wait, 5)
        stopper = asyncio.create_task(col.stop())
        for _ in range(500):  # until stop() has marked the collection closed
            if col._closed:
                break
            await asyncio.sleep(0.01)
        assert col._closed, "stop() never marked the collection closed"
        await asyncio.sleep(0.1)
        closed_under_the_writer = stopper.done()
        release.set()
        await asyncio.wait_for(stopper, 5)
        return closed_under_the_writer

    assert asyncio.run(run()) is False  # stop() waited for db_lock before closing
    assert outcome == {"committed": "done"}


def test_read_connection_opened_during_stop_is_closed_not_leaked(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    opened = []
    connecting, release = threading.Event(), threading.Event()
    real_connect = sqlite3.connect

    def spy_connect(*args, **kw):
        conn = real_connect(*args, **kw)
        opened.append(conn)
        if threading.current_thread().name == "late-reader":
            connecting.set()  # past _rdb's _closed check, not yet registered
            release.wait(5)
        return conn

    monkeypatch.setattr(store.sqlite3, "connect", spy_connect)
    outcome = {}

    def late_reader():
        try:
            outcome["conn"] = col._rdb()
        except RuntimeError as e:
            outcome["error"] = e

    async def run():
        reader = threading.Thread(target=late_reader, name="late-reader")
        reader.start()
        assert await asyncio.to_thread(connecting.wait, 5)
        await col.stop()
        release.set()
        await asyncio.to_thread(reader.join, 5)

    asyncio.run(run())
    assert "error" in outcome  # a closed collection hands out no connection
    for conn in opened:  # and every connection it opened is closed (Windows: the
        with pytest.raises(sqlite3.ProgrammingError):  # dir stays deletable)
            conn.execute("SELECT 1")


class _CloseSpy:
    """Stands in for a sqlite3 connection; records the collection's lock state at close()."""

    def __init__(self, inner, col, seen):
        self._inner, self._col, self._seen = inner, col, seen

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self):
        self._seen.append((self._col.lock._writing, self._col.db_lock.locked()))
        self._inner.close()


def test_stop_closes_connections_inside_the_write_lock_and_under_db_lock(tmp_path):
    # the stop() contract plan D2 builds on (its flush goes in the same section, before
    # the close): no search mid-query on a read connection (write lock), no write
    # transaction mid-flight on self.db (db_lock) when the connections close
    col = make_collection(tmp_path)
    seen = []

    async def run():
        col.start_worker()
        await asyncio.to_thread(col.stats)  # a pool thread registers a read connection
        col.db = _CloseSpy(col.db, col, seen)
        col._read_conns[:] = [_CloseSpy(c, col, seen) for c in col._read_conns]
        await col.stop()

    asyncio.run(run())
    assert len(seen) >= 2
    assert set(seen) == {(True, True)}


def test_deleted_collection_leaves_no_files_and_its_name_can_be_reused(tmp_path, monkeypatch):
    # stop() must close every connection the collection opened (the write connection
    # and one read connection per thread that served it): on Windows one leaked handle
    # makes delete's rmtree(ignore_errors=True) leave the directory behind
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        await c.enqueue({"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": vec(1)}]}]})
        deadline = time.monotonic() + 10
        while c.pending_jobs():
            assert time.monotonic() < deadline, "ingest job never finished"
            await asyncio.sleep(0.01)
        await asyncio.gather(*(asyncio.to_thread(c.list_records, "both", None, None, 10, 0)
                               for _ in range(8)))
        assert c.get_document("d") is not None
        await m.delete_collection("x")
        gone = not m._dir("x").exists()
        await m.create_collection("x", DIM, 4, None, None, None)
        docs = (await m.touch("x")).stats()["documents"]
        await m.shutdown()
        return gone, docs

    assert asyncio.run(run()) == (True, 0)


# ---- R4: copy-on-write indexed_counts ----


def test_indexed_counts_writers_rebind_never_mutate_the_published_dict(tmp_path):
    # readers (search gating, BM25's N, request_index) iterate the dict from other
    # threads; on free-threaded builds an in-place insert races that iteration
    # ("dictionary changed size during iteration"), so writers must publish a new dict
    col = make_collection(tmp_path)

    def chunk(i):
        return {"id": f"c{i}", "text": f"t {i}", "vector": vec(i)}

    asyncio.run(col._process_job({"documents": [
        {"doc_id": "d1", "chunks": [chunk(1)]}, {"doc_id": "d2", "chunks": [chunk(2)]}]}))
    writes = {
        # first summary: a key the dict never had, plus a re-upsert (-1 then +1)
        "upsert": lambda: asyncio.run(col._process_job({"documents": [
            {"doc_id": "d1", "summary": {"text": "s", "vector": vec(9)}, "chunks": [chunk(1)]}]})),
        "delete": lambda: asyncio.run(col.delete_document("d2")),
    }
    for name, write in writes.items():
        published = col.indexed_counts
        before = dict(published)
        write()
        assert published == before, f"{name} mutated the published dict in place"
        assert col.indexed_counts is not published, f"{name} did not publish a new dict"
    assert col.indexed_counts == {"chunk": 1, "summary": 1}


def test_indexed_counts_is_published_only_under_db_lock(tmp_path):
    # plan D moves the publication after the commit: it must stay inside db_lock, or
    # the worker's upsert and a delete_document copy the same base and one rebind
    # drops the other's update (counts drift; search gating skips a needed allowlist)
    col = make_collection(tmp_path)
    seen = []

    class Spy(Collection):
        def __setattr__(self, name, value):
            if name == "indexed_counts":
                seen.append(self.db_lock.locked())
            super().__setattr__(name, value)

    col.__class__ = Spy
    asyncio.run(col._process_job({"documents": [
        {"doc_id": "d1", "chunks": [{"id": "c1", "text": "t", "vector": vec(1)}]}]}))
    asyncio.run(col.delete_document("d1"))
    assert seen == [True, True]  # one publication per write, each under db_lock


def test_no_in_place_indexed_counts_mutation_in_store_source():
    # tripwire for future writers (the ingest path is rewritten by later plans)
    src = Path(store.__file__).read_text(encoding="utf-8")
    assert not re.search(r"indexed_counts\[[^\n]*?\]\s*(=(?!=)|\+=|-=)", src)
    assert not re.search(r"del\s+[\w.]*indexed_counts\[", src)
    assert not re.search(r"indexed_counts\.(update|pop|popitem|setdefault|clear)\(", src)


# ---- mixed-workload stress: a regression guard, checked after quiescence ----

# test-only knobs (not Settings): a longer soak is RAGGIO_STRESS_SECONDS=60
STRESS_SECONDS = float(os.environ.get("RAGGIO_STRESS_SECONDS", "3"))
STRESS_CLIENTS = int(os.environ.get("RAGGIO_STRESS_CLIENTS", "16"))
SEED_DOCS = 300
WORDS = [f"w{i}" for i in range(400)] + ["alpha", "beta", "gamma", "delta", "planet", "star"]
_CLOSED = re.compile(
    r"RuntimeError: collection '\w+' is closed|ProgrammingError: Cannot operate on a closed database"
)


def _text(rng) -> str:
    # Zipf-ish: low word ids are common, so the pruner hits its budget (two-stage BM25)
    return " ".join(WORDS[min(int(rng.zipf(1.3)), len(WORDS) - 1)] for _ in range(rng.integers(8, 40)))


def _doc(rng, i: int) -> dict:
    g = int(rng.integers(0, 30))
    return {
        "doc_id": f"d{i}",
        "summary": {"text": _text(rng), "metadata": {"g": g}} if rng.random() < 0.5 else None,
        "chunks": [
            {"id": f"d{i}c{j}", "position": j, "text": _text(rng), "metadata": {"g": g, "p": j}}
            for j in range(int(rng.integers(1, 5)))
        ],
    }


async def _drain(col: Collection, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while col.pending_jobs():
        assert time.monotonic() < deadline, "ingest jobs did not drain"
        await asyncio.sleep(0.05)


async def _seeded(tmp_path) -> Collection:
    col = make_collection(tmp_path)
    col.start_worker()
    rng = np.random.default_rng(1)
    for s in range(0, SEED_DOCS, 100):
        await col.enqueue({"documents": [_doc(rng, i) for i in range(s, s + 100)]})
    await _drain(col)
    return col


async def _storm(col: Collection, seed: int, stop_after: float | None = None):
    """STRESS_CLIENTS clients for STRESS_SECONDS: searches (15% cancelled mid-flight,
    leaving orphaned to_thread work), upserts and re-upserts, metadata patches,
    deletes, and the unlocked reads (list_records, get_document, stats). With
    stop_after, the collection is stopped mid-storm. Returns (errors, ops)."""
    rng, prng = np.random.default_rng(seed), random.Random(seed)
    emb = FakeEmbedder()
    errors, ops = collections.Counter(), collections.Counter()
    next_doc = SEED_DOCS
    deadline = time.monotonic() + STRESS_SECONDS

    async def search():
        mode = prng.choice(["vector", "vector", "text", "hybrid"])
        q = " ".join(prng.choice(WORDS[:60]) for _ in range(prng.randint(2, 6)))
        qvec = None
        if mode != "text":
            qvec = store._normalize(np.array(await emb.embed([q]), dtype=np.float32))
        expand = None
        if prng.random() < 0.2:
            expand = SimpleNamespace(siblings_topk=prng.choice([None, 3]),
                                     siblings_all=prng.random() < 0.3, summary=prng.random() < 0.5)
        filt = prng.choice([None, {"g": prng.randrange(30)},
                            {"g": {"in": [prng.randrange(30), prng.randrange(30)]}}])
        coro = col.search(mode, qvec, q, prng.choice([5, 10, 50]),
                          prng.choice(["chunks", "summaries", "both"]), filt, expand)
        if prng.random() < 0.15:
            try:
                await asyncio.wait_for(coro, prng.choice([0.0005, 0.002, 0.01]))
            except TimeoutError:  # cancelled; its to_thread body runs on, unlocked
                ops["search_cancelled"] += 1
            return
        await coro
        ops["search"] += 1

    async def write():
        nonlocal next_doc
        r = prng.random()
        if r < 0.45:
            docs = [_doc(rng, next_doc)]
            if prng.random() < 0.5:  # re-upsert an existing doc: the remove + add path
                docs.append(_doc(rng, prng.randrange(next_doc)))
            next_doc += 1
            await col.enqueue({"documents": docs})
        elif r < 0.75:
            await col.patch_metadata(f"d{prng.randrange(next_doc)}", {"g": prng.randrange(30)}, True)
        else:
            await col.delete_document(f"d{prng.randrange(next_doc)}")
        ops["write"] += 1

    async def read():
        r = prng.random()
        if r < 0.5:
            await asyncio.to_thread(
                col.list_records, prng.choice(["chunks", "both"]),
                prng.choice([None, {"g": prng.randrange(30)}]), prng.choice([None, "-g", "p"]),
                20, 0, prng.random() < 0.3,
            )
        elif r < 0.8:
            col.get_document(f"d{prng.randrange(next_doc)}")
        else:
            col.stats()
        ops["read"] += 1

    async def client():
        while time.monotonic() < deadline:
            r = prng.random()
            try:
                await (search() if r < 0.70 else write() if r < 0.85 else read())
            except Exception as e:
                errors[f"{type(e).__name__}: {str(e)[:80]}"] += 1
            await asyncio.sleep(0)

    async def stopper():
        await asyncio.sleep(stop_after)
        await col.stop()
        ops["stopped"] += 1

    await asyncio.wait_for(  # a lock deadlock fails the test instead of hanging the run
        asyncio.gather(*(client() for _ in range(STRESS_CLIENTS)),
                       *([stopper()] if stop_after is not None else [])),
        STRESS_SECONDS + 60)
    return errors, ops


def test_mixed_workload_keeps_index_and_counts_consistent(tmp_path):
    async def run():
        col = await _seeded(tmp_path)
        errors, ops = await _storm(col, seed=7)
        await _drain(col)
        await asyncio.sleep(0.5)  # orphaned to_thread bodies of cancelled searches land
        db = col._rdb()
        indexed = db.execute("SELECT COUNT(*) FROM records WHERE indexed=1").fetchone()[0]
        by_type = dict(db.execute(
            "SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type").fetchall())
        state = len(col.index), indexed, {k: v for k, v in col.indexed_counts.items() if v}, by_type
        await col.stop()
        return errors, ops, state

    errors, ops, (index_len, indexed, counts, by_type) = asyncio.run(run())
    assert errors == {}
    assert ops["search"] and ops["search_cancelled"] and ops["write"] and ops["read"]
    assert index_len == indexed  # every indexed row is in the vector index, nothing else
    assert counts == by_type  # indexed_counts never drifted from the table


def test_stop_under_load_fails_closed_and_closes_every_connection(tmp_path, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kw):
        conn = real_connect(*args, **kw)
        opened.append(conn)
        return conn

    monkeypatch.setattr(store.sqlite3, "connect", spy_connect)

    async def run():
        col = await _seeded(tmp_path)
        out = await _storm(col, seed=11, stop_after=STRESS_SECONDS / 2)
        await asyncio.sleep(0.5)  # orphaned to_thread bodies finish (and fail closed)
        return out

    errors, ops = asyncio.run(run())
    assert ops["stopped"] == 1
    assert errors, "no operation ran against the stopped collection"
    assert {k: v for k, v in errors.items() if not _CLOSED.match(k)} == {}
    for conn in opened:  # the write connection and every per-thread read connection
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


# ---- B1/B2: searches over HTTP at c=16, and racing DELETE /collections/{name} ----


def test_http_searches_at_c16_and_racing_a_collection_delete(tmp_path, monkeypatch):
    # spec 3.1 B2, in process: (1) 16 clients search, half with the collection
    # key: every one 200 (R1: no 401 on a valid key, no 500); (2) 16 clients search
    # while DELETE /collections/x lands behind a writer: 200 before it, 404 after
    # it, never 500 (B1)
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    app = create_app(embedder_factory=lambda cfg: FakeEmbedder())
    key = {"x-api-key": "key-x"}
    inside, release, gate = threading.Event(), threading.Event(), {}
    real_patch = Collection._patch_rows

    def gated_patch(self, *args):  # holds a PATCH inside its write section
        gate["col"] = self
        inside.set()
        release.wait(5)
        return real_patch(self, *args)

    def body(i):
        mode = ("vector", "text", "hybrid")[i % 3]
        q = {} if mode == "vector" else {"text": f"w{i % 7} w{i % 5}"}
        if mode != "text":
            q["vector"] = vec(i)
        return {"query": q, "mode": mode, "k": 5}

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post("/collections", headers=ROOT,
                                 json={"name": "x", "collection_key": "key-x"})
                assert r.status_code == 201, r.text
                docs = [{"doc_id": f"d{i}", "chunks": [
                    {"id": f"d{i}c{j}", "text": f"w{i % 7} w{j}", "vector": vec(10 * i + j)}
                    for j in range(3)]} for i in range(40)]
                r = await c.post("/collections/x/documents", headers=ROOT, json={"documents": docs})
                job = r.json()["job_id"]
                for _ in range(500):
                    r = await c.get(f"/collections/x/jobs/{job}", headers=ROOT)
                    if r.json()["status"] == "done":
                        break
                    await asyncio.sleep(0.01)
                assert r.json()["status"] == "done", r.text
                phase1, phase2 = collections.Counter(), collections.Counter()

                async def client(t):  # phase 1: 20 searches each, alternating keys
                    for n in range(20):
                        hdr, who = (key, "key") if n % 2 else (ROOT, "root")
                        r = await c.post("/collections/x/search", headers=hdr, json=body(t + n))
                        phase1[f"{who}:{r.status_code}"] += 1

                await asyncio.gather(*(client(t) for t in range(16)))

                async def looper(t):  # phase 2: search until the delete reaches us
                    for n in range(10_000):
                        r = await c.post("/collections/x/search", headers=ROOT, json=body(t + n))
                        phase2[r.status_code] += 1
                        if r.status_code != 200:
                            return

                monkeypatch.setattr(Collection, "_patch_rows", gated_patch)
                loopers = [asyncio.create_task(looper(t)) for t in range(16)]
                await asyncio.sleep(0.05)
                patch = asyncio.create_task(c.patch("/collections/x/documents/d0", headers=ROOT,
                                                    json={"metadata": {"g": 1}}))
                assert await asyncio.to_thread(inside.wait, 5)
                await asyncio.sleep(0.1)  # the loopers' next searches queue behind it
                delete = asyncio.create_task(c.delete("/collections/x", headers=ROOT))
                for _ in range(500):  # until stop() waits for the write lock too
                    if gate["col"].lock._writers_waiting:
                        break
                    await asyncio.sleep(0.01)
                queued = bool(gate["col"].lock._writers_waiting)
                release.set()
                assert queued, "stop() never queued for the write lock"
                statuses = [(await patch).status_code, (await delete).status_code]
                await asyncio.gather(*loopers)
                r = await c.post("/collections/x/search", headers=ROOT, json=body(0))
                statuses.append(r.status_code)
        return dict(phase1), sorted(phase2), phase2[404], statuses

    assert asyncio.run(run()) == (
        {"key:200": 160, "root:200": 160}, [200, 404], 16, [200, 200, 404])


# ---- B1: a search that the server embeds, with DELETE landing during the embedding call ----


class GatedEmbedder:
    """embed() parks until released; aclose() releases it. With fail_after_close,
    a parked call then fails the way a closed httpx client does."""

    def __init__(self, fail_after_close: bool):
        self.fail_after_close = fail_after_close
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def embed(self, texts):
        self.entered.set()
        await self.release.wait()
        if self.closed and self.fail_after_close:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        return [vec(0) for _ in texts]

    async def aclose(self):
        self.closed = True
        self.release.set()


@pytest.mark.parametrize("fail_after_close", [True, False])
def test_http_search_embedding_while_delete_lands_answers_404(tmp_path, monkeypatch, fail_after_close):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    emb = GatedEmbedder(fail_after_close)
    app = create_app(embedder_factory=lambda cfg: emb)
    search_body = {"query": {"text": "w1"}, "mode": "hybrid", "k": 5}

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post("/collections", headers=ROOT, json={"name": "x"})
                assert r.status_code == 201, r.text
                docs = [{"doc_id": f"d{i}", "chunks": [
                    {"id": f"d{i}c{j}", "text": f"w{i % 7} w{j}", "vector": vec(10 * i + j)}
                    for j in range(3)]} for i in range(5)]
                r = await c.post("/collections/x/documents", headers=ROOT, json={"documents": docs})
                job = r.json()["job_id"]
                for _ in range(500):
                    r = await c.get(f"/collections/x/jobs/{job}", headers=ROOT)
                    if r.json()["status"] == "done":
                        break
                    await asyncio.sleep(0.01)
                assert r.json()["status"] == "done", r.text
                search = asyncio.create_task(
                    c.post("/collections/x/search", headers=ROOT, json=search_body))
                await asyncio.wait_for(emb.entered.wait(), 5)
                d = await c.delete("/collections/x", headers=ROOT)
                s = await asyncio.wait_for(search, 10)
                after = await c.post("/collections/x/search", headers=ROOT, json=search_body)
        return d.status_code, s.status_code, after.status_code

    assert asyncio.run(run()) == (200, 404, 404)


def test_closed_collection_never_builds_an_embedder(tmp_path):
    calls = []
    col = Collection(CollectionConfig("t", DIM, 4, None, None, None), Path(tmp_path),
                     lambda: calls.append(1) or FakeEmbedder())
    asyncio.run(col.stop())
    with pytest.raises(RuntimeError, match="is closed"):
        col.embedder
    col.deleted = True
    with pytest.raises(CollectionDeletedError):
        col.embedder
    assert calls == []
