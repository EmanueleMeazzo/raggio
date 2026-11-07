# Benchmark: arxiv abstracts, 0.3.0 re-measurement (2026-11)

The [end-to-end A/B of 2026-10](benchmark-arxiv-2026-10.md) measured commit
`19c3293`. Release 0.3.0 followed (PRs #6–#14: relative fusion, `doc_id` and
`contains_any` filters, exact small allowlists, a document-scoped BM25 leg,
metadata keys in BM25, native JSON parsing for ingest and search requests,
direct response encoding, the `unix-excl` SQLite VFS, a data-directory lock and
warm scoped-search batching;
see the [ADR index](adr/index.md)). This page re-measures 0.3.0 against
`19c3293` with the same protocol, on the same corpus and host.

- **A:** commit `19c3293`, the image measured on 2026-10-03.
- **B:** commit `f1b0ab3`, `main` on 2026-11-06. Its runtime is 0.3.0 as merged
  in `2e8082b`; the later commits change documentation only.

**Result: over six runs per arm, no figure moved beyond the run-to-run
spread**, for flat or IVF search, at 8 or 16 clients: vector, filtered and
hybrid latency and throughput, CPU per query, recall, text-hit rate, memory,
disk, cold start, index build and detach. Ingest throughput (two runs per arm)
is within the spread too; three two-run rows measured right after an ingest are
not ([below](#right-after-a-fresh-ingest)). The current figures in
[Benchmark: arxiv abstracts](benchmark-arxiv.md#results) and the README are
B's medians.

## Setup

- **Host:** NVIDIA DGX Spark (GB10, 20 Arm cores: 10x Cortex-X925 + 10x
  Cortex-A725), about 121 GiB of RAM, native Linux, rootless podman, one boot
  for the whole measurement.
- **Data:** the [arxiv corpus](benchmark-arxiv.md#setup), 2,549,119 chunks x
  1024 dims, 4-bit, one collection; each arm ingested it into its own volume.
- **Containers:** raggio capped at **4 GiB** for every serving run, build and
  detach, with the host's default swap (sampled once per second).
- **Bench:** `bench/bench.py` and `bench/dgx/` of `f1b0ab3`, byte-identical to
  `19c3293`'s, with the same two environment-variable patches as in 2026-10 (the
  volume and fingerprint directory per arm). Seed 42, 500 queries, 200 filtered
  queries, k=10, concurrency 8 and 16. IVF is `nlist` 256, `nprobe` 16.
- **Images:** built from the repo's Dockerfile with the same cached base layers.
  Both have CPython 3.12.15, SQLite 3.53.1, Debian 13 (glibc 2.41), numpy 2.5.3,
  turbovec 1.0.0, `raggio-native` 0.1.0, fastapi 0.142.2, pydantic 2.13.5,
  uvicorn 0.54.0 and the same image environment (`OPENBLAS_NUM_THREADS=1`,
  `MALLOC_TRIM_THRESHOLD_`). They differ in `src/` only (and the package version,
  0.1.0 against 0.3.0); B's `src/` hash matches the commit's.
- **Run:** ingests and cold starts on 2026-11-06, 09:49Z to 10:51Z; the search
  sessions on 2026-11-06/07, 23:38Z to 01:48Z (see
  [the first attempt](#the-first-attempt-at-the-search-sessions)).

## Protocol in brief

As [in 2026-10](benchmark-arxiv-2026-10.md#protocol-in-brief), except that both
arms detach the IVF index at 4 GiB (both are post-2026-09 images) and the search
sessions ran behind a host-CPU guard.

1. **Ingest:** two full reingests per arm, ordered A B A B, at 4 GiB.
2. **True-cold starts:** the volume's files evicted from the page cache, a fresh
   4 GiB container, start to the first answered `GET /collections/bench`. Two
   flat starts per arm here, plus one IVF and one flat start per session below.
3. **Search sessions:** eight, in the order A1, A1-c16, B1, B1-c16, A2, A2-c16,
   B2, B2-c16. Each builds the IVF index, takes an IVF cold start, runs one
   discarded run0 plus three measured runs of `raggio-ivf`, detaches the index,
   runs run0 plus three measured runs of `raggio`, and ends with a flat cold
   start. Every run uses a fresh 4 GiB container.
4. **Guard:** a 1 Hz sampler classed every process on the host as the bench's
   own, the raggio container, container runtime and port forwarding, kernel, or
   foreign. Before each session the run waited for 120 s with mean foreign CPU
   under 0.25 cores and no second over 1 core. A session counted only if its
   whole window had mean foreign CPU under 0.25 cores and no 10 s mean over
   1 core, a criterion fixed before the sessions ran; otherwise it was to be
   repeated. All eight passed at the first attempt (mean foreign CPU 0.013 to
   0.022 cores).
5. **Statistics:** medians over the measured runs of both sessions of an arm, so
   6 runs per arm, engine and concurrency. A difference counts as real only when
   it exceeds both arms' bands (max minus min of their runs).

## Results

All figures are medians, A then B. Concurrency 8 unless stated. Latencies are
server round trips over REST. Every row is within the spread unless marked. The
per-run values and both bands of every row are in `bench/results-arxiv-2026-11.md`.

### Ingest, cold start and index build

| Metric | 19c3293 | 0.3.0 |
|---|---|---|
| Reingest, 2.55M vectors, 4 GiB (vec/s) | 3,116 (3,124 and 3,107) | 3,378 (3,749 and 3,007) |
| Reingest wall time (s) | 818 | 764 |
| True-cold start, flat: start to first GET (s) | 4.8 (4.5 to 4.9) | 4.9 (4.7 to 5.1) |
| True-cold start, IVF: start to first GET (s) | 5.2 (5.0 to 5.4) | 5.2 (5.0 to 5.3) |
| Slowest `/healthz` answer during that load (s) | 0.01 | 0.01 |
| IVF index build at 4 GiB (s) | 180 | 183 |
| IVF to flat detach at 4 GiB (s) | 110 | 109 |

### Flat scan

| Metric | 19c3293 | 0.3.0 |
|---|---|---|
| Search p50 / p95 / p99 (ms) | 18.7 / 20.1 / 20.8 | 18.5 / 19.9 / 20.5 |
| QPS serial | 53 | 54 |
| QPS concurrent | 125 | 126 |
| p95 under concurrency (ms) | 70.3 | 69.4 |
| Server CPU per vector query under concurrency (ms) | 118.1 | 117.7 |
| Filtered p50 / p95 (ms) | 26.2 / 31.9 | 24.4 / 31.0 |
| Recall@10 vs exact | 1.000 | 1.000 |
| Hybrid p50 / p95 / p99 (ms) | 48.5 / 73.2 / 98.4 | 48.1 / 73.4 / 98.8 |
| Hybrid QPS serial / concurrent | 20.3 / 36.0 | 20.5 / 34.9 |
| Hybrid p99 under concurrency (ms) | 468 | 459 |
| Server CPU per hybrid query under concurrency (ms) | 338 | 345 |
| Hybrid text-hit@10 | 0.984 | 0.984 |
| Memory after a restart / under query load (MB) | 1,501 / 1,505 | 1,501 / 1,506 |
| Disk footprint (MB) | 16,463 | 16,463 |

Flat concurrent QPS is bimodal in both arms, as before: most runs near 125, a
few near 175.

### IVF index

| Metric | 19c3293 | 0.3.0 |
|---|---|---|
| Search p50 / p95 / p99 (ms) | 6.4 / 8.3 / 9.0 | 6.2 / 8.0 / 8.9 |
| QPS serial | 152 | 160 |
| QPS concurrent | 378 | 385 |
| p95 under concurrency (ms) | 24.5 | 25.5 |
| Server CPU per vector query under concurrency (ms) | 10.1 | 9.9 |
| Filtered p50 / p95 (ms) | 5.7 / 6.7 | 6.0 / 7.2 |
| Recall@10 vs exact | 0.994 | 0.994 |
| Hybrid p50 / p95 / p99 (ms) | 44.9 / 71.1 / 96.3 | 45.1 / 70.8 / 96.1 |
| Hybrid QPS serial / concurrent | 21.9 / 38.1 | 21.9 / 38.5 |
| Hybrid p99 under concurrency (ms) | 447 | 444 |
| Server CPU per hybrid query under concurrency (ms) | 203 | 200 |
| Hybrid text-hit@10 | 0.984 | 0.984 |
| Memory after a restart / under query load (MB) | 1,508 / 1,515 | 1,506 / 1,514 |
| Disk footprint (MB) | 16,751 | 16,751 |

### Concurrency 16

| Metric | 19c3293 | 0.3.0 |
|---|---|---|
| IVF QPS concurrent | 400 | 430 |
| IVF p95 under concurrency (ms) | 59.4 | 56.0 |
| IVF hybrid QPS concurrent | 37.5 | 37.7 |
| IVF hybrid p99 under concurrency (ms) | 932 | 912 |
| Flat QPS concurrent | 209 | 206 |
| Flat hybrid QPS concurrent | 34.3 | 34.5 |
| Flat hybrid p99 under concurrency (ms) | 1,034 | 1,007 |

### Right after a fresh ingest

Each ingest run also measures the flat set in the container that just ingested,
without a restart (two runs per arm, so each band is a single difference). Three
rows exceed both bands, in both directions: memory after ingest 1,606 against
1,555 MB, search p99 29.3 against 30.6 ms, and hybrid p99 without the first 10
queries 215.8 against 208.6 ms. The same rows measured over six runs after a
restart (above) do not differ. They are reported, not claimed.

### Swap and OOM

No container was OOM-killed and no serving run swapped, in either arm. IVF
builds at 4 GiB swapped up to 2,066 MiB (A) and 2,016 MiB (B), and detaches up
to 755 and 705 MiB; ingests up to 42 and 29 MiB. In 2026-10 the same `19c3293`
image swapped at most 58 MiB in its builds; its build time is the same here
(178 s then, 180 s now).

## Why nothing moved

0.3.0's changes on this benchmark's request path were each measured per request
in their ADRs, and their gains are largest where this benchmark does not go:
direct response encoding saved 1.8% (0.13 ms) of a 10-hit vector search and 14
to 23% for 100 to 1,000 hits ([ADR 0010](adr/0010-search-response-encoding.md));
native search parsing was measured on 3,072-dim vectors with document-scoped
hybrid queries ([ADR 0016](adr/0016-native-search-json.md)); `unix-excl` removes
file-lock round trips that matter on NFS
([ADR 0007](adr/0007-sofia-backend-requirements.md)); native ingest parsing was
measured on request admission with the index workers paused
([ADR 0008](adr/0008-native-ingest-json.md)). Against an 18.5 ms flat or 6.2 ms
IVF search with k=10 over 1024-dim vectors on a local disk, savings of that size
stay inside the spread. The new filters, fusion mode, scoped-search batching and
BM25 metadata keys are opt-in or apply to other request shapes, and this
benchmark does not exercise them.

## The first attempt at the search sessions

The search sessions first ran on 2026-11-06, 10:51Z to 13:00Z. In bursts during
B's sessions, an unrelated multi-process job ran on the host: up to 67% of host
CPU in user time over a 10-minute window, against 4 to 16% for the bench alone
in the guarded re-run.
B's runs in those windows were up to 59% slower in serial flat search and up to
43% lower in concurrent IVF throughput, and only those runs. Memory, swap and server logs
showed nothing different. That set is discarded for both arms. All eight
sessions were re-run behind the guard above, and B's slow runs did not recur.
A's sessions in the discarded set, on a quiet host, matched the 2026-10 figures
for the same image (flat p50 18.7 against 18.5 ms, IVF concurrent QPS 367
against 380). The ingests and the first cold starts ran while the host was
otherwise idle (4 to 8% busy over 10-minute windows) and are kept.

## Caveats

- Weaviate was not re-run; its figures stay those of 2026-08-23.
- The ingest rows rest on two runs per arm, and B's two differ by 742 vec/s.
- The c=8 and c=16 sessions of an arm are not independent samples (same volume,
  same host state).
- One host, one boot, no reboot between arms; true-cold means the volume's files
  were evicted, not a cold boot.
