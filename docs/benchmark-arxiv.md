# Benchmark: arxiv abstracts

raggio vs [Weaviate](https://weaviate.io/) on 2,549,619 real arxiv paper
abstracts, embedded locally and ingested into both engines under fixed
container memory caps — raggio **4 GiB**, Weaviate **32 GiB**. Same harness
(`bench/bench.py`), protocol, and host (an NVIDIA DGX Spark) as the
[email benchmark](benchmark.md); this run changes the corpus, not the rules.

The corpus is the HuggingFace dataset
[`Rendra8631/arxiv-papers`](https://huggingface.co/datasets/Rendra8631/arxiv-papers):
for every paper, the title and abstract are concatenated into one chunk of
real prose (abstracts run roughly 200–500 words), embedded with
`Qwen/Qwen3-Embedding-0.6B` at 1024 dims on the DGX's GB10 GPU via a local
vLLM server. Embedding happens once, offline — the benchmark ships
client-supplied vectors, so no embedding call sits in any measured path.

## How it differs from the email benchmark

- **Real text.** The email corpus paired real vectors with synthetic bodies
  (boilerplate + each document's path tokens), so BM25 always had an exact
  token match and hybrid text-hit@10 was 1.000 by construction. Here every
  chunk stores the actual title + abstract, and hybrid queries use real
  titles against real bodies — a meaningful lexical signal for the first
  time.
- **~4.6x the vectors.** 2.55M vs 553k, in a single collection. This is a
  genuinely >2M-vector collection — the regime the optional
  [IVF index](indexing.md) (ADR-0002) targets, where a flat scan's linear
  cost starts to bite.
- **Different embedding.** Qwen3-Embedding-0.6B at 1024 dims (email:
  `text-embedding-3-small` at 1536), ~1.1B tokens embedded locally on GPU.
- **Raised caps.** 4 GiB / 32 GiB instead of 1 GiB / 8 GiB — scaled with the
  corpus, same 8x ratio.

## Setup

| | raggio | raggio-ivf | Weaviate 1.38.11 |
|---|---|---|---|
| Container memory cap | **4 GiB** | **4 GiB** (same container) | 32 GiB |
| Vector index | 4-bit quantized flat (brute-force scan), TQ+ calibrated | same store + optional IVF index (approximate, default nprobe=16) | HNSW, uncompressed float32, defaults |
| Text index | SQLite FTS5, BM25 | SQLite FTS5, BM25 | built-in BM25 over `body` |
| Hybrid fusion | unweighted RRF (k=60) | unweighted RRF (k=60) | `rankedFusion`, alpha 0.5 |

The `raggio-ivf` column reuses the exact collection ingested for the flat
column; its own measured setup step is the IVF index build (reported as
"IVF index build (s)"). It answers the ADR-0002 question directly: what does
the optional index buy — and cost in recall — at a real >2M-vector scale.

- **Corpus**: 2,549,619 arxiv rows (minus empty-abstract and duplicate-id
  rows dropped during prep), each chunk = `title\n\nabstract`, 1024-dim
  L2-normalized vectors, path `arxiv/{year}/{id}` so filtered search can
  target a publication year.
- **Queries**: 500 held-out corpus vectors, k=10, seed 42. Hybrid queries
  pair each held-out vector with the **title** of a sampled ingested paper.
- **Ground truth**: exact float32 cosine top-10 over the full corpus,
  computed offline once and cached (`bench/gt-{limit}-{seed}-d1024.npz`).
- **Host**: NVIDIA DGX Spark — GB10 Grace, 20 aarch64 cores, 122 GB unified
  LPDDR5x, native Linux, rootless podman. Both engines queried over REST
  with client-supplied vectors, identical stored payloads.

Cap rationale: raggio's 4-bit index at 2.55M x 1024 is ~1.3 GB plus
SQLite/FTS overhead, so 4 GiB leaves working headroom; Weaviate holds
uncompressed float32 vectors (~10.4 GB raw) plus the HNSW graph in memory,
so 32 GiB extrapolates the email run's observed ratio with margin. The caps
are fixed before the run and enforced by podman — an engine that outgrows
its cap fails loudly rather than getting a quiet upgrade.

## Results

Measured 2026-08-23 on the DGX Spark (full raw report: `bench/results-arxiv.md`).
Search, recall, and hybrid rows were re-measured after the exact-rescoring work
([ADR 0003](adr/0003-recall-rescoring.md)) on the same ingested volume. Ingest,
memory, IVF-build, and cold-start rows are kept from the original matched-conditions
run: the re-run's volume reuse warms the host page cache and compacts on-disk files,
which would flatter startup metrics whose code did not change (the warm re-run
measured cold start at 5.1 s — a reuse artifact, not an improvement).

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
| Recall@10 vs exact | **1.000** | 0.994 | 0.995 |
| Hybrid p50 (ms) | 104.0 | 91.4 | 35.8 |
| Hybrid p95 (ms) | 172.2 | 159.0 | 72.5 |
| Hybrid p99 (ms) | 588.3 | 592.6 | 114.4 |
| Hybrid QPS serial | 8.6 | 9.6 | 25.3 |
| Hybrid QPS concurrent | 13.8 | 13.3 | 169.0 |
| Hybrid text-hit@10 | **0.984** | **0.984** | 0.978 |
| Cold start to first query (s) | 30.4 | 28.3 | 12.8 |

## How to read the results

- **Recall@10 vs exact** compares each engine's top-10 against the exact
  float32 cosine top-10. raggio's quantized scan over-fetches and re-ranks
  candidates against the stored fp16 originals ([ADR 0003](adr/0003-recall-rescoring.md)),
  so the flat scan is effectively exact (1.000) and the IVF number prices only
  the unprobed cells at `nprobe=16`; for Weaviate the number prices the HNSW
  approximation. Neither engine is told the ground truth.
- **The fixed caps are the point.** raggio's design goal is a useful
  collection inside a small container (≤8 GB per collection); Weaviate's
  fp32 HNSW wants RAM proportional to raw vector size. The memory rows show
  what each engine actually consumes to serve the same 2.55M vectors, and
  the caps show what you must provision.
- **Ingest throughput, cold start, disk** are the operating costs: how long
  a 2.55M-vector load takes, how long a container restart leaves you dark
  (at this scale, reloading a large HNSW index is the number to watch —
  the harness allows up to 600 s), and what the volume costs at rest.
- **Hybrid text-hit@10** asks whether the paper whose title was used as the
  lexical query surfaces in the fused top-10. It is an **exact-title
  known-item task** under vector noise (the paired vector is an unrelated
  held-out abstract), not a full hybrid-relevance measure: with disjoint
  legs and RRF k=60, it effectively measures whether the target reaches the
  text leg's top-5. It drove the two-stage BM25 work in
  [ADR 0003](adr/0003-recall-rescoring.md), which is also where the hybrid
  latency increase over the pre-rescoring run comes from. The 0.984 vs 0.978
  margin is 3 queries out of 500 — within noise; read it as parity. Exact
  titles are also the best case for the AND candidate branch (every query
  token is guaranteed present in the target); perturbed queries (typos,
  paraphrases, extra words) would land closer to the full-query-BM25-only
  ablation (~0.93) for both engines' non-WAND paths.
- **The IVF question.** The headline run uses raggio's default flat scan,
  which is linear in N — this corpus is ~4.6x the email run's work per
  query. This is exactly the scale where attaching the optional
  [IVF index](indexing.md) is meant to pay; the flat numbers here are the
  baseline that decision gets measured against.

## Running it

### Prerequisites

- A Linux box with a CUDA GPU and [vLLM](https://docs.vllm.ai) ≥ 0.8.5
  (Qwen3-Embedding support), rootless podman (or docker), and `uv`.
- ~50 GB free disk: ~6 GB dataset, ~10.4 GB vector file, ~1.2 GB model,
  ~30 GB engine volumes.
- Several hours: download + text prep ~30 min, embedding **4–9 h**
  (dominates everything), ground truth + ingest + query phases ~2–3 h.

Run every long phase detached with unbuffered output — otherwise `nohup`
buffers stdout and the log stays empty for hours:

```bash
nohup env PYTHONUNBUFFERED=1 <command> > <logfile> 2>&1 &
```

### 1. Start the embedding server

```bash
vllm serve Qwen/Qwen3-Embedding-0.6B \
  --max-model-len 2048 \
  --max-num-batched-tokens 131072 \
  --max-num-seqs 1024 \
  --no-enable-prefix-caching \
  --gpu-memory-utilization 0.80 \
  --api-server-count 4 \
  --port 8001
```

Embedding is a prefill-only workload, so `--max-num-batched-tokens` is the
throughput knob (halve it if your GPU rejects it); prefix caching is off
because abstracts share no prefixes. Smoke-test before committing hours:

```bash
curl -s localhost:8001/v1/embeddings \
  -d '{"model":"Qwen/Qwen3-Embedding-0.6B","input":["hello"]}'
# expect 1024 floats, ~unit norm
```

### 2. Build the corpus

```bash
nohup env PYTHONUNBUFFERED=1 uv run --with pyarrow --with huggingface_hub \
  python bench/prep_arxiv.py --stage all > bench/prep.log 2>&1 &
```

`prep_arxiv.py` runs two stages (`--stage text|embed|all`):

- **text** (CPU, ~15 min after download): downloads the parquet files,
  drops empty-abstract and duplicate-id rows, and writes
  `bench/corpus/abstracts.jsonl` (line i ↔ vector row i) plus
  `bench/corpus/embed-meta.json` (model, dims, paths). It prints the final
  kept count **N** — you need it for `--limit` below.
- **embed** (GPU, 4–9 h): streams the jsonl through the vLLM endpoint
  (`--url`, default `http://localhost:8001/v1`; `--batch` texts per request,
  default 256; `--concurrency` in-flight requests, default 16) into
  `bench/corpus/embed-vecs.npy` (float32 N x 1024, L2-normalized).

The embed stage checkpoints a contiguous prefix to
`bench/corpus/prep-progress.json` — a crash or kill resumes where it left
off, nothing is re-embedded. That also enables a cheap calibration pass:

```bash
uv run --with pyarrow --with huggingface_hub \
  python bench/prep_arxiv.py --stage embed --limit 20000
```

embeds only the first 20k rows and prints measured rows/s plus an
extrapolated full-corpus ETA. If the ETA is unacceptable, retune the server
before launching the full job; the full run then resumes from row 20,000.

`abstracts.jsonl` + `embed-vecs.npy` + `embed-meta.json` are the corpus
contract: `bench.py` consumes exactly these three files, and when
`abstracts.jsonl` is present it automatically switches to real chunk text
and title-based hybrid queries (without it, it falls back to the email
corpus's synthetic bodies).

### 3. Run the benchmark

Stop the vLLM server first (free the memory), then start fresh engine
containers with the raised caps:

```bash
podman rm -f bench-tv bench-wv; podman volume rm bench-tv bench-wv
podman build -t raggio .
podman run -d --name bench-tv --memory 4g -p 18000:8000 -v bench-tv:/data \
  -e ROOT_API_KEY=bench raggio
podman run -d --name bench-wv --memory 32g -p 18080:8080 -v bench-wv:/var/lib/weaviate \
  -e AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true \
  -e PERSISTENCE_DATA_PATH=/var/lib/weaviate \
  docker.io/semitechnologies/weaviate:1.38.11

# if you previously ran the email benchmark, clear its checkpoints
rm -f bench/fingerprint-*.json bench/results-partial.json
```

Smoke first, then the full run detached. `--limit` must be the N printed by
the text stage — `bench.py`'s default is the email corpus size:

```bash
uv run python bench/bench.py --limit 20000 --queries 100 \
  --out bench/results-arxiv-smoke.md

nohup env PYTHONUNBUFFERED=1 uv run python bench/bench.py --limit <N> \
  --out bench/results-arxiv.md \
  --caps-note "raggio 4 GiB, Weaviate 32 GiB" > bench/arxiv-run.log 2>&1 &
```

The harness computes ground truth, ingests both engines, runs the serial /
concurrent / filtered / hybrid query phases, and writes the results table to
`bench/results-arxiv.md`. It checkpoints `bench/results-partial.json` after
each phase and fingerprints ingested data, so a re-run skips straight to the
phases that remain.

### Monitoring

```bash
tail -f bench/prep.log                     # heartbeat every 30 s: %, rows/s, ETA, retries
cat bench/corpus/prep-progress.json        # embed checkpoint (next_row)
tail -f bench/arxiv-run.log                # per-phase bench progress
podman stats bench-tv bench-wv --no-stream # live memory vs the caps
```

## Caveats

- One machine, one run. The DGX Spark's unified LPDDR5x bandwidth flatters
  memory-bandwidth-bound scans; rankings can shift on other hardware.
- One embedding model and one document shape: short title+abstract chunks
  from a single 1024-dim model. Longer documents or other models may rank
  the engines differently.
- Weaviate runs with default settings inside its cap — no engine-specific
  tuning (no vector compression, no HNSW parameter tuning). The comparison
  is out-of-the-box vs out-of-the-box, not best-effort vs best-effort.
- Hybrid fusion settings are matched as closely as the two engines allow
  (unweighted RRF vs rankedFusion at alpha 0.5), not proven equivalent.
