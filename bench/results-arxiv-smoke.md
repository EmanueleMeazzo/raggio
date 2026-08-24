# raggio vs Weaviate — 19,900 vectors x 1024 dims (real abstracts, Qwen/Qwen3-Embedding-0.6B @a2c6afb5)

Config: k=10, 100 held-out corpus queries, concurrency 8, seed 42. raggio container capped at 4 GiB (4-bit quantized flat index), Weaviate at 32 GiB (HNSW, float32, defaults). Host: Linux/aarch64, 20 CPUs. Hybrid: raggio unweighted RRF (k=60) vs Weaviate rankedFusion alpha 0.5 over the body property; query = held-out vector + title of a sampled ingested doc.

| Metric | raggio | weaviate |
|---|---|---|
| Ingest wall time (s) | 13 | 8 |
| Ingest throughput (vec/s) | 1533 | 2536 |
| Memory after ingest (MB) | 202 | 707 |
| Memory under query load (MB) | 202 | 743 |
| Disk footprint (MB) | 155 | 340 |
| Search p50 (ms) | 3.7 | 6.7 |
| Search p95 (ms) | 4.8 | 7.8 |
| Search p99 (ms) | 5.4 | 13.1 |
| QPS serial | 268 | 159 |
| QPS concurrent | 976 | 794 |
| p95 under concurrency (ms) | 16.5 | 17.5 |
| Filtered p50 (ms) | 3.2 | 6.6 |
| Filtered p95 (ms) | 4.1 | 7.7 |
| Recall@10 vs exact | 0.961 | 1.000 |
| Hybrid p50 (ms) | 6.1 | 9.3 |
| Hybrid p95 (ms) | 8.9 | 12.6 |
| Hybrid p99 (ms) | 17.0 | 15.7 |
| Hybrid QPS serial | 150.5 | 102.6 |
| Hybrid QPS concurrent | 409.0 | 688.5 |
| Hybrid text-hit@10 | 0.920 | 1.000 |
| Cold start to first query (s) | 0.8 | 7.9 |

Notes: raggio = single Python asyncio process, brute-force scan over 4-bit quantized vectors, per-collection lock. Weaviate = Go, HNSW approximate index, uncompressed vectors. Both queried via REST with client-supplied vectors; identical stored payloads.