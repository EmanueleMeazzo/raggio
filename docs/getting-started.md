# Getting started

## Run the container

```bash
podman build -t raggio .
podman run -p 8000:8000 -v raggio-data:/data \
  -e ROOT_API_KEY=change-me \
  -e EMBEDDING_BASE_URL=https://api.openai.com/v1 \
  -e EMBEDDING_API_KEY=sk-... \
  -e EMBEDDING_MODEL=text-embedding-3-small \
  raggio
```

Any OpenAI-compatible `/embeddings` endpoint works. For Azure AI Foundry, set
`EMBEDDING_BASE_URL` to the full deployment URL — the key is sent as both
`Authorization: Bearer` and `api-key`, so both providers work unchanged.

!!! tip "No embedding endpoint?"
    raggio works without one: ingest pre-computed vectors and search with
    `mode: "vector"` + a raw query vector, or use `mode: "text"` (BM25), which
    never calls an embedding endpoint.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `ROOT_API_KEY` | — (required) | Admin key with full access to every endpoint |
| `EMBEDDING_BASE_URL` | — | OpenAI-compatible embeddings endpoint |
| `EMBEDDING_API_KEY` | — | Key for the embeddings endpoint |
| `EMBEDDING_MODEL` | — | Model name sent with each embeddings request |
| `EMBEDDING_DIM` | probed | Skip the dimension-probe call at collection creation |
| `DATA_DIR` | `/data` | Storage root (mount a volume here) |
| `MAX_RESIDENT_COLLECTIONS` | `4` | LRU cap on in-memory collections |
| `COLLECTION_IDLE_TTL` | `900` | Seconds before an idle collection is offloaded to disk |

## Authentication

Every request needs a key, passed as `X-API-Key: <key>` or
`Authorization: Bearer <key>`.

- The **root key** (`ROOT_API_KEY`) can do everything, including collection
  CRUD and listing collections.
- Each collection may set its own **collection key** at creation
  (`collection_key`). That key grants access to that collection only: its
  info, documents, search, and jobs. If no collection key is set, only the
  root key can access the collection.

## First collection

```bash
ROOT=change-me

# dim is probed from the embedding endpoint if omitted
curl :8000/collections -H "x-api-key: $ROOT" \
  --json '{"name": "kb", "collection_key": "kb-secret", "bit_width": 4}'
```

Collection options:

| Field | Default | Description |
|---|---|---|
| `name` | required | `[a-zA-Z0-9_-]{1,64}` |
| `dim` | probed | Vector dimension; must be a positive multiple of 8 |
| `bit_width` | `4` | Vector quantization: `4` (≈8× smaller) or `2` |
| `model` / `base_url` | env defaults | Per-collection embedding override |
| `collection_key` | none | Per-collection API key |
| `tokenizer` | `unicode61` | BM25 tokenizer: `unicode61` or `trigram` — see [Search](search.md#tokenizers) |

## Ingest

Ingest is asynchronous: the API journals the job and returns `202` with a
`job_id` immediately.

```bash
curl :8000/collections/kb/documents -H "x-api-key: kb-secret" --json '{
  "documents": [{
    "doc_id": "contract-42",
    "summary": {"text": "Master agreement with ACME covering 2026 pricing."},
    "chunks": [
      {"id": "contract-42#0", "text": "...", "position": 0,
       "metadata": {"src": "sharepoint", "date": "2026-03-01"}},
      {"id": "contract-42#1", "text": "..."}
    ]}]}'
# -> {"job_id": 1}

curl :8000/collections/kb/jobs/1 -H "x-api-key: kb-secret"
# -> {"job_id": 1, "status": "done", ...}
```

Documents are pre-chunked by you. Each chunk (and the optional document
summary) takes `text` (embedded server-side), a ready `vector`, or both.
Re-ingesting an existing chunk or document id upserts it.

## Search

```bash
curl :8000/collections/kb/search -H "x-api-key: kb-secret" --json '{
  "query": {"text": "acme pricing terms"},
  "mode": "hybrid",
  "k": 5,
  "filter": {"src": "sharepoint", "date": {"gte": "2026-01-01"}},
  "expand": {"siblings_topk": 3, "summary": true}}'
```

See [Search](search.md) for the three modes, filtering, and result expansion.

## Develop

```bash
uv sync
uv run pytest
```

Build these docs locally:

```bash
uv sync --group docs
uv run mkdocs serve
```
