"""IVF fan-out probe for ADR 0005: read-only, in-process, against a live collection.

  stages   Where one search micro-batch spends its time. 3a's DGX cycle model puts
           ~31 ms of non-kernel work in every 8-query IVF cycle; 3b blames the serial
           drain of nq x nprobe shard calls. This times each stage of the real path:
             parse    json.loads + SearchIn + _normalize, per request (summed)
             index    _IvfIndex.search wall: route + shard calls + merge
               route    queries @ centroids.T + argpartition (also inside index)
               kernel   sum of the per-(query, shard) turbovec call times
             unpack   _search_rescored's -inf filter loop
             rescore  _rescore_rows: fp16 blob fetch + exact cosine
             hydrate  _hydrate per request (summed; on the event loop in the server)
             encode   jsonable_encoder + json.dumps per request (summed)
           --filter-key adds the filtered path (nq=1, the key's most common value):
           its one-off allowlist SELECT, then per query the same stages.
           A bench cycle (1000 * 8 / QPS-concurrent ms) minus the nq=8 `total` is the
           HTTP/asyncio residual this probe cannot see.
  fanout   serial vs plain (shipped) vs grouped (audience batching, NOT shipped) over
           pool sizes on nq=8 micro-batches, ABBA-timed so every cell has a run1/run2
           noise band, plus the real group-size histogram. Spec D3 is settled by p3
           (plain q1 fan-out), so grouped is a regression guard only: GUARD prints
           REVISIT if grouped's slower run beats plain's faster run at the chosen pool
           size, which reopens D3 with the user. Nothing ships on it.
  exact    Spec §7 F item 1: ids and float32 score bits identical to
           IVF_SEARCH_THREADS=1 (the serial path) at the largest --threads, compared
           position by position so tie order counts. Unfiltered in nq=8 batches;
           filtered at nq=1 on --filter-key's most common value (the in-task cut,
           >128 ids) and on a 100-id slice of it (the tiny owners path).

Every mode first prints LISTS: the largest IVF list against turbovec's 32,768-row
pooled-path cliff (spec §7 F item 7). Never set RAYON_NUM_THREADS=1 (spec §4.2).

Queries: stages and fanout use stored fp16 vectors of random rows, --seed 7 (tunables
are chosen on seed 7). exact with --vectors uses bench.py's own query selection
(load_corpus: default_rng(seed).choice(limit, queries), sorted, rounded to 5 decimals),
so `--seed 42` is the bench's set; without --vectors it falls back to stored rows.

DGX (bench-tv container removed, so nothing else holds the volume; `podman unshare`
for the volume's file ownership; OPENBLAS_NUM_THREADS=1 as in both A/B arms, D16):
  VOL=$(podman volume inspect bench-tv --format '{{.Mountpoint}}')
  podman unshare env OPENBLAS_NUM_THREADS=1 .venv/bin/python -u bench/ivf_fanout_probe.py \\
    stages --data "$VOL" --threads 1,12 --filter-key year
  podman unshare env OPENBLAS_NUM_THREADS=1 .venv/bin/python -u bench/ivf_fanout_probe.py \\
    fanout --data "$VOL" --threads 1,2,4,8,12,16,20
  podman unshare env OPENBLAS_NUM_THREADS=1 .venv/bin/python -u bench/ivf_fanout_probe.py \\
    exact --data "$VOL" --threads 12 --vectors bench/corpus/embed-vecs.npy \\
    --limit 2549619 --queries 500 --seed 42 --filter-key year
"""
import argparse
import json
import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path

import numpy as np
from fastapi.encoders import jsonable_encoder

from raggio.app import SearchIn
from raggio.config import default_ivf_search_threads
from raggio.store import (
    IVF_DEFAULT_NPROBE,
    Collection,
    CollectionConfig,
    _filter_sql,
    _IvfIndex,
    _normalize,
    _rows_by_id,
    _ShardPool,
)

STAGES = ("parse", "route", "index", "kernel", "unpack", "rescore", "hydrate", "encode", "total")
# turbovec 1.0.0: nq=1 on a shard of this many rows (1,024 blocks) or more leaves the
# inline path for the shared rayon pool, and fan-out tops out near 2.45x (p3)
POOLED_CLIFF = 32_768


def largest_list(ivf: _IvfIndex) -> int:
    return max((len(sh) for sh in ivf.shards), default=0)


def open_collection(data: Path, name: str) -> Collection:
    """A read-only Collection shell holding exactly what the search path touches."""
    cat = sqlite3.connect(f"file:{data / 'catalog.db'}?mode=ro", uri=True)
    row = cat.execute(
        "SELECT name, dim, bit_width, model, base_url, key_hash, tokenizer, index_config"
        " FROM collections WHERE name=?",
        (name,),
    ).fetchone()
    cat.close()
    if row is None:
        sys.exit(f"no collection {name!r} in {data / 'catalog.db'}")
    cfg = CollectionConfig(*row[:6], row[6] or "unicode61", json.loads(row[7]) if row[7] else None)
    if not cfg.index_config:
        sys.exit(f"collection {name!r} has no IVF index attached")
    col = Collection.__new__(Collection)
    col.cfg, col.dir, col._closed = cfg, data / "collections" / name, False
    meta, local = col.dir / "meta.db", threading.local()

    def rdb() -> sqlite3.Connection:  # per thread, mode=ro: never modifies the database
        db = getattr(local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{meta}?mode=ro", uri=True, check_same_thread=False)
            local.db = db
        return db

    col._rdb = rdb
    t = time.perf_counter()
    col.index = _IvfIndex.load(
        col.dir / "ivf", cfg.dim, cfg.bit_width, cfg.index_config.get("nprobe", IVF_DEFAULT_NPROBE)
    )
    col.index.prepare()
    print(
        f"loaded {name}: {len(col.index)} rows, nlist {col.index.nlist}, nprobe "
        f"{col.index.nprobe}, dim {cfg.dim}, {cfg.bit_width}-bit, {time.perf_counter() - t:.1f}s",
        flush=True,
    )
    big = largest_list(col.index)
    print(
        f"LISTS largest {big} rows, {100 * big / POOLED_CLIFF:.0f}% of the "
        f"{POOLED_CLIFF:,}-row pooled-path cliff"
        + (" -- AT OR ABOVE THE CLIFF" if big >= POOLED_CLIFF else ""),
        flush=True,
    )
    return col


def sample_queries(col: Collection, n: int, seed: int) -> np.ndarray:
    """n stored vectors of random rows (fp16 blobs, renormalized), in id order."""
    db, dim = col._rdb(), col.cfg.dim
    hi = db.execute("SELECT MAX(id) FROM vecs").fetchone()[0] or 0
    ids = np.random.default_rng(seed).permutation(np.arange(1, hi + 1))
    out: list[np.ndarray] = []
    for s in range(0, len(ids), 4 * n):
        chunk = [int(i) for i in ids[s : s + 4 * n]]
        for _, blob in _rows_by_id(db, "SELECT id, vec FROM vecs WHERE id IN ({})", chunk):
            if blob is not None and len(blob) == dim * 2:
                out.append(np.frombuffer(blob, dtype=np.float16).astype(np.float32))
        if len(out) >= n:
            break
    if len(out) < n:
        sys.exit(f"only {len(out)} stored vectors, need {n}")
    return _normalize(np.stack(out[:n]))


def bench_queries(path: Path, limit: int, n: int, seed: int) -> np.ndarray:
    """bench.py's own query set (load_corpus): n corpus rows drawn with
    default_rng(seed).choice(limit, n, replace=False), sorted, rounded to 5 decimals
    as the bench sends them, then normalized as the server does."""
    vecs = np.load(path, mmap_mode="r")
    limit = min(limit, vecs.shape[0])
    rows = np.sort(np.random.default_rng(seed).choice(limit, size=n, replace=False))
    return _normalize(np.round(np.asarray(vecs[rows], dtype=np.float32), 5))


def filter_allowlist(col: Collection, key: str):
    """The key's most common stored value (bench.py filters on the mode of `year`) and
    its allowlist, selected and sorted as _vector_ids does; plus the SELECT+sort ms."""
    where, params = _filter_sql("chunks", None)
    val = col._rdb().execute(
        f"SELECT json_extract(metadata, ?) v FROM records WHERE {where} AND v IS NOT NULL"
        " GROUP BY v ORDER BY COUNT(*) DESC LIMIT 1",
        ["$." + key, *params],
    ).fetchone()[0]
    where, params = _filter_sql("chunks", {key: val})
    t = time.perf_counter()  # the allow-cache miss, paid once per filter per write
    ids = [r[0] for r in col._rdb().execute(f"SELECT id FROM records WHERE {where}", params)]
    allow = np.sort(np.array(ids, dtype=np.uint64))
    return val, allow, (time.perf_counter() - t) * 1e3


class Timed:
    """Delegating shard proxy (IdMapIndex is a frozen pyclass) logging call seconds."""

    def __init__(self, inner, log: list) -> None:
        self.inner, self.log = inner, log

    def __len__(self) -> int:
        return len(self.inner)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def search(self, q, k, allowlist=None):
        t = time.perf_counter()
        try:
            return self.inner.search(q, k=k, allowlist=allowlist)
        finally:
            self.log.append(time.perf_counter() - t)


def one_batch(col: Collection, qs: np.ndarray, k: int, allow, log: list) -> dict:
    """One micro-batch through the server's search path; seconds per stage."""
    ivf = col.index
    bodies = [json.dumps({"query": {"vector": np.round(q, 5).tolist()}, "k": k}) for q in qs]
    t0 = time.perf_counter()
    rows = [SearchIn.model_validate(json.loads(b)).query.vector for b in bodies]
    mat = np.vstack([_normalize(np.array([v], dtype=np.float32)) for v in rows])
    t1 = time.perf_counter()
    npb = min(ivf.nprobe, ivf.nlist)
    sims = mat @ ivf.centroids.T
    [np.argpartition(-sims[qi], npb - 1)[:npb] for qi in range(len(mat))]
    t2 = time.perf_counter()
    log.clear()
    kw = {"allowlist": allow} if allow is not None else {}
    t3 = time.perf_counter()
    scores, ids = ivf.search(mat, k=col._rescore_k(k), nprobe=None, **kw)
    t4 = time.perf_counter()
    unpacked = []  # Collection._search_rescored's unpack, verbatim
    for row in range(len(mat)):
        r_ids, r_scores = [], []
        for i, s in zip(ids[row], scores[row]):
            if s > -np.inf:
                r_ids.append(int(i))
                r_scores.append(float(s))
        unpacked.append((r_ids, r_scores, k))
    t5 = time.perf_counter()
    res = col._rescore_rows(mat, unpacked)
    t6 = time.perf_counter()
    hits = [col._hydrate(i, s) for i, s in res]
    t7 = time.perf_counter()
    for h in hits:
        json.dumps(jsonable_encoder({"hits": h}))
    t8 = time.perf_counter()
    return {
        "parse": t1 - t0, "route": t2 - t1, "index": t4 - t3, "kernel": sum(log),
        "calls": len(log), "unpack": t5 - t4, "rescore": t6 - t5, "hydrate": t7 - t6,
        "encode": t8 - t7, "total": (t1 - t0) + (t8 - t3),
    }


def cell(col, qs, nq, batches, k, allow, log) -> dict:
    one_batch(col, qs[:nq], k, allow, log)  # warm: page in shards, start the pool
    runs = [one_batch(col, qs[b * nq : (b + 1) * nq], k, allow, log) for b in range(batches)]
    out = {s: statistics.median(r[s] for r in runs) * 1e3 for s in STAGES}
    out["calls"] = statistics.median(r["calls"] for r in runs)
    return out


def run_stages(col: Collection, args) -> list[dict]:
    ivf, log = col.index, []
    ivf.shards = [Timed(sh, log) for sh in ivf.shards]
    nqs = [int(x) for x in args.nq.split(",")]
    qs = sample_queries(col, max(nqs) * args.batches, args.seed)
    paths = [("vector", None, nqs)]
    if args.filter_key:
        val, allow, sql_ms = filter_allowlist(col, args.filter_key)
        t = time.perf_counter()  # _intersect's shard-id cache, refilled per write to a shard
        for j in range(ivf.nlist):
            ivf._shard_ids(j)
        print(
            f"filter {args.filter_key}={val!r}: {len(allow)} ids; allowlist SELECT+sort "
            f"{sql_ms:.0f} ms, shard-id cache fill {(time.perf_counter() - t) * 1e3:.0f} ms "
            "(each once per cache miss, not in the rows below)",
            flush=True,
        )
        paths.append(("filtered", allow, [1]))
    head = f"{'path':9s} {'nq':>3s} {'T':>3s} " + " ".join(f"{s:>8s}" for s in STAGES)
    print(head + f" {'calls':>6s} {'other':>8s}   (ms, median of {args.batches})", flush=True)
    rows = []
    for label, allow, path_nqs in paths:
        for nq in path_nqs:
            for threads in [int(x) for x in args.threads.split(",")]:
                ivf._pool.close()
                ivf._pool = _ShardPool(threads, "probe")
                r = cell(col, qs, nq, args.batches, 10, allow, log)
                r.update(path=label, nq=nq, threads=threads, other=r["total"] - r["index"])
                rows.append(r)
                print(
                    f"{label:9s} {nq:3d} {threads:3d} "
                    + " ".join(f"{r[s]:8.2f}" for s in STAGES)
                    + f" {r['calls']:6.0f} {r['other']:8.2f}",
                    flush=True,
                )
    ivf._pool.close()
    return rows


def grouped_search(ivf: _IvfIndex, queries: np.ndarray, k: int, pool: _ShardPool):
    """Audience batching (NOT shipped; a regression guard for the settled spec D3): one
    call per distinct probed shard carrying every query that probes it, on the same
    pool, merged like _IvfIndex.search."""
    npb = min(ivf.nprobe, ivf.nlist)
    sims = queries @ ivf.centroids.T
    groups: dict[int, list[int]] = {}
    for qi in range(len(queries)):
        for j in np.argpartition(-sims[qi], npb - 1)[:npb]:
            if len(ivf.shards[j]):
                groups.setdefault(int(j), []).append(qi)

    def run(item):
        j, qis = item
        sh = ivf.shards[j]
        s, i = sh.search(np.ascontiguousarray(queries[qis]), k=min(k, len(sh)))
        return qis, s, i

    parts: list = [([], []) for _ in range(len(queries))]
    for qis, s, i in pool.map(run, list(groups.items())):
        for r, qi in enumerate(qis):
            parts[qi][0].append(s[r])
            parts[qi][1].append(i[r])
    width = min(k, max(sum(len(p) for p in ps) for ps, _ in parts))
    out_s = np.full((len(queries), width), -np.inf, np.float32)
    out_i = np.zeros((len(queries), width), np.uint64)
    for qi, (ps, pi) in enumerate(parts):
        s, i = np.concatenate(ps), np.concatenate(pi)
        top = np.argsort(-s)[:k]
        out_s[qi, : len(top)], out_i[qi, : len(top)] = s[top], i[top]
    return out_s, out_i, [len(q) for q in groups.values()]


def same_topk(a, b) -> bool:
    """Same (score, id) multiset per query row: the tie ORDER may differ."""
    return all(
        np.array_equal(np.sort(sa), np.sort(sb)) and np.array_equal(np.sort(ia), np.sort(ib))
        for sa, ia, sb, ib in zip(a[0], a[1], b[0], b[1])
    )


def median_ms(fn, batches) -> float:
    fn(batches[0])  # warm
    ts = []
    for b in batches:
        t = time.perf_counter()
        fn(b)
        ts.append(time.perf_counter() - t)
    return statistics.median(ts) * 1e3


def run_fanout(col: Collection, args) -> dict:
    ivf, nq = col.index, int(args.nq)
    k = col._rescore_k(10)  # the server's over-fetch depth for k=10
    qs = sample_queries(col, nq * args.batches, args.seed)
    batches = [np.ascontiguousarray(qs[b * nq : (b + 1) * nq]) for b in range(args.batches)]
    serial_pool = _ShardPool(1)
    sizes: list[int] = []
    for b in batches:
        sizes += grouped_search(ivf, b, k, serial_pool)[2]
    hist = np.bincount(sizes)[1:].tolist()
    print(
        f"nq={nq} nprobe={ivf.nprobe} k={k}: {nq * min(ivf.nprobe, ivf.nlist)} plain calls/batch, "
        f"{len(sizes) / len(batches):.1f} grouped calls/batch; audience mean "
        f"{np.mean(sizes):.2f}, histogram (size 1..) {hist}",
        flush=True,
    )

    def serial(b):
        ivf._pool = serial_pool
        return ivf.search(b, k)

    s1 = median_ms(serial, batches)
    ref = [serial(b) for b in batches]
    out = {"hist": hist, "k": k, "nq": nq, "serial": [s1], "cells": []}
    print(f"{'T':>3s} {'plain r1/r2 ms':>17s} {'grouped r1/r2 ms':>18s} {'plain x':>8s} {'grp x':>6s}  verdict")
    for threads in [int(x) for x in args.threads.split(",")]:
        pool = _ShardPool(threads, "probe")

        def plain(b):
            ivf._pool = pool
            return ivf.search(b, k)

        def grouped(b):
            return grouped_search(ivf, b, k, pool)[:2]

        plain_exact = all(
            np.array_equal(p[0], r[0]) and np.array_equal(p[1], r[1])
            for p, r in zip(map(plain, batches), ref)
        )
        grouped_same = sum(same_topk(grouped(b), r) for b, r in zip(batches, ref))
        p1, g1, g2, p2 = (median_ms(f, batches) for f in (plain, grouped, grouped, plain))
        wins = max(g1, g2) < min(p1, p2)
        c = {"threads": threads, "plain": [p1, p2], "grouped": [g1, g2], "plain_exact": plain_exact,
             "grouped_same": grouped_same, "grouped_wins": wins}
        out["cells"].append(c)
        pool.close()
        print(
            f"{threads:3d} {p1:8.2f}/{p2:8.2f} {g1:8.2f}/{g2:8.2f} {s1 / ((p1 + p2) / 2):8.2f} "
            f"{((p1 + p2) / 2) / ((g1 + g2) / 2):6.2f}  "
            f"{'GROUPED WINS' if wins else 'plain'}  (plain==serial: {plain_exact}; "
            f"grouped same top-k {grouped_same}/{len(batches)})",
            flush=True,
        )
    out["serial"].append(median_ms(serial, batches))
    means = {c["threads"]: sum(c["plain"]) / 2 for c in out["cells"]}
    bands = {c["threads"]: abs(c["plain"][0] - c["plain"][1]) for c in out["cells"]}
    best = min(means, key=means.get)
    # the smallest pool within noise of the fastest: fewer threads for the same QPS
    out["best_threads"] = min(
        t for t in means if means[t] - means[best] <= max(bands[t], bands[best])
    )
    at = next(c for c in out["cells"] if c["threads"] == out["best_threads"])
    # spec D3 is settled (p3): plain q1 ships. grouped only guards that decision: a
    # REVISIT is reported to the user, and nothing ships on it
    out["guard"] = "REVISIT" if at["grouped_wins"] else "OK"
    print(
        f"serial r1/r2 {out['serial'][0]:.2f}/{out['serial'][1]:.2f} ms; chosen pool size "
        f"{out['best_threads']} (default here {default_ivf_search_threads()}); "
        f"GUARD grouped vs plain at {out['best_threads']}: {out['guard']}",
        flush=True,
    )
    return out


def run_exact(col: Collection, args) -> dict:
    """Spec §7 F item 1: every result at the largest --threads is compared with the
    serial path (_ShardPool(1), i.e. IVF_SEARCH_THREADS=1) position by position, ids
    and float32 score bits, so a changed tie order fails too."""
    ivf, k, seed = col.index, col._rescore_k(10), args.seed
    threads = max(int(x) for x in args.threads.split(","))
    if args.vectors:
        qs = bench_queries(args.vectors, args.limit, args.queries, seed)
    else:
        qs = sample_queries(col, args.queries, seed)
    serial, pooled = _ShardPool(1), _ShardPool(threads, "probe")

    def same(kw: dict, q: np.ndarray) -> bool:
        ivf._pool = serial
        s1, i1 = ivf.search(q, k, **kw)
        ivf._pool = pooled
        s2, i2 = ivf.search(q, k, **kw)
        return (
            s1.dtype == s2.dtype == np.float32 and i1.dtype == i2.dtype
            and np.array_equal(s1.view(np.uint32), s2.view(np.uint32))
            and np.array_equal(i1, i2)
        )

    batches = [np.ascontiguousarray(qs[s : s + 8]) for s in range(0, len(qs), 8)]
    singles = [np.ascontiguousarray(qs[i : i + 1]) for i in range(len(qs))]
    out = {
        "threads": threads, "seed": seed, "queries": len(qs), "batches": len(batches),
        "unfiltered_same": sum(same({}, b) for b in batches), "filter": None,
        "filter_ids": 0, "filtered_same": 0, "tiny_ids": 0, "tiny_same": 0,
        "max_list": largest_list(ivf), "cliff": POOLED_CLIFF,
    }
    ok = out["unfiltered_same"] == len(batches)
    note = "no --filter-key"
    if args.filter_key:
        val, allow, _ = filter_allowlist(col, args.filter_key)
        tiny = allow[:: max(1, len(allow) // 100)][:100]  # <=128: the owners path
        out.update(
            filter=f"{args.filter_key}={val}", filter_ids=len(allow), tiny_ids=len(tiny),
            filtered_same=sum(same({"allowlist": allow}, q) for q in singles),
            tiny_same=sum(same({"allowlist": tiny}, q) for q in singles),
        )
        ok = ok and out["filtered_same"] == out["tiny_same"] == len(singles)
        note = f"{args.filter_key}={val!r}, {len(allow)} and {len(tiny)} ids"
    pooled.close()
    out["verdict"] = "PASS" if ok else "FAIL"
    print(
        f"EXACT seed {seed} T={threads} vs IVF_SEARCH_THREADS=1: unfiltered "
        f"{out['unfiltered_same']}/{len(batches)} batches, filtered {out['filtered_same']}"
        f"/{len(singles)}, tiny {out['tiny_same']}/{len(singles)} ({note}; ids, float32 "
        f"score bits and tie order): {out['verdict']}",
        flush=True,
    )
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["stages", "fanout", "exact"])
    ap.add_argument("--data", type=Path, required=True, help="DATA_DIR: holds catalog.db")
    ap.add_argument("--collection", default="bench")
    ap.add_argument("--seed", type=int, default=7, help="exact on the bench's set: 42")
    ap.add_argument("--batches", type=int, default=40)
    ap.add_argument("--nq", default=None, help="stages: list (default 1,8); fanout: one (8)")
    ap.add_argument("--threads", default=f"1,{default_ivf_search_threads()}",
                    help="exact: the largest value is compared with the serial path")
    ap.add_argument("--filter-key", default=None,
                    help="stages: also time this key's mode value; exact: also check it")
    ap.add_argument("--vectors", type=Path, default=None,
                    help="exact: bench.py's corpus .npy, for its own query selection")
    ap.add_argument("--limit", type=int, default=2549619, help="exact: bench.py --limit")
    ap.add_argument("--queries", type=int, default=500, help="exact: bench.py --queries")
    args = ap.parse_args(argv)
    col = open_collection(args.data, args.collection)
    if args.mode == "exact":
        return run_exact(col, args)
    if args.mode == "stages":
        args.nq = args.nq or "1,8"
        return run_stages(col, args)
    args.nq = args.nq or "8"
    return run_fanout(col, args)


if __name__ == "__main__":
    main()
