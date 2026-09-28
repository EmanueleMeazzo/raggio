# item4b-freethreading-code

## Summary
- **Critical race, already live on today's 3.12 image.**
  - `CollectionManager.get_config` and `list_collections` read the shared catalog sqlite3 connection without a lock (store.py:1630-1651). `require_collection` is a sync FastAPI dependency (app.py:113-120), so each request calls it on a different threadpool thread.
  - Measured on the Windows laptop: wrong rows, `InterfaceError`, and a reproduced cross-tenant auth bypass. On 3.12, 4 of 1,280 requests for collection A authorised with B's key got HTTP 200.
  - On 3.14t the same race segfaults.
  - Two fixes were verified in scratch copies, each with zero anomalies on both 3.12 and 3.14t: take `_catalog_lock` on the reads, or make `require_collection` `async def`. Recommendation: ship this now as a security fix.
- **Second bug, any build: stale filter allowlist.** An orphaned `to_thread` search can cache an allowlist computed before a write. There is a deterministic repro on 3.12 and 3.14t. A generation-counter fix was verified: the repro passes, and the 46 tests and the stress test pass on 3.12 and 3.14t.
- **Smaller fixes of the same kind:**
  - the IVF shard-id cache has the same stale-cache problem;
  - `stop()` closes connections without `db_lock` and can leak read connections;
  - `indexed_counts` iteration can fail on 3.14t only;
  - `delete_collection` evicts without `_load_lock` (an asyncio issue, not a free-threading one).
- **Everything else is safe on 3.14t.** 46/46 tests pass there, and the GIL stays off after importing all runtime deps. The df/avgdl caches are benign; each read connection belongs to one thread (`threading.local`); every write to `self.db` holds `db_lock`; the turbovec index is behind a Rust `RwLock` and releases the GIL on every call.
- **turbovec 1.0.0 ships abi3 wheels only.** On 3.14t, uv builds it from the sdist (needs Rust 1.89 or newer, no patch; wheel tag cp314-cp314t). Item 1's PyO3 0.29 extension builds the same way.
- **Docker.** No official 3.14t images exist, so the plan is a Dockerfile.ft that uses uv's standalone CPython builds, Rust in a build stage, and a build-time check that the GIL is off.
- **Where free-threading helps: the text/hybrid leg.** Laptop numbers:
  - `_text_ids` scales 4.27x at 6 threads on 3.14t, versus 1.08–1.12x on GIL builds.
  - The next limit is the free-threaded build's stop-the-world GC; `gc.freeze()` gives +38%.
  - After item 1's native rescore, 3.14t still roughly doubles text-leg throughput (250/s versus a best of about 128/s on 3.12).
  - The vector path gains little.
  - Multiple uvicorn workers are not an option (the index is per process, jobs get claimed twice, and index syncs overwrite each other).
- **Plan:** ship the stage-1 fixes and the stress test now, add a 3.14 GIL build argument and Dockerfile.ft, then decide on 3.14t with the DGX protocol, preferably after item 1 lands. Adoption bar: hybrid concurrent throughput at least +50% over the best GIL image, with vector regression of 5% or less.

## Brief
# Item 4b: free-threaded CPython (3.14t) for raggio

## 0. How to read this brief
- **[WIN]** marks a number measured on the Windows x86-64 laptop (Xeon E-2226G, 6 cores / 6 threads), **not the DGX**.
- Interpreters used:
  - CPython 3.12.8, from the repo's `.venv`, run read-only with `PYTHONDONTWRITEBYTECODE=1`.
  - python-build-standalone 3.14.7 (GIL build) and 3.14.7t (free-threaded build), both installed through uv.
- The repo was not modified (`git status` is clean). All probes live in `D:\DEV\_scratch\probe-item4b\`:

  | Script | Purpose |
  |---|---|
  | `catalog_race.py` | Thread hammer on `get_config` |
  | `auth_race_http.py` | Real app over httpx ASGITransport. Variants: `auth_race_async.py`, `auth_race_lock.py` |
  | `stale_allow.py` / `stale_allow_gen.py` | Deterministic stale-allowlist repro, before and after the fix |
  | `stress_collection.py` | Mixed workload with post-quiescence invariants |
  | `bench_textleg.py`, `bench_rescore_gc.py`, `bench_rescore_private.py`, `bench_split.py`, `textleg_native.py`, `native_*.py` | Scaling benches |
  | `work*/` | Scratch source copies. `work-lock` = catalog lock fix; `work-async` = async dependency fix; `work-gen` = lock fix plus allowlist generation fix |

- Docs cited:
  - Free-threading HOWTO: https://docs.python.org/3.14/howto/free-threading-python.html. Built-in types use internal per-object locks, but that is "a description of the current implementation, not a guarantee". Importing an extension that is not marked free-threading-safe re-enables the GIL with a RuntimeWarning. There is a single-thread penalty.
  - Thread-safety page: https://docs.python.org/3.14/library/threadsafety.html.
  - Extensions HOWTO: https://docs.python.org/3.14/howto/free-threading-extensions.html.
  - FT garbage collector: https://github.com/python/cpython/blob/3.14/InternalDocs/garbage_collector.md. It is non-generational ("every collection operates on the entire heap") and stop-the-world.
  - PyO3 free-threading guide (https://pyo3.rs/main/free-threading): from 0.28 on, modules are assumed safe (`gil_used = false`) by default.
  - PyO3 performance guide (https://pyo3.rs/main/performance): keep detaching in free-threaded builds for work over about 1 ms, to avoid stalls during stop-the-world GC.
  - FastAPI (https://fastapi.tiangolo.com/async/): sync `def` dependencies run in an external threadpool.

## 1. Baseline compatibility
- **Tests.** The suite passes 46/46 on 3.14t without `PYTHON_GIL=0` (8.3–9.0 s) and 46/46 on 3.14 GIL (9.7 s) [WIN].
- **GIL stays off.** `sys._is_gil_enabled()` stays `False` after importing numpy, turbovec (locally built cp314t wheel), pydantic_core, pydantic, fastapi, starlette, httpx, uvicorn, httptools, yaml, websockets, watchfiles, anyio, `raggio.app` and uvicorn's auto loop/protocol modules [WIN].
- **uvloop 0.22.1 can't be imported on Windows.** Its `loop.pyx` declares `# cython: ... freethreading_compatible=True`, but that is source evidence only. The Dockerfile.ft build check in stage 3 verifies it on Linux.
- **Wheels.** uv.lock already has 105 cp314t wheel entries. Every locked runtime dependency has cp314 and cp314t manylinux aarch64 wheels, **except turbovec 1.0.0**, which ships abi3 wheels plus an sdist (see §5).

## 2. Race inventory
In the table, "loop" means the event-loop thread, "pool" means FastAPI/anyio threadpool threads, and "to_thread" means asyncio's default executor. The "Fix on GIL too?" column answers whether a fix is worth shipping on today's 3.12 image.

| ID | Where (file:line) | Shared state and who touches it | GIL build today | 3.14t | Evidence | Minimal fix | Fix on GIL too? |
|---|---|---|---|---|---|---|---|
| **R1** (critical, security) | store.py:1598 (`catalog = sqlite3.connect(..., check_same_thread=False)`); 1630-1640 `get_config` and 1650-1651 `list_collections` hold **no lock**. Callers: app.py:113-120 `require_collection` (sync `def` dependency, so pool threads, one per concurrent request); loop callers at store.py:1665, 1699, 1733 and app.py:175; loop writers under `_catalog_lock` at 1642-1648, 1685-1691, 1726-1728 (`_save_index_config` 866-868 runs on the loop). | One Python `sqlite3.Connection` (statement cache, cursor state) used concurrently by several pool threads and the loop. | **Already broken.** 16 threads × 5 s [WIN] on 3.12.8: 57,197 ok, **11,895 wrong row**, 3,653 None, 1,962 `InterfaceError: bad parameter or other API misuse`, plus IndexError/SystemError. On 3.14 GIL: 22,131 wrong rows and 11,518 `InterfaceError`. Two threads are enough (3.12: 10,057 wrong out of about 66k). Over HTTP, 64 concurrent requests × 40 rounds per target (1,280 requests each): 3.12 returned **A:200 ×4 with B's key (auth bypass)**, 18 spurious B 401s and 19 `InterfaceError`s. | **Segfault** under the thread hammer. Over HTTP: A:200 ×4 (bypass), 27 spurious 401s, `DatabaseError` and `InterfaceError`. | catalog_race.py, auth_race_http.py (re-run in this session). Wrong-row mechanism: `get_config(A)` returns B's row, so `cfg.key_hash == hash(keyB)` and A is accepted. The bench uses the root key, so it never exercised this path. | **(a)** Wrap the `execute().fetchone()` in `get_config` and the query in `list_collections` in `with self._catalog_lock:`. Verified in `work-lock`: 3.12 and 3.14t both give `{'A:401': 1280, 'B:200': 1280}`; the locked thread hammer gives 135,668 ok with 0 errors on 3.12 and 96,637 ok on 3.14t. **(b)** Alternatively, `async def require_collection`: every catalog user is then the loop thread, because all catalog writers already run there. Also verified: 0 anomalies on both builds, and it saves one threadpool hop per request. Recommendation: do (a); optionally also (b). Also correct the misleading comment at store.py:1613-1614: index_config updates run on the loop, not on worker threads. | **YES. Ship now, standalone.** |
| **R2** (medium: filter leak) | store.py:1178-1191, inside `_vector_ids.run()` (to_thread). Invalidated by `_allow_cache.clear()` after commit at 854 (`_upsert_rows`), 1521 (`_delete_doc_rows`) and 1576 (`_patch_rows`). | The RW lock (store.py:215-255) is **asyncio-level**. Once the awaiting task is gone (cancelled, or a hybrid `asyncio.gather` at 1418-1421 whose sibling raised, since gather does not cancel siblings), the to_thread body keeps running with no lock held. It can then insert an allowlist computed from a pre-write snapshot **after** the writer's `clear()`. | **Already broken.** Deterministic. | Same | stale_allow.py: slows the allowlist SELECT, cancels the search, patches d3 from g=1 to g=2, then searches with g=1. **"STALE: d3 (now g=2) returned for filter g=1"** on 3.12.8 and 3.14.7t. The random stress run (CANCEL=1) never hit it; the window is narrow unless the SELECT is slow, as on a large corpus. If clients use metadata filters for access control, this is a data leak. | Generation counter, verified in `work-gen`. See the code below the table. After the fix: "ok: filter honored" on 3.12 and 3.14t; 46/46 tests on both; stress S=10 C=24 CANCEL=1 PASS on 3.14t. | **YES** |
| **R2b** (low: false negatives) | store.py:375 and 448-475, `_IvfIndex._id_cache`. Writers set `None` at 453 and 460; the reader stores at 474. The comment "racing readers compute the same array; last wins" is only true reader-vs-reader. | Same class as R2: an orphaned reader stores ids from before an `add_with_ids`, after the writer has reset that shard's entry. | Latent | Same | Reasoning only (no repro built). Effect: filtered IVF searches miss rows added to that shard until the next write to it. | `self._id_gen = [0]*nlist` plus a small `threading.Lock`. Writers bump the generation and clear the entry under the lock. Readers capture the generation before `sh.search` and store only if it is unchanged. | Yes (cheap) |
| **R3** (low: lifecycle) | store.py:655-669. `stop()` closes `_read_conns` and `self.db` (665-667) **without `db_lock`**. `_rdb` (1065-1075) checks `_closed` unlocked and appends under `db_lock`. | (1) A worker cancelled at 657 while awaiting `to_thread(self._upsert_rows)` (777) leaves a thread still executing on `self.db` while `stop()` closes it. (2) A read connection created after the close loop runs is never closed. That leaks a handle and, on Windows, keeps the directory undeletable (comment at 596-598). (3) In-flight to_thread readers (list_records via app.py:225, orphaned searches) have their connection closed underneath them. | No crash observed. In-flight operations get `ProgrammingError: Cannot operate on a closed database` or `RuntimeError: collection is closed`, i.e. 500s after eviction. | Same; no crash observed. | stress_collection.py with STOP=1 on 3.12 and 3.14t: no crash or hang. | See the `stop()` code below the table. | Yes (low) |
| **R4** (FT-only) | store.py:575-577: `indexed_counts` is initialised from GROUP BY, so it only has the types that exist. First insert of a new key at 850 (and decrement at 839/1515), in to_thread under `db_lock`. Readers: `sum(self.indexed_counts.values())` at 878 (`request_index`, loop, **no RW lock**), 1254 and 1314 (reader threads), plus `.get` at 1175 and 1359. | Iteration while another thread inserts the first `"summary"` or `"chunk"` key. | Safe: `sum()` over dict values is a single C call with no bytecode boundary, so no other thread interleaves. | `RuntimeError: dictionary changed size during iteration` in `POST /index` (a 500). Only possible on the first record of a new type. | Reasoning plus FT docs (iterating while mutating is not safe). | Pre-seed `{"chunk": 0, "summary": 0, **dict(GROUP BY)}`. The key set never changes afterwards, and value updates don't resize. | Harmless one-liner; ship with stage 1. |
| A1 (asyncio, not FT) | store.py:1724-1729. `delete_collection` calls `_evict` **without `_load_lock`**. | A concurrent `touch(name)` (1694-1718) can reload the collection between evict and the catalog DELETE / rmtree. | Latent | Same | Reasoning only | Hold `_load_lock` across evict plus catalog delete. | Optional |

**R2 fix code (verified in `work-gen`).**
```python
# __init__:  self._allow_gen = 0; self._allow_lock = threading.Lock()
def _invalidate_allow(self):              # replaces the 3 _allow_cache.clear() calls
    with self._allow_lock:
        self._allow_gen += 1
        self._allow_cache.clear()
# in _vector_ids.run():
gen = self._allow_gen                     # read BEFORE the SELECT takes its snapshot
... SELECT ...
with self._allow_lock:
    if gen == self._allow_gen:
        if len(self._allow_cache) >= 8:
            self._allow_cache.pop(next(iter(self._allow_cache)))
        self._allow_cache[key] = allow
```
This also removes the `try/except (StopIteration, RuntimeError)` workaround at 1186-1190.

**R3 fix code.** In `stop()`, after the write-locked sync, call `await asyncio.to_thread(self._close_conns)`:
```python
def _close_conns(self):
    with self.db_lock:
        conns, self._read_conns = self._read_conns, []
        for c in conns: c.close()
        self.db.close()
```
In `_rdb`, re-check `_closed` inside `with self.db_lock:` before appending. If it is set, close the new connection and raise. Optionally, `list_records` and `get_document` could take `lock.read()` so that eviction waits for them; that is a user decision.

**Benign under FT: no fix needed, only comment updates.**
- **B1 `_df_cache`** (get/set/clear at 1233-1243 and 1260-1263). Dict operations are internally locked on 3.14t. A lost insert or a double clear is harmless, and df is a heuristic.
- **B2 `_df_cache_churn`** (`+=` at 855 and 1522 on the writer, reset at 1263 on the reader). A lost update only shifts a heuristic.
- **B3 `_avgdl_cache`** (587-589, 1285, 1301). A reference store/load is atomic. Reword the comment at 587 from "GIL-atomic swap" to "atomic reference swap (holds on free-threaded builds too)".
- **B4 `self.index` swap and `index.nprobe`** (960, 1010, 1060). Attribute stores; readers hold the read lock.
- **B5 `_closed`.** A plain bool.
- **B6 `_cal_reservoir` and calibration** (784-800). Only the worker task touches them, and its to_thread calls run one after another.
- **B7 `_scan_queue` / `_scan_task` / `_drain_scans`** (1199-1231). Loop-only; the thread only reads the index.
- **B8 `CollectionManager.resident`, `_load_lock`, `last_used`.** Loop-only.
- **B9 `self.db`.** Every write path holds `db_lock` (679, 697, 709, 828, 944, 1508, 1570). `_reconcile_ghosts` (643) and init (576) run before the worker starts.
- **B10 read connections.** Each thread gets its own via `threading.local` (1065-1075), so there is exactly one user per connection.
- **B11 module-level objects.** `_TOKEN_RE` (185), `unicodedata` and the constants are immutable or thread-safe.
- **B12 turbovec index objects.** An internal `RwLock`; see §5.

## 3. sqlite3 under free-threading
- **Local builds [WIN].**

  | Interpreter | SQLite version | `sqlite3.threadsafety` | `compile_options` |
  |---|---|---|---|
  | 3.12.8 | 3.47.1 | 3 | — |
  | python-build-standalone 3.14.7 and 3.14.7t | 3.53.1 | 3 | `THREADSAFE=1` (serialized) |

- **What `threadsafety=3` does and does not cover.** It describes the SQLite C library (a serialized mutex per connection). It does **not** make the Python `Connection`/`Cursor` objects safe to share. Two open CPython issues show this:
  - #118172, "SQLite3 multithreading cache inconsistency": a connection shared across threads returns wrong rows or `InterfaceError`.
  - #146471, "Segfault from sqlite3 module when abusing threads".
  - raggio's own comment at store.py:590-593 already says "pysqlite connections aren't concurrency-safe". Only the catalog (R1) ignores this.
- **raggio's three sqlite patterns:**
  - **One writer connection per collection, every use under `db_lock` (`check_same_thread=False`).** Sequential multi-thread use is supported by the sqlite3 docs ("write operations may need to be serialized by the user"), and the lock makes it safe on both builds.
  - **`threading.local` read connections.** One thread per connection; safe on both builds.
  - **The catalog.** Unsafe on both builds today (R1).
- **#155781** (closed 2026-08-21) is a converter-registry crash on free-threaded builds. It does not apply: raggio uses no `detect_types`, `register_adapter`/`register_converter` or `create_function`.
- **Where FT adds parallelism.** sqlite3 already releases the GIL around `sqlite3_step` on both builds, so SQLite work overlaps today. FT additionally parallelises the Python-side row materialisation (tuple and str creation, list building). That is the part that stops scaling on GIL builds (§6).

## 4. Why multiple uvicorn workers can't substitute for FT
- **The index is per process.** `Collection.index` is loaded into each process's RAM, so memory grows N-fold (the arxiv run already needs a 4 GiB cap).
- **Jobs get claimed twice.** `_claim_next` (store.py:694-706) deliberately claims `status IN ('pending','processing')` so that crashes replay. Two processes would therefore both claim the same job.
- **Index writes overwrite each other.** Each process runs `_sync_index` (859-863) against the same `index.tvim` or `ivf/` directory. The last writer wins, and the other processes never reload, so they miss rows.
- **Nothing is shared across processes.** The RW lock, the allowlist/df/avgdl caches and residency/eviction are all in-process, with no cross-process invalidation.
- **Conclusion:** using several cores for Python-level work inside one process requires threads, and threads only run Python in parallel under FT.

## 5. turbovec and the item-1 extension on 3.14t
- **turbovec 1.0.0 on PyPI.** abi3 (`abi3-py39`) wheels plus a self-contained sdist (Cargo.lock and the core crate included; `turbovec/Cargo.toml` has `rust-version = "1.89"`; build backend `maturin>=1.12,<2.0`).
  - abi3 wheels can't be installed on free-threaded 3.14; abi3t (PEP 803) arrives with 3.15. So uv falls back to building the sdist, which needs `cargo` on PATH.
- **PyO3 behaviour.**
  - turbovec-python uses `pyo3 = { version = "0.29.0", features = ["abi3-py39"] }` (turbovec-python/Cargo.toml:31) and a plain `#[pymodule]` (lib.rs:2151).
  - From PyO3 0.28 on that means `gil_used = false`. pyo3-build-config ignores `abi3` on a gil-disabled interpreter below 3.15.
  - maturin warns "abi3 does not yet support CPython 3.14t ... build artifacts will be version-specific" and produces a **cp314-cp314t** wheel. **No patch is needed.**
- **Verified builds [WIN].**
  - `CARGO_TARGET_DIR=... maturin build --release --locked -i python3.14t --out wheels` took 58.6 s and produced `turbovec-1.0.0-cp314-cp314t-win_amd64.whl`.
  - `uv sync --frozen --no-dev --python 3.14t` built the sdist automatically (1 min 06 s) when cargo was present.
  - After import, the GIL stays off.
- **Concurrency semantics** (turbovec-python/src/lib.rs:367-380, 416-431; python/turbovec/__init__.py:18):
  - The pyclass is `frozen`, with the index behind a `std::sync::RwLock`.
  - **Every method acquires the lock inside `py.detach`**, so waiting on the lock never holds the GIL.
  - Concurrent `search` calls share the read lock and run in parallel. `add`/`add_with_ids`/`remove`/`sync` take the write lock and block searches for the duration of each call.
  - The Python wrappers split batches at `BATCH_CHUNK_SIZE = 4096` rows, one lock acquisition per slice.
  - raggio's asyncio RW lock already keeps searches and writes apart, except for orphaned threads. turbovec's lock is the backstop that makes those orphan cases memory-safe, as the comment at store.py:1211-1213 already assumes.
- **Item 1's native extension** (copy at `D:\DEV\_scratch\probe-item4b\item1-native`).
  - pyo3 0.29.2, no abi3, `#[pymodule(gil_used = false)]`, and both `fold_tokens` and `bm25_topn` detach.
  - `maturin build --release --locked -i python3.14t` took 19 s and produced `raggio_native-0.1.0-cp314-cp314t-win_amd64.whl`; the GIL stays off [WIN].
  - Packaging requirement for item 1: whichever way it ships (path dependency with a maturin backend, or a separate wheel), it must build for cp312, cp314 and cp314t inside the same build stage as turbovec.
  - Keep detaching even on free-threaded builds (PyO3 performance guide).
- **Container images.**
  - Docker Hub `python` has no 3.14t or free-threaded tags.
  - `ghcr.io/astral-sh/uv:python3.14t-bookworm-slim` and the trixie variant return 404.
  - These do exist (multi-arch amd64/arm64): `uv:python3.12-bookworm-slim`, `uv:python3.14-bookworm-slim`, `uv:python3.14-trixie-slim` and `uv:bookworm-slim`.
  - uv offers `cpython-3.14.7+freethreaded-linux-aarch64-gnu`. The request `3.14t` selects the FT build and `3.14+gil` forces the GIL build (uv docs, concepts/python-versions; uv 0.9 changelog #16142).

## 6. Where free-threading helps: request-path analysis
**What runs where.**

| Work | Runs on | GIL-held Python? | FT effect |
|---|---|---|---|
| httptools parse, pydantic `SearchIn` validation, embedder httpx call and JSON, `_normalize`, `_hydrate` SQL (loop's own read connection), `_rrf`, `_expand` bookkeeping, response JSON encoding | Loop thread | Yes | **None.** One event loop means one thread. |
| `require_collection` / `require_root` | Pool thread (sync `def`) | Yes (tiny) | None. It costs a threadpool hop per request (fix R1b removes it). |
| Vector leg: `_vector_ids` → `_scan_batched` → `_search_rescored` | to_thread, one batched pass per collection at a time (1210-1231) | No: the turbovec kernel detaches | **Small.** Already parallel and memory-bound. |
| Filter allowlist SELECT to list to `np.array` | to_thread | Row materialisation, yes | Moderate |
| Text leg `_text_ids` (1345-1397): FTS5 MATCH, `_prune_common` df lookups, Python BM25/SDM `_bm25_rescore` (1304-1343: `_fold_tokens` NFKD + regex + tf loops) | to_thread | **Yes, CPU-bound** | **Large.** This is the target. |

**Text-leg scaling [WIN].** Scale factors are relative to one thread. `_text_ids` ran on a 40k-record Zipf corpus through the two-stage path; `_bm25_rescore` used 1,500 candidates.

| Build | `_text_ids`, 1 → 6 threads | `_bm25_rescore`, 1 → 6 threads |
|---|---|---|
| 3.12 GIL | 12.1/s → 13.1/s (×1.08) | 6.2/s → 6.4/s |
| 3.14 GIL | 14.7/s → 16.4/s (×1.12) | 6.5/s → 6.6/s |
| **3.14t** | 13.2/s → **56.2/s (×4.27)** | 6.3/s → 17.3/s (×2.75; ×2.92 at 4 threads) |

At one thread, 3.14t is about 10% slower than 3.14 GIL, in line with the documented single-thread penalty.

**The rescore limit on 3.14t is the free-threaded GC, not reference-count contention [WIN].**
- Giving each thread its own private copy of the candidates did not help (still 17.3/s).
- Each piece scales ×4.2–4.9 on its own (fold, NFKD, findall, split, tf loop).
- With `gc.disable()` it reaches 24.1/s at 6 threads (×4.3); with `gc.freeze()`, 23.8/s.
- After `create_app` on 3.14t there are 89,684 GC-tracked objects. A full collection takes 10.89 ms with **all threads paused**, and 3.84 ms after `gc.freeze()`.
- Proposed knob: call `gc.freeze()` at the end of lifespan startup (user decision, measured on the DGX).

**Item 1 changes the absolute numbers but not the conclusion [WIN].**
- Native `bm25_topn` over 1,500 candidates: 161/s → 791/s on 3.12 (×4.9, because it detaches) and 143/s → 767/s on 3.14t. That is about 26× the Python rescore, and it scales on both builds.
- Native `fold_tokens` called once per text: flat at 40/s on 3.12; 40/s → 178/s on 3.14t.
- `_text_ids` with the native rescore swapped in (textleg_native.py):

  | Build | 1 thread | 2 | 4 | 6 |
  |---|---|---|---|---|
  | 3.12 GIL | 85.2/s | 127.8 | 125.0 | **76.2 (×0.89)** |
  | 3.14t | 86.0/s | 151.0 | 230.0 | **249.9 (×2.90)** |

- On 3.12, the remaining GIL-held glue (SQLite row-to-str creation, `_prune_common`, list building) caps throughput at about 1.5 cores, and it gets *worse* at 6 threads.
- So FT and item 1 are complementary. Item 1 gives the big constant-factor win; FT gives multicore scaling of what is left.
- With 20 cores on the DGX, the GIL ceiling stays at about 1.5 cores, so FT's relative advantage should be larger than on the laptop (UNVERIFIED).

**Loop-bound mixed workload [WIN].** stress_collection.py, 15 s, 24 clients: 3.12 completed 4,062 ops, 3.14 5,243 and 3.14t 5,196. This shows no FT regression for loop-bound traffic, not a speedup.

**GIL convoy.** On GIL builds, CPU-bound text threads hold the GIL for up to the 5 ms switch interval, which delays the loop. Expected FT effects: lower hybrid p99, and less inflation of vector p95 under mixed load (UNVERIFIED on the DGX). bench.py has no mixed-load phase; hybrid concurrent is the proxy.

**Published DGX baseline** (bench/results-arxiv.md: GB10, 20 cores, 2,549,119 × 1024 vectors): vector 42 QPS serial / 129 concurrent; hybrid 8.6 serial / **13.8 concurrent (only ×1.6 at c=8)**, p99 588 ms. That pattern is consistent with a GIL-bound text leg.

## 7. Staged plan (TDD tasks)

### Stage 1: FT-safe code plus a stress test (ship on 3.12 now)
New file: `tests/test_concurrency.py`. Follow the existing style: `asyncio.run`, `tmp_path`, `monkeypatch`, `FakeEmbedder`, `make_collection` from test_store_optimizations.py. Do the stage-1 tasks in this order.

1. **T1.1 (R1): catalog race.**
   - RED tests:
     - `test_catalog_get_config_concurrent`: 16 threads for about 1 s. Each alternates `get_config("a")` / `get_config("b")` and asserts `cfg.name` equals the requested name, with no exceptions. It fails within milliseconds on 3.12.
     - On 3.14t an unfixed run segfaults. Run the hammer in `subprocess.run([sys.executable, "-c", ...])` so a crash shows up as a clean assertion failure.
     - `test_collection_key_never_crosses_tenants`: httpx `AsyncClient(transport=ASGITransport(app))`, collections A and B with keys keyA and keyB, `gather` of 64 requests × 20 rounds of `GET /collections/{A|B}` with keyB. Assert A is always 401 and B is always 200. Baseline [WIN]: 4 bypasses per 1,280 requests on 3.12.
   - GREEN: lock the reads in `get_config` and `list_collections` (verified), and fix the comment at 1613-1614.
   - Optional T1.1b: `async def require_collection` and `require_root` (verified).
   - Acceptance: both tests pass on 3.12, 3.14 and 3.14t, and the existing 46 tests pass.
2. **T1.2 (R2): stale allowlist.**
   - RED: port stale_allow.py as `test_orphaned_search_cannot_cache_stale_allowlist`. Monkeypatch `col._rdb` with a wrapper that sleeps 0.3 s on `SELECT id FROM records WHERE`. Cancel the search after 50 ms, `patch_metadata("d3", {"g": 2})`, sleep 0.5 s, then assert that a `{"g": 1}` search excludes d3. Fails on every build today.
   - GREEN: generation counter (verified code in §2).
3. **T1.3 (R2b): IVF shard-id cache.**
   - RED: `_IvfIndex` with 1,024 or more rows. Monkeypatch shard `search` in `_shard_ids` to block on a `threading.Event`, run `_shard_ids(j)` in a thread, `add_with_ids` a new id into shard j from the main thread, release the event, then assert that `_id_cache[j]` is either None or contains the new id.
   - GREEN: per-shard generations.
4. **T1.4 (R4): `indexed_counts` key set.**
   - Test: on a fresh collection, ingest chunks only, then a first summary; assert the key set is unchanged (`{"chunk", "summary"}`).
   - GREEN: pre-seed the keys.
5. **T1.5 (R3): `stop()` lifecycle.**
   - Test (a): patch `sqlite3.connect` in `raggio.store` to count opens and closes. Run `list_records` and `get_document` in threads while `await col.stop()`. Assert opens equal closes, and that `_rdb()` after stop raises `RuntimeError` without opening a connection.
   - Test (b): cancel the worker mid-`_upsert_rows` (monkeypatch it to block on an Event); `stop()` must wait for `db_lock` rather than closing under it.
   - GREEN: `_close_conns` under `db_lock`, and the re-check in `_rdb`.
6. **T1.6: stress test.**
   - Port stress_collection.py as `test_concurrent_mixed_workload_invariants`. Defaults: 3 s, 16 clients, CANCEL on, SEED fixed. Env `RAGGIO_STRESS_SECONDS` / `RAGGIO_STRESS_CLIENTS` enable long runs.
   - Invariants, checked after quiescence: I1 `len(index)` equals the count of indexed rows; I2 `indexed_counts` equals SQL GROUP BY; I3 every cached allowlist equals a fresh evaluation; I4 no exceptions.
   - Acceptance: passes on 3.12, 3.14 and 3.14t. Before the fixes it PASSED [WIN] on all three at S=15, so it guards against regressions rather than serving as a RED test. Long run: `RAGGIO_STRESS_SECONDS=60`.
7. **T1.7: comments.** store.py:587 ("GIL-atomic"), 474 (id cache), 1613-1614 (catalog), 1416 (still correct; could add "and FT builds run the Python rescore in parallel").
8. **T1.8 (optional, user decision): GC freeze.**
   - `Settings.gc_freeze` from env `RAGGIO_GC_FREEZE`, default "0" until the DGX A/B.
   - The lifespan calls `gc.freeze()` after `resume_pending()`.
   - Test: monkeypatch `gc.freeze` and assert it is called only when the env var is set.
9. **T1.9 (optional, user decision): CI.** Add a pytest workflow with a matrix of 3.12, 3.14 and 3.14t. The 3.14t job needs cargo, which GitHub ubuntu runners ship. Today the only workflow is `.github/workflows/docs.yml`.
10. **Local runs** (`--python 3.14t` builds turbovec from source, which needs cargo):
    ```
    uv run --python 3.12 pytest
    uv run --python 3.14 pytest
    uv run --python 3.14t pytest
    ```

### Stage 2: 3.14 GIL baseline image
- **T2.1** Dockerfile, first two lines:
  ```
  ARG PYTHON_VERSION=3.12
  FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim
  ```
  The rest is unchanged. uv.lock is universal (`requires-python >=3.12`); turbovec's abi3 wheel installs on 3.14 GIL; tests pass on 3.14 [WIN].
- Acceptance:
  - `podman build --build-arg PYTHON_VERSION=3.14 -t raggio:py314 .` succeeds on the DGX.
  - The default build stays byte-identical in behaviour (3.12).
  - The verify command in §8 prints `3.14.x gil True`.

### Stage 3: 3.14t experimental image (`Dockerfile.ft`, new file)
```dockerfile
# Experimental: python-build-standalone CPython via uv, free-threaded or GIL (control).
#   podman build -f Dockerfile.ft --build-arg PYTHON=3.14t    -t raggio:pbs314t .
#   podman build -f Dockerfile.ft --build-arg PYTHON=3.14+gil -t raggio:pbs314  .
ARG PYTHON=3.14t

FROM ghcr.io/astral-sh/uv:bookworm-slim AS build
ARG PYTHON
ENV UV_PYTHON_INSTALL_DIR=/python UV_PYTHON=${PYTHON} UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH=/root/.cargo/bin:$PATH
# turbovec 1.0.0 ships abi3 wheels only; free-threaded 3.14 can't use abi3, so uv
# builds its sdist (maturin, Rust >= 1.89). Drop once cp314t/abi3t wheels exist.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
RUN uv python install ${PYTHON}
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev
# GIL gate: an extension without free-threading support re-enables the GIL with a
# RuntimeWarning at import; -W error makes that a build failure. Never set PYTHON_GIL=0.
RUN .venv/bin/python -W error::RuntimeWarning -c "import sys, sqlite3, numpy, pydantic_core, turbovec, uvloop, httptools, websockets, watchfiles, yaml, raggio.app; ft = sys.argv[1].endswith('t'); gil = sys._is_gil_enabled(); print(sys.version, 'gil', gil, 'sqlite', sqlite3.sqlite_version); assert gil != ft, 'GIL state does not match the requested build'" "${PYTHON}"

FROM debian:bookworm-slim
COPY --from=build /python /python
COPY --from=build /app /app
WORKDIR /app
RUN useradd -m app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
```
- **Why the paths match.** `/python` must be the same path in both stages, because the venv's `python` symlink points into `/python/cpython-3.14.x+freethreaded-linux-aarch64-gnu/bin/`.
- **Pinning.** Consider `--default-toolchain 1.9x.y` instead of `stable`, and `PYTHON=3.14.7t` once the syntax is confirmed.
- **Fallback if the uvloop check fails.** Add `--loop asyncio` to the CMD of the FT image only.
- **After item 1 lands,** its crate builds in this same stage.
- Acceptance:
  - Both build args build on the DGX, and the check prints `gil False` for 3.14t and `gil True` for `3.14+gil`.
  - `podman logs` of a running container contains no "GIL" RuntimeWarning.
  - Build time and image size are recorded.
  - Optional: add a `docs/` note describing the image as experimental.

### Stage 4: DGX measurement protocol (for a human or executor on the DGX; agents have no access)
Before starting:
- Put the DGX on the commit that has stages 1–3.
- Stop vLLM (docs/benchmark-arxiv.md: "Stop the vLLM server first").
- Stop Weaviate (`podman stop bench-wv`).
- The client stays the same across images: `uv run python bench/bench.py` on the host (3.12).

```bash
cd ~/raggio && mkdir -p bench/ft
time podman build -t raggio:py312 .
time podman build --build-arg PYTHON_VERSION=3.14 -t raggio:py314 .
time podman build -f Dockerfile.ft --build-arg PYTHON=3.14+gil -t raggio:pbs314 .
time podman build -f Dockerfile.ft --build-arg PYTHON=3.14t -t raggio:pbs314t .
podman images | grep raggio > bench/ft/images.txt
for t in py312 py314 pbs314 pbs314t; do
  podman run --rm --entrypoint python raggio:$t -c "import sys,sqlite3,uvloop,httptools,turbovec,raggio.app; c=sqlite3.connect(':memory:'); print('$t', sys.version.split()[0], 'gil', getattr(sys,'_is_gil_enabled',lambda:True)(), 'sqlite', sqlite3.sqlite_version, [o for (o,) in c.execute('pragma compile_options') if o.startswith('THREADSAFE')])"
done | tee bench/ft/versions.txt
```

Why four images: the official-based images (py312, py314) probably link Debian bookworm's system SQLite (3.40.x, UNVERIFIED), while the python-build-standalone images bundle a newer SQLite (3.53.1 on Windows). **pbs314 (GIL) is the control that separates the FT effect from SQLite/compiler differences.** The FT verdict compares pbs314t with pbs314; the production verdict compares pbs314t with the best GIL image.

One run function:
```bash
run() {  # $1=image tag  $2=engine (raggio|raggio-ivf)  $3=concurrency  $4=rep  [$5=extra podman -e args]
  local id=$CORPUS-$1-$2-c$3-r$4${5:+-gc}
  podman rm -f bench-tv >/dev/null 2>&1
  podman run -d --name bench-tv --memory $MEM -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench $5 raggio:$1
  until curl -sf localhost:18000/healthz >/dev/null; do sleep 1; done
  rm -f bench/results-partial.json   # bench.py merges results per engine name across runs
  (while sleep 2; do echo "$(date +%T) $(podman stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' bench-tv)"; done) > bench/ft/$id.cpu &
  local mon=$!
  PYTHONUNBUFFERED=1 uv run python bench/bench.py --engine $2 --limit $N --concurrency $3 \
    --out bench/ft/$id.md --caps-note "raggio $MEM, image raggio:$1 $5" 2>&1 \
    | while IFS= read -r l; do echo "$(date +%T) $l"; done > bench/ft/$id.log
  kill $mon; cp bench/results-partial.json bench/ft/$id.json
  podman logs bench-tv 2>&1 | grep -iE "GIL|Traceback|Segmentation|Fatal" > bench/ft/$id.server.txt
}
```

**Corpus A: arxiv (the current `bench/corpus/` on the DGX).** `export CORPUS=arxiv N=2549119 MEM=4g`. If the fingerprint and count match, the existing bench-tv volume is reused (no re-ingest; data formats are identical across images). Flat runs come first, because the `raggio` engine drops any IVF index (`ensure_no_index`). Repeats run in reverse order to cancel drift.
```bash
for r in 1 2; do
  imgs="py312 py314 pbs314 pbs314t"; [ $r = 2 ] && imgs="pbs314t pbs314 py314 py312"
  for i in $imgs; do for c in 8 32; do run $i raggio $c $r; done; done
done
for r in 1 2; do for i in py312 py314 pbs314 pbs314t; do run $i raggio-ivf 8 $r; done; done
# optional: run $i raggio 16 1 ; GC A/B if T1.8 shipped:
run pbs314t raggio 32 1 "-e RAGGIO_GC_FREEZE=1"
```

**Corpus B: email.** Restore the email corpus files into `bench/corpus/`, as documented in docs/benchmark.md. Their presence on the DGX is UNVERIFIED. Then:
- `export CORPUS=email N=553015 MEM=1g`.
- The first `run py312 raggio 8 1` re-ingests (the fingerprint changes); this also re-measures ingest on 3.12.
- Then repeat the same loops.
- The email corpus's text is synthetic (a constant body plus path tokens, bench.py:44-51), so expect a small FT effect there. It mainly serves as a vector-path and regression check.

What to collect:
- Per run: the `.md` table (QPS, p50/p95/p99, recall@10, text-hit, memory, cold start), `.json`, `.cpu` and `.server.txt`.
- Summary: a sheet with the median of the two repeats, per image × phase. If the repeats differ by more than 10%, run a third.
- The `.cpu` log during the "hybrid concurrent" phase should show more than about 150% CPU on 3.14t and about 100–150% on GIL images.

Rough runtime: about 5 min per run, about 25 runs per corpus, so about 2–3 h per corpus (UNVERIFIED).

### Stage 5: adoption criteria
- **Gate 0: correctness (every image).**
  - The stage-1 tests and stress test pass on 3.12, 3.14 and 3.14t.
  - The build check passes, and `server.txt` contains no GIL warnings, tracebacks or crashes.
  - The bench records zero errors.
  - recall@10 and text-hit are within ±0.002 of py312.
- **3.14 GIL upgrade** (independent of FT). Adopt `PYTHON_VERSION=3.14` as the default if py314 is within −2% of py312 on every QPS row and p99 on both corpora, or better.
- **Adopt 3.14t** (Dockerfile.ft becomes the default, or the main Dockerfile moves to python-build-standalone 3.14t) if all of the following hold on arxiv at c=8 **and** c=32, flat and IVF:
  - hybrid concurrent QPS is **at least +50%** over the best GIL image, and hybrid p99 is no worse;
  - vector serial/concurrent/filtered QPS and p50 regress by **5% or less**;
  - hybrid serial regresses by 10% or less (documented single-thread penalty);
  - peak memory rises by 15% or less and stays under the 4 GiB cap (1 GiB for email);
  - cold start rises by 20% or less;
  - the email corpus shows no row worse than −5%.
- **Between +25% and +50%:** keep Dockerfile.ft opt-in and re-measure after item 1 merges, or once turbovec publishes cp314t or abi3t wheels.
- **Below +25%:** don't adopt. Keep Dockerfile.ft as documented-experimental or delete it (user decision).
- **In every case,** the stage-1 fixes ship regardless: R1 immediately, as a security fix.

## 8. Risks
- **Maturity of the FT ecosystem.** uvloop, httptools, websockets and watchfiles on Linux aarch64 are only verified by the build check. Fallback: `--loop asyncio`.
- **Build-time Rust.** `curl | sh` rustup is a supply-chain and reproducibility concern (pin the toolchain). It also adds about 1–3 min to the build.
- **Free-threaded GC pauses.** Stop-the-world, full-heap collections. Mitigations: `gc.freeze()`, and fewer allocations in the hot path (item 1 removes most of them).
- **Single-thread penalty.** About 5–10% per the docs; the laptop showed about 10% on the text leg at one thread.
- **Memory overhead** of per-object FT headers and mimalloc under the 1 GiB email cap (UNVERIFIED). Most memory is turbovec and SQLite, not Python objects.
- **Reference-count and shared-object contention at 20 cores** (for example the shared `_df_cache` dict or interned strings). Not visible at 6 threads.
- **Timing-based stress tests can be flaky.** Keep a fixed seed, check invariants only after quiescence, and run the thread hammers in a subprocess so a segfault becomes a failure rather than a crash.
- **Orphaned to_thread work stays a design hazard** on every build. The asyncio RW lock does not cover threads whose awaiting task is gone. R2 and R2b are the known instances; review new caches against this pattern.

## User decisions
- Catalog fix style (R1): take _catalog_lock in get_config and list_collections (minimal and robust; verified), or make require_collection async def (verified; also saves one threadpool hop per request, but relies on every catalog access staying on the loop). Recommended: the lock now, and optionally the async dependency as well.
- Ship R1 immediately as its own security fix on the 3.12 image, ahead of the FT work, and decide whether to issue a security note for existing deployments that use collection keys. Recommended: yes, a standalone PR right away.
- gc.freeze() after lifespan startup: add it behind an env var RAGGIO_GC_FREEZE, defaulting to off until the DGX A/B. Recommended: add the knob in stage 1 and turn it on by default if pbs314t at c=32 gains 5% or more hybrid QPS with no memory regression.
- Timing of the FT adoption decision: only after item 1 (native rescore) merges, or now. Recommended: build the images and run a baseline now if the DGX is free, but make the adopt/no-adopt call on post-item-1 code.
- Base image for the 3.14 GIL upgrade: the official-based ghcr uv:python3.14-bookworm-slim (py314) or python-build-standalone for both builds. Recommended: official for the production GIL image, python-build-standalone only for the FT image, with pbs314 as the measurement control.
- Rust toolchain in the Dockerfile.ft build stage (pinned version) versus waiting for turbovec to publish cp314t or abi3t wheels. Recommended: Rust in the build stage for the experiment, and ask turbovec upstream for cp314t wheels (nothing was posted by this agent).
- Stress test placement: a short run (about 3 s) in the default pytest suite plus an env-driven long run, or marked slow and opt-in. Recommended: short by default.
- Add a CI test workflow with a 3.12 / 3.14 / 3.14t matrix (none exists today; only docs.yml). Recommended: yes, once stage 1 lands.
- Eviction semantics: should get_document, list_records and job_status take the collection read lock, so eviction or stop waits for them instead of their clients seeing 500s? Recommended: yes for list_records and get_document; this is a lifecycle issue, not FT-specific.
- Adoption thresholds: at least +50% hybrid concurrent QPS versus the best GIL image, with 5% or less vector regression, 10% or less hybrid-serial regression, and 15% or less memory increase. Also what happens to Dockerfile.ft if the criteria fail. Recommended: these thresholds, and keep Dockerfile.ft as documented-experimental rather than deleting it.

## Unverified
- Behaviour on DGX aarch64/Linux: the turbovec sdist build inside Dockerfile.ft (rustup plus maturin) and the item-1 crate build were verified only on Windows x86-64.
- uvloop 0.22.1 keeping the GIL disabled at runtime: source evidence only (the Cython directive freethreading_compatible=True in loop.pyx); it can't be imported on Windows. The Dockerfile.ft build check is the verification.
- httptools, websockets, watchfiles and pyyaml cp314t manylinux aarch64 wheels keeping the GIL off: verified only with the win_amd64 wheels.
- SQLite versions in the images: official python:3.12/3.14 bookworm images are believed to link Debian's system SQLite 3.40.x, and the python-build-standalone Linux builds to bundle 3.53.x with THREADSAFE=1. Only Windows builds were checked; the protocol's verify command prints the versions.
- The uv request syntax '3.14+gil' (and a pinned '3.14.7t') inside the ghcr uv:bookworm-slim build container: taken from the uv docs and changelog (#16142), not exercised on Linux.
- Expected DGX gains: hybrid concurrent QPS uplift, lower p99 from removing the GIL convoy, and scaling beyond 6 cores (reference-count or shared-dict contention at 20 cores). All scaling numbers are from the 6-core Windows laptop.
- Free-threaded memory overhead staying within the 4 GiB (arxiv) and 1 GiB (email) container caps.
- Whether closing a read connection while another thread is mid-query (R3) can crash on 3.14t: not observed in the STOP=1 stress runs, but CPython #146471 shows shared-object misuse can segfault.
- Production likelihood of the stale allowlist (R2): it needs orphaned to_thread work, from task cancellation or a hybrid gather sibling raising. uvicorn/Starlette do not cancel handlers on client disconnect, so it is expected to be rare. The repro forces the window.
- R2b (IVF _id_cache stale ids) and A1 (delete_collection vs touch resurrection) come from code reading only; no repro was built.
- Whether the email-corpus files still exist on the DGX for the corpus-B runs, and the rough protocol runtime (about 5 min per run, 2–3 h per corpus).
- The default asyncio to_thread executor size inside the container (min(32, cpu_count+4), so about 24 on 20 cores): not measured there.
