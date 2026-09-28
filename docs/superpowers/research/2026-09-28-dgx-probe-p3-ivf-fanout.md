# DGX probe p3 — IVF fan-out variants on aarch64 (2026-09-28)

This probe ran on gn100 from 08:43 to 09:35 CEST. The host is a GB10 with two core types:

- X925 big cores: cpus 5–9 and 15–19;
- A725 little cores: cpus 0–4 and 10–14.

The baseline image is `localhost/raggio:baseline` at 19a8a2e, running Debian python 3.12.12 with SQLite 3.40.1.

**Every run is warm; there are no cold runs.** Runs are capped at `--memory 4g` unless marked UNCAPPED. Values are the median, with [min..max] over the stated reps.

Raw evidence is complete on the DGX in `~/probe/p3-ivf-fanout/{scripts,srv,logs,out}`. The laptop copy in `D:/DEV/_scratch/dgx-probes/p3-ivf-fanout/` is **incomplete**, because the Bash tool stopped working at about 09:35 (see Caveats).

**The prototype is not Plan F's code.** It uses a module-level `ThreadPoolExecutor`, not Plan F's per-collection `_ShardPool`. The in-process harness shims `Collection` with cfg, index and a read-only thread-local `_rdb`. It does not run the app.

## Key numbers

### Index facts (data-ivf)

- **Index:** nlist 256, nprobe 16, 2,549,119 rows.
- **Shard rows:** min 44, p50 9,199, p90 14,925, max 30,443.
  - No shard reaches 32,768 rows, so **every nq=1 call runs inline on the calling thread**, not in turbovec's rayon pool.
- **Allowlist:** the `year=2024` filter selects 212,331 ids (8.3 %).
  - The SQL build takes 963–1,056 ms once per `_allow_cache` miss.
  - The server's first filtered call took 987–1,045 ms in every config.

### Kernel costs (synthetic, capped, k=50)

**Fixed cost per nq=1 call.** About 28–30 µs per call plus about 11 ns per row: 136–141 µs at 10k rows, 422 µs at 32k.

**Batched calls are not amortized.** Per-query cost of an nq>1 call relative to nq=1, over 1,500 calls per point:

| nq | 8,192 rows | 10k rows |
|---|---|---|
| 2 | 2.05× | 2.61× |
| 3 | 4.89× | 1.95× |
| 8 | 0.87× | 0.82× |

Break-even comes only at nq=8. Batched rows are bit-identical to nq=1.

**GIL scaling.** At 10k rows, nq=1 calls per second:

| T | calls/s |
|---|---|
| 1 | 6,752 |
| 8 | 40,850 |
| 12 | 41,907 (peak) |
| 20 | 34,342 |

**ivf_batch** (UNCAPPED, because the capped run was OOM-killed). 256 shards of 10k rows, 8 queries × 16 probes, in ms per batch:

| Variant | ms/batch |
|---|---|
| serial | 27.80 |
| grouped | 35.57 |
| fan_q1 T=8 | 7.51 (3.70×, the best) |
| fan_q1 T=12 | 8.35 |
| fan_grp, best | 8.66 (T=12) |

**Shard-side intersect**, 16 shards, current vs shard-side:

| Allowlist size | current | shard-side |
|---|---|---|
| 25.5k | 3.7 ms | 1.57 ms |
| 255k | 36.3 ms | 2.40 ms |
| 765k | 112 ms | 3.35 ms |

### Exactness (real data, capped)

**Unfiltered.** Seed 7, 500 nq=1 rows plus 119 mixed batches (nq 1..8) at raw k=50.

- q1:1–20, grp:1–20 and serial/ss return ids and float32 score bits identical to serial, including 3 exact-tie pairs.

**Filtered.** Every filtered variant is identical to serial.

**Rescored top-10.** 0 mismatches in every pass.

**Recall@10** on seed 42 is 0.9928 for every variant. The server shows 0.993 in every config.

### In-process concurrent throughput (seed 42, 8 concurrent micro-batched, 5 reps)

The two BLAS columns differ in `OPENBLAS_NUM_THREADS`: the default (unset) vs `OPENBLAS_NUM_THREADS=1`.

| Variant | QPS, default BLAS | QPS, OPENBLAS=1 |
|---|---|---|
| serial | 241.5 [240.4..243.8] | 242.9 |
| q1:8 | 549.3 | — |
| **q1:12** | **610.4 [607.0..626.5] (2.53×)** | **661.7 [595.8..687.1] (2.72×)** |
| q1:16 | 522.5 | 668.2 |
| q1:20 | 508.6 | 650.0 |
| grp:1 | 148.2 | — |
| grp, best | 570.6 (grp:16) | 640.7 (grp:20) |

Serial-issue p50, in ms:

| BLAS | serial | q1:12 |
|---|---|---|
| default | 8.18 [3.89..8.19] | 3.03 |
| OPENBLAS=1 | 4.02 | 2.66 |

The seed-7 tuning (5 reps) independently picks T=12 in both BLAS settings:

| BLAS | q1:12 | q1:16 | q1:20 |
|---|---|---|---|
| default | 589.2 | 508.3 | 506.3 |
| OPENBLAS=1 | 690.3 | 642.5 | 644.9 |

### Filtered path, in-process (seed 42, 200 queries, 3 reps)

p50 in ms:

| Variant | default BLAS | OPENBLAS=1 |
|---|---|---|
| serial, current intersect | 31.93 | 32.40 |
| serial/ss (shard-side intersect) | 3.94 | 3.98 |
| fan-out, intersect **up front** on the caller (Plan F's `allow_for`/`cut`) | 6.4–7.0 | 3.87–4.39 |
| **q1:8/ss, intersect inside each task** | **3.68 [3.64..3.70]** | **2.20 [2.11..2.47]** |
| q1:8, current intersect inside the task | 17.71 | about 9.4 (seed 7) |

### Latency attribution (store serial loop, seed 42, median ms)

| Query type | Total | Breakdown |
|---|---|---|
| nq=1 vector | 3.855 | kernel 3.532 over 16 calls; other in-process work about 0.33 |
| nq=8 batch | 30.47 | kernel 28.10 over 128 calls, issued one after another |
| filtered, current intersect | 31.85 | **intersect 29.78**, kernel 1.556 |
| filtered, shard-side intersect | 3.885 | intersect 2.043 |

**Real-shard kernel under T concurrent threads**, nq=1, 8,000 calls, in calls/s:

| T | calls/s | median per call |
|---|---|---|
| 1 | 4,500 | 209 µs |
| 8 | 16,825 | — |
| 12 | 19,239 | 458 µs |
| 16 | 18,571 | — |
| 20 | 18,345 | 795 µs |

With `OPENBLAS_NUM_THREADS=1` the peak is 20,056 calls/s at T=16.

### Server A/B (real app over HTTP, capped 4g, warm)

Setup:

- The client is bench-like and runs on the same host.
- 2 interleaved rounds × 3 passes = 6 passes per config.
- The concurrent phase uses c=8.

F12 is the prototype with fan-out at T=12 and the shard-side intersect done inside each task.

| Config | serial p50 ms | concurrent QPS | filtered p50 ms |
|---|---|---|---|
| base (19a8a2e) | 11.27 [11.22..11.30] | 165.3 [125.0..196.1] | 71.1 [36.6..71.6] |
| base + OPENBLAS=1 | 8.44 [7.46..9.90] | 189.2 [162.8..207.3] | 36.6 [35.3..37.3] |
| F12, default BLAS | 6.15 [6.05..6.19] | 203.0 [200.7..205.8] | 6.84 [6.78..6.98] |
| **F12 + OPENBLAS=1** | **6.50 [6.12..6.76]** | **324.7 [308.6..381.4] (+96 %)** | **6.41 [6.24..7.22]** |

**Control.** F1cur-check is the patched image at T=1 with the current intersect, run for 3 passes. It reproduces base: 11.25–11.29 ms, 141.9–181.7 QPS, filtered 36.6–71.6 ms.

**Memory.** The server cgroup memory peak is 1.82 GB in every config, so the fan-out adds no memory under the 4 GiB cap.

### Server bottleneck diagnosis

1 run per config, 2 reps per concurrency level. QPS at client concurrency c=1/8/16/32:

| Config | c=1 | c=8 | c=16 | c=32 | Where the CPU goes |
|---|---|---|---|---|---|
| F12 + OPENBLAS=1 | 150 | 302 | 345–370 | 374–408 | event loop 0.35–0.48 cores; other threads 2.5–3.6; client 0.3–0.6. Nothing is saturated. |
| F12, default BLAS | 151 | 200 | 222 | 232 | other threads at 16.4–16.8 cores **already at c=1**: OpenBLAS spinners, 1.00 core each |
| base + OPENBLAS=1 | 85 | 205 | 225 | 210–217 | the drain uses about 0.9 cores (a single thread) |

**HTTP and client overhead** is about 3.1 ms per request. It is the server serial p50 minus the in-process serial-issue p50:

- F12: 6.15 − 3.03 ms;
- base: 11.27 − 8.18 ms.

Event-loop CPU is 1.2–1.5 ms per request.

### OpenBLAS spinners and core type

With the default setting, numpy's scipy-openblas starts 20 pthreads that busy-spin. Under any search load they keep about 15–16 cores busy and push the query and event-loop threads onto the A725 little cores.

**Core-type effect.** In-process, OPENBLAS=1, pinned via `sched_setaffinity`, 3 reps:

| Core | serial ms | filtered-ss ms | filtered-cur ms |
|---|---|---|---|
| cpu5 (X925) | 4.06–4.08 | 4.11–4.51 | 32.5–32.6 |
| cpu0 (A725) | 8.31–8.33 | 8.49–9.43 | 69.5 |

This bimodality explains three observations:

- the baseline's 4 vs 8 ms serial p50;
- the baseline's 36 vs 70 ms filtered p50;
- a large part of the 16–24 % vector noise band.

### Rayon pool

**Pool size.** In-process 8-concurrent QPS at T=12, seed 42, OPENBLAS=1:

| Setting | q1:12 | grp:12 |
|---|---|---|
| default (20 threads) | 661.7 | 601.8 |
| `RAYON_NUM_THREADS=4` | 593.1 | 465.7 |
| `RAYON_NUM_THREADS=1` | 585.3 | 324.0 |

The q1 differences are within the between-run noise. The grp drops are real.

**Big shards.** Synthetic, capped, OPENBLAS=1, 16 shards:

- **10k-row nq=1, inline path:** peaks at 23.6k calls/s at T=12, and 22.9k with `RAYON_NUM_THREADS=1`.
- **40k-row nq=1, pooled path:**
  - 1,436 calls/s at T=1 and 3,525 at T=12, a **2.45× ceiling**;
  - with `RAYON_NUM_THREADS=1`: 827 at T=1, then flat at about 1,440 from T=2 on.

## Conclusions

**D3 is settled on aarch64: plain per-(query, shard) nq=1 fan-out, and no audience batching.**

- Grouping never beats q1 beyond noise: best grp 570.6 vs q1:12 610.4 QPS with default BLAS, and 640.7 vs 661.7 with OPENBLAS=1.
- grp:1 without threads is a **39 % regression**.
- grp depends on turbovec's rayon pool.
- The mean audience group at nq=8 and nprobe 16/256 has only 1.40 queries. That lands in the unamortized nq=2–3 regime, at 2–5× the per-query cost.

**The default thread count is 12, not `min(32, cpu_count)`.**

- T=12 was picked on seed 7 and held on seed 42.
- On this 20-core host `min(32, …)` resolves to T=20, which loses 14–17 % with default BLAS and is never better with OPENBLAS=1.
- Kernel throughput plateaus at T=12, at 19.2k–19.6k calls/s.

**The gain clears the noise band, with one condition.**

- In-process the gain is 2.5–2.7× concurrent QPS and −63 % serial p50.
- At the HTTP server, serial p50 (−45 %) and filtered p50 (−90 %) clear the band easily.
- Concurrent QPS clears it only with `OPENBLAS_NUM_THREADS=1`: +96 %, with F12's minimum of 308.6 above base's maximum of 196.1.
- With default BLAS it is +23 % on medians and only 5 % max-over-max, which is inside the 16–24 % band.

**Expected after Plan F on gn100** at the server, c=8, capped, warm:

| Metric | Today | After Plan F |
|---|---|---|
| IVF concurrent QPS | about 165 (125–196) | about 325 (309–381) with OPENBLAS=1; about 203 with default BLAS |
| filtered p50 | 36.6–71.6 ms | 6.4–6.8 ms |
| vector serial p50 | 11.3 ms | 6.2–6.5 ms |
| recall@10 | 0.993 | 0.993 (unchanged) |

**Where the filtered-path intersect runs matters.**

- The shard-side intersect is the dominant filtered win: 29.8 ms becomes 2.0 ms.
- Done inside each fan-out task it gives 3.7 ms (default BLAS) or 2.2 ms (OPENBLAS=1).
- Done up front on the caller, as Plan F's `allow_for`/`cut` does, it gives 6.4–7.0 or 3.9–4.4 ms. That is no better than serial/ss.
- Filtered searches are always nq=1, so sharing `cut` across queries buys nothing.

**Latency attribution confirms 3b's "serial drain" for unfiltered search.** In an nq=8 batch, 128 kernel calls issued one after another take 28.1 of the 30.5 ms. 3a's "31 ms non-kernel overhead" is real in size, but it is specifically `_IvfIndex._intersect` on the filtered path, not general overhead.

**After Plan F, the server limit is the HTTP and event-loop pipeline, not the kernel.**

- Each request costs about 3.1 ms, of which 1.2–1.5 ms is loop CPU.
- One drain batch is in flight at a time, and the client is closed-loop.
- Nothing is CPU-saturated, and QPS keeps rising with client concurrency (408 at c=32).

**Rayon oversubscription is not an issue today, but there is a cliff.**

- Every list is under 32,768 rows, so nq=1 runs inline.
- A list at or above 32,768 rows goes through the pooled path. There all T fan-out threads share one 20-thread rayon pool, and the ceiling falls to about 2.45×.
- The largest list is already 30,443 rows, 93 % of the threshold.
- `RAYON_NUM_THREADS=1` makes pooled calls flat at about 1,440/s from T=2 and must never be set.

**New finding: OpenBLAS spinners are the main DGX noise source.** numpy's default scipy-openblas has 20 busy-spinning threads that burn about 16 cores under any search load. They push latency-critical threads onto A725 cores. They cause the bimodal baseline numbers and cap Plan F's server QPS at about 200 instead of about 325.

## Plan impacts

1. **Spec §4.2 and Plan F Global Constraints.** Change the `IVF_SEARCH_THREADS` default from `min(32, os.cpu_count())` to `min(12, os.cpu_count())`. Drop Plan F's rule that the default "stays min(32, …) whatever seed 7 says". Update `test_settings_ivf_search_threads` and the README/getting-started env rows to match.
2. **Plan F Task 1, `_IvfIndex.search`.**
   - Intersect the allowlist inside the pool task, per (query, shard), instead of computing `allow_for(j)`/`cut` up front on the calling thread.
   - Keep the tiny-allowlist (≤128) owners path.
   - In-task intersection was verified bit-identical, including tie order.
3. **Plan F Task 2, shard-side `_intersect` with a sorted allowlist at cache fill.** Keep it as designed; it is the dominant filtered win. The SQL build already returns a sorted allowlist on this corpus, but `np.sort` at cache fill is cheap and keeps the invariant.
4. **Plan A's Dockerfile and the spec §4.2 knob list.** Add `ENV OPENBLAS_NUM_THREADS=1` to the image, with owner A, or F if A has already merged.
   - Without it, F's concurrent-QPS acceptance is marginal: +23 %, inside the band.
   - With it: +96 %, and the core-type bimodality disappears.
   - **Not measured:** IVF train/attach (k-means matmuls), CREATE INDEX, ingest, and text/hybrid search with it. Measure these before shipping, or restrict BLAS threads to the search path (threadpoolctl).
5. **Plan A's baseline protocol.**
   - Record that the 16–24 % noise band and the filtered 44–70 ms spread come mainly from the OpenBLAS spinners plus X925/A725 placement.
   - Fix `OPENBLAS_NUM_THREADS` to the same value in both arms of every A/B.
   - When attributing single-thread latency, report the big-core fraction or pin in-process.
6. **D3 and the Plan F probe (`bench/ivf_fanout_probe.py`).** D3 is settled: q1 fan-out, and audience batching is not shipped. Keep the probe's grp arm only as a regression guard, or drop it.
7. **Plan F `_ShardPool` docstring and ADR 0005.**
   - Document the 32,768-row pooled-path cliff.
   - Add a bench/health check of the maximum list size.
   - Consider sizing nlist to keep lists under the threshold as collections grow.
   - State that `RAYON_NUM_THREADS=1` must never be set.
8. **Spec §7, F acceptance.**
   - Expect about 325 QPS at c=8 (408 at c=32) at the server, against an in-process ceiling of 610–662.
   - The remaining limit is the HTTP/event-loop pipeline. Follow-up candidates, outside plans A–G:
     - allow two `_drain_scans` batches in flight;
     - move `_hydrate` off the loop;
     - parse the vector body more cheaply.

## Caveats

**Failures**

- `probe_ivf_batch.py` at `--memory 4g` was OOM-killed (exit 137) while it built 256 synthetic 10k-row shards. It was rerun UNCAPPED. All real-data, server and other synthetic runs were capped at 4g.
- `--cpuset-cpus` pinning failed (exit 127, `cpuset.cpus: no such file`), because rootless podman delegates only the cpu, memory and pids controllers. Pinning was redone in-process with `os.sched_setaffinity`.
- At about 09:35 CEST the Bash tool stopped executing anything, and a sandbox-disabled retry was denied and not pursued. As a result:
  - the laptop copy lacks the bigfrac/pin/rayon/bigshard logs, the `run_*.out` files, all `out/*.json` and `srv/store*.py`;
  - `logs/real-blas1-42.log` is a partial copy;
  - the end host state was not recorded.

**Measurement limits**

- The seed-7 tuning sample contains only ingested rows. It was used only for timing and for picking T. Recall and the reported numbers use seed 42.
- The in-process QPS of 610–662 is a ceiling, because the harness does not run HTTP. The server reaches 203–408.
- The server client shares the host's CPUs, including with the OpenBLAS spinners in the default-BLAS configs.
- The explanation of the server gap as "serial drain plus event-loop pipeline" is inferred from per-thread CPU sampling, not from a per-stage trace.
- DGX noise:
  - in-process run-to-run spread is about 10 %;
  - base concurrent QPS spread is 125–196 within the session;
  - the rayon1/rayon4 q1 drops are within that noise, with no repeat run.
- Plan F's own filtered design, with the intersect up front via `cut`, was measured only in-process, not at the server. The server prototype used the intersect inside each task.
- Every number is warm (host Cached about 38 GB).

## DGX cleanup state

**Incomplete.** The final cleanup command's result could not be observed. Last verified state, at 09:35:04 CEST:

- no `probe-*` containers;
- `bench-tv` Exited and untouched;
- the `bench-tv` and `bench-wv` volumes, `~/turborag` and `~/raggio` were only read;
- `~/probe/data-ivf` was only mounted read-only.

Assume these are **still present** until verified:

- the image `localhost/probe-p3-fanout`. Remove it with `podman rmi localhost/probe-p3-fanout`.
- `~/probe/p3-ivf-fanout/data-x`, a 16 GB copy of data-ivf owned by uid 100999. Remove it with `podman unshare rm -rf ~/probe/p3-ivf-fanout/data-x`.
