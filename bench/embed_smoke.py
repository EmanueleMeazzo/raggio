"""Server-side embedding smoke test: raggio calls the embedding endpoint itself.

Exercises the three previously untested Embedder paths end to end:
  1. dim probe at collection create (no dim supplied)
  2. text-only ingest (worker embeds in batches of 64)
  3. text queries in vector/hybrid search
Then measures overhead vs pre-supplied vectors and samples container RSS
across ingest/search/delete cycles to catch leaks.

Run on the box hosting both containers (raggio on :8000 with
EMBEDDING_BASE_URL set, vLLM on :8001):

  uv run --with httpx python bench/embed_smoke.py
"""

import random
import statistics
import subprocess
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
EMB = "http://127.0.0.1:8001/v1/embeddings"
MODEL = "Qwen/Qwen3-Embedding-0.6B"
KEY = "smoke"
CONTAINER = "embed-smoke"

api = httpx.Client(base_url=BASE, headers={"x-api-key": KEY}, timeout=120.0)
emb = httpx.Client(timeout=120.0)


def rss_mb() -> float:
    out = subprocess.run(["podman", "exec", CONTAINER, "cat", "/proc/1/status"],
                         capture_output=True, text=True, check=True).stdout
    kb = next(int(l.split()[1]) for l in out.splitlines() if l.startswith("VmRSS"))
    return kb / 1024


def wait_job(coll: str, job_id: int) -> float:
    t0 = time.perf_counter()
    while True:
        r = api.get(f"/collections/{coll}/jobs/{job_id}").json()
        if r["status"] == "done":
            return time.perf_counter() - t0
        if r["status"] == "error":
            sys.exit(f"job {job_id} failed: {r['error']}")
        time.sleep(0.25)


def embed_direct(texts: list[str]) -> list[list[float]]:
    vecs = []
    for s in range(0, len(texts), 256):
        r = emb.post(EMB, json={"model": MODEL, "input": texts[s:s + 256]})
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        vecs += [d["embedding"] for d in data]
    return vecs


WORDS = ("signal lattice orbit quantum ferment yeast basalt magma glacier tensor "
         "cache vector shard recall entropy sonnet harbor cobalt prairie violin").split()


def mk_text(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(40))


def fresh(coll: str, **body) -> dict:
    api.delete(f"/collections/{coll}")  # 404 on first run is fine
    r = api.post("/collections", json={"name": coll, **body})
    assert r.status_code == 201, r.text
    return r.json()


# ---- phase 1: correctness ----
print("== phase 1: correctness ==", flush=True)
info = fresh("smoke")
assert info["dim"] == 1024, f"dim probe returned {info['dim']}"
print(f"dim probe ok: {info['dim']}")

docs = {
    "volcano": "Basalt lava flows from Icelandic fissure eruptions cool into columnar joints.",
    "qec": "Surface codes correct quantum errors by measuring stabilizers on a qubit lattice.",
    "bread": "Bakers ferment sourdough with wild yeast and lactobacilli for an open crumb.",
}
payload = {"documents": [
    {"doc_id": k, "summary": {"text": v},
     "chunks": [{"id": f"{k}-0", "text": v, "position": 0},
                {"id": f"{k}-1", "text": v + " More detail follows.", "position": 1}]}
    for k, v in docs.items()]}
wait_job("smoke", api.post("/collections/smoke/documents", json=payload).json()["job_id"])

q = "how do bakers ferment sourdough bread"
for mode in ("vector", "hybrid"):
    hits = api.post("/collections/smoke/search",
                    json={"query": {"text": q}, "mode": mode, "k": 3}).json()["hits"]
    assert hits and hits[0]["doc_id"] == "bread", f"{mode}: got {hits[:1]}"
    print(f"{mode} text-query search ok (top: {hits[0]['doc_id']})")

qv = embed_direct([q])[0]
hits = api.post("/collections/smoke/search",
                json={"query": {"vector": qv}, "mode": "vector", "k": 3}).json()["hits"]
assert hits[0]["doc_id"] == "bread"
print("supplied-vector search agrees with server-side embedding")

# ---- phase 2: overhead ----
print("== phase 2: overhead ==", flush=True)
rng = random.Random(7)
texts = [mk_text(rng) for _ in range(1000)]
docs2 = [{"doc_id": f"d{i}", "chunks": [
    {"id": f"d{i}-{j}", "text": texts[i * 5 + j], "position": j} for j in range(5)]}
    for i in range(200)]

fresh("smoke-text", dim=1024)
t_text = wait_job("smoke-text", api.post(
    "/collections/smoke-text/documents", json={"documents": docs2}).json()["job_id"])

t0 = time.perf_counter()
vecs = embed_direct(texts)
t_embed = time.perf_counter() - t0

docs2v = [{"doc_id": f"d{i}", "chunks": [
    {"id": f"d{i}-{j}", "text": texts[i * 5 + j], "vector": vecs[i * 5 + j], "position": j}
    for j in range(5)]} for i in range(200)]
fresh("smoke-vec", dim=1024)
t_vec = wait_job("smoke-vec", api.post(
    "/collections/smoke-vec/documents", json={"documents": docs2v}).json()["job_id"])

print(f"ingest 1000 records: text-only {t_text:.1f}s ({1000 / t_text:.0f} rec/s) | "
      f"pre-embedded {t_vec:.1f}s ({1000 / t_vec:.0f} rec/s) | "
      f"direct embed alone {t_embed:.1f}s")


def bench_q(body_fn, n=50):
    api.post("/collections/smoke-text/search", json=body_fn(0))  # warmup
    ts = []
    for i in range(n):
        t0 = time.perf_counter()
        r = api.post("/collections/smoke-text/search", json=body_fn(i))
        r.raise_for_status()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.mean(ts), statistics.quantiles(ts, n=20)[18]


qtexts = [mk_text(rng) for _ in range(50)]
qvecs = embed_direct(qtexts)
m_t, p95_t = bench_q(lambda i: {"query": {"text": qtexts[i]}, "mode": "vector", "k": 10})
m_v, p95_v = bench_q(lambda i: {"query": {"vector": qvecs[i]}, "mode": "vector", "k": 10})
print(f"query: text {m_t:.1f}ms avg (p95 {p95_t:.1f}) | vector {m_v:.1f}ms avg "
      f"(p95 {p95_v:.1f}) | embed overhead {m_t - m_v:.1f}ms/query")

# ---- phase 3: leak check ----
print("== phase 3: leak check ==", flush=True)
samples = [rss_mb()]
for cyc in range(12):
    docs3 = [{"doc_id": f"c{cyc}-d{i}", "chunks": [
        {"id": f"c{cyc}-d{i}-{j}", "text": mk_text(rng), "position": j} for j in range(3)]}
        for i in range(30)]
    wait_job("smoke", api.post("/collections/smoke/documents",
                               json={"documents": docs3}).json()["job_id"])
    for _ in range(30):
        api.post("/collections/smoke/search",
                 json={"query": {"text": mk_text(rng)}, "mode": "hybrid", "k": 10})
    for i in range(30):
        api.delete(f"/collections/smoke/documents/c{cyc}-d{i}")
    samples.append(rss_mb())
    print(f"cycle {cyc + 1}: rss {samples[-1]:.1f} MB", flush=True)

base_rss, last = samples[3], samples[-1]  # skip allocator warmup
growth = (last - base_rss) / base_rss
print(f"rss cycle3->end: {base_rss:.1f} -> {last:.1f} MB ({growth:+.1%})")
assert growth < 0.10, "possible leak: rss grew >10% after warmup"
print("leak check PASS")

for c in ("smoke", "smoke-text", "smoke-vec"):
    api.delete(f"/collections/{c}")
print("ALL OK")
