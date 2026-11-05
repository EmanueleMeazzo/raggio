# Sofia retrieval benchmark after PR #9

Date: 2026-11-04. Source: a benchmark summary supplied by the user from Sofia's
environment. Values below are transcribed from the [original screenshot](assets/sofia-retrieval-benchmark-2026-11-04.png).
This is deployment evidence for [ADR 0010](../adr/0010-search-response-encoding.md),
separate from its local synthetic measurements. The raw requests, per-query samples,
resource logs and scripts were not supplied; no new benchmark was run for this report.

This is historical evidence for the PR #9 revision. The subsequent
[PR #11 evaluation and accepted baseline](sofia-scoped-search-baseline-2026-11-05.md)
records the scoped-search optimization and replaces PR #9 as the comparison baseline.

## Reported setup

- Same settings in the earlier baseline and PR #9 run: IVF index, `nprobe=16`,
  3 vCPU / 6 GiB, the same 60 queries, and the same in-environment client.
- Collections: `legal_knowhow` and `datascience`.
- DEV raggio was reported running main, image label `raggio:0.2.x-6327693`.
- PR #9 is merged at `6327693`. PR #10 was not tested in this deployment run.
- Sofia's planned flow uses summary search and search within one document;
  in-document search is reported as the most frequent query in `document_search`.
  The planned flow does not use `expand.siblings_topk`.

## Single-query latency

Times are milliseconds. Improvement is `(before - after) / before`, calculated
from the displayed rounded p50 values.

| `legal_knowhow` query | Before PR #9 p50 / p95 | With PR #9 p50 / p95 | p50 improvement | Weaviate p50 / p95 |
|---|---:|---:|---:|---:|
| Summary search, k=10 | 21.5 / 30.9 | 19.8 / 30.9 | 7.9% | 25.4 / 27.2 |
| Search within one document, k=5 | 5.2 / 6.4 | 3.6 / 5.4 | 30.8% | 4.0 / 7.1 |
| Plain vector top-10 | 10.9 / 12.8 | 8.9 / 11.2 | 18.3% | 4.4 / 5.8 |

| `datascience` query | Before PR #9 p50 | With PR #9 p50 | p50 improvement |
|---|---:|---:|---:|
| Summary search | 20.4 | 16.3 | 20.1% |
| Search within one document | 5.0 | 3.2 | 36.0% |
| Plain vector | 13.1 | 11.3 | 13.7% |

The strongest measured benefit is in-document latency, improving on both collections.
On `legal_knowhow`, raggio has lower p50 than Weaviate for both planned Sofia query
shapes. The summary p95 is unchanged and remains higher than Weaviate's; plain vector
top-10 remains slower than Weaviate. Do not describe every latency metric as a win.

## Throughput under load

Maximum reported QPS on `legal_knowhow`. Each cell lists 1 / 8 / 16 clients.

| Query | Before PR #9 | With PR #9 | Weaviate |
|---|---:|---:|---:|
| Summary search | 44 / 49 / 47 | 48 / 50 / 48 | 39 / 77 / 77 |
| Search within one document | 180 / 293 / 278 | 248 / 308 / 304 | 224 / 819 / 833 |
| Plain vector | 95 / 139 / 139 | 121 / 149 / 147 | 222 / 873 / 890 |

Single-client QPS rises 37.8% for in-document search, 27.4% for plain vector and
9.1% for summary search. At eight clients, gains are 5.1%, 7.2% and 2.0%
respectively; at sixteen, 9.4%, 5.8% and 2.1%. Latency gains do not establish a
similar improvement in throughput under load.

The report says CPU per query is nearly unchanged under 8–16 clients. Summary
search uses all three cores near 49 QPS; in-document search levels off around
305 QPS with about 1.7 cores busy. The latter is consistent with serialization or
GIL contention, but utilization alone does not identify the limiting stage.
At sixteen clients, Weaviate reaches about 1.6 times raggio's summary QPS and
2.7 times its in-document QPS. These are throughput ratios for this run, not
latency ratios or a forecast for other CPU limits.

## Memory and workload value

Reported raggio working set stays around 0.87 GiB, compared with Weaviate's
5.6 GiB: about 84.5% lower, or 6.4 times smaller. This is an observed working set,
not a maximum-memory guarantee or a new memory saving caused by PR #9.

Sofia is reported to need only a few searches per second at peak, so latency on
its actual query shapes has more immediate value than maximum-QPS parity.
PR #9 therefore provides useful deployment evidence for the focused Python/native
architecture. This run does not evaluate a Rust rewrite or isolate language costs.

## Implications for the next investigation

PR #10's bounded sibling-vector reuse applies to ranked vector expansion through
`expand.siblings_topk`. It cannot improve the planned Sofia requests described here.
Keep its optional feature evaluation separate; do not attribute these results to it
or require an expanded-sibling benchmark to assess Sofia's normal flow.

Prioritize the existing scoped-search path and result materialization:

1. Profile summary scope and in-document filters at 1 / 8 / 16 clients on the same
   corpus. Separate request parsing/normalization, allowlist construction/cache
   misses, masked index search, fp16 fetch/rescoring, hydration/metadata decoding,
   response encoding, event-loop delay and worker-queue time. Record p50/p95,
   CPU/query and working set alongside QPS.
2. In the current implementation, `_vector_ids` routes summary-scoped searches
   when chunk rows exist, and document-filtered searches, through a per-request
   allowlist path. They bypass `_scan_batched`, which coalesces unrestricted
   full-index queries. Evaluate batching compatible large scoped allowlists only
   if profiling shows repeated scans/rescoring dominate; preserve snapshot,
   cancellation, score/order and low-load latency behavior. Small in-document
   allowlists already use direct exact scoring and may need a different improvement.
3. `_hydrate` performs record reads and metadata decoding synchronously in
   `Collection.search`. Measure its event-loop contribution before choosing
   off-loop materialization or native work; moving work to threads does not itself
   remove the GIL or guarantee less CPU.

[ADR 0006](../adr/0006-free-threaded-python.md) already records mixed free-threaded
tradeoffs, including memory and single-query latency costs. Its experimental image
can help isolate interpreter contention after profiling, but the utilization figures
here do not justify making it the default or overriding its decision gates.

Use the same query requests, corpus/index snapshot, software versions, CPU/memory
limits and BLAS settings in alternating baseline/candidate runs. Check response
IDs, scores and order, and retain raw rounds. Improvements for Sofia should include
its frequent low-concurrency requests, not just saturated synthetic expansion.
