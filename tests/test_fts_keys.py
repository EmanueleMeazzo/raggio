"""R4: one BM25 text across FTS, rescoring, PATCH and collection lifecycle."""
import asyncio
import json
import sqlite3

import numpy as np
import pytest
from fastapi.testclient import TestClient

import raggio.store as store
from raggio.store import Collection, CollectionConfig, _fts_metadata, open_meta_db
from test_api import DIM, ROOT, client, ingest, make_app, wait_job


def integrity(db):
    # rank=1 also checks the postings against external content (stronger than default).
    db.execute("INSERT INTO records_fts(records_fts, rank) VALUES('integrity-check', 1)")
    db.commit()


def collection(path, keys=None, tokenizer="unicode61"):
    path.mkdir(parents=True, exist_ok=True)
    return Collection(CollectionConfig("t", 8, 4, None, None, None,
                                       tokenizer=tokenizer, fts_keys=keys), path, lambda: None)


@pytest.fixture
def col(tmp_path):
    c = collection(tmp_path, ["title", "tags"])
    yield c
    asyncio.run(c.stop())


def payload(title="quasar", text="body unchanged", doc_id="d", tags=None):
    record = {"text": text, "metadata": {"title": title, "tags": tags, "private": "secret"},
              "vector": [1.0] + [0.0] * 7}
    return {"documents": [{"doc_id": doc_id, "summary": record,
                           "chunks": [{"id": doc_id + "-c", **record}]}]}


@pytest.mark.parametrize("tokenizer", ["unicode61", "trigram"])
@pytest.mark.parametrize("scope", ["summaries", "chunks"])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mode,fusion", [("text", "rrf"), ("hybrid", "rrf"),
                                        ("hybrid", "relative")])
def test_title_only_search_and_original_returns(tmp_path, tokenizer, scope, native, mode, fusion):
    c = collection(tmp_path, ["title"], tokenizer)
    c._native_bm25 = native
    try:
        asyncio.run(c._process_job(payload()))
        integrity(c.db)
        vector = np.array([[1.0] + [0.0] * 7], dtype=np.float32) if mode == "hybrid" else None
        for filt in (None, {"doc_id": "d"}, {"title": {"contains": "QUASAR"}},
                     {"title": {"contains_any": ["missing", "QUASAR"]}}):
            hits = asyncio.run(c.search(mode, vector, "quasar", 10, scope, filt, None, fusion=fusion))
            assert len(hits) == 1
            assert hits[0]["text"] == "body unchanged"
            assert hits[0]["metadata"] == {"title": "quasar", "tags": None, "private": "secret"}
        assert not c._text_ids("secret", 10, scope, None)[0]
        assert not c._text_ids("quasar", 10, scope, {"title": {"contains": "body"}})[0]
    finally:
        asyncio.run(c.stop())


@pytest.mark.parametrize("metadata,expected", [
    ({"title": "hello", "tags": ["one", 2, True, 1.0]}, "\nhello\none 2 True 1.0"),
    ({"title": 1.0000000000000002, "tags": False}, "\n1.0000000000000002\nFalse"),
    ({"title": -0.0, "tags": 1e-20}, "\n-0.0\n1e-20"),
    ({"title": 2**100, "tags": [None, False, "x"]}, "\n1267650600228229401496703205376\nFalse x"),
    ({"title": {"ignored": "x"}, "tags": None}, ""),
    ({"title": ["x", {"object": 1}], "tags": [["nested"]]}, ""),
    ({"title": "", "tags": []}, "\n\n"),
    ({}, ""),
])
def test_value_conversion_and_null_body(col, metadata, expected):
    assert _fts_metadata(metadata, col.cfg.fts_keys) == expected
    p = payload(text=None)
    for record in (p["documents"][0]["summary"], *p["documents"][0]["chunks"]):
        record["metadata"] = metadata
    asyncio.run(col._process_job(p))
    assert col.db.execute("SELECT bm25_text FROM records").fetchall() == [(expected,), (expected,)]
    assert all(row[0] is None for row in col.db.execute("SELECT text FROM records"))
    integrity(col.db)


def test_literal_keys_and_order(tmp_path):
    c = collection(tmp_path, ["a.b", "title"])
    try:
        p = payload(text=None)
        for r in (p["documents"][0]["summary"], *p["documents"][0]["chunks"]):
            r["metadata"] = {"a.b": "literal", "a": {"b": "nested"}, "title": "quasar"}
        asyncio.run(c._process_job(p))
        assert c.db.execute("SELECT bm25_text FROM records LIMIT 1").fetchone()[0] == "\nliteral\nquasar"
        assert c._text_ids("quasar", 5, "chunks", None)[0]
        assert not c._text_ids("nested", 5, "chunks", None)[0]
        integrity(c.db)
    finally:
        asyncio.run(c.stop())


def test_empty_selected_values_do_not_fail_direct_search(col):
    asyncio.run(col._process_job(payload(title="", text=None, tags=[])))
    assert col._avgdl() == 1.0
    assert col._text_ids("missing", 5, "both", {"doc_id": "d"}) == ([], [])
    integrity(col.db)


def test_patch_upsert_delete_and_caches(col):
    asyncio.run(col._process_job(payload(tags=["cosmic", "dust"])))
    integrity(col.db)
    assert col._df("quasar") == 2
    assert col._avgdl() == 5
    before = list(col.db.execute("SELECT * FROM records_fts_data ORDER BY id"))
    assert asyncio.run(col.patch_metadata("d", {"private": "changed"}, True)) == 2
    assert list(col.db.execute("SELECT * FROM records_fts_data ORDER BY id")) == before
    integrity(col.db)
    # Summary-only patch must not change chunks; df and length caches invalidate now.
    assert asyncio.run(col.patch_metadata("d", {"title": "nebula nebula", "tags": None}, False)) == 1
    assert not col._df_cache and col._avgdl_cache is None
    assert col._df("quasar") == 1 and col._df("nebula") == 1
    assert col._text_ids("quasar", 5, "chunks", {"doc_id": "d"})[0]
    assert not col._text_ids("quasar", 5, "summaries", {"doc_id": "d"})[0]
    assert col._text_ids("nebula", 5, "summaries", {"doc_id": "d"})[0]
    integrity(col.db)
    asyncio.run(col.patch_metadata("d", {"title": None, "tags": None}, True))
    assert not col._text_ids("quasar", 5, "both", None)[0]
    integrity(col.db)
    asyncio.run(col._process_job(payload(title="replacement")))
    assert not col._text_ids("nebula", 5, "both", None)[0]
    assert len(col._text_ids("replacement", 5, "both", None)[0]) == 2
    integrity(col.db)
    asyncio.run(col.delete_document("d"))
    assert not col._text_ids("replacement", 5, "both", None)[0]
    integrity(col.db)


def test_patch_failure_rolls_back_metadata_and_fts(col):
    asyncio.run(col._process_job(payload()))
    col.db.executescript("CREATE TRIGGER fail_patch AFTER UPDATE ON records"
                        " WHEN new.type='chunk' BEGIN SELECT RAISE(ABORT, 'injected'); END;")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        asyncio.run(col.patch_metadata("d", {"title": "changed"}, True))
    assert len(col._text_ids("quasar", 5, "both", None)[0]) == 2
    assert not col._text_ids("changed", 5, "both", None)[0]
    assert all(json.loads(r[0])["title"] == "quasar" for r in col.db.execute("SELECT metadata FROM records"))
    integrity(col.db)


def test_rescore_direct_avgdl_and_native_parity(col, monkeypatch):
    asyncio.run(col._process_job(payload(text="body", tags=["rare", "word"])))
    assert col._avgdl() == 4
    cand = list(col.db.execute("SELECT id, bm25_text FROM records"))
    # Pin exact FTS corpus length; one query token has no proximity term.
    monkeypatch.setattr(col, "_avgdl", lambda: 4.0)
    ranked = col.db.execute("SELECT rowid, -bm25(records_fts) FROM records_fts"
                            " WHERE records_fts MATCH 'quasar' ORDER BY bm25(records_fts)").fetchall()
    col._native_bm25 = False
    reference = col._bm25_rescore("quasar", cand, 5)
    assert reference[0] == [r[0] for r in ranked]
    assert reference[1] == pytest.approx([r[1] for r in ranked], rel=1e-12)
    assert col._text_ids("quasar", 5, "both", {"doc_id": "d"}) == reference
    # Force two-stage path, so candidate fetches must use generated combined text.
    monkeypatch.setattr(col, "_prune_common", lambda text: (["rare"], ["quasar", "rare"]))
    stage2 = col._text_ids("quasar rare", 5, "both", None)
    assert stage2 == col._bm25_rescore("quasar rare", cand, 5)
    if store._native is not None:
        col._native_bm25 = True
        assert col._bm25_rescore("quasar", cand, 5) == reference
        assert col._text_ids("quasar rare", 5, "both", None) == stage2


@pytest.mark.parametrize("tokenizer", ["unicode61", "trigram"])
def test_text_siblings_use_title(col, tokenizer, monkeypatch):
    # Dedicated tokenizer collection for expansion through FTS5, not direct rescoring.
    from raggio.app import ExpandIn
    if tokenizer == "trigram":
        store._ensure_fts(col.db, tokenizer, "bm25_text")
        col.cfg.tokenizer = tokenizer
    p = payload()
    p["documents"][0]["chunks"].append({"id": "other", "text": "other body",
                                        "metadata": {"title": "quasar"}, "vector": [1.0] * 8})
    asyncio.run(col._process_job(p))
    hits = asyncio.run(col.search("text", None, "quasar", 1, "summaries", None,
                                  ExpandIn(siblings_topk=2)))
    assert {s["id"] for s in hits[0]["expansion"]["siblings"]} == {"d-c", "other"}
    integrity(col.db)


@pytest.mark.parametrize("keys", [None, []])
def test_default_record_and_fts_schema_and_reopen(tmp_path, keys):
    old = open_meta_db(tmp_path / "meta.db")
    schema = old.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    old.close()
    c = collection(tmp_path, keys)
    try:
        assert c.db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
        asyncio.run(c._process_job(payload()))
        assert not c._text_ids("quasar", 5, "both", None)[0]
        original = c._text_ids("body", 5, "both", None)
    finally:
        asyncio.run(c.stop())
    c = collection(tmp_path, keys)
    try:
        assert c._text_ids("body", 5, "both", None) == original
        assert c.db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
        integrity(c.db)
    finally:
        asyncio.run(c.stop())


@pytest.mark.parametrize("keys", [["doc_id"], ["title", "title"], ["a"] * 9,
                                  [f"k{i}" for i in range(9)], [""], ["x" * 65],
                                  ["title'"], ["nested[0]"], ["café"], [1], "title", {}])
def test_api_invalid_keys(client, keys):
    r = client.post("/collections", headers=ROOT, json={"name": "bad", "dim": DIM, "fts_keys": keys})
    assert r.status_code in (400, 422)
    assert client.get("/collections", headers=ROOT).json()["collections"] == []


@pytest.mark.parametrize("keys", [None, [], ["title", "source", "tags"], [f"k{i}" for i in range(8)]])
def test_api_keys_persist_and_original_listing(make_app, keys):
    with TestClient(make_app()) as c:
        r = c.post("/collections", headers=ROOT, json={"name": "main", "dim": DIM, "fts_keys": keys})
        assert r.status_code == 201 and r.json()["fts_keys"] == keys
        doc = payload()["documents"][0]
        for rec in (doc["summary"], *doc["chunks"]):
            rec["vector"] = [1.0] + [0.0] * (DIM - 1)
        ingest(c, "main", [doc])
        result = c.get("/collections/main/documents/d", headers=ROOT).json()
        assert result["summary"]["text"] == "body unchanged"
        assert result["summary"]["metadata"]["title"] == "quasar"
        listing = c.get("/collections/main/documents", headers=ROOT).json()["records"]
        assert all(r["text"] == "body unchanged" and r["metadata"]["title"] == "quasar" for r in listing)
    with TestClient(make_app()) as c:
        assert c.get("/collections/main", headers=ROOT).json()["fts_keys"] == keys


def test_api_patch_and_null_vector_only_title(client):
    client.post("/collections", headers=ROOT, json={"name": "main", "dim": 8,
                                                   "fts_keys": ["title", "tags"]}).raise_for_status()
    ingest(client, "main", payload(text=None, tags=[1.0, True])["documents"])
    for title in ("quasar", "nebula"):
        response = client.post("/collections/main/search", headers=ROOT,
                               json={"mode": "text", "query": {"text": title}, "scope": "both",
                                     "filter": {"doc_id": "d"}})
        assert len(response.json()["hits"]) == 2
        assert all(h["text"] is None and h["metadata"]["tags"] == [1.0, True]
                   for h in response.json()["hits"])
        client.patch("/collections/main/documents/d", headers=ROOT,
                     json={"metadata": {"title": "nebula"}}).raise_for_status()
    response = client.post("/collections/main/search", headers=ROOT,
                           json={"mode": "text", "query": {"text": "quasar"}, "scope": "both"})
    assert response.json()["hits"] == []


@pytest.mark.parametrize("tokenizer", ["unicode61", "trigram"])
def test_restart_pending_binary_and_legacy_jobs(make_app, tmp_path, tokenizer):
    from raggio.store import _encode_payload
    with TestClient(make_app()) as c:
        c.post("/collections", headers=ROOT, json={"name": "main", "dim": 8,
                                                   "fts_keys": ["title"], "tokenizer": tokenizer})
        ingest(c, "main", payload()["documents"])
    db = sqlite3.connect(tmp_path / "collections/main/meta.db")
    jobs = []
    for binary, title in ((False, "legacy"), (True, "binary")):
        p = payload(title=title, doc_id=title)
        cur = db.execute("INSERT INTO jobs(payload, status, created_at, updated_at)"
                         " VALUES(?, 'processing', '', '')", (None if binary else json.dumps(p),))
        jobs.append(cur.lastrowid)
        if binary:
            db.execute("INSERT INTO job_payloads(job_id, data) VALUES(?,?)", (cur.lastrowid, _encode_payload(p)))
    db.commit()
    db.close()
    with TestClient(make_app()) as c:
        assert all(wait_job(c, "main", job)["status"] == "done" for job in jobs)
        for title in ("quasar", "legacy", "binary"):
            r = c.post("/collections/main/search", headers=ROOT,
                       json={"mode": "text", "scope": "both", "query": {"text": title}})
            assert len(r.json()["hits"]) == 2
    db = sqlite3.connect(tmp_path / "collections/main/meta.db")
    integrity(db)
    db.close()


def test_old_catalog_migration_keeps_text_only(make_app, tmp_path):
    with TestClient(make_app()) as c:
        c.post("/collections", headers=ROOT, json={"name": "main", "dim": 8}).raise_for_status()
        ingest(c, "main", payload()["documents"])
    db = sqlite3.connect(tmp_path / "catalog.db")
    db.execute("ALTER TABLE collections DROP COLUMN fts_keys")
    db.commit()
    db.close()
    with TestClient(make_app()) as c:
        assert c.get("/collections/main", headers=ROOT).json()["fts_keys"] is None
        r = c.post("/collections/main/search", headers=ROOT,
                   json={"mode": "text", "query": {"text": "quasar"}})
        assert r.json()["hits"] == []


def test_fts_column_mismatch_rebuilds_from_combined_text(tmp_path):
    c = collection(tmp_path, ["title"])
    asyncio.run(c._process_job(payload()))
    asyncio.run(c.stop())
    db = sqlite3.connect(tmp_path / "meta.db")
    db.executescript("DROP TRIGGER records_fts_ai; DROP TRIGGER records_fts_ad;"
                    " DROP TRIGGER records_fts_au; DROP TABLE records_fts;"
                    " CREATE VIRTUAL TABLE records_fts USING fts5(text, content='records',"
                    " content_rowid='id', tokenize='unicode61 remove_diacritics 2');")
    db.close()
    c = collection(tmp_path, ["title"])
    try:
        assert len(c._text_ids("quasar", 5, "both", None)[0]) == 2
        integrity(c.db)
        schema = c.db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        asyncio.run(c.stop())
    db = open_meta_db(tmp_path / "meta.db", "unicode61", ["title"])
    assert db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    integrity(db)
    db.close()


def test_ivf_attach_detach_preserves_metadata_text(col, monkeypatch):
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)
    docs = [payload(title=f"quasar{i}", doc_id=f"d{i}")["documents"][0] for i in range(60)]
    rng = np.random.default_rng(42)
    for doc in docs:
        for rec in (doc["summary"], *doc["chunks"]):
            rec["vector"] = rng.standard_normal(8).tolist()
    asyncio.run(col._process_job({"documents": docs}))
    before = col.db.execute("SELECT id, text, metadata, bm25_text FROM records ORDER BY id").fetchall()
    expected = col._text_ids("quasar4", 5, "both", None)
    for op in ({"op": "attach_index", "nlist": 8, "nprobe": 8}, {"op": "detach_index"}):
        asyncio.run(col._process_job(op))
        assert col.db.execute("SELECT id, text, metadata, bm25_text FROM records ORDER BY id").fetchall() == before
        assert col._text_ids("quasar4", 5, "both", None) == expected
        integrity(col.db)
