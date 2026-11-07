"""Document-shaped retrieval benchmark: summary scope, one-document search and large k.

bench.py measures one chunk per document (the arxiv abstracts), so it never exercises a
document store's request shapes. This harness runs them on the corpus of bench/prep_wiki.py
(25,000 Wikipedia articles: one summary + up to 10 chunks each, ~173k records), on raggio
(flat or with its IVF index) and on Weaviate as a reference:

  summary-vector    scope summaries, k=10, query = an article title's embedding
  summary-hybrid    the same + the title as BM25 text (RRF; Weaviate rankedFusion alpha 0.5)
  indoc-vector      scope chunks, one document, k=5, query = a sentence of one of its chunks
  indoc-hybrid      the same + the sentence as BM25 text
  chunks-k10        scope chunks, no filter, k=10, the title queries (the control row)
  chunks-k100       ... k=100
  chunks-k1000      ... k=1000

Each workload runs its queries serially (latency percentiles, QPS) and then at --concurrency
(QPS, p95, server CPU per query from the container's cgroup). Quality per workload: the own
summary in the top 10 (summary), the source chunk in the top 5 (indoc), recall@k against an
exact search over the stored chunk vectors (chunks-k*).

One document scope has two spellings. --doc-filter doc_id is the reserved filter key of
raggio 0.2.0+ (ADR 0007); --doc-filter metadata filters on the `doc` metadata key every record
carries, the only way before 0.2.0. Every engine returns the same fields per hit (id, document,
kind, title, text): Weaviate is asked for them, so response sizes match.

  --mode ingest  (re)create the collection and ingest the corpus; prints and records the timing
  --mode search  measure; the collection must hold exactly the corpus (summary and chunk counts)
"""
import argparse
import asyncio
import json
import statistics
import time
import uuid
from pathlib import Path

import httpx
import numpy as np
import orjson

import bench  # podman metrics, cgroup CPU, percentiles

CORPUS = Path("bench/corpus-wiki")
TR, WV = "http://localhost:18000", "http://localhost:18080"
TR_HDRS = {"x-api-key": "bench", "content-type": "application/json"}
WV_HDRS = {"content-type": "application/json"}
COLL, WCLASS = "docs", "WikiDoc"
WV_FIELDS = "recId docId kind title body"

WORKLOADS = [  # name, query set, scope, k, hybrid, document filter
    ("summary-vector", "summary", "summaries", 10, False, False),
    ("summary-hybrid", "summary", "summaries", 10, True, False),
    ("indoc-vector", "indoc", "chunks", 5, False, True),
    ("indoc-hybrid", "indoc", "chunks", 5, True, True),
    ("chunks-k10", "summary", "chunks", 10, False, False),
    ("chunks-k100", "summary", "chunks", 100, False, False),
    ("chunks-k1000", "summary", "chunks", 1000, False, False),
]
EXACT_KS = (10, 100, 1000)


# ---------- corpus ----------

def load_corpus(corpus: Path):
    meta = json.loads((corpus / "meta.json").read_text())
    if not meta.get("embedded"):
        raise SystemExit(f"{corpus}/meta.json: the corpus is not embedded yet (prep_wiki.py --stage embed)")
    with open(corpus / "records.jsonl", "rb") as f:
        records = [orjson.loads(line) for line in f]
    with open(corpus / "queries.jsonl", "rb") as f:
        queries = [orjson.loads(line) for line in f]
    vecs = np.load(corpus / "vecs.npy", mmap_mode="r")
    qvecs = np.load(corpus / "qvecs.npy")
    assert vecs.shape[0] == len(records) == meta["n_records"], "records/vectors out of sync"
    assert qvecs.shape[0] == len(queries), "queries/query vectors out of sync"
    for q, v in zip(queries, qvecs):
        q["vector"] = np.round(v.astype(np.float32), 5)
    return meta, records, vecs, queries


def query_sets(queries, n_summary, n_indoc):
    s = [q for q in queries if q["set"] == "summary"][:n_summary]
    i = [q for q in queries if q["set"] == "indoc"][:n_indoc]
    return {"summary": s, "indoc": i}


def exact_topk(records, vecs, queries, ks=EXACT_KS, cache: Path | None = None):
    """Exact top-max(ks) chunk ids per summary query (the stored vectors are unit length,
    so the dot product is the cosine): ground truth for the chunks-k* recall."""
    if cache and cache.exists():
        z = np.load(cache)
        return {k: z[f"k{k}"] for k in ks}
    chunk_rows = np.array([i for i, r in enumerate(records) if r["kind"] == "chunk"])
    q = np.stack([x["vector"] for x in queries]).astype(np.float32)
    kmax = min(max(ks), len(chunk_rows))  # a tiny smoke corpus can hold fewer chunks than k
    best = np.empty((len(q), kmax), dtype=np.int64)
    scores = np.zeros((len(q), len(chunk_rows)), dtype=np.float32)
    for s in range(0, len(chunk_rows), 50_000):
        rows = chunk_rows[s:s + 50_000]
        scores[:, s:s + len(rows)] = q @ np.asarray(vecs[rows], dtype=np.float32).T
    part = np.argpartition(-scores, kmax - 1, axis=1)[:, :kmax]
    for i in range(len(q)):
        order = part[i][np.argsort(-scores[i, part[i]])]
        best[i] = chunk_rows[order]
    out = {k: best[:, :min(k, kmax)] for k in ks}
    if cache:
        np.savez_compressed(cache, **{f"k{k}": v for k, v in out.items()})
    return out


# ---------- requests (pure; tests/test_bench_docs.py) ----------

def doc_filter(doc: str, mode: str) -> dict:
    if mode == "doc_id":
        return {"doc_id": doc}
    if mode == "metadata":
        return {"doc": doc}
    raise ValueError(f"unknown doc filter mode {mode!r}")


def raggio_request(q: dict, scope: str, k: int, hybrid: bool, by_doc: bool, filter_mode: str):
    body = {"query": {"vector": q["vector"]}, "k": k, "scope": scope}
    if hybrid:
        body["query"]["text"] = q["text"]
        body["mode"] = "hybrid"
    if by_doc:
        body["filter"] = doc_filter(q["doc"], filter_mode)
    return f"{TR}/collections/{COLL}/search", body


def weaviate_request(q: dict, scope: str, k: int, hybrid: bool, by_doc: bool, filter_mode=None):
    kind = {"summaries": "summary", "chunks": "chunk"}[scope]
    operands = [f'{{path: ["kind"], operator: Equal, valueText: "{kind}"}}']
    if by_doc:
        operands.append(f'{{path: ["docId"], operator: Equal, valueText: {json.dumps(q["doc"])}}}')
    where = (operands[0] if len(operands) == 1 else
             f'{{operator: And, operands: [{", ".join(operands)}]}}')
    vec = orjson.dumps(q["vector"], option=orjson.OPT_SERIALIZE_NUMPY).decode()
    if hybrid:
        search = (f'hybrid: {{query: {json.dumps(q["text"])}, vector: {vec}, alpha: 0.5, '
                  f'fusionType: rankedFusion, properties: ["body"]}}')
    else:
        search = f"nearVector: {{vector: {vec}}}"
    gql = f'{{Get {{{WCLASS}({search}, limit: {k}, where: {where}) {{{WV_FIELDS}}}}}}}'
    return f"{WV}/v1/graphql", {"query": gql}


def parse_hits(engine: str, resp: dict) -> list[str]:
    if engine.startswith("raggio"):
        return [h["id"] for h in resp["hits"]]
    if resp.get("errors"):
        raise RuntimeError(f"weaviate graphql: {str(resp['errors'])[:300]}")
    return [h["recId"] for h in ((resp.get("data") or {}).get("Get", {}).get(WCLASS) or [])]


def quality(workload: str, queries: list[dict], hits: list[list[str]], truth=None, k=None) -> float:
    if workload.startswith(("summary", "indoc")):
        return statistics.mean(q["target"] in h for q, h in zip(queries, hits))
    return statistics.mean(len(set(h) & set(t)) / len(t) for h, t in zip(hits, truth))


# ---------- ingest ----------

def raggio_doc(doc_records, vecs, rows):
    s, chunks = doc_records[0], doc_records[1:]
    def rec(r, row):
        return {"text": r["text"], "vector": np.round(np.asarray(vecs[row], dtype=np.float32), 5),
                "metadata": {"doc": r["doc"], "title": r["title"], "kind": r["kind"]}}
    return {"doc_id": s["doc"], "summary": rec(s, rows[0]),
            "chunks": [{"id": r["id"], "position": i, **rec(r, row)}
                       for i, (r, row) in enumerate(zip(chunks, rows[1:]))]}


def by_document(records):
    """[(records of one document, their rows)], summary first, in corpus order."""
    out, cur, cur_rows = [], [], []
    for i, r in enumerate(records):
        if cur and r["doc"] != cur[0]["doc"]:
            out.append((cur, cur_rows))
            cur, cur_rows = [], []
        cur.append(r)
        cur_rows.append(i)
    if cur:
        out.append((cur, cur_rows))
    return out


async def wait_job(client, jid):
    while True:
        st = (await client.get(f"{TR}/collections/{COLL}/jobs/{jid}")).json()
        if st["status"] == "done":
            return
        if st["status"] == "error":
            raise RuntimeError(f"raggio job {jid} failed: {st['error']}")
        await asyncio.sleep(0.25)


async def ingest_raggio(records, vecs, docs_per_batch, concurrency):
    async with httpx.AsyncClient(headers=TR_HDRS, timeout=600) as client:
        await client.delete(f"{TR}/collections/{COLL}")
        (await client.post(f"{TR}/collections", json={"name": COLL, "dim": int(vecs.shape[1])})).raise_for_status()
        docs = by_document(records)
        reqs = [(lambda b=docs[i:i + docs_per_batch]: (
                    f"{TR}/collections/{COLL}/documents", {"documents": [raggio_doc(d, vecs, rows) for d, rows in b]}))
                for i in range(0, len(docs), docs_per_batch)]
        t0 = time.time()
        results = await bench.post_batches(client, reqs, concurrency, "raggio ingest")
        for jid in sorted(r["job_id"] for r in results):
            await wait_job(client, jid)
        elapsed = time.time() - t0
        info = (await client.get(f"{TR}/collections/{COLL}")).json()
        return {"ingest_s": elapsed, "ingest_rps": len(records) / elapsed, "summaries": info["summaries"],
                "chunks": info["chunks"]}


async def ingest_weaviate(records, vecs, docs_per_batch, concurrency):
    async with httpx.AsyncClient(headers=WV_HDRS, timeout=120) as client:
        await client.delete(f"{WV}/v1/schema/{WCLASS}")
        props = [{"name": n, "dataType": ["text"], "tokenization": "field"} for n in ("recId", "docId", "kind")]
        props += [{"name": n, "dataType": ["text"]} for n in ("title", "body")]
        schema = {"class": WCLASS, "vectorizer": "none", "vectorIndexConfig": {"distance": "cosine"},
                  "properties": props}
        (await client.post(f"{WV}/v1/schema", json=schema)).raise_for_status()

        def objects(batch):
            return {"objects": [
                {"class": WCLASS, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, r["id"])),
                 "properties": {"recId": r["id"], "docId": r["doc"], "kind": r["kind"], "title": r["title"],
                                "body": r["text"]},
                 "vector": np.round(np.asarray(vecs[row], dtype=np.float32), 5)}
                for d, rows in batch for r, row in zip(d, rows)]}

        docs = by_document(records)
        reqs = [(lambda b=docs[i:i + docs_per_batch]: (f"{WV}/v1/batch/objects", objects(b)))
                for i in range(0, len(docs), docs_per_batch)]
        t0 = time.time()
        results = await bench.post_batches(client, reqs, concurrency, "weaviate ingest",
                                           slim=lambda r: [o["result"] for o in r
                                                           if o.get("result", {}).get("status") == "FAILED"])
        errors = [e for r in results for e in r]
        if errors:
            raise RuntimeError(f"weaviate batch errors: {errors[:3]} (+{len(errors)} total)")
        elapsed = time.time() - t0
        counts = await weaviate_counts(client)
        return {"ingest_s": elapsed, "ingest_rps": len(records) / elapsed, **counts}


async def weaviate_counts(client):
    out = {}
    for kind, key in (("summary", "summaries"), ("chunk", "chunks")):
        q = (f'{{Aggregate {{{WCLASS}(where: {{path: ["kind"], operator: Equal, valueText: "{kind}"}}) '
             f'{{meta {{count}}}}}}}}')
        r = (await client.post(f"{WV}/v1/graphql", json={"query": q})).json()
        out[key] = r["data"]["Aggregate"][WCLASS][0]["meta"]["count"]
    return out


async def engine_counts(engine):
    if engine.startswith("raggio"):
        async with httpx.AsyncClient(headers=TR_HDRS, timeout=1800) as client:
            info = (await client.get(f"{TR}/collections/{COLL}")).json()
            return {"summaries": info["summaries"], "chunks": info["chunks"],
                    "index": (info.get("index") or {}).get("type"), "pending": info.get("pending_jobs")}
    async with httpx.AsyncClient(headers=WV_HDRS, timeout=120) as client:
        return {**await weaviate_counts(client), "index": "hnsw", "pending": 0}


# ---------- search ----------

async def run_phase(engine, reqs, concurrency):
    """(latencies ms, wall s, hits per request, mean response bytes)."""
    lat, hits, size = [None] * len(reqs), [None] * len(reqs), [0] * len(reqs)
    sem = asyncio.Semaphore(concurrency)
    hdrs = TR_HDRS if engine.startswith("raggio") else WV_HDRS
    async with httpx.AsyncClient(headers=hdrs, timeout=300) as client:
        async def one(i):
            url, body = reqs[i]
            payload = orjson.dumps(body, option=orjson.OPT_SERIALIZE_NUMPY)
            async with sem:
                t = time.perf_counter()
                r = await client.post(url, content=payload)
                lat[i] = (time.perf_counter() - t) * 1000
            r.raise_for_status()
            size[i] = len(r.content)
            hits[i] = parse_hits(engine, orjson.loads(r.content))
        t0 = time.time()
        await asyncio.gather(*[one(i) for i in range(len(reqs))])
        return lat, time.time() - t0, hits, statistics.mean(size)


async def cpu_around(container, phase):
    if not container:
        return await phase(), None
    path = bench.cpu_stat_path(container)
    before = bench.read_usage_usec(path)
    out = await phase()
    return out, (bench.read_usage_usec(path) - before) / 1000


async def bench_workloads(engine, sets, truth, args):
    builder = raggio_request if engine.startswith("raggio") else weaviate_request
    res = {}
    for name, qset, scope, k, hybrid, by_doc in WORKLOADS:
        qs = sets[qset][: args.k1000_queries] if k == 1000 else sets[qset]
        reqs = [builder(q, scope, k, hybrid, by_doc, args.doc_filter) for q in qs]
        print(f"[{engine}] {name}: {len(reqs)} queries serial...", flush=True)
        lat, wall, hits, size = await run_phase(engine, reqs, 1)
        if not all(hits):
            raise RuntimeError(f"{engine} {name}: a query returned no hits (wrong document filter?)")
        tr = truth[k][: len(qs)] if name.startswith("chunks") else None
        w = {"n": len(reqs), "k": k, "p50": bench.pct(lat, 50), "p95": bench.pct(lat, 95),
             "p99": bench.pct(lat, 99), "qps_serial": len(lat) / wall, "resp_kb": size / 1024,
             "quality": quality(name, qs, hits, tr, k)}
        print(f"[{engine}] {name}: concurrent x{args.concurrency}...", flush=True)
        (lat_c, wall_c, _, _), cpu_ms = await cpu_around(args.cpu_container,
                                                         lambda: run_phase(engine, reqs, args.concurrency))
        w.update(qps_c=len(lat_c) / wall_c, p50_c=bench.pct(lat_c, 50), p95_c=bench.pct(lat_c, 95))
        if cpu_ms is not None:
            w["cpu_ms_per_q_c"] = cpu_ms / len(lat_c)
        res[name] = w
        print(f"[{engine}] {name}: p50 {w['p50']:.1f} ms, QPS {w['qps_serial']:.1f} / {w['qps_c']:.1f}, "
              f"quality {w['quality']:.3f}, {w['resp_kb']:.0f} KB", flush=True)
    return res


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=["raggio", "raggio-ivf", "weaviate"])
    ap.add_argument("--mode", required=True, choices=["ingest", "search"])
    ap.add_argument("--doc-filter", default="doc_id", choices=["doc_id", "metadata"],
                    help="raggio's one-document filter: the doc_id key (0.2.0+) or the `doc` metadata key")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--summary-queries", type=int, default=500)
    ap.add_argument("--indoc-queries", type=int, default=300)
    ap.add_argument("--k1000-queries", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--cpu-container", metavar="NAME", help="container whose cgroup CPU is read per concurrent phase")
    ap.add_argument("--mem-container", metavar="NAME", help="container whose memory is read (podman stats)")
    ap.add_argument("--volume", metavar="NAME", help="volume whose size is the disk footprint")
    ap.add_argument("--docs-per-batch", type=int, default=40)
    ap.add_argument("--ingest-concurrency", type=int, default=2)
    ap.add_argument("--label", default="")
    ap.add_argument("--out", required=True)
    return ap


async def main(argv=None):
    args = build_parser().parse_args(argv)
    corpus = Path(args.corpus)
    meta, records, vecs, queries = load_corpus(corpus)
    out = {"engine": args.engine, "mode": args.mode, "label": args.label, "doc_filter": args.doc_filter,
           "concurrency": args.concurrency, "corpus": {k: meta[k] for k in
           ("dataset", "config", "revision", "shards", "model", "dims", "n_docs", "n_records", "n_summaries", "n_chunks")}}
    if args.mode == "ingest":
        ingest = ingest_raggio if args.engine.startswith("raggio") else ingest_weaviate
        out["ingest"] = await ingest(records, vecs, args.docs_per_batch, args.ingest_concurrency)
        if args.mem_container:
            out["mem_after_ingest_mb"] = bench.podman_mem(args.mem_container)
    else:
        counts = await engine_counts(args.engine)
        want = {"summaries": meta["n_summaries"], "chunks": meta["n_chunks"]}
        if {k: counts[k] for k in want} != want or counts["pending"]:
            raise SystemExit(f"{args.engine} holds {counts}, expected {want} and no pending jobs")
        if args.engine == "raggio-ivf" and counts["index"] != "ivf":
            raise SystemExit(f"raggio-ivf needs the IVF index; the collection's index is {counts['index']}")
        if args.engine == "raggio" and counts["index"] not in (None, "flat"):
            raise SystemExit(f"raggio (flat) found index {counts['index']}")
        sets = query_sets(queries, args.summary_queries, args.indoc_queries)
        rows = exact_topk(records, vecs, sets["summary"], cache=corpus / f"exact-{len(sets['summary'])}.npz")
        truth = {k: [[records[r]["id"] for r in row] for row in v] for k, v in rows.items()}
        if args.mem_container:
            out["mem_before_mb"] = bench.podman_mem(args.mem_container)
        out["workloads"] = await bench_workloads(args.engine, sets, truth, args)
        if args.mem_container:
            out["mem_after_mb"] = bench.podman_mem(args.mem_container)
    if args.volume:
        out["disk_mb"] = bench.podman_disk(args.volume)
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "workloads"}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
