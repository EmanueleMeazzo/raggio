# Benchmarks

raggio vs [Weaviate](https://weaviate.io/) on 552,515 real email embeddings,
measured with the same harness, corpus, queries, and host — an NVIDIA DGX
Spark. The point of the comparison is what a single 1 GiB Python container
gives up (and doesn't) against a dedicated vector database running with 8x
the memory.

## Setup

| | raggio | Weaviate |
|---|---|---|
| Container memory cap | **1 GiB** | 8 GiB |
| Vector index | 4-bit quantized flat (brute-force scan), TQ+ calibrated | HNSW, uncompressed float32, defaults |
| Text index | SQLite FTS5, BM25 | built-in BM25 over `body` |
| Hybrid fusion | unweighted RRF (k=60) | `rankedFusion`, alpha 0.5 |

- **Corpus**: 552,515 email embeddings, 1536 dims (`text-embedding-3-small`),
  each chunk carrying a distinct body (shared boilerplate + the email's path
  tokens) so BM25 has something real to rank.
- **Queries**: 500 held-out corpus vectors, k=10, seed 42. Hybrid queries pair
  each held-out vector with the path tokens of a sampled ingested document.
- **Ground truth**: exact float32 cosine top-10 over the full corpus,
  computed offline once and cached.
- **Host**: NVIDIA DGX Spark — GB10 Grace, 20 aarch64 cores (10x Cortex-X925 +
  10x Cortex-A725), 122 GB unified LPDDR5x, native Linux, rootless podman.
  Both engines queried over REST with client-supplied vectors (no embedding
  calls in the measured path), identical stored payloads.

## Results

| Metric | raggio (1 GiB) | Weaviate (8 GiB) |
|---|---|---|
| Ingest wall time (s) | 500 | **204** |
| Ingest throughput (vec/s) | 1105 | **2713** |
| Memory after ingest (MB) | **490** | 7851 |
| Memory under query load (MB) | **588** | 7738 |
| Disk footprint (MB) | **980** | 4979 |
| Vector search p50 (ms) | **9.0** | 21.8 |
| Vector search p95 (ms) | **11.1** | 35.8 |
| Vector search p99 (ms) | **14.7** | 42.9 |
| QPS serial | **98** | 44 |
| QPS concurrent (8) | **264** | 260 |
| p95 under concurrency (ms) | **39.8** | 57.9 |
| Filtered search p50 (ms) | **6.8** | 30.0 |
| Filtered search p95 (ms) | **9.8** | 50.9 |
| Recall@10 vs exact | 0.976 | **0.989** |
| Hybrid p50 (ms) | **14.5** | 60.8 |
| Hybrid p95 (ms) | **26.5** | 147.7 |
| Hybrid p99 (ms) | **67.0** | 247.1 |
| Hybrid QPS serial | **60.1** | 13.9 |
| Hybrid QPS concurrent (8) | 88.3 | **93.4** |
| Hybrid text-hit@10 | 1.000 | 1.000 |
| Cold start to first query (s) | **2.2** | 9.2 |

## Reading the numbers

**Where raggio wins.** Every search latency, including the one a flat index
is "supposed" to lose: raw vector search is 2.4x faster than HNSW at p50
(9.0 vs 21.8 ms) and 3.2x at p95, serial QPS is 2.2x higher, and hybrid and
filtered search win at every percentile. The reason is that the two engines
are bound by different resources: the 4-bit brute-force scan streams a
~500 MB quantized index sequentially and is memory-bandwidth-bound, so
Grace's unified LPDDR5x feeds it at full rate across 20 cores — while HNSW
traversal is a chain of *dependent* random reads that no amount of bandwidth
or cores accelerates. Footprint stays the headline elsewhere: ~13x less
memory under load, 5x less disk (the raw float32 vectors alone are 3.4 GB —
the whole raggio deployment is 980 MB), and a 4x faster cold start. Both
engines surface the lexically-targeted document in the fused top-10 for 100%
of hybrid queries (text-hit@10 = 1.000).

**Where Weaviate wins.** Ingest: 204 s vs 500 s — HNSW construction
parallelizes across all 20 cores, while raggio's durable single-writer
journal pipeline becomes the bottleneck (the POSTs themselves complete in
~256 s; the rest is queue drain). Recall: 0.989 (uncompressed HNSW) vs 0.976
(4-bit quantization with TQ+ calibration) — the price of the 8x index
compression. And hybrid concurrent QPS by a hair (93.4 vs 88.3).

**Ingestion.** raggio journals each batch to SQLite before returning 202,
replays interrupted jobs after a crash, marks a job done only after the
vector index is synced to disk, and returns the journal's pages to the OS as
jobs complete — the 1,105 vec/s and the 8.3-minute wall time include all of
that. Peak transient disk during a deep ingest backlog is roughly the size of
the un-drained journaled payloads on top of the steady-state footprint.

## Reproducing

```bash
# containers (podman or docker), one volume each so re-runs skip ingest
podman build -t raggio .
podman run -d --name bench-tv --memory 1g -p 18000:8000 -v bench-tv:/data \
  -e ROOT_API_KEY=bench raggio
podman run -d --name bench-wv --memory 8g -p 18080:8080 -v bench-wv:/var/lib/weaviate \
  -e AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true \
  -e PERSISTENCE_DATA_PATH=/var/lib/weaviate \
  semitechnologies/weaviate

uv run python bench/bench.py                  # full run, both engines
uv run python bench/bench.py --engine raggio --reingest   # one engine, wipe first
uv run python bench/bench.py --limit 20000 --queries 100    # quick smoke
```

The harness (`bench/bench.py`) ingests, then measures serial / concurrent /
filtered / hybrid search, recall against cached exact ground truth, memory,
disk, and cold start, and writes `bench/results.md`. Ingested data stays in
the volumes and is fingerprinted, so repeat runs start straight at the search
phases; ingest wall time is measured on `--reingest` runs. Supporting
decomposition tools live next to it: `bench/microbench.py` (kernel / FTS /
hydration cost split), `bench/ivf_probe.py` (IVF feasibility sweep), and
`bench/cal_probe.py` (TQ+ calibration flows).
