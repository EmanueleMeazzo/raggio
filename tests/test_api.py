import json
import sqlite3
import time
import zlib

import numpy as np
import pytest
from fastapi.testclient import TestClient

from raggio.app import create_app

DIM = 32
ROOT = {"x-api-key": "root-key"}


class FakeEmbedder:
    """Deterministic: same text -> same unit vector; different texts ~orthogonal."""

    async def embed(self, texts):
        out = []
        for t in texts:
            rng = np.random.default_rng(zlib.crc32(t.encode()))
            v = rng.standard_normal(DIM)
            out.append((v / np.linalg.norm(v)).tolist())
        return out

    async def aclose(self):
        pass


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return lambda: create_app(embedder_factory=lambda cfg: FakeEmbedder())


@pytest.fixture
def client(make_app):
    with TestClient(make_app()) as c:
        yield c


def wait_job(client, coll, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/collections/{coll}/jobs/{job_id}", headers=ROOT).json()
        if r["status"] in ("done", "error"):
            return r
        time.sleep(0.05)
    raise TimeoutError("job did not finish")


DOC = {
    "doc_id": "doc1",
    "summary": {"text": "a summary about the solar system", "metadata": {"kind": "summary"}},
    "chunks": [
        {"id": "c1", "text": "mercury is the closest planet", "metadata": {"src": "wiki", "date": "2026-01-15"}},
        {"id": "c2", "text": "jupiter is the largest planet", "metadata": {"src": "wiki", "date": "2026-07-01"}},
        {"id": "c3", "text": "pluto is a dwarf planet", "metadata": {"src": "blog", "date": "2025-12-31"}},
    ],
}


def ingest(client, coll, docs):
    r = client.post(f"/collections/{coll}/documents", headers=ROOT, json={"documents": docs})
    assert r.status_code == 202, r.text
    job = wait_job(client, coll, r.json()["job_id"])
    assert job["status"] == "done", job
    return job


def test_full_lifecycle(client):
    r = client.post("/collections", headers=ROOT, json={"name": "main"})
    assert r.status_code == 201 and r.json()["dim"] == DIM  # dim probed from embedder
    ingest(client, "main", [DOC])

    r = client.get("/collections/main", headers=ROOT).json()
    assert (r["documents"], r["chunks"], r["summaries"]) == (1, 3, 1)

    # exact-text search ranks the matching chunk first; summaries excluded by default scope
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "jupiter is the largest planet"}, "k": 2})
    hits = r.json()["hits"]
    assert hits[0]["id"] == "c2" and hits[0]["score"] > 0.99
    assert all(h["type"] == "chunk" for h in hits)

    # scope=summaries finds the summary
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "a summary about the solar system"}, "scope": "summaries"})
    assert r.json()["hits"][0]["type"] == "summary"

    # equality filter
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "planet"}, "k": 10, "filter": {"src": "blog"}})
    assert [h["id"] for h in r.json()["hits"]] == ["c3"]

    # date range filter
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "planet"}, "k": 10,
                          "filter": {"date": {"gte": "2026-01-01", "lte": "2026-06-30"}}})
    assert [h["id"] for h in r.json()["hits"]] == ["c1"]

    # expansions: topk siblings exclude the hit; summary attached
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "jupiter is the largest planet"}, "k": 1,
                          "expand": {"siblings_topk": 2, "summary": True}})
    hit = r.json()["hits"][0]
    assert {s["id"] for s in hit["expansion"]["siblings"]} == {"c1", "c3"}
    assert hit["expansion"]["summary"]["doc_id"] == "doc1"

    # siblings_all in document order
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "mercury is the closest planet"}, "k": 1,
                          "expand": {"siblings_all": True}})
    assert [s["id"] for s in r.json()["hits"][0]["expansion"]["siblings"]] == ["c2", "c3"]

    # upsert: re-ingest c2 with new text, old vector must be gone
    doc2 = {"doc_id": "doc1", "chunks": [{"id": "c2", "text": "saturn has rings"}]}
    ingest(client, "main", [doc2])
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "saturn has rings"}, "k": 1})
    assert r.json()["hits"][0]["id"] == "c2"

    # fetch and delete document
    assert len(client.get("/collections/main/documents/doc1", headers=ROOT).json()["chunks"]) == 3
    assert client.delete("/collections/main/documents/doc1", headers=ROOT).status_code == 200
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "planet"}, "scope": "both"})
    assert r.json()["hits"] == []


def test_client_supplied_vectors(client):
    client.post("/collections", headers=ROOT, json={"name": "vecs", "dim": DIM})
    vec = [1.0] + [0.0] * (DIM - 1)
    doc = {"doc_id": "d", "chunks": [{"id": "v1", "text": "stored text", "vector": vec}]}
    ingest(client, "vecs", [doc])
    r = client.post("/collections/vecs/search", headers=ROOT, json={"query": {"vector": vec}, "k": 1})
    hit = r.json()["hits"][0]
    assert hit["id"] == "v1" and hit["text"] == "stored text" and hit["score"] > 0.9


def test_vector_only_without_embedding_endpoint(tmp_path, monkeypatch):
    # no EMBEDDING_BASE_URL and the real Embedder: vector-only usage must still work
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as c:
        assert c.post("/collections", headers=ROOT, json={"name": "v", "dim": 8}).status_code == 201
        assert c.post("/collections", headers=ROOT, json={"name": "odd", "dim": 12}).status_code == 400
        vec = [1.0] + [0.0] * 7
        ingest(c, "v", [{"doc_id": "d", "chunks": [{"id": "c1", "text": "t", "vector": vec}]}])
        r = c.post("/collections/v/search", headers=ROOT, json={"query": {"vector": vec}, "k": 1})
        assert r.json()["hits"][0]["id"] == "c1"
        # text query needs the endpoint -> clean 400, not a 500
        r = c.post("/collections/v/search", headers=ROOT, json={"query": {"text": "t"}})
        assert r.status_code == 400 and "EMBEDDING_BASE_URL" in r.json()["detail"]
        # ...but BM25 text mode works without any embedding endpoint
        r = c.post("/collections/v/search", headers=ROOT,
                   json={"query": {"text": "t"}, "mode": "text", "k": 1})
        assert r.json()["hits"][0]["id"] == "c1"
        # so does creating a collection without an explicit dim
        assert c.post("/collections", headers=ROOT, json={"name": "nodim"}).status_code == 400


def test_persistence_and_crash_replay(make_app, tmp_path):
    with TestClient(make_app()) as c:
        c.post("/collections", headers=ROOT, json={"name": "main"})
        ingest(c, "main", [DOC])

    # simulate a crash that left a job behind: insert it directly into the queue db
    db = sqlite3.connect(tmp_path / "collections" / "main" / "meta.db")
    payload = {"documents": [{"doc_id": "doc2", "chunks": [{"id": "n1", "text": "neptune is windy"}]}]}
    db.execute("INSERT INTO jobs(payload, status, created_at, updated_at) VALUES (?, 'processing', '', '')",
               (json.dumps(payload),))
    db.commit()
    job_id = db.execute("SELECT MAX(id) FROM jobs").fetchone()[0]
    db.close()

    with TestClient(make_app()) as c:  # boot replays the interrupted job
        assert wait_job(c, "main", job_id)["status"] == "done"
        db = sqlite3.connect(tmp_path / "collections" / "main" / "meta.db")
        assert db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()[0] is None
        db.close()
        r = c.post("/collections/main/search", headers=ROOT,
                   json={"query": {"text": "neptune is windy"}, "k": 1})
        assert r.json()["hits"][0]["id"] == "n1"
        r = c.post("/collections/main/search", headers=ROOT,  # pre-restart data intact
                   json={"query": {"text": "jupiter is the largest planet"}, "k": 1})
        assert r.json()["hits"][0]["id"] == "c2"


def test_auth(client):
    client.post("/collections", headers=ROOT, json={"name": "bu1", "collection_key": "bu1-key"})
    client.post("/collections", headers=ROOT, json={"name": "bu2"})
    search = {"query": {"text": "x"}, "k": 1}

    assert client.post("/collections/bu1/search", json=search).status_code == 401
    assert client.post("/collections/bu1/search", headers={"x-api-key": "wrong"}, json=search).status_code == 401
    ok = client.post("/collections/bu1/search", headers={"Authorization": "Bearer bu1-key"}, json=search)
    assert ok.status_code == 200
    # bu1's key must not reach bu2, and bu2 (no key set) is root-only
    assert client.post("/collections/bu2/search", headers={"x-api-key": "bu1-key"}, json=search).status_code == 401
    assert client.post("/collections/bu2/search", headers=ROOT, json=search).status_code == 200
    assert client.post("/collections", headers={"x-api-key": "bu1-key"}, json={"name": "nope"}).status_code == 401


def test_validation(client):
    client.post("/collections", headers=ROOT, json={"name": "main"})
    bad_dim = {"documents": [{"doc_id": "d", "chunks": [{"id": "c", "vector": [1.0, 2.0]}]}]}
    assert client.post("/collections/main/documents", headers=ROOT, json=bad_dim).status_code == 400
    empty = {"documents": [{"doc_id": "d", "chunks": [{"id": "c"}]}]}
    assert client.post("/collections/main/documents", headers=ROOT, json=empty).status_code == 400
    assert client.post("/collections/main/search", headers=ROOT, json={"query": {}}).status_code == 400
    assert client.post("/collections/missing/search", headers=ROOT,
                       json={"query": {"text": "x"}}).status_code == 404


def test_text_and_hybrid_modes(client):
    client.post("/collections", headers=ROOT, json={"name": "main"})
    ingest(client, "main", [DOC])

    # text mode: BM25 ranks the chunk containing the query terms first
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "jupiter largest"}, "mode": "text", "k": 3})
    hits = r.json()["hits"]
    assert hits[0]["id"] == "c2" and hits[0]["score"] > 0

    # metadata filters and scope apply in text mode too
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "planet"}, "mode": "text", "k": 10,
                          "filter": {"src": "blog"}})
    assert [h["id"] for h in r.json()["hits"]] == ["c3"]
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "solar system"}, "mode": "text", "scope": "summaries"})
    assert r.json()["hits"][0]["type"] == "summary"

    # hybrid: RRF-fused; the lexically matching chunk surfaces even though
    # FakeEmbedder makes different texts ~orthogonal
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "pluto dwarf"}, "mode": "hybrid", "k": 3})
    hits = r.json()["hits"]
    assert hits[0]["id"] == "c3" and 0 < hits[0]["score"] < 1

    # expansion in text mode: siblings ranked by BM25 ("planet" matches c2 and c3)
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "mercury closest planet"}, "mode": "text", "k": 1,
                          "expand": {"siblings_topk": 2, "summary": True}})
    hit = r.json()["hits"][0]
    assert hit["id"] == "c1"
    assert {s["id"] for s in hit["expansion"]["siblings"]} == {"c2", "c3"}
    assert hit["expansion"]["summary"]["doc_id"] == "doc1"


def test_mode_validation(client):
    client.post("/collections", headers=ROOT, json={"name": "main"})
    ingest(client, "main", [DOC])
    vec = [1.0] + [0.0] * (DIM - 1)
    for body in (
        {"query": {"vector": vec}, "mode": "text"},        # text needs text
        {"query": {"vector": vec}, "mode": "hybrid"},      # hybrid needs text
        {"query": {"text": "x", "vector": vec}, "mode": "text"},   # text rejects vectors
        {"query": {"text": "x", "vector": vec}, "mode": "vector"}, # vector: exactly one
    ):
        assert client.post("/collections/main/search", headers=ROOT, json=body).status_code == 400
    # empty/whitespace text is a clean 400 in every mode, never a 500 via the embedder
    for mode in ("vector", "text", "hybrid"):
        r = client.post("/collections/main/search", headers=ROOT,
                        json={"query": {"text": "   "}, "mode": mode})
        assert r.status_code == 400, mode
    # hybrid accepts text + vector: the vector replaces the embedding call
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "planet", "vector": vec}, "mode": "hybrid"})
    assert r.status_code == 200 and r.json()["hits"]


def test_trigram_collection(client):
    r = client.post("/collections", headers=ROOT, json={"name": "tri", "tokenizer": "trigram"})
    assert r.status_code == 201 and r.json()["tokenizer"] == "trigram"
    ingest(client, "tri", [{"doc_id": "d", "chunks": [
        {"id": "t1", "text": "PostgreSQL configuration parameters"}]}])
    # substring (and case-insensitive) match, impossible with unicode61
    r = client.post("/collections/tri/search", headers=ROOT,
                    json={"query": {"text": "gresql"}, "mode": "text", "k": 1})
    assert r.json()["hits"][0]["id"] == "t1"
    assert client.get("/collections/tri", headers=ROOT).json()["tokenizer"] == "trigram"


def test_fts_backfill_on_upgrade(make_app, tmp_path):
    with TestClient(make_app()) as c:
        c.post("/collections", headers=ROOT, json={"name": "main"})
        ingest(c, "main", [DOC])
    # simulate a collection created before FTS existed: drop the table and triggers
    db = sqlite3.connect(tmp_path / "collections" / "main" / "meta.db")
    db.executescript(
        "DROP TRIGGER records_fts_ai; DROP TRIGGER records_fts_ad; DROP TABLE records_fts;"
    )
    db.close()
    with TestClient(make_app()) as c:  # reload triggers the rebuild backfill
        r = c.post("/collections/main/search", headers=ROOT,
                   json={"query": {"text": "jupiter"}, "mode": "text", "k": 1})
        assert r.json()["hits"][0]["id"] == "c2"


def test_fts_tokenizer_selfheal(make_app, tmp_path):
    with TestClient(make_app()) as c:
        c.post("/collections", headers=ROOT, json={"name": "main"})
        ingest(c, "main", [DOC])
    # simulate a divergent FTS table (e.g. dir survived a crashed create with another
    # tokenizer): on reload it must be dropped and rebuilt with the catalog's tokenizer
    db = sqlite3.connect(tmp_path / "collections" / "main" / "meta.db")
    db.executescript(
        "DROP TRIGGER records_fts_ai; DROP TRIGGER records_fts_ad; DROP TABLE records_fts;"
        "CREATE VIRTUAL TABLE records_fts USING fts5("
        "text, content='records', content_rowid='id', tokenize='trigram');"
    )
    db.close()
    with TestClient(make_app()) as c:
        r = c.post("/collections/main/search", headers=ROOT,
                   json={"query": {"text": "jupiter"}, "mode": "text", "k": 1})
        assert r.json()["hits"][0]["id"] == "c2"  # rebuilt: legacy rows searchable again


def test_create_never_resurrects_leftover_dir(make_app, tmp_path):
    # a stale dir from a crashed create / failed delete must be wiped, not reused
    ghost = tmp_path / "collections" / "ghost"
    ghost.mkdir(parents=True)
    (ghost / "meta.db").write_bytes(b"not a database")
    with TestClient(make_app()) as c:
        assert c.post("/collections", headers=ROOT, json={"name": "ghost"}).status_code == 201
        assert c.get("/collections/ghost", headers=ROOT).json()["documents"] == 0


def test_eviction_roundtrip(make_app, monkeypatch):
    monkeypatch.setenv("MAX_RESIDENT_COLLECTIONS", "1")
    with TestClient(make_app()) as c:
        c.post("/collections", headers=ROOT, json={"name": "a"})
        c.post("/collections", headers=ROOT, json={"name": "b"})
        ingest(c, "a", [{"doc_id": "d", "chunks": [{"id": "a1", "text": "alpha text"}]}])
        ingest(c, "b", [{"doc_id": "d", "chunks": [{"id": "b1", "text": "beta text"}]}])  # evicts a
        assert c.get("/healthz").json()["resident_collections"] == ["b"]
        r = c.post("/collections/a/search", headers=ROOT, json={"query": {"text": "alpha text"}, "k": 1})
        assert r.json()["hits"][0]["id"] == "a1"  # reloaded from disk with data intact
        r = c.post("/collections/a/search", headers=ROOT,
                   json={"query": {"text": "alpha"}, "mode": "text", "k": 1})
        assert r.json()["hits"][0]["id"] == "a1"  # FTS index survives eviction too
