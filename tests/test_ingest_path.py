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
