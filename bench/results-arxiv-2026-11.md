# arxiv end-to-end re-measurement, 2026-11: 19c3293 (A) against 0.3.0 / f1b0ab3 (B)

Raw analysis tables behind [docs/benchmark-arxiv-2026-11.md](../docs/benchmark-arxiv-2026-11.md).
DGX Spark, 2.55M x 1024 arxiv corpus, 4 GiB containers, seed 42, 500 queries, k=10.
Ingests and cold starts (`e2e-*`, `c0-*`): 2026-11-06 09:49Z-10:51Z. Search sessions (`g-*`, host-CPU
guarded): 2026-11-06 23:38Z-2026-11-07 01:48Z. Per row: per-run values, medians, ratio (above 1 = B better),
each arm's band (max - min) and whether the difference exceeds both bands. "Memory after ingest" in the
search-session tables is read after a container restart.

## Run facts
- stage 3 sessions used: a1 c=8: g-a1, a1 c=16: g-a1-c16, a2 c=8: g-a2, a2 c=16: g-a2-c16, b1 c=8: g-b1, b1 c=16: g-b1-c16, b2 c=8: g-b2, b2 c=16: g-b2-c16
- sessions without a clean attempt: none

## Ingest (bench.py --reingest, full corpus, 4 GiB unless labelled)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Ingest throughput (vec/s) | 3124 3107 | 3749 3007 | 3116 | 3378 | 1.08 | 16 | 742 | within band | runs i1,i2 / i1,i2 |
| Ingest wall time (s) | 816 820 | 680 848 | 818 | 764 | 1.07 | 4 | 168 | within band | runs i1,i2 / i1,i2 |

Memory/other measures from the same ingest runs (fresh ingest, no restart):

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Memory after ingest (MB) | 1590 1621 | 1537 1573 | 1606 | 1555 | 1.03 | 31 | 36 | B better | 2 runs per arm |
| Memory under query load (MB) | 1596 1630 | 1570 1604 | 1613 | 1587 | 1.02 | 34 | 34 | within band | 2 runs per arm |
| Disk footprint (MB) | 16495 16495 | 16495 16495 | 16495 | 16495 | 1.00 | 0 | 0 | within band | 2 runs per arm |
| Search p50 (ms) | 22.8 23.1 | 23.4 22.1 | 23.0 | 22.7 | 1.01 | 0.3 | 1.3 | within band | 2 runs per arm |
| Search p95 (ms) | 27.0 28.0 | 27.6 25.4 | 27.5 | 26.5 | 1.04 | 1.0 | 2.2 | within band | 2 runs per arm |
| Search p99 (ms) | 28.7 29.9 | 30.1 31.2 | 29.3 | 30.6 | 0.96 | 1.2 | 1.1 | B worse | 2 runs per arm |
| QPS serial | 43 42 | 42 45 | 43 | 43 | 1.01 | 1 | 2 | within band | 2 runs per arm |
| QPS concurrent | 98 128 | 98 145 | 113 | 122 | 1.07 | 30 | 47 | within band | 2 runs per arm |
| p95 under concurrency (ms) | 90.9 67.8 | 91.5 68.7 | 79.4 | 80.1 | 0.99 | 23.1 | 22.8 | within band | 2 runs per arm |
| CPU per query under concurrency (ms) | 118.1 117.0 | 118.6 98.9 | 117.5 | 108.8 | 1.08 | 1.0 | 19.7 | within band | 2 runs per arm |
| Filtered p50 (ms) | 28.7 31.2 | 30.4 32.7 | 29.9 | 31.5 | 0.95 | 2.4 | 2.3 | within band | 2 runs per arm |
| Filtered p95 (ms) | 34.4 39.0 | 37.2 38.8 | 36.7 | 38.0 | 0.97 | 4.6 | 1.7 | within band | 2 runs per arm |
| Recall@10 vs exact | 1.000 1.000 | 1.000 1.000 | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | 2 runs per arm |
| Hybrid p50 (ms) | 84.9 89.7 | 84.0 96.2 | 87.3 | 90.1 | 0.97 | 4.8 | 12.2 | within band | 2 runs per arm |
| Hybrid p95 (ms) | 152.4 153.3 | 144.0 165.1 | 152.8 | 154.5 | 0.99 | 0.9 | 21.1 | within band | 2 runs per arm |
| Hybrid p99 (ms) | 253.1 309.7 | 273.5 325.4 | 281.4 | 299.5 | 0.94 | 56.7 | 51.9 | within band | 2 runs per arm |
| Hybrid first 10 queries, slowest (ms) | 469.7 501.2 | 494.7 560.3 | 485.5 | 527.5 | 0.92 | 31.5 | 65.6 | within band | 2 runs per arm |
| Hybrid p99 without the first 10 (ms) | 213.1 218.6 | 205.3 211.8 | 215.8 | 208.6 | 1.03 | 5.5 | 6.5 | B better | 2 runs per arm |
| Hybrid QPS serial | 10.8 10.3 | 11.0 9.5 | 10.6 | 10.2 | 0.97 | 0.5 | 1.5 | within band | 2 runs per arm |
| Hybrid QPS concurrent | 24.1 24.5 | 24.3 24.3 | 24.3 | 24.3 | 1.00 | 0.4 | 0.0 | within band | 2 runs per arm |
| Hybrid p99 under concurrency (ms) | 568.6 575.2 | 575.2 575.1 | 571.9 | 575.1 | 0.99 | 6.7 | 0.1 | within band | 2 runs per arm |
| Hybrid CPU per query under concurrency (ms) | 419.5 410.3 | 414.2 405.4 | 414.9 | 409.8 | 1.01 | 9.3 | 8.8 | within band | 2 runs per arm |
| Hybrid text-hit@10 | 0.984 0.984 | 0.984 0.984 | 0.984 | 0.984 | 1.00 | 0.000 | 0.000 | within band | 2 runs per arm |

## True-cold start (fadvise-evicted volume, fresh 4 GiB container)

Seconds from container start to the first `GET /collections/bench` answer (C1), slowest `/healthz` answer during the load (C2, 99 = none in 5 s), slowest of 3 later GETs (C3).

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| flat: start -> first GET (s) | 4.6 4.5 4.8 4.9 4.9 4.9 | 5.1 5.1 4.7 4.8 4.7 5.0 | 4.8 | 4.9 | 0.98 | 0.4 | 0.4 | within band |  |
| flat: start -> /healthz up (s) | 0.45 0.45 0.45 0.34 0.44 0.44 | 0.33 0.34 0.32 0.42 0.31 0.44 | 0.45 | 0.33 | 1.34 | 0.11 | 0.13 | within band |  |
| flat: slowest /healthz during load (s) | 0.00 0.00 0.00 0.00 0.01 0.00 | 0.00 0.01 0.00 0.01 0.00 0.00 | 0.00 | 0.00 | 1.00 | 0.01 | 0.00 | within band |  |
| flat: slowest later GET (s) | 0.504 0.475 0.506 0.507 0.482 0.511 | 0.539 0.481 0.506 0.475 0.482 0.502 | 0.505 | 0.492 | 1.03 | 0.036 | 0.064 | within band |  |

(flat: n = 6 A, 6 B)

| ivf: start -> first GET (s) | 5.3 5.4 5.0 5.1 | 5.3 5.0 5.2 5.2 | 5.2 | 5.2 | 1.00 | 0.4 | 0.3 | within band |  |
| ivf: start -> /healthz up (s) | 0.32 0.46 0.42 0.32 | 0.44 0.42 0.46 0.35 | 0.37 | 0.43 | 0.86 | 0.14 | 0.11 | within band |  |
| ivf: slowest /healthz during load (s) | 0.00 0.00 0.00 0.00 | 0.01 0.00 0.01 0.00 | 0.00 | 0.00 | 0.50 | 0.00 | 0.01 | within band |  |
| ivf: slowest later GET (s) | 0.477 0.478 0.514 0.479 | 0.477 0.475 0.475 0.475 | 0.478 | 0.475 | 1.01 | 0.037 | 0.002 | within band |  |

(ivf: n = 4 A, 4 B)

## flat, c=8 (A sessions g-a1, g-a2; B sessions g-b1, g-b2; 6 / 6 measured runs)

### Serial vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Search p50 (ms) | 18.6 18.9 18.7 18.6 18.7 18.9 | 18.4 18.5 18.6 18.5 18.5 18.8 | 18.7 | 18.5 | 1.01 | 0.4 | 0.4 | within band |  |
| Search p95 (ms) | 19.9 20.3 19.9 20.2 19.8 20.3 | 19.7 19.9 19.9 20.0 19.7 20.2 | 20.1 | 19.9 | 1.01 | 0.5 | 0.5 | within band |  |
| Search p99 (ms) | 20.6 21.3 20.6 21.1 20.5 21.0 | 20.5 20.5 20.6 20.5 20.7 20.8 | 20.8 | 20.5 | 1.01 | 0.7 | 0.3 | within band |  |
| QPS serial | 53 53 53 53 53 53 | 54 54 54 54 54 53 | 53 | 54 | 1.01 | 1 | 1 | within band |  |

### Concurrent vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| QPS concurrent | 125 174 125 125 124 175 | 126 126 126 175 125 125 | 125 | 126 | 1.01 | 51 | 50 | within band |  |
| p95 under concurrency (ms) | 69.6 69.8 72.9 70.8 71.7 69.8 | 69.7 68.4 69.0 66.7 70.2 71.5 | 70.3 | 69.4 | 1.01 | 3.4 | 4.8 | within band |  |

### Filtered search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Filtered p50 (ms) | 24.9 25.2 27.5 26.9 25.8 26.5 | 25.2 23.5 22.8 23.3 25.4 25.7 | 26.2 | 24.4 | 1.07 | 2.6 | 2.8 | within band |  |
| Filtered p95 (ms) | 30.1 32.5 32.5 31.2 27.9 32.6 | 31.0 30.9 27.9 28.5 31.1 32.6 | 31.9 | 31.0 | 1.03 | 4.7 | 4.7 | within band |  |

### Hybrid (vector + BM25)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Hybrid p50 (ms) | 48.5 48.4 48.6 49.3 48.9 48.0 | 48.6 48.4 48.4 47.8 47.6 47.5 | 48.5 | 48.1 | 1.01 | 1.3 | 1.1 | within band |  |
| Hybrid p95 (ms) | 73.0 72.9 73.4 74.1 74.6 72.2 | 74.3 73.4 73.6 72.4 73.2 73.4 | 73.2 | 73.4 | 1.00 | 2.4 | 1.9 | within band |  |
| Hybrid p99 (ms) | 97.5 98.5 97.3 107.3 99.5 98.3 | 97.0 96.9 110.7 105.0 97.5 100.0 | 98.4 | 98.8 | 1.00 | 10.0 | 13.7 | within band |  |
| Hybrid QPS serial | 20.3 20.3 20.2 20.0 20.2 20.4 | 20.3 20.4 20.4 20.5 20.6 20.7 | 20.3 | 20.5 | 1.01 | 0.5 | 0.4 | within band |  |
| Hybrid QPS concurrent | 36.6 35.1 35.9 34.9 36.2 37.9 | 34.8 37.5 37.7 34.7 33.8 35.0 | 36.0 | 34.9 | 0.97 | 3.0 | 3.9 | within band |  |
| Hybrid p99 under concurrency (ms) | 476.9 457.2 465.1 470.1 451.2 472.8 | 449.8 458.4 473.8 460.1 479.8 446.7 | 467.6 | 459.3 | 1.02 | 25.7 | 33.1 | within band |  |
| Hybrid first 10 queries, slowest (ms) | 121.4 121.5 123.4 123.1 124.9 123.7 | 127.3 121.5 123.2 121.2 123.4 122.0 | 123.3 | 122.6 | 1.01 | 3.5 | 6.1 | within band |  |
| Hybrid p99 without the first 10 (ms) | 89.6 89.5 88.5 89.2 89.3 88.1 | 89.7 89.3 90.9 90.3 88.6 88.2 | 89.2 | 89.5 | 1.00 | 1.6 | 2.7 | within band |  |

### Recall

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Recall@10 vs exact | 1.000 1.000 1.000 1.000 1.000 1.000 | 1.000 1.000 1.000 1.000 1.000 1.000 | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band |  |
| Hybrid text-hit@10 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 | 0.984 | 1.00 | 0.000 | 0.000 | within band |  |

### Memory (ABAB runs: after a container restart, and under load; podman stats)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Memory after ingest (MB) | 1501 1502 1501 1501 1501 1501 | 1501 1501 1501 1501 1501 1502 | 1501 | 1501 | 1.00 | 1 | 1 | within band |  |
| Memory under query load (MB) | 1505 1505 1505 1505 1505 1505 | 1505 1506 1506 1505 1506 1505 | 1505 | 1506 | 1.00 | 0 | 1 | within band |  |

### CPU per query under concurrency

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| CPU per query under concurrency (ms) | 118.7 76.1 118.8 117.4 118.8 76.4 | 116.7 117.9 118.3 78.7 117.4 119.4 | 118.1 | 117.7 | 1.00 | 42.7 | 40.7 | within band |  |
| Hybrid CPU per query under concurrency (ms) | 335.5 345.3 338.0 342.1 337.5 327.3 | 345.6 329.3 327.0 345.0 350.7 346.5 | 337.7 | 345.3 | 0.98 | 18.0 | 23.7 | within band |  |

### Disk

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Disk footprint (MB) | 16463 16463 16463 16463 16463 16463 | 16463 16463 16463 16463 16463 16463 | 16463 | 16463 | 1.00 | 0 | 0 | within band |  |

## IVF, c=8 (A sessions g-a1, g-a2; B sessions g-b1, g-b2; 6 / 6 measured runs)

### Serial vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Search p50 (ms) | 6.4 6.4 6.7 6.3 6.5 6.1 | 6.5 6.0 6.2 6.2 6.4 5.5 | 6.4 | 6.2 | 1.04 | 0.6 | 1.0 | within band |  |
| Search p95 (ms) | 8.4 8.3 8.3 7.8 8.7 7.2 | 8.5 7.6 7.6 8.5 8.4 7.7 | 8.3 | 8.0 | 1.04 | 1.5 | 0.9 | within band |  |
| Search p99 (ms) | 9.9 8.8 9.2 8.6 9.4 7.7 | 9.2 8.6 8.0 9.5 9.4 8.2 | 9.0 | 8.9 | 1.01 | 2.2 | 1.5 | within band |  |
| QPS serial | 150 152 149 160 151 165 | 151 163 163 157 154 176 | 152 | 160 | 1.05 | 16 | 25 | within band |  |

### Concurrent vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| QPS concurrent | 400 369 387 397 366 361 | 382 396 387 373 398 380 | 378 | 385 | 1.02 | 38 | 25 | within band |  |
| p95 under concurrency (ms) | 24.3 22.9 25.6 22.3 24.6 24.6 | 26.1 26.7 24.9 27.4 23.0 22.9 | 24.5 | 25.5 | 0.96 | 3.3 | 4.5 | within band |  |

### Filtered search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Filtered p50 (ms) | 5.4 5.7 5.7 5.9 5.7 5.7 | 5.3 6.2 6.0 6.0 6.0 5.7 | 5.7 | 6.0 | 0.96 | 0.5 | 0.9 | within band |  |
| Filtered p95 (ms) | 6.6 6.6 6.8 6.9 6.6 7.1 | 6.4 7.7 7.3 7.5 7.2 6.8 | 6.7 | 7.2 | 0.93 | 0.6 | 1.3 | within band |  |

### Hybrid (vector + BM25)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Hybrid p50 (ms) | 44.7 46.0 44.3 44.9 44.9 45.4 | 45.7 45.2 44.6 44.8 45.1 45.1 | 44.9 | 45.1 | 1.00 | 1.8 | 1.1 | within band |  |
| Hybrid p95 (ms) | 69.9 71.1 71.2 71.1 70.7 71.7 | 71.4 70.9 70.2 70.4 70.7 71.1 | 71.1 | 70.8 | 1.00 | 1.8 | 1.1 | within band |  |
| Hybrid p99 (ms) | 96.7 93.9 96.0 106.0 95.2 96.7 | 94.6 96.0 96.3 98.8 96.6 95.9 | 96.3 | 96.1 | 1.00 | 12.0 | 4.2 | within band |  |
| Hybrid QPS serial | 22.0 21.5 22.1 21.8 22.0 21.7 | 21.6 21.8 22.0 21.8 21.9 21.9 | 21.9 | 21.9 | 1.00 | 0.6 | 0.4 | within band |  |
| Hybrid QPS concurrent | 38.4 39.0 37.1 37.7 36.8 39.2 | 38.6 38.4 36.0 34.8 38.6 38.9 | 38.1 | 38.5 | 1.01 | 2.4 | 4.1 | within band |  |
| Hybrid p99 under concurrency (ms) | 430.5 424.7 485.9 454.8 449.0 445.1 | 416.3 454.2 475.2 466.8 417.8 434.2 | 447.1 | 444.2 | 1.01 | 61.2 | 58.9 | within band |  |
| Hybrid first 10 queries, slowest (ms) | 122.4 121.1 122.9 121.2 122.0 123.0 | 123.5 121.1 120.9 120.7 119.9 121.1 | 122.2 | 121.0 | 1.01 | 2.0 | 3.6 | within band |  |
| Hybrid p99 without the first 10 (ms) | 87.2 86.3 85.8 88.8 87.1 87.7 | 86.3 86.1 87.8 86.9 86.6 87.5 | 87.2 | 86.8 | 1.00 | 3.0 | 1.6 | within band |  |

### Recall

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Recall@10 vs exact | 0.995 0.995 0.995 0.994 0.994 0.994 | 0.995 0.995 0.995 0.994 0.994 0.994 | 0.994 | 0.994 | 1.00 | 0.000 | 0.001 | within band |  |
| Hybrid text-hit@10 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 | 0.984 | 1.00 | 0.000 | 0.000 | within band |  |

### Memory (ABAB runs: after a container restart, and under load; podman stats)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Memory after ingest (MB) | 1508 1506 1508 1507 1508 1507 | 1506 1505 1504 1507 1507 1507 | 1508 | 1506 | 1.00 | 2 | 3 | within band |  |
| Memory under query load (MB) | 1515 1514 1516 1515 1515 1515 | 1514 1513 1511 1514 1514 1514 | 1515 | 1514 | 1.00 | 2 | 3 | within band |  |

### CPU per query under concurrency

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| CPU per query under concurrency (ms) | 9.6 10.1 10.2 9.7 10.1 11.0 | 9.9 9.8 9.9 9.6 9.5 10.1 | 10.1 | 9.9 | 1.03 | 1.4 | 0.6 | within band |  |
| Hybrid CPU per query under concurrency (ms) | 201.6 198.1 208.7 203.4 211.2 197.8 | 199.5 198.8 214.0 221.9 200.8 198.3 | 202.5 | 200.1 | 1.01 | 13.4 | 23.7 | within band |  |

### Disk

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Disk footprint (MB) | 16751 16751 16751 16751 16751 16751 | 16751 16751 16751 16751 16751 16751 | 16751 | 16751 | 1.00 | 0 | 0 | within band |  |

## flat, c=16 (A sessions g-a1-c16, g-a2-c16; B sessions g-b1-c16, g-b2-c16; 6 / 6 measured runs)

### Serial vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Search p50 (ms) | 18.7 18.6 18.6 18.6 18.6 18.8 | 18.5 18.5 18.8 18.6 18.5 18.6 | 18.6 | 18.5 | 1.01 | 0.1 | 0.3 | within band |  |
| Search p95 (ms) | 20.0 19.9 20.0 20.0 20.0 20.1 | 19.9 19.8 20.0 20.2 20.0 20.0 | 20.0 | 20.0 | 1.00 | 0.3 | 0.3 | within band |  |
| Search p99 (ms) | 20.8 20.5 20.5 20.8 20.7 20.8 | 20.8 20.5 20.6 21.0 20.6 20.4 | 20.7 | 20.6 | 1.01 | 0.3 | 0.6 | within band |  |
| QPS serial | 53 54 53 53 53 53 | 54 54 53 54 54 53 | 53 | 54 | 1.01 | 0 | 1 | within band |  |

### Concurrent vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| QPS concurrent | 223 198 205 214 196 244 | 214 200 209 202 345 201 | 209 | 206 | 0.98 | 48 | 145 | within band |  |
| p95 under concurrency (ms) | 88.3 98.0 87.9 93.9 103.3 100.4 | 89.0 92.0 101.4 97.1 73.1 92.3 | 95.9 | 92.1 | 1.04 | 15.4 | 28.3 | within band |  |

### Filtered search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Filtered p50 (ms) | 25.4 23.0 27.1 26.3 24.1 24.0 | 26.8 27.0 28.4 27.8 28.4 26.4 | 24.8 | 27.4 | 0.91 | 4.2 | 2.0 | within band |  |
| Filtered p95 (ms) | 32.1 28.2 31.1 31.6 29.7 30.5 | 29.9 32.0 31.6 32.3 32.8 31.9 | 30.8 | 32.0 | 0.96 | 3.9 | 2.9 | within band |  |

### Hybrid (vector + BM25)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Hybrid p50 (ms) | 48.0 49.7 49.0 47.1 47.0 48.2 | 48.6 47.5 47.1 47.2 47.7 48.5 | 48.1 | 47.6 | 1.01 | 2.7 | 1.5 | within band |  |
| Hybrid p95 (ms) | 73.9 75.5 74.0 73.2 71.5 71.8 | 74.4 71.9 73.4 72.6 71.8 73.9 | 73.6 | 73.0 | 1.01 | 4.1 | 2.6 | within band |  |
| Hybrid p99 (ms) | 109.4 107.4 98.3 105.5 106.8 94.9 | 97.4 97.4 102.9 107.5 107.6 94.4 | 106.2 | 100.2 | 1.06 | 14.6 | 13.1 | within band |  |
| Hybrid QPS serial | 20.4 19.8 20.3 20.8 20.9 20.5 | 20.2 20.7 20.7 20.7 20.7 20.6 | 20.4 | 20.7 | 1.01 | 1.1 | 0.5 | within band |  |
| Hybrid QPS concurrent | 34.6 33.7 34.6 33.1 34.1 34.8 | 33.1 33.9 34.6 34.9 34.4 34.9 | 34.3 | 34.5 | 1.00 | 1.7 | 1.8 | within band |  |
| Hybrid p99 under concurrency (ms) | 1028.6 1032.1 1035.0 1035.2 1016.8 1040.0 | 990.5 1047.9 1024.0 952.6 1048.6 941.0 | 1033.6 | 1007.2 | 1.03 | 23.3 | 107.6 | within band |  |
| Hybrid first 10 queries, slowest (ms) | 126.5 124.7 126.1 125.0 123.9 122.5 | 123.9 123.5 122.1 124.7 121.1 124.7 | 124.8 | 123.7 | 1.01 | 4.0 | 3.6 | within band |  |
| Hybrid p99 without the first 10 (ms) | 93.5 92.4 89.7 88.5 87.1 87.7 | 88.6 86.5 90.1 90.2 86.2 89.0 | 89.1 | 88.8 | 1.00 | 6.4 | 4.0 | within band |  |

### Recall

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Recall@10 vs exact | 1.000 1.000 1.000 1.000 1.000 1.000 | 1.000 1.000 1.000 1.000 1.000 1.000 | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band |  |
| Hybrid text-hit@10 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 | 0.984 | 1.00 | 0.000 | 0.000 | within band |  |

### Memory (ABAB runs: after a container restart, and under load; podman stats)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Memory after ingest (MB) | 1501 1501 1501 1501 1501 1501 | 1501 1502 1502 1501 1501 1501 | 1501 | 1501 | 1.00 | 0 | 1 | within band |  |
| Memory under query load (MB) | 1506 1504 1506 1506 1506 1506 | 1506 1506 1506 1504 1506 1506 | 1506 | 1506 | 1.00 | 2 | 2 | within band |  |

### CPU per query under concurrency

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| CPU per query under concurrency (ms) | 57.0 68.4 69.1 65.0 69.7 53.1 | 64.4 69.5 66.1 69.5 32.1 69.8 | 66.7 | 67.8 | 0.98 | 16.5 | 37.7 | within band |  |
| Hybrid CPU per query under concurrency (ms) | 477.4 486.7 475.5 495.6 478.9 473.0 | 495.0 482.1 479.3 471.3 478.0 471.0 | 478.2 | 478.6 | 1.00 | 22.6 | 24.1 | within band |  |

### Disk

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Disk footprint (MB) | 16463 16463 16463 16463 16463 16463 | 16463 16463 16463 16463 16463 16463 | 16463 | 16463 | 1.00 | 0 | 0 | within band |  |

## IVF, c=16 (A sessions g-a1-c16, g-a2-c16; B sessions g-b1-c16, g-b2-c16; 6 / 6 measured runs)

### Serial vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Search p50 (ms) | 6.8 6.4 5.8 6.0 6.7 6.3 | 6.7 6.1 5.7 6.2 6.6 5.6 | 6.3 | 6.2 | 1.03 | 1.0 | 1.0 | within band |  |
| Search p95 (ms) | 8.6 8.3 7.0 7.9 8.9 8.0 | 8.7 8.4 6.8 8.5 8.8 7.2 | 8.2 | 8.5 | 0.96 | 2.0 | 2.0 | within band |  |
| Search p99 (ms) | 9.8 9.3 7.6 9.0 10.3 8.3 | 9.7 9.1 7.4 9.2 9.6 7.9 | 9.1 | 9.1 | 1.00 | 2.7 | 2.2 | within band |  |
| QPS serial | 146 154 171 164 145 158 | 147 161 177 155 149 175 | 156 | 158 | 1.01 | 25 | 29 | within band |  |

### Concurrent vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| QPS concurrent | 385 408 421 392 433 375 | 429 445 417 434 420 432 | 400 | 430 | 1.08 | 58 | 28 | within band |  |
| p95 under concurrency (ms) | 59.2 59.7 59.6 55.3 58.5 62.7 | 52.8 55.4 55.7 62.6 56.3 57.1 | 59.4 | 56.0 | 1.06 | 7.4 | 9.8 | within band |  |

### Filtered search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Filtered p50 (ms) | 6.0 6.4 5.9 5.6 5.9 6.1 | 5.4 5.9 5.3 5.8 5.3 5.6 | 6.0 | 5.5 | 1.08 | 0.8 | 0.6 | within band |  |
| Filtered p95 (ms) | 7.0 7.9 7.0 7.5 7.2 7.2 | 7.0 6.8 6.3 7.1 6.2 6.9 | 7.2 | 6.9 | 1.05 | 0.9 | 0.9 | within band |  |

### Hybrid (vector + BM25)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Hybrid p50 (ms) | 44.9 44.3 45.0 44.9 44.0 44.6 | 45.0 44.3 44.2 44.6 44.8 44.0 | 44.8 | 44.5 | 1.01 | 1.0 | 1.0 | within band |  |
| Hybrid p95 (ms) | 70.1 70.1 70.3 70.2 69.8 69.4 | 70.3 70.5 70.1 69.9 70.6 69.6 | 70.1 | 70.2 | 1.00 | 0.9 | 1.0 | within band |  |
| Hybrid p99 (ms) | 105.0 95.3 95.2 96.6 96.7 95.7 | 98.9 94.1 94.8 98.1 93.8 104.9 | 96.2 | 96.5 | 1.00 | 9.8 | 11.2 | within band |  |
| Hybrid QPS serial | 21.9 22.1 21.8 21.8 22.3 22.0 | 21.9 22.2 22.2 22.1 22.1 22.3 | 21.9 | 22.1 | 1.01 | 0.6 | 0.4 | within band |  |
| Hybrid QPS concurrent | 37.1 37.0 37.4 38.1 37.9 37.7 | 37.3 37.9 38.0 36.1 38.3 37.6 | 37.5 | 37.7 | 1.01 | 1.1 | 2.2 | within band |  |
| Hybrid p99 under concurrency (ms) | 1018.6 887.9 947.4 895.1 951.2 915.8 | 893.3 900.7 936.5 946.6 922.3 890.1 | 931.6 | 911.5 | 1.02 | 130.6 | 56.5 | within band |  |
| Hybrid first 10 queries, slowest (ms) | 122.7 123.6 122.9 122.8 121.3 121.4 | 122.2 120.9 119.8 120.3 120.2 123.1 | 122.7 | 120.6 | 1.02 | 2.3 | 3.3 | within band |  |
| Hybrid p99 without the first 10 (ms) | 88.6 86.8 86.6 87.9 84.6 86.4 | 86.4 85.7 85.9 85.7 84.8 86.7 | 86.7 | 85.8 | 1.01 | 4.0 | 1.9 | within band |  |

### Recall

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Recall@10 vs exact | 0.995 0.995 0.995 0.992 0.992 0.992 | 0.996 0.996 0.996 0.995 0.995 0.995 | 0.994 | 0.995 | 1.00 | 0.004 | 0.001 | within band |  |
| Hybrid text-hit@10 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 | 0.984 | 1.00 | 0.000 | 0.000 | within band |  |

### Memory (ABAB runs: after a container restart, and under load; podman stats)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Memory after ingest (MB) | 1507 1508 1505 1504 1505 1505 | 1507 1508 1508 1507 1507 1509 | 1505 | 1508 | 1.00 | 4 | 2 | within band |  |
| Memory under query load (MB) | 1516 1519 1513 1514 1512 1513 | 1516 1516 1517 1515 1520 1517 | 1514 | 1516 | 1.00 | 7 | 5 | within band |  |

### CPU per query under concurrency

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| CPU per query under concurrency (ms) | 9.7 10.6 10.0 10.5 10.1 10.6 | 10.3 10.1 10.4 9.5 9.6 9.6 | 10.3 | 9.9 | 1.05 | 0.9 | 0.9 | within band |  |
| Hybrid CPU per query under concurrency (ms) | 396.5 397.9 395.2 382.5 389.3 390.3 | 395.0 389.2 384.3 403.0 384.8 394.4 | 392.8 | 391.8 | 1.00 | 15.4 | 18.7 | within band |  |

### Disk

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Disk footprint (MB) | 16751 16751 16751 16751 16751 16751 | 16751 16751 16751 16751 16751 16751 | 16751 | 16751 | 1.00 | 0 | 0 | within band |  |

## Memory peaks, swap and OOM (cgroup, 1 Hz sampler; peak = memory.peak of the container at its last sample)

| Step | Arm | containers sampled | max cgroup peak (MiB) | max anon (MiB) | max swap.peak (MiB) | OOM kills |
|---|---|---|---|---|---|---|
| ingest e2e-a-i1 | A | 1 | 4100 | 1557 | 42 | 0 |
| ingest e2e-a-i2 | A | 1 | 4099 | 1585 | 3 | 0 |
| session g-a1 | A | 13 | 4096 | 3363 | 755 | 0 |
| session g-a1-c16 | A | 13 | 4096 | 3318 | 753 | 0 |
| session g-a2 | A | 13 | 4096 | 3390 | 754 | 0 |
| session g-a2-c16 | A | 13 | 4096 | 3235 | 2066 | 0 |
| ingest e2e-b-i1 | B | 1 | 4098 | 1550 | 29 | 0 |
| ingest e2e-b-i2 | B | 1 | 4098 | 1580 | 6 | 0 |
| session g-b1 | B | 13 | 4096 | 3330 | 699 | 0 |
| session g-b1-c16 | B | 13 | 4096 | 3281 | 2016 | 0 |
| session g-b2 | B | 13 | 4096 | 3394 | 705 | 0 |
| session g-b2-c16 | B | 13 | 4096 | 3366 | 35 | 0 |

## IVF build and detach

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| IVF build time, POST -> job done (s) | 180 180 180 183 | 182 185 183 183 | 180 | 183 | 0.99 | 2 | 2 | within band | build caps A ['4g'] B ['4g'] |
| IVF build container peak (MiB) | 4096 4096 4096 4096 | 4096 4096 4096 4096 | 4096 | 4096 | 1.00 | 0 | 0 | within band |  |
| IVF -> flat detach time (s) | 109 110 110 109 | 109 109 109 109 | 110 | 109 | 1.00 | 1 | 0 | within band | detach caps A ['4g'] B ['4g'] |

## Per-phase memory: anon and swap (windowed samples; cgroup memory.peak is pinned at the limit because it includes page cache)

| Phase | Arm | containers | max anon (MiB) | max swap.current (MiB) | max swap.peak (MiB) | OOM kills |
|---|---|---|---|---|---|---|
| IVF build | A | 4 | 3390 | 2048 | 2066 | 0 |
| IVF -> flat detach | A | 8 | 3066 | 595 | 755 | 0 |
| serving runs (rest of the session) | A | 40 | 1556 | 0 | 0 | 0 |
| IVF build | B | 5 | 3394 | 2016 | 2016 | 0 |
| IVF -> flat detach | B | 8 | 2999 | 704 | 705 | 0 |
| serving runs (rest of the session) | B | 40 | 1555 | 0 | 0 | 0 |

## Swap and OOM per stage and arm

(see the memory peaks table above: max swap.peak and OOM kills per ingest and per session; any swap above 0 MB in a measured run is a finding)

