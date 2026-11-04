# ADR 0009 — Reuse validated vectors during ingest payload conversion

Status: accepted · Date: 2026-11-04 · Builds on ADR 0008

## Context

After native JSON parsing, ingest still calls `IngestIn.model_dump()` before
enqueueing. This traverses and copies every validated vector list, even though
the existing journal encoder immediately converts those lists into a float32
matrix. A 250-vector request of dimension 1024 copies 256,000 floats through the
model serializer on the request's event-loop thread.

## Decision

`_ingest_payload` dumps the model with only summary/chunk vectors excluded, then
reconstructs each record using its validated vector list. Metadata retains normal
Pydantic serialization, including nested fields named `vector`. Record keys retain
the declared `RecordIn` / `ChunkIn` order, so the existing journal encoder produces
the same bytes as `body.model_dump()`. Summaries use the declared `RecordIn` fields
even if the supplied Python model is a subclass.

The payload holds strong references to those vectors while `Collection.enqueue`
passes it to the encoder thread. The encoder reads them without mutation and
materializes the float32 matrix and immutable journal bytes before committing the
job and returning. Workers subsequently read the journal, so request-model changes
after admission cannot alter a queued job. The route owns the validated model;
callers of the helper must not mutate its vectors during the awaited enqueue.

The API schema, validation, parser fallback, journal format, writer locking and
commit-before-202 contract remain the same. No dependency, Rust crate or
configuration option is added.

`tests/test_ingest_payload.py` checks exact journal bytes across mixed records,
metadata, Unicode, coercions, non-finite and signed-zero floats, duplicate IDs and
random inputs. It also checks vector identity, detached metadata, declared summary
fields, encoder non-mutation and durable replay after request-model mutation.

## Measurements

The conversion mode of `bench/ingest_json_probe.py` compares the same application
with two payload converters: `body.model_dump()` (copy) and `_ingest_payload`
(reuse). Both arms use ADR 0008's native JSON parser. The converter is patched for
each synchronous request, with the same patch machinery outside both timers.
The default parsing mode still compares stdlib/native parsing using `model_dump`
in both arms, to retain ADR 0008's comparison.

Five fresh paired rounds used 20 timed requests per arm, after three warmups, with
alternating arm order across rounds and requests. Workers were paused to isolate
durable admission: parse, validate, convert, encode, commit, return 202. The final
job in each arm was checked to remain pending. The generated seed-719 input
contained 250 normalized, five-decimal float32 vectors of dimension 1024, text and
metadata: 2,312,459 JSON bytes. The converters' journal bytes were checked equal.
Component timings use an already validated model, five warmups and the mean of
20 operations per round.

Environment: x86_64 WSL2 Linux, 3-CPU quota, 4 GiB container limit, local container
storage, `OPENBLAS_NUM_THREADS=1`, CPython 3.12.15, FastAPI 0.142.2, Pydantic 2.13.5,
NumPy 2.5.3, turbovec 1.0.0 and SQLite 3.53.1. Dependencies came from the repository's
frozen lock file, including its native extension. Baseline behavior is main
`96b55ed93ee4` with its existing native parser and ordinary model dump.

Medians across the five rounds:

| Metric | Copy vectors | Reuse vectors | Change |
|---|---:|---:|---:|
| Model conversion (ms/request) | 6.74 | 0.91 | 86.4% lower |
| Conversion + journal encoding (ms/request) | 11.96 | 7.56 | 36.8% lower |
| Durable admission p50 (ms/request) | 35.07 | 29.02 | 17.3% lower |
| Durable admission p95 (ms/request) | 43.24 | 35.79 | 17.2% lower |
| Process CPU per admission (ms/request) | 37.93 | 30.10 | 20.6% lower |

Admission p50 improved in all five pairs, by 16.4–25.6%. The baseline p50 ranged
from 31.44–40.42 ms and reuse from 26.01–30.33 ms. Raw results are in
`bench/results-ingest-conversion.json`.

A small-request check used one 8-dimensional vector (737 JSON bytes), five pairs
and 50 timed requests per arm. Admission p50 was 1.32 vs 1.28 ms, CPU 1.40 vs
1.41 ms and p95 1.92 vs 2.21 ms. Pairwise p50 changes had mixed signs. Conversion
itself added about 2 microseconds (1.76 vs 3.63 microseconds); no small-request
speedup is claimed. Raw results: `bench/results-ingest-conversion-small.json`.

The transport is in-process ASGI `TestClient`; CPU includes its work. These are
local admission measurements with paused workers, not completed ingest throughput,
embedding latency or concurrent production load. The p95 samples are small and
do not establish production tail latency. The large-corpus benchmarks were not
rerun. ADR 0008's measurements were a separate run; their absolute timings and
percentage gains should not be combined with this comparison.

Reproduce with local temporary storage:

```bash
uv run --group bench python bench/ingest_json_probe.py --comparison conversion --pairs 5 --iterations 20 --out results.json
uv run --group bench python bench/ingest_json_probe.py --comparison conversion --rows 1 --dim 8 --pairs 5 --iterations 50 --out small-results.json
```

## Consequences

Large precomputed-vector requests avoid an intermediate list copy and spend less
CPU on admission. Small requests pay a few microseconds for record reconstruction.
Future ingest model or serializer changes must update the exclusion mask and
record reconstruction together and retain the journal-byte parity tests.
