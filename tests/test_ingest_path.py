"""Plan D (ingest path): the binary job journal, payload work off the event loop,
failure-safe upserts, the vacuum policy, the ingest probe and the batched index sync.
One `# ---- <topic>` section per plan task, in task order."""
import asyncio
import copy
import json
import logging
import shutil
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
from raggio.config import Settings  # noqa: E402
from raggio.store import Collection, CollectionConfig, CollectionManager, open_meta_db  # noqa: E402

# the deadline of each wait for something that must happen. Generous on purpose: with
# every CPU busy, a thread hand-off on the free-threaded build can take a second or more
# (a contended PyMutex yields the CPU up to 40 times before it parks); one claim-cursor
# test took 20.7-37.1 s under 6 busy loops on a 6-core laptop (0.37 s unloaded)
WAIT_SECONDS = 360


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


async def drain(col, timeout=WAIT_SECONDS):
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
    with col._reading() as db:
        return db.execute(sql, args).fetchall()


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
    # before the clock starts, so the timed jobs' syncs write the IVF shards
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


# ---- ADR 0001 addendum: D1 DGX results

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0001-performance-optimization-decisions.md"
D1_RESULTS = "### Results " + chr(0x2014) + " DGX A/B (D1)"


def adr_d1_results():
    """(the D1 results subsection with \\r\\n normalized, its Measurements JSON or {}).
    The subsection ends at the next ### or ## heading: Task 10 appends D2's after it."""
    text = ADR.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index(D1_RESULTS)
    ends = [i for i in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if i != -1]
    section = text[start:min(ends, default=len(text))]
    fence = "`" * 3
    if "#### Measurements (D1)" not in section or fence + "json\n" not in section:
        return section, {}
    raw = section.split(fence + "json\n", 1)[1].split("\n" + fence, 1)[0]
    return section, json.loads(raw)


def test_adr_ingest_path_d1_results_record_the_gates():
    """Spec section 7 D on gn100: ingest vec/s beyond the noise band on the flat path,
    every job done with no payload row left, and one final state across arms. The IVF
    gain fell within its band: the record keeps that FAIL and the maintainer's override."""
    import statistics

    section, m = adr_d1_results()
    assert "Pending:" not in section
    assert "#### Measurements (D1)" in section and m, "no Measurements (D1) JSON block"
    assert {"date", "base_sha", "cand_sha", "probe", "bench", "labels", "gates"} <= set(m)
    assert m["base_sha"] != m["cand_sha"]

    def beyond_band(base, cand, floor=1.0):  # Plan A's rule: gain > max(bands, resolution)
        band = max(max(base) - min(base), max(cand) - min(cand), floor)
        return statistics.median(cand) - statistics.median(base) > band

    for mode in ("flat", "ivf256"):
        p = m["probe"][mode]
        assert len(p["base"]) == 3 and len(p["cand"]) == 3, mode
        assert p["fingerprints_equal"] and p["jobs_all_done"], mode
        assert set(p["payload_rows"]["base"]) == {None}, mode  # the base has no side table
        assert set(p["payload_rows"]["cand"]) == {0}, mode  # D1 leaves no payload row
    flat, ivf = m["probe"]["flat"], m["probe"]["ivf256"]
    assert beyond_band(flat["base"], flat["cand"]), "probe flat: the gain is within the band"
    assert flat["verdict"] == "better" and "- PASS: D1-flat\n" in section
    # D1-ivf256 failed as written (gain within the band) and the maintainer overrode it on
    # 2026-10-01: every cand run beat every base run, and one fast cand run sets the band
    assert not beyond_band(ivf["base"], ivf["cand"]) and ivf["verdict"] == "within band"
    assert min(ivf["cand"]) > max(ivf["base"])
    assert m["gates"]["D1-ivf256"] == "FAIL" and set(m.get("overrides", {})) == {"D1-ivf256"}
    assert "- FAIL: D1-ivf256\n" in section
    assert "- OVERRIDE: D1-ivf256, by the maintainer on 2026-10-01" in section
    lp = m["labels"]["probe"]
    assert lp["regime"] == ["host-warm, uncapped host process"] and lp["cap"]
    assert lp["openblas_num_threads"] == ["1"] and len(lp["sqlite_version"]) == 1
    b = m["bench"]
    if b is None:  # probe-only window, or not enough time left for the reingests
        assert "Not run: the bench reingest" in section and "- SKIP: D1-bench\n" in section
    else:
        assert len(b["base"]) == 2 and len(b["cand"]) == 2
        assert beyond_band(b["base"], b["cand"]) and b["verdict"] == "better"
        assert len(b["runs"]) == 4
        for name, run in b["runs"].items():
            assert set(run["jobs"]) == {"done"}, name  # no error, pending or processing job
            assert run["payload_rows"] == (None if name.startswith("base") else 0), name
        lb = m["labels"]["bench"]
        assert lb["regime"] == ["host-warm"] and lb["cap"] == ["4g"]
        assert lb["openblas_num_threads"] == ["1"]
        assert all(len(v) == 1 for v in lb["sqlite_version"].values())
        assert "- PASS: D1-bench\n" in section
    for gate in ("D1-run", "D1-fingerprints", "D1-jobs", "D1-labels"):
        assert f"- PASS: {gate}\n" in section, gate
    assert [line for line in section.splitlines() if line.startswith("- FAIL:")] == ["- FAIL: D1-ivf256"]


# ---- batched sync: plumbing


def test_settings_sync_batch_knobs(monkeypatch):
    monkeypatch.delenv("SYNC_BATCH_JOBS", raising=False)
    monkeypatch.delenv("SYNC_BATCH_MS", raising=False)
    s = Settings()
    assert (s.sync_batch_jobs, s.sync_batch_ms) == (8, 1000.0)
    assert type(s.sync_batch_jobs) is int and type(s.sync_batch_ms) is float
    monkeypatch.setenv("SYNC_BATCH_JOBS", "3")
    monkeypatch.setenv("SYNC_BATCH_MS", "250.5")
    s = Settings()
    assert (s.sync_batch_jobs, s.sync_batch_ms) == (3, 250.5)


@pytest.mark.parametrize("name, value", [("SYNC_BATCH_JOBS", "0"), ("SYNC_BATCH_MS", "-1")])
def test_settings_rejects_bad_sync_batch_knobs(monkeypatch, name, value):
    # a batch of zero jobs could never close, and a negative wait is a typo: refuse to boot
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings()


def test_manager_passes_sync_batch_knobs_to_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_BATCH_JOBS", "3")
    monkeypatch.setenv("SYNC_BATCH_MS", "250")

    async def go():
        mgr = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        try:
            await mgr.create_collection("m", 8, 4, None, None, None)
            col = await mgr.touch("m")
            return col.sync_batch_jobs, col.sync_batch_ms
        finally:
            await mgr.shutdown()

    assert asyncio.run(go()) == (3, 250.0)


def test_claim_cursor_skips_already_claimed_jobs(tmp_path):
    # the batched worker claims job k+1 while job k is still 'processing' (a job is
    # finished only after its batch's sync), so the cursor, not the status, moves on
    col = make_collection(tmp_path)
    try:
        assert [col._enqueue_row(docs_payload([i])) for i in (1, 2, 3)] == [1, 2, 3]
        assert col._claim_next()[0] == 1  # no cursor: the lowest open job, as before
        assert col._claim_next(1)[0] == 2
        job_id, payload, error = col._claim_next(2)
        assert (job_id, payload["documents"][0]["doc_id"], error) == (3, "d3", None)
        assert col._claim_next(3) is None  # nothing open above the cursor
        assert fetch(col, "SELECT id, status FROM jobs ORDER BY id") == [
            (1, "processing"), (2, "processing"), (3, "processing")]
        # cursor 0 (a worker's first claim, as on boot): 'processing' jobs replay first
        assert col._claim_next()[0] == 1
    finally:
        asyncio.run(col.stop())


def test_claim_query_never_scans_the_journal(tmp_path):
    # the journal keeps every done job. The INDEXED BY hint is there so a planner change
    # cannot silently turn the claim into a walk over all the done rows (cursor 0): the
    # walk Plan C's partial index idx_jobs_open exists to avoid
    col = make_collection(tmp_path)
    try:
        with col.db_lock:
            col.db.executemany(LEGACY_JOB, [(i, "{}", "done") for i in range(1, 51)]
                               + [(51, "{}", "processing"), (52, "{}", "pending")])
            col.db.commit()
        assert store._CLAIM_SQL.count("?") == 1  # the cursor
        plan = " | ".join(r[3] for r in fetch(col, "EXPLAIN QUERY PLAN " + store._CLAIM_SQL, 0))
        # one range search on the open-jobs index, in id order: no rowid range, no sort
        assert plan.startswith("SEARCH") and "USING INDEX idx_jobs_open" in plan, plan
        assert "PRIMARY KEY" not in plan and "TEMP B-TREE" not in plan, plan
        assert col._claim_next(51)[0] == 52
        assert col._claim_next()[0] == 51
    finally:
        asyncio.run(col.stop())


def spy_sync(col):
    """Wrap col._sync_index. Each call first appends the removal count it found (None
    before Task 7 added the counter), then runs the real sync. Returns that list."""
    seen, real = [], col._sync_index

    def spy():
        seen.append(getattr(col, "_removed_since_sync", None))
        real()

    col._sync_index = spy
    return seen


def test_process_job_sync_flag(tmp_path):
    # sync=False is the batched worker's call: the job commits and updates the index in
    # memory, and leaves writing the index file to its batch's one sync
    col = make_collection(tmp_path)

    async def run():
        try:
            seen = spy_sync(col)
            await col._process_job(docs_payload([1, 2]))  # records 1, 2
            calls = [len(seen)]
            await col._process_job(docs_payload([3]), sync=False)  # record 3
            calls.append(len(seen))
            return calls, index_ids(col), fetch(col, "SELECT id FROM vecs ORDER BY id")
        finally:
            await col.stop()

    calls, ids, vecs = asyncio.run(run())
    assert calls == [1, 1]  # the default still syncs after every job; sync=False never
    assert ids == [1, 2, 3]  # searchable at once: the in-memory index has record 3
    assert vecs == [(1,), (2,), (3,)]  # and its rows are committed


def test_removal_cap_syncs_before_crossing_the_cap(tmp_path, monkeypatch):
    # turbovec keeps each removal as a redo op in the index file's header and rewrites
    # the whole file past 1024 of them, so one sync must not carry more than
    # SYNC_MAX_REMOVALS removals piled up across a batch's jobs. A lone job over the
    # cap is not split: it syncs with everything it removed, as every job does today
    monkeypatch.setattr(store, "SYNC_MAX_REMOVALS", 10)
    col = make_collection(tmp_path)

    async def run():
        try:
            await col._process_job(docs_payload(range(1, 13)))  # records 1-12, synced
            seen = spy_sync(col)
            for _ in range(3):  # each re-upsert replaces docs 1-6: 6 removals per job
                await col._process_job(docs_payload(range(1, 7)), sync=False)
            steps = [list(seen), col._removed_since_sync]
            await asyncio.to_thread(col._sync_index)  # what the batch's flush will do
            steps += [list(seen), col._removed_since_sync]
            # one job replacing all 12 rows (docs 1-6's current rows, docs 7-12's first)
            await col._process_job(docs_payload(range(1, 13)), sync=False)
            steps += [list(seen), col._removed_since_sync]
            await col._process_job(docs_payload([13]))  # the default sync=True
            steps += [list(seen), col._removed_since_sync]
            return steps, index_ids(col), fetch(col, "SELECT id FROM records ORDER BY id")
        finally:
            await col.stop()

    steps, ids, records = asyncio.run(run())
    assert steps == [
        [6, 6], 6,  # jobs 2 and 3 each synced the 6 pending removals first: 6 + 6 > 10
        [6, 6, 6], 0,  # the flush wrote job 3's 6 and reset the count
        [6, 6, 6], 12,  # the count was 0, so no pre-sync: the lone job keeps all 12
        [6, 6, 6, 12], 0,  # the next sync writes them in one go
    ]
    assert ids == [r[0] for r in records] and len(ids) == 13  # nothing lost, no ghost


def test_removal_cap_lets_a_sync_carry_exactly_the_cap(tmp_path, monkeypatch):
    # the check is "would pass the cap" (>): pending plus this job's removals may equal
    # SYNC_MAX_REMOVALS, and that sync still carries them all in one go
    monkeypatch.setattr(store, "SYNC_MAX_REMOVALS", 10)
    col = make_collection(tmp_path)

    async def run():
        try:
            await col._process_job(docs_payload(range(1, 11)))  # records 1-10, synced
            seen = spy_sync(col)
            await col._process_job(docs_payload(range(1, 5)), sync=False)  # 4 removals
            await col._process_job(docs_payload(range(5, 11)), sync=False)  # 4 + 6 == 10
            return list(seen), col._removed_since_sync
        finally:
            await col.stop()

    assert asyncio.run(run()) == ([], 10)  # no pre-sync: the batch's sync takes all 10


def test_sync_resets_the_removal_counter(tmp_path):
    # every sync writes the pending removals, whoever runs it (the worker, the cap,
    # delete_document, stop()), so every sync resets the count
    col = make_collection(tmp_path)

    async def run():
        try:
            await col._process_job(docs_payload([1, 2, 3]))  # records 1-3, synced
            counts = [col._removed_since_sync]
            await col._process_job(docs_payload([1, 2]), sync=False)  # replaces 2 rows
            counts.append(col._removed_since_sync)
            await asyncio.to_thread(col._sync_index)
            counts.append(col._removed_since_sync)
            seen = spy_sync(col)
            await col.delete_document("d3")  # removes record 3's id, then syncs
            counts.append(col._removed_since_sync)
            return counts, list(seen)
        finally:
            await col.stop()

    counts, seen = asyncio.run(run())
    assert counts == [0, 2, 0, 0]
    assert seen == [1]  # delete_document's _unindex counted its removal before the sync


def test_ingest_probe_accepts_batch_flags(tmp_path, monkeypatch):
    # Task 10 tunes N and T with the probe. Without the flags it builds the collection
    # exactly as before, so the same file still runs on a tree without the knobs
    probe = load_probe(monkeypatch)
    made, real = [], probe.Collection

    def spy(*args, **kw):
        made.append(kw)
        return real(*args, **kw)

    monkeypatch.setattr(probe, "Collection", spy)
    out = probe.main(probe_args(tmp_path, batch_jobs=3, batch_ms=250))
    assert made == [{"sync_batch_jobs": 3, "sync_batch_ms": 250.0}]
    assert (out["sync_batch_jobs"], out["sync_batch_ms"]) == (3, 250.0)
    assert (out["args"]["batch_jobs"], out["args"]["batch_ms"]) == (3, 250.0)
    assert out["jobs"] == {"done": 4}
    made.clear()
    out = probe.main(probe_args(tmp_path))
    assert made == [{}]
    assert (out["sync_batch_jobs"], out["sync_batch_ms"]) == (8, 1000.0)
    assert out["args"]["batch_jobs"] is None and out["args"]["batch_ms"] is None
    for bad in ({"batch_jobs": 0}, {"batch_ms": -1}):
        with pytest.raises(SystemExit):
            probe.main(probe_args(tmp_path, **bad))


# ---- batched sync: worker


def fingerprint(col):
    """What a run leaves behind, keyed by external id so that two collections whose
    internal ids differ still compare: every record row, every stored vector, the
    published indexed_counts and the vector index size. Call it before stop()."""
    return (
        fetch(col, "SELECT external_id, doc_id, type, position, text, metadata, indexed"
                   " FROM records ORDER BY external_id"),
        fetch(col, "SELECT r.external_id, v.vec FROM records r JOIN vecs v ON v.id = r.id"
                   " ORDER BY r.external_id"),
        dict(col.indexed_counts),
        len(col.index),
    )


def snapshot(col, dst):
    """Copy the collection's directory as a crash at this instant would leave it.
    Under db_lock no transaction is half-written. The -shm file is skipped: Windows
    holds byte-range locks on it, and SQLite rebuilds it from the -wal on open."""
    with col.db_lock:
        shutil.copytree(col.dir, dst, ignore=shutil.ignore_patterns("*-shm"))


async def run_batched(col, payloads, timeout=WAIT_SECONDS):
    """Journal every payload, then start the worker and wait until no job is open."""
    for p in payloads:
        await asyncio.to_thread(col._enqueue_row, p)
    col.start_worker()
    await drain(col, timeout)


def reference(tmp_path, payloads, **kw):
    """The fingerprint the same jobs leave when every job syncs on its own
    (sync_batch_jobs=1), in a fresh collection under tmp_path / "reference"."""
    d = tmp_path / "reference"
    d.mkdir()
    col = make_collection(d, sync_batch_jobs=1, **kw)

    async def go():
        try:
            await run_batched(col, payloads)
            return fingerprint(col)
        finally:
            await col.stop()

    return asyncio.run(go())


def track(col, after_job=None):
    """Record the worker's steps, in order, in the returned list:
    ("job", label) when a job's _process_job ends (label: the job's first doc_id,
    or its op for an index job); ("sync", labels) when an index sync starts, with the
    labels of the ingest jobs begun since the previous sync; ("synced",) when that
    sync returns; ("finish", job_id, status) when a job is finished.
    after_job(label, kw), if given, is awaited after each job that did not raise."""
    events, since = [], []
    real_process, real_sync, real_finish = col._process_job, col._sync_index, col._finish_job

    async def process(payload, **kw):
        docs = payload.get("documents")
        label = docs[0]["doc_id"] if docs else payload["op"]
        if docs:
            since.append(label)
        try:
            await real_process(payload, **kw)
        finally:
            events.append(("job", label))
        if after_job is not None:
            await after_job(label, kw)

    def sync():
        events.append(("sync", list(since)))
        since.clear()
        real_sync()
        events.append(("synced",))

    def finish(job_id, status, error):
        events.append(("finish", job_id, status))
        real_finish(job_id, status, error)

    col._process_job, col._sync_index, col._finish_job = process, sync, finish
    return events


def batches(events):
    """The labels each index sync covered, one list per sync."""
    return [e[1] for e in events if e[0] == "sync"]


def late_finishes(events):
    """Job ids finished before their data reached the index file, for runs whose job n
    is doc "dn". Job n is on time only if a sync starts after its ("job", ...) event,
    that sync returns, and only then is job n finished."""
    late = []
    for i, e in enumerate(events):
        if e[0] != "job":
            continue
        n = int(e[1][1:])
        start = next((k for k in range(i + 1, len(events)) if events[k][0] == "sync"), None)
        end = None if start is None else next(
            (k for k in range(start + 1, len(events)) if events[k][0] == "synced"), None)
        fin = next((k for k, f in enumerate(events) if f[0] == "finish" and f[1] == n), None)
        if end is None or fin is None or fin < end:
            late.append(n)
    return late


def test_batch_syncs_once_for_many_jobs(tmp_path):
    # before: every job wrote the whole index file. Now the 8 jobs of one batch share
    # one sync, and every one of them ends 'done'
    col = make_collection(tmp_path, sync_batch_jobs=8, sync_batch_ms=60_000)
    events = track(col)

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in range(1, 9)])
            return list(events), fetch(col, "SELECT id, status FROM jobs ORDER BY id"), index_ids(col)
        finally:
            await col.stop()

    events, jobs, ids = asyncio.run(go())
    assert batches(events) == [[f"d{j}" for j in range(1, 9)]]
    assert jobs == [(j, "done") for j in range(1, 9)]
    assert ids == list(range(1, 9))


def test_no_job_is_done_before_its_covering_sync(tmp_path):
    # D8: 'done' promises that the job's rows are in the index file, so they survive a
    # crash. Each job may finish only after a sync that began after its _process_job
    # ended has returned
    col = make_collection(tmp_path, sync_batch_jobs=3, sync_batch_ms=60_000)
    events = track(col)

    async def go():
        try:
            await run_batched(col, [docs_payload([j, j + 100]) for j in range(1, 9)])
            return list(events), fetch(col, "SELECT status FROM jobs ORDER BY id")
        finally:
            await col.stop()

    events, statuses = asyncio.run(go())
    assert late_finishes(events) == []
    assert [len(b) for b in batches(events)] == [3, 3, 2]  # two count-capped, one idle
    assert statuses == [("done",)] * 8


def test_idle_queue_flushes_the_open_batch(tmp_path):
    # both caps are far away, so only the claim finding nothing can close the batch:
    # a trickle of jobs must still reach the index file and 'done' promptly
    col = make_collection(tmp_path, sync_batch_jobs=100, sync_batch_ms=60_000)
    seen = spy_sync(col)

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in (1, 2, 3)])
            first = len(seen)
            await col.enqueue(docs_payload([4]))  # the worker is idle: this wakes it
            await drain(col)
            return first, len(seen), fetch(col, "SELECT status FROM jobs ORDER BY id")
        finally:
            await col.stop()

    first, second, statuses = asyncio.run(go())
    assert (first, second) == (1, 2)  # one sync per idle flush, not one per job
    assert statuses == [("done",)] * 4


def test_batch_closes_at_the_time_cap(tmp_path):
    # a batch closes once sync_batch_ms have passed since its first job joined, even
    # with more jobs queued. Waiting out a real cap would make the test slow and
    # timing-dependent, so after_job ages the open batch instead, as if 60 s had passed
    aged, zero = tmp_path / "aged", tmp_path / "zero"
    aged.mkdir()
    zero.mkdir()
    col = make_collection(aged, sync_batch_jobs=100, sync_batch_ms=5_000)

    async def age(label, kw):
        if label in ("d2", "d4") and getattr(col, "_batch_t0", None) is not None:
            col._batch_t0 -= 60.0

    events = track(col, after_job=age)
    col0 = make_collection(zero, sync_batch_jobs=100, sync_batch_ms=0)
    events0 = track(col0)

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in range(1, 6)])
            await run_batched(col0, [docs_payload([j]) for j in (1, 2, 3)])
            return batches(events), batches(events0)
        finally:
            await col.stop()
            await col0.stop()

    got, got0 = asyncio.run(go())
    assert got == [["d1", "d2"], ["d3", "d4"], ["d5"]]  # aged twice, then the idle flush
    assert got0 == [["d1"], ["d2"], ["d3"]]  # sync_batch_ms=0: each batch closes at once


def test_claim_cursor_processes_each_job_once(tmp_path):
    # a batch's jobs stay 'processing' until its sync, so a claim on status alone would
    # hand the worker its own open jobs again. The worker's cursor moves past them
    col = make_collection(tmp_path, sync_batch_jobs=8, sync_batch_ms=60_000)
    events = track(col)
    claims, real_claim = [], col._claim_next

    def claim(*args):
        claims.append(args)
        return real_claim(*args)

    col._claim_next = claim

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in range(1, 21)])
            return list(claims), list(events), fetch(col, "SELECT COUNT(*) FROM records")
        finally:
            await col.stop()

    claims, events, records = asyncio.run(go())
    # a fresh worker starts at cursor 0, then claims above the last job it claimed
    assert claims[:21] == [(k,) for k in range(21)]
    assert set(claims[21:]) <= {(20,)}  # idle: nothing is open above job 20
    assert [e[1] for e in events if e[0] == "job"] == [f"d{j}" for j in range(1, 21)]
    assert [e[1] for e in events if e[0] == "finish"] == list(range(1, 21))
    assert [len(b) for b in batches(events)] == [8, 8, 4]
    assert records == [(20,)]


def test_error_job_in_a_batch_is_isolated(tmp_path):
    # a failing job joins the batch as 'error'. It neither aborts the batch nor
    # finishes ahead of it, and the good jobs around it all land
    col = make_collection(tmp_path, sync_batch_jobs=6, sync_batch_ms=60_000)
    zero = docs_payload([3])
    zero["documents"][0]["chunks"][0]["vector"] = [0.0] * 8  # fails in _process_job
    for p in (docs_payload([1]), docs_payload([2]), zero):
        col._enqueue_row(p)
    with col.db_lock:  # job 4: a pre-2026-09 row that decodes to a list, not an object
        col.db.execute(LEGACY_JOB, (4, "[]", "pending"))
        col.db.commit()
    for p in (docs_payload([5]), docs_payload([6])):
        col._enqueue_row(p)
    events = track(col)

    async def go():
        col.start_worker()
        try:
            await drain(col)
            return (list(events), fetch(col, "SELECT id, status, error FROM jobs ORDER BY id"),
                    fetch(col, "SELECT external_id FROM records ORDER BY external_id"))
        finally:
            await col.stop()

    events, jobs, records = asyncio.run(go())
    assert [e for e in events if e[0] != "job"] == [
        ("sync", ["d1", "d2", "d3", "d5", "d6"]), ("synced",),
        ("finish", 1, "done"), ("finish", 2, "done"), ("finish", 3, "error"),
        ("finish", 4, "error"), ("finish", 5, "done"), ("finish", 6, "done"),
    ]
    assert jobs == [
        (1, "done", None), (2, "done", None),
        (3, "error", "zero vector cannot be normalized"),
        (4, "error", "bad job payload: payload is a JSON list, not an object"),
        (5, "done", None), (6, "done", None),
    ]
    assert records == [("c1",), ("c2",), ("c5",), ("c6",)]


def test_index_jobs_are_batch_barriers(tmp_path, monkeypatch):
    # attach and detach rebuild the index and write it themselves. The open batch is
    # flushed before them, and the index job runs alone and finishes at once
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 32)
    col = make_collection(tmp_path, sync_batch_jobs=100, sync_batch_ms=60_000)
    payloads = [docs_payload([101]), docs_payload([102]),
                {"op": "attach_index", "nlist": 4, "nprobe": None},
                docs_payload([103]), {"op": "detach_index"}, docs_payload([104])]

    async def go():
        try:
            await col._process_job(docs_payload(range(1, 65)))  # nlist 4 needs >= 32 rows
            events = track(col)
            await run_batched(col, payloads)
            return (list(events), col.index_info(),
                    fetch(col, "SELECT COUNT(*) FROM records"), len(col.index))
        finally:
            await col.stop()

    events, info, records, size = asyncio.run(go())
    assert events == [
        ("job", "d101"), ("job", "d102"),
        ("sync", ["d101", "d102"]), ("synced",), ("finish", 1, "done"), ("finish", 2, "done"),
        ("job", "attach_index"), ("finish", 3, "done"),
        ("job", "d103"),
        ("sync", ["d103"]), ("synced",), ("finish", 4, "done"),
        ("job", "detach_index"), ("finish", 5, "done"),
        ("job", "d104"),
        ("sync", ["d104"]), ("synced",), ("finish", 6, "done"),
    ]
    assert (info, records, size) == ({"type": "flat"}, [(68,)], 68)


def test_calibration_crossing_inside_one_batch_matches_per_job(tmp_path, monkeypatch):
    # the job that crosses CAL_THRESHOLD re-encodes the index in memory. Batched, the
    # re-encoded index reaches the file at the batch's one sync instead of at that
    # job's own sync, and a reopen finds the same state either way
    monkeypatch.setattr(store, "CAL_THRESHOLD", 6)
    monkeypatch.setattr(store, "CAL_SAMPLE", 4)
    payloads = [docs_payload([2 * j - 1, 2 * j]) for j in range(1, 6)]  # 5 jobs, 10 rows

    def run(name, n):
        d = tmp_path / name
        d.mkdir()
        col = make_collection(d, sync_batch_jobs=n, sync_batch_ms=60_000)
        col._cal_rng = np.random.default_rng(7)
        seen = spy_sync(col)

        async def go():
            try:
                await run_batched(col, payloads)
                return len(seen), col.index.calibration_state
            finally:
                await col.stop()

        syncs, live = asyncio.run(go())
        col = make_collection(d)  # reopen: what the files hold
        try:
            return (syncs, live, col.index.calibration_state, col._cal_reservoir is None,
                    fingerprint(col), index_ids(col))
        finally:
            asyncio.run(col.stop())

    per_job, batched = run("per_job", 1), run("batched", 5)
    assert (per_job[0], batched[0]) == (5, 1)  # the crossing (job 3) sits inside the batch
    assert batched[1:] == per_job[1:]
    assert batched[1:4] == ("calibrated", "calibrated", True)
    assert batched[5] == list(range(1, 11))


def versioned(ids, v):
    """docs_payload(ids) with per-version text and vectors, so a replay that left an
    older version where a newer one belongs would show in the fingerprint."""
    p = docs_payload(ids)
    for d, i in zip(p["documents"], ids):
        chunk = d["chunks"][0]
        chunk["text"], chunk["vector"] = f"text {i} v{v}", vec(1000 * v + i)
    return p


def crash_jobs():
    # at cap 4, jobs 1-4 make the first batch and jobs 5-6 the second. Jobs 3, 5 and 6
    # replace records that earlier jobs wrote, and job 6 replaces one of job 5's
    return [versioned([1, 2, 3, 4], 1), versioned([5, 6, 7, 8], 1), versioned([2, 9], 2),
            versioned([10, 11], 1), versioned([1, 5, 12], 2), versioned([5, 13], 3)]


@pytest.mark.parametrize("point", ["mid_batch", "before_sync", "after_sync_before_finish"])
def test_crash_window_replays_without_loss(tmp_path, point):
    # wherever a crash lands inside a batch, reopening must replay the batch to the
    # state that the same jobs leave when each one syncs on its own
    live, image = tmp_path / "live", tmp_path / "crash"
    live.mkdir()
    ref = reference(tmp_path, crash_jobs())
    col = make_collection(live, sync_batch_jobs=4, sync_batch_ms=60_000)
    processed, finished, taken = [], [], []
    real_process, real_sync, real_finish = col._process_job, col._sync_index, col._finish_job

    def crash_here(at):
        if at == point and not taken:
            snapshot(col, image)
            taken.append(at)

    async def process(payload, **kw):
        await real_process(payload, **kw)
        processed.append(len(processed) + 1)  # job n is the n-th one processed
        if processed[-1] == 5 and kw.get("sync") is False:
            crash_here("mid_batch")  # job 5 committed, not synced; job 6 still pending

    def sync():
        if 6 in processed and 5 not in finished:
            crash_here("before_sync")  # jobs 5 and 6 committed, the index file older
        real_sync()

    def finish(job_id, status, error):
        if job_id == 5 and 6 in processed:
            crash_here("after_sync_before_finish")  # synced, 5 and 6 still 'processing'
        real_finish(job_id, status, error)
        finished.append(job_id)

    col._process_job, col._sync_index, col._finish_job = process, sync, finish

    async def go():
        try:
            await run_batched(col, crash_jobs())
            return fingerprint(col)
        finally:
            await col.stop()

    got_live = asyncio.run(go())
    assert taken == [point], f"the {point} crash point never occurred"
    assert got_live == ref  # the spies change nothing

    col = make_collection(image, sync_batch_jobs=4, sync_batch_ms=60_000)

    async def replay():
        try:
            jobs = fetch(col, "SELECT id, status FROM jobs ORDER BY id")
            col.start_worker()
            await drain(col)
            return (jobs, fingerprint(col), index_ids(col),
                    fetch(col, "SELECT id FROM records WHERE indexed=1 ORDER BY id"),
                    fetch(col, "SELECT DISTINCT status FROM jobs"),
                    fetch(col, "SELECT COUNT(*) FROM job_payloads"))
        finally:
            await col.stop()

    jobs, got, ids, indexed, statuses, payload_rows = asyncio.run(replay())
    six = "pending" if point == "mid_batch" else "processing"
    assert jobs == [(j, "done") for j in range(1, 5)] + [(5, "processing"), (6, six)]
    assert got == ref  # no lost row, no stale version, same counts, same index size
    assert ids == [r[0] for r in indexed]  # no ghost left in the index, none missing
    assert statuses == [("done",)]
    assert payload_rows == [(0,)]


class ShardWrites:
    """Delegating proxy for one IVF shard that logs its number j each time the shard
    file is written (Plan F's SyncSpy pattern in tests/test_ivf_fanout.py)."""

    def __init__(self, inner, log, j):
        self.inner, self.log, self.j = inner, log, j

    def __len__(self):
        return len(self.inner)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def sync(self, path):
        self.log.append(self.j)
        return self.inner.sync(path)


def shards_holding(ivf, ids):
    """The numbers of the shards whose id maps hold any of ids."""
    return {j for j, sh in enumerate(ivf.shards) for i in ids if sh.contains(i)}


@pytest.mark.skipif(not hasattr(store._IvfIndex, "dirty_shards"),
                    reason="needs Plan F's dirty-shard tracking")
def test_batched_ivf_sync_writes_each_dirty_shard_once(tmp_path, monkeypatch):
    # an IVF sync writes only the shards changed since the last one (Plan F). A batch
    # of four jobs is then one sync, which writes each shard any of them touched once
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 32)
    col = make_collection(tmp_path, sync_batch_jobs=4, sync_batch_ms=60_000)
    payloads = [docs_payload([1, 101, 102]), docs_payload([2, 103, 104]),
                docs_payload([105, 106, 107]), docs_payload([108, 109, 110])]

    async def go():
        try:
            await col._process_job(docs_payload(range(1, 65)))
            await col._process_job({"op": "attach_index", "nlist": 4, "nprobe": None})
            ivf = col.index
            assert isinstance(ivf, store._IvfIndex) and ivf.dirty_shards == frozenset()
            old = [r[0] for r in fetch(
                col, "SELECT id FROM records WHERE external_id IN ('c1', 'c2')")]
            touched = shards_holding(ivf, old)  # re-upserting c1 and c2 removes these ids
            wrote, flushes, real_sync = [], [], col._sync_index
            ivf.shards = [ShardWrites(sh, wrote, j) for j, sh in enumerate(ivf.shards)]

            def sync():
                dirty, start = ivf.dirty_shards, len(wrote)
                real_sync()
                flushes.append((dirty, wrote[start:], ivf.dirty_shards))

            col._sync_index = sync
            await run_batched(col, payloads)
            new = [r[0] for r in fetch(col, "SELECT id FROM records WHERE id > 64")]
            return list(flushes), touched | shards_holding(ivf, new)
        finally:
            await col.stop()

    flushes, touched = asyncio.run(go())
    assert len(flushes) == 1  # four jobs, one sync
    dirty, wrote, after = flushes[0]
    assert dirty == frozenset(touched)  # every shard a job touched, and only those
    assert sorted(wrote) == sorted(dirty)  # each one written exactly once
    assert after == frozenset()


# ---- batched sync: failure and shutdown


class LogRecords(logging.Handler):
    """Keeps every ERROR record logged on the logger it is added to. It sits on that
    logger itself, so it sees them whatever propagation the logging config set."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def failing_sync(col, failures):
    """Make col._sync_index raise OSError on its first `failures` calls; later calls
    run the real sync. Each failing call first records the jobs table as it stands
    while the batch waits. Returns (calls, states): one entry per call, and one
    table per failed call."""
    calls, states, real = [], [], col._sync_index

    def sync():
        calls.append(len(calls) + 1)
        if len(calls) <= failures:
            states.append(fetch(col, "SELECT id, status FROM jobs ORDER BY id"))
            raise OSError(f"disk full (sync {len(calls)})")
        real()

    col._sync_index = sync
    return calls, states


async def wait_for_retries(col, logs, n, timeout=WAIT_SECONDS):
    """Wait until the worker has logged n failed syncs. If the worker ended instead,
    re-raise what ended it."""
    deadline = time.monotonic() + timeout
    while len(logs.records) < n:
        if col._worker is not None and col._worker.done():
            col._worker.result()  # re-raises the worker's exception, if any
            pytest.fail(f"the worker exited after {len(logs.records)} logged failures")
        assert time.monotonic() < deadline, f"{len(logs.records)} failures logged after {timeout} s"
        await asyncio.sleep(0.01)


def job_states(directory):
    """The jobs table as a stopped collection left it on disk."""
    db = sqlite3.connect(Path(directory) / "meta.db")
    try:
        return db.execute("SELECT id, status FROM jobs ORDER BY id").fetchall()
    finally:
        db.close()


def test_sync_failure_keeps_batch_processing_and_retries(tmp_path, monkeypatch):
    # before: a sync that raised in the flush ended the worker, and its batch sat in
    # 'processing' until the next open. Now the batch waits in 'processing' while the
    # worker retries with a doubling, capped delay and claims nothing new, then lands
    monkeypatch.setattr(store, "SYNC_RETRY_MIN_S", 0.01, raising=False)
    monkeypatch.setattr(store, "SYNC_RETRY_MAX_S", 0.04, raising=False)
    col = make_collection(tmp_path, sync_batch_jobs=3, sync_batch_ms=60_000)
    calls, states = failing_sync(col, 4)
    logs = LogRecords()
    store._log.addHandler(logs)

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in range(1, 5)])
            return (len(calls), fetch(col, "SELECT id, status FROM jobs ORDER BY id"),
                    index_ids(col))
        finally:
            await col.stop()

    try:
        n_calls, jobs, ids = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    # the batch [1, 2, 3] closes at the count cap: four failed syncs, then the one that
    # lands. Job 4 is claimed only after that, and its idle flush is the sixth sync
    assert n_calls == 6
    waiting = [(1, "processing"), (2, "processing"), (3, "processing"), (4, "pending")]
    assert states == [waiting] * 4
    assert len(logs.records) == 4
    for r in logs.records:
        assert r.exc_info[0] is OSError and "index sync failed" in r.getMessage()
    assert [r.args[1] for r in logs.records] == [3] * 4  # the jobs left 'processing'
    assert [r.args[2] for r in logs.records] == pytest.approx([0.01, 0.02, 0.04, 0.04])
    assert jobs == [(j, "done") for j in range(1, 5)]
    assert ids == [1, 2, 3, 4]


def test_stop_during_sync_backoff_finishes_the_batch(tmp_path, monkeypatch):
    # the retry waits outside the write lock: stop() takes the lock at once, cancels
    # the wait, and syncs and finishes the batch itself instead of sitting out 30 s
    monkeypatch.setattr(store, "SYNC_RETRY_MIN_S", 30.0, raising=False)
    monkeypatch.setattr(store, "SYNC_RETRY_MAX_S", 30.0, raising=False)
    col = make_collection(tmp_path, sync_batch_jobs=3, sync_batch_ms=60_000)
    calls, states = failing_sync(col, 1)
    logs = LogRecords()
    store._log.addHandler(logs)

    async def go():
        for j in (1, 2, 3):
            await asyncio.to_thread(col._enqueue_row, docs_payload([j]))
        col.start_worker()
        await wait_for_retries(col, logs, 1)  # the worker now waits 30 s to retry
        t0 = time.monotonic()
        await col.stop()
        return time.monotonic() - t0

    try:
        took = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    assert took < 5.0
    assert states == [[(1, "processing"), (2, "processing"), (3, "processing")]]
    assert len(calls) == 2  # the failed flush, then stop()'s own sync
    assert job_states(tmp_path) == [(1, "done"), (2, "done"), (3, "done")]
    reopened = make_collection(tmp_path)
    try:
        assert index_ids(reopened) == [1, 2, 3]  # stop()'s sync wrote the batch's rows
        assert reopened.pending_jobs() == 0  # nothing is left to replay
    finally:
        asyncio.run(reopened.stop())


def test_a_cancel_inside_a_sync_retry_ends_the_worker(tmp_path, monkeypatch):
    # the retry catches Exception, never BaseException: a cancel that lands while the
    # retry waits for the write lock ends the worker. It is not logged as one more
    # failed sync and retried. The retry's first wait is the window in which the test
    # must take the read lock, so it is long enough for a loaded runner
    monkeypatch.setattr(store, "SYNC_RETRY_MIN_S", 2.0, raising=False)
    col = make_collection(tmp_path)
    failing_sync(col, 1)
    logs = LogRecords()
    store._log.addHandler(logs)

    async def go():
        try:
            await asyncio.to_thread(col._enqueue_row, docs_payload([1]))
            col.start_worker()
            await wait_for_retries(col, logs, 1)  # the first sync failed; the retry waits
            async with col.lock.read():  # so the retry's lock.write() has to wait
                deadline = time.monotonic() + WAIT_SECONDS
                while not col.lock._writers_waiting:
                    assert time.monotonic() < deadline, "the retry never asked for the lock"
                    await asyncio.sleep(0.01)
                col._worker.cancel()
                await asyncio.wait({col._worker}, timeout=WAIT_SECONDS)
                return col._worker.cancelled(), len(logs.records)
        finally:
            await col.stop()

    try:
        ended, failures = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    assert (ended, failures) == (True, 1)
    assert job_states(tmp_path) == [(1, "done")]  # stop() synced and finished the batch


def test_stop_flushes_the_open_batch(tmp_path):
    # RF4: stop() finishes the open batch inside B's write-locked section, after the
    # worker's cancel and before the connections close. Jobs 1-3 are processed and
    # wait for their batch's sync; job 4 is claimed and parked before its write section
    live = tmp_path / "live"
    live.mkdir()
    payloads = [docs_payload([j]) for j in range(1, 5)]
    col = make_collection(live, sync_batch_jobs=100, sync_batch_ms=60_000)
    real_process, real_finish = col._process_job, col._finish_job
    finishes = []

    def finish(job_id, status, error):
        finishes.append((job_id, col.lock._writing, col._closed))
        real_finish(job_id, status, error)

    col._finish_job = finish

    async def go():
        parked, gate = asyncio.Event(), asyncio.Event()

        async def process(payload, **kw):
            if payload["documents"][0]["doc_id"] == "d4":
                parked.set()
                await gate.wait()  # never set: stop() cancels the worker here
            await real_process(payload, **kw)

        col._process_job = process
        for p in payloads:
            await asyncio.to_thread(col._enqueue_row, p)
        col.start_worker()
        try:
            await asyncio.wait_for(parked.wait(), WAIT_SECONDS)
            return [job[0] for job in col._unsynced]
        finally:
            await col.stop()

    assert asyncio.run(go()) == [1, 2, 3]
    # before: jobs 1-3 stayed 'processing', and the next open ran them again
    assert job_states(live) == [(1, "done"), (2, "done"), (3, "done"), (4, "processing")]
    # each finish ran write-locked and before _closed (so before _close_conns)
    assert finishes == [(1, True, False), (2, True, False), (3, True, False)]

    again = make_collection(live, sync_batch_jobs=100, sync_batch_ms=60_000)

    async def replay():
        try:
            before = index_ids(again)  # what stop()'s sync wrote
            again.start_worker()
            await drain(again)
            return (before, fetch(again, "SELECT id, status FROM jobs ORDER BY id"),
                    fingerprint(again))
        finally:
            await again.stop()

    before, jobs, got = asyncio.run(replay())
    assert before == [1, 2, 3]
    assert jobs == [(j, "done") for j in range(1, 5)]  # job 4 replayed
    assert got == reference(tmp_path, payloads)


def test_failed_removal_cap_presync_does_not_fail_the_job(tmp_path, monkeypatch):
    # the cap's pre-sync runs after the job's commit and before its removals and adds.
    # Raising out of the job left it 'error' (never replayed) with committed rows the
    # index lacked and the replaced ids still in it. Now the job goes on, and the
    # batch's own sync writes everything
    monkeypatch.setattr(store, "SYNC_MAX_REMOVALS", 1)
    col = make_collection(tmp_path, sync_batch_jobs=10, sync_batch_ms=60_000)
    calls, _ = failing_sync(col, 1)
    seen = spy_sync(col)  # the count each sync started with, failed or not
    logs = LogRecords()
    store._log.addHandler(logs)

    async def go():
        try:
            # job 2 replaces c1 (count 0, no pre-sync; then 1). Job 3 replaces c2:
            # 1 + 1 > 1, so it pre-syncs, and that first sync fails
            await run_batched(col, [docs_payload([1, 2]), docs_payload([1]), docs_payload([2])])
            return (len(calls), fetch(col, "SELECT id, status, error FROM jobs ORDER BY id"),
                    index_ids(col), fetch(col, "SELECT id FROM records ORDER BY id"),
                    list(seen))  # before stop()'s own sync adds a third entry
        finally:
            await col.stop()

    try:
        n_calls, jobs, ids, records, seen = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    assert n_calls == 2  # the failed pre-sync, then the batch's idle flush
    assert seen == [1, 2]  # the failed pre-sync left the count alone: the flush carried both
    assert jobs == [(1, "done", None), (2, "done", None), (3, "done", None)]
    assert ids == [r[0] for r in records] and len(ids) == 2  # no ghost, none missing
    assert len(logs.records) == 1 and logs.records[0].exc_info[0] is OSError


def closed_state(col):
    """What a completed stop() leaves: the closed flag, whether the writer connection
    still answers, how many read connections stay registered, and whether Plan F's
    shard pool is closed (True on a tree without Plan F, which has no pool)."""
    try:
        col.db.execute("SELECT 1")
        writer_open = True
    except sqlite3.ProgrammingError:  # Cannot operate on a closed database.
        writer_open = False
    pool = getattr(col, "_shard_pool", None)
    return col._closed, writer_open, len(col._read_conns), pool is None or pool._closed


def disk_rows(directory, sql):
    """Rows of a stopped collection's meta.db, read on a fresh connection."""
    db = sqlite3.connect(Path(directory) / "meta.db")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def stop_with_open_batch(directory, fail):
    """Jobs 1-3 wait in the open batch for their sync, and job 4 is claimed and parked
    before its write section, as in test_stop_flushes_the_open_batch. Then fail(col)
    installs a failure and returns its call list, and stop() runs. Returns those calls,
    the open batch's job ids as stop() began, the ERROR records logged and the stopped
    collection. A stop() that raises leaves this function."""
    col = make_collection(directory, sync_batch_jobs=100, sync_batch_ms=60_000)
    real_process = col._process_job
    logs = LogRecords()

    async def go():
        parked, gate = asyncio.Event(), asyncio.Event()

        async def process(payload, **kw):
            if payload["documents"][0]["doc_id"] == "d4":
                parked.set()
                await gate.wait()  # never set: stop() cancels the worker here
            await real_process(payload, **kw)

        col._process_job = process
        for j in range(1, 5):
            await asyncio.to_thread(col._enqueue_row, docs_payload([j]))
        col.start_worker()
        await asyncio.wait_for(parked.wait(), WAIT_SECONDS)
        batch = [job[0] for job in col._unsynced]
        calls = fail(col)  # from here on only stop() syncs and finishes
        await col.stop()
        return calls, batch

    store._log.addHandler(logs)
    try:
        calls, batch = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    return calls, batch, logs.records, col


def stop_with_failing_sync(directory):
    """stop_with_open_batch where stop()'s own index sync (the first since the open)
    fails. The calls returned are the sync calls."""
    return stop_with_open_batch(directory, lambda col: failing_sync(col, 1)[0])


def test_stop_with_a_failing_sync_logs_it_and_still_closes(tmp_path):
    # spec 3.1 D2: when stop()'s own index sync fails, stop() logs it once, completes
    # the shutdown and raises nothing. It finishes no job: the open batch keeps
    # 'processing' and its payloads, so the next open replays it
    live, normal = tmp_path / "live", tmp_path / "normal"
    live.mkdir()
    normal.mkdir()
    calls, batch, records, col = stop_with_failing_sync(live)  # before: OSError here
    stopped = make_collection(normal)
    asyncio.run(stopped.stop())  # a normal stop, to compare with
    assert batch == [1, 2, 3]
    assert calls == [1]  # stop()'s one sync, and it failed
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.ERROR and "index sync failed in stop()" in rec.getMessage()
    assert rec.exc_info[0] is OSError and rec.exc_info[2] is not None  # with its traceback
    assert rec.args == ("t", 3)  # the collection, and the batch's jobs left 'processing'
    # closed as a normal stop() closes: _closed set, the writer connection and every
    # read connection closed, Plan F's shard pool closed
    assert closed_state(col) == closed_state(stopped) == (True, False, 0, True)
    assert job_states(live) == [(j, "processing") for j in range(1, 5)]
    assert disk_rows(live, "SELECT job_id FROM job_payloads ORDER BY job_id") == [
        (1,), (2,), (3,), (4,)]


def test_a_failed_stop_sync_replays_on_the_next_open(tmp_path):
    # the jobs a failed stop() sync left 'processing' replay on the next open (D8) and
    # end 'done', with their rows in the index: none lost, no ghost
    live = tmp_path / "live"
    live.mkdir()
    stop_with_failing_sync(live)  # before: OSError here
    again = make_collection(live, sync_batch_jobs=100, sync_batch_ms=60_000)

    async def replay():
        try:
            before = index_ids(again)  # the failed sync wrote none of the batch's rows
            again.start_worker()
            await drain(again)
            return (before, fetch(again, "SELECT id, status FROM jobs ORDER BY id"),
                    fetch(again, "SELECT COUNT(*) FROM job_payloads"), index_ids(again),
                    fetch(again, "SELECT id FROM records WHERE indexed=1 ORDER BY id"),
                    fingerprint(again))
        finally:
            await again.stop()

    before, jobs, payload_rows, ids, indexed, got = asyncio.run(replay())
    assert before == []
    assert jobs == [(j, "done") for j in range(1, 5)]
    assert payload_rows == [(0,)]
    assert ids == [r[0] for r in indexed] and len(ids) == 4  # no ghost, none missing
    assert got == reference(tmp_path, [docs_payload([j]) for j in range(1, 5)])


def flaky_finish(col, fail_on):
    """Make col._finish_job raise sqlite3.OperationalError (meta.db full) on the calls
    numbered in fail_on, counting from 1, before the real finish runs; every other call
    runs it. Returns the calls, one job id per call."""
    calls, real = [], col._finish_job

    def finish(job_id, status, error):
        calls.append(job_id)
        if len(calls) in fail_on:
            raise sqlite3.OperationalError(f"database or disk is full (finish {len(calls)})")
        real(job_id, status, error)

    col._finish_job = finish
    return calls


def test_stop_logs_a_dead_workers_exception_and_still_closes(tmp_path):
    # spec 3.1 D3: the batch [1, 2, 3] syncs, then the worker's finish of job 1 raises,
    # which ends the worker (as at base). stop() logs that exception once instead of
    # re-raising it, still flushes the rest of the batch and closes as usual. Job 1
    # stays 'processing' with its payload, and the next open replays it
    live = tmp_path / "live"
    live.mkdir()
    payloads = [docs_payload([j]) for j in range(1, 4)]
    col = make_collection(live, sync_batch_jobs=3, sync_batch_ms=60_000)
    calls = flaky_finish(col, {1})
    logs = LogRecords()

    async def go():
        for p in payloads:
            await asyncio.to_thread(col._enqueue_row, p)
        col.start_worker()
        done, _ = await asyncio.wait({col._worker}, timeout=WAIT_SECONDS)  # never re-raises
        assert done, f"the worker is still running after {WAIT_SECONDS} s"
        left = [job[0] for job in col._unsynced]
        await col.stop()  # before: sqlite3.OperationalError here
        return left

    store._log.addHandler(logs)
    try:
        left = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    assert left == [2, 3]  # job 1 was popped for its finish, which raised
    assert calls == [1, 2, 3]  # the worker's failed finish, then stop()'s two
    assert len(logs.records) == 1
    rec = logs.records[0]
    assert rec.levelno == logging.ERROR and "ingest worker had died" in rec.getMessage()
    assert rec.exc_info[0] is sqlite3.OperationalError and rec.exc_info[2] is not None
    assert rec.args == ("t", 2)  # the collection, and the open batch stop() flushes
    assert closed_state(col) == (True, False, 0, True)  # what a normal stop() leaves
    assert job_states(live) == [(1, "processing"), (2, "done"), (3, "done")]
    assert disk_rows(live, "SELECT job_id FROM job_payloads ORDER BY job_id") == [(1,)]
    again = make_collection(live)

    async def replay():
        try:
            before = index_ids(again)  # the worker's sync wrote the whole batch
            again.start_worker()
            await drain(again)
            return (before, fetch(again, "SELECT id, status FROM jobs ORDER BY id"),
                    fetch(again, "SELECT COUNT(*) FROM job_payloads"), index_ids(again),
                    fetch(again, "SELECT id FROM records WHERE indexed=1 ORDER BY id"),
                    fingerprint(again))
        finally:
            await again.stop()

    before, jobs, payload_rows, ids, indexed, got = asyncio.run(replay())
    assert before == [1, 2, 3]
    assert jobs == [(j, "done") for j in range(1, 4)]  # job 1 replayed
    assert payload_rows == [(0,)]
    assert ids == [r[0] for r in indexed] and len(ids) == 3  # no ghost, none missing
    assert got == reference(tmp_path, payloads)


def test_a_failed_finish_in_stop_is_logged_and_replays(tmp_path):
    # spec 3.1 D3: stop()'s own sync lands, then its finish of job 1 raises (meta.db
    # full). stop() logs it once, tries no later finish, closes as usual and raises
    # nothing. Jobs 1-3 keep 'processing' and their payloads although the index file
    # holds their rows, so the next open re-applies them: the same rows, no ghost
    live = tmp_path / "live"
    live.mkdir()
    calls, batch, records, col = stop_with_open_batch(live, lambda c: flaky_finish(c, {1}))
    assert batch == [1, 2, 3]
    assert calls == [1]  # job 1's finish raised, and no later job was tried
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.ERROR and "finishing jobs failed in stop()" in rec.getMessage()
    assert rec.exc_info[0] is sqlite3.OperationalError and rec.exc_info[2] is not None
    assert rec.args == ("t", 3)  # the collection, and the batch's jobs left 'processing'
    assert closed_state(col) == (True, False, 0, True)  # what a normal stop() leaves
    assert job_states(live) == [(j, "processing") for j in range(1, 5)]
    assert disk_rows(live, "SELECT job_id FROM job_payloads ORDER BY job_id") == [
        (1,), (2,), (3,), (4,)]
    again = make_collection(live, sync_batch_jobs=100, sync_batch_ms=60_000)

    async def replay():
        try:
            before = index_ids(again)  # stop()'s sync wrote the batch's rows
            again.start_worker()
            await drain(again)
            return (before, fetch(again, "SELECT id, status FROM jobs ORDER BY id"),
                    fetch(again, "SELECT COUNT(*) FROM job_payloads"), index_ids(again),
                    fetch(again, "SELECT id FROM records WHERE indexed=1 ORDER BY id"),
                    fingerprint(again))
        finally:
            await again.stop()

    before, jobs, payload_rows, ids, indexed, got = asyncio.run(replay())
    assert before == [1, 2, 3]
    assert jobs == [(j, "done") for j in range(1, 5)]  # 1-3 re-applied, 4 replayed
    assert payload_rows == [(0,)]
    assert ids == [r[0] for r in indexed] and len(ids) == 4  # no ghost, none missing
    assert got == reference(tmp_path, [docs_payload([j]) for j in range(1, 5)])


class EmbedderStub:
    """Stands in for a collection's embedder in stop(), which only calls aclose().
    Records each call, one number per call; with fail=True the call then raises
    OSError, as an HTTP client whose transport fails to close does."""

    def __init__(self, fail):
        self.fail, self.calls = fail, []

    async def aclose(self):
        self.calls.append(len(self.calls) + 1)
        if self.fail:
            raise OSError(f"embedder transport close failed (close {len(self.calls)})")


def test_a_failing_embedder_close_is_logged_and_stop_goes_on(tmp_path, monkeypatch):
    # spec 3.1 D4: the embedder's HTTP client raises as it closes. stop() logs it once
    # and still closes F's shard pool after it; the flush and the connections before
    # it ran as in a normal stop()
    live = tmp_path / "live"
    live.mkdir()
    client = EmbedderStub(fail=True)

    def fail(col):
        monkeypatch.setattr(col, "_embedder", client)
        return client.calls

    calls, batch, records, col = stop_with_open_batch(live, fail)  # before: OSError here
    assert batch == [1, 2, 3]
    assert calls == [1]  # stop() closed the client once, and that raised
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.ERROR
    assert rec.getMessage() == (
        "collection t: closing the embedder's HTTP client failed in stop(); the shutdown goes on")
    assert rec.exc_info[0] is OSError and rec.exc_info[2] is not None  # with its traceback
    assert rec.args == ("t", "the embedder's HTTP client")
    assert closed_state(col) == (True, False, 0, True)  # connections and shard pool closed
    assert job_states(live) == [(1, "done"), (2, "done"), (3, "done"), (4, "processing")]


def test_a_failing_close_conns_is_logged_and_the_later_steps_run(tmp_path, monkeypatch):
    # spec 3.1 D4: closing the SQLite connections raises. stop() logs it once, has set
    # _closed before it, and still closes the embedder's client and F's shard pool
    live = tmp_path / "live"
    live.mkdir()
    client = EmbedderStub(fail=False)

    def fail(col):
        calls = []

        def close_conns():
            calls.append(col.lock._writing)
            raise sqlite3.OperationalError(
                "unable to close due to unfinalized statements or unfinished backups")

        monkeypatch.setattr(col, "_close_conns", close_conns)
        monkeypatch.setattr(col, "_embedder", client)
        return calls

    calls, batch, records, col = stop_with_open_batch(live, fail)  # before: OperationalError here
    try:
        assert batch == [1, 2, 3]
        assert calls == [True]  # called once, inside stop()'s write lock as at B
        assert len(records) == 1
        rec = records[0]
        assert rec.levelno == logging.ERROR and "failed in stop()" in rec.getMessage()
        assert rec.exc_info[0] is sqlite3.OperationalError and rec.exc_info[2] is not None
        assert rec.args == ("t", "the SQLite connections")
        assert client.calls == [1]  # the next step ran: the embedder's client closed
        closed, writer_open, _, pool_closed = closed_state(col)
        assert (closed, writer_open, pool_closed) == (True, True, True)  # meta.db still open
    finally:
        Collection._close_conns(col)  # B's real close
    assert closed_state(col) == (True, False, 0, True)
    assert job_states(live) == [(1, "done"), (2, "done"), (3, "done"), (4, "processing")]


@pytest.mark.skipif(not hasattr(store, "_ShardPool"),
                    reason="needs Plan F's shard pool")
def test_a_failing_shard_pool_close_is_logged(tmp_path, monkeypatch):
    # spec 3.1 D4: F's shard pool, the last close step, raises as it closes. stop()
    # logs it once and returns; every earlier step ran as in a normal stop()
    live = tmp_path / "live"
    live.mkdir()
    client = EmbedderStub(fail=False)

    def fail(col):
        calls = []

        def close():
            calls.append(col._closed)
            raise RuntimeError("cannot join current thread")

        monkeypatch.setattr(col._shard_pool, "close", close)
        monkeypatch.setattr(col, "_embedder", client)
        return calls

    calls, batch, records, col = stop_with_open_batch(live, fail)  # before: RuntimeError here
    assert batch == [1, 2, 3]
    assert calls == [True]  # called once, after _closed, as F requires
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.ERROR and "failed in stop()" in rec.getMessage()
    assert rec.exc_info[0] is RuntimeError and rec.exc_info[2] is not None
    assert rec.args == ("t", "the IVF shard pool")
    assert client.calls == [1]  # the steps before it ran
    # the pool stays open (its close raised); a flat collection's pool starts no thread
    assert closed_state(col) == (True, False, 0, False)
    assert job_states(live) == [(1, "done"), (2, "done"), (3, "done"), (4, "processing")]


def test_shutdown_closes_every_collection_after_a_failed_stop(tmp_path, monkeypatch):
    # spec 3.1 D3 and D4: no Exception leaves stop(), so shutdown() goes on past a
    # collection whose worker died and whose embedder client fails to close, and it
    # closes the next one and the catalog. Before, a's stop() re-raised the worker's
    # exception, and b and the catalog stayed open
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    logs = LogRecords()
    client = EmbedderStub(fail=True)

    async def go():
        mgr = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        await mgr.create_collection("a", 8, 4, None, None, None)
        await mgr.create_collection("b", 8, 4, None, None, None)
        a, b = await mgr.touch("a"), await mgr.touch("b")  # resident, and shut, in this order
        flaky_finish(a, {1})
        monkeypatch.setattr(a, "_embedder", client)  # a's second close step raises
        await a.enqueue(docs_payload([1]))  # its flush syncs, then the finish raises
        done, _ = await asyncio.wait({a._worker}, timeout=WAIT_SECONDS)
        assert done, f"a's worker is still running after {WAIT_SECONDS} s"
        await mgr.shutdown()  # before: sqlite3.OperationalError here
        return mgr, a, b

    store._log.addHandler(logs)
    try:
        mgr, a, b = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    assert closed_state(a) == closed_state(b) == (True, False, 0, True)
    assert mgr.resident == {}
    with pytest.raises(sqlite3.ProgrammingError):  # the catalog is closed too
        mgr.catalog.execute("SELECT 1")
    assert client.calls == [1]
    assert len(logs.records) == 2  # a's dead worker, then a's embedder; b stopped cleanly
    died, closing = logs.records
    assert died.exc_info[0] is sqlite3.OperationalError and "worker had died" in died.getMessage()
    assert died.args == ("a", 0)  # the worker had popped job 1, the batch's only job
    assert closing.exc_info[0] is OSError and "failed in stop()" in closing.getMessage()
    assert closing.args == ("a", "the embedder's HTTP client")  # then a's pool closed


# ---- eviction cancelled mid-stop (issue #3)


def test_shutdown_stops_a_collection_whose_eviction_was_cancelled(tmp_path, monkeypatch):
    # _evict pops the collection before it awaits stop(): a cancel landing there (the
    # app cancels housekeeping just before shutdown()) must not leave it open with
    # nothing left to close it. shutdown() waits for that stop() before the catalog
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def go():
        mgr = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        await mgr.create_collection("m", 8, 4, None, None, None)
        col = await mgr.touch("m")
        async with col.lock.read():  # so stop()'s lock.write() has to wait
            ev = asyncio.create_task(mgr._evict("m"))
            deadline = time.monotonic() + WAIT_SECONDS
            while not col.lock._writers_waiting:
                assert time.monotonic() < deadline, "stop() never asked for the lock"
                await asyncio.sleep(0.01)
            ev.cancel()
            sh = asyncio.create_task(mgr.shutdown())
            await asyncio.sleep(0.05)
            early = (sh.done(), col._closed)
        await asyncio.wait({ev, sh}, timeout=WAIT_SECONDS)
        sh.result()  # shutdown() finished and raised nothing
        with pytest.raises(sqlite3.ProgrammingError):
            mgr.catalog.execute("SELECT 1")
        return early, ev.cancelled(), closed_state(col), mgr.resident

    early, cancelled, state, resident = asyncio.run(go())
    assert early == (False, False)  # shutdown() waits for the stop() still running
    assert cancelled  # the eviction's caller still sees its cancel
    assert state == (True, False, 0, True)
    assert resident == {}


def test_a_cancelled_eviction_keeps_the_load_lock_until_stop_ends(tmp_path, monkeypatch):
    # the cancelled caller re-raises only once stop() ends, even when cancelled twice,
    # so its _load_lock covers the whole close: a touch() queued behind it cannot
    # reopen the directory beside a collection still closing
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def go():
        mgr = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        try:
            await mgr.create_collection("m", 8, 4, None, None, None)
            col = await mgr.touch("m")

            async def evict():  # as housekeeping evicts: under _load_lock
                async with mgr._load_lock:
                    await mgr._evict("m")

            async with col.lock.read():  # so stop()'s lock.write() has to wait
                ev = asyncio.create_task(evict())
                deadline = time.monotonic() + WAIT_SECONDS
                while not col.lock._writers_waiting:
                    assert time.monotonic() < deadline, "stop() never asked for the lock"
                    await asyncio.sleep(0.01)
                ev.cancel()
                await asyncio.sleep(0.01)  # into _evict's wait loop
                ev.cancel()  # a second cancel must not end that wait either
                again = asyncio.create_task(mgr.touch("m"))
                await asyncio.sleep(0.05)
                early = (ev.done(), again.done(), col._closed)
            col2 = await asyncio.wait_for(again, WAIT_SECONDS)
            await asyncio.wait({ev}, timeout=WAIT_SECONDS)
            return early, ev.cancelled(), col2 is not col, closed_state(col)
        finally:
            await mgr.shutdown()

    early, cancelled, reloaded, state = asyncio.run(go())
    assert early == (False, False, False)  # the cancelled eviction still holds the lock
    assert cancelled
    assert reloaded  # touch() loaded the collection anew, once the old one had closed
    assert state == (True, False, 0, True)


# ---- ADR 0001 addendum: D2 DGX results

D2_RESULTS = "### Results " + chr(0x2014) + " DGX A/B (D2)"


def adr_d2_results():
    """(the D2 results subsection with \\r\\n normalized, its Measurements JSON or {}).
    The subsection ends at the next ### or ## heading, or at the end of the file."""
    adr = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0001-performance-optimization-decisions.md"
    text = adr.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index(D2_RESULTS)
    ends = [i for i in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if i != -1]
    section = text[start:min(ends, default=len(text))]
    fence = "`" * 3
    if "#### Measurements (D2)" not in section or fence + "json\n" not in section:
        return section, {}
    raw = section.split(fence + "json\n", 1)[1].split("\n" + fence, 1)[0]
    return section, json.loads(raw)


def test_adr_ingest_path_d2_results_pass_the_gates(monkeypatch):
    """Spec sections 6 and 7 D for D2 on gn100: N and T chosen on seed 7; on seed 42, ingest
    vec/s beyond the band on IVF nlist 256 and not worse on flat, every job done with no
    payload row left, one final state across arms; and the shipped defaults are the choice."""
    import inspect
    import statistics

    section, m = adr_d2_results()
    assert "Pending:" not in section
    assert "#### Measurements (D2)" in section and m, "no Measurements (D2) JSON block"
    assert sorted(m) == ["base_sha", "bench", "cand_sha", "date", "gates", "labels", "probe", "tuning"]
    assert m["base_sha"] != m["cand_sha"]

    def gain_band(base, cand, floor=1.0):  # Plan A's rule: band = max(both spreads, resolution)
        band = max(max(base) - min(base), max(cand) - min(cand), floor)
        return statistics.median(cand) - statistics.median(base), band

    def labelled(lab):  # spec section 6: each row names its regime, cap, sqlite and BLAS threads
        return (lab["regime"] == ["host-warm, uncapped host process"] and bool(lab["cap"])
                and lab["openblas_num_threads"] == ["1"] and len(lab["sqlite_version"]) == 1)

    # seed 7: four pairs x 3 runs per mode, and a batched choice that beats (1, 0) on ivf256
    t = m["tuning"]
    assert t["seed"] == 7 and t["complete"] and t["fingerprints_equal"] and t["labels_ok"]
    assert sorted(t["pairs"]) == ["n1-t0", "n32-t1000", "n32-t4000", "n8-t1000"]
    assert all(len(p[mode]) == 3 for p in t["pairs"].values() for mode in ("flat", "ivf256"))
    n, ms = t["choice"]
    assert (n, ms) != (1, 0), "the choice must batch"
    ref, pick = t["pairs"]["n1-t0"], t["pairs"][f"n{n}-t{ms}"]
    gain, band = gain_band(ref["ivf256"], pick["ivf256"])
    assert gain > band and pick["ivf256_vs_ref"] == "better", "seed 7: the ivf256 gain is within the band"
    gain, band = gain_band(ref["flat"], pick["flat"])
    assert -gain <= band and pick["flat_vs_ref"] in ("better", "within band"), "seed 7: flat is worse"
    assert f"Chosen: SYNC_BATCH_JOBS={n}, SYNC_BATCH_MS={ms}.\n" in section
    assert labelled(m["labels"]["tune"])

    # seed 42: base (main after D1, E and F) against D2 at the chosen defaults
    for mode in ("flat", "ivf256"):
        p = m["probe"][mode]
        assert len(p["base"]) == 3 and len(p["cand"]) == 3, mode
        assert p["fingerprints_equal"] and p["jobs_all_done"], mode
        assert set(p["payload_rows"]["base"]) == {0} and set(p["payload_rows"]["cand"]) == {0}, mode
        assert f"- PASS: D2-{mode}\n" in section
    ivf, flat = m["probe"]["ivf256"], m["probe"]["flat"]
    gain, band = gain_band(ivf["base"], ivf["cand"])
    assert gain > band and ivf["verdict"] == "better", "probe ivf256: the gain is within the band"
    gain, band = gain_band(flat["base"], flat["cand"])
    assert -gain <= band and flat["verdict"] in ("better", "within band"), "probe flat: worse"
    assert labelled(m["labels"]["probe"])
    b = m["bench"]
    if b is None:  # probe-only window, or not enough time left for the reingests
        assert "Not run: the bench reingest" in section and "- SKIP: D2-bench\n" in section
    else:
        assert len(b["base"]) == 2 and len(b["cand"]) == 2 and len(b["runs"]) == 4
        gain, band = gain_band(b["base"], b["cand"])
        assert -gain <= band and b["verdict"] in ("better", "within band"), "bench reingest: worse"
        for name, run in b["runs"].items():
            assert set(run["jobs"]) == {"done"} and run["payload_rows"] == 0, name  # no error job
        lb = m["labels"]["bench"]
        assert lb["regime"] == ["host-warm"] and lb["cap"] == ["4g"] and lb["openblas_num_threads"] == ["1"]
        assert all(len(v) == 1 for v in lb["sqlite_version"].values())
        assert "- PASS: D2-bench\n" in section
    for gate in ("D2-run", "D2-tuning", "D2-fingerprints", "D2-jobs", "D2-labels"):
        assert f"- PASS: {gate}\n" in section, gate
    assert "- FAIL:" not in section

    # what ships is what was measured: the defaults and the documented rows are the choice
    monkeypatch.delenv("SYNC_BATCH_JOBS", raising=False)
    monkeypatch.delenv("SYNC_BATCH_MS", raising=False)
    s = Settings()
    assert (s.sync_batch_jobs, s.sync_batch_ms) == (n, float(ms))
    params = inspect.signature(Collection).parameters
    assert (params["sync_batch_jobs"].default, params["sync_batch_ms"].default) == (n, float(ms))
    root = Path(__file__).resolve().parents[1]
    for doc in ("README.md", "docs/getting-started.md"):
        text = (root / doc).read_text(encoding="utf-8")
        assert f"| `SYNC_BATCH_JOBS` | `{n}` |" in text and f"| `SYNC_BATCH_MS` | `{ms}` |" in text, doc
