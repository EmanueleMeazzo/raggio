# item3a-turbovec-internals

## Summary
The "~0.4 ms fixed cost per shard search call" is mostly not a fixed cost. A turbovec 1.0.0 IdMapIndex nq=1 search on a small shard runs inline on the calling Python thread and does not use the rayon pool (turbovec-python/src/lib.rs:1211, 1863). Its cost is the bytes the shard holds, scanned at single-core speed. The DGX's own IVF numbers in ADR 0002 work out to about 20.7 GB/s for IVF, against about 93 GB/s for the flat scan. At 16 probes per query, 5.1 ms / 16 = 0.32 ms per call, and that already includes the bytes, so a 0.4 ms fixed cost is impossible there.

On arXiv, raggio's _IvfIndex.search (src/raggio/store.py:485-530) makes nq x nprobe serial nq=1 calls: 128 calls, 653 MB, per 8-query micro-batch, all on one core. A cycle model fits the DGX results: about 31 ms of non-kernel overhead per batch plus about 38 ms of kernel gives 116 QPS.

Recommended interim fix (item 3): send the per-(query, shard) nq=1 calls to a dedicated module-level ThreadPoolExecutor, merged in the same order as today. I measured the GIL as released and parallelism as real (loaded Windows x86-64 laptop, not the DGX: a simulated 8x16-probe batch ran 3.3-3.8x faster). Also move the allowlist intersection to the shard side, which on the same laptop is 18x faster at a 255k allowlist and accounts for the 72 ms filtered p50.

Estimated DGX result (UNVERIFIED): concurrent QPS 116 -> about 175-205, recall unchanged at 0.994, serial p50 11.2 -> about 8.5 ms, filtered p50 72 -> about 12-16 ms.

Turbovec 1.0.0 closes none of raggio's Python API gaps. Ranked upstream asks:
1. E1: a multi-part batched search_many with shared query prep, one GIL release, pool scheduling and a foreign-id-tolerant allowlist. It fits turbovec's no-training principle.
2. Expose ids(), which already exists in Rust as iter_ids.
3. An allowlist intersect mode.
4. Option A, a native IVF with caller-supplied centroids.
5. Then D (RaBitQ-IVF), B (SOAR) and C (HNSW/Vamana), which fit poorly. SOAR would push the 4 GiB cap past its limit at arXiv scale.

## Brief
# Item 3a: turbovec internals and raggio IVF concurrency. Research brief

All local timings come from a loaded Windows x86-64 laptop, not the DGX: a Xeon E-2226G (6 cores / 6 threads, AVX2, no AVX-512) with about 77% background CPU from other processes. They are labelled "laptop" and are directional only. DGX figures are either taken from committed docs (ADR 0002, docs/benchmark-arxiv.md) or derived from them, and are labelled that way. The turbovec version is 1.0.0 (PyPI latest; raggio pins `turbovec>=1.0.0`, pyproject.toml:10; installed in raggio .venv: 1.0.0). Source read at D:\DEV\_scratch\turbovec (read-only).

---

## 1. Architecture map (turbovec 1.0.0)

### 1.1 Quantizer and code layout
- **TurboQuant pipeline:**
  - Block-Hadamard rotation, fixed seed, K=2 rounds. Applied to queries in `search` at turbovec/src/search.rs:3241.
  - A Lloyd-Max codebook that depends only on (bits, dim).
  - TQ+ per-coordinate calibration (shift and scale), fitted by `calibrate` (turbovec/src/lib.rs:2733) and applied to queries by `calibrate_queries` (search.rs:3154).
  - A per-vector scale.
- README principle: "a data-oblivious quantizer ... no separate training phase" and "Online ingest ... no train step, no parameter tuning, no rebuilds" (README.md:16-18). This matters for ranking the upstream options.
- **Consequence for raggio:** every raggio shard is calibrated from the same `sample[:CAL_SAMPLE]` (store.py:405-410). All shards therefore share rotation, codebook and calibration, so query prep could in principle be done once for all shards. That is the premise of E1 below.
- **Code layouts:**
  - Canonical bit-plane packing in SIMD blocks of 32 rows (`pack_blocked_native`, pack.rs:19).
  - On x86 the classic layout is perm0-interleaved and served by the AVX2 nibble-LUT kernel. The vector-major layout is used only with AVX-512 VBMI+VNNI+BW (`use_vector_major` pack.rs:1189, `vector_major_for` pack.rs:1260).
  - On aarch64 with i8mm, the vm8 layout (`use_vm8` pack.rs:1307, `vm8_for` pack.rs:1314) feeds SMMLA permute-dot kernels.
  - Whether the DGX (Cortex-X925/A725) actually takes the vm8/i8mm path is UNVERIFIED; it likely does.

### 1.2 Search dispatch (core)
- **Thresholds:**
  - `SINGLE_QUERY_PARALLEL_MIN_BLOCKS = 1024` (search.rs:30), i.e. 32,768 rows.
  - `single_query_parallelizes` (search.rs:84), `serial_required` (search.rs:111), `n_block_ranges` (search.rs:125).
  - `rescan_min` (search.rs:244), `range_cap_for_k` (search.rs:265).
- **NEON (aarch64) path:**
  - When nq==1 and n_blocks >= 1024, it takes the block-parallel path `search_single_query_block_parallel_neon` (search.rs:3438).
  - Otherwise it builds query tiles, with qbs=12 if nq>=12, 8 if nq>=8, else 4 (search.rs:~3538-3546), and runs `pd_scan!(12/8/4)` (search.rs:~3603).
  - Batches smaller than 4 queries fall to a tail that runs one single-query kernel per query and rebuilds `vm8_single` each time (search.rs:3717-3760). **So nq=2 and nq=3 get no amortization on NEON.**
  - Supporting helpers: `scan_range_neon` (search.rs:3289), `neon_block_topk_update` (search.rs:2651), `build_query_neon_lut_from_slice` (search.rs:2856), `mask_allows` (search.rs:3000), `block_has_allowed` (search.rs:3015; block-level allowlist skip, from upstream #30).
- **x86 path:**
  - `nq_batch` is 8, or 10 only with pd and a 1-thread pool (search.rs:3997).
  - The AVX2 kernel consumes 4-query chunks padded to 4 (search.rs:~4170-4200), so an nq=1 call does roughly the arithmetic of nq=4.
- **Upstream ARM measurements** (Axion c4a, benchmarks/hillclimb/LOG_search.md):
  - P42 (line 5064, table at 5076): nq=1 single-threaded kernel runs at 20.06 GB/s, 95% of single-core streaming.
  - H93 (line 5155, 5168): nq=1 multi-threaded is "0.014 ms fixed + 135.3 GB/s marginal".
  - H101 (lines 5795-5818): ms/pass by nq is 1: 0.560, 2: 1.046, 4: 0.943, 8: 1.235, 12: 1.618. The batched kernel is compute-bound (the byte rate falls from 137 to 47 GB/s), but per query it is about 3.6-4.2x cheaper at nq>=8. nq=2 is slower than nq=4.

### 1.3 IdMapIndex (turbovec/src/id_map.rs)
- `search` (id_map.rs:593).
- `try_search_with_allowlist` (id_map.rs:664) allocates `vec![false; self.inner.len()]` on every call (id_map.rs:675). It errors on an empty allowlist or on ids the index does not hold (tests in turbovec/tests/filtering.rs).
- `remove` (id_map.rs:520) is swap-and-pop.
- `iter_ids` (id_map.rs:722) is **Rust-only; it is not exposed in Python.**
- Core lib: `search` (lib.rs:1239), `try_search_with_mask` (lib.rs:1345), `prepare` (lib.rs:1496), `sync` (lib.rs:1737), `from_parts` (lib.rs:2371), `swap_remove` (lib.rs:2864).

### 1.4 Python binding (turbovec-python/src/lib.rs)
- `search` releases the GIL via `py.detach` (lib.rs:1177). It then dispatches with `with_pool_if(nq > 1 || single_query_parallelizes(len))` (lib.rs:1211).
- `with_pool` (lib.rs:1821) and `with_pool_if` (lib.rs:1863): the inline nq=1 path runs on the calling thread and never touches the one-thread global sentinel pool (`pin_global_pool_sentinel`, lib.rs:1879).
- The process pool is `current_pool` (lib.rs:1734), sized by `RAYON_NUM_THREADS` or the hardware default (`desired_threads`, lib.rs:~1690).
- Calls with nq>1 pay the ~70 µs `install` handoff documented in the comment at lib.rs:224-234.
- **Consequence:** concurrent Python threads each issuing nq=1 calls on small shards run truly in parallel, with no pool contention.
- Python surface of IdMapIndex (lib.rs 952+): new, add_with_ids, remove, search(queries, k, *, allowlist), contains, prepare, calibrate, write, sync, load, to_bytes/from_bytes, __reduce__, __len__, __contains__, dim, calibration_state, bit_width.
- The `_interruptible.py` wrapper (turbovec-python, lines 231-269) exposes the raw method as `IdMapIndex.search.__wrapped__`. Laptop: wrapper cost is about equal to raw.

### 1.5 Persistence
- io_v7.rs:1-60 describes the v7 format: a superblock; alternating A/B headers with CRC; 32-row block units with ids; one fsync per `sync`; redo ops for removals.
- raggio stores one v7 file per shard (`shard-NNNN.tvim`) plus `centroids.npy` (store.py:412-430).

### 1.6 raggio's use (src/raggio/store.py)
- `_ivf_auto_nlist` (store.py:356-359) targets about 8192 rows per shard. At 2.55M rows that gives nlist=256, about 10k rows and 5.1 MB of codes per shard (d=1024, 512 B/row).
- `_IvfIndex` (store.py:362):
  - `train` uses 8 Lloyd iterations; every shard is calibrated from the same sample.
  - `_shard_ids` (store.py:463-475) uses a k=len probe. Laptop: 1.7 ms per 10k shard.
  - `_intersect` (store.py:477-483) runs a searchsorted of the **whole allowlist** into the shard ids, for every probed shard: O(|allow| log |shard|).
  - `search` (store.py:485-530) runs, per query and per probe, a serial nq=1 `sh.search`, then merges with `np.argsort(-s)[:k]`. Short batch rows are padded with id 0 / -inf.
- Callers:
  - `_search_rescored` (store.py:1146) over-fetches to `_rescore_k` = max(50, 2n) capped at 2000 (store.py:38-40, 1106), then does the fp16 rescore (store.py:1111).
  - The filtered path `_vector_ids` (store.py:1164) runs nq=1 via `to_thread`, with the allowlist in `_allow_cache`.
  - Unfiltered requests go to `_scan_batched`/`_drain_scans` (store.py:1199-1233): one micro-batch in flight, nprobe = max over the batch, `asyncio.to_thread(self._search_rescored, ...)`.
- **Consequence:** a batch of 8 concurrent queries at nprobe=16 becomes 128 serial nq=1 calls on one thread.

## 2. Root cause of the "~0.4 ms fixed cost per shard search call"

### 2.1 Laptop measurements (Windows x86-64, loaded, AVX2; not the DGX)
Probe: D:\DEV\_scratch\probe-item3a\probe_fixed_cost.py; log alongside.

**Part A: nq=1, k=50, d=1024, 4-bit (µs per call by shard rows)**

| rows | µs/call |
|---|---|
| 64 | 70 |
| 256 | 96 |
| 1,024 | 163 |
| 4,096 | 383 |
| 8,192 | 780 |
| 10,000 | 859 |
| 16,384 | 1,535 |
| 32,768 | 1,329 (pool path engages at 1024 blocks) |
| 65,536 | 2,404 |
| 131,072 | 4,670 |

- The fit is about 70-85 µs of intercept plus about 77 ns per row. The intercept is partly the AVX2 pad-to-4 kernel plus query prep; the breakdown was not profiled (UNVERIFIED).
- Wrapper cost is about equal to raw; `len()` takes 0.3 µs.

**k sweep at 10k rows:**

| k | ms |
|---|---|
| 1 | 0.93 |
| 50 | 1.0 |
| 200 | 1.2 |
| 1000 | 3.7 |

So k=50 is not the problem.

**Part B: nq sweep at 10k rows**

| nq | µs/call | µs/query |
|---|---|---|
| 1 | 1,100 | 1,100 |
| 2 | 1,163 | 582 |
| 4 | 1,564 | 391 |
| 8 | 2,606 | 326 |
| 16 | 2,787 | 174 |
| 64 | 8,008 | 125 |
| 128 | 12,408 | 97 |

**Multi-query `IdMapIndex.search` works:** the output shape is (nq, k), and batched rows equal the nq=1 rows exactly (asserted in the probe).

**Part C: allowlist at 10k rows**

| allowlist ids | µs |
|---|---|
| 16 | 132 (block skip) |
| 128 | 418 |
| 1,000 | 1,017 |
| 5,000 | 1,170 |

**GIL probe** (probe_gil.py; threads doing nq=1 calls on separate shards), calls/s:

| rows | T=1 | T=6 | T=12 |
|---|---|---|---|
| 64 | 9.8k | 40.6k | 41.2k |
| 10k | 457 | 2,383 | 3,524 |

- The GIL is released and scaling is real.
- The GIL-held share per call is at most about 24 µs (the 64-row ceiling).

### 2.2 DGX derivation (from committed ADR 0002 evidence; 2.2M x 1536-d, 4-bit = 768 B/row, about 1.70 GB)
- **Flat:** 18.1 ms, i.e. 1.70 GB in 18.1 ms, **about 94 GB/s**. This is the multi-threaded block-parallel path.
- **IVF, nlist=256, nprobe=16:** 5.1 ms for 16/256 of 1.70 GB = 106 MB, **about 20.7 GB/s**.
- **IVF, nprobe=8:** 2.7 ms for 53 MB, about 19.7 GB/s.
- 5.1 ms / 16 calls = 0.32 ms per call, **including** 6.6 MB of bytes. A 0.4 ms fixed cost is therefore arithmetically impossible on the DGX at that scale.
- The nprobe 8 vs 16 intercept is about 0 ms (2 x 2.7 is about 5.1).
- ~20 GB/s is single-core throughput. It matches the ARM P42 roofline (20.06 GB/s on Axion) and sits below GB10 single-core DRAM bandwidth (about 38 GB/s on X925 and 26 GB/s on A725 per Chips and Cheese). "Single-core-bound" is the accurate label; whether the limit is bandwidth or per-core NEON compute is UNVERIFIED.
- **Root cause:**
  - Shard scans are serial, inline, on one thread. IVF reads 6.25% of the bytes at about 1/4.5 of the flat scan's bandwidth, hence only about 3.5x.
  - The "fixed cost" narrative (ADR 0001:30, ADR 0002:8, docs/concepts.md:151, docs/indexing.md:19, store.py:72 and 357-358) probably came from timing ~8k-row shards, whose cost is about 0.3-0.4 ms of bytes. It may also have come from the AVX2 laptop, where the intercept really is ~75 µs. Which host produced the figure is UNVERIFIED: ivf-probe.log is not in the repo.
- Upstream H93 on ARM puts the multi-threaded fixed cost at 0.014 ms. The single-threaded inline fixed cost on GB10 is expected to be tens of µs (UNVERIFIED; the protocol step 2 measures it).

### 2.3 Concurrency cycle model (DGX, arXiv; derived)
- arXiv: 2,549,119 rows x 512 B = 1.305 GB of codes; nlist=256 gives about 5.1 MB per shard; nprobe=16 gives 81.6 MB per query and 653 MB per 8-query batch.
- **Flat, concurrency 8:** 129 QPS, i.e. 62 ms per 8-query cycle.
  - The nq=8 flat pass is about 2.2x the nq=1 pass (H101 ratio, measured on Axion not GB10). nq=1 is 1.305 GB / 94 GB/s = 13.9 ms, so the nq=8 pass is about 31 ms.
  - That leaves **about 31 ms of non-kernel work per cycle**: rescore blob fetch, hydration, HTTP/JSON for 8 requests, mostly GIL-serialized.
- **IVF, concurrency 8:** 116 QPS, i.e. 69 ms per cycle, which is 31 ms of non-kernel work plus about 38 ms of kernel (128 serial calls x ~0.25-0.3 ms). The model is consistent with the measurement.
- **IVF serial:** p50 11.2 ms, which is about 4-4.5 ms of kernel (16 serial calls) plus about 7 ms of non-kernel work.
- **IVF filtered:** p50 72.3 ms, dominated by `_intersect` (next item).
- **Intersect probe** (laptop; probe_intersect.py; 2.55M ids, 16 shards):

  | allowlist | current | shard-side searchsorted |
  |---|---|---|
  | 25.5k | 6.9 ms | 2.8 ms |
  | 255k (~10%, like the mode-year filter) | 94.1 ms | 5.2 ms |
  | 765k | 345 ms | 8.4 ms |

## 3. API gaps raggio hits, and turbovec 1.0.0 status

| Gap | raggio workaround | 1.0.0 status | Proposal |
|---|---|---|---|
| No partitioned / multi-index batched search | Python loop, store.py:516-525 | absent | E1 search_many, then A |
| Many small calls: each pays query prep; nq>1 pays about 70 µs handoff | none | nq=1 inline has no handoff (lib.rs:1211, 1863); nq>1 has install (lib.rs:224-234, 1821) | E1: one release, one handoff per batch |
| nq=2-3 get no amortization on NEON | n/a | tail path search.rs:3717-3760 | E2: pad nq=2-3 into a qbs=4 tile (small) |
| No id enumeration in Python | k=len probe, store.py:463-475; `all_ids` 532 | `iter_ids` exists in Rust only (id_map.rs:722) | expose `ids()` (tiny) |
| Allowlist must be a subset of the index | per-shard `_intersect`, store.py:477-483 | errors on unknown ids (filtering.rs) | `allowlist_mode="intersect"` or `strict=False` |
| Per-call mask allocation O(len) | n/a | id_map.rs:675 | allocate a mask sized to the allowlist, or reuse it |
| No seeded or shared top-k threshold across calls | none | #203 closed as completed, but no such API found in the 1.0.0 source (UNVERIFIED) | part of E1 (per-query running kth score across parts) |
| No per-query allowlists | n/a (raggio's filtered path is nq=1) | one allowlist per call | low priority |
| No reconstruction | fp16 copies in meta.db (ADR 0002) | absent | low priority; raggio already pays for this |
| No coarse quantizer | `_IvfIndex` (store.py:362) | absent; README says "no train step" | A with caller-supplied centroids |

**Summary:** 1.0.0 closes none of these on the Python surface. Upstream issues checked through the GitHub API:
- no IVF or HNSW feature issue exists;
- #112 is a discussion (pgvector HNSW comparison);
- #30: cardinality-aware allowlist, shipped as the block skip;
- #201: large k;
- #202: TQ+ calibration exposure;
- #203: seeded cutoff;
- #311: IdHasher fix.

## 4. Design options at 2.55M x 1024, 4-bit

**DGX bandwidth assumption (UNVERIFIED):**
- Spec: 273 GB/s LPDDR5x.
- CPU-achievable all-core: about 100-135 GB/s (NVIDIA forum lmbench about 135 GB/s; raggio's flat scan achieves about 94 GB/s).
- Single core: about 20-38 GB/s.
- Kernel figures below use 80-135 GB/s for "all cores" and 20.7 GB/s for "one core".
- Baseline flat nq=1 is about 14 ms per query; the flat nq=8 pass is about 31 ms.

| Option | Kernel per query (nq=1) | 8-query batch | Recall@10 (rescored) | Memory vs flat (1.31 GB codes) | LOC (UNVERIFIED) | Fit with turbovec principles |
|---|---|---|---|---|---|---|
| raggio today (serial nq=1 shards) | ~4-4.5 ms | ~38 ms | 0.994 @ nprobe 16 (measured) | +~20 MB ids cache, +1 MB centroids | n/a | n/a |
| raggio interim T1 (thread fan-out) | ~0.8-1.4 ms | ~8-11 ms | identical results | same | ~60 + tests | n/a |
| **E1** search_many (multi-part batched primitive) | ~0.6-1.2 ms (3.5-7x vs today; 12-23x vs flat) | ~5-8 ms (4-6x vs flat nq=8) | identical | same | 400-700 core + ~150 binding + ~800 tests | Excellent: no training, no format change, pure scheduling and shared prep |
| **A** native IVF-FastScan (PartitionedIdMapIndex with caller-supplied centroids) | 0.6-1.0 ms (81.6 MB at 80-135 GB/s) | ~5-8 ms | same as raggio IVF at the same nlist/nprobe (0.994 @ 16) | +20 MB ids, +1 MB centroids, +~60-80 MB id to (list, slot) map (optional) | 3-4k incl. tests | Moderate: k-means and rebuild-on-drift conflict with "no train step, no rebuilds" unless training stays in the caller |
| **B** SOAR (spilled secondary assignment) on top of A | ~1.2-1.5x fewer bytes at fixed recall (literature; UNVERIFIED here) | similar | higher at the same nprobe | **2x codes (+1.3 GB)**. IVF memory under load is 3.07 GB today, so this would reach about 4.4 GB, **over the 4 GiB cap** | +~500 over A | Poor |
| **C** HNSW/Vamana over 4-bit codes | ~0.3-1 ms single thread, latency-bound | high QPS | 0.95-0.99, depends on ef, needs rescore | +128-256 B/vec adjacency (+330-650 MB), plus a per-vector code layout copy | 4-6k | Very poor: graph build takes minutes; deletes need tombstones and repair; filters need ACORN-style traversal; the 32-row SIMD block layout does not support random per-vector access; ADR 0002 already rejected it |
| **D** RaBitQ-IVF (1-bit residual codes, 128 B/vec) | ~2-4x fewer bytes than A | ~2-4x over A | needs a deeper rerank (k 200-500), so raggio's fp16 blob fetches grow 4-10x | -75% codes if replacing 4-bit, +25% if alongside | 4-6k, new quantizer | Poor near-term; research grade |

**Takeaways:**
- At concurrency 8, QPS is capped by raggio's ~31 ms per-cycle non-kernel overhead: at most about 8/31 ms = 258 QPS even with a zero-cost kernel.
- The interim fan-out captures most of the achievable concurrent-QPS gain.
- Upstream E1/A mainly buys CPU efficiency (fewer cores burned per query), serial latency, and headroom at higher concurrency.

## 5. Interim raggio design: fan-out batched `_IvfIndex.search`

### 5.1 Simulation (laptop; probe_ivf_batch.py)
- Setup: 256 shards x 10k rows, 8 queries x 16 uniform random probes, k=50.
- Probes touched 103.1 distinct shards per batch (of 128 calls). Group-size histogram: [1620, 390, 48, 4], so overlap is low.

| strategy | ms/batch | vs serial |
|---|---|---|
| serial (today) | 123.9 | 1x |
| grouped (serial) | 104.4 | 1.19x |
| fan_q1, T=1 | 138.9 | 0.89x (pool overhead, so T<=1 must bypass the pool) |
| fan_q1, T=2 | 72.3 | 1.7x |
| fan_q1, T=6 | 52.9 | 2.3x |
| fan_q1, T=12 | 37.5 | 3.3x |
| fan_grp, T=6 | 42.6 | 2.9x |
| fan_grp, T=12 | 32.7 | 3.8x |
| fan_query (per-query tasks), T=12 | 47.1 | 2.6x |

All strategies returned identical id sets. On NEON, grouping mainly produces nq=2-3 groups, which get no amortization (search.rs:3717-3760) and add the ~70 µs install. **Recommend plain (query, shard) nq=1 fan-out.** Grouping only helps groups of 4 or more; make it an optional follow-up gated on the real arXiv group histogram.

### 5.2 Design (store.py)
```python
IVF_SEARCH_THREADS = int(os.environ.get("IVF_SEARCH_THREADS", "0")) or min(32, _cpus())
# _cpus(): len(os.sched_getaffinity(0)) where available, else os.cpu_count()

_ivf_pool: tuple[int, ThreadPoolExecutor] | None = None   # (size, pool), lazily built under a lock

def _ivf_map(fn, tasks):
    """Order-preserving map on a DEDICATED pool. Never the asyncio default executor:
    callers already run inside it (to_thread), so waiting on it there can deadlock."""
    n = IVF_SEARCH_THREADS
    if n <= 1 or len(tasks) <= 1:
        return [fn(t) for t in tasks]          # today's exact serial behaviour
    pool = _get_pool(n)                        # rebuild if size changed (tests monkeypatch)
    return list(pool.map(fn, tasks))
```

Inside `_IvfIndex.search`:
1. Compute `probes` exactly as today.
2. Build `tasks = [(qi, j, allow_j)]`, skipping empty shards and empty per-shard allowlists. Memoize `allow_j` per shard within the call, because the allowlist is the same for all queries.
3. Run `run(t) = shard.search(queries[qi:qi+1], k=min(k, len(shard)), allowlist=allow_j)` through `_ivf_map`.
4. Regroup results per qi in task order, so concatenation order and the argsort result are identical to today.
5. Keep the padding contract (id 0 / -inf; nq=1 trimmed).

Everything else (nprobe max per batch, rescore, `_drain_scans`) stays unchanged.

### 5.3 Filtered path (shard-side intersection)
- `_allow_cache` stores the allowlist **sorted** (sorted once per cache miss).
- `_intersect(allow_sorted, j)` iterates the smaller side:
  - if `len(allow) < len(sids)`: today's direction;
  - else: `pos = searchsorted(allow_sorted, sids)`, clip it, and return `sids[allow_sorted[pos] == sids]`.
- The ≤128 owner scan keeps the allow-side direction automatically.
- Laptop: 94 ms becomes 5.2 ms at 255k.

### 5.4 Estimates for DGX (UNVERIFIED; model in section 2.3)
- **Concurrent:** kernel phase is 653 MB / 80-135 GB/s = 4.8-8.2 ms, plus GIL dispatch of up to 128 x ~24 µs = 3 ms, so about 8-11 ms. The cycle becomes about 39-46 ms, i.e. **about 175-205 QPS** (from 116).
- **Serial p50:** 11.2 ms becomes about 8-8.5 ms (kernel 4-4.5 ms down to about 1.1 ms).
- **Filtered p50:** 72.3 ms becomes about 12-16 ms (flat is 29.3 ms).
- **Recall:** unchanged at 0.994, since the results are identical by construction.

## 6. Upstream proposal (ranked), with Rust API sketch
1. **E1: `search_many`.**
```rust
pub enum AllowlistMode { Strict, Intersect }        // Strict = today's error on unknown ids
pub struct MultiSearch<'a> {
    pub parts: &'a [&'a IdMapIndex],
    pub probe_offsets: &'a [usize],                  // CSR over queries: nq + 1
    pub probes: &'a [u32],                           // part indices per query
    pub allowlist: Option<&'a [u64]>,
    pub allowlist_mode: AllowlistMode,
}
impl IdMapIndex {
    pub fn search_many(ms: &MultiSearch, queries: &[f32], k: usize)
        -> Result<SearchResults, SearchError>;       // (nq*k) scores desc, ids; short rows padded
}
```
   Python: `turbovec.search_many(parts, queries, k, *, probes, allowlist=None, allowlist_mode="intersect") -> (scores[nq,k], ids[nq,k])`. The padding sentinel (0 vs u64::MAX, or also returning counts) is an open question for upstream.

   Semantics:
   - Validate that the parts share dim, bit width and quantizer state (calibration vectors equal). If not, return `IncompatibleParts`, or fall back to per-part prep.
   - Prepare each query once: rotation (search.rs:3241), `calibrate_queries` (3154), LUTs (2856).
   - Invert probes into per-part query lists. Schedule (part, query-tile) units on the process pool, with one `py.detach` and one `with_pool`.
   - Use pd tiles for >=4 queries and the single kernel otherwise. Merge per query with a k-way heap.
   - Optional per-query running kth threshold shared across units, which feeds `neon_block_topk_update` (2651). This is the #203 idea.
   - In Intersect mode, build the allowlist mask from ids the part holds, with no error; keep the `block_has_allowed` skip.
2. **Expose `ids()`** in Python, backed by `iter_ids` (id_map.rs:722). About 30 LOC. This deletes raggio's k=len probe hack.
3. **`allowlist_mode="intersect"`** on `IdMapIndex.search`. About 50-100 LOC. This deletes `_intersect` entirely.
4. **E2:** pad nq=2-3 into the qbs=4 tile on NEON. Small, and it makes grouping worthwhile.
5. **A:** `PartitionedIdMapIndex` / `IvfIdMapIndex`, built on E1.
   - `new(dim, bits, centroids)`, an optional `train_centroids` helper outside the core principle, `calibrate`, `add_with_ids` routing by argmax.
   - `remove`/`contains` through an id to (list, slot) map, `search(queries, k, IvfSearchOpts{nprobe, allowlist, probe_lists})`, `ids`, `list_sizes`.
   - `sync`/`load` as a directory of per-list v7 files plus a manifest, or a v8 list tag.
6. D, 7. B, 8. C (see section 4).

The maintainer asked for "simple english" in issues (#203 thread). The drafts should be short and plain.

## 7. Test strategy

**Upstream (mirrors turbovec/tests style: self-contained xorshift gaussian generators, seam tests):**
- `search_many.rs`:
  - Over one part with every query probing it, the result equals `IdMapIndex::search` bitwise (like batch_matches_single.rs).
  - With random probes and nq in {1,2,3,4,5,8,12,13}, it equals the per-part search plus a host merge.
  - Incompatible parts are rejected.
  - Short rows are padded; k greater than the part length works.
  - Out-of-range, duplicate or empty probes are rejected (as in input_validation.rs).
- `filtering.rs` additions: search_many allowlist in Intersect mode `mask_matches_post_hoc_filter`; foreign ids are ignored; the block-skip test still passes.
- `concurrent_search.rs`: search_many racing add/remove on the parts.
- `allocation_hot_paths.rs`: no O(part.len()) allocation per part per call.
- `recall_sanity.rs`: all parts probed gives flat recall.
- Python: test_gil_release.py (threads progress during search_many), test_fork_safety.py (pool after fork), test_interruptible.py, test_argument_errors.py, test_id_map.py (ids()).

**raggio (tests/test_ivf_index.py; reuse the existing helpers make_collection/rowvec/ingest/attach/top_ids, DIM=8, and the autouse IVF_MIN_ROWS=100 fixture):**
- `test_ivf_fanout_matches_serial`: over nq in {1,3,8} and nprobe in {1,4,nlist}, compare IVF_SEARCH_THREADS=1 with 4. Scores and ids must be `np.array_equal`.
- `test_ivf_fanout_padding_contract`: one query whose probed shards are empty gives an id 0 / -inf row; nq=1 is trimmed.
- `test_ivf_fanout_propagates_shard_error`: a monkeypatched shard.search raises, the error propagates, and `_drain_scans` resolves every future.
- `test_ivf_fanout_runs_on_pool_threads`: a spy that sleeps 5 ms records the thread idents; more than one worker ident and none equal to the caller thread.
- `test_ivf_fanout_no_deadlock_single_worker_default_executor`: `loop.set_default_executor(ThreadPoolExecutor(1))` plus 16 concurrent searches complete under `wait_for(10 s)`.
- `test_intersect_both_directions_match_intersect1d`: allowlist sizes {0, 5, 128, 129, 5000, all}, unsorted input with foreign ids.
- `test_filtered_ivf_large_allowlist_full_probe_matches_flat`: allowlist larger than 128 with nprobe=nlist gives the same ids as the flat index.
- `test_allow_cache_is_sorted`.
- All existing IVF tests must stay green, in particular test_attach_matches_flat_and_survives_reload (56), test_upsert_and_delete_while_indexed (87), test_filtered_search_uses_owning_shards (154) and test_index_api_lifecycle (191).

## 8. Ordered task list

**T1: fan-out `_IvfIndex.search`**
- Files: src/raggio/store.py (module pool and knob near IVF constants at store.py:68-78; search at 485-530).
- Tests: the 5 fan-out tests in section 7.
- Acceptance:
  - Tests green.
  - Laptop `IVF_BATCH` mode of the new ivf_probe on a synthetic corpus shows at least 2x per batch at T>=4.
  - DGX concurrent QPS above 116 (target at least 170), recall exactly 0.994, serial p50 at or below 11.2 ms, hybrid within ±10%.

**T2: shard-side intersection plus sorted `_allow_cache`**
- Files: store.py `_intersect` (477-483) and `_vector_ids` (1164-1196).
- Tests: the 3 intersect/filtered tests.
- Acceptance: DGX filtered p50 at or below 29.3 ms (flat), target 20 ms or less.

**T3: bench tooling**
- `bench/ivf_probe.py`:
  - LIMIT and DIM taken from `embed-vecs.npy` (env `IVF_LIMIT` override), today hardcoded at ivf_probe.py:16;
  - gt at `bench/gt-{LIMIT}-{SEED}-d{DIM}.npz` (bench.py:504 naming), replacing lines 88-89;
  - env sweeps `IVF_NLISTS`, `IVF_NPROBES`, `IVF_CAL_MODES` (loops at 198, 211, 219);
  - `IVF_RESCORE_K=50` emulation: over-fetch 50, fp16-round the originals, exact rescore, top-10;
  - `IVF_BATCH=8` plus `IVF_THREADS=1,4,...` fan-out throughput mode that logs the group-size histogram.
- New `bench/shard_call_probe.py`: a port of the scratch probe_fixed_cost.py Parts A, B and C plus the GIL probe. Synthetic data, no corpus.
- Acceptance: at nlist=256 and nprobe=16 with rescore, arXiv recall is within 0.003 of the bench's 0.994.

**T4: docs and ADR correction** (after the DGX numbers)
- New ADR 0004 (or an ADR 0002 addendum): IVF fan-out and the corrected root cause.
- Edit docs/concepts.md:151-152, docs/indexing.md:19 and 42, store.py:72 and 357-358, and ADR 0002 Consequences (76-77).
- Acceptance: no remaining "0.4 ms fixed" claims; the numbers cite `bench/results-arxiv-ivf-fanout*.md`.

**T5 (optional, gated): group queries sharing a shard when the group has at least `GROUP_MIN` queries (4 on NEON)**
- Only if the DGX fan-out log shows a mean group size of 2 or more.
- Test: grouped equals ungrouped.

**T6 (optional, gated): nprobe retune**
- Only if the sweep shows nprobe 12 at recall of 0.99 or more with at least a 10% QPS gain. This is a user decision.

**T7: upstream issue drafts** (text only; the user decides whether to post)
- E1 search_many, ids(), allowlist intersect mode, E2 tail padding.

## 9. DGX measurement protocol (human or executor; no SSH from agents)

**Step 1: host facts**
```
lscpu; lscpu -e=CPU,CORE,MAXMHZ; nproc
uv run python -c "import turbovec, numpy; print(turbovec.__version__, numpy.__version__)"
git clone --depth 1 https://github.com/RyanCodrai/turbovec /tmp/turbovec
cc -O3 -pthread -o /tmp/mem_rates /tmp/turbovec/benchmarks/hillclimb/mem_rates.c
for t in 1 4 10 20; do /tmp/mem_rates $t; done
taskset -c <an X925 cpu> /tmp/mem_rates 1; taskset -c <an A725 cpu> /tmp/mem_rates 1
```
Use `lscpu -e` MAXMHZ to identify the X925 and A725 CPUs.

**Step 2: per-call cost** (the T3 probe)
```
uv run python bench/shard_call_probe.py | tee bench/shard-call-dgx-host.log
podman cp bench/shard_call_probe.py bench-tv:/tmp/
podman exec bench-tv python /tmp/shard_call_probe.py | tee bench/shard-call-dgx-container.log
```
Record the intercept and ns/row at d=1024 and the nq sweep.

**Step 3: arXiv IVF probe** (needs `bench/corpus/embed-vecs.npy` 2,549,619x1024 and `bench/gt-2549619-42-d1024.npz`)
```
IVF_NLISTS=256 IVF_NPROBES=8,12,16,24 IVF_RESCORE_K=50 IVF_CAL_MODES=global uv run python bench/ivf_probe.py | tee bench/ivf-probe-arxiv.log
IVF_NLISTS=256 IVF_NPROBES=16 IVF_BATCH=8 IVF_THREADS=1,4,8,12,16,20 uv run python bench/ivf_probe.py | tee bench/ivf-probe-arxiv-fanout.log
```

**Step 4: same-day baseline on current main** (the DGX was restarted, so the 2026-08-23 numbers may drift)
- Build `raggio:main` from the main commit.
- Recreate `bench-tv` with it: `podman run -d --name bench-tv --memory 4g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench raggio:main`.
- Run `PYTHONUNBUFFERED=1 uv run python bench/bench.py --engine raggio-ivf --limit 2549619 --out bench/results-arxiv-ivf-baseline.md --caps-note "raggio 4 GiB, Weaviate 32 GiB"`.

**Step 5: candidate**
```
podman build -t raggio .; podman rm -f bench-tv
podman run -d --name bench-tv --memory 4g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench raggio
for i in 1 2 3; do PYTHONUNBUFFERED=1 uv run python bench/bench.py --engine raggio-ivf --limit 2549619 --out bench/results-arxiv-ivf-fanout-$i.md --caps-note "raggio 4 GiB, Weaviate 32 GiB"; done
```
- Knob sweep: recreate the container with `-e IVF_SEARCH_THREADS=8` and then `=20`, one run each.
- Optional: `--concurrency 32`.
- nprobe retune without a rebuild: `curl -s -X POST localhost:18000/collections/bench/index -H 'x-api-key: bench' -H 'content-type: application/json' -d '{"nprobe": 12}'`, poll `GET /collections/bench/jobs/<id>`, then rerun.

**Step 6: flat control, run LAST.** `--engine raggio` calls `ensure_no_index` (bench.py:399), which drops the IVF index; the next raggio-ivf run then rebuilds it (about 337 s).
```
uv run python bench/bench.py --engine raggio --limit 2549619 --out bench/results-arxiv-flat-control.md --caps-note "raggio 4 GiB, Weaviate 32 GiB"
```

**Acceptance:**
- Concurrent QPS above 116 in all 3 runs (target median of 170 or more).
- Recall@10 of 0.99 or more (expect 0.994 at nprobe 16).
- Serial p50 at or below baseline.
- Filtered p50 at or below the flat p50.
- Hybrid within ±10%.
- Memory under load at or below the 3.1 GB baseline plus 50 MB.

## 10. Risks
- **All-core CPU bandwidth may be lower than assumed** (forum reports about 135 GB/s via lmbench; containers may differ). Mitigation: step 1 measures it.
- **GIL convoy:** 128 tasks x ≤24 µs of GIL time contend with the event loop. This could raise p95 for hybrid and ingest. Mitigation: cap the pool size; watch "p95 under concurrency" (77.8 ms baseline).
- **Oversubscription:**
  - turbovec's rayon pool (nq>1 calls, or shards over 32,768 rows) plus the fan-out pool plus the asyncio default executor (min(32, cpu+4)).
  - The X925/A725 heterogeneity adds tail latency. Dynamic task scheduling mitigates it.
  - Setting `RAYON_NUM_THREADS` or `IVF_SEARCH_THREADS` below 20 may help.
- **Skewed k-means lists:** a list over 32,768 rows switches to the pool path, which serializes against other callers. `ivf_probe` logs the maximum shard size.
- **Nested-executor deadlock:** a future refactor could move the fan-out onto the default executor. The test covers this.
- **Pool lifecycle:** the pool is created lazily (fork-safe as long as it is never built at import); atexit joins short tasks.
- **Upstream:** the maintainer may reject IVF-in-core on principle; E1 is principle-neutral. #203's status is unclear.
- **Evidence quality:** laptop numbers are noisy (heavy background load). The simulation used uniform probes; real arXiv overlap is unknown.

## 11. Sources
- [Chips and Cheese: Inside NVIDIA GB10's memory subsystem](https://chipsandcheese.com/p/inside-nvidia-gb10s-memory-subsystem): single-core DRAM bandwidth of X925 and A725.
- [NVIDIA forum: DDR bandwidth significantly lower than the claimed 273 GB/s](https://forums.developer.nvidia.com/t/the-ddr-bandwidth-is-significantly-lower-than-the-claimed-273gb-s/363238): lmbench about 135 GB/s.
- [NVIDIA DGX Spark hardware docs](https://docs.nvidia.com/dgx/dgx-spark/hardware.html): 273 GB/s LPDDR5x.
- [turbovec repo (README principles, benchmarks/hillclimb)](https://github.com/RyanCodrai/turbovec)
- [turbovec #203: seeded top-k threshold](https://github.com/RyanCodrai/turbovec/issues/203)
- [turbovec #30: allowlist](https://github.com/RyanCodrai/turbovec/issues/30)
- [turbovec #112: HNSW discussion](https://github.com/RyanCodrai/turbovec/issues/112)
- [turbovec #201](https://github.com/RyanCodrai/turbovec/issues/201), [#202](https://github.com/RyanCodrai/turbovec/issues/202), [#311](https://github.com/RyanCodrai/turbovec/issues/311)
- [PyPI turbovec JSON (latest 1.0.0)](https://pypi.org/pypi/turbovec/json)
- [TurboQuant paper](https://arxiv.org/abs/2504.19874)
- Literature cited from memory and not fetched this session (UNVERIFIED links):
  - [SOAR](https://arxiv.org/abs/2404.00774)
  - [RaBitQ](https://arxiv.org/abs/2405.12497)
  - [ACORN](https://arxiv.org/abs/2403.04871)
- Local evidence:
  - D:\DEV\_scratch\probe-item3a\ (probe_fixed_cost, probe_ivf_batch, probe_gil, probe_intersect, probe_klen, each .py with its .log);
  - D:\DEV\raggio\docs\adr\0002-optional-ivf-index.md:22;
  - D:\DEV\raggio\docs\benchmark-arxiv.md:83-100.

## User decisions
- Adopt the interim thread fan-out in _IvfIndex.search as item 3's fix now, rather than waiting for upstream. Recommend yes: it gives identical results with an estimated 1.5-1.8x concurrent QPS.
- Name and default of the fan-out knob. Recommend env IVF_SEARCH_THREADS, defaulting to min(32, available CPUs) (20 on the DGX); 1 restores today's exact serial path. Also decide whether to document RAYON_NUM_THREADS interplay.
- Include the filtered-path fix (shard-side intersection plus sorted allowlist cache) in item 3's scope. Recommend yes: about 20 LOC, and filtered IVF p50 is currently 72 ms against 29 ms flat.
- Query grouping by shared shard (T5). Recommend deferring unless the DGX fan-out log shows a mean group size of 2 or more; NEON nq=2-3 gets no amortization.
- Default nprobe. Recommend keeping 16 unless the arXiv sweep shows nprobe 12 at recall of 0.99 or more with at least 10% more QPS. This is a recall/speed product call.
- How to correct the '~0.4 ms fixed cost' narrative. Recommend a new ADR 0004 (IVF fan-out and corrected root cause), plus edits to docs/concepts.md, docs/indexing.md and store.py comments, leaving the accepted ADRs' history intact apart from a pointer.
- Whether and when to file upstream turbovec issues (agents must not post). Recommend in this order: E1 search_many (with allowlist intersect mode), expose ids(), E2 nq=2-3 tile padding. Defer the native IVF (A) until E1 lands, and do not propose SOAR or graph indexes.
- Whether pipelining two micro-batches (T6-style) is in scope. Recommend no: under closed-loop concurrency 8 it has little to overlap, and the non-kernel cycle cost dominates after fan-out.
- Whether to revisit auto nlist (about 8192 rows per shard), whose rationale rested on the fixed-cost premise. Recommend keeping 256 at 2.55M for now, because Python dispatch cost grows with task count; revisit only once E1 exists.

## Unverified
- DGX CPU memory bandwidth: single core about 20-38 GB/s (Chips and Cheese, plus raggio's derived 20.7 GB/s); all-core about 100-135 GB/s (forum lmbench, raggio flat about 94 GB/s). Not measured by me.
- DGX per-call fixed cost of an inline nq=1 IdMapIndex.search on a 10k-row d=1024 shard, assumed to be tens of µs by analogy with the Axion H93 figure of 0.014 ms (multi-threaded path).
- Which host and configuration produced the original '~0.4 ms fixed cost per shard' figure (ivf-probe.log is not in the repo).
- The 20.7 GB/s derivation assumes the ADR 0002 scaled-run timings are kernel-dominated single-thread nq=1 scans of 768 B/row codes.
- The concurrency cycle model: about 31 ms of non-kernel overhead per 8-query cycle, derived from flat 129 QPS and the Axion (not GB10) H101 nq=8/nq=1 pass ratio.
- DGX estimates after the interim fix: concurrent QPS about 175-205, serial p50 about 8-8.5 ms, filtered p50 about 12-16 ms.
- Probe overlap and group-size distribution on real arXiv queries; the laptop simulation used uniform random probes.
- Fraction of the corpus matched by the bench's mode-year filter (assumed about 10%, i.e. about 255k ids).
- Status of turbovec #203 (seeded top-k threshold): closed as completed on GitHub, but no such API found in the 1.0.0 source.
- Whether turbovec on GB10 (X925/A725) selects the vm8/i8mm SMMLA path.
- LOC estimates for E1, A, B, C and D, and the speed, recall and memory figures for B (SOAR), C (graph) and D (RaBitQ-IVF), which come from literature and reasoning rather than measurement.
- Breakdown of the ~70-85 µs intercept per nq=1 call on the AVX2 laptop (pad-to-4 kernel vs query prep vs binding); not profiled.
- Literature URLs for SOAR, RaBitQ and ACORN were cited from memory and not fetched in this session.
