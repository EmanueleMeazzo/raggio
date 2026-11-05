# ADR 0010 — Direct encoding of hydrated search results

Status: accepted · Date: 2026-11-04 · Builds on ADRs 0003–0005

## Context

The next optimization priority is retrieval latency, CPU and memory. Ingestion is
already sufficiently fast for its frequency. Vector scans and stage-2 BM25 already
run natively; a language rewrite would leave their engine and SQLite costs in place.

`POST /collections/{name}/search` returns hydrated hits containing strings, numbers,
booleans, nulls, lists and dictionaries. Metadata comes from `json.loads` of stored
JSON; scores are Python numbers; expansion uses the same hydrated fields. FastAPI's
ordinary dictionary return path recursively walks and copies these values through
`jsonable_encoder` before its `JSONResponse` serializes them. That second conversion
adds CPU and temporary allocations without changing the values.

## Decision

`_search_response` checks the result containers without copying them, then returns
the existing `JSONResponse(content={"hits": hits})` directly when FastAPI's encoder
would preserve every value. Dictionaries with `_sa*` keys or non-string keys, and
values outside the built-in JSON types, return the original content dictionary so
FastAPI handles them normally. Containers deeper than 32 levels also use that
fallback, bounding inspection of deep or cyclic structures. Its
`sqlalchemy_safe=True` default removes `_sa*`
keys even in user metadata; bypassing that behavior would change valid responses.
Use the existing stdlib JSON encoding and UTF-8/error policy. No dependency,
request parser, retrieval algorithm or storage change is added.

The request schema, ranking, filters, expansion and authorization use the same
code. Exact response bytes, content type and content length match the ordinary
FastAPI return path in endpoint parity tests for vector/text/hybrid, relative
fusion at multiple weights, scopes, expansion, no hits and nested Unicode/numeric
metadata, including nested `_sa*` keys. Non-finite metadata retains the same encoding
error. Bytes, datetime, paths, tuples, NumPy scalar subclasses and non-string keys
retain FastAPI's conversion through the fallback. Deep and cyclic values also
retain its handling. The check does not mutate results;
compatibility cases retain the ordinary encoder and pay the added inspection cost.

## Measurement protocol

`bench/search_response_probe.py` uses one temporary local collection containing
20,000 synthetic chunks plus 1,000 summaries, dimension 1024, four-bit indexing,
text and nested metadata. Fixture construction uses the ordinary storage/index
path outside the timers. Data seed is 719 and query-vector seed is 743. Requests
carry pre-encoded, five-decimal vectors; hybrid supplies both text and vector, so
there is no external embedding call. Text queries use the same synthetic topic
query. The index is flat, below turbovec's 32,768-row single-query pool threshold.

Both arms use the same application and collection. The baseline restores the
dictionary return for FastAPI's automatic encoder; the candidate uses the direct
response, including its compatibility check. Both apply the same patch machinery
outside the clocks. Each case has
three warmup requests per arm, then five paired rounds of 20 requests per arm with
alternating arm order. Response bytes are compared for every paired request.

Concurrency uses eight client threads, one untimed 20-request warmup per arm,
then five rounds of 100 requests per arm. Both arms compare the same ordered
response bytes. A preliminary run used shorter concurrent bursts; the final
protocol warms client threads and measures longer samples. Its original raw run
is retained locally in `C:/DevOps/raggio-retrieval-20261104/results-search-response.json`.
The subsequent unchecked-response experiment is also retained locally as
`results-search-response-final.json`; discovery of the `_sa*` compatibility behavior
required the guard above and a fresh measurement. The committed results measure
the guarded implementation.

Environment: x86_64 WSL2 Linux, 3-CPU quota, 4 GiB container limit, local container
storage, `OPENBLAS_NUM_THREADS=1`, CPython 3.12.15, FastAPI 0.142.2, Pydantic 2.13.5,
NumPy 2.5.3, turbovec 1.0.0 and SQLite 3.53.1. The frozen lock file and native BM25
extension were used. Baseline behavior is main `813b6da`.

## Results

Final paired measurements are in `bench/results-search-response.json`.

Medians over five rounds. Times are milliseconds per search; negative improvement means a slower median.

| Case | p50 default | p50 direct | p50 improvement | CPU default | CPU direct |
|---|---:|---:|---:|---:|---:|
| Vector, 10 hits | 7.46 | 7.33 | 1.8% | 7.86 | 7.56 |
| Text, 10 hits | 5.11 | 4.81 | 5.9% | 5.41 | 4.97 |
| Hybrid, 10 hits | 8.43 | 8.49 | -0.7% | 11.90 | 11.95 |
| Vector, 100 hits | 12.52 | 10.81 | 13.6% | 13.27 | 11.81 |
| Vector, 1,000 hits | 59.82 | 46.05 | 23.0% | 65.70 | 46.75 |
| Vector, 10 hits + expansion | 14.85 | 13.98 | 5.8% | 15.41 | 14.26 |
| Text, 10 hits + expansion | 9.92 | 9.32 | 6.0% | 9.96 | 9.99 |
| Document-filtered vector, 10 hits | 2.54 | 2.36 | 7.1% | 2.75 | 2.56 |

Concurrent vector throughput was 232.6 vs 240.4 QPS.
Round ranges were 196.6–272.1 vs 230.8–294.8;
the overlapping ranges support a directional result, not a production throughput claim.

Encoding component timings use already hydrated responses. Allocation tracing runs
separately from timing and measures temporary Python encoding allocations, excluding
the pre-existing hits. It does not measure service RSS, SQLite memory or index size.

| Response | Encode p50 default (ms) | Encode p50 direct (ms) | Temporary peak default (bytes) | Temporary peak direct (bytes) |
|---|---:|---:|---:|---:|
| Vector, 10 hits | 0.182 | 0.065 | 38,677 | 31,525 |
| Vector, 100 hits | 1.985 | 0.622 | 372,023 | 303,655 |
| Vector, 1,000 hits | 19.563 | 6.556 | 3,706,039 | 3,024,935 |
| Vector, 10 hits + expansion | 0.677 | 0.211 | 166,083 | 129,803 |

The transport is in-process ASGI `TestClient`, so CPU includes client work. These
are local synthetic measurements, not a re-run of the 2.55M-row DGX benchmark.
Small top-10 savings are only tenths of a millisecond and overlap run-to-run
variation. Larger responses have more encoder work to remove. The small p95 and
concurrent samples do not establish production tail latency or throughput.
Ranking/recall algorithms were not modified; response-byte parity is the gate.

Reproduce with local temporary storage and the same CPU/memory limits:

```bash
uv run --group bench python bench/search_response_probe.py --pairs 5 --iterations 20 --out results.json
```

## Rejected experiments

### Filtered FTS5 `ORDER BY rank`

The unfiltered text path already uses FTS5's optimized rank cursor. Replacing
`bm25(records_fts)` with `records_fts.rank` in the joined/filtered path removed a
temporary SQL sort but made retrieval slower in this experiment. The rank cursor
sorts matches before the record filter; the existing shape can filter rows first.

`bench/fts_rank_probe.py` built 100,000 synthetic text records, then compared four
rare/common OR expressions with scope, metadata and document filters, six timed
alternating repeats per case. IDs, scores and order matched. Common-term scope
queries took 59.25 vs 81.11 ms; metadata filtering 45.16 vs 67.48 ms; a small
document filter 14.31 vs 127.76 ms. Rare-term cases also provided no meaningful
improvement. No production SQL change was made. Existing document-scoped text
search retains ADR 0007's direct-score path.

### Automatically cap IVF threads to the CPU quota

`bench/ivf_thread_probe.py` used 65,536 synthetic 1024-dimension vectors in 16 balanced
shards, eight probes, on the same three-CPU quota. Pool sizes 1, 3 and 12 were compared
at batch sizes 1, 8 and 16, with three alternating rounds of ten batches. IDs and
float32 score bits matched the serial reference. This is a scheduling experiment;
the balanced random shards do not represent trained real-corpus IVF recall.

At batch size eight, milliseconds per query:

| Threads | Wall time | Process CPU |
|---:|---:|---:|
| 1 | 4.41 | 4.41 |
| 3 | 1.94 | 5.18 |
| 12 | 1.73 | 5.45 |

Fewer threads trade latency for CPU. Capping to three did not improve speed and
did not consistently lower CPU across all batch sizes. ADR 0005's default and
explicit `IVF_SEARCH_THREADS` override remain available; this experiment does not
justify changing the default solely from a quota calculation.

Both rejected experiments used the same software environment as above. Raw
results and query plans are in `bench/results-retrieval-experiments.json`.

```bash
uv run --group bench python bench/fts_rank_probe.py --out fts-results.json
uv run --group bench python bench/ivf_thread_probe.py --out ivf-results.json
```

## Further retrieval work

### Sofia deployment follow-up, 2026-11-04

The user supplied a [Sofia benchmark summary](../research/sofia-retrieval-benchmark-2026-11-04.md)
for main `6327693`, using the same earlier-run settings (IVF, `nprobe=16`,
3 vCPU / 6 GiB, 60 queries and in-environment client). In-document p50 improves
from 5.2 to 3.6 ms on `legal_knowhow` and 5.0 to 3.2 ms on `datascience`
(30.8% and 36.0%). Summary p50 improves too, but `legal_knowhow` summary p95
stays at 30.9 ms. Throughput under 8–16 clients moves modestly, and the reported
0.87 GiB working set remains much smaller than Weaviate's 5.6 GiB.

The linked report retains the supplied screenshot, exact displayed tables,
provenance and limits; these are reported deployment measurements, not a local
re-run or raw samples. Sofia's planned requests do not use `expand.siblings_topk`,
so ADR 0012's optional reuse is outside this workload. The next investigation
should profile scoped ranking/rescoring and hydration for Sofia's actual query
shapes. CPU utilization suggests possible serialization but does not prove the GIL
is the sole bottleneck. Existing free-threaded decision gates in ADR 0006 remain.

### Remaining candidates

The next candidates require workload-specific profiles, rather than another
language change:

- Consolidate repeated document/sibling/summary reads during expansion, especially
  when several hits belong to the same document. Preserve exclusion, order and
  score behavior, and check both legacy missing-vector and concurrent-write cases.
- Measure selective metadata-filter misses and allowlist-cache memory on a large
  corpus. The cache is bounded by eight entries, not bytes; wide distinct filters
  can retain large arrays. Indexing hot metadata fields and a byte budget require
  evaluating the extra cache misses against the memory saved.
- Profile the HTTP/event-loop and scan-batch pipeline on the DGX. Hydration and
  batch scheduling are remaining application costs; CPU quota, thread pools and
  SQLite contention must be held constant in comparisons.
