# ADR 0019 — Current-main sibling-vector reuse reevaluation

Status: accepted · Date: 2026-11-08

Builds on [ADR 0012](0012-per-search-sibling-vector-reuse.md), whose historical
candidate remains unmerged, and preserves [ADR 0013](0013-scoped-search-batching.md)
and [ADR 0017](0017-turbovec-1-1.md).

## Decision

Adopt the validated current-main port of request-scoped sibling reuse for raggio's
existing ranked-expansion feature. The maintainer authorized inclusion after the
regression gates pass. The measured controls have no gate failures; the full
production/native/free-threaded CI matrix must also pass before integration.
The historical PR #10 branch remains preserved as an archive. Sofia's recorded
summary/in-document flow without expansion has no claimed performance benefit.

The cache remains per request, bounded to 8 MiB of prepared float32 rows and 4,096
rows, with read-only arrays and existing membership/scoring/legacy fallback.
Generation and stopped-reader guards continue to bound orphaned preparation.
No persistent cache, public setting, ingest change or storage format is introduced.

## Evidence and gates

The [reevaluation report](../research/sibling-vector-reevaluation-2026-11-08.md)
records the frozen source versions, protocol, calibration correction, full controls,
resource audit and raw recordings. Primary Wikipedia data comprises 5,000 articles,
dimension 1024, on GN100 at 3 CPUs / 4 GiB with fixed fast-core affinity.

At eight clients, requested k=10 ranked in-document search uses 12.6% less server
CPU, with 7.5% lower paired p50 and 8.8% higher QPS, beyond matched A/A noise.
Global ranked cases reduce CPU by 3.7–4.8%; their endpoint gains have wider noise.
The old ranked-summary slowdown is not reproduced. Longer ordinary-query controls
show small changes, with uncertainty recorded rather than claimed equivalence.
The favorable 3072-dimension synthetic case remains separate from real data.

Exact response parity, bounded resources and the full native Windows/Linux arm64
suites pass. Optional 3.14t candidate/lifecycle checks pass with the GIL off;
3.14t performance remains unmeasured. A contaminated long A/A recording is preserved
and excluded; its replacement completes on an idle host. The usable recordings
contain 579,758 exact-parity HTTP requests, with maximum host CPU busy 21.0%.

## Limits and compatibility

The real-data benefit is modest and workload dependent. Temporary allocation
peaks do not materially decrease; the explicit cache bound excludes response,
bookkeeping and native memory. Host-local HTTP and local storage do not establish
Sofia's deployment results. The optimization activates for eligible repeated
document hits using ranked vector/hybrid expansion. Other request shapes and
unsupported documents retain their existing operations and score behavior.
