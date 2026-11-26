# Search

`POST /collections/{name}/search` supports three modes, chosen per request
with the `mode` field.

## Modes

| Mode | How it ranks | Query requirements | Score in `hits[].score` |
|---|---|---|---|
| `vector` (default) | Cosine similarity: quantized scan, then exact re-rank of the top candidates against the stored fp16 vectors. In a [`dot` collection](#dot-product-collections), re-ranked by dot product against the stored float32 vectors | Exactly one of `query.text` (embedded server-side) or `query.vector` | Cosine similarity (exact), higher = better; in a `dot` collection the dot product |
| `text` | BM25 over SQLite FTS5 — no embedding endpoint involved | `query.text` only | BM25, higher = better (unbounded) |
| `hybrid` | Both rankings fused, with Reciprocal Rank Fusion (default) or relative score fusion | `query.text` required; `query.vector` optional (skips the embedding call) | RRF score in `(0, 2/61]`; relative score in `[0, 1]` |

!!! warning "Scores are not comparable across modes"
    Each mode has its own score scale. Compare scores only within a single
    response.

The vector leg scans the quantized index (all vectors by default; on
collections with an [IVF index](indexing.md) attached it probes `nprobe`
shards instead, and the request-level `nprobe` field trades recall against
speed per query). The scan over-fetches, and the top candidates are re-ranked
against the fp16 originals kept on disk — so quantization affects which
candidates are *considered*, never how the returned hits are *ordered*, and
returned scores are exact cosine similarities. A filter or scope that leaves
no more records than the re-rank depth (50 for `k` up to 25 at 4-bit, 200 for
a hybrid leg) skips the scan: those records' fp16 originals are scored
directly, with the same result (with an IVF index attached, one at least as
good: no shard goes unprobed) ([ADR 0007](adr/0007-sofia-backend-requirements.md)).

=== "vector"

    ```bash
    curl :8000/collections/kb/search -H "x-api-key: $KEY" --json '{
      "query": {"text": "what were the 2026 payment terms?"},
      "k": 5}'
    ```

=== "text"

    ```bash
    curl :8000/collections/kb/search -H "x-api-key: $KEY" --json '{
      "query": {"text": "ACME payment terms 2026"},
      "mode": "text",
      "k": 5}'
    ```

=== "hybrid"

    ```bash
    curl :8000/collections/kb/search -H "x-api-key: $KEY" --json '{
      "query": {"text": "what were the 2026 payment terms?"},
      "mode": "hybrid",
      "k": 5}'
    ```

### When to use which

- **vector** — paraphrased or conceptual queries; the classic RAG case.
- **text** — exact terms matter: identifiers, names, error codes, legal
  phrases. Never calls the embedding endpoint (vector mode can also skip it,
  but only when you supply a raw query vector).
- **hybrid** — the robust default for user-facing search: semantic recall
  from the vector leg, exact-term precision from the BM25 leg.

## Dot-product collections

By default raggio scales every vector to unit length, keeps an fp16 copy, and
scores by cosine. A collection created with `"metric": "dot"` instead keeps
each vector as the float32 values it was sent and ranks by their dot product
with the query as it was sent ([ADR 0020](adr/0020-dot-metric-float32.md)). It
is for clients that compute with their stored vectors, or whose vectors are
close to unit length without being exactly so and whose ranking must count
that length.

```bash
curl :8000/collections -H "x-api-key: $ROOT" --json '{"name": "kb", "dim": 1024, "metric": "dot"}'
# -> {"name": "kb", "dim": 1024, "bit_width": 4, "tokenizer": "unicode61", "fts_keys": null, "metric": "dot"}
```

In such a collection:

- **Stored vectors are the values sent.** Each JSON number becomes the nearest
  float32, and those are kept, unnormalised.
  `GET /collections/{name}/documents?include_vector=true` returns them bit for
  bit. A vector embedded by the server from `text` is kept as the embedding
  endpoint returned it.
- **`score` is the dot product** of the stored float32 values with the float32
  query, `Σ query[i] × stored[i]`, summed in double precision. The query is
  not normalised: twice the query gives twice every score. Scores are not
  bounded by 1 and can be negative.
- **Candidates still come from the quantized scan, by angle.** The scan keeps
  its usual `2k` candidates (never fewer than 50 at 4-bit, 200 at 2-bit; never
  more than 2000), and those are re-ranked by dot product. So this is an exact
  re-ranking of approximate candidates, not an exact search: a record outside
  the candidates is not returned, however large its dot product. For vectors
  near unit length the two orders differ only among close neighbours, which
  the candidates cover; for vectors of very different lengths they do not,
  and a `dot` collection is the wrong tool. Whenever the candidates cover
  everything searched (a collection, a filter result or a scope of at most
  that many records), the result is the exact top `k` by dot product.
- **Hybrid and text work as elsewhere.** `text` mode is the same BM25. In
  `hybrid` mode the vector leg is the dot-product ranking: RRF uses its ranks,
  relative fusion min-max normalises its dot products. Ranked sibling
  expansion uses the dot product too.
- **A zero or non-finite vector is refused**: the ingest job ends as `error`
  and stores nothing, and such a query is a `400`. A number beyond the float32
  range (about 3.4e38) counts as non-finite.
- **The metric is fixed at creation.** Creating an existing name with another
  metric is a `409` that names the existing one. Filters, scopes, expansion
  and the [IVF index](indexing.md) work unchanged.

The stored vectors take `dim × 4` bytes per record on disk instead of
`dim × 2` ([sizing](storage.md#sizing)); memory is the same.

### Notes for client authors

- **Check the answer to the create request.** It must contain
  `"metric": "dot"`. A raggio from before this option ignores the field,
  answers `201` and creates a cosine collection; its response has no `metric`
  key. `GET /collections/{name}` reports the metric too.
- **Send float32 values as the doubles they equal** (numpy:
  `vector.astype(numpy.float32).tolist()`), and read them back with
  `numpy.asarray(record["vector"], dtype=numpy.float32)`: the bytes are the
  ones you sent. To fetch particular records, filter the listing by `doc_id`
  (`filter={"doc_id": ["a", "b"]}`, up to 1000 records a page).
- **Do not expect your own float32 arithmetic to reproduce a score to the
  last bit.** A float32 `stored @ query` agrees to float32 rounding (about
  1e-7 relative), a float64 one to about 1e-15.
- **The candidate depth is the request's `k`**: to have more candidates
  considered, ask for a larger `k` (up to 1000, which re-ranks 2000) and keep
  the head of the list.
- **Replacing a vector is re-ingesting its record**: an upsert replaces the
  whole chunk, text and metadata included. Ingest is a job; a refused vector
  shows as `"status": "error"` on `GET /collections/{name}/jobs/{job_id}`.

## How hybrid works

1. Each leg (vector and BM25) retrieves `max(100, k)` candidates, both under
   the same `scope` and `filter`.
2. The two lists are fused, as the request's `fusion` field says:
    - `rrf` (default) — **Reciprocal Rank Fusion**:
      `score(id) = Σ 1 / (60 + rank_in_leg)`. A record found by both legs
      scores higher than one found by either alone. Only ranks count, and
      there is nothing to configure.
    - `relative` — **relative score fusion**, Weaviate's `relativeScoreFusion`:
      each leg's scores are min-max normalised to `[0, 1]` (best candidate 1,
      worst 0; a leg whose scores are all equal, a single candidate included,
      gives 1), then `score(id) = alpha × vector + (1 − alpha) × bm25`, where a
      leg that did not return the record adds 0. `alpha` defaults to `0.5`;
      `alpha: 1` (vector only) and `alpha: 0` (BM25 only) skip the other leg.
3. The fused top `k` is returned; ties keep the vector leg's order first.

There is no model-based reranking stage. Relative fusion is there for
clients written against Weaviate's hybrid search: same formula, same `[0, 1]`
scale, and `alpha` tunes the balance per request. Its scores are relative to
each query's own candidates, as Weaviate's are: a hit that tops both legs
scores 1 whatever the query, so a high score says the hit leads both rankings,
not that it is relevant in absolute terms.

```bash
curl :8000/collections/kb/search -H "x-api-key: $KEY" --json '{
  "query": {"text": "what were the 2026 payment terms?"},
  "mode": "hybrid", "fusion": "relative", "alpha": 0.5,
  "k": 5}'
```

`alpha` is ignored by `rrf`, and `fusion` outside `hybrid` mode.

!!! note "Records without text"
    A vector-only record can match BM25 when its collection indexes metadata
    through `fts_keys`. Without indexed metadata values it is invisible to the
    BM25 leg and `text` mode. The vector leg can still find it.

## What BM25 indexes

By default BM25 indexes each record's `text`. At collection creation, optional
`fts_keys`, for example `["title", "source", "tags"]`, append those metadata
values to the indexed text, in the specified order, separated by newlines.
Keys refer to literal top-level metadata names; `a.b` is a key, not a nested path.

Strings are used as supplied, numbers and booleans use Python `str()` (for example
`True` and `1.0`), and scalar lists are joined with spaces. Nulls and objects are
skipped. Null list members are skipped; a list containing an object or nested list
is skipped altogether. A null body contributes no text, so metadata alone can match.

The combined text is used for FTS5 ranking, full-query rescoring, document-scoped
search, length normalization and text-mode sibling expansion. Selected metadata
therefore also contributes to document length and term frequencies. Metadata is
never added to embeddings. Search hits and document reads retain the original
`text` and `metadata`, and filters still inspect metadata itself.

`fts_keys` is immutable; recreate a collection to change it. Metadata PATCH updates
the keyword index transactionally when selected values change. A patch to other
keys leaves that index untouched. Existing collections keep text-only indexing.

## Text query handling

Query text is treated as plain natural language, not FTS5 syntax: it is split
into word tokens, each quoted, and combined with `OR` — so any matching term
qualifies a record. Operators like `AND`, `NEAR` or `*` have no special
meaning. Only the first 100 tokens of a query are used. Empty or
whitespace-only query text is rejected with `400`.

Ranking always scores the **full query**. On large collections, letting FTS5
rank a query containing near-universal tokens would scan most of the corpus,
so retrieval runs in two stages: FTS5 generates candidates at bounded cost
(the rarest tokens ranked, plus records containing *every* query token), and
the candidates are then re-ranked with full-query BM25 (identical k1/b/IDF to
FTS5's `bm25()`) plus a small adjacent-bigram proximity bonus, so records
matching more of the query — or the query as a phrase — rank ahead of records
that merely repeat its rarest word. Small collections skip the second stage;
the single FTS5 query already ranks the full query there.

A query filtered by `doc_id` to at most 1000 records skips FTS5 altogether:
those records are ranked by the second stage directly, with the collection's
corpus-wide statistics, so searching inside a document costs what the document
costs, not what the corpus does. Its scores are always the second stage's,
small collections included. `trigram` collections, and `doc_id` filters
matching more records, take the FTS5 path
([ADR 0007](adr/0007-sofia-backend-requirements.md)).

## Tokenizers

BM25 tokenization is chosen per collection at creation and cannot be changed
afterwards:

| `tokenizer` | Matching | Use when |
|---|---|---|
| `unicode61` (default) | Exact tokens, language-neutral, diacritics removed (`perche` matches `perché`) | General text in any language |
| `trigram` | Case-insensitive substring matching (`gresql` matches `PostgreSQL`) | Codes, identifiers, partial terms — costs a larger index and noisier ranking |

!!! warning "Trigram minimum length"
    With `trigram`, query tokens shorter than 3 characters never match.

## Scope and filters

Both apply to every mode (in hybrid, to both legs):

```json
{
  "scope": "chunks",
  "filter": {"src": "sharepoint", "date": {"gte": "2026-01-01", "lte": "2026-06-30"}}
}
```

- `scope`: `chunks` (default), `summaries`, or `both`.
- `filter`: one clause per metadata key, all ANDed:
    - scalar — equality (`"src": "sharepoint"`);
    - list — membership (`"src": ["sharepoint", "smb"]`, same as `{"in": [...]}`);
    - object — `gte` / `lte` / `gt` / `lt` ranges (string comparison, so ISO
      dates work naturally), `in`, `contains` (case-insensitive substring,
      ASCII case folding only: `{"title": {"contains": "acme"}}`) and
      `contains_any` (a list of 1–256 scalar values, matching any substring with the same folding:
      `{"title": {"contains_any": ["q3", "report"]}}`).

  `contains_any` applies OR within that key and AND with other keys. It does not
  split phrases into words or require word boundaries; `%`, `_` and quotes are
  literal characters. Values use the same string conversion as `contains`.
  Empty, nested, non-list or oversized inputs return 400. More alternatives can
  increase metadata-scan cost, so use a short list of meaningful terms.

  `doc_id` is a reserved key: it filters on the document id itself, not on
  metadata, with equality or `in` only (`{"doc_id": "contract-42"}`,
  `{"doc_id": ["contract-42", "contract-43"]}`), and is answered from an
  index instead of a scan of every record's metadata. A metadata key named
  `doc_id` therefore cannot be filtered. There is no `or`, no `not`, and no
  filtering on `text`. The same grammar drives the unranked
  [listing endpoint](api.md#list-records).

## Result expansion

Each hit can be expanded with its neighborhood, useful for handing more
context to an LLM:

```json
{"expand": {"siblings_topk": 3, "summary": true}}
```

| Field | Effect |
|---|---|
| `siblings_topk: n` | Up to `n` most relevant sibling chunks from the same document. Ranked by vector similarity in `vector`/`hybrid` mode, by BM25 in `text` mode (siblings matching no query term are omitted). |
| `siblings_all: true` | All sibling chunks in document order |
| `summary: true` | The document's summary record, if one was ingested |

When ranked vector/hybrid expansion returns several hits from one document,
raggio reuses its sibling vectors within that request. The reuse is bounded;
larger or unsupported documents keep their ordinary scoring path. Measurements
and compatibility checks are in [ADR 0019](adr/0019-sibling-vector-reuse-reevaluation.md).

Expansion results are attached per hit under `expansion`:

```json
{
  "id": "contract-42#0",
  "score": 0.031,
  "expansion": {
    "siblings": [{"id": "contract-42#1", "...": "..."}],
    "summary": {"id": "contract-42", "type": "summary", "...": "..."}
  }
}
```
