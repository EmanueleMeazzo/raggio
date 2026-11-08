# turbovec 1.0.0 → 1.1.2 A/B: raw tables (2026-11-08)

Raw output of the analysis scripts for [ADR 0017](../docs/adr/0017-turbovec-1-1.md).
Host: NVIDIA DGX Spark (GB10, 20 arm64 cores), one boot, 2026-11-08 00:39Z–03:17Z.

- **A:** `7947eba`, raggio 0.3.0, turbovec 1.0.0.
- **B:** `5581e4d`, raggio 0.4.0, turbovec 1.1.2.

Both images were built from archives. Their installed packages are identical except
raggio and turbovec: CPython 3.12.15, SQLite 3.53.1, numpy 2.5.3, raggio-native 0.1.0,
fastapi 0.142.2, pydantic 2.13.5, uvicorn 0.54.0. Each figure is the median of the
measured runs; a difference counts as real only when it exceeds both arms' bands
(max minus min).

- **arxiv:** 2,549,119 vectors. Each arm got its own byte copy of the existing
  collection volume, so there was no ingest. Eight guarded sessions at 8 and 16 clients.
- **Documents:** 25,000 Wikipedia articles, 173,320 records. Two ingests per arm,
  four guarded sessions at 8 clients, `doc_id` filter in both arms. Weaviate was not
  re-run.

## Session ledger (host-CPU guard)

Columns: session, arm, pair, concurrency (arxiv only), attempt, flagged, start and
end (epoch seconds), verdict on foreign CPU in cores.

```
g-a1	a	1	8	1	0	1794098370.789187246	1794099092.899488591	clean mean=0.022 max10=0.16 over1=0 n=717 top=python3:6,nvidia-smi:4,polkitd:1
g-a1-c16	a	1	16	1	0	1794099092.933759157	1794099995.748727541	clean mean=0.007 max10=0.02 over1=0 n=898 top=xdg-document-po:1,nvidia-smi:1,containerd:1
g-b1	b	1	8	1	0	1794099995.776406074	1794100718.151599780	clean mean=0.005 max10=0.01 over1=0 n=717 top=xdg-document-po:1,containerd:1,gnome-shell:0
g-b1-c16	b	1	16	1	0	1794100718.181538474	1794101620.794752159	clean mean=0.006 max10=0.02 over1=0 n=897 top=xdg-document-po:1,containerd:1,gnome-shell:1
g-a2	a	2	8	1	0	1794101620.825941951	1794102583.606734110	clean mean=0.012 max10=0.14 over1=0 n=958 top=python3:5,xdg-document-po:1,containerd:1
g-a2-c16	a	2	16	1	0	1794102583.659844676	1794103546.586578014	clean mean=0.005 max10=0.01 over1=0 n=957 top=xdg-document-po:1,containerd:1,gnome-shell:1
g-b2	b	2	8	1	0	1794103546.638757338	1794104449.426534004	clean mean=0.006 max10=0.01 over1=0 n=897 top=xdg-document-po:1,containerd:1,gnome-shell:1
g-b2-c16	b	2	16	1	0	1794104449.459827799	1794105352.472297709	clean mean=0.006 max10=0.01 over1=0 n=897 top=xdg-document-po:1,containerd:1,gnome-shell:1
s-a1	a	1	1	0	1794105770.882395346	1794106283.314686760	clean mean=0.016 max10=0.13 over1=0 n=509 top=python3:6,containerd:0,fwupd:0
s-b1	b	1	1	0	1794106283.431020066	1794106794.474217692	clean mean=0.004 max10=0.01 over1=0 n=509 top=containerd:0,gnome-shell:0,dashboard-servi:0
s-a2	a	2	1	0	1794106794.597351039	1794107312.566936936	clean mean=0.003 max10=0.01 over1=0 n=515 top=containerd:0,dashboard-servi:0,tailscaled:0
s-b2	b	2	1	0	1794107312.671905012	1794107825.883776217	clean mean=0.004 max10=0.01 over1=0 n=511 top=containerd:0,gnome-shell:0,dashboard-servi:0
```

Document ingests (`run.log`):

```
2026-11-08T02:38:59Z ingest-a1: exit 0 74 s 2353 rec/s 25000 148320
2026-11-08T02:40:15Z ingest-b1: exit 0 72 s 2392 rec/s 25000 148320
2026-11-08T02:41:33Z ingest-a2: exit 0 75 s 2323 rec/s 25000 148320
2026-11-08T02:42:50Z ingest-b2: exit 0 74 s 2354 rec/s 25000 148320
```

## arxiv

### Run facts
- stage 3 sessions used: a1 c=8: g-a1, a1 c=16: g-a1-c16, a2 c=8: g-a2, a2 c=16: g-a2-c16, b1 c=8: g-b1, b1 c=16: g-b1-c16, b2 c=8: g-b2, b2 c=16: g-b2-c16
- sessions without a clean attempt: none

### Ingest

Not run: each arm searched a byte copy of the existing collection (see the header).

### True-cold start (fadvise-evicted volume, fresh 4 GiB container)

Seconds from container start to the first `GET /collections/bench` answer (C1), slowest `/healthz` answer during the load (C2, 99 = none in 5 s), slowest of 3 later GETs (C3).

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| flat: start -> first GET (s) | 4.8 4.8 4.7 5.0 | 4.7 5.2 5.2 4.8 | 4.8 | 5.0 | 0.96 | 0.3 | 0.5 | within band |  |
| flat: start -> /healthz up (s) | 0.32 0.42 0.34 0.45 | 0.31 0.43 0.46 0.43 | 0.38 | 0.43 | 0.89 | 0.13 | 0.14 | within band |  |
| flat: slowest /healthz during load (s) | 0.01 0.00 0.00 0.01 | 0.01 0.00 0.00 0.00 | 0.00 | 0.00 | 1.40 | 0.01 | 0.00 | within band |  |
| flat: slowest later GET (s) | 0.463 0.493 0.505 0.465 | 0.501 0.507 0.497 0.496 | 0.479 | 0.499 | 0.96 | 0.042 | 0.011 | within band |  |

(flat: n = 4 A, 4 B)

| ivf: start -> first GET (s) | 5.1 4.8 5.0 5.1 | 5.2 5.2 5.2 5.1 | 5.1 | 5.2 | 0.98 | 0.3 | 0.1 | within band |  |
| ivf: start -> /healthz up (s) | 0.35 0.40 0.32 0.32 | 0.32 0.43 0.32 0.33 | 0.33 | 0.33 | 1.03 | 0.08 | 0.11 | within band |  |
| ivf: slowest /healthz during load (s) | 0.00 0.00 0.00 0.00 | 0.00 0.00 0.01 0.00 | 0.00 | 0.00 | 1.00 | 0.00 | 0.01 | within band |  |
| ivf: slowest later GET (s) | 0.461 0.461 0.505 0.468 | 0.463 0.462 0.475 0.496 | 0.465 | 0.469 | 0.99 | 0.044 | 0.034 | within band |  |

(ivf: n = 4 A, 4 B)

### flat, c=8 (A sessions g-a1, g-a2; B sessions g-b1, g-b2; 6 / 6 measured runs)

### Serial vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Search p50 (ms) | 18.4 18.4 18.8 18.5 18.5 18.4 | 7.9 8.1 7.6 8.0 8.0 8.3 | 18.5 | 8.0 | 2.31 | 0.5 | 0.7 | B better |  |
| Search p95 (ms) | 19.8 19.8 20.2 19.8 19.8 19.8 | 9.3 9.3 8.7 9.7 9.2 9.9 | 19.8 | 9.3 | 2.12 | 0.4 | 1.3 | B better |  |
| Search p99 (ms) | 20.7 20.6 21.0 20.3 20.5 20.3 | 9.9 9.8 9.1 10.4 9.8 10.5 | 20.6 | 9.8 | 2.09 | 0.7 | 1.4 | B better |  |
| QPS serial | 54 54 53 54 54 54 | 127 122 131 123 125 119 | 54 | 124 | 2.30 | 1 | 12 | B better |  |

### Concurrent vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| QPS concurrent | 128 179 170 217 125 127 | 317 319 327 323 335 313 | 149 | 321 | 2.16 | 92 | 23 | B better |  |
| p95 under concurrency (ms) | 69.4 66.5 67.9 63.3 69.5 70.6 | 30.6 31.3 29.0 31.3 32.6 31.1 | 68.6 | 31.2 | 2.20 | 7.3 | 3.6 | B better |  |

### Filtered search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Filtered p50 (ms) | 26.5 23.5 26.7 26.6 25.7 26.1 | 20.9 21.0 23.6 22.4 26.7 20.7 | 26.3 | 21.7 | 1.21 | 3.3 | 6.0 | within band |  |
| Filtered p95 (ms) | 32.0 30.8 28.6 32.3 32.1 31.1 | 25.0 24.8 26.2 24.7 27.4 22.2 | 31.5 | 24.9 | 1.27 | 3.7 | 5.2 | B better |  |

### Hybrid (vector + BM25)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Hybrid p50 (ms) | 47.9 47.8 46.5 47.9 47.8 48.0 | 46.0 45.2 45.4 45.5 45.3 45.2 | 47.8 | 45.4 | 1.05 | 1.6 | 0.9 | B better |  |
| Hybrid p95 (ms) | 72.5 71.6 70.9 71.6 72.8 72.5 | 71.5 70.1 70.7 71.4 70.6 70.6 | 72.1 | 70.6 | 1.02 | 1.9 | 1.5 | within band |  |
| Hybrid p99 (ms) | 96.3 96.6 95.5 95.9 106.7 109.7 | 93.7 94.8 94.6 95.3 94.5 93.9 | 96.4 | 94.6 | 1.02 | 14.2 | 1.6 | within band |  |
| Hybrid QPS serial | 20.6 20.7 21.1 20.5 20.5 20.5 | 21.5 21.7 21.8 21.7 21.7 21.8 | 20.6 | 21.7 | 1.05 | 0.6 | 0.3 | B better |  |
| Hybrid QPS concurrent | 35.8 39.1 34.7 35.7 35.9 40.7 | 36.8 38.3 38.0 37.8 36.9 37.9 | 35.9 | 37.9 | 1.05 | 5.9 | 1.5 | within band |  |
| Hybrid p99 under concurrency (ms) | 447.6 431.0 459.9 496.3 471.5 396.7 | 441.7 423.0 445.6 449.3 432.3 423.7 | 453.8 | 437.0 | 1.04 | 99.5 | 26.4 | within band |  |
| Hybrid first 10 queries, slowest (ms) | 123.2 121.9 121.9 120.8 122.8 122.7 | 119.0 121.6 120.3 119.8 121.5 121.7 | 122.3 | 120.9 | 1.01 | 2.4 | 2.7 | within band |  |
| Hybrid p99 without the first 10 (ms) | 88.3 87.0 86.3 88.3 88.1 87.9 | 86.9 87.1 85.6 87.9 85.8 87.2 | 88.0 | 87.0 | 1.01 | 2.0 | 2.3 | within band |  |

### Recall

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Recall@10 vs exact | 1.000 1.000 1.000 1.000 1.000 1.000 | 1.000 1.000 1.000 1.000 1.000 1.000 | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band |  |
| Hybrid text-hit@10 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 | 0.984 | 1.00 | 0.000 | 0.000 | within band |  |

### Memory (ABAB runs: after a container restart, and under load; podman stats)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Memory after ingest (MB) | 1502 1502 1502 1501 1502 1501 | 1470 1470 1470 1470 1470 1470 | 1502 | 1470 | 1.02 | 1 | 0 | B better |  |
| Memory under query load (MB) | 1506 1506 1506 1506 1506 1506 | 1480 1479 1479 1478 1479 1478 | 1506 | 1479 | 1.02 | 0 | 2 | B better |  |

### CPU per query under concurrency

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| CPU per query under concurrency (ms) | 118.0 74.2 80.1 57.2 117.4 117.4 | 29.0 30.4 28.6 27.9 26.9 30.9 | 98.8 | 28.8 | 3.43 | 60.8 | 4.0 | B better |  |
| Hybrid CPU per query under concurrency (ms) | 340.3 319.7 345.6 339.4 338.3 314.9 | 231.4 223.4 225.7 225.7 231.1 227.0 | 338.9 | 226.4 | 1.50 | 30.8 | 8.0 | B better |  |

### Disk

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Disk footprint (MB) | 16463 16463 16463 16463 16463 16463 | 16463 16463 16463 16463 16463 16463 | 16463 | 16463 | 1.00 | 0 | 0 | within band |  |

### IVF, c=8 (A sessions g-a1, g-a2; B sessions g-b1, g-b2; 6 / 6 measured runs)

### Serial vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Search p50 (ms) | 6.1 5.1 6.1 5.8 6.1 5.8 | 6.8 5.8 6.1 6.3 6.2 5.7 | 6.0 | 6.2 | 0.96 | 1.0 | 1.1 | within band |  |
| Search p95 (ms) | 8.0 6.4 8.0 8.2 7.4 7.2 | 8.6 7.1 7.5 8.9 7.9 7.6 | 7.7 | 7.8 | 0.99 | 1.7 | 1.9 | within band |  |
| Search p99 (ms) | 8.9 7.0 8.7 9.2 8.4 7.8 | 9.7 8.0 8.0 9.7 8.8 8.2 | 8.6 | 8.5 | 1.01 | 2.2 | 1.8 | within band |  |
| QPS serial | 161 193 161 166 162 169 | 145 172 164 152 156 170 | 164 | 160 | 0.97 | 32 | 27 | within band |  |

### Concurrent vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| QPS concurrent | 387 404 408 390 387 378 | 387 411 392 410 387 428 | 389 | 401 | 1.03 | 30 | 41 | within band |  |
| p95 under concurrency (ms) | 26.3 25.3 21.3 23.1 24.3 28.0 | 22.3 23.4 24.7 20.2 23.8 22.2 | 24.8 | 22.8 | 1.09 | 6.7 | 4.5 | within band |  |

### Filtered search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Filtered p50 (ms) | 5.8 4.8 5.7 5.8 5.7 5.7 | 5.7 5.2 5.9 5.6 5.8 6.1 | 5.7 | 5.7 | 1.00 | 1.0 | 1.0 | within band |  |
| Filtered p95 (ms) | 7.1 5.9 6.9 7.5 6.8 6.5 | 6.7 6.4 7.3 7.2 6.8 7.5 | 6.9 | 7.0 | 0.98 | 1.6 | 1.1 | within band |  |

### Hybrid (vector + BM25)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Hybrid p50 (ms) | 44.3 43.8 44.4 44.9 44.4 44.6 | 44.5 44.7 44.3 43.7 44.7 44.9 | 44.4 | 44.6 | 1.00 | 1.1 | 1.2 | within band |  |
| Hybrid p95 (ms) | 70.5 69.3 69.2 70.6 68.7 70.0 | 69.9 69.7 69.3 69.7 70.7 68.9 | 69.6 | 69.7 | 1.00 | 1.9 | 1.8 | within band |  |
| Hybrid p99 (ms) | 96.0 97.8 95.8 96.1 94.4 93.6 | 94.5 91.9 93.5 93.7 95.5 94.0 | 95.9 | 93.8 | 1.02 | 4.3 | 3.7 | within band |  |
| Hybrid QPS serial | 22.2 22.4 22.2 21.9 22.1 22.1 | 22.0 22.0 22.2 22.5 22.1 22.0 | 22.2 | 22.1 | 0.99 | 0.5 | 0.5 | within band |  |
| Hybrid QPS concurrent | 39.1 35.7 38.7 38.9 39.1 38.6 | 38.5 38.7 36.6 38.4 35.9 34.2 | 38.8 | 37.5 | 0.97 | 3.5 | 4.5 | within band |  |
| Hybrid p99 under concurrency (ms) | 433.2 466.9 425.0 433.7 418.0 437.8 | 412.1 431.4 464.8 428.1 481.6 533.4 | 433.4 | 448.1 | 0.97 | 48.9 | 121.4 | within band |  |
| Hybrid first 10 queries, slowest (ms) | 120.7 118.5 120.3 127.3 121.0 121.7 | 121.3 121.7 121.2 119.1 121.8 119.1 | 120.8 | 121.3 | 1.00 | 8.8 | 2.7 | within band |  |
| Hybrid p99 without the first 10 (ms) | 87.3 84.6 87.4 87.1 85.9 85.2 | 86.9 84.5 86.4 86.1 85.9 86.5 | 86.5 | 86.2 | 1.00 | 2.7 | 2.4 | within band |  |

### Recall

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Recall@10 vs exact | 0.994 0.994 0.994 0.995 0.995 0.995 | 0.994 0.994 0.994 0.992 0.992 0.992 | 0.995 | 0.993 | 1.00 | 0.002 | 0.001 | within band |  |
| Hybrid text-hit@10 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 | 0.984 | 1.00 | 0.000 | 0.000 | within band |  |

### Memory (ABAB runs: after a container restart, and under load; podman stats)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Memory after ingest (MB) | 1508 1508 1508 1508 1508 1508 | 1506 1506 1507 1505 1505 1506 | 1508 | 1506 | 1.00 | 0 | 2 | within band |  |
| Memory under query load (MB) | 1515 1517 1516 1515 1515 1515 | 1514 1512 1514 1513 1513 1513 | 1515 | 1513 | 1.00 | 2 | 2 | within band |  |

### CPU per query under concurrency

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| CPU per query under concurrency (ms) | 9.4 10.1 9.5 9.8 9.7 10.0 | 9.4 9.7 9.8 9.2 9.9 10.3 | 9.7 | 9.7 | 1.00 | 0.7 | 1.0 | within band |  |
| Hybrid CPU per query under concurrency (ms) | 198.3 216.7 200.5 198.9 197.2 200.6 | 200.8 198.8 210.7 202.1 215.4 224.6 | 199.7 | 206.4 | 0.97 | 19.6 | 25.9 | within band |  |

### Disk

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Disk footprint (MB) | 16751 16751 16751 16751 16751 16751 | 16751 16751 16751 16751 16751 16751 | 16751 | 16751 | 1.00 | 0 | 0 | within band |  |

### flat, c=16 (A sessions g-a1-c16, g-a2-c16; B sessions g-b1-c16, g-b2-c16; 6 / 6 measured runs)

### Serial vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Search p50 (ms) | 18.6 18.5 18.7 18.1 18.5 18.9 | 8.0 8.2 7.5 8.2 8.0 7.9 | 18.6 | 8.0 | 2.33 | 0.8 | 0.7 | B better |  |
| Search p95 (ms) | 20.1 19.8 20.0 19.7 19.8 20.3 | 9.7 9.6 9.1 9.7 9.0 9.6 | 19.9 | 9.6 | 2.07 | 0.6 | 0.7 | B better |  |
| Search p99 (ms) | 20.7 20.4 20.9 20.6 20.5 21.0 | 10.4 9.9 9.8 10.1 9.4 10.2 | 20.6 | 10.0 | 2.06 | 0.6 | 1.1 | B better |  |
| QPS serial | 53 54 54 55 54 53 | 124 122 132 120 125 123 | 54 | 123 | 2.30 | 2 | 12 | B better |  |

### Concurrent vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| QPS concurrent | 203 200 206 207 212 205 | 432 481 411 411 409 425 | 206 | 418 | 2.03 | 12 | 72 | B better |  |
| p95 under concurrency (ms) | 93.1 89.5 91.8 96.3 96.1 98.6 | 53.9 49.3 55.9 52.5 62.1 64.0 | 94.6 | 54.9 | 1.72 | 9.2 | 14.7 | B better |  |

### Filtered search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Filtered p50 (ms) | 25.5 27.3 27.4 23.9 24.2 26.0 | 24.0 26.9 21.7 17.6 22.1 26.2 | 25.8 | 23.0 | 1.12 | 3.6 | 9.4 | within band |  |
| Filtered p95 (ms) | 30.3 31.6 32.0 27.7 28.3 32.2 | 30.8 30.2 25.7 18.9 24.1 30.8 | 31.0 | 28.0 | 1.11 | 4.4 | 11.9 | within band |  |

### Hybrid (vector + BM25)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Hybrid p50 (ms) | 48.0 46.6 48.5 47.1 45.7 47.4 | 45.6 45.5 45.0 45.6 45.7 45.6 | 47.2 | 45.6 | 1.04 | 2.8 | 0.6 | within band |  |
| Hybrid p95 (ms) | 72.1 71.5 72.9 72.3 70.9 71.2 | 71.9 73.2 72.1 71.7 72.1 70.5 | 71.8 | 72.0 | 1.00 | 2.1 | 2.6 | within band |  |
| Hybrid p99 (ms) | 104.5 95.4 94.6 96.5 96.1 99.5 | 96.2 95.8 94.9 96.4 95.0 99.6 | 96.3 | 96.0 | 1.00 | 10.0 | 4.7 | within band |  |
| Hybrid QPS serial | 20.5 20.9 20.3 20.9 21.4 20.7 | 21.6 21.5 21.8 21.5 21.6 21.6 | 20.8 | 21.6 | 1.04 | 1.1 | 0.3 | within band |  |
| Hybrid QPS concurrent | 34.6 35.9 34.8 34.1 33.9 33.9 | 36.1 37.7 37.3 37.2 36.9 37.6 | 34.4 | 37.3 | 1.08 | 2.0 | 1.6 | B better |  |
| Hybrid p99 under concurrency (ms) | 983.8 933.7 1013.2 960.3 1016.7 1025.6 | 926.4 939.0 909.2 939.9 970.8 899.3 | 998.5 | 932.7 | 1.07 | 91.9 | 71.5 | within band |  |
| Hybrid first 10 queries, slowest (ms) | 121.2 122.0 124.3 121.6 122.0 122.1 | 120.7 120.5 120.3 119.3 119.3 121.3 | 122.0 | 120.4 | 1.01 | 3.1 | 2.0 | within band |  |
| Hybrid p99 without the first 10 (ms) | 88.6 87.2 88.2 89.5 86.8 87.9 | 87.9 89.2 87.2 88.0 85.3 87.7 | 88.1 | 87.8 | 1.00 | 2.7 | 3.8 | within band |  |

### Recall

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Recall@10 vs exact | 1.000 1.000 1.000 1.000 1.000 1.000 | 1.000 1.000 1.000 1.000 1.000 1.000 | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band |  |
| Hybrid text-hit@10 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 | 0.984 | 1.00 | 0.000 | 0.000 | within band |  |

### Memory (ABAB runs: after a container restart, and under load; podman stats)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Memory after ingest (MB) | 1501 1502 1501 1501 1502 1501 | 1470 1470 1470 1470 1470 1470 | 1501 | 1470 | 1.02 | 1 | 0 | B better |  |
| Memory under query load (MB) | 1506 1507 1504 1506 1506 1506 | 1484 1481 1479 1483 1478 1479 | 1506 | 1480 | 1.02 | 3 | 6 | B better |  |

### CPU per query under concurrency

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| CPU per query under concurrency (ms) | 69.3 69.3 69.2 67.0 64.1 68.8 | 21.6 16.8 22.2 22.9 22.1 22.1 | 69.0 | 22.1 | 3.12 | 5.2 | 6.0 | B better |  |
| Hybrid CPU per query under concurrency (ms) | 475.4 464.3 474.0 482.2 481.1 488.5 | 415.2 398.6 402.8 401.0 407.6 401.3 | 478.3 | 402.1 | 1.19 | 24.3 | 16.6 | B better |  |

### Disk

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Disk footprint (MB) | 16463 16463 16463 16463 16463 16463 | 16463 16463 16463 16463 16463 16463 | 16463 | 16463 | 1.00 | 0 | 0 | within band |  |

### IVF, c=16 (A sessions g-a1-c16, g-a2-c16; B sessions g-b1-c16, g-b2-c16; 6 / 6 measured runs)

### Serial vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Search p50 (ms) | 6.5 6.2 6.1 6.6 6.5 5.7 | 6.7 6.3 5.6 6.5 6.2 5.6 | 6.3 | 6.2 | 1.02 | 0.9 | 1.2 | within band |  |
| Search p95 (ms) | 8.7 8.4 7.4 8.3 8.2 7.0 | 8.4 8.5 6.6 8.7 8.0 6.8 | 8.2 | 8.2 | 1.01 | 1.7 | 2.1 | within band |  |
| Search p99 (ms) | 10.0 9.4 8.0 9.3 9.5 7.5 | 9.2 9.4 7.0 9.6 8.9 7.3 | 9.3 | 9.1 | 1.03 | 2.5 | 2.6 | within band |  |
| QPS serial | 152 157 163 151 153 173 | 151 155 178 149 159 177 | 155 | 157 | 1.01 | 22 | 29 | within band |  |

### Concurrent vector search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| QPS concurrent | 461 422 441 420 466 417 | 442 432 443 408 455 468 | 431 | 443 | 1.03 | 49 | 60 | within band |  |
| p95 under concurrency (ms) | 51.7 54.7 47.7 55.7 46.6 60.8 | 59.7 51.1 54.3 64.7 57.4 58.8 | 53.2 | 58.1 | 0.91 | 14.2 | 13.7 | within band |  |

### Filtered search

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Filtered p50 (ms) | 5.7 5.6 5.9 5.5 5.7 5.7 | 5.7 5.9 5.3 5.8 5.7 5.2 | 5.7 | 5.7 | 1.00 | 0.4 | 0.7 | within band |  |
| Filtered p95 (ms) | 7.1 6.9 7.0 6.8 6.9 6.8 | 7.0 6.6 6.3 7.0 6.9 6.6 | 6.9 | 6.8 | 1.02 | 0.3 | 0.8 | within band |  |

### Hybrid (vector + BM25)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Hybrid p50 (ms) | 43.9 44.1 44.8 44.6 44.5 43.4 | 44.6 43.8 44.9 44.5 44.9 43.7 | 44.3 | 44.5 | 0.99 | 1.3 | 1.2 | within band |  |
| Hybrid p95 (ms) | 70.2 67.9 69.7 71.5 69.5 68.3 | 71.1 68.5 70.1 70.7 69.0 68.5 | 69.6 | 69.6 | 1.00 | 3.7 | 2.6 | within band |  |
| Hybrid p99 (ms) | 96.7 92.9 94.7 95.1 95.0 93.5 | 104.4 93.6 92.6 95.4 95.0 95.5 | 94.8 | 95.2 | 1.00 | 3.8 | 11.8 | within band |  |
| Hybrid QPS serial | 22.1 22.4 22.1 22.1 22.0 22.5 | 22.0 22.4 22.0 22.0 22.0 22.5 | 22.1 | 22.0 | 1.00 | 0.5 | 0.5 | within band |  |
| Hybrid QPS concurrent | 38.4 37.0 38.7 37.6 37.9 36.7 | 38.4 38.1 36.9 38.2 37.9 37.7 | 37.7 | 38.0 | 1.01 | 2.0 | 1.5 | within band |  |
| Hybrid p99 under concurrency (ms) | 911.8 847.3 850.8 976.5 923.6 951.1 | 850.8 915.5 921.8 928.1 935.0 933.4 | 917.7 | 925.0 | 0.99 | 129.2 | 84.1 | within band |  |
| Hybrid first 10 queries, slowest (ms) | 120.3 118.5 121.1 120.0 122.2 119.1 | 120.6 126.2 117.7 118.9 118.6 120.4 | 120.2 | 119.6 | 1.00 | 3.7 | 8.6 | within band |  |
| Hybrid p99 without the first 10 (ms) | 89.7 84.2 86.6 86.0 85.4 84.9 | 86.0 86.0 85.3 86.6 85.6 85.4 | 85.7 | 85.8 | 1.00 | 5.5 | 1.4 | within band |  |

### Recall

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Recall@10 vs exact | 0.994 0.994 0.994 0.993 0.993 0.993 | 0.992 0.992 0.992 0.995 0.995 0.995 | 0.994 | 0.993 | 1.00 | 0.000 | 0.002 | within band |  |
| Hybrid text-hit@10 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 0.984 0.984 0.984 0.984 0.984 | 0.984 | 0.984 | 1.00 | 0.000 | 0.000 | within band |  |

### Memory (ABAB runs: after a container restart, and under load; podman stats)

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Memory after ingest (MB) | 1507 1508 1508 1506 1506 1506 | 1508 1508 1508 1506 1507 1507 | 1506 | 1508 | 1.00 | 2 | 2 | within band |  |
| Memory under query load (MB) | 1517 1517 1517 1514 1513 1513 | 1516 1517 1518 1513 1515 1515 | 1516 | 1516 | 1.00 | 4 | 5 | within band |  |

### CPU per query under concurrency

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| CPU per query under concurrency (ms) | 9.9 9.9 9.8 9.9 10.1 9.8 | 9.6 9.4 9.3 9.3 9.4 9.1 | 9.9 | 9.4 | 1.06 | 0.3 | 0.5 | within band |  |
| Hybrid CPU per query under concurrency (ms) | 384.9 397.6 378.7 393.4 389.6 400.3 | 383.7 385.3 402.1 383.6 391.8 392.3 | 391.5 | 388.6 | 1.01 | 21.6 | 18.5 | within band |  |

### Disk

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| Disk footprint (MB) | 16751 16751 16751 16751 16751 16751 | 16751 16751 16751 16751 16751 16751 | 16751 | 16751 | 1.00 | 0 | 0 | within band |  |

### Memory peaks, swap and OOM (cgroup, 1 Hz sampler; peak = memory.peak of the container at its last sample)

| Step | Arm | containers sampled | max cgroup peak (MiB) | max anon (MiB) | max swap.peak (MiB) | OOM kills |
|---|---|---|---|---|---|---|
| session g-a1 | A | 13 | 4096 | 3378 | 774 | 0 |
| session g-a1-c16 | A | 13 | 4096 | 3377 | 779 | 0 |
| session g-a2 | A | 13 | 4096 | 3391 | 774 | 0 |
| session g-a2-c16 | A | 13 | 4096 | 3252 | 2059 | 0 |
| session g-b1 | B | 13 | 4096 | 3315 | 796 | 0 |
| session g-b1-c16 | B | 13 | 4096 | 3389 | 313 | 0 |
| session g-b2 | B | 13 | 4096 | 3347 | 789 | 0 |
| session g-b2-c16 | B | 13 | 4096 | 3370 | 778 | 0 |

### IVF build and detach

| Metric | A per-run | B per-run | A median | B median | ratio (>1 = B better) | A band | B band | exceeds both bands | note |
|---|---|---|---|---|---|---|---|---|---|
| IVF build time, POST -> job done (s) | 40 168 176 180 | 46 178 180 178 | 172 | 178 | 0.96 | 141 | 135 | within band | build caps A ['4g'] B ['4g'] |
| IVF build container peak (MiB) | 4096 4096 4096 4096 | 4096 4096 4096 4096 | 4096 | 4096 | 1.00 | 0 | 0 | within band |  |
| IVF -> flat detach time (s) | 78 94 110 110 | 94 93 94 94 | 102 | 94 | 1.09 | 32 | 1 | within band | detach caps A ['4g'] B ['4g'] |

### Per-phase memory: anon and swap (windowed samples; cgroup memory.peak is pinned at the limit because it includes page cache)

| Phase | Arm | containers | max anon (MiB) | max swap.current (MiB) | max swap.peak (MiB) | OOM kills |
|---|---|---|---|---|---|---|
| IVF build | A | 4 | 3391 | 2048 | 2059 | 0 |
| IVF -> flat detach | A | 8 | 3043 | 730 | 803 | 0 |
| serving runs (rest of the session) | A | 40 | 1561 | 0 | 0 | 0 |
| IVF build | B | 4 | 3389 | 35 | 35 | 0 |
| IVF -> flat detach | B | 8 | 3027 | 796 | 796 | 0 |
| serving runs (rest of the session) | B | 40 | 1556 | 0 | 0 | 0 |

### Swap and OOM per stage and arm

(see the memory peaks table above: max swap.peak and OOM kills per ingest and per session; any swap above 0 MB in a measured run is a finding)

### arxiv run timeline

Per run: serial p50 (ms), serial QPS, concurrent QPS, concurrent CPU per query (ms), hybrid p50 (ms), hybrid serial QPS, hybrid concurrent CPU per query (ms).

```
start                 session      run  engine       p50 qps_s qps_c  cpu_c  hyb50 hqps_s   hcpu
2026-11-08T00:40:28Z  g-a1         run0 raggio-ivf  11.2    85   374    9.6   64.0   14.0  200.9
2026-11-08T00:42:08Z  g-a1         run1 raggio-ivf   6.1   161   387    9.4   44.3   22.2  198.3
2026-11-08T00:43:21Z  g-a1         run2 raggio-ivf   5.1   193   404   10.1   43.8   22.4  216.7
2026-11-08T00:44:19Z  g-a1         run3 raggio-ivf   6.1   161   408    9.5   44.4   22.2  200.5
2026-11-08T00:46:40Z  g-a1         run0 raggio      18.7    53   127  117.5   46.9   20.9  323.3
2026-11-08T00:47:49Z  g-a1         run1 raggio      18.4    54   128  118.0   47.9   20.6  340.3
2026-11-08T00:49:00Z  g-a1         run2 raggio      18.4    54   179   74.2   47.8   20.7  319.7
2026-11-08T00:50:07Z  g-a1         run3 raggio      18.8    53   170   80.1   46.5   21.1  345.6
2026-11-08T00:54:39Z  g-a1-c16     run0 raggio-ivf  11.6    83   452    9.7   68.6   12.9  386.5
2026-11-08T00:56:28Z  g-a1-c16     run1 raggio-ivf   6.5   152   461    9.9   43.9   22.1  384.9
2026-11-08T00:57:47Z  g-a1-c16     run2 raggio-ivf   6.2   157   422    9.9   44.1   22.4  397.6
2026-11-08T00:58:47Z  g-a1-c16     run3 raggio-ivf   6.1   163   441    9.8   44.8   22.1  378.7
2026-11-08T01:01:21Z  g-a1-c16     run0 raggio      18.6    53   204   67.1   48.0   20.6  485.5
2026-11-08T01:02:32Z  g-a1-c16     run1 raggio      18.6    53   203   69.3   48.0   20.5  475.4
2026-11-08T01:03:41Z  g-a1-c16     run2 raggio      18.5    54   200   69.3   46.6   20.9  464.3
2026-11-08T01:04:50Z  g-a1-c16     run3 raggio      18.7    54   206   69.2   48.5   20.3  474.0
2026-11-08T01:07:40Z  g-b1         run0 raggio-ivf  12.8    76   377   10.0   69.7   12.6  199.5
2026-11-08T01:09:32Z  g-b1         run1 raggio-ivf   6.8   145   387    9.4   44.5   22.0  200.8
2026-11-08T01:10:52Z  g-b1         run2 raggio-ivf   5.8   172   411    9.7   44.7   22.0  198.8
2026-11-08T01:11:51Z  g-b1         run3 raggio-ivf   6.1   164   392    9.8   44.3   22.2  210.7
2026-11-08T01:14:27Z  g-b1         run0 raggio       8.1   124   309   31.0   45.4   21.6  235.8
2026-11-08T01:15:28Z  g-b1         run1 raggio       7.9   127   317   29.0   46.0   21.5  231.4
2026-11-08T01:16:29Z  g-b1         run2 raggio       8.1   122   319   30.4   45.2   21.7  223.4
2026-11-08T01:17:29Z  g-b1         run3 raggio       7.6   131   327   28.6   45.4   21.8  225.7
2026-11-08T01:21:55Z  g-b1-c16     run0 raggio-ivf  12.8    77   414    9.7   70.3   12.7  379.1
2026-11-08T01:23:47Z  g-b1-c16     run1 raggio-ivf   6.7   151   442    9.6   44.6   22.0  383.7
2026-11-08T01:25:07Z  g-b1-c16     run2 raggio-ivf   6.3   155   432    9.4   43.8   22.4  385.3
2026-11-08T01:26:07Z  g-b1-c16     run3 raggio-ivf   5.6   178   443    9.3   44.9   22.0  402.1
2026-11-08T01:28:42Z  g-b1-c16     run0 raggio       7.8   125   436   20.8   45.3   21.6  413.1
2026-11-08T01:29:43Z  g-b1-c16     run1 raggio       8.0   124   432   21.6   45.6   21.6  415.2
2026-11-08T01:30:45Z  g-b1-c16     run2 raggio       8.2   122   481   16.8   45.5   21.5  398.6
2026-11-08T01:31:46Z  g-b1-c16     run3 raggio       7.5   132   411   22.2   45.0   21.8  402.8
2026-11-08T01:36:55Z  g-a2         run0 raggio-ivf  13.1    75   393    9.3   70.6   12.5  199.1
2026-11-08T01:38:48Z  g-a2         run1 raggio-ivf   5.8   166   390    9.8   44.9   21.9  198.9
2026-11-08T01:40:08Z  g-a2         run2 raggio-ivf   6.1   162   387    9.7   44.4   22.1  197.2
2026-11-08T01:41:09Z  g-a2         run3 raggio-ivf   5.8   169   378   10.0   44.6   22.1  200.6
2026-11-08T01:44:00Z  g-a2         run0 raggio      18.6    53   127  115.8   47.2   20.9  343.5
2026-11-08T01:45:11Z  g-a2         run1 raggio      18.5    54   217   57.2   47.9   20.5  339.4
2026-11-08T01:46:20Z  g-a2         run2 raggio      18.5    54   125  117.4   47.8   20.5  338.3
2026-11-08T01:47:30Z  g-a2         run3 raggio      18.4    54   127  117.4   48.0   20.5  314.9
2026-11-08T01:53:03Z  g-a2-c16     run0 raggio-ivf  12.9    76   483    9.5   70.6   12.6  393.9
2026-11-08T01:54:56Z  g-a2-c16     run1 raggio-ivf   6.6   151   420    9.9   44.6   22.1  393.4
2026-11-08T01:56:18Z  g-a2-c16     run2 raggio-ivf   6.5   153   466   10.1   44.5   22.0  389.6
2026-11-08T01:57:19Z  g-a2-c16     run3 raggio-ivf   5.7   173   417    9.8   43.4   22.5  400.3
2026-11-08T02:00:10Z  g-a2-c16     run0 raggio      18.7    53   227   59.2   48.0   20.7  473.2
2026-11-08T02:01:19Z  g-a2-c16     run1 raggio      18.1    55   207   67.0   47.1   20.9  482.2
2026-11-08T02:02:28Z  g-a2-c16     run2 raggio      18.5    54   212   64.1   45.7   21.4  481.1
2026-11-08T02:03:36Z  g-a2-c16     run3 raggio      18.9    53   205   68.8   47.4   20.7  488.5
2026-11-08T02:09:06Z  g-b2         run0 raggio-ivf  12.6    78   367    9.8   70.4   12.5  206.0
2026-11-08T02:10:59Z  g-b2         run1 raggio-ivf   6.3   152   410    9.2   43.7   22.5  202.1
2026-11-08T02:12:19Z  g-b2         run2 raggio-ivf   6.2   156   387    9.9   44.7   22.1  215.4
2026-11-08T02:13:20Z  g-b2         run3 raggio-ivf   5.7   170   428   10.3   44.9   22.0  224.6
2026-11-08T02:15:58Z  g-b2         run0 raggio       8.3   118   312   30.4   45.5   21.5  220.5
2026-11-08T02:16:58Z  g-b2         run1 raggio       8.0   123   323   27.9   45.5   21.7  225.7
2026-11-08T02:17:59Z  g-b2         run2 raggio       8.0   125   335   26.9   45.3   21.7  231.1
2026-11-08T02:19:01Z  g-b2         run3 raggio       8.3   119   313   30.9   45.2   21.8  227.0
2026-11-08T02:24:07Z  g-b2-c16     run0 raggio-ivf  12.1    79   418   10.0   70.9   12.5  390.4
2026-11-08T02:25:59Z  g-b2-c16     run1 raggio-ivf   6.5   149   408    9.3   44.5   22.0  383.6
2026-11-08T02:27:19Z  g-b2-c16     run2 raggio-ivf   6.2   159   455    9.4   44.9   22.0  391.8
2026-11-08T02:28:20Z  g-b2-c16     run3 raggio-ivf   5.6   177   468    9.1   43.7   22.5  392.3
2026-11-08T02:30:54Z  g-b2-c16     run0 raggio       7.9   124   435   21.6   45.7   21.5  411.7
2026-11-08T02:31:56Z  g-b2-c16     run1 raggio       8.2   120   411   22.9   45.6   21.5  401.0
2026-11-08T02:32:55Z  g-b2-c16     run2 raggio       8.0   125   409   22.1   45.7   21.6  407.6
2026-11-08T02:33:56Z  g-b2-c16     run3 raggio       7.9   123   425   22.1   45.6   21.6  401.3
```

## Documents

### Run facts

- sessions used: A ['s-a1', 's-a2'], B ['s-b1', 's-b2'], Weaviate None

### Ingest (fresh 4 GiB container, DELETE + create + ingest; 2 per arm)

| Metric | A runs | B runs | A median | B median |
|---|---|---|---|---|
| Ingest wall time (s) | 74 75 | 72 74 | 74 | 73 |
| Ingest (records/s) | 2353 2323 | 2392 2354 | 2338 | 2373 |
| Memory after ingest (MB) | 322 309 | 325 326 | 316 | 325 |
| Disk (MB) | 2066 2065 | 2066 2066 | 2066 | 2066 |

### IVF build and detach (s)

- s-a1: build 4.1 s, detach 2.1 s
- s-a2: build 4.1 s, detach 2.1 s
- s-b1: build 4.1 s, detach 2.1 s
- s-b2: build 4.1 s, detach 2.1 s

### raggio flat (6 / 6 measured runs)

| Workload | Metric | A median | B median | ratio (>1 = B better) | A band | B band | verdict | Weaviate |
|---|---|---|---|---|---|---|---|---|
| summary-vector | p50 (ms) | 7.7 | 6.9 | 1.12 | 0.8 | 1.3 | within band | - |
| summary-vector | p95 (ms) | 9.5 | 8.0 | 1.18 | 1.1 | 1.1 | B better | - |
| summary-vector | p99 (ms) | 10.2 | 8.6 | 1.19 | 0.9 | 1.5 | B better | - |
| summary-vector | QPS serial | 129.3 | 145.4 | 1.12 | 12.5 | 29.1 | within band | - |
| summary-vector | QPS concurrent (8) | 472.8 | 522.1 | 1.10 | 94.6 | 31.2 | within band | - |
| summary-vector | p95 under concurrency (ms) | 19.3 | 21.0 | 0.92 | 5.0 | 1.5 | within band | - |
| summary-vector | server CPU per query under concurrency (ms) | 7.0 | 4.5 | 1.55 | 2.1 | 0.4 | B better | - |
| summary-vector | quality (hit rate / recall@k) | 0.996 | 0.996 | 1.00 | 0.000 | 0.000 | within band | - |
| summary-vector | response size (KB) | 15 | 15 | 1.00 | 0 | 0 | within band | - |
| summary-hybrid | p50 (ms) | 10.0 | 9.2 | 1.08 | 0.4 | 1.0 | within band | - |
| summary-hybrid | p95 (ms) | 25.1 | 23.0 | 1.09 | 3.5 | 2.8 | within band | - |
| summary-hybrid | p99 (ms) | 43.4 | 45.2 | 0.96 | 12.8 | 9.7 | within band | - |
| summary-hybrid | QPS serial | 82.2 | 87.7 | 1.07 | 4.3 | 9.7 | within band | - |
| summary-hybrid | QPS concurrent (8) | 220.3 | 242.1 | 1.10 | 5.4 | 6.1 | B better | - |
| summary-hybrid | p95 under concurrency (ms) | 43.9 | 42.6 | 1.03 | 6.4 | 3.9 | within band | - |
| summary-hybrid | server CPU per query under concurrency (ms) | 13.6 | 12.7 | 1.07 | 0.8 | 0.5 | B better | - |
| summary-hybrid | quality (hit rate / recall@k) | 0.994 | 0.994 | 1.00 | 0.000 | 0.000 | within band | - |
| summary-hybrid | response size (KB) | 15 | 15 | 1.00 | 0 | 0 | within band | - |
| indoc-vector | p50 (ms) | 2.9 | 3.2 | 0.88 | 1.8 | 1.2 | within band | - |
| indoc-vector | p95 (ms) | 3.6 | 4.5 | 0.81 | 2.0 | 2.2 | within band | - |
| indoc-vector | p99 (ms) | 4.3 | 4.8 | 0.89 | 1.8 | 2.5 | within band | - |
| indoc-vector | QPS serial | 337.3 | 294.5 | 0.87 | 196.1 | 148.9 | within band | - |
| indoc-vector | QPS concurrent (8) | 873.7 | 1075.9 | 1.23 | 295.8 | 355.0 | within band | - |
| indoc-vector | p95 under concurrency (ms) | 12.8 | 13.7 | 0.94 | 3.0 | 2.6 | within band | - |
| indoc-vector | server CPU per query under concurrency (ms) | 1.4 | 1.2 | 1.21 | 0.6 | 0.4 | within band | - |
| indoc-vector | quality (hit rate / recall@k) | 0.993 | 0.993 | 1.00 | 0.000 | 0.000 | within band | - |
| indoc-vector | response size (KB) | 24 | 24 | 1.00 | 0 | 0 | within band | - |
| indoc-hybrid | p50 (ms) | 5.4 | 5.7 | 0.95 | 0.9 | 0.7 | within band | - |
| indoc-hybrid | p95 (ms) | 10.0 | 11.0 | 0.91 | 1.9 | 1.4 | within band | - |
| indoc-hybrid | p99 (ms) | 16.5 | 17.4 | 0.95 | 4.6 | 3.1 | within band | - |
| indoc-hybrid | QPS serial | 162.5 | 159.5 | 0.98 | 22.3 | 10.1 | within band | - |
| indoc-hybrid | QPS concurrent (8) | 702.0 | 613.2 | 0.87 | 241.5 | 204.4 | within band | - |
| indoc-hybrid | p95 under concurrency (ms) | 16.9 | 19.1 | 0.88 | 8.4 | 10.9 | within band | - |
| indoc-hybrid | server CPU per query under concurrency (ms) | 2.4 | 2.5 | 0.97 | 0.7 | 0.6 | within band | - |
| indoc-hybrid | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | - |
| indoc-hybrid | response size (KB) | 24 | 24 | 1.00 | 0 | 0 | within band | - |
| chunks-k10 | p50 (ms) | 12.1 | 12.2 | 1.00 | 1.4 | 2.8 | within band | - |
| chunks-k10 | p95 (ms) | 14.7 | 14.2 | 1.04 | 2.0 | 1.5 | within band | - |
| chunks-k10 | p99 (ms) | 15.8 | 14.9 | 1.06 | 2.2 | 1.6 | within band | - |
| chunks-k10 | QPS serial | 80.2 | 81.6 | 1.02 | 10.0 | 18.6 | within band | - |
| chunks-k10 | QPS concurrent (8) | 340.2 | 337.7 | 0.99 | 100.8 | 67.0 | within band | - |
| chunks-k10 | p95 under concurrency (ms) | 27.4 | 26.9 | 1.02 | 5.5 | 2.7 | within band | - |
| chunks-k10 | server CPU per query under concurrency (ms) | 8.4 | 6.1 | 1.38 | 3.4 | 0.5 | within band | - |
| chunks-k10 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | - |
| chunks-k10 | response size (KB) | 52 | 52 | 1.00 | 0 | 0 | within band | - |
| chunks-k100 | p50 (ms) | 18.0 | 19.4 | 0.93 | 3.8 | 1.7 | within band | - |
| chunks-k100 | p95 (ms) | 20.8 | 21.3 | 0.97 | 3.6 | 2.6 | within band | - |
| chunks-k100 | p99 (ms) | 21.7 | 22.7 | 0.96 | 4.2 | 2.0 | within band | - |
| chunks-k100 | QPS serial | 53.3 | 49.9 | 0.94 | 11.3 | 3.6 | within band | - |
| chunks-k100 | QPS concurrent (8) | 176.3 | 179.1 | 1.02 | 27.2 | 17.3 | within band | - |
| chunks-k100 | p95 under concurrency (ms) | 56.2 | 55.3 | 1.02 | 8.8 | 11.6 | within band | - |
| chunks-k100 | server CPU per query under concurrency (ms) | 10.8 | 10.0 | 1.08 | 2.8 | 1.2 | within band | - |
| chunks-k100 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | - |
| chunks-k100 | response size (KB) | 522 | 522 | 1.00 | 0 | 0 | within band | - |
| chunks-k1000 | p50 (ms) | 69.4 | 67.9 | 1.02 | 4.0 | 6.2 | within band | - |
| chunks-k1000 | p95 (ms) | 77.3 | 76.2 | 1.01 | 4.6 | 5.8 | within band | - |
| chunks-k1000 | p99 (ms) | 80.5 | 80.6 | 1.00 | 10.1 | 4.6 | within band | - |
| chunks-k1000 | QPS serial | 12.7 | 13.0 | 1.02 | 1.5 | 1.2 | within band | - |
| chunks-k1000 | QPS concurrent (8) | 21.5 | 21.4 | 1.00 | 0.9 | 1.0 | within band | - |
| chunks-k1000 | p95 under concurrency (ms) | 390.5 | 390.2 | 1.00 | 20.0 | 27.5 | within band | - |
| chunks-k1000 | server CPU per query under concurrency (ms) | 64.7 | 61.0 | 1.06 | 1.8 | 7.6 | within band | - |
| chunks-k1000 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | - |
| chunks-k1000 | response size (KB) | 5208 | 5208 | 1.00 | 0 | 0 | within band | - |

Memory after the run (podman stats, MB): A 475, B 489

### raggio IVF (6 / 6 measured runs)

| Workload | Metric | A median | B median | ratio (>1 = B better) | A band | B band | verdict | Weaviate |
|---|---|---|---|---|---|---|---|---|
| summary-vector | p50 (ms) | 6.8 | 6.4 | 1.06 | 0.7 | 0.8 | within band | - |
| summary-vector | p95 (ms) | 7.7 | 7.3 | 1.06 | 0.7 | 0.9 | within band | - |
| summary-vector | p99 (ms) | 8.2 | 7.9 | 1.04 | 0.4 | 0.9 | within band | - |
| summary-vector | QPS serial | 146.1 | 153.3 | 1.05 | 14.4 | 19.2 | within band | - |
| summary-vector | QPS concurrent (8) | 325.3 | 323.7 | 1.00 | 36.5 | 33.0 | within band | - |
| summary-vector | p95 under concurrency (ms) | 29.3 | 27.8 | 1.05 | 2.6 | 4.6 | within band | - |
| summary-vector | server CPU per query under concurrency (ms) | 13.6 | 13.2 | 1.03 | 0.9 | 0.7 | within band | - |
| summary-vector | quality (hit rate / recall@k) | 0.996 | 0.996 | 1.00 | 0.000 | 0.000 | within band | - |
| summary-vector | response size (KB) | 15 | 15 | 1.00 | 0 | 0 | within band | - |
| summary-hybrid | p50 (ms) | 9.5 | 9.3 | 1.02 | 1.2 | 1.5 | within band | - |
| summary-hybrid | p95 (ms) | 23.9 | 24.5 | 0.97 | 5.1 | 7.1 | within band | - |
| summary-hybrid | p99 (ms) | 40.6 | 42.5 | 0.96 | 15.6 | 17.4 | within band | - |
| summary-hybrid | QPS serial | 83.9 | 84.7 | 1.01 | 12.3 | 19.5 | within band | - |
| summary-hybrid | QPS concurrent (8) | 180.2 | 181.6 | 1.01 | 8.9 | 7.9 | within band | - |
| summary-hybrid | p95 under concurrency (ms) | 60.0 | 61.8 | 0.97 | 12.5 | 5.4 | within band | - |
| summary-hybrid | server CPU per query under concurrency (ms) | 24.1 | 24.4 | 0.99 | 0.5 | 1.0 | within band | - |
| summary-hybrid | quality (hit rate / recall@k) | 0.994 | 0.994 | 1.00 | 0.000 | 0.000 | within band | - |
| summary-hybrid | response size (KB) | 15 | 15 | 1.00 | 0 | 0 | within band | - |
| indoc-vector | p50 (ms) | 3.4 | 3.4 | 1.00 | 1.2 | 1.4 | within band | - |
| indoc-vector | p95 (ms) | 4.5 | 4.4 | 1.03 | 1.3 | 1.9 | within band | - |
| indoc-vector | p99 (ms) | 5.2 | 5.1 | 1.02 | 1.1 | 2.4 | within band | - |
| indoc-vector | QPS serial | 284.2 | 286.3 | 1.01 | 104.9 | 131.4 | within band | - |
| indoc-vector | QPS concurrent (8) | 953.1 | 847.2 | 0.89 | 464.7 | 169.1 | within band | - |
| indoc-vector | p95 under concurrency (ms) | 11.4 | 13.4 | 0.85 | 7.5 | 3.5 | within band | - |
| indoc-vector | server CPU per query under concurrency (ms) | 1.5 | 1.6 | 0.96 | 0.5 | 0.5 | within band | - |
| indoc-vector | quality (hit rate / recall@k) | 0.993 | 0.993 | 1.00 | 0.000 | 0.000 | within band | - |
| indoc-vector | response size (KB) | 24 | 24 | 1.00 | 0 | 0 | within band | - |
| indoc-hybrid | p50 (ms) | 5.8 | 5.7 | 1.02 | 1.4 | 0.6 | within band | - |
| indoc-hybrid | p95 (ms) | 10.9 | 11.1 | 0.98 | 1.7 | 1.9 | within band | - |
| indoc-hybrid | p99 (ms) | 16.3 | 17.1 | 0.95 | 5.8 | 4.8 | within band | - |
| indoc-hybrid | QPS serial | 152.5 | 157.4 | 1.03 | 34.5 | 16.3 | within band | - |
| indoc-hybrid | QPS concurrent (8) | 571.7 | 682.9 | 1.19 | 192.5 | 161.7 | within band | - |
| indoc-hybrid | p95 under concurrency (ms) | 18.5 | 17.4 | 1.06 | 9.3 | 5.8 | within band | - |
| indoc-hybrid | server CPU per query under concurrency (ms) | 2.8 | 2.6 | 1.07 | 0.7 | 0.5 | within band | - |
| indoc-hybrid | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | - |
| indoc-hybrid | response size (KB) | 24 | 24 | 1.00 | 0 | 0 | within band | - |
| chunks-k10 | p50 (ms) | 7.1 | 7.0 | 1.01 | 0.8 | 0.6 | within band | - |
| chunks-k10 | p95 (ms) | 8.1 | 8.1 | 1.01 | 0.9 | 0.6 | within band | - |
| chunks-k10 | p99 (ms) | 8.6 | 8.8 | 0.98 | 0.8 | 0.8 | within band | - |
| chunks-k10 | QPS serial | 136.9 | 137.5 | 1.00 | 14.2 | 13.3 | within band | - |
| chunks-k10 | QPS concurrent (8) | 289.4 | 305.9 | 1.06 | 28.5 | 46.8 | within band | - |
| chunks-k10 | p95 under concurrency (ms) | 32.4 | 31.4 | 1.03 | 2.1 | 7.2 | within band | - |
| chunks-k10 | server CPU per query under concurrency (ms) | 16.3 | 15.8 | 1.03 | 1.1 | 1.7 | within band | - |
| chunks-k10 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | - |
| chunks-k10 | response size (KB) | 52 | 52 | 1.00 | 0 | 0 | within band | - |
| chunks-k100 | p50 (ms) | 12.9 | 13.2 | 0.98 | 2.5 | 2.5 | within band | - |
| chunks-k100 | p95 (ms) | 14.3 | 14.4 | 0.99 | 2.5 | 2.3 | within band | - |
| chunks-k100 | p99 (ms) | 15.0 | 15.3 | 0.98 | 2.4 | 2.2 | within band | - |
| chunks-k100 | QPS serial | 74.1 | 71.7 | 0.97 | 13.8 | 14.8 | within band | - |
| chunks-k100 | QPS concurrent (8) | 143.7 | 143.7 | 1.00 | 15.4 | 5.8 | within band | - |
| chunks-k100 | p95 under concurrency (ms) | 66.9 | 69.5 | 0.96 | 16.5 | 7.6 | within band | - |
| chunks-k100 | server CPU per query under concurrency (ms) | 22.5 | 22.6 | 1.00 | 1.2 | 0.9 | within band | - |
| chunks-k100 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | - |
| chunks-k100 | response size (KB) | 522 | 522 | 1.00 | 0 | 0 | within band | - |
| chunks-k1000 | p50 (ms) | 63.9 | 62.3 | 1.03 | 2.3 | 4.4 | within band | - |
| chunks-k1000 | p95 (ms) | 70.7 | 68.4 | 1.03 | 1.4 | 4.3 | within band | - |
| chunks-k1000 | p99 (ms) | 74.3 | 70.4 | 1.06 | 4.4 | 4.3 | within band | - |
| chunks-k1000 | QPS serial | 14.0 | 14.6 | 1.04 | 0.8 | 1.6 | within band | - |
| chunks-k1000 | QPS concurrent (8) | 20.5 | 20.6 | 1.01 | 1.1 | 1.4 | within band | - |
| chunks-k1000 | p95 under concurrency (ms) | 406.9 | 411.1 | 0.99 | 26.8 | 32.3 | within band | - |
| chunks-k1000 | server CPU per query under concurrency (ms) | 140.5 | 138.8 | 1.01 | 3.3 | 2.6 | within band | - |
| chunks-k1000 | quality (hit rate / recall@k) | 1.000 | 1.000 | 1.00 | 0.000 | 0.000 | within band | - |
| chunks-k1000 | response size (KB) | 5208 | 5208 | 1.00 | 0 | 0 | within band | - |

Memory after the run (podman stats, MB): A 480, B 481

### Documents: server memory per run (MB, `podman stats`)

Before the queries → after the workload set, measured runs 1–3 of each session.

| Engine | Session | Run 1 | Run 2 | Run 3 |
|---|---|---|---|---|
| raggio | s-a1 | 150.6 → 474.9 | 150.8 → 476.8 | 150.5 → 475.4 |
| raggio | s-a2 | 150.7 → 473.8 | 150.5 → 474.8 | 150.7 → 474.5 |
| raggio | s-b1 | 148.4 → 486.7 | 148.4 → 488.7 | 148.4 → 489.9 |
| raggio | s-b2 | 148.4 → 491.1 | 148.4 → 489.0 | 148.4 → 486.3 |
| raggio-ivf | s-a1 | 151.1 → 478.8 | 151.0 → 478.8 | 151.0 → 483.4 |
| raggio-ivf | s-a2 | 150.9 → 480.2 | 150.9 → 481.9 | 150.8 → 478.8 |
| raggio-ivf | s-b1 | 150.7 → 483.1 | 150.8 → 481.0 | 150.6 → 475.9 |
| raggio-ivf | s-b2 | 150.7 → 481.1 | 150.7 → 481.3 | 150.8 → 479.2 |

### Quality at full precision

The tables above round to three decimals. Every measured run, by arm (sets of the
distinct values over 6 runs):

| Engine | Workload (n, k) | A | B |
|---|---|---|---|
| raggio | chunks (500, k=100) recall@100 | 0.99988 | 0.99986 (s-b1), 0.99984 (s-b2) |
| raggio | chunks (200, k=1000) recall@1000 | 0.999895 | 0.999895 |
| raggio | chunks (500, k=10) recall@10 | 1.0 | 1.0 |
| raggio | summaries vector / hybrid hit rate | 0.996 / 0.994 | 0.996 / 0.994 |
| raggio | one document vector / hybrid hit rate | 0.993333 / 1.0 | 0.993333 / 1.0 |
| raggio-ivf | every workload | as raggio A, with recall@100 0.99988 | identical to A |

arxiv: flat recall@10 0.9998 in every run of both arms; IVF recall@10 per session
A 0.9938, 0.9954, 0.9938, 0.9934 and B 0.9936, 0.9924, 0.9922, 0.9946 (g-x1, g-x2,
g-x1-c16, g-x2-c16).
