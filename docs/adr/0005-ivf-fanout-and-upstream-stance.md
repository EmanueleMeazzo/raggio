# ADR 0005 — IVF shard fan-out, and the turbovec upstream stance

Status: accepted · Date: 2026-10-02 · Partly supersedes ADR 0001:48–49; corrects ADR 0001:30 and ADR 0002:7-9

## Context

The 2026-09-28 DGX baseline (main @19a8a2e, 2,549,119 × 1024 arXiv vectors, 4 GiB cap,
`docs/superpowers/research/2026-09-28-dgx-baseline.md`) had the optional IVF index
(nlist 256, nprobe 16, recall@10 0.994) scan 1/16 of the flat scan's bytes, yet reach
only 166-214 QPS at concurrency 8 against flat's 116.5-116.9 (a 16% band between warm
IVF runs; the published run had IVF *behind* flat, 116 vs 129). Filtered IVF search was
slower than flat outright: p50 43.7-70.5 ms vs 25.2-28.0 ms.

Reading turbovec 1.0.0 and raggio together
(`docs/superpowers/research/2026-09-28-item3a-turbovec-internals.md`,
`docs/superpowers/research/2026-09-28-item3b-upstream-landscape.md`) found two things:

1. **The "~0.4 ms fixed cost per shard search call" (ADR 0001:30, ADR 0002:7-9) was a
   misread.** An `IdMapIndex` search of one query on a shard under 32,768 rows (1,024
   blocks, `SINGLE_QUERY_PARALLEL_MIN_BLOCKS`, turbovec/src/search.rs:30) runs inline on
   the calling thread with the GIL released (`py.detach`, turbovec-python/src/lib.rs:1177;
   `with_pool_if`, lib.rs:1211 and 1863). The extension pins rayon's global pool to a
   one-thread sentinel (lib.rs:1879, called at 2153-2154), so no other thread helps. Such a
   call costs the shard's bytes at single-core speed. On the DGX, ADR 0002's own numbers
   give 5.1 ms / 16 probes = 0.32 ms per call *including* 6.6 MB of codes, and nprobe 8
   vs 16 (2.7 vs 5.1 ms) has an intercept of about zero. IVF read 6.25% of the bytes at
   about 1/4.5 of the flat scan's bandwidth (20.7 vs 94 GB/s): one core against all 20.
2. **The serial loop was the bottleneck.** `_IvfIndex.search` ran every (query, shard)
   call one after another on one thread: 128 calls per 8-query micro-batch at nprobe 16,
   while the flat scan runs one 8-query pass on every core. ADR 0001:49 ("application-level
   paths are exhausted") never tried running those calls in parallel.

Filtered IVF search had a serial cost of its own: `_intersect` binary-searched every
allowlist id into each probed shard's ids, O(|allow| log |shard|) per shard. For a
255k-id filter over 16 probed shards of 2.55M rows that was 94 ms per query (laptop).

DGX probe p3 (`docs/superpowers/research/2026-09-28-dgx-probe-p3-ivf-fanout.md`)
measured a prototype of these decisions on the same corpus. In-process, fan-out on 12
threads reached 610.4 QPS (661.7 with `OPENBLAS_NUM_THREADS=1`). At the HTTP server (c=8,
4 GiB cap, warm, `OPENBLAS_NUM_THREADS=1` in both arms), concurrent QPS rose from 189.2
[162.8..207.3] to 324.7 [308.6..381.4], filtered p50 fell from 36.6 to 6.41 ms, and serial
p50 fell from 8.44 to 6.50 ms. The memory peak was 1.82 GB in both arms.

DGX probe p4 (`docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`)
measured the server before this change: 19a8a2e, IVF vector at c=16, 4 GiB cap,
`OPENBLAS_NUM_THREADS=1`. Only 1.1–1.25 cores were busy (per-thread CPU from `top -H`:
about 1.1 on Debian CPython 3.12, 1.25 on free-threaded 3.14t), and no thread was
saturated. Splitting the client in two did not raise aggregate QPS beyond trial-to-trial
spread. So concurrent IVF was bound by the single `_drain_scans` batch pipeline, whose
shard calls ran one after another, not by the client and not by the GIL: 3.14t's
+14–31 % QPS at c=8 and c=16 sits on top of that pipeline. This is the pre-fan-out
expectation. The DGX A/B records cores busy at c=16 in both arms, as a record and not as
a gate.

## Decisions

| Decision | Rationale |
|---|---|
| **Fan out every (query, shard) search as its own one-query task** on a per-collection thread pool (`_ShardPool` in `store.py`) | Each task runs inline and GIL-free inside turbovec, so T threads scan T shards at once. Laptop (6 CPUs; 262,144 × 1024, nlist 32, nprobe 16, 8-query batches): 118 ms serial → 60 ms at T=2, 31 ms at T=6 (3.75x) |
| Merge in the serial loop's order | Tasks are built in the old loop's order and results are consumed in that order, so every pool size returns the serial result bit-for-bit, tie order included (`tests/test_ivf_fanout.py`) |
| `IVF_SEARCH_THREADS` env var, default `min(12, os.cpu_count())`; `1` = serial | A deployment knob, not an algorithm constant. 12 is p3's best pool size on the 20-core DGX: chosen on seed 7 and held on seed 42, with kernel throughput flat from 12 threads up (19.2k–19.6k calls/s). The earlier draft default, capped at 32 threads, would resolve to 20 there, and 20 threads lose 14–17 %. `1` never starts a thread and is the pre-0005 code path bit-for-bit: the escape hatch |
| Pool owned by the `Collection`: built on the first multi-shard search, closed at the end of `stop()` | Never the asyncio default executor: searches already run inside it (`asyncio.to_thread`), and waiting on it from there can deadlock. Flat collections never start threads. A search orphaned by a cancelled request that outlives `stop()` falls back to the serial loop instead of raising |
| **The 32,768-row pooled-path cliff** is documented and reported, not engineered around | From 32,768 rows (1,024 blocks) up, a one-query shard search leaves the inline path for turbovec's pooled one. All fan-out threads then share one rayon pool, and the fan-out ceiling falls to about 2.45× (p3: 1,436 → 3,525 calls/s on 40k-row shards). Results stay identical (tested with 35k-row shards and 4 concurrent callers). The default `nlist` aims at about 8k rows per shard, but k-means lists are uneven: at nlist 256 on the 2.55M DGX corpus the largest list is 30,443 rows, 93 % of the cliff. `bench/ivf_fanout_probe.py` prints the largest list on every run. Growing collections may need a larger `nlist` to stay under the cliff; that is a future, measured change. **Never set `RAYON_NUM_THREADS=1`**: it flattens the pooled path to about 1,440 calls/s from 2 threads on |
| Audience batching (one call per shard carrying every query that probes it) is **not shipped** (spec D3, settled by p3) | Grouping never beat plain one-query fan-out beyond noise on aarch64: best grouped 570.6 vs 610.4 QPS at 12 threads (640.7 vs 661.7 with `OPENBLAS_NUM_THREADS=1`), and grouped without threads is −39 % against serial. The mean audience at nq=8 and nprobe 16 of 256 is 1.40 queries, where 2–3-query calls cost 2–5× per query (on NEON they share nothing, turbovec/src/search.rs:3717-3760, and each pays a ~70 µs pool install, lib.rs:224-234). `bench/ivf_fanout_probe.py fanout` keeps the grouped arm only as a regression guard |
| **Shard-side allowlist intersection**: binary-search the smaller side into the larger; allowlists are sorted once per cache miss | 255k-id filter, 16 shards of 2.55M rows (laptop): 94 → 5 ms per query; 765k ids: 345 → 8 ms. DGX (p3): the intersect falls from 29.8 to 2.0 ms per query |
| **Each filtered (query, shard) task cuts its own allowlist slice** on its pool thread; only tiny allowlists (≤128 ids) find their owning shards on the caller | p3, filtered p50 in-process: 3.7 ms (2.2 ms with `OPENBLAS_NUM_THREADS=1`) cut inside the tasks, against 6.4–7.0 ms (3.9–4.4 ms) cut up front on the caller. Filtered searches are nq=1, so a caller-side cut shared across queries bought nothing. Results are bit-identical to serial, including tie order |
| **Generation counters** on `_allow_cache` (per collection) and on the IVF shard-id cache (per shard) | A search whose request was cancelled keeps running without a read lock. If a write lands mid-scan, its older snapshot may serve that search once but is never cached |
| **Dirty-shard sync**: `_IvfIndex.sync()` writes only shards written since their last sync, plus any shard file the target directory lacks, and returns the count; `dirty_shards` exposes the set | A one-row job used to rewrite all 256 shard files; now it writes one. Plan D2's batched sync builds on this API |

## Upstream stance (partly supersedes ADR 0001:48–49)

ADR 0001:48 put turbovec kernel changes (SIMD, ANN, multi-partition search) out of scope.
For **code** that still holds: raggio never forks turbovec and carries no turbovec kernel
code. What changes:

- **Upstream proposals are in scope.** Short, measured, plain-English issue drafts live
  outside the repo (`D:/DEV/_scratch/upstream-drafts/turbovec-*.md`). raggio's maintainer
  posts them personally, if at all. Each draft names a raggio-side acceptance test, so an
  upstream change is adopted only when it measurably deletes raggio code or beats this
  ADR's fan-out: `search_many` (one call for many shards), `ids()`, an allowlist intersect
  mode, native partitioned/IVF search (building on the maintainer's `feat/ivf-residual`
  branch, 1452b6e), cp314t wheels, and the NEON 2–3-query tail.
- ADR 0001:49's "application-level paths are exhausted" is withdrawn: parallel shard
  fan-out was one. (ADR 0004 records the first-party native BM25 exception to 0001:48.)

## Consequences

- Up to `IVF_SEARCH_THREADS` threads per resident indexed collection, so at most
  `MAX_RESIDENT_COLLECTIONS` pools. Idle threads cost only their stacks.
- `os.cpu_count()` ignores cgroup CPU quotas: containers started with `--cpus` should set
  `IVF_SEARCH_THREADS` explicitly.
- ADR 0002:76-77 ("batched concurrent scans degrade to per-query probing") still describes
  the task granularity, but no longer the throughput: the probed shards now scan in parallel.
- The fan-out's server gain depends on `OPENBLAS_NUM_THREADS=1`, which is an image `ENV`
  owned by the Dockerfile change of the performance program's Plan A (spec D16), not a
  raggio setting. With numpy's default OpenBLAS, 20 busy-spinning threads burn about 16
  cores under any search load. They push query and event-loop threads onto the slow cores
  and cap concurrent IVF QPS at about 203 instead of about 325 (p3). Deployments outside
  the image should set it too.
- Never set `RAYON_NUM_THREADS=1` (see the cliff above).
- After the fan-out, the server's limit is the HTTP/event-loop pipeline: about 3.1 ms per
  request, against an in-process ceiling of 610–662 QPS. The follow-ups (two
  `_drain_scans` batches in flight, `_hydrate` off the loop, a cheaper vector-body parse)
  are outside this ADR.

## Verification

Pending: Plan F's DGX A/B appends the results here (base vs F default vs
`IVF_SEARCH_THREADS=1`, seed 42, spec §6 noise bands, spec §7 F items 1–7), the stage
attribution from `bench/ivf_fanout_probe.py stages`, the seed-7 pool-size sweep with the
grouped regression guard, the seed-42 exactness check, and the largest IVF list size.
