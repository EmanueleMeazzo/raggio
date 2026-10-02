# ADR 0001 — Performance optimization decisions (552k-vector benchmark rounds)

Status: accepted · Date: 2026-08-22

Every path below was direction-tested on the real benchmark corpus (552,515
email embeddings × 1536 dims, 1 GiB container) before being adopted or
rejected. Full results: `bench/results.md`; docs page: `docs/benchmark.md`.
Probe tooling referenced here is committed under `bench/`.

## Round 1 — search latency (390.7 → ~34 ms vector p50)

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| Skip the allowlist when the scope excludes nothing | **Accepted** | Every default-scope query built a 553k-id allowlist (244 ms SQL + 9.5 ms convert) and forced a ~2x slower masked scan — to exclude zero rows. Gated on in-memory per-type counts (`bench/microbench.py`) |
| BM25 token pruning, cumulative doc-frequency budget (rarest-first, 2% of corpus, 1000-row floor) | **Accepted** | Constant tokens ("emails", df 538k) made FTS5 score the whole corpus (540–670 ms/query). Budget swept over 0.02/0.03/0.05/0.08: all 0 fused misses, 0.02 fastest |
| BM25 pruning via hard per-token df cap (drop df > 1%) | **Rejected** | Caused the only hybrid text-hit miss (0.998): dropped 4 of 5 mid-frequency meaningful tokens on one query. Replaced by the cumulative budget above |
| Bare-FTS fast path (skip the records join when nothing is excluded) | **Accepted** | ~40% of the pruned text-leg cost |
| Hybrid legs run concurrently (`asyncio.gather`) | **Accepted** | SQLite releases the GIL; legs overlap fully |
| Readers-writer lock + micro-batched scans (concurrent full scans stack into one kernel call) | **Accepted** | Store-level probe: 347 qps at 32 in-flight vs 3 fully serialized; batched kernel 3.2 vs 11.9 ms/query |
| Allowlist cache (FIFO 8) + `prepare()` at load | **Accepted** | Warm filtered p50 5.6 ms; first query no longer pays one-time kernel init |
| Per-thread read-only SQLite connections | **Accepted (correctness)** | Concurrent readers on one shared connection raise SQLITE_MISUSE under the new RW lock |

## Round 2 — "give it all" (34.5 → 32.8 ms vector, 48.3 → 41.3 ms hybrid, recall 0.960 → 0.969)

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| TQ+ calibration (turbovec `calibrate()`, one-shot at 10k vectors from a reservoir sample) | **Accepted** | recall@10 0.960 → 0.969 for free. `bench/cal_probe.py`: calibrate-once-at-threshold beats ideal uniform calibration; milestone refits LOSE recall (re-encode is a second quantization); late calibration of a fully-ingested index is WORSE than staying uncalibrated (0.9596 vs 0.9606) — hence calibrate-early-or-never |
| df-lookup cache for BM25 pruning (folded token → doc frequency, churn-based invalidation) | **Accepted** | The `fts5vocab` df lookup walks the term's whole posting list: 15–30 ms per hybrid query just to *decide* pruning. Hybrid p50 48.3 → 41.3 ms |
| uvicorn `--no-access-log` | **Accepted** | Per-request stdout line through the container log pipe cost ~1.7 ms/query: served floor 4.19 → 2.52 ms p50 (`bench/floor_probe.py`). Weaviate doesn't per-request-log at default verbosity, so also fairness-correct |
| IVF coarse partitioning on top of `IdMapIndex` (k-means shards, nprobe search) | **Rejected** | `bench/ivf_probe.py`, full sweep (nlist 16/64/256/1024 × nprobe 1–32 × 3 calibration modes): best cell 1.63x speedup at −1 pt recall, below the ≥2x-at-≥0.95 gate; every higher-recall cell is slower than the full scan. ~0.4 ms fixed cost per shard search call eats the savings. Closing the vector-latency gap vs HNSW needs an ANN index inside the turbovec kernel. *Corrected by ADR 0005: not a fixed cost; each one-query shard call is a single-core scan of the shard's bytes, and the calls ran one after another* |
| orjson response serialization | **Rejected** | Real 10-hit response: stdlib `json.dumps` 0.028 ms, orjson 0.003 ms — 26 µs/query is noise; not worth a runtime dependency |
| Request-path overhead (pydantic parse of the 1536-float query vector, etc.) | **Rejected (exhausted)** | Server-side parse+validate+normalize is 0.35 ms total; the remaining ~2 ms floor is transport that both engines pay equally |
| Cheaper cold-start ghost reconciliation | **Rejected (no API)**; corrected 2026-09 | `IdMapIndex` exposes no id enumeration or reconstruction; the k=n probe stays (~0.7 s at 553k), correctness over cold-start. **Correction (2026-09, see the cold-start addendum):** the probe was not the cost. At 2.55M rows, true-cold under a 4 GiB cap, the `indexed_counts` GROUP BY (39.4 s) and the reconcile's live-set scan (38.3 s) over the `records` pages were. Both now read a covering index and the diff runs in numpy; the k=n probe stays |

## Storage & concurrency design (bugs found by the re-runs, fixed in `c6b97ff`)

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| One meta.db write connection loosely shared between event loop and worker threads (commits locked, statements not) | **Rejected** | Cross-thread statements join each other's in-flight transactions: a full-corpus ingest LOST 1 of 2,211 journaled jobs (202 + job_id returned, row gone, ids contiguous, integrity ok) |
| Two independent write connections (jobs on the loop, records in threads) | **Rejected** | SQLite busy handlers starve under a hot writer loop: "database is locked" past a 30 s timeout, dead worker, 500s on ingest. Reproduced in isolation with `incremental_vacuum` churn |
| **Single-writer discipline**: every write transaction wholly inside `db_lock`, in a worker thread, never on the event loop; loop-side reads on per-thread read connections | **Accepted** | Stress test (3 concurrent enqueuers × 30 jobs vs draining worker): previously lost a job or wedged in 90 s+, now drains 4,500 rows in 1.2 s. Cost: ingest 785 → 483 vec/s together with the vacuum fix below — accepted for durability (still 1.7x Weaviate) |
| `PRAGMA auto_vacuum=INCREMENTAL` ordered *before* `journal_mode=WAL` | **Accepted (bug fix)** | journal_mode initializes the db file, after which auto_vacuum is a silent no-op: a full ingest left meta.db at 7.3 GB, 93% freelist, 26 s cold start |
| `incremental_vacuum` exhausted with `.fetchall()` | **Accepted (bug fix)** | The pragma frees pages per cursor STEP; pysqlite's `execute()` steps once = frees exactly one page. Measured: 4,900-page freelist → 0 and the file truncates. End state: meta.db 990 MB, cold start 5.1 s |
| Calibration policy details: arm only below the 10k threshold (re-arm on reload below it), feed only fresh rows, slice bulk jobs to calibrate AT the threshold, never fail the ingest job on calibrate errors | **Accepted** | Adversarial review findings: memory-only reservoir + born-empty arming silently forfeited calibration after any pre-threshold eviction/restart; upsert churn skewed the sample; a single bulk job calibrated too late; a calibrate error made the job terminally `error` with committed rows left unsynced |

## Standing constraints these decisions respect

- turbovec is an external dependency — kernel changes (SIMD, ANN, multi-partition search) are out of scope; everything layers on `IdMapIndex`. *Partly superseded by ADR 0005: upstream turbovec proposals are in scope; raggio still carries no turbovec kernel code and never forks it (first-party native code: ADR 0004).*
- Remaining known headroom needs one of: an ANN index in the kernel, more CPUs, or free-threaded Python. Application-level paths are exhausted as of this ADR. *Withdrawn by ADR 0005: parallel IVF shard fan-out was an untried application-level path.*
- Ingest index syncs are batched: one sync covers up to `SYNC_BATCH_JOBS` jobs or `SYNC_BATCH_MS` ms, and a job turns `done` only after that sync. The full vacuum waits for an idle queue. See "Addendum 2026-09 — ingest path" below.

## Addendum 2026-09 — concurrency correctness

Date: 2026-09-29 · Plan B of the 2026-09-28 performance program (`docs/superpowers/specs/2026-09-28-raggio-performance-design.md`). Correctness and availability fixes only, no performance claim. Every Accepted row is pinned by `tests/test_concurrency.py`.

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| Every catalog statement (reads too: `get_config`, `list_collections`, the close in `shutdown`) runs under `_catalog_lock` | **Accepted (correctness)** | `require_collection` is a sync FastAPI dependency, so `get_config` runs on threadpool threads, concurrently with each other and with the loop's catalog writes, on one pysqlite connection. A 16-thread hammer on 3.12 (item 4b brief) got 11,895 wrong rows and 1,962 `InterfaceError`s in 5 s. Over HTTP at c=16 (16 concurrent requests × 80 rounds), a collection key drew 500s and spurious 401s in 16 of 16 sandbox runs (per run, of 640 each: `B:401` 26–61, `B:500` 12–49, `A:500` 11–34). The key check also ran against another collection's row: in 15 of the 16 runs, requests to collection A were accepted with collection B's key. On gn100 at 19a8a2e (probe p4, c=16), collection-key requests failed at 5.4–6.8 % on the GIL builds and 0.46 % on 3.14t: `InterfaceError` at store.py:1631, `IndexError: tuple index out of range` on `row[7]` at store.py:1639 (another statement's row came back), and silent 401s on valid keys. After the fix: the exact 401/200 split, with 0 errors, and 0×401/0×500 for collection-key searches at c=16. `async def require_collection` was not adopted; it would rely on every catalog caller staying on the loop, while the lock holds wherever the caller runs |
| `delete_collection` evicts the collection and deletes its catalog row under `_load_lock` | **Accepted (bug fix)** | Eviction's `stop()` yields. A `touch()` that landed there reloaded the collection from the still-present row and files. The result was a resident collection on deleted files, which also shadowed a re-created collection of the same name. When the delete landed during a load, the result was a 500 (`unable to open database file`). The rmtree stays outside the lock: once the row is gone, a racing `touch()` gets a 404 |
| `Collection.stop()` takes the write lock, then cancels the worker, syncs, and closes every connection under `db_lock` (`_close_conns`). `_rdb` re-checks `_closed` under `db_lock` before it registers a connection | **Accepted (bug fix)** | Cancelling the worker first released the write lock mid-upsert while the orphaned `to_thread` body kept running (asyncio locks don't bind threads). That body committed `indexed=1` rows the index never received, until the job's replay re-upserted them. A filtered search in that window raised turbovec `KeyError: allowlist contains id(s) not present in index`; this was seen in 1 of 50 stress runs of an interim fix that kept the cancel-first order. Closing outside `db_lock` pulled `self.db` out from under an in-flight write transaction. A read connection opened during `stop()` was never closed, which leaked a handle and, on Windows, left the directory undeletable. The worker can now be cancelled only outside its write section. Plan D2's batched-sync flush goes in the same section, before `_close_conns` |
| A search racing `DELETE /collections/{name}` answers 404: `delete_collection` sets `Collection.deleted` before `stop()`, a search that enters the closed collection raises `CollectionDeletedError`, and the search endpoint maps that one error to 404 | **Accepted (availability, spec §3.1 B1)** | On gn100 at 19a8a2e (probe p4), searches racing `DELETE` got 171–704 500s per run on every build, with the errors of a collection working on deleted files: `unable to open database file`, `no such table`, `disk I/O error`, and an IVF `FileNotFoundError`. The `_load_lock` row removes the resurrected collection, and the `stop()` row makes a stopped collection fail closed. After those two fixes, a search queued for the read lock behind a writer (ingest, PATCH, document delete, index swap) still ran after `stop()`, because writer preference lets `stop()` go first, and got a 500 on the closed collection. Only `CollectionDeletedError` is mapped. Broad SQLite errors are never turned into 404: on a live collection they are real faults. The in-process HTTP test gives 0×401 and 0×500 for 320 searches at c=16, half with the collection key, and only 200 or 404 for 16 searches queued behind a PATCH when `DELETE` lands. A text query that the server embeds (vector or hybrid mode, no supplied vector) also answers 404 when `DELETE` lands during its embedding call: the endpoint maps a failed embedding call on a deleted collection to `CollectionDeletedError`, and a closed collection refuses to build a new embedder client. The gn100 re-run of p4's race phase is journaled after merge (plan B, Task 8) |
| `indexed_counts` is copy-on-write: writers copy, update and rebind it under `db_lock`, and readers read the attribute once | **Accepted (free-threading readiness)** | An in-place insert of a new type key races `sum(indexed_counts.values())` on reader threads under free-threading ("dictionary changed size during iteration"). With the GIL, `sum()` is one C call and can't fail, so the tests pin the discipline structurally: an identity check, a `db_lock` spy, and a source tripwire. Pre-seeding the key set (the item 4b option) was not adopted: copy-on-write also gives readers one consistent version of all counts and needs no list of types |
| Mixed-workload stress test: 16 clients × 3 s of searches (15% cancelled mid-flight), upserts and re-upserts, metadata patches, deletes and unlocked reads, plus a `stop()` mid-storm | **Accepted (regression guard)** | Checked after quiescence: the vector index size equals the number of indexed rows, `indexed_counts` equals `GROUP BY type`, and `stop()` fails closed (only "collection is closed" or "closed database" errors) and closes every connection it opened. After the fixes above: 50/50 green runs of `tests/test_concurrency.py`, measured three times: before and after the 404 row's test was added, and on the final module (Windows, CPython 3.12). One 60 s soak also passed (`RAGGIO_STRESS_SECONDS=60`: both stress tests in 127 s) |
| Orphaned `to_thread` work from cancelled searches (stale allowlist R2, IVF id cache R2b); the request reads that take no RW lock (`list_records`, `get_document`, job status), which fail with "closed database" 500s during an eviction; the requests other than a queued search that race `DELETE /collections/{name}`; a same-name create racing the delete's file removal; and the wait a `DELETE` on a busy collection adds to every collection's requests | **Deferred** | R2 and R2b belong to plan F, together with `_allow_cache` and `_IvfIndex`. *Closed by ADR 0005's generation counters.* Probe p4 did not observe R2 over HTTP on gn100: 0 stale documents in 6 runs. The 404 row above covers only a search that reaches the read lock after `stop()`. These still answer 500: writes racing the delete (ingest, metadata PATCH, document delete, index requests); the unlocked reads; and a search on a collection that an LRU or idle eviction closed, which stays a `RuntimeError` 500 and never becomes a 404, because that collection still exists. All of them fail with an error rather than a wrong answer, as far as tested. The 404 for a server-embedded search that `DELETE` interrupts arrives only after the embedder's retry backoff (about 7 s), because `Embedder` retries every error, a closed client included, and `embeddings.py` is outside this plan's files. No test pins closing a connection under a statement that is still running on it. In the final review's probes on Windows (SQLite 3.47.1), `close()` waited for the running statement. With raggio's connection setup it did not crash in 8 of 8 probes; a bare connection with no earlier statement did crash. While it waits, the close holds `db_lock`, and on `DELETE` also `_load_lock`. Whether they should take the RW lock, or map to 404 or 503, is an open review question. The delete fix brings two known costs. First, a same-name `create_collection` that lands between the delete's catalog `DELETE` and its unlocked `rmtree` loses its directory, and its next request answers 500. 19a8a2e has the same window, and plan B does not widen it. The likely fix renames the directory to a tombstone under `_load_lock`. Second, `delete_collection` holds `_load_lock` across a `stop()` that now waits for an in-flight write section. A `DELETE` on a busy collection therefore makes every collection's requests wait until that `stop()` finishes. First the victim's in-flight readers drain. Then any writer queued ahead of it runs; an ingest write section has no size cap. Then come the index sync and the connection close, which also waits for any orphaned read still running. The LRU and idle evictions already held `_load_lock` across `stop()` at 19a8a2e. Plan C moves collection construction off the event loop, and that is where `stop()` can leave `_load_lock`. Its acceptance should measure this stall as p99 on other collections while one is deleted mid-ingest. **Update (2026-09, plan C):** `stop()` stays under `_load_lock`. Requests to resident collections no longer wait for it; requests for collections that are not loaded yet still do. Plan C does not measure this stall on gn100; see the cold-start addendum |

## Addendum 2026-09 — cold start and loop hygiene

Date: 2026-09-30 · Plan C of the 2026-09-28 performance program (`docs/superpowers/specs/2026-09-28-raggio-performance-design.md`). Direction-tested on gn100 (DGX Spark, aarch64) at 2,549,119 chunks × 1024 dims, 4-bit, `--memory 4g`, SQLite 3.40.1, by the p1 prototype (`docs/superpowers/research/2026-09-28-dgx-probe-p1-coldstart-sqlite-memory.md`). "True-cold" means the collection's files were evicted from the page cache with `POSIX_FADV_DONTNEED` (spec D12). The figures in the rows are the p1 prototype's and set each direction; the acceptance run on the shipped image (SQLite 3.53.1), under "Acceptance run" below, is the authoritative measurement. Every Accepted row is pinned by `tests/test_cold_start.py`.

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| Covering index `idx_records_doc_type(doc_id, type, indexed)` replaces `idx_records_doc`; partial `idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')` | **Accepted** | `Collection()` true-cold at 4 GiB: 80.1 s → 3.4 s (IVF), reading 8.8 → 1.7 GB. Every later `GET /collections/{name}`: about 41 s → 0.57–0.59 s. Pending-jobs query true-cold 0.31 → 0.04 s |
| Build the covering index as a one-time migration inside `open_meta_db` (off the loop, progress logged, build before drop) | **Accepted** | 40.8 s true-cold, 1.73 s warm at 2.55M rows (about 16 s per 1M cold); meta.db +96 MB, 86 MB freelist left until the next job's `incremental_vacuum` (until a `VACUUM` on a meta.db created before auto-vacuum), WAL peak about 105 MB. A lazy background build was rejected: every scan would stay on the slow plan until it finished, and it adds a second writer during load. Loads and migrations run one at a time, under `_load_lock`: a request for any collection that is not loaded yet waits for them. A collection with unfinished jobs is loaded, and migrated, at start-up, before the server begins answering |
| Pin the listing page query with `NOT INDEXED` | **Accepted (bug fix)** | After the migration the planner walks `idx_records_doc_type` for a sorted `type='chunk'` page: 0.91 → 2.73 s warm. `ANALYZE` leaves the plan unchanged (2.60 s); `NOT INDEXED` gives 0.76 s |
| Id-set diffs in numpy (`np.fromiter` + `np.setdiff1d(assume_unique=True)` over `uint64`) for ghost reconcile and the attach/detach swap | **Accepted** | 0.02 s against 0.3–0.73 s for the Python set diff, identical ghost sets. Both sides stay `uint64`: an `int64`/`uint64` mix promotes to `float64` |
| `Collection(...)` constructed in a worker thread under `_load_lock`, cancel-safe; `stats()` in a thread | **Accepted** | Baseline3: the first GET after a start blocked `/healthz` and every request for the whole 128 s load. The p1 prototype kept `/healthz` at ≤ 3 ms during a cold load. A cancelled request no longer orphans a half-built collection |
| `_vec_sample` by random rowids, with an `ORDER BY RANDOM()` fallback below id density 0.2, when k > n/2, or when too few hits come back after 8 rounds | **Accepted** | k = 65,536: 110.5 s true-cold (16.9 GB read) → 8.05 s (829 MB); 9.35 → 0.39 s warm |
| `ENV MALLOC_TRIM_THRESHOLD_=134217728` in the image plus `malloc_trim(0)` after every index job | **Accepted** | With default glibc a swapless 4 GiB `POST /index` was OOM-killed mid-build after the headroom guard had passed, and with swap the post-job RSS stayed at 3.0 GiB against 1.5 GiB loaded, so the next job was refused. With both: HWM 3,631–3,646 MiB, RSS 1,506–1,707 MiB after each job |
| `malloc_trim(0)` alone, or `MALLOC_ARENA_MAX=2` | **Rejected** | Trim alone was OOM-killed like default glibc. `MALLOC_ARENA_MAX=2` gave no memory benefit and 10–20 % slower builds (57–63 s against 52 s) |
| Headroom guard before any SQL, need × `ATTACH_NEED_FACTOR` (1.25) | **Accepted** | Cold refusals wasted 139 s (detach) and 194 s (attach) of pre-work before the guard ran. The summed need (1,757 MiB, 1,884 MiB with the margin) was below the measured growth of 2,176–2,186 MiB; × 1.25 gives 2,195 MiB. The guard reads `memory.max − VmRSS` and ignores swap, so it stays conservative and the swapless run is the gate |
| Rows without a retained vector found from the build stream (live ids − streamed ids) instead of an anti-join before the build | **Accepted** | The pre-work (backfill `LEFT JOIN` + `COUNT`) took 178 s true-cold. At 1024-d each ~2 KB fp16 vector fills its own 4 KiB `vecs` page, so any existence check over `vecs` visits every leaf, about 10.5 GB (derived, not measured); the stream reads exactly those pages anyway. A collection with too few retained vectors to fill the training sample still backfills everything first. A detach that finds missing vectors now fails after its stream rather than before; an attach backfills every such row, so that only happens if `vecs` rows were lost |
| A `DELETE` keeps `stop()` under `_load_lock` (plan B's Deferred row) | **Unchanged** | Requests to resident collections no longer wait for it: `touch()` returns a resident collection without taking the lock (`test_resident_touch_does_not_wait_for_another_collections_delete`). A request for a collection that is not loaded yet still waits for a running delete's `stop()`. Moving `stop()` out of the lock was rejected: without renaming the directory to a tombstone first it widens the name-reuse race, and Windows cannot rename a directory while SQLite handles are open in it. Plan C does not measure this stall on gn100 |
| `GET /collections/{name}` while a `DELETE` or an eviction closes the collection | **Deferred** | `stats()` now runs in a worker thread, so the close can land mid-call and the request answers 500 ("collection is closed"). This is the class of unlocked reads that plan B's Deferred row lists (`list_records`, `get_document`, job status): an error, never a wrong answer |
| The k=n probe in `_reconcile_ghosts` | **Unchanged** | `IdMapIndex` still exposes no id enumeration |

### Acceptance run

Session `c1`, 2026-09-30, on gn100 (DGX Spark, aarch64). Image `localhost/raggio:022674c` (`sha256:5c7819694ccf`): Python 3.12.14, SQLite 3.53.1 (`sqlite3.sqlite_version`), `OPENBLAS_NUM_THREADS=1`, turbovec 1.0.0. Collection `bench`: 2,549,119 chunks × 1024 dims. Every container ran with `--memory 4g`; the memory run added `--memory-swap 4g`, so it had no swap. **true-cold**: every file of the volume fsynced and dropped with `posix_fadvise(POSIX_FADV_DONTNEED)`, with `mincore` confirming at most 16 MB still cached (D12). **host-warm**: every file of the volume read once just before. Each row states its regime.

| Index | Start (true-cold) | → `/healthz` | → first GET | Slowest `/healthz` | Later GETs | VmRSS |
|---|---|---|---|---|---|---|
| ivf | `p2-ivf-1` | 0.43 s | 4.88 s | 0.002 s (18 probes) | 0.44 / 0.45 / 0.44 s | 1,464 MiB |
| ivf | `p2-ivf-2` | 0.32 s | 4.53 s | 0.003 s (17 probes) | 0.44 / 0.48 / 0.45 s | 1,464 MiB |
| ivf | `p2-ivf-3` | 0.34 s | 4.82 s | 0.001 s (18 probes) | 0.44 / 0.44 / 0.44 s | 1,464 MiB |
| flat | `p2-flat-1` | 0.32 s | 4.14 s | 0.002 s (15 probes) | 0.45 / 0.45 / 0.48 s | 1,460 MiB |
| flat | `p2-flat-2` | 0.45 s | 4.32 s | 0.001 s (16 probes) | 0.45 / 0.45 / 0.49 s | 1,461 MiB |
| flat | `p2-flat-3` | 0.33 s | 4.18 s | 0.002 s (15 probes) | 0.44 / 0.44 / 0.48 s | 1,461 MiB |

| Index | Regime | `touch()` | Read | Index files | `_vec_sample(65536)` | List page, median of 3 |
|---|---|---|---|---|---|---|
| ivf | true-cold | 4.12 s | 1,650 MB | 1,550 MB | 10.39 s | – |
| ivf | host-warm | 2.15 s | 0 MB | 1,550 MB | 0.42 s | 0.92 s |
| flat | true-cold | 3.21 s | 1,374 MB | 1,275 MB | 11.12 s | – |
| flat | host-warm | 1.76 s | 0 MB | 1,275 MB | 0.42 s | 0.94 s |

Memory run (`--memory 4g --memory-swap 4g`, starting from the flat state):

| Snapshot | Regime | VmRSS | VmHWM | cgroup | VmSwap | `oom_kill` |
|---|---|---|---|---|---|---|
| loaded | true-cold | 1,460 MiB | 1,540 MiB | 2,828 MiB | 0 MiB | 0 |
| after_attach1 | true-cold | 1,686 MiB | 3,515 MiB | 2,474 MiB | 0 MiB | 0 |
| after_attach1_15s | true-cold | 1,695 MiB | 3,515 MiB | 2,472 MiB | 0 MiB | 0 |
| after_detach | host-warm | 1,500 MiB | 3,515 MiB | 2,334 MiB | 0 MiB | 0 |
| after_detach_15s | host-warm | 1,514 MiB | 3,515 MiB | 2,334 MiB | 0 MiB | 0 |
| after_attach2 | host-warm | 1,741 MiB | 3,527 MiB | 2,541 MiB | 0 MiB | 0 |
| after_attach2_15s | host-warm | 1,754 MiB | 3,527 MiB | 2,540 MiB | 0 MiB | 0 |

Jobs: attach1 done in 185.5 s (true-cold); detach done in 15.5 s (host-warm); attach2 done in 42.8 s (host-warm).
First attach (true-cold), from its `attach_index` line: n=2,549,119, nlist 256; pre-work 0.00 s, sample 10.88 s, build 170.68 s, live-id diff 1.63 s, backfill 0 rows 0.00 s, swap 2.30 s, total 185.49 s.
Migration `CREATE INDEX idx_records_doc_type`: 49.6 s true-cold, 1.5 s host-warm.

Checks (spec §7 C):

- PASS: C1 — start → first `GET /collections/bench`, true-cold: ivf 4.82 s (≤ 6 s, median of 3), flat 4.18 s (≤ 5 s, median of 3)
- PASS: C2 — slowest `/healthz` while the first GET loaded the collection 0.003 s (≤ 0.1 s; 99 probes over 6 starts)
- PASS: C3 — later GETs, median over starts of each start's slowest of 3: ivf 0.45 s, flat 0.48 s (≤ 1.0 s)
- PASS: C4 — `Collection()` true-cold read: ivf 1,650 MB with 1,550 MB of index files (≤ 1,700 MB) in 4.12 s; flat 1,374 MB with 1,275 MB of index files (≤ 1,425 MB) in 3.21 s
- PASS: C5 — migration `CREATE INDEX` 49.6 s true-cold (≤ 60 s), 1.5 s host-warm (≤ 3 s); growth 91.5 MB (≤ 120 MB; file 91.5 MB, covering index 91.4 MB, first migration of this volume: yes)
- PASS: C6 — `_vec_sample(65536)` slowest 11.12 s true-cold (≤ 15 s), 0.42 s host-warm (≤ 1 s)
- PASS: C7 — swapless POST → DELETE → POST: jobs done/done/done; no swap and no OOM kill: yes; max VmHWM 3,527 MiB (≤ 3,891 MiB); VmRSS 15 s after each job vs loaded 1,460 MiB: +235, +54, +294 MiB (≤ +512 MiB)
- PASS: C8 — host-warm list page (`chunks`, sort `-year`, 20 rows), median: ivf 0.92 s over 3 pages; flat 0.94 s over 3 pages (≤ 1.0 s)
- PASS: C9 — true-cold `POST /index` pre-work 1.63 s (≤ 10 s): guard 0.00 s + live-id diff 1.63 s + backfill of 0 rows 0.00 s
- PASS: labels — 31 rows, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1; every measurement row states its regime and memory cap

## Addendum 2026-09 — ingest path

Date: 2026-09-30 · Plan D, phase D1, of the 2026-09-28 performance program (`docs/superpowers/specs/2026-09-28-raggio-performance-design.md`). Direction-tested by the item2a prototype (`docs/superpowers/research/2026-09-28-item2a-ingest-path.md`) on a Windows x86-64 laptop: 250 × 1024 jobs, a 12k-row prefill, 2 enqueuers, every 5th job a re-upsert. Laptop numbers carry about ±30 % noise. The shipped code's gn100 A/B is under "Results — DGX A/B (D1)" below. Every accepted code change is pinned by `tests/test_ingest_path.py`.

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| Binary job journal in a side table `job_payloads(job_id INTEGER PRIMARY KEY, data BLOB)`, with `jobs.payload` NULL. Format: a `<4sIII` header (magic `RGJ\x01`, JSON length, vector count, dim), UTF-8 JSON in which each supplied vector is replaced by its row index, then little-endian float32 rows | **Accepted** | The claim `UPDATE` rewrote the 2.7 MB payload overflow chain, because `'pending'` and `'processing'` differ in length: 40–66 ms per job. With the side table: 3–10 ms. Enqueue SQL 71–94 → 41 ms. Bit-exact: the worker already stored float32 |
| The binary payload inline in `jobs.payload` | **Rejected** | No schema change, but it keeps the 40–66 ms claim rewrite |
| Legacy TEXT-JSON rows still replay, in id order, next to binary rows. A row with neither payload becomes an `error` job (`bad job payload: ...`) and never kills the worker | **Accepted (compatibility)** | A volume upgraded with open jobs holds both kinds. `error` jobs keep their payload row (`docs/storage.md`) |
| Downgrade: drain the ingest queue (`pending_jobs == 0` on every collection) before starting an image from before the binary job journal | **Accepted (documented, not enforced)** | An older image reads `jobs.payload` only, so it would mark new-format open jobs `error`. A drained queue leaves no open job for the older image to claim. Failed jobs keep their payload rows, which it ignores |
| Encode, decode, flattening and matrix building off the event loop, in the threads that already do the SQLite work. The encode runs before `db_lock` is taken, and the decode after it is released | **Accepted** | Base: `json.loads` of a 2.7 MB payload (41–68 ms per job) ran on the loop, and `json.dumps` (74–124 ms) was evaluated on the loop before `to_thread`. Loop stalls over 20 ms per 40-job run: 10.5–11.4 s → 0.4–0.5 s. Binary decode: 0.7–1 ms |
| Set-based, failure-safe upsert: one transaction, an `IN` lookup per 512 ids, `executemany` DELETE then INSERT, rollback on any exception. The index removal (`_unindex`) and the one copy-on-write `indexed_counts` rebind (still under `db_lock`) run after the commit. Deletes follow the same order | **Accepted (bug fix)** | Base removed index ids and changed counts before the commit, with no rollback. A failure midway (SQLITE_FULL, I/O) left the implicit transaction open, and the worker's `_finish_job('error')` committed the half-applied upsert under a terminal job that never replays. Throughput: +0–12 % |
| The upsert writes `vecs` with `INSERT OR REPLACE` | **Accepted (bug fix)** | A `vecs` row at a new id, past `MAX(records.id)`, is an orphan that no record owns. A plain `INSERT` fails that job, and since the rollback leaves `MAX(id)` where it was, every later job of the collection fails too. Replacing the orphan heals it and cannot overwrite a live vector |
| `POST /index`'s log line counts the vectors the backfill wrote (`backfill=`), not the records it tried | **Accepted (log fix)** | A record deleted during the backfill's embed was counted but not written. D1 changes no other index-job logic |
| Several jobs in one SQLite transaction | **Rejected** | Holds `db_lock` longer to save about one fsync per job |
| A failure after the commit (`add_with_ids`, calibration) | **Unchanged (known gap)** | It still leaves an `error` job whose committed rows are missing from the index. `_reconcile_ghosts` only evicts; it never re-adds |
| Vacuum policy: a full `incremental_vacuum` only when no job is open. While jobs are open, a finish that sees `VACUUM_FREELIST_PAGES` (16,384 pages, 64 MB at 4 KiB) or more trims the freelist back to `VACUUM_FREELIST_PAGES − VACUUM_CHUNK_PAGES` (`VACUUM_CHUNK_PAGES` = 2,048, so 14,336 pages). Both pragmas are drained with `.fetchall()` | **Accepted** | Per-job vacuum with the side table cost 38–54 ms; idle-only, 7–20 ms. A fixed-size chunk would let the freelist grow whenever a job frees more than one chunk |
| `bench/ingest_probe.py`, an in-process drain benchmark (flat and IVF) that also runs in the base tree | **Accepted (tooling)** | `bench.py` never ingests into an IVF collection: `--engine raggio-ivf --reingest` only rebuilds the index. Probe rows are labelled host-warm, uncapped host process |
| Batched index syncs (the lever in the standing constraints above) | **Deferred to D2** | D1 keeps one sync per job. Prototype, laptop: base 468–483 → + binary journal 810–849 → + set-based upsert 815–955 → + vacuum policy 995–1046 vec/s (flat); IVF nlist 64: 254 → 408 vec/s. Every variant's final state was identical to base; done in D2, see "Batched sync (D2)" below |

### Results — DGX A/B (D1)

#### Measurements (D1)

Run 2026-09-30 on gn100 (NVIDIA DGX Spark, GB10 Grace, 20 aarch64 cores), base `cd3cbf2266ab` (main before D1) against cand `2274a6a0d60d` (D1). Probe rows: `bench/ingest_probe.py --seed 42 --prefill 100000 --jobs 200 --job-rows 250` with `--ivf 0` and `--ivf 256`, each arm running its own tree, one discarded base run0 and then 3 interleaved rounds per mode. Bench row: `bench.py --limit 2549619 --engine raggio --reingest` (2,549,119 x 1024) in the order base, cand, base, cand, each in a fresh 4 GiB container. Values are ingest vec/s. Band = max(base max - min, cand max - min, 1 vec/s); a claim needs gain > band.

| Row | Regime | Cap | sqlite_version | OPENBLAS_NUM_THREADS | base median | cand median | gain | band | verdict |
|---|---|---|---|---|---|---|---|---|---|
| probe flat | host-warm, uncapped host process | none (uncapped host process) | 3.53.1 | 1 | 2342.4 | 4982.0 | 2639.6 | 417.3 | better |
| probe ivf256 | host-warm, uncapped host process | none (uncapped host process) | 3.53.1 | 1 | 533.5 | 612.0 | 78.5 | 98.7 | within band |
| bench reingest | host-warm | 4g | base 3.53.1, cand 3.53.1 | 1 | 1530.9 | 3035.4 | 1504.5 | 17.4 | better |

- PASS: D1-run
- PASS: D1-flat
- FAIL: D1-ivf256
- PASS: D1-fingerprints
- PASS: D1-jobs
- PASS: D1-bench
- PASS: D1-labels
- OVERRIDE: D1-ivf256, by the maintainer on 2026-10-01. The gate fails as written: the gain (78.5 vec/s) is within the band (98.7). Every cand run beat every base run (611.7, 612.0, 710.4 against 505.0, 533.5, 535.2), and the fast third cand run alone sets the band. D1 ships on that evidence; the gate is not loosened, and the session was not rerun.

```json
{
  "date": "2026-09-30",
  "base_sha": "cd3cbf2266ab",
  "cand_sha": "2274a6a0d60d",
  "probe": {
    "flat": {
      "base": [
        2274.9,
        2355.4,
        2342.4
      ],
      "cand": [
        5020.8,
        4982.0,
        4603.5
      ],
      "base_median": 2342.4,
      "cand_median": 4982.0,
      "gain": 2639.6,
      "band": 417.3,
      "verdict": "better",
      "drain_s": {
        "base": [
          21.98,
          21.23,
          21.35
        ],
        "cand": [
          9.96,
          10.04,
          10.86
        ]
      },
      "loop_stall_s": {
        "base": [
          6.38,
          5.475,
          5.498
        ],
        "cand": [
          0,
          0,
          0
        ]
      },
      "payload_rows": {
        "base": [
          null,
          null,
          null,
          null
        ],
        "cand": [
          0,
          0,
          0
        ]
      },
      "complete": true,
      "fingerprints_equal": true,
      "jobs_all_done": true
    },
    "ivf256": {
      "base": [
        505.0,
        533.5,
        535.2
      ],
      "cand": [
        611.7,
        612.0,
        710.4
      ],
      "base_median": 533.5,
      "cand_median": 612.0,
      "gain": 78.5,
      "band": 98.7,
      "verdict": "within band",
      "drain_s": {
        "base": [
          99.01,
          93.73,
          93.42
        ],
        "cand": [
          81.74,
          81.7,
          70.39
        ]
      },
      "loop_stall_s": {
        "base": [
          8.326,
          8.187,
          7.828
        ],
        "cand": [
          0,
          0.19,
          0
        ]
      },
      "payload_rows": {
        "base": [
          null,
          null,
          null,
          null
        ],
        "cand": [
          0,
          0,
          0
        ]
      },
      "complete": true,
      "fingerprints_equal": true,
      "jobs_all_done": true
    }
  },
  "bench": {
    "base": [
      1531.1,
      1530.7
    ],
    "cand": [
      3026.7,
      3044.1
    ],
    "base_median": 1530.9,
    "cand_median": 3035.4,
    "gain": 1504.5,
    "band": 17.4,
    "verdict": "better",
    "runs": {
      "base-run1": {
        "ingest_s": 1664.9,
        "jobs": {
          "done": 10197
        },
        "payload_rows": null,
        "freelist": 0
      },
      "base-run2": {
        "ingest_s": 1665.3,
        "jobs": {
          "done": 10197
        },
        "payload_rows": null,
        "freelist": 0
      },
      "cand-run1": {
        "ingest_s": 842.2,
        "jobs": {
          "done": 10197
        },
        "payload_rows": 0,
        "freelist": 0
      },
      "cand-run2": {
        "ingest_s": 837.4,
        "jobs": {
          "done": 10197
        },
        "payload_rows": 0,
        "freelist": 0
      }
    }
  },
  "labels": {
    "probe": {
      "regime": [
        "host-warm, uncapped host process"
      ],
      "sqlite_version": [
        "3.53.1"
      ],
      "openblas_num_threads": [
        "1"
      ],
      "turbovec": [
        "1.0.0"
      ],
      "python": [
        "3.12.14"
      ],
      "cap": [
        "none (uncapped host process)"
      ]
    },
    "bench": {
      "regime": [
        "host-warm"
      ],
      "cap": [
        "4g"
      ],
      "openblas_num_threads": [
        "1"
      ],
      "sqlite_version": {
        "base": [
          "3.53.1"
        ],
        "cand": [
          "3.53.1"
        ]
      }
    }
  },
  "gates": {
    "D1-run": "PASS",
    "D1-flat": "PASS",
    "D1-ivf256": "FAIL",
    "D1-fingerprints": "PASS",
    "D1-jobs": "PASS",
    "D1-bench": "PASS",
    "D1-labels": "PASS"
  },
  "overrides": {
    "D1-ivf256": "by the maintainer on 2026-10-01. The gate fails as written: the gain (78.5 vec/s) is within the band (98.7). Every cand run beat every base run (611.7, 612.0, 710.4 against 505.0, 533.5, 535.2), and the fast third cand run alone sets the band. D1 ships on that evidence; the gate is not loosened, and the session was not rerun."
  }
}
```

### Batched sync (D2)

Date: 2026-10-02 · Plan D, phase D2. It replaces the "Deferred to D2" row above: D1 kept one index sync per job. Direction from the item2a research (laptop): turbovec v7 writes appends whole and keeps each removal as a header redo op until the next sync, which rewrites the 32-row unit; past 1024 ops a sync rewrites the whole file through a temp file and a rename (about 7 µs per row). A flat sync costs 7–9 ms (laptop), about 15 % of a flat job on gn100 after D1 (about 0.05 s per 250-row job). An IVF sync writes only the shards a job dirtied (Plan F), at about 8 ms per shard (laptop, nlist 64). One 250-row job of evenly spread rows dirties about 160 of 256 shards and a batch of 8 jobs nearly all 256, so one batched sync writes about a fifth of the shards that 8 per-job syncs write (an estimate, not measured; a whole IVF probe job took 0.35–0.41 s on gn100 after D1). The knob defaults are chosen on seed 7 and published on seed 42 (spec §6), under "Results — DGX A/B (D2)" below. Every row is pinned by `tests/test_ingest_path.py`.

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| One index sync per batch of ingest jobs. A batch closes when it holds `SYNC_BATCH_JOBS` jobs (default 8), when `SYNC_BATCH_MS` ms (default 1000) have passed since its first job joined, when the queue goes idle, before an attach or detach job (which then runs alone), and in `stop()`. `SYNC_BATCH_JOBS=1` syncs after every job, as D1 did | **Accepted** | Per-job syncs are a large part of IVF ingest: in the laptop prototype (nlist 64) batching doubled IVF ingest, 404 → 802 vec/s. The seed-7 grid (1, 0), (8, 1000), (32, 1000), (32, 4000) in the results below picks the defaults. On the probe's job mix every fifth job replaces 250 rows, so the removal cap ends a batch about every 10 jobs and the (32, *) pairs act as N ≈ 10 |
| Done-after-sync: a job's rows are committed and indexed in memory, and the job stays `processing` until the sync that covers it returns. The batch's jobs then finish in order, `error` jobs included | **Accepted** | No job reads `done` before its vectors are on disk. A crash before the batch's sync returns leaves all of its jobs `processing`, and a crash during its finishes leaves the unfinished rest `processing`. The idempotent replay redoes them. A document deleted or patched after a job that wrote it ran, but before that job's batch synced, comes back or reverts if the server crashes in between: the replay re-runs the job (spec §3 D8). Batching widens this window from one finish to up to `SYNC_BATCH_MS` plus one job. |
| Claim cursor: the claim adds `AND id > ?` to its `INDEXED BY idx_jobs_open` query, so the worker never re-claims its own `processing` jobs | **Accepted** | A batch's jobs stay `processing` until its sync |
| Removal cap: no sync carries more than `SYNC_MAX_REMOVALS` (512) removals accumulated across a batch's jobs. The job that would pass it syncs first, after its own commit. A lone job over the cap is not split | **Accepted** | Removals pile up as header ops; past 1024 ops the sync becomes a full-file rewrite, extrapolated at about 20 s under the collection's write lock at 2.55M rows |
| A failed sync is logged and retried after `SYNC_RETRY_MIN_S` (1 s), doubling up to `SYNC_RETRY_MAX_S` (30 s). Meanwhile the batch's jobs stay `processing` and the worker claims nothing new; the wait holds no lock. A failed removal-cap pre-sync is logged, and the job goes on | **Accepted (bug fix)** | Before, a sync that raised failed its job as `error`: terminal, never replayed, with committed rows missing from the index file |
| `stop()` syncs the open batch and finishes its jobs. If that sync fails, `stop()` logs it and completes the shutdown without raising: it finishes nothing, the jobs stay `processing` with their payloads, and the next open replays them. A dead worker's exception, and a finish that fails after that sync landed, are logged the same way, and their jobs replay too. A failure to close the SQLite connections, the embedding client or the IVF shard pool is logged, and the next close step still runs | **Accepted** | A clean shutdown finishes what it synced; a failing index file, meta.db or close step never stops a shutdown half-way, so every collection and the catalog close, and the journal carries the unfinished jobs to the next open |
| Several jobs per SQLite transaction | **Still rejected** | Each job keeps its own transaction and its own finish; only the index sync is shared |

### Results — DGX A/B (D2)

Pending: the gn100 A/B of Plan D Task 10 has not run yet.
