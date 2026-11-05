# Metadata BM25 keys: local evaluation

Date: 2026-11-05. Implementation: [ADR 0015](../adr/0015-metadata-fts-keys.md).
This capability is stacked on PR #13 (`a197726311296d77fd12d932be7a594a6c46c08a`).
Sofia deployment/title acceptance and remote benchmarks are a separate follow-up.

## Protocol and revisions

`bench/fts_keys_probe.py` compares that exact base source, current source with no
keys, and current source with `["title", "source", "tags"]`, in fresh subprocesses.
Each repetition reverses arm order. The seeded corpus has 1,200 documents, one
summary and six chunks each (8,400 records), 256-dimensional supplied vectors,
80-word bodies, file-like titles, SharePoint-like URLs and positional tags.
Queries cover summary and document-scoped text/hybrid retrieval, including the
two-stage and small-document paths. Body-query response digests must be identical
between base and current default. Opt-in ranking may change by design.

Environment: frozen `localhost/raggio-ingest-conversion:20261104` evaluation image,
managed CPython 3.12.15, SQLite 3.53.1, native BM25, Linux container on this Windows
host, 3 CPUs / 4 GiB limit, `OPENBLAS_NUM_THREADS=1`, local temporary storage.
Only source files are replaced; the frozen dependencies are unchanged. Version
0.3.0's updated lock separately passes `uv lock --check`.

Measured current LF-normalized source hashes:

- `store.py`: `cdc0dbc8591bcb37aad9e7978ce783269d3500343a0c8f98a928c2104fa719db`
- `app.py`: `b69848a655d92bc4e9a3e694e5be2d4f20ed7307ab5314fa6e593258d2efaa01`

Raw results retain both source hashes and per-arm/per-repetition values:
[initial three runs](https://github.com/EmanueleMeazzo/raggio/blob/feat/fts-keys/bench/results-fts-keys.json)
(100 queries per shape) and
[five repeated runs](https://github.com/EmanueleMeazzo/raggio/blob/feat/fts-keys/bench/results-fts-keys-repeat.json)
(500 queries per shape).

Search timing calls `Collection.search` with hydration and asyncio thread handoffs;
it excludes HTTP, network and embedding. CPU measures the process during each search.
Admission times an in-process ASGI TestClient request containing 175 records, with
workers paused, through durable journal creation and 202; its CPU includes the client.
Completed ingest measures `_process_job` including vector insertion and index sync,
with payload generation outside the clock. FTS size sums SQLite `dbstat` pages for
`records_fts_*`; database size follows a WAL checkpoint. RSS is the whole worker peak,
including generated payloads and admission, rather than a serving working set.

## Measured costs and quality

The table reports medians of each repetition's p50/p95, from the five-run repeat.
It does not pool samples or claim a deployment percentile.

| Query | Base p50 / p95 ms | Current default | Current with keys |
|---|---:|---:|---:|
| Text summaries | 0.497 / 1.009 | 0.502 / 0.971 | 0.479 / 0.907 |
| Text inside document | 0.294 / 0.580 | 0.308 / 0.560 | 0.356 / 0.686 |
| Hybrid summaries | 1.911 / 3.121 | 1.877 / 2.874 | 1.933 / 3.199 |
| Hybrid inside document | 0.938 / 1.466 | 1.117 / 1.736 | 0.915 / 1.607 |

These separate-process timings vary substantially across repetitions. For example,
default hybrid-document p50 spans 0.917–1.315 ms, and the initial run's median moves
in the opposite direction. They cannot establish a default-path regression or a
speedup. An interleaved default-method control is recorded below to isolate code
cost from changes in host scheduling/cache state.

FTS pages are identical in base/default: **2,400,256 bytes**. With keys they become
**2,973,696 bytes (+23.9%)**. Total checkpointed `meta.db` is likewise identical in
base/default: **16,941,056 bytes**, versus **20,283,392 bytes (+19.7%)** with keys.
Only the rendered metadata suffix adds record storage; the body is generated.
Additional postings are an unavoidable cost of indexing additional content.

Median completed ingest is 12,359 versus 10,431 records/s in the initial default/keys
comparison (keys **15.6% slower**), and 12,078 versus 10,529 in the repeat (**12.8%
slower**). Host variation affects individual rates; the cost is consistent at the
median level. Ingestion is already sufficiently fast for Sofia's infrequent migration.
Admission medians move in opposite directions between runs, as expected for an
unchanged admission path: selected-key conversion occurs in the worker. No admission
improvement or degradation is attributed to this feature. Peak worker RSS remains
around 118–120 MiB in the repeat; no serving-memory benefit is claimed.

All **20/20 synthetic title-only queries** find their document in the top three with
keys; base/default find **0/20**, because those tokens occur only in metadata. This
checks capability, not Sofia recall: the private-corpus title acceptance remains
required. The tests cover both hybrid fusions and both tokenizers independently.

## Default-path control and validation

The control loads the exact base's changed BM25 methods and alternates them with
current methods on the **same warmed collection**, using the same event loop,
vector implementation, reader/cache state and query order. It compares full returned
hits per pair and compares current default schema SQL against an actual base-created
database. This isolates the feature's default BM25 code changes; it is not a whole
deployment benchmark or a concurrent throughput measurement.

Three fresh control runs each execute 1,000 alternating pairs per shape. Raw data:
[run 1](https://github.com/EmanueleMeazzo/raggio/blob/feat/fts-keys/bench/results-fts-control-1.json),
[run 2](https://github.com/EmanueleMeazzo/raggio/blob/feat/fts-keys/bench/results-fts-control-2.json),
[run 3](https://github.com/EmanueleMeazzo/raggio/blob/feat/fts-keys/bench/results-fts-control-3.json).

| Query | Default p50 change, runs 1 / 2 / 3 | Default CPU change, runs 1 / 2 / 3 |
|---|---:|---:|
| Text summaries | −0.45% / +0.35% / −0.21% | −0.62% / −0.10% / −0.31% |
| Text inside document | −0.45% / +0.68% / +0.33% | −0.02% / +0.45% / −0.39% |
| Hybrid summaries | −0.79% / −0.84% / +0.35% | −1.08% / −0.04% / +2.93% |
| Hybrid inside document | +0.42% / +2.62% / −1.47% | −0.58% / −0.05% / −0.18% |

These controls show no repeatable default-path slowdown. They also explain why the
larger separate-process median differences should not be attributed to the code.
No new speed improvement is claimed. Every paired result and default schema SQL
comparison passes exactly.

Final local native suite: **824 passed, 4 expected skips**, including 63 feature
cases, with the updated pyproject/lock mounted (237.52 seconds on the busy host).
Native/Python scorer comparisons, external-content FTS integrity, PATCH rollback,
catalog migration, both-tokenizer restart/replay and IVF lifecycle all pass.
`uv lock --check` passes; documentation is built with MkDocs strict mode.

## Reproduction and next step

With the frozen native environment and base source mounted at `/baseline-src`:

```bash
uv run --group bench python bench/fts_keys_probe.py --queries 500 --rounds 5 --out results.json
uv run --group bench python bench/fts_keys_probe.py --arm default --paired-control --queries 1000 --out control.json
```

The selected design and rejected alternatives are documented in ADR 0015. The
Sofia agent should use the published 0.3.0 image, recreate document collections with
the three selected keys, then run title top-3 acceptance and its established latency,
CPU and working-set benchmark. This PR stays unmerged for that review; no application
deployment is performed here.
