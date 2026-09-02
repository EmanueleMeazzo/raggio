# Search

`POST /collections/{name}/search` supports three modes, chosen per request
with the `mode` field.

## Modes

| Mode | How it ranks | Query requirements | Score in `hits[].score` |
|---|---|---|---|
| `vector` (default) | Cosine similarity: quantized scan, then exact re-rank of the top candidates against the stored fp16 vectors | Exactly one of `query.text` (embedded server-side) or `query.vector` | Cosine similarity (exact), higher = better |
| `text` | BM25 over SQLite FTS5 — no embedding endpoint involved | `query.text` only | BM25, higher = better (unbounded) |
| `hybrid` | Both rankings fused with Reciprocal Rank Fusion | `query.text` required; `query.vector` optional (skips the embedding call) | RRF score in `(0, 2/61]` |

!!! warning "Scores are not comparable across modes"
    Each mode has its own score scale. Compare scores only within a single
    response.

The vector leg scans the quantized index (all vectors by default; on
collections with an [IVF index](indexing.md) attached it probes `nprobe`
shards instead, and the request-level `nprobe` field trades recall against
speed per query). The scan over-fetches, and the top candidates are re-ranked
against the fp16 originals kept on disk — so quantization affects which
candidates are *considered*, never how the returned hits are *ordered*, and
returned scores are exact cosine similarities.

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

## How hybrid works

1. Each leg (vector and BM25) retrieves `max(100, k)` candidates, both under
   the same `scope` and `filter`.
2. The two rankings are fused with **Reciprocal Rank Fusion**:
   `score(id) = Σ 1 / (60 + rank_in_leg)`. A record found by both legs scores
   higher than one found by either alone.
3. The fused top `k` is returned.

There is no model-based reranking stage and nothing to configure: RRF is pure
rank arithmetic.

!!! note "Records without text"
    Chunks ingested with only a `vector` are invisible to the BM25 leg (and
    to `text` mode). They can still be found by the vector leg.

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
      dates work naturally), `in`, and `contains` (case-insensitive substring,
      ASCII case folding only: `{"title": {"contains": "acme"}}`).

  There is no `or`, no `not`, and no filtering on `doc_id` or `text`. The same
  grammar drives the unranked [listing endpoint](api.md#list-records).

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
