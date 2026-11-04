# Rust rewrite evaluation — raggio

> Archived baseline assessment: these measurements describe `da432a6`, before
> the native ingest parser and vector conversion changes. Subsequent decisions
> and implementations are recorded in the [ADR index](../adr/index.md).
> Retrieval is the optimization priority from 2026-11-04 onward; the original
> suggested ingest work below has been completed and is no longer the priority.


Evaluated 2026-11-04 at `da432a6cf01fafeddd1bbd4da6c4a73b67acae37`.

**Recommendation: retain the current Python/Rust architecture and pursue measured hot paths.**
A production Rust service that preserves the current behavior is roughly **12–20 engineer-weeks** for someone experienced in Rust, asynchronous services and SQLite. The strongest immediate opportunity is ingest parsing and allocation; the evidence does not support a several-fold improvement in large-corpus search merely by changing the service language.

This is an engineering estimate, not a measured full-service Rust comparison. No service rewrite was made. At evaluation time, all experiments and this report were outside the repository, in `C:/DevOps/raggio-rust-evaluation-20261104/`. The project's working tree remained clean.

## Why the potential gain is bounded

The project has 3,492 lines of Python runtime code, of which 3,027 are in `store.py`, plus 303 lines of its own Rust scorer. It has 9,740 lines of tests and 383 test functions before parametrization. The API is small, but the storage and lifecycle semantics are substantial.

The computational core is already native: turbovec is Rust; stage-2 BM25 is Rust in the default container; numeric matrix operations use NumPy; FTS5 and the durable journal use SQLite. A rewrite should reuse these engines. It would primarily remove Python objects, request processing, thread handoffs and orchestration, rather than replace slow interpreted vector math.

Existing repository evidence already records the large BM25 win: its Rust scorer reduced stage-2 time from 31.5 to 1.5 ms on the DGX. That benefit is already present and must not be counted again. The current large-corpus text leg still spends significant time in FTS5; changing the caller to Rust leaves that work and SQLite's locks in place.

## New experiments

519 tests passed, 4 skipped, zero failures/errors, in a clean Linux container with the native extension required. The four skips need a free-threaded interpreter. The first Windows run had environment failures: a stale editable install, Windows/WSL Bash paths and an older interpreter. The clean container used the checkout's frozen lock file and passed the suite; no project changes were needed.

The probes used CPython 3.12.15, Rust 1.99.0, turbovec 1.0.0, NumPy 2.5.3 and SQLite 3.53.1, on x86_64 WSL2 Linux, with a 3-CPU quota, 4 GiB limit and one OpenBLAS thread. There were five paired runs, using 20,000 synthetic 1024-dimensional vectors. HTTP payloads follow `bench/bench.py`: five-decimal float32 vectors serialized with orjson. The ingest request was 2,310,618 bytes; the search request 8,630 bytes.

These are component timings in milliseconds, with medians across paired runs:

| Component | Current Python | Small Rust probe | Python/Rust ratio |
|---|---:|---:|---:|
| Quantized engine search, 20k vectors, k=50 | 1.975 | 1.611 | 1.23x |
| Parse one 1024-dimension search request | 0.067 | 0.022 | 3.01x |
| Parse and validate a 250-vector ingest request | 23.088 | 5.998 | 3.85x |
| Decode, normalize and rescore 200 fp16 vectors | 0.374 | 0.347 | 1.08x |
| Serialize a 10-hit response | 0.021 | 0.004 | 5.86x |

The engine timings have wide overlapping ranges and change direction between pairs. They show **no clear engine improvement**. Both callers execute the same Rust engine; local compilation and CPU scheduling can affect the numbers. The 20k size deliberately stays below turbovec's 32,768-row pooled-search threshold, so this does not establish how a direct Rust caller would behave on a multi-million-vector collection.

Ingest parsing saves approximately 17.1 ms per 250-vector request in this workload. Search parsing saves approximately 0.044 ms per request. Serialization has a large relative ratio but saves only a few hundredths of a millisecond for this small response. Large responses and requests could benefit more.

The Rust parser handles representative valid requests, with relevant dimension/range checks. It does not implement the complete FastAPI/Pydantic error contract, authentication, limits or every optional field. Its times are an estimate of parser potential, not a validated API replacement.

The Rust rescorer matched the tested top-10 order with a maximum score difference of about 1.12e-7. It is not bit-identical to NumPy and was only checked on this synthetic case. Production ranking and numeric compatibility would need broader tests. The timings exclude SQLite retrieval and API work.

The probe also demonstrated both file directions using existing upstream code: Python wrote a `.tvim` file that Rust loaded; Rust removed an ID, inserted a vector, synced, and Python loaded the result. Quantized IDs and float32 scores matched exactly for the checked queries. This proves a useful reuse path, not complete migration compatibility across formats, versions or crash states.

## Improvements available while retaining Python

`IngestIn.model_validate_json` took **13.7 ms** per ingest request, against **23.1 ms** for the current stdlib JSON plus model-validation path. It uses Pydantic's existing native parser. The same direct path parsed a search request in **0.026 ms**.

orjson plus model validation took 13.4 ms per ingest request. orjson is currently bench-only by repository policy, so this would require a dependency/packaging decision. Direct Pydantic JSON parsing does not add that dependency, but integrating it with FastAPI still requires preserving validation errors and the OpenAPI contract. No route was changed.

These results support a small request-path experiment before a rewrite. Direct Pydantic parsing captures a substantial part of the measured ingest saving and much of the query-parser saving. Binary/vector-oriented input could remove more allocations, but changing the public protocol is a separate product decision.

## Storage, memory and concurrency

A current-service profile on local Linux container storage produced:

| Current path, 20k vectors | p50 ms | p95 ms |
|---|---:|---:|
| Unfiltered vector | 3.03 | 5.96 |
| Unfiltered hybrid | 4.46 | 7.01 |
| One-document vector | 0.27 | 0.42 |
| One-document hybrid | 0.80 | 1.50 |

These measurements exclude HTTP and embeddings. The combined engine/rescore call averaged 2.73 ms; fetching/decoding/normalizing 50 retained vectors averaged 0.122 ms; hydrating 10 hits averaged 0.023 ms. Separate measurements are not an additive latency accounting. The profile confirms that native search dominates and that Python/worker coordination remains a possible smaller target. Windows bind-mount storage measurements were abandoned because their I/O cost distorted the experiment.

In separate minimal processes, importing the Python application and searching the same 20k index used 76.2 MiB RSS, versus 13.9 MiB for a Rust process that only loaded/searched the index. The approximately 62.2 MiB difference mostly reflects fixed runtime/import cost. **The Rust process has no HTTP server, SQLite service, BM25 scorer, collection manager or workers.** It is an illustration of overhead, not a measured final-service memory saving. The index's incremental memory was nearly the same in both processes.

This fixed cost matters for tiny collections and many deployed instances. It is a small fraction of the roughly 1.5 GB footprint in the repository's large-corpus benchmark. The stored vectors, fp16 copies and FTS5 data would retain their current sizes. Warm index load/prepare was about 220 ms in Python and 219 ms in Rust; changing the language did not remove that work. No full cold-start comparison was made.

For concurrency, Rust removes the GIL and can avoid some Python thread coordination, but the existing native operations already release it. ADR 0006's historical DGX ABAB is more relevant than a language slogan: concurrent IVF vector QPS improved roughly 23–25% on free-threaded Python at concurrency 8, while hybrid improved about 10–15%; serial large-corpus timings mostly stayed similar. IVF vector QPS improved about 26–29% at concurrency 16. Those runs also changed interpreter/build details, and flat throughput had unmatched fast/slow states, so they are directional evidence, not a forecast for Rust. SQLite locking/allocation contention, the search engine's thread pools and memory bandwidth remain.

As of this evaluation, the latest published turbovec is 1.1.2; no cp313t/cp314t wheels were listed on PyPI. Existing repository constraints on free-threaded production images still need consideration. This evaluation pinned 1.0.0 and did not conflate an upstream upgrade with a rewrite.

## Effort estimate

Assume the same REST API, SQLite/FTS5 layout, `.tvim` formats, embedding endpoints and collection behavior, with turbovec reused rather than forked. One engineer-week means five focused working days.

| Work | Engineer-weeks |
|---|---:|
| REST models, validation/error parity, auth, settings, embeddings | 1–2 |
| SQLite schema, binary/legacy journal, durable worker and recovery | 3–4 |
| Search orchestration, filters, fusion, expansion, scorer adaptation | 1–2 |
| IVF lifecycle, online swaps, LRU/idle eviction, shutdown/cancellation | 2–3 |
| Cross-implementation regression, migration, concurrency and failure tests | 2–3 |
| Packaging, CI, deployment/readiness behavior and operational checks | 0.5–1 |
| Base implementation and validation | 9.5–15 |
| Planning allowance for integration and newly exposed edge cases | approximately 25–35% |
| **Production-ready total, rounded** | **12–20** |

That is about three to five months for one experienced engineer. Two engineers can shorten elapsed time, but storage ownership, migration and integration remain coupled. A useful narrow Rust prototype would take roughly 2–4 engineer-weeks; it would not establish production equivalence. A focused hot-path extension or parsing improvement is roughly 1–3 weeks, depending on the API and packaging changes involved. Reimplementing turbovec itself is outside this estimate and would add substantial work and risk.

The expensive contracts are: a durable 202 admission; jobs done only after index sync; replay after crashes; failure-safe replacement of records/vectors; crash-safe online IVF attach/detach; collection deletion/eviction while searches or cancelled worker threads still exist; reader connection draining; single-process data-dir handover and POSIX `unix-excl`; legacy payload/index compatibility; Unicode folding and bit-exact BM25 arithmetic; and existing filter/ranking semantics. Many tests inspect Python internals or monkeypatch collaborators, so the test suite cannot simply be pointed at a Rust binary. Behavior tests must be extracted and run against both implementations.

Rust would improve static checking and ownership clarity and simplify interpreter/native-wheel deployment. Python is already memory-safe, and Rust's type system does not prove database durability, correct rankings or cancellation behavior. A clearer architecture is achievable in Python as well. The transition would temporarily maintain two implementations and slow unrelated feature delivery.

## Expected return and decision

| Area | Assessment |
|---|---|
| Large unfiltered vector latency | Low to moderate benefit; the engine is already Rust. No several-fold gain is supported. |
| Hybrid latency | Low unless the rewrite also changes the SQLite/text-search design; that is an additional architecture change. |
| Precomputed-vector ingest | Strongest measured CPU opportunity: parser/allocation reduction. Whole ingest throughput remains unmeasured. |
| Embedding-backed requests | Language change leaves external embedding latency and cost in place. |
| Concurrent IVF and small/scoped queries | Plausible moderate benefit from less coordination; requires full-service load testing. |
| Small-instance memory and packaging | Useful fixed-overhead reduction; modest effect on large resident indexes. |
| Storage size and recall | No intrinsic improvement from changing the service language. |

For budgeting, I would assume only a modest large-corpus improvement until demonstrated: **roughly 0–25% lower search latency and potentially tens of percent more throughput**, with low confidence and workload dependence. These are planning assumptions, not Rust service results. A 2–10x general search gain is not justified by this evidence. Removing an additional SQLite or engine bottleneck could change the result, but should be evaluated as its own intervention.

Component speedups must be weighted by their share of the request. For example, a 4x faster parser gives only 1.18x total throughput if parsing is 20% of the original cost, or 1.43x if it is 40%, assuming the rest and concurrency stay unchanged. The local parser times cannot be directly substituted into the DGX benchmark's wall time.

**Next investment:** spend 1–3 weeks on direct native request parsing and a tightly scoped ingestion/rescore experiment. Compare CPU per accepted request, completed ingest throughput, concurrent p95/p99 and memory on representative data, with the same engine, SQLite build, CPU limits and storage. Keep crash/replay and ranking tests as gates. A full rewrite becomes attractive if these experiments show a broad remaining Python bottleneck, if tiny-instance memory is a central requirement, or if operating a single Rust service is a strategic goal worth three to five months independently of search speed.

Suggested decision gates for a rewrite pilot are at least a 30% improvement in an actual primary workload's CPU/throughput or a material deployment-footprint reduction, with preserved quality, durability and tail latency. These are proposed business thresholds, not requirements already agreed with the project.

## Evidence and reproduction

Local evidence: `microbench-results.json`, `full-precision-microbench-results.json` (earlier longer decimal payloads), `compatibility-results.json`, `current-path-results.json`, `memory-results.json`, `vector-orchestration-profile.txt`, `linux-tests.log`, `linux-tests.xml`, `probe/Cargo.lock`, `probe/src/main.rs`, `upstream-facts.json` and the checked PyPI sdist.

Repository evidence: `README.md`, `docs/benchmark-arxiv-2026-10.md`, `docs/adr/0004-native-bm25.md`, `docs/adr/0005-ivf-fanout-and-upstream-stance.md`, `docs/adr/0006-free-threaded-python.md`, `docs/adr/0007-sofia-backend-requirements.md`, and `src/raggio/store.py`. Historical DGX measurements were read from the checkout; the 2.55M corpus was not rerun here.

From PowerShell, using the evaluation folder as the image context:

```powershell
rtk podman build -t localhost/raggio-rust-evaluation:20261104 C:\DevOps\raggio-rust-evaluation-20261104
rtk podman run --rm --cpus 3 --memory 4g -v C:\DevOps\raggio-rust-evaluation-20261104:/eval localhost/raggio-rust-evaluation:20261104 python /eval/run_tests.py
rtk podman run --rm --cpus 3 --memory 4g -v C:\DevOps\raggio-rust-evaluation-20261104:/eval localhost/raggio-rust-evaluation:20261104 python /eval/probe.py
rtk podman run --rm --cpus 3 --memory 4g -v C:\DevOps\raggio-rust-evaluation-20261104:/eval localhost/raggio-rust-evaluation:20261104 python /eval/memory_probe.py
```

The default probe builds with the saved Cargo lock file. `SKIP_RUST_BUILD=1` is only for rerunning an already built, unchanged probe. Run performance experiments after the suite/build jobs finish. The retained `project/` folder is the evaluated source snapshot; `prepare.py` refreshes it from the live checkout and should only be used deliberately.
