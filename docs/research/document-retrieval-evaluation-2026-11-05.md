# Document retrieval evaluation, 2026-11-05

Baseline: main `daaab6d27b9f9aceadd86b8a82be0e53d9fb50d0`, including PRs #11 and #12.
This is a local investigation, not a new Sofia deployment benchmark.

## Adoption rule

The user accepted a repeatable improvement of at least 10%, or a 1–9% improvement
when the implementation also becomes simple and clearer. Results, score bits and
ordering must remain compatible; an improvement in one case does not excuse
material regressions or unbounded retained memory elsewhere.

## Workload and comparison

Sofia's local client source (`src/vector_store.py` and
`src/tools/weaviate_rag.py` in the Sofia backend) supplies an embedding vector with
hybrid requests, uses relative fusion with alpha 0.5, filters chunks by `doc_id`,
and searches summaries before drilling into documents. Actual request traces,
production document reuse rates and a private corpus were not available.

`bench/document_retrieval_probe.py` generates 320 documents, each with a 300-word
summary and 3–10 chunks of 1,000 generated words, dimension 3072 and four-bit codes.
There are 2,080 chunks and 320 summaries. Metadata includes title, source, tags and
nested Unicode/scalar values. Data/query seeds are 1105/1106. The fixture uses the
flat index; it does not reproduce Sofia's 22k-summary IVF index or its vocabulary.
Summary timings are a control for this fixture, not a forecast for deployed summary
latency. Ingestion only builds the untimed fixture and is not an optimization target.

The frozen native CPython 3.12 image runs with three CPUs, a 4 GiB memory limit,
local temporary storage and `OPENBLAS_NUM_THREADS=1`. A separate spawned uvicorn
process serves loopback HTTP. Client requests are pre-encoded outside the timers;
there are no embeddings or external network calls. Server process CPU is sampled
around timed blocks, so CPU/query excludes client work. Sampling includes a small
amortized control-request cost in every arm. Client and server share the local
container CPU quota; loopback throughput is not a deployment-capacity forecast.

Primary cases are hybrid chunks, k=5, with either eight recurring documents or
240 distinct documents beyond the prototype's cache capacity. Vector, text and
summary searches are controls. Each final comparison has seven paired rounds of
240 timed requests per arm at 1/8/16 clients, with equivalent untimed warmup.
Arms alternate in 32-request blocks; the first arm rotates by block and round and
the ordering reverses on alternate rounds. A third unchanged-code arm estimates
local drift. Both reference arms bypass native search parsing while keeping the
same route and benchmark instrumentation. All arms use the same resident corpus,
index and requests, and every response byte must match.

Full-precision float32 values are serialized as ordinary JSON numbers in the
primary run. A shorter five-decimal payload is evaluated separately. Ratios are
calculated per paired round before taking their median; stage timings are nested
diagnostics outside endpoint timers and cannot be added to reconstruct latency.
Raw samples retain source hashes, versions, request sizes, RSS snapshots and cache
counters. RSS snapshots are not a peak-memory measurement.

## Prepared-vector cache: rejected

The initial prototype retains normalized float32 matrices for small allowlists,
with 32 entries and an 8 MiB matrix budget. It is benchmark-only, not a complete
production invalidation/lifecycle design. It caches vectors, never query results.

The exploratory three-round run (`bench/results-document-cache-pilot.json`) helped
repeated vector searches under concurrency, but its cold and wider hybrid cases
regressed and hybrid gains did not persist at 16 clients. Wider access retained
roughly 2.5 MiB while recording no hits. Text/summary control timings also moved
substantially despite unchanged paths. Those short, whole-round comparisons are
not adequate evidence for adoption, and their positive ratios must not be treated
as deployment gains. No production vector cache is added.

## Search JSON parsing

The diagnostic profile found material CPU in decoding vector-bearing search JSON,
while the current ingress route already uses Pydantic's native parser. The search
candidate generalizes that existing request/route helper to the route's single
Pydantic body model and also applies it to `/collections/{name}/search`.

Valid JSON is parsed and validated once into `SearchIn`, which FastAPI can reuse.
On native validation failure the original request's JSON method preserves ordinary
encodings, lone surrogates, validation details and dependency ordering. Existing
ingest behavior is retained through the same shared helper. The public schema,
ranking, filters, storage and dependencies do not change.

The exploratory parser run (`bench/results-document-parse-pilot.json`) used
whole-round arm ordering and five-decimal vectors. Its noisy controls and inconsistent
cold single-client result motivated the final block-alternating comparison; it is
not the source of an adoption claim.

## Final measurements and decision

The shared native parser **passes the adoption gate**. The controlled full-vector
run improves primary hybrid p50 by 11.7–16.9% and CPU/query by 10.2–16.9%
across 1/8/16 clients. Every primary hybrid CPU round improves (42/42).
The fresh-server single-client repeat confirms roughly 17% lower p50 and CPU/query
for both recurring and distinct documents. Its CPU rounds also all improve (14/14).

These are medians of per-round paired changes, not pooled-median ratios:

| Query | Clients | p50 | p95 | QPS | CPU/query |
|---|---:|---:|---:|---:|---:|
| hybrid-hot | 1 | -13.9% | -11.1% | +16.1% | -16.0% |
| hybrid-hot | 8 | -14.4% | -23.1% | +17.9% | -12.4% |
| hybrid-hot | 16 | -11.7% | -15.9% | +12.6% | -10.2% |
| hybrid-cold | 1 | -15.9% | -15.5% | +17.9% | -16.9% |
| hybrid-cold | 8 | -16.9% | -18.9% | +21.1% | -15.5% |
| hybrid-cold | 16 | -14.8% | -20.9% | +17.1% | -10.7% |
| text-hot | 1 | +0.3% | +1.2% | -0.0% | +0.1% |
| text-hot | 8 | +1.5% | +4.3% | -2.4% | +2.9% |
| text-hot | 16 | -0.1% | +3.5% | +0.9% | -0.9% |
| vector-hot | 1 | -18.8% | -10.5% | +19.8% | -19.7% |
| vector-hot | 8 | -20.6% | -15.8% | +22.9% | -18.5% |
| vector-hot | 16 | -21.7% | -19.4% | +22.3% | -18.5% |
| summary | 1 | -3.4% | -4.1% | +6.7% | -5.6% |
| summary | 8 | -8.2% | -11.8% | +9.3% | -6.8% |
| summary | 16 | -7.7% | -8.3% | +6.2% | -3.8% |

Primary unchanged-code control median CPU changes range from -1.8% to +1.9%,
smaller than the candidate gains. Individual rounds still contain drift and
tail outliers; no improvement is promised for every request. Text-only changes
are small and inconsistent. Summary work is a small generated flat-index control;
its 3.4–8.2% p50 changes do not predict Sofia summary latency.

Independent fresh-container checks, each with seven paired rounds at one client:

| Payload / case | p50 | p95 | QPS | CPU/query | Reference-control CPU |
|---|---:|---:|---:|---:|---:|
| Full precision repeat / hybrid-hot-c1 | -17.0% | -18.2% | +20.2% | -18.0% | -0.8% |
| Full precision repeat / hybrid-cold-c1 | -17.0% | -14.7% | +20.0% | -17.2% | +1.5% |
| Five-decimal check / hybrid-hot-c1 | -15.6% | -19.0% | +23.7% | -19.4% | +1.2% |
| Five-decimal check / hybrid-cold-c1 | -16.1% | -19.7% | +20.1% | -19.0% | +1.0% |

Full-vector document requests are approximately 65.8–66.0 kB; text-only requests
are 173 bytes. The independent shorter-payload check also passes the speed gate.
The native parser is adopted because its benefit repeats without depending on
document cache hits, and its implementation reuses a shared helper. No persistent
cache is added. RSS snapshots are retained as diagnostics, but do not establish a
controlled footprint reduction or peak-memory saving.

Baseline diagnostic profile, twenty warm hybrid document requests. Times are
nested and include instrumentation; they are not additive endpoint accounting:

| Stage | p50 wall (ms) | Mean thread CPU (ms) |
|---|---:|---:|
| `request_json` | 0.621 | 0.631 |
| `allowed_search` | 0.752 | 0.510 |
| `_fp16_vecs` | 0.573 | 0.397 |
| `_text_ids` | 1.004 | 0.738 |
| `_bm25_rescore` | 0.315 | 0.310 |
| `_hydrate` | 0.102 | 0.124 |
| `response_encode` | 0.170 | 0.168 |
| `worker_queue` | 0.162 | - |

Frozen versions match the repository lock: FastAPI 0.142.2, Pydantic 2.13.5,
NumPy 2.5.3, turbovec 1.0.0 and uvicorn 0.54.0. The raw results retain the exact
interpreter, SQLite version and platform. Measured source hashes:

- Store: `969e7648b951f01ab34f33a5c9c81bfe068938b8025ab2c499f9f0e632202cc1` (baseline store unchanged).
- Candidate API: `e6588224a774ef0eb411380e04d42225eda1d386226fb8a21803a0cd2155fc45`.

The measured Windows worktree files use CRLF line endings; Git commits normalize
them to LF. Their canonical LF fingerprints are store
`cdd7b70fe021e8c1938095659fcc0dfbb224d53b026896cde19d047170c78b08`
and candidate API
`87cf11faa70d2f7f6f9e7b9db22a7fce23be02550edeab944bdaab4ec4271abe`.
The retained raw runs report the actual mounted bytes. New probe outputs also
record LF-normalized hashes so Linux checkouts can be compared without treating
line-ending normalization as a runtime change.

Raw evidence under `bench/`: `results-document-parse-full.json`,
`results-document-parse-repeat.json` and `results-document-parse-short.json`.
The two pilot files remain exploratory evidence with their original whole-round
protocol; they are not inputs to the final tables.

Reproduce the final comparison in a native Linux environment with the same
quota, local storage and BLAS setting:

```bash
rtk proxy uv run --group bench --extra native python bench/document_retrieval_probe.py --candidate parse --with-control --precision full --docs 320 --requests 240 --pairs 7 --block 32 --cases hybrid-hot hybrid-cold text-hot vector-hot summary --out results.json
```

Use `--cases hybrid-hot hybrid-cold text-hot --concurrency 1` for the fresh-server
repeat, and `--precision five --cases hybrid-hot hybrid-cold --concurrency 1`
for the shorter-payload check. The probe captures measured source hashes at import.
It exposes its control endpoint only in the separately spawned loopback benchmark
server; production routes are unaffected by that instrumentation.

The next deployment check should repeat Sofia's fixed requests on the candidate
and baseline with equivalent restart/cache history, retaining results and CPU/query.
This local adoption does not replace the labeled PR #11 Sofia baseline.

## Compatibility validation

The focused native Linux run passed 212 tests across `test_search_json.py`,
`test_ingest_json.py` and `test_api.py`. It checks valid/invalid requests, authentication
precedence, malformed JSON, UTF-16/UTF-32 and BOM fallback, lone surrogates, duplicate
keys, integer limits, content types, unchanged OpenAPI, cached bodies/models and
10,000 random float-bit comparisons, as well as existing endpoint/filter behavior.
The complete native Linux suite passed **761 tests, 4 skipped**, in 64.76 seconds;
JUnit evidence is retained locally in `C:/DevOps/raggio-document-analysis-20261105/tests.xml`.
The strict MkDocs build passed. All five pull-request CI jobs are required before
merge: production/reference, native x86_64/ARM64 and experimental free-threaded
x86_64/ARM64 with extended concurrency stress. Their exact head and results are
recorded in the pull request; this change does not promote the experimental interpreter.
