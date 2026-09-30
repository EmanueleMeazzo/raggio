"""Plan D (ingest path): the binary job journal, payload work off the event loop,
failure-safe upserts, the vacuum policy, the ingest probe and the batched index sync.
One `# ---- <topic>` section per plan task, in task order."""
import asyncio
import copy
import json
import sqlite3
import struct
import sys
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
