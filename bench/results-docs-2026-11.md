# Document retrieval benchmark, 2026-11: 19c3293 (A) against 0.3.0 / 5da2b28 (B), Weaviate 1.38.11 as reference

Raw analysis tables behind [docs/benchmark-documents.md](../docs/benchmark-documents.md). DGX Spark;
25,000 Wikipedia articles as documents (one summary + up to 10 chunks each, 173,320 records, 1024-dim
Qwen3-Embedding-0.6B; bench/prep_wiki.py); bench/bench_docs.py at concurrency 8. Ingests 2026-11-07
14:23Z-14:28Z; search sessions 17:48Z-19:02Z (A1 repeated: the first attempt overlapped another job on the
host, see the page); Weaviate 19:02Z-19:10Z. Per arm, engine and workload: medians of the 6 measured runs
(run1-3 of two sessions); bands are max - min; "verdict" says whether B's difference exceeds both bands.
Weaviate: median of 3 measured runs. One-document filter: A `{"doc": id}` (metadata), B `{"doc_id": id}`.

## Run facts

- sessions used: A ['s-a1-r2', 's-a2'], B ['s-b1', 's-b2'], Weaviate weaviate

## Ingest (fresh 4 GiB container, DELETE + create + ingest; 2 per arm)

| Metric | A runs | B runs | A median | B median |
|---|---|---|---|---|
| Ingest wall time (s) | 81 81 | 70 72 | 81 | 71 |
| Ingest (records/s) | 2152 2142 | 2464 2417 | 2147 | 2441 |
| Memory after ingest (MB) | 318 322 | 309 318 | 320 | 313 |
| Disk (MB) | 2069 2066 | 2068 2067 | 2067 | 2067 |

Weaviate ingest: 168 s, 1033 records/s; disk 3178 MB.

## IVF build and detach (s)

- s-a1-r2: build 6.1 s, detach 2.1 s
- s-a2: build 4.1 s, detach 2.1 s
- s-b1: build 12.2 s, detach 2.1 s
- s-b2: build 4.1 s, detach 2.1 s

## raggio flat (6 / 6 measured runs)

| Workload | Metric | A median | B median | ratio (>1 = B better) | A band | B band | verdict | Weaviate |
|---|---|---|---|---|---|---|---|---|
| summary-vector | p50 (ms) | 7.6 | 7.5 | 1.02 | 0.7 | 2.1 | within band | 21.2 |
| summary-vector | p95 (ms) | 9.5 | 9.6 | 0.99 | 0.5 | 1.6 | within band | 22.0 |
| summary-vector | p99 (ms) | 10.2 | 10.4 | 0.99 | 1.0 | 1.3 | within band | 22.7 |
| summary-vector | QPS serial | 127.6 | 130.6 | 1.02 | 8.4 | 36.4 | within band | 51.7 |
| summary-vector | QPS concurrent (8) | 393.0 | 461.3 | 1.17 | 16.3 | 65.9 | B better | 284.6 |
| summary-vector | p95 under concurrency (ms) | 28.4 | 23.0 | 1.23 | 1.7 | 6.4 | within band | 33.5 |
| summary-vector | server CPU per query under concurrency (ms) | 17.6 | 7.7 | 2.29 | 0.2 | 1.0 | B better | 24.9 |
| summary-vector | quality (hit rate / recall@k) | 0.996 | 0.996 | 1.00 | 0.000 | 0.000 | within band | 0.996 |
| summary-vector | response size (KB) | 15 | 15 | 1.00 | 0 | 0 | within band | 14 |
| summary-hybrid | p50 (ms) | 10.3 | 9.9 | 1.04 | 0.8 | 1.2 | within band | 19.6 |
| summary-hybrid | p95 (ms) | 24.1 | 24.2 | 0.99 | 2.9 | 5.7 | within band | 23.3 |
| summary-hybrid | p99 (ms) | 45.6 | 43.4 | 1.05 | 11.7 | 13.5 | within band | 23.9 |
| summary-hybrid | QPS serial | 79.2 | 83.4 | 1.05 | 6.4 | 13.3 | within band | 50.7 |
| summary-hybrid | QPS concurrent (8) | 135.3 | 221.7 | 1.64 | 2.5 | 16.1 | B better | 261.5 |
| summary-hybrid | p95 under concurrency (ms) | 136.4 | 44.1 | 3.09 | 13.4 | 6.6 | B better | 35.7 |
| summary-hybrid | server CPU per query under concurrency (ms) | 39.2 | 13.7 | 2.86 | 0.4 | 0.5 | B better | 30.1 |
| summary-hybrid | quality (hit rate / recall@k) | 0.994 | 0.994 | 1.00 | 0.000 | 0.000 | within band | 0.994 |
| summary-hybrid | response size (KB) | 15 | 15 | 1.00 | 0 | 0 | within band | 15 |
| indoc-vector | p50 (ms) | 197.2 | 2.9 | 68.49 | 4.9 | 1.1 | B better | 5.9 |
| indoc-vector | p95 (ms) | 205.1 | 4.0 | 51.40 | 2.1 | 1.1 | B better | 6.9 |
| indoc-vector | p99 (ms) | 221.7 | 4.5 | 48.95 | 0.9 | 1.4 | B better | 7.7 |
| indoc-vector | QPS serial | 5.1 | 337.9 | 66.68 | 0.1 | 108.6 | B better | 176.6 |
| indoc-vector | QPS concurrent (8) | 28.8 | 902.4 | 31.37 | 1.5 | 434.8 | B better | 1201.0 |
| indoc-vector | p95 under concurrency (ms) | 306.6 | 12.5 | 24.53 | 18.6 | 7.0 | B better | 11.3 |
| indoc-vector | server CPU per query under concurrency (ms) | 265.0 | 1.4 | 189.23 | 13.3 | 0.5 | B better | 4.4 |
| indoc-vector | quality (hit rate / recall@k) | 0.993 | 0.993 | 1.00 | 0.000 | 0.000 | within band | 0.993 |
| indoc-vector | response size (KB) | 24 | 24 | 1.00 | 0 | 0 | within band | 23 |
| indoc-hybrid | p50 (ms) | 199.6 | 5.5 | 36.07 | 1.2 | 0.5 | B better | 7.3 |
| indoc-hybrid | p95 (ms) | 204.9 | 11.1 | 18.49 | 0.9 | 1.9 | B better | 10.7 |
| indoc-hybrid | p99 (ms) | 209.0 | 18.1 | 11.52 | 16.1 | 7.1 | B better | 12.5 |
| indoc-hybrid | QPS serial | 5.0 | 160.4 | 31.90 | 0.1 | 16.9 | B better | 133.6 |
| indoc-hybrid | QPS concurrent (8) | 24.2 | 607.5 | 25.05 | 0.6 | 150.9 | B better | 1011.4 |
| indoc-hybrid | p95 under concurrency (ms) | 366.3 | 20.4 | 17.98 | 10.2 | 8.0 | B better | 12.2 |
| indoc-hybrid | server CPU per query under concurrency (ms) | 349.2 | 2.6 | 132.79 | 11.5 | 0.5 | B better | 6.7 |
| indoc-hybrid | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | 1.000 |
| indoc-hybrid | response size (KB) | 24 | 24 | 1.00 | 0 | 0 | within band | 23 |
| chunks-k10 | p50 (ms) | 12.8 | 12.1 | 1.06 | 1.5 | 0.9 | within band | 8.1 |
| chunks-k10 | p95 (ms) | 15.4 | 14.8 | 1.04 | 1.5 | 1.7 | within band | 9.6 |
| chunks-k10 | p99 (ms) | 16.4 | 15.6 | 1.05 | 1.2 | 1.6 | within band | 10.1 |
| chunks-k10 | QPS serial | 76.7 | 80.4 | 1.05 | 9.3 | 6.5 | within band | 130.6 |
| chunks-k10 | QPS concurrent (8) | 319.0 | 355.6 | 1.11 | 35.9 | 103.9 | within band | 985.8 |
| chunks-k10 | p95 under concurrency (ms) | 35.9 | 26.0 | 1.38 | 3.1 | 7.0 | B better | 11.8 |
| chunks-k10 | server CPU per query under concurrency (ms) | 20.9 | 7.6 | 2.73 | 0.4 | 3.4 | B better | 6.6 |
| chunks-k10 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | 0.979 |
| chunks-k10 | response size (KB) | 52 | 52 | 1.00 | 0 | 0 | within band | 52 |
| chunks-k100 | p50 (ms) | 18.7 | 18.1 | 1.03 | 2.1 | 1.9 | within band | 17.2 |
| chunks-k100 | p95 (ms) | 22.5 | 21.7 | 1.04 | 2.3 | 1.7 | within band | 22.4 |
| chunks-k100 | p99 (ms) | 24.1 | 22.8 | 1.06 | 2.6 | 1.8 | within band | 24.0 |
| chunks-k100 | QPS serial | 51.4 | 53.3 | 1.04 | 5.1 | 6.2 | within band | 55.7 |
| chunks-k100 | QPS concurrent (8) | 116.6 | 166.0 | 1.42 | 2.3 | 22.6 | B better | 396.4 |
| chunks-k100 | p95 under concurrency (ms) | 89.8 | 65.4 | 1.37 | 3.6 | 16.2 | B better | 25.7 |
| chunks-k100 | server CPU per query under concurrency (ms) | 31.3 | 12.0 | 2.61 | 0.5 | 1.8 | B better | 18.6 |
| chunks-k100 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | 0.982 |
| chunks-k100 | response size (KB) | 522 | 522 | 1.00 | 0 | 0 | within band | 514 |
| chunks-k1000 | p50 (ms) | 73.7 | 68.7 | 1.07 | 5.6 | 2.8 | within band | 74.9 |
| chunks-k1000 | p95 (ms) | 83.7 | 76.4 | 1.10 | 3.7 | 3.6 | B better | 84.5 |
| chunks-k1000 | p99 (ms) | 89.5 | 79.9 | 1.12 | 9.1 | 4.1 | B better | 87.4 |
| chunks-k1000 | QPS serial | 12.2 | 13.0 | 1.07 | 0.8 | 0.9 | within band | 12.3 |
| chunks-k1000 | QPS concurrent (8) | 14.2 | 21.2 | 1.49 | 0.5 | 0.8 | B better | 87.7 |
| chunks-k1000 | p95 under concurrency (ms) | 811.6 | 398.6 | 2.04 | 41.8 | 25.6 | B better | 109.8 |
| chunks-k1000 | server CPU per query under concurrency (ms) | 203.5 | 65.7 | 3.10 | 6.1 | 2.9 | B better | 83.3 |
| chunks-k1000 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | 0.949 |
| chunks-k1000 | response size (KB) | 5208 | 5208 | 1.00 | 0 | 0 | within band | 5132 |

Memory after the run (podman stats, MB): A 366, B 474

## raggio IVF (6 / 6 measured runs)

| Workload | Metric | A median | B median | ratio (>1 = B better) | A band | B band | verdict | Weaviate |
|---|---|---|---|---|---|---|---|---|
| summary-vector | p50 (ms) | 6.6 | 6.5 | 1.01 | 0.2 | 0.5 | within band | 21.2 |
| summary-vector | p95 (ms) | 7.5 | 7.4 | 1.02 | 0.5 | 1.0 | within band | 22.0 |
| summary-vector | p99 (ms) | 8.1 | 8.0 | 1.01 | 0.8 | 1.2 | within band | 22.7 |
| summary-vector | QPS serial | 148.7 | 150.9 | 1.02 | 6.3 | 14.1 | within band | 51.7 |
| summary-vector | QPS concurrent (8) | 256.9 | 312.5 | 1.22 | 12.1 | 36.5 | B better | 284.6 |
| summary-vector | p95 under concurrency (ms) | 36.4 | 29.7 | 1.23 | 1.6 | 5.0 | B better | 33.5 |
| summary-vector | server CPU per query under concurrency (ms) | 16.3 | 13.8 | 1.18 | 0.6 | 1.4 | B better | 24.9 |
| summary-vector | quality (hit rate / recall@k) | 0.996 | 0.996 | 1.00 | 0.000 | 0.000 | within band | 0.996 |
| summary-vector | response size (KB) | 15 | 15 | 1.00 | 0 | 0 | within band | 14 |
| summary-hybrid | p50 (ms) | 9.6 | 8.8 | 1.09 | 0.7 | 0.4 | B better | 19.6 |
| summary-hybrid | p95 (ms) | 24.8 | 22.5 | 1.10 | 3.1 | 2.2 | within band | 23.3 |
| summary-hybrid | p99 (ms) | 41.7 | 42.0 | 0.99 | 13.3 | 6.7 | within band | 23.9 |
| summary-hybrid | QPS serial | 83.3 | 90.9 | 1.09 | 6.2 | 3.8 | B better | 50.7 |
| summary-hybrid | QPS concurrent (8) | 102.4 | 179.8 | 1.75 | 4.3 | 8.7 | B better | 261.5 |
| summary-hybrid | p95 under concurrency (ms) | 191.9 | 58.7 | 3.27 | 15.5 | 3.9 | B better | 35.7 |
| summary-hybrid | server CPU per query under concurrency (ms) | 39.0 | 24.3 | 1.61 | 1.1 | 0.6 | B better | 30.1 |
| summary-hybrid | quality (hit rate / recall@k) | 0.994 | 0.994 | 1.00 | 0.000 | 0.000 | within band | 0.994 |
| summary-hybrid | response size (KB) | 15 | 15 | 1.00 | 0 | 0 | within band | 15 |
| indoc-vector | p50 (ms) | 197.1 | 3.0 | 65.81 | 4.4 | 1.1 | B better | 5.9 |
| indoc-vector | p95 (ms) | 204.0 | 3.9 | 52.41 | 2.1 | 1.6 | B better | 6.9 |
| indoc-vector | p99 (ms) | 221.2 | 4.5 | 48.67 | 2.9 | 2.1 | B better | 7.7 |
| indoc-vector | QPS serial | 5.1 | 323.0 | 63.64 | 0.1 | 95.5 | B better | 176.6 |
| indoc-vector | QPS concurrent (8) | 29.7 | 775.8 | 26.09 | 1.5 | 435.0 | B better | 1201.0 |
| indoc-vector | p95 under concurrency (ms) | 293.8 | 13.6 | 21.57 | 14.6 | 4.6 | B better | 11.3 |
| indoc-vector | server CPU per query under concurrency (ms) | 254.4 | 1.7 | 149.34 | 11.2 | 0.6 | B better | 4.4 |
| indoc-vector | quality (hit rate / recall@k) | 0.993 | 0.993 | 1.00 | 0.000 | 0.000 | within band | 0.993 |
| indoc-vector | response size (KB) | 24 | 24 | 1.00 | 0 | 0 | within band | 23 |
| indoc-hybrid | p50 (ms) | 198.6 | 5.7 | 34.56 | 6.4 | 1.2 | B better | 7.3 |
| indoc-hybrid | p95 (ms) | 204.8 | 10.8 | 18.92 | 1.7 | 1.2 | B better | 10.7 |
| indoc-hybrid | p99 (ms) | 207.8 | 19.4 | 10.69 | 10.1 | 7.8 | B better | 12.5 |
| indoc-hybrid | QPS serial | 5.1 | 156.3 | 30.90 | 0.1 | 28.6 | B better | 133.6 |
| indoc-hybrid | QPS concurrent (8) | 24.1 | 599.5 | 24.85 | 0.7 | 140.2 | B better | 1011.4 |
| indoc-hybrid | p95 under concurrency (ms) | 368.1 | 17.3 | 21.29 | 9.7 | 2.0 | B better | 12.2 |
| indoc-hybrid | server CPU per query under concurrency (ms) | 345.0 | 2.8 | 123.55 | 10.3 | 0.7 | B better | 6.7 |
| indoc-hybrid | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | 1.000 |
| indoc-hybrid | response size (KB) | 24 | 24 | 1.00 | 0 | 0 | within band | 23 |
| chunks-k10 | p50 (ms) | 7.2 | 7.0 | 1.02 | 0.4 | 0.7 | within band | 8.1 |
| chunks-k10 | p95 (ms) | 8.2 | 8.3 | 0.99 | 0.8 | 0.8 | within band | 9.6 |
| chunks-k10 | p99 (ms) | 8.6 | 9.0 | 0.95 | 0.9 | 1.1 | within band | 10.1 |
| chunks-k10 | QPS serial | 135.1 | 136.9 | 1.01 | 9.8 | 13.9 | within band | 130.6 |
| chunks-k10 | QPS concurrent (8) | 247.8 | 295.1 | 1.19 | 17.4 | 27.2 | B better | 985.8 |
| chunks-k10 | p95 under concurrency (ms) | 40.0 | 32.0 | 1.25 | 1.8 | 3.4 | B better | 11.8 |
| chunks-k10 | server CPU per query under concurrency (ms) | 19.0 | 16.3 | 1.16 | 0.7 | 0.9 | B better | 6.6 |
| chunks-k10 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | 0.979 |
| chunks-k10 | response size (KB) | 52 | 52 | 1.00 | 0 | 0 | within band | 52 |
| chunks-k100 | p50 (ms) | 13.4 | 13.0 | 1.03 | 3.9 | 2.0 | within band | 17.2 |
| chunks-k100 | p95 (ms) | 15.5 | 14.5 | 1.07 | 3.7 | 2.3 | within band | 22.4 |
| chunks-k100 | p99 (ms) | 16.2 | 15.3 | 1.06 | 4.0 | 2.2 | within band | 24.0 |
| chunks-k100 | QPS serial | 70.3 | 72.8 | 1.04 | 17.7 | 10.6 | within band | 55.7 |
| chunks-k100 | QPS concurrent (8) | 110.1 | 144.9 | 1.32 | 5.2 | 23.4 | B better | 396.4 |
| chunks-k100 | p95 under concurrency (ms) | 90.9 | 69.1 | 1.32 | 6.2 | 16.4 | B better | 25.7 |
| chunks-k100 | server CPU per query under concurrency (ms) | 31.1 | 23.3 | 1.34 | 0.8 | 1.2 | B better | 18.6 |
| chunks-k100 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | 0.982 |
| chunks-k100 | response size (KB) | 522 | 522 | 1.00 | 0 | 0 | within band | 514 |
| chunks-k1000 | p50 (ms) | 66.9 | 61.9 | 1.08 | 0.9 | 4.6 | B better | 74.9 |
| chunks-k1000 | p95 (ms) | 75.0 | 69.5 | 1.08 | 3.1 | 5.6 | within band | 84.5 |
| chunks-k1000 | p99 (ms) | 79.4 | 72.4 | 1.10 | 7.6 | 10.4 | within band | 87.4 |
| chunks-k1000 | QPS serial | 13.5 | 14.5 | 1.07 | 0.5 | 1.7 | within band | 12.3 |
| chunks-k1000 | QPS concurrent (8) | 14.4 | 20.2 | 1.40 | 0.3 | 1.1 | B better | 87.7 |
| chunks-k1000 | p95 under concurrency (ms) | 724.2 | 419.2 | 1.73 | 60.5 | 19.1 | B better | 109.8 |
| chunks-k1000 | server CPU per query under concurrency (ms) | 206.3 | 139.7 | 1.48 | 6.0 | 6.1 | B better | 83.3 |
| chunks-k1000 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | 0.949 |
| chunks-k1000 | response size (KB) | 5208 | 5208 | 1.00 | 0 | 0 | within band | 5132 |

Memory after the run (podman stats, MB): A 363, B 479
