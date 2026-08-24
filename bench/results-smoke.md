# raggio vs Weaviate — 19,900 vectors x 1536 dims (real email embeddings)

Config: k=10, 100 held-out corpus queries, concurrency 8, seed 42. raggio container capped at 1 GiB (4-bit quantized flat index), Weaviate at 8 GiB (HNSW, float32, defaults). Podman WSL VM: 8 GB RAM / 4 CPUs shared. Hybrid: raggio unweighted RRF (k=60) vs Weaviate rankedFusion alpha 0.5 over the body property; query = held-out vector + path tokens of a sampled ingested doc.

| Metric | raggio | weaviate |
|---|---|---|
| Ingest wall time (s) | 21 | 13 |
| Ingest throughput (vec/s) | 933 | 1534 |
| Memory after ingest (MB) | 113 | 4389 |
| Memory under query load (MB) | 110 | 3330 |
| Disk footprint (MB) | 306 | 0 |
| Search p50 (ms) | 16.5 | 8.4 |
| Search p95 (ms) | 19.7 | 12.9 |
| Search p99 (ms) | 285.1 | 274.1 |
| QPS serial | 51 | 86 |
| QPS concurrent | 59 | 163 |
| p95 under concurrency (ms) | 357.2 | 301.8 |
| Filtered p50 (ms) | 20.9 | 15.6 |
| Filtered p95 (ms) | 25.2 | 19.3 |
| Recall@10 vs exact | 0.961 | 0.990 |
| Hybrid p50 (ms) | 39.1 | 14.5 |
| Hybrid p95 (ms) | 47.8 | 24.9 |
| Hybrid p99 (ms) | 302.7 | 275.8 |
| Hybrid QPS serial | 23.4 | 55.8 |
| Hybrid QPS concurrent | 22.0 | 103.9 |
| Hybrid text-hit@10 | 1.000 | 1.000 |
| Cold start to first query (s) | 2.4 | 8.0 |

Notes: raggio = single Python asyncio process, brute-force scan over 4-bit quantized vectors, per-collection lock. Weaviate = Go, HNSW approximate index, uncompressed vectors. Both queried via REST with client-supplied vectors; identical stored payloads.