# Benchmark: arxiv abstracts, end-to-end A/B (2026-10)

The [arxiv benchmark](benchmark-arxiv.md) was measured on 2026-08-23. Since then
the code has changed a lot (the 2026-09 performance work: ingest path, cold
start, native BM25, IVF shard fan-out, memory hygiene). This page is the
re-measurement of raggio alone, end to end, on the same corpus and host: the
**current code** against the **original baseline**, both run from container
images built from the repo's Dockerfile, in one interleaved session.

- **A, baseline:** commit `19a8a2e` (2026-09-02, the code the 2026-09 work
  started from).
- **B, current:** commit `19c3293`.

Weaviate was not re-run. Its figures in the other tables stay those of the
2026-08-23 run.

## Setup

- **Host:** NVIDIA DGX Spark (GB10, 20 Arm cores: 10x Cortex-X925 + 10x
  Cortex-A725), about 121 GiB of RAM, native Linux, rootless podman. Nothing
  else ran on it during the measurement; one boot, no reboot.
- **Data:** the [arxiv corpus](benchmark-arxiv.md#setup), 2,549,119 chunks x
  1024 dims, 4-bit, one collection.
- **Containers:** raggio in a **4 GiB** container, one volume per arm. Serving
  runs use host-default swap (a 4 GiB cap allows 4 GiB more swap before an OOM
  kill); swap was sampled once per second and is reported below.
- **Bench:** `bench/bench.py` of `19c3293` drives both arms (the harness is newer
  than the baseline server, and compatible with it). Seed 42, 500 queries, 200
  filtered queries, k=10, concurrency 8 and 16. IVF is `nlist` 256, `nprobe` 16.
- **Software in the images:** see [What differs besides the code](#what-differs-besides-the-code).
  Run on 2026-10-03, 01:40Z to 07:15Z.

## Protocol in brief

1. **Ingest:** two full reingests per arm, ordered A B A B (the first on an
   empty volume, the second on a populated one), at 4 GiB.
2. **True-cold starts:** the volume's files are evicted from the page cache
   (`posix_fadvise(DONTNEED)`, residency checked), a fresh 4 GiB container
   starts, and the harness times start to the first answered
   `GET /collections/bench`, probes `/healthz` every 0.25 s during the load, and
   times three later GETs. Six flat and four IVF starts per arm.
3. **Search sessions:** eight sessions in the order A1, A1-c16, B1, B1-c16, A2,
   A2-c16, B2, B2-c16. Each session builds the IVF index (4 GiB cap), measures
   an IVF cold start, runs one discarded run0 plus three measured runs of
   `raggio-ivf`, detaches the index back to flat, then runs run0 plus three
   measured runs of `raggio`, and ends with a flat cold start. Every run uses a
   fresh 4 GiB container. Concurrency 8 in the first session of each arm, 16 in
   the second.
4. **Statistics:** medians over the measured runs of both sessions of an arm, so
   6 runs per arm, engine and concurrency. The exception is the baseline's flat
   concurrency-8 rows, which pool 5 (the first baseline session ran 2 flat runs
   instead of 3; found and fixed after that session). A "band" is the max minus
   min of an arm's runs; a difference counts as real only when it exceeds both
   arms' bands. Rows marked *within noise* below do not.

## Results

All figures are medians. Concurrency 8 unless stated. "Baseline" is A, "current"
is B. Latencies are server round trips over REST, as in the other benchmark
pages.

### Ingest, cold start and index build

| Metric | baseline | current |
|---|---|---|
| Reingest, 2.55M vectors, 4 GiB (vec/s) | 1,362 | **3,174** |
| Reingest wall time (s) | 1,871 (31 min) | **803 (13 min)** |
| True-cold start, flat: start to first GET (s) | 155.0 (114.2 to 156.4) | **4.6** (4.2 to 4.8) |
| True-cold start, IVF: start to first GET (s) | 154.5 (118.0 to 155.7) | **5.2** (5.1 to 5.3) |
| `/healthz` during that load | no answer within 5 s, every start | answers in 0.01 s |
| Later `GET /collections/bench` after a cold start, flat (s) | 52.0 | **0.50** |
| IVF index build at 4 GiB (s) | 391 | **178** |
| IVF to flat detach (s) | 295 (at 8 GiB, the baseline refuses 4 GiB) | **102** (at 4 GiB) |

Ranges in brackets are min to max over the starts. The baseline's first two
cold starts, right after its ingests, took 114 to 115 s and the later ones 155 s.

### Flat scan, concurrency 8

| Metric | baseline | current |
|---|---|---|
| Search p50 / p95 / p99 (ms) | 19.5 / 22.0 / 23.0 | 18.5 / 19.6 / 20.3 (p50 within noise) |
| QPS serial | 51 | 54 (within noise) |
| QPS concurrent | 111 | 137 (within noise, bimodal) |
| p95 under concurrency (ms) | 80.9 | 69.4 |
| Filtered p50 / p95 (ms) | 25.7 / 29.6 | 24.7 / 29.6 (within noise) |
| Recall@10 vs exact | 1.000 | 1.000 |
| Hybrid p50 / p95 / p99 (ms) | 87.0 / 162.5 / 570.3 | **48.2 / 72.6 / 100.5** |
| Hybrid QPS serial | 10.0 | **20.5** |
| Hybrid QPS concurrent | 14.0 | **36.1** |
| Hybrid p99 under concurrency (ms) | 3,545 | **471** |
| Hybrid text-hit@10 | 0.984 | 0.984 |
| Server CPU per hybrid query under concurrency (ms) | 855.5 | **337.3** |
| Memory after a restart / under query load (MB) | 1,547 / 1,550 | 1,502 / 1,506 |
| Disk footprint (MB) | 16,469 | 16,463 |

Flat concurrent QPS ranges from 105 to 186 for the baseline and from 126 to 174
for the current code, so the two overlap. The flat scan's vector search did not
change in this period.

### IVF index, concurrency 8

| Metric | baseline | current |
|---|---|---|
| Search p50 / p95 / p99 (ms) | 10.9 / 12.2 / 12.8 | **6.1 / 7.7 / 8.4** |
| QPS serial | 90 | **161** |
| QPS concurrent | 191 | **380** |
| p95 under concurrency (ms) | 77.0 | **25.0** |
| Server CPU per vector query under concurrency (ms) | 50.0 | **9.7** |
| Filtered p50 / p95 (ms) | 37.2 / 74.5 | **5.9 / 7.0** |
| Recall@10 vs exact | 0.993 | 0.994 (within noise) |
| Hybrid p50 / p95 / p99 (ms) | 86.6 / 158.4 / 555.5 | **44.9 / 69.7 / 95.5** |
| Hybrid QPS serial | 9.8 | **22.0** |
| Hybrid QPS concurrent | 13.4 | **38.1** |
| Hybrid p99 under concurrency (ms) | 3,884 | **437** |
| Hybrid text-hit@10 | 0.984 | 0.984 |
| Server CPU per hybrid query under concurrency (ms) | 955.1 | **202.7** |
| Memory after a restart / under query load (MB) | 1,506 / 1,511 | 1,506 / 1,513 |
| Disk footprint (MB) | 16,758 | 16,751 |

### Concurrency 16

| Metric | baseline | current |
|---|---|---|
| IVF QPS concurrent | 198 | **395** |
| IVF p95 under concurrency (ms) | 130.1 | **61.9** |
| IVF hybrid QPS concurrent | 12.3 | **37.6** |
| IVF hybrid p99 under concurrency (ms) | 7,258 | **899** |
| Flat QPS concurrent | 182 | 202 (within noise) |
| Flat hybrid QPS concurrent | 12.6 | **34.4** |
| Flat hybrid p99 under concurrency (ms) | 7,108 | **989** |

### What did not improve, and one that got worse

- Flat vector search, flat filtered search, recall and the serving footprint
  (memory about 1.5 GB, disk 16.5 GB flat and 16.8 GB with the index) are the
  same within noise.
- **Memory right after a fresh ingest, in the container that ingested** (no
  restart), is higher: 1,611 MB (1,608 and 1,614 in the two ingests) for the
  current code against 513 MB (574 and 451) for the baseline. After a restart both
  arms read about 1.5 GB. The baseline pages out heavily during its 4 GiB ingest
  (below), so this row measures paging as much as the allocators; it is reported
  as measured, not explained. Right after ingest and under query load, the
  baseline reads 1,772 MB and the current code 1,618 MB.

### Swap and OOM

No container was OOM-killed, no step needed more than the 4 GiB cap, and no
measured serving run swapped on either arm (peak swap 0 in all of them).
Per stage, peak swap from the 1 Hz sampler (MiB):

| Stage | baseline | current |
|---|---|---|
| Ingest (4 GiB), two ingests | 1,359 and 1,501 | 2 and 6 |
| IVF build (4 GiB), four builds | up to 2,902 | up to 58 |
| IVF to flat detach | 0 (at 8 GiB) | up to 774 (at 4 GiB) |
| Serving runs (4 GiB) | 0 | 0 |

## What differs besides the code

The two arms are the images as built, not equalised: the comparison is "what a
user gets", and no row isolates a single change.

| | A, baseline (`19a8a2e`) | B, current (`19c3293`) |
|---|---|---|
| Base image | Debian 12, glibc 2.36 | Debian 13, glibc 2.41 |
| CPython | 3.12.12 (GCC 12.2) | 3.12.15 (Clang 22.1.3, uv python-build-standalone) |
| SQLite | 3.40.1 (system) | 3.53.1 (bundled) |
| `OPENBLAS_NUM_THREADS` | unset | 1 |
| glibc malloc | defaults | `MALLOC_TRIM_THRESHOLD_=134217728`, plus `malloc_trim(0)` after every index job |
| BM25 scorer | SQLite FTS5 path only | native Rust extension ([ADR 0004](adr/0004-native-bm25.md)) |
| turbovec | 1.0.0 | 1.0.0 (same binary) |
| Dependencies | FastAPI 0.141.1, uvicorn 0.52.4 | FastAPI 0.142.2, uvicorn 0.54.0 (minor bumps) |
| `meta.db` indexes | before the 2026-09 covering index | the covering index, created by that arm's own ingest |
| Detach cap | 8 GiB | 4 GiB |

Each arm ran on a volume its own server wrote: no volume was shared across
versions, so the one-time `meta.db` migration ([storage](storage.md#upgrading))
is not measured here.

## Caveats

- **The images differ beyond the code** (table above). The hybrid and concurrent
  rows in particular combine the native BM25 scorer and the single BLAS thread
  with the code changes; the harness cannot separate them.
- **The baseline swapped during ingest and during index builds at 4 GiB.** Its
  anonymous memory reaches 1.8 GiB (ingest) and 3.2 GiB (build) plus page cache
  against the cap, so its ingest (1,362 vec/s) and build (391 s) timings include
  swap-out. The current code is almost swap-free in both. With a swapless
  container the baseline might have been killed instead.
- **The detach caps differ.** The baseline's memory-headroom guard
  ([ADR 0001](adr/0001-performance-optimization-decisions.md)) refuses an IVF to
  flat detach at 4 GiB in earlier baseline runs, so it ran at 8 GiB here.
  The detach row is not like for like; the cap is itself part of the result.
- **True-cold means the volume's files only.** Eviction drops the volume's page
  cache; the image layers (interpreter, libraries) stay cached after the first
  container, and the host is not rebooted. Without eviction (host-warm), the
  bench's own restart-to-first-search figure is 4.6 to 4.8 s for the baseline and
  2.0 to 2.6 s for the current code (flat).
- **"Cold start" here is not the 2026-08-23 row.** The earlier "Cold start to
  first query" (30.4 s flat) timed a container restart to the first vector
  search, without evicting the file cache. The figures above time container start
  to the first answered `GET /collections/bench`, which loads the collection and
  counts its chunks, so it is a slightly heavier first request than a search
  (host-warm, flat, in one session of the current code: 3.1 to 3.2 s for the first
  GET against 2.0 to 2.6 s for the bench's first search). The baseline measured under this method
  takes 155 s, not 30.4 s, so the two methods are not interchangeable.
- **Concurrency and run counts.** The flat concurrent-QPS rows are bimodal and
  within noise. The baseline's flat concurrency-8 rows pool 5 runs, not 6. The
  concurrency-8 and 16 sessions are not independent samples of one arm (same
  volume and host state).
- **One host, one boot, one day.** The ABAB order and the repeats control drift,
  not machine-to-machine variation.
- **No Weaviate re-run.** Comparisons with Weaviate use its 2026-08-23 figures.
