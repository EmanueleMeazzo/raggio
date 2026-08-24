# raggio vs Weaviate — 552,515 vectors x 1536 dims (real email embeddings)

Config: k=10, 500 held-out corpus queries, concurrency 8, seed 42. raggio container capped at 1 GiB (4-bit quantized flat index), Weaviate at 8 GiB (HNSW, float32, defaults). Host: NVIDIA DGX Spark (GB10 Grace, 20 aarch64 cores — 10x Cortex-X925 + 10x Cortex-A725, 122 GB unified LPDDR5x), rootless podman, native Linux. Hybrid: raggio unweighted RRF (k=60) vs Weaviate rankedFusion alpha 0.5 over the body property; query = held-out vector + path tokens of a sampled ingested doc.

| Metric | raggio | weaviate |
|---|---|---|
| Ingest wall time (s) | 500 | 204 |
| Ingest throughput (vec/s) | 1105 | 2713 |
| Memory after ingest (MB) | 490 | 7851 |
| Memory under query load (MB) | 588 | 7738 |
| Disk footprint (MB) | 980 | 4979 |
| Search p50 (ms) | 9.0 | 21.8 |
| Search p95 (ms) | 11.1 | 35.8 |
| Search p99 (ms) | 14.7 | 42.9 |
| QPS serial | 98 | 44 |
| QPS concurrent | 264 | 260 |
| p95 under concurrency (ms) | 39.8 | 57.9 |
| Filtered p50 (ms) | 6.8 | 30.0 |
| Filtered p95 (ms) | 9.8 | 50.9 |
| Recall@10 vs exact | 0.976 | 0.989 |
| Hybrid p50 (ms) | 14.5 | 60.8 |
| Hybrid p95 (ms) | 26.5 | 147.7 |
| Hybrid p99 (ms) | 67.0 | 247.1 |
| Hybrid QPS serial | 60.1 | 13.9 |
| Hybrid QPS concurrent | 88.3 | 93.4 |
| Hybrid text-hit@10 | 1.000 | 1.000 |
| Cold start to first query (s) | 2.2 | 9.2 |

Notes: raggio = single Python asyncio process, brute-force scan over 4-bit quantized vectors, per-collection lock. Weaviate = Go, HNSW approximate index, uncompressed vectors. Both queried via REST with client-supplied vectors; identical stored payloads.