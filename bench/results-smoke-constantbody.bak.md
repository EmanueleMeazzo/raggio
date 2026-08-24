# raggio vs Weaviate — 19,900 vectors x 1536 dims (real email embeddings)

Config: k=10, 100 held-out corpus queries, concurrency 8, seed 42. raggio container capped at 1 GiB (4-bit quantized flat index), Weaviate at 6 GiB (HNSW, float32, defaults). Podman WSL VM: 8 GB RAM / 4 CPUs shared.

| Metric | raggio | weaviate |
|---|---|---|
| Ingest wall time (s) | 19 | 10 |
| Ingest throughput (vec/s) | 1075 | 2007 |
| Memory after ingest (MB) | 140 | 638 |
| Memory under query load (MB) | 125 | 744 |
| Disk footprint (MB) | 50 | 0 |
| Search p50 (ms) | 15.3 | 8.0 |
| Search p95 (ms) | 17.9 | 12.4 |
| Search p99 (ms) | 268.3 | 270.5 |
| QPS serial | 55 | 89 |
| QPS concurrent | 76 | 177 |
| p95 under concurrency (ms) | 383.4 | 281.2 |
| Filtered p50 (ms) | 16.8 | 14.9 |
| Filtered p95 (ms) | 21.4 | 20.5 |
| Recall@10 vs exact | 0.961 | 1.000 |
| Cold start to first query (s) | 2.3 | 7.7 |

Notes: raggio = single Python asyncio process, brute-force scan over 4-bit quantized vectors, per-collection lock. Weaviate = Go, HNSW approximate index, uncompressed vectors. Both queried via REST with client-supplied vectors; identical stored payloads.