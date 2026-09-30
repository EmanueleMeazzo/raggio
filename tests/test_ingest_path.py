"""Plan D (ingest path): the binary job journal, payload work off the event loop,
failure-safe upserts, the vacuum policy, the ingest probe and the batched index sync.
One `# ---- <topic>` section per plan task, in task order."""
import asyncio
import copy
import json
import sqlite3
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, "src")
import raggio.store as store  # noqa: E402
from raggio.store import Collection, CollectionConfig, open_meta_db  # noqa: E402


def make_collection(tmp_path, dim=8, **kw):
    """A collection without an embedder: every ingest payload must carry vectors.
    Extra keywords go to Collection(...)."""
    return Collection(
        CollectionConfig("t", dim, 4, None, None, None), Path(tmp_path), lambda: None, **kw
    )


def vec(seed, dim=8):
    """A seeded float64 vector (standard normal, so never all zero)."""
    return np.random.default_rng(seed).standard_normal(dim).tolist()


def docs_payload(ids, dim=8, with_vectors=True):
    """An ingest payload in IngestIn.model_dump() shape: one document per id, each with
    one chunk, so it makes len(ids) records. with_vectors=False leaves every vector
    None ("embed this")."""
    return {"documents": [
        {"doc_id": f"d{i}", "summary": None, "chunks": [{
            "text": f"text {i}", "vector": vec(i, dim) if with_vectors else None,
            "metadata": {"i": i}, "id": f"c{i}", "position": None,
        }]}
        for i in ids
    ]}


async def drain(col, timeout=30):
    """Wait until no job is open. Re-raises whatever killed the worker, and fails the
    test after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while col.pending_jobs():
        if col._worker is not None and col._worker.done():
            col._worker.result()  # re-raises the worker's exception, if any
            pytest.fail(f"worker exited with {col.pending_jobs()} jobs open")
        assert time.monotonic() < deadline, f"{col.pending_jobs()} jobs open after {timeout} s"
        await asyncio.sleep(0.02)


# ---- binary job journal: codec


def mixed_payload():
    """Every field _process_job's flatten step reads: a summary with a vector, a chunk
    whose vector is None (embed this), an explicit position, nested and None metadata,
    non-ASCII text and awkward float64 values (dim 8)."""
    p = docs_payload([1, 2, 3])
    p["documents"][0]["summary"] = {
        "text": "summary " + chr(0xE9) + chr(0x4E2D), "vector": vec(100), "metadata": {"k": [1, "x"]},
    }
    p["documents"][1]["chunks"].append(
        {"text": "no vector", "vector": None, "metadata": None, "id": "c2b", "position": 7}
    )
    p["documents"][2]["chunks"][0]["vector"] = [0.1, -2.5e-8, 1e30, 3.0000001, 0.0, -0.0, 1.0, 2.0]
    return p


def f4(v):
    """The bytes the worker indexes: _process_job converts every vector to float32."""
    return np.asarray(v, dtype="<f4").tobytes()


def without_vector(rec):
    return {k: v for k, v in rec.items() if k != "vector"}


def test_codec_round_trips_documents_bit_exact():
    payload = mixed_payload()
    out = store._decode_payload(store._encode_payload(payload))
    assert set(out) == set(payload)
    assert len(out["documents"]) == len(payload["documents"])
    for d_in, d_out in zip(payload["documents"], out["documents"]):
        assert d_out["doc_id"] == d_in["doc_id"]
        if d_in["summary"] is None:
            assert d_out["summary"] is None
        else:
            assert without_vector(d_out["summary"]) == without_vector(d_in["summary"])
            assert f4(d_out["summary"]["vector"]) == f4(d_in["summary"]["vector"])
        assert len(d_out["chunks"]) == len(d_in["chunks"])
        for c_in, c_out in zip(d_in["chunks"], d_out["chunks"]):
            assert without_vector(c_out) == without_vector(c_in)
            if c_in["vector"] is None:
                assert c_out["vector"] is None
            else:
                assert f4(c_out["vector"]) == f4(c_in["vector"])
    v = out["documents"][0]["chunks"][0]["vector"]
    assert (v.dtype, v.shape, v.flags.writeable) == (np.dtype("<f4"), (8,), False)


def test_codec_round_trips_index_ops():
    for payload in ({"op": "attach_index", "nlist": 64, "nprobe": None}, {"op": "detach_index"}):
        blob = store._encode_payload(payload)
        magic, json_len, n_vecs, dim = struct.unpack_from("<4sIII", blob)
        assert (magic, n_vecs, dim) == (b"RGJ\x01", 0, 0)
        assert len(blob) == 16 + json_len
        assert store._decode_payload(blob) == payload


def test_codec_header_is_little_endian_f4():
    blob = store._encode_payload(docs_payload(range(5), dim=12))
    magic, json_len, n_vecs, dim = struct.unpack_from("<4sIII", blob)
    assert (magic, n_vecs, dim) == (b"RGJ\x01", 5, 12)
    assert store._JOB_HEADER.size == 16
    assert len(blob) == 16 + json_len + 4 * n_vecs * dim
    meta = json.loads(blob[16 : 16 + json_len].decode("utf-8"))
    assert [d["chunks"][0]["vector"] for d in meta["documents"]] == [0, 1, 2, 3, 4]
    tail = np.frombuffer(blob, dtype="<f4", offset=16 + json_len)
    assert tail.tobytes() == np.asarray([vec(i, 12) for i in range(5)], dtype="<f4").tobytes()
    # a ragged payload has no single dim to put in the header
    ragged = docs_payload([1, 2])
    ragged["documents"][1]["chunks"][0]["vector"] = vec(2, 9)
    with pytest.raises(ValueError):
        store._encode_payload(ragged)


def test_codec_does_not_mutate_its_input():
    payload = mixed_payload()
    before = copy.deepcopy(payload)
    store._encode_payload(payload)
    assert payload == before


@pytest.mark.parametrize("damage", ["bad_magic", "truncated"])
def test_decode_rejects_bad_magic_and_truncation(damage):
    blob = store._encode_payload(docs_payload([1, 2]))
    if damage == "bad_magic":
        cases = [b"RGJ\x02" + blob[4:], b"XXXX" + blob[4:]]
    else:  # cut inside the float block, inside the JSON, inside the header, empty
        cases = [blob[:-1], blob[:40], blob[:10], b""]
    for bad in cases:
        with pytest.raises(ValueError):
            store._decode_payload(bad)


def test_fresh_meta_db_has_job_payloads_table(tmp_path):
    db = open_meta_db(tmp_path / "meta.db")
    cols = [(r[1], r[2], r[5]) for r in db.execute("PRAGMA table_info(job_payloads)")]
    assert cols == [("job_id", "INTEGER", 1), ("data", "BLOB", 0)]  # (name, type, pk)
    db.close()


def test_existing_meta_db_gains_job_payloads_and_keeps_jobs(tmp_path):
    # the layout from before the binary job journal, with a TEXT-JSON job left 'processing' by a crash
    path = tmp_path / "meta.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE records(id INTEGER PRIMARY KEY, external_id TEXT UNIQUE, doc_id TEXT,
            type TEXT CHECK(type IN ('chunk','summary')), position INTEGER, text TEXT,
            metadata TEXT, indexed INTEGER DEFAULT 0);
        CREATE TABLE vecs(id INTEGER PRIMARY KEY, vec BLOB);
        CREATE INDEX idx_records_doc ON records(doc_id);
        CREATE TABLE jobs(id INTEGER PRIMARY KEY, payload TEXT, status TEXT, error TEXT,
            created_at TEXT, updated_at TEXT);
        """
    )
    text = json.dumps(docs_payload([1]))
    legacy.execute(
        "INSERT INTO jobs(id, payload, status, error, created_at, updated_at)"
        " VALUES (7, ?, 'processing', NULL, 't0', 't0')", (text,),
    )
    legacy.commit()
    legacy.close()
    db = open_meta_db(path)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_payloads" in tables
    assert db.execute("SELECT id, payload, status, error FROM jobs").fetchall() == [
        (7, text, "processing", None)
    ]
    assert db.execute("SELECT COUNT(*) FROM job_payloads").fetchone()[0] == 0
    db.close()


# ---- binary job journal: queue

# what an enqueue from before the binary job journal left: the payload as JSON TEXT
LEGACY_JOB = (
    "INSERT INTO jobs(id, payload, status, created_at, updated_at) VALUES (?, ?, ?, '', '')"
)


def fetch(col, sql, *args):
    """Committed rows, read on the calling thread's read connection (before stop())."""
    return col._rdb().execute(sql, args).fetchall()


def test_enqueue_writes_job_and_payload_atomically(tmp_path):
    col = make_collection(tmp_path)

    async def run():
        try:
            job_id = await col.enqueue(docs_payload([1, 2]))
            assert fetch(col, "SELECT payload, status FROM jobs WHERE id=?", job_id) == [
                (None, "pending")
            ]
            [(data,)] = fetch(col, "SELECT data FROM job_payloads WHERE job_id=?", job_id)
            assert data[:4] == store._JOB_MAGIC
            assert [d["doc_id"] for d in store._decode_payload(data)["documents"]] == ["d1", "d2"]
            # the payload INSERT fails after the jobs INSERT: both must roll back together
            with col.db_lock:
                col.db.execute(
                    "CREATE TEMP TRIGGER boom BEFORE INSERT ON job_payloads"
                    " BEGIN SELECT RAISE(ABORT, 'boom'); END"
                )
            with pytest.raises(sqlite3.IntegrityError, match="boom"):
                await col.enqueue(docs_payload([3]))
            assert not col.db.in_transaction  # nothing left for the next commit to persist
            assert fetch(col, "SELECT id FROM jobs") == [(job_id,)]
            assert fetch(col, "SELECT job_id FROM job_payloads") == [(job_id,)]
            with col.db_lock:
                col.db.execute("DROP TRIGGER boom")
            assert await col.enqueue(docs_payload([3])) == job_id + 1
            assert fetch(col, "SELECT job_id FROM job_payloads ORDER BY job_id") == [
                (job_id,), (job_id + 1,)
            ]
        finally:
            await col.stop()

    asyncio.run(run())


def test_enqueue_encodes_before_taking_db_lock(tmp_path, monkeypatch):
    # encoding a 250x1024 job is ~10 ms of CPU: under db_lock it would stall every
    # other writer (the worker's claim and finish, concurrent enqueues)
    col = make_collection(tmp_path)
    real, held = store._encode_payload, []

    def spy(payload):
        held.append(col.db_lock.locked())
        return real(payload)

    monkeypatch.setattr(store, "_encode_payload", spy)

    async def run():
        try:
            await col.enqueue(docs_payload([1]))
        finally:
            await col.stop()

    asyncio.run(run())
    assert held == [False]


def test_claim_returns_decoded_payload(tmp_path):
    col = make_collection(tmp_path)

    async def run():
        try:
            job_id = await col.enqueue(docs_payload([1, 2]))
            row = await asyncio.to_thread(col._claim_next)
            return job_id, row, fetch(col, "SELECT status FROM jobs WHERE id=?", job_id)
        finally:
            await col.stop()

    job_id, row, status = asyncio.run(run())
    assert len(row) == 3
    claimed, payload, bad = row
    assert (claimed, bad, status) == (job_id, None, [("processing",)])
    assert [d["doc_id"] for d in payload["documents"]] == ["d1", "d2"]
    v = payload["documents"][1]["chunks"][0]["vector"]
    assert (v.dtype, v.shape) == (np.dtype("<f4"), (8,))
    assert v.tobytes() == np.asarray(vec(2), dtype=np.float32).tobytes()


def test_mixed_legacy_and_binary_queue_in_order(tmp_path):
    # an in-place upgrade: TEXT-JSON jobs from the old release (one left 'processing'
    # by a crash, one still 'pending') around binary jobs journaled by this one (one of
    # them also left 'processing' by the crash)
    first = make_collection(tmp_path)

    async def journal():
        try:
            with first.db_lock:
                first.db.execute(LEGACY_JOB, (1, json.dumps(docs_payload([1])), "processing"))
                first.db.commit()
            assert await first.enqueue(docs_payload([2])) == 2
            assert await first.enqueue(docs_payload([3])) == 3
            with first.db_lock:
                first.db.execute(LEGACY_JOB, (4, json.dumps(docs_payload([4])), "pending"))
                first.db.execute("UPDATE jobs SET status = 'processing' WHERE id = 3")
                first.db.commit()
            return fetch(first, "SELECT job_id FROM job_payloads ORDER BY job_id")
        finally:
            await first.stop()

    assert asyncio.run(journal()) == [(2,), (3,)]
    col = make_collection(tmp_path)  # the reboot replays the whole journal
    seen = []
    real = col._process_job

    async def spy(payload, **kw):  # **kw: forwards any keyword arguments _process_job takes
        seen.append([d["doc_id"] for d in payload["documents"]])
        await real(payload, **kw)

    col._process_job = spy

    async def replay():
        col.start_worker()
        try:
            await drain(col)
            return (
                fetch(col, "SELECT id, status, error, payload FROM jobs ORDER BY id"),
                fetch(col, "SELECT COUNT(*) FROM job_payloads"),
                fetch(col, "SELECT external_id FROM records ORDER BY id"),
            )
        finally:
            await col.stop()

    jobs, payload_rows, records = asyncio.run(replay())
    assert seen == [["d1"], ["d2"], ["d3"], ["d4"]]
    assert jobs == [(i, "done", None, None) for i in (1, 2, 3, 4)]
    assert payload_rows == [(0,)]
    assert records == [("c1",), ("c2",), ("c3",), ("c4",)]  # record ids follow job order


def test_error_job_keeps_payload_row(tmp_path):
    col = make_collection(tmp_path)
    zero = docs_payload([2])
    zero["documents"][0]["chunks"][0]["vector"] = [0.0] * 8  # _normalize rejects it

    async def run():
        col.start_worker()
        try:
            for p in (docs_payload([1]), zero, docs_payload([3])):
                await col.enqueue(p)
            await drain(col)
            return (
                fetch(col, "SELECT id, status, error FROM jobs ORDER BY id"),
                fetch(col, "SELECT job_id, data FROM job_payloads ORDER BY job_id"),
            )
        finally:
            await col.stop()

    jobs, kept = asyncio.run(run())
    assert [(i, s) for i, s, _ in jobs] == [(1, "done"), (2, "error"), (3, "done")]
    assert "zero vector" in jobs[1][2]
    assert [job_id for job_id, _ in kept] == [2]  # kept for diagnosis; done rows are gone
    assert store._decode_payload(kept[0][1])["documents"][0]["doc_id"] == "d2"


@pytest.mark.parametrize("damage", ["bad_magic", "truncated", "no_payload"])
def test_bad_payload_becomes_an_error_job(tmp_path, damage):
    col = make_collection(tmp_path)
    blob = store._encode_payload(docs_payload([1]))
    reason = {"bad_magic": "bad magic", "truncated": "truncated", "no_payload": "no payload"}

    async def run():
        assert await col.enqueue(docs_payload([1])) == 1
        assert await col.enqueue(docs_payload([2])) == 2
        with col.db_lock:
            if damage == "no_payload":  # jobs.payload is NULL too: nothing left to decode
                col.db.execute("DELETE FROM job_payloads WHERE job_id=1")
            else:
                bad = b"XXXX" + blob[4:] if damage == "bad_magic" else blob[:-3]
                col.db.execute(
                    "INSERT OR REPLACE INTO job_payloads(job_id, data) VALUES (1, ?)", (bad,)
                )
            col.db.commit()
        col.start_worker()
        try:
            await drain(col)
            return (
                not col._worker.done(),
                fetch(col, "SELECT id, status, error FROM jobs ORDER BY id"),
                fetch(col, "SELECT external_id FROM records"),
            )
        finally:
            await col.stop()

    alive, jobs, records = asyncio.run(run())
    assert [(i, s) for i, s, _ in jobs] == [(1, "error"), (2, "done")]
    assert jobs[0][2].startswith("bad job payload: ")
    assert reason[damage] in jobs[0][2]
    assert alive  # a bad row is an error job, never a dead worker
    assert records == [("c2",)]


def test_payload_codec_runs_off_the_event_loop(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    calls = {"encode": [], "decode": []}
    real_encode, real_decode = store._encode_payload, store._decode_payload

    def encode(payload):
        calls["encode"].append(threading.get_ident())
        return real_encode(payload)

    def decode(data):
        calls["decode"].append(threading.get_ident())
        return real_decode(data)

    monkeypatch.setattr(store, "_encode_payload", encode)
    monkeypatch.setattr(store, "_decode_payload", decode)

    async def run():
        col.start_worker()
        try:
            for i in range(3):
                await col.enqueue(docs_payload([i]))
            await drain(col)
        finally:
            await col.stop()
        return threading.get_ident()

    loop_thread = asyncio.run(run())
    assert (len(calls["encode"]), len(calls["decode"])) == (3, 3)
    assert loop_thread not in calls["encode"] + calls["decode"]


# ---- upsert ordering and rollback


def index_ids(col):
    """Every id in a flat collection's vector index, sorted: a search whose k is the
    index size returns them all (the probe _reconcile_ghosts uses)."""
    if not len(col.index):
        return []
    probe = np.zeros((1, col.cfg.dim), dtype=np.float32)
    probe[0, 0] = 1.0
    return sorted(int(i) for i in col.index.search(probe, k=len(col.index))[1][0])


def index_state(col):
    """What a failed write must leave untouched: the committed records and vecs, the
    vector index's ids and the published indexed_counts."""
    return (
        fetch(col, "SELECT id, external_id FROM records ORDER BY id"),
        fetch(col, "SELECT id FROM vecs ORDER BY id"),
        index_ids(col),
        dict(col.indexed_counts),
    )


def test_upsert_failure_rolls_back_and_keeps_index(tmp_path):
    # before: a failure mid-upsert had already removed the replaced ids from the index
    # and left the transaction open, and the worker's own _finish_job('error') commit
    # then persisted the half-applied upsert under a terminal job that never replays
    col = make_collection(tmp_path)

    async def run():
        col.start_worker()
        try:
            await col.enqueue(docs_payload([1, 2, 3]))  # job 1: records 1, 2, 3
            await drain(col)
            before = index_state(col)
            with col.db_lock:  # TEMP: only col.db (the worker's upserts) fires it
                col.db.execute(
                    "CREATE TEMP TRIGGER boom BEFORE INSERT ON vecs WHEN NEW.id = 5"
                    " BEGIN SELECT RAISE(ABORT, 'boom'); END"
                )
            # job 2 replaces all three rows with ids 4, 5, 6: the second vec INSERT aborts
            await col.enqueue(docs_payload([1, 2, 3]))
            await drain(col)
            # read after job 2's _finish_job('error') committed: nothing partial rode on it
            after, in_txn = index_state(col), col.db.in_transaction
            with col.db_lock:
                col.db.execute("DROP TRIGGER boom")
            await col.enqueue(docs_payload([1, 2, 3]))  # job 3: the same upsert succeeds
            await drain(col)
            final = index_state(col)
            # an orphan vecs row at the next record id, as an older release's backfill
            # could leave for a record deleted mid-embed: job 4 must replace it, not fail
            orphan = fetch(col, "SELECT MAX(id) + 1 FROM records")[0][0]
            with col.db_lock:
                col.db.execute("INSERT INTO vecs(id, vec) VALUES (?, ?)", (orphan, bytes(16)))
                col.db.commit()
            await col.enqueue(docs_payload([4]))  # job 4: record 7, over the orphan
            await drain(col)
            jobs = fetch(col, "SELECT id, status, error FROM jobs ORDER BY id")
            blob = fetch(col, "SELECT vec FROM vecs WHERE id = ?", orphan)
            return before, after, in_txn, jobs, final, orphan, blob, index_state(col)
        finally:
            await col.stop()

    before, after, in_txn, jobs, final, orphan, blob, healed = asyncio.run(run())
    assert before == ([(1, "c1"), (2, "c2"), (3, "c3")], [(1,), (2,), (3,)], [1, 2, 3], {"chunk": 3})
    assert jobs[:3] == [(1, "done", None), (2, "error", "boom"), (3, "done", None)]
    assert after == before  # records, vecs, index ids and counts all unchanged
    assert in_txn is False
    # the rollback left MAX(id) at 3, so job 3's rows are 4, 5, 6
    assert final == ([(4, "c1"), (5, "c2"), (6, "c3")], [(4,), (5,), (6,)], [4, 5, 6], {"chunk": 3})
    # job 4's record 7 replaced the orphan: one vec per record, holding its own vector
    assert (orphan, jobs[3]) == (7, (4, "done", None))
    unit = store._normalize(np.array([vec(4)], dtype=np.float32))
    assert blob == [(unit.astype(np.float16).tobytes(),)]
    assert healed == (
        [(4, "c1"), (5, "c2"), (6, "c3"), (7, "c4")], [(4,), (5,), (6,), (7,)], [4, 5, 6, 7], {"chunk": 4}
    )
    reopened = make_collection(tmp_path)  # what reached disk: no partial rows, no ghosts
    try:
        assert index_state(reopened) == healed
    finally:
        asyncio.run(reopened.stop())


def test_delete_failure_rolls_back_and_keeps_index(tmp_path):
    # before: delete_document removed the ids from the index first, and a failing
    # DELETE left the vecs deletion in an open transaction for the next commit
    col = make_collection(tmp_path)

    async def run():
        try:
            await col._process_job(docs_payload([1, 2]))  # records 1 (d1), 2 (d2)
            before = index_state(col)
            with col.db_lock:
                col.db.execute(
                    "CREATE TEMP TRIGGER boom BEFORE DELETE ON records"
                    " BEGIN SELECT RAISE(ABORT, 'boom'); END"
                )
            with pytest.raises(sqlite3.IntegrityError, match="boom"):
                await col.delete_document("d1")
            in_txn = col.db.in_transaction
            await col.enqueue(docs_payload([9]))  # a later writer commits on col.db
            after = index_state(col)
            with col.db_lock:
                col.db.execute("DROP TRIGGER boom")
            deleted = await col.delete_document("d1")
            return before, in_txn, after, deleted, index_state(col)
        finally:
            await col.stop()

    before, in_txn, after, deleted, final = asyncio.run(run())
    assert before == ([(1, "c1"), (2, "c2")], [(1,), (2,)], [1, 2], {"chunk": 2})
    assert (in_txn, after) == (False, before)
    assert deleted == 1
    assert final == ([(2, "c2")], [(2,)], [2], {"chunk": 1})


# a cancelled delete releases lock.write() while its thread runs on, so the ids must
# leave the index before db_lock lets the next upsert reuse them
def test_delete_unindexes_before_releasing_db_lock(tmp_path):
    col = make_collection(tmp_path)
    real, held = col._unindex, []

    def spy(rids):
        held.append(col.db_lock.locked())
        return real(rids)

    col._unindex = spy

    async def run():
        try:
            await col._process_job(docs_payload([1, 2]))  # records 1 (d1), 2 (d2)
            deleted = await col.delete_document("d1")
            return deleted, index_ids(col)
        finally:
            await col.stop()

    deleted, ids = asyncio.run(run())
    assert deleted == 1
    assert held == [True]
    assert ids == [2]


def test_indexed_counts_published_after_commit(tmp_path):
    # the rebind used to run inside the open transaction; now it runs after the commit, still
    # under db_lock and still once per write, so no reader ever sees counts for rows a
    # rollback then discards
    col = make_collection(tmp_path)
    seen = []

    class Spy(Collection):
        def __setattr__(self, name, value):
            if name == "indexed_counts":
                seen.append((self.db_lock.locked(), self.db.in_transaction))
            super().__setattr__(name, value)

    col.__class__ = Spy

    async def run():
        try:
            await col._process_job(docs_payload([1, 2]))
            await col._process_job(docs_payload([2, 3]))  # replaces c2: -1 then +1
            await col.delete_document("d1")
            return dict(col.indexed_counts)
        finally:
            await col.stop()

    counts = asyncio.run(run())
    assert seen == [(True, False)] * 3  # one rebind per write: under db_lock, committed
    assert counts == {"chunk": 2}


def test_upsert_old_row_lookup_is_set_based(tmp_path):
    # before: one `WHERE external_id=?` point query per row, all under db_lock
    col = make_collection(tmp_path)
    statements = []

    async def run():
        try:
            await col._process_job(docs_payload(range(300)))  # records 1..300
            with col.db_lock:
                col.db.set_trace_callback(statements.append)
            await col._process_job(docs_payload(range(300)))  # replaces all 300
            with col.db_lock:
                col.db.set_trace_callback(None)
            return (
                fetch(col, "SELECT COUNT(*), MIN(id), MAX(id) FROM records"),
                fetch(col, "SELECT COUNT(*) FROM vecs"),
                index_ids(col),
                dict(col.indexed_counts),
            )
        finally:
            await col.stop()

    records, vecs, ids, counts = asyncio.run(run())
    # the trace shows expanded SQL (external_id='c7'), so match on the spaceless text
    flat = [s.replace(" ", "") for s in statements]
    assert sum("WHEREexternal_id=" in s for s in flat) == 0
    assert sum("WHEREexternal_idIN(" in s for s in flat) == 1  # 300 ids: one 512-id chunk
    assert records == [(300, 301, 600)]
    assert vecs == [(300,)]
    assert ids == list(range(301, 601))
    assert counts == {"chunk": 300}


def test_ingest_prep_runs_off_the_event_loop(tmp_path, monkeypatch):
    # np.array over a 250x1024 job's nested float lists is ~13 ms of CPU, plus the
    # flatten loop: on the loop it stalled every request the process served meanwhile
    col = make_collection(tmp_path)
    real_rows, real_matrix = store._payload_rows, store._rows_matrix
    calls = {"rows": [], "matrix": []}

    def rows_spy(payload):
        calls["rows"].append(threading.get_ident())
        return real_rows(payload)

    def matrix_spy(rows):
        calls["matrix"].append(threading.get_ident())
        return real_matrix(rows)

    monkeypatch.setattr(store, "_payload_rows", rows_spy)
    monkeypatch.setattr(store, "_rows_matrix", matrix_spy)

    class FakeEmbedder:  # embeds the chunks that arrive without a vector
        def __init__(self):
            self.texts = []

        async def embed(self, texts):
            self.texts += texts
            return [vec(100 + n) for n in range(len(texts))]

        async def aclose(self):
            pass

    col._embedder = embedder = FakeEmbedder()  # the collection's lazily built embedder

    async def run():
        col.start_worker()
        try:
            for i in range(2):
                await col.enqueue(docs_payload([i, i + 10]))
            # one job mixing a supplied vector with one the worker must embed
            mixed = docs_payload([2])
            mixed["documents"] += docs_payload([12], with_vectors=False)["documents"]
            await col.enqueue(mixed)
            await drain(col)
            return (
                threading.get_ident(),
                fetch(col, "SELECT COUNT(*) FROM records"),
                fetch(col, "SELECT external_id FROM records JOIN vecs USING (id) ORDER BY id"),
                index_ids(col),
            )
        finally:
            await col.stop()

    loop_thread, records, stored, indexed = asyncio.run(run())
    assert (len(calls["rows"]), len(calls["matrix"])) == (3, 3)
    assert loop_thread not in calls["rows"] + calls["matrix"]
    assert records == [(6,)]
    assert embedder.texts == ["text 12"]  # only the vectorless chunk was embedded
    assert stored == [("c0",), ("c10",), ("c1",), ("c11",), ("c2",), ("c12",)]
    assert indexed == [1, 2, 3, 4, 5, 6]  # both kinds reached the vector index


# ---- vacuum policy


def big_payload(job, dim=8):
    """One document of 50 chunks of 5000 characters: about 62 pages of job_payloads
    data at SQLite's default 4 KiB page size, all freed when the job finishes done."""
    return {"documents": [{"doc_id": f"v{job}", "summary": None, "chunks": [
        {"text": "x" * 5000, "vector": vec(j, dim), "metadata": None,
         "id": f"v{job}-{j}", "position": None}
        for j in range(50)
    ]}]}


def test_vacuum_is_deferred_while_jobs_are_open(tmp_path):
    # before: every _finish_job ran a full incremental_vacuum, even with a backlog
    # queued behind it. Now only the finish that leaves the queue empty does. The
    # default VACUUM_FREELIST_PAGES (16_384) is far above five jobs' ~310 pages, so no
    # trimming vacuum runs either
    col = make_collection(tmp_path)
    try:
        ids = [col._enqueue_row(big_payload(n)) for n in range(5)]
        traced = []
        col.db.set_trace_callback(traced.append)
        try:
            vacuums = []
            for job_id in ids:
                traced.clear()
                col._finish_job(job_id, "done", None)
                vacuums.append([s for s in traced if "incremental_vacuum" in s])
        finally:
            col.db.set_trace_callback(None)
        assert vacuums == [[], [], [], [], ["PRAGMA incremental_vacuum"]]
        assert not col.db.in_transaction
        assert col.db.execute("PRAGMA freelist_count").fetchone()[0] <= 1
        assert fetch(col, "SELECT COUNT(*) FROM job_payloads") == [(0,)]
    finally:
        asyncio.run(col.stop())


def test_freelist_bounded_during_backlog(tmp_path, monkeypatch):
    # deferring the full vacuum must not let a backlog grow meta.db without bound: a
    # finish that sees VACUUM_FREELIST_PAGES or more free pages trims the freelist back
    # below it, however many pages one job freed (~62 here, against a chunk of 16)
    monkeypatch.setattr(store, "VACUUM_FREELIST_PAGES", 64)
    monkeypatch.setattr(store, "VACUUM_CHUNK_PAGES", 16)
    col = make_collection(tmp_path)
    try:
        ids = [col._enqueue_row(big_payload(n)) for n in range(20)]
        # the backlog's payloads span far more pages than the threshold (~1240 vs 64)
        [(total,)] = fetch(col, "SELECT SUM(LENGTH(data)) FROM job_payloads")
        assert total > 20 * 50 * 5000
        free = []
        for job_id in ids:
            col._finish_job(job_id, "done", None)
            free.append(col.db.execute("PRAGMA freelist_count").fetchone()[0])
        backlog, idle = free[:-1], free[-1]
        assert max(backlog) <= 64, backlog  # bounded while jobs are open ...
        assert max(backlog) > 1, backlog  # ... without a full vacuum at every finish
        assert idle <= 1  # the finish that empties the queue vacuums in full
    finally:
        asyncio.run(col.stop())


# ---- bench/ingest_probe.py

PROBE = Path(__file__).resolve().parents[1] / "bench" / "ingest_probe.py"


def load_probe(monkeypatch):
    """Import bench/ingest_probe.py as a module. The probe puts its tree's src/ first
    on sys.path at import; patching in a copy of sys.path undoes that after the test."""
    import importlib.util

    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("ingest_probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def probe_args(tmp_path, **over):
    """argv for a tiny probe run: dim 16, 200 prefill rows, 4 jobs x 20 rows, job 0
    re-upserting prefill rows 0-19. Keywords override a flag ('_' for '-')."""
    opts = {"dim": 16, "prefill": 200, "jobs": 4, "job_rows": 20, "reupsert_every": 5,
            "enqueuers": 2, "seed": 1, "tmp": tmp_path, **over}
    argv = []
    for name, value in opts.items():
        argv += ["--" + name.replace("_", "-"), str(value)]
    return argv


def test_ingest_probe_runs_flat_and_reports(tmp_path, monkeypatch):
    probe = load_probe(monkeypatch)
    assert sys.path[0] == str(PROBE.parents[1] / "src")  # measures its own tree's code
    out_file = tmp_path / "probe.json"
    out = probe.main(probe_args(tmp_path, out=out_file))
    assert {
        "ingest_vps", "drain_s", "enqueue_s", "loop_stall_s", "loop_lag_max_ms", "rows",
        "jobs", "freelist", "fingerprint", "store_file", "sqlite_version", "turbovec", "args",
    } <= set(out)
    assert out["jobs"] == {"done": 4}
    assert out["rows"] == 80  # 4 jobs x 20 rows, the re-upsert included
    assert out["index"] == {"type": "flat"}
    # 200 prefill rows + 3 fresh jobs x 20; job 0 replaced 20 prefill rows in place
    assert out["fingerprint"]["counts"] == {"chunk": 260}
    assert out["fingerprint"]["n_index"] == 260
    assert out["freelist"] <= 1  # the queue went idle, so the last finish vacuumed in full
    assert out["payload_rows"] == 0  # a done job's job_payloads row is deleted
    assert out["ingest_vps"] == pytest.approx(80 / out["drain_s"])
    assert 0 < out["enqueue_s"] <= out["drain_s"]
    assert out["loop_stall_s"] >= 0 and out["loop_lag_max_ms"] >= 0
    assert out["store_file"] == store.__file__
    assert out["sqlite_version"] == sqlite3.sqlite_version
    assert out["regime"] == "host-warm, uncapped host process"
    assert (out["args"]["seed"], out["args"]["ivf"], out["args"]["job_rows"]) == (1, 0, 20)
    assert json.loads(out_file.read_text(encoding="utf-8")) == out


def test_ingest_probe_runs_ivf(tmp_path, monkeypatch):
    # bench.py never ingests into an IVF collection; the probe attaches one (nlist 4)
    # before the clock starts, so every timed job syncs the IVF shards
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)
    probe = load_probe(monkeypatch)
    out = probe.main(probe_args(tmp_path, prefill=400, ivf=4, seed=3))
    assert out["index"]["type"] == "ivf" and out["index"]["nlist"] == 4
    assert out["jobs"] == {"done": 4}
    assert out["rows"] == 80
    assert out["fingerprint"]["counts"] == {"chunk": 460}  # 400 + 3 fresh jobs x 20
    assert out["fingerprint"]["n_index"] == 460
    assert out["payload_rows"] == 0
    assert out["ingest_vps"] > 0


def test_ingest_probe_fingerprint_is_deterministic(tmp_path, monkeypatch):
    # three enqueuers race to land six jobs (jobs 0 and 3 re-upsert disjoint prefill
    # blocks), so the journal order differs between runs; the final state must not
    probe = load_probe(monkeypatch)
    shape = {"prefill": 100, "jobs": 6, "job_rows": 10, "reupsert_every": 3, "enqueuers": 3}
    a = probe.main(probe_args(tmp_path, seed=7, **shape))
    b = probe.main(probe_args(tmp_path, seed=7, **shape))
    c = probe.main(probe_args(tmp_path, seed=8, **shape))
    assert a["jobs"] == b["jobs"] == c["jobs"] == {"done": 6}
    assert a["fingerprint"] == b["fingerprint"]
    assert a["fingerprint"]["counts"] == {"chunk": 140}  # 100 + 4 fresh jobs x 10
    assert a["fingerprint"]["n_index"] == 140
    # the fingerprint does see the data: another seed gives other texts and vectors
    assert c["fingerprint"]["records"] != a["fingerprint"]["records"]
    assert c["fingerprint"]["vecs"] != a["fingerprint"]["vecs"]
