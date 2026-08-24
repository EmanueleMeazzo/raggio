# raggio vs Weaviate — 2,549,119 vectors x 1024 dims (real abstracts, Qwen/Qwen3-Embedding-0.6B @a2c6afb5)

Config: k=10, 500 held-out corpus queries, concurrency 8, seed 42. raggio container capped at 4 GiB (4-bit quantized flat index; IVF column adds the optional index), Weaviate at 32 GiB (HNSW, float32, defaults). Host: NVIDIA DGX Spark - GB10 Grace, 20 aarch64 cores (10x Cortex-X925 + 10x Cortex-A725), 122 GB unified LPDDR5x, native Linux, rootless podman. Hybrid: raggio unweighted RRF (k=60) vs Weaviate rankedFusion alpha 0.5 over the body property; query = held-out vector + title of a sampled ingested doc.

| Metric | raggio | raggio-ivf | weaviate |
|---|---|---|---|
| Ingest wall time (s) | 1879 | — | 975 |
| Ingest throughput (vec/s) | 1357 | — | 2614 |
| IVF index build (s) | — | 337 | — |
| Memory after ingest (MB) | 729 | 3052 | 22990 |
| Memory under query load (MB) | 1761 | 3071 | 23380 |
| Disk footprint (MB) | 16470 | 16758 | 18374 |
| Search p50 (ms) | 23.0 | 11.2 | 15.3 |
| Search p95 (ms) | 28.7 | 12.5 | 50.3 |
| Search p99 (ms) | 33.9 | 13.2 | 82.5 |
| QPS serial | 42 | 88 | 49 |
| QPS concurrent | 129 | 116 | 1050 |
| p95 under concurrency (ms) | 68.7 | 77.8 | 10.8 |
| Filtered p50 (ms) | 29.3 | 72.3 | 12.8 |
| Filtered p95 (ms) | 36.2 | 77.8 | 21.1 |
| Recall@10 vs exact | 1.000 | 0.994 | 0.995 |
| Hybrid p50 (ms) | 104.0 | 91.4 | 35.8 |
| Hybrid p95 (ms) | 172.2 | 159.0 | 72.5 |
| Hybrid p99 (ms) | 588.3 | 592.6 | 114.4 |
| Hybrid QPS serial | 8.6 | 9.6 | 25.3 |
| Hybrid QPS concurrent | 13.8 | 13.3 | 169.0 |
| Hybrid text-hit@10 | 0.984 | 0.984 | 0.978 |
| Cold start to first query (s) | 30.4 | 28.3 | 12.8 |

Notes: raggio = single Python asyncio process, brute-force scan over 4-bit quantized vectors + exact fp16 rescore of the top candidates, two-stage BM25 (bounded FTS5 candidate generation + full-query rescore), per-collection lock. raggio-ivf = the same store and data with the optional IVF index built (approximate, default nprobe, same rescore). Weaviate = Go, HNSW approximate index, uncompressed vectors. All queried via REST with client-supplied vectors; identical stored payloads.

Provenance: search, recall, and hybrid rows were re-measured 2026-08-23 after the ADR 0003
exact-rescoring changes, reusing the ingested volume. Ingest, memory, IVF-build, and
cold-start rows are kept from the original matched-conditions run — the re-run's reuse path
warms the host page cache and the detach/attach cycle compacts on-disk files (the re-run
measured cold start at 5.1 s), which would flatter startup metrics whose code did not change.
The text-hit margin over Weaviate (0.984 vs 0.978) is 3 queries of 500 — within noise; read
it as parity.