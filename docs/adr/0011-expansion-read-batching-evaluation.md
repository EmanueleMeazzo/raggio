# ADR 0011 — Expansion read batching and sibling-vector reuse evaluation

Status: proposed; exploration only · Date: 2026-11-04 · Builds on ADRs 0007 and 0010

## Decision

Keep production retrieval unchanged during this investigation. Prioritize bounded,
request-scoped sibling-vector reuse as the next implementation candidate for ranked
expansion with repeated document hits: the follow-up prototype improved ten-hit
vector latency/CPU by 14.8%/13.9%, and hybrid latency/CPU by 10.5%/12.9% locally.
Keep original paths for unsupported cases and retain exact score/ordering behavior.

Batched expansion reads are a separate candidate for large `siblings_all` responses, where the prototype
reduced median endpoint latency and CPU by about 21%. The same change did not
consistently speed up common five/ten-hit ranked expansion, and did not lower the
measured peak retrieval allocations. Do not apply it broadly based on SELECT counts.

A search returning ten chunks of one document fetches sibling vectors ten times.
The second prototype fetches/normalizes eligible sibling vectors once within that
search, then retains the existing per-hit scoring and exact/quantized selection.
Both prototypes remain under `bench/`; neither is implemented in production.

## Context and prototype

Main `6327693` includes PR #9's search response optimization. `Collection.search`
holds a read lock across ranking, hydration and a sequential `_expand` call for each
hit. Each expansion separately identifies siblings and the summary, then hydrates
their rows. Several hits may belong to one document; `siblings_all` can return the
same immutable text many times.

`bench/expansion_read_probe.py` derives a candidate from the existing search function,
replacing only its expansion loop. A guarded source match prevents silently hooking
a changed function. The prototype remains inside the original collection read lock:

- Fetch summary rows for the distinct result documents in one query, or fetch their
  chunks and summaries together for `siblings_all`. Bound parameters in groups of 512.
- Materialize SQL rows inside the existing read-connection guard; no cursor or await
  escapes that guard. Sort siblings by SQLite's NULL-position-first, position/id order.
- Keep ranked sibling selection on the original `_expand` path, with its original
  cosine/BM25 scores, `indexed=1` restriction and per-hit self exclusion.
- Preserve `siblings_topk` precedence over `siblings_all`, unindexed chunks in
  `siblings_all`, summary-hit behavior, missing summaries and output key order.
- Decode fresh mutable metadata for every output occurrence. Share immutable text
  only within one request. No persistent cache or invalidation mechanism is added.

The prototype and compatibility probes live under `bench/`; `src/` is unchanged.
This is evidence for choosing the next implementation, rather than a shipped feature.

## Measurement protocol

Baseline and prototype use one application and the same two temporary collections.
Each contains 10,000 chunks plus 500 summaries, dimension 3072, four-bit indexing,
documents of 20 chunks, repeated Unicode text and nested metadata. Seed 821 generates
the data and queries. Clustered vectors use document centers plus noise; dispersed
vectors are independent, so the first diagnostic search has ten distinct documents.
The clustered ten-hit searches have one; its 100-hit diagnostic has fourteen.

Each of eleven cases has three warmups per arm, then five alternating paired rounds
of twenty requests per arm. Queries vary over twenty document centers; text/hybrid
requests use their topic token. JSON vectors are pre-encoded to five decimals.
Hybrid supplies both text and vector, avoiding external embedding calls. Every paired
endpoint response is compared byte for byte. Patch machinery is outside both clocks.

SQL trace and allocation measurements are separate from timings. The SQL count is
for one warm store search on the first query, excluding FTS5 internal trace statements.
`tracemalloc` measures one complete store search and its returned hits, excluding the
pre-existing collection/index and earlier results. It is a temporary Python allocation
peak, not process RSS, retained index size or production memory usage.

Environment: x86_64 WSL2 Linux, local container storage, three-CPU quota, 4 GiB limit,
`OPENBLAS_NUM_THREADS=1`, CPython 3.12.15, SQLite 3.53.1, NumPy 2.5.3, turbovec 1.0.0,
FastAPI 0.142.2, Pydantic 2.13.5, frozen dependencies and the native BM25 extension.
These in-process ASGI TestClient measurements include client work and do not measure
network, external embeddings, DGX/NFS performance or production tail latency.

Raw measurements: `bench/results-expansion-reads.json`, including rounds, environment,
source hash, SQL categories, distinct-document counts and allocation peaks.

## Results

Medians over five rounds; milliseconds per request. Negative reductions mean slower medians.

| Case | Latency baseline | Latency prototype | Latency reduction | CPU reduction |
|---|---:|---:|---:|---:|
| `clustered-k5-ranked` | 16.86 | 17.77 | -5.4% | -3.6% |
| `clustered-k10-ranked` | 17.99 | 19.15 | -6.4% | -2.2% |
| `clustered-k10-summary` | 12.20 | 12.37 | -1.4% | 6.4% |
| `clustered-k10-all` | 12.23 | 11.94 | 2.4% | 3.8% |
| `clustered-k100-all` | 49.60 | 39.19 | 21.0% | 21.2% |
| `dispersed-k10-ranked` | 22.19 | 21.33 | 3.9% | -0.2% |
| `dispersed-k10-all` | 17.52 | 16.48 | 5.9% | 1.4% |
| `summary-hits-all` | 14.64 | 13.78 | 5.9% | 3.4% |
| `text-k10-ranked` | 13.97 | 13.59 | 2.7% | 4.4% |
| `hybrid-k10-ranked` | 25.71 | 25.84 | -0.5% | 2.1% |
| `clustered-k10-control` | 9.39 | 9.65 | -2.8% | 1.8% |

The no-expansion control differs by 2.8% despite running the same search path. Small
changes overlap host/run variation; SELECT reductions alone are not evidence of a
useful latency gain. The large `siblings_all` response is the useful signal.

At ten clustered hits with all siblings, SELECTs fall from 42 to 3; at 100 hits,
from 402 to 3. The latter temporary store allocation peaks are 6,305,097 vs
6,305,345 bytes. There is no measured peak-allocation benefit. Vector rescoring
temporaries remain in both arms; reducing expansion reads does not remove them.

For ten clustered ranked hits, SELECTs fall from 52 to 33, but vector-blob reads
remain eleven: one main rescore and ten sibling searches. The clustered ranked
five/ten-hit medians actually regress here, while dispersed/text results vary.
Keep the simple production path for these cases until a stronger profile supports
another change. Summary-only batching also fails to improve median latency here.

## Follow-up: bounded sibling-vector reuse

`bench/expansion_vector_probe.py` adds a third arm using the same fixture and paired
protocol. The original production `_expand` loop still selects/excludes each hit's
siblings and hydrates the same fields. Only its vector fetch/decode can reuse data:

- Require a vector query, ranked siblings and multiple hits from a document.
- Count indexed chunks before fetching IDs. Cache only complete documents no larger
  than the existing exact-search depth; choose higher-reuse documents first within an
  8 MiB float32 payload budget. Large documents keep the original path.
- Fetch and normalize eligible fp16 vectors with the existing routine once. Cache
  only documents whose vectors are all present and valid. Legacy rows keep their
  original quantized fallback.
- Select a fresh float32 matrix in the same per-hit rowid order for the original
  scoring operation; do not reuse ranked results or change candidate selection.
- Keep cache state local to the search, propagated to its worker calls and reset in
  `finally`. There is no cross-request cache or write-invalidation mechanism.

Raw follow-up results are in `bench/results-expansion-vectors.json`. All three arms
compare exact endpoint bytes. The `vectors` arm is the new candidate; `prototype`
still denotes summary/all-row batching. Percentages below use this follow-up run's
baseline, rather than mixing results from separate runs.

| Case | Latency baseline (ms) | Vector reuse (ms) | Latency reduction | CPU reduction |
|---|---:|---:|---:|---:|
| `clustered-k5-ranked` | 16.39 | 15.38 | 6.2% | 9.4% |
| `clustered-k10-ranked` | 20.29 | 17.30 | 14.8% | 13.9% |
| `clustered-k10-summary` | 10.46 | 10.34 | 1.1% | -0.1% |
| `clustered-k10-all` | 11.86 | 12.06 | -1.7% | 13.4% |
| `clustered-k100-all` | 39.54 | 40.27 | -1.9% | 3.2% |
| `dispersed-k10-ranked` | 21.33 | 21.05 | 1.3% | 0.3% |
| `dispersed-k10-all` | 14.03 | 14.26 | -1.7% | 3.0% |
| `summary-hits-all` | 12.60 | 12.80 | -1.6% | -1.6% |
| `text-k10-ranked` | 9.81 | 10.39 | -5.9% | -1.3% |
| `hybrid-k10-ranked` | 20.74 | 18.57 | 10.5% | 12.9% |
| `clustered-k10-control` | 8.14 | 8.39 | -3.0% | -3.9% |

Ten repeated-document ranked hits reduce vector-blob SELECTs from 11 to 2 (one main
rescore plus one sibling prefetch). Total SELECTs fall from 52 to 45 because two
eligibility/ID queries are added and hydration/summary reads remain unchanged.
The allocation peaks are 1,668,340 vs 1,668,372 bytes; hybrid peaks are 6,311,046 vs
6,312,054. There is no measured peak-allocation improvement. Cached vectors trade
some request-local storage for less repeated work.

Dispersed document hits, text, summary-only and all-sibling searches do not use the
vector cache. Their variations and the 3% no-expansion control difference caution
against promising a general speedup. The five-hit gain is smaller than the ten-hit
signal. These single-client results do not establish production concurrency gains.

## Compatibility checks and implementation gates

`bench/expansion_parity_probe.py` passes 332 combinations for each prototype, including
80 valid-vector cases before legacy mutations, then 252 combinations of vector/text/hybrid,
chunk/summary/both scopes, document filters including no matches, and expansion
options. Exact serialized results match with missing and invalid retained vectors,
an unindexed chunk, NULL/duplicate positions, missing summaries and summary-only
documents. Additional checks cover independent nested metadata, ten concurrent
readers in each of the valid/legacy states, and freshness after metadata writes.

These checks support the prototype; they do not replace production regression tests.
Before implementing a narrower large-response path, run existing lifecycle/cancellation
and mixed-write stress tests, flat/IVF and tokenizer variants, large documents and
parameter-boundary cases. Measure batch-prefetch memory on larger documents and keep
reads inside the collection lock/connection guard. Actual workload frequency should
justify extra code; common small ranked searches provide no compelling gain from
summary/all-row batching alone.

For vector reuse, preserve score bits on ARM/IVF and across matrix shapes, bit widths,
document sizes, rescore-depth and parameter boundaries. Test cache-budget exhaustion
and simultaneous writes/cancellation. The prototype's initial prefetch/decode is
synchronous; a production implementation should move that preparation to a worker
while retaining the collection read lock, then measure concurrent request latency.
The 8 MiB limit bounds retained float32 payload, not decoding temporaries or RSS.

```bash
uv run --group bench --extra native python bench/expansion_read_probe.py --baseline-revision 6327693 --pairs 5 --iterations 20 --out results.json
uv run --group bench --extra native python bench/expansion_parity_probe.py
uv run --group bench --extra native python bench/expansion_read_probe.py --vector-reuse --baseline-revision 6327693 --pairs 5 --iterations 20 --out vectors.json
uv run --group bench --extra native python bench/expansion_parity_probe.py --vector-reuse
```
