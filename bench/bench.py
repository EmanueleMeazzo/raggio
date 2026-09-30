"""Benchmark raggio vs Weaviate, side by side in podman, same corpus & protocol.

Corpus: real email embeddings from D:\\EKB (553k x 1536 float32, text-embedding-3-large).
Run:  uv run --group bench python bench/bench.py --limit 20000   # smoke
      uv run --group bench python bench/bench.py                  # full run
"""

import argparse
import asyncio
import json
import os
import platform
import re
import statistics
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import numpy as np
import orjson

from bench_reuse import (STATE_TIMEOUT_S, BenchAbort, decide, fingerprint_path, prepare_engine,
                         read_fingerprint, wait_idle, weaviate_state)

# local SSD copies of D:\EKB\kb\.ekb\* — the D: drive is slow/contended and ingest
# reads vectors inside the timed window (cp them here before running)
VEC_PATH = "bench/corpus/embed-vecs.npy"
META_PATH = "bench/corpus/embed-meta.json"
TR, WV = "http://localhost:18000", "http://localhost:18080"
TR_HDRS = {"x-api-key": "bench", "content-type": "application/json"}
WV_HDRS = {"content-type": "application/json"}
COLL, WCLASS = "bench", "Email"
TEXTS_PATH = "bench/corpus/abstracts.jsonl"  # optional: line i ↔ vector row i → real chunk text
BODY = ("Quarterly stock ageing summary for the EMEA region with revenue and GPM impact, "
        "follow-ups owed to finance and the demand planning team. ") * 3  # ~400 chars, same payload both engines

DIM = None            # set by load_corpus from the vector file
TEXTS = TITLES = None  # set by load_corpus when TEXTS_PATH exists
CORPUS_LABEL = "real email embeddings"


def path_tokens(path: str) -> str:
    return re.sub(r"[/\-_.]", " ", path.removesuffix(".md")).strip()


def doc_text(path: str, row: int | None = None) -> str:
    if TEXTS is not None:
        return TEXTS[row]  # a row-less call raises here: real-text corpora need the row
    # constant body + the doc's own path tokens, so BM25 has something to rank
    return f"{BODY} {path_tokens(path)}"


# ---------- corpus ----------

def load_corpus(limit: int, n_queries: int, seed: int):
    global DIM, TEXTS, TITLES, CORPUS_LABEL
    vecs = np.load(VEC_PATH, mmap_mode="r")
    meta = json.load(open(META_PATH))
    assert vecs.shape[0] == len(meta["paths"]), \
        f"corpus mismatch: {vecs.shape[0]} vectors vs {len(meta['paths'])} meta paths — stale mix?"
    limit = min(limit, vecs.shape[0])
    DIM = int(vecs.shape[1])
    prog = Path("bench/corpus/prep-progress.json")
    if prog.exists():  # calibration-only prep leaves rows beyond next_row as zeros
        next_row = json.loads(prog.read_text())["next_row"]
        assert next_row >= limit, f"embed checkpoint at row {next_row} < limit {limit} — corpus not fully embedded"
    # texts_file in meta binds abstracts.jsonl to THIS corpus; a leftover jsonl from another
    # corpus (meta without the key) is ignored rather than silently paired with wrong vectors
    if meta.get("texts_file") and Path(TEXTS_PATH).exists():
        TEXTS, TITLES = [], []
        with open(TEXTS_PATH, "rb") as f:
            for line in f:
                rec = orjson.loads(line)
                TEXTS.append(rec["text"])
                TITLES.append(rec["title"])
                if len(TEXTS) >= limit:
                    break
        assert len(TEXTS) >= limit, f"{TEXTS_PATH}: {len(TEXTS)} lines < limit {limit} — truncated?"
        CORPUS_LABEL = f"real abstracts, {meta.get('model', 'unknown model')}"
        CORPUS_LABEL += f" @{meta['dataset_revision'][:8]}" if meta.get("dataset_revision") else ""
        print(f"corpus text: {len(TEXTS)} real abstracts loaded from {TEXTS_PATH}")
    paths = meta["paths"][:limit]
    rng = np.random.default_rng(seed)
    query_rows = np.sort(rng.choice(limit, size=n_queries, replace=False))
    is_query = np.zeros(limit, dtype=bool)
    is_query[query_rows] = True
    ingest_rows = np.nonzero(~is_query)[0]
    years = [p.split("/")[1] if "/" in p else "na" for p in paths]
    # rounding to 5dp shrinks JSON ~2.5x; unit-vector error ~1e-5, irrelevant for top-10
    queries = np.round(np.asarray(vecs[query_rows], dtype=np.float32), 5)
    return vecs, paths, years, ingest_rows, query_rows, queries


def ground_truth(vecs, ingest_rows, queries, cache: Path, k=10):
    if cache.exists():
        return np.load(cache)["gt"]
    qn = (queries / np.linalg.norm(queries, axis=1, keepdims=True)).T.astype(np.float32)
    best_s = np.full((queries.shape[0], 0), 0, dtype=np.float32)
    best_r = np.full((queries.shape[0], 0), -1, dtype=np.int64)
    t0 = time.time()
    for start in range(0, len(ingest_rows), 50_000):
        rows = ingest_rows[start : start + 50_000]
        block = np.asarray(vecs[rows], dtype=np.float32)
        block /= np.linalg.norm(block, axis=1, keepdims=True)
        s = block @ qn  # (block, nq)
        top = np.argpartition(-s, min(k, len(rows) - 1), axis=0)[:k].T  # (nq, k)
        best_s = np.concatenate([best_s, np.take_along_axis(s.T, top, 1)], axis=1)
        best_r = np.concatenate([best_r, rows[top]], axis=1)
        keep = np.argpartition(-best_s, k - 1, axis=1)[:, :k]
        best_s = np.take_along_axis(best_s, keep, 1)
        best_r = np.take_along_axis(best_r, keep, 1)
        print(f"  ground truth {start + len(rows)}/{len(ingest_rows)} ({time.time() - t0:.0f}s)", end="\r")
    print()
    np.savez_compressed(cache, gt=best_r)
    return best_r


# ---------- podman metrics ----------

def podman_mem(container: str) -> float:
    out = subprocess.run(["podman", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
                         capture_output=True, text=True).stdout.strip()
    m = re.match(r"([\d.]+)\s*([a-zA-Z]+)", out.split("/")[0].strip())  # e.g. "512.3MB / 1.074GB"
    return float(m.group(1)) * {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "MiB": 1048576,
                                "GB": 1e9, "GiB": 1073741824}[m.group(2)] / 1e6  # MB


def podman_disk(volume: str) -> float:
    out = subprocess.run(["podman", "run", "--rm", "-v", f"{volume}:/x:ro", "alpine", "du", "-sb", "/x"],
                         capture_output=True, text=True).stdout
    return int(out.split()[0]) / 1e6  # MB


CGROUP_ROOT = "/sys/fs/cgroup"
CPU_KEYS = {"cpu_ms_per_q_c", "hybrid_cpu_ms_per_q_c"}  # absent, never 0, without a cgroup read


def cpu_stat_path(container):
    """The container's cgroup v2 cpu.stat, found as DGX probe p4 did: /sys/fs/cgroup plus
    `podman inspect --format {{.State.CgroupPath}}`. OSError when podman or the inspect fails."""
    r = subprocess.run(["podman", "inspect", container, "--format", "{{.State.CgroupPath}}"],
                       capture_output=True, text=True)
    cgroup = r.stdout.strip()
    if r.returncode != 0 or not cgroup.startswith("/"):
        raise OSError(f"podman inspect {container} gave no cgroup path (exit {r.returncode})")
    return f"{CGROUP_ROOT}{cgroup}/cpu.stat"


def read_usage_usec(path):
    """usage_usec (the cgroup's total CPU time, microseconds) from a cgroup v2 cpu.stat."""
    for line in Path(path).read_text().splitlines():
        field, _, value = line.partition(" ")
        if field == "usage_usec":
            return int(value)
    raise OSError(f"{path} has no usage_usec line")


async def cpu_phase(name, key, container, phase):
    """Run one concurrent phase and read the server container's cgroup CPU around it (spec §3.1
    G5). Returns (the phase's result, {key: CPU ms per request}); the dict is empty, with one
    line saying why, when --cpu-container is unset or another engine's, or a read fails."""
    before = why = None
    if not container:
        why = "no --cpu-container"
    elif container != ENGINES[name]["container"]:
        why = f"--cpu-container {container} is not {name}'s container"
    else:
        try:
            path = cpu_stat_path(container)
            before = read_usage_usec(path)
        except (OSError, ValueError) as e:
            why = str(e)
    out = await phase()
    if why is None:
        try:
            return out, {key: (read_usage_usec(path) - before) / 1000 / len(out[0])}
        except (OSError, ValueError) as e:
            why = str(e)
    print(f"[{name}] {key} absent: {why}")
    return out, {}


# ---------- ingest ----------

def batches_by_doc(paths, years, ingest_rows, batch_size):
    """Yield lists of (row, path, year), whole documents kept in one batch."""
    batch, prev_path = [], None
    for row in ingest_rows:
        p = paths[row]
        if len(batch) >= batch_size and p != prev_path:
            yield batch
            batch = []
        batch.append((int(row), p, years[row]))
        prev_path = p
    if batch:
        yield batch


def to_docs(batch, vecs):
    docs, cur = [], None
    for row, path, year in batch:
        if cur is None or cur["doc_id"] != path:
            cur = {"doc_id": path, "chunks": []}
            docs.append(cur)
        vec = np.round(np.asarray(vecs[row], dtype=np.float32), 5)
        cur["chunks"].append({"id": f"r{row}", "text": doc_text(path, row), "vector": vec,
                              "metadata": {"year": year}})
    return docs


async def post_batches(client, reqs, concurrency, label, slim=None):
    sem, results, t0 = asyncio.Semaphore(concurrency), [], time.time()
    done = 0

    async def one(build):
        nonlocal done
        async with sem:
            url, payload = build()
            r = await client.post(url, content=orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY))
            r.raise_for_status()
            done += 1
            if done % 100 == 0:
                print(f"  {label} {done}/{len(reqs)} batches ({time.time() - t0:.0f}s)", end="\r")
            resp = r.json()
            # slim reduces each response before it is retained: weaviate echoes every object
            # back INCLUDING its vector — keeping 10k raw responses OOMs the host at 2.55M rows
            return slim(resp) if slim else resp

    results = await asyncio.gather(*[one(b) for b in reqs])
    print()
    return results


async def tr_wait_job(client, jid):
    while True:
        s = (await client.get(f"{TR}/collections/{COLL}/jobs/{jid}")).json()
        if s["status"] == "done":
            return
        if s["status"] == "error":
            raise RuntimeError(f"raggio job {jid} failed: {s['error']}")
        await asyncio.sleep(0.25)


async def ingest_raggio(vecs, paths, years, ingest_rows, batch_size, concurrency):
    async with httpx.AsyncClient(headers=TR_HDRS, timeout=600) as client:
        await client.delete(f"{TR}/collections/{COLL}")
        (await client.post(f"{TR}/collections", json={"name": COLL, "dim": DIM})).raise_for_status()
        reqs = [(lambda b=b: (f"{TR}/collections/{COLL}/documents", {"documents": to_docs(b, vecs)}))
                for b in batches_by_doc(paths, years, ingest_rows, batch_size)]
        t0 = time.time()
        results = await post_batches(client, reqs, concurrency, "raggio ingest")
        for jid in sorted(r["job_id"] for r in results):
            await tr_wait_job(client, jid)  # worker is serial, so polling in id order visits each once
        elapsed = time.time() - t0
        count = (await client.get(f"{TR}/collections/{COLL}")).json()["chunks"]
        return elapsed, count


async def ensure_no_index():
    """A leftover IVF index would silently accelerate the flat raggio column."""
    async with httpx.AsyncClient(headers=TR_HDRS, timeout=600) as client:
        r = await client.delete(f"{TR}/collections/{COLL}/index")
        if r.status_code == 202:
            await tr_wait_job(client, r.json()["job_id"])
            print("  dropped leftover IVF index")


async def build_ivf_index():
    """The measured raggio-ivf step: (re)build the IVF index over the flat collection's data."""
    async with httpx.AsyncClient(headers=TR_HDRS, timeout=STATE_TIMEOUT_S) as client:
        t0 = time.time()
        r = await client.post(f"{TR}/collections/{COLL}/index", json={})
        r.raise_for_status()
        await tr_wait_job(client, r.json()["job_id"])
        elapsed = time.time() - t0
        info = (await client.get(f"{TR}/collections/{COLL}")).json()
        print(f"  ivf index built in {elapsed:.0f}s: {info.get('index')}")
        return elapsed


async def ingest_weaviate(vecs, paths, years, ingest_rows, batch_size, concurrency):
    # 120s/request: a healthy Weaviate answers a 250-object batch in <5s; a longer stall
    # means it's wedged (memory livelock) and we want a loud failure, not an overnight hang
    async with httpx.AsyncClient(headers=WV_HDRS, timeout=120) as client:
        await client.delete(f"{WV}/v1/schema/{WCLASS}")
        schema = {"class": WCLASS, "vectorizer": "none",
                  "vectorIndexConfig": {"distance": "cosine"},
                  "properties": [{"name": n, "dataType": ["text"]}
                                 for n in ("chunkId", "docId", "year", "body")]}
        (await client.post(f"{WV}/v1/schema", json=schema)).raise_for_status()

        def build_objects(batch):
            return {"objects": [
                {"class": WCLASS, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"r{row}")),
                 "properties": {"chunkId": f"r{row}", "docId": path, "year": year, "body": doc_text(path, row)},
                 "vector": np.round(np.asarray(vecs[row], dtype=np.float32), 5)}
                for row, path, year in batch]}

        reqs = [(lambda b=b: (f"{WV}/v1/batch/objects", build_objects(b)))
                for b in batches_by_doc(paths, years, ingest_rows, batch_size)]
        t0 = time.time()
        results = await post_batches(client, reqs, concurrency, "weaviate ingest",
                                     slim=lambda r: [o["result"] for o in r
                                                     if o.get("result", {}).get("status") == "FAILED"])
        errors = [e for r in results for e in r]
        if errors:
            raise RuntimeError(f"weaviate batch errors: {errors[:3]} (+{len(errors)} total)")
        elapsed = time.time() - t0
        agg = {"query": f'{{Aggregate {{{WCLASS} {{meta {{count}}}}}}}}'}
        r = (await client.post(f"{WV}/v1/graphql", json=agg)).json()
        count = r["data"]["Aggregate"][WCLASS][0]["meta"]["count"]
        return elapsed, count


# ---------- search ----------

def tr_search_req(qvec, k=10, filt=None, text=None):
    body = {"query": {"vector": qvec}, "k": k}
    if text:
        body["query"]["text"] = text
        body["mode"] = "hybrid"
    if filt:
        body["filter"] = filt
    return f"{TR}/collections/{COLL}/search", body


def wv_search_req(qvec, k=10, filt=None, text=None):
    vec = orjson.dumps(qvec, option=orjson.OPT_SERIALIZE_NUMPY).decode()
    where = (f'where: {{path: ["year"], operator: Equal, valueText: "{filt["year"]}"}}, ' if filt else "")
    if text:
        # alpha 0.5 + rankedFusion + body-only BM25 ~ raggio's unweighted RRF over chunk text
        search = (f'hybrid: {{query: {json.dumps(text)}, vector: {vec}, alpha: 0.5, '
                  f'fusionType: rankedFusion, properties: ["body"]}}')
        extra = "_additional {score}"
    else:
        search = f'nearVector: {{vector: {vec}}}'
        extra = "_additional {distance}"
    q = f'{{Get {{{WCLASS}({search}, limit: {k}, {where}) {{chunkId {extra}}}}}}}'
    return f"{WV}/v1/graphql", {"query": q}


def parse_hits(engine, resp):
    if engine.startswith("raggio"):
        return [h["id"] for h in resp["hits"]]
    if resp.get("errors"):  # weaviate returns HTTP 200 for graphql errors
        raise RuntimeError(f"weaviate graphql: {str(resp['errors'])[:300]}")
    data = (resp.get("data") or {}).get("Get", {}).get(WCLASS) or []
    return [h["chunkId"] for h in data]


async def run_queries(engine, queries, concurrency, headers, filt=None, k=10, texts=None):
    """Return (latencies_ms, wall_seconds, hits_per_query)."""
    build = tr_search_req if engine.startswith("raggio") else wv_search_req
    lat, hits = [None] * len(queries), [None] * len(queries)
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:

        async def one(i):
            async with sem:
                url, body = build(queries[i], k=k, filt=filt, text=texts[i] if texts else None)
                t = time.perf_counter()
                r = await client.post(url, content=orjson.dumps(body, option=orjson.OPT_SERIALIZE_NUMPY))
                lat[i] = (time.perf_counter() - t) * 1000
                r.raise_for_status()
                hits[i] = parse_hits(engine, r.json())

        t0 = time.time()
        await asyncio.gather(*[one(i) for i in range(len(queries))])
        return lat, time.time() - t0, hits


def pct(lat, p):
    return statistics.quantiles(lat, n=100)[p - 1]


def hybrid_first_pass(lat_h, n=10):
    """The first n serial hybrid queries apart from the rest (spec §6: after a load the first
    ~10 take 470-980 ms). With concurrency 1 the queries run in list order, so lat_h[:n] are
    the first n sent."""
    if len(lat_h) < n + 2:
        return {}
    return {"hybrid_first10_max_ms": max(lat_h[:n]), "hybrid_p99_after10": pct(lat_h[n:], 99)}


def cold_start(container, engine, probe_vec, headers):
    subprocess.run(["podman", "restart", "-t", "2", container], capture_output=True)
    t0 = time.time()
    url, body = (tr_search_req if engine.startswith("raggio") else wv_search_req)(probe_vec)
    while True:
        try:
            r = httpx.post(url, content=orjson.dumps(body, option=orjson.OPT_SERIALIZE_NUMPY),
                           headers=headers, timeout=60)
            if r.status_code == 200 and parse_hits(engine, r.json()):
                return time.time() - t0
        except (httpx.HTTPError, RuntimeError):
            # RuntimeError: weaviate answers 200 + graphql error ("non-existing index")
            # while the HNSW index is still loading — that's "not ready", not a failure
            pass
        time.sleep(0.5)
        if time.time() - t0 > 600:
            raise TimeoutError(f"{container} not serving after restart")


# ---------- main ----------

# "fp": whose data the column measures. raggio-ivf runs on the flat collection's data
# (plus an index), so both raggio columns share one fingerprint.
ENGINES = {
    "raggio": {"container": "bench-tv", "hdrs": TR_HDRS, "volume": "bench-tv", "fp": "raggio",
               "ingest": ingest_raggio, "build_index": None},
    "raggio-ivf": {"container": "bench-tv", "hdrs": TR_HDRS, "volume": "bench-tv", "fp": "raggio",
                   "ingest": ingest_raggio, "build_index": build_ivf_index},
    "weaviate": {"container": "bench-wv", "hdrs": WV_HDRS, "volume": "bench-wv", "fp": "weaviate",
                 "ingest": ingest_weaviate, "build_index": None},
}


async def engine_state(name, timeout):
    """What the engine holds; raggio is read once its job queue has drained."""
    async with httpx.AsyncClient(headers=ENGINES[name]["hdrs"], timeout=timeout) as client:
        if name.startswith("raggio"):
            return await wait_idle(client, TR, COLL)
        return await weaviate_state(client, WV, WCLASS)


def fp_file(name, args):
    return fingerprint_path(args.fingerprint_dir, ENGINES[name]["fp"], args.limit)


def fingerprint(args):
    # corpus identity the chunk count alone can't see: split params + payload-text version
    return {"limit": args.limit, "seed": args.seed, "queries": args.queries,
            "text_v": f"{CORPUS_LABEL}/d{DIM}" if TEXTS is not None else doc_text("emails/2000/01/probe.md")}


async def preflight(names, expected, args):
    """Every engine's reuse decision before the ground-truth pass (minutes at 2.5M rows):
    a wrong --limit, a missing fingerprint or an unreachable engine aborts here."""
    for name in names:
        st = await engine_state(name, args.state_timeout)
        fp_path = fp_file(name, args)
        action = decide(name, st, expected, read_fingerprint(fp_path), fingerprint(args),
                        reingest=args.reingest, adopt=args.adopt, limit=args.limit, fp_path=fp_path)
        print(f"[{name}] preflight: {st.chunks} chunks, ivf={st.ivf} -> {action}")


async def bench_engine(name, vecs, paths, years, ingest_rows, queries, gt_rows, hybrid_texts, hybrid_paths, args):
    e = ENGINES[name]
    res = await prepare_engine(
        name, state=lambda: engine_state(name, args.state_timeout),
        ingest=lambda: e["ingest"](vecs, paths, years, ingest_rows, args.batch_size, args.ingest_concurrency),
        build_index=e["build_index"], fp_path=fp_file(name, args), expected=len(ingest_rows),
        fp_now=fingerprint(args), reingest=args.reingest, adopt=args.adopt, limit=args.limit)
    if name == "raggio":
        await ensure_no_index()  # flat column must measure the brute-force scan, never a leftover index
    if name.startswith("raggio"):
        await engine_state(name, args.state_timeout)  # measure only once the job queue is empty
    res["mem_after_ingest_mb"] = podman_mem(e["container"])
    res["disk_mb"] = podman_disk(e["volume"])

    print(f"[{name}] serial search...")
    lat, wall, hits = await run_queries(name, queries, 1, e["hdrs"])
    res.update(lat_p50=pct(lat, 50), lat_p95=pct(lat, 95), lat_p99=pct(lat, 99), qps_serial=len(lat) / wall)

    truth = [set(f"r{r}" for r in row) for row in gt_rows]
    res["recall_at_10"] = statistics.mean(len(set(h) & t) / 10 for h, t in zip(hits, truth))

    print(f"[{name}] concurrent search (x{args.concurrency})...")
    mem_probe = asyncio.create_task(asyncio.to_thread(podman_mem, e["container"]))
    (lat_c, wall_c, _), cpu = await cpu_phase(
        name, "cpu_ms_per_q_c", args.cpu_container,
        lambda: run_queries(name, queries, args.concurrency, e["hdrs"]))
    res.update(qps_concurrent=len(lat_c) / wall_c, lat_c_p95=pct(lat_c, 95),
               mem_under_load_mb=await mem_probe, **cpu)

    year = statistics.mode(years[r] for r in ingest_rows)
    print(f"[{name}] filtered search (year={year})...")
    lat_f, _, hits_f = await run_queries(name, queries[: args.filtered_queries], 1, e["hdrs"], filt={"year": year})
    res.update(lat_filtered_p50=pct(lat_f, 50), lat_filtered_p95=pct(lat_f, 95),
               filtered_ok=all(hits_f))

    print(f"[{name}] hybrid search (vector + BM25)...")
    lat_h, wall_h, hits_h = await run_queries(name, queries, 1, e["hdrs"], texts=hybrid_texts)
    res.update(hybrid_p50=pct(lat_h, 50), hybrid_p95=pct(lat_h, 95), hybrid_p99=pct(lat_h, 99),
               hybrid_qps_serial=len(lat_h) / wall_h)
    res.update(hybrid_first_pass(lat_h))  # the text index's warm-up, reported on its own
    # did the doc whose path tokens we queried surface through fusion?
    res["hybrid_text_hit_rate"] = statistics.mean(
        any(paths[int(h[1:])] == p for h in hits) for hits, p in zip(hits_h, hybrid_paths))
    assert all(hits_h), f"{name}: hybrid queries returned empty hits"

    print(f"[{name}] hybrid concurrent search (x{args.concurrency})...")
    (lat_hc, wall_hc, _), cpu = await cpu_phase(
        name, "hybrid_cpu_ms_per_q_c", args.cpu_container,
        lambda: run_queries(name, queries, args.concurrency, e["hdrs"], texts=hybrid_texts))
    res.update(hybrid_qps_concurrent=len(lat_hc) / wall_hc, hybrid_c_p99=pct(lat_hc, 99), **cpu)

    print(f"[{name}] cold start...")
    res["cold_start_s"] = cold_start(e["container"], name, queries[0], e["hdrs"])
    return res


ROWS = [("Ingest wall time (s)", "ingest_s", "{:.0f}"), ("Ingest throughput (vec/s)", "ingest_vps", "{:.0f}"),
        ("IVF index build (s)", "index_build_s", "{:.0f}"),
        ("Memory after ingest (MB)", "mem_after_ingest_mb", "{:.0f}"),
        ("Memory under query load (MB)", "mem_under_load_mb", "{:.0f}"),
        ("Disk footprint (MB)", "disk_mb", "{:.0f}"),
        ("Search p50 (ms)", "lat_p50", "{:.1f}"), ("Search p95 (ms)", "lat_p95", "{:.1f}"),
        ("Search p99 (ms)", "lat_p99", "{:.1f}"), ("QPS serial", "qps_serial", "{:.0f}"),
        ("QPS concurrent", "qps_concurrent", "{:.0f}"), ("p95 under concurrency (ms)", "lat_c_p95", "{:.1f}"),
        ("CPU per query under concurrency (ms)", "cpu_ms_per_q_c", "{:.1f}"),
        ("Filtered p50 (ms)", "lat_filtered_p50", "{:.1f}"), ("Filtered p95 (ms)", "lat_filtered_p95", "{:.1f}"),
        ("Recall@10 vs exact", "recall_at_10", "{:.3f}"),
        ("Hybrid p50 (ms)", "hybrid_p50", "{:.1f}"), ("Hybrid p95 (ms)", "hybrid_p95", "{:.1f}"),
        ("Hybrid p99 (ms)", "hybrid_p99", "{:.1f}"),
        ("Hybrid first 10 queries, slowest (ms)", "hybrid_first10_max_ms", "{:.1f}"),
        ("Hybrid p99 without the first 10 (ms)", "hybrid_p99_after10", "{:.1f}"),
        ("Hybrid QPS serial", "hybrid_qps_serial", "{:.1f}"),
        ("Hybrid QPS concurrent", "hybrid_qps_concurrent", "{:.1f}"),
        ("Hybrid p99 under concurrency (ms)", "hybrid_c_p99", "{:.1f}"),
        ("Hybrid CPU per query under concurrency (ms)", "hybrid_cpu_ms_per_q_c", "{:.1f}"),
        ("Hybrid text-hit@10", "hybrid_text_hit_rate", "{:.3f}"),
        ("Cold start to first query (s)", "cold_start_s", "{:.1f}")]


def report(results, args, n_ingested):
    names = list(results)
    hybrid_src = "title" if TEXTS is not None else "path tokens"
    lines = [f"# raggio vs Weaviate — {n_ingested:,} vectors x {DIM} dims ({CORPUS_LABEL})", "",
             f"Config: k=10, {args.queries} held-out corpus queries, concurrency {args.concurrency}, "
             f"seed {args.seed}. {args.caps_note} Host: {args.host}. "
             f"Hybrid: raggio unweighted RRF (k=60) vs Weaviate rankedFusion alpha 0.5 over the body "
             f"property; query = held-out vector + {hybrid_src} of a sampled ingested doc.", "",
             "| Metric | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for label, key, fmt in ROWS:
        if key in CPU_KEYS and not any(key in results[n] for n in names):
            continue  # no cgroup read on this host: the row is absent, never 0
        vals = [fmt.format(results[n][key]) if key in results[n] else "—" for n in names]
        lines.append(f"| {label} | " + " | ".join(vals) + " |")
    lines += ["", "Notes: raggio = single Python asyncio process, brute-force scan over 4-bit quantized "
              "vectors + exact fp16 rescore of the top candidates, two-stage BM25 (bounded FTS5 candidate "
              "generation + full-query rescore), per-collection lock. raggio-ivf = the same store and "
              "data with the optional IVF index built (approximate, default nprobe, same rescore). "
              "Weaviate = Go, HNSW approximate index, uncompressed vectors. All queried via REST with "
              "client-supplied vectors; identical stored payloads. The concurrent phases repeat the "
              "serial phases' queries (warm caches). The first 10 serial hybrid queries are also "
              "reported on their own: after a container start they include the text index's warm-up. "
              "CPU per query is the server container's cgroup CPU time over a concurrent phase, "
              "divided by its requests; its rows are absent where the cgroup is not readable."]
    return "\n".join(lines)


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="all", choices=["all", "raggio", "raggio-ivf", "weaviate"])
    ap.add_argument("--limit", type=int, default=553_015)
    ap.add_argument("--queries", type=int, default=500)
    ap.add_argument("--filtered-queries", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--cpu-container", metavar="NAME",
                    help="container whose cgroup cpu.stat gives the server CPU per query of the "
                         "concurrent phases (the DGX runbook passes bench-tv); without it, or when "
                         "the cgroup is not readable, the CPU rows are absent")
    ap.add_argument("--ingest-concurrency", type=int, default=2)
    ap.add_argument("--batch-size", type=int, default=250)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reingest", action="store_true",
                    help="wipe and re-ingest even if the engine already holds the corpus "
                         "(raggio-ivf: rebuild only the index when the data matches)")
    ap.add_argument("--adopt", action="store_true",
                    help="trust the engine's data when its chunk count matches and no fingerprint "
                         "is on file, e.g. a volume carried over from another checkout; records "
                         "this run's fingerprint (a different fingerprint on file still aborts)")
    ap.add_argument("--fingerprint-dir", default="bench",
                    help="directory for fingerprint-<data>-<limit>.json; keep it outside a checkout "
                         "that a deploy wipes")
    ap.add_argument("--state-timeout", type=float, default=STATE_TIMEOUT_S,
                    help="seconds to wait for an engine's state (the first request after a "
                         "container start opens the collection)")
    ap.add_argument("--out", default="bench/results.md")
    ap.add_argument("--host", default=f"{platform.system()}/{platform.machine()}, {os.cpu_count()} CPUs",
                    help="host description for the report header (default: autodetected)")
    ap.add_argument("--caps-note", default="raggio container capped at 1 GiB (4-bit quantized flat "
                    "index), Weaviate at 8 GiB (HNSW, float32, defaults).",
                    help="container memory-caps sentence for the report header")
    return ap


async def main(argv=None):
    args = build_parser().parse_args(argv)
    names = list(ENGINES) if args.engine == "all" else [args.engine]

    vecs, paths, years, ingest_rows, _, queries = load_corpus(args.limit, args.queries, args.seed)
    print(f"corpus: {len(ingest_rows)} ingest rows, {len(queries)} queries")
    await preflight(names, len(ingest_rows), args)
    # dim in the cache name: swapping the corpus in place must not reuse a stale ground truth
    gt = ground_truth(vecs, ingest_rows, queries, Path(f"bench/gt-{args.limit}-{args.seed}-d{DIM}.npz"))

    # hybrid queries: held-out vector + title (real-text corpus) or path tokens of a sampled ingested doc
    hrng = np.random.default_rng(args.seed)
    hybrid_rows = hrng.choice(ingest_rows, size=len(queries), replace=False)
    hybrid_texts = [TITLES[r] if TITLES is not None else path_tokens(paths[r]) for r in hybrid_rows]
    hybrid_paths = [paths[r] for r in hybrid_rows]

    partial = Path("bench/results-partial.json")
    results = json.loads(partial.read_text()) if partial.exists() else {}
    for name in names:
        res = await bench_engine(name, vecs, paths, years, ingest_rows, queries, gt, hybrid_texts, hybrid_paths, args)
        results[name] = {**results.get(name, {}), **res}  # keep checkpointed ingest stats on reuse runs
        partial.write_text(json.dumps(results))  # survive a crash of the other engine

    md = report(results, args, len(ingest_rows))
    Path(args.out).write_text(md, encoding="utf-8")
    print("\n" + md)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BenchAbort as e:
        raise SystemExit(f"bench aborted: {e}")
