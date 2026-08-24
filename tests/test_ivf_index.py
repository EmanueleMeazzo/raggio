"""The optional IVF index: attach/detach lifecycle, correctness vs the flat scan,
updates while indexed, persistence/reload, ghost reconcile, and the API surface."""
import asyncio
import sqlite3
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import sys
sys.path.insert(0, "src")
import raggio.store as store
from raggio.app import create_app
from raggio.store import Collection, CollectionConfig, IdMapIndex, _IvfIndex, open_meta_db

DIM = 8


def make_collection(tmp_path):
    return Collection(CollectionConfig("t", DIM, 4, None, None, None), Path(tmp_path), lambda: None)


def rowvec(i):
    v = np.random.default_rng(1000 + i).standard_normal(DIM)
    return (v / np.linalg.norm(v)).tolist()


def ingest(col, n, meta=None):
    docs = [
        {"doc_id": f"d{i}", "chunks": [
            {"id": f"c{i}", "text": f"chunk {i}", "vector": rowvec(i),
             "metadata": (meta or {}).get(i)},
        ]}
        for i in range(n)
    ]
    asyncio.run(col._process_job({"documents": docs}))


def attach(col, nlist=8, nprobe=None):
    asyncio.run(col._process_job({"op": "attach_index", "nlist": nlist, "nprobe": nprobe}))


def top_ids(col, i, k=5, nprobe=None, filt=None):
    q = np.array([rowvec(i)], dtype=np.float32)
    hits = asyncio.run(col.search("vector", q, None, k, "chunks", filt, None, nprobe))
    return [h["id"] for h in hits]


@pytest.fixture(autouse=True)
def small_min_rows(monkeypatch):
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)


def test_attach_matches_flat_and_survives_reload(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    assert top_ids(col, 5)[0] == "c5"  # flat baseline
    attach(col, nlist=8, nprobe=8)  # probe everything: same results as flat
    assert isinstance(col.index, _IvfIndex)
    assert len(col.index) == 300
    assert not col.index_path.exists()  # flat file dropped after the swap
    assert top_ids(col, 5)[0] == "c5"
    assert col.cfg.index_config == {"nlist": 8, "nprobe": 8}

    asyncio.run(col.stop())
    col2 = Collection(col.cfg, Path(tmp_path), lambda: None)  # reload from ivf/
    assert isinstance(col2.index, _IvfIndex)
    assert len(col2.index) == 300
    assert top_ids(col2, 5)[0] == "c5"


def test_detach_restores_flat(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    attach(col, nlist=8)
    asyncio.run(col._process_job({"op": "detach_index"}))
    assert isinstance(col.index, IdMapIndex)
    assert len(col.index) == 300
    assert col.index_path.exists()
    assert not col.ivf_dir.exists()
    assert col.cfg.index_config is None
    assert top_ids(col, 5)[0] == "c5"


def test_upsert_and_delete_while_indexed(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=8)
    # upsert c7 under a new vector: old id must leave its shard, new one be findable
    asyncio.run(col._process_job({"documents": [
        {"doc_id": "d7", "chunks": [{"id": "c7", "text": "moved", "vector": rowvec(9999)}]},
    ]}))
    assert len(col.index) == 300
    assert top_ids(col, 9999)[0] == "c7"
    assert asyncio.run(col.delete_document("d8")) == 1
    assert len(col.index) == 299
    assert "c8" not in top_ids(col, 8)
    # retained vectors follow their records: no orphan blobs accumulating on disk
    assert (col.db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
            == col.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 299)


def test_reconcile_evicts_ivf_ghosts(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    attach(col, nlist=8)
    rid = col.db.execute("SELECT id FROM records WHERE external_id='c3'").fetchone()[0]
    col.db.execute("DELETE FROM records WHERE id=?", (rid,))  # crash-shaped: index keeps the id
    col.db.commit()
    asyncio.run(col.stop())
    col2 = Collection(col.cfg, Path(tmp_path), lambda: None)
    assert len(col2.index) == 299
    assert not col2.index.contains(rid)


def test_request_validation(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 10)
    with pytest.raises(ValueError, match="at least"):
        asyncio.run(col.request_index(None, None))
    with pytest.raises(ValueError, match="no index"):
        asyncio.run(col.request_index_drop())
    ingest_more = [{"doc_id": "dx", "chunks": [
        {"id": f"x{i}", "text": "t", "vector": rowvec(5000 + i)} for i in range(150)
    ]}]
    asyncio.run(col._process_job({"documents": ingest_more}))
    with pytest.raises(ValueError, match="too large"):
        asyncio.run(col.request_index(64, None))  # 160 rows < 8*64


def test_attach_fails_without_vectors_or_text(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    col.db.execute("DELETE FROM vecs WHERE id<=5")  # legacy rows: no retained vector
    col.db.execute("UPDATE records SET text=NULL WHERE id<=5")  # ...and nothing to re-embed
    col.db.commit()
    with pytest.raises(ValueError, match="re-ingest"):
        asyncio.run(col._process_job({"op": "attach_index", "nlist": 8, "nprobe": None}))


def test_nprobe_only_retune_skips_rebuild(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=2)
    obj = col.index
    asyncio.run(col._process_job({"op": "attach_index", "nlist": None, "nprobe": 6}))
    assert col.index is obj  # no rebuild
    assert col.index.nprobe == 6
    assert col.cfg.index_config == {"nlist": 8, "nprobe": 6}


def test_filtered_search_uses_owning_shards(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300, meta={i: {"src": "wiki"} for i in range(3)})
    attach(col, nlist=8, nprobe=1)  # nprobe=1 would miss rows in unprobed shards...
    ids = top_ids(col, 1, k=3, filt={"src": "wiki"})
    assert set(ids) == {"c0", "c1", "c2"}  # ...but tiny allowlists probe exactly the owners


def test_vecs_table_created_on_legacy_db(tmp_path):
    db = sqlite3.connect(tmp_path / "meta.db")  # pre-retention schema
    db.execute(
        "CREATE TABLE records(id INTEGER PRIMARY KEY, external_id TEXT UNIQUE, doc_id TEXT,"
        " type TEXT, position INTEGER, text TEXT, metadata TEXT, indexed INTEGER DEFAULT 0)"
    )
    db.commit()
    db.close()
    db = open_meta_db(tmp_path / "meta.db")
    assert db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0] == 0
    db.close()


# ---- API surface ----

API_DIM = 32
ROOT = {"x-api-key": "root-key"}


def wait_job(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/collections/t/jobs/{job_id}", headers=ROOT).json()
        if r["status"] in ("done", "error"):
            return r
        time.sleep(0.05)
    raise TimeoutError("job did not finish")


def test_index_api_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)
    with TestClient(create_app(embedder_factory=lambda cfg: None)) as client:
        assert client.post("/collections", headers=ROOT,
                           json={"name": "t", "dim": API_DIM}).status_code == 201
        rng = np.random.default_rng(0)
        chunks = [{"id": f"c{i}", "text": f"chunk {i}",
                   "vector": rng.standard_normal(API_DIM).tolist()} for i in range(300)]
        r = client.post("/collections/t/documents", headers=ROOT,
                        json={"documents": [{"doc_id": "d", "chunks": chunks}]})
        assert r.status_code == 202
        assert wait_job(client, r.json()["job_id"])["status"] == "done"

        info = client.get("/collections/t", headers=ROOT).json()
        assert info["index"] == {"type": "flat"}
        r = client.post("/collections/t/index", headers=ROOT, json={"nlist": 8})
        assert r.status_code == 202
        assert wait_job(client, r.json()["job_id"])["status"] == "done"
        info = client.get("/collections/t", headers=ROOT).json()
        assert info["index"] == {"type": "ivf", "nlist": 8, "nprobe": store.IVF_DEFAULT_NPROBE}

        q = chunks[7]["vector"]
        hits = client.post("/collections/t/search", headers=ROOT,
                           json={"query": {"vector": q}, "k": 3, "nprobe": 8}).json()["hits"]
        assert hits[0]["id"] == "c7"

        r = client.delete("/collections/t/index", headers=ROOT)
        assert r.status_code == 202
        assert wait_job(client, r.json()["job_id"])["status"] == "done"
        assert client.get("/collections/t", headers=ROOT).json()["index"] == {"type": "flat"}
        assert client.delete("/collections/t/index", headers=ROOT).status_code == 404
