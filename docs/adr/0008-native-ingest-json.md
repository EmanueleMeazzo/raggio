# ADR 0008 — Native JSON parsing for ingest admission

Status: accepted · Date: 2026-11-04 · Narrows ADR 0001's stdlib-only JSON stance for the ingest request body

## Context

The Rust rewrite evaluation found that the vector engine and stage-2 BM25 already
run natively. A remaining cost is parsing precomputed vectors: FastAPI first
materializes the JSON body with stdlib `json.loads`, then Pydantic validates it and
builds the ingest models. A 250-vector, 1024-dimension request contains about 2.3 MB
of rounded float32 JSON. Pydantic's existing Rust parser can validate directly from
those bytes, avoiding the intermediate dictionary and vector-list copies.

## Decision

- Only `POST /collections/{name}/documents` uses `_IngestJSONRoute`. Its request
  adapter calls `IngestIn.model_validate_json` and supplies the resulting model to
  FastAPI's existing single-body-model validation and dependency handling.
- Keep the endpoint's `IngestIn` annotation, status code, OpenAPI schema, collection
  authorization, record/dimension checks and journal-before-202 behavior.
- Reuse the original request's cached body, including when middleware has read it.
- On Pydantic `ValidationError`, use the original request's stdlib JSON decoder.
  FastAPI then produces its ordinary syntax/model errors in the same dependency
  order. This also preserves accepted UTF-8 BOM, UTF-16/32 and lone-surrogate
  inputs that the native parser rejects. Exceptional requests may be parsed twice.
- Other request bodies, metadata and the binary job journal retain their current
  processing. There is no new dependency, configuration knob or Rust crate.
  Parsing still runs on the request's event-loop thread; no GIL-release or
  concurrent completed-ingest claim is made.

`tests/test_ingest_json.py` compares the route against ordinary FastAPI for valid
models, exact errors, authorization precedence, content types, malformed JSON,
legacy encodings, OpenAPI and numeric bits. Existing API, ingest durability and
concurrency tests continue to exercise the production endpoint.

## Measurements

`bench/ingest_json_probe.py` ran five paired rounds of 20 requests per arm, with
alternating arm order. Each fresh temporary collection used the same API, SQLite
schema and job journal. Workers were paused in both arms to isolate admission:
parse, validate, encode and persist the job, then return 202. The last job was
checked to remain pending. Inputs were generated from seed 719: 250 normalized,
five-decimal float32 vectors of dimension 1024, with text and metadata. The request
was 2,312,459 bytes.

Environment: x86_64 WSL2 Linux, 3-CPU quota, 4 GiB container limit, local container
storage, CPython 3.12.15, FastAPI 0.142.2, Pydantic 2.13.5, NumPy 2.5.3, turbovec
1.0.0 and SQLite 3.53.1; the repository's frozen lock file and native extension.
Baseline is the same application with the ingest route using ordinary `APIRoute`.

Medians across the five rounds:

| Metric | stdlib + model validation | native model parsing | Change |
|---|---:|---:|---:|
| Parse and validate (ms/request) | 22.53 | 12.24 | 45.7% lower |
| Durable admission p50 (ms/request) | 40.54 | 28.39 | 30.0% lower |
| Durable admission p95 (ms/request) | 58.11 | 35.66 | 38.6% lower |
| Process CPU per admission (ms/request) | 42.80 | 29.26 | 31.6% lower |

Admission p50 improved in every pair (baseline 37.83–47.40 ms, native
25.51–30.84 ms). Full per-round results and versions are in
`bench/results-ingest-json.json`.

The transport is in-process ASGI `TestClient`; CPU includes that client's work.
The p95 samples are small (20 per round) and are not a production tail-latency
claim. These results measure durable admission with workers paused, not drain
throughput, embedding latency, network transport or concurrent production load.
The large-corpus published benchmarks were not rerun for this change.

Reproduce with local temporary storage:

```bash
uv run --group bench python bench/ingest_json_probe.py --pairs 5 --iterations 20 --out results.json
```

## Consequences

Valid large ingest requests use less parser CPU and fewer intermediate objects.
Compatibility/error cases retain the old path and can cost an extra native parse.
The custom route assumes the endpoint has one unembedded `IngestIn` body; a future
change to that signature must update the adapter and parity tests together.
Upgrades to FastAPI/Pydantic should run these parity tests before adoption.

## Shared helper follow-up, 2026-11-05

[ADR 0016](0016-native-search-json.md) generalizes the existing adapter to the
route's single Pydantic body model and also uses it for search. The private classes
are now `_ValidatedJSONRequest` and `_ValidatedJSONRoute`; ingestion still validates
`IngestIn` and retains the same compatibility path. Its original request-contract
tests are kept and run alongside the new search parity tests. The measurements
above retain their original revision and are not new ingestion results.
