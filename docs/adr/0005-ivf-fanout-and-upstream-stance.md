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
| `IVF_SEARCH_THREADS` env var, default `min(12, os.cpu_count())`; `1` = serial | A deployment knob, not an algorithm constant. 12 is p3's best pool size on the 20-core DGX: chosen on seed 7 and held on seed 42, with kernel throughput flat from 12 threads up (19.2k–19.6k calls/s). The earlier draft default, capped at 32 threads, would resolve to 20 there, and 20 threads lose 14–17 %. `1` never starts a thread and is the escape hatch: the pre-0005 code path, bit-for-bit for distinct allowlists (every caller's) |
| Pool owned by the `Collection`: built on the first multi-shard search, closed at the end of `stop()` | Never the asyncio default executor: searches already run inside it (`asyncio.to_thread`), and waiting on it from there can deadlock. Flat collections never start threads. A search orphaned by a cancelled request that outlives `stop()` falls back to the serial loop instead of raising |
| **The 32,768-row pooled-path cliff** is documented and reported, not engineered around | From 32,768 rows (1,024 blocks) up, a one-query shard search leaves the inline path for turbovec's pooled one. All fan-out threads then share one rayon pool, and the fan-out ceiling falls to about 2.45× (p3: 1,436 → 3,525 calls/s on 40k-row shards). Results stay identical (tested with 35k-row shards and 4 concurrent callers). The default `nlist` aims at about 8k rows per shard, but k-means lists are uneven: at nlist 256 on the 2.55M DGX corpus the largest list is 30,443 rows, 93 % of the cliff. `bench/ivf_fanout_probe.py` prints the largest list on every run. Growing collections may need a larger `nlist` to stay under the cliff; that is a future, measured change. **Never set `RAYON_NUM_THREADS=1`**: it flattens the pooled path to about 1,440 calls/s from 2 threads on |
| Audience batching (one call per shard carrying every query that probes it) is **not shipped** (spec D3, settled by p3) | Grouping never beat plain one-query fan-out beyond noise on aarch64: best grouped 570.6 vs 610.4 QPS at 12 threads (640.7 vs 661.7 with `OPENBLAS_NUM_THREADS=1`), and grouped without threads is −39 % against serial. The mean audience at nq=8 and nprobe 16 of 256 is 1.40 queries, where 2–3-query calls cost 2–5× per query (on NEON they share nothing, turbovec/src/search.rs:3717-3760, and each runs on turbovec's pool, whose cost depends on which core runs it; see the NEON note under Verification). `bench/ivf_fanout_probe.py fanout` keeps the grouped arm only as a regression guard |
| **Shard-side allowlist intersection**: binary-search the smaller side into the larger; allowlists are sorted once per cache miss | 255k-id filter, 16 shards of 2.55M rows (laptop): 94 → 5 ms per query; 765k ids: 345 → 8 ms. DGX (p3): the intersect falls from 29.8 to 2.0 ms per query |
| **Each filtered (query, shard) task cuts its own allowlist slice** on its pool thread; only tiny allowlists (≤128 ids) find their owning shards on the caller | p3, filtered p50 in-process: 3.7 ms (2.2 ms with `OPENBLAS_NUM_THREADS=1`) cut inside the tasks, against 6.4–7.0 ms (3.9–4.4 ms) cut up front on the caller. Filtered searches are nq=1, so a caller-side cut shared across queries bought nothing. Results are bit-identical to serial, including tie order |
| **Generation counters** on `_allow_cache` (per collection) and on the IVF shard-id cache (per shard) | A search whose request was cancelled keeps running without a read lock. If a write lands mid-scan, its older snapshot may serve that search once but is never cached |
| **Dirty-shard sync**: `_IvfIndex.sync()` writes only shards written since their last sync, plus any shard file the target directory lacks, and returns the count; `dirty_shards` exposes the set | A one-row job used to rewrite all 256 shard files; now it writes one. Plan D2's batched sync builds on this API |

## Upstream stance (partly supersedes ADR 0001:48–49)

ADR 0001:48 put turbovec kernel changes (SIMD, ANN, multi-partition search) out of scope.
For **code** that still holds: raggio never forks turbovec and carries no turbovec kernel
code. What changes:

- **Upstream proposals are in scope.** Short, measured, plain-English issue drafts live
  outside the repo (one `turbovec-*.md` per proposal). raggio's maintainer
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

DGX A/B, 2026-10-02: gn100 (GB10, 20 aarch64 cores). Base `fa0fa9f` (the preceding main) vs
Plan F `fd98580` at the default `IVF_SEARCH_THREADS` (12 on gn100) and at `IVF_SEARCH_THREADS=1`.
arXiv 2,549,119 × 1024, seed 42, 500 queries, concurrency 8 (the concurrent phase repeats
the same 500 queries, so its caches are warm). Host-warm, `--memory 4g`, and
`OPENBLAS_NUM_THREADS=1` in every arm (spec §7 F, D16). Every run is a fresh container, so
the bands include between-server variance: a discarded run0, then 3 interleaved rounds per
arm (median, band = max − min; spec §6). The row-label table gives each arm's
`sqlite3.sqlite_version` (D15). The raw results are kept outside the repo.

### IVF A/B (seed 42, 2,549,119 x 1024, nlist 256, nprobe 16, c=8, host-warm, --memory 4g, OPENBLAS_NUM_THREADS=1 in every arm)

| Metric | base (preceding main): median (band, n) | F, default threads: median (band, n) | F, IVF_SEARCH_THREADS=1: median (band, n) |
|---|---|---|---|
| QPS concurrent | 203.1 (10.8, 3) | 342.1 (27.7, 3) | 183.1 (10.2, 3) |
| Filtered p50 (ms) | 49.0 (1.5, 3) | 6.2 (0.1, 3) | 8.5 (1.7, 3) |
| Search p50 (ms) | 8.8 (0.2, 3) | 6.2 (0.2, 3) | 7.7 (1.0, 3) |
| QPS serial | 112.1 (2.4, 3) | 159.6 (6.3, 3) | 127.7 (15.8, 3) |
| p95 under concurrency (ms) | 43.7 (5.2, 3) | 28.6 (2.0, 3) | 50.9 (8.3, 3) |
| Filtered p95 (ms) | 51.2 (2.1, 3) | 7.4 (0.1, 3) | 10.3 (1.7, 3) |
| Recall@10 | 0.995 (0.000, 3) | 0.995 (0.000, 3) | 0.995 (0.000, 3) |
| Hybrid p50 (ms) | 46.2 (0.4, 3) | 46.5 (1.3, 3) | 45.9 (1.3, 3) |
| Memory under load, one sample (MB) | 1510 (1, 3) | 1512 (2, 3) | 1510 (2, 3) |
| Memory peak before the restart, cgroup (MB) | 1530 (2566, 3) | 1529 (35, 3) | 1530 (2, 3) |
| Memory peak after bench's restart, cgroup (MB) | 1529 (1, 3) | 1529 (1, 3) | 1528 (1, 3) |
| CPU per query at c=8 (ms) | 5.19 (0.32, 3) | 10.09 (0.65, 3) | 5.81 (0.57, 3) |

### Row labels (spec §6: regime, cap, memory_swap, sqlite3.sqlite_version, OPENBLAS_NUM_THREADS, index)

| Row label | base (preceding main) | F, default threads | F, IVF_SEARCH_THREADS=1 |
|---|---|---|---|
| regime | host-warm | host-warm | host-warm |
| cap | 4g | 4g | 4g |
| memory_swap | host-default | host-default | host-default |
| sqlite_version | 3.53.1 | 3.53.1 | 3.53.1 |
| openblas_num_threads | 1 | 1 | 1 |
| index | {"nlist": 256, "nprobe": 16, "type": "ivf"} | {"nlist": 256, "nprobe": 16, "type": "ivf"} | {"nlist": 256, "nprobe": 16, "type": "ivf"} |

### Seed-7 pool-size sweep and grouped regression guard

`bench/ivf_fanout_probe.py fanout` (seed 7): serial r1/r2 28.82/29.75 ms; chosen pool size 12 (default here 12); GUARD grouped vs plain at 12: REVISIT

### Cores busy at c=16 (record-only, not a gate)

`planF-cpu.py` after each measured base and F run: 1,500 vector searches at c=16 after a discarded 200, with CPU from the container cgroup's `cpu.stat` and from per-thread `/proc/<pid>/task/*/stat` deltas. The pre-F expectation is p4's: 1.1–1.25 cores busy at 19a8a2e, bound by the single `_drain_scans` pipeline.

| Metric | base (preceding main): median [min..max], n | F, default threads: median [min..max], n |
|---|---|---|
| QPS at c=16 | 208.9 [192.6..217.5], 3 | 408.0 [382.3..416.8], 3 |
| CPU per query, cgroup cpu.stat (ms) | 5.34 [5.12..5.77], 3 | 10.23 [10.20..10.38], 3 |
| Cores busy, cgroup | 1.11 [1.11..1.12], 3 | 4.18 [3.97..4.25], 3 |
| Cores busy, sum of server threads | 1.10 [1.10..1.10], 3 | 4.15 [3.93..4.22], 3 |
| Main thread, event loop (cores) | 0.19 [0.18..0.20], 3 | 0.38 [0.35..0.42], 3 |
| Server threads above 0.05 cores | 3 [3..3], 3 | 15 [15..15], 3 |
| Driver (cores) | 0.14 [0.14..0.23], 3 | 0.40 [0.37..0.42], 3 |

### §7 F acceptance

- PASS: F1 recall@10 unchanged at the default nprobe: [0.995] [0.993]; EXACT seed 42 T=12 vs IVF_SEARCH_THREADS=1: unfiltered 63/63 batches, filtered 500/500, tiny 500/500 (year='2024', 212331 and 100 ids; ids, float32 score bits and tie order): PASS
- PASS: F2 concurrent QPS (c=8, medians of 3 and 3 runs): F 342.1 [316.2..344.0] vs base 203.1 [198.3..209.1], 1.68x; needs >= 1.5x and F's min above base's max [p3: 324.7 [308.6..381.4] vs 189.2 [162.8..207.3]]
- PASS: F3 filtered p50 6.16 ms, needs <= 10 (base 48.98) [p3: 6.41 vs 36.6; base expected about 49 in restarted containers (plan C's MALLOC_TRIM_THRESHOLD_)]
- PASS: F4 serial p50 better beyond the band: 6.21 vs 8.84: gain +2.63, band 0.18 ms [p3: 6.50 vs 8.44]
- PASS: F5 container memory peak (cgroup memory.peak, median) 1529 vs base 1530 MB, delta -1.0 MB; fails only above +50 MB (one-sided, spec §3.1 F1); F's peak is 1.0 MB lower, reported [p3: 1.82 GB in both]
- FAIL: F6 IVF_SEARCH_THREADS=1 reproduces the serial path within the band: serial p50 7.66 vs 8.84: diff -1.19, band 1.05; concurrent QPS 183.15 vs 203.08: diff -19.94, band 10.77
- PASS: F7 largest IVF list 24,123 rows, 74% of the 32,768-row pooled-path cliff [30,443]
- PASS: labels: every measured IVF run is host-warm, cap 4g, swap host-default, OPENBLAS_NUM_THREADS=1, index {"nlist": 256, "nprobe": 16, "type": "ivf"}, one sqlite_version per arm (base 3.53.1; f 3.53.1; f1 3.53.1)
- OVERRIDE: F6, recorded as failed under the maintainer's standing ruling on failed gates (first given for plan D1's ivf256 gate, 2026-10-01); nothing was re-run to replace it. For unfiltered searches the `IVF_SEARCH_THREADS=1` path makes base's per-(query, shard) turbovec calls in base's order. In this A/B the T=1 arm always ran right after F's c=16 capture. A follow-up on gn100 the same day (base against T=1 only, a discarded run0, then 4 rounds in ABBA order, no c=16 captures) did not reproduce the gap: its medians put T=1 within 0.1 ms and 10 QPS of base, ahead on both (serial p50 8.53 [8.34..11.72] against 8.62 [8.53..8.91] ms, concurrent QPS 197.5 [163.4..204.0] against 188.2 [178.0..199.4]). But the A/B's two fast T=1 serial runs (7.65 and 7.66 ms) were faster than any follow-up run, and its slowest T=1 QPS run (175.3) was below every follow-up base run. The gap is consistent with variance between fresh containers on gn100, which the follow-up did not test; four runs per arm, in a different order and without the c=16 captures, cannot rule out a smaller difference.

Filtered p50: base measures 48.98 ms in restarted containers (plan C's
`MALLOC_TRIM_THRESHOLD_`), and F brings it to 6.16 ms.

Memory peak before the restart: base run 1, the first measured container after the
discarded run0, reached 4,096 MB, the cap (run0 did too). Every other measured run peaked
between 1,527 and 1,562 MB, so the medians are unaffected; base's 2,566 MB band is that one
run. It was not investigated.

Batching guard: **REVISIT** at T=12: grouped beat plain beyond its run1/run2 band on seed 7. Nothing ships on it; it is reported to the maintainer as a reason to reopen spec D3. The default pool on gn100 (T=12) is the smallest within noise of the fastest on seed 7.

After F, the server limit is the HTTP/event-loop pipeline, about 3.1 ms per request, against
an in-process ceiling of 610–662 QPS (spec §7 F, p3). The follow-ups (two `_drain_scans`
batches in flight, `_hydrate` off the event loop, a cheaper parse of the vector body) are
outside this ADR. Concurrent numbers also carry SQLite's global memstatus malloc mutex (p2,
D15), so arms are compared only at the same `sqlite_version`.

### Seed-42 exactness and the largest IVF list (`bench/ivf_fanout_probe.py exact`, in-process)

```text
# rows: host-warm, uncapped (in-process on the host), sqlite_version 3.53.1, OPENBLAS_NUM_THREADS=1
loaded bench: 2549119 rows, nlist 256, nprobe 16, dim 1024, 4-bit, 1.0s
LISTS largest 24123 rows, 74% of the 32,768-row pooled-path cliff
EXACT seed 42 T=12 vs IVF_SEARCH_THREADS=1: unfiltered 63/63 batches, filtered 500/500, tiny 500/500 (year='2024', 212331 and 100 ids; ids, float32 score bits and tie order): PASS
```

### Stage attribution (`bench/ivf_fanout_probe.py stages`, seed 7, in-process)

```text
# rows: host-warm, uncapped (in-process on the host), sqlite_version 3.53.1, OPENBLAS_NUM_THREADS=1
loaded bench: 2549119 rows, nlist 256, nprobe 16, dim 1024, 4-bit, 1.1s
LISTS largest 24123 rows, 74% of the 32,768-row pooled-path cliff
filter year='2024': 212331 ids; allowlist SELECT+sort 828 ms, shard-id cache fill 184 ms (each once per cache miss, not in the rows below)
path       nq   T    parse    route    index   kernel   unpack  rescore  hydrate   encode    total  calls    other   (ms, median of 40)
vector      1   1     0.17     0.02     2.88     2.77     0.01     2.63     0.06     0.09     5.87     16     2.99
vector      1  12     0.19     0.04     2.14     5.11     0.02     0.32     0.07     0.10     2.78     16     0.63
vector      8   1     1.29     0.12    27.84    27.34     0.06    20.05     0.43     0.65    50.65    128    22.81
vector      8  12     1.30     0.12     7.47    63.46     0.07     1.87     0.44     0.65    11.80    128     4.32
filtered    1   1     0.17     0.02     2.70     1.05     0.01     1.86     0.05     0.09     4.82     16     2.12
filtered    1  12     0.18     0.03     2.05     2.56     0.01     0.23     0.06     0.10     2.64     16     0.59
```

### Pool-size sweep and grouped regression guard (`bench/ivf_fanout_probe.py fanout`, seed 7)

```text
# rows: host-warm, uncapped (in-process on the host), sqlite_version 3.53.1, OPENBLAS_NUM_THREADS=1
loaded bench: 2549119 rows, nlist 256, nprobe 16, dim 1024, 4-bit, 1.0s
LISTS largest 24123 rows, 74% of the 32,768-row pooled-path cliff
nq=8 nprobe=16 k=50: 128 plain calls/batch, 53.5 grouped calls/batch; audience mean 2.39, histogram (size 1..) [986, 424, 242, 179, 131, 91, 61, 27]
  T    plain r1/r2 ms   grouped r1/r2 ms  plain x  grp x  verdict
  1    29.49/   29.58    37.75/   45.03     0.98   0.71  plain  (plain==serial: True; grouped same top-k 40/40)
  2    20.29/   20.50    25.20/   24.34     1.41   0.82  plain  (plain==serial: True; grouped same top-k 40/40)
  4    12.05/   16.91    13.46/   13.99     1.99   1.05  plain  (plain==serial: True; grouped same top-k 40/40)
  8     8.56/    7.89     7.81/    8.51     3.50   1.01  plain  (plain==serial: True; grouped same top-k 40/40)
 12     7.42/    7.57     6.74/    7.12     3.84   1.08  GROUPED WINS  (plain==serial: True; grouped same top-k 40/40)
 16     7.01/    7.55     7.28/    7.49     3.96   0.99  plain  (plain==serial: True; grouped same top-k 40/40)
 20     7.49/    7.68     7.17/    7.69     3.80   1.02  plain  (plain==serial: True; grouped same top-k 40/40)
serial r1/r2 28.82/29.75 ms; chosen pool size 12 (default here 12); GUARD grouped vs plain at 12: REVISIT
```

### turbovec small-batch repro on GB10 (`neon_small_nq_repro.py`)

```text
turbovec 1.0.0, aarch64, 3.12.14, RAYON_NUM_THREADS=1; IdMapIndex 8192 x 1024, 4-bit, k=50, median of 300
 nq   us/call  us/query  vs nq=1
  1     120.0     120.0     1.00
  2     556.7     278.3     2.32
  3     768.9     256.3     2.14
  4     797.6     199.4     1.66
  5     785.2     157.0     1.31
  8     982.4     122.8     1.02
 12    1408.9     117.4     0.98
per-query cost vs nq=1: nq=2 2.32, nq=3 2.14, nq=4 1.66. Measured on GB10 NEON: nq=2 2.05, nq=3 4.89 (no sharing); on x86 AVX2 all below 0.7.
```

The output's last sentence is the repro's built-in quote of p3's 8,192-row run; this run's figures
are the first sentence. The same day, pinned to one Cortex-X925 core (`taskset -c 5`), the repro
printed nq=2 1.02, nq=3 1.01, nq=4 0.65: the 2-3-query tail shares no work. The rest of the
unpinned cost depends on which core runs the search. Every multi-query search runs on turbovec's
pool, while a one-query search on a part under 32,768 rows runs inline on the caller
(turbovec-python/src/lib.rs:1211). With the caller pinned to core 5 and the pool's one worker
pinned on its own, nq=2 cost 1.01–1.02 per query with the worker on the same core, 1.48–1.49 on
another X925 in the same L3, 1.69–2.80 on an X925 in the other L3, and 3.74–5.14 on a
Cortex-A725, which runs this kernel about 3.2× slower (two runs each). With the worker on the
caller's core the handoff adds at most about 4 µs per call, which this data cannot separate from
the tail's own cost. The repro and its upstream draft were updated to say so.
