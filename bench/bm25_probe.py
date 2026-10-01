"""Stage-2 BM25 on a real collection, and one A/B arm's hybrid passes (ADR 0004).

`parity` runs raggio's own text leg (Collection._text_ids and everything it calls) over
a collection's meta.db, opened read-only, once with raggio_native and once with the
pure-Python reference, on the hybrid query texts bench.py sends. It reports:
  - parity over every query: id-order and score-bit mismatches (both must be 0) and
    the largest relative score difference;
  - speed, on a second pass over a warm page cache: stage-2 and text-leg p50/p99 per
    scorer, and text-leg queries/s per thread count per scorer (reported, not gated:
    SQLite's memstatus mutex caps it, spec D15);
  - the ranked OR (stage 1a) against the posting budget: how many queries overrun it,
    and the match count of every underscore query (spec D14, the prune arm's gate).
`passes` sends bench.py's hybrid queries to a running server: --passes serial passes
(pass 1, the first after the load, is reported apart), then one concurrent pass.

1. On the host, in the bench venv (reads the corpus the way bench.py does):
     .venv/bin/python bench/bm25_probe.py queries bench/bm25-queries.json
2. Inside the image under test (its interpreter and its extension), bench-tv stopped:
     podman run --rm --memory 4g -v bench-tv:/data -v "$PWD/bench:/bench:ro" IMAGE \\
       python /bench/bm25_probe.py parity /data/collections/bench/meta.db /bench/bm25-queries.json
   The last line printed is the JSON summary. Exit status 1 when any score differs.
3. On the host, with a freshly started bench-tv serving on :18000:
     .venv/bin/python bench/bm25_probe.py passes arm.json
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from raggio import store
from raggio.store import Collection, CollectionConfig, _or_query

N = store.HYBRID_DEPTH  # the text leg's depth in a hybrid search with k <= 100
SCOPE = "chunks"


class TextLeg(Collection):
    """Collection's text leg over one meta.db, opened read-only per thread.
    Collection.__init__ is skipped on purpose: it would open the vector index, the
    writer and the job worker, and _text_ids (with what it calls) reads none of them."""

    def __init__(self, meta_db, native_bm25: bool):  # no super().__init__(): see above
        self.meta_uri = Path(meta_db).resolve().as_uri() + "?mode=ro"
        self._read_local = threading.local()
        self._df_cache, self._df_cache_churn, self._avgdl_cache = {}, 0, None
        self._native_bm25 = native_bm25
        self.stage2_ms: list[float] = []
        db = self._rdb()
        # _prune_common branches on the tokenizer (D14): read it from the FTS schema
        (fts_sql,) = db.execute("SELECT sql FROM sqlite_master WHERE name='records_fts'").fetchone()
        tokenizer = "trigram" if "trigram" in fts_sql else "unicode61"
        self.cfg = CollectionConfig("probe", 0, 0, None, None, None, tokenizer=tokenizer)
        self.indexed_counts = dict(
            db.execute("SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type")
        )

    def _rdb(self) -> sqlite3.Connection:
        db = getattr(self._read_local, "db", None)
        if db is None:
            db = sqlite3.connect(self.meta_uri, uri=True, check_same_thread=False)
            db.execute("PRAGMA query_only=1")
            self._read_local.db = db
        return db

    def _bm25_rescore(self, qtext, cand, n):
        t0 = time.perf_counter()
        try:
            return super()._bm25_rescore(qtext, cand, n)
        finally:
            self.stage2_ms.append((time.perf_counter() - t0) * 1e3)


def hybrid_inputs(limit: int, n: int, seed: int):
    """bench.py's hybrid inputs for --limit/--queries/--seed, derived with the same lines
    as its main() (tests/test_native_bm25.py pins them). Returns the bench module, the
    query vectors, the query texts, the corpus paths, and each text's source path."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bench
    import numpy as np

    _, paths, _, ingest_rows, _, queries = bench.load_corpus(limit, n, seed)
    hrng = np.random.default_rng(seed)
    hybrid_rows = hrng.choice(ingest_rows, size=len(queries), replace=False)
    titles = bench.TITLES
    texts = [titles[r] if titles is not None else bench.path_tokens(paths[r]) for r in hybrid_rows]
    return bench, queries, texts, paths, [paths[r] for r in hybrid_rows]


def is_two_stage(leg: TextLeg, q: str) -> bool:
    kept, toks = leg._prune_common(q)
    return 0 < len(kept) < len(toks)


def or_budget(leg: TextLeg) -> int:
    """The posting budget _prune_common holds the ranked OR to."""
    total = sum(leg.indexed_counts.values())
    return max(store.FTS_SCAN_BUDGET_MIN_ROWS, int(store.FTS_SCAN_BUDGET * total))


def or_matches(leg: TextLeg, q: str) -> int:
    """Rows the ranked OR (stage 1a) matches for q before its LIMIT: the scan the budget
    bounds. A df-0 underscore token kept by the old prune made this most of the corpus."""
    kept, _ = leg._prune_common(q)
    if not kept:
        return 0
    sql = "SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH ?"
    return leg._rdb().execute(sql, [_or_query(kept)]).fetchone()[0]


def _pct(ms: list[float], p: int) -> float | None:
    if not ms:
        return None
    xs = sorted(ms)
    return round(xs[min(len(xs) - 1, p * len(xs) // 100)], 3)


def _qps(fn, queries: list[str], threads: int) -> float:
    """Queries/s of fn over the queries, split round-robin across `threads` threads."""
    def work(i):
        for q in queries[i::threads]:
            fn(q)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(work, range(threads)))  # re-raises a worker's exception
    return round(len(queries) / (time.perf_counter() - t0), 1)


def parity(meta_db, queries: list[str], threads=(1, 2, 4, 8)) -> dict:
    if store._native is None:
        raise SystemExit("raggio_native is not loaded: run the probe in the image")
    nat, py = TextLeg(meta_db, True), TextLeg(meta_db, False)
    res: dict = {"queries": len(queries), "avgdl_native": nat._avgdl(), "avgdl_python": py._avgdl()}
    two = sum(is_two_stage(py, q) for q in queries)
    ids_bad = bits_bad = 0
    max_rel = 0.0
    for q in queries:  # pass 1: parity; also warms the page cache and both df caches
        a, b = nat._text_ids(q, N, SCOPE, None), py._text_ids(q, N, SCOPE, None)
        ids_bad += a[0] != b[0]
        bits_bad += list(map(repr, a[1])) != list(map(repr, b[1]))
        for x, y in zip(a[1], b[1]):
            if x != y:
                max_rel = max(max_rel, abs(x - y) / max(abs(x), abs(y)))
    res.update(two_stage=two, id_mismatches=ids_bad, score_bit_mismatches=bits_bad,
               max_rel_diff=max_rel)
    # pass 2: latency; the scorers take turns going first, so neither always runs warmer
    lat: dict[str, list[float]] = {"native": [], "python": []}
    legs = {"native": nat, "python": py}
    nat.stage2_ms.clear()
    py.stage2_ms.clear()
    for i, q in enumerate(queries):
        for name in ("native", "python") if i % 2 else ("python", "native"):
            t0 = time.perf_counter()
            legs[name]._text_ids(q, N, SCOPE, None)
            lat[name].append((time.perf_counter() - t0) * 1e3)
    for name, leg in legs.items():
        res[f"stage2_ms_{name}"] = [_pct(leg.stage2_ms, 50), _pct(leg.stage2_ms, 99)]
        res[f"text_leg_ms_{name}"] = [_pct(lat[name], 50), _pct(lat[name], 99)]
    for name, leg in legs.items():
        res[f"text_leg_qps_{name}"] = {
            str(t): _qps(lambda q, leg=leg: leg._text_ids(q, N, SCOPE, None), queries, t)
            for t in threads
        }
    # the ranked OR against the posting budget (D14): the prune arm's gate in Task 11
    budget = or_budget(py)
    counts = [or_matches(py, q) for q in queries]
    res.update(or_budget=budget, or_over_budget=sum(c > budget for c in counts),
               or_matches_underscore=[c for q, c in zip(queries, counts) if "_" in q])
    return res


def passed(res: dict) -> bool:
    return (res["id_mismatches"] == res["score_bit_mismatches"] == 0
            and res["avgdl_native"] == res["avgdl_python"])


def report(res: dict) -> str:
    return "\n".join([
        f"parity: {res['queries']} queries ({res['two_stage']} two-stage): id-order mismatches"
        f" {res['id_mismatches']}, score bit-mismatches {res['score_bit_mismatches']},"
        f" max relative diff {res['max_rel_diff']:.3g}",
        f"avgdl: native {res['avgdl_native']!r} python {res['avgdl_python']!r}",
        f"stage 2 p50/p99 ms: native {res['stage2_ms_native']} python {res['stage2_ms_python']}",
        f"text leg p50/p99 ms: native {res['text_leg_ms_native']} python {res['text_leg_ms_python']}",
        f"text leg q/s by threads: native {res['text_leg_qps_native']} python {res['text_leg_qps_python']}",
        f"ranked OR: budget {res['or_budget']} rows, queries over it {res['or_over_budget']},"
        f" underscore queries' matches {res['or_matches_underscore']}",
        "PASS" if passed(res) else "FAIL",
    ])


def _pass_stats(bench, lat, wall, hits, paths, want) -> dict:
    return {
        "p50": round(bench.pct(lat, 50), 3), "p95": round(bench.pct(lat, 95), 3),
        "p99": round(bench.pct(lat, 99), 3), "first10_max": round(max(lat[:10]), 3),
        "qps": round(len(lat) / wall, 1),
        # bench.py's hybrid text-hit: did the doc the query text came from reach the top 10?
        "text_hit": sum(any(paths[int(h[1:])] == w for h in hs) for hs, w in zip(hits, want)) / len(want),
    }


async def _passes(bench, queries, texts, paths, want, n_passes: int, concurrency: int) -> dict:
    res: dict = {"queries": len(texts), "passes": []}
    for _ in range(n_passes):
        lat, wall, hits = await bench.run_queries("raggio", queries, 1, bench.TR_HDRS, texts=texts)
        res["passes"].append(_pass_stats(bench, lat, wall, hits, paths, want))
    res["top10"] = hits  # the last serial pass: the arms' rankings, compared query by query
    lat, wall, _ = await bench.run_queries("raggio", queries, concurrency, bench.TR_HDRS, texts=texts)
    res["concurrent"] = {"qps": round(len(lat) / wall, 1), "p50": round(bench.pct(lat, 50), 3),
                         "p99": round(bench.pct(lat, 99), 3)}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stage-2 BM25 native vs Python on a real collection")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queries", help="write bench.py's hybrid query texts as JSON (host)")
    q.add_argument("out")
    q.add_argument("--limit", type=int, default=2549619)
    q.add_argument("--queries", type=int, default=500)
    q.add_argument("--seed", type=int, default=42)
    p = sub.add_parser("parity", help="native vs Python text leg over a meta.db (in the image)")
    p.add_argument("meta_db")
    p.add_argument("queries_json")
    p.add_argument("--threads", default="1,2,4,8")
    s = sub.add_parser("passes", help="bench.py's hybrid queries against the running server (host)")
    s.add_argument("out")
    s.add_argument("--limit", type=int, default=2549619)
    s.add_argument("--queries", type=int, default=500)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--passes", type=int, default=3)
    s.add_argument("--concurrency", type=int, default=8)
    a = ap.parse_args(argv)
    if a.cmd == "queries":
        texts = hybrid_inputs(a.limit, a.queries, a.seed)[2]
        Path(a.out).write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
        print(f"{len(texts)} query texts -> {a.out}")
        return 0
    if a.cmd == "passes":
        if a.passes < 1:
            ap.error("--passes must be at least 1")
        bench, qvecs, texts, paths, want = hybrid_inputs(a.limit, a.queries, a.seed)
        res = asyncio.run(_passes(bench, qvecs, texts, paths, want, a.passes, a.concurrency))
        Path(a.out).write_text(json.dumps(res), encoding="utf-8")
        for i, st in enumerate(res["passes"], 1):
            print(f"serial pass {i}: p50/p95/p99 {st['p50']}/{st['p95']}/{st['p99']} ms, first 10 max"
                  f" {st['first10_max']} ms, {st['qps']} q/s, text-hit {st['text_hit']:.3f}")
        print(f"concurrent x{a.concurrency}: {res['concurrent']} -> {a.out}")
        return 0
    queries = json.loads(Path(a.queries_json).read_text(encoding="utf-8"))
    res = parity(a.meta_db, queries, tuple(int(t) for t in a.threads.split(",")))
    print(report(res))
    print(json.dumps(res))
    return 0 if passed(res) else 1


if __name__ == "__main__":
    sys.exit(main())
