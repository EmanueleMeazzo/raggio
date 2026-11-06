# API reference

Base URL: `http://<host>:8000`. All requests and responses are JSON.

**Authentication** — every endpoint (except `/healthz`) requires a key via
`X-API-Key: <key>` or `Authorization: Bearer <key>`. Endpoints marked
**root** require `ROOT_API_KEY`; the rest accept the collection's
`collection_key` or the root key.

An interactive OpenAPI UI is served at `/docs` (FastAPI built-in).

**A collection closed under a request** — the LRU cap (`MAX_RESIDENT_COLLECTIONS`)
and the idle TTL (`COLLECTION_IDLE_TTL`) can offload a collection while a request on
it is in flight ([memory management](storage.md#memory-management)). Any endpoint
on that collection then answers **503** with `Retry-After: 1`: the
collection still exists, and the retry loads it again. A request that races
`DELETE /collections/{name}` answers **404**, as for a name that never existed.
Other errors stay **500**.

---

## Collections

### Create collection

`POST /collections` — **root**

```json
{
  "name": "kb",
  "dim": 1536,
  "bit_width": 4,
  "model": "text-embedding-3-small",
  "base_url": null,
  "collection_key": "kb-secret",
  "tokenizer": "unicode61",
  "fts_keys": ["title", "source", "tags"]
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | `[a-zA-Z0-9_-]{1,64}` |
| `dim` | int | probed | Positive multiple of 8; probed from the embedding endpoint if omitted |
| `bit_width` | `2` \| `4` | `4` | Vector quantization width |
| `model` | string | env | Per-collection embedding model override |
| `base_url` | string | env | Per-collection embedding endpoint override |
| `collection_key` | string | none | Per-collection API key; if unset, root-only |
| `tokenizer` | `unicode61` \| `trigram` | `unicode61` | BM25 tokenizer, immutable after creation |
| `fts_keys` | string array or null | `null` | Metadata keys appended to BM25 text, immutable; at most 8 unique literal keys matching `[A-Za-z0-9_.-]{1,64}`, excluding `doc_id` |

**201** `{"name": "kb", "dim": 1536, "bit_width": 4, "tokenizer": "unicode61", "fts_keys": ["title", "source", "tags"]}` ·
**409** name exists · **400** invalid dim / no embedding endpoint to probe

Invalid `fts_keys` returns **422**. Omitted, `null`, or empty keys preserve text-only
indexing. See [indexed metadata](search.md#what-bm25-indexes) for value conversion.

### List collections

`GET /collections` — **root**

**200** `{"collections": ["kb", "sales"]}`

### Collection info

`GET /collections/{name}`

**200**

```json
{
  "name": "kb", "dim": 1536, "bit_width": 4,
  "model": null, "tokenizer": "unicode61",
  "fts_keys": ["title", "source", "tags"],
  "index": {"type": "flat"},
  "documents": 12, "chunks": 340, "summaries": 12, "pending_jobs": 0
}
```

`index` is `{"type": "flat"}` (default exact scan) or
`{"type": "ivf", "nlist": 256, "nprobe": 16}` when an
[IVF index](indexing.md) is attached.

### Attach / rebuild index

`POST /collections/{name}/index`

```json
{"nlist": 256, "nprobe": 16}
```

Both fields optional (empty body = auto `nlist` ≈ rows/8192 as a power of two,
`nprobe` 16). Builds in the background over the ingest job queue; searches keep
serving from the current representation until the swap. On an already-indexed
collection, a body with only `nprobe` retunes the default without rebuilding.

**202** `{"job_id": 7}` · **400** too few records (min 1024) or `nlist` too
large for the collection

### Remove index

`DELETE /collections/{name}/index`

Rebuilds the flat index from the retained fp16 vectors and drops the shards.

**202** `{"job_id": 8}` · **404** collection has no index

### Delete collection

`DELETE /collections/{name}` — **root**

Deletes the collection and all its data on disk. **200** `{"deleted": "kb"}`

---

## Documents

### Ingest documents

`POST /collections/{name}/documents`

Asynchronous: the job is journaled durably, then **202** returns immediately.

```json
{
  "documents": [{
    "doc_id": "contract-42",
    "summary": {"text": "Master agreement with ACME.", "metadata": {"kind": "summary"}},
    "chunks": [
      {"id": "contract-42#0", "text": "...", "position": 0,
       "metadata": {"src": "sharepoint", "date": "2026-03-01"}},
      {"id": "contract-42#1", "vector": [0.12, "..."]}
    ]
  }]
}
```

- Each chunk/summary needs `text` and/or `vector`; text without a vector is
  embedded server-side, a supplied `vector` must match the collection `dim`.
- `position` defaults to the chunk's array index.
- Re-ingesting an existing chunk `id` or `doc_id` upserts it.

**202** `{"job_id": 1}` · **400** missing text+vector or wrong vector dim

### Job status

`GET /collections/{name}/jobs/{job_id}`

**200** `{"job_id": 1, "status": "pending" | "processing" | "done" | "error", "error": null, "created_at": "...", "updated_at": "..."}`

### Get document

`GET /collections/{name}/documents/{doc_id}`

**200** `{"doc_id": "...", "summary": {...} | null, "chunks": [...]}` · **404**

### List records

`GET /collections/{name}/documents`

Unranked listing with a total count: the no-query counterpart of search, for
browsing, paging, per-filter counts and exports.

| Query param | Type | Default | Notes |
|---|---|---|---|
| `scope` | `chunks` \| `summaries` \| `both` | `both` | Record types listed |
| `filter` | JSON object (URL-encoded) | none | Same grammar as the search `filter`, `doc_id` included: `{"doc_id": "contract-42"}` pages one document's records |
| `sort` | string | insertion order | Metadata key; `-` prefix for descending. Records lacking the key sort first ascending, last descending |
| `limit` | int | `20` | 1–1000 |
| `offset` | int | `0` | |
| `include_vector` | bool | `false` | Attach each record's stored vector (decoded from the fp16 copy) as `vector` |

```bash
curl -G :8000/collections/kb/documents -H "x-api-key: $KEY" \
  --data-urlencode 'scope=summaries' --data-urlencode 'sort=-date' \
  --data-urlencode 'filter={"src": "sharepoint"}' --data-urlencode 'limit=20'
```

**200** `{"records": [hit…], "total": 12}` — hits shaped as in search, without
`score` · **400** bad filter or sort

### Patch document metadata

`PATCH /collections/{name}/documents/{doc_id}`

```json
{"metadata": {"src": "archive", "reviewed": true, "draft": null}, "apply_to_chunks": true}
```

[JSON merge patch](https://www.rfc-editor.org/rfc/rfc7396) applied to the
document's summary and, with `apply_to_chunks` (default `true`), every chunk:
keys are added or replaced, `null` deletes a key, other keys are kept. When a
selected `fts_keys` value changes, the keyword index updates in the same transaction.
Other metadata patches leave it untouched. Vectors are never re-embedded.

**200** `{"patched_records": 4}` · **404**

### Delete document

`DELETE /collections/{name}/documents/{doc_id}`

Removes the document's summary and chunks from both indexes.
**200** `{"deleted_records": 4}` · **404**

---

## Search

`POST /collections/{name}/search`

```json
{
  "query": {"text": "acme pricing terms", "vector": null},
  "mode": "hybrid",
  "k": 5,
  "scope": "chunks",
  "filter": {"src": "sharepoint", "date": {"gte": "2026-01-01"}},
  "expand": {"siblings_topk": 3, "siblings_all": false, "summary": true}
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `query.text` | string | — | Required in `text`/`hybrid` mode |
| `query.vector` | float[] | — | Must match collection `dim`; forbidden in `text` mode; in `hybrid` it skips the embedding call |
| `mode` | `vector` \| `text` \| `hybrid` | `vector` | See [Search](search.md) |
| `k` | int | `10` | 1–1000 |
| `scope` | `chunks` \| `summaries` \| `both` | `chunks` | Record types searched |
| `filter` | object | none | Metadata filters, ANDed: scalar = equality, list = `in`, object = `gte`/`lte`/`gt`/`lt`, `in`, `contains`, `contains_any` (1–256 scalar alternatives); the reserved key `doc_id` filters on the document id (equality and `in` only) ([grammar](search.md#scope-and-filters)) |
| `expand` | object | none | Per-hit context expansion |
| `nprobe` | int | index default | [IVF](indexing.md) shards probed for this query (speed/recall knob); ignored without an index |
| `fusion` | `rrf` \| `relative` | `rrf` | `hybrid` only: how the two legs are fused ([Search](search.md#how-hybrid-works)); ignored in the other modes |
| `alpha` | float, 0–1 | `0.5` | `relative` fusion only: weight of the vector leg (0 = BM25 only, 1 = vector only); ignored by `rrf` |

**200**

```json
{
  "hits": [{
    "id": "contract-42#0",
    "doc_id": "contract-42",
    "type": "chunk",
    "position": 0,
    "text": "...",
    "metadata": {"src": "sharepoint"},
    "score": 0.0322,
    "expansion": {"siblings": ["..."], "summary": {"...": "..."}}
  }]
}
```

`score` is on the mode's scale ([Search](search.md#modes)): cosine for
`vector`, BM25 for `text`, and for `hybrid` an RRF score in `(0, 2/61]` or, with
`fusion: relative`, a score in `[0, 1]`.

**400** — query/mode mismatch, wrong vector dim, unsupported filter operator,
or a text query in `vector`/`hybrid` mode with no embedding endpoint
configured · **422** — a field outside its schema (`k` or `alpha` out of range,
an unknown `mode` or `fusion`).

---

## Health

`GET /healthz` — no auth

**200** `{"status": "ok", "resident_collections": ["kb"], "bm25": "native", "gil_enabled": true}`

`bm25` is the stage-2 BM25 scorer this instance runs: `native` (the `raggio_native`
extension, [ADR 0004](adr/0004-native-bm25.md)) or `python` (the extension is not
installed, was built for another Unicode version, or `NATIVE_BM25=0`). Both return the
same results.

`gil_enabled` is `false` only on the experimental free-threaded 3.14t image
([ADR 0006](adr/0006-free-threaded-python.md)).

While another process holds the data directory
([one process per data directory](storage.md#one-process-per-data-directory)),
`/healthz` answers **200** `{"status": "starting"}` and every other endpoint
**503** with `Retry-After: 2`. If start-up fails, `/healthz` answers **503**
`{"status": "failed"}`.
