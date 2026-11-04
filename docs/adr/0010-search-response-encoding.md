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

Return the existing `JSONResponse(content={"hits": hits})` directly from the search
endpoint. Use its existing stdlib JSON encoding and UTF-8/error policy. No dependency,
request parser, retrieval algorithm or storage change is added.

The request schema, ranking, filters, expansion and authorization use the same
code. Exact response bytes, content type and content length match the ordinary
FastAPI return path in endpoint parity tests for vector/text/hybrid, relative
fusion at multiple weights, scopes, expansion, no hits and nested Unicode/numeric
metadata. Non-finite metadata retains the same encoding error. New hit fields must
be JSON-compatible before reaching this response: datetime, bytes, NumPy arrays or
Pydantic objects would require explicit conversion rather than this fast path.

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
response. Both apply the same patch machinery outside the clocks. Each case has
three warmup requests per arm, then five paired rounds of 20 requests per arm with
alternating arm order. Response bytes are compared for every paired request.

Concurrency uses eight client threads, one untimed 20-request warmup per arm,
then five rounds of 100 requests per arm. Both arms compare the same ordered
response bytes. A preliminary run used shorter concurrent bursts; the final
protocol warms client threads and measures longer samples. Its original raw run
is retained locally in `C:/DevOps/raggio-retrieval-20261104/results-search-response.json`.

Environment: x86_64 WSL2 Linux, 3-CPU quota, 4 GiB container limit, local container
storage, `OPENBLAS_NUM_THREADS=1`, CPython 3.12.15, FastAPI 0.142.2, Pydantic 2.13.5,
NumPy 2.5.3, turbovec 1.0.0 and SQLite 3.53.1. The frozen lock file and native BM25
extension were used. Baseline behavior is main `813b6da`.

## Results

Final paired measurements are in `bench/results-search-response.json`.

Medians over five rounds. Times are milliseconds per search; negative improvement means a slower median.

| Case | p50 default | p50 direct | p50 improvement | CPU default | CPU direct |
|---|---:|---:|---:|---:|---:|
| Vector, 10 hits | 5.32 | 5.03 | 5.6% | 5.33 | 5.05 |
| Text, 10 hits | 4.08 | 3.92 | 3.8% | 4.22 | 4.06 |
| Hybrid, 10 hits | 6.98 | 7.07 | -1.4% | 10.00 | 9.82 |
| Vector, 100 hits | 8.46 | 7.16 | 15.3% | 8.84 | 7.25 |
| Vector, 1,000 hits | 48.14 | 33.81 | 29.8% | 50.90 | 35.10 |
| Vector, 10 hits + expansion | 8.59 | 8.09 | 5.9% | 8.94 | 8.32 |
| Text, 10 hits + expansion | 6.25 | 5.95 | 4.8% | 6.41 | 6.21 |
| Document-filtered vector, 10 hits | 1.61 | 1.45 | 9.6% | 1.67 | 1.54 |

Concurrent vector throughput was 216.0 vs 233.7 QPS.
Round ranges were 195.4–238.6 vs 225.4–250.3;
the overlapping ranges support a directional result, not a production throughput claim.

Encoding component timings use already hydrated responses. Allocation tracing runs
separately from timing and measures temporary Python encoding allocations, excluding
the pre-existing hits. It does not measure service RSS, SQLite memory or index size.

| Response | Encode p50 default (ms) | Encode p50 direct (ms) | Temporary peak default (bytes) | Temporary peak direct (bytes) |
|---|---:|---:|---:|---:|
| Vector, 10 hits | 0.154 | 0.027 | 38,677 | 30,517 |
| Vector, 100 hits | 1.583 | 0.246 | 372,023 | 297,607 |
| Vector, 1,000 hits | 15.794 | 2.334 | 3,706,039 | 2,968,487 |
| Vector, 10 hits + expansion | 0.646 | 0.104 | 166,083 | 128,627 |

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
