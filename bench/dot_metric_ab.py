"""A/B probe for ADR 0020: does the `metric` option change what a default (cosine)
collection returns, or what a search on it costs?

In-process, no HTTP, against one data directory, with whichever raggio comes first on
PYTHONPATH (so the same script runs on the baseline source and on the branch):

    python bench/dot_metric_ab.py build DATA_DIR            # once, with the baseline source
    python bench/dot_metric_ab.py build-dot DATA_DIR        # once, with a source that has the option
    python bench/dot_metric_ab.py run DATA_DIR OUT.json     # once per arm and round, alternating
    python bench/dot_metric_ab.py paired DATA_DIR BASE_SRC OUT.json   # BASE_SRC: the baseline's src/raggio

`build` ingests N float32 vectors of uneven length into a default collection "plain";
`build-dot` the same vectors into "exact", a collection with metric "dot". `run` times
request shapes on "plain" (and the vector shapes on "exact" when the source knows the
option) and hashes every response: both arms read the same files, so equal hashes mean
equal answers, and the timings compare the code alone.

Two kinds of case. "search" cases call Collection.search as the route does (the event
loop, the batched scan, a worker thread, hydration). "direct" cases call the two
functions the option touches, _search_rescored and _search_allowed, on the calling
thread, with no loop and no thread hand-off in the timing.

`paired` is the finer instrument. One process loads the baseline source twice (under two
package names) and the installed source once, opens "plain" with each, and runs every
direct call on all three, in a fresh random order per call. Run-to-run drift then
hits the three alike, and the comparison is the per-call difference: baseline copy two
against baseline copy one is the noise floor (A/A), installed against baseline copy one
the effect (A/B). When "exact" exists it joins as a fourth arm (installed source, the
same vectors under metric "dot", the queries as they are): what the dot path costs.
"""
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np

N, DIM, SEED = 20_000, 1024, 20261125
BATCH = 500
QUERIES, WARMUP = 300, 30
DIRECT = 1500  # calls per direct case, cycling over the queries
GROUPS = 500  # metadata "group": N / GROUPS = 40 records each, under the rescore depth of 50


def vectors() -> np.ndarray:
    rng = np.random.default_rng(SEED)
    v = rng.standard_normal((N, DIM), dtype=np.float32)
    v *= np.exp(rng.uniform(-0.7, 0.7, (N, 1))).astype(np.float32) / np.float32(np.sqrt(DIM))
    return v


def queries() -> np.ndarray:
    rng = np.random.default_rng(SEED + 1)
    return rng.standard_normal((QUERIES + WARMUP, DIM), dtype=np.float32) / np.float32(np.sqrt(DIM))


async def build(data_dir: str, name: str, **options) -> None:
    os.environ["DATA_DIR"] = data_dir
    from raggio.config import Settings
    from raggio.store import CollectionManager

    m = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
    await m.create_collection(name, DIM, 4, None, None, None, **options)
    c = await m.touch(name)
    v = vectors()
    t0 = time.perf_counter()
    for s in range(0, N, BATCH):
        docs = [{"doc_id": f"r{i}", "chunks": [{"id": f"r{i}", "vector": v[i], "metadata": {"group": i % GROUPS}}]}
                for i in range(s, min(s + BATCH, N))]
        await c._process_job({"documents": docs}, sync=False)
    await m.shutdown()  # one index sync, at stop
    print(f"built {name}: {N} x {DIM} in {time.perf_counter() - t0:.1f} s")


async def run(data_dir: str, out: str) -> None:
    os.environ["DATA_DIR"] = data_dir
    import raggio
    from raggio import store
    from raggio.config import Settings

    has_option = hasattr(store, "METRICS")
    m = store.CollectionManager(Settings(), embedder_factory=lambda cfg: None)
    q = queries()
    unit = store._normalize(q)  # what the search route hands a cosine collection
    result = {"source": str(Path(raggio.__file__).parent), "has_option": has_option, "cases": {}}

    def record(label: str, times: list, digest) -> None:
        times.sort()
        result["cases"][label] = {
            "calls": len(times),
            "p50_us": round(statistics.median(times) * 1e6, 1),
            "p90_us": round(times[int(0.9 * len(times))] * 1e6, 1),
            "mean_us": round(statistics.fmean(times) * 1e6, 1),
            "sha256": digest.hexdigest(),
        }

    async def measure(label: str, call) -> None:
        for i in range(WARMUP):
            await call(i)
        times, digest = [], hashlib.sha256()
        for i in range(WARMUP, WARMUP + QUERIES):
            t0 = time.perf_counter()
            res = await call(i)
            times.append(time.perf_counter() - t0)
            digest.update(json.dumps(res, sort_keys=True).encode())
        record(label, times, digest)

    def measure_direct(label: str, call) -> None:
        for i in range(WARMUP):
            call(i)
        times, digest = [], hashlib.sha256()
        for n in range(DIRECT):
            i = WARMUP + n % QUERIES
            t0 = time.perf_counter()
            res = call(i)
            times.append(time.perf_counter() - t0)
            digest.update(json.dumps(res).encode())
        record(label, times, digest)

    def group_ids(col) -> np.ndarray:
        with col._reading() as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM records WHERE json_extract(metadata, '$.group') = 7 AND indexed = 1")]
        return np.sort(np.array(ids, dtype=np.uint64))

    plain = await m.touch("plain")
    allow = group_ids(plain)
    assert len(allow) == N // GROUPS

    async def listing(i):
        return await asyncio.to_thread(plain.list_records, "chunks", None, None, 100, (i * 37) % (N - 100), True)

    measure_direct("plain direct: scan + rescore, k=10", lambda i: plain._search_rescored(unit[i : i + 1], [10]))
    measure_direct("plain direct: scan + rescore, k=100", lambda i: plain._search_rescored(unit[i : i + 1], [100]))
    measure_direct("plain direct: allowlist of 40, k=10", lambda i: plain._search_allowed(unit[i : i + 1], 10, allow))
    await measure("plain search: vector k=10", lambda i: plain.search("vector", unit[i : i + 1], None, 10, "chunks", None, None))
    await measure("plain search: vector k=100", lambda i: plain.search("vector", unit[i : i + 1], None, 100, "chunks", None, None))
    await measure("plain search: vector k=10, filter of 40",
                  lambda i: plain.search("vector", unit[i : i + 1], None, 10, "chunks", {"group": 7}, None))
    await measure("plain list 100 with vectors", listing)
    if has_option and m.get_config("exact") is not None:
        exact = await m.touch("exact")
        allow = group_ids(exact)
        measure_direct("exact direct: scan + rescore, k=10", lambda i: exact._search_rescored(q[i : i + 1], [10]))
        measure_direct("exact direct: scan + rescore, k=100", lambda i: exact._search_rescored(q[i : i + 1], [100]))
        measure_direct("exact direct: allowlist of 40, k=10", lambda i: exact._search_allowed(q[i : i + 1], 10, allow))
        await measure("exact search: vector k=10", lambda i: exact.search("vector", q[i : i + 1], None, 10, "chunks", None, None))
        await measure("exact search: vector k=100", lambda i: exact.search("vector", q[i : i + 1], None, 100, "chunks", None, None))
    await m.shutdown()
    Path(out).write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


def load_source(package: str, src: Path):
    """The raggio at `src` (a src/raggio directory) as a package of its own name, so two
    sources live in one process; returns its store module."""
    import importlib
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        package, src / "__init__.py", submodule_search_locations=[str(src)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    return importlib.import_module(package + ".store")


def paired(data_dir: str, base_src: str, out: str) -> None:
    import raggio.store as installed

    stores = {
        "base_a": load_source("raggio_base_a", Path(base_src)),
        "base_b": load_source("raggio_base_b", Path(base_src)),
        "new": installed,
    }
    assert not hasattr(stores["base_a"], "METRICS") and hasattr(installed, "METRICS")
    root = Path(data_dir) / "collections"
    cols = {
        arm: st.Collection(st.CollectionConfig("plain", DIM, 4, None, None, None), root / "plain", lambda: None)
        for arm, st in stores.items()
    }
    q = queries()
    unit = installed._normalize(q)
    sent = {arm: unit for arm in cols}  # a cosine collection gets the query's direction
    if (root / "exact").exists():
        # a fourth arm, for what the dot metric itself costs: the same vectors in a dot
        # collection, asked with the queries as they are
        cols["dot"] = installed.Collection(
            installed.CollectionConfig("exact", DIM, 4, None, None, None, metric="dot"), root / "exact", lambda: None)
        sent["dot"] = q
    allow = {}
    for arm, c in cols.items():
        with c._reading() as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM records WHERE json_extract(metadata, '$.group') = 7 AND indexed = 1")]
        allow[arm] = np.sort(np.array(ids, dtype=np.uint64))
    cases = {
        "scan + rescore, k=10": lambda a, i: cols[a]._search_rescored(sent[a][i : i + 1], [10]),
        "scan + rescore, k=100": lambda a, i: cols[a]._search_rescored(sent[a][i : i + 1], [100]),
        "allowlist of 40, k=10": lambda a, i: cols[a]._search_allowed(sent[a][i : i + 1], 10, allow[a]),
    }
    arms = list(cols)
    rng = np.random.default_rng(SEED + 2)
    order = np.random.default_rng(SEED + 3)
    result = {"sources": {arm: str(Path(st.__file__).parent) for arm, st in stores.items()},
              "calls": DIRECT, "cases": {}}
    for label, call in cases.items():
        for arm in arms:
            for i in range(WARMUP):
                call(arm, i)
        times = {arm: np.empty(DIRECT) for arm in arms}
        same = True
        for n in range(DIRECT):
            i = WARMUP + n % QUERIES
            answers = {}
            # a fresh random order per call: a fixed rotation would always run the same
            # arm after the dot arm, whose larger reads leave it colder caches
            for arm in order.permutation(arms):
                t0 = time.perf_counter()
                answers[arm] = call(arm, i)
                times[arm][n] = time.perf_counter() - t0
            same = same and answers["base_a"] == answers["base_b"] == answers["new"]

        def difference(arm: str) -> dict:
            d = (times[arm] - times["base_a"]) * 1e6
            boot = np.median(rng.choice(d, (2000, len(d))), axis=1)
            base = float(np.median(times["base_a"]) * 1e6)
            return {
                "median_us": round(float(np.median(d)), 2),
                "ci95_us": [round(float(x), 2) for x in np.percentile(boot, [2.5, 97.5])],
                "median_pct_of_base_p50": round(100 * float(np.median(d)) / base, 2),
                "iqr_us": [round(float(x), 1) for x in np.percentile(d, [25, 75])],
            }

        result["cases"][label] = {
            "p50_us": {arm: round(float(np.median(times[arm]) * 1e6), 1) for arm in arms},
            "mean_us": {arm: round(float(times[arm].mean() * 1e6), 1) for arm in arms},
            "same_answers": same,
            "aa_base_b_minus_base_a": difference("base_b"),
            "ab_new_minus_base_a": difference("new"),
        }
        if "dot" in cols:
            result["cases"][label]["dot_minus_base_a"] = difference("dot")
    for c in cols.values():
        # not stop(): it syncs the index file, and turbovec refuses a second writer on
        # one file. Nothing was written here, so closing the connections is the whole
        # teardown
        c._closed = True
        c._close_conns()
    Path(out).write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    op = sys.argv[1]
    if op == "build":
        asyncio.run(build(sys.argv[2], "plain"))
    elif op == "build-dot":
        asyncio.run(build(sys.argv[2], "exact", metric="dot"))
    elif op == "run":
        asyncio.run(run(sys.argv[2], sys.argv[3]))
    elif op == "paired":
        paired(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        raise SystemExit(__doc__)
