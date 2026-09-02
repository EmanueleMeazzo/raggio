# raggio

![raggio logo](docs/raggio-logo.png)

Containerized RAG vector store built on [turbovec](https://github.com/RyanCodrai/turbovec).
(Raggio is built on TurboVec, which is built on TurboQuant. The turbo is under the hood now.)
Single instance, REST API, durable async ingest queue, multiple physically separate
collections, disk-backed with LRU load/eviction so the dataset can exceed RAM.

**📖 Documentation: [emanuelemeazzo.github.io/raggio](https://emanuelemeazzo.github.io/raggio/)** —
getting started, search modes, [concepts & terminology](https://emanuelemeazzo.github.io/raggio/concepts/),
indexing, storage, API reference, and the full benchmarks.

**Why raggio**: a plug-and-play vector database for companies and individuals that
don't want a commercial hosted service and don't want to hand-roll FAISS — raggio is
faster anyway ([benchmarks vs FAISS](https://github.com/RyanCodrai/turbovec#search-speed) on the
turbovec page, the engine raggio runs on). One small container, limited resources,
lots of documents:

- **Big on small hardware** — TurboQuant 4-bit quantization shrinks vector indexes
  ≈8x, and collections are offloaded to disk instead of kept always in memory, so
  stored data isn't bounded by RAM.
- **Fits where big databases don't** — when running LLM inference locally, every
  byte of RAM is precious: it's needed for model weights and KV cache, with no room
  to waste, and raggio gives you a local, high-performance RAG DB with the
  smallest possible footprint. The same small footprint serves multi-user
  deployments in resource-constrained environments — an SME, a single department —
  where a big-scale database makes no sense.
- **Multi-user without the auth project** — each collection can carry its own API key
  and is physically separate on disk: hand every user or team a key and they share one
  deployment while never being able to touch each other's collections.
- **Full-text search included** — BM25 alongside (or fused with) vector search, with a
  per-collection tokenizer choice: `unicode61` (default, word matching) or `trigram`
  (substring matching).
- **Embeddings optional** — ingest pre-computed vectors from your own pipeline, or
  point raggio at any OpenAI-compatible `/embeddings` endpoint and it embeds text
  server-side.
- **Index optional too** — collections default to an exact quantized scan (fastest
  and highest-recall up to ~1M vectors); multi-million collections can attach a
  ScaNN-style IVF index at any time, and remove it again. See
  [when it makes sense](docs/indexing.md).

## Run

```bash
podman build -t raggio .
podman run -p 8000:8000 -v raggio-data:/data -e ROOT_API_KEY=change-me raggio
```

That's all a vector database needs: ingest pre-computed vectors and search. Optionally,
let raggio create embeddings for you by pointing it at any OpenAI-compatible
`/embeddings` endpoint (Azure Foundry: set `EMBEDDING_BASE_URL` to the full deployment
URL; the key is sent as both `Authorization: Bearer` and `api-key`):

```bash
podman run -p 8000:8000 -v raggio-data:/data \
  -e ROOT_API_KEY=change-me \
  -e EMBEDDING_BASE_URL=https://api.openai.com/v1 \
  -e EMBEDDING_API_KEY=sk-... \
  -e EMBEDDING_MODEL=text-embedding-3-small \
  raggio
```

### Config (env)

| Var | Default | |
|---|---|---|
| `ROOT_API_KEY` | — (required) | admin key, full access |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | — | default embedding endpoint |
| `EMBEDDING_DIM` | probed | skip the probe call at collection creation |
| `DATA_DIR` | `/data` | storage root |
| `MAX_RESIDENT_COLLECTIONS` | `4` | LRU cap on in-memory collections |
| `COLLECTION_IDLE_TTL` | `900` | seconds before an idle collection is offloaded to disk |

## API

Auth: `X-API-Key: <key>` or `Authorization: Bearer <key>`. Collection CRUD needs the
root key. Each collection may set its own `collection_key` at creation; if unset, only
the root key can access it. Different users can hold different collection keys on the
same instance — collections are physically separate on disk, so a key for one grants
nothing on the others.

```bash
# create a collection (dim probed from the embedding endpoint if omitted;
# tokenizer: unicode61 [default] or trigram for substring matching, used by BM25)
curl :8000/collections -H "x-api-key: $ROOT" \
  --json '{"name": "bu-sales", "collection_key": "sales-secret", "bit_width": 4, "tokenizer": "unicode61"}'

# ingest (async, 202 + job id): pre-chunked docs, optional parent summary,
# each record takes text (embedded server-side) and/or a ready vector
curl :8000/collections/bu-sales/documents -H "x-api-key: sales-secret" --json '{
  "documents": [{
    "doc_id": "contract-42",
    "summary": {"text": "Master agreement with ACME covering 2026 pricing."},
    "chunks": [
      {"id": "contract-42#0", "text": "...", "position": 0, "metadata": {"src": "sharepoint", "date": "2026-03-01"}},
      {"id": "contract-42#1", "text": "...", "position": 1}
    ]}]}'
# -> {"job_id": 1}    poll: GET /collections/bu-sales/jobs/1

# search: mode vector (default) | text (BM25) | hybrid (RRF fusion of both),
# scope chunks|summaries|both, metadata filters (equality, ranges, in, contains),
# per-hit expansion to sibling chunks and/or the parent summary
curl :8000/collections/bu-sales/search -H "x-api-key: sales-secret" --json '{
  "query": {"text": "acme pricing terms"},
  "mode": "hybrid",
  "k": 5,
  "filter": {"src": "sharepoint", "date": {"gte": "2026-01-01"}},
  "expand": {"siblings_topk": 3, "summary": true}}'
```

Search modes: `vector` embeds the query (or takes a raw vector) and returns cosine
scores; `text` runs BM25 over SQLite FTS5 (no embedding endpoint needed, score is
`-bm25`, higher = better); `hybrid` fuses both rankings with Reciprocal Rank Fusion
(text required, vector optional to skip the embedding call). Records ingested without
text are invisible to the BM25 leg.

Also: `GET/DELETE /collections/{name}`, `GET/PATCH/DELETE /collections/{name}/documents/{doc_id}`
(PATCH merge-patches metadata without re-embedding), `GET /collections/{name}/documents`
(unranked filtered listing with paging, sort and total count), `GET /healthz`. Re-ingesting
an existing chunk/doc id upserts it.

### Optional vector index

Collections don't need an index — below ~1M vectors the exact quantized scan is as
fast as (or faster than) a graph index at higher recall, so the default is none. Past
~2M vectors, attach a ScaNN-style IVF index (measured 3.5–6.8x faster vector search at
2.2M for ~1pt recall@10, tunable per query via `nprobe`):

```bash
curl -X POST :8000/collections/bu-sales/index -H "x-api-key: sales-secret" --json '{}'
# -> {"job_id": 7}   background rebuild; searches keep serving meanwhile
curl -X DELETE :8000/collections/bu-sales/index -H "x-api-key: sales-secret"   # back to flat
```

Both directions are online and reversible. Guidance on when an index pays off, its
memory/disk costs, and the recall trade: [docs/indexing.md](docs/indexing.md).

## How it stores data

```
/data/catalog.db                     collections registry + key hashes
/data/collections/<name>/index.tvim  turbovec quantized vector index (4-bit ≈ 8x smaller)
/data/collections/<name>/meta.db     text, metadata, fp16 vector copies, FTS5 index (BM25), job queue (SQLite)
/data/collections/<name>/ivf/        IVF shards + centroids (only when an index is attached)
```

Ingest requests are journaled to SQLite before the 202 returns and replayed after a
crash; a job is marked done only after the index is synced to disk. Collections load
on first touch and are offloaded (synced + dropped from memory) by LRU/idle policy,
so total stored data can far exceed container memory.

## Performance

**2,549,119 real arXiv abstracts** (title+abstract chunks, 1024-dim Qwen3
embeddings, k=10), raggio capped at **4 GiB** vs Weaviate at **32 GiB** (HNSW,
uncompressed float32, defaults), on an NVIDIA DGX Spark (GB10 Grace, 20 arm64
cores, 122 GB unified LPDDR5x):

| Metric | raggio (4 GiB) | + IVF index | Weaviate (32 GiB) |
|---|---|---|---|
| Vector search p50 / p95 | 23.0 / 28.7 ms | **11.2 / 12.5 ms** | 15.3 / 50.3 ms |
| Recall@10 vs exact float32 | **1.000** (4-bit scan + fp16 rescore) | 0.994 | 0.995 |
| Hybrid search (RRF) p50 | 104.0 ms | 91.4 ms | **35.8 ms** |
| Hybrid text-hit@10 | 0.984 | 0.984 | 0.978 |
| QPS concurrent (8) | 129 | 116 | **1050** |
| Memory under query load | **1.8 GB** | 3.1 GB | 23.4 GB |
| Disk footprint | **16.5 GB** | 16.8 GB | 18.4 GB |
| Ingest 2.55M vectors (journaled + crash-safe) | 31.3 min (1,357 vec/s) | +5.6 min index build | **16.3 min (2,614 vec/s)** |
| Cold start to first query | 30.4 s | 28.3 s | **12.8 s** |

The trade in one line: raggio serves the same 2.55M vectors in ~13x less memory
with **exact recall** — the quantized scan re-ranks its candidates against the
stored fp16 originals, so quantization costs no recall — and text-hit parity from
two-stage BM25 (bounded FTS5 candidate generation + full-query rescoring); the
optional IVF index then beats HNSW on vector latency at equal recall. Weaviate
wins concurrent throughput, hybrid latency, ingest speed, and cold start. Full
table, method, and caveats: [docs/benchmark-arxiv.md](docs/benchmark-arxiv.md).

An earlier 553k-email run (1 GiB vs 8 GiB, pre-rescoring build) told the same
memory story at smaller scale — there the flat scan also won every latency row.
Setup and full table: [docs/benchmark.md](docs/benchmark.md).

## Develop

```bash
uv sync
uv run pytest
```

## License

[Apache-2.0](LICENSE). Built on [turbovec](https://github.com/RyanCodrai/turbovec) (MIT);
all runtime dependencies are permissively licensed (MIT/BSD/PSF, certifi MPL-2.0).
