# Sibling-vector reuse reevaluation protocol

Prepared 2026-11-08, before new performance measurements.

## Frozen comparison

- Baseline: main `37c686eb1c1ee89f31f91b71d133b25a772f7011` (raggio 0.4.0).
- Historical candidate: `614c4b87f6411a449fe9ab8bc10e0cc666b7233c`.
- Port only sibling reuse onto current main. Preserve current scoped batching,
  metadata FTS, JSON handling, lifecycle fixes and turbovec 1.1.2.
- The reference copies all three changed production methods, not just the
  expansion-loop wrapper. Keep identical dependencies, native BM25 and one BLAS
  thread in both arms. Evaluate production CPython 3.12 first.
- Original branch remains untouched. This investigation does not approve adoption.

## Correctness and resource gates

1. Exact serialized scores, IDs, order and expansion contents against the frozen
   current-main methods; cover flat/IVF, 2/4-bit, dimension/alignment, legacy rows,
   ties, fusion, tokenizers and expansion modes.
2. Existing metadata and lifecycle checks stay green. Preparation must remain off
   the loop, bounded during cancellation, and discarded after a write or close.
3. Prepared float32 storage stays at most 8 MiB and 4,096 rows per request. This
   bound excludes Python bookkeeping, decode temporaries and returned responses;
   measure total temporary allocations and process RSS separately.
4. Show reduced vector reads on eligible repeated-document requests. Controls
   must not prepare vectors or introduce extra vector reads.

## Workloads

Use deterministic document/chunk data, seed 821, 20 chunks and one summary per
document. Primary dimension is 1024; use 3072 as a targeted sensitivity check.
Include an oversized-document fallback case and byte-equal responses across arms.

- Vector and hybrid ranked sibling expansion, k=5 and k=10, with repeated documents.
- Ranked expansion with one result per document and summary-only result hits.
- Sofia controls: summary search and filtered in-document vector/hybrid search
  without expansion.
- Expansion controls: summary-only and `siblings_all`; these cannot use this cache.
- Concurrency 1 and 8. Greater concurrency or free-threading is follow-up work if
  the production-interpreter comparison shows a useful result.

Fixture shapes are functional proxies, not evidence that a real caller uses ranked
expansion. Current `doc-expand` measurements use `siblings_all` and cannot establish
the candidate's benefit. Report applicability separately from speed.

## Measurement

Start with same-code A/A comparisons for a positive-shaped request, the strict
ranked-summary control, and ordinary summary search. Then measure A/B with seven
alternating paired rounds; each measured arm runs for at least two seconds.
Warm each case first. Record every round and preserve ordering, sample counts,
response parity, environment, source hashes and fixture identity.

For hardware evaluation, run isolated HTTP servers on GN100 with identical data
copies, interpreter, dependencies, CPU/memory limits and BLAS configuration. Use
a fixed external client. Collect server process CPU and RSS independently of client
CPU; record latency percentiles and throughput. Monitor host contention. Never
replace the regular checkout or touch unrelated services/data volumes.

SQL/array diagnostics and allocation tracing run separately from latency timings.
In-process local measurements, if used, include client cost and are direction-only;
they cannot establish deployment throughput or server-only CPU improvements.

## Interpretation and disposition

Report paired percentage changes and deterministic paired-bootstrap 95% intervals.
Measure an A/A noise envelope using the same analysis. A useful result requires
an eligible-case gain beyond both the A/A envelope and an interval containing zero.
Treat an unaffected-control loss above max(3%, its A/A envelope), with an interval
excluding zero, as a regression gate failure. If uncertainty is too wide, report it
as inconclusive rather than claiming equivalence or attributing the old slowdown.

No adoption recommendation without exact correctness, bounded resources, useful
ranked-expansion gains and acceptable unaffected controls. Record limitations and
the outcome in an ADR, retain raw results, and return the decision for discussion.
