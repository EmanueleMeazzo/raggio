# ADR 0015: Select metadata keys for BM25

Date: 2026-11-05. Status: accepted after Sofia DEV validation; merged in PR #14.

## Problem and scope

Sofia searches for documents by title, but titles, SharePoint sources and tags live
in metadata rather than summary/chunk bodies. Text-only BM25 cannot match these
values. Its migration handover identified this retrieval-quality gap as R4 and
reserved ADR 0015. This change is stacked on native search JSON parsing (ADR 0016,
PR #13); it does not roll that work back. It adds a retrieval capability rather than
claiming another performance improvement. Version becomes 0.3.0.

## Decision

Collections optionally declare immutable `fts_keys`: at most eight unique literal
top-level keys matching `[A-Za-z0-9_.-]{1,64}`, excluding `doc_id`. Persist the list
as JSON in the catalog and return it in collection creation/info. Omitted/null/empty
keys keep the original record and FTS schemas. Existing catalog rows migrate to null;
opening those collections does not rebuild their keyword index.

Opt-in records have a stored `fts_metadata` suffix and a **VIRTUAL** generated
`bm25_text = coalesce(text, '') || fts_metadata`. Only the selected metadata text is
duplicated on disk; the body is not duplicated. FTS5 has one external-content column,
`bm25_text`, preserving its single-column BM25 statistics. All candidate fetches,
document-scoped direct scoring and average-length sampling read that same column.
Text-mode siblings and vocabulary frequencies read its FTS5 index. No Python
function, JSON decoding or key traversal runs when readers evaluate the generated
column; SQLite concatenates the body and suffix.

The write helper converts values once: strings unchanged, numbers and booleans with
Python `str()`, scalar lists space-joined. Objects/nulls are skipped. Null list members
are skipped; lists containing objects or nested lists are skipped altogether. Keys
with dots are literal names. Each included value has a preceding newline, in key
order. Metadata alone can match a null-body vector record. An opt-in length sample
with no tokens uses 1.0 to avoid division by zero when only separators or punctuation
remain. Embeddings, vector storage, response bodies and metadata filters stay original.

Inserts/upserts calculate the suffix before the database write lock. Existing delete
then insert semantics keep replay idempotent. PATCH of selected keys uses one UPDATE
that applies SQLite RFC 7396 `json_patch` and calculates the new suffix with the same
helper, registered on the writer only. An UPDATE trigger deletes the **exact OLD
generated text** from FTS5 and inserts the NEW text, in that transaction, only when
the combined text changes. Non-selected patches use the old UPDATE and do no suffix
conversion or FTS writes. Exceptions explicitly roll back metadata and postings.

Selected-key PATCH immediately clears df/avgdl caches; every successful patch
invalidates filter allowlists. Upserts/deletes keep the existing churn-based df/avgdl
staleness tolerance. The FTS schema repair check distinguishes both tokenizer and
content-column differences, and rebuilds table/triggers/backfill atomically.

## Alternatives considered

- Pure SQL VIRTUAL conversion of JSON: SQLite generated expressions cannot contain
  `json_each` subqueries needed for arbitrary scalar arrays. SQLite casts also differ
  from Python `str()` for booleans and some floats. A string-only approximation fails
  the handover contract.
- VIRTUAL conversion through a deterministic Python UDF: handles the contract with
  no stored suffix, but adds Python callbacks, JSON parsing, allocations and GIL work
  to candidate rescoring, direct search and length sampling. Rejected on design grounds;
  no performance measurement is claimed for this alternative.
- Materialize the entire combined text: simple reads, but duplicates every document
  body. Store the much smaller suffix and generate the concatenation instead.
- A second FTS column for metadata: rejected because per-column statistics/weights
  change BM25 behavior and diverge from the single-text stage-2 scorer (ADR 0003).

## Correctness and evidence

`tests/test_fts_keys.py` covers title-only text/hybrid (both fusions), both scopes and
tokenizers, native/reference scoring, stage-2 candidate fetches, direct scoring,
length sampling, siblings, conversion/null bodies, original reads/listings and filters,
validation, non-indexed/indexed PATCH, rollback, upsert/delete, old catalog/schema,
column repair, restart with legacy/binary pending jobs and IVF attach/detach.
Mutation tests run FTS5 `integrity-check` **with rank=1**, also checking external content.

The local cost protocol and raw data are in [the evaluation report](../research/fts-keys-evaluation-2026-11-05.md).
These generated-data measurements do not establish Sofia recall or remote/NFS latency.

## Sofia acceptance and merge, 2026-11-05

Sofia recreated the three document collections with `["title", "source", "tags"]`
and reported matching title top-3 results with Weaviate, equal document/chunk counts,
and zero mismatches in 100 spot checks per collection. The user accepted the result
and authorized merging PRs #13 and #14. Main merge `2e8082b` contains the accepted
0.3.0 runtime. The [deployment acceptance report](../research/sofia-fts-keys-acceptance-2026-11-05.md)
preserves the screenshot, exact counts, exclusions, image identity and changed IVF
settings. It validates quality and copy integrity; deployment performance remains
to be measured with those settings.
