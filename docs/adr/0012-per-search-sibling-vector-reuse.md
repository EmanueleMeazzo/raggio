# ADR 0012 — Bounded per-search sibling-vector reuse

Status: accepted · Date: 2026-11-04 · Implements the vector-reuse candidate in ADR 0011

## Decision and scope

Reuse normalized retained sibling vectors within one search when ranked vector
expansion returns several hits from a document. Keep the existing ranking, self
exclusion, membership, score calculation and legacy-vector fallback. Text expansion,
summary-only expansion, `siblings_all` and searches without repeated document hits
retain the original reads. Ingestion, storage formats, dependencies and request
schemas are unaffected.

The production implementation uses explicit per-search data, rather than the
exploration prototype's context variable and patched vector-fetch method. All
preparation runs in a worker while `Collection.search` holds its existing read lock.
The result is passed to `_expand`/`_search_allowed`; nothing is stored on the collection.

The subsequent [Sofia deployment report](../research/sofia-retrieval-benchmark-2026-11-04.md)
evaluates PR #9, not this change. Sofia's planned flow does not use
`expand.siblings_topk`; this reuse is an optional feature improvement and is not
expected to accelerate that flow. Further Sofia work should target its summary
and in-document queries directly.

## Eligibility, bounds and compatibility

- Prioritize documents with more returned hits. Fetch their indexed chunk IDs with
  `ORDER BY id LIMIT min(rescore_depth, remaining_row_budget) + 1` through the covering
  document/type/indexed index. Reject oversized documents without reading their full
  membership or vector payload. No corpus-wide count or scan is added.
- Bound prepared float32 payload to 8 MiB and row bookkeeping to 4,096 rows per search.
  A document must fit completely within the remaining budget and exact-search depth;
  do not enlarge the existing exact-search threshold.
- Fetch/decode/renormalize eligible fp16 vectors using the existing `_fp16_vecs` routine.
  Cache only complete valid documents; any missing or invalid blob preserves that
  document's ordinary quantized fallback.
- Keep cached arrays read-only. Each hit still executes its original membership
  query, including self exclusion and `indexed=1`, and constructs a fresh float32
  matrix in the same rowid order for the original dot product and tie break.
- Continue hydrating fresh metadata per occurrence; sibling score or mutable metadata
  objects are not shared. No ranked result or query response is cached.

The 8 MiB bound covers the entire prepared float32 backing matrix, including rows
discarded from partially valid documents. It excludes decode temporaries, bookkeeping,
the main rescore and returned hits. The separate row cap bounds bookkeeping at low
dimensions. This is not an overall process-memory limit or a promised RSS reduction.

## Concurrency and cancellation

The active search holds the collection read lock across preparation and expansion,
so membership/vector writes cannot change its snapshot. SQL row materialization runs
inside existing per-thread read-connection guards; no guard nests, awaits or leaks a
cursor. The cache is immutable after construction and used only by that search's
worker calls.

Cancellation releases the parent search's read lock while a `to_thread` worker may
continue. Limited ID reads remain bounded in this case, even if a writer grows a
document. Generation checks discard preparation if a write is observed between its
reads. A stopped collection continues to fail closed through `_reading`. No orphan
can publish a collection-level cache. Cached data is released when its search/worker
references end.

## Evaluation protocol

`bench/expansion_read_probe.py --production` compares the real production method with
the original expansion loop. Both use the same current search/collection implementation;
only reuse preparation is bypassed in the baseline. Patch machinery is outside both
clocks, and every response is compared byte for byte. Baseline behavior is main
`6327693`; ADR 0011's prototype measurements are historical and separate.

Two temporary collections each contain 10,000 chunks and 500 summaries, dimension
3072, four-bit indexing, 20 chunks per document, Unicode text and nested metadata.
Seed 821 generates clustered and dispersed vectors. Requests use pre-encoded
five-decimal vectors; hybrid supplies text and vector, avoiding embedding calls.
Each of twelve single-client cases has three warmups per arm, then five alternating
paired rounds of 20 requests per arm.

Environment: x86_64 WSL2 Linux, local container storage, three-CPU quota, 4 GiB limit,
`OPENBLAS_NUM_THREADS=1`, CPython 3.12.15, FastAPI 0.142.2, Pydantic 2.13.5,
NumPy 2.5.3, turbovec 1.0.0, SQLite 3.53.1, frozen dependencies and native BM25.
In-process ASGI TestClient timings include client work and exclude network/embedding
costs. These are local synthetic measurements, not DGX/NFS production measurements.

## Results

Median of five paired rounds; milliseconds per request. Negative changes mean less time/work.

| Case | Baseline p50 | Reuse p50 | Latency change | Baseline CPU | Reuse CPU | CPU change |
|---|---:|---:|---:|---:|---:|---:|
| `clustered-k5-ranked` | 16.339 | 15.757 | -3.6% | 17.862 | 16.572 | -7.2% |
| `clustered-k10-ranked` | 18.994 | 16.433 | -13.5% | 20.194 | 17.217 | -14.7% |
| `clustered-k10-summary` | 9.183 | 9.237 | +0.6% | 9.386 | 9.531 | +1.5% |
| `clustered-k10-all` | 11.430 | 11.661 | +2.0% | 11.704 | 11.733 | +0.3% |
| `clustered-k100-all` | 40.058 | 43.540 | +8.7% | 46.715 | 45.049 | -3.6% |
| `dispersed-k10-ranked` | 19.501 | 19.353 | -0.8% | 21.528 | 20.381 | -5.3% |
| `dispersed-k10-all` | 12.094 | 11.852 | -2.0% | 12.415 | 12.381 | -0.3% |
| `summary-hits-all` | 9.991 | 9.848 | -1.4% | 10.071 | 10.427 | +3.5% |
| `summaries-k10-ranked` | 15.613 | 15.621 | +0.1% | 15.922 | 15.987 | +0.4% |
| `text-k10-ranked` | 11.801 | 11.350 | -3.8% | 12.255 | 11.925 | -2.7% |
| `hybrid-k10-ranked` | 23.143 | 20.087 | -13.2% | 25.471 | 22.352 | -12.2% |
| `clustered-k10-control` | 9.262 | 9.435 | +1.9% | 9.571 | 9.711 | +1.5% |

The ten-hit repeated-document cases reduce latency by 13.5% (vector) and 13.2%
(hybrid), and CPU by 14.7% and 12.2%. Five-hit gains are smaller. The ranked
summary-hit control is effectively unchanged with one client. The 100-hit
`siblings_all` control measures 8.7% higher p50 despite 3.6% less CPU and
unchanged SQL; no benefit is claimed for paths that cannot reuse vectors.

Measured production store SHA-256: `f47c3f31e5e1ea1ac0fb58f10fbcc9a09ffcdf86c8657017d92d0f1c6da74686`.

At ten repeated-document vector hits, vector-blob SELECTs fall from 11 to 2, and all
SELECTs from 52 to 44. Peak temporary store allocations are 1,668,628 vs 1,668,372 bytes.
Hybrid peaks are 6,311,494 vs 6,311,078 bytes. The useful result is less repeated
work and CPU, not reduced measured peak memory. Unsupported/control paths show mixed
changes despite retaining the same operations; do not promise a general speedup.

SQL and allocation tracing run separately from timing, on one warm store search.
Allocation peaks include the returned hits but exclude pre-existing collection/index
memory. They do not measure process RSS. Single-client raw rounds, source hash,
environment, SQL categories and allocations are in `bench/results-sibling-vector-reuse.json`.

### Concurrent follow-up

The final concurrent experiment uses eight client threads, a 20-request warmup per
arm, then five alternating rounds of 100 requests per arm. Summary-hit ranked
expansion is a strict no-reuse control: a valid collection has one summary per
document, so its ten hits are distinct. All responses are compared byte for byte.
The raw concurrent rounds are included in the same published results file. Preliminary
runs also exposed variable controls and prompted a direct original-loop fallback for
ineligible searches; they are retained locally in
`C:/DevOps/raggio-sibling-reuse-20261104/` and are not the source of the table below.

| Case | Baseline median QPS (range) | Reuse median QPS (range) | Baseline CPU ms | Reuse CPU ms |
|---|---:|---:|---:|---:|
| `clustered-k10-ranked` | 30.45 (28.2–34.8) | 51.24 (39.6–66.3) | 49.99 | 29.53 |
| `hybrid-k10-ranked` | 24.55 (19.4–26.5) | 29.45 (24.1–34.2) | 66.28 | 55.14 |
| `dispersed-k10-ranked` | 36.76 (26.1–46.5) | 36.06 (27.0–43.4) | 42.06 | 42.65 |
| `summaries-k10-ranked` | 33.32 (29.4–38.6) | 30.81 (23.7–35.5) | 47.92 | 51.35 |

Repeated-document cases improve in this run, but the dispersed control is
mixed and the strict summary control has 7.5% lower median throughput and
7.2% more CPU. That adverse control result remains unresolved locally;
the wide round-to-round spread prevents attributing every concurrent change
to vector reuse. Do not infer a general concurrent speedup or advertise the
larger vector QPS change as an expected production gain. Validate representative
concurrent workloads on deployment hardware before rollout.

These samples do not establish production throughput or tail latency. Deployments
with different CPU quotas, BLAS threading, document sizes and hit distributions need
their own load measurements.

## Correctness and validation

`tests/test_sibling_vectors.py` covers exact serialized scores/order on flat and IVF
indexes; 2-bit/4-bit vectors; dimensions 8, 24, 32, 40, 1024 and 3072; exact-depth boundaries
and 512/513-ID fetch boundaries; ties; Unicode61/trigram; relative fusion including
zero weight; summary hits, missing summaries and no hits; legacy missing/invalid
vectors and unindexed/NULL-position chunks; budget exhaustion; independent metadata;
and freshness after metadata/vector writes.

Deterministic lifecycle tests verify off-loop preparation, writer exclusion while
the search is active, and discarded preparation after cancellation followed by a
write or stop. A query-plan check confirms bounded covering-index admission without
a temporary sort or a vector fetch for oversized documents. The existing mixed-write
and shutdown stress suite is retained.

The final source passes the complete native CPython 3.12 Linux suite: **675 passed,
4 skipped**, with `RAGGIO_STRESS_SECONDS=30` (135.35 seconds overall). This includes
the 32 new parity/resource/lifecycle cases and the existing 16-client mixed-write
and shutdown stress tests. All final benchmark responses match byte for byte,
including eight concurrent clients. The MkDocs strict build passes. Logs and JUnit
results are retained locally in `C:/DevOps/raggio-sibling-reuse-20261104/`.

The repository CI also runs the full native suite on x86_64 and ARM64, alongside
the Python reference suite and experimental free-threaded native jobs on both
architectures. These are the platform gates for this pull request; local x86_64
measurements alone do not establish ARM64 performance.

Reproduce using the same limits and native environment:

```bash
uv run --group bench --extra native python bench/expansion_read_probe.py --production --concurrency 8 --baseline-revision 6327693 --pairs 5 --iterations 20 --out results.json
uv run --group bench --extra native python bench/expansion_read_probe.py --production --concurrency 8 --concurrency-only --baseline-revision 6327693 --pairs 5 --iterations 20 --out concurrent.json
uv run --group bench --extra native pytest tests/test_sibling_vectors.py -q
```
