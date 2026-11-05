# ADR 0014 — Bounded contains_any metadata filtering

Status: accepted · Date: 2026-11-05 · Extends ADR 0007 · Implementation: PR #12

## Context

Sofia can ask for documents by title or source. A contiguous `contains` filter for
`q3 report` does not find `Report - Q3`. The existing grammar ANDs metadata keys;
it cannot express alternative substrings of a single key. PR #12 adds that operation
without replacing existing filters or introducing a general boolean filter language.

This is a retrieval capability change, not a speed optimization. The accepted
[PR #11 performance baseline](../research/sofia-scoped-search-baseline-2026-11-05.md)
remains historical evidence for its tested revision and query shapes. Ingestion,
indexing, ranking, rescoring, batching and result expansion are unaffected.

## Decision and contract

Accept `{"title": {"contains_any": ["q3", "report"]}}` as OR of the existing
`contains` predicate on one metadata key. Parenthesize the OR group so scope,
`indexed = 1`, other keys and other operators on that key remain ANDed with it.
The shared filter compiler applies this to unranked listing and vector/text/hybrid
retrieval. The reserved `doc_id` key still supports only equality and `in`.

Require a list of **1–256 scalar values**. Reject empty, nested, non-list and
oversized operands with `ValueError`, translated to HTTP 400 by the existing
listing/search error handlers. Scalar conversion matches `contains` (`str(value)`).

Use parameterized `instr(lower(json_extract(metadata, ?)), lower(?)) > 0` for each
alternative. Metadata paths and values are bound parameters; quotes, `%` and `_`
are literal substring characters. Folding remains SQLite's ASCII-only `lower()`.
The operator does not tokenize a phrase, remove accents or require word boundaries;
`report` also matches `Reportage`. An empty-string alternative retains `contains`
behavior and matches existing non-NULL values. NULL/missing metadata does not match.
Callers wanting word alternatives must supply their own terms and account for the
broader substring semantics rather than assuming exact Weaviate token parity.

## Review correction and cost

Review reproduced a failure in the initial PR at `e463ce7`: a valid-looking list
of 1,001 alternatives caused `sqlite3.OperationalError: Expression tree is too large
(maximum depth 1000)` instead of a client error. The explicit 256-value maximum
keeps each new OR group comfortably below that limit and bounds generated predicates.
It is an API limit, not a general maximum on all filter complexity or process memory.

Each alternative adds a substring predicate and bound parameters. Cost depends on
alternative count, match order, metadata size and scanned rows; no performance-neutral
claim or new throughput/CPU measurement is made. Prefer short meaningful term lists.
Existing queries that do not use this operator keep their existing SQL predicates.

## Validation and records

The focused native CPython 3.12 review run passed five tests, including the original
listing/filter and `doc_id` tests plus three added regression cases. They cover:

- Sofia's title example in listing and all search modes, scope/key conjunctions,
  missing metadata and substring rather than whole-word/phrase-token behavior.
- Literal wildcard/quote characters, bound values resembling SQL, ASCII-only case
  folding, and NULL/missing/empty-substring behavior.
- The accepted 256-value boundary and rejected 257/1,001-value lists, alongside
  empty/nested/non-list operands, through listing and every search mode.

The original PR's five platform CI jobs passed before the review correction.
Merge of the final head is gated on the same reference/native/free-threaded suite
matrix on x86_64 and ARM64. Documentation is checked with a strict MkDocs build.
API grammar is published in [Search](../search.md#scope-and-filters) and
[API reference](../api.md#search).

No storage migration, ingestion change or runtime dependency is required.
