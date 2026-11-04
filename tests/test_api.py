import json
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import zlib

import numpy as np
import pytest
from fastapi.testclient import TestClient

import raggio.app as app_module
from raggio.app import create_app

DIM = 32
ROOT = {"x-api-key": "root-key"}

# the deadline of each wait for something that must happen. Generous on purpose: with
# every CPU busy, a thread hand-off on the free-threaded build can take a second or more
# (a contended PyMutex yields the CPU up to 40 times before it parks)
WAIT_SECONDS = 360


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


def wait_job(client, coll, job_id, timeout=WAIT_SECONDS):
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


@pytest.mark.parametrize("options", [
    {"mode": "vector"},
    {"mode": "text"},
    {"mode": "hybrid"},
    {"mode": "hybrid", "fusion": "relative", "alpha": 0.25},
    {"mode": "hybrid", "fusion": "relative", "alpha": 0},
    {"mode": "hybrid", "fusion": "relative", "alpha": 1},
    {"mode": "vector", "scope": "summaries"},
    {"mode": "vector", "scope": "both", "k": 1000},
    {"mode": "vector", "expand": {"siblings_topk": 2, "summary": True}},
    {"mode": "text", "expand": {"siblings_topk": 2, "summary": True}},
    {"mode": "hybrid", "expand": {"siblings_all": True, "summary": True}},
    {"mode": "vector", "filter": {"doc_id": "missing"}},
])
@pytest.mark.parametrize("encoder_keys", [False, True])
def test_search_response_bytes_match_fastapi_default(client, monkeypatch, options, encoder_keys):
    client.post("/collections", headers=ROOT, json={"name": "main", "dim": DIM}).raise_for_status()
    document = json.loads(json.dumps(DOC))
    document["chunks"][0]["metadata"].update({
        "nested": {"array": [None, True, False, -0.0, 2**100, "café Σ漢字\n\t\\\""]},
        "number": 1.0000000000000002,
    })
    if encoder_keys:
        document["summary"]["metadata"]["_sample"] = "hidden by FastAPI"
        for chunk in document["chunks"]:
            chunk["metadata"].update({"_sample": "hidden by FastAPI", "ordinary": "kept",
                                      "nested_encoder_keys": [{"_sa_hidden": 1, "visible": {"_safe": 2, "ok": 3}}]})
    ingest(client, "main", [document])
    body = {"query": {"text": "planet"}, **options}
    direct = client.post("/collections/main/search", headers=ROOT, json=body)
    # Returning the dict restores FastAPI's ordinary jsonable_encoder + JSONResponse
    # path while retaining exactly the same retrieval implementation and fixture.
    with monkeypatch.context() as reference:
        reference.setattr(app_module, "_search_response", lambda hits: {"hits": hits})
        baseline = client.post("/collections/main/search", headers=ROOT, json=body)
    assert direct.status_code == baseline.status_code == 200
    assert direct.content == baseline.content
    assert direct.headers["content-type"] == baseline.headers["content-type"] == "application/json"
    assert direct.headers["content-length"] == baseline.headers["content-length"]


@pytest.mark.parametrize("value", [
    b"caf\xc3\xa9", datetime(2026, 11, 4, 10, 30), Path("logical/path"),
    ({"_sa_hidden": 1, "visible": 2},), {1: "numeric key", None: "null key"}, np.float64(1.25),
])
def test_search_response_preserves_other_fastapi_conversions(client, monkeypatch, value):
    from raggio.store import Collection

    client.post("/collections", headers=ROOT, json={"name": "main", "dim": DIM}).raise_for_status()

    async def results(self, *args, **kwargs):
        return [{"id": "c", "doc_id": "d", "type": "chunk", "position": 0, "text": "text",
                 "score": 0.5, "metadata": {"value": value}}]

    monkeypatch.setattr(Collection, "search", results)
    body = {"query": {"text": "planet"}, "mode": "text"}
    direct = client.post("/collections/main/search", headers=ROOT, json=body)
    with monkeypatch.context() as reference:
        reference.setattr(app_module, "_search_response", lambda hits: {"hits": hits})
        baseline = client.post("/collections/main/search", headers=ROOT, json=body)
    assert direct.status_code == baseline.status_code == 200
    assert direct.content == baseline.content


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_search_response_keeps_nonfinite_metadata_error(client, monkeypatch, value):
    client.post("/collections", headers=ROOT, json={"name": "main", "dim": DIM}).raise_for_status()
    document = json.loads(json.dumps(DOC))
    for chunk in document["chunks"]:
        chunk["metadata"]["nonfinite"] = value
    response = client.post("/collections/main/documents", headers={**ROOT, "content-type": "application/json"},
                           content=json.dumps({"documents": [document]}))
    assert response.status_code == 202
    assert wait_job(client, "main", response.json()["job_id"])["status"] == "done"
    body = {"query": {"text": "planet"}, "mode": "text"}
    with pytest.raises(ValueError, match="Out of range float values"):
        client.post("/collections/main/search", headers=ROOT, json=body)
    with monkeypatch.context() as reference:
        reference.setattr(app_module, "_search_response", lambda hits: {"hits": hits})
        with pytest.raises(ValueError, match="Out of range float values"):
            client.post("/collections/main/search", headers=ROOT, json=body)


def test_search_response_deep_or_cyclic_values_use_the_encoder():
    deep = {"value": 0}
    for _ in range(70):
        deep = {"nested": [deep]}
    hits = [{"metadata": deep}]
    assert app_module._search_response(hits)["hits"] is hits
    cyclic = []
    cyclic.append(cyclic)
    hits = [{"metadata": {"value": cyclic}}]
    assert app_module._search_response(hits)["hits"] is hits


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


def test_listing_filter_ops_and_patch(client):
    client.post("/collections", headers=ROOT, json={"name": "main"})
    ingest(client, "main", [DOC])
    url = "/collections/main/documents"
    ids = lambda r: [x["id"] for x in r.json()["records"]]
    flt = lambda f, **kw: client.get(url, headers=ROOT, params={"scope": "chunks", "filter": json.dumps(f), **kw})

    # unranked listing: total honors scope+filter; offset/limit page; default order = ingest order
    r = client.get(url, headers=ROOT, params={"scope": "chunks", "limit": 2}).json()
    assert r["total"] == 3 and [x["id"] for x in r["records"]] == ["c1", "c2"]
    assert ids(client.get(url, headers=ROOT, params={"scope": "chunks", "limit": 2, "offset": 2})) == ["c3"]
    r = client.get(url, headers=ROOT, params={"scope": "summaries"}).json()
    assert r["total"] == 1 and r["records"][0]["type"] == "summary" and "score" not in r["records"][0]
    assert client.get(url, headers=ROOT).json()["total"] == 4  # default scope: both

    # sort on a metadata key, both directions
    assert ids(client.get(url, headers=ROOT, params={"scope": "chunks", "sort": "-date"})) == ["c2", "c1", "c3"]
    assert ids(client.get(url, headers=ROOT, params={"scope": "chunks", "sort": "date"})) == ["c3", "c1", "c2"]

    # filter grammar: equality, in (explicit + list shorthand), contains (case-insensitive), ANDed
    assert flt({"src": "wiki"}).json()["total"] == 2
    assert ids(flt({"src": {"in": ["blog"]}})) == ["c3"]
    assert flt({"src": ["blog", "wiki"]}).json()["total"] == 3
    assert ids(flt({"src": {"contains": "IK"}})) == ["c1", "c2"]
    assert ids(flt({"src": {"contains": "log"}, "date": {"lt": "2026-01-01"}})) == ["c3"]

    # vectors on request: decoded fp16 originals, unit length
    rec = flt({"src": "blog"}, include_vector="true").json()["records"][0]
    assert len(rec["vector"]) == DIM and abs(sum(v * v for v in rec["vector"]) - 1) < 1e-2

    # the new operators apply in every search mode
    for mode in ("vector", "text", "hybrid"):
        r = client.post("/collections/main/search", headers=ROOT,
                        json={"query": {"text": "planet"}, "mode": mode, "k": 10,
                              "filter": {"src": {"contains": "LOG"}}})
        assert [h["id"] for h in r.json()["hits"]] == ["c3"], mode

    # bad input is a 400, never a 500
    assert client.get(url, headers=ROOT, params={"filter": "not json"}).status_code == 400
    assert client.get(url, headers=ROOT, params={"filter": "[1]"}).status_code == 400
    assert client.get(url, headers=ROOT, params={"sort": "date; DROP TABLE records"}).status_code == 400
    assert flt({"src": {"like": "x"}}).status_code == 400
    assert flt({"src": {"in": []}}).status_code == 400
    assert flt({"src": {"in": [[1]]}}).status_code == 400

    # metadata merge-patch: summary + chunks, existing keys kept, filters see the change
    r = client.patch("/collections/main/documents/doc1", headers=ROOT,
                     json={"metadata": {"src": "archive", "reviewed": True}})
    assert r.json() == {"patched_records": 4}
    doc = client.get("/collections/main/documents/doc1", headers=ROOT).json()
    assert doc["summary"]["metadata"] == {"kind": "summary", "src": "archive", "reviewed": True}
    assert all(c["metadata"]["src"] == "archive" and "date" in c["metadata"] for c in doc["chunks"])
    r = client.post("/collections/main/search", headers=ROOT,
                    json={"query": {"text": "planet"}, "mode": "text", "k": 10, "filter": {"src": "archive"}})
    assert len(r.json()["hits"]) == 3
    # null deletes a key; apply_to_chunks=false touches the summary only
    r = client.patch("/collections/main/documents/doc1", headers=ROOT,
                     json={"metadata": {"kind": None}, "apply_to_chunks": False})
    assert r.json() == {"patched_records": 1}
    assert "kind" not in client.get("/collections/main/documents/doc1", headers=ROOT).json()["summary"]["metadata"]
    assert client.patch("/collections/main/documents/nope", headers=ROOT, json={"metadata": {}}).status_code == 404


DOC_B = {
    "doc_id": "doc2",
    "chunks": [
        {"id": "b1", "text": "the closest star is the sun", "metadata": {"src": "wiki"}},
        {"id": "b2", "text": "a planet orbits a star", "metadata": {"src": "blog", "doc_id": "doc1"}},
    ],
}


def test_relative_fusion(client):
    client.post("/collections", headers=ROOT, json={"name": "main"})
    ingest(client, "main", [DOC, DOC_B])

    def search(**body):
        r = client.post("/collections/main/search", headers=ROOT,
                        json={"query": {"text": "closest star"}, "k": 100, **body})
        assert r.status_code == 200, r.text
        return r.json()["hits"]

    ids = lambda hits: [h["id"] for h in hits]
    vec, text = search(mode="vector"), search(mode="text")
    norm = lambda hits: {h["id"]: (h["score"] - hits[-1]["score"]) / (hits[0]["score"] - hits[-1]["score"])
                         for h in hits}
    assert len(vec) == 5 and ids(text)[0] == "b1" and len(text) == 3  # c1, b1, b2 match a term

    # alpha 1 / 0 reproduce the single-leg rankings, rescaled to [0, 1]
    hits = search(mode="hybrid", fusion="relative", alpha=1)
    assert ids(hits) == ids(vec) and hits[0]["score"] == 1.0 and hits[-1]["score"] == 0.0
    hits = search(mode="hybrid", fusion="relative", alpha=0)
    assert ids(hits) == ids(text) and hits[0]["score"] == 1.0
    # a mixed alpha is alpha * vector + (1 - alpha) * text over the min-max normalised legs
    v, t = norm(vec), norm(text)
    want = {rid: 0.3 * v.get(rid, 0.0) + 0.7 * t.get(rid, 0.0) for rid in {*v, *t}}
    hits = search(mode="hybrid", fusion="relative", alpha=0.3)
    assert ids(hits) == sorted(want, key=lambda rid: -want[rid])
    assert all(h["score"] == pytest.approx(want[h["id"]]) and 0 <= h["score"] <= 1 for h in hits)
    hits = search(mode="hybrid", fusion="relative", k=2)  # default alpha 0.5, k still applies
    assert len(hits) == 2 and all(0 <= h["score"] <= 1 for h in hits)

    # rrf stays the default, and ignores alpha
    rrf = search(mode="hybrid")
    assert rrf == search(mode="hybrid", fusion="rrf") == search(mode="hybrid", alpha=0.9)
    assert all(0 < h["score"] <= 2 / 61 for h in rrf)

    for bad in ({"alpha": -0.1}, {"alpha": 1.5}, {"fusion": "weighted"}):
        r = client.post("/collections/main/search", headers=ROOT,
                        json={"query": {"text": "planet"}, "mode": "hybrid", "fusion": "relative", **bad})
        assert r.status_code == 422, bad


def test_doc_id_filter(client):
    client.post("/collections", headers=ROOT, json={"name": "main"})
    ingest(client, "main", [DOC, DOC_B])

    def found(filt, mode="vector", scope="chunks", status=200):
        r = client.post("/collections/main/search", headers=ROOT,
                        json={"query": {"text": "planet"}, "mode": mode, "k": 10, "scope": scope, "filter": filt})
        assert r.status_code == status, r.text
        return {h["id"] for h in r.json()["hits"]} if status == 200 else None

    # equality and `in` (list shorthand and explicit), in every mode
    for mode in ("vector", "hybrid"):
        assert found({"doc_id": "doc2"}, mode) == {"b1", "b2"}, mode
        assert found({"doc_id": ["doc1", "doc2"]}, mode) == {"c1", "c2", "c3", "b1", "b2"}, mode
        assert found({"doc_id": {"in": ["doc2", "nope"]}}, mode) == {"b1", "b2"}, mode
    assert found({"doc_id": "doc2"}, "text") == {"b2"}  # BM25: only b2 says "planet"
    assert found({"doc_id": "nope"}) == set()
    # ANDed with metadata filters
    for mode in ("vector", "text", "hybrid"):
        assert found({"doc_id": "doc1", "src": "wiki"}, mode) == {"c1", "c2"}, mode
    # scope still applies; doc2 has no summary
    assert found({"doc_id": "doc1"}, scope="summaries") == {"doc1"}
    assert found({"doc_id": "doc1"}, scope="both") == {"doc1", "c1", "c2", "c3"}
    assert found({"doc_id": "doc2"}, scope="summaries") == set()
    # reserved: b2's metadata key doc_id is not what the filter reads
    assert "b2" not in found({"doc_id": "doc1"})
    # equality and `in` only
    for bad in ({"gte": "doc1"}, {"contains": "doc"}, {"in": []}, {"like": "doc%"}):
        found({"doc_id": bad}, status=400)

    # listing: the same key, with paging and total
    url = "/collections/main/documents"
    lst = lambda f, **kw: client.get(url, headers=ROOT, params={"filter": json.dumps(f), **kw})
    r = lst({"doc_id": "doc1"}, scope="chunks", limit=2).json()
    assert r["total"] == 3 and [x["id"] for x in r["records"]] == ["c1", "c2"]
    r = lst({"doc_id": "doc1"}, scope="chunks", limit=2, offset=2).json()
    assert [x["id"] for x in r["records"]] == ["c3"]
    assert [x["id"] for x in lst({"doc_id": "doc1"}, scope="chunks", sort="-date").json()["records"]] == ["c2", "c1", "c3"]
    assert lst({"doc_id": ["doc1", "doc2"], "src": "blog"}).json()["total"] == 2
    assert lst({"doc_id": "doc1"}).json()["total"] == 4  # default scope: both
    assert lst({"doc_id": {"lt": "doc2"}}).status_code == 400


# ---- one process per DATA_DIR (ADR 0007) ----


def wait_health(client, status):
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        r = client.get("/healthz")
        if r.json()["status"] == status:
            return r
        time.sleep(0.02)
    raise TimeoutError(f"/healthz never said {status!r}")


def fake_held_lock(monkeypatch, tmp_path, free):
    """Another process holds the data-dir lock until free() says True (any platform)."""
    fd = os.open(tmp_path / "fake.lock", os.O_RDWR | os.O_CREAT)
    monkeypatch.setattr(app_module, "open_data_dir_lock", lambda data_dir: fd)
    monkeypatch.setattr(app_module, "lock_data_dir", lambda fd: free())
    monkeypatch.setattr(app_module, "DATA_DIR_LOCK_POLL_S", 0.01)


def test_start_waits_while_another_process_holds_the_data_dir(make_app, monkeypatch, tmp_path):
    held = threading.Event()
    held.set()
    fake_held_lock(monkeypatch, tmp_path, lambda: not held.is_set())
    with TestClient(make_app()) as c:  # the server answers at once: the lifespan did not wait
        r = c.get("/healthz")
        assert r.status_code == 200 and r.json() == {"status": "starting"}
        for r in (c.get("/collections", headers=ROOT),
                  c.post("/collections/x/search", headers=ROOT, json={})):  # before validation too
            assert r.status_code == 503 and r.headers["retry-after"] == "2"
        assert not (tmp_path / "catalog.db").exists()  # nothing in DATA_DIR opened yet
        held.clear()
        wait_health(c, "ok")
        assert c.get("/collections", headers=ROOT).json() == {"collections": []}


def test_failed_start_reports_unhealthy(make_app, monkeypatch, tmp_path):
    tries = iter([False])  # held at the first try only
    fake_held_lock(monkeypatch, tmp_path, lambda: next(tries, True))

    def broken(*args, **kwargs):
        raise RuntimeError("catalog unreadable")

    monkeypatch.setattr(app_module, "CollectionManager", broken)
    with TestClient(make_app()) as c:
        assert wait_health(c, "failed").status_code == 503  # the platform restarts it
        assert c.get("/collections", headers=ROOT).status_code == 503


LOCK_CHILD = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("held", flush=True)
    sys.exit()
print("locked", flush=True)
sys.stdin.read()  # holds the lock until the test closes stdin
"""


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl record locks are POSIX-only")
def test_data_dir_lock_keeps_a_second_process_out(make_app, tmp_path):
    def child():
        return subprocess.Popen([sys.executable, "-c", LOCK_CHILD, str(tmp_path / "raggio.lock")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    holder = child()
    try:
        assert holder.stdout.readline().strip() == "locked"
        with TestClient(make_app()) as c:
            assert c.get("/healthz").json() == {"status": "starting"}
            assert c.get("/collections", headers=ROOT).status_code == 503
            holder.stdin.close()  # the other process exits
            assert holder.wait(WAIT_SECONDS) == 0
            wait_health(c, "ok")
            assert c.get("/collections", headers=ROOT).status_code == 200
            assert child().communicate(timeout=WAIT_SECONDS)[0].strip() == "held"  # now ours
        assert child().communicate(timeout=WAIT_SECONDS)[0].strip() == "locked"  # released at shutdown
    finally:
        holder.kill()


def test_data_dir_with_uri_characters(tmp_path, monkeypatch):
    # every database opens through a file: URI on POSIX (ADR 0007): the path is escaped there
    data = tmp_path / ("data dir 100% #1" + ("" if sys.platform == "win32" else " ?vfs=unix"))
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(data))
    for first in (True, False):  # the second start reopens the catalog and each collection
        with TestClient(create_app(embedder_factory=lambda cfg: FakeEmbedder())) as c:
            if first:
                assert c.post("/collections", headers=ROOT, json={"name": "main"}).status_code == 201
                ingest(c, "main", [DOC])
            r = c.post("/collections/main/search", headers=ROOT,
                       json={"query": {"text": "jupiter"}, "mode": "hybrid", "k": 1})
            assert r.json()["hits"][0]["id"] == "c2"
    assert (data / "catalog.db").exists() and (data / "collections" / "main" / "meta.db").exists()

