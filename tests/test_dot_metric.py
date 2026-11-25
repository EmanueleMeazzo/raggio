"""metric "dot" (ADR 0020): a collection keeps the float32 vectors it was sent and ranks
by their dot product with the query as sent; the default metric stays the cosine."""
import asyncio
import sqlite3
import zlib

import numpy as np
import pytest
from fastapi.testclient import TestClient

import raggio.store as store
from raggio.app import create_app
from raggio.config import Settings
from raggio.store import Collection, CollectionConfig, CollectionManager, _encode_payload, open_meta_db
from test_api import DIM, ROOT, client, ingest, make_app, wait_job


def f32(a) -> np.ndarray:
    return np.asarray(a, dtype=np.float32)


def dots(vectors, q) -> np.ndarray:
    """The reference score: the float32 values multiplied, the sum taken in float64."""
    return f32(vectors).astype(np.float64) @ f32(q).astype(np.float64)


def e1(scale: float = 1.0) -> np.ndarray:
    q = np.zeros(DIM, np.float32)
    q[0] = scale
    return q


def ranked_apart() -> np.ndarray:
    """Four vectors whose order against e1 differs by metric.
    cosine: r0 (1.0), r1 (0.994), r2 (0.6), r3 (-1.0); dot: r2 (3.0), r1 (0.9), r0 (0.5), r3 (-2.0)."""
    v = np.zeros((4, DIM), np.float32)
    v[0, 0] = 0.5
    v[1, :2] = (0.9, 0.1)
    v[2, :2] = (3.0, 4.0)
    v[3, 0] = -2.0
    return v


def uneven(n: int, seed: int, dim: int = DIM) -> np.ndarray:
    """n random float32 vectors with lengths from about 0.2 to 5: nothing like unit vectors."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal((n, dim)) * np.exp(rng.uniform(-1.5, 1.5, (n, 1))) / np.sqrt(dim)
    return v.astype(np.float32)


def records(vectors, prefix: str = "r", texts=None, metadata=None) -> list[dict]:
    """One document with one chunk per vector, both under the record's id."""
    docs = []
    for i, v in enumerate(vectors):
        chunk = {"id": f"{prefix}{i}", "vector": f32(v).tolist()}
        if texts is not None:
            chunk["text"] = texts[i]
        if metadata is not None:
            chunk["metadata"] = metadata[i]
        docs.append({"doc_id": f"{prefix}{i}", "chunks": [chunk]})
    return docs


def create(c, name: str = "main", **options) -> dict:
    r = c.post("/collections", headers=ROOT, json={"name": name, "dim": DIM, **options})
    assert r.status_code == 201, r.text
    return r.json()


def search(c, name: str, vector=None, **body) -> list[dict]:
    query = dict(body.pop("query", {}))
    if vector is not None:
        query["vector"] = f32(vector).tolist()
    r = c.post(f"/collections/{name}/search", headers=ROOT, json={"query": query, **body})
    assert r.status_code == 200, r.text
    return r.json()["hits"]


def read_back(c, name: str, **params) -> dict[str, np.ndarray]:
    """Every chunk's stored vector as float32, by record id (GET /documents?include_vector=true)."""
    r = c.get(f"/collections/{name}/documents", headers=ROOT,
              params={"scope": "chunks", "limit": 1000, "include_vector": "true", **params})
    assert r.status_code == 200, r.text
    return {rec["id"]: f32(rec["vector"]) for rec in r.json()["records"]}


def assert_ranked_by_dot(hits: list[dict], vectors: dict[str, np.ndarray], q) -> None:
    """Each hit's score is its record's dot product with q, and the hits are in that order."""
    scores = [h["score"] for h in hits]
    assert scores == pytest.approx([float(dots(vectors[h["id"]], q)) for h in hits], rel=1e-12, abs=1e-12)
    assert scores == sorted(scores, reverse=True)


def top_by_dot(vectors: np.ndarray, q, k: int, prefix: str = "r") -> tuple[list[str], list[float]]:
    exact = dots(vectors, q)
    order = np.argsort(-exact, kind="stable")[:k]
    return [f"{prefix}{i}" for i in order], exact[order].tolist()


def collection(path, metric: str = "dot", dim: int = DIM) -> Collection:
    """A Collection without the app, for the paths the API does not single out."""
    path.mkdir(parents=True, exist_ok=True)
    return Collection(CollectionConfig("t", dim, 4, None, None, None, metric=metric), path, lambda: None)


# ---- the option ----

def test_metric_is_a_collection_option_that_defaults_to_cosine(client):
    assert create(client, "plain")["metric"] == "cosine"
    assert create(client, "exact", metric="dot")["metric"] == "dot"
    assert create(client, "named", metric="cosine")["metric"] == "cosine"
    assert client.get("/collections/plain", headers=ROOT).json()["metric"] == "cosine"
    assert client.get("/collections/exact", headers=ROOT).json()["metric"] == "dot"


# ---- what is stored and read back ----

def test_float32_vectors_that_are_not_unit_length_read_back_bit_for_bit(client):
    create(client, metric="dot")
    sent = uneven(40, seed=1)
    norms = np.linalg.norm(sent.astype(np.float64), axis=1)
    assert norms.min() < 0.5 and norms.max() > 2.0  # the point: nothing here is a unit vector
    ingest(client, "main", records(sent))
    got = read_back(client, "main")
    assert len(got) == len(sent)
    for i, v in enumerate(sent):
        assert got[f"r{i}"].tobytes() == v.tobytes()


def test_any_finite_length_is_kept_exactly(client):
    """Lengths whose float32 sum of squares overflows or underflows, and the float32 edge
    values: the largest, the smallest normal, a subnormal, a negative zero."""
    create(client, metric="dot")
    sent = np.zeros((3, DIM), np.float32)
    sent[0, :4] = (3.4028235e38, -3.0e38, 1.0, -0.0)        # length 4.5e38: its square is past float32
    sent[1, :4] = (1.1754944e-38, 1e-45, -2e-39, 3e-30)     # length 3e-30: its square is 0 in float32
    sent[2] = np.float32(1) / np.arange(3, DIM + 3, dtype=np.float32)  # full mantissas
    assert np.signbit(sent[0, 3])
    ingest(client, "main", records(sent))
    got = read_back(client, "main")
    for i, v in enumerate(sent):
        assert got[f"r{i}"].tobytes() == v.tobytes()
    # and they are indexed and scored: asked along each one's direction, the three come
    # back by dot product (the one of length 4.5e38 leads wherever it is not orthogonal)
    for v in sent:
        q = (v.astype(np.float64) / np.linalg.norm(v.astype(np.float64))).astype(np.float32)
        hits = search(client, "main", q, k=3)
        assert [h["id"] for h in hits] == top_by_dot(sent, q, 3)[0]
        assert_ranked_by_dot(hits, got, q)


def test_what_meta_db_holds(tmp_path):
    """A dot collection's vecs rows are the float32 values, little-endian; a default
    collection's are the fp16 copies of the unit vectors, as before. Same schema."""
    sent = uneven(6, seed=2, dim=8)
    payload = {"documents": records(sent)}
    schemas = {}
    for metric in ("cosine", "dot"):
        col = collection(tmp_path / metric, metric=metric, dim=8)
        try:
            asyncio.run(col._process_job(payload))
            blobs = [b for (b,) in col.db.execute("SELECT vec FROM vecs ORDER BY id")]
            schemas[metric] = col.db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        finally:
            asyncio.run(col.stop())
        if metric == "dot":
            assert blobs == [v.astype("<f4").tobytes() for v in sent]
        else:
            assert blobs == [u.tobytes() for u in store._normalize(sent).astype(np.float16)]
    assert schemas["cosine"] == schemas["dot"]
    fresh = open_meta_db(tmp_path / "fresh.db")
    assert fresh.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schemas["dot"]
    fresh.close()


def test_upsert_replaces_the_stored_vector(client):
    create(client, metric="dot")
    first, second = uneven(2, seed=3)
    ingest(client, "main", records([first]))
    ingest(client, "main", records([second]))  # the same id again
    assert read_back(client, "main")["r0"].tobytes() == second.tobytes()
    hits = search(client, "main", second, k=5)
    assert [h["id"] for h in hits] == ["r0"]
    assert hits[0]["score"] == pytest.approx(float(dots(second, second)), rel=1e-12)


# ---- ranking and scores ----

def test_ranks_by_dot_product_where_cosine_ranks_otherwise(client):
    create(client, "exact", metric="dot")
    create(client, "plain")
    v = ranked_apart()
    for name in ("exact", "plain"):
        ingest(client, name, records(v))
    hits = search(client, "exact", e1(), k=4)
    assert [h["id"] for h in hits] == ["r2", "r1", "r0", "r3"]
    # e1 picks one coordinate, so the sum has one term and the score is that float32
    # value: 0.9 as float32 (0.8999999761581421), not 0.9 and not a cosine
    assert [h["score"] for h in hits] == [3.0, float(np.float32(0.9)), 0.5, -2.0]
    cosine = search(client, "plain", e1(), k=4)
    assert [h["id"] for h in cosine] == ["r0", "r1", "r2", "r3"]
    assert [h["score"] for h in cosine] == pytest.approx([1.0, 0.9 / np.hypot(0.9, 0.1), 0.6, -1.0], abs=2e-3)


def test_the_query_is_scored_as_sent(client):
    """The query's length counts too: twice the query, twice every score, the same order."""
    create(client, metric="dot")
    v = ranked_apart()
    ingest(client, "main", records(v))
    hits = search(client, "main", e1(2.0), k=4)
    assert [h["id"] for h in hits] == ["r2", "r1", "r0", "r3"]
    assert [h["score"] for h in hits] == [6.0, 2 * float(np.float32(0.9)), 1.0, -4.0]


def test_top_k_is_the_dot_products_when_the_candidates_cover_the_collection(client):
    """200 records and k=100: the scan's 2k candidates are every record, so the answer is
    the exact top 100 by dot product, scores included."""
    create(client, metric="dot")
    v = uneven(200, seed=4)
    q = uneven(1, seed=5)[0]
    ingest(client, "main", records(v))
    hits = search(client, "main", q, k=100)
    ids, scores = top_by_dot(v, q, 100)
    assert [h["id"] for h in hits] == ids
    assert [h["score"] for h in hits] == pytest.approx(scores, rel=1e-12, abs=1e-12)
    # and it is not the cosine's order
    by_cosine = np.argsort(-(dots(v, q) / np.linalg.norm(v.astype(np.float64), axis=1)), kind="stable")[:100]
    assert ids != [f"r{i}" for i in by_cosine]


def far_and_near() -> tuple[np.ndarray, np.ndarray]:
    """100 unit vectors within 60 degrees of e1, then one of length 100 at 84 degrees:
    the largest dot product by far (10 against at most 0.95), the smallest cosine."""
    rng = np.random.default_rng(6)
    side = rng.standard_normal((101, DIM))
    side[:, 0] = 0.0
    side /= np.linalg.norm(side, axis=1, keepdims=True)
    cos = np.append(np.linspace(0.5, 0.95, 100), 0.1)[:, None]
    v = cos * e1().astype(np.float64) + np.sqrt(1 - cos**2) * side
    v[100] *= 100.0
    return v.astype(np.float32), e1()


def test_candidates_are_the_nearest_by_angle(client):
    """What the guarantee is not: the dot product re-ranks the candidates of the quantized
    cosine scan, max(50, 2k) deep at 4 bits, and scans nothing else. The long vector at a
    wide angle is not among 50 candidates of 101, so k=10 does not return it; with k=60 the
    candidates are the whole collection and it comes first."""
    create(client, metric="dot")
    v, q = far_and_near()
    ingest(client, "main", records(v))
    stored = {f"r{i}": x for i, x in enumerate(v)}
    hits = search(client, "main", q, k=10)
    assert len(hits) == 10 and "r100" not in [h["id"] for h in hits]
    assert_ranked_by_dot(hits, stored, q)
    hits = search(client, "main", q, k=60)
    assert hits[0]["id"] == "r100" and hits[0]["score"] == pytest.approx(10.0, rel=1e-6)
    assert [h["id"] for h in hits] == top_by_dot(v, q, 60)[0]


def test_filters_and_scope_rank_by_dot_product(client):
    create(client, metric="dot")
    v = uneven(130, seed=7)
    q = uneven(1, seed=8)[0]
    meta = [{"group": "small" if i < 30 else "large"} for i in range(130)]
    docs = records(v, metadata=meta)
    # summaries, so a chunk scope has something to leave out
    docs += [{"doc_id": f"s{i}", "summary": {"vector": v[i].tolist()}} for i in range(5)]
    ingest(client, "main", docs)
    stored = {f"r{i}": x for i, x in enumerate(v)} | {f"s{i}": v[i] for i in range(5)}
    # a filter no larger than the candidate depth is scored directly: exact over all of it
    hits = search(client, "main", q, k=10, filter={"group": "small"})
    ids, scores = top_by_dot(v[:30], q, 10)
    assert [h["id"] for h in hits] == ids
    assert [h["score"] for h in hits] == pytest.approx(scores, rel=1e-12, abs=1e-12)
    # larger ones go through the masked scan (twice: the second scope search finds the
    # allowlist cached and takes the coalesced path)
    for body in ({"filter": {"group": "large"}}, {}, {}, {"scope": "both"}):
        hits = search(client, "main", q, k=10, **body)
        assert len(hits) == 10
        if "filter" in body:
            assert all(int(h["id"][1:]) >= 30 for h in hits)
        if body.get("scope") != "both":
            assert all(h["type"] == "chunk" for h in hits)
        assert_ranked_by_dot(hits, stored, q)
    hits = search(client, "main", q, k=5, scope="summaries")
    assert [h["id"] for h in hits] == top_by_dot(v[:5], q, 5, prefix="s")[0]
    assert_ranked_by_dot(hits, stored, q)


def test_ranked_siblings_are_ranked_by_dot_product(client):
    create(client, metric="dot")
    v = uneven(12, seed=9)
    q = uneven(1, seed=10)[0]
    docs = [{"doc_id": f"d{d}", "chunks": [{"id": f"r{i}", "vector": v[i].tolist()}
                                           for i in range(6 * d, 6 * d + 6)]} for d in range(2)]
    ingest(client, "main", docs)
    hits = search(client, "main", q, k=4, expand={"siblings_topk": 3})
    assert [h["id"] for h in hits] == top_by_dot(v, q, 4)[0]
    for h in hits:
        doc = int(h["doc_id"][1:])
        others = [i for i in range(6 * doc, 6 * doc + 6) if f"r{i}" != h["id"]]
        exact = dots(v[others], q)
        order = np.argsort(-exact, kind="stable")[:3]
        siblings = h["expansion"]["siblings"]
        assert [s["id"] for s in siblings] == [f"r{others[j]}" for j in order]
        assert [s["score"] for s in siblings] == pytest.approx(exact[order].tolist(), rel=1e-12, abs=1e-12)


# ---- text and hybrid ----

TEXTS = ["mercury is the closest planet", "jupiter is the largest planet", "pluto is a dwarf planet",
         "the moon is not a planet at all"]


def test_text_mode_is_the_same_bm25_in_both_metrics(client):
    v = ranked_apart()
    for name, options in (("exact", {"metric": "dot"}), ("plain", {})):
        create(client, name, **options)
        ingest(client, name, records(v, texts=TEXTS))
    body = {"query": {"text": "largest planet"}, "mode": "text", "k": 4}
    exact, plain = search(client, "exact", **body), search(client, "plain", **body)
    assert exact == plain and exact[0]["id"] == "r1"


@pytest.mark.parametrize("fusion,alpha", [("rrf", 0.5), ("relative", 0.5), ("relative", 0.8),
                                           ("relative", 1.0), ("relative", 0.0)])
def test_hybrid_fuses_the_dot_product_leg(client, fusion, alpha):
    """The vector leg of a hybrid search is the dot-product ranking: RRF takes its ranks,
    relative fusion min-max normalises its dot products. The expected lists are fused
    here from the two single-mode searches, by the documented formulas."""
    create(client, metric="dot")
    v = ranked_apart()
    ingest(client, "main", records(v, texts=TEXTS))
    vec = search(client, "main", e1(), k=4)
    assert [h["id"] for h in vec] == ["r2", "r1", "r0", "r3"]
    text = search(client, "main", query={"text": "largest planet"}, mode="text", k=4)
    fused: dict[str, float] = {}
    if fusion == "rrf":
        for leg in (vec, text):
            for rank, h in enumerate(leg, start=1):
                fused[h["id"]] = fused.get(h["id"], 0.0) + 1.0 / (60 + rank)
    else:
        for leg, weight in ((vec, alpha), (text, 1 - alpha)):
            if weight == 0:
                continue  # relative fusion skips a leg with no weight
            lo, hi = min(h["score"] for h in leg), max(h["score"] for h in leg)
            for h in leg:
                fused[h["id"]] = fused.get(h["id"], 0.0) + weight * ((h["score"] - lo) / (hi - lo))
    expected = sorted(fused.items(), key=lambda kv: -kv[1])
    hits = search(client, "main", e1(), query={"text": "largest planet"}, mode="hybrid", k=4,
                  fusion=fusion, alpha=alpha)
    assert [h["id"] for h in hits] == [i for i, _ in expected]
    assert [h["score"] for h in hits] == pytest.approx([s for _, s in expected], rel=1e-12)
    if fusion == "relative" and alpha == 1.0:
        assert [h["id"] for h in hits] == ["r2", "r1", "r0", "r3"]  # the dot order alone


class LongEmbedder:
    """Vectors of length 3, in double precision: what an endpoint that does not normalise returns."""

    @staticmethod
    def vector(text: str) -> list[float]:
        v = np.random.default_rng(zlib.crc32(text.encode())).standard_normal(DIM)
        return (3.0 * v / np.linalg.norm(v)).tolist()

    async def embed(self, texts):
        return [self.vector(t) for t in texts]

    async def aclose(self):
        pass


def test_vectors_embedded_by_the_server_are_kept_as_the_endpoint_returned_them(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(create_app(embedder_factory=lambda cfg: LongEmbedder())) as c:
        create(c, "exact", metric="dot")
        create(c, "plain")
        docs = [{"doc_id": f"r{i}", "chunks": [{"id": f"r{i}", "text": t}]} for i, t in enumerate(TEXTS)]
        for name in ("exact", "plain"):
            ingest(c, name, docs)
        got = read_back(c, "exact")
        for i, t in enumerate(TEXTS):  # the float32 nearest to each number the endpoint gave
            assert got[f"r{i}"].tobytes() == f32(LongEmbedder.vector(t)).tobytes()
        hits = search(c, "exact", query={"text": TEXTS[1]}, k=4)  # the query is embedded, and not normalised
        assert hits[0]["id"] == "r1" and hits[0]["score"] == pytest.approx(9.0, rel=1e-5)
        assert_ranked_by_dot(hits, got, LongEmbedder.vector(TEXTS[1]))
        cosine = search(c, "plain", query={"text": TEXTS[1]}, k=4)
        assert cosine[0]["id"] == "r1" and cosine[0]["score"] == pytest.approx(1.0, abs=1e-3)


# ---- the default metric is what it was ----

def test_a_default_collection_stores_and_ranks_as_before(client):
    """Unit vectors, an fp16 copy, and the cosine of that copy as the score, whatever the
    lengths of the stored vectors and of the query."""
    create(client)
    v = uneven(40, seed=11)
    q = uneven(1, seed=12)[0]
    ingest(client, "main", records(v))
    unit16 = store._normalize(v).astype(np.float16)
    got = read_back(client, "main")
    for i in range(len(v)):  # the fp16 copy of the unit vector, not what was sent
        assert got[f"r{i}"].tobytes() == unit16[i].astype(np.float32).tobytes()
    ref = unit16.astype(np.float32)
    ref /= np.linalg.norm(ref, axis=1, keepdims=True)
    cos = ref @ store._normalize(q[None])[0]
    order = np.argsort(-cos, kind="stable")[:20]
    for scale in (1.0, 7.0):
        hits = search(client, "main", scale * q, k=20)
        assert [h["id"] for h in hits] == [f"r{i}" for i in order]
        assert [h["score"] for h in hits] == pytest.approx(cos[order].tolist(), abs=1e-6)
    assert ([h["id"] for h in hits] != top_by_dot(v, q, 20)[0])  # and not the dot product's order


def test_a_catalog_from_before_the_option_opens_as_cosine(make_app, tmp_path):
    v = ranked_apart()
    with TestClient(make_app()) as c:
        create(c)
        ingest(c, "main", records(v))
        before = search(c, "main", e1(), k=4)
        vectors = read_back(c, "main")
    db = sqlite3.connect(tmp_path / "catalog.db")
    db.execute("ALTER TABLE collections DROP COLUMN metric")
    db.commit()
    db.close()
    with TestClient(make_app()) as c:
        assert c.get("/collections/main", headers=ROOT).json()["metric"] == "cosine"
        assert search(c, "main", e1(), k=4) == before
        again = read_back(c, "main")
        assert all(again[i].tobytes() == vectors[i].tobytes() for i in vectors)
        assert create(c, "exact", metric="dot")["metric"] == "dot"  # the migrated catalog takes the option


# ---- persistence ----

def test_a_dot_collection_survives_a_restart(make_app, tmp_path):
    v = uneven(60, seed=13)
    q = uneven(1, seed=14)[0]
    with TestClient(make_app()) as c:
        create(c, metric="dot")
        ingest(c, "main", records(v))
        before = search(c, "main", q, k=30)
    with TestClient(make_app()) as c:
        assert c.get("/collections/main", headers=ROOT).json()["metric"] == "dot"
        got = read_back(c, "main")
        assert all(got[f"r{i}"].tobytes() == v[i].tobytes() for i in range(len(v)))
        hits = search(c, "main", q, k=30)
        assert hits == before
        ids, scores = top_by_dot(v, q, 30)
        assert [h["id"] for h in hits] == ids
        assert [h["score"] for h in hits] == pytest.approx(scores, rel=1e-12, abs=1e-12)
        # and it still takes writes as a dot collection
        extra = uneven(1, seed=15)[0] * 50
        ingest(c, "main", [{"doc_id": "late", "chunks": [{"id": "late", "vector": extra.tolist()}]}])
        assert read_back(c, "main")["late"].tobytes() == extra.tobytes()


def test_a_job_replayed_after_a_crash_keeps_the_float32_values(make_app, tmp_path):
    v = uneven(3, seed=16)
    with TestClient(make_app()) as c:
        create(c, metric="dot")
        ingest(c, "main", records(v[:1]))
    db = sqlite3.connect(tmp_path / "collections/main/meta.db")
    cur = db.execute("INSERT INTO jobs(payload, status, created_at, updated_at)"
                     " VALUES(NULL, 'processing', '', '')")
    db.execute("INSERT INTO job_payloads(job_id, data) VALUES(?,?)",
               (cur.lastrowid, _encode_payload({"documents": records(v[1:], prefix="late")})))
    db.commit()
    db.close()
    with TestClient(make_app()) as c:
        assert wait_job(c, "main", cur.lastrowid)["status"] == "done"
        got = read_back(c, "main")
        assert got["r0"].tobytes() == v[0].tobytes()
        assert got["late0"].tobytes() == v[1].tobytes() and got["late1"].tobytes() == v[2].tobytes()


def test_an_evicted_dot_collection_reloads_as_one(make_app, monkeypatch):
    monkeypatch.setenv("MAX_RESIDENT_COLLECTIONS", "1")
    v = ranked_apart()
    with TestClient(make_app()) as c:
        create(c, "exact", metric="dot")
        create(c, "plain")
        ingest(c, "exact", records(v))
        ingest(c, "plain", records(v))  # evicts "exact"
        assert c.get("/healthz").json()["resident_collections"] == ["plain"]
        hits = search(c, "exact", e1(), k=4)
        assert [h["score"] for h in hits] == [3.0, float(np.float32(0.9)), 0.5, -2.0]
        assert read_back(c, "exact")["r2"].tobytes() == v[2].tobytes()


# ---- what is refused ----

@pytest.mark.parametrize("metric", ["l2", "euclidean", "DOT", "", 1, None, ["dot"]])
def test_an_unknown_metric_is_refused(client, metric):
    r = client.post("/collections", headers=ROOT, json={"name": "bad", "dim": DIM, "metric": metric})
    assert r.status_code == 422
    assert client.get("/collections", headers=ROOT).json()["collections"] == []


@pytest.mark.parametrize("first,second", [("cosine", "dot"), ("dot", "cosine")])
def test_the_metric_of_an_existing_collection_cannot_be_changed(client, first, second):
    create(client, metric=first)
    v = ranked_apart()
    ingest(client, "main", records(v))
    before = search(client, "main", e1(), k=4)
    r = client.post("/collections", headers=ROOT, json={"name": "main", "dim": DIM, "metric": second})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "already exists" in detail and f"metric '{first}'" in detail and "cannot be changed" in detail
    # nothing changed: the collection, its metric and its records are as they were
    assert client.get("/collections/main", headers=ROOT).json()["metric"] == first
    assert search(client, "main", e1(), k=4) == before
    # a create that repeats the metric is the plain name clash it always was
    r = client.post("/collections", headers=ROOT, json={"name": "main", "dim": DIM, "metric": first})
    assert r.status_code == 409 and r.json()["detail"] == "collection 'main' already exists"


def test_a_metric_the_catalog_does_not_know_is_not_opened(tmp_path, monkeypatch):
    """A catalog written by a later release: guessing a metric would rank, and read back,
    another representation than the one on disk."""
    with pytest.raises(ValueError, match="metric 'l2'"):
        Collection(CollectionConfig("t", 8, 4, None, None, None, metric="l2"), tmp_path, lambda: None)
    assert not (tmp_path / "meta.db").exists()  # refused before anything was opened
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    async def create():
        m = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        try:
            with pytest.raises(ValueError, match="metric must be one of"):
                await m.create_collection("x", 8, 4, None, None, None, metric="l2")
            assert m.list_collections() == []
        finally:
            await m.shutdown()

    asyncio.run(create())


def test_a_zero_vector_fails_its_job_and_stores_nothing(client):
    create(client, metric="dot")
    good = uneven(1, seed=17)[0]
    ingest(client, "main", records([good]))
    docs = records([good * 2, np.zeros(DIM, np.float32)], prefix="z")
    r = client.post("/collections/main/documents", headers=ROOT, json={"documents": docs})
    assert r.status_code == 202
    job = wait_job(client, "main", r.json()["job_id"])
    assert job["status"] == "error" and "zero" in job["error"]
    assert set(read_back(client, "main")) == {"r0"}  # the job's other record is not there either


@pytest.mark.filterwarnings("ignore:overflow encountered in cast")
def test_a_vector_past_float32_fails_its_job(client):
    create(client, metric="dot")
    doc = {"doc_id": "big", "chunks": [{"id": "big", "vector": [1e39] + [0.0] * (DIM - 1)}]}
    r = client.post("/collections/main/documents", headers=ROOT, json={"documents": [doc]})
    assert r.status_code == 202
    job = wait_job(client, "main", r.json()["job_id"])
    assert job["status"] == "error" and "finite" in job["error"]
    assert read_back(client, "main") == {}


@pytest.mark.filterwarnings("ignore:overflow encountered in cast")
@pytest.mark.parametrize("query,word", [([0.0] * DIM, "zero"), ([1e39] + [0.0] * (DIM - 1), "finite")])
def test_a_query_without_a_direction_is_a_400(client, query, word):
    create(client, metric="dot")
    ingest(client, "main", records(ranked_apart()))
    for body in ({}, {"filter": {"doc_id": "r0"}}, {"mode": "hybrid", "query": {"text": "planet", "vector": query}}):
        r = client.post("/collections/main/search", headers=ROOT,
                        json={"query": {"vector": query}, **body})
        assert r.status_code == 400 and word in r.json()["detail"], r.text
    r = client.post("/collections/main/search", headers=ROOT, json={"query": {"vector": [1.0] * (DIM + 1)}})
    assert r.status_code == 400 and "dim" in r.json()["detail"]


# ---- the store's other paths ----

def test_queries_that_share_a_scan_keep_their_own_dot_products(tmp_path):
    """Concurrent full scans are stacked into one pass over the codes: each query row
    is still scored as it was sent, whatever its length."""
    col = collection(tmp_path)
    v = uneven(120, seed=18)
    queries = uneven(6, seed=19) * np.array([[0.1], [1.0], [3.0], [10.0], [0.5], [2.0]], np.float32)
    stored = {f"r{i}": x for i, x in enumerate(v)}

    async def run():
        await col._process_job({"documents": records(v)})
        stacked = []
        real = col._search_rescored

        def spy(mat, ns, **kw):
            stacked.append(len(mat))
            return real(mat, ns, **kw)

        col._search_rescored = spy
        results = await asyncio.gather(*(
            col.search("vector", q[None], None, 60, "chunks", None, None) for q in queries))
        return stacked, results

    try:
        stacked, results = asyncio.run(run())
    finally:
        asyncio.run(col.stop())
    assert max(stacked) > 1  # at least two of them went through one scan
    for q, hits in zip(queries, results):
        assert [h["id"] for h in hits] == top_by_dot(v, q, 60)[0]
        assert_ranked_by_dot(hits, stored, q)


def test_ivf_attach_and_detach_keep_the_vectors_and_the_ranking(tmp_path, monkeypatch):
    """An index rebuild reads the retained vectors back: the float32 values stay as they
    are, the shards get their directions, and the hits are the dot product's as before."""
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)
    col = collection(tmp_path)
    v, q = far_and_near()
    more = uneven(60, seed=20)
    allv = np.vstack([v, more])
    stored = {f"r{i}": x for i, x in enumerate(allv)}

    def state():
        async def run():
            deep = await col.search("vector", q[None], None, 100, "chunks", None, None, nprobe=8)
            shallow = await col.search("vector", q[None], None, 10, "chunks", None, None, nprobe=8)
            return deep, shallow

        deep, shallow = asyncio.run(run())
        blobs = col.db.execute("SELECT id, vec FROM vecs ORDER BY id").fetchall()
        return deep, shallow, blobs

    try:
        asyncio.run(col._process_job({"documents": records(allv)}))
        deep, shallow, blobs = state()
        assert [b for _, b in blobs] == [x.astype("<f4").tobytes() for x in allv]
        assert [h["id"] for h in deep] == top_by_dot(allv, q, 100)[0]  # 200 candidates: all 161
        assert deep[0]["id"] == "r100"
        assert "r100" not in [h["id"] for h in shallow]  # 50 candidates, by angle
        for op, kind in (({"op": "attach_index", "nlist": 8, "nprobe": 8}, "ivf"),
                         ({"op": "detach_index"}, "flat")):
            asyncio.run(col._process_job(op))
            assert col.index_info()["type"] == kind
            deep2, shallow2, blobs2 = state()
            assert blobs2 == blobs
            assert deep2 == deep
            # the rebuilt index was given directions: had it been given the vectors as
            # stored, the one of length 100 would lead its scan
            assert "r100" not in [h["id"] for h in shallow2]
            assert_ranked_by_dot(shallow2, stored, q)
    finally:
        asyncio.run(col.stop())


def test_an_attach_re_embeds_a_record_that_lost_its_vector(tmp_path, monkeypatch):
    """An attach re-embeds, from their text, records that have no retained vector (in a
    default collection: rows from before vectors were retained). A dot collection has no
    such rows of its own; should one lose a vecs row, what the endpoint returns is kept
    as it is, like any vector the server embeds, and indexed by its direction."""
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)
    col = Collection(CollectionConfig("t", DIM, 4, None, None, None, metric="dot"), tmp_path, LongEmbedder)
    v = uneven(150, seed=21)
    texts = [f"text number {i}" for i in range(150)]

    def lose(*ids):
        marks = ",".join("?" * len(ids))
        with col.db_lock:
            col.db.execute(f"DELETE FROM vecs WHERE id IN (SELECT id FROM records WHERE external_id IN ({marks}))", ids)
            col.db.commit()

    try:
        asyncio.run(col._process_job({"documents": records(v, texts=texts)}))
        lose("r3", "r77")
        asyncio.run(col._process_job({"op": "attach_index", "nlist": 8, "nprobe": 8}))
        want = {f"r{i}": f32(LongEmbedder.vector(texts[i])) if i in (3, 77) else v[i] for i in range(150)}
        blobs = dict(col.db.execute("SELECT r.external_id, v.vec FROM records r JOIN vecs v ON v.id=r.id"))
        assert blobs == {i: x.astype("<f4").tobytes() for i, x in want.items()}
        q = want["r3"]
        hits = asyncio.run(col.search("vector", q[None], None, 100, "chunks", None, None, nprobe=8))
        assert hits[0]["id"] == "r3" and hits[0]["score"] == pytest.approx(9.0, rel=1e-5)
        assert_ranked_by_dot(hits, want, q)
        # the other way in: a row the build's stream did not find is handed to the new index
        lose("r5")
        rid = col.db.execute("SELECT id FROM records WHERE external_id='r5'").fetchone()[0]
        added = []
        assert asyncio.run(col._backfill_vecs(np.array([rid], dtype=np.uint64),
                                              lambda ids, mat: added.append((ids.tolist(), mat)))) == 1
        (ids, mat), = added
        embedded = f32(LongEmbedder.vector(texts[5]))
        assert ids == [rid] and mat.dtype == np.float32
        assert mat[0] == pytest.approx(embedded / 3.0, abs=1e-6)  # its direction, for the index
        assert col.db.execute("SELECT vec FROM vecs WHERE id=?", (rid,)).fetchone()[0] == embedded.tobytes()
    finally:
        asyncio.run(col.stop())
