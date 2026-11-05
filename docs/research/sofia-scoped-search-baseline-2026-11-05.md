# Sofia retrieval baseline after PR #11

Recorded: 2026-11-05. The user supplied this Sofia benchmark summary and accepted
[PR #11](https://github.com/EmanueleMeazzo/raggio/pull/11) as the new baseline.
The tables below are transcribed from the [source screenshot](assets/sofia-scoped-search-baseline-2026-11-05.png).
No benchmark was rerun for this document; raw requests, per-query samples, repeated
rounds, resource logs and benchmark scripts were not supplied.

## Baseline identity

- Previous baseline: main `632769359c783435cdfdf3ed916110560ab5608b`, including PR #9.
- Tested implementation: `1cdae1df2196634b914662b470e4fa7d3c0e2fc5`, PR #11's bounded
  warm scope-search batching, documented in [ADR 0013](../adr/0013-scoped-search-batching.md).
- Accepted code baseline: main after merge `01b160ff62a7d03573e2f5f9536399b8861b7814`.
  The report and decision updates add documentation without changing that runtime.
- PR #10 was closed without merging on 2026-11-05; its optional ranked sibling-vector
  reuse is excluded from this baseline. [ADR 0012](../adr/0012-per-search-sibling-vector-reuse.md)
  retains the evaluation and closure rationale.
- Store source SHA-256:
  `2e5d9647907df908f4dec19c4ab48a0d682ec9eb73392a2ae4a5f18c35fc631d`.
- The same implementation passed all five PR CI jobs: production/reference,
  native x86_64/ARM64, and experimental free-threaded x86_64/ARM64 including their
  extended concurrency stress runs. The production interpreter decision is unchanged.

## Reported deployment setup

Both runs used the same data state, with 33 resident collections including PRD
copies, a 3 vCPU / 6 GiB container, and the same 60 queries on `legal_knowhow`
(about 165,000 records, IVF, `nprobe=16`). Summary search is Sofia's first step.
No ranked sibling expansion is reported for this workload.

## Summary search results

Percent changes are calculated from the displayed rounded values. CPU values are
approximate. These deployment measurements are separate from ADR 0013's generated
local fixture and must not be combined with its paired ratios.

| Metric | Main `6327693` | PR #11 `1cdae1d` | Change |
|---|---:|---:|---:|
| QPS, 1 client | 49.3 | 45.2 | -8.3% |
| QPS, 8 clients | 49.4 | 60.0 | +21.5% |
| QPS, 16 clients | 47.8 | 58.6 | +22.6% |
| p50 / p95, 8 clients | 153 / 248 ms | 127 / 173 ms | p50 -17.0%, p95 -30.2% |
| p50 / p95, 16 clients | 327 / 435 ms | 287 / 344 ms | p50 -12.2%, p95 -20.9% |
| CPU per query at saturation | approximately 60 ms | approximately 44 ms | approximately -26.7% |
| Busy cores at saturation | approximately 2.9 of 3 | approximately 2.6 of 3 | Less CPU pressure |

The report describes the single-client QPS difference as within noise. Without
raw repeated runs this document cannot establish that statistically; retain the
observed decline and claim no single-query speed improvement. Concurrent summary
capacity improves by about 22%, with less CPU per query and better tail latency.
This is smaller than the local synthetic gain and is the relevant deployment evidence.

## Control query shapes

| Metric | Main `6327693` | PR #11 `1cdae1d` |
|---|---:|---:|
| In-document QPS, 8 / 16 clients | 308 / 302 | 307 / 306 |
| Plain vector QPS, 8 / 16 clients | 153 / 155 | 158 / 159 |
| Sequential summary p50 / p95 | 18.4 / 30.0 ms | 19.2 / 32.5 ms |

In-document searches retain their exact small-set path; unrestricted vector
searches were already batched. Their throughput changes are small, and the
sequential summary sample is slightly slower. PR #11 improves concurrent summary
capacity rather than the frequent low-concurrency document-search latency.

The report cites Weaviate at 77 summary QPS, narrowing its throughput ratio over
raggio from about 1.6 to 1.3. This is a reported comparator, not a new independent
Weaviate measurement. In-document searches still plateau near 305 QPS at similar
CPU use. Serialization/GIL contention remains a hypothesis; utilization alone
does not identify the limiting stage or justify a different interpreter.

## Memory interpretation

Reported working set was 4.5 GiB in the baseline run and 1.5 GiB in the PR #11 run.
The baseline container had remained running after overnight PRD copies and still
held ingestion file cache; the candidate container had just restarted. **Do not
attribute this difference to PR #11 or treat it as a controlled memory comparison.**

ADR 0013's separate local eight-query diagnostic found about 2.45 MB *more*
temporary allocation with batching. Its 16 MiB per-cohort float32 payload budget
is not an overall RSS cap. The deployment working-set sample does not supersede
that documented CPU/temporary-memory tradeoff.

## Use for future comparisons

Use the PR #11 runtime on main as the comparison baseline for further retrieval
work. Keep the November 4 PR #9 report and ADR 0013's local raw experiments as
historical evidence, with their original revision and environment labels.

Future benchmarks should fix the corpus/index state, query requests, interpreter,
CPU/memory limits and BLAS settings; give both arms equivalent restart/cache warmup
history, alternate repeated rounds, check result IDs/scores/order, and retain raw
samples. Report single-query and concurrent latency, QPS, CPU/query and controlled
memory separately. Ingestion remains outside the optimization scope.
