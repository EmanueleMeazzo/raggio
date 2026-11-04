# ADR 0013 — Bounded batching of warm scope searches

Status: accepted candidate implementation · Date: 2026-11-04 · Builds on ADRs 0003–0005 and 0010

## Context and scope

The [Sofia deployment benchmark](../research/sofia-retrieval-benchmark-2026-11-04.md)
validates PR #9's latency improvement, but reports little change to saturated
throughput. Sofia uses summary search and in-document search, without ranked
sibling expansion. This change branches from main `6327693` independently of
[draft PR #10](https://github.com/EmanueleMeazzo/raggio/pull/10), whose sibling reuse
does not affect that planned flow. ADRs 0011–0012 are tracked in that separate PR.

`_vector_ids` sends unrestricted vector queries through `_scan_batched`, sharing
scan/rescore work across already-arrived queries. Scope searches on collections
containing both chunks and summaries build a cached allowlist but previously scan
and fetch fp16 candidates independently for every request. Summary search therefore
misses the existing coalescing path. In-document searches often have small allowlists
and already use direct exact scoring.

## Decision

Coalesce **warm scope-only vector searches** when their cached allowlist exceeds
the existing exact-search depth. Use a fixed scope cache key on the event loop;
explicit filters retain their original worker-side key construction and SQL path.
This avoids moving potentially large filter serialization onto the event loop and
keeps Sofia's in-document path independent.

The same vector leg is eligible inside hybrid search. Text retrieval, explicit
document/metadata filters, cold/empty/small allowlists, ingestion, storage and public
request schemas retain their existing paths. No persistent vector or result cache,
runtime dependency or new deployment setting is added.

Each independent cohort is keyed by allowlist **object identity**, exact candidate
depth and effective IVF `nprobe`. Sharing an array identifies one immutable cached
membership snapshot; equal arrays from different cache snapshots are not combined.
Different filters/scopes, depths or probes have separate drain tasks, and the
unrestricted scan queue remains separate. Queries with different final k may share
only when their original over-fetch depth is identical.

No timer waits for a batch to fill. Drain only requests already queued. A singleton
uses the ordinary `_search_allowed` call. A multi-query cohort uses one index call
and the existing union fp16 fetch/rescore, then hydrates each response independently.

## Score and routing compatibility

Keep candidate depth and effective probe count equal to each independent request;
merging the largest depth/probe across incompatible requests could change recall.
Scoped IVF cohorts request `_single_query_routing`: compute centroid similarities
with each original one-row query shape. A matrix-matrix reduction can differ from
the original one-row reduction at a centroid selection boundary. Existing
unrestricted batching and direct IVF calls keep their prior default calculation.

Index task order, candidate merge, fp16 normalization/rescore, score/id tie break,
relative/RRF fusion and per-request hydration retain their existing implementation.
Partially retained or invalid legacy vector blobs still use their ordinary quantized
score fallback. Exact serialized result equality is the correctness gate.

## Resource and lifecycle bounds

- Limit a cohort to 16 queries and a worst-case 16 MiB of float32 rescore rows,
  using `candidate_depth × dimension × 4` per query. If one existing request already
  exceeds that payload, process it alone under its prior per-request limit.
- This bounds cohort materialization, not overall process RSS. Decode/normalization
  temporaries, index task/candidate arrays, metadata, pending request vectors and
  other active groups are outside that payload estimate.
- Each non-cancelled waiter holds the collection's existing read lock. Writes cannot
  change its index/membership snapshot. Worker SQL uses the existing read guards;
  no cursor or connection guard crosses an await.
- Skip cancelled queued waiters; cancellation of one reader does not cancel others.
  A worker exception resolves every affected live future, and later cohorts can retry.
  Cancelling a drain propagates cancellation to its remaining waiters.
- If all parents cancel, a worker may continue without their read locks, as in the
  existing scan path. Guarded reads and closed-collection failures still apply;
  orphaned results go to no live waiter. Drain state is removed on completion/error/
  cancellation, and no prepared vector cache is published to the collection.

## Experiments and protocol

`bench/scoped_retrieval_probe.py` builds generated local temporary data: 20,000
chunks, 1,000 summaries, dimension 1024, four-bit vectors, 20 chunks per document,
nested Unicode metadata, 32 IVF lists and `nprobe=16`. Seeds are 927 (data) and 929
(queries). IVF training uses the ordinary collection sampler, whose sample/order
is not fixed by those fixture seeds. Both arms share the same resident index within
each run; fresh runs can train different partitions. Fixture construction and
diagnostics are outside endpoint timers.

Requests provide pre-encoded five-decimal vectors, avoiding embeddings. Cases are
summary k=10, in-document k=5, explicit metadata-filtered k=10 and unrestricted k=10
over both types. No expansion is requested. The last three are control paths for
the scoped optimization. In-process ASGI TestClient measurements include client work,
and exclude network latency; corpus/index statistics differ from Sofia's deployment.

Each case has a 20-request warmup per arm. Five rounds each measure 100 requests per
arm (five repetitions of 20 queries), at 1 / 8 / 16 clients. Single-client requests
alternate arms per query; concurrent rounds alternate arm order. All response bytes
must match. Final comparison uses the same production vector method in both arms:
the baseline replaces only `_scan_scoped` with independent calls, and both install
equivalent wrapper/patch machinery outside clocks. Scope cache admission is shared;
baseline candidate selection/scoring remains main's independent behavior.

A preliminary production comparison instead replayed main's full vector method.
It also improved every summary load round, but divergent control timings prompted
the final shared-path comparison. Its raw run is retained separately; do not combine
the two protocols' medians.

Environment: native CPython 3.12.15, x86_64 WSL2 Linux, local container storage,
three-CPU quota, 4 GiB limit, `OPENBLAS_NUM_THREADS=1`, SQLite 3.53.1 and frozen
dependencies. The raw results record package versions and the measured store hash.
These are local experiments; deployment gains require a Sofia re-run.

## Results

Changes below are the median of **per-round paired ratios**, not the ratio of
separately pooled medians. Negative latency/CPU changes mean less time/work.
The table uses the final shared-path IVF comparison.

| Case / clients | Paired p50 change | Paired QPS change | Paired CPU/query change |
|---|---:|---:|---:|
| `summary-k10-c1` | +0.5% | -0.7% | +0.7% |
| `summary-k10-c8` | -39.4% | +66.3% | -43.0% |
| `summary-k10-c16` | -40.7% | +62.4% | -41.3% |
| `document-k5-c1` | +0.7% | +0.2% | -0.0% |
| `document-k5-c8` | +0.2% | -0.1% | +3.5% |
| `document-k5-c16` | -17.7% | +11.9% | -18.4% |
| `metadata-k10-c1` | -1.7% | -1.1% | +1.7% |
| `metadata-k10-c8` | +10.2% | -13.4% | +13.1% |
| `metadata-k10-c16` | -2.8% | +2.8% | -2.5% |
| `plain-k10-c1` | -3.5% | +1.0% | -0.9% |
| `plain-k10-c8` | -10.9% | +10.7% | -6.8% |
| `plain-k10-c16` | -13.4% | +10.0% | -12.8% |

Summary searches improve in every concurrent round: paired median QPS rises
66.3% at eight clients and 62.4% at sixteen, with 43.0% and 41.3% less CPU
per query. Concurrent p50 falls 39.4% and 40.7%. Single-client summary p50
changes by +0.5%; there is no claimed low-load latency improvement.

Controls still vary despite identical code paths: the metadata c=8 control
shows -13.4% paired QPS while the plain c=8 control shows +10.7%. These
are not effects of scoped batching. Do not predict deployment throughput
from a pooled ratio or claim that every query improves. A Sofia re-run
with its own index, limits and query mix is required before rollout.

A separate flat-index check uses the same fixture and three paired rounds:

| Flat summary clients | Paired p50 change | Paired QPS change | Paired CPU/query change |
|---|---:|---:|---:|
| 1 | +2.7% | -1.9% | +2.0% |
| 8 | -39.3% | +57.6% | -51.3% |
| 16 | -45.5% | +71.9% | -55.0% |

Raw final rounds are in `bench/results-scoped-search.json` and
`bench/results-scoped-search-flat.json`. The first full-method baseline
comparison is `bench/results-scoped-search-main-reference.json`; the pilot
with off-loop hydration is `bench/results-scoped-retrieval-pilot.json`.
Neither preliminary run is the source of the final table above.

Measured production store SHA-256: `2e5d9647907df908f4dec19c4ab48a0d682ec9eb73392a2ae4a5f18c35fc631d`.

Eight-query store cohort diagnostics, measured separately from endpoint timing:

| Case | Vector fetches baseline / batch | Candidate rows baseline / batch | Temporary peak bytes baseline / batch |
|---|---:|---:|---:|
| `summary-k10` | 8 / 1 | 400 / 335 | 1,161,083 / 3,609,924 |
| `document-k5` | 8 / 8 | 160 / 160 | 393,308 / 715,234 |
| `metadata-k10` | 8 / 8 | 400 / 400 | 1,116,054 / 1,382,366 |
| `plain-k10` | 1 / 1 | 396 / 396 | 4,244,911 / 4,244,863 |

The summary cohort fetches once instead of eight times and decodes 335 rather
than 400 candidate vectors in this sample. Its temporary allocation peak
increases from 1,161,083 to 3,609,924 bytes: about 2.45 MB more, released
after the cohort. This is a CPU/throughput improvement with a bounded
temporary memory cost, not a memory reduction. The flat check shows the
same tradeoff. The 16 MiB payload cap is not a process-memory guarantee.

Stage timings are separate diagnostics over twenty single store requests and are
nested rather than additive. An eight-query store cohort separately records vector
fetches, candidate row counts and temporary traced allocations. It excludes existing
index/collection memory and does not measure process RSS. Controls retain their
original work, so mixed differences on those paths are not optimization gains.

## Rejected or deferred alternatives

The initial prototype compared batching and moving hydration to `asyncio.to_thread`
independently. Hydration was a small component of single-query time, and moving it
to a worker hurt concurrent performance in the pilot. No production hydration
change is made. The pilot is historical evidence, not the final performance claim.

The first general batching prototype also serialized explicit filter keys on the
event loop. The production scope is narrower: fixed scope keys only. Broader filtered
batching needs its own profile and admission design; small exact document queries
and varied cold filters should not pay an extra worker/queue stage without evidence.

This change does not improve Sofia's common small in-document requests. Their
request/response overhead remains a separate profiling target. CPU utilization alone
does not justify removing the GIL or promoting the experimental interpreter; the
tradeoffs and decision gates in ADR 0006 still apply.

## Validation and reproduction

`tests/test_scoped_batching.py` has 31 cases covering exact serialized score/order
parity with flat/IVF, 2/4-bit vectors, dimensions 24/40/1024/3072, RRF/relative fusion
including zero weight, mixed k/probes/scopes, ties, legacy vectors and unindexed rows,
bounded cohorts, cold/empty/small bypass, independent fresh metadata, failed workers,
partial/all cancellation, writes/shutdown and centroid selection boundaries. Flat
candidate-cutoff ties are checked below and above the native 32,768-row pool threshold.

The native CPython 3.12 full suite passed **672 tests, 4 skipped**, including the
30-second concurrency stress run, in 130.77 seconds. The two candidate-cutoff tie
cases were then added; the complete focused file passed **31 tests** in 21.33
seconds with the same measured runtime source. The strict MkDocs build passed.
Pull-request CI also exercises native x86_64/ARM64 and the experimental
free-threaded interpreters; those checks remain rollout gates.

```bash
uv run --group bench --extra native python bench/scoped_retrieval_probe.py --arms production --baseline shared --pairs 5 --iterations 20 --concurrency 8 16 --out results.json
uv run --group bench --extra native pytest tests/test_scoped_batching.py -q
```
