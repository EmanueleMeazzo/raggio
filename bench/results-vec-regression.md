# raggio vs Weaviate — 552,515 vectors x 1536 dims (real email embeddings)

Config: k=10, 500 held-out corpus queries, concurrency 8, seed 42. raggio container capped at 1 GiB (4-bit quantized flat index), Weaviate at 8 GiB (HNSW, float32, defaults). Podman WSL VM: 8 GB RAM / 4 CPUs shared. Hybrid: raggio unweighted RRF (k=60) vs Weaviate rankedFusion alpha 0.5 over the body property; query = held-out vector + path tokens of a sampled ingested doc.

| Metric | raggio | weaviate |
|---|---|---|
| Ingest wall time (s) | 521 | 204 |
| Ingest throughput (vec/s) | 1060 | 2713 |
| Memory after ingest (MB) | 498 | 7851 |
| Memory under query load (MB) | 867 | 7738 |
| Disk footprint (MB) | 3252 | 4979 |
| Search p50 (ms) | 9.3 | 21.8 |
| Search p95 (ms) | 11.2 | 35.8 |
| Search p99 (ms) | 12.6 | 42.9 |
| QPS serial | 101 | 44 |
| QPS concurrent | 305 | 260 |
| p95 under concurrency (ms) | 36.8 | 57.9 |
| Filtered p50 (ms) | 8.0 | 30.0 |
| Filtered p95 (ms) | 10.9 | 50.9 |
| Recall@10 vs exact | 0.976 | 0.989 |
| Hybrid p50 (ms) | 15.3 | 60.8 |
| Hybrid p95 (ms) | 33.7 | 147.7 |
| Hybrid p99 (ms) | 98.7 | 247.1 |
| Hybrid QPS serial | 54.3 | 13.9 |
| Hybrid QPS concurrent | 90.1 | 93.4 |
| Hybrid text-hit@10 | 1.000 | 1.000 |
| Cold start to first query (s) | 4.6 | 9.2 |

Notes: raggio = single Python asyncio process, brute-force scan over 4-bit quantized vectors, per-collection lock. Weaviate = Go, HNSW approximate index, uncompressed vectors. Both queried via REST with client-supplied vectors; identical stored payloads.