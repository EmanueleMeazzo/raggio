# Ingest Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make raggio's ingest path fast and failure-safe without any API change.
- **D1:** a binary side-table job journal; payload encode/decode, flattening and matrix building off the event loop; a set-based upsert that rolls back cleanly and publishes `indexed_counts` after commit; a bounded vacuum policy; and `bench/ingest_probe.py`.
- **D2:** a batched index sync that closes on N jobs, T ms, an idle queue, an index-job barrier or `stop()`. It has done-after-sync semantics, a claim cursor, a removal cap, retry on sync failure, and a flush in `stop()`. If that flush's sync fails, `stop()` logs it, finishes no job and still completes the shutdown without raising (spec §3.1 D2). A dead worker's exception and a finish that fails after that sync landed are logged the same way (spec §3.1 D3). A failure in any of `stop()`'s three close steps (the SQLite connections, the embedder's HTTP client, F's shard pool) is logged too, and the steps after it still run (spec §3.1 D4). `stop()` therefore never raises an `Exception` from its worker, its final sync, a finish or a close step, and `shutdown()` reaches every collection and the catalog. `CancelledError` and other `BaseException`s that are not `Exception`s still propagate.

Working notes for this plan: outline `D:/DEV/_scratch/plan-d/outline.md`, rationale `D:/DEV/_scratch/plan-d/design.md`.

**Architecture:**
- **D1, journal.** `enqueue` writes the `jobs` row (with `payload` NULL) and a `job_payloads(job_id INTEGER PRIMARY KEY, data BLOB)` row in one transaction, so the claim UPDATE no longer rewrites a multi-megabyte overflow chain. The payload is encoded before `db_lock` is taken and decoded after it is released, in the same worker thread. Legacy TEXT-JSON rows still replay.
- **D1, upsert.** D1 keeps one claim and one sync per job, but every bulky step (encode, decode, flatten, matrix build) runs in the threads that already do the SQLite work. Index removal moves out of the upsert transaction into a new `_unindex`, so a failed upsert is a pure database rollback. The one `indexed_counts` rebind per job happens after the commit, still under `db_lock` and still copy-on-write.
- **D1, vacuum.** No full `incremental_vacuum` runs per job while jobs are open. The freelist is bounded by chunked vacuums, and a full vacuum runs when the queue is idle.
- **D2, batching.** The per-job `lock.write()` sections stay. Instead of syncing, each job queues `(job_id, status, error)` in `Collection._unsynced`. `_flush_unsynced` then syncs once and finishes each job. A cursor (`id > ?`) stops the worker from re-claiming its own `processing` jobs. A removal counter forces a sync before one would carry more than `SYNC_MAX_REMOVALS` removals across jobs.

**Tech Stack:** Python >=3.12 stdlib (`struct`, `json`, `sqlite3`, `asyncio`, `threading`), numpy, turbovec 1.0.0 (`IdMapIndex`, and Plan F's `_IvfIndex` dirty-shard sync), FastAPI (untouched), pytest via `uv run pytest -q` (no pytest-asyncio: tests drive async code with `asyncio.run`), uv. Tests live in `tests/test_ingest_path.py`, one `# ---- <topic>` section per task.

**Spec:** `docs/superpowers/specs/2026-09-28-raggio-performance-design.md`:
- §2: order 4 (phase D1) and order 7 (phase D2), each its own PR.
- §3 D1 (ADR 0001 addendum, the 0001:50 lever note) and D8 (batched-sync failure policy).
- §4.1 rows: `Collection.stop()`, `indexed_counts`, `open_meta_db` indexes, the job journal / `enqueue` / `_run_worker` / `_upsert_rows` / `_sync_index` batching row (owner D), and `_IvfIndex` (F's dirty-shard API).
- §4.2: the `SYNC_BATCH_JOBS` / `SYNC_BATCH_MS` knobs. §4.3: invariants. §6: DGX protocol. §7: "D" acceptance.

Research: `docs/superpowers/research/2026-09-28-item2a-ingest-path.md` (primary), `-critic.md`, `-dgx-baseline.md`.

**Validation status:** nothing in this plan was executed. It was written without a sandbox, and the shell was unavailable. Every test body, expected failure and expected pass count was derived by reading the spec, the research, the other plans and the code at 19a8a2e. So the first execution step of every task runs the new tests and confirms the stated FAIL message before any implementation. If the message differs, stop and reconcile the plan with the tree before going on.

**Phases:** D1 lands after Plan C (spec order 4); D2 after Plan F (spec order 7).
- D1: branch `ingest-path-d1`, base = `main` after Plans A, B and C. Tasks 1-6.
- D2: branch `ingest-path-d2`, base = `main` after D1, E and F. Tasks 7-10. D2 re-derives D1's code from D1's commits and anchors every edit on quoted text, never on line numbers.

**Suite counts (N0):**
- **N0** is the full-suite pass count of `uv run pytest -q` on D1's branch base, that is, after Plan C. Expected: `253 passed, 1 warning`. That is Plan A's 162+9 = 171 + Plan B's 17 = 188, plus Plan C's 65 (its Task 12 DGX acceptance test is the 65th). It is 252 if C's Task 12 test did not land.
- The count also assumes Git Bash on the host (Plan A's 21 `needs_bash` tests, 14 from its Task 3, 6 from its Task 4 and 1 from its Task 5, skip without it) and `uv sync --group bench` (tests that import `bench.py` `importorskip("orjson")`).
- Record N0 on the branch base before Task 1. Do not hard-code 253. Every D1 task states its expected total as `N0 + k`.
- **N0'** is the same count re-recorded on D2's branch base (after D1, E and F) before Task 7. Every D2 task states `N0' + k`.
- The one pre-existing Starlette deprecation warning stays throughout.

## Global Constraints

Spec, quoted verbatim:
- §2: "Every plan ships as its own branch/PR. It must leave `uv run pytest -q` green and must not depend on a later plan." D1 and D2 are two PRs. D1 must not assume E, F or D2; D2 builds on the code as D1, E and F leave it.
- §4.3: "Single writer: every write transaction runs wholly inside `db_lock`, in a worker thread, never on the event loop."
- §4.3: "auto_vacuum: `PRAGMA auto_vacuum=INCREMENTAL` comes before `journal_mode=WAL`, and `incremental_vacuum` is drained with `.fetchall()`."
- §4.3: "Job journal: a job is `done` only after the index sync covering it."
- §4.3: "Memory: the cgroup headroom guard refuses the job rather than letting the container OOM." Keep the `_require_headroom` guard where it is: the index builds in `_attach_index` / `_detach_index` call it before they allocate (Plan C adjusts the estimate), and D touches neither method. Keep C's per-index-job `malloc_trim(0)` `finally` (D13) at the head of `_process_job`.
- §4.1: "D's claim query, `resume_pending` and `pending_jobs` keep the literal predicate `status IN ('pending','processing')`." This is the text C's partial `idx_jobs_open` matches. D1's `_CLAIM_SQL` is C's claim text byte for byte. D2 adds `INDEXED BY idx_jobs_open` after the table name and `AND id > ?` after the predicate; the predicate itself is unchanged.
- §4.1, `Collection.stop()`: "D2 adds its sync flush inside that same critical section, before the connections close." That is after the worker cancel and before `_closed = True` / `_close_conns`; F's `_shard_pool.close` stays last, outside the lock.
- §4.1, `indexed_counts`: "D moves the update after commit, keeping copy-on-write."
- §3 D8: "A job stays `processing`, payload kept, until the index sync covering it lands. Batches are bounded by N jobs or T ms. Flush in `stop()`. Crashes recover through the existing replay."
- §3.1 D1: "The seed-7 tuning may change the `SYNC_BATCH_JOBS=8` / `SYNC_BATCH_MS=1000` defaults. Seed 42 confirms the chosen values, and the PR states them." Task 10 Step 10 moves them, the publish runs at them, and Step 19's report states them for the PR.
- §3.1 D2: "If the final sync in `stop()` fails, it is logged and shutdown completes. The jobs stay `processing` with their payloads kept, and the startup replay recovers them (D8). Nothing is raised." Task 9 catches that `Exception` inside `_sync_and_finish`, so `stop()` keeps its order (`lock.write()`, worker cancel, sync, `_closed = True`, `_close_conns` under `db_lock`, then the embedder's `aclose()` and F's `_shard_pool.close`) and differs from B and F only by that swapped line, D3's `except Exception` clause and D4's `close_step` wrapping (next two bullets). `CancelledError` and `KeyboardInterrupt` still propagate.
- §3.1 D3: "Follow-up to D2: `stop()` never raises, so shutdown always completes and closes every collection. Two more errors are logged and not raised: a dead worker's exception, and a failure to finish jobs after the final sync succeeded. In both cases the affected jobs stay `processing` and the startup replay (D8) recovers them." Task 9 logs the dead worker's exception in an `except Exception` clause on `stop()`'s await of the worker, and a failed finish inside `_sync_and_finish`, both through `_log.exception` with the D2 path's `(name, jobs)` args. `stop()` keeps the close order above. Only `Exception` is caught.
- §3.1 D4: "Follow-up to D3: every close step in `stop()` logs its own `Exception` and moves on to the next. This covers the embedder's HTTP client, the SQLite connections (`_close_conns`) and F's shard pool. So `shutdown()` always reaches every collection and the catalog, and each failure is an ERROR log with its traceback. `CancelledError` and other `BaseException`s are still not caught." Task 9 awaits each of the three steps through `close_step`, a helper local to `stop()` that logs an `Exception` through `_log.exception` with the args `(collection name, step)` and returns. The order stays B's and F's: `_closed = True`, then `_close_conns` (under `db_lock`, inside `lock.write()`), then the embedder's `aclose()`, then F's `_shard_pool.close`, last and outside the lock.
- §4.2: "Deployment knobs: unprefixed environment variables read in `raggio.config.Settings`, following the existing style, and passed down through `CollectionManager` into `Collection`. Algorithm constants stay module-level in `store.py`, where tests monkeypatch them." D2 adds the knobs `SYNC_BATCH_JOBS` (default 8) and `SYNC_BATCH_MS` (default 1000). The constants `VACUUM_FREELIST_PAGES`, `VACUUM_CHUNK_PAGES`, `SYNC_MAX_REMOVALS`, `SYNC_RETRY_MIN_S` and `SYNC_RETRY_MAX_S` stay module-level in `store.py`.
- §6: "Any DGX run over about 5 minutes needs the user's explicit go-ahead in the session, with a time cap." / "Tunables (pool size, batch N/T, candidate depth) are chosen on an independent sample (seed 7 ...) and published on seed 42." / "Never pass `--reingest` except in an ingest-measurement run, which comes last because it rewrites and compacts files."
- §7: "D: ingest vec/s beyond the noise band on the IVF and flat paths, and no lost or unsynced job under the crash/replay tests." D1 compares against `main` after C; D2 against `main` after D1, E and F (the re-baselined preceding main). D1's gates need `better` on both the flat and the IVF probe. D2's gates need `better` on IVF, but only `better` or `within band` on flat and on the bench reingest. The reason is item2a: a flat append-only sync is 7-9 ms, 2-5% of a job, so batching has almost nothing to save there. This reading of §7 for D2 is flagged for the user in the Self-Review.

Plan rules:
- Base and branch: D1 runs `git switch -c ingest-path-d1` on `main` after A, B and C. D2 runs `git switch -c ingest-path-d2` on `main` after D1, E and F. Line numbers quoted in this plan are orientation only; anchor every edit on the quoted text.
- Python: >=3.12 via `uv run`. No new dependency, and no `pyproject.toml` or `uv.lock` change.
- Files touched, D1: `src/raggio/store.py`, `tests/test_ingest_path.py` (new), `bench/ingest_probe.py` (new), `docs/storage.md`, `docs/adr/0001-performance-optimization-decisions.md`.
- Files touched, D2: `src/raggio/store.py`, `src/raggio/config.py`, `tests/test_ingest_path.py`, `bench/ingest_probe.py`, `docs/storage.md`, the same ADR, `README.md` and `docs/getting-started.md`.
- Never touched: `app.py`, existing test modules (spec §4.1: plans do not reorganize them), `bench/bench.py`, `bench/dgx/*.sh`. The DGX drivers live outside the repo in `D:/DEV/_scratch/dgx/planD/` and are never committed.
- Plan A: no `src/**/*.py` may contain the substring "orjson". Runtime JSON stays stdlib (spec D5).
- Pins from Plans B, C and F that stay green unchanged:
  - no `await` inside any `with ...db_lock` block (B's AST test);
  - exactly one `indexed_counts` rebind per job and per delete, under `db_lock`, never mutated in place (B's tripwire and `__setattr__` spy tests);
  - the names and arity of `_finish_job(job_id, status, error)` and `_upsert_rows(rows, mat)`;
  - `col._claim_next()[0] == 51` in C's `test_open_job_queries_use_the_partial_index`, so `_claim_next` stays callable with no argument, and a legacy `"{}"` TEXT payload with no `job_payloads` row still claims;
  - `enqueue(payload_dict)` and `_process_job(payload_dict)` keep the call shapes C's tests use;
  - the three `self._allow_cache.clear()` call sites keep that exact text; they may move, but are never merged or renamed (F finds them by text and turns each into `self._invalidate_allow()`, which D2 does not touch);
  - F's `test_reupsert_that_moves_a_row_syncs_both_shards` and F's clean-stop test `test_reload_then_stop_writes_no_shards`.
- Removal counting: `_IvfIndex.remove` returns `None`, before and after F. The removal count behind `SYNC_MAX_REMOVALS` therefore comes from the rows D deletes or replaces, never from `index.remove`'s return value.
- Lock kinds and order: `db_lock` is a non-reentrant `threading.Lock`, never held across an `await`. Lock order is `lock.write()` before `db_lock`.
- Connections: no new cross-thread `meta.db` `sqlite3.connect` call site (Plan G adds `cached_statements` to the existing two).
- ADR: D1 appends `## Addendum 2026-09 — ingest path` to ADR 0001, after C's `## Addendum 2026-09 — cold start and loop hygiene`. D2 adds its sections inside that addendum and updates the 0001:50 lever note (spec D1).
- Downgrade: a pre-D image cannot read binary journal rows. "Drain the ingest queue before downgrading" is documented in the ADR addendum and `docs/storage.md`, not enforced.
- Encoding: new comments, `bench/ingest_probe.py` and the whole test module are ASCII. `store.py` and the ADR stay UTF-8 (they contain em dashes); edit them with a UTF-8 aware editor, never a Windows cp1252 shell redirect.
- Commit trailer: every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- DGX (Tasks 6 and 10):
  - step 1 asks the user for a run window and a time cap; if the user declines, the ADR results stay `Pending:`;
  - every ssh/scp is written out in full with `-F "$SSH_CONFIG" -o BatchMode=yes` (`SSH_CONFIG` is the maintainer's local ssh config for gn100: set in the environment, never committed);
  - `podman rm -f` names only `bench-tv`, never `bench-wv`; never `pkill -f`; long runs launch with `setsid -f` into a log file, which is then polled;
  - every arm runs on a fresh container, and arms are interleaved;
  - `--reingest` appears only in the ingest runs, last in the session. It leaves the `bench-tv` volume flat with a drained queue.

## Review Focus

Five failure modes, most damaging first. Each is pinned by a test in the task that owns it. Reviewers should read these tests first, then the code they guard.

1. **RF1: a job is `done` before the index sync covering it, or a crash loses a job.**
   - Implied by §4.3 (Job journal) and §3 D8. `_reconcile_ghosts` only evicts; it never re-adds a DB row that is missing from the index. So a job finished before its sync, followed by a crash, leaves `indexed=1` rows with no vector, and nothing detects it.
   - Pins: `test_no_job_is_done_before_its_covering_sync` and `test_crash_window_replays_without_loss` (Task 8). The crash test runs at `mid_batch`, `before_sync` and `after_sync_before_finish`. Each snapshot must reopen, replay and drain to the `sync_batch_jobs=1` reference fingerprint, with `len(index)` equal to the indexed row count.
2. **RF2: a failed upsert leaves a half-applied state.**
   - Implied by §4.3 (single writer) and §4.1 (`indexed_counts` after commit). At base, index removals and count updates run before the commit, with no rollback. The worker's `_finish_job('error')` commit then persists the partial upsert under a terminal job that never replays.
   - Pin: `test_upsert_failure_rolls_back_and_keeps_index` (Task 3). A TEMP trigger on `vecs` aborts the re-upsert. Records, vecs, index ids and `indexed_counts` must be unchanged, the job must be `error`, and the next job's commit must not persist partial rows.
3. **RF3: a legacy TEXT-JSON job is lost or reordered after an in-place upgrade.**
   - Implied by §4.1 (the job journal row: "legacy TEXT JSON accepted on replay"). A volume upgraded with open jobs holds TEXT payloads next to new binary `job_payloads` rows.
   - Pins: `test_mixed_legacy_and_binary_queue_in_order` (Task 2), plus the base `test_persistence_and_crash_replay` left unchanged.
4. **RF4: `stop()` loses the open batch or flushes outside B's critical section.**
   - Implied by §4.1 (`Collection.stop()` row) and §3 D8 ("Flush in `stop()`"). The flush must sync and finish the open batch inside `lock.write()`, after the worker cancel and before `_close_conns`.
   - Spec §3.1 D2: if that sync fails, `stop()` logs it, finishes no job and still closes the collection without raising. The batch's jobs keep `'processing'` and their payloads, and the next open replays them.
   - Spec §3.1 D3: a dead worker's exception and a finish that fails after the sync landed are logged the same way, `stop()` still closes, and the jobs left unfinished replay on the next open.
   - Spec §3.1 D4: each close step (the SQLite connections, the embedder's HTTP client, F's shard pool) logs its own `Exception` with its traceback, and the steps after it still run. With D2 and D3, no `Exception` from the worker, the final sync, a finish or a close step leaves `stop()`, so `shutdown()` reaches every collection and the catalog. `CancelledError` and other non-`Exception` `BaseException`s still propagate.
   - Pins: `test_stop_flushes_the_open_batch`, `test_stop_with_a_failing_sync_logs_it_and_still_closes`, `test_a_failed_stop_sync_replays_on_the_next_open`, `test_stop_logs_a_dead_workers_exception_and_still_closes`, `test_a_failed_finish_in_stop_is_logged_and_replays`, `test_a_failing_embedder_close_is_logged_and_stop_goes_on`, `test_a_failing_close_conns_is_logged_and_the_later_steps_run`, `test_a_failing_shard_pool_close_is_logged` and `test_shutdown_closes_every_collection_after_a_failed_stop` (Task 9), plus B's `test_stop_closes_connections_inside_the_write_lock_and_under_db_lock` (Plan B, Task 3), which stays green.
5. **RF5: one sync carries removals from several jobs past the full-rewrite cliff.**
   - Implied by item2a: turbovec full-rewrites the index file once a header holds more than 1024 ops, which is extrapolated at about 20 s under `lock.write` at 2.55M rows.
   - Pin: `test_removal_cap_syncs_before_crossing_the_cap` (Task 7). No sync may carry more than `SYNC_MAX_REMOVALS = 512` removals accumulated across jobs. The check runs before `_unindex`: if the counter is non-zero and adding this job's replaced rows would pass the cap, sync first. A single job that alone exceeds the cap is not split, as at base.

---

## File Structure

- `src/raggio/store.py` (modify), D1:
  - Task 1: `import struct`; `_JOB_MAGIC = b"RGJ\x01"` and `_JOB_HEADER = struct.Struct("<4sIII")`; `_encode_payload(payload) -> bytes` and `_decode_payload(data) -> dict`, placed after `_normalize`. `open_meta_db` gains `CREATE TABLE IF NOT EXISTS job_payloads(job_id INTEGER PRIMARY KEY, data BLOB)`, after the `jobs` table.
  - Task 2: `enqueue` hands the dict to `_enqueue_row`, which encodes before taking `db_lock`. Also `_CLAIM_SQL` (C's text byte for byte), `_claim_next` (decodes after releasing `db_lock`), `_run_worker`, and `_finish_job`, which deletes the `job_payloads` row on `done`.
  - Task 3: module-level `_payload_rows` and `_rows_matrix`; `_process_job`; `_upsert_rows` (set-based, rolls back, returns `(ids, fresh, replaced)`); a new `_unindex`; `_delete_doc_rows`.
  - Task 4: `VACUUM_FREELIST_PAGES = 16_384` and `VACUUM_CHUNK_PAGES = 2_048`; `_vacuum_after_finish`, called from `_finish_job`.
- `src/raggio/store.py` (modify), D2:
  - Task 7: `SYNC_MAX_REMOVALS = 512`. `Collection.__init__` gains `sync_batch_jobs: int = 8, sync_batch_ms: float = 1000.0` after `ivf_search_threads`, and `CollectionManager._construct` passes both. Also `_CLAIM_SQL` gains `INDEXED BY idx_jobs_open` and `AND id > ?`, `_claim_next(after=0)`, `_process_job(payload, *, sync=True)`, and `Collection._removed_since_sync`, counted in `_unindex` and reset in `_sync_index`.
  - Task 8: `Collection._unsynced` and `_batch_t0`; a `_run_worker` rewrite with a local claim cursor; a new `_flush_unsynced`.
  - Task 9: `SYNC_RETRY_MIN_S = 1.0` and `SYNC_RETRY_MAX_S = 30.0`; retry with doubling backoff in `_flush_unsynced`; a new `_sync_and_finish`, which logs a failed sync and returns without finishing any job, so `stop()` still closes (spec §3.1 D2), and logs a finish that fails after the sync landed the same way (spec §3.1 D3). In `stop()`, the `_sync_index` line is swapped, the worker await gains an `except Exception` clause that logs a dead worker's exception (spec §3.1 D3), and the three close steps run through a local `close_step` helper that logs each one's `Exception` and goes on (spec §3.1 D4).
- `src/raggio/config.py` (modify, Task 7): `Settings.sync_batch_jobs` (`SYNC_BATCH_JOBS`, default 8) and `Settings.sync_batch_ms` (`SYNC_BATCH_MS`, default 1000), appended after F's `ivf_search_threads` line. `ValueError` if jobs < 1 or ms < 0.
- `tests/test_ingest_path.py` (create in Task 1, append in every later task): the shared helpers `make_collection`, `vec`, `docs_payload` and `drain` sit at the top, then one `# ---- <topic>` section per task, in task order.
- `bench/ingest_probe.py` (create in Task 5; Task 7 adds `--batch-jobs` / `--batch-ms`): an in-process drain benchmark for flat and IVF ingest that also runs in the base tree.
- `docs/storage.md` (modify): the job journal paragraph (Task 2), one vacuum sentence (Task 4), batched sync and the knobs (Task 10).
- `docs/adr/0001-performance-optimization-decisions.md` (modify): Task 6 appends `## Addendum 2026-09 — ingest path` with its D1 results. Task 10 updates the line-50 lever note and adds `### Batched sync (D2)` and `### Results — DGX A/B (D2)` inside that addendum.
- `README.md` and `docs/getting-started.md` (modify, Task 10): environment rows for `SYNC_BATCH_JOBS` and `SYNC_BATCH_MS`, appended last.
- Outside the repo, never committed: `D:/DEV/_scratch/dgx/planD/` holds `planD-ab.sh`, `planD-build.sh`, `planD-summary.py` and `planD-check.py` (Task 6). Task 10 adds the `d2tune` and `d2` modes to `planD-ab.sh` and creates `planD-d2-summary.py` and `planD-d2-check.py`.

---

## Phase D1 (branch `ingest-path-d1`, base = `main` after Plans A, B and C)

Before Task 1: `git switch main && git pull && git switch -c ingest-path-d1`, then run `uv run pytest -q` and write down the pass count as **N0** (expected `253 passed, 1 warning`; see "Suite counts" above).

### Task 1: Binary payload codec and job_payloads table

Goal: a stdlib + numpy codec for job payloads and an additive `job_payloads` side table. Nothing calls either yet, so no runtime behaviour changes in this task; Task 2 wires them in.

**Files:**
- Modify `src/raggio/store.py` (line numbers are base 19a8a2e, orientation only; anchor on the quoted text):
  - imports (base lines 1-21): add `import struct` directly below `import sqlite3` (base line 8; after Plan C the next line is C's `import sys`).
  - after `_normalize` (base lines 129-133, unchanged by A, B and C): new module constants `_JOB_MAGIC = b"RGJ\x01"` and `_JOB_HEADER = struct.Struct("<4sIII")`, and new module functions `_encode_payload` and `_decode_payload`. The constants sit with the codec rather than next to the IVF constants, because Plan C added its own constant blocks after `IVF_BUILD_BLOCK`.
  - `open_meta_db` (base lines 273-320; the `jobs` table is base lines 306-313): append `CREATE TABLE IF NOT EXISTS job_payloads(job_id INTEGER PRIMARY KEY, data BLOB);` to the `executescript` right after the `jobs` table, so it runs before Plan C's `_ensure_indexes(db, str(path))` call.
- Create `tests/test_ingest_path.py`: module docstring, imports, the shared helpers `make_collection`, `vec`, `docs_payload` and `drain` (used from Task 2 on), and the section `# ---- binary job journal: codec`.
- Test: `uv run pytest tests/test_ingest_path.py -v`.

**Interfaces:**
- Consumes:
  - Plan C's `open_meta_db(path: Path, tokenizer: str = "unicode61") -> sqlite3.Connection`, whose `executescript` ends with the `jobs` table and is followed by `_ensure_indexes(db, str(path))` and `_ensure_fts(db, tokenizer)`.
  - `_normalize(mat: np.ndarray) -> np.ndarray` (unchanged; only used as the insertion anchor).
  - `Collection(cfg: CollectionConfig, directory: Path, embedder_factory, set_index_config=None)` and `CollectionConfig(name, dim, bit_width, model, base_url, key_hash, tokenizer="unicode61", index_config=None)` (test helpers only).
  - `Collection.pending_jobs() -> int` and `Collection._worker: asyncio.Task | None` (the `drain` helper only).
- Produces:
  - `_JOB_MAGIC: bytes = b"RGJ\x01"` and `_JOB_HEADER: struct.Struct = struct.Struct("<4sIII")` (magic, json_len, n_vecs, dim; 16 bytes, little-endian).
  - `_encode_payload(payload: dict) -> bytes`. Layout: `_JOB_HEADER` + UTF-8 `json.dumps` of the payload, with every non-None `vector` of a summary or chunk replaced by its row index + `n_vecs * dim` little-endian float32 values (`<f4`). A `None` vector stays `null` ("embed this"). Index-op payloads (`{"op": ...}`) encode `n_vecs = 0, dim = 0`. It never mutates its input. Vectors that are not one rectangular float matrix (ragged lengths, scalars, nested lists) raise `ValueError`.
  - `_decode_payload(data: bytes) -> dict`: the inverse. Each decoded `vector` is a row of one read-only `np.frombuffer(data, dtype="<f4", ...).reshape(n_vecs, dim)` matrix (a view, not a copy). Every malformed input raises `ValueError`: a short buffer, bad magic, a length that disagrees with the header, bad JSON or UTF-8, a non-dict top level, or a vector index out of range. Task 2 relies on this to turn a bad row into an `error` job.
  - The table `job_payloads(job_id INTEGER PRIMARY KEY, data BLOB)`, created by every `open_meta_db` call, fresh or existing. Existing `jobs` rows are untouched.
  - Test helpers in `tests/test_ingest_path.py`, which later tasks reuse unchanged:
    - `make_collection(tmp_path, dim=8, **kw) -> Collection` (embedder factory `lambda: None`, so every ingest payload must carry vectors; `**kw` goes to `Collection(...)`);
    - `vec(seed, dim=8) -> list[float]` (seeded float64 standard normal, never all zero);
    - `docs_payload(ids, dim=8, with_vectors=True) -> dict` (`IngestIn.model_dump()` shape: per id `i`, document `d{i}` with `summary: None` and one chunk `{"text": f"text {i}", "vector": vec(i, dim) or None, "metadata": {"i": i}, "id": f"c{i}", "position": None}`, so it makes `len(ids)` records);
    - `async drain(col, timeout=30) -> None` (waits for `col.pending_jobs() == 0`; re-raises the exception that killed the worker; fails the test after `timeout` seconds).

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_path.py`. The file is ASCII only: build non-ASCII test text with `chr()`. First the module head and the shared helpers:

```python
"""Plan D (ingest path): the binary job journal, payload work off the event loop,
failure-safe upserts, the vacuum policy, the ingest probe and the batched index sync.
One `# ---- <topic>` section per plan task, in task order."""
import asyncio
import copy
import json
import sqlite3
import struct
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, "src")
import raggio.store as store  # noqa: E402
from raggio.store import Collection, CollectionConfig, open_meta_db  # noqa: E402


def make_collection(tmp_path, dim=8, **kw):
    """A collection without an embedder: every ingest payload must carry vectors.
    Extra keywords go to Collection(...)."""
    return Collection(
        CollectionConfig("t", dim, 4, None, None, None), Path(tmp_path), lambda: None, **kw
    )


def vec(seed, dim=8):
    """A seeded float64 vector (standard normal, so never all zero)."""
    return np.random.default_rng(seed).standard_normal(dim).tolist()


def docs_payload(ids, dim=8, with_vectors=True):
    """An ingest payload in IngestIn.model_dump() shape: one document per id, each with
    one chunk, so it makes len(ids) records. with_vectors=False leaves every vector
    None ("embed this")."""
    return {"documents": [
        {"doc_id": f"d{i}", "summary": None, "chunks": [{
            "text": f"text {i}", "vector": vec(i, dim) if with_vectors else None,
            "metadata": {"i": i}, "id": f"c{i}", "position": None,
        }]}
        for i in ids
    ]}


async def drain(col, timeout=30):
    """Wait until no job is open. Re-raises whatever killed the worker, and fails the
    test after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while col.pending_jobs():
        if col._worker is not None and col._worker.done():
            col._worker.result()  # re-raises the worker's exception, if any
            pytest.fail(f"worker exited with {col.pending_jobs()} jobs open")
        assert time.monotonic() < deadline, f"{col.pending_jobs()} jobs open after {timeout} s"
        await asyncio.sleep(0.02)
```

Then the Task 1 section, appended below the helpers:

```python
# ---- binary job journal: codec


def mixed_payload():
    """Every field _process_job's flatten step reads: a summary with a vector, a chunk
    whose vector is None (embed this), an explicit position, nested and None metadata,
    non-ASCII text and awkward float64 values (dim 8)."""
    p = docs_payload([1, 2, 3])
    p["documents"][0]["summary"] = {
        "text": "summary " + chr(0xE9) + chr(0x4E2D), "vector": vec(100), "metadata": {"k": [1, "x"]},
    }
    p["documents"][1]["chunks"].append(
        {"text": "no vector", "vector": None, "metadata": None, "id": "c2b", "position": 7}
    )
    p["documents"][2]["chunks"][0]["vector"] = [0.1, -2.5e-8, 1e30, 3.0000001, 0.0, -0.0, 1.0, 2.0]
    return p


def f4(v):
    """The bytes the worker indexes: _process_job converts every vector to float32."""
    return np.asarray(v, dtype="<f4").tobytes()


def without_vector(rec):
    return {k: v for k, v in rec.items() if k != "vector"}


def test_codec_round_trips_documents_bit_exact():
    payload = mixed_payload()
    out = store._decode_payload(store._encode_payload(payload))
    assert set(out) == set(payload)
    assert len(out["documents"]) == len(payload["documents"])
    for d_in, d_out in zip(payload["documents"], out["documents"]):
        assert d_out["doc_id"] == d_in["doc_id"]
        if d_in["summary"] is None:
            assert d_out["summary"] is None
        else:
            assert without_vector(d_out["summary"]) == without_vector(d_in["summary"])
            assert f4(d_out["summary"]["vector"]) == f4(d_in["summary"]["vector"])
        assert len(d_out["chunks"]) == len(d_in["chunks"])
        for c_in, c_out in zip(d_in["chunks"], d_out["chunks"]):
            assert without_vector(c_out) == without_vector(c_in)
            if c_in["vector"] is None:
                assert c_out["vector"] is None
            else:
                assert f4(c_out["vector"]) == f4(c_in["vector"])
    v = out["documents"][0]["chunks"][0]["vector"]
    assert (v.dtype, v.shape, v.flags.writeable) == (np.dtype("<f4"), (8,), False)


def test_codec_round_trips_index_ops():
    for payload in ({"op": "attach_index", "nlist": 64, "nprobe": None}, {"op": "detach_index"}):
        blob = store._encode_payload(payload)
        magic, json_len, n_vecs, dim = struct.unpack_from("<4sIII", blob)
        assert (magic, n_vecs, dim) == (b"RGJ\x01", 0, 0)
        assert len(blob) == 16 + json_len
        assert store._decode_payload(blob) == payload


def test_codec_header_is_little_endian_f4():
    blob = store._encode_payload(docs_payload(range(5), dim=12))
    magic, json_len, n_vecs, dim = struct.unpack_from("<4sIII", blob)
    assert (magic, n_vecs, dim) == (b"RGJ\x01", 5, 12)
    assert store._JOB_HEADER.size == 16
    assert len(blob) == 16 + json_len + 4 * n_vecs * dim
    meta = json.loads(blob[16 : 16 + json_len].decode("utf-8"))
    assert [d["chunks"][0]["vector"] for d in meta["documents"]] == [0, 1, 2, 3, 4]
    tail = np.frombuffer(blob, dtype="<f4", offset=16 + json_len)
    assert tail.tobytes() == np.asarray([vec(i, 12) for i in range(5)], dtype="<f4").tobytes()
    # a ragged payload has no single dim to put in the header
    ragged = docs_payload([1, 2])
    ragged["documents"][1]["chunks"][0]["vector"] = vec(2, 9)
    with pytest.raises(ValueError):
        store._encode_payload(ragged)


def test_codec_does_not_mutate_its_input():
    payload = mixed_payload()
    before = copy.deepcopy(payload)
    store._encode_payload(payload)
    assert payload == before


@pytest.mark.parametrize("damage", ["bad_magic", "truncated"])
def test_decode_rejects_bad_magic_and_truncation(damage):
    blob = store._encode_payload(docs_payload([1, 2]))
    if damage == "bad_magic":
        cases = [b"RGJ\x02" + blob[4:], b"XXXX" + blob[4:]]
    else:  # cut inside the float block, inside the JSON, inside the header, empty
        cases = [blob[:-1], blob[:40], blob[:10], b""]
    for bad in cases:
        with pytest.raises(ValueError):
            store._decode_payload(bad)


def test_fresh_meta_db_has_job_payloads_table(tmp_path):
    db = open_meta_db(tmp_path / "meta.db")
    cols = [(r[1], r[2], r[5]) for r in db.execute("PRAGMA table_info(job_payloads)")]
    assert cols == [("job_id", "INTEGER", 1), ("data", "BLOB", 0)]  # (name, type, pk)
    db.close()


def test_existing_meta_db_gains_job_payloads_and_keeps_jobs(tmp_path):
    # the pre-2026-09 layout, with a TEXT-JSON job left 'processing' by a crash
    path = tmp_path / "meta.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE records(id INTEGER PRIMARY KEY, external_id TEXT UNIQUE, doc_id TEXT,
            type TEXT CHECK(type IN ('chunk','summary')), position INTEGER, text TEXT,
            metadata TEXT, indexed INTEGER DEFAULT 0);
        CREATE TABLE vecs(id INTEGER PRIMARY KEY, vec BLOB);
        CREATE INDEX idx_records_doc ON records(doc_id);
        CREATE TABLE jobs(id INTEGER PRIMARY KEY, payload TEXT, status TEXT, error TEXT,
            created_at TEXT, updated_at TEXT);
        """
    )
    text = json.dumps(docs_payload([1]))
    legacy.execute(
        "INSERT INTO jobs(id, payload, status, error, created_at, updated_at)"
        " VALUES (7, ?, 'processing', NULL, 't0', 't0')", (text,),
    )
    legacy.commit()
    legacy.close()
    db = open_meta_db(path)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_payloads" in tables
    assert db.execute("SELECT id, payload, status, error FROM jobs").fetchall() == [
        (7, text, "processing", None)
    ]
    assert db.execute("SELECT COUNT(*) FROM job_payloads").fetchone()[0] == 0
    db.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `8 failed`, for these reasons:
- `test_codec_round_trips_documents_bit_exact`: `AttributeError: module 'raggio.store' has no attribute '_decode_payload'`. The outer call's attribute is looked up first.
- `test_codec_round_trips_index_ops`, `test_codec_header_is_little_endian_f4`, `test_codec_does_not_mutate_its_input`, `test_decode_rejects_bad_magic_and_truncation[bad_magic]` and `[truncated]`: `AttributeError: module 'raggio.store' has no attribute '_encode_payload'`. The encode call sits outside `pytest.raises`, so none of them can pass by accident.
- `test_fresh_meta_db_has_job_payloads_table`: `AssertionError: assert [] == [('job_id', 'INTEGER', 1), ('data', 'BLOB', 0)]`. `PRAGMA table_info` of a missing table returns no rows.
- `test_existing_meta_db_gains_job_payloads_and_keeps_jobs`: `AssertionError: assert 'job_payloads' in {...}`.

A collection error (for example an `ImportError`) instead means the helpers or imports are wrong. Fix those before going on.

- [ ] **Step 3: Implement the codec and the table**

(a) In `src/raggio/store.py`, add the import. The existing text after Plan C:

```python
import sqlite3
import sys
```

Replace it with:

```python
import sqlite3
import struct
import sys
```

If the line below `import sqlite3` is not `import sys` on your tree, still put `import struct` directly below `import sqlite3`.

(b) Directly below `_normalize`, which reads (unchanged by Plans A, B and C):

```python
def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    if (norms == 0).any():
        raise ValueError("zero vector cannot be normalized")
    return (mat / norms).astype(np.float32)
```

insert, after two blank lines:

```python
# Binary job journal row (job_payloads.data): _JOB_HEADER (magic, JSON byte length,
# vector count, dim; little-endian, so aarch64 and x86 read the same bytes), then the
# payload as UTF-8 JSON with each supplied vector replaced by its row index, then the
# vectors as little-endian float32. float32 is what the worker indexes anyway, so no
# precision is lost, and the vectors skip a JSON round trip that costs ~100 ms per
# 250x1024 job. Stdlib + numpy only: runtime JSON stays stdlib.
_JOB_MAGIC = b"RGJ\x01"
_JOB_HEADER = struct.Struct("<4sIII")


def _encode_payload(payload: dict) -> bytes:
    """Encode a job payload for job_payloads. A summary's or chunk's non-None `vector`
    becomes an int row index into the float32 block; None stays None ("embed this").
    Index-op payloads carry no vectors (n_vecs = dim = 0). Copies whatever it rewrites,
    so the caller's dict is never mutated. Raises ValueError unless the vectors form one
    rectangular float matrix (the route checks dims, so only direct callers hit that)."""
    vectors: list = []

    def take(rec: dict) -> dict:
        if rec.get("vector") is None:
            return rec
        vectors.append(rec["vector"])
        return {**rec, "vector": len(vectors) - 1}

    docs = payload.get("documents")
    if docs is not None:
        out = []
        for d in docs:
            d = dict(d)
            if d.get("summary"):
                d["summary"] = take(d["summary"])
            if d.get("chunks"):
                d["chunks"] = [take(c) for c in d["chunks"]]
            out.append(d)
        payload = {**payload, "documents": out}
    try:
        mat = np.asarray(vectors, dtype="<f4") if vectors else np.empty((0, 0), dtype="<f4")
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"job payload vectors are not one rectangular float matrix: {e}"
        ) from None
    if mat.ndim != 2:
        raise ValueError(
            f"job payload vectors are not one rectangular float matrix: shape {mat.shape}"
        )
    meta = json.dumps(payload).encode("utf-8")
    n, dim = mat.shape
    return _JOB_HEADER.pack(_JOB_MAGIC, len(meta), n, dim) + meta + mat.tobytes()


def _decode_payload(data: bytes) -> dict:
    """Inverse of _encode_payload. Each vector comes back as a row of one read-only
    little-endian float32 matrix (views into `data`, no copy). Anything malformed
    raises ValueError: short or overlong data, bad magic, bad UTF-8 or JSON, a non-dict
    top level, a malformed document list, or a vector index out of range."""
    size = _JOB_HEADER.size
    if len(data) < size:
        raise ValueError(f"job payload truncated: {len(data)} bytes, the header alone is {size}")
    magic, json_len, n, dim = _JOB_HEADER.unpack_from(data)
    if magic != _JOB_MAGIC:
        raise ValueError(f"job payload has bad magic {bytes(magic)!r}")
    end = size + json_len
    if len(data) != end + 4 * n * dim:
        raise ValueError(
            f"job payload is {len(data)} bytes but its header says {end + 4 * n * dim}"
            " (truncated or corrupt)"
        )
    payload = json.loads(bytes(data[size:end]).decode("utf-8"))  # both errors are ValueErrors
    if not isinstance(payload, dict):
        raise ValueError(f"job payload is a JSON {type(payload).__name__}, not an object")
    if n * dim:
        mat = np.frombuffer(data, dtype="<f4", count=n * dim, offset=end).reshape(n, dim)
    else:
        mat = np.empty((n, dim), dtype="<f4")
    mat.flags.writeable = False  # also for bytearray input: rows are shared views

    def row(i) -> np.ndarray:
        if type(i) is not int or not 0 <= i < n:
            raise ValueError(f"job payload vector index {i!r} is outside 0..{n - 1}")
        return mat[i]

    try:
        for d in payload.get("documents") or ():
            s = d.get("summary")
            if s and s.get("vector") is not None:
                s["vector"] = row(s["vector"])
            for c in d.get("chunks") or ():
                if c.get("vector") is not None:
                    c["vector"] = row(c["vector"])
    except (AttributeError, TypeError) as e:
        raise ValueError(f"job payload has a malformed document list: {e}") from None
    return payload
```

Why these choices:
- The `<` in `_JOB_HEADER` and the `<f4` dtype pin little-endian on every host, so a volume moved between aarch64 and x86 still decodes.
- `json.dumps` keeps its defaults, so text and metadata escape exactly as today's TEXT journal does.
- Rows are converted with the same float64-to-float32 cast `_process_job` applies (`np.array(..., dtype=np.float32)`), so the indexed bytes are identical, which the bit-exact test pins.

(c) In `open_meta_db`, the end of the `executescript` and the line after it, as Plan C leaves them:

```python
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY,
            payload TEXT,
            status TEXT,
            error TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    _ensure_indexes(db, str(path))
```

Replace with:

```python
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY,
            payload TEXT,
            status TEXT,
            error TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        -- binary job payloads (_encode_payload), one row per open or failed job. A side
        -- table, NOT jobs.payload: the claim's status UPDATE would otherwise rewrite the
        -- payload's whole overflow chain (MBs per job). jobs.payload stays TEXT: journals
        -- written before 2026-09 keep their JSON there and still replay.
        CREATE TABLE IF NOT EXISTS job_payloads(
            job_id INTEGER PRIMARY KEY,
            data BLOB
        );
        """
    )
    _ensure_indexes(db, str(path))
```

The table is additive: `CREATE TABLE IF NOT EXISTS` adds it to existing volumes on their next open and leaves every `jobs` row alone. `job_id` is the rowid alias, so no extra index appears, and C's `PRAGMA index_list` / `idx_jobs_open` tests are unaffected. The comments are ASCII.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `8 passed`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`

Expected: `N0 + 8 passed, 1 warning` (`261 passed, 1 warning` when N0 is 253). No existing test changes: nothing calls the codec yet, and the new table only adds a `sqlite_master` row. These must stay green:
- `test_fresh_meta_db_has_incremental_autovacuum` (the pragma order in `open_meta_db` is untouched);
- Plan C's `test_fresh_meta_db_has_the_covering_and_partial_indexes` and `test_open_job_queries_use_the_partial_index`;
- Plan A's `test_orjson_is_a_bench_only_dependency` (the codec uses `json` and `struct` only).

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_ingest_path.py
git commit -F - <<'EOF'
Add binary job payload codec and job_payloads side table

Stdlib + numpy codec for job payloads: a little-endian header (magic,
JSON length, vector count, dim), the payload JSON with each vector
replaced by its row index, then the vectors as little-endian float32.
open_meta_db creates job_payloads(job_id INTEGER PRIMARY KEY, data BLOB)
on fresh and existing volumes. Nothing is wired to the queue yet.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

This is D1's first commit. Task 6 finds D1's base with `git log --format=%H -1 --fixed-strings --grep "Add binary job payload codec and job_payloads side table"` and takes its parent, so keep the subject exactly as written.

**Review Focus for Task 1:** none of RF1-RF5 is pinned here. The round-trip, header and truncation tests are the ground Task 2's `test_mixed_legacy_and_binary_queue_in_order` (RF3) stands on. When reviewing, check that:
- the decoder rejects a length mismatch before it parses anything;
- the `<` byte order is in both the `struct` format and the dtype;
- the encoder never writes into the caller's dicts, because Task 2 passes the route's `model_dump()` straight through.

### Task 2: Wire binary journal; encode/decode off the loop

Goal: `enqueue` writes the `jobs` row (with `payload` NULL) and its `job_payloads` row in one transaction. The payload is encoded in `enqueue`'s thread before `db_lock` is taken, and decoded in the claim's thread after `db_lock` is released, so no JSON or float work on a job payload runs on the event loop any more. Legacy TEXT-JSON rows still replay. A `done` job's payload row is deleted in the same commit as its status; an `error` job keeps it for diagnosis. A payload that cannot be decoded turns its job into an `error` job; it never kills the worker.

**Files:**
- Modify `src/raggio/store.py` (line numbers are base 19a8a2e, orientation only; A, B and C leave every function below unchanged, so the quoted text is base text plus Task 1):
  - new module constant `_CLAIM_SQL`, directly below Task 1's `_decode_payload`;
  - `Collection.enqueue` (base lines 671-676): hands the dict to `_enqueue_row`, with no `json.dumps` on the loop;
  - `Collection._enqueue_row` (base lines 678-688): new signature `(self, payload: dict)`. It encodes before `db_lock`, then writes both rows in one transaction and rolls back on any failure;
  - `Collection._claim_next` (base lines 695-706): runs `_CLAIM_SQL`, reads the `job_payloads` row, claims, commits, releases `db_lock`, then decodes. Returns a 3-tuple;
  - `Collection._finish_job` (base lines 708-721): on `done`, also `DELETE FROM job_payloads` before the commit. The vacuum line is unchanged here; Task 4 replaces it;
  - `Collection._run_worker` (base lines 723-740): unpacks the 3-tuple, raises `ValueError(bad)` inside its `try`, and no longer calls `json.loads`.
- Modify `tests/test_ingest_path.py`: add `import threading` to the module head, and append the section `# ---- binary job journal: queue`.
- Modify `docs/storage.md` (lines 27-36, `## Ingest durability`, untouched by A, B and C): the binary side-table journal, error keeps its payload row, legacy replay, and "drain the ingest queue before downgrading".
- Test: `uv run pytest tests/test_ingest_path.py -v`.

**Interfaces:**
- Consumes:
  - Task 1: `_encode_payload(payload: dict) -> bytes`, `_decode_payload(data: bytes) -> dict` (every malformed input raises `ValueError`), `_JOB_MAGIC`, and the `job_payloads(job_id INTEGER PRIMARY KEY, data BLOB)` table. Both codec functions are called through their module globals, so tests can `monkeypatch.setattr(store, "_encode_payload", ...)`.
  - Task 1 test helpers `make_collection`, `vec`, `docs_payload` and `drain`.
  - Plan C's partial index `idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')`, and C's pin `test_open_job_queries_use_the_partial_index`: `col._claim_next()[0] == 51` over legacy rows whose `payload` is the TEXT `'{}'` and which have no `job_payloads` row.
  - Plan B's `stop()` (the worker is cancelled only while `stop()` holds `lock.write()`; `_close_conns` closes `self.db` under `db_lock`) and B's pin that patches `col._finish_job` with a 3-argument `slow_finish(job_id, status, error)`.
- Produces:
  - `_CLAIM_SQL: str = "SELECT id, payload FROM jobs WHERE status IN ('pending','processing') ORDER BY id LIMIT 1"`. This is byte for byte the claim text C's test pins. Task 7 appends ` AND id > ?` after the predicate and adds `INDEXED BY idx_jobs_open` after the table name.
  - `Collection.enqueue(self, payload: dict) -> int` (signature unchanged).
  - `Collection._enqueue_row(self, payload: dict) -> int`. It calls `_encode_payload` before `with self.db_lock:`. Under the lock it computes `job_id = MAX(id)+1`, runs `INSERT INTO jobs(...) VALUES (?, NULL, 'pending', ?, ?)` and `INSERT INTO job_payloads(job_id, data) VALUES (?, ?)`, then commits. On any `BaseException` it rolls back and re-raises, so neither row exists and `self.db.in_transaction` is False.
  - `Collection._claim_next(self) -> tuple[int, dict | None, str | None] | None`:
    - `None` when no job is open;
    - `(job_id, payload, None)` on success, where a binary row decodes via `_decode_payload` and a legacy TEXT row via `json.loads`;
    - `(job_id, None, "bad job payload: <reason>")` when neither exists, or when decoding raises. The job is already `processing` by then.
    - The decode runs after `db_lock` is released, in the same worker thread.
  - `Collection._finish_job(self, job_id: int, status: str, error: str | None) -> None` (signature unchanged). It keeps the `payload=CASE ...` UPDATE, which clears legacy TEXT rows. On `status == "done"` it also deletes the `job_payloads` row, in the same commit.
  - `Collection._run_worker(self) -> None`: `job_id, payload, bad = row`; a non-None `bad` raises `ValueError(bad)` inside the `try`, so the job finishes as `error` with `error == bad`.
  - Test module: `import threading` at the top (later tasks may use it).
  - Test helpers in `tests/test_ingest_path.py`, which later tasks may reuse: `LEGACY_JOB` (the INSERT a pre-2026-09 release's journal row amounts to: `(id, payload_text, status)`) and `fetch(col, sql, *args) -> list[tuple]` (committed rows, read on the calling thread's `col._rdb()` connection; call it before `stop()`).

- [ ] **Step 1: Write the failing tests**

(a) In `tests/test_ingest_path.py`, the module head from Task 1 has:

```python
import sys
import time
```

Replace it with:

```python
import sys
import threading
import time
```

(b) Append this section at the end of the file, below Task 1's section. Every test drives its async part in one `asyncio.run(...)` and reads through `fetch` before `stop()` closes the connections:

```python
# ---- binary job journal: queue

# what a pre-2026-09 release's enqueue left in the journal: the payload as JSON TEXT
LEGACY_JOB = (
    "INSERT INTO jobs(id, payload, status, created_at, updated_at) VALUES (?, ?, ?, '', '')"
)


def fetch(col, sql, *args):
    """Committed rows, read on the calling thread's read connection (before stop())."""
    return col._rdb().execute(sql, args).fetchall()


def test_enqueue_writes_job_and_payload_atomically(tmp_path):
    col = make_collection(tmp_path)

    async def run():
        try:
            job_id = await col.enqueue(docs_payload([1, 2]))
            assert fetch(col, "SELECT payload, status FROM jobs WHERE id=?", job_id) == [
                (None, "pending")
            ]
            [(data,)] = fetch(col, "SELECT data FROM job_payloads WHERE job_id=?", job_id)
            assert data[:4] == store._JOB_MAGIC
            assert [d["doc_id"] for d in store._decode_payload(data)["documents"]] == ["d1", "d2"]
            # the payload INSERT fails after the jobs INSERT: both must roll back together
            with col.db_lock:
                col.db.execute(
                    "CREATE TEMP TRIGGER boom BEFORE INSERT ON job_payloads"
                    " BEGIN SELECT RAISE(ABORT, 'boom'); END"
                )
            with pytest.raises(sqlite3.IntegrityError, match="boom"):
                await col.enqueue(docs_payload([3]))
            assert not col.db.in_transaction  # nothing left for the next commit to persist
            assert fetch(col, "SELECT id FROM jobs") == [(job_id,)]
            assert fetch(col, "SELECT job_id FROM job_payloads") == [(job_id,)]
            with col.db_lock:
                col.db.execute("DROP TRIGGER boom")
            assert await col.enqueue(docs_payload([3])) == job_id + 1
            assert fetch(col, "SELECT job_id FROM job_payloads ORDER BY job_id") == [
                (job_id,), (job_id + 1,)
            ]
        finally:
            await col.stop()

    asyncio.run(run())


def test_enqueue_encodes_before_taking_db_lock(tmp_path, monkeypatch):
    # encoding a 250x1024 job is ~10 ms of CPU: under db_lock it would stall every
    # other writer (the worker's claim and finish, concurrent enqueues)
    col = make_collection(tmp_path)
    real, held = store._encode_payload, []

    def spy(payload):
        held.append(col.db_lock.locked())
        return real(payload)

    monkeypatch.setattr(store, "_encode_payload", spy)

    async def run():
        try:
            await col.enqueue(docs_payload([1]))
        finally:
            await col.stop()

    asyncio.run(run())
    assert held == [False]


def test_claim_returns_decoded_payload(tmp_path):
    col = make_collection(tmp_path)

    async def run():
        try:
            job_id = await col.enqueue(docs_payload([1, 2]))
            row = await asyncio.to_thread(col._claim_next)
            return job_id, row, fetch(col, "SELECT status FROM jobs WHERE id=?", job_id)
        finally:
            await col.stop()

    job_id, row, status = asyncio.run(run())
    assert len(row) == 3
    claimed, payload, bad = row
    assert (claimed, bad, status) == (job_id, None, [("processing",)])
    assert [d["doc_id"] for d in payload["documents"]] == ["d1", "d2"]
    v = payload["documents"][1]["chunks"][0]["vector"]
    assert (v.dtype, v.shape) == (np.dtype("<f4"), (8,))
    assert v.tobytes() == np.asarray(vec(2), dtype=np.float32).tobytes()
```

Then, in the same section, the replay, error and off-loop tests:

```python
def test_mixed_legacy_and_binary_queue_in_order(tmp_path):
    # an in-place upgrade: TEXT-JSON jobs from the old release (one left 'processing'
    # by a crash, one still 'pending') around binary jobs journaled by this one
    first = make_collection(tmp_path)

    async def journal():
        try:
            with first.db_lock:
                first.db.execute(LEGACY_JOB, (1, json.dumps(docs_payload([1])), "processing"))
                first.db.commit()
            assert await first.enqueue(docs_payload([2])) == 2
            assert await first.enqueue(docs_payload([3])) == 3
            with first.db_lock:
                first.db.execute(LEGACY_JOB, (4, json.dumps(docs_payload([4])), "pending"))
                first.db.commit()
            return fetch(first, "SELECT job_id FROM job_payloads ORDER BY job_id")
        finally:
            await first.stop()

    assert asyncio.run(journal()) == [(2,), (3,)]
    col = make_collection(tmp_path)  # the reboot replays the whole journal
    seen = []
    real = col._process_job

    async def spy(payload, **kw):  # **kw: Task 7 adds the keyword-only `sync` flag
        seen.append([d["doc_id"] for d in payload["documents"]])
        await real(payload, **kw)

    col._process_job = spy

    async def replay():
        col.start_worker()
        try:
            await drain(col)
            return (
                fetch(col, "SELECT id, status, error, payload FROM jobs ORDER BY id"),
                fetch(col, "SELECT COUNT(*) FROM job_payloads"),
                fetch(col, "SELECT external_id FROM records ORDER BY id"),
            )
        finally:
            await col.stop()

    jobs, payload_rows, records = asyncio.run(replay())
    assert seen == [["d1"], ["d2"], ["d3"], ["d4"]]
    assert jobs == [(i, "done", None, None) for i in (1, 2, 3, 4)]
    assert payload_rows == [(0,)]
    assert records == [("c1",), ("c2",), ("c3",), ("c4",)]  # record ids follow job order


def test_error_job_keeps_payload_row(tmp_path):
    col = make_collection(tmp_path)
    zero = docs_payload([2])
    zero["documents"][0]["chunks"][0]["vector"] = [0.0] * 8  # _normalize rejects it

    async def run():
        col.start_worker()
        try:
            for p in (docs_payload([1]), zero, docs_payload([3])):
                await col.enqueue(p)
            await drain(col)
            return (
                fetch(col, "SELECT id, status, error FROM jobs ORDER BY id"),
                fetch(col, "SELECT job_id, data FROM job_payloads ORDER BY job_id"),
            )
        finally:
            await col.stop()

    jobs, kept = asyncio.run(run())
    assert [(i, s) for i, s, _ in jobs] == [(1, "done"), (2, "error"), (3, "done")]
    assert "zero vector" in jobs[1][2]
    assert [job_id for job_id, _ in kept] == [2]  # kept for diagnosis; done rows are gone
    assert store._decode_payload(kept[0][1])["documents"][0]["doc_id"] == "d2"


@pytest.mark.parametrize("damage", ["bad_magic", "truncated", "no_payload"])
def test_bad_payload_becomes_an_error_job(tmp_path, damage):
    col = make_collection(tmp_path)
    blob = store._encode_payload(docs_payload([1]))
    reason = {"bad_magic": "bad magic", "truncated": "truncated", "no_payload": "no payload"}

    async def run():
        assert await col.enqueue(docs_payload([1])) == 1
        assert await col.enqueue(docs_payload([2])) == 2
        with col.db_lock:
            if damage == "no_payload":  # jobs.payload is NULL too: nothing left to decode
                col.db.execute("DELETE FROM job_payloads WHERE job_id=1")
            else:
                bad = b"XXXX" + blob[4:] if damage == "bad_magic" else blob[:-3]
                col.db.execute(
                    "INSERT OR REPLACE INTO job_payloads(job_id, data) VALUES (1, ?)", (bad,)
                )
            col.db.commit()
        col.start_worker()
        try:
            await drain(col)
            return (
                not col._worker.done(),
                fetch(col, "SELECT id, status, error FROM jobs ORDER BY id"),
                fetch(col, "SELECT external_id FROM records"),
            )
        finally:
            await col.stop()

    alive, jobs, records = asyncio.run(run())
    assert [(i, s) for i, s, _ in jobs] == [(1, "error"), (2, "done")]
    assert jobs[0][2].startswith("bad job payload: ")
    assert reason[damage] in jobs[0][2]
    assert alive  # a bad row is an error job, never a dead worker
    assert records == [("c2",)]


def test_payload_codec_runs_off_the_event_loop(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    calls = {"encode": [], "decode": []}
    real_encode, real_decode = store._encode_payload, store._decode_payload

    def encode(payload):
        calls["encode"].append(threading.get_ident())
        return real_encode(payload)

    def decode(data):
        calls["decode"].append(threading.get_ident())
        return real_decode(data)

    monkeypatch.setattr(store, "_encode_payload", encode)
    monkeypatch.setattr(store, "_decode_payload", decode)

    async def run():
        col.start_worker()
        try:
            for i in range(3):
                await col.enqueue(docs_payload([i]))
            await drain(col)
        finally:
            await col.stop()
        return threading.get_ident()

    loop_thread = asyncio.run(run())
    assert (len(calls["encode"]), len(calls["decode"])) == (3, 3)
    assert loop_thread not in calls["encode"] + calls["decode"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `9 failed, 8 passed`. Task 1's 8 tests pass; the 9 new ones fail for these reasons:
- `test_enqueue_writes_job_and_payload_atomically`: `AssertionError: assert [('{"documents": ...', 'pending')] == [(None, 'pending')]`. The base `enqueue` still journals JSON text in `jobs.payload`.
- `test_enqueue_encodes_before_taking_db_lock`: `assert [] == [False]`. The codec is never called.
- `test_claim_returns_decoded_payload`: `assert 2 == 3`. The base claim returns `(id, payload_text)`.
- `test_mixed_legacy_and_binary_queue_in_order`: `assert [] == [(2,), (3,)]`. No `job_payloads` rows are written.
- `test_error_job_keeps_payload_row`: `assert [] == [2]`. The statuses already match; the payload row is what is missing.
- `test_bad_payload_becomes_an_error_job[bad_magic]`, `[truncated]` and `[no_payload]`: `AssertionError: assert [(1, 'done'), (2, 'done')] == [(1, 'error'), (2, 'done')]`. The base worker decodes the TEXT copy in `jobs.payload` and never reads the damaged side-table row.
- `test_payload_codec_runs_off_the_event_loop`: `assert (0, 0) == (3, 3)`.

An `ImportError`, a `NameError` for `threading` or `fetch`, or an `OperationalError` instead means Step 1 was pasted wrong. Fix that before going on.

- [ ] **Step 3: Implement the binary journal**

All edits are in `src/raggio/store.py`.

(a) Add `_CLAIM_SQL` directly below Task 1's `_decode_payload`, whose last lines are:

```python
    except (AttributeError, TypeError) as e:
        raise ValueError(f"job payload has a malformed document list: {e}") from None
    return payload
```

Replace them with:

```python
    except (AttributeError, TypeError) as e:
        raise ValueError(f"job payload has a malformed document list: {e}") from None
    return payload


# the worker's claim: the lowest open job. 'processing' is included so jobs interrupted
# by a crash replay on boot. Keep the literal predicate byte for byte: it is what Plan
# C's partial index idx_jobs_open matches (spec 4.1), and C's plan test pins this text
_CLAIM_SQL = (
    "SELECT id, payload FROM jobs WHERE status IN ('pending','processing') ORDER BY id LIMIT 1"
)
```

(b) `Collection.enqueue` and `Collection._enqueue_row`. The existing text (base, unchanged by A, B and C):

```python
    async def enqueue(self, payload: dict) -> int:
        # journaling a bulky payload is real I/O: run the transaction in a thread and
        # only touch the (non-thread-safe) wake event back on the loop
        job_id = await asyncio.to_thread(self._enqueue_row, json.dumps(payload))
        self._wake.set()
        return job_id

    def _enqueue_row(self, payload_json: str) -> int:
        with self.db_lock:
            # explicit id, not lastrowid: MAX+1 is race-free under db_lock
            job_id = self.db.execute("SELECT COALESCE(MAX(id),0)+1 FROM jobs").fetchone()[0]
            self.db.execute(
                "INSERT INTO jobs(id, payload, status, created_at, updated_at)"
                " VALUES (?, ?, 'pending', ?, ?)",
                (job_id, payload_json, _now(), _now()),
            )
            self.db.commit()
        return job_id
```

Replace it with:

```python
    async def enqueue(self, payload: dict) -> int:
        # encoding a bulky payload is real CPU and journaling it real I/O: both run in
        # a thread, and only the (non-thread-safe) wake event is touched on the loop
        job_id = await asyncio.to_thread(self._enqueue_row, payload)
        self._wake.set()
        return job_id

    def _enqueue_row(self, payload: dict) -> int:
        # encode BEFORE taking db_lock: the lock serializes every write on self.db, so
        # CPU work under it would stall the worker's claim/finish and other enqueues
        data = _encode_payload(payload)
        with self.db_lock:
            try:
                # explicit id, not lastrowid: MAX+1 is race-free under db_lock
                job_id = self.db.execute("SELECT COALESCE(MAX(id),0)+1 FROM jobs").fetchone()[0]
                # jobs.payload stays NULL: the payload lives in job_payloads, so the
                # claim's status UPDATE rewrites a small row, not an MB overflow chain
                self.db.execute(
                    "INSERT INTO jobs(id, payload, status, created_at, updated_at)"
                    " VALUES (?, NULL, 'pending', ?, ?)",
                    (job_id, _now(), _now()),
                )
                self.db.execute(
                    "INSERT INTO job_payloads(job_id, data) VALUES (?, ?)", (job_id, data)
                )
                self.db.commit()
            except BaseException:
                # one transaction: a jobs row without its payload would replay as an
                # error job, and a half-written txn would ride on the next commit
                self.db.rollback()
                raise
        return job_id
```

`enqueue`'s callers are unchanged: the ingest route passes `body.model_dump()`, and `request_index` / `request_index_drop` pass their small `{"op": ...}` dicts. `_encode_payload` copies what it rewrites, so the route's dict is never mutated.

(c) `Collection._claim_next`. The existing text (base, unchanged by A, B and C):

```python
    def _claim_next(self) -> tuple | None:
        # 'processing' included so jobs interrupted by a crash are replayed on boot
        with self.db_lock:
            row = self.db.execute(
                "SELECT id, payload FROM jobs WHERE status IN ('pending','processing') ORDER BY id LIMIT 1"
            ).fetchone()
            if row is not None:
                self.db.execute(
                    "UPDATE jobs SET status='processing', updated_at=? WHERE id=?", (_now(), row[0])
                )
                self.db.commit()
            return row
```

Replace it with:

```python
    def _claim_next(self) -> tuple[int, dict | None, str | None] | None:
        """Claim the lowest open job. Returns (job_id, payload, None), or
        (job_id, None, "bad job payload: ...") when its payload cannot be decoded, or
        None when no job is open. The claim is one short transaction under db_lock;
        the decode runs after the lock is released, still in this worker thread, so
        neither the loop nor the other writers wait for it."""
        with self.db_lock:
            row = self.db.execute(_CLAIM_SQL).fetchone()
            if row is None:
                return None
            job_id, text = row
            blob = self.db.execute(
                "SELECT data FROM job_payloads WHERE job_id=?", (job_id,)
            ).fetchone()
            self.db.execute(
                "UPDATE jobs SET status='processing', updated_at=? WHERE id=?", (_now(), job_id)
            )
            self.db.commit()
        try:
            if blob is not None and blob[0] is not None:
                return job_id, _decode_payload(blob[0]), None
            if text is None:
                raise ValueError(
                    f"job {job_id} has no payload (neither jobs.payload nor job_payloads)"
                )
            payload = json.loads(text)  # a journal row written before 2026-09
            if not isinstance(payload, dict):
                raise ValueError(f"payload is a JSON {type(payload).__name__}, not an object")
            return job_id, payload, None
        except Exception as e:
            # as when json.loads ran inside the worker's try: an undecodable row ends
            # as an 'error' job with its payload row kept, and the worker lives on
            return job_id, None, f"bad job payload: {e}"
```

Why this shape:
- The claim still returns a sequence with the job id at `[0]`, so C's `col._claim_next()[0] == 51` holds. C's legacy rows carry the TEXT `'{}'` and no `job_payloads` row, so they decode to `{}` on the legacy branch.
- A binary row wins over `jobs.payload`, which `_enqueue_row` leaves NULL. A legacy row has no `job_payloads` row, so it takes the `json.loads` branch. No row ever has both.
- If `stop()` cancels the worker while this runs in its thread, the claim transaction completes under `db_lock` (B's `_close_conns` waits for it), and the decode touches no connection. The claimed job stays `processing` and replays on the next open.

(d) `Collection._finish_job`. The existing text (base, unchanged by A, B and C):

```python
    def _finish_job(self, job_id: int, status: str, error: str | None) -> None:
        with self.db_lock:
            # payload cleared on success so vector-heavy jobs don't accumulate on disk;
            # kept on error for diagnosis
            self.db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=?,"
                " payload=CASE WHEN ?='done' THEN NULL ELSE payload END WHERE id=?",
                (status, error, _now(), status, job_id),
            )
            self.db.commit()
```

Replace it with:

```python
    def _finish_job(self, job_id: int, status: str, error: str | None) -> None:
        with self.db_lock:
            # payload cleared on success so vector-heavy jobs don't accumulate on disk;
            # kept on error for diagnosis. The CASE clears a legacy TEXT payload; a
            # binary one is its job_payloads row, deleted in the same commit
            self.db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=?,"
                " payload=CASE WHEN ?='done' THEN NULL ELSE payload END WHERE id=?",
                (status, error, _now(), status, job_id),
            )
            if status == "done":
                self.db.execute("DELETE FROM job_payloads WHERE job_id=?", (job_id,))
            self.db.commit()
```

The lines after the commit (the `# return the cleared payload's pages ...` comment and `self.db.execute("PRAGMA incremental_vacuum").fetchall()`) stay as they are. They now also return the deleted payload row's pages, which `test_job_payload_pages_are_returned_after_processing` measures. Task 4 replaces that vacuum line.

(e) `Collection._run_worker`. The existing text (base, unchanged by A, B and C):

```python
            job_id, payload = row
            try:
                await self._process_job(json.loads(payload))
                status, error = "done", None
```

Replace it with:

```python
            # decoded in the claim's thread; an undecodable row comes back as
            # (job_id, None, reason) and finishes as 'error' like any failed job
            job_id, payload, bad = row
            try:
                if bad is not None:
                    raise ValueError(bad)
                await self._process_job(payload)
                status, error = "done", None
```

The rest of `_run_worker` stays: the `_wake.clear()` before the claim, the `except asyncio.CancelledError: raise`, the `except Exception` that sets `status, error = "error", str(e)`, and the `_finish_job` call. `json` is still used by `_upsert_rows` and the legacy branch of `_claim_next`, so the import stays.

(f) In `docs/storage.md`, `## Ingest durability`, replace:

```markdown
1. `POST /documents` journals the job to `meta.db` **before** the `202`
   response is sent.
2. A per-collection worker embeds missing vectors, writes records, and
   updates both indexes.
3. The job is marked `done` only after the vector index is synced to disk.
   Successful payloads are cleared so vector-heavy jobs don't accumulate;
   failed jobs keep their payload for diagnosis.

After a crash, any `pending` or `processing` job is replayed on boot.
Replays are idempotent: records are upserted by id.
```

with:

```markdown
1. `POST /documents` journals the job to `meta.db` **before** the `202`
   response is sent: a row in `jobs` (status and timestamps) and, in the
   same transaction, the payload as one binary row in `job_payloads` (the
   request's JSON fields, with the vectors stored as little-endian float32).
2. A per-collection worker embeds missing vectors, writes records, and
   updates both indexes.
3. The job is marked `done` only after the vector index is synced to disk.
   A successful job's payload row is deleted so vector-heavy jobs don't
   accumulate; a failed job keeps it for diagnosis. A payload that cannot
   be read fails its job with a `bad job payload: ...` error.

After a crash, any `pending` or `processing` job is replayed on boot.
Replays are idempotent: records are upserted by id. Jobs journaled by a
release before 2026-09, whose JSON payload sits in `jobs.payload`, still
replay after an upgrade. The reverse does not hold: an older release cannot
read `job_payloads`, so drain the ingest queue (`pending_jobs` is 0 in
`GET /collections/{name}` for every collection) before downgrading.
```

`docs/storage.md` is UTF-8 and the new text is ASCII. Plan C's `## Upgrading` section sits further down the file, so this edit does not touch it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `17 passed` (Task 1's 8 and this task's 9).

Then run the journal pins from the other plans and the base tests:

Run: `uv run pytest tests/test_cold_start.py tests/test_concurrency.py tests/test_store_optimizations.py tests/test_api.py -q`

Expected: all pass, with the same counts as on the branch base. In particular:
- C's `test_open_job_queries_use_the_partial_index`: `_CLAIM_SQL` is its literal claim text, and `_claim_next()` returns `(51, {}, None)`.
- `test_persistence_and_crash_replay`: the legacy TEXT row replays through the `json.loads` branch and its `jobs.payload` is NULL afterwards.
- `test_concurrent_enqueue_with_worker_loses_nothing` and `test_job_payload_pages_are_returned_after_processing`.
- B's `stop()` tests: `slow_finish` still replaces `_finish_job` with 3 arguments, and the `_CloseSpy` proxy forwards `execute` and `commit`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`

Expected: `N0 + 17 passed, 1 warning` (`270 passed, 1 warning` when N0 is 253). No existing test changes. Plan A's `test_orjson_is_a_bench_only_dependency` stays green: no new import, and the word does not appear in `store.py`.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_ingest_path.py docs/storage.md
git commit -F - <<'EOF'
Journal ingest payloads in job_payloads; encode and decode off the loop

enqueue encodes the payload in its thread before taking db_lock and
writes the jobs row (payload NULL) and its job_payloads row in one
transaction, rolled back on any failure. The claim reads the row under
db_lock and decodes it after releasing the lock, still in the worker
thread; legacy TEXT-JSON rows replay through json.loads. An undecodable
payload finishes its job as 'error' ("bad job payload: ...") instead of
killing the worker. A done job's payload row is deleted in the same
commit as its status; an error job keeps it. _CLAIM_SQL keeps Plan C's
claim text byte for byte.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Review Focus for Task 2:** RF3 (legacy journal replay) is pinned here by `test_mixed_legacy_and_binary_queue_in_order`, together with the base `test_persistence_and_crash_replay`, which stays unchanged. When reviewing, check that:
- `_CLAIM_SQL` is C's claim text byte for byte, including the literal `status IN ('pending','processing')`;
- `_encode_payload` runs before `with self.db_lock:` and `_decode_payload` after it (`test_enqueue_encodes_before_taking_db_lock`, `test_payload_codec_runs_off_the_event_loop`);
- both INSERTs in `_enqueue_row` share one transaction that rolls back on `BaseException` (`test_enqueue_writes_job_and_payload_atomically`);
- a bad row can only produce an `error` job, never a dead worker (`test_bad_payload_becomes_an_error_job`);
- `_finish_job` deletes the payload row only on `done` (`test_error_job_keeps_payload_row`).

### Task 3: Failure-safe set-based upsert; counts after commit; prep off loop

Goal: `_upsert_rows` and `_delete_doc_rows` each run as one transaction that rolls back on any `BaseException`. Neither touches the vector index or `indexed_counts` before its commit. After the commit, still under `db_lock`, each rebinds `indexed_counts` exactly once (copy-on-write, as B left it). The replaced rows leave the index in the new `_unindex`, only after the upsert committed. The old-row lookup becomes one `IN (...)` query per 512 rows instead of a point query per row, and the writes become `executemany` (DELETE, then INSERT, in the same transaction). Payload flattening and the float32 matrix build move off the event loop into worker threads.

Out of scope, unchanged from base: `index.add_with_ids` (or `_unindex`) failing AFTER the commit still leaves an `error` job whose committed rows are missing from the index. Task 6's ADR addendum records this.

**Files:**
- Modify `src/raggio/store.py` (line numbers are base 19a8a2e, orientation only; anchor on the quoted text):
  - new module functions `_payload_rows` and `_rows_matrix`, directly below Task 2's `_CLAIM_SQL = (...)` block;
  - `Collection._process_job` (base lines 742-803; Plan C wrapped the index-op head in `try/finally: _malloc_trim`, which stays untouched): the flatten/dedup block (base 750-764) and the `mat = _normalize(...)` line (base 771) become `asyncio.to_thread` calls, and the `lock.write()` section unpacks a 3-tuple and calls `_unindex`;
  - `Collection._upsert_rows` (base lines 822-856, as Plan B Task 4 left it): set-based, one transaction with rollback, counts published after the commit; returns `(ids, fresh, replaced)`;
  - new `Collection._unindex`, directly below `_upsert_rows` (above `# ---- optional IVF index (attach / detach) ----`). Plan F inserts its own method directly BEFORE `def _upsert_rows(`, so nothing new goes there;
  - `Collection._delete_doc_rows` (base lines 1507-1523, as Plan B Task 4 left it): one transaction with rollback, counts published after the commit, index removal through `_unindex` after `db_lock` is released. `delete_document` (base 1499-1505) is unchanged.
- Modify `tests/test_ingest_path.py`: append the section `# ---- upsert ordering and rollback` (two helpers, five tests). The module head is unchanged: it already imports `sqlite3`, `threading`, `numpy` and `pytest`.
- Test: `uv run pytest tests/test_ingest_path.py -v`.

**Interfaces:**
- Consumes:
  - Plan B: copy-on-write `indexed_counts` (writers rebind a new dict under `db_lock`, once per write, never mutate in place) and its pins `test_indexed_counts_is_published_only_under_db_lock` (`[True, True]` for one `_process_job` + one `delete_document`), `test_no_in_place_indexed_counts_mutation_in_store_source`, `test_no_threading_lock_is_held_across_an_await`, and `test_stop_does_not_cancel_the_worker_inside_its_write_section`, whose `slow_upsert(rows, mat)` returns `real_upsert(rows, mat)` unchanged, so it passes the new 3-tuple through.
  - `_rows_by_id(db, sql: str, ids: list)` (base lines 167-172, unchanged): runs `sql` with one `{}` qmark slot in chunks of 512 values. It is typed `list[int]` but only joins qmarks, so string external ids work.
  - `_normalize(mat: np.ndarray) -> np.ndarray` (raises `ValueError("zero vector cannot be normalized")`, which Task 2's `test_error_job_keeps_payload_row` matches).
  - Task 2: the `_CLAIM_SQL` block (insertion anchor); the worker turning any exception from `_process_job` into an `error` finish; the test helpers `make_collection`, `docs_payload`, `drain` and `fetch`.
- Produces:
  - `_payload_rows(payload: dict) -> list[list]`: the base flatten loop and the last-occurrence-wins dedup, moved verbatim. Each row is a list `[external_id, doc_id, type, position, text, metadata, vector]`; `vector` is None when the worker must embed it. Module-level and called through the module global, so tests can monkeypatch it.
  - `_rows_matrix(rows: list[list]) -> np.ndarray`: `_normalize(np.array([r[6] for r in rows], dtype=np.float32))`. Module-level, monkeypatchable.
  - `Collection._upsert_rows(self, rows: list, mat: np.ndarray) -> tuple[list[int], list[bool], list[int]]`, returning `(ids, fresh, replaced)`:
    - `ids`: the new record ids, `MAX(id)+1 ...` in row order;
    - `fresh[n]`: True when `rows[n]`'s external_id had no record before;
    - `replaced`: the ids of the replaced records that were `indexed=1`, i.e. the ids the caller must remove from the index. Task 7's removal cap counts `len(replaced)`, never `index.remove`'s return (`_IvfIndex.remove` returns None).
    - Name and positional arity `(rows, mat)` are unchanged (B's pin). The caller must hold `lock.write()`.
  - `Collection._unindex(self, rids: list[int]) -> None`: `self.index.remove(rid)` for each id. The caller holds `lock.write()` and has already committed the rows' removal. `db_lock` is not needed and not taken. Task 7 adds its removal counter here.
  - `Collection._delete_doc_rows(self, doc_id: str) -> int` (signature unchanged; it keeps C's pinned `SELECT id, indexed, type FROM records WHERE doc_id=?` text).
  - `Collection._process_job(self, payload: dict) -> None` (signature unchanged). Task 7 inserts its removal-cap check directly above the `if replaced:` line this task adds.
  - Test helpers in `tests/test_ingest_path.py`: `index_ids(col) -> list[int]` (every id in a flat collection's vector index, sorted) and `index_state(col) -> tuple` (committed records `(id, external_id)`, vec ids, `index_ids(col)`, a copy of `indexed_counts`).

- [ ] **Step 1: Write the failing tests**

Append this section at the end of `tests/test_ingest_path.py`, below Task 2's section. Record and job ids are deterministic in a fresh collection (`MAX(id)+1`), so the tests spell them out.

```python
# ---- upsert ordering and rollback


def index_ids(col):
    """Every id in a flat collection's vector index, sorted: a search whose k is the
    index size returns them all (the probe _reconcile_ghosts uses)."""
    if not len(col.index):
        return []
    probe = np.zeros((1, col.cfg.dim), dtype=np.float32)
    probe[0, 0] = 1.0
    return sorted(int(i) for i in col.index.search(probe, k=len(col.index))[1][0])


def index_state(col):
    """What a failed write must leave untouched: the committed records and vecs, the
    vector index's ids and the published indexed_counts."""
    return (
        fetch(col, "SELECT id, external_id FROM records ORDER BY id"),
        fetch(col, "SELECT id FROM vecs ORDER BY id"),
        index_ids(col),
        dict(col.indexed_counts),
    )


def test_upsert_failure_rolls_back_and_keeps_index(tmp_path):
    # before: a failure mid-upsert had already removed the replaced ids from the index
    # and left the transaction open, and the worker's own _finish_job('error') commit
    # then persisted the half-applied upsert under a terminal job that never replays
    col = make_collection(tmp_path)

    async def run():
        col.start_worker()
        try:
            await col.enqueue(docs_payload([1, 2, 3]))  # job 1: records 1, 2, 3
            await drain(col)
            before = index_state(col)
            with col.db_lock:  # TEMP: only col.db (the worker's upserts) fires it
                col.db.execute(
                    "CREATE TEMP TRIGGER boom BEFORE INSERT ON vecs WHEN NEW.id = 5"
                    " BEGIN SELECT RAISE(ABORT, 'boom'); END"
                )
            # job 2 replaces all three rows with ids 4, 5, 6: the second vec INSERT aborts
            await col.enqueue(docs_payload([1, 2, 3]))
            await drain(col)
            # read after job 2's _finish_job('error') committed: nothing partial rode on it
            after, in_txn = index_state(col), col.db.in_transaction
            with col.db_lock:
                col.db.execute("DROP TRIGGER boom")
            await col.enqueue(docs_payload([1, 2, 3]))  # job 3: the same upsert succeeds
            await drain(col)
            jobs = fetch(col, "SELECT id, status, error FROM jobs ORDER BY id")
            return before, after, in_txn, jobs, index_state(col)
        finally:
            await col.stop()

    before, after, in_txn, jobs, final = asyncio.run(run())
    assert before == ([(1, "c1"), (2, "c2"), (3, "c3")], [(1,), (2,), (3,)], [1, 2, 3], {"chunk": 3})
    assert jobs == [(1, "done", None), (2, "error", "boom"), (3, "done", None)]
    assert after == before  # records, vecs, index ids and counts all unchanged
    assert in_txn is False
    # the rollback left MAX(id) at 3, so job 3's rows are 4, 5, 6
    assert final == ([(4, "c1"), (5, "c2"), (6, "c3")], [(4,), (5,), (6,)], [4, 5, 6], {"chunk": 3})
    reopened = make_collection(tmp_path)  # what reached disk: no partial rows, no ghosts
    try:
        assert index_state(reopened) == final
    finally:
        asyncio.run(reopened.stop())


def test_delete_failure_rolls_back_and_keeps_index(tmp_path):
    # before: delete_document removed the ids from the index first, and a failing
    # DELETE left the vecs deletion in an open transaction for the next commit
    col = make_collection(tmp_path)

    async def run():
        try:
            await col._process_job(docs_payload([1, 2]))  # records 1 (d1), 2 (d2)
            before = index_state(col)
            with col.db_lock:
                col.db.execute(
                    "CREATE TEMP TRIGGER boom BEFORE DELETE ON records"
                    " BEGIN SELECT RAISE(ABORT, 'boom'); END"
                )
            with pytest.raises(sqlite3.IntegrityError, match="boom"):
                await col.delete_document("d1")
            in_txn = col.db.in_transaction
            await col.enqueue(docs_payload([9]))  # a later writer commits on col.db
            after = index_state(col)
            with col.db_lock:
                col.db.execute("DROP TRIGGER boom")
            deleted = await col.delete_document("d1")
            return before, in_txn, after, deleted, index_state(col)
        finally:
            await col.stop()

    before, in_txn, after, deleted, final = asyncio.run(run())
    assert before == ([(1, "c1"), (2, "c2")], [(1,), (2,)], [1, 2], {"chunk": 2})
    assert (in_txn, after) == (False, before)
    assert deleted == 1
    assert final == ([(2, "c2")], [(2,)], [2], {"chunk": 1})
```

Then, in the same section, the ordering, set-based and off-loop tests:

```python
def test_indexed_counts_published_after_commit(tmp_path):
    # B published inside the open transaction; D publishes after the commit, still
    # under db_lock and still once per write, so no reader ever sees counts for rows a
    # rollback then discards
    col = make_collection(tmp_path)
    seen = []

    class Spy(Collection):
        def __setattr__(self, name, value):
            if name == "indexed_counts":
                seen.append((self.db_lock.locked(), self.db.in_transaction))
            super().__setattr__(name, value)

    col.__class__ = Spy

    async def run():
        try:
            await col._process_job(docs_payload([1, 2]))
            await col._process_job(docs_payload([2, 3]))  # replaces c2: -1 then +1
            await col.delete_document("d1")
            return dict(col.indexed_counts)
        finally:
            await col.stop()

    counts = asyncio.run(run())
    assert seen == [(True, False)] * 3  # one rebind per write: under db_lock, committed
    assert counts == {"chunk": 2}


def test_upsert_old_row_lookup_is_set_based(tmp_path):
    # before: one `WHERE external_id=?` point query per row, all under db_lock
    col = make_collection(tmp_path)
    statements = []

    async def run():
        try:
            await col._process_job(docs_payload(range(300)))  # records 1..300
            with col.db_lock:
                col.db.set_trace_callback(statements.append)
            await col._process_job(docs_payload(range(300)))  # replaces all 300
            with col.db_lock:
                col.db.set_trace_callback(None)
            return (
                fetch(col, "SELECT COUNT(*), MIN(id), MAX(id) FROM records"),
                fetch(col, "SELECT COUNT(*) FROM vecs"),
                index_ids(col),
                dict(col.indexed_counts),
            )
        finally:
            await col.stop()

    records, vecs, ids, counts = asyncio.run(run())
    # the trace shows expanded SQL (external_id='c7'), so match on the spaceless text
    flat = [s.replace(" ", "") for s in statements]
    assert sum("WHEREexternal_id=" in s for s in flat) == 0
    assert sum("WHEREexternal_idIN(" in s for s in flat) == 1  # 300 ids: one 512-id chunk
    assert records == [(300, 301, 600)]
    assert vecs == [(300,)]
    assert ids == list(range(301, 601))
    assert counts == {"chunk": 300}


def test_ingest_prep_runs_off_the_event_loop(tmp_path, monkeypatch):
    # np.array over a 250x1024 job's nested float lists is ~13 ms of CPU, plus the
    # flatten loop: on the loop it stalled every request the process served meanwhile
    col = make_collection(tmp_path)
    real_rows, real_matrix = store._payload_rows, store._rows_matrix
    calls = {"rows": [], "matrix": []}

    def rows_spy(payload):
        calls["rows"].append(threading.get_ident())
        return real_rows(payload)

    def matrix_spy(rows):
        calls["matrix"].append(threading.get_ident())
        return real_matrix(rows)

    monkeypatch.setattr(store, "_payload_rows", rows_spy)
    monkeypatch.setattr(store, "_rows_matrix", matrix_spy)

    async def run():
        col.start_worker()
        try:
            for i in range(3):
                await col.enqueue(docs_payload([i, i + 10]))
            await drain(col)
            return threading.get_ident(), fetch(col, "SELECT COUNT(*) FROM records")
        finally:
            await col.stop()

    loop_thread, records = asyncio.run(run())
    assert (len(calls["rows"]), len(calls["matrix"])) == (3, 3)
    assert loop_thread not in calls["rows"] + calls["matrix"]
    assert records == [(6,)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `5 failed, 17 passed`. Tasks 1 and 2's 17 tests pass; the 5 new ones fail for these reasons:
- `test_upsert_failure_rolls_back_and_keeps_index`: the `before` and `jobs` asserts pass, then `assert after == before` fails with `AssertionError: assert ([(3, 'c3'), (4, 'c1'), (5, 'c2')], [(3,), (4,)], [3], {'chunk': 3}) == ([(1, 'c1'), (2, 'c2'), (3, 'c3')], [(1,), (2,), (3,)], [1, 2, 3], {'chunk': 3})`. The base loop had removed ids 1 and 2 from the index and replaced c1 and c2 before the vec INSERT for id 5 aborted, and job 2's `_finish_job('error')` committed all of it. (`indexed_counts` still matches only because B's rebind comes after the loop.)
- `test_delete_failure_rolls_back_and_keeps_index`: `AssertionError: assert (True, ([(1, 'c1'), (2, 'c2')], [(2,)], [2], {'chunk': 2})) == (False, ([(1, 'c1'), (2, 'c2')], [(1,), (2,)], [1, 2], {'chunk': 2}))`. The transaction is still open after the failed DELETE, id 1 is gone from the index, and the enqueue's commit persisted the vecs deletion.
- `test_indexed_counts_published_after_commit`: `assert [(True, True), (True, True), (True, True)] == [(True, False), (True, False), (True, False)]`. B rebinds before `commit()`.
- `test_upsert_old_row_lookup_is_set_based`: `assert 300 == 0`. One point lookup per row.
- `test_ingest_prep_runs_off_the_event_loop`: `AttributeError: module 'raggio.store' has no attribute '_payload_rows'`.

A `NameError` for `fetch`, `drain` or `index_ids`, or an `OperationalError` from a trigger statement, means Step 1 was pasted wrong. If the upsert test instead fails on `before` or `jobs`, or the set-based test counts a number other than 300, stop and reconcile this plan with the tree before implementing.

- [ ] **Step 3: Implement the failure-safe upsert**

All edits are in `src/raggio/store.py`.

(a) Add `_payload_rows` and `_rows_matrix` directly below Task 2's `_CLAIM_SQL` block, which reads:

```python
_CLAIM_SQL = (
    "SELECT id, payload FROM jobs WHERE status IN ('pending','processing') ORDER BY id LIMIT 1"
)
```

Replace it with:

```python
_CLAIM_SQL = (
    "SELECT id, payload FROM jobs WHERE status IN ('pending','processing') ORDER BY id LIMIT 1"
)


def _payload_rows(payload: dict) -> list[list]:
    """Flatten an ingest payload's documents into record rows [external_id, doc_id,
    type, position, text, metadata, vector]; vector None means "embed this". Rows are
    lists because the worker fills in embedded vectors. Pure CPU: _process_job runs it
    in a worker thread."""
    rows = []
    for d in payload["documents"]:
        if d.get("summary"):
            s = d["summary"]
            rows.append([d["doc_id"], d["doc_id"], "summary", None, s.get("text"), s.get("metadata"), s.get("vector")])
        for i, c in enumerate(d.get("chunks") or []):
            pos = c.get("position") if c.get("position") is not None else i
            rows.append([c["id"], d["doc_id"], "chunk", pos, c.get("text"), c.get("metadata"), c.get("vector")])
    # last occurrence wins: _upsert_rows inserts a job's rows with one executemany, so
    # a duplicate external_id within one payload would violate UNIQUE and fail the job
    # (and the first copy would only have been replaced by the second anyway)
    return list({r[0]: r for r in rows}.values())


def _rows_matrix(rows: list[list]) -> np.ndarray:
    """The rows' vectors as one unit-norm float32 matrix (ValueError on a zero vector).
    np.array over nested float lists is ~13 ms per 250x1024 job: _process_job runs it
    in a worker thread."""
    return _normalize(np.array([r[6] for r in rows], dtype=np.float32))
```

The flatten loop is base text moved verbatim. Only its dedup comment is rewritten, because the reason changed: a duplicate would now break the job's single `executemany`, where at base it left a ghost id in the index.

(b) `Collection._process_job`. Plan C changed only its head (the index-op dispatch in `try/finally: _malloc_trim`), which stays as it is. The existing text below that head (base lines 750-777, unchanged by A, B, C and Tasks 1-2):

```python
        # flatten documents into records: (external_id, doc_id, type, position, text, metadata, vector)
        rows = []
        for d in payload["documents"]:
            if d.get("summary"):
                s = d["summary"]
                rows.append([d["doc_id"], d["doc_id"], "summary", None, s.get("text"), s.get("metadata"), s.get("vector")])
            for i, c in enumerate(d.get("chunks") or []):
                pos = c.get("position") if c.get("position") is not None else i
                rows.append([c["id"], d["doc_id"], "chunk", pos, c.get("text"), c.get("metadata"), c.get("vector")])
        if not rows:
            return
        # last occurrence wins: a duplicate external_id within one payload would
        # otherwise leave the first copy's id in the vector index (the in-batch
        # replace can't remove it — it isn't added until after the row loop)
        rows = list({r[0]: r for r in rows}.values())
        need = [i for i, r in enumerate(rows) if r[6] is None]
        for start in range(0, len(need), 64):
            batch = need[start : start + 64]
            vecs = await self.embedder.embed([rows[i][4] for i in batch])
            for i, v in zip(batch, vecs):
                rows[i][6] = v
        mat = _normalize(np.array([r[6] for r in rows], dtype=np.float32))

        async with self.lock.write():
            # off the event loop: the FTS triggers tokenize every row (expensive with
            # trigram). Batch atomicity relies on job replay + idempotent upserts, not
            # on one transaction, so an interleaved commit (e.g. enqueue) is harmless.
            ids, fresh = await asyncio.to_thread(self._upsert_rows, rows, mat)
```

Replace it with:

```python
        # flatten documents into records (external_id, doc_id, type, position, text,
        # metadata, vector) and build the matrix in worker threads: both are pure CPU
        # the event loop should not carry
        rows = await asyncio.to_thread(_payload_rows, payload)
        if not rows:
            return
        need = [i for i, r in enumerate(rows) if r[6] is None]
        for start in range(0, len(need), 64):
            batch = need[start : start + 64]
            vecs = await self.embedder.embed([rows[i][4] for i in batch])
            for i, v in zip(batch, vecs):
                rows[i][6] = v
        mat = await asyncio.to_thread(_rows_matrix, rows)

        async with self.lock.write():
            # off the event loop: the FTS triggers tokenize every row (expensive with
            # trigram). The upsert is one transaction that rolls back on any failure and
            # touches neither the index nor indexed_counts before it commits, so a
            # failed job leaves the collection as it was. The rows it replaced leave the
            # index only after that commit.
            ids, fresh, replaced = await asyncio.to_thread(self._upsert_rows, rows, mat)
            if replaced:
                await asyncio.to_thread(self._unindex, replaced)
```

Everything after it stays: `idarr = np.array(ids, dtype=np.uint64)`, the calibration-aware add loop, and the `# ponytail:` comment with its `_sync_index` call. `_payload_rows` and `_rows_matrix` are looked up as module globals at call time, so `monkeypatch.setattr(store, ...)` reaches them. A zero vector still raises `ValueError("zero vector cannot be normalized")`: `asyncio.to_thread` re-raises it in `_process_job`, and the worker finishes the job as `error`, as before.

(c) `Collection._upsert_rows`, and the new `_unindex` below it. The existing text (base lines 822-856 as Plan B Task 4 left it; C and Tasks 1-2 do not touch it):

```python
    def _upsert_rows(self, rows: list, mat: np.ndarray) -> tuple[list[int], list[bool]]:
        # fp16 originals retained on disk (half the f32 size, negligible loss vs the
        # 4-bit codes): the only way to rebuild the index representation later, since
        # turbovec can't enumerate or reconstruct vectors
        vecs16 = mat.astype(np.float16)
        ids, fresh = [], []
        with self.db_lock:
            counts = dict(self.indexed_counts)  # copy-on-write, published below
            # explicit ids, not lastrowid: records are only inserted here, in the single worker
            next_id = self.db.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0] + 1
            for n, (ext_id, doc_id, rtype, pos, text, meta, _) in enumerate(rows):
                old = self.db.execute(
                    "SELECT id, indexed, type FROM records WHERE external_id=?", (ext_id,)
                ).fetchone()
                fresh.append(old is None)
                if old:  # upsert: replace record; makes crash-replay of a job idempotent
                    if old[1]:
                        self.index.remove(old[0])
                        counts[old[2]] -= 1
                    self.db.execute("DELETE FROM records WHERE id=?", (old[0],))
                    self.db.execute("DELETE FROM vecs WHERE id=?", (old[0],))
                self.db.execute(
                    "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
                    " VALUES (?,?,?,?,?,?,?,1)",
                    (next_id, ext_id, doc_id, rtype, pos, text, json.dumps(meta or {}), ),
                )
                self.db.execute(
                    "INSERT INTO vecs(id, vec) VALUES (?,?)", (next_id, vecs16[n].tobytes())
                )
                counts[rtype] = counts.get(rtype, 0) + 1
                ids.append(next_id)
                next_id += 1
            self.indexed_counts = counts  # one reference store: readers see old or new
            self.db.commit()
        self._allow_cache.clear()
        self._df_cache_churn += len(rows)
        return ids, fresh
```

Replace it with:

```python
    def _upsert_rows(self, rows: list, mat: np.ndarray) -> tuple[list[int], list[bool], list[int]]:
        """Insert `rows` (_payload_rows' shape) with `mat` as their vectors, replacing
        any record with the same external_id: one transaction, rolled back on any
        failure. Returns (ids, fresh, replaced): the new record ids in row order, per
        row whether its external_id was new, and the replaced records' ids that are in
        the vector index. The index is not touched here: the caller (holding
        lock.write()) removes `replaced` with _unindex once this has committed, so a
        failed upsert is a pure database rollback."""
        # fp16 originals retained on disk (half the f32 size, negligible loss vs the
        # 4-bit codes): the only way to rebuild the index representation later, since
        # turbovec can't enumerate or reconstruct vectors
        vecs16 = mat.astype(np.float16)
        # row CPU before db_lock: the lock serializes every write on self.db
        blobs = [v.tobytes() for v in vecs16]
        metas = [json.dumps(r[5] or {}) for r in rows]
        with self.db_lock:
            try:
                # one IN query per 512 rows instead of a point query per row
                old = {
                    ext_id: (rid, indexed, rtype)
                    for ext_id, rid, indexed, rtype in _rows_by_id(
                        self.db,
                        "SELECT external_id, id, indexed, type FROM records WHERE external_id IN ({})",
                        [r[0] for r in rows],
                    )
                }
                # explicit ids, not lastrowid: records are only inserted here, in the single worker
                first = self.db.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0] + 1
                ids = list(range(first, first + len(rows)))
                # upsert = DELETE then INSERT in one transaction: external_id stays UNIQUE,
                # and replaying a job after a crash is idempotent
                gone = [(rid,) for rid, _, _ in old.values()]
                self.db.executemany("DELETE FROM records WHERE id=?", gone)
                self.db.executemany("DELETE FROM vecs WHERE id=?", gone)
                self.db.executemany(
                    "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
                    " VALUES (?,?,?,?,?,?,?,1)",
                    [(i, *r[:5], m) for i, r, m in zip(ids, rows, metas)],
                )
                self.db.executemany("INSERT INTO vecs(id, vec) VALUES (?,?)", list(zip(ids, blobs)))
                self.db.commit()
            except BaseException:
                # the transaction is still open after a failed statement: without this,
                # the next commit on self.db (the worker's own _finish_job('error'))
                # would persist the half-applied upsert
                self.db.rollback()
                raise
            # published after the commit, still under db_lock: copy-on-write, one rebind
            # per job, none when the transaction rolled back
            counts = dict(self.indexed_counts)
            for _, indexed, rtype in old.values():
                if indexed:
                    counts[rtype] -= 1
            for r in rows:
                counts[r[2]] = counts.get(r[2], 0) + 1
            self.indexed_counts = counts  # one reference store: readers see old or new
        self._allow_cache.clear()
        self._df_cache_churn += len(rows)
        fresh = [r[0] not in old for r in rows]
        replaced = [rid for rid, indexed, _ in old.values() if indexed]
        return ids, fresh, replaced

    def _unindex(self, rids: list[int]) -> None:
        """Remove records whose rows a committed transaction replaced or deleted from
        the vector index (the .tvim drops them at the next _sync_index). The caller
        holds lock.write(), so no search runs meanwhile; db_lock is not needed."""
        for rid in rids:
            self.index.remove(rid)
```

Notes on this edit:
- `(i, *r[:5], m)` is `(id, external_id, doc_id, type, position, text, metadata_json)`: the same 7 values, in the same order, as base's per-row INSERT.
- `rows` never repeats an external_id (`_payload_rows` dedups), so `old` has at most one entry per row and `fresh[n]` matches base's `old is None`.
- A job always has at least one row (`_process_job` returns early on an empty payload), so `_rows_by_id` never runs an empty `IN ()`.
- The rebind line keeps B's text and comment, and `counts[...]` never names `indexed_counts`, so B's source tripwire stays green. `self._allow_cache.clear()` and `self._df_cache_churn += len(rows)` keep their exact text, outside `db_lock` (Plan F finds the first by text).
- No `await` appears anywhere in the function, so B's AST pin (no await inside `with ...db_lock`) holds.

(d) `Collection._delete_doc_rows`. The existing text (base lines 1507-1523 as Plan B Task 4 left it; C and Tasks 1-2 do not touch it):

```python
    def _delete_doc_rows(self, doc_id: str) -> int:
        with self.db_lock:
            rows = self.db.execute(
                "SELECT id, indexed, type FROM records WHERE doc_id=?", (doc_id,)
            ).fetchall()
            counts = dict(self.indexed_counts)  # copy-on-write, as in _upsert_rows
            for rid, indexed, rtype in rows:
                if indexed:
                    self.index.remove(rid)
                    counts[rtype] -= 1
            self.db.execute(
                "DELETE FROM vecs WHERE id IN (SELECT id FROM records WHERE doc_id=?)", (doc_id,)
            )
            self.db.execute("DELETE FROM records WHERE doc_id=?", (doc_id,))
            self.indexed_counts = counts
            self.db.commit()
        self._allow_cache.clear()
        self._df_cache_churn += len(rows)
        return len(rows)
```

Replace it with:

```python
    def _delete_doc_rows(self, doc_id: str) -> int:
        with self.db_lock:
            try:
                rows = self.db.execute(
                    "SELECT id, indexed, type FROM records WHERE doc_id=?", (doc_id,)
                ).fetchall()
                self.db.execute(
                    "DELETE FROM vecs WHERE id IN (SELECT id FROM records WHERE doc_id=?)", (doc_id,)
                )
                self.db.execute("DELETE FROM records WHERE doc_id=?", (doc_id,))
                self.db.commit()
            except BaseException:
                self.db.rollback()  # as in _upsert_rows: the next commit must not persist half a delete
                raise
            # published after the commit, still under db_lock: copy-on-write, one rebind
            counts = dict(self.indexed_counts)
            for _, indexed, rtype in rows:
                if indexed:
                    counts[rtype] -= 1
            self.indexed_counts = counts
        # the rows are gone for good: only now do their ids leave the index
        self._unindex([rid for rid, indexed, _ in rows if indexed])
        self._allow_cache.clear()
        self._df_cache_churn += len(rows)
        return len(rows)
```

`delete_document` stays as it is: it holds `lock.write()` around this call and its `_sync_index`, which is the lock `_unindex` needs. The index removal now runs after `db_lock` is released, as in the upsert path, so an enqueue is not held up behind it. The rebind is unconditional, as in B's version: B's `[True, True]` spy expects exactly one per `delete_document`. The `SELECT id, indexed, type FROM records WHERE doc_id=?` text is unchanged; Plan C's covering-index test pins it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `22 passed` (Task 1's 8, Task 2's 9 and this task's 5).

Then run the suites that pin the code this task rewrote:

Run: `uv run pytest tests/test_concurrency.py tests/test_store_optimizations.py tests/test_ivf_index.py tests/test_cold_start.py tests/test_api.py -q`

Expected: all pass, with the same counts as on the branch base. In particular:
- B's `test_indexed_counts_is_published_only_under_db_lock` (`[True, True]`: one rebind in `_upsert_rows`, one in `_delete_doc_rows`), `test_indexed_counts_writers_rebind_never_mutate_the_published_dict`, `test_no_in_place_indexed_counts_mutation_in_store_source`, `test_no_threading_lock_is_held_across_an_await` and `test_mixed_workload_keeps_index_and_counts_consistent`.
- B's `test_stop_does_not_cancel_the_worker_inside_its_write_section`: its `slow_upsert(rows, mat)` passes the 3-tuple through, and the row still reaches the index before the close (`(1, 1)`).
- Base `test_duplicate_external_id_in_one_payload_leaves_no_ghost` (the dedup now lives in `_payload_rows`), the calibration tests (`fresh` keeps its meaning), `test_concurrent_enqueue_with_worker_loses_nothing`, and `test_ivf_index.py::test_upsert_and_delete_while_indexed` (`_unindex` ignores `_IvfIndex.remove`'s None).
- C's tests that call `_process_job` directly, and C's covering-index test on the `_delete_doc_rows` SELECT.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`

Expected: `N0 + 22 passed, 1 warning` (`275 passed, 1 warning` when N0 is 253). No existing test changes.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_ingest_path.py
git commit -F - <<'EOF'
Make upserts and deletes failure-safe; publish counts after commit

_upsert_rows is one set-based transaction (an IN lookup per 512 rows,
executemany DELETE then INSERT) that rolls back on any failure and no
longer touches the vector index. It returns the replaced indexed ids,
which _process_job removes through the new _unindex after the commit.
_delete_doc_rows rolls back the same way and unindexes after its commit.
Both publish indexed_counts once, after the commit, under db_lock,
copy-on-write. Payload flattening (_payload_rows) and the float32 matrix
build (_rows_matrix) move off the event loop into worker threads.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Review Focus for Task 3:** RF2 (a failed upsert leaves a half-applied state) is pinned here by `test_upsert_failure_rolls_back_and_keeps_index`, together with `test_delete_failure_rolls_back_and_keeps_index` for the delete path. When reviewing, check that:
- neither `_upsert_rows` nor `_delete_doc_rows` calls `index.remove` or rebinds `indexed_counts` before `self.db.commit()`, and every statement of each transaction sits inside the `try` whose `except BaseException` rolls back and re-raises;
- the `indexed_counts` rebind comes after the commit, under `db_lock`, exactly once per call (`test_indexed_counts_published_after_commit` and B's `[True, True]` spy);
- `_unindex` runs only after the commit, under the caller's `lock.write()`, and `replaced` holds only the old ids with `indexed=1`, never all replaced rows (Task 7's removal cap counts `len(replaced)`);
- the DELETEs run before the INSERTs in the same transaction, so `external_id UNIQUE` holds and a crash replay stays idempotent;
- the old-row lookup is set-based (`test_upsert_old_row_lookup_is_set_based`), and `_payload_rows` / `_rows_matrix` run off the loop (`test_ingest_prep_runs_off_the_event_loop`);
- out of scope, and unchanged from base: `index.add_with_ids`, calibration or `_unindex` failing after the commit still leaves an `error` job with committed rows missing from the index. `_reconcile_ghosts` only evicts; it never re-adds. Task 6's ADR addendum states this.

### Task 4: Vacuum policy

Goal: stop paying a full `PRAGMA incremental_vacuum` after every job while a backlog drains (item2a measured 38-54 ms per job, against 7-20 ms when it runs only at idle), while keeping `meta.db` bounded. After each job's finish commit, still under `db_lock`, the new `_vacuum_after_finish` does one of three things:
- no open job (the queue is empty): a full `PRAGMA incremental_vacuum`, as at base;
- open jobs and `PRAGMA freelist_count >= VACUUM_FREELIST_PAGES`: `PRAGMA incremental_vacuum(n)` with `n = freelist - VACUUM_FREELIST_PAGES + VACUUM_CHUNK_PAGES`, which trims the freelist back to `VACUUM_FREELIST_PAGES - VACUUM_CHUNK_PAGES`;
- otherwise nothing.

So during a backlog the freelist is below `VACUUM_FREELIST_PAGES` (64 MB at 4 KiB pages) after every finish, however many pages one job frees. Each trimming vacuum costs about the pages freed since the last one plus `VACUUM_CHUNK_PAGES` (8 MB), so an enqueue never waits behind a whole-file vacuum. Both vacuum pragmas are drained with `.fetchall()` (spec §4.3).

**Files:**
- Modify `src/raggio/store.py` (base line numbers, orientation only; anchor on the quoted text):
  - module constants `VACUUM_FREELIST_PAGES = 16_384` and `VACUUM_CHUNK_PAGES = 2_048`, directly below `CAL_SAMPLE = 1024 ...` (base line 68). Plan F later rewrites the `# Optional ScaNN-style IVF index ...` comment below them; it anchors on that comment's own text, which this task leaves alone;
  - `Collection._finish_job` (base lines 708-721, as Task 2 left it): the post-commit vacuum comment and `PRAGMA incremental_vacuum` line become one call to `self._vacuum_after_finish()`;
  - new `Collection._vacuum_after_finish`, directly below `_finish_job` (above `async def _run_worker`).
- Modify `docs/storage.md`, `## Ingest durability`: one sentence appended to item 3 as Task 2 left it.
- Modify `tests/test_ingest_path.py`: append the section `# ---- vacuum policy` (one helper, two tests). The module head is unchanged.
- Test: `uv run pytest tests/test_ingest_path.py -v`.

**Interfaces:**
- Consumes:
  - Task 2: `Collection._finish_job(self, job_id: int, status: str, error: str | None) -> None` (the jobs UPDATE, the `job_payloads` DELETE on `done`, one commit, all under `db_lock`) and `Collection._enqueue_row(self, payload: dict) -> int` (encodes, then inserts the `jobs` and `job_payloads` rows in one transaction under `db_lock`). The tests call both directly, without a worker.
  - Plan C: the partial index `idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')`. C's `test_open_job_queries_use_the_partial_index` asserts that the plan of the exact string `SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')` uses it. `_vacuum_after_finish` runs that same string, so it counts open jobs without scanning the journal.
  - Base `open_meta_db`: `PRAGMA auto_vacuum=INCREMENTAL` before `journal_mode=WAL` (pinned by `test_fresh_meta_db_has_incremental_autovacuum`), so `incremental_vacuum` works on every meta.db this code creates.
  - Test helpers `make_collection`, `vec` (Task 1) and `fetch` (Task 2).
- Produces:
  - `VACUUM_FREELIST_PAGES = 16_384` and `VACUUM_CHUNK_PAGES = 2_048`: module-level algorithm constants (spec §4.2), read through the module globals at call time, so tests monkeypatch them on `raggio.store`. They are not `Settings` knobs.
  - `Collection._vacuum_after_finish(self) -> None`: the caller holds `db_lock` and has just committed, so no transaction is open. It runs no DML, so it opens none either.
  - Test helper `big_payload(job: int, dim: int = 8) -> dict`: one document of 50 chunks of 5000 characters (external ids `v<job>-<j>`), about 62 pages of `job_payloads` data at SQLite's default 4 KiB page size.
  - For D2 (Tasks 8 and 9): the batched flush still calls `_finish_job` once per job. While other jobs of the batch are still `processing`, those finishes only trim, and only past the threshold; the finish that leaves no open job runs the full vacuum.

- [ ] **Step 1: Write the failing tests**

Append this section at the end of `tests/test_ingest_path.py`, below Task 3's section. Both tests call `_enqueue_row` and `_finish_job` directly from the test thread, with no worker, so the order of the finishes is fixed and no record insert reuses the freed pages. They read `PRAGMA freelist_count` on `col.db`, as the base `test_job_payload_pages_are_returned_after_processing` does.

```python
# ---- vacuum policy


def big_payload(job, dim=8):
    """One document of 50 chunks of 5000 characters: about 62 pages of job_payloads
    data at SQLite's default 4 KiB page size, all freed when the job finishes done."""
    return {"documents": [{"doc_id": f"v{job}", "summary": None, "chunks": [
        {"text": "x" * 5000, "vector": vec(j, dim), "metadata": None,
         "id": f"v{job}-{j}", "position": None}
        for j in range(50)
    ]}]}


def test_vacuum_is_deferred_while_jobs_are_open(tmp_path):
    # before: every _finish_job ran a full incremental_vacuum, even with a backlog
    # queued behind it. Now only the finish that leaves the queue empty does. The
    # default VACUUM_FREELIST_PAGES (16_384) is far above five jobs' ~310 pages, so no
    # trimming vacuum runs either
    col = make_collection(tmp_path)
    try:
        ids = [col._enqueue_row(big_payload(n)) for n in range(5)]
        traced = []
        col.db.set_trace_callback(traced.append)
        try:
            vacuums = []
            for job_id in ids:
                traced.clear()
                col._finish_job(job_id, "done", None)
                vacuums.append([s for s in traced if "incremental_vacuum" in s])
        finally:
            col.db.set_trace_callback(None)
        assert vacuums == [[], [], [], [], ["PRAGMA incremental_vacuum"]]
        assert not col.db.in_transaction
        assert col.db.execute("PRAGMA freelist_count").fetchone()[0] <= 1
        assert fetch(col, "SELECT COUNT(*) FROM job_payloads") == [(0,)]
    finally:
        asyncio.run(col.stop())


def test_freelist_bounded_during_backlog(tmp_path, monkeypatch):
    # deferring the full vacuum must not let a backlog grow meta.db without bound: a
    # finish that sees VACUUM_FREELIST_PAGES or more free pages trims the freelist back
    # below it, however many pages one job freed (~62 here, against a chunk of 16)
    monkeypatch.setattr(store, "VACUUM_FREELIST_PAGES", 64)
    monkeypatch.setattr(store, "VACUUM_CHUNK_PAGES", 16)
    col = make_collection(tmp_path)
    try:
        ids = [col._enqueue_row(big_payload(n)) for n in range(20)]
        # the backlog's payloads span far more pages than the threshold (~1240 vs 64)
        [(total,)] = fetch(col, "SELECT SUM(LENGTH(data)) FROM job_payloads")
        assert total > 20 * 50 * 5000
        free = []
        for job_id in ids:
            col._finish_job(job_id, "done", None)
            free.append(col.db.execute("PRAGMA freelist_count").fetchone()[0])
        backlog, idle = free[:-1], free[-1]
        assert max(backlog) <= 64, backlog  # bounded while jobs are open ...
        assert max(backlog) > 1, backlog  # ... without a full vacuum at every finish
        assert idle <= 1  # the finish that empties the queue vacuums in full
    finally:
        asyncio.run(col.stop())
```

Why these numbers: with the threshold at 64 and the chunk at 16, the policy leaves the freelist below 64 after a finish that trims nothing, and at 48 (64 - 16) after one that trims. A policy that only vacuums at idle ends the backlog near 1180 free pages. A fixed `incremental_vacuum(16)` per trim adds about 46 pages per job and passes 64 by the second or third finish. Both fail the first assertion. If the host's SQLite uses a larger default page size, each job frees fewer pages, and the assertions still hold: the payloads still total over 5 MB, far above 64 pages.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `2 failed, 22 passed` (Tasks 1-3's 22 pass):
- `test_vacuum_is_deferred_while_jobs_are_open` fails its first assertion: `assert [['PRAGMA incremental_vacuum'], ['PRAGMA incremental_vacuum'], ['PRAGMA incremental_vacuum'], ['PRAGMA incremental_vacuum'], ['PRAGMA incremental_vacuum']] == [[], [], [], [], ['PRAGMA incremental_vacuum']]`, because every finish still runs the full vacuum.
- `test_freelist_bounded_during_backlog` fails in its first line with `AttributeError: <module 'raggio.store' from '...'> has no attribute 'VACUUM_FREELIST_PAGES'` (`monkeypatch.setattr` checks that the attribute exists).

- [ ] **Step 3: Implement the vacuum policy**

(a) Module constants, next to the calibration constants. The existing text (base lines 67-68, unchanged by A, B, C and Tasks 1-3):

```python
CAL_THRESHOLD = 10_000
CAL_SAMPLE = 1024  # ~1024 representative rows is enough per turbovec docs
```

Replace it with:

```python
CAL_THRESHOLD = 10_000
CAL_SAMPLE = 1024  # ~1024 representative rows is enough per turbovec docs

# meta.db vacuum policy after a job finishes (Collection._vacuum_after_finish): a full
# incremental_vacuum only once no job is open; while a backlog drains, a freelist of
# VACUUM_FREELIST_PAGES or more (64 MB at 4 KiB pages) is trimmed back to
# VACUUM_FREELIST_PAGES - VACUUM_CHUNK_PAGES, so one trim returns about the pages freed
# since the last trim plus 8 MB and never stalls enqueues on a whole-file vacuum
VACUUM_FREELIST_PAGES = 16_384
VACUUM_CHUNK_PAGES = 2_048
```

The blank line and the `# Optional ScaNN-style IVF index ...` comment that follow stay as they are.

(b) `Collection._finish_job` and the new `Collection._vacuum_after_finish`, in one edit. The existing text is the tail of `_finish_job` as Task 2 left it (its `DELETE FROM job_payloads` lines, then the base post-commit comment and vacuum line), the blank line after it, and the first line of `_run_worker` (unchanged by Task 2):

```python
            if status == "done":
                self.db.execute("DELETE FROM job_payloads WHERE job_id=?", (job_id,))
            self.db.commit()
            # return the cleared payload's pages to the OS between jobs. fetchall() is
            # load-bearing: the pragma frees pages per STEP, and pysqlite's execute()
            # steps once — without exhausting the cursor it frees a single page
            self.db.execute("PRAGMA incremental_vacuum").fetchall()

    async def _run_worker(self) -> None:
```

(The comment's `—` is a real em dash. `store.py` is UTF-8.) Replace it with:

```python
            if status == "done":
                self.db.execute("DELETE FROM job_payloads WHERE job_id=?", (job_id,))
            self.db.commit()
            # return freed pages to the OS: in full once the queue is empty, in bounded
            # trims while a backlog drains
            self._vacuum_after_finish()

    def _vacuum_after_finish(self) -> None:
        """Return freed meta.db pages to the OS after a job's finish commit (caller
        holds db_lock; no transaction is open). A full incremental_vacuum after every
        job cost 38-54 ms per job while a backlog drained, so it runs only once no job
        is open. Until then a freelist of VACUUM_FREELIST_PAGES or more is trimmed
        back to VACUUM_FREELIST_PAGES - VACUUM_CHUNK_PAGES: the file stays bounded
        however many pages one job freed, and no trim is a whole-file vacuum that
        holds enqueues behind db_lock."""
        # the literal predicate, byte for byte pending_jobs' query: idx_jobs_open
        # serves it, so counting open jobs never scans the journal
        open_jobs = self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
        ).fetchone()[0]
        # fetchall() is load-bearing on both vacuum pragmas: the pragma frees pages per
        # STEP, and pysqlite's execute() steps once -- without exhausting the cursor it
        # frees a single page
        if open_jobs == 0:
            self.db.execute("PRAGMA incremental_vacuum").fetchall()
            return
        free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
        if free >= VACUUM_FREELIST_PAGES:
            # a pragma argument cannot be a bound parameter; n is an int computed here
            n = int(free - VACUUM_FREELIST_PAGES + VACUUM_CHUNK_PAGES)
            self.db.execute(f"PRAGMA incremental_vacuum({n})").fetchall()

    async def _run_worker(self) -> None:
```

Notes on this edit:
- `_finish_job` keeps its name, its arity `(job_id, status, error)` (B's `slow_finish` pin) and its single `with self.db_lock:` block. The vacuum still runs inside that block, after the commit, as at base.
- The old `.fetchall()` comment moves into `_vacuum_after_finish`, in ASCII (`--` for the em dash), and now covers both pragmas (spec §4.3).
- `VACUUM_FREELIST_PAGES` and `VACUUM_CHUNK_PAGES` are read as module globals at call time, so `monkeypatch.setattr(store, ...)` takes effect.
- The method runs only SELECT and PRAGMA statements in autocommit mode, so `col.db.in_transaction` stays False after it. There is no `await` in it (B's AST test).

(c) `docs/storage.md`, `## Ingest durability`. The existing text is item 3 as Task 2 left it:

```markdown
3. The job is marked `done` only after the vector index is synced to disk.
   A successful job's payload row is deleted so vector-heavy jobs don't
   accumulate; a failed job keeps it for diagnosis. A payload that cannot
   be read fails its job with a `bad job payload: ...` error.
```

Replace it with (one sentence appended):

```markdown
3. The job is marked `done` only after the vector index is synced to disk.
   A successful job's payload row is deleted so vector-heavy jobs don't
   accumulate; a failed job keeps it for diagnosis. A payload that cannot
   be read fails its job with a `bad job payload: ...` error. Freed pages
   go back to the OS once the queue is empty; while a backlog drains,
   `meta.db` keeps at most 16,384 free pages (64 MB at SQLite's default
   4 KiB page size) and trims the excess as each job finishes.
```

The new text is ASCII. Nothing else in the file changes.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `24 passed` (Task 1's 8, Task 2's 9, Task 3's 5 and this task's 2).

Then run the suites that pin the vacuum, the finish and the job queries:

Run: `uv run pytest tests/test_store_optimizations.py tests/test_cold_start.py tests/test_concurrency.py tests/test_ivf_index.py tests/test_api.py -q`

Expected: all pass, with the same counts as on the branch base. In particular:
- base `test_job_payload_pages_are_returned_after_processing`: its single job's finish sees no open job, so the full vacuum runs and `freelist_count <= 1` holds;
- base `test_fresh_meta_db_has_incremental_autovacuum` (`open_meta_db` is untouched);
- C's `test_open_job_queries_use_the_partial_index`: its EXPLAIN of `SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')` covers the open-job count `_vacuum_after_finish` runs, since the text is the same;
- B's stop tests, including the one whose `slow_finish(job_id, status, error)` wraps `_finish_job` (the arity is unchanged), and B's `test_no_threading_lock_is_held_across_an_await`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`

Expected: `N0 + 24 passed, 1 warning` (`277 passed, 1 warning` when N0 is 253). No existing test changes.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_ingest_path.py docs/storage.md
git commit -F - <<'EOF'
Defer the full meta.db vacuum to an idle queue; trim it in a backlog

_finish_job no longer runs a full PRAGMA incremental_vacuum after every
job. The new _vacuum_after_finish runs it only once no job is open.
While a backlog drains, it trims a freelist of VACUUM_FREELIST_PAGES
(16_384) or more back to VACUUM_FREELIST_PAGES - VACUUM_CHUNK_PAGES
(2_048), so meta.db stays bounded and no trim is a whole-file vacuum
under db_lock. Both vacuum pragmas are drained with fetchall().

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Review Focus for Task 4:** the header assigns none of RF1-RF5 to this task. Its own pins are `test_vacuum_is_deferred_while_jobs_are_open` and `test_freelist_bounded_during_backlog`, with the base `test_job_payload_pages_are_returned_after_processing` for the idle case. When reviewing, check that:
- `_vacuum_after_finish` runs only inside `_finish_job`'s `with self.db_lock:` block, after `self.db.commit()`, and never opens a transaction (no DML; `col.db.in_transaction` is False after it);
- the open-job count uses the literal `status IN ('pending','processing')` predicate, byte for byte `pending_jobs`' text, so `idx_jobs_open` serves it (spec §4.1);
- both vacuum pragmas end in `.fetchall()` (spec §4.3), and `PRAGMA freelist_count` is read with `fetchone()`;
- the trim size is `free - VACUUM_FREELIST_PAGES + VACUUM_CHUNK_PAGES`, not a fixed `VACUUM_CHUNK_PAGES`. A fixed chunk leaves the freelist growing whenever a job frees more than one chunk, which the backlog test catches;
- the constants are module-level and read at call time (spec §4.2: algorithm constants, not `Settings` knobs);
- D2 (Tasks 8 and 9) keeps per-job `_finish_job` calls, so within a flushed batch only the finish that leaves no open job runs the full vacuum.

### Task 5: bench/ingest_probe.py

Goal: an in-process drain benchmark that measures ingest vec/s on the flat and the IVF path, and that runs unchanged in the base tree for Task 6's A/B. `bench.py` cannot do this. Its flat ingest is a ~31 min HTTP reingest of 2.55M rows, and `--engine raggio-ivf --reingest` only rebuilds the index, so it never measures IVF ingest (Plan A's `decide()`). The probe drives one `Collection` in its own process:
1. prefill `--prefill` rows through `_process_job`, in 2000-row payloads (so a large prefill calibrates at `CAL_THRESHOLD`, like a real collection); with `--ivf N`, attach an IVF index and run one index sync before the clock starts;
2. start the worker; `--enqueuers` tasks enqueue `--jobs` bench-shaped payloads of `--job-rows` rows while the worker drains them; every `--reupsert-every`-th job re-upserts existing rows (the removal path). The clock stops when `pending_jobs()` reaches 0;
3. report vec/s, enqueue time, event-loop stalls from a 5 ms ticker, job statuses, the freelist, and a fingerprint of the final state.

**Files:**
- Create `bench/ingest_probe.py`: a port of `D:/DEV/_scratch/probe-item2a/ab_probe.py`. Compared with that script it drops the `proto` plugin and the variant list, and it drops the reload, FTS and top-10 fingerprints (IVF top-10 differs between any two builds). It builds the prefill payloads lazily, one 2000-row payload at a time (100k x 1024 floats as Python lists would take about 3.3 GB). It takes a CLI and returns the result from `main`. ASCII only.
- Modify `tests/test_ingest_path.py`: append the section `# ---- bench/ingest_probe.py` (a module constant `PROBE`, two helpers and three tests) below Task 4's section. The module head is unchanged: the loader imports `importlib.util` locally, as Plan F's `load_probe` does.
- Not touched: `src/`, `bench/bench.py`, `bench/dgx/*.sh` (spec §4.1: new probe files only).
- Test: `uv run pytest tests/test_ingest_path.py -v`.

**Interfaces:**
- Consumes. Every name below exists on `main` after Plan C and also at 19a8a2e; the probe uses nothing Tasks 1-4 added, which is what lets a copy of it run in the base tree:
  - `Collection(cfg, directory, embedder_factory, set_index_config=None)` and `CollectionConfig(name, dim, bit_width, model, base_url, key_hash, ...)`;
  - `await Collection._process_job(payload: dict) -> None`, for the prefill payloads and for `{"op": "attach_index", "nlist": N, "nprobe": None}` (Plan C wraps index jobs in its `_malloc_trim` `finally`; an attach that fails raises out of `_process_job`);
  - `Collection._sync_index(self) -> None` and `Collection.index_info(self) -> dict` (`{"type": "flat"}` or `{"type": "ivf", "nlist": ..., "nprobe": ...}`);
  - `await Collection.enqueue(payload: dict) -> int`, `Collection.start_worker() -> None`, `Collection.pending_jobs() -> int` (a read connection, callable on the loop) and `await Collection.stop() -> None`;
  - the attributes `db`, `db_lock`, `index` (`len()`), `indexed_counts`, `_worker` and `_cal_rng`;
  - `raggio.store.IVF_MIN_ROWS` (1024), read by the attach build at call time, so the IVF test monkeypatches it on the module.
  - Test side: the module imports `json`, `sqlite3`, `sys`, `Path`, `pytest` and `store` from Task 1's head. No earlier helper is needed.
- Produces:
  - `bench/ingest_probe.py` with `main(argv: list[str] | None = None) -> dict`. It prints the result as one JSON line on stdout and, with `--out FILE`, also writes it indented to FILE.
  - The CLI, with defaults: `--dim 1024 --prefill 100000 --jobs 200 --job-rows 250 --reupsert-every 5 --enqueuers 2 --ivf 0 --seed 42 --tmp DIR --out FILE`. `--ivf` is the nlist (0 = flat). `--reupsert-every 0` disables re-upserts. `--tmp` is the parent of the throwaway collection dir (default: the system temp dir), and the dir is deleted at the end. Bad values exit with argparse's usage error (status 2): any of `--dim`, `--jobs`, `--job-rows`, `--enqueuers` below 1; `--prefill`, `--reupsert-every`, `--ivf` or `--seed` below 0; or re-upsert blocks that do not fit in the prefill (`ceil(jobs / reupsert_every) * job_rows > prefill`).
  - Result keys:
    - timing: `ingest_vps` (= `rows / drain_s`), `drain_s` (worker start to `pending_jobs() == 0`), `enqueue_s` (worker start to the last enqueue returning), `prefill_s`;
    - event loop: `loop_stall_s` (the sum of the 5 ms ticker's lateness over the ticks later than 20 ms), `loop_lag_max_ms`;
    - `rows` (= `jobs * job_rows`) and `index` (`index_info()` at the end);
    - final state: `jobs` (`{status: count}`), `freelist` (`PRAGMA freelist_count`), `payload_rows` (rows left in `job_payloads`, or `None` in a tree without that table), `fingerprint` = `{"records", "vecs", "counts", "n_index"}`;
    - provenance and labels: `store_file` (`raggio.store.__file__`, which says which tree was measured), `sqlite_version`, `turbovec` (the dist version, or `None`), `python`, `machine`, `openblas_num_threads` (the env value or `None`), `regime` (always `"host-warm, uncapped host process"`), and `args` (the parsed arguments).
  - Module helpers: `build_parser() -> argparse.ArgumentParser`, `make_payload(ids: range, dim: int, tag: str, rng: np.random.Generator) -> dict`, `make_jobs(args: argparse.Namespace) -> list[dict]`, `async timed_drain(col, payloads: list[dict], enqueuers: int) -> tuple[float, float, list[float]]` (`drain_s`, `enqueue_s`, tick lags in s), `final_state(col) -> dict` (the four final-state keys), `async run(args, work: Path) -> dict`, and `_version(dist: str) -> str | None`.
  - Determinism: for the same arguments, `fingerprint` is the same on every run and every correct tree. Records are hashed by `external_id` (never by rowid), and re-upsert blocks never overlap, so the order in which concurrent enqueuers land their jobs does not matter. Calibration is seeded through `_cal_rng`, although the fingerprint does not depend on it.
  - For Task 7: in `run()`, the line `col = Collection(CollectionConfig("probe", args.dim, 4, None, None, None), work, lambda: None)` is the anchor for forwarding `--batch-jobs` / `--batch-ms`.
  - Test helpers: the module constant `PROBE` (the probe's absolute path), `load_probe(monkeypatch)` and `probe_args(tmp_path, **over) -> list[str]` (keyword overrides of the tiny default run; `job_rows=` becomes `--job-rows`).

- [ ] **Step 1: Write the failing tests**

Append this section at the end of `tests/test_ingest_path.py`, below Task 4's section. Every run is tiny (dim 16, at most 400 prefill rows and 6 jobs), so the three tests take a few seconds together. `load_probe` swaps `sys.path` for a copy first, so the `src/` entry the probe inserts at import is gone after each test. The probe finds `raggio.store` already imported by this module, so a `monkeypatch.setattr(store, ...)` reaches the probe too.

```python
# ---- bench/ingest_probe.py

PROBE = Path(__file__).resolve().parents[1] / "bench" / "ingest_probe.py"


def load_probe(monkeypatch):
    """Import bench/ingest_probe.py as a module. The probe puts its tree's src/ first
    on sys.path at import; patching in a copy of sys.path undoes that after the test."""
    import importlib.util

    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("ingest_probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def probe_args(tmp_path, **over):
    """argv for a tiny probe run: dim 16, 200 prefill rows, 4 jobs x 20 rows, job 0
    re-upserting prefill rows 0-19. Keywords override a flag ('_' for '-')."""
    opts = {"dim": 16, "prefill": 200, "jobs": 4, "job_rows": 20, "reupsert_every": 5,
            "enqueuers": 2, "seed": 1, "tmp": tmp_path, **over}
    argv = []
    for name, value in opts.items():
        argv += ["--" + name.replace("_", "-"), str(value)]
    return argv


def test_ingest_probe_runs_flat_and_reports(tmp_path, monkeypatch):
    probe = load_probe(monkeypatch)
    assert sys.path[0] == str(PROBE.parents[1] / "src")  # measures its own tree's code
    out_file = tmp_path / "probe.json"
    out = probe.main(probe_args(tmp_path, out=out_file))
    assert {
        "ingest_vps", "drain_s", "enqueue_s", "loop_stall_s", "loop_lag_max_ms", "rows",
        "jobs", "freelist", "fingerprint", "store_file", "sqlite_version", "turbovec", "args",
    } <= set(out)
    assert out["jobs"] == {"done": 4}
    assert out["rows"] == 80  # 4 jobs x 20 rows, the re-upsert included
    assert out["index"] == {"type": "flat"}
    # 200 prefill rows + 3 fresh jobs x 20; job 0 replaced 20 prefill rows in place
    assert out["fingerprint"]["counts"] == {"chunk": 260}
    assert out["fingerprint"]["n_index"] == 260
    assert out["freelist"] <= 1  # the queue went idle, so the last finish vacuumed in full
    assert out["payload_rows"] == 0  # a done job's job_payloads row is deleted (Task 2)
    assert out["ingest_vps"] == pytest.approx(80 / out["drain_s"])
    assert 0 < out["enqueue_s"] <= out["drain_s"]
    assert out["loop_stall_s"] >= 0 and out["loop_lag_max_ms"] >= 0
    assert out["store_file"] == store.__file__
    assert out["sqlite_version"] == sqlite3.sqlite_version
    assert out["regime"] == "host-warm, uncapped host process"
    assert (out["args"]["seed"], out["args"]["ivf"], out["args"]["job_rows"]) == (1, 0, 20)
    assert json.loads(out_file.read_text(encoding="utf-8")) == out


def test_ingest_probe_runs_ivf(tmp_path, monkeypatch):
    # bench.py never ingests into an IVF collection; the probe attaches one (nlist 4)
    # before the clock starts, so every timed job syncs the IVF shards
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)
    probe = load_probe(monkeypatch)
    out = probe.main(probe_args(tmp_path, prefill=400, ivf=4, seed=3))
    assert out["index"]["type"] == "ivf" and out["index"]["nlist"] == 4
    assert out["jobs"] == {"done": 4}
    assert out["rows"] == 80
    assert out["fingerprint"]["counts"] == {"chunk": 460}  # 400 + 3 fresh jobs x 20
    assert out["fingerprint"]["n_index"] == 460
    assert out["payload_rows"] == 0
    assert out["ingest_vps"] > 0


def test_ingest_probe_fingerprint_is_deterministic(tmp_path, monkeypatch):
    # three enqueuers race to land six jobs (jobs 0 and 3 re-upsert disjoint prefill
    # blocks), so the journal order differs between runs; the final state must not
    probe = load_probe(monkeypatch)
    shape = {"prefill": 100, "jobs": 6, "job_rows": 10, "reupsert_every": 3, "enqueuers": 3}
    a = probe.main(probe_args(tmp_path, seed=7, **shape))
    b = probe.main(probe_args(tmp_path, seed=7, **shape))
    c = probe.main(probe_args(tmp_path, seed=8, **shape))
    assert a["jobs"] == b["jobs"] == c["jobs"] == {"done": 6}
    assert a["fingerprint"] == b["fingerprint"]
    assert a["fingerprint"]["counts"] == {"chunk": 140}  # 100 + 4 fresh jobs x 10
    assert a["fingerprint"]["n_index"] == 140
    # the fingerprint does see the data: another seed gives other texts and vectors
    assert c["fingerprint"]["records"] != a["fingerprint"]["records"]
    assert c["fingerprint"]["vecs"] != a["fingerprint"]["vecs"]
```

The tests do not check that the throwaway collection dir is gone. The probe deletes it with `shutil.rmtree(..., ignore_errors=True)`, as `CollectionManager.delete_collection` does, and warns on stderr if it survives. Whether a Windows host still has a file open in it right after `stop()` is not something these tests should depend on.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `3 failed, 24 passed` (Tasks 1-4's 24 pass). All three new tests fail inside `load_probe`, at `spec.loader.exec_module(mod)`, with `FileNotFoundError: [Errno 2] No such file or directory: '<repo>/bench/ingest_probe.py'` (backslashes on Windows). `spec_from_file_location` does not check that the file exists; the loader's read does.

- [ ] **Step 3: Create `bench/ingest_probe.py`**

Create the file with the four parts below, in this order, as one ASCII module.

(a) Docstring, imports and constants. The `sys.path` insert comes before the `raggio` imports, so the tree the probe sits in is the tree it measures. The one exception: when `raggio.store` is already imported (as under pytest), the import returns that module.

```python
"""In-process ingest drain benchmark for the flat and IVF paths (Plan D, Task 5).

bench.py measures flat ingest end to end over HTTP (about 31 min for 2.55M rows) and
never measures IVF ingest: `--engine raggio-ivf --reingest` only rebuilds the index.
This probe drives one Collection in-process instead:

1. prefill --prefill rows through Collection._process_job in 2000-row payloads (a
   large prefill calibrates at CAL_THRESHOLD, like a real collection); with --ivf N,
   attach an IVF index of nlist N and run one index sync, so the full write that
   follows an attach is not timed;
2. start the worker and let --enqueuers tasks enqueue --jobs payloads of --job-rows
   rows each (IngestIn.model_dump() shape, vectors rounded to 5 decimals as in the
   bench) while the worker drains them. Every --reupsert-every-th job replaces
   existing rows (the removal path). The clock stops at pending_jobs() == 0;
3. report ingest_vps = jobs * job_rows / drain_s, the event-loop lag seen by a 5 ms
   ticker, the job statuses, the freelist and a fingerprint of records, vecs, counts
   and index size. The same arguments give the same fingerprint on every correct
   tree. IVF search results are not fingerprinted: two builds train differently.

It puts its own tree's src/ first on sys.path and uses only APIs that main had before
Plan D, so a copy dropped into an older checkout measures that checkout's code
(store_file in the result says which). The rows come from an uncapped host process
on a warm page cache: label them so, never as container rows. The timed payloads are
built before the clock starts: about 1.7 GB of Python floats at the defaults.

Usage: uv run python bench/ingest_probe.py [--ivf 256] [--tmp DIR] [--out FILE]
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # this tree's raggio, not an installed copy
import raggio.store as store  # noqa: E402
from raggio.store import Collection, CollectionConfig  # noqa: E402

PREFILL_CHUNK = 2_000  # rows per prefill payload
WORDS = [f"w{i}" for i in range(30_000)]  # zipf-drawn vocabulary of the chunk texts
WORDS_PER_ROW = 250
TICK_S = 0.005  # period of the event-loop lag ticker
STALL_S = 0.020  # a tick later than this counts toward loop_stall_s
POLL_S = 0.01  # pending_jobs() poll period while the queue drains
```

(b) The argument parser and the payload builders:

```python
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="In-process ingest drain benchmark (flat or IVF).")
    p.add_argument("--dim", type=int, default=1024)
    p.add_argument("--prefill", type=int, default=100_000, help="rows ingested before the clock starts")
    p.add_argument("--jobs", type=int, default=200, help="timed ingest jobs")
    p.add_argument("--job-rows", type=int, default=250, help="rows per timed job")
    p.add_argument("--reupsert-every", type=int, default=5,
                   help="every Nth job replaces existing rows (0 = never)")
    p.add_argument("--enqueuers", type=int, default=2, help="concurrent enqueue tasks")
    p.add_argument("--ivf", type=int, default=0, help="attach an IVF index of this nlist (0 = flat)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--tmp", default=None,
                   help="parent dir of the throwaway collection (default: the system temp"
                        " dir); put it on the disk under test")
    p.add_argument("--out", default=None, help="also write the result JSON to this file")
    return p


def make_payload(ids: range, dim: int, tag: str, rng: np.random.Generator) -> dict:
    """One bench-shaped ingest payload in IngestIn.model_dump() field order: per id a
    document arxiv/<id> with one chunk r<id>, a zipf text of WORDS_PER_ROW words and
    a float vector rounded to 5 decimals."""
    vecs = np.round(rng.standard_normal((len(ids), dim)) / np.sqrt(dim), 5)
    words = np.minimum(rng.zipf(1.2, size=(len(ids), WORDS_PER_ROW)), len(WORDS) - 1).tolist()
    return {"documents": [
        {"doc_id": f"arxiv/{i}", "summary": None, "chunks": [{
            "text": tag + " " + " ".join(WORDS[w] for w in ws),
            "vector": v.tolist(), "metadata": {"year": 2019 + i % 5},
            "id": f"r{i}", "position": None,
        }]}
        for i, v, ws in zip(ids, vecs, words)
    ]}


def make_jobs(args: argparse.Namespace) -> list[dict]:
    """The timed payloads. Job j adds the fresh rows prefill + j*job_rows onwards,
    except every reupsert_every-th job, which replaces prefill block m = j //
    reupsert_every. The blocks never overlap, so the final state does not depend on
    the order in which concurrent enqueuers land their jobs."""
    jobs = []
    for j in range(args.jobs):
        rng = np.random.default_rng([args.seed, 2, j])
        if args.reupsert_every and j % args.reupsert_every == 0:
            start, tag = j // args.reupsert_every * args.job_rows, "re"
        else:
            start, tag = args.prefill + j * args.job_rows, "new"
        jobs.append(make_payload(range(start, start + args.job_rows), args.dim, tag, rng))
    return jobs
```

(c) The timed drain and the final-state read:

```python
async def timed_drain(col: Collection, payloads: list[dict], enqueuers: int) -> tuple[float, float, list[float]]:
    """Start the worker, enqueue the payloads from `enqueuers` tasks (round-robin
    split) and wait for an empty queue. Returns (drain_s, enqueue_s, lags): the time
    from the worker start to pending_jobs() == 0 and to the last enqueue returning,
    and how late each 5 ms tick of a loop ticker woke up, in seconds."""
    lags: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        while not done.is_set():
            t = time.perf_counter()
            await asyncio.sleep(TICK_S)
            # asyncio may wake a timer up to one clock resolution early: clamp at 0
            lags.append(max(0.0, time.perf_counter() - t - TICK_S))

    tick = asyncio.create_task(ticker())
    col.start_worker()
    t0 = time.perf_counter()
    enqueued: list[float] = []

    async def enqueuer(part: list[dict]) -> None:
        for payload in part:
            await col.enqueue(payload)
        enqueued.append(time.perf_counter() - t0)

    try:
        await asyncio.gather(*(enqueuer(payloads[i::enqueuers]) for i in range(enqueuers)))
        while col.pending_jobs():
            if col._worker.done():
                col._worker.result()  # re-raises whatever killed the worker
                raise RuntimeError(f"the worker exited with {col.pending_jobs()} jobs open")
            await asyncio.sleep(POLL_S)
        drain_s = time.perf_counter() - t0
    finally:
        done.set()
        await tick
    return drain_s, max(enqueued), lags


def final_state(col: Collection) -> dict:
    """Job statuses, freelist, leftover payload rows and the state fingerprint. Read on
    the write connection under db_lock: pending_jobs() can read 0 on a read connection
    while the last finish is still vacuuming in the worker's thread."""
    with col.db_lock:
        db = col.db
        jobs = dict(db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        freelist = db.execute("PRAGMA freelist_count").fetchone()[0]
        side_table = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='job_payloads'"
        ).fetchone()[0]
        payload_rows = (  # None in a tree from before Plan D's job_payloads table
            db.execute("SELECT COUNT(*) FROM job_payloads").fetchone()[0] if side_table else None
        )
        # keyed by external_id, never by rowid: rowids depend on the job order
        records = hashlib.sha256()
        for row in db.execute(
            "SELECT external_id, doc_id, type, position, text, metadata, indexed"
            " FROM records ORDER BY external_id"
        ):
            records.update(repr(row).encode())
        vecs = hashlib.sha256()
        for ext, blob in db.execute(
            "SELECT r.external_id, v.vec FROM records r JOIN vecs v ON v.id = r.id"
            " ORDER BY r.external_id"
        ):
            vecs.update(ext.encode() + b"\0" + blob)
        fingerprint = {
            "records": records.hexdigest()[:16],
            "vecs": vecs.hexdigest()[:16],
            "counts": dict(sorted(col.indexed_counts.items())),
            "n_index": len(col.index),
        }
    return {"jobs": jobs, "freelist": freelist, "payload_rows": payload_rows,
            "fingerprint": fingerprint}
```

(d) The run, the version lookup and `main`:

```python
async def run(args: argparse.Namespace, work: Path) -> dict:
    """Prefill, optionally attach IVF, then the timed drain; the result without args."""
    col = Collection(CollectionConfig("probe", args.dim, 4, None, None, None), work, lambda: None)
    try:
        col._cal_rng = np.random.default_rng([args.seed, 3])  # seeded calibration sample
        t = time.perf_counter()
        for s in range(0, args.prefill, PREFILL_CHUNK):
            ids = range(s, min(s + PREFILL_CHUNK, args.prefill))
            rng = np.random.default_rng([args.seed, 1, s])
            await col._process_job(make_payload(ids, args.dim, "pre", rng))
        if args.ivf:
            await col._process_job({"op": "attach_index", "nlist": args.ivf, "nprobe": None})
            if col.index_info()["type"] != "ivf":
                raise RuntimeError(f"attach_index nlist={args.ivf} left the index flat")
            # the first sync after an attach writes every shard in full: not timed
            await asyncio.to_thread(col._sync_index)
        prefill_s = time.perf_counter() - t
        payloads = make_jobs(args)
        drain_s, enqueue_s, lags = await timed_drain(col, payloads, args.enqueuers)
        state = await asyncio.to_thread(final_state, col)
        index = col.index_info()
    finally:
        await col.stop()
    rows = args.jobs * args.job_rows
    return {
        "ingest_vps": rows / drain_s,
        "drain_s": drain_s,
        "enqueue_s": enqueue_s,
        "prefill_s": prefill_s,
        "loop_stall_s": sum(x for x in lags if x > STALL_S),
        "loop_lag_max_ms": max(lags, default=0.0) * 1000,
        "rows": rows,
        "index": index,
        **state,
        "store_file": store.__file__,
        "sqlite_version": sqlite3.sqlite_version,
        "turbovec": _version("turbovec"),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "openblas_num_threads": os.environ.get("OPENBLAS_NUM_THREADS"),
        "regime": "host-warm, uncapped host process",
    }


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def main(argv: list[str] | None = None) -> dict:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name in ("dim", "jobs", "job_rows", "enqueuers"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be >= 1")
    for name in ("prefill", "reupsert_every", "ivf", "seed"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must be >= 0")
    if args.reupsert_every:
        blocks = -(-args.jobs // args.reupsert_every)  # re-upsert jobs 0, k, 2k, ...
        if blocks * args.job_rows > args.prefill:
            parser.error(f"{blocks} re-upsert jobs of {args.job_rows} rows need"
                         f" --prefill >= {blocks * args.job_rows}")
    work = Path(tempfile.mkdtemp(prefix="ingest-probe-", dir=args.tmp))
    try:
        result = asyncio.run(run(args, work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            print(f"warning: could not remove {work}", file=sys.stderr)
    result["args"] = dict(vars(args))
    print(json.dumps(result, sort_keys=True), flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    main()
```

Notes on the probe:
- It uses only the names listed under Consumes. They are all on `main` after Plan C (and at 19a8a2e), so Task 6 can copy this file into the base tree unchanged. At D1 the timed path goes through Tasks 2-4 (binary journal, off-loop prep, set-based upsert, vacuum policy); in the base tree it goes through the base code. `store_file` shows which one ran.
- Only the drain is timed: the payloads are built, and the prefill and the IVF attach are done, before `t0`. `enqueue_s` measures what the enqueuers pay per job (at base, the `json.dumps` on the loop; at D1, the encode in a thread). `drain_s` is the throughput figure. `ingest_vps` counts every timed row, the re-upserted ones included.
- The final read waits for `db_lock`, so it runs after the last finish's vacuum. That is why `freelist <= 1` holds once the queue is idle.
- `--tmp` decides which disk the fsyncs hit. Task 6 passes a directory on the DGX's data disk, not a tmpfs.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `27 passed` (Task 1's 8, Task 2's 9, Task 3's 5, Task 4's 2 and this task's 3).

Then smoke the CLI once per index type. Each should print one JSON line and exit 0:

Run: `uv run python bench/ingest_probe.py --dim 32 --prefill 1000 --jobs 6 --job-rows 25`

Expected: `"jobs": {"done": 6}`, `"index": {"type": "flat"}`, `"rows": 150`, `"payload_rows": 0`, `"counts": {"chunk": 1100}`, `"n_index": 1100` (jobs 0 and 5 re-upsert prefilled ids, and the other 4 add 100 new rows), a positive `ingest_vps`, and a `store_file` ending in `src/raggio/store.py` (on Windows, `src\raggio\store.py`) under this checkout.

Run: `uv run python bench/ingest_probe.py --dim 32 --prefill 2000 --jobs 6 --job-rows 25 --ivf 8`

Expected: the same `jobs`, `rows` and `payload_rows`, with `"index": {"type": "ivf", "nlist": 8, "nprobe": ...}`, `"counts": {"chunk": 2100}` and `"n_index": 2100`. At 2000 rows, `IVF_MIN_ROWS` (1024) and the `8 * nlist` floor both hold.

Then check the parser guard:

Run: `uv run python bench/ingest_probe.py --prefill 10 --jobs 4 --job-rows 20`

Expected: exit status 2 with `error: 1 re-upsert jobs of 20 rows need --prefill >= 20` on stderr. No temp directory is created, because the check runs before `mkdtemp`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`

Expected: `N0 + 27 passed, 1 warning` (`280 passed, 1 warning` when N0 is 253). No existing test changes. pytest does not collect `bench/ingest_probe.py`, because its name does not match `test_*.py`.

- [ ] **Step 6: Commit**

```bash
git add bench/ingest_probe.py tests/test_ingest_path.py
git commit -F - <<'EOF'
Add bench/ingest_probe.py, an in-process ingest drain benchmark

The probe prefills a Collection with seeded synthetic chunks
(optionally attaching an IVF index) outside the clock. It then times
how long the worker takes to drain --jobs upsert jobs of --job-rows
rows, which --enqueuers concurrent enqueuers submit; every
--reupsert-every-th job re-upserts prefilled ids. While it drains, a
5 ms ticker measures event-loop lag. The probe reports ingest_vps,
drain_s, loop_stall_s, loop_lag_max_ms, the final job counts, the
meta.db freelist and a content fingerprint, and it labels each result
with store_file, the SQLite and turbovec versions, and a host-warm,
uncapped regime.

It puts src/ first on sys.path and uses only APIs that exist before
this plan, so the same file can run in a base-tree worktree for the
A/B comparison. bench.py and bench/dgx/*.sh are unchanged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Review Focus for Task 5:** the header assigns none of RF1-RF5 to this task. Its own pins are `test_ingest_probe_runs_flat_and_reports`, `test_ingest_probe_runs_ivf` and `test_ingest_probe_fingerprint_is_deterministic`. When reviewing, check that:
- the probe reaches `raggio.store` only through the names listed under Consumes. It must use no helper added by Tasks 1-4 (`_encode_payload`, `_decode_payload`, `_vacuum_after_finish`, writes to `job_payloads`, and so on; the one read of `job_payloads` is guarded by the `sqlite_master` check), because Task 6 runs this same file in the base tree. `payload_rows` must stay `None` there;
- `sys.path.insert(0, str(ROOT / "src"))` runs before the first `raggio` import, so `store_file` names the checkout under test and not an installed wheel. The flat test pins this;
- everything outside the drain happens before `t0`: building the payloads, the prefill, the IVF attach and the first full `_sync_index`. `t0` is taken after `start_worker()`, and `drain_s` ends when `pending_jobs()` first reads 0;
- a dead worker ends the drain with an exception and cannot hang it: `_worker.done()` is checked on every poll;
- `final_state` runs in `asyncio.to_thread` and reads everything under `col.db_lock`, so it waits for the last finish's vacuum. `stop()` runs in a `finally`;
- the fingerprint is ordered by `external_id` and not by rowid, and re-upsert blocks never overlap (`m = j // k`, with the parser guard), so the enqueuer interleaving cannot change it. The determinism test pins this;
- the file is ASCII, `bench/bench.py` and `bench/dgx/*.sh` are untouched, and every result carries `"regime": "host-warm, uncapped host process"`. Task 6 must not report these rows as capped-container numbers.

### Task 6: ADR 0001 addendum and DGX A/B for D1

Goal: record D1's decisions in ADR 0001 and measure D1 against `main` after Plan C on gn100 (spec §2: "Before each plan's DGX A/B, the preceding `main` is re-baselined"; §7 D: "ingest vec/s beyond the noise band on the IVF and flat paths"). There are two sources of rows:
- **the probe** (Task 5), in-process on the host, flat and `--ivf 256`, seed 42, 3 interleaved rounds per arm after one discarded warm-up per mode. This is the only way to get IVF ingest vec/s, because `bench.py --engine raggio-ivf --reingest` only rebuilds the index (Plan A's `decide()`);
- **the bench reingest**, the end-to-end HTTP path at 2,549,119 rows: `bench.py --engine raggio --reingest`, twice per arm, interleaved base/cand/base/cand, each on a fresh 4 GiB `bench-tv`. It runs last in the session (spec §6), and only if the user grants the long window.

A declined window leaves the addendum with its `Pending:` line (Step 2 is committed either way). The gate test is appended only after a completed run.

**Files:**
- Modify `docs/adr/0001-performance-optimization-decisions.md`: append `## Addendum 2026-09 — ingest path` at the very end, after Plan C's addendum and its `### Acceptance run` block (spec D1). Step 2 writes it with a `### Results — DGX A/B (D1)` subsection that holds one `Pending:` line; Step 14 replaces that line with the rendered results (via `planD-summary.py --adr`).
- Create, outside the repo and never committed: `D:/DEV/_scratch/dgx/planD/planD-ab.sh` (the gn100 driver), `planD-build.sh` (image build), `planD-summary.py` (bands, gates, ADR block) and `planD-check.py` (laptop self-check of the other three). Fetched results land in `D:/DEV/_scratch/dgx/planD/d1/`, and pinned SHAs in `D:/DEV/_scratch/dgx/planD/d1-shas.env`.
- Modify `tests/test_ingest_path.py` (Step 12, only after a completed run): append the section `# ---- ADR 0001 addendum: D1 DGX results` with the constant `ADR`, the helper `adr_d1_results()` and `test_adr_ingest_path_d1_results_pass_the_gates`.
- Not touched: `src/`, `bench/bench.py`, `bench/dgx/*.sh`, `bench/ingest_probe.py` (spec §4.1).
- Test: `uv run pytest tests/test_ingest_path.py -v -k adr_ingest_path_d1`.

**Interfaces:**
- Consumes:
  - Task 5: `bench/ingest_probe.py` with its CLI (`--ivf N --seed S --prefill --jobs --job-rows --tmp DIR --out FILE`) and its result keys `ingest_vps`, `drain_s`, `jobs`, `payload_rows`, `freelist`, `fingerprint` (`records`, `vecs`, `counts`, `n_index`), `store_file`, `sqlite_version`, `turbovec`, `openblas_num_threads`, `regime` (`"host-warm, uncapped host process"`). The probe puts its own tree's `src/` first on `sys.path`, so a copy inside the base tree measures the base code, and `store_file` shows which tree ran.
  - Plan A: `bench/dgx/compare.py` (`load(dir)`, `values(engine_runs, key)`, `summary(vals) -> (median, max - min)`, `verdict(key, base, cand)`, `RUN_FILE` = `run<N>-<engine>.json`, `ONE_SHOT` counting `ingest_vps` from every run); `bench/bench.py` with `--limit --caps-note --host --engine raggio --reingest --fingerprint-dir --out`, which leaves `bench/results-partial.json` (`{"raggio": {"ingest_s", "ingest_vps", ...}}`) and aborts on a short ingest; A's image, with `python` on `PATH` and `ENV OPENBLAS_NUM_THREADS=1` (D16).
  - Plan C: its addendum ends with `### Acceptance run`, and its `test_adr_records_the_acceptance_run` slices its own section up to the next `\n## `, so a new `## ` heading after it keeps that test green.
  - Task 1's commit subject `Add binary job payload codec and job_payloads side table` (to find D1's base commit).
- Produces:
  - The ADR section `## Addendum 2026-09 — ingest path`: a decision table, then `### Results — DGX A/B (D1)`. After Step 14, that subsection holds a `#### Measurements (D1)` heading, then a short text, the row table, the `- PASS: D1-...` gate lines (`- SKIP: D1-bench` without the reingests) and one fenced `json` block. The JSON's top-level keys are `date`, `base_sha`, `cand_sha`, `probe`, `bench`, `labels` and `gates`, laid out in Step 3c. Task 10 appends `### Batched sync (D2)` and `### Results — DGX A/B (D2)` after this subsection, so the D1 subsection ends at the next `\n### ` or `\n## `.
  - `planD-ab.sh <mode> <base sha> <cand sha> <reingest 0|1> <cap minutes>`, where mode is `d1` (Task 10 adds `d2`). It writes everything under `~/planD/<mode>/` on gn100: `ab.log`, `ab.pgid`, `probe/<flat|ivf256>-<base|cand>-run<N>.json`, and `bench-<base|cand>/run<N>-raggio.{md,json}` with `jobs-run<N>.json` and `meta-run<N>.json`.
  - `planD-summary.py <results dir> [--adr ADR]`. It prints the results block, exits 1 on any `- FAIL:` line, and with `--adr` replaces the `Pending:` line only when every gate passes.
  - Test helper `adr_d1_results() -> tuple[str, dict]` (the subsection text, with `\r\n` normalized, and the parsed Measurements JSON).

- [ ] **Step 1: Ask the user for a run window (time cap)**

Ask, and wait for an explicit answer. Steps 2-5 are laptop-only and may proceed meanwhile, except that Step 5's env file needs the answer. Nothing runs on gn100 before the answer:

> Plan D1's DGX A/B needs a run window on gn100. The full A/B takes about 4 hours:
> - up to 20 min to build two images (main after Plan C, and D1), if they are absent;
> - about 70 min for 14 in-process ingest probe runs (`bench/ingest_probe.py`, seed 42, 100k prefill rows, 200 jobs x 250 rows at 1024-d). Per mode, flat and IVF nlist 256, there is one discarded warm-up, then 3 interleaved rounds of base and D1. These are host processes with `OPENBLAS_NUM_THREADS=1`, and no container is running;
> - about 150 min for 4 full bench reingests of the 2.55M-row arXiv corpus (`bench.py --limit 2549619 --engine raggio --reingest`, ~31 min of ingest plus the search pass each). They are interleaved base, D1, base, D1, each on a fresh 4 GiB `bench-tv` container. They run last, and only if at least 180 min are left when the probes finish.
>
> Suggested cap: 4 h 50 min in all, of which 4 h 30 min is for the detached A/B launch. The reingests start only if 180 min of the launch cap are left after the probes, so a launch cap under about 4 h 15 min skips them. A probe-only option takes about 90 min (cap 1 h 45 min, of which 85 min is for the launch). It gives the flat and IVF probe numbers but no end-to-end reingest.
>
> It uses only the `bench-tv` container and volume, `~/raggio` (replaced with D1's tree, spec §6 deploy) and `~/planD/`. It never touches `bench-wv` or `~/turborag`, and uses no `sudo`. The reingests delete and re-create the `bench` collection in the `bench-tv` volume. It ends flat (no IVF index), with the same 2,549,119 rows written by the D1 image, and the queue drained. A pre-D image reads that state, because the drained queue leaves no binary journal row behind. If the cap kills a reingest midway, the volume holds a partial collection until one more reingest runs.
>
> May I run it? If so, the full A/B or probe-only, and until what time? Is anything else running on gn100 in that window?

Record the answer, the start time and the cap:
- If the user declines, do Step 2 only (the addendum stays `Pending:`), then stop. The suite count stays at `N0 + 27`.
- If the user grants probe-only, use `REINGEST=0` in Step 5; otherwise `REINGEST=1`.
- `CAP_MIN` in Step 5 is the minutes left for the launch (Step 9), after the builds.

- [ ] **Step 2: Append the ingest-path addendum to ADR 0001 and commit it**

Append at the very end of `docs/adr/0001-performance-optimization-decisions.md`, after Plan C's `### Acceptance run` block and one blank line. The file is UTF-8; use a UTF-8 aware editor.

```markdown
## Addendum 2026-09 — ingest path

Date: 2026-09-28 · Plan D, phase D1, of the 2026-09-28 performance program (`docs/superpowers/specs/2026-09-28-raggio-performance-design.md`). Direction-tested by the item2a prototype (`docs/superpowers/research/2026-09-28-item2a-ingest-path.md`) on a Windows x86-64 laptop: 250 × 1024 jobs, a 12k-row prefill, 2 enqueuers, every 5th job a re-upsert. Laptop numbers carry about ±30 % noise. The shipped code's gn100 A/B is under "Results — DGX A/B (D1)" below. Every accepted code change is pinned by `tests/test_ingest_path.py`.

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| Binary job journal in a side table `job_payloads(job_id INTEGER PRIMARY KEY, data BLOB)`, with `jobs.payload` NULL. Format: a `<4sIII` header (magic `RGJ\x01`, JSON length, vector count, dim), UTF-8 JSON in which each supplied vector is replaced by its row index, then little-endian float32 rows | **Accepted** | The claim `UPDATE` rewrote the 2.7 MB payload overflow chain, because `'pending'` and `'processing'` differ in length: 40–66 ms per job. With the side table: 3–10 ms. Enqueue SQL 71–94 → 41 ms. Bit-exact: the worker already stored float32 |
| The binary payload inline in `jobs.payload` | **Rejected** | No schema change, but it keeps the 40–66 ms claim rewrite |
| Legacy TEXT-JSON rows still replay, in id order, next to binary rows. A row with neither payload becomes an `error` job (`bad job payload: ...`) and never kills the worker | **Accepted (compatibility)** | A volume upgraded with open jobs holds both kinds. `error` jobs keep their payload row (`docs/storage.md`) |
| Downgrade: drain the ingest queue (`pending_jobs == 0` on every collection) before starting a pre-2026-09 image | **Accepted (documented, not enforced)** | An older image reads `jobs.payload` only, so it would mark new-format open jobs `error`. A drained queue has no payload row left |
| Encode, decode, flattening and matrix building off the event loop, in the threads that already do the SQLite work. The encode runs before `db_lock` is taken, and the decode after it is released | **Accepted** | Base: `json.loads` of a 2.7 MB payload (41–68 ms per job) ran on the loop, and `json.dumps` (74–124 ms) was evaluated on the loop before `to_thread`. Loop stalls over 20 ms per 40-job run: 10.5–11.4 s → 0.4–0.5 s. Binary decode: 0.7–1 ms |
| Set-based, failure-safe upsert: one transaction, an `IN` lookup per 512 ids, `executemany` DELETE then INSERT, rollback on any exception. The index removal (`_unindex`) and the one copy-on-write `indexed_counts` rebind (still under `db_lock`) run after the commit. Deletes follow the same order | **Accepted (bug fix)** | Base removed index ids and changed counts before the commit, with no rollback. A failure midway (SQLITE_FULL, I/O) left the implicit transaction open, and the worker's `_finish_job('error')` committed the half-applied upsert under a terminal job that never replays. Throughput: +0–12 % |
| Several jobs in one SQLite transaction | **Rejected** | Holds `db_lock` longer to save about one fsync per job |
| A failure after the commit (`add_with_ids`, calibration) | **Unchanged (known gap)** | It still leaves an `error` job whose committed rows are missing from the index. `_reconcile_ghosts` only evicts; it never re-adds |
| Vacuum policy: a full `incremental_vacuum` only when no job is open. While jobs are open, a finish that sees `VACUUM_FREELIST_PAGES` (16,384 pages, 64 MB at 4 KiB) or more trims the freelist back to `VACUUM_FREELIST_PAGES − VACUUM_CHUNK_PAGES` (`VACUUM_CHUNK_PAGES` = 2,048, so 14,336 pages). Both pragmas are drained with `.fetchall()` | **Accepted** | Per-job vacuum with the side table cost 38–54 ms; idle-only, 7–20 ms. A fixed-size chunk would let the freelist grow whenever a job frees more than one chunk |
| `bench/ingest_probe.py`, an in-process drain benchmark (flat and IVF) that also runs in the base tree | **Accepted (tooling)** | `bench.py` never ingests into an IVF collection: `--engine raggio-ivf --reingest` only rebuilds the index. Probe rows are labelled host-warm, uncapped host process |
| Batched index syncs (the lever in the standing constraints above) | **Deferred to D2** | D1 keeps one sync per job. Prototype, laptop: base 468–483 → + binary journal 810–849 → + set-based upsert 815–955 → + vacuum policy 995–1046 vec/s (flat); IVF nlist 64: 254 → 408 vec/s. Every variant's final state was identical to base |

### Results — DGX A/B (D1)

Pending: the gn100 A/B of Plan D Task 6 has not run yet.
```

Check that Plan C's ADR test still passes, since its section now ends at this new `## ` heading:

Run: `uv run pytest tests/test_cold_start.py -v -k acceptance_run`
Expected: `1 passed, 64 deselected`. If Plan C's Task 12 test did not land (N0 = 252), this prints `64 deselected` with no test run; that is fine.

Run: `uv run pytest -q`
Expected: `N0 + 27 passed, 1 warning`. This step adds no test.

```bash
git add docs/adr/0001-performance-optimization-decisions.md
git commit -F - <<'EOF'
Record the D1 ingest path decisions in ADR 0001

Append "Addendum 2026-09 - ingest path": the binary side-table job
journal and its format, legacy TEXT replay, the drain-before-downgrade
rule, codec and prep off the event loop, the set-based failure-safe
upsert with counts published after commit (and the unchanged
add-after-commit gap), and the vacuum policy constants. The gn100 A/B
results are Pending until Task 6's run.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 3: Write the DGX scripts (outside the repo)**

Create `D:/DEV/_scratch/dgx/planD/` with the four files below. They are never committed. Write them with LF line endings and ASCII only; Step 4 checks both. They follow Plan F's Task 9 pattern (a detached driver, per-run label files, a laptop-side summary). They do not use Plan A's `session.sh`, which never passes `--reingest`.

3a. `D:/DEV/_scratch/dgx/planD/planD-build.sh`:

```bash
#!/bin/bash
# Plan D: build localhost/raggio:<tag> from <source dir> unless that image already exists.
# Usage (on gn100): bash ~/planD/planD-build.sh <source dir> <tag>
set -uo pipefail
src=$1
tag=$2
if podman image exists "localhost/raggio:$tag"; then
  echo "== BUILD_DONE $tag (image already present) $(date '+%F %T') =="
  exit 0
fi
echo "== BUILD START $tag from $src $(date '+%F %T') =="
if podman build -t "localhost/raggio:$tag" "$src"; then
  echo "== BUILD_DONE $tag $(date '+%F %T') =="
else
  echo "== BUILD_FAILED $tag $(date '+%F %T') =="
  exit 1
fi
```

3b. `D:/DEV/_scratch/dgx/planD/planD-ab.sh`, the detached driver. It is the only script that runs `ingest_probe.py` or `bench.py` on the DGX, and `reingest()` is the only place that passes `--reingest`:

```bash
#!/bin/bash
# Plan D DGX A/B (spec section 6 protocol, section 7 D acceptance). base = main before D1, cand = D1.
# Probe phase: bench/ingest_probe.py (seed 42) as a host process, flat and then --ivf 256; per
# mode one discarded base run0, then 3 interleaved rounds of base, cand. Each arm runs its own
# tree's copy of the probe, so store_file says which code was measured.
# Reingest phase LAST (spec section 6: it rewrites and compacts the volume): bench.py --engine
# raggio --reingest, in the order base, cand, base, cand, each in a fresh 4 GiB bench-tv of that
# arm's image. It runs only with REINGEST=1 and 180 min left before the cap.
# It touches no container but bench-tv, and it only removes bench-tv.
# Usage (on gn100; ~/raggio = cand tree, ~/planD/base-src = base tree, both images built):
#   setsid -f timeout --kill-after=60 <cap>m bash ~/planD/planD-ab.sh d1 <base sha> <cand sha> <0|1> <cap min> >/dev/null 2>&1 </dev/null
set -uo pipefail
MODE=$1
BASE=$2
CAND=$3
REINGEST=$4
CAP_MIN=$5
[ "$MODE" = d1 ] || { echo "unknown mode $MODE"; exit 2; }
export OPENBLAS_NUM_THREADS=1
OUT=~/planD/$MODE
mkdir -p "$OUT/probe" "$OUT/bench-base" "$OUT/bench-cand" ~/planD/tmp
exec >>"$OUT/ab.log" 2>&1
ps -o pgid= $$ | tr -d ' ' >"$OUT/ab.pgid"
DEADLINE=$(( $(date +%s) + CAP_MIN * 60 - 120 ))
cd ~/raggio || exit 1
H=(-H x-api-key:bench)
URL=localhost:18000/collections/bench
CAPS="raggio container capped at 4 GiB (4-bit quantized flat index)."
HOST="NVIDIA DGX Spark - GB10 Grace, 20 aarch64 cores, 122 GB unified LPDDR5x, native Linux, rootless podman"
PY=~/raggio/.venv/bin/python
ts() { date '+%F %T'; }
fail() { echo "== PLAND AB FAILED: $* ($(ts)) =="; exit 1; }

probe() {  # $1 = flat|ivf256, $2 = base|cand, $3 = run number
  local tree=~/raggio nl=0 f=$OUT/probe/$1-$2-run$3.json
  [ "$2" = base ] && tree=~/planD/base-src
  [ "$1" = ivf256 ] && nl=256
  echo "== probe $1 $2 run$3 ($(ts)) =="
  "$PY" -u "$tree/bench/ingest_probe.py" --ivf "$nl" --seed 42 --prefill 100000 --jobs 200 \
    --job-rows 250 --tmp ~/planD/tmp --out "$f" || fail "probe $1 $2 run$3"
  "$PY" - "$f" "$tree/src/raggio/store.py" <<'EOF' || fail "probe $1 $2 run$3: wrong tree or unfinished jobs"
import json, os, sys
r = json.load(open(sys.argv[1]))
print("  vps %.0f drain %.1f s stall %.2f s jobs %s payload_rows %s freelist %s" % (
    r["ingest_vps"], r["drain_s"], r["loop_stall_s"], r["jobs"], r["payload_rows"], r["freelist"]))
same = os.path.realpath(r["store_file"]) == os.path.realpath(sys.argv[2])
sys.exit(0 if same and r["jobs"] == {"done": 200} else 1)
EOF
}
start_ctr() {  # $1 = image tag: a fresh 4 GiB bench-tv of that image
  podman rm -f bench-tv >/dev/null 2>&1
  podman run -d --name bench-tv --memory 4g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench \
    -e OPENBLAS_NUM_THREADS=1 "localhost/raggio:$1" >/dev/null || fail "podman run $1"
  echo "[$(ts)] ctr started ($1)"  # spec section 6: time to /healthz and to the first GET
  for _ in $(seq 1 1200); do curl -sf localhost:18000/healthz >/dev/null && break; sleep 0.5; done
  curl -sf localhost:18000/healthz >/dev/null || fail "no /healthz from $1 after 10 min"
  echo "[$(ts)] healthz ok ($1)"
  local body
  body=$(curl -s --max-time 1800 "${H[@]}" $URL)
  echo "[$(ts)] ctr up, first GET /collections/bench ($1): $body"
}
wait_pending() {
  local p
  for _ in $(seq 1 1440); do
    p=$(curl -s --max-time 900 "${H[@]}" $URL | python3 -c 'import sys,json; print(json.load(sys.stdin).get("pending_jobs", "missing"))' 2>/dev/null)
    [ "$p" = "0" ] && { echo "[$(ts)] pending_jobs=0"; return 0; }
    [ "$p" = "missing" ] && fail "GET /collections/bench has no pending_jobs (404 or 401?)"
    sleep 5
  done
  fail "pending jobs never drained"
}
jobs_check() {  # $1 = arm dir, $2 = run number: a read-only look at the collection's meta.db
  podman exec bench-tv python -c '
import json, sqlite3
db = sqlite3.connect("file:/data/collections/bench/meta.db?mode=ro", uri=True)
jobs = dict(db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
has = db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", ("job_payloads",)).fetchone()
rows = db.execute("SELECT COUNT(*) FROM job_payloads").fetchone()[0] if has else None
free = db.execute("PRAGMA freelist_count").fetchone()[0]
print(json.dumps({"jobs": jobs, "payload_rows": rows, "freelist": free}))' >"$1/jobs-run$2.json" \
    || fail "jobs check $1 run$2"
  echo "  jobs run$2: $(cat "$1/jobs-run$2.json")"
}
meta() {  # $1 = arm dir, $2 = run number: the section 6 row labels, read from the container
  local pk sv ob
  pk=$(podman exec bench-tv python -c 'print(open("/sys/fs/cgroup/memory.peak").read().strip())') || fail "memory.peak"
  sv=$(podman exec bench-tv python -c 'import sqlite3; print(sqlite3.sqlite_version)') || fail "sqlite_version"
  ob=$(podman exec bench-tv python -c 'import os; print(os.environ.get("OPENBLAS_NUM_THREADS", "unset"))') || fail "OPENBLAS"
  printf '{"regime": "host-warm", "cap": "4g", "sqlite_version": "%s", "openblas_num_threads": "%s", "memory_peak_mb": %s}\n' \
    "$sv" "$ob" "$(awk "BEGIN{printf \"%.1f\", $pk/1048576}")" >"$1/meta-run$2.json"
  echo "  labels: host-warm, cap 4g, sqlite_version $sv, OPENBLAS_NUM_THREADS=$ob, memory.peak $pk bytes"
}
reingest() {  # $1 = base|cand, $2 = run number; the only place that passes --reingest
  local dir=$OUT/bench-$1 tag=planD-$BASE
  [ "$1" = cand ] && tag=planD-$CAND
  echo "== reingest $1 run$2 ($(ts)) =="
  start_ctr "$tag"
  wait_pending
  rm -f bench/results-partial.json
  .venv/bin/python -u bench/bench.py --limit 2549619 --caps-note "$CAPS" --host "$HOST" \
    --fingerprint-dir ~/raggio-bench/state --engine raggio --reingest --out "$dir/run$2-raggio.md" \
    || fail "reingest $1 run$2"
  mv bench/results-partial.json "$dir/run$2-raggio.json" || fail "no results for $1 run$2"
  wait_pending
  jobs_check "$dir" "$2"
  meta "$dir" "$2"
}

echo "== PLAND AB START $(ts) mode=$MODE base=$BASE cand=$CAND reingest=$REINGEST cap=${CAP_MIN}m =="
pgrep -af '[b]ench/bench\.py|[i]ngest_probe' && fail "another bench.py or probe is in flight"
[ -n "$(podman ps --filter name='^bench-tv$' --format '{{.Names}}')" ] && fail "bench-tv is already running"
grep -q '"--fingerprint-dir"' bench/bench.py || fail "~/raggio/bench/bench.py is not Plan A's bench"
uptime
free -m | head -2
grep -E '^Cached' /proc/meminfo
podman ps -a
for s in "$BASE" "$CAND"; do
  podman image inspect "localhost/raggio:planD-$s" --format "image planD-$s id {{.Id}} digest {{.Digest}}" \
    || fail "no image planD-$s"
  podman run --rm --entrypoint python "localhost/raggio:planD-$s" -c \
    'import sqlite3, importlib.metadata as m; print("turbovec", m.version("turbovec"), "sqlite_version", sqlite3.sqlite_version)' \
    || fail "image facts planD-$s"
done
"$PY" -c 'import os, sqlite3; print("host venv sqlite_version", sqlite3.sqlite_version, "OPENBLAS_NUM_THREADS", os.environ["OPENBLAS_NUM_THREADS"])'
echo "labels: probe rows host-warm, uncapped host process; bench rows host-warm, cap 4g"

for m in flat ivf256; do
  probe "$m" base 0  # warm-up, discarded
  for r in 1 2 3; do
    probe "$m" base "$r"
    probe "$m" cand "$r"
  done
done
echo "== probes done ($(ts)) =="

if [ "$REINGEST" != 1 ]; then
  echo "== reingest skipped: not requested ($(ts)) =="
elif [ $(( DEADLINE - $(date +%s) )) -lt $(( 4 * 45 * 60 )) ]; then
  echo "== reingest skipped: less than 180 min left before the cap ($(ts)) =="
else
  for r in 1 2; do
    reingest base "$r"
    reingest cand "$r"
  done
  podman stop -t 60 bench-tv >/dev/null
fi
echo "== PLAND AB DONE $(ts) =="
```

3c. `D:/DEV/_scratch/dgx/planD/planD-summary.py`. It runs on the laptop from the repo root and imports Plan A's `bench/dgx/compare.py` from there, so the probe rows and the bench rows use the same band rule (`compare.summary`, `compare.verdict` with key `ingest_vps`, band floor `compare.RESOLUTION["ingest_vps"]` = 1 vec/s). The file is ASCII, so the em dash of the ADR heading is `chr(0x2014)` and the code fence is built as ``"`" * 3``.

The block it writes is `#### Measurements (D1)`, a short text, a row table (every row names its regime, cap, `sqlite_version` and `OPENBLAS_NUM_THREADS`), the gate lines, and one fenced `json` block laid out like this:

- `date`, `base_sha`, `cand_sha`: from the `== PLAND AB START` line of `ab.log`.
- `probe`: `{"flat": M, "ivf256": M}`. Each `M` has:
  - `base` and `cand`: the three measured `ingest_vps` values per arm, rounded to 0.1, in run order (run0 is not included);
  - `base_median`, `cand_median`, `gain`, `band` and `verdict`, from `compare.summary` and `compare.verdict`, computed on the rounded values;
  - `drain_s` and `loop_stall_s`: `{"base": [...], "cand": [...]}`;
  - `payload_rows`: `{"base": [...], "cand": [...]}` over every run including run0 (base `null`, cand `0`);
  - `complete` (3 values per arm), `fingerprints_equal` (all 7 runs of the mode), `jobs_all_done` (every run `{"done": 200}`).
- `bench`: `null` when the reingest phase did not run. Otherwise it has:
  - `base`, `cand`, `base_median`, `cand_median`, `gain`, `band`, `verdict`, the same as `M` but from `compare.values(..., "ingest_vps")`;
  - `runs`: `{"base-run1": R, ...}` with `R` = `ingest_s`, `jobs` (`{status: count}`), `payload_rows`, `freelist`, `memory_peak_mb`.
- `labels`:
  - `probe`: `regime`, `cap`, `sqlite_version`, `openblas_num_threads`, `turbovec` (sorted lists of the values seen);
  - `bench`: `null`, or `regime`, `cap`, `openblas_num_threads` (sorted lists) and `sqlite_version` (`{"base": [...], "cand": [...]}`).
- `gates`: `{"D1-run": ..., "D1-flat": ..., "D1-ivf256": ..., "D1-fingerprints": ..., "D1-jobs": ..., "D1-bench": ..., "D1-labels": ...}`, each `"PASS"` or `"FAIL"`. `D1-bench` is `"SKIP"` when `bench` is `null`.

```python
#!/usr/bin/env python3
"""Plan D DGX A/B summary: the gate lines, the ADR Measurements block and the ADR edit.

Usage, from the repo root (it imports Plan A's bench/dgx/compare.py from there):
  uv run python D:/DEV/_scratch/dgx/planD/planD-summary.py <results dir> [--adr ADR]
<results dir> is the fetched ~/planD/d1: ab.log, probe/, bench-base/, bench-cand/.
It prints the "#### Measurements (D1)" block and exits 1 if any gate fails. With --adr,
and only when no gate fails, it replaces the Pending line of the ADR's D1 results
section with that block. Nothing here loosens a gate.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "bench" / "dgx"))
import compare  # noqa: E402  (Plan A: load, values, summary, verdict, RESOLUTION)

DASH = chr(0x2014)
RESULTS_HEAD = "### Results " + DASH + " DGX A/B (D1)"
PENDING = "Pending: the gn100 A/B of Plan D Task 6 has not run yet."
NOT_RUN = ("Not run: the bench reingest was skipped (a probe-only window, or under 180 min"
           " left before the cap), so this run gives no end-to-end ingest_vps row for D1.")
FENCE = "`" * 3
MODES = ("flat", "ivf256")
ARMS = ("base", "cand")
ROUNDS = (1, 2, 3)
PROBE_REGIME = "host-warm, uncapped host process"
PROBE_CMD = "bench/ingest_probe.py --seed 42 --prefill 100000 --jobs 200 --job-rows 250"


def read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def start_facts(d):
    """(date, base sha, cand sha, finished) from ab.log."""
    log = (d / "ab.log").read_text(encoding="utf-8")
    start = [line for line in log.splitlines() if line.startswith("== PLAND AB START ")][-1]
    parts = start.split()
    kv = dict(p.split("=", 1) for p in parts[6:-1])
    finished = "== PLAND AB DONE " in log and "== PLAND AB FAILED" not in log
    return parts[4], kv["base"], kv["cand"], finished


def stats(base, cand):
    """compare.py's median and band rule on two lists of ingest_vps values."""
    if not base or not cand:
        return {"base": base, "cand": cand, "verdict": "missing"}
    (bm, bb), (cm, cb) = compare.summary(base), compare.summary(cand)
    return {"base": base, "cand": cand, "base_median": round(bm, 1), "cand_median": round(cm, 1),
            "gain": round(cm - bm, 1), "band": round(max(bb, cb, compare.RESOLUTION["ingest_vps"]), 1),
            "verdict": compare.verdict("ingest_vps", base, cand)}


def store_ok(arm, path):
    p = path.replace("\\", "/")
    if arm == "base":
        return p.endswith("/planD/base-src/src/raggio/store.py")
    return p.endswith("/raggio/src/raggio/store.py") and "/planD/" not in p


def probe_part(d):
    """({mode: M}, probe labels, labels ok) from probe/<mode>-<arm>-run<N>.json."""
    out, seen, ok = {}, {k: set() for k in ("regime", "sqlite_version", "openblas_num_threads", "turbovec")}, True
    for m in MODES:
        runs = {p.stem[len(m) + 1:]: read(p) for p in sorted((d / "probe").glob(f"{m}-*-run*.json"))}
        vals = {a: [round(runs[f"{a}-run{n}"]["ingest_vps"], 1) for n in ROUNDS if f"{a}-run{n}" in runs]
                for a in ARMS}
        entry = stats(vals["base"], vals["cand"])
        for key, nd in (("drain_s", 2), ("loop_stall_s", 3)):
            entry[key] = {a: [round(runs[f"{a}-run{n}"][key], nd) for n in ROUNDS if f"{a}-run{n}" in runs]
                          for a in ARMS}
        entry["payload_rows"] = {a: [r["payload_rows"] for k, r in runs.items() if k.startswith(a)] for a in ARMS}
        entry["complete"] = all(len(vals[a]) == len(ROUNDS) for a in ARMS)
        fps = {json.dumps(r["fingerprint"], sort_keys=True) for r in runs.values()}
        entry["fingerprints_equal"] = len(runs) == 1 + 2 * len(ROUNDS) and len(fps) == 1
        entry["jobs_all_done"] = bool(runs) and all(r["jobs"] == {"done": 200} for r in runs.values())
        out[m] = entry
        for k, r in runs.items():
            ok = ok and store_ok(k.split("-")[0], r["store_file"])
            for key in seen:
                seen[key].add(str(r[key]))
    labels = {key: sorted(v) for key, v in seen.items()}
    labels["cap"] = ["none (uncapped host process)"]
    ok = ok and labels["regime"] == [PROBE_REGIME] and labels["openblas_num_threads"] == ["1"]
    return out, labels, ok and len(labels["sqlite_version"]) == 1


def bench_part(d):
    """(bench or None, bench labels or None, labels ok, jobs ok) from bench-<arm>/."""
    dirs = {a: d / f"bench-{a}" for a in ARMS}
    has = {a: bool(list(dirs[a].glob("run*-raggio.json"))) for a in ARMS}
    if not any(has.values()):
        return None, None, True, True
    vals, runs, ok, jobs_ok = {}, {}, True, True
    seen = {k: set() for k in ("regime", "cap", "openblas_num_threads")}
    sv = {a: set() for a in ARMS}
    for a in ARMS:
        eng = compare.load(dirs[a]).get("raggio", {}) if has[a] else {}
        vals[a] = [round(v, 1) for v in compare.values(eng, "ingest_vps")]
        for n in sorted(eng):
            jobs, meta = read(dirs[a] / f"jobs-run{n}.json"), read(dirs[a] / f"meta-run{n}.json")
            if jobs is None or meta is None:
                ok = jobs_ok = False
                continue
            runs[f"{a}-run{n}"] = {"ingest_s": round(eng[n].get("ingest_s", 0.0), 1), "jobs": jobs["jobs"],
                                   "payload_rows": jobs["payload_rows"], "freelist": jobs["freelist"],
                                   "memory_peak_mb": meta["memory_peak_mb"]}
            left = None if a == "base" else 0
            jobs_ok = jobs_ok and set(jobs["jobs"]) == {"done"} and jobs["payload_rows"] == left
            for key in seen:
                seen[key].add(str(meta[key]))
            sv[a].add(meta["sqlite_version"])
    bench = stats(vals["base"], vals["cand"])
    bench["runs"] = runs
    labels = {key: sorted(v) for key, v in seen.items()}
    labels["sqlite_version"] = {a: sorted(sv[a]) for a in ARMS}
    ok = (ok and labels["regime"] == ["host-warm"] and labels["cap"] == ["4g"]
          and labels["openblas_num_threads"] == ["1"] and all(len(sv[a]) == 1 for a in ARMS))
    return bench, labels, ok, jobs_ok


def gates_for(finished, probe, bench, labels_ok, jobs_ok):
    g = {"D1-run": finished}
    for m in MODES:
        g[f"D1-{m}"] = probe[m]["complete"] and probe[m]["verdict"] == "better"
    g["D1-fingerprints"] = all(probe[m]["fingerprints_equal"] for m in MODES)
    g["D1-jobs"] = jobs_ok and all(
        probe[m]["jobs_all_done"] and set(probe[m]["payload_rows"]["base"]) == {None}
        and set(probe[m]["payload_rows"]["cand"]) == {0} for m in MODES)
    g["D1-bench"] = None if bench is None else (
        bench["verdict"] == "better" and all(len(bench[a]) == 2 for a in ARMS))
    g["D1-labels"] = labels_ok
    return {k: "SKIP" if v is None else ("PASS" if v else "FAIL") for k, v in g.items()}


def row(name, regime, cap, sv, ob, e):
    return (f"| {name} | {regime} | {cap} | {sv} | {ob} | {e.get('base_median')} | {e.get('cand_median')}"
            f" | {e.get('gain')} | {e.get('band')} | {e['verdict']} |")


def render(date, base_sha, cand_sha, probe, bench, labels, gates):
    pl = labels["probe"]
    lines = ["#### Measurements (D1)", "",
             f"Run {date} on gn100 (NVIDIA DGX Spark, GB10 Grace, 20 aarch64 cores), base `{base_sha}`"
             f" (main before D1) against cand `{cand_sha}` (D1). Probe rows: `{PROBE_CMD}` with `--ivf 0`"
             " and `--ivf 256`, each arm running its own tree, one discarded base run0 and then 3"
             " interleaved rounds per mode. Bench row: `bench.py --limit 2549619 --engine raggio"
             " --reingest` (2,549,119 x 1024) in the order base, cand, base, cand, each in a fresh"
             " 4 GiB container. Values are ingest vec/s. Band = max(base max - min, cand max - min,"
             " 1 vec/s); a claim needs gain > band.", "",
             "| Row | Regime | Cap | sqlite_version | OPENBLAS_NUM_THREADS | base median | cand median"
             " | gain | band | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for m in MODES:
        lines.append(row(f"probe {m}", PROBE_REGIME, pl["cap"][0], ", ".join(pl["sqlite_version"]),
                         ", ".join(pl["openblas_num_threads"]), probe[m]))
    if bench is None:
        lines += ["", NOT_RUN]
    else:
        bl = labels["bench"]
        sv = ", ".join(f"{a} {'/'.join(bl['sqlite_version'][a])}" for a in ARMS)
        lines.append(row("bench reingest", ", ".join(bl["regime"]), ", ".join(bl["cap"]), sv,
                         ", ".join(bl["openblas_num_threads"]), bench))
    lines += [""] + [f"- {v}: {k}" for k, v in gates.items()]
    data = {"date": date, "base_sha": base_sha, "cand_sha": cand_sha, "probe": probe,
            "bench": bench, "labels": labels, "gates": gates}
    lines += ["", FENCE + "json", json.dumps(data, indent=2), FENCE]
    return "\n".join(lines) + "\n"


def record(adr, block):
    """Replace the Pending line of the D1 results section; keep the file's line endings."""
    raw = adr.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")
    start = text.index(RESULTS_HEAD)
    ends = [i for i in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if i != -1]
    cut = text.find(PENDING, start, min(ends, default=len(text)))
    if cut == -1:
        raise SystemExit(f"{adr}: no Pending line in the D1 results section; nothing recorded")
    text = text[:cut] + block.rstrip("\n") + text[cut + len(PENDING):]
    adr.write_bytes(text.replace("\n", nl).encode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plan D DGX A/B summary and ADR record")
    ap.add_argument("results", type=Path)
    ap.add_argument("--adr", type=Path)
    args = ap.parse_args(argv)
    date, base_sha, cand_sha, finished = start_facts(args.results)
    probe, probe_labels, probe_ok = probe_part(args.results)
    bench, bench_labels, bench_ok, jobs_ok = bench_part(args.results)
    labels = {"probe": probe_labels, "bench": bench_labels}
    gates = gates_for(finished, probe, bench, probe_ok and bench_ok, jobs_ok)
    block = render(date, base_sha, cand_sha, probe, bench, labels, gates)
    print(block)
    if "FAIL" in gates.values():
        print("planD-summary: a gate failed; the ADR is unchanged", file=sys.stderr)
        return 1
    if args.adr:
        record(args.adr, block)
        print(f"planD-summary: recorded in {args.adr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

3d. `D:/DEV/_scratch/dgx/planD/planD-check.py`, the offline check of the three scripts above. It runs on the laptop from the repo root, before anything touches the DGX:

```python
#!/usr/bin/env python3
"""Offline checks of the Plan D DGX scripts. Run from the repo root before any DGX step.

Usage: uv run python D:/DEV/_scratch/dgx/planD/planD-check.py
Checks: LF and ASCII only; the safety rules in the shell scripts; planD-summary.py on
synthetic result dirs (PASS, SKIP and each FAIL gate); the ADR edit on a copy.
Prints "Plan D DGX scripts OK" or stops at the first failed assert.
"""
import contextlib
import importlib.util
import io
import json
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = ("planD-ab.sh", "planD-build.sh", "planD-summary.py", "planD-check.py")
spec = importlib.util.spec_from_file_location("planD_summary", HERE / "planD-summary.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
FP = {"records": "r0", "vecs": "v0", "counts": {"chunk": 150000}, "n_index": 150000}
TREES = {"base": "/home/u/planD/base-src/src/raggio/store.py", "cand": "/home/u/raggio/src/raggio/store.py"}
START = "== PLAND AB START 2026-09-30 10:00:00 mode=d1 base=aaaaaaaaaaaa cand=bbbbbbbbbbbb reingest=1 cap=250m ==\n"


def check_text():
    for name in SCRIPTS:
        data = (HERE / name).read_bytes()
        assert b"\r" not in data, f"{name}: CRLF line endings"
        assert all(b < 128 for b in data), f"{name}: non-ASCII bytes"
    for name in ("planD-ab.sh", "planD-build.sh"):
        text = (HERE / name).read_text(encoding="ascii")
        for bad in ("bench-wv", "turborag", "pkill", "sudo", "rm -rf", "volume rm", "podman rmi"):
            assert bad not in text, f"{name} contains {bad!r}"
        for line in text.splitlines():
            if "podman rm" in line:
                assert line.strip() == "podman rm -f bench-tv >/dev/null 2>&1", f"{name}: {line}"
            if "podman stop" in line:
                assert "podman stop -t 60 bench-tv" in line, f"{name}: {line}"
    in_fn = False
    for line in (HERE / "planD-ab.sh").read_text(encoding="ascii").splitlines():
        in_fn = line.startswith("reingest() {") or (in_fn and line != "}")
        if "--reingest" in line and not line.lstrip().startswith("#"):
            assert in_fn, f"--reingest outside reingest(): {line}"


def fake_run(tmp, gain=100.0, bench=True, done=True):
    """A synthetic ~/planD/d1 that passes every gate (cand faster by `gain` vec/s)."""
    d = Path(tmp) / f"d1-{len(list(Path(tmp).iterdir()))}"
    (d / "probe").mkdir(parents=True)
    (d / "ab.log").write_text(START + ("== PLAND AB DONE 2026-09-30 13:00:00 ==\n" if done else ""))
    for m in S.MODES:
        for a, rounds in (("base", (0, 1, 2, 3)), ("cand", (1, 2, 3))):
            for n in rounds:
                r = {"ingest_vps": 1000.0 + n + (gain if a == "cand" else 0.0), "drain_s": 50.0,
                     "loop_stall_s": 0.0, "fingerprint": FP, "jobs": {"done": 200},
                     "payload_rows": None if a == "base" else 0, "store_file": TREES[a],
                     "regime": S.PROBE_REGIME, "sqlite_version": "3.46.1",
                     "openblas_num_threads": "1", "turbovec": "0.0-test"}
                (d / "probe" / f"{m}-{a}-run{n}.json").write_text(json.dumps(r))
    for a in S.ARMS:
        b = d / f"bench-{a}"
        b.mkdir()
        for n in (1, 2) if bench else ():
            vps = 3000.0 + n + (gain if a == "cand" else 0.0)
            (b / f"run{n}-raggio.json").write_text(json.dumps({"raggio": {"ingest_s": 2549119 / vps, "ingest_vps": vps}}))
            (b / f"jobs-run{n}.json").write_text(json.dumps(
                {"jobs": {"done": 1275}, "payload_rows": None if a == "base" else 0, "freelist": 0}))
            (b / f"meta-run{n}.json").write_text(json.dumps(
                {"regime": "host-warm", "cap": "4g", "sqlite_version": "3.46.1",
                 "openblas_num_threads": "1", "memory_peak_mb": 3500.0}))
    return d


def edit(path, key, value):
    data = json.loads(path.read_text())
    data[key] = value
    path.write_text(json.dumps(data))


def summarize(d, adr=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = S.main([str(d)] + (["--adr", str(adr)] if adr else []))
    return code, out.getvalue()


def check_summary(tmp):
    code, out = summarize(fake_run(tmp))
    assert code == 0 and "- FAIL" not in out, out
    for g in ("D1-run", "D1-flat", "D1-ivf256", "D1-fingerprints", "D1-jobs", "D1-bench", "D1-labels"):
        assert f"- PASS: {g}\n" in out, g
    data = json.loads(out.split(S.FENCE + "json\n", 1)[1].split("\n" + S.FENCE, 1)[0])
    assert data["probe"]["flat"]["base"] == [1001.0, 1002.0, 1003.0]  # run0 is not counted
    assert data["probe"]["ivf256"]["verdict"] == "better" and data["bench"]["verdict"] == "better"
    code, out = summarize(fake_run(tmp, bench=False))
    assert code == 0 and "- SKIP: D1-bench\n" in out and S.NOT_RUN in out, out
    fails = {  # gate -> a mutation that must fail it
        "D1-run": lambda d: (d / "ab.log").write_text(START),
        "D1-flat": lambda d: edit(d / "probe/flat-cand-run2.json", "ingest_vps", 1000.5),
        "D1-ivf256": lambda d: (d / "probe/ivf256-cand-run3.json").unlink(),
        "D1-fingerprints": lambda d: edit(d / "probe/ivf256-base-run0.json", "fingerprint", dict(FP, vecs="v1")),
        "D1-jobs": lambda d: edit(d / "bench-cand/jobs-run2.json", "jobs", {"done": 1274, "error": 1}),
        "D1-bench": lambda d: edit(d / "bench-cand/run1-raggio.json", "raggio", {"ingest_vps": 2990.0}),
        "D1-labels": lambda d: edit(d / "bench-base/meta-run1.json", "openblas_num_threads", "unset"),
    }
    for gate, mutate in fails.items():
        d = fake_run(tmp)
        mutate(d)
        code, out = summarize(d)
        assert code == 1 and f"- FAIL: {gate}\n" in out, (gate, out)
    d = fake_run(tmp)
    edit(d / "probe/flat-cand-run1.json", "store_file", TREES["base"])  # cand measured the base tree
    assert "- FAIL: D1-labels\n" in summarize(d)[1]
    d = fake_run(tmp)
    edit(d / "probe/flat-cand-run1.json", "payload_rows", 3)  # a payload row left behind
    assert "- FAIL: D1-jobs\n" in summarize(d)[1]


def check_adr(tmp):
    real = Path("docs/adr/0001-performance-optimization-decisions.md").read_text(encoding="utf-8")
    assert real.count(S.RESULTS_HEAD) == 1, "run from the repo root, after Task 6 Step 2"
    d2 = "Pending: the gn100 A/B of Plan D Task 10 has not run yet."
    text = ("# ADR\n\n## Addendum 2026-09 " + S.DASH + " ingest path\n\n" + S.RESULTS_HEAD + "\n\n"
            + S.PENDING + "\n\n### Results " + S.DASH + " DGX A/B (D2)\n\n" + d2 + "\n")
    for nl in ("\n", "\r\n"):
        adr = Path(tmp) / f"adr-{len(nl)}.md"
        adr.write_bytes(text.replace("\n", nl).encode("utf-8"))
        assert summarize(fake_run(tmp, gain=1.0), adr)[0] == 1  # a failed gate records nothing
        assert adr.read_bytes() == text.replace("\n", nl).encode("utf-8")
        assert summarize(fake_run(tmp), adr)[0] == 0
        after = adr.read_bytes().decode("utf-8")
        assert S.PENDING not in after and d2 in after  # only the D1 Pending line is replaced
        assert ("\n" not in after.replace("\r\n", "")) if nl == "\r\n" else ("\r" not in after)
        body = after.replace("\r\n", "\n")
        start = body.index(S.RESULTS_HEAD)
        sec = body[start:body.index("\n### ", start + 1)]  # the gate test's slice
        assert "#### Measurements (D1)" in sec and "- PASS: D1-flat\n" in sec
        data = json.loads(sec.split(S.FENCE + "json\n", 1)[1].split("\n" + S.FENCE, 1)[0])
        assert data["gates"]["D1-bench"] == "PASS" and data["cand_sha"] == "bbbbbbbbbbbb"
        try:
            summarize(fake_run(tmp), adr)
        except SystemExit:
            pass
        else:
            raise AssertionError("a second record must refuse: no Pending line is left")


def main():
    check_text()
    with tempfile.TemporaryDirectory() as tmp:
        check_summary(tmp)
        check_adr(tmp)
    print("Plan D DGX scripts OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Check the scripts on the laptop**

Run from the repo root (the check imports `bench/dgx/compare.py` through the summary and reads the ADR from there):

Run: `uv run python D:/DEV/_scratch/dgx/planD/planD-check.py`
Expected: `Plan D DGX scripts OK`

Run: `bash -n D:/DEV/_scratch/dgx/planD/planD-ab.sh && bash -n D:/DEV/_scratch/dgx/planD/planD-build.sh && echo SYNTAX_OK`
Expected: `SYNTAX_OK`

If an assert fails, fix the script it names. Never weaken the check.

- [ ] **Step 5: Pin the SHAs and check the tree**

The base arm is `main` as it stood before D1, which is the parent of Task 1's commit. The candidate is `HEAD`, which is Step 2's ADR commit, so the D1 image carries the addendum.

```bash
cd D:/DEV/raggio
T1=$(git log --format=%H -n 1 --fixed-strings --grep="Add binary job payload codec and job_payloads side table" HEAD)
BASE_SHA=$(git rev-parse --short=12 "$T1^")
CAND_SHA=$(git rev-parse --short=12 HEAD)
echo "T1=$T1 BASE_SHA=$BASE_SHA CAND_SHA=$CAND_SHA"
git log --oneline -n 1 "$BASE_SHA"
git show "$BASE_SHA:docs/adr/0001-performance-optimization-decisions.md" | grep -c "ingest path"
git status --porcelain
```

Expected:
- `T1` is a full SHA (not empty), and the base line is Plan C's last commit;
- the `grep -c` prints `0`: the base tree predates D1's addendum;
- `git status --porcelain` prints nothing.

If `T1` is empty or the tree is dirty, stop and ask.

Run: `uv run pytest -q`
Expected: `N0 + 27 passed, 1 warning`

Write the env file. The last two values come from Step 1: `REINGEST=1 CAP_MIN=270` for the full A/B, or `REINGEST=0 CAP_MIN=85` for probe-only. For a shorter cap, set `CAP_MIN` to the granted minutes minus 20 for the builds.

```bash
printf 'BASE_SHA=%s\nCAND_SHA=%s\nREINGEST=%s\nCAP_MIN=%s\n' "$BASE_SHA" "$CAND_SHA" 1 270 >D:/DEV/_scratch/dgx/planD/d1-shas.env
cat D:/DEV/_scratch/dgx/planD/d1-shas.env
```

Expected: four lines, and the two SHAs are 12 hex digits each.

- [ ] **Step 6: Read-only preflight on gn100**

This step runs only after the user has granted the window in Step 1.

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
uptime
free -m | head -2
pgrep -af "[b]ench\.py|[i]ngest_probe" || echo "no bench.py or probe running"
podman ps -a --format "{{.Names}} {{.Status}}"
podman volume exists bench-tv && echo "volume: bench-tv"
df -h ~ | tail -n 1
ls -d ~/planD 2>/dev/null || echo "no ~/planD yet"'
```

Expected:
- a load average under about 2;
- `no bench.py or probe running`;
- no `bench-tv` line with status `Up` (a `bench-wv` line, in any state, is someone else's: leave it alone);
- `volume: bench-tv`;
- at least 20 GB free: two images plus a 1 GB probe work directory.

If a bench.py or probe is running, `bench-tv` is up, or the load is high, that is someone else's work in progress. Stop and ask the user, and never stop it yourself. If the volume is missing, stop and ask: the reingests need the collection's volume, and the probes do not replace them.

- [ ] **Step 7: Stage `~/planD`, copy the scripts and deploy both trees**

An existing `~/planD` holds an earlier session's results, so move it aside and never delete it. `scp` reads `D:/...` as a host name, so `cd` first and pass relative names.

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'if [ -e ~/planD ]; then mv ~/planD ~/planD.prev-$(date +%Y%m%d-%H%M%S); fi; mkdir -p ~/planD/base-src ~/planD/tmp'
cd D:/DEV/_scratch/dgx/planD
scp -F "$SSH_CONFIG" -o BatchMode=yes planD-ab.sh planD-build.sh gn100:planD/
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'ls -l ~/planD; bash -n ~/planD/planD-ab.sh && bash -n ~/planD/planD-build.sh && echo SYNTAX_OK'
```

Expected: `planD-ab.sh`, `planD-build.sh`, `base-src` and `tmp` are listed, then `SYNTAX_OK`.

Deploy D1's tree to `~/raggio` (spec §6), with the corpus link, the ground truth and the bench venv. `~/turborag` is only read here. Deploy the base tree to `~/planD/base-src` and copy D1's probe into it (Task 5: the probe only uses APIs the base has).

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planD/d1-shas.env
git archive "$CAND_SHA" | ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'rm -rf ~/raggio && mkdir ~/raggio && tar -x -C ~/raggio'
git archive "$BASE_SHA" | ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tar -x -C ~/planD/base-src'
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
cd ~/raggio || exit 1
ln -sfn ~/turborag/bench/corpus bench/corpus
cp ~/turborag/bench/gt-2549619-42-d1024.npz bench/
~/.local/bin/uv sync --frozen --group bench 2>&1 | tail -2
cp bench/ingest_probe.py ~/planD/base-src/bench/
.venv/bin/python -c "import raggio.store as s; print(\"cand tree OK\", hasattr(s, \"VACUUM_FREELIST_PAGES\"))"
test -f ~/planD/base-src/bench/ingest_probe.py && echo "base probe present"
grep -c VACUUM_FREELIST_PAGES ~/planD/base-src/src/raggio/store.py'
```

Expected: `cand tree OK True`, `base probe present`, then `0` (the base store has no D1 vacuum constant).

- [ ] **Step 8: Build both images (detached, then poll)**

`planD-build.sh` skips an image that already exists under its tag. The tags are `planD-<sha>`.

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planD/d1-shas.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "setsid -f bash -c 'bash ~/planD/planD-build.sh ~/planD/base-src planD-$BASE_SHA && bash ~/planD/planD-build.sh ~/raggio planD-$CAND_SHA' >>~/planD/build.log 2>&1 </dev/null"
```

Poll every 2 minutes:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'grep -E "^== BUILD" ~/planD/build.log; tail -n 2 ~/planD/build.log'
```

It is done when there are two `== BUILD_DONE` lines, for `planD-$BASE_SHA` and `planD-$CAND_SHA`. If a `== BUILD_FAILED` line appears, stop and report the tail of `~/planD/build.log` to the user.

- [ ] **Step 9: Launch the A/B (detached, capped)**

`timeout` enforces the cap on the whole process group. The driver also writes its process group id to `ab.pgid` for an early stop (Step 10).

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planD/d1-shas.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "setsid -f timeout --kill-after=60 ${CAP_MIN}m bash ~/planD/planD-ab.sh d1 $BASE_SHA $CAND_SHA $REINGEST $CAP_MIN >/dev/null 2>&1 </dev/null"
sleep 90
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'cat ~/planD/d1/ab.pgid; head -n 40 ~/planD/d1/ab.log'
```

Expected:
- a process group id;
- `== PLAND AB START <date> <time> mode=d1 base=<BASE_SHA> cand=<CAND_SHA> reingest=<0|1> cap=<CAP_MIN>m ==`;
- the uptime, memory, `Cached` and `podman ps -a` lines;
- two `image planD-<sha> id ... digest ...` lines, each followed by `turbovec <version> sqlite_version <version>`;
- `host venv sqlite_version <version> OPENBLAS_NUM_THREADS 1`;
- `labels: probe rows host-warm, uncapped host process; bench rows host-warm, cap 4g`;
- `== probe flat base run0 (<ts>) ==`.

If the log ends with `== PLAND AB FAILED: <reason>`, go to Step 11 to fetch it. Report the reason and the log tail to the user, then stop.

- [ ] **Step 10: Poll until done (every 5 minutes)**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'grep -E "^== (probe|reingest|PLAND AB)" ~/planD/d1/ab.log | tail -n 3; grep -E "^  (vps|jobs run|labels:)" ~/planD/d1/ab.log | tail -n 2; date'
```

Progress runs as follows:
- `== probe flat base run0`, then `flat base run1`, `flat cand run1`, and so on to `flat cand run3`. The same follows for `ivf256`. Each run is followed by a `  vps <n> drain <s> s stall <s> s jobs {'done': 200} payload_rows <None|0> freelist <n>` line (base `None`, D1 `0`);
- `== probes done`;
- then either `== reingest skipped: ...`, or `== reingest base run1`, `cand run1`, `base run2`, `cand run2`, each followed by `  jobs run<N>: {...}` and `  labels: host-warm, cap 4g, sqlite_version <v>, OPENBLAS_NUM_THREADS=1, memory.peak <bytes> bytes`;
- finally `== PLAND AB DONE <ts> ==`.

If `== PLAND AB FAILED: <reason>` appears, go to Step 11 to fetch the log. Report the reason and the log tail to the user, and stop: record nothing.

`timeout` ends the driver at the cap by itself. If the user asks to stop early, or the cap passed and a process is still listed, stop only this session's process group and `bench-tv`. Never use `pkill`, and never touch another container:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
PG=$(cat ~/planD/d1/ab.pgid)
kill -TERM -- "-$PG" 2>/dev/null
sleep 5
pgrep -af "[p]lanD-ab\.sh|[b]ench/bench\.py|[i]ngest_probe" || echo "no Plan D process left"
podman stop -t 60 bench-tv
podman ps -a --filter name=bench-tv --format "{{.Names}} {{.Status}}"'
```

Expected: `no Plan D process left`, then `bench-tv Exited (...)`. If a process is still listed, report it to the user and do not kill anything by name. If the stop hit a reingest (the log's last `==` line is `== reingest ...`), tell the user that the `bench-tv` volume holds a partial `bench` collection until one more reingest runs, and do not start one without a new go-ahead. Then fetch (Step 11), report, and stop.

- [ ] **Step 11: Fetch the results and preview the summary**

```bash
cd D:/DEV/_scratch/dgx/planD
if [ -e d1 ]; then mv d1 d1.prev-$(date +%Y%m%d-%H%M%S); fi
scp -r -F "$SSH_CONFIG" -o BatchMode=yes gn100:planD/d1 .
ls d1 d1/probe d1/bench-base d1/bench-cand
```

Expected:
- `ab.log`, `ab.pgid`, `bench-base`, `bench-cand`, `probe`;
- 14 files in `probe`: `flat-base-run0.json` through `ivf256-cand-run3.json`;
- with the reingests, per arm: `run1-raggio.json`, `run1-raggio.md`, `jobs-run1.json`, `meta-run1.json`, and the same four for run2. Without them, both arm directories are empty.

Preview from the repo root (nothing is written yet):

```bash
cd D:/DEV/raggio
uv run python D:/DEV/_scratch/dgx/planD/planD-summary.py D:/DEV/_scratch/dgx/planD/d1
echo "exit $?"
uv run python bench/dgx/compare.py D:/DEV/_scratch/dgx/planD/d1/bench-base D:/DEV/_scratch/dgx/planD/d1/bench-cand
```

Expected:
- the `#### Measurements (D1)` block, with `- PASS:` on `D1-run`, `D1-flat`, `D1-ivf256`, `D1-fingerprints`, `D1-jobs` and `D1-labels`, `- PASS:` or `- SKIP:` on `D1-bench`, and `exit 0`;
- with the reingests, Plan A's table showing the `Ingest throughput (vec/s)` row as `better`. Without them, the last command exits with `...bench-base: no run<N>-<engine>.json files`, which is expected.

If any line reads `- FAIL:`, stop here. Show the block to the user and do not go on to Step 12: the addendum keeps its `Pending:` line, and no test is appended. Never loosen a gate or rerun selectively to get a pass. A rerun is a new session: ask for a new window.

- [ ] **Step 12: Write the failing gate test**

Only after Step 11's preview shows no `- FAIL:` line. Append at the end of `tests/test_ingest_path.py`, below Task 5's section. The file is ASCII, so the heading's em dash is `chr(0x2014)`. `json`, `Path` and `sys` come from the module head, and `statistics` is imported inside the test.

```python
# ---- ADR 0001 addendum: D1 DGX results

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0001-performance-optimization-decisions.md"
D1_RESULTS = "### Results " + chr(0x2014) + " DGX A/B (D1)"


def adr_d1_results():
    """(the D1 results subsection with \\r\\n normalized, its Measurements JSON or {}).
    The subsection ends at the next ### or ## heading: Task 10 appends D2's after it."""
    text = ADR.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index(D1_RESULTS)
    ends = [i for i in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if i != -1]
    section = text[start:min(ends, default=len(text))]
    fence = "`" * 3
    if "#### Measurements (D1)" not in section or fence + "json\n" not in section:
        return section, {}
    raw = section.split(fence + "json\n", 1)[1].split("\n" + fence, 1)[0]
    return section, json.loads(raw)


def test_adr_ingest_path_d1_results_pass_the_gates():
    """Spec section 7 D on gn100: ingest vec/s beyond the noise band on the flat and IVF
    paths, every job done with no payload row left, and one final state across arms."""
    import statistics

    section, m = adr_d1_results()
    assert "Pending:" not in section
    assert "#### Measurements (D1)" in section and m, "no Measurements (D1) JSON block"
    assert {"date", "base_sha", "cand_sha", "probe", "bench", "labels", "gates"} <= set(m)
    assert m["base_sha"] != m["cand_sha"]

    def beyond_band(base, cand, floor=1.0):  # Plan A's rule: gain > max(bands, resolution)
        band = max(max(base) - min(base), max(cand) - min(cand), floor)
        return statistics.median(cand) - statistics.median(base) > band

    for mode in ("flat", "ivf256"):
        p = m["probe"][mode]
        assert len(p["base"]) == 3 and len(p["cand"]) == 3, mode
        assert beyond_band(p["base"], p["cand"]), f"probe {mode}: the gain is within the band"
        assert p["verdict"] == "better" and p["fingerprints_equal"] and p["jobs_all_done"], mode
        assert set(p["payload_rows"]["base"]) == {None}, mode  # the base has no side table
        assert set(p["payload_rows"]["cand"]) == {0}, mode  # D1 leaves no payload row
        assert f"- PASS: D1-{mode}\n" in section
    lp = m["labels"]["probe"]
    assert lp["regime"] == ["host-warm, uncapped host process"] and lp["cap"]
    assert lp["openblas_num_threads"] == ["1"] and len(lp["sqlite_version"]) == 1
    b = m["bench"]
    if b is None:  # probe-only window, or not enough time left for the reingests
        assert "Not run: the bench reingest" in section and "- SKIP: D1-bench\n" in section
    else:
        assert len(b["base"]) == 2 and len(b["cand"]) == 2
        assert beyond_band(b["base"], b["cand"]) and b["verdict"] == "better"
        assert len(b["runs"]) == 4
        for name, run in b["runs"].items():
            assert set(run["jobs"]) == {"done"}, name  # no error, pending or processing job
            assert run["payload_rows"] == (None if name.startswith("base") else 0), name
        lb = m["labels"]["bench"]
        assert lb["regime"] == ["host-warm"] and lb["cap"] == ["4g"]
        assert lb["openblas_num_threads"] == ["1"]
        assert all(len(v) == 1 for v in lb["sqlite_version"].values())
        assert "- PASS: D1-bench\n" in section
    for gate in ("D1-run", "D1-fingerprints", "D1-jobs", "D1-labels"):
        assert f"- PASS: {gate}\n" in section, gate
    assert "- FAIL:" not in section
```

- [ ] **Step 13: Run it to verify that it fails**

Run: `uv run pytest tests/test_ingest_path.py -v -k adr_ingest_path_d1`
Expected: `FAILED tests/test_ingest_path.py::test_adr_ingest_path_d1_results_pass_the_gates - AssertionError: assert 'Pending:' not in '### Results — DGX A/B (D1)\n\nPending: the gn100 A/B of Plan D Task 6 has not run yet.\n'` (pytest may shorten the quoted section), then `1 failed` and the rest deselected. The failure is on the first assert because the ADR still holds Step 2's `Pending:` line.

- [ ] **Step 14: Record the results in the ADR**

Run from the repo root. The summary replaces only the `Pending:` line of `### Results — DGX A/B (D1)`, and only when no gate reads `FAIL`. It keeps the file's line endings.

```bash
cd D:/DEV/raggio
uv run python D:/DEV/_scratch/dgx/planD/planD-summary.py D:/DEV/_scratch/dgx/planD/d1 --adr docs/adr/0001-performance-optimization-decisions.md
git diff --stat
```

Expected: the block from Step 11 again, then `planD-summary: recorded in docs/adr/0001-performance-optimization-decisions.md`. `git diff --stat` lists only the ADR and `tests/test_ingest_path.py`. `git diff docs/adr/0001-performance-optimization-decisions.md` shows one removed line (`Pending: ...`) and the added block. It shows no change above `### Results — DGX A/B (D1)`.

If the summary exits 1 now, a result file changed after the preview. Stop and report to the user: do not edit the ADR by hand, and revert the test (`git checkout -- tests/test_ingest_path.py`).

- [ ] **Step 15: Run the test to verify that it passes**

Run: `uv run pytest tests/test_ingest_path.py -v -k adr_ingest_path_d1`
Expected: `PASSED tests/test_ingest_path.py::test_adr_ingest_path_d1_results_pass_the_gates`, then `1 passed` and the rest deselected.

Run: `uv run pytest tests/test_cold_start.py -v -k acceptance_run`
Expected: `1 passed, 64 deselected`, unchanged from Step 2. Plan C's section still ends at `## Addendum 2026-09 — ingest path`.

- [ ] **Step 16: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 28 passed, 1 warning` (`281 passed, 1 warning` when N0 is 253). Only this task's test is new.

- [ ] **Step 17: Commit, then report to the user**

```bash
git add docs/adr/0001-performance-optimization-decisions.md tests/test_ingest_path.py
git commit -F - <<'EOF'
Record the D1 gn100 A/B results in ADR 0001

Replace the Pending line of "Results - DGX A/B (D1)" with the measured
block. It holds the in-process ingest probe rows (flat and IVF nlist
256, seed 42, 3 interleaved rounds per arm after a discarded warm-up)
and, when they ran, the end-to-end bench --reingest rows (2,549,119 x
1024, 4 GiB container, 2 interleaved runs per arm). Every row is
labelled with its regime, cap, sqlite_version and OPENBLAS_NUM_THREADS,
and a "Measurements (D1)" JSON block follows. Add
test_adr_ingest_path_d1_results_pass_the_gates, which re-checks each
gate from that JSON with Plan A's band rule.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

Then report to the user:
- the flat and IVF probe medians, gains and bands;
- the bench reingest rows, or that they did not run;
- the `sqlite_version` per arm;
- the state the run left on gn100: `bench-tv` stopped, and its volume flat and drained, written by the D1 image (or untouched when the reingests were skipped);
- `~/planD/d1` kept on gn100, and the local copy in `D:/DEV/_scratch/dgx/planD/d1/`.

**Review Focus for Task 6:** the header assigns none of RF1-RF5 to this task. Its own pins are `test_adr_ingest_path_d1_results_pass_the_gates` and Plan C's `test_adr_records_the_acceptance_run` (which must stay green once the new `## ` heading follows C's section). When reviewing, check that:
- nothing ran on gn100 before the user's explicit window answer (Step 1), and the launch ran under `timeout` with the granted cap. Every `ssh` and `scp` uses the full `-F "$SSH_CONFIG" -o BatchMode=yes` form;
- `planD-ab.sh` and `planD-build.sh` never name `bench-wv` or `~/turborag`, and use no `pkill`, `sudo` or `rm -rf`. `podman rm -f` appears only as `podman rm -f bench-tv`, and `--reingest` only inside `reingest()`. `planD-check.py` asserts all of this; the deploy step only reads `~/turborag`;
- the reingest phase comes after every probe run (spec §6), and it runs only with `REINGEST=1` and 180 min left. Each reingest runs in a fresh 4 GiB `bench-tv` of that arm's image, interleaved base, cand, base, cand;
- each probe arm runs its own tree's `bench/ingest_probe.py`, and both the driver and the summary check `store_file`. A base row measured with D1 code would fake the gain;
- every row in the block names its regime (`host-warm, uncapped host process` for the probe, `host-warm` for the bench), its cap (none, or `4g`), its `sqlite_version` and `OPENBLAS_NUM_THREADS`. The probe rows are never presented as capped-container numbers;
- the gates use Plan A's rule (`gain > max(base band, cand band, 1 vec/s)`), computed on the same rounded values the JSON stores, so the test's recomputation agrees with the summary. A `FAIL` records nothing, and no threshold is loosened;
- `adr_d1_results()` slices the D1 subsection up to the next `\n### ` or `\n## `, so Task 10's D2 sections cannot leak into it, and `#### Measurements (D1)` does not end it;
- a declined or probe-only window is handled: the addendum keeps `Pending:` (declined) or records `Not run: the bench reingest ...` with `- SKIP: D1-bench` (probe-only).

## Phase D2 (branch `ingest-path-d2`, base = `main` after D1, Plan E and Plan F)

Before Task 7: `git switch main && git pull && git switch -c ingest-path-d2`, then run `uv run pytest -q` and write down the pass count as **N0'** (see "Suite counts" above). Do not reuse N0: E and F add their own tests. `tests/test_ingest_path.py` holds D1's 28 tests at this point, or 27 if Task 6's window was declined and its gate test never landed; write down which. Every D2 edit anchors on quoted text. D2's text for D1's functions is the text Tasks 1-5 wrote, and the E/F lines are quoted as those plans print them.

### Task 7: Knobs, claim cursor, sync flag, removal cap

Goal: the plumbing the batched worker (Task 8) needs. Each piece is inert on its own: the worker still claims with no cursor, calls `_process_job(payload)` (so `sync=True`) and syncs after every job, so no behaviour changes in this task.
- The `SYNC_BATCH_JOBS` / `SYNC_BATCH_MS` knobs, from `Settings` through `CollectionManager` into `Collection`.
- A claim cursor: `_claim_next(after)` claims the lowest open job above `after`.
- A `sync` flag on `_process_job`.
- The cross-job removal cap: a counter `_removed_since_sync` that `_unindex` raises and `_sync_index` resets. `_process_job` syncs before a job's removals would push that counter past `SYNC_MAX_REMOVALS`.
- `--batch-jobs` / `--batch-ms` on the ingest probe, so Task 10 can tune N and T.

**Files:**
- Modify `src/raggio/config.py`: `Settings.__init__`. Append after F's last line, `self.ivf_search_threads = threads if threads > 0 else default_ivf_search_threads()` (F Task 1). E's `native_bm25` lines sit above it.
- Modify `src/raggio/store.py` (base 19a8a2e line numbers, orientation only; anchor on the quoted text):
  - module constant `SYNC_MAX_REMOVALS = 512`, directly below Task 4's `VACUUM_CHUNK_PAGES = 2_048`;
  - `_CLAIM_SQL` (Task 2's block, directly below `_decode_payload`): gains `AND id > ?` after the literal predicate, and `INDEXED BY idx_jobs_open` after the table name, so that the cursor query stays on C's index (Step 3d explains why). The predicate is unchanged;
  - `Collection.__init__`: the signature (base lines 562-564, as E and F left it) gains `sync_batch_jobs: int = 8, sync_batch_ms: float = 1000.0` after `ivf_search_threads`. The body stores both, plus `self._removed_since_sync = 0`, directly below F's `self._shard_pool = ...` line;
  - `Collection._claim_next` (Task 2's text, base lines 695-706): `after: int = 0`;
  - `Collection._process_job` (base lines 742-803): the signature gains `*, sync: bool = True`. The removal-cap check goes before Task 3's `_unindex` call. The `# ponytail:` comment and its `_sync_index` line become an `if sync:` block;
  - `Collection._unindex` (Task 3's text): counts its removals;
  - `Collection._sync_index` (base lines 860-864, unchanged by A-F): resets the counter after the sync;
  - `CollectionManager._construct` (Plan C; E and F added keywords to its `asyncio.to_thread(Collection, ...)` call): passes both knobs after F's `ivf_search_threads=` keyword.
- Modify `bench/ingest_probe.py` (Task 5): `build_parser` gains `--batch-jobs` and `--batch-ms`. `main` validates them. `run()` forwards them to `Collection(...)` only when given, and the result gains `sync_batch_jobs` / `sync_batch_ms`.
- Modify `tests/test_ingest_path.py`: one import-block edit (`Settings`, `CollectionManager`), then the section `# ---- batched sync: plumbing` appended at the end (10 tests).
- Test: `uv run pytest tests/test_ingest_path.py -v`.

**Interfaces:**
- Consumes:
  - Task 2: `_CLAIM_SQL`, and `Collection._claim_next(self) -> tuple[int, dict | None, str | None] | None`. The claim runs under `db_lock` and the decode after it.
  - Task 3: the `_process_job` lines `ids, fresh, replaced = await asyncio.to_thread(self._upsert_rows, rows, mat)` / `if replaced:` / `await asyncio.to_thread(self._unindex, replaced)`, inside `async with self.lock.write():`. Also `Collection._unindex(self, rids: list[int]) -> None` (the caller holds `lock.write()`), which `_delete_doc_rows` also calls for deletes.
  - Task 4: the `VACUUM_CHUNK_PAGES = 2_048` line (the insertion anchor).
  - Task 5: `bench/ingest_probe.py`'s `build_parser`, `main` and `run`, plus the test helpers `load_probe(monkeypatch)` and `probe_args(tmp_path, **over)`.
  - Plan F: `Settings.ivf_search_threads`, `Collection(..., ivf_search_threads: int | None = None)`, `self._shard_pool = _ShardPool(...)`, and the `ivf_search_threads=self.settings.ivf_search_threads,` keyword in `_construct`. Also F's `_sync_index` contract: `_IvfIndex.sync(directory) -> int` writes only dirty shards, and `_sync_index` ignores the int. Plan E: `native_bm25` (keyword and setting), which stays where E put it.
  - Plan C: `idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')`, and C's `test_open_job_queries_use_the_partial_index`, which EXPLAINs its own literal claim string (not `_CLAIM_SQL`) and calls `col._claim_next()` with no argument.
  - Test helpers `make_collection`, `docs_payload`, `fetch`, `index_ids`.
- Produces:
  - `Settings.sync_batch_jobs: int` (`SYNC_BATCH_JOBS`, default 8) and `Settings.sync_batch_ms: float` (`SYNC_BATCH_MS`, default 1000.0). `Settings()` raises `ValueError` naming the variable when jobs < 1 or ms < 0 (NaN included).
  - `SYNC_MAX_REMOVALS = 512`: a module-level algorithm constant (spec §4.2), read at call time, so tests monkeypatch it on `raggio.store`.
  - `Collection.__init__(self, cfg, directory, embedder_factory, set_index_config=None, native_bm25: bool = True, ivf_search_threads: int | None = None, sync_batch_jobs: int = 8, sync_batch_ms: float = 1000.0)`, and the attributes `Collection.sync_batch_jobs: int`, `Collection.sync_batch_ms: float` and `Collection._removed_since_sync: int`. Task 8's worker reads the first two; `_process_job` reads the third.
  - `_CLAIM_SQL` with one `?` parameter: `"SELECT id, payload FROM jobs INDEXED BY idx_jobs_open WHERE status IN ('pending','processing') AND id > ? ORDER BY id LIMIT 1"`. It plans as one `SEARCH` on `idx_jobs_open`.
  - `Collection._claim_next(self, after: int = 0) -> tuple[int, dict | None, str | None] | None`. It claims the lowest open job with `id > after`, and `after=0` gives the old order.
  - `Collection._process_job(self, payload: dict, *, sync: bool = True) -> None`. With `sync=False`, an ingest job commits, removes and adds in memory but does not write the index file. Only a removal-cap pre-sync can write it then. Index jobs ignore the flag.
  - `Collection._unindex` adds `len(rids)` to `_removed_since_sync`. `Collection._sync_index` sets it to 0 after a sync that returns; a sync that raises leaves it as it was.
  - `bench/ingest_probe.py`: `--batch-jobs N` (int >= 1) and `--batch-ms T` (float >= 0), both default `None`, which means not forwarded. Result keys `sync_batch_jobs` / `sync_batch_ms` read back from the collection; they are `None` in a tree without them.

- [ ] **Step 1: Write the failing tests**

(a) In `tests/test_ingest_path.py`, the module head from Task 1 has:

```python
import raggio.store as store  # noqa: E402
from raggio.store import Collection, CollectionConfig, open_meta_db  # noqa: E402
```

Replace it with:

```python
import raggio.store as store  # noqa: E402
from raggio.config import Settings  # noqa: E402
from raggio.store import Collection, CollectionConfig, CollectionManager, open_meta_db  # noqa: E402
```

(b) Append this section at the end of the file, below Task 6's section, or below Task 5's section if Task 6's gate test never landed. The tests call `_claim_next`, `_process_job` and `_sync_index` directly, without a worker: Task 8 wires them into one. Spies go on the instance (`col._sync_index = spy`), because `_process_job` looks the method up through `self` at call time.

```python
# ---- batched sync: plumbing


def test_settings_sync_batch_knobs(monkeypatch):
    monkeypatch.delenv("SYNC_BATCH_JOBS", raising=False)
    monkeypatch.delenv("SYNC_BATCH_MS", raising=False)
    s = Settings()
    assert (s.sync_batch_jobs, s.sync_batch_ms) == (8, 1000.0)
    assert type(s.sync_batch_jobs) is int and type(s.sync_batch_ms) is float
    monkeypatch.setenv("SYNC_BATCH_JOBS", "3")
    monkeypatch.setenv("SYNC_BATCH_MS", "250.5")
    s = Settings()
    assert (s.sync_batch_jobs, s.sync_batch_ms) == (3, 250.5)


@pytest.mark.parametrize("name, value", [("SYNC_BATCH_JOBS", "0"), ("SYNC_BATCH_MS", "-1")])
def test_settings_rejects_bad_sync_batch_knobs(monkeypatch, name, value):
    # a batch of zero jobs could never close, and a negative wait is a typo: refuse to boot
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings()


def test_manager_passes_sync_batch_knobs_to_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_BATCH_JOBS", "3")
    monkeypatch.setenv("SYNC_BATCH_MS", "250")

    async def go():
        mgr = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        try:
            await mgr.create_collection("m", 8, 4, None, None, None)
            col = await mgr.touch("m")
            return col.sync_batch_jobs, col.sync_batch_ms
        finally:
            await mgr.shutdown()

    assert asyncio.run(go()) == (3, 250.0)
```

Then, in the same section:

```python
def test_claim_cursor_skips_already_claimed_jobs(tmp_path):
    # the batched worker claims job k+1 while job k is still 'processing' (a job is
    # finished only after its batch's sync), so the cursor, not the status, moves on
    col = make_collection(tmp_path)
    try:
        assert [col._enqueue_row(docs_payload([i])) for i in (1, 2, 3)] == [1, 2, 3]
        assert col._claim_next()[0] == 1  # no cursor: the lowest open job, as before
        assert col._claim_next(1)[0] == 2
        job_id, payload, error = col._claim_next(2)
        assert (job_id, payload["documents"][0]["doc_id"], error) == (3, "d3", None)
        assert col._claim_next(3) is None  # nothing open above the cursor
        assert fetch(col, "SELECT id, status FROM jobs ORDER BY id") == [
            (1, "processing"), (2, "processing"), (3, "processing")]
        # cursor 0 (a worker's first claim, as on boot): 'processing' jobs replay first
        assert col._claim_next()[0] == 1
    finally:
        asyncio.run(col.stop())


def test_claim_query_never_scans_the_journal(tmp_path):
    # the journal keeps every done job. jobs.id is the rowid, so a bare `id > ?` may
    # plan as a rowid range, and every worker's first claim (cursor 0) would walk all
    # the done rows: the walk Plan C's partial index idx_jobs_open exists to avoid
    col = make_collection(tmp_path)
    try:
        with col.db_lock:
            col.db.executemany(LEGACY_JOB, [(i, "{}", "done") for i in range(1, 51)]
                               + [(51, "{}", "processing"), (52, "{}", "pending")])
            col.db.commit()
        assert store._CLAIM_SQL.count("?") == 1  # the cursor
        plan = " | ".join(r[3] for r in fetch(col, "EXPLAIN QUERY PLAN " + store._CLAIM_SQL, 0))
        # one range search on the open-jobs index, in id order: no rowid range, no sort
        assert plan.startswith("SEARCH") and "USING INDEX idx_jobs_open" in plan, plan
        assert "PRIMARY KEY" not in plan and "TEMP B-TREE" not in plan, plan
        assert col._claim_next(51)[0] == 52
        assert col._claim_next()[0] == 51
    finally:
        asyncio.run(col.stop())


def spy_sync(col):
    """Wrap col._sync_index. Each call first appends the removal count it found (None
    before Task 7 added the counter), then runs the real sync. Returns that list."""
    seen, real = [], col._sync_index

    def spy():
        seen.append(getattr(col, "_removed_since_sync", None))
        real()

    col._sync_index = spy
    return seen


def test_process_job_sync_flag(tmp_path):
    # sync=False is the batched worker's call: the job commits and updates the index in
    # memory, and leaves writing the index file to its batch's one sync
    col = make_collection(tmp_path)

    async def run():
        try:
            seen = spy_sync(col)
            await col._process_job(docs_payload([1, 2]))  # records 1, 2
            calls = [len(seen)]
            await col._process_job(docs_payload([3]), sync=False)  # record 3
            calls.append(len(seen))
            return calls, index_ids(col), fetch(col, "SELECT id FROM vecs ORDER BY id")
        finally:
            await col.stop()

    calls, ids, vecs = asyncio.run(run())
    assert calls == [1, 1]  # the default still syncs after every job; sync=False never
    assert ids == [1, 2, 3]  # searchable at once: the in-memory index has record 3
    assert vecs == [(1,), (2,), (3,)]  # and its rows are committed
```

Then, in the same section:

```python
def test_removal_cap_syncs_before_crossing_the_cap(tmp_path, monkeypatch):
    # turbovec keeps each removal as a redo op in the index file's header and rewrites
    # the whole file past 1024 of them, so one sync must not carry more than
    # SYNC_MAX_REMOVALS removals piled up across a batch's jobs. A lone job over the
    # cap is not split: it syncs with everything it removed, as every job does today
    monkeypatch.setattr(store, "SYNC_MAX_REMOVALS", 10)
    col = make_collection(tmp_path)

    async def run():
        try:
            await col._process_job(docs_payload(range(1, 13)))  # records 1-12, synced
            seen = spy_sync(col)
            for _ in range(3):  # each re-upsert replaces docs 1-6: 6 removals per job
                await col._process_job(docs_payload(range(1, 7)), sync=False)
            steps = [list(seen), col._removed_since_sync]
            await asyncio.to_thread(col._sync_index)  # what the batch's flush will do
            steps += [list(seen), col._removed_since_sync]
            # one job replacing all 12 rows (docs 1-6's current rows, docs 7-12's first)
            await col._process_job(docs_payload(range(1, 13)), sync=False)
            steps += [list(seen), col._removed_since_sync]
            await col._process_job(docs_payload([13]))  # the default sync=True
            steps += [list(seen), col._removed_since_sync]
            return steps, index_ids(col), fetch(col, "SELECT id FROM records ORDER BY id")
        finally:
            await col.stop()

    steps, ids, records = asyncio.run(run())
    assert steps == [
        [6, 6], 6,  # jobs 2 and 3 each synced the 6 pending removals first: 6 + 6 > 10
        [6, 6, 6], 0,  # the flush wrote job 3's 6 and reset the count
        [6, 6, 6], 12,  # the count was 0, so no pre-sync: the lone job keeps all 12
        [6, 6, 6, 12], 0,  # the next sync writes them in one go
    ]
    assert ids == [r[0] for r in records] and len(ids) == 13  # nothing lost, no ghost


def test_sync_resets_the_removal_counter(tmp_path):
    # every sync writes the pending removals, whoever runs it (the worker, the cap,
    # delete_document, stop()), so every sync resets the count
    col = make_collection(tmp_path)

    async def run():
        try:
            await col._process_job(docs_payload([1, 2, 3]))  # records 1-3, synced
            counts = [col._removed_since_sync]
            await col._process_job(docs_payload([1, 2]), sync=False)  # replaces 2 rows
            counts.append(col._removed_since_sync)
            await asyncio.to_thread(col._sync_index)
            counts.append(col._removed_since_sync)
            seen = spy_sync(col)
            await col.delete_document("d3")  # removes record 3's id, then syncs
            counts.append(col._removed_since_sync)
            return counts, list(seen)
        finally:
            await col.stop()

    counts, seen = asyncio.run(run())
    assert counts == [0, 2, 0, 0]
    assert seen == [1]  # delete_document's _unindex counted its removal before the sync
```

Then, closing the section:

```python
def test_ingest_probe_accepts_batch_flags(tmp_path, monkeypatch):
    # Task 10 tunes N and T with the probe. Without the flags it builds the collection
    # exactly as before, so the same file still runs on a tree without the knobs
    probe = load_probe(monkeypatch)
    made, real = [], probe.Collection

    def spy(*args, **kw):
        made.append(kw)
        return real(*args, **kw)

    monkeypatch.setattr(probe, "Collection", spy)
    out = probe.main(probe_args(tmp_path, batch_jobs=3, batch_ms=250))
    assert made == [{"sync_batch_jobs": 3, "sync_batch_ms": 250.0}]
    assert (out["sync_batch_jobs"], out["sync_batch_ms"]) == (3, 250.0)
    assert (out["args"]["batch_jobs"], out["args"]["batch_ms"]) == (3, 250.0)
    assert out["jobs"] == {"done": 4}
    made.clear()
    out = probe.main(probe_args(tmp_path))
    assert made == [{}]
    assert (out["sync_batch_jobs"], out["sync_batch_ms"]) == (8, 1000.0)
    assert out["args"]["batch_jobs"] is None and out["args"]["batch_ms"] is None
    for bad in ({"batch_jobs": 0}, {"batch_ms": -1}):
        with pytest.raises(SystemExit):
            probe.main(probe_args(tmp_path, **bad))
```

`probe_args` turns `batch_ms=-1` into `--batch-ms -1`. argparse reads the `-1` as the option's value, because the parser has no option that looks like a negative number.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `10 failed, 28 passed` (Tasks 1-6's 28 pass), or `10 failed, 27 passed` without Task 6's gate test. The failures, in file order:
- `test_settings_sync_batch_knobs`: `AttributeError: 'Settings' object has no attribute 'sync_batch_jobs'`.
- `test_settings_rejects_bad_sync_batch_knobs[SYNC_BATCH_JOBS-0]` and `[SYNC_BATCH_MS--1]`: `Failed: DID NOT RAISE <class 'ValueError'>`.
- `test_manager_passes_sync_batch_knobs_to_collection`: `AttributeError: 'Collection' object has no attribute 'sync_batch_jobs'`.
- `test_claim_cursor_skips_already_claimed_jobs`: `TypeError: Collection._claim_next() takes 1 positional argument but 2 were given`, at `col._claim_next(1)`.
- `test_claim_query_never_scans_the_journal`: `assert 0 == 1` at `store._CLAIM_SQL.count("?")`.
- `test_process_job_sync_flag`: `TypeError: Collection._process_job() got an unexpected keyword argument 'sync'`. The first job, which syncs by default, has already passed through the spy.
- `test_removal_cap_syncs_before_crossing_the_cap`: `AttributeError: <module 'raggio.store' from '...'> has no attribute 'SYNC_MAX_REMOVALS'`, raised by `monkeypatch.setattr`.
- `test_sync_resets_the_removal_counter`: `AttributeError: 'Collection' object has no attribute '_removed_since_sync'`.
- `test_ingest_probe_accepts_batch_flags`: `SystemExit: 2`. stderr has argparse's `error: unrecognized arguments: --batch-jobs 3 --batch-ms 250`.

- [ ] **Step 3: Implement the plumbing**

(a) `src/raggio/config.py`. The last lines of `Settings.__init__` are Plan F's:

```python
        threads = int(os.environ.get("IVF_SEARCH_THREADS", "0") or 0)
        self.ivf_search_threads = threads if threads > 0 else default_ivf_search_threads()
```

Append directly below them, as the last lines of `__init__`:

```python
        # the worker's batched index sync (spec 4.2, D8): one sync closes a batch of at
        # most SYNC_BATCH_JOBS jobs or SYNC_BATCH_MS ms, whichever comes first; 1 = per job
        self.sync_batch_jobs = int(os.environ.get("SYNC_BATCH_JOBS", "8"))
        self.sync_batch_ms = float(os.environ.get("SYNC_BATCH_MS", "1000"))
        if self.sync_batch_jobs < 1:
            raise ValueError(f"SYNC_BATCH_JOBS must be >= 1, not {self.sync_batch_jobs}")
        if not self.sync_batch_ms >= 0:  # also refuses nan
            raise ValueError(f"SYNC_BATCH_MS must be >= 0, not {self.sync_batch_ms}")
```

(b) `src/raggio/store.py`, module constants. Task 4 left:

```python
VACUUM_FREELIST_PAGES = 16_384
VACUUM_CHUNK_PAGES = 2_048
```

Add directly below `VACUUM_CHUNK_PAGES = 2_048`, before the blank line that follows it:

```python
# removals one index sync may carry when a batch piles them up across jobs. turbovec
# keeps each removal as a redo op in the index file's header and rewrites the whole
# file past 1024 of them, so the worker syncs before a job would take the count over
# this. A lone job over the cap is not split: it syncs with all its own removals
SYNC_MAX_REMOVALS = 512
```

(c) `Collection.__init__`. Its signature ends with Plan F's parameter:

```python
        ivf_search_threads: int | None = None,
    ) -> None:
```

Replace those two lines with:

```python
        ivf_search_threads: int | None = None,
        sync_batch_jobs: int = 8,
        sync_batch_ms: float = 1000.0,
    ) -> None:
```

In the body, Plan F's line is:

```python
        self._shard_pool = _ShardPool(ivf_search_threads or default_ivf_search_threads(), cfg.name)
```

Add directly below it:

```python
        # the worker's batch bounds (Settings.sync_batch_jobs / sync_batch_ms) and the
        # removals made since the last index sync: _unindex counts, _sync_index resets,
        # and _process_job syncs first when a job would take the count over the cap
        self.sync_batch_jobs = sync_batch_jobs
        self.sync_batch_ms = sync_batch_ms
        self._removed_since_sync = 0
```

The attributes sit before the index load and `_reconcile_ghosts`. The reconcile's removals are its own business: it syncs what it evicts itself, and the counter starts at 0 afterwards either way.

In `CollectionManager._construct`, the `asyncio.to_thread(Collection, ...)` call (Plan C) ends with Plan E's and Plan F's keywords:

```python
            native_bm25=self.settings.native_bm25 == "auto",
            ivf_search_threads=self.settings.ivf_search_threads,
        ))
```

Replace those three lines with:

```python
            native_bm25=self.settings.native_bm25 == "auto",
            ivf_search_threads=self.settings.ivf_search_threads,
            sync_batch_jobs=self.settings.sync_batch_jobs,
            sync_batch_ms=self.settings.sync_batch_ms,
        ))
```

(d) The claim cursor. Replace Task 2's block:

```python
# the worker's claim: the lowest open job. 'processing' is included so jobs interrupted
# by a crash replay on boot. Keep the literal predicate byte for byte: it is what Plan
# C's partial index idx_jobs_open matches (spec 4.1), and C's plan test pins this text
_CLAIM_SQL = (
    "SELECT id, payload FROM jobs WHERE status IN ('pending','processing') ORDER BY id LIMIT 1"
)
```

with:

```python
# the worker's claim: the lowest open job above a cursor. 'processing' is included so
# jobs interrupted by a crash replay on boot (cursor 0). The batched worker moves the
# cursor past the jobs it claimed, which stay 'processing' until their batch's sync.
# Keep the literal predicate byte for byte: Plan C's partial index idx_jobs_open
# matches it (spec 4.1). INDEXED BY holds the plan on that index: jobs.id is the
# rowid, so the planner may otherwise serve `id > ?` as a rowid range, and a claim
# from cursor 0 would then walk every done row in the journal
_CLAIM_SQL = (
    "SELECT id, payload FROM jobs INDEXED BY idx_jobs_open"
    " WHERE status IN ('pending','processing') AND id > ? ORDER BY id LIMIT 1"
)
```

`INDEXED BY` needs the index to exist and to be usable. `open_meta_db` creates `idx_jobs_open` on every open (Plan C, `CREATE INDEX IF NOT EXISTS`). The literal predicate makes the partial index usable. If the index were missing, the claim would fail loudly (`no such index`) instead of silently walking the journal. C's `test_open_job_queries_use_the_partial_index` EXPLAINs its own literal claim string, not `_CLAIM_SQL`, so it does not change.

In `Collection._claim_next`, replace the signature, the docstring's first sentence and the execute. That is, replace:

```python
    def _claim_next(self) -> tuple[int, dict | None, str | None] | None:
        """Claim the lowest open job. Returns (job_id, payload, None), or
```

with:

```python
    def _claim_next(self, after: int = 0) -> tuple[int, dict | None, str | None] | None:
        """Claim the lowest open job with an id above `after` (0: the lowest open job,
        as on boot; the batched worker passes the last id it claimed, because a claimed
        job stays 'processing' until its batch syncs). Returns (job_id, payload, None), or
```

and replace:

```python
            row = self.db.execute(_CLAIM_SQL).fetchone()
```

with:

```python
            row = self.db.execute(_CLAIM_SQL, (after,)).fetchone()
```

The rest of `_claim_next` stays: the `job_payloads` lookup, the UPDATE to `'processing'`, the commit, and the decode after the lock.

(e) `Collection._process_job`. Replace the signature line:

```python
    async def _process_job(self, payload: dict) -> None:
```

with:

```python
    async def _process_job(self, payload: dict, *, sync: bool = True) -> None:
        """Run one job. An ingest job ends by writing the index file (_sync_index) only
        when sync is True, the default every direct caller keeps; the batched worker
        passes False and syncs once per batch. Index jobs (attach/detach) ignore it:
        their swap writes the new index in full."""
```

Plan C's head (`op = payload.get("op")` and the attach/detach branch) follows unchanged. Then replace Task 3's lines:

```python
            ids, fresh, replaced = await asyncio.to_thread(self._upsert_rows, rows, mat)
            if replaced:
                await asyncio.to_thread(self._unindex, replaced)
```

with:

```python
            ids, fresh, replaced = await asyncio.to_thread(self._upsert_rows, rows, mat)
            if replaced:
                if (self._removed_since_sync
                        and self._removed_since_sync + len(replaced) > SYNC_MAX_REMOVALS):
                    # write earlier jobs' removals first, so no sync carries more than
                    # the cap across jobs. A job alone over it is not split (count 0)
                    await asyncio.to_thread(self._sync_index)
                await asyncio.to_thread(self._unindex, replaced)
```

The pre-sync runs under the same `lock.write()`, after this job's commit and before its removals and adds. The index file then holds the earlier jobs' changes, whose jobs are still `'processing'` in a batch. A crash after it replays them and this job, and replay is idempotent, so an extra sync is always safe. Last, replace the tail of the ingest path:

```python
            # ponytail: sync after every job; batch on an interval if write throughput matters
            await asyncio.to_thread(self._sync_index)
```

with:

```python
            # the batched worker passes sync=False and writes the index once per batch
            # (Settings.sync_batch_jobs / sync_batch_ms); a direct call syncs here
            if sync:
                await asyncio.to_thread(self._sync_index)
```

(f) The counter. Replace Task 3's `_unindex`:

```python
    def _unindex(self, rids: list[int]) -> None:
        """Remove records whose rows a committed transaction replaced or deleted from
        the vector index (the .tvim drops them at the next _sync_index). The caller
        holds lock.write(), so no search runs meanwhile; db_lock is not needed."""
        for rid in rids:
            self.index.remove(rid)
```

with:

```python
    def _unindex(self, rids: list[int]) -> None:
        """Remove records whose rows a committed transaction replaced or deleted from
        the vector index (the .tvim drops them at the next _sync_index). The caller
        holds lock.write(), so no search runs meanwhile; db_lock is not needed."""
        # counted from the list, never from remove()'s return (None on _IvfIndex), and
        # before the loop: a remove that raises midway leaves the count high, which
        # only brings the next pre-sync early
        self._removed_since_sync += len(rids)
        for rid in rids:
            self.index.remove(rid)
```

and replace `_sync_index` (base lines 860-864, as Plans A-F leave it):

```python
    def _sync_index(self) -> None:
        if isinstance(self.index, _IvfIndex):
            self.index.sync(self.ivf_dir)
        else:
            self.index.sync(str(self.index_path))
```

with:

```python
    def _sync_index(self) -> None:
        if isinstance(self.index, _IvfIndex):
            self.index.sync(self.ivf_dir)
        else:
            self.index.sync(str(self.index_path))
        # the file now holds every pending removal. A sync that raises skips this, so
        # the count stays, and the next attempt still sees what it carries
        self._removed_since_sync = 0
```

Every `_sync_index` resets the count, whoever runs it: the ingest tail, the cap, `delete_document`, `_reconcile_ghosts`, `stop()`. The attach/detach swap writes its new index directly (`ivf.sync(tmp)` / `flat.sync(str(tmp))`, not `_sync_index`), so the count survives it. The new index has no pending removals, so a stale count can at most bring one pre-sync early. Task 8 also flushes the batch before any index job. `_IvfIndex.sync` still returns F's dirty-shard count, and `_sync_index` still ignores it.

(g) `bench/ingest_probe.py`. In `build_parser`, replace:

```python
    p.add_argument("--out", default=None, help="also write the result JSON to this file")
    return p
```

with:

```python
    p.add_argument("--out", default=None, help="also write the result JSON to this file")
    p.add_argument("--batch-jobs", type=int, default=None,
                   help="SYNC_BATCH_JOBS for the collection (default: Collection's own)")
    p.add_argument("--batch-ms", type=float, default=None,
                   help="SYNC_BATCH_MS for the collection (default: Collection's own)")
    return p
```

In `main`, directly below the `>= 0` loop:

```python
    for name in ("prefill", "reupsert_every", "ivf", "seed"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must be >= 0")
```

add:

```python
    if args.batch_jobs is not None and args.batch_jobs < 1:
        parser.error("--batch-jobs must be >= 1")
    if args.batch_ms is not None and not args.batch_ms >= 0:
        parser.error("--batch-ms must be >= 0")
```

In `run`, replace:

```python
    col = Collection(CollectionConfig("probe", args.dim, 4, None, None, None), work, lambda: None)
```

with:

```python
    # forwarded only when given: without the flags the probe builds the collection as
    # before, so this file still runs on a tree whose Collection has no such keywords
    batch = {}
    if args.batch_jobs is not None:
        batch["sync_batch_jobs"] = args.batch_jobs
    if args.batch_ms is not None:
        batch["sync_batch_ms"] = args.batch_ms
    col = Collection(
        CollectionConfig("probe", args.dim, 4, None, None, None), work, lambda: None, **batch
    )
```

and in the result dict, replace its last line and the brace:

```python
        "regime": "host-warm, uncapped host process",
    }
```

with:

```python
        "regime": "host-warm, uncapped host process",
        # the batch bounds the collection ran with (None on a tree without them)
        "sync_batch_jobs": getattr(col, "sync_batch_jobs", None),
        "sync_batch_ms": getattr(col, "sync_batch_ms", None),
    }
```

`col` is still bound after `stop()`, and the two attributes are plain values. The prefill keeps calling `col._process_job(...)` with the default `sync=True`, so the prefill does not change. `result["args"]` picks up `batch_jobs` / `batch_ms` through `vars(args)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `38 passed`, or `37 passed` without Task 6's gate test.

Then run the suites that pin the code this task touched:

Run: `uv run pytest tests/test_cold_start.py tests/test_concurrency.py tests/test_ivf_fanout.py tests/test_ivf_index.py tests/test_native_bm25.py tests/test_store_optimizations.py tests/test_api.py -q`

Expected: all pass, with the same counts as on the branch base. In particular:
- C's `test_open_job_queries_use_the_partial_index`: it EXPLAINs its own literal claim, and `col._claim_next()[0] == 51` runs with the default cursor 0.
- F's `test_settings_ivf_search_threads` and `test_manager_passes_setting_to_collection`: the new `Settings` lines and `_construct` keywords sit after F's.
- F's `test_one_row_job_syncs_one_shard` and `test_reupsert_that_moves_a_row_syncs_both_shards`: they call `_process_job` directly, so the default `sync=True` still syncs per job.
- E's `NATIVE_BM25` settings and manager tests.
- B's `test_no_threading_lock_is_held_across_an_await`: the pre-sync awaits under the asyncio `lock.write()`, never under `db_lock`.
- Base `test_ivf_index.py::test_upsert_and_delete_while_indexed`: `_unindex` counts `len(rids)` and ignores `_IvfIndex.remove`'s None.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`

Expected: `N0' + 10 passed, 1 warning`. No existing test changes.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/config.py src/raggio/store.py bench/ingest_probe.py tests/test_ingest_path.py
git commit -F - <<'EOF'
Add batched-sync plumbing: knobs, claim cursor, sync flag, removal cap

SYNC_BATCH_JOBS (8) and SYNC_BATCH_MS (1000) reach Collection through
Settings and CollectionManager. _claim_next takes a cursor (after=0 is
the old claim). _CLAIM_SQL keeps C's literal predicate and is held on
idx_jobs_open with INDEXED BY, so the cursor never walks done rows.
_process_job gains a keyword-only sync=True; with sync=False an ingest
job leaves the index file to its batch. _unindex counts removals, every
_sync_index resets the count, and a job whose removals would take a
batch past SYNC_MAX_REMOVALS (512) syncs first; a lone job is not split.
The ingest probe gains --batch-jobs / --batch-ms. The worker itself is
unchanged and still syncs after every job.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Review Focus for Task 7:** RF5 (one sync carries removals from several jobs past the full-rewrite cliff) is pinned here by `test_removal_cap_syncs_before_crossing_the_cap`, with `test_sync_resets_the_removal_counter` for the reset. When reviewing, check that:
- the cap check sits inside `if replaced:` and before `_unindex`, under the same `lock.write()`. It needs a non-zero count, so a lone job over the cap is not split, and it uses `>`, so a sync may carry exactly `SYNC_MAX_REMOVALS`. It reads `SYNC_MAX_REMOVALS` as a module global at call time;
- `_removed_since_sync` rises only in `_unindex` (from `len(rids)`, never from `remove()`'s return) and falls to 0 only in `_sync_index`, after the sync call returns. `_reconcile_ghosts` removes directly and syncs at once, so it needs no count;
- `_CLAIM_SQL` keeps the literal `status IN ('pending','processing')` byte for byte and has exactly one `?`. Its plan is one `SEARCH` on `idx_jobs_open`, with no rowid range and no sort (`test_claim_query_never_scans_the_journal`);
- `after` defaults to 0 and `sync` to True, so every existing caller behaves as before: the worker (it still claims with no cursor and syncs per job), C's claim pin, F's per-job shard tests, the probe's prefill. Nothing in `_run_worker` changes in this task;
- the `Collection` defaults (8, 1000.0) equal the `Settings` defaults, so collections built directly (tests, the probe) batch like served ones once Task 8 lands;
- the probe forwards the flags only when given (`made == [{}]` without them), so the same file still runs in the trees Task 10 compares;
- out of scope: the batched worker and its flush (Task 8), the `stop()` flush (Task 9), and ADR 0001 line 50's lever note, which points at the `ponytail:` comment this task removes (Task 10 rewrites it).

### Task 8: Batched worker with done-after-sync

Goal: the worker defers the index sync over a batch of jobs and finishes each job only after the sync that covers it (spec §4.3, D8).
- A batch closes when it holds `sync_batch_jobs` jobs, when `sync_batch_ms` have passed since its first job joined, or when the queue goes idle (the claim finds nothing).
- Attach and detach jobs are barriers. The open batch is flushed first, then the index job runs alone and finishes at once.
- The worker keeps a local claim cursor (Task 7), because its own claimed jobs stay `'processing'` until their batch syncs.
- A crash anywhere in a batch leaves the batch's jobs `'processing'`, and the existing replay redoes them on the next open.

`stop()` does not change in this task. It still cancels the worker and syncs, so jobs left in an open batch stay `'processing'` and replay on the next open. Task 9 makes `stop()` finish them.

**Files:**
- Modify `src/raggio/store.py` (base 19a8a2e line numbers, orientation only; anchor on the quoted text):
  - `Collection.__init__` (base lines 541-619): adds `self._unsynced` and `self._batch_t0` directly below Task 7's `self.sync_batch_jobs` / `self.sync_batch_ms` / `self._removed_since_sync = 0` lines;
  - `Collection._run_worker` (base lines 723-740, as Task 2 left it): rewritten as a whole;
  - new `Collection._flush_unsynced`, directly below `_run_worker` and above `_process_job`.
- Modify `tests/test_ingest_path.py`: one import-block edit (`shutil`), then the section `# ---- batched sync: worker` appended at the end (helpers plus 10 test functions, 12 test ids).
- Test: `uv run pytest tests/test_ingest_path.py -v`.

**Interfaces:**
- Consumes:
  - Task 7: `Collection.sync_batch_jobs: int`, `Collection.sync_batch_ms: float`, `Collection._claim_next(self, after: int = 0)`, `Collection._process_job(self, payload: dict, *, sync: bool = True)`, `SYNC_MAX_REMOVALS` (the pre-sync inside `_process_job`), and `_sync_index` resetting `_removed_since_sync`. Test helper `spy_sync(col)`.
  - Task 2: `_claim_next` returns `(job_id, payload, None)` or `(job_id, None, "bad job payload: ...")`, and the legacy-payload error text `bad job payload: payload is a JSON list, not an object`. Test helpers `LEGACY_JOB` and `fetch`.
  - Task 3: a zero vector makes `_process_job` raise `ValueError("zero vector cannot be normalized")` (from `_normalize`, via `_rows_matrix`) before any row is written, and a failed upsert rolls back. Test helper `index_ids(col)` (flat only).
  - Task 4: `_finish_job(self, job_id, status, error)`, whose vacuum policy runs the full vacuum only once no job is open.
  - Plan B: `self.lock.write()` (the RW lock), `stop()`'s write-locked cancel, and `test_stop_waits_for_a_write_transaction_its_cancelled_worker_left_running`, which replaces `col._finish_job` with a 3-argument function.
  - Plan F: `_IvfIndex.dirty_shards -> frozenset[int]` and `_IvfIndex.sync(directory) -> int`, which writes each dirty shard through `shard.sync(str(path))`. Only the IVF test uses them, and it skips without Plan F.
  - Test helpers `make_collection`, `docs_payload`, `vec`, `drain`.
- Produces:
  - `Collection._unsynced: list[tuple[int, str, str | None]]`: the open batch, one `(job_id, status, error)` per job processed and not yet finished. Task 9's `stop()` finishes it once its own sync lands. It leaves the whole batch `'processing'` for the replay if that sync fails (spec §3.1 D2), and the failing job and every job after it if a finish fails (spec §3.1 D3).
  - `Collection._batch_t0: float | None`: the `time.monotonic()` at which the open batch's first job joined, or `None` when no batch is open.
  - `async Collection._flush_unsynced(self) -> None`: one `_sync_index` under `lock.write()`, then each queued job's `_finish_job`, popped before its await. Task 9 adds the retry loop to it.
  - `Collection._run_worker(self) -> None`: batched. Ingest jobs run through `_process_job(payload, sync=False)`, and index jobs through `_process_job(payload)`.

- [ ] **Step 1: Write the failing tests**

(a) In `tests/test_ingest_path.py`, the module head (Task 1's, with Task 2's `threading`) has:

```python
import json
import sqlite3
```

Replace it with:

```python
import json
import shutil
import sqlite3
```

(b) Append this section at the end of the file, below Task 7's `# ---- batched sync: plumbing` section. The helpers come first.

These tests enqueue every job before they start the worker. The claim then always finds the next job, so the batches are deterministic: the count cap and the idle flush close them, and no enqueue races the worker. The time cap has a test of its own.

Spies go on the instance (`col._process_job = ...`), as in Task 7. `stop()` runs one more `_sync_index`, which the spies also record, so every test reads its events before `stop()`.

```python
# ---- batched sync: worker


def fingerprint(col):
    """What a run leaves behind, keyed by external id so that two collections whose
    internal ids differ still compare: every record row, every stored vector, the
    published indexed_counts and the vector index size. Call it before stop()."""
    return (
        fetch(col, "SELECT external_id, doc_id, type, position, text, metadata, indexed"
                   " FROM records ORDER BY external_id"),
        fetch(col, "SELECT r.external_id, v.vec FROM records r JOIN vecs v ON v.id = r.id"
                   " ORDER BY r.external_id"),
        dict(col.indexed_counts),
        len(col.index),
    )


def snapshot(col, dst):
    """Copy the collection's directory as a crash at this instant would leave it.
    Under db_lock no transaction is half-written. The -shm file is skipped: Windows
    holds byte-range locks on it, and SQLite rebuilds it from the -wal on open."""
    with col.db_lock:
        shutil.copytree(col.dir, dst, ignore=shutil.ignore_patterns("*-shm"))


async def run_batched(col, payloads, timeout=30):
    """Journal every payload, then start the worker and wait until no job is open."""
    for p in payloads:
        await asyncio.to_thread(col._enqueue_row, p)
    col.start_worker()
    await drain(col, timeout)


def reference(tmp_path, payloads, **kw):
    """The fingerprint the same jobs leave when every job syncs on its own
    (sync_batch_jobs=1), in a fresh collection under tmp_path / "reference"."""
    d = tmp_path / "reference"
    d.mkdir()
    col = make_collection(d, sync_batch_jobs=1, **kw)

    async def go():
        try:
            await run_batched(col, payloads)
            return fingerprint(col)
        finally:
            await col.stop()

    return asyncio.run(go())


def track(col, after_job=None):
    """Record the worker's steps, in order, in the returned list:
    ("job", label) when a job's _process_job ends (label: the job's first doc_id,
    or its op for an index job); ("sync", labels) when an index sync starts, with the
    labels of the ingest jobs begun since the previous sync; ("synced",) when that
    sync returns; ("finish", job_id, status) when a job is finished.
    after_job(label, kw), if given, is awaited after each job that did not raise."""
    events, since = [], []
    real_process, real_sync, real_finish = col._process_job, col._sync_index, col._finish_job

    async def process(payload, **kw):
        docs = payload.get("documents")
        label = docs[0]["doc_id"] if docs else payload["op"]
        if docs:
            since.append(label)
        try:
            await real_process(payload, **kw)
        finally:
            events.append(("job", label))
        if after_job is not None:
            await after_job(label, kw)

    def sync():
        events.append(("sync", list(since)))
        since.clear()
        real_sync()
        events.append(("synced",))

    def finish(job_id, status, error):
        events.append(("finish", job_id, status))
        real_finish(job_id, status, error)

    col._process_job, col._sync_index, col._finish_job = process, sync, finish
    return events


def batches(events):
    """The labels each index sync covered, one list per sync."""
    return [e[1] for e in events if e[0] == "sync"]


def late_finishes(events):
    """Job ids finished before their data reached the index file, for runs whose job n
    is doc "dn". Job n is on time only if a sync starts after its ("job", ...) event,
    that sync returns, and only then is job n finished."""
    late = []
    for i, e in enumerate(events):
        if e[0] != "job":
            continue
        n = int(e[1][1:])
        start = next((k for k in range(i + 1, len(events)) if events[k][0] == "sync"), None)
        end = None if start is None else next(
            (k for k in range(start + 1, len(events)) if events[k][0] == "synced"), None)
        fin = next((k for k, f in enumerate(events) if f[0] == "finish" and f[1] == n), None)
        if end is None or fin is None or fin < end:
            late.append(n)
    return late
```

Then, in the same section, the batch bounds:

```python
def test_batch_syncs_once_for_many_jobs(tmp_path):
    # before: every job wrote the whole index file. Now the 8 jobs of one batch share
    # one sync, and every one of them ends 'done'
    col = make_collection(tmp_path, sync_batch_jobs=8, sync_batch_ms=60_000)
    events = track(col)

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in range(1, 9)])
            return list(events), fetch(col, "SELECT id, status FROM jobs ORDER BY id"), index_ids(col)
        finally:
            await col.stop()

    events, jobs, ids = asyncio.run(go())
    assert batches(events) == [[f"d{j}" for j in range(1, 9)]]
    assert jobs == [(j, "done") for j in range(1, 9)]
    assert ids == list(range(1, 9))


def test_no_job_is_done_before_its_covering_sync(tmp_path):
    # D8: 'done' promises that the job's rows are in the index file, so they survive a
    # crash. Each job may finish only after a sync that began after its _process_job
    # ended has returned
    col = make_collection(tmp_path, sync_batch_jobs=3, sync_batch_ms=60_000)
    events = track(col)

    async def go():
        try:
            await run_batched(col, [docs_payload([j, j + 100]) for j in range(1, 9)])
            return list(events), fetch(col, "SELECT status FROM jobs ORDER BY id")
        finally:
            await col.stop()

    events, statuses = asyncio.run(go())
    assert late_finishes(events) == []
    assert [len(b) for b in batches(events)] == [3, 3, 2]  # two count-capped, one idle
    assert statuses == [("done",)] * 8


def test_idle_queue_flushes_the_open_batch(tmp_path):
    # both caps are far away, so only the claim finding nothing can close the batch:
    # a trickle of jobs must still reach the index file and 'done' promptly
    col = make_collection(tmp_path, sync_batch_jobs=100, sync_batch_ms=60_000)
    seen = spy_sync(col)

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in (1, 2, 3)], timeout=10)
            first = len(seen)
            await col.enqueue(docs_payload([4]))  # the worker is idle: this wakes it
            await drain(col, timeout=10)
            return first, len(seen), fetch(col, "SELECT status FROM jobs ORDER BY id")
        finally:
            await col.stop()

    first, second, statuses = asyncio.run(go())
    assert (first, second) == (1, 2)  # one sync per idle flush, not one per job
    assert statuses == [("done",)] * 4
```

```python
def test_batch_closes_at_the_time_cap(tmp_path):
    # a batch closes once sync_batch_ms have passed since its first job joined, even
    # with more jobs queued. Waiting out a real cap would make the test slow and
    # timing-dependent, so after_job ages the open batch instead, as if 60 s had passed
    aged, zero = tmp_path / "aged", tmp_path / "zero"
    aged.mkdir()
    zero.mkdir()
    col = make_collection(aged, sync_batch_jobs=100, sync_batch_ms=5_000)

    async def age(label, kw):
        if label in ("d2", "d4") and getattr(col, "_batch_t0", None) is not None:
            col._batch_t0 -= 60.0

    events = track(col, after_job=age)
    col0 = make_collection(zero, sync_batch_jobs=100, sync_batch_ms=0)
    events0 = track(col0)

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in range(1, 6)])
            await run_batched(col0, [docs_payload([j]) for j in (1, 2, 3)])
            return batches(events), batches(events0)
        finally:
            await col.stop()
            await col0.stop()

    got, got0 = asyncio.run(go())
    assert got == [["d1", "d2"], ["d3", "d4"], ["d5"]]  # aged twice, then the idle flush
    assert got0 == [["d1"], ["d2"], ["d3"]]  # sync_batch_ms=0: each batch closes at once


def test_claim_cursor_processes_each_job_once(tmp_path):
    # a batch's jobs stay 'processing' until its sync, so a claim on status alone would
    # hand the worker its own open jobs again. The worker's cursor moves past them
    col = make_collection(tmp_path, sync_batch_jobs=8, sync_batch_ms=60_000)
    events = track(col)
    claims, real_claim = [], col._claim_next

    def claim(*args):
        claims.append(args)
        return real_claim(*args)

    col._claim_next = claim

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in range(1, 21)])
            return list(claims), list(events), fetch(col, "SELECT COUNT(*) FROM records")
        finally:
            await col.stop()

    claims, events, records = asyncio.run(go())
    # a fresh worker starts at cursor 0, then claims above the last job it claimed
    assert claims[:21] == [(k,) for k in range(21)]
    assert set(claims[21:]) <= {(20,)}  # idle: nothing is open above job 20
    assert [e[1] for e in events if e[0] == "job"] == [f"d{j}" for j in range(1, 21)]
    assert [e[1] for e in events if e[0] == "finish"] == list(range(1, 21))
    assert [len(b) for b in batches(events)] == [8, 8, 4]
    assert records == [(20,)]


def test_error_job_in_a_batch_is_isolated(tmp_path):
    # a failing job joins the batch as 'error'. It neither aborts the batch nor
    # finishes ahead of it, and the good jobs around it all land
    col = make_collection(tmp_path, sync_batch_jobs=6, sync_batch_ms=60_000)
    zero = docs_payload([3])
    zero["documents"][0]["chunks"][0]["vector"] = [0.0] * 8  # fails in _process_job
    for p in (docs_payload([1]), docs_payload([2]), zero):
        col._enqueue_row(p)
    with col.db_lock:  # job 4: a pre-2026-09 row that decodes to a list, not an object
        col.db.execute(LEGACY_JOB, (4, "[]", "pending"))
        col.db.commit()
    for p in (docs_payload([5]), docs_payload([6])):
        col._enqueue_row(p)
    events = track(col)

    async def go():
        col.start_worker()
        try:
            await drain(col)
            return (list(events), fetch(col, "SELECT id, status, error FROM jobs ORDER BY id"),
                    fetch(col, "SELECT external_id FROM records ORDER BY external_id"))
        finally:
            await col.stop()

    events, jobs, records = asyncio.run(go())
    assert [e for e in events if e[0] != "job"] == [
        ("sync", ["d1", "d2", "d3", "d5", "d6"]), ("synced",),
        ("finish", 1, "done"), ("finish", 2, "done"), ("finish", 3, "error"),
        ("finish", 4, "error"), ("finish", 5, "done"), ("finish", 6, "done"),
    ]
    assert jobs == [
        (1, "done", None), (2, "done", None),
        (3, "error", "zero vector cannot be normalized"),
        (4, "error", "bad job payload: payload is a JSON list, not an object"),
        (5, "done", None), (6, "done", None),
    ]
    assert records == [("c1",), ("c2",), ("c5",), ("c6",)]
```

Then the index jobs and calibration:

```python
def test_index_jobs_are_batch_barriers(tmp_path, monkeypatch):
    # attach and detach rebuild the index and write it themselves. The open batch is
    # flushed before them, and the index job runs alone and finishes at once
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 32)
    col = make_collection(tmp_path, sync_batch_jobs=100, sync_batch_ms=60_000)
    payloads = [docs_payload([101]), docs_payload([102]),
                {"op": "attach_index", "nlist": 4, "nprobe": None},
                docs_payload([103]), {"op": "detach_index"}, docs_payload([104])]

    async def go():
        try:
            await col._process_job(docs_payload(range(1, 65)))  # nlist 4 needs >= 32 rows
            events = track(col)
            await run_batched(col, payloads)
            return (list(events), col.index_info(),
                    fetch(col, "SELECT COUNT(*) FROM records"), len(col.index))
        finally:
            await col.stop()

    events, info, records, size = asyncio.run(go())
    assert events == [
        ("job", "d101"), ("job", "d102"),
        ("sync", ["d101", "d102"]), ("synced",), ("finish", 1, "done"), ("finish", 2, "done"),
        ("job", "attach_index"), ("finish", 3, "done"),
        ("job", "d103"),
        ("sync", ["d103"]), ("synced",), ("finish", 4, "done"),
        ("job", "detach_index"), ("finish", 5, "done"),
        ("job", "d104"),
        ("sync", ["d104"]), ("synced",), ("finish", 6, "done"),
    ]
    assert (info, records, size) == ({"type": "flat"}, [(68,)], 68)


def test_calibration_crossing_inside_one_batch_matches_per_job(tmp_path, monkeypatch):
    # the job that crosses CAL_THRESHOLD re-encodes the index in memory. Batched, the
    # re-encoded index reaches the file at the batch's one sync instead of at that
    # job's own sync, and a reopen finds the same state either way
    monkeypatch.setattr(store, "CAL_THRESHOLD", 6)
    monkeypatch.setattr(store, "CAL_SAMPLE", 4)
    payloads = [docs_payload([2 * j - 1, 2 * j]) for j in range(1, 6)]  # 5 jobs, 10 rows

    def run(name, n):
        d = tmp_path / name
        d.mkdir()
        col = make_collection(d, sync_batch_jobs=n, sync_batch_ms=60_000)
        col._cal_rng = np.random.default_rng(7)
        seen = spy_sync(col)

        async def go():
            try:
                await run_batched(col, payloads)
                return len(seen), col.index.calibration_state
            finally:
                await col.stop()

        syncs, live = asyncio.run(go())
        col = make_collection(d)  # reopen: what the files hold
        try:
            return (syncs, live, col.index.calibration_state, col._cal_reservoir is None,
                    fingerprint(col), index_ids(col))
        finally:
            asyncio.run(col.stop())

    per_job, batched = run("per_job", 1), run("batched", 5)
    assert (per_job[0], batched[0]) == (5, 1)  # the crossing (job 3) sits inside the batch
    assert batched[1:] == per_job[1:]
    assert batched[1:4] == ("calibrated", "calibrated", True)
    assert batched[5] == list(range(1, 11))
```

Then the crash window. Each case copies the live collection's files at one instant inside the second batch, as a crash there would leave them. It then reopens the copy and lets the replay run. The trigger conditions are states that only a batching worker reaches, so on the old per-job worker no copy is taken and the `taken` assertion fails.

```python
def versioned(ids, v):
    """docs_payload(ids) with per-version text and vectors, so a replay that left an
    older version where a newer one belongs would show in the fingerprint."""
    p = docs_payload(ids)
    for d, i in zip(p["documents"], ids):
        chunk = d["chunks"][0]
        chunk["text"], chunk["vector"] = f"text {i} v{v}", vec(1000 * v + i)
    return p


def crash_jobs():
    # at cap 4, jobs 1-4 make the first batch and jobs 5-6 the second. Jobs 3, 5 and 6
    # replace records that earlier jobs wrote, and job 6 replaces one of job 5's
    return [versioned([1, 2, 3, 4], 1), versioned([5, 6, 7, 8], 1), versioned([2, 9], 2),
            versioned([10, 11], 1), versioned([1, 5, 12], 2), versioned([5, 13], 3)]


@pytest.mark.parametrize("point", ["mid_batch", "before_sync", "after_sync_before_finish"])
def test_crash_window_replays_without_loss(tmp_path, point):
    # wherever a crash lands inside a batch, reopening must replay the batch to the
    # state that the same jobs leave when each one syncs on its own
    live, image = tmp_path / "live", tmp_path / "crash"
    live.mkdir()
    ref = reference(tmp_path, crash_jobs())
    col = make_collection(live, sync_batch_jobs=4, sync_batch_ms=60_000)
    processed, finished, taken = [], [], []
    real_process, real_sync, real_finish = col._process_job, col._sync_index, col._finish_job

    def crash_here(at):
        if at == point and not taken:
            snapshot(col, image)
            taken.append(at)

    async def process(payload, **kw):
        await real_process(payload, **kw)
        processed.append(len(processed) + 1)  # job n is the n-th one processed
        if processed[-1] == 5 and kw.get("sync") is False:
            crash_here("mid_batch")  # job 5 committed, not synced; job 6 still pending

    def sync():
        if 6 in processed and 5 not in finished:
            crash_here("before_sync")  # jobs 5 and 6 committed, the index file older
        real_sync()

    def finish(job_id, status, error):
        if job_id == 5 and 6 in processed:
            crash_here("after_sync_before_finish")  # synced, 5 and 6 still 'processing'
        real_finish(job_id, status, error)
        finished.append(job_id)

    col._process_job, col._sync_index, col._finish_job = process, sync, finish

    async def go():
        try:
            await run_batched(col, crash_jobs())
            return fingerprint(col)
        finally:
            await col.stop()

    got_live = asyncio.run(go())
    assert taken == [point], f"the {point} crash point never occurred"
    assert got_live == ref  # the spies change nothing

    col = make_collection(image, sync_batch_jobs=4, sync_batch_ms=60_000)

    async def replay():
        try:
            jobs = fetch(col, "SELECT id, status FROM jobs ORDER BY id")
            col.start_worker()
            await drain(col)
            return (jobs, fingerprint(col), index_ids(col),
                    fetch(col, "SELECT id FROM records WHERE indexed=1 ORDER BY id"),
                    fetch(col, "SELECT DISTINCT status FROM jobs"),
                    fetch(col, "SELECT COUNT(*) FROM job_payloads"))
        finally:
            await col.stop()

    jobs, got, ids, indexed, statuses, payload_rows = asyncio.run(replay())
    six = "pending" if point == "mid_batch" else "processing"
    assert jobs == [(j, "done") for j in range(1, 5)] + [(5, "processing"), (6, six)]
    assert got == ref  # no lost row, no stale version, same counts, same index size
    assert ids == [r[0] for r in indexed]  # no ghost left in the index, none missing
    assert statuses == [("done",)]
    assert payload_rows == [(0,)]
```

Last, the IVF sync. It needs Plan F's dirty-shard tracking and skips on a tree without it:

```python
class ShardWrites:
    """Delegating proxy for one IVF shard that logs its number j each time the shard
    file is written (Plan F's SyncSpy pattern in tests/test_ivf_fanout.py)."""

    def __init__(self, inner, log, j):
        self.inner, self.log, self.j = inner, log, j

    def __len__(self):
        return len(self.inner)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def sync(self, path):
        self.log.append(self.j)
        return self.inner.sync(path)


def shards_holding(ivf, ids):
    """The numbers of the shards whose id maps hold any of ids."""
    return {j for j, sh in enumerate(ivf.shards) for i in ids if sh.contains(i)}


@pytest.mark.skipif(not hasattr(store._IvfIndex, "dirty_shards"),
                    reason="needs Plan F's dirty-shard tracking")
def test_batched_ivf_sync_writes_each_dirty_shard_once(tmp_path, monkeypatch):
    # an IVF sync writes only the shards changed since the last one (Plan F). A batch
    # of four jobs is then one sync, which writes each shard any of them touched once
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 32)
    col = make_collection(tmp_path, sync_batch_jobs=4, sync_batch_ms=60_000)
    payloads = [docs_payload([1, 101, 102]), docs_payload([2, 103, 104]),
                docs_payload([105, 106, 107]), docs_payload([108, 109, 110])]

    async def go():
        try:
            await col._process_job(docs_payload(range(1, 65)))
            await col._process_job({"op": "attach_index", "nlist": 4, "nprobe": None})
            ivf = col.index
            assert isinstance(ivf, store._IvfIndex) and ivf.dirty_shards == frozenset()
            old = [r[0] for r in fetch(
                col, "SELECT id FROM records WHERE external_id IN ('c1', 'c2')")]
            touched = shards_holding(ivf, old)  # re-upserting c1 and c2 removes these ids
            wrote, flushes, real_sync = [], [], col._sync_index
            ivf.shards = [ShardWrites(sh, wrote, j) for j, sh in enumerate(ivf.shards)]

            def sync():
                dirty, start = ivf.dirty_shards, len(wrote)
                real_sync()
                flushes.append((dirty, wrote[start:], ivf.dirty_shards))

            col._sync_index = sync
            await run_batched(col, payloads)
            new = [r[0] for r in fetch(col, "SELECT id FROM records WHERE id > 64")]
            return list(flushes), touched | shards_holding(ivf, new)
        finally:
            await col.stop()

    flushes, touched = asyncio.run(go())
    assert len(flushes) == 1  # four jobs, one sync
    dirty, wrote, after = flushes[0]
    assert dirty == frozenset(touched)  # every shard a job touched, and only those
    assert sorted(wrote) == sorted(dirty)  # each one written exactly once
    assert after == frozenset()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `12 failed, 38 passed` (`12 failed, 37 passed` if Task 6's gate test never landed). Task 7's `Collection` already takes both knobs, and `_process_job` already takes `sync=`, so every failure is an assertion, not a `TypeError`:
- `test_batch_syncs_once_for_many_jobs`: `assert [['d1'], ['d2'], ['d3'], ['d4'], ['d5'], ['d6'], ...] == [['d1', 'd2', 'd3', 'd4', 'd5', 'd6', ...]]`. The old worker syncs inside every job.
- `test_no_job_is_done_before_its_covering_sync`: `assert [1, 2, 3, 4, 5, 6, ...] == []`.
- `test_idle_queue_flushes_the_open_batch`: `assert (3, 4) == (1, 2)`.
- `test_batch_closes_at_the_time_cap`: `assert [['d1'], ['d2'], ['d3'], ['d4'], ['d5']] == [['d1', 'd2'], ['d3', 'd4'], ['d5']]`. The ageing is a no-op there, because the old code has no `_batch_t0`.
- `test_claim_cursor_processes_each_job_once`: `assert [(), (), (), (), (), (), ...] == [(0,), (1,), (2,), (3,), (4,), (5,), ...]`.
- `test_error_job_in_a_batch_is_isolated`: the event list differs at index 0, `('sync', ['d1']) != ('sync', ['d1', 'd2', 'd3', 'd5', 'd6'])`.
- `test_index_jobs_are_batch_barriers`: differs at index 0, `('sync', ['d101']) != ('job', 'd101')`.
- `test_calibration_crossing_inside_one_batch_matches_per_job`: `assert (5, 5) == (5, 1)`.
- `test_crash_window_replays_without_loss[mid_batch]`, `[before_sync]` and `[after_sync_before_finish]`: `AssertionError: the mid_batch crash point never occurred` (likewise `before_sync`, `after_sync_before_finish`).
- `test_batched_ivf_sync_writes_each_dirty_shard_once`: `assert 4 == 1`, with `where 4 = len([...])`. On a tree without Plan F it is skipped instead: `11 failed, 38 passed, 1 skipped`.

- [ ] **Step 3: Implement the batched worker**

All three edits are in `src/raggio/store.py`. `time` is already imported (`__init__` reads `time.monotonic()`).

(a) `Collection.__init__`. Task 7's block ends with these lines (the only place where the three stand together; `_sync_index` also has a `self._removed_since_sync = 0`, so anchor on all three):

```python
        self.sync_batch_jobs = sync_batch_jobs
        self.sync_batch_ms = sync_batch_ms
        self._removed_since_sync = 0
```

Add directly below them:

```python
        # the worker's open batch: (job_id, status, error) for each job it processed
        # that no index sync covers yet. Each one finishes only after such a sync
        # (_flush_unsynced). _batch_t0 is time.monotonic() when the batch's first job
        # joined, and None while no batch is open
        self._unsynced: list[tuple[int, str, str | None]] = []
        self._batch_t0: float | None = None
```

(b) `Collection._run_worker`. The whole method as it stands (base, plus Task 2's unpack; Task 4's `_vacuum_after_finish` sits directly above it):

```python
    async def _run_worker(self) -> None:
        while True:
            # clear BEFORE claiming: an enqueue landing during the claim either becomes
            # visible to the claim itself or re-sets the event, so no wakeup is lost
            self._wake.clear()
            row = await asyncio.to_thread(self._claim_next)
            if row is None:
                await self._wake.wait()
                continue
            # decoded in the claim's thread; an undecodable row comes back as
            # (job_id, None, reason) and finishes as 'error' like any failed job
            job_id, payload, bad = row
            try:
                if bad is not None:
                    raise ValueError(bad)
                await self._process_job(payload)
                status, error = "done", None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status, error = "error", str(e)
            await asyncio.to_thread(self._finish_job, job_id, status, error)
```

Replace it with:

```python
    async def _run_worker(self) -> None:
        # claim cursor: a claimed job stays 'processing' until its batch's sync, so each
        # claim starts above the last job this worker claimed. It is 0 on every start,
        # so the jobs a crash or a stop() left 'processing' replay first, in id order
        after = 0
        while True:
            # clear BEFORE claiming: an enqueue landing during the claim either becomes
            # visible to the claim itself or re-sets the event, so no wakeup is lost
            self._wake.clear()
            row = await asyncio.to_thread(self._claim_next, after)
            if row is None:
                if self._unsynced:
                    # the queue is idle: close the open batch now, not at a cap
                    await self._flush_unsynced()
                    continue
                await self._wake.wait()
                continue
            # decoded in the claim's thread; an undecodable row comes back as
            # (job_id, None, reason) and finishes as 'error' like any failed job
            job_id, payload, bad = row
            after = job_id
            if bad is None and payload.get("op") in ("attach_index", "detach_index"):
                # an index job is a barrier: it rebuilds the index and writes the new
                # one itself. The open batch syncs and finishes first, then the index
                # job runs alone and finishes at once
                if self._unsynced:
                    await self._flush_unsynced()
                try:
                    await self._process_job(payload)
                    status, error = "done", None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    status, error = "error", str(e)
                await asyncio.to_thread(self._finish_job, job_id, status, error)
                continue
            try:
                if bad is not None:
                    raise ValueError(bad)
                # sync=False: the job commits its rows and updates the in-memory index.
                # The batch's one sync writes the file, and only then does the job finish
                await self._process_job(payload, sync=False)
                status, error = "done", None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status, error = "error", str(e)
            # a failed job joins the batch too: finishes stay in claim order, and one
            # finish path covers every job
            if not self._unsynced:
                self._batch_t0 = time.monotonic()
            self._unsynced.append((job_id, status, error))
            if (
                len(self._unsynced) >= self.sync_batch_jobs
                or (time.monotonic() - self._batch_t0) * 1000.0 >= self.sync_batch_ms
            ):
                await self._flush_unsynced()
```

Why this shape:
- The cursor is a local, not an attribute, so every `start_worker()` starts at 0. Plan C's `idx_jobs_open` keeps that first claim cheap, and Task 7 pins the query to it.
- The time cap is checked after each job joins. A batch can therefore outlive `sync_batch_ms` by at most one job's processing time. When the queue runs dry, the idle flush closes the batch without waiting for a cap.
- A job that raised holds no rows (Task 3 rolls a failed upsert back), so it waits for the sync only to keep its finish in claim order.
- A removal-cap pre-sync inside `_process_job` (Task 7) may write the index in mid-batch. The batch's earlier jobs are then already in the file, but they still finish only at the flush: late, never early.
- Index jobs keep the default `sync=True`. `_process_job` returns straight after `_attach_index` / `_detach_index`, which write their index themselves.

(c) Add the new method directly below `_run_worker`, above `async def _process_job(self, payload: dict, *, sync: bool = True) -> None:`:

```python
    async def _flush_unsynced(self) -> None:
        """Close the worker's open batch: one index sync covers every job in it, then
        each job finishes, in claim order. None of them is 'done' before that sync
        returns (the job journal invariant), so a crash before it leaves them all
        'processing' and the replay redoes them. A crash between the sync and the last
        finish replays the unfinished rest, which rewrites the same rows."""
        async with self.lock.write():  # the per-job sync ran under it too
            await asyncio.to_thread(self._sync_index)
        self._batch_t0 = None  # the batch closed at its sync
        while self._unsynced:
            # pop BEFORE the await: a stop() that cancels the worker mid-finish leaves
            # this job to the orphaned thread, and nothing finishes it twice
            job = self._unsynced.pop(0)
            await asyncio.to_thread(self._finish_job, *job)
```

Why this shape:
- The sync takes `lock.write()`, as the per-job sync inside `_process_job`'s write section always has. The finishes run outside it: `_finish_job` serializes on `db_lock` alone, and holding the write lock across them would block searches for no reason. No new code takes `db_lock` inside `lock.write()`, so the lock order is unchanged.
- If a sync raises, the exception leaves `_run_worker` and ends the worker, with `_unsynced` intact and every job in it still `'processing'`. `drain()` re-raises it in tests, and the next open replays the jobs. Task 9 replaces this with a retry loop.
- `stop()` is unchanged in this task. It takes `lock.write()` first, so the worker it cancels is either waiting for the flush's lock (the cancel lands there, `_unsynced` untouched) or running a finish outside it. Either way, `stop()`'s own `_sync_index` writes the batch's rows, and the jobs left in `_unsynced` stay `'processing'` and replay on the next open, rewriting the same rows. Task 9 swaps that `_sync_index` line for `_sync_and_finish`, which finishes them.
- Plan B's `test_stop_waits_for_a_write_transaction_its_cancelled_worker_left_running` replaces `col._finish_job` on the instance, as before. The flush calls it through `self` with the same three arguments, so that test's slow finish runs in the orphaned thread, and `stop()` still waits for it under `db_lock`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `50 passed`, or `49 passed` without Task 6's gate test.

Then run the suites that pin the worker and the code around it:

Run: `uv run pytest tests/test_cold_start.py tests/test_concurrency.py tests/test_ivf_fanout.py tests/test_ivf_index.py tests/test_native_bm25.py tests/test_store_optimizations.py tests/test_api.py -q`

Expected: all pass, with the same counts as after Task 7. In particular:
- B's `test_stop_waits_for_a_write_transaction_its_cancelled_worker_left_running`: the job's idle flush syncs, then calls the test's slow `_finish_job`. `stop()` cancels the worker while that finish is in flight, and still waits for it under `db_lock`.
- B's `test_no_threading_lock_is_held_across_an_await`: the flush awaits under the asyncio `lock.write()` only, never under `db_lock`.
- Base `test_concurrent_enqueue_with_worker_loses_nothing`: 30 interleaved enqueues all reach `done`. Job ids come from `MAX(id)+1` under `db_lock`, and the claim is also a `db_lock` transaction, so no job is ever committed below the cursor. Nothing deletes journal rows.
- Base `test_job_payload_pages_are_returned_after_processing` and Task 4's vacuum tests: the batch's last finish is the first one that sees no open job, so the full vacuum still runs once the queue drains.
- Base `test_api.py::test_persistence_and_crash_replay`: the booted worker starts at cursor 0 and replays the `'processing'` row.
- F's per-job shard tests call `_process_job` directly, so the default `sync=True` still syncs per job.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`

Expected: `N0' + 22 passed, 1 warning`. No existing test changes.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_ingest_path.py
git commit -F - <<'EOF'
Batch the worker's index sync; finish jobs only after it

The worker runs ingest jobs with _process_job(sync=False) and gathers
them in an open batch (Collection._unsynced). The batch closes after
SYNC_BATCH_JOBS jobs, SYNC_BATCH_MS after its first job, or when the
claim finds the queue empty. _flush_unsynced then runs one _sync_index
under the write lock and only afterwards finishes each job, so no job
is 'done' before its rows are in the index file. A local claim cursor
skips the batch's own 'processing' jobs. It restarts at 0, so a crash
anywhere in a batch replays it. Attach and detach flush the open batch
first and run alone. A failed sync still ends the worker, and stop()
still leaves an open batch to replay: the next change adds the retry
and the stop() flush.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Review Focus for Task 8:** RF1 (a job is `done` before the index sync covering it, or a crash loses a job) is pinned here by `test_no_job_is_done_before_its_covering_sync` and by `test_crash_window_replays_without_loss` at `mid_batch`, `before_sync` and `after_sync_before_finish`. When reviewing, check that:
- `_finish_job` for an ingest job is reached only from `_flush_unsynced`, after `_sync_index` returns. An index job is finished directly in `_run_worker`, and only after the open batch's flush;
- every job popped from `_unsynced` is popped before its `await`, and nothing re-reads it afterwards. A cancel mid-finish leaves exactly that one job to the orphaned thread;
- the cursor is local to `_run_worker`, starts at 0, and moves to each claimed id, including undecodable rows and index jobs. No path claims without it;
- `_batch_t0` is set only when a job joins an empty batch, and reset only after the flush's sync. With `sync_batch_ms=0`, every batch closes at its first job;
- the idle flush runs before the `_wake.wait()`, so a batch never waits for the next enqueue, and the `_wake.clear()` still precedes the claim;
- each crash case's trigger is a state that only a batching worker reaches (`sync=False` on job 5, a sync with job 6 processed and job 5 unfinished, job 5's finish after job 6 ran). The snapshot is taken under `db_lock` and without the `-shm` file, so it matches what a crash leaves on disk on Windows and Linux alike;
- out of scope: a sync that raises still ends the worker (Task 9 adds the retry with backoff), `stop()` does not finish `_unsynced` (Task 9's `_sync_and_finish`), and the D2 benchmark (Task 10).

### Task 9: Sync failure retry and stop() flush

Goal: a failing index sync never finishes a job and never ends the worker, and `stop()` finishes the open batch (spec §3 D8: "Flush in `stop()`"; §4.1 `Collection.stop()` row).
- `_flush_unsynced` retries a sync that raises. It waits `SYNC_RETRY_MIN_S`, then twice as long after each failure, up to `SYNC_RETRY_MAX_S`, and logs each failure. The batch's jobs stay `'processing'` meanwhile, and the worker claims nothing new. The wait runs outside the write lock, so searches, deletes and `stop()` are not held up.
- `stop()` swaps its one `_sync_index` line for `_sync_and_finish`: the same sync, then the open batch's finishes, in the same write-locked section, after the worker cancel and before `_close_conns`. The other changes to `stop()` are an `except Exception` clause on its await of the worker (spec §3.1 D3, below) and a local `close_step` helper that its three close steps run through (spec §3.1 D4, below). Nothing in `stop()` moves. F's `_shard_pool.close` stays last, outside the lock.
- If `stop()`'s own sync raises an `Exception`, `_sync_and_finish` logs it once through `_log.exception`, finishes nothing and returns, so `stop()` still sets `_closed` and runs its three close steps, and the failed sync does not leave `stop()` (spec §3.1 D2). The batch's jobs stay `'processing'` with their payloads and replay on the next open (spec D8). `CancelledError` and `KeyboardInterrupt` are not `Exception`s, so they still propagate. At B, the exception left `stop()` before the connections closed.
- Two more errors no longer leave `stop()` (spec §3.1 D3). They go through `_log.exception` in the D2 path's style, with `(collection name, jobs in the batch)` as the args, and `stop()` still sets `_closed` and runs its three close steps:
  - a dead worker's exception, which B's `stop()` re-raised at `await self._worker`. `stop()` then flushes the open batch as usual. A job the worker held when it died (popped for a finish that raised) stays `'processing'` with its payload and replays, unless that finish left its transaction open (Step 3, "Why this shape");
  - a `_finish_job` that raises in `_sync_and_finish` after the sync landed. No later job is tried: the failing job and the rest of the batch stay `'processing'` with their payloads, and the replay re-applies their rows, which rewrites the same rows.
- Each close step logs its own `Exception` and moves on to the next (spec §3.1 D4). The steps are `_close_conns` (the SQLite connections, under `db_lock`, still inside `lock.write()`), the embedder's `aclose()` (its HTTP client) and F's `_shard_pool.close`, in that order, all after `_closed = True`. Each runs through `close_step`, a helper local to `stop()`, which logs through `_log.exception` with `(collection name, step)` as the args, so each failure is one ERROR record with its traceback. At B and F, a close step that raised left `stop()` and skipped every later step, and `shutdown()` then left every later collection and the catalog open.
- Only `Exception` is caught: D3 and D4 add no handler for `CancelledError` or any other `BaseException`.
- Task 7's removal-cap pre-sync inside `_process_job` runs after the job's commit and before its removals and adds. Until now a failure there raised out of the job, which then ended `'error'` with committed rows the index never got, and it never replayed. Task 9 logs that failure and lets the job go on; the batch's own sync, with its retry, writes everything.

**Files:**
- Modify `src/raggio/store.py` (base 19a8a2e line numbers, orientation only; anchor on the quoted text):
  - module constants `SYNC_RETRY_MIN_S = 1.0` and `SYNC_RETRY_MAX_S = 30.0`, directly below Task 7's `SYNC_MAX_REMOVALS = 512`;
  - `Collection._flush_unsynced` (Task 8's text, between `_run_worker`, base line 723, and `_process_job`, base line 742): rewritten with the retry loop. The new `Collection._sync_and_finish` goes directly below it;
  - `Collection.stop()` (base lines 655-668, as Plans B and F left it), replaced whole: the single line `await asyncio.to_thread(self._sync_index)` becomes `await asyncio.to_thread(self._sync_and_finish)`, the worker await's `except asyncio.CancelledError:` clause gets an `except Exception:` sibling that logs (spec §3.1 D3), and a local `close_step` helper at its head wraps the three close steps, `_close_conns`, the embedder's `aclose()` and F's `_shard_pool.close` (spec §3.1 D4);
  - `Collection._process_job` (base lines 742-803): Task 7's removal-cap pre-sync gets a `try` / `except Exception` that logs and goes on.
- Modify `tests/test_ingest_path.py`: one import-block edit (`logging`), then the section `# ---- batched sync: failure and shutdown` appended at the end (10 helpers, 12 tests).
- Test: `uv run pytest tests/test_ingest_path.py -v`.

**Interfaces:**
- Consumes:
  - Task 8: `Collection._unsynced: list[tuple[int, str, str | None]]`, `Collection._batch_t0: float | None`, and `async Collection._flush_unsynced(self) -> None`, whose sync runs under `lock.write()` and whose finishes pop each job before their await. Test helpers `fingerprint(col)`, `reference(tmp_path, payloads, **kw)` and `run_batched(col, payloads, timeout=30)`.
  - Task 7: `Collection(..., sync_batch_jobs: int = 8, sync_batch_ms: float = 1000.0)`, and `_process_job(self, payload: dict, *, sync: bool = True)`, which the worker calls with `sync=False`. `SYNC_MAX_REMOVALS` and the removal-cap pre-sync. `_sync_index` resets `_removed_since_sync` only after a sync that returns.
  - Task 4: `_finish_job(self, job_id, status, error)`, which takes `db_lock` itself and runs the vacuum policy.
  - Task 2: `_finish_job` deletes a job's `job_payloads` row only when the job finishes `done`, so an unfinished job keeps its payload for the replay.
  - Plan C: `_log = logging.getLogger("uvicorn.error")` in `store.py`.
  - Plan B: `stop()`'s write-locked section (cancel the worker, sync, `_closed = True`, `_close_conns()` under `db_lock`), and its tests `test_stop_waits_for_a_write_transaction_its_cancelled_worker_left_running` and `test_stop_closes_connections_inside_the_write_lock_and_under_db_lock`. `_close_conns()` closes `self.db` and every read connection in `_read_conns`, and leaves `_read_conns` empty.
  - Base: `Collection._embedder`, `None` until the first `embedder` access, and `Embedder.aclose()`, which closes its `httpx.AsyncClient`. `stop()` awaits it only when `_embedder` is not `None`, after the write-locked section.
  - Plan F: `stop()` ends with `await asyncio.to_thread(self._shard_pool.close)`, and `_ShardPool.close()` sets `_ShardPool._closed = True` under the pool's lock before it joins the pool's threads; `_IvfIndex.sync` leaves a shard whose write failed dirty, so a retry writes it again, and a clean index syncs nothing (`test_reload_then_stop_writes_no_shards`).
  - Plan B: `CollectionManager.shutdown()` evicts the resident collections in insertion order, awaiting each one's `stop()`, then closes the catalog under `_catalog_lock`. At B a `stop()` that raised ended that loop.
  - Task 7: the test module's `Settings` and `CollectionManager` imports, and the manager set-up of `test_manager_passes_sync_batch_knobs_to_collection` (`DATA_DIR` set to `tmp_path`, `CollectionManager(Settings(), embedder_factory=lambda cfg: None)`, `create_collection(name, 8, 4, None, None, None)`, `touch`).
  - Test helpers `make_collection`, `docs_payload`, `drain`, `fetch`, `index_ids`.
- Produces:
  - `SYNC_RETRY_MIN_S = 1.0` and `SYNC_RETRY_MAX_S = 30.0`: module-level algorithm constants in seconds (spec §4.2), read at call time, so tests monkeypatch them on `raggio.store`.
  - `async Collection._flush_unsynced(self) -> None`: retries a failed sync until one returns, then finishes the batch. It logs each failure through `_log.exception` with the args `(collection name, jobs in the batch, delay in seconds)`. Only `CancelledError` ends it early, and that leaves `_unsynced` intact.
  - `Collection._sync_and_finish(self) -> None`: a thread body, and the caller holds `lock.write()`. It calls `self._sync_index()`, then finishes each job of `_unsynced` in claim order through `self._finish_job`, popping it once its finish returns. If the sync raises an `Exception`, it logs it once through `_log.exception` with the args `(collection name, jobs in the batch)`, finishes nothing, leaves `_unsynced` intact and returns normally (spec §3.1 D2). If a finish raises an `Exception` after the sync landed, it logs it once the same way, with the args `(collection name, jobs left in _unsynced)`, tries no later job and returns normally (spec §3.1 D3). Anything that is not an `Exception` propagates.
  - `Collection.stop()`: syncs and finishes the open batch before `_closed = True` and `_close_conns()`. If that sync fails, `stop()` still sets `_closed` and runs its three close steps, and the failure does not leave `stop()`. The batch's jobs stay `'processing'` with their payloads and replay on the next open (spec §3.1 D2). A worker that died of an `Exception` is logged through `_log.exception` with the args `(collection name, jobs in the open batch)`, and the flush and the close then run as usual (spec §3.1 D3). Each close step, `_close_conns`, the embedder's `aclose()` and F's `_shard_pool.close`, runs through the local `close_step`: an `Exception` it raises is logged through `_log.exception` with the args `(collection name, step)`, where step is `"the SQLite connections"`, `"the embedder's HTTP client"` or `"the IVF shard pool"`, and the next step runs (spec §3.1 D4). So no `Exception` from the worker, the final sync, a finish or a close step leaves `stop()`; `CancelledError` and other non-`Exception` `BaseException`s propagate.
  - `Collection._process_job`: a removal-cap pre-sync that raises is logged, and the job goes on to its removals and adds. The signature is unchanged.

- [ ] **Step 1: Write the failing tests**

(a) In `tests/test_ingest_path.py`, the module head (with Task 8's `shutil`) has:

```python
import json
import shutil
```

Replace it with:

```python
import json
import logging
import shutil
```

(b) Append this section at the end of the file, below Task 8's `# ---- batched sync: worker` section. The shared helpers come first; the four helpers of the failed `stop()` sync sit with their two tests after the removal-cap test, then `flaky_finish` with two spec §3.1 D3 tests, then `EmbedderStub` with the three spec §3.1 D4 tests, and last the shutdown test, which covers D3 and D4.

The failure tests read the retry delays from the log records' arguments, not from a patched `asyncio.sleep`. Patching `asyncio.sleep` would hit every caller in the process, `drain()` included, and the sleep itself is too short to time: on Windows the event loop's clock resolution is about 15 ms. The logger is `uvicorn.error` (Plan C's `_log`). The handler sits on that logger itself, so the test does not depend on propagation, which uvicorn's logging config turns off at `"uvicorn"` once any test in the session has built a `uvicorn.Config`.

```python
# ---- batched sync: failure and shutdown


class LogRecords(logging.Handler):
    """Keeps every ERROR record logged on the logger it is added to. It sits on that
    logger itself, so it sees them whatever propagation the logging config set."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def failing_sync(col, failures):
    """Make col._sync_index raise OSError on its first `failures` calls; later calls
    run the real sync. Each failing call first records the jobs table as it stands
    while the batch waits. Returns (calls, states): one entry per call, and one
    table per failed call."""
    calls, states, real = [], [], col._sync_index

    def sync():
        calls.append(len(calls) + 1)
        if len(calls) <= failures:
            states.append(fetch(col, "SELECT id, status FROM jobs ORDER BY id"))
            raise OSError(f"disk full (sync {len(calls)})")
        real()

    col._sync_index = sync
    return calls, states


async def wait_for_retries(col, logs, n, timeout=10):
    """Wait until the worker has logged n failed syncs. If the worker ended instead,
    re-raise what ended it."""
    deadline = time.monotonic() + timeout
    while len(logs.records) < n:
        if col._worker is not None and col._worker.done():
            col._worker.result()  # re-raises the worker's exception, if any
            pytest.fail(f"the worker exited after {len(logs.records)} logged failures")
        assert time.monotonic() < deadline, f"{len(logs.records)} failures logged after {timeout} s"
        await asyncio.sleep(0.01)


def job_states(directory):
    """The jobs table as a stopped collection left it on disk."""
    db = sqlite3.connect(Path(directory) / "meta.db")
    try:
        return db.execute("SELECT id, status FROM jobs ORDER BY id").fetchall()
    finally:
        db.close()
```

Then, in the same section, the two failure tests:

```python
def test_sync_failure_keeps_batch_processing_and_retries(tmp_path, monkeypatch):
    # before: a sync that raised in the flush ended the worker, and its batch sat in
    # 'processing' until the next open. Now the batch waits in 'processing' while the
    # worker retries with a doubling, capped delay and claims nothing new, then lands
    monkeypatch.setattr(store, "SYNC_RETRY_MIN_S", 0.01, raising=False)
    monkeypatch.setattr(store, "SYNC_RETRY_MAX_S", 0.04, raising=False)
    col = make_collection(tmp_path, sync_batch_jobs=3, sync_batch_ms=60_000)
    calls, states = failing_sync(col, 4)
    logs = LogRecords()
    store._log.addHandler(logs)

    async def go():
        try:
            await run_batched(col, [docs_payload([j]) for j in range(1, 5)])
            return (len(calls), fetch(col, "SELECT id, status FROM jobs ORDER BY id"),
                    index_ids(col))
        finally:
            await col.stop()

    try:
        n_calls, jobs, ids = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    # the batch [1, 2, 3] closes at the count cap: four failed syncs, then the one that
    # lands. Job 4 is claimed only after that, and its idle flush is the sixth sync
    assert n_calls == 6
    waiting = [(1, "processing"), (2, "processing"), (3, "processing"), (4, "pending")]
    assert states == [waiting] * 4
    assert len(logs.records) == 4
    for r in logs.records:
        assert r.exc_info[0] is OSError and "index sync failed" in r.getMessage()
    assert [r.args[1] for r in logs.records] == [3] * 4  # the jobs left 'processing'
    assert [r.args[2] for r in logs.records] == pytest.approx([0.01, 0.02, 0.04, 0.04])
    assert jobs == [(j, "done") for j in range(1, 5)]
    assert ids == [1, 2, 3, 4]


def test_stop_during_sync_backoff_finishes_the_batch(tmp_path, monkeypatch):
    # the retry waits outside the write lock: stop() takes the lock at once, cancels
    # the wait, and syncs and finishes the batch itself instead of sitting out 30 s
    monkeypatch.setattr(store, "SYNC_RETRY_MIN_S", 30.0, raising=False)
    monkeypatch.setattr(store, "SYNC_RETRY_MAX_S", 30.0, raising=False)
    col = make_collection(tmp_path, sync_batch_jobs=3, sync_batch_ms=60_000)
    calls, states = failing_sync(col, 1)
    logs = LogRecords()
    store._log.addHandler(logs)

    async def go():
        for j in (1, 2, 3):
            await asyncio.to_thread(col._enqueue_row, docs_payload([j]))
        col.start_worker()
        await wait_for_retries(col, logs, 1)  # the worker now waits 30 s to retry
        t0 = time.monotonic()
        await col.stop()
        return time.monotonic() - t0

    try:
        took = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    assert took < 5.0
    assert states == [[(1, "processing"), (2, "processing"), (3, "processing")]]
    assert len(calls) == 2  # the failed flush, then stop()'s own sync
    assert job_states(tmp_path) == [(1, "done"), (2, "done"), (3, "done")]
    reopened = make_collection(tmp_path)
    try:
        assert index_ids(reopened) == [1, 2, 3]  # stop()'s sync wrote the batch's rows
        assert reopened.pending_jobs() == 0  # nothing is left to replay
    finally:
        asyncio.run(reopened.stop())
```

Then the `stop()` flush:

```python
def test_stop_flushes_the_open_batch(tmp_path):
    # RF4: stop() finishes the open batch inside B's write-locked section, after the
    # worker's cancel and before the connections close. Jobs 1-3 are processed and
    # wait for their batch's sync; job 4 is claimed and parked before its write section
    live = tmp_path / "live"
    live.mkdir()
    payloads = [docs_payload([j]) for j in range(1, 5)]
    col = make_collection(live, sync_batch_jobs=100, sync_batch_ms=60_000)
    real_process, real_finish = col._process_job, col._finish_job
    finishes = []

    def finish(job_id, status, error):
        finishes.append((job_id, col.lock._writing, col._closed))
        real_finish(job_id, status, error)

    col._finish_job = finish

    async def go():
        parked, gate = asyncio.Event(), asyncio.Event()

        async def process(payload, **kw):
            if payload["documents"][0]["doc_id"] == "d4":
                parked.set()
                await gate.wait()  # never set: stop() cancels the worker here
            await real_process(payload, **kw)

        col._process_job = process
        for p in payloads:
            await asyncio.to_thread(col._enqueue_row, p)
        col.start_worker()
        try:
            await asyncio.wait_for(parked.wait(), 10)
            return [job[0] for job in col._unsynced]
        finally:
            await col.stop()

    assert asyncio.run(go()) == [1, 2, 3]
    # before: jobs 1-3 stayed 'processing', and the next open ran them again
    assert job_states(live) == [(1, "done"), (2, "done"), (3, "done"), (4, "processing")]
    # each finish ran write-locked and before _closed (so before _close_conns)
    assert finishes == [(1, True, False), (2, True, False), (3, True, False)]

    again = make_collection(live, sync_batch_jobs=100, sync_batch_ms=60_000)

    async def replay():
        try:
            before = index_ids(again)  # what stop()'s sync wrote
            again.start_worker()
            await drain(again)
            return (before, fetch(again, "SELECT id, status FROM jobs ORDER BY id"),
                    fingerprint(again))
        finally:
            await again.stop()

    before, jobs, got = asyncio.run(replay())
    assert before == [1, 2, 3]
    assert jobs == [(j, "done") for j in range(1, 5)]  # job 4 replayed
    assert got == reference(tmp_path, payloads)
```

And the removal-cap pre-sync, the one sync that runs inside a job:

```python
def test_failed_removal_cap_presync_does_not_fail_the_job(tmp_path, monkeypatch):
    # the cap's pre-sync runs after the job's commit and before its removals and adds.
    # Raising out of the job left it 'error' (never replayed) with committed rows the
    # index lacked and the replaced ids still in it. Now the job goes on, and the
    # batch's own sync writes everything
    monkeypatch.setattr(store, "SYNC_MAX_REMOVALS", 1)
    col = make_collection(tmp_path, sync_batch_jobs=10, sync_batch_ms=60_000)
    calls, _ = failing_sync(col, 1)
    logs = LogRecords()
    store._log.addHandler(logs)

    async def go():
        try:
            # job 2 replaces c1 (count 0, no pre-sync; then 1). Job 3 replaces c2:
            # 1 + 1 > 1, so it pre-syncs, and that first sync fails
            await run_batched(col, [docs_payload([1, 2]), docs_payload([1]), docs_payload([2])])
            return (len(calls), fetch(col, "SELECT id, status, error FROM jobs ORDER BY id"),
                    index_ids(col), fetch(col, "SELECT id FROM records ORDER BY id"))
        finally:
            await col.stop()

    try:
        n_calls, jobs, ids, records = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    assert n_calls == 2  # the failed pre-sync, then the batch's idle flush
    assert jobs == [(1, "done", None), (2, "done", None), (3, "done", None)]
    assert ids == [r[0] for r in records] and len(ids) == 2  # no ghost, none missing
    assert len(logs.records) == 1 and logs.records[0].exc_info[0] is OSError
```

Then `stop()` with a failing sync of its own (spec §3.1 D2). The helpers come first. `stop_with_open_batch` builds the open batch as `test_stop_flushes_the_open_batch` does, and calls its `fail` argument only once job 4 is parked, so the failure it installs hits `stop()` alone. `stop_with_failing_sync` passes `failing_sync`, so the one sync that fails is `stop()`'s own: jobs 1-3 ran with `sync=False` and replaced no row, so nothing synced before. The spec §3.1 D3 finish test below reuses `stop_with_open_batch` with a failing finish, and the three spec §3.1 D4 tests with a failing close step. On a failed run the exception leaves the helper, and the collection stays open, which is harmless there.

```python
def closed_state(col):
    """What a completed stop() leaves: the closed flag, whether the writer connection
    still answers, how many read connections stay registered, and whether Plan F's
    shard pool is closed (True on a tree without Plan F, which has no pool)."""
    try:
        col.db.execute("SELECT 1")
        writer_open = True
    except sqlite3.ProgrammingError:  # Cannot operate on a closed database.
        writer_open = False
    pool = getattr(col, "_shard_pool", None)
    return col._closed, writer_open, len(col._read_conns), pool is None or pool._closed


def disk_rows(directory, sql):
    """Rows of a stopped collection's meta.db, read on a fresh connection."""
    db = sqlite3.connect(Path(directory) / "meta.db")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def stop_with_open_batch(directory, fail):
    """Jobs 1-3 wait in the open batch for their sync, and job 4 is claimed and parked
    before its write section, as in test_stop_flushes_the_open_batch. Then fail(col)
    installs a failure and returns its call list, and stop() runs. Returns those calls,
    the open batch's job ids as stop() began, the ERROR records logged and the stopped
    collection. A stop() that raises leaves this function."""
    col = make_collection(directory, sync_batch_jobs=100, sync_batch_ms=60_000)
    real_process = col._process_job
    logs = LogRecords()

    async def go():
        parked, gate = asyncio.Event(), asyncio.Event()

        async def process(payload, **kw):
            if payload["documents"][0]["doc_id"] == "d4":
                parked.set()
                await gate.wait()  # never set: stop() cancels the worker here
            await real_process(payload, **kw)

        col._process_job = process
        for j in range(1, 5):
            await asyncio.to_thread(col._enqueue_row, docs_payload([j]))
        col.start_worker()
        await asyncio.wait_for(parked.wait(), 10)
        batch = [job[0] for job in col._unsynced]
        calls = fail(col)  # from here on only stop() syncs and finishes
        await col.stop()
        return calls, batch

    store._log.addHandler(logs)
    try:
        calls, batch = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    return calls, batch, logs.records, col


def stop_with_failing_sync(directory):
    """stop_with_open_batch where stop()'s own index sync (the first since the open)
    fails. The calls returned are the sync calls."""
    return stop_with_open_batch(directory, lambda col: failing_sync(col, 1)[0])
```

Then the two tests, one for the shutdown and one for the replay:

```python
def test_stop_with_a_failing_sync_logs_it_and_still_closes(tmp_path):
    # spec 3.1 D2: when stop()'s own index sync fails, stop() logs it once, completes
    # the shutdown and raises nothing. It finishes no job: the open batch keeps
    # 'processing' and its payloads, so the next open replays it
    live, normal = tmp_path / "live", tmp_path / "normal"
    live.mkdir()
    normal.mkdir()
    calls, batch, records, col = stop_with_failing_sync(live)  # before: OSError here
    stopped = make_collection(normal)
    asyncio.run(stopped.stop())  # a normal stop, to compare with
    assert batch == [1, 2, 3]
    assert calls == [1]  # stop()'s one sync, and it failed
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.ERROR and "index sync failed in stop()" in rec.getMessage()
    assert rec.exc_info[0] is OSError and rec.exc_info[2] is not None  # with its traceback
    assert rec.args == ("t", 3)  # the collection, and the batch's jobs left 'processing'
    # closed as a normal stop() closes: _closed set, the writer connection and every
    # read connection closed, Plan F's shard pool closed
    assert closed_state(col) == closed_state(stopped) == (True, False, 0, True)
    assert job_states(live) == [(j, "processing") for j in range(1, 5)]
    assert disk_rows(live, "SELECT job_id FROM job_payloads ORDER BY job_id") == [
        (1,), (2,), (3,), (4,)]


def test_a_failed_stop_sync_replays_on_the_next_open(tmp_path):
    # the jobs a failed stop() sync left 'processing' replay on the next open (D8) and
    # end 'done', with their rows in the index: none lost, no ghost
    live = tmp_path / "live"
    live.mkdir()
    stop_with_failing_sync(live)  # before: OSError here
    again = make_collection(live, sync_batch_jobs=100, sync_batch_ms=60_000)

    async def replay():
        try:
            before = index_ids(again)  # the failed sync wrote none of the batch's rows
            again.start_worker()
            await drain(again)
            return (before, fetch(again, "SELECT id, status FROM jobs ORDER BY id"),
                    fetch(again, "SELECT COUNT(*) FROM job_payloads"), index_ids(again),
                    fetch(again, "SELECT id FROM records WHERE indexed=1 ORDER BY id"),
                    fingerprint(again))
        finally:
            await again.stop()

    before, jobs, payload_rows, ids, indexed, got = asyncio.run(replay())
    assert before == []
    assert jobs == [(j, "done") for j in range(1, 5)]
    assert payload_rows == [(0,)]
    assert ids == [r[0] for r in indexed] and len(ids) == 4  # no ghost, none missing
    assert got == reference(tmp_path, [docs_payload([j]) for j in range(1, 5)])
```

Then spec §3.1 D3: the two other errors `stop()` now logs instead of raising. `flaky_finish` comes first. It raises before the real finish runs, so the job stays `'processing'` with its payload, as when a finish's transaction fails and rolls back. The dead-worker test lets the worker's own flush fail its first finish, which ends the worker as at base; `asyncio.wait` waits for that without re-raising. `closed_state`, `job_states` and `disk_rows` read the stopped collection as in the D2 tests, and the replay follows `test_a_failed_stop_sync_replays_on_the_next_open`.

```python
def flaky_finish(col, fail_on):
    """Make col._finish_job raise sqlite3.OperationalError (meta.db full) on the calls
    numbered in fail_on, counting from 1, before the real finish runs; every other call
    runs it. Returns the calls, one job id per call."""
    calls, real = [], col._finish_job

    def finish(job_id, status, error):
        calls.append(job_id)
        if len(calls) in fail_on:
            raise sqlite3.OperationalError(f"database or disk is full (finish {len(calls)})")
        real(job_id, status, error)

    col._finish_job = finish
    return calls


def test_stop_logs_a_dead_workers_exception_and_still_closes(tmp_path):
    # spec 3.1 D3: the batch [1, 2, 3] syncs, then the worker's finish of job 1 raises,
    # which ends the worker (as at base). stop() logs that exception once instead of
    # re-raising it, still flushes the rest of the batch and closes as usual. Job 1
    # stays 'processing' with its payload, and the next open replays it
    live = tmp_path / "live"
    live.mkdir()
    payloads = [docs_payload([j]) for j in range(1, 4)]
    col = make_collection(live, sync_batch_jobs=3, sync_batch_ms=60_000)
    calls = flaky_finish(col, {1})
    logs = LogRecords()

    async def go():
        for p in payloads:
            await asyncio.to_thread(col._enqueue_row, p)
        col.start_worker()
        done, _ = await asyncio.wait({col._worker}, timeout=10)  # never re-raises
        assert done, "the worker is still running after 10 s"
        left = [job[0] for job in col._unsynced]
        await col.stop()  # before: sqlite3.OperationalError here
        return left

    store._log.addHandler(logs)
    try:
        left = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    assert left == [2, 3]  # job 1 was popped for its finish, which raised
    assert calls == [1, 2, 3]  # the worker's failed finish, then stop()'s two
    assert len(logs.records) == 1
    rec = logs.records[0]
    assert rec.levelno == logging.ERROR and "ingest worker had died" in rec.getMessage()
    assert rec.exc_info[0] is sqlite3.OperationalError and rec.exc_info[2] is not None
    assert rec.args == ("t", 2)  # the collection, and the open batch stop() flushes
    assert closed_state(col) == (True, False, 0, True)  # what a normal stop() leaves
    assert job_states(live) == [(1, "processing"), (2, "done"), (3, "done")]
    assert disk_rows(live, "SELECT job_id FROM job_payloads ORDER BY job_id") == [(1,)]
    again = make_collection(live)

    async def replay():
        try:
            before = index_ids(again)  # the worker's sync wrote the whole batch
            again.start_worker()
            await drain(again)
            return (before, fetch(again, "SELECT id, status FROM jobs ORDER BY id"),
                    fetch(again, "SELECT COUNT(*) FROM job_payloads"), index_ids(again),
                    fetch(again, "SELECT id FROM records WHERE indexed=1 ORDER BY id"),
                    fingerprint(again))
        finally:
            await again.stop()

    before, jobs, payload_rows, ids, indexed, got = asyncio.run(replay())
    assert before == [1, 2, 3]
    assert jobs == [(j, "done") for j in range(1, 4)]  # job 1 replayed
    assert payload_rows == [(0,)]
    assert ids == [r[0] for r in indexed] and len(ids) == 3  # no ghost, none missing
    assert got == reference(tmp_path, payloads)
```

Then the finish that fails after `stop()`'s own sync landed, through `stop_with_open_batch`:

```python
def test_a_failed_finish_in_stop_is_logged_and_replays(tmp_path):
    # spec 3.1 D3: stop()'s own sync lands, then its finish of job 1 raises (meta.db
    # full). stop() logs it once, tries no later finish, closes as usual and raises
    # nothing. Jobs 1-3 keep 'processing' and their payloads although the index file
    # holds their rows, so the next open re-applies them: the same rows, no ghost
    live = tmp_path / "live"
    live.mkdir()
    calls, batch, records, col = stop_with_open_batch(live, lambda c: flaky_finish(c, {1}))
    assert batch == [1, 2, 3]
    assert calls == [1]  # job 1's finish raised, and no later job was tried
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.ERROR and "finishing jobs failed in stop()" in rec.getMessage()
    assert rec.exc_info[0] is sqlite3.OperationalError and rec.exc_info[2] is not None
    assert rec.args == ("t", 3)  # the collection, and the batch's jobs left 'processing'
    assert closed_state(col) == (True, False, 0, True)  # what a normal stop() leaves
    assert job_states(live) == [(j, "processing") for j in range(1, 5)]
    assert disk_rows(live, "SELECT job_id FROM job_payloads ORDER BY job_id") == [
        (1,), (2,), (3,), (4,)]
    again = make_collection(live, sync_batch_jobs=100, sync_batch_ms=60_000)

    async def replay():
        try:
            before = index_ids(again)  # stop()'s sync wrote the batch's rows
            again.start_worker()
            await drain(again)
            return (before, fetch(again, "SELECT id, status FROM jobs ORDER BY id"),
                    fetch(again, "SELECT COUNT(*) FROM job_payloads"), index_ids(again),
                    fetch(again, "SELECT id FROM records WHERE indexed=1 ORDER BY id"),
                    fingerprint(again))
        finally:
            await again.stop()

    before, jobs, payload_rows, ids, indexed, got = asyncio.run(replay())
    assert before == [1, 2, 3]
    assert jobs == [(j, "done") for j in range(1, 5)]  # 1-3 re-applied, 4 replayed
    assert payload_rows == [(0,)]
    assert ids == [r[0] for r in indexed] and len(ids) == 4  # no ghost, none missing
    assert got == reference(tmp_path, [docs_payload([j]) for j in range(1, 5)])
```

Then spec §3.1 D4: each close step logs its own failure and the next one runs. `EmbedderStub` comes first. `make_collection` has no embedder (`_embedder` stays `None`, so `stop()` skips that step), and a test that needs the step puts a stub there with `monkeypatch`. Each test fails one step through `stop_with_open_batch`, so `stop()` also flushes the open batch first: jobs 1-3 end `done` and job 4 stays `'processing'`, as in `test_stop_flushes_the_open_batch`. `closed_state` and `job_states` read what the close left.

```python
class EmbedderStub:
    """Stands in for a collection's embedder in stop(), which only calls aclose().
    Records each call, one number per call; with fail=True the call then raises
    OSError, as an HTTP client whose transport fails to close does."""

    def __init__(self, fail):
        self.fail, self.calls = fail, []

    async def aclose(self):
        self.calls.append(len(self.calls) + 1)
        if self.fail:
            raise OSError(f"embedder transport close failed (close {len(self.calls)})")


def test_a_failing_embedder_close_is_logged_and_stop_goes_on(tmp_path, monkeypatch):
    # spec 3.1 D4: the embedder's HTTP client raises as it closes. stop() logs it once
    # and still closes F's shard pool after it; the flush and the connections before
    # it ran as in a normal stop()
    live = tmp_path / "live"
    live.mkdir()
    client = EmbedderStub(fail=True)

    def fail(col):
        monkeypatch.setattr(col, "_embedder", client)
        return client.calls

    calls, batch, records, col = stop_with_open_batch(live, fail)  # before: OSError here
    assert batch == [1, 2, 3]
    assert calls == [1]  # stop() closed the client once, and that raised
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.ERROR
    assert rec.getMessage() == (
        "collection t: closing the embedder's HTTP client failed in stop(); the shutdown goes on")
    assert rec.exc_info[0] is OSError and rec.exc_info[2] is not None  # with its traceback
    assert rec.args == ("t", "the embedder's HTTP client")
    assert closed_state(col) == (True, False, 0, True)  # connections and shard pool closed
    assert job_states(live) == [(1, "done"), (2, "done"), (3, "done"), (4, "processing")]
```

Then the connections, which fail before they close. The test closes them itself afterwards, with B's real `_close_conns`, so the files are released:

```python
def test_a_failing_close_conns_is_logged_and_the_later_steps_run(tmp_path, monkeypatch):
    # spec 3.1 D4: closing the SQLite connections raises. stop() logs it once, has set
    # _closed before it, and still closes the embedder's client and F's shard pool
    live = tmp_path / "live"
    live.mkdir()
    client = EmbedderStub(fail=False)

    def fail(col):
        calls = []

        def close_conns():
            calls.append(col.lock._writing)
            raise sqlite3.OperationalError(
                "unable to close due to unfinalized statements or unfinished backups")

        monkeypatch.setattr(col, "_close_conns", close_conns)
        monkeypatch.setattr(col, "_embedder", client)
        return calls

    calls, batch, records, col = stop_with_open_batch(live, fail)  # before: OperationalError here
    try:
        assert batch == [1, 2, 3]
        assert calls == [True]  # called once, inside stop()'s write lock as at B
        assert len(records) == 1
        rec = records[0]
        assert rec.levelno == logging.ERROR and "failed in stop()" in rec.getMessage()
        assert rec.exc_info[0] is sqlite3.OperationalError and rec.exc_info[2] is not None
        assert rec.args == ("t", "the SQLite connections")
        assert client.calls == [1]  # the next step ran: the embedder's client closed
        closed, writer_open, _, pool_closed = closed_state(col)
        assert (closed, writer_open, pool_closed) == (True, True, True)  # meta.db still open
    finally:
        Collection._close_conns(col)  # B's real close
    assert closed_state(col) == (True, False, 0, True)
    assert job_states(live) == [(1, "done"), (2, "done"), (3, "done"), (4, "processing")]
```

Then F's shard pool, the last step. The test needs Plan F's pool, so it skips without it, as Task 8's IVF test does:

```python
@pytest.mark.skipif(not hasattr(store, "_ShardPool"),
                    reason="needs Plan F's shard pool")
def test_a_failing_shard_pool_close_is_logged(tmp_path, monkeypatch):
    # spec 3.1 D4: F's shard pool, the last close step, raises as it closes. stop()
    # logs it once and returns; every earlier step ran as in a normal stop()
    live = tmp_path / "live"
    live.mkdir()
    client = EmbedderStub(fail=False)

    def fail(col):
        calls = []

        def close():
            calls.append(col._closed)
            raise RuntimeError("cannot join current thread")

        monkeypatch.setattr(col._shard_pool, "close", close)
        monkeypatch.setattr(col, "_embedder", client)
        return calls

    calls, batch, records, col = stop_with_open_batch(live, fail)  # before: RuntimeError here
    assert batch == [1, 2, 3]
    assert calls == [True]  # called once, after _closed, as F requires
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.ERROR and "failed in stop()" in rec.getMessage()
    assert rec.exc_info[0] is RuntimeError and rec.exc_info[2] is not None
    assert rec.args == ("t", "the IVF shard pool")
    assert client.calls == [1]  # the steps before it ran
    # the pool stays open (its close raised); a flat collection's pool starts no thread
    assert closed_state(col) == (True, False, 0, False)
    assert job_states(live) == [(1, "done"), (2, "done"), (3, "done"), (4, "processing")]
```

Last, the shutdown that depends on D3 and D4, through Task 7's manager set-up. Collection a's worker dies, and a's embedder client fails to close:

```python
def test_shutdown_closes_every_collection_after_a_failed_stop(tmp_path, monkeypatch):
    # spec 3.1 D3 and D4: no Exception leaves stop(), so shutdown() goes on past a
    # collection whose worker died and whose embedder client fails to close, and it
    # closes the next one and the catalog. Before, a's stop() re-raised the worker's
    # exception, and b and the catalog stayed open
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    logs = LogRecords()
    client = EmbedderStub(fail=True)

    async def go():
        mgr = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        await mgr.create_collection("a", 8, 4, None, None, None)
        await mgr.create_collection("b", 8, 4, None, None, None)
        a, b = await mgr.touch("a"), await mgr.touch("b")  # resident, and shut, in this order
        flaky_finish(a, {1})
        monkeypatch.setattr(a, "_embedder", client)  # a's second close step raises
        await a.enqueue(docs_payload([1]))  # its flush syncs, then the finish raises
        done, _ = await asyncio.wait({a._worker}, timeout=10)
        assert done, "a's worker is still running after 10 s"
        await mgr.shutdown()  # before: sqlite3.OperationalError here
        return mgr, a, b

    store._log.addHandler(logs)
    try:
        mgr, a, b = asyncio.run(go())
    finally:
        store._log.removeHandler(logs)
    assert closed_state(a) == closed_state(b) == (True, False, 0, True)
    assert mgr.resident == {}
    with pytest.raises(sqlite3.ProgrammingError):  # the catalog is closed too
        mgr.catalog.execute("SELECT 1")
    assert client.calls == [1]
    assert len(logs.records) == 2  # a's dead worker, then a's embedder; b stopped cleanly
    died, closing = logs.records
    assert died.exc_info[0] is sqlite3.OperationalError and "worker had died" in died.getMessage()
    assert died.args == ("a", 0)  # the worker had popped job 1, the batch's only job
    assert closing.exc_info[0] is OSError and "failed in stop()" in closing.getMessage()
    assert closing.args == ("a", "the embedder's HTTP client")  # then a's pool closed
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `12 failed, 50 passed` (`12 failed, 49 passed` if Task 6's gate test never landed). On a tree without Plan F, `test_a_failing_shard_pool_close_is_logged` and Task 8's `test_batched_ivf_sync_writes_each_dirty_shard_once` are skipped instead: `11 failed, 49 passed, 2 skipped` (`11 failed, 48 passed, 2 skipped` without the gate test too). The retry constants are patched with `raising=False`, so no failure is an `AttributeError`:
- `test_sync_failure_keeps_batch_processing_and_retries`: `OSError: disk full (sync 1)`. Task 8's flush lets the first failed sync end the worker. `drain()` re-raises it, and the `finally`'s `stop()` raises it once more when it awaits the dead worker, since Plan B's `stop()` only swallows `CancelledError`. (That exception comes from the dead worker, not from `stop()`'s own sync. After this task the retry keeps the worker alive, and a `stop()` that did find a dead worker would log its exception instead, spec §3.1 D3.)
- `test_stop_during_sync_backoff_finishes_the_batch`: `OSError: disk full (sync 1)`, re-raised by `wait_for_retries`. The test never reaches `stop()`, and leaving that collection open is harmless in a failing run.
- `test_stop_flushes_the_open_batch`: `AssertionError: assert [(1, 'processing'), (2, 'processing'), (3, 'processing'), (4, 'processing')] == [(1, 'done'), (2, 'done'), (3, 'done'), (4, 'processing')]`. Task 8's `stop()` syncs but finishes nothing. The first assert passes: Task 8 already keeps jobs 1-3 in `_unsynced`.
- `test_failed_removal_cap_presync_does_not_fail_the_job`: `AssertionError: assert [(1, 'done', None), (2, 'done', None), (3, 'error', 'disk full (sync 1)')] == [(1, 'done', None), (2, 'done', None), (3, 'done', None)]`. The first assert passes: the failed pre-sync and the idle flush are the two syncs either way.
- `test_stop_with_a_failing_sync_logs_it_and_still_closes` and `test_a_failed_stop_sync_replays_on_the_next_open`: `OSError: disk full (sync 1)`, raised out of `stop()` inside `stop_with_failing_sync` by Task 8's plain `_sync_index` line, which Plan B's `stop()` does not catch. That collection stays open, which is harmless in a failing run.
- `test_stop_logs_a_dead_workers_exception_and_still_closes`: `sqlite3.OperationalError: database or disk is full (finish 1)`, re-raised by `await col.stop()` when Plan B's `stop()` awaits the dead worker. The asserts before it pass: the worker's flush synced and popped job 1 as at Task 8.
- `test_a_failed_finish_in_stop_is_logged_and_replays`: `AssertionError: assert [] == [1]`. Task 8's `stop()` syncs but finishes nothing, so `flaky_finish` sees no call. The first assert passes.
- `test_a_failing_embedder_close_is_logged_and_stop_goes_on`: `OSError: embedder transport close failed (close 1)`, raised out of `stop()` inside `stop_with_open_batch` by Plan B's plain `await self._embedder.aclose()`. F's shard pool stays open, which is harmless in a failing run.
- `test_a_failing_close_conns_is_logged_and_the_later_steps_run`: `sqlite3.OperationalError: unable to close due to unfinalized statements or unfinished backups`, raised out of `stop()` inside `stop_with_open_batch` by Plan B's plain `await asyncio.to_thread(self._close_conns)`. The test's `try` is never reached, so the connections stay open, which is harmless in a failing run.
- `test_a_failing_shard_pool_close_is_logged`: `RuntimeError: cannot join current thread`, raised out of `stop()` inside `stop_with_open_batch` by Plan F's plain `await asyncio.to_thread(self._shard_pool.close)`.
- `test_shutdown_closes_every_collection_after_a_failed_stop`: `sqlite3.OperationalError: database or disk is full (finish 1)`, raised out of `mgr.shutdown()` by a's `stop()` when it awaits the dead worker, before it reaches a's embedder. b and the catalog stay open, which is harmless in a failing run.

- [ ] **Step 3: Implement the retry and the stop() flush**

All four edits, (a) to (d), are in `src/raggio/store.py`. `_log` and `logging` are already there (Plan C, Task 1).

(a) Module constants. Task 7 left this line at the end of its comment block, directly below Task 4's `VACUUM_CHUNK_PAGES = 2_048`:

```python
SYNC_MAX_REMOVALS = 512
```

Replace it with:

```python
SYNC_MAX_REMOVALS = 512
# a failed index sync (a full disk, an I/O error) is retried after SYNC_RETRY_MIN_S s,
# then twice as long after each failure, up to SYNC_RETRY_MAX_S s. The batch stays
# 'processing' meanwhile, so if the process ends first the next open replays it
SYNC_RETRY_MIN_S = 1.0
SYNC_RETRY_MAX_S = 30.0
```

(b) `Collection._flush_unsynced`. The whole method as Task 8 wrote it (directly below `_run_worker`):

```python
    async def _flush_unsynced(self) -> None:
        """Close the worker's open batch: one index sync covers every job in it, then
        each job finishes, in claim order. None of them is 'done' before that sync
        returns (the job journal invariant), so a crash before it leaves them all
        'processing' and the replay redoes them. A crash between the sync and the last
        finish replays the unfinished rest, which rewrites the same rows."""
        async with self.lock.write():  # the per-job sync ran under it too
            await asyncio.to_thread(self._sync_index)
        self._batch_t0 = None  # the batch closed at its sync
        while self._unsynced:
            # pop BEFORE the await: a stop() that cancels the worker mid-finish leaves
            # this job to the orphaned thread, and nothing finishes it twice
            job = self._unsynced.pop(0)
            await asyncio.to_thread(self._finish_job, *job)
```

Replace it with the retrying version plus the new `_sync_and_finish` below it:

```python
    async def _flush_unsynced(self) -> None:
        """Close the worker's open batch: one index sync covers every job in it, then
        each job finishes, in claim order. None of them is 'done' before that sync
        returns (the job journal invariant), so a crash before it leaves them all
        'processing' and the replay redoes them. A crash between the sync and the last
        finish replays the unfinished rest, which rewrites the same rows.

        A sync that raises is logged and retried, after SYNC_RETRY_MIN_S and then twice
        as long each time, up to SYNC_RETRY_MAX_S. Until one lands the batch stays
        'processing' and the worker claims nothing new. The wait runs outside the write
        lock, so searches, deletes and stop() go on; stop() cancels the wait and
        finishes the batch itself (_sync_and_finish)."""
        delay = SYNC_RETRY_MIN_S
        while True:
            try:
                async with self.lock.write():  # the per-job sync ran under it too
                    await asyncio.to_thread(self._sync_index)
                break
            except Exception:
                # nothing is finished and the index keeps its in-memory rows; an IVF
                # shard whose write failed stays dirty (Plan F), so the retry writes it
                _log.exception(
                    "collection %s: index sync failed, %d jobs stay 'processing';"
                    " retrying in %.2f s",
                    self.cfg.name, len(self._unsynced), delay,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, SYNC_RETRY_MAX_S)
        self._batch_t0 = None  # the batch closed at its sync
        while self._unsynced:
            # pop BEFORE the await: a stop() that cancels the worker mid-finish leaves
            # this job to the orphaned thread, and nothing finishes it twice
            job = self._unsynced.pop(0)
            await asyncio.to_thread(self._finish_job, *job)

    def _sync_and_finish(self) -> None:
        """stop()'s flush, run in a thread while stop() holds lock.write() and the
        worker is already cancelled or dead: sync the index, then finish every job of
        the open batch in claim order. A sync that raises an Exception is logged, and
        nothing is finished: the jobs keep 'processing' and their payloads, and the next
        open replays them. A finish that raises an Exception after the sync landed is
        logged too, and no later job is tried; those jobs replay the same way. It then
        returns normally, so stop() still closes the collection."""
        try:
            self._sync_index()
        except Exception:
            # never raised to stop()'s caller, so shutdown, eviction and delete complete
            # (spec 3.1 D2). Only Exception: a KeyboardInterrupt or SystemExit propagates
            _log.exception(
                "collection %s: index sync failed in stop(), %d jobs stay 'processing'"
                " for the replay on the next open",
                self.cfg.name, len(self._unsynced),
            )
            return
        self._batch_t0 = None
        while self._unsynced:
            try:
                self._finish_job(*self._unsynced[0])
            except Exception:
                # spec 3.1 D3: the sync landed, but this job and the rest stay
                # 'processing' with their payloads; the replay re-applies their rows,
                # which rewrites the same rows. Only Exception, as above
                _log.exception(
                    "collection %s: finishing jobs failed in stop(), %d jobs stay"
                    " 'processing' for the replay on the next open",
                    self.cfg.name, len(self._unsynced),
                )
                return
            # popped only once finished, so if a later finish fails, _unsynced holds
            # that job and every job after it: the ones left for the replay. No worker
            # runs now, so no other finish can take the same job
            self._unsynced.pop(0)
```

(c) `Collection._process_job`, Task 7's removal-cap pre-sync. Task 7 wrote, inside `if replaced:` under the job's `async with self.lock.write():`:

```python
                    # write earlier jobs' removals first, so no sync carries more than
                    # the cap across jobs. A job alone over it is not split (count 0)
                    await asyncio.to_thread(self._sync_index)
```

Replace those three lines with:

```python
                    # write earlier jobs' removals first, so no sync carries more than
                    # the cap across jobs. A job alone over it is not split (count 0)
                    try:
                        await asyncio.to_thread(self._sync_index)
                    except Exception:
                        # this job's rows are already committed, so it must go on to
                        # its removals and adds, or the index would lack them for good.
                        # The counter keeps the pending removals, and the batch's sync
                        # (retried until it lands) writes them all
                        _log.exception(
                            "collection %s: removal-cap index sync failed; the batch's"
                            " sync will write these removals",
                            self.cfg.name,
                        )
```

The following `await asyncio.to_thread(self._unindex, replaced)` line and the rest of the job are unchanged. A direct `_process_job(payload)` call (`sync=True`) still ends with its own sync, which raises if the disk is still failing, as at base.

(d) `Collection.stop()`. The whole method as Plans B and F left it, unchanged by D1 and Tasks 7-8 (the last two lines are Plan F's Task 1 Step 3j):

```python
    async def stop(self) -> None:
        async with self.lock.write():
            # cancel the worker only once write-locked: it is then outside its write
            # section (upsert -> index add -> sync), so no orphaned to_thread body is
            # left committing rows the index never receives behind a released lock
            if self._worker:
                self._worker.cancel()
                try:
                    await self._worker
                except asyncio.CancelledError:
                    pass
            await asyncio.to_thread(self._sync_index)
            self._closed = True
            # still write-locked, so no search is mid-query on a read connection
            await asyncio.to_thread(self._close_conns)
        if self._embedder is not None:
            await self._embedder.aclose()
        # after _closed: an orphaned search still running falls back to the serial loop
        await asyncio.to_thread(self._shard_pool.close)
```

Replace the whole method with the version below. Every step of B and F stays, in the same order, and every other line stays word for word. The changes are the `close_step` helper at its head (spec §3.1 D4), the `except Exception:` clause on the worker await (spec §3.1 D3), the `_sync_and_finish` line (the D8 flush, with the spec §3.1 D2 and D3 catches inside it), and the three close steps, each now awaited through `close_step` (spec §3.1 D4):

```python
    async def stop(self) -> None:
        async def close_step(what, step):
            # spec 3.1 D4: a close step that raises an Exception is logged with its
            # traceback and stop() goes on to the next one, so shutdown() still
            # reaches every collection and the catalog. Only Exception: a cancel
            # of stop(), a KeyboardInterrupt or a SystemExit propagates
            try:
                await step
            except Exception:
                _log.exception(
                    "collection %s: closing %s failed in stop(); the shutdown goes on",
                    self.cfg.name, what,
                )

        async with self.lock.write():
            # cancel the worker only once write-locked: it is then outside its write
            # section (upsert -> index add -> sync), so no orphaned to_thread body is
            # left committing rows the index never receives behind a released lock
            if self._worker:
                self._worker.cancel()
                try:
                    await self._worker
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # spec 3.1 D3: a worker that died of an exception (a finish or a
                    # claim that raised) is logged, never re-raised, and stop() goes on
                    # to flush and close. Any job the worker held stays 'processing'
                    _log.exception(
                        "collection %s: the ingest worker had died; stop() flushes its"
                        " open batch of %d jobs, and any job it held stays 'processing'"
                        " for the replay on the next open",
                        self.cfg.name, len(self._unsynced),
                    )
            await asyncio.to_thread(self._sync_and_finish)
            self._closed = True
            # still write-locked, so no search is mid-query on a read connection
            await close_step("the SQLite connections", asyncio.to_thread(self._close_conns))
        if self._embedder is not None:
            await close_step("the embedder's HTTP client", self._embedder.aclose())
        # after _closed: an orphaned search still running falls back to the serial loop
        await close_step("the IVF shard pool", asyncio.to_thread(self._shard_pool.close))
```

On a tree without Plan F (the spec's §2 order runs F at step 6, before D2 at step 7, so this should not happen), B's method ends at the embedder's `aclose()`: leave out the last `close_step` line and its comment. F's Task 1 Step 3j then appends its pool line as `await close_step("the IVF shard pool", asyncio.to_thread(self._shard_pool.close))`, not as its plain `await asyncio.to_thread(self._shard_pool.close)`, and `test_a_failing_shard_pool_close_is_logged` stops skipping.

Why this shape:
- The wait runs outside `lock.write()`, so searches and deletes go on during a backoff, and `stop()` takes the lock at once. `test_stop_during_sync_backoff_finishes_the_batch` would take 30 s otherwise.
- The retry never gives up. Giving up could only mean ending the worker (Task 8's behaviour) or finishing jobs whose rows are not in the index file, and the journal invariant allows neither. Under a lasting failure, every attempt logs at ERROR with the batch size, at most `SYNC_RETRY_MAX_S` apart. `pending_jobs()` stays above 0, so the LRU eviction leaves the collection resident. Shutdown and delete call `stop()`, which tries one more sync. If that fails too, `stop()` logs it and still closes, and the jobs replay on the next open (spec §3.1 D2).
- `delay` is local, so every flush starts again at `SYNC_RETRY_MIN_S`. Both constants are read at call time, so tests can monkeypatch them.
- `CancelledError` is not an `Exception`, so the retry loop never swallows `stop()`'s cancel. The cancel lands in one of three places:
  - the sleep, with `_unsynced` intact;
  - a retry's wait for the write lock, also with `_unsynced` intact;
  - a finish's thread. That job is already popped and belongs to the orphaned thread, and `_close_conns` waits for it under `db_lock`. This is B's `test_stop_waits_for_a_write_transaction_its_cancelled_worker_left_running`, where `_unsynced` is then empty.
- `_sync_and_finish` runs in one thread, under `stop()`'s `lock.write()`. The lock order stays `lock.write()` then `db_lock` (inside each `_finish_job`), and no `await` sits under `db_lock`. It calls `self._sync_index` and `self._finish_job` through `self`, so instance spies in tests see both calls.
- The catch for spec §3.1 D2 sits in `_sync_and_finish`, not in `stop()`, so `stop()` keeps B's and F's text and order and differs from them only by the one swapped line, D3's `except Exception:` clause and D4's `close_step` (both below). It catches `Exception` only: a cancel of `stop()` lands on its `await`, outside the thread, and a `KeyboardInterrupt` or `SystemExit` still propagates. It logs as `_flush_unsynced` does, with `(name, jobs)` as the args, and finishes nothing, since no job may be `done` before a sync covers it. Raising helped no caller: `CollectionManager._evict` has already popped the collection when it awaits `stop()`, and a raise in `shutdown()`'s loop left that collection, every later one and the catalog open. B's `stop()` tests never fail the final sync, so none of them changes.
- Spec §3.1 D3 closes the two other ways out of `stop()`, with the same pattern: `_log.exception`, `(name, jobs)` as the args, `Exception` only, and no change to the close order. `_closed`, `_close_conns()` under `db_lock` inside `lock.write()`, then the embedder's `aclose()` and F's `_shard_pool.close` outside it, run on every path:
  - A worker that died of an exception (a `_finish_job` in its flush that raised, say) re-raised it at `await self._worker`. The new `except Exception:` sibling of B's `except asyncio.CancelledError:` logs it with the open batch's size and goes on. The job the worker was finishing was popped before its failed finish and stays `'processing'` with its payload, unless its failed transaction was left open (next bullet but one). The rest of the batch is in `_unsynced`: `_sync_and_finish` syncs again and finishes them, which the journal invariant allows, since that sync covers them. The logged count is the batch `stop()` flushes; the job the dead worker held is not in it, and counting it would take a query.
  - A finish that raises after `stop()`'s sync landed ended `_sync_and_finish` and left `stop()` before `_closed`. It now pops each job only once its `_finish_job` returns and stops at the first failure, so `_unsynced` and the logged count hold the failing job and every job after it. It stops there because meta.db is failing, and a later finish would most likely fail the same way. Those jobs replay on the next open and rewrite the rows the index file already holds, as Task 8's `after_sync_before_finish` crash point does.
  - A finish in `_sync_and_finish` that raised inside its transaction leaves nothing half-done: no statement runs on the writer connection after it, `_close_conns` closes that connection, and SQLite rolls back the open transaction. The finish that killed a worker can differ: `_finish_job` has no rollback, so its transaction may still be open, and the next commit on that connection takes it along, whether an enqueue's before `stop()` or the first finish in `_sync_and_finish`. That job then ends with the status its finish set instead of `'processing'`, and a `done` one keeps its `job_payloads` row if the delete is what failed, which costs disk only. The worker finishes a job only after its covering sync (Task 8), so the journal invariant holds. A finish that raised after its commit (in `_vacuum_after_finish`) left its job `done` and still counts in the log; the replay skips it, so that is harmless.
  - `except Exception` leaves `CancelledError`, `KeyboardInterrupt` and `SystemExit` to propagate, as the D2 catch does. The cancel `stop()` sends a live worker comes back as `CancelledError` and still meets B's clause, so the new one sees only a worker that had already died of an `Exception`.
- Spec §3.1 D4 closes the last way out, the three close steps. Each is awaited through `close_step`, which logs a failure with `_log.exception` and `(name, step)` as the args, catches `Exception` only, and returns, so the next step runs. At B and F, a step that raised left `stop()` and skipped every later step, and `shutdown()`'s loop skipped every later collection and the catalog. The order and the locks do not change:
  - `_closed = True` is set before the first close step, so no failed step can leave the collection open to new work. A search that takes the read lock after the close stops at B's `_closed` check (plan B Task 6: 404 when `DELETE` closed the collection, spec §3.1 B1, and the `closed` error otherwise), `_rdb` raises for any other reader, and only an orphaned search still running in its thread reaches F's pool after the close, where it runs the serial loop.
  - `_close_conns` still runs in a thread under `db_lock`, inside `lock.write()`. Its failure is caught inside the `async with`, so `lock.write()` is released the normal way, and a request waiting on it finds the collection closed.
  - A failed step leaves whatever it did not close to the garbage collector. B's `_close_conns` empties `_read_conns` before its first `close()`, so a read connection it did not reach is closed when its last reference, the thread-local of the thread that opened it, is freed, and `self.db` when the collection is. D4 does not wrap each `close()` in `_close_conns`: that is B's method, and a single failed close is already logged with its traceback. An embedder whose `aclose()` failed, or a pool whose join failed, is left the same way; F's `_ShardPool.close()` sets its `_closed` under its lock before it joins its threads, so a failed join still sends every later search to the serial loop.
  - `close_step` takes an awaitable, not a callable. `asyncio.to_thread(...)` and `Embedder.aclose()` are coroutine functions, so building their coroutine at the call site runs nothing, and all the work, the failing part included, happens inside `close_step`'s `try`. It is nested in `stop()` because nothing else uses it, so `Collection` gains no method.
  - A cancel of `stop()` during a close step is a `CancelledError`, not an `Exception`: it propagates and skips the later steps, as at B and F. So do `KeyboardInterrupt` and `SystemExit`.
  - B's `test_stop_closes_connections_inside_the_write_lock_and_under_db_lock` still pins the order and the locks. Its close spy records the lock state inside `close()` and the test asserts only after `stop()` returns, so `close_step`'s catch never meets a failed assertion. No B or F test makes a close step raise.
- With an empty batch, `_sync_and_finish` is `_sync_index` alone. B's `test_stop_closes_connections_inside_the_write_lock_and_under_db_lock` and F's `test_reload_then_stop_writes_no_shards` (a clean IVF index writes nothing) see the same calls as before.
- In B's `test_stop_does_not_cancel_the_worker_inside_its_write_section`, `stop()` now finishes the one job as `done` where Task 8 left it `'processing'`. The test checks only the reopened rows and index, `(1, 1)`.
- `_removed_since_sync` resets only after a sync that returns (Task 7). A failed sync, the pre-sync included, keeps the count, and the sync that lands resets it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest_path.py -v`

Expected: `62 passed`, or `61 passed` without Task 6's gate test. On a tree without Plan F, `test_a_failing_shard_pool_close_is_logged` and Task 8's IVF test are skipped: `60 passed, 2 skipped` (`59 passed, 2 skipped` without the gate test too).

Then run the suites that pin the worker, `stop()` and the code around them:

Run: `uv run pytest tests/test_cold_start.py tests/test_concurrency.py tests/test_ivf_fanout.py tests/test_ivf_index.py tests/test_native_bm25.py tests/test_store_optimizations.py tests/test_api.py -q`

Expected: all pass, with the same counts as after Task 8. In particular:
- B's four `stop()` tests: `test_stop_does_not_cancel_the_worker_inside_its_write_section`, `test_stop_waits_for_a_write_transaction_its_cancelled_worker_left_running`, `test_read_connection_opened_during_stop_is_closed_not_leaked` and `test_stop_closes_connections_inside_the_write_lock_and_under_db_lock`.
- B's `test_no_threading_lock_is_held_across_an_await`: the retry sleeps under no lock at all.
- F's `test_reload_then_stop_writes_no_shards`: the stop flush of a clean IVF index writes no shard.
- C's `test_reconcile_without_ghosts_does_not_sync`: `_sync_and_finish` is not called at `__init__`.

In `tests/test_ingest_path.py`, Task 7's `test_removal_cap_syncs_before_crossing_the_cap` sees the same pre-syncs, since none of them fails. Task 8's `test_crash_window_replays_without_loss` is unchanged too: its batches flush before `stop()`, so `stop()` finds `_unsynced` empty.

No test in the base suite or in Plans B, C, E and F expects `stop()` or `shutdown()` to raise, and none makes a close step fail (their embedder stubs' `aclose()` returns), so spec §3.1 D3 and D4 change none of them. The helpers here that surface a dead worker, `drain()` and `wait_for_retries`, re-raise its exception through `col._worker.result()`, not through `stop()`, so they fail a test as before.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`

Expected: `N0' + 34 passed, 1 warning`. No existing test changes.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_ingest_path.py
git commit -F - <<'EOF'
Retry failed index syncs; finish the open batch in stop()

A sync that raises in _flush_unsynced no longer ends the worker. It is
logged and retried after SYNC_RETRY_MIN_S (1 s), doubling up to
SYNC_RETRY_MAX_S (30 s). The wait runs outside the write lock, and the
batch's jobs stay 'processing' meanwhile, so none is 'done' before a
sync covers it. stop() now runs _sync_and_finish in place of its plain
_sync_index: the same sync inside the write-locked section, then the
open batch's finishes, before the connections close. If that sync
fails, it is logged, nothing is finished, and stop() still closes the
collection without raising; the jobs stay 'processing' with their
payloads and replay on the next open (spec 3.1 D2). A dead worker's
exception and a finish that fails after the final sync landed are
logged the same way, and their jobs replay too (spec 3.1 D3). Each
close step (connections, embedder client, shard pool) logs its own
failure and the next one still runs (spec 3.1 D4). So no Exception
from the worker, the final sync, a finish or a close step leaves
stop(), and shutdown closes every collection and the catalog. A
removal-cap pre-sync that fails inside a job is logged and the job
goes on, so its committed rows still reach the index.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Review Focus for Task 9:** RF4 (`stop()` loses the open batch or flushes outside B's critical section) is pinned here by `test_stop_flushes_the_open_batch`, together with Plan B's `test_stop_closes_connections_inside_the_write_lock_and_under_db_lock`, which stays green. Spec §3.1 D2 (a failed final sync is logged and the shutdown completes) is pinned by `test_stop_with_a_failing_sync_logs_it_and_still_closes` and `test_a_failed_stop_sync_replays_on_the_next_open`. Spec §3.1 D3 (a dead worker and a failed finish are logged) is pinned by `test_stop_logs_a_dead_workers_exception_and_still_closes` and `test_a_failed_finish_in_stop_is_logged_and_replays`. Spec §3.1 D4 (a failed close step is logged and the next one runs) is pinned by `test_a_failing_embedder_close_is_logged_and_stop_goes_on`, `test_a_failing_close_conns_is_logged_and_the_later_steps_run` and `test_a_failing_shard_pool_close_is_logged`. `test_shutdown_closes_every_collection_after_a_failed_stop` pins D3 and D4 together: no `Exception` from the worker, the final sync, a finish or a close step leaves `stop()`. When reviewing, check that:
- `stop()` differs from Plans B and F by the one swapped line, the `except Exception:` clause on the worker await and the `close_step` helper around each of the three close steps, nothing else. `_sync_and_finish` runs after the worker's cancel and await and before `_closed = True` and `_close_conns()`, all inside `lock.write()`. The embedder's `aclose()` follows outside the lock, and F's `_shard_pool.close` stays last;
- `_sync_and_finish` catches only an `Exception` from its sync, logs it once through `_log.exception` and finishes nothing, so `stop()` still sets `_closed` and runs its three close steps, and the sync's failure does not leave it (spec §3.1 D2). On a sync that lands, it pops each job once its `_finish_job` returns; a finish that raises an `Exception` is logged once with the jobs still in `_unsynced`, and no later job is tried (spec §3.1 D3);
- a worker that died of an `Exception` is logged once through `_log.exception` at `await self._worker`, with `(name, jobs in the open batch)`, and `stop()` then flushes and closes as usual. `CancelledError` still meets B's clause, and nothing but `Exception` is caught (spec §3.1 D3);
- `close_step` awaits its step inside `try`, catches `Exception` only and logs it once through `_log.exception` with `(name, step)`, so the next step runs. Each step is passed as an unstarted coroutine (`asyncio.to_thread(...)`, `aclose()`), so no part of it runs before the `try`. `_closed = True` is set before the first step, and a failure in `_close_conns` is caught inside `lock.write()`, which is then released the normal way (spec §3.1 D4);
- the retry's `asyncio.sleep` sits outside `async with self.lock.write()`, and the handler catches `Exception`, never `BaseException`, so a cancel always passes through. No path finishes a job of the batch before a sync returns;
- the delay starts at `SYNC_RETRY_MIN_S` in every flush, doubles, and never exceeds `SYNC_RETRY_MAX_S`. Each failure is logged with `_log.exception`, and the traceback goes with it;
- the pre-sync guard in `_process_job` only logs: the job goes on to `_unindex` and its adds, and the removal counter is not reset;
- out of scope: a `_finish_job` that raises inside the worker still ends it, as at base; spec §3.1 D3 covers what `stop()` does after that, not the worker. A failed `_close_conns` leaves the connections it did not reach to the garbage collector: D4 wraps B's method as one step and does not change it. The docs and the D2 benchmark are Task 10's.

### Task 10: Docs, env rows, DGX tuning and publish

Goal: document D2's batched index sync and its two knobs, choose N and T on seed 7, and publish D2 against its base on seed 42 (spec §6: "Tunables (pool size, batch N/T, candidate depth) are chosen on an independent sample (seed 7 ...) and published on seed 42"; §7 D). There are two gn100 launches, both through Task 6's driver:
- **tuning** (`planD-ab.sh d2tune`): the D2 tree only, `bench/ingest_probe.py --seed 7`, flat and `--ivf 256`, over the pairs (`SYNC_BATCH_JOBS`, `SYNC_BATCH_MS`) in {(1, 0), (8, 1000), (32, 1000), (32, 4000)}. Per mode there is one discarded warm-up, then 3 rounds, each round running the four pairs in turn. (1, 0) syncs after every job and is the reference. If the rule in Step 3b picks a pair other than (8, 1000), the defaults, their two pins and the docs change in one commit before the publish (Step 10; spec §3.1 D1 allows it, seed 42 confirms the chosen values, and the PR states them);
- **publish** (`planD-ab.sh d2`): Task 6's A/B unchanged, seed 42, base = `main` after D1, E and F (the parent of Task 7's commit) against D2 at its defaults, 3 interleaved rounds per mode after a discarded base warm-up, and the bench reingests last, only if the window allows.

A declined window leaves the D2 results `Pending:` (Step 2 is committed either way), and no test is appended.

**Files:**
- Modify `docs/adr/0001-performance-optimization-decisions.md` (UTF-8):
  - the lever bullet at line 50, in `## Standing constraints these decisions respect`. A-F leave it as it is (F edits lines 48 and 49 only);
  - append `### Batched sync (D2)` and `### Results — DGX A/B (D2)` at the very end of the file. That is inside D1's `## Addendum 2026-09 — ingest path`, after Task 6's D1 results subsection: E and F add nothing to ADR 0001 after it.
- Modify `docs/storage.md`: item 3 of `## Ingest durability` as Task 4 left it, and the `## Backup` sentence that says `index.tvim` "is rewritten on every sync" (base line 41, untouched by A-F).
- Modify `README.md` and `docs/getting-started.md`: two rows appended to the environment table, after Plan F's `IVF_SEARCH_THREADS` row, which is the last row after E and F.
- Only if seed 7 picks another pair (Step 10): `src/raggio/config.py` and `src/raggio/store.py` (Task 7's two default lines in each), Task 7's `test_settings_sync_batch_knobs` and `test_ingest_probe_accepts_batch_flags`, the two env rows and the ADR's knob row.
- Outside the repo, never committed:
  - modify `D:/DEV/_scratch/dgx/planD/planD-ab.sh` to add the modes `d2tune` and `d2`;
  - create `planD-d2-summary.py` (the tuning choice, the D2 gates, the ADR block) and `planD-d2-check.py` (a laptop self-check).
  - `planD-summary.py`, `planD-check.py` and `planD-build.sh` stay as Task 6 wrote them. Results are fetched to `D:/DEV/_scratch/dgx/planD/d2tune/` and `.../d2/`, and the SHAs are pinned in `D:/DEV/_scratch/dgx/planD/d2-shas.env`.
- Modify `tests/test_ingest_path.py` (Step 14, only after a completed publish): append the section `# ---- ADR 0001 addendum: D2 DGX results` with `D2_RESULTS`, `adr_d2_results()` and `test_adr_ingest_path_d2_results_pass_the_gates`.
- Not touched: `bench/ingest_probe.py` (Task 7's flags are enough), `bench/bench.py`, `bench/dgx/*.sh`, `app.py`.
- Test: `uv run pytest tests/test_ingest_path.py -v -k adr_ingest_path_d2`.

**Interfaces:**
- Consumes:
  - Task 6: `planD-ab.sh <mode> <base sha> <cand sha> <reingest 0|1> <cap minutes>` with its `probe`, `reingest`, `fail` and `ts` functions and `$PY`/`$OUT`. From `planD-summary.py`: `DASH`, `FENCE`, `MODES`, `ARMS`, `ROUNDS`, `PROBE_REGIME`, `PROBE_CMD`, `NOT_RUN`, `read`, `start_facts(d) -> (date, base_sha, cand_sha, finished)`, `stats(base, cand)`, `probe_part(d)`, `bench_part(d)`, `row(...)`, and its module-level `compare` import. From `planD-check.py`: `check_text()`, which already checks the text of `planD-ab.sh`. Also Task 6's D1 results subsection, `Pending:` or measured, and its `adr_d1_results()` slice, which ends at the next `\n### `.
  - Task 7: `Settings.sync_batch_jobs` / `sync_batch_ms` (defaults 8 and 1000.0), `Collection(..., sync_batch_jobs: int = 8, sync_batch_ms: float = 1000.0)`, and the probe's `--batch-jobs N --batch-ms T` with the result keys `sync_batch_jobs` / `sync_batch_ms` (`None` on a tree without the knobs). Also its commit subject `Add batched-sync plumbing: knobs, claim cursor, sync flag, removal cap`, which finds D2's base.
  - Tasks 8 and 9: the behaviour the docs describe (batch bounds, idle flush, index-job barrier, claim cursor, removal cap, `SYNC_RETRY_MIN_S = 1.0` / `SYNC_RETRY_MAX_S = 30.0`, and the `stop()` flush, which logs a failed final sync and still closes, spec §3.1 D2; a dead worker's exception and a failed finish are logged too, spec §3.1 D3, and so is a failed close step, after which the next one still runs, spec §3.1 D4).
  - Plan A: `bench/dgx/compare.py` `summary`, `verdict` (`"better"`, `"worse"`, `"within band"`) and `RESOLUTION["ingest_vps"]` (1 vec/s).
  - The test module head: `json`, `Path`, `Settings`.
- Produces:
  - The ADR D2 subsections. `### Results — DGX A/B (D2)` first holds `Pending: the gn100 A/B of Plan D Task 10 has not run yet.`, the exact line Task 6's `planD-check.py` `check_adr` already uses. Step 16 replaces it with `#### Measurements (D2)`: a short text, the tuning table, a `Chosen:` line, the publish row table, the `- PASS: D2-...` gate lines and one fenced `json` block. Its keys are `date`, `base_sha`, `cand_sha`, `tuning`, `probe`, `bench`, `labels`, `gates`.
  - `tuning` = `{"seed": 7, "pairs": {"n1-t0": P, "n8-t1000": P, "n32-t1000": P, "n32-t4000": P}, "choice": [N, T] or null, "complete": bool, "fingerprints_equal": bool, "labels_ok": bool}`. Each `P` holds `flat` and `ivf256` (the 3 measured `ingest_vps` values, rounded to 0.1), `flat_median`, `ivf256_median`, `flat_vs_ref` and `ivf256_vs_ref` (`compare.verdict` against (1, 0)).
  - `planD-ab.sh` modes: `d2tune` writes `~/planD/d2tune/probe/<flat|ivf256>-n<N>-t<T>-run<r>.json`; `d2` writes the same layout as `d1`, under `~/planD/d2/`.
  - `planD-d2-summary.py tune <d2tune dir>` prints the table and a last line `CHOICE <N> <T>` (exit 0), or `NO CHOICE` (exit 1). `planD-d2-summary.py publish <d2 dir> --tune <d2tune dir> [--adr ADR]` prints the block, exits 1 on any `- FAIL:` line, and with `--adr` replaces only the D2 `Pending:` line.
  - Test helper `adr_d2_results() -> tuple[str, dict]`.

- [ ] **Step 1: Ask the user for a run window (time cap)**

Ask, and wait for an explicit answer. Steps 2-5 are laptop-only and may proceed meanwhile, but nothing runs on gn100 before the answer. The per-run time is Task 6's estimate, about 5 minutes per probe run (100k prefill rows, then 200 jobs x 250 rows at 1024-d):

> Plan D2's DGX work needs gn100 for two launches. They can share one window or use two.
> 1. **Tuning, about 2 h 40 min:** about 5 min to deploy the D2 tree, then about 130 min for 26 in-process ingest probe runs of the D2 tree only (`bench/ingest_probe.py --seed 7`, flat and IVF nlist 256). They cover 4 settings of the new knobs (`SYNC_BATCH_JOBS`, `SYNC_BATCH_MS`) = (1, 0), (8, 1000), (32, 1000), (32, 4000), 3 rounds each, after one discarded warm-up per mode. The launch cap is 150 min. These are host processes with `OPENBLAS_NUM_THREADS=1`, and no container runs.
> 2. **Publish, about 1 h 50 min probe-only, or about 4 h 50 min with the reingests:** up to 20 min to build two images (`main` after D1, E and F, and D2), then Task 6's A/B at seed 42: 14 probe runs, about 70 min (launch cap 85 min). Optionally, 4 full bench reingests of the 2.55M-row corpus follow, interleaved base, D2, base, D2, each on a fresh 4 GiB `bench-tv` (+150 min, launch cap 270 min in all). They run last, and only if 180 min of the launch cap are left after the probes.
>
> Between the two launches, about 10 minutes of laptop work pick the defaults. If seed 7 prefers a setting other than (8, 1000), I commit that change before the publish.
>
> It uses only the `bench-tv` container and volume, `~/raggio` (replaced with D2's tree, spec §6 deploy) and `~/planD/`, where an earlier `~/planD` is moved aside, never deleted. It never touches `bench-wv` or `~/turborag`, and uses no `sudo`. The reingests delete and re-create the `bench` collection in the `bench-tv` volume. They leave it flat, with 2,549,119 rows written by the D2 image and the queue drained. If the cap kills a reingest midway, the volume holds a partial collection until one more reingest runs.
>
> May I run them? If so: tuning and publish in one window or two; the publish probe-only or with the reingests; and until what time? Is anything else running on gn100 in that window?

Record the answer, the start time and the caps:
- If the user declines, do Step 2 only (the D2 results stay `Pending:`), then stop. The suite count stays at `N0' + 34`.
- `TUNE_CAP=150` for the tuning launch (Step 8). A shorter grant cannot cover the grid: ask again rather than trimming the grid.
- `REINGEST=0 PUB_CAP=85` for a probe-only publish, or `REINGEST=1 PUB_CAP=270` with the reingests (Step 12). For a shorter grant, `PUB_CAP` is the minutes left after the builds.
- If the grant covers only the tuning, stop after Step 10. The publish (Steps 11-19) is a new window and a new question.

- [ ] **Step 2: Document batched sync and the knobs; record the D2 decisions with the results Pending**

This step runs whatever the answer to Step 1. It changes docs only, so it adds no test.

2a. `docs/adr/0001-performance-optimization-decisions.md` (UTF-8; use a UTF-8 aware editor). Task 7 removed the `ponytail:` comment that the lever bullet points at. Replace this exact line in `## Standing constraints these decisions respect`:

```markdown
- Ingest throughput has a known lever if it ever matters: batch index syncs / vacuum every N jobs instead of per job (`ponytail:` comment in `store.py`).
```

with:

```markdown
- Ingest index syncs are batched: one sync covers up to `SYNC_BATCH_JOBS` jobs or `SYNC_BATCH_MS` ms, and a job turns `done` only after that sync. The full vacuum waits for an idle queue. See "Addendum 2026-09 — ingest path" below.
```

Then append at the very end of the file, after Task 6's `### Results — DGX A/B (D1)` subsection (its `Pending:` line, or its measurements) and one blank line:

```markdown
### Batched sync (D2)

Date: 2026-09-28 · Plan D, phase D2. It replaces the "Deferred to D2" row above: D1 kept one index sync per job. Direction from the item2a research (laptop): turbovec v7 writes appends whole and keeps each removal as a header redo op until the next sync, which rewrites the 32-row unit; past 1024 ops a sync rewrites the whole file through a temp file and a rename (about 7 µs per row). A flat sync costs 7–9 ms. An IVF sync costs about 8 ms per dirty shard, and one 250-row job dirties about 160 of 256 shards. The knob defaults are chosen on seed 7 and published on seed 42 (spec §6), under "Results — DGX A/B (D2)" below. Every row is pinned by `tests/test_ingest_path.py`.

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| One index sync per batch of ingest jobs. A batch closes when it holds `SYNC_BATCH_JOBS` jobs (default 8), when `SYNC_BATCH_MS` ms (default 1000) have passed since its first job joined, when the queue goes idle, before an attach or detach job (which then runs alone), and in `stop()`. `SYNC_BATCH_JOBS=1` syncs after every job, as D1 did | **Accepted** | Per-job syncs dominate IVF ingest: about 1.3 s of shard writes per 250-row job at nlist 256. The seed-7 grid (1, 0), (8, 1000), (32, 1000), (32, 4000) in the results below picks the defaults |
| Done-after-sync: a job's rows are committed and indexed in memory, and the job stays `processing` until the sync that covers it returns. The batch's jobs then finish in order, `error` jobs included | **Accepted** | No job reads `done` before its vectors are on disk. A crash before the batch's sync returns leaves all of its jobs `processing`, and a crash during its finishes leaves the unfinished rest `processing`. The idempotent replay redoes them |
| Claim cursor: the claim adds `AND id > ?` to its `INDEXED BY idx_jobs_open` query, so the worker never re-claims its own `processing` jobs | **Accepted** | A batch's jobs stay `processing` until its sync |
| Removal cap: no sync carries more than `SYNC_MAX_REMOVALS` (512) removals accumulated across a batch's jobs. The job that would pass it syncs first, after its own commit. A lone job over the cap is not split | **Accepted** | Removals pile up as header ops; past 1024 ops the sync becomes a full-file rewrite, extrapolated at about 20 s under the collection's write lock at 2.55M rows |
| A failed sync is logged and retried after `SYNC_RETRY_MIN_S` (1 s), doubling up to `SYNC_RETRY_MAX_S` (30 s). Meanwhile the batch's jobs stay `processing` and the worker claims nothing new; the wait holds no lock. A failed removal-cap pre-sync is logged, and the job goes on | **Accepted (bug fix)** | Before, a sync that raised failed its job as `error`: terminal, never replayed, with committed rows missing from the index file |
| `stop()` syncs the open batch and finishes its jobs. If that sync fails, `stop()` logs it and completes the shutdown without raising: it finishes nothing, the jobs stay `processing` with their payloads, and the next open replays them. A dead worker's exception, and a finish that fails after that sync landed, are logged the same way, and their jobs replay too. A failure to close the SQLite connections, the embedding client or the IVF shard pool is logged, and the next close step still runs | **Accepted** | A clean shutdown finishes what it synced; a failing index file, meta.db or close step never stops a shutdown half-way, so every collection and the catalog close, and the journal carries the unfinished jobs to the next open |
| Several jobs per SQLite transaction | **Still rejected** | Each job keeps its own transaction and its own finish; only the index sync is shared |

### Results — DGX A/B (D2)

Pending: the gn100 A/B of Plan D Task 10 has not run yet.
```

`### Batched sync (D2)` ends Task 6's `adr_d1_results()` slice, which stops at the next `\n### `, so the D1 test sees the same section as before. `Pending: the gn100 A/B of Plan D Task 10 has not run yet.` is the exact line Task 6's `planD-check.py` `check_adr` already uses.

2b. `docs/storage.md` (UTF-8; the new text is ASCII). Three edits, each anchored on quoted text:

In `## Ingest durability`, item 3 as Task 4 left it, replace only its first line:

```markdown
3. The job is marked `done` only after the vector index is synced to disk.
```

with:

```markdown
3. The job is marked `done` only after the index sync that covers it has
   written its vectors to disk. One sync covers a batch of up to
   `SYNC_BATCH_JOBS` jobs, or the jobs of `SYNC_BATCH_MS` milliseconds (see
   [Configuration](getting-started.md#configuration)), so a finished job can
   stay `processing` that long.
```

The rest of item 3 (`A successful job's payload row is deleted ...` through Task 4's `trims the excess as each job finishes.`) stays.

In the replay paragraph below the list, replace its first line:

```markdown
After a crash, any `pending` or `processing` job is replayed on boot.
```

with:

```markdown
After a crash, any `pending` or `processing` job is replayed on boot. A
crash before a batch's sync lands leaves all of the batch's jobs
`processing`, so the whole batch replays; a crash while its jobs finish
replays only the ones not yet `done`. A shutdown whose last index sync
fails (a full disk, say) logs the error and still closes; that batch's
jobs stay `processing` and replay the same way. The same holds when the
shutdown finds the ingest worker stopped by an error, or cannot mark
synced jobs `done`: the error is logged, every collection still closes,
and the unfinished jobs replay. A collection whose database connections,
embedding client or IVF thread pool fail to close logs the error, and
the shutdown goes on to its next close step and the next collection.
```

In `## Backup`, replace the line:

```markdown
SQLite database and `index.tvim` is rewritten on every sync, so copy while
```

with:

```markdown
SQLite database, and every index sync writes to `index.tvim` (or to the
changed shard files under `ivf/`), so copy while
```

The next line, `nothing writes: stop the container, ...`, continues the sentence unchanged.

2c. The environment tables. In `README.md`, insert directly below the row that starts with `` | `IVF_SEARCH_THREADS` | `min(12, CPUs)` | `` (Plan F's row):

```markdown
| `SYNC_BATCH_JOBS` | `8` | ingest jobs per vector-index sync; a job is `done` only after its sync; `1` = sync per job |
| `SYNC_BATCH_MS` | `1000` | ms after a batch's first job before its sync runs, even if fewer jobs arrived; `0` = sync per job |
```

In `docs/getting-started.md`, insert directly below the row that starts with `` | `IVF_SEARCH_THREADS` | `min(12, CPUs)` | ``:

```markdown
| `SYNC_BATCH_JOBS` | `8` | Ingest jobs covered by one vector-index sync; each job turns `done` only after the sync that covers it. `1` syncs after every job |
| `SYNC_BATCH_MS` | `1000` | Milliseconds after a batch's first job at which the batch syncs even if it holds fewer jobs; `0` closes each batch at its first job |
```

`_run_worker` (Task 8) closes a batch once `(time.monotonic() - self._batch_t0) * 1000.0 >= self.sync_batch_ms`, so `0` does close each batch at its first job. Plan F's test only looks for its own row, and Plan E's test finds `NATIVE_BM25` by `startswith`, so neither sees the new rows.

Run: `uv run pytest -q`
Expected: `N0' + 34 passed, 1 warning`. Step 2 adds no test. `N0'` is the suite count on D2's branch base, recorded before Task 7 (the Phase D2 note).

Run: `uv run pytest tests/test_cold_start.py -q -k acceptance_run`
Expected: `1 passed, 64 deselected`, or `64 deselected` if Plan C's Task 12 test did not land. Plan C's section still ends at `## Addendum 2026-09 — ingest path`.

Run: `uv run pytest tests/test_ingest_path.py -q -k adr_ingest_path_d1`
Expected: `1 passed, 61 deselected` if Task 6's run landed, or `61 deselected` if it did not. Either way the D1 section is unchanged.

```bash
git add docs/adr/0001-performance-optimization-decisions.md docs/storage.md README.md docs/getting-started.md
git commit -F - <<'EOF'
Document batched index syncs and their two knobs

ADR 0001: the standing-constraint lever bullet now says index syncs
are batched, and "Batched sync (D2)" records the batch bounds,
done-after-sync, the claim cursor, the removal cap, the sync retry
and the stop() flush. The gn100 results are Pending until Task 10's
run. docs/storage.md: a job is done only after the sync that covers
it, a crash before a batch's sync replays the whole batch, so does a
shutdown whose last sync fails (logged, and the shutdown completes),
and so do the jobs of a dead worker or of a failed finish at shutdown;
a close that fails is logged and the shutdown goes on. The backup note
no longer says index.tvim is rewritten on every sync. README and
Getting started list SYNC_BATCH_JOBS and SYNC_BATCH_MS.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 3: Extend the DGX scripts (outside the repo)**

All of this is in `D:/DEV/_scratch/dgx/planD/`, next to Task 6's four files, and is never committed. Keep LF line endings and ASCII only; Step 4 checks both. `planD-build.sh`, `planD-summary.py` and `planD-check.py` do not change. `planD-check.py` still checks the text of the edited `planD-ab.sh` (no `bench-wv`, `turborag`, `sudo`, `rm -rf`; the one `podman rm` line; `--reingest` only inside `reingest()`).

3a. `planD-ab.sh`: five edits, each anchored on Task 6's text.

(1) Directly above the line `set -uo pipefail`, insert:

```bash
# Modes: d1 (Task 6) and d2 (Task 10) run the A/B above into ~/planD/<mode>. d2tune (Task 10)
# runs only the cand tree (~/raggio) at --seed 7 over the SYNC_BATCH_JOBS/SYNC_BATCH_MS grid,
# needs no image, and stops before the reingest phase. Pass the cand sha twice and REINGEST=0:
#   setsid -f timeout --kill-after=60 150m bash ~/planD/planD-ab.sh d2tune <cand> <cand> 0 150 >/dev/null 2>&1 </dev/null
```

(2) Replace the mode check:

```bash
[ "$MODE" = d1 ] || { echo "unknown mode $MODE"; exit 2; }
```

with:

```bash
case $MODE in d1|d2|d2tune) ;; *) echo "unknown mode $MODE"; exit 2 ;; esac
```

(3) Directly above the line `start_ctr() {  # $1 = image tag: a fresh 4 GiB bench-tv of that image`, insert a sibling of `probe()`:

```bash
tune_probe() {  # d2tune: $1 = flat|ivf256, $2 = SYNC_BATCH_JOBS, $3 = SYNC_BATCH_MS, $4 = run number
  local nl=0 f=$OUT/probe/$1-n$2-t$3-run$4.json
  [ "$1" = ivf256 ] && nl=256
  echo "== tune $1 n$2 t$3 run$4 ($(ts)) =="
  "$PY" -u ~/raggio/bench/ingest_probe.py --ivf "$nl" --seed 7 --prefill 100000 --jobs 200 \
    --job-rows 250 --batch-jobs "$2" --batch-ms "$3" --tmp ~/planD/tmp --out "$f" \
    || fail "tune $1 n$2 t$3 run$4"
  "$PY" - "$f" ~/raggio/src/raggio/store.py "$2" "$3" <<'EOF' || fail "tune $1 n$2 t$3 run$4: wrong tree, knobs or unfinished jobs"
import json, os, sys
r = json.load(open(sys.argv[1]))
print("  vps %.0f drain %.1f s stall %.2f s jobs %s knobs %s/%s payload_rows %s" % (
    r["ingest_vps"], r["drain_s"], r["loop_stall_s"], r["jobs"], r["sync_batch_jobs"],
    r["sync_batch_ms"], r["payload_rows"]))
same = os.path.realpath(r["store_file"]) == os.path.realpath(sys.argv[2])
knobs = (r["sync_batch_jobs"], r["sync_batch_ms"]) == (int(sys.argv[3]), float(sys.argv[4]))
sys.exit(0 if same and knobs and r["jobs"] == {"done": 200} else 1)
EOF
}
```

(4) The d2tune launch builds no image, so the preflight must not look for one. Replace the line:

```bash
for s in "$BASE" "$CAND"; do
```

with:

```bash
IMAGES="$BASE $CAND"
[ "$MODE" = d2tune ] && IMAGES=""  # host probes only: no image is built before the tuning
for s in $IMAGES; do
```

(5) Directly below the line `echo "labels: probe rows host-warm, uncapped host process; bench rows host-warm, cap 4g"` and above `for m in flat ivf256; do`, insert the tuning branch. It ends the script before the A/B loop and the reingest phase:

```bash
if [ "$MODE" = d2tune ]; then
  for m in flat ivf256; do
    tune_probe "$m" 8 1000 0  # warm-up, discarded
    for r in 1 2 3; do
      for p in "1 0" "8 1000" "32 1000" "32 4000"; do
        tune_probe "$m" $p "$r"  # $p splits into N and T
      done
    done
  done
  echo "== tune done ($(ts)) =="
  echo "== PLAND AB DONE $(ts) =="
  exit 0
fi
```

Nothing else changes. `d2` runs Task 6's code path unchanged: `probe` passes no batch flags, so the cand tree runs at the `Collection` defaults, which Step 10 keeps equal to the seed-7 choice. The base tree (`main` before Task 7) gets D2's probe copied in (Step 11), which reports `sync_batch_jobs` / `sync_batch_ms` as `None` there. `mkdir -p` still creates empty `bench-base`/`bench-cand` dirs under `~/planD/d2tune`; the tuning summary ignores them.

3b. Create `D:/DEV/_scratch/dgx/planD/planD-d2-summary.py`. It loads `planD-summary.py` as a module (as `planD-check.py` does) and reuses its probe and bench readers, `stats` and `row`. It does not reuse D1's gate function or its bench jobs check, which expects no `job_payloads` table in the base. D2's base already has one, so both arms must end with 0 payload rows. The file is ASCII.

The choice rule applies spec §6 to seed 7 only. A batched pair qualifies if its flat verdict against (1, 0) is `better` or `within band` and its ivf256 verdict against (1, 0) is `better`. Among the qualifying pairs, the best ivf256 median wins, but (8, 1000) stays unless the winner beats it on ivf256 beyond the band: a default moves only on a measured difference. No qualifying pair means `NO CHOICE`: stop and report (Step 9).

```python
#!/usr/bin/env python3
"""Plan D2 DGX summary: the seed-7 tuning choice, the D2 gates and the ADR record.

Usage, from the repo root (planD-summary.py imports Plan A's bench/dgx/compare.py from there):
  uv run python D:/DEV/_scratch/dgx/planD/planD-d2-summary.py tune <d2tune dir>
  uv run python D:/DEV/_scratch/dgx/planD/planD-d2-summary.py publish <d2 dir> --tune <d2tune dir> [--adr ADR]
tune prints the tuning table and a last line "CHOICE <N> <T>" (exit 0) or "NO CHOICE" (exit 1).
publish prints the "#### Measurements (D2)" block and exits 1 if any gate fails. With --adr,
and only when no gate fails, it replaces the Pending line of the ADR's D2 results section.
Nothing here loosens a gate.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("planD_summary", Path(__file__).resolve().parent / "planD-summary.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)
compare = S.compare

RESULTS_HEAD = "### Results " + S.DASH + " DGX A/B (D2)"
PENDING = "Pending: the gn100 A/B of Plan D Task 10 has not run yet."
NOT_RUN = ("Not run: the bench reingest was skipped (a probe-only window, or under 180 min"
           " left before the cap), so this run gives no end-to-end ingest_vps row for D2.")
PAIRS = ((1, 0), (8, 1000), (32, 1000), (32, 4000))  # (SYNC_BATCH_JOBS, SYNC_BATCH_MS)
REF = (1, 0)  # one sync per job, as in D1
DEFAULT = (8, 1000)  # Task 7's defaults
TUNE_CMD = ("bench/ingest_probe.py --seed 7 --prefill 100000 --jobs 200 --job-rows 250"
            " --batch-jobs N --batch-ms T")
CLAIMABLE = ("better", "within band")  # "not worse", given complete runs


def key(pair):
    return f"n{pair[0]}-t{pair[1]}"


def tune_part(d):
    """(tuning, tuning labels) from probe/<mode>-n<N>-t<T>-run<r>.json of a fetched ~/planD/d2tune."""
    runs, fps, ok = {}, {m: set() for m in S.MODES}, True
    seen = {k: set() for k in ("regime", "sqlite_version", "openblas_num_threads", "turbovec")}
    for path in sorted((d / "probe").glob("*-n*-t*-run*.json")):
        m, n, t, run = path.stem.split("-")
        pair, r = (int(n[1:]), int(t[1:])), S.read(path)
        runs[(m, pair, int(run[3:]))] = r
        fps[m].add(json.dumps(r["fingerprint"], sort_keys=True))
        ok = (ok and S.store_ok("cand", r["store_file"])
              and (r["sync_batch_jobs"], r["sync_batch_ms"]) == (pair[0], float(pair[1])))
        for k in seen:
            seen[k].add(str(r[k]))
    pairs = {}
    for pair in PAIRS:
        p = {}
        for m in S.MODES:
            p[m] = [round(runs[(m, pair, n)]["ingest_vps"], 1) for n in S.ROUNDS if (m, pair, n) in runs]
            p[f"{m}_median"] = round(compare.summary(p[m])[0], 1) if p[m] else None
        pairs[key(pair)] = p
    ref = pairs[key(REF)]
    for pair in PAIRS:
        p = pairs[key(pair)]
        for m in S.MODES:
            p[f"{m}_vs_ref"] = ("reference" if pair == REF else "missing" if not (ref[m] and p[m])
                                else compare.verdict("ingest_vps", ref[m], p[m]))
    labels = {k: sorted(v) for k, v in seen.items()}
    labels["cap"] = ["none (uncapped host process)"]
    complete = (S.start_facts(d)[3]
                and all(len(pairs[key(p)][m]) == len(S.ROUNDS) for p in PAIRS for m in S.MODES)
                and all(r["jobs"] == {"done": 200} and r["payload_rows"] == 0 for r in runs.values()))
    tuning = {"seed": 7, "pairs": pairs, "choice": None, "complete": bool(complete),
              "fingerprints_equal": all(
                  sum(k[0] == m for k in runs) == 1 + len(PAIRS) * len(S.ROUNDS) and len(fps[m]) == 1
                  for m in S.MODES),
              "labels_ok": (ok and labels["regime"] == [S.PROBE_REGIME]
                            and labels["openblas_num_threads"] == ["1"]
                            and len(labels["sqlite_version"]) == 1)}
    if tuning["complete"] and tuning["fingerprints_equal"] and tuning["labels_ok"]:
        choice = choose(pairs)
        tuning["choice"] = list(choice) if choice else None
    return tuning, labels


def choose(pairs):
    """Seed 7's pick: see Step 3b. None when no batched pair qualifies."""
    ok = [p for p in PAIRS if p != REF and pairs[key(p)]["flat_vs_ref"] in CLAIMABLE
          and pairs[key(p)]["ivf256_vs_ref"] == "better"]
    if not ok:
        return None
    best = max(ok, key=lambda p: pairs[key(p)]["ivf256_median"])  # ties: the first in PAIRS
    if DEFAULT in ok and best != DEFAULT and compare.verdict(
            "ingest_vps", pairs[key(DEFAULT)]["ivf256"], pairs[key(best)]["ivf256"]) != "better":
        return DEFAULT
    return best


def tune_table(tuning, labels):
    lines = ["| SYNC_BATCH_JOBS | SYNC_BATCH_MS | Regime | Cap | sqlite_version | OPENBLAS_NUM_THREADS"
             " | flat (3 runs) | flat median | flat vs (1, 0) | ivf256 (3 runs) | ivf256 median"
             " | ivf256 vs (1, 0) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for pair in PAIRS:
        p = tuning["pairs"][key(pair)]
        lines.append(f"| {pair[0]} | {pair[1]} | {S.PROBE_REGIME} | {labels['cap'][0]}"
                     f" | {', '.join(labels['sqlite_version'])} | {', '.join(labels['openblas_num_threads'])}"
                     f" | {', '.join(map(str, p['flat']))} | {p['flat_median']} | {p['flat_vs_ref']}"
                     f" | {', '.join(map(str, p['ivf256']))} | {p['ivf256_median']} | {p['ivf256_vs_ref']} |")
    return lines


def knobs_ok(d, choice):
    """Every publish probe: base (main before Task 7) has no knobs, cand runs at the choice."""
    if choice is None:
        return False
    want = {"base": (None, None), "cand": (choice[0], float(choice[1]))}
    runs = sorted((d / "probe").glob("*-run*.json"))
    return bool(runs) and all(
        (r.get("sync_batch_jobs"), r.get("sync_batch_ms")) == want[p.stem.split("-")[1]]
        for p, r in ((p, S.read(p)) for p in runs))


def bench_jobs_ok(bench):
    """D2's bench jobs check: both arms keep job_payloads, so both end with 0 payload rows."""
    return bench is None or all(set(r["jobs"]) == {"done"} and r["payload_rows"] == 0
                                for r in bench["runs"].values())


def gates_for(finished, tuning, knobs, probe, bench, labels_ok, jobs_ok):
    g = {"D2-run": finished,
         "D2-tuning": (tuning["complete"] and tuning["fingerprints_equal"] and tuning["labels_ok"]
                       and tuning["choice"] is not None and knobs),
         "D2-flat": probe["flat"]["complete"] and probe["flat"]["verdict"] in CLAIMABLE,
         "D2-ivf256": probe["ivf256"]["complete"] and probe["ivf256"]["verdict"] == "better",
         "D2-fingerprints": all(probe[m]["fingerprints_equal"] for m in S.MODES),
         "D2-jobs": jobs_ok and all(
             probe[m]["jobs_all_done"] and set(probe[m]["payload_rows"]["base"]) == {0}
             and set(probe[m]["payload_rows"]["cand"]) == {0} for m in S.MODES),
         "D2-bench": None if bench is None else (
             bench["verdict"] in CLAIMABLE and all(len(bench[a]) == 2 for a in S.ARMS)),
         "D2-labels": labels_ok}
    return {k: "SKIP" if v is None else ("PASS" if v else "FAIL") for k, v in g.items()}


def render(tune_facts, tuning, facts, probe, bench, labels, gates):
    (tdate, tsha), (date, base_sha, cand_sha) = tune_facts, facts
    c = tuning["choice"]
    lines = ["#### Measurements (D2)", "",
             f"Tuning run {tdate} on gn100 (NVIDIA DGX Spark, GB10 Grace, 20 aarch64 cores), D2 tree"
             f" `{tsha}` only: `{TUNE_CMD}` with `--ivf 0` and `--ivf 256`, one discarded (8, 1000) run0"
             " and then 3 rounds per mode, each running the four pairs in turn. Values are ingest vec/s."
             " (1, 0) syncs after every job, as D1 did. Band = max(max - min of each side, 1 vec/s);"
             " a claim needs gain > band.", ""] + tune_table(tuning, labels["tune"]) + [""]
    lines.append("Chosen: none." if c is None else f"Chosen: SYNC_BATCH_JOBS={c[0]}, SYNC_BATCH_MS={c[1]}.")
    lines += ["", f"Publish run {date}: base `{base_sha}` (main after D1, E and F) against cand"
              f" `{cand_sha}` (D2 at the chosen defaults). Probe rows: `{S.PROBE_CMD}` with `--ivf 0`"
              " and `--ivf 256`, each arm running its own tree with D2's probe, one discarded base run0"
              " and then 3 interleaved rounds per mode. Bench row: `bench.py --limit 2549619 --engine"
              " raggio --reingest` (2,549,119 x 1024) in the order base, cand, base, cand, each in a"
              " fresh 4 GiB container. D2-flat and D2-bench need not worse (better or within band);"
              " D2-ivf256 needs better.", "",
              "| Row | Regime | Cap | sqlite_version | OPENBLAS_NUM_THREADS | base median | cand median"
              " | gain | band | verdict |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    pl = labels["probe"]
    for m in S.MODES:
        lines.append(S.row(f"probe {m}", S.PROBE_REGIME, pl["cap"][0], ", ".join(pl["sqlite_version"]),
                           ", ".join(pl["openblas_num_threads"]), probe[m]))
    if bench is None:
        lines += ["", NOT_RUN]
    else:
        bl = labels["bench"]
        sv = ", ".join(f"{a} {'/'.join(bl['sqlite_version'][a])}" for a in S.ARMS)
        lines.append(S.row("bench reingest", ", ".join(bl["regime"]), ", ".join(bl["cap"]), sv,
                           ", ".join(bl["openblas_num_threads"]), bench))
    lines += [""] + [f"- {v}: {k}" for k, v in gates.items()]
    data = {"date": date, "base_sha": base_sha, "cand_sha": cand_sha, "tuning": tuning,
            "probe": probe, "bench": bench, "labels": labels, "gates": gates}
    lines += ["", S.FENCE + "json", json.dumps(data, indent=2), S.FENCE]
    return "\n".join(lines) + "\n"


def record(adr, block):
    """Replace the Pending line of the D2 results section; keep the file's line endings."""
    raw = adr.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")
    start = text.index(RESULTS_HEAD)
    ends = [i for i in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if i != -1]
    cut = text.find(PENDING, start, min(ends, default=len(text)))
    if cut == -1:
        raise SystemExit(f"{adr}: no Pending line in the D2 results section; nothing recorded")
    text = text[:cut] + block.rstrip("\n") + text[cut + len(PENDING):]
    adr.write_bytes(text.replace("\n", nl).encode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plan D2 DGX tuning choice, summary and ADR record")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tune").add_argument("results", type=Path)
    pub = sub.add_parser("publish")
    pub.add_argument("results", type=Path)
    pub.add_argument("--tune", type=Path, required=True)
    pub.add_argument("--adr", type=Path)
    args = ap.parse_args(argv)
    tdir = args.results if args.cmd == "tune" else args.tune
    tdate, _, tsha, _ = S.start_facts(tdir)
    tuning, tune_labels = tune_part(tdir)
    if args.cmd == "tune":
        print("\n".join(tune_table(tuning, tune_labels)))
        print(f"run {tdate} tree {tsha} complete {tuning['complete']}"
              f" fingerprints_equal {tuning['fingerprints_equal']} labels_ok {tuning['labels_ok']}")
        print("NO CHOICE" if tuning["choice"] is None else "CHOICE %d %d" % tuple(tuning["choice"]))
        return 1 if tuning["choice"] is None else 0
    date, base_sha, cand_sha, finished = S.start_facts(args.results)
    probe, probe_labels, probe_ok = S.probe_part(args.results)
    bench, bench_labels, bench_ok, _ = S.bench_part(args.results)  # its jobs check is D1's; see bench_jobs_ok
    labels = {"tune": tune_labels, "probe": probe_labels, "bench": bench_labels}
    gates = gates_for(finished, tuning, knobs_ok(args.results, tuning["choice"]), probe, bench,
                      probe_ok and bench_ok, bench_jobs_ok(bench))
    block = render((tdate, tsha), tuning, (date, base_sha, cand_sha), probe, bench, labels, gates)
    print(block)
    if "FAIL" in gates.values():
        print("planD-d2-summary: a gate failed; the ADR is unchanged", file=sys.stderr)
        return 1
    if args.adr:
        record(args.adr, block)
        print(f"planD-d2-summary: recorded in {args.adr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`S.bench_part` still counts a run whose `jobs-run<N>.json` or `meta-run<N>.json` is missing as a label failure, so D2-labels catches it. `S.probe_part` keeps D1's per-mode checks: 3 values per arm, 7 equal fingerprints, every run `{"done": 200}`, the store file of the arm's own tree, one `sqlite_version`, `OPENBLAS_NUM_THREADS=1`.

3c. Create `D:/DEV/_scratch/dgx/planD/planD-d2-check.py`, the offline check of 3a and 3b. It loads `planD-check.py` as a module (its `main` is guarded) to reuse `check_text`, `edit`, `FP` and `TREES`:

```python
#!/usr/bin/env python3
"""Offline checks of the Plan D2 DGX additions. Run from the repo root before any D2 DGX step.

Usage: uv run python D:/DEV/_scratch/dgx/planD/planD-d2-check.py
Checks: planD-check.py's text rules, which cover the edited planD-ab.sh; LF and ASCII in the
two D2 files; the d2tune branch of planD-ab.sh; planD-d2-summary.py on synthetic tuning and
publish dirs (each choice case, PASS, SKIP and each FAIL gate); the ADR edit on a copy.
Prints "Plan D2 DGX scripts OK" or stops at the first failed assert.
"""
import contextlib
import importlib.util
import io
import json
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, module):
    spec = importlib.util.spec_from_file_location(module, HERE / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = load("planD-check.py", "planD_check")
D2 = load("planD-d2-summary.py", "planD_d2_summary")
S = D2.S
TUNE_START = ("== PLAND AB START 2026-10-01 09:00:00 mode=d2tune base=cccccccccccc cand=cccccccccccc"
              " reingest=0 cap=150m ==\n")
START = "== PLAND AB START 2026-10-01 13:00:00 mode=d2 base=aaaaaaaaaaaa cand=cccccccccccc reingest=1 cap=270m ==\n"
DONE = "== PLAND AB DONE 2026-10-01 12:00:00 ==\n"
# per pair, the run-1 vec/s; runs 2 and 3 add 1 and 2, so every band is 2 vec/s
FLAT = {(1, 0): 1000.0, (8, 1000): 1100.0, (32, 1000): 1120.0, (32, 4000): 1110.0}
IVF = {(1, 0): 300.0, (8, 1000): 600.0, (32, 1000): 601.0, (32, 4000): 599.0}


def check_text():
    C.check_text()
    for name in ("planD-d2-summary.py", "planD-d2-check.py"):
        data = (HERE / name).read_bytes()
        assert b"\r" not in data, f"{name}: CRLF line endings"
        assert all(b < 128 for b in data), f"{name}: non-ASCII bytes"
    lines = (HERE / "planD-ab.sh").read_text(encoding="ascii").splitlines()
    assert 'case $MODE in d1|d2|d2tune) ;; *) echo "unknown mode $MODE"; exit 2 ;; esac' in lines
    assert '[ "$MODE" = d2tune ] && IMAGES=""  # host probes only: no image is built before the tuning' in lines
    start = lines.index('if [ "$MODE" = d2tune ]; then')
    end = lines.index("fi", start)
    branch = lines[start:end]
    assert "  exit 0" in branch and '  echo "== PLAND AB DONE $(ts) =="' in branch
    # the tuning branch ends the script before the A/B loop and the reingest phase
    assert end < lines.index('  probe "$m" base 0  # warm-up, discarded') < lines.index('if [ "$REINGEST" != 1 ]; then')
    in_fn = False
    for line in lines:
        in_fn = line.startswith("tune_probe() {") or (in_fn and line != "}")
        if line.lstrip().startswith("#"):
            continue
        if "--seed 7" in line or "--batch-" in line:
            assert in_fn, f"tuning flags outside tune_probe(): {line}"
        assert not (in_fn and "base-src" in line), f"tune_probe() must run the cand tree: {line}"
    assert sum("--seed 42" in ln for ln in lines) == 1  # probe() is Task 6's, unchanged


def fake_tune(tmp, flat=None, ivf=None, done=True):
    """A synthetic ~/planD/d2tune: one (8, 1000) run0 per mode, then 3 rounds of the 4 pairs."""
    vps = {"flat": {**FLAT, **(flat or {})}, "ivf256": {**IVF, **(ivf or {})}}
    d = Path(tmp) / f"tune-{len(list(Path(tmp).iterdir()))}"
    (d / "probe").mkdir(parents=True)
    (d / "ab.log").write_text(TUNE_START + (DONE if done else ""))
    for m in S.MODES:
        for pair, n in [((8, 1000), 0)] + [(p, n) for n in S.ROUNDS for p in D2.PAIRS]:
            r = {"ingest_vps": vps[m][pair] + max(n - 1, 0), "drain_s": 50.0, "loop_stall_s": 0.0,
                 "fingerprint": C.FP, "jobs": {"done": 200}, "payload_rows": 0, "store_file": C.TREES["cand"],
                 "sync_batch_jobs": pair[0], "sync_batch_ms": float(pair[1]), "regime": S.PROBE_REGIME,
                 "sqlite_version": "3.46.1", "openblas_num_threads": "1", "turbovec": "0.0-test"}
            (d / "probe" / f"{m}-n{pair[0]}-t{pair[1]}-run{n}.json").write_text(json.dumps(r))
    return d


def fake_pub(tmp, gains=None, bench=True, done=True, knobs=(8, 1000)):
    """A synthetic ~/planD/d2 that passes every gate: flat and bench within band, ivf256 better."""
    gains = {"flat": 0.0, "ivf256": 100.0, "bench": 0.0, **(gains or {})}
    d = Path(tmp) / f"d2-{len(list(Path(tmp).iterdir()))}"
    (d / "probe").mkdir(parents=True)
    (d / "ab.log").write_text(START + (DONE if done else ""))
    for m in S.MODES:
        for a, rounds in (("base", (0, 1, 2, 3)), ("cand", (1, 2, 3))):
            for n in rounds:
                c = a == "cand"
                r = {"ingest_vps": 1000.0 + n + (gains[m] if c else 0.0), "drain_s": 50.0,
                     "loop_stall_s": 0.0, "fingerprint": C.FP, "jobs": {"done": 200}, "payload_rows": 0,
                     "store_file": C.TREES[a], "sync_batch_jobs": knobs[0] if c else None,
                     "sync_batch_ms": float(knobs[1]) if c else None, "regime": S.PROBE_REGIME,
                     "sqlite_version": "3.46.1", "openblas_num_threads": "1", "turbovec": "0.0-test"}
                (d / "probe" / f"{m}-{a}-run{n}.json").write_text(json.dumps(r))
    for a in S.ARMS:
        b = d / f"bench-{a}"
        b.mkdir()
        for n in (1, 2) if bench else ():
            v = 3000.0 + n + (gains["bench"] if a == "cand" else 0.0)
            (b / f"run{n}-raggio.json").write_text(json.dumps({"raggio": {"ingest_s": 2549619 / v, "ingest_vps": v}}))
            (b / f"jobs-run{n}.json").write_text(json.dumps({"jobs": {"done": 1275}, "payload_rows": 0, "freelist": 0}))
            (b / f"meta-run{n}.json").write_text(json.dumps(
                {"regime": "host-warm", "cap": "4g", "sqlite_version": "3.46.1",
                 "openblas_num_threads": "1", "memory_peak_mb": 3500.0}))
    return d


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = D2.main([str(a) for a in argv])
    return code, out.getvalue()


def check_tune(tmp):
    tuning, labels = D2.tune_part(fake_tune(tmp))
    assert tuning["complete"] and tuning["fingerprints_equal"] and tuning["labels_ok"], tuning
    p = tuning["pairs"]
    assert p["n8-t1000"]["flat"] == [1100.0, 1101.0, 1102.0]  # run0 is not counted
    assert p["n1-t0"]["ivf256_vs_ref"] == "reference" and p["n32-t4000"]["ivf256_vs_ref"] == "better"
    assert labels["cap"] == ["none (uncapped host process)"]
    cases = [  # (flat, ivf) overrides -> the last line
        ({}, {}, "CHOICE 8 1000"),  # (32, 1000) leads on ivf256, but within the band
        ({}, {(32, 4000): 700.0}, "CHOICE 32 4000"),  # beats (8, 1000) beyond the band
        ({(32, 4000): 900.0}, {(32, 4000): 700.0}, "CHOICE 8 1000"),  # flat worse than (1, 0)
        ({(8, 1000): 900.0}, {}, "CHOICE 32 1000"),  # the default does not qualify
        ({}, {p: 300.0 for p in D2.PAIRS}, "NO CHOICE"),  # no batched pair beats (1, 0) on ivf256
    ]
    for flat, ivf, last in cases:
        code, out = run("tune", fake_tune(tmp, flat, ivf))
        assert out.rstrip().endswith("\n" + last) and code == (1 if last == "NO CHOICE" else 0), (last, out)
        assert out.count("\n| ") == 4  # the 4 pair rows below the header and the separator
    breaks = {  # a tuning run that must not yield a choice
        "unfinished": lambda d: (d / "ab.log").write_text(TUNE_START),
        "a missing run": lambda d: (d / "probe/ivf256-n32-t1000-run2.json").unlink(),
        "wrong knobs": lambda d: C.edit(d / "probe/flat-n32-t4000-run1.json", "sync_batch_ms", 1000.0),
        "a fingerprint": lambda d: C.edit(d / "probe/flat-n8-t1000-run0.json", "fingerprint", dict(C.FP, vecs="v1")),
        "an error job": lambda d: C.edit(d / "probe/ivf256-n1-t0-run3.json", "jobs", {"done": 199, "error": 1}),
        "a payload row": lambda d: C.edit(d / "probe/ivf256-n32-t4000-run1.json", "payload_rows", 2),
        "the base tree": lambda d: C.edit(d / "probe/flat-n1-t0-run1.json", "store_file", C.TREES["base"]),
        "OPENBLAS": lambda d: C.edit(d / "probe/flat-n1-t0-run2.json", "openblas_num_threads", "unset"),
    }
    for what, mutate in breaks.items():
        d = fake_tune(tmp)
        mutate(d)
        code, out = run("tune", d)
        assert code == 1 and out.rstrip().endswith("\nNO CHOICE"), (what, out)


def check_publish(tmp):
    tune = fake_tune(tmp)
    code, out = run("publish", fake_pub(tmp), "--tune", tune)
    assert code == 0 and "- FAIL" not in out, out
    for g in ("D2-run", "D2-tuning", "D2-flat", "D2-ivf256", "D2-fingerprints", "D2-jobs", "D2-bench", "D2-labels"):
        assert f"- PASS: {g}\n" in out, g
    assert "Chosen: SYNC_BATCH_JOBS=8, SYNC_BATCH_MS=1000.\n" in out
    data = json.loads(out.split(S.FENCE + "json\n", 1)[1].split("\n" + S.FENCE, 1)[0])
    assert sorted(data) == ["base_sha", "bench", "cand_sha", "date", "gates", "labels", "probe", "tuning"]
    assert data["tuning"]["choice"] == [8, 1000] and data["probe"]["flat"]["verdict"] == "within band"
    assert data["probe"]["ivf256"]["verdict"] == "better" and data["bench"]["verdict"] == "within band"
    code, out = run("publish", fake_pub(tmp, bench=False), "--tune", tune)
    assert code == 0 and "- SKIP: D2-bench\n" in out and D2.NOT_RUN in out, out
    other = fake_tune(tmp, ivf={(32, 4000): 700.0})  # seed 7 chose (32, 4000): the cand must run it
    assert run("publish", fake_pub(tmp, knobs=(32, 4000)), "--tune", other)[0] == 0
    assert "- FAIL: D2-tuning\n" in run("publish", fake_pub(tmp), "--tune", other)[1]
    none = fake_tune(tmp, ivf={p: 300.0 for p in D2.PAIRS})
    assert "- FAIL: D2-tuning\n" in run("publish", fake_pub(tmp), "--tune", none)[1]
    fails = {  # gate -> a mutation of a passing publish dir that must fail it
        "D2-run": lambda d: (d / "ab.log").write_text(START),
        "D2-tuning": lambda d: C.edit(d / "probe/flat-cand-run1.json", "sync_batch_jobs", 32),
        "D2-flat": lambda d: [C.edit(d / f"probe/flat-cand-run{n}.json", "ingest_vps", 990.0 + n) for n in S.ROUNDS],
        "D2-ivf256": lambda d: (d / "probe/ivf256-cand-run3.json").unlink(),
        "D2-fingerprints": lambda d: C.edit(d / "probe/ivf256-base-run0.json", "fingerprint", dict(C.FP, vecs="v1")),
        "D2-jobs": lambda d: C.edit(d / "probe/flat-base-run2.json", "payload_rows", None),
        "D2-bench": lambda d: [C.edit(d / f"bench-cand/run{n}-raggio.json", "raggio", {"ingest_vps": 2900.0 + n})
                               for n in (1, 2)],
        "D2-labels": lambda d: C.edit(d / "bench-base/meta-run1.json", "openblas_num_threads", "unset"),
    }
    for gate, mutate in fails.items():
        d = fake_pub(tmp)
        mutate(d)
        code, out = run("publish", d, "--tune", tune)
        assert code == 1 and f"- FAIL: {gate}\n" in out, (gate, out)
    d = fake_pub(tmp)
    C.edit(d / "bench-cand/jobs-run2.json", "jobs", {"done": 1274, "error": 1})  # an error job in a reingest
    assert "- FAIL: D2-jobs\n" in run("publish", d, "--tune", tune)[1]
    d = fake_pub(tmp)
    C.edit(d / "bench-base/jobs-run1.json", "payload_rows", None)  # D1's base expectation is wrong for D2
    assert "- FAIL: D2-jobs\n" in run("publish", d, "--tune", tune)[1]


def check_adr(tmp):
    real = Path("docs/adr/0001-performance-optimization-decisions.md").read_text(encoding="utf-8")
    assert real.count(D2.RESULTS_HEAD) == 1 and D2.PENDING in real, "run from the repo root, after Task 10 Step 2"
    d1 = S.RESULTS_HEAD + "\n\n" + S.PENDING + "\n"
    text = ("# ADR\n\n## Addendum 2026-09 " + S.DASH + " ingest path\n\n" + d1 + "\n### Batched sync (D2)\n\n"
            "| a | b |\n\n" + D2.RESULTS_HEAD + "\n\n" + D2.PENDING + "\n")
    tune = fake_tune(tmp)
    for nl in ("\n", "\r\n"):
        adr = Path(tmp) / f"adr-{len(nl)}.md"
        adr.write_bytes(text.replace("\n", nl).encode("utf-8"))
        assert run("publish", fake_pub(tmp, {"ivf256": 1.0}), "--tune", tune, "--adr", adr)[0] == 1
        assert adr.read_bytes() == text.replace("\n", nl).encode("utf-8")  # a failed gate records nothing
        assert run("publish", fake_pub(tmp), "--tune", tune, "--adr", adr)[0] == 0
        after = adr.read_bytes().decode("utf-8")
        assert D2.PENDING not in after and d1.replace("\n", nl) in after  # the D1 section is untouched
        assert ("\n" not in after.replace("\r\n", "")) if nl == "\r\n" else ("\r" not in after)
        body = after.replace("\r\n", "\n")
        sec = body[body.index(D2.RESULTS_HEAD):]  # the gate test's slice: the last section
        assert "#### Measurements (D2)" in sec and "- PASS: D2-ivf256\n" in sec
        data = json.loads(sec.split(S.FENCE + "json\n", 1)[1].split("\n" + S.FENCE, 1)[0])
        assert data["gates"]["D2-bench"] == "PASS" and data["cand_sha"] == "cccccccccccc"
        try:
            run("publish", fake_pub(tmp), "--tune", tune, "--adr", adr)
        except SystemExit:
            pass
        else:
            raise AssertionError("a second record must refuse: no Pending line is left")


def main():
    check_text()
    with tempfile.TemporaryDirectory() as tmp:
        check_tune(tmp)
        check_publish(tmp)
        check_adr(tmp)
    print("Plan D2 DGX scripts OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Check the scripts on the laptop**

Run from the repo root, after Step 2's commit and before Step 16 (the check reads the real ADR's D2 `Pending:` line):

Run: `uv run python D:/DEV/_scratch/dgx/planD/planD-d2-check.py`
Expected: `Plan D2 DGX scripts OK`

Run: `uv run python D:/DEV/_scratch/dgx/planD/planD-check.py`
Expected: `Plan D DGX scripts OK`. Task 6's check still passes on the edited driver.

Run: `bash -n D:/DEV/_scratch/dgx/planD/planD-ab.sh && bash -n D:/DEV/_scratch/dgx/planD/planD-build.sh && echo SYNTAX_OK`
Expected: `SYNTAX_OK`

If an assert fails, fix the script it names. Never weaken a check.

- [ ] **Step 5: Pin the SHAs and check the tree**

The base arm is `main` after D1, E and F, which is the parent of Task 7's commit. The candidate is `HEAD`, Step 2's docs commit, so the D2 image carries the docs.

```bash
cd D:/DEV/raggio
T7=$(git log --format=%H -n 1 --fixed-strings --grep="Add batched-sync plumbing: knobs, claim cursor, sync flag, removal cap" HEAD)
BASE_SHA=$(git rev-parse --short=12 "$T7^")
CAND_SHA=$(git rev-parse --short=12 HEAD)
echo "T7=$T7 BASE_SHA=$BASE_SHA CAND_SHA=$CAND_SHA"
git log --oneline -n 1 "$BASE_SHA"
git show "$BASE_SHA:src/raggio/store.py" | grep -c "SYNC_MAX_REMOVALS"
git show "$BASE_SHA:src/raggio/store.py" | grep -c "VACUUM_FREELIST_PAGES"
git status --porcelain --untracked-files=no
```

Expected:
- `T7` is a full SHA (not empty), and the base line is `main`'s last commit before `ingest-path-d2` branched;
- the first `grep -c` prints `0` (the base has no batching) and the second prints at least `1` (the base has D1);
- `git status` prints nothing.

If `T7` is empty or a tracked file is modified, stop and ask.

Run: `uv run pytest -q`
Expected: `N0' + 34 passed, 1 warning`

Write the env file with the caps from Step 1: `TUNE_CAP=150`, and `REINGEST=1 PUB_CAP=270` or `REINGEST=0 PUB_CAP=85`:

```bash
printf 'BASE_SHA=%s\nCAND_SHA=%s\nTUNE_CAP=%s\nREINGEST=%s\nPUB_CAP=%s\n' "$BASE_SHA" "$CAND_SHA" 150 1 270 >D:/DEV/_scratch/dgx/planD/d2-shas.env
cat D:/DEV/_scratch/dgx/planD/d2-shas.env
```

Expected: five lines, and the two SHAs are 12 hex digits each.

- [ ] **Step 6: Read-only preflight on gn100**

This step runs only after the user has granted the window in Step 1. Run it again at the start of each window.

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
uptime
free -m | head -2
pgrep -af "[b]ench\.py|[i]ngest_probe|[p]lanD-ab\.sh" || echo "no bench.py, probe or driver running"
podman ps -a --format "{{.Names}} {{.Status}}"
podman volume exists bench-tv && echo "volume: bench-tv"
df -h ~ | tail -n 1
ls ~/planD 2>/dev/null || echo "no ~/planD yet"'
```

Expected:
- a load average under about 2;
- `no bench.py, probe or driver running`;
- no `bench-tv` line with status `Up` (a `bench-wv` line, in any state, is someone else's: leave it alone);
- `volume: bench-tv` (the reingests need it; the tuning does not);
- at least 20 GB free: two images plus a 1 GB probe work directory.

If something is running, `bench-tv` is up, or the load is high, that is someone else's work in progress. Stop and ask the user, and never stop it yourself. If the volume is missing and `REINGEST=1`, stop and ask before the publish.

- [ ] **Step 7: Stage `~/planD`, copy the scripts and deploy the D2 tree**

An existing `~/planD` holds an earlier session's results (D1's, for one), so move it aside and never delete it. `scp` reads `D:/...` as a host name, so `cd` first and pass relative names.

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'if [ -e ~/planD ]; then mv ~/planD ~/planD.prev-$(date +%Y%m%d-%H%M%S); fi; mkdir -p ~/planD/base-src ~/planD/tmp'
cd D:/DEV/_scratch/dgx/planD
scp -F "$SSH_CONFIG" -o BatchMode=yes planD-ab.sh planD-build.sh gn100:planD/
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'ls -l ~/planD; bash -n ~/planD/planD-ab.sh && bash -n ~/planD/planD-build.sh && echo SYNTAX_OK'
```

Expected: `planD-ab.sh`, `planD-build.sh`, `base-src` and `tmp` are listed, then `SYNTAX_OK`.

Deploy D2's tree to `~/raggio` (spec §6), with the corpus link, the ground truth and the bench venv, exactly as Task 6 did. `~/turborag` is only read.

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planD/d2-shas.env
git archive "$CAND_SHA" | ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'rm -rf ~/raggio && mkdir ~/raggio && tar -x -C ~/raggio'
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
cd ~/raggio || exit 1
ln -sfn ~/turborag/bench/corpus bench/corpus
cp ~/turborag/bench/gt-2549619-42-d1024.npz bench/
~/.local/bin/uv sync --frozen --group bench 2>&1 | tail -2
.venv/bin/python -c "import raggio.store as s; print(\"cand tree OK\", s.SYNC_MAX_REMOVALS, s.SYNC_RETRY_MAX_S)"
grep -c "add_argument(.--batch-" bench/ingest_probe.py'
```

Expected: `cand tree OK 512 30.0`, then `2` (the probe's `--batch-jobs` and `--batch-ms`).

- [ ] **Step 8: Launch the seed-7 tuning (detached, capped), then poll**

No image is needed. `timeout` enforces the cap on the whole process group, and the driver writes its process group id to `ab.pgid`.

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planD/d2-shas.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "setsid -f timeout --kill-after=60 ${TUNE_CAP}m bash ~/planD/planD-ab.sh d2tune $CAND_SHA $CAND_SHA 0 $TUNE_CAP >/dev/null 2>&1 </dev/null"
sleep 90
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'cat ~/planD/d2tune/ab.pgid; head -n 30 ~/planD/d2tune/ab.log'
```

Expected:
- a process group id;
- `== PLAND AB START <date> <time> mode=d2tune base=<CAND_SHA> cand=<CAND_SHA> reingest=0 cap=150m ==`;
- the uptime, memory, `Cached` and `podman ps -a` lines, and no `image planD-...` line;
- `host venv sqlite_version <version> OPENBLAS_NUM_THREADS 1`;
- `labels: probe rows host-warm, uncapped host process; bench rows host-warm, cap 4g`;
- `== tune flat n8 t1000 run0 (<ts>) ==`.

Poll every 5 minutes:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'grep -E "^== (tune|PLAND AB)" ~/planD/d2tune/ab.log | tail -n 3; grep -E "^  vps" ~/planD/d2tune/ab.log | tail -n 2; date'
```

Progress: per mode (flat, then ivf256), `n8 t1000 run0`, then 3 rounds of `n1 t0`, `n8 t1000`, `n32 t1000`, `n32 t4000`: 13 runs, about 65 minutes. Each run is followed by `  vps <n> drain <s> s stall <s> s jobs {'done': 200} knobs <N>/<T> payload_rows 0`, with `<T>` printed as a float (`1000.0`). The end is `== tune done (<ts>) ==` and `== PLAND AB DONE <ts> ==`.

If `== PLAND AB FAILED: <reason>` appears, or the cap passes before `== tune done`, fetch the log (Step 9) and report the reason and the log tail to the user, then stop. The tuning cannot be resumed: a new attempt is a new window, a whole new grid, with `~/planD/d2tune` moved aside first (`mv ~/planD/d2tune ~/planD/d2tune.prev-$(date +%Y%m%d-%H%M%S)`).

If the user asks to stop early, stop only this session's process group. Never use `pkill`:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
PG=$(cat ~/planD/d2tune/ab.pgid)
kill -TERM -- "-$PG" 2>/dev/null
sleep 5
pgrep -af "[p]lanD-ab\.sh|[i]ngest_probe" || echo "no Plan D process left"'
```

Expected: `no Plan D process left`. If a process is still listed, report it to the user and do not kill anything by name.

- [ ] **Step 9: Fetch the tuning and read the choice**

```bash
cd D:/DEV/_scratch/dgx/planD
if [ -e d2tune ]; then mv d2tune d2tune.prev-$(date +%Y%m%d-%H%M%S); fi
scp -r -F "$SSH_CONFIG" -o BatchMode=yes gn100:planD/d2tune .
ls d2tune/probe | wc -l
cd D:/DEV/raggio
uv run python D:/DEV/_scratch/dgx/planD/planD-d2-summary.py tune D:/DEV/_scratch/dgx/planD/d2tune
echo "exit $?"
```

Expected: `26`; the 4-row table with every row labelled `host-warm, uncapped host process`, `none (uncapped host process)`, one `sqlite_version` and `OPENBLAS_NUM_THREADS` `1`; the line `run <date> tree <CAND_SHA> complete True fingerprints_equal True labels_ok True`; then `CHOICE <N> <T>` and `exit 0`.

If the last line is `NO CHOICE` (exit 1), stop. Show the table to the user and do not publish. The D2 results keep their `Pending:` line, and no test is appended. With `complete`, `fingerprints_equal` and `labels_ok` all `True`, it means that no batched pair beat one sync per job on ivf256 beyond the band without losing on flat, on seed 7. Otherwise the run is unusable, and a rerun is a new window.

Record the choice in the env file, taken from the same command's last line:

```bash
cd D:/DEV/raggio
set -- $(uv run python D:/DEV/_scratch/dgx/planD/planD-d2-summary.py tune D:/DEV/_scratch/dgx/planD/d2tune | tail -n 1)
[ "$1" = CHOICE ] && printf 'CHOICE_N=%s\nCHOICE_T=%s\n' "$2" "$3" >>D:/DEV/_scratch/dgx/planD/d2-shas.env
cat D:/DEV/_scratch/dgx/planD/d2-shas.env
```

Expected: seven lines, the last two `CHOICE_N=<N>` and `CHOICE_T=<T>`, equal to the `CHOICE` line.

- [ ] **Step 10: Move the defaults to the choice (only if it is not (8, 1000))**

If `CHOICE_N=8` and `CHOICE_T=1000`, nothing changes: go to Step 11 with the pinned `CAND_SHA`.

Otherwise the defaults, their pins and the docs move in one commit before the publish (spec §6: chosen on seed 7, published on seed 42). The probe builds its `Collection` directly, so the `store.py` defaults must move too; otherwise the publish would measure (8, 1000). Every pattern below matches one line in its file, and none is anchored at the line end, so CRLF checkouts work too:

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planD/d2-shas.env
sed -i "s/\"SYNC_BATCH_JOBS\", \"8\"/\"SYNC_BATCH_JOBS\", \"$CHOICE_N\"/; s/\"SYNC_BATCH_MS\", \"1000\"/\"SYNC_BATCH_MS\", \"$CHOICE_T\"/" src/raggio/config.py
sed -i "s/^        sync_batch_jobs: int = 8,/        sync_batch_jobs: int = $CHOICE_N,/; s/^        sync_batch_ms: float = 1000.0,/        sync_batch_ms: float = $CHOICE_T.0,/" src/raggio/store.py
sed -i "s/== (8, 1000.0)/== ($CHOICE_N, $CHOICE_T.0)/" tests/test_ingest_path.py
sed -i "s/^| \`SYNC_BATCH_JOBS\` | \`8\` |/| \`SYNC_BATCH_JOBS\` | \`$CHOICE_N\` |/; s/^| \`SYNC_BATCH_MS\` | \`1000\` |/| \`SYNC_BATCH_MS\` | \`$CHOICE_T\` |/" README.md docs/getting-started.md
sed -i "s/jobs (default 8), when \`SYNC_BATCH_MS\` ms (default 1000)/jobs (default $CHOICE_N), when \`SYNC_BATCH_MS\` ms (default $CHOICE_T)/" docs/adr/0001-performance-optimization-decisions.md
git diff --stat
```

Expected: `6 files changed, 11 insertions(+), 11 deletions(-)`: two lines each in `config.py`, `store.py` and `README.md`, `docs/getting-started.md` and the test module (Task 7's pins in `test_settings_sync_batch_knobs` and `test_ingest_probe_accepts_batch_flags`), and the one ADR row. If the count differs, run `git checkout -- src/raggio/config.py src/raggio/store.py tests/test_ingest_path.py README.md docs/getting-started.md docs/adr/0001-performance-optimization-decisions.md`, find the moved anchor with `git diff` / `grep -n`, and edit it by hand. The storage docs name no default values.

Run: `uv run pytest tests/test_ingest_path.py -q -k "sync_batch_knobs or accepts_batch_flags"`
Expected: `2 passed` and the rest deselected.

Run: `uv run pytest -q`
Expected: `N0' + 34 passed, 1 warning`

```bash
git add src/raggio/config.py src/raggio/store.py tests/test_ingest_path.py README.md docs/getting-started.md docs/adr/0001-performance-optimization-decisions.md
git commit -F - <<EOF
Set the batched-sync defaults to the seed-7 choice

SYNC_BATCH_JOBS=$CHOICE_N and SYNC_BATCH_MS=$CHOICE_T are the pair Plan D
Task 10's seed-7 grid picked on gn100: better than one sync per job on
the ivf256 ingest probe and not worse on flat, where (8, 1000) either
did not qualify or lost to it on ivf256 beyond the band. The Settings
and Collection defaults, their two pins, the environment tables and
the ADR row follow. The seed-42 publish runs at these defaults.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
sed -i "s/^CAND_SHA=.*/CAND_SHA=$(git rev-parse --short=12 HEAD)/" D:/DEV/_scratch/dgx/planD/d2-shas.env
cat D:/DEV/_scratch/dgx/planD/d2-shas.env
```

Expected: the new commit; `CAND_SHA` is now its 12-digit SHA and the other six lines are unchanged. The heredoc delimiter is unquoted on purpose, so the shell fills in the two numbers.

If the grant covered only the tuning, stop here and report the table and the choice. The publish is a new window.

- [ ] **Step 11: Stage the publish: deploy both trees and build both images**

If the publish has its own window, ask Step 1's publish question again, now naming the choice, and wait for an explicit answer. Then rerun Step 6's preflight. If the answer changes the reingest or the cap, rewrite those two lines (e.g. `sed -i "s/^REINGEST=.*/REINGEST=0/; s/^PUB_CAP=.*/PUB_CAP=85/" D:/DEV/_scratch/dgx/planD/d2-shas.env`). In one shared window, go straight on.

`~/planD` now holds the tuning, so it stays. Only an earlier `d2`, `base-src` or `build.log` inside it is moved aside, and the scripts are copied again (the same files as in Step 7):

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'mkdir -p ~/planD/tmp; for d in d2 base-src build.log; do if [ -e ~/planD/$d ]; then mv ~/planD/$d ~/planD/$d.prev-$(date +%Y%m%d-%H%M%S); fi; done; mkdir ~/planD/base-src'
cd D:/DEV/_scratch/dgx/planD
scp -F "$SSH_CONFIG" -o BatchMode=yes planD-ab.sh planD-build.sh gn100:planD/
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'ls ~/planD; bash -n ~/planD/planD-ab.sh && bash -n ~/planD/planD-build.sh && echo SYNTAX_OK'
```

Expected: `base-src`, `d2tune`, `planD-ab.sh`, `planD-build.sh` and `tmp` (and any `*.prev-*` names), then `SYNTAX_OK`.

Deploy the D2 tree again, because Step 10 may have moved `CAND_SHA` and a separate window may find `~/raggio` changed. Deploy the base tree into `~/planD/base-src` and copy D2's probe into it: it reads the knobs with `getattr`, so it runs on the base and reports `None` there.

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planD/d2-shas.env
git archive "$CAND_SHA" | ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'rm -rf ~/raggio && mkdir ~/raggio && tar -x -C ~/raggio'
git archive "$BASE_SHA" | ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tar -x -C ~/planD/base-src'
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
cd ~/raggio || exit 1
ln -sfn ~/turborag/bench/corpus bench/corpus
cp ~/turborag/bench/gt-2549619-42-d1024.npz bench/
~/.local/bin/uv sync --frozen --group bench 2>&1 | tail -2
cp bench/ingest_probe.py ~/planD/base-src/bench/
.venv/bin/python -c "import inspect, raggio.store as s; p = inspect.signature(s.Collection).parameters; print(\"cand defaults\", p[\"sync_batch_jobs\"].default, p[\"sync_batch_ms\"].default)"
grep -c SYNC_MAX_REMOVALS ~/planD/base-src/src/raggio/store.py
grep -c VACUUM_FREELIST_PAGES ~/planD/base-src/src/raggio/store.py'
echo "want: cand defaults $CHOICE_N $CHOICE_T.0"
```

Expected:
- `cand defaults <N> <T>.0`, the same as the `want:` line (e.g. `cand defaults 8 1000.0`);
- `0`, because the base has no batching;
- at least `1`, because the base has D1.

If the defaults differ from the choice, Step 10 was skipped or not committed at `CAND_SHA`: stop and fix that before building.

Build both images. `planD-build.sh` skips a tag that already exists:

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planD/d2-shas.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "setsid -f bash -c 'bash ~/planD/planD-build.sh ~/planD/base-src planD-$BASE_SHA && bash ~/planD/planD-build.sh ~/raggio planD-$CAND_SHA' >>~/planD/build.log 2>&1 </dev/null"
```

Poll every 2 minutes:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'grep -E "^== BUILD" ~/planD/build.log; tail -n 2 ~/planD/build.log'
```

The build is done when there are two `== BUILD_DONE` lines, for `planD-$BASE_SHA` and `planD-$CAND_SHA`. If a `== BUILD_FAILED` line appears, stop and report the tail of `~/planD/build.log` to the user.

- [ ] **Step 12: Launch the seed-42 publish (detached, capped), then poll**

Task 6's A/B, unchanged, in mode `d2`. The reingests run last, and only with `REINGEST=1` and 180 min left before the cap.

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planD/d2-shas.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "setsid -f timeout --kill-after=60 ${PUB_CAP}m bash ~/planD/planD-ab.sh d2 $BASE_SHA $CAND_SHA $REINGEST $PUB_CAP >/dev/null 2>&1 </dev/null"
sleep 90
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'cat ~/planD/d2/ab.pgid; head -n 40 ~/planD/d2/ab.log'
```

Expected:
- a process group id;
- `== PLAND AB START <date> <time> mode=d2 base=<BASE_SHA> cand=<CAND_SHA> reingest=<0|1> cap=<PUB_CAP>m ==`;
- the uptime, memory, `Cached` and `podman ps -a` lines;
- two `image planD-<sha> id ... digest ...` lines, each followed by `turbovec <version> sqlite_version <version>`;
- `host venv sqlite_version <version> OPENBLAS_NUM_THREADS 1`, the same version as in the tuning;
- `labels: probe rows host-warm, uncapped host process; bench rows host-warm, cap 4g`;
- `== probe flat base run0 (<ts>) ==`.

If the log ends with `== PLAND AB FAILED: <reason>`, go to Step 13 to fetch it. Report the reason and the log tail to the user, then stop.

Poll every 5 minutes:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'grep -E "^== (probe|reingest|PLAND AB)" ~/planD/d2/ab.log | tail -n 3; grep -E "^  (vps|jobs run|labels:)" ~/planD/d2/ab.log | tail -n 2; date'
```

Progress:
- `== probe flat base run0`, then `flat base run1`, `flat cand run1`, and so on to `flat cand run3`, then the same for `ivf256`. Each run is followed by `  vps <n> drain <s> s stall <s> s jobs {'done': 200} payload_rows 0 freelist <n>`. Both arms print `payload_rows 0`, because D2's base already has D1's `job_payloads`;
- `== probes done`, about 70 minutes after the start;
- then either `== reingest skipped: ...`, or `== reingest base run1`, `cand run1`, `base run2`, `cand run2`, each followed by `  jobs run<N>: {"jobs": {"done": <n>}, "payload_rows": 0, "freelist": <n>}` (only `done` jobs, in both arms) and `  labels: host-warm, cap 4g, sqlite_version <v>, OPENBLAS_NUM_THREADS=1, memory.peak <bytes> bytes`;
- finally `== PLAND AB DONE <ts> ==`.

If `== PLAND AB FAILED: <reason>` appears, go to Step 13 to fetch the log. Report the reason and the log tail to the user, and stop: record nothing.

`timeout` ends the driver at the cap by itself. If the user asks to stop early, or the cap has passed and a process is still listed, stop only this session's process group and `bench-tv`. Never use `pkill`, and never touch another container:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
PG=$(cat ~/planD/d2/ab.pgid)
kill -TERM -- "-$PG" 2>/dev/null
sleep 5
pgrep -af "[p]lanD-ab\.sh|[b]ench/bench\.py|[i]ngest_probe" || echo "no Plan D process left"
podman stop -t 60 bench-tv
podman ps -a --filter name=bench-tv --format "{{.Names}} {{.Status}}"'
```

Expected: `no Plan D process left`, then `bench-tv Exited (...)`. If a process is still listed, report it to the user and do not kill anything by name. If the stop hit a reingest (the log's last `==` line is `== reingest ...`), tell the user that the `bench-tv` volume holds a partial `bench` collection until one more reingest runs, and do not start one without a new go-ahead. Then fetch (Step 13), report, and stop.

- [ ] **Step 13: Fetch the publish and preview the summary**

```bash
cd D:/DEV/_scratch/dgx/planD
if [ -e d2 ]; then mv d2 d2.prev-$(date +%Y%m%d-%H%M%S); fi
scp -r -F "$SSH_CONFIG" -o BatchMode=yes gn100:planD/d2 .
ls d2 d2/probe d2/bench-base d2/bench-cand
```

Expected:
- `ab.log`, `ab.pgid`, `bench-base`, `bench-cand`, `probe`;
- 14 files in `probe`, `flat-base-run0.json` through `ivf256-cand-run3.json`;
- with the reingests, per arm: `run1-raggio.json`, `run1-raggio.md`, `jobs-run1.json`, `meta-run1.json`, and the same four for run2. Without them, both arm directories are empty.

Preview from the repo root. Nothing is written yet:

```bash
cd D:/DEV/raggio
uv run python D:/DEV/_scratch/dgx/planD/planD-d2-summary.py publish D:/DEV/_scratch/dgx/planD/d2 --tune D:/DEV/_scratch/dgx/planD/d2tune
echo "exit $?"
uv run python bench/dgx/compare.py D:/DEV/_scratch/dgx/planD/d2/bench-base D:/DEV/_scratch/dgx/planD/d2/bench-cand
```

Expected:
- the `#### Measurements (D2)` block: the tuning table, `Chosen: SYNC_BATCH_JOBS=<N>, SYNC_BATCH_MS=<T>.` (the pair in `d2-shas.env`), the publish rows, `- PASS:` on `D2-run`, `D2-tuning`, `D2-flat`, `D2-ivf256`, `D2-fingerprints`, `D2-jobs` and `D2-labels`, `- PASS:` or `- SKIP:` on `D2-bench`, then `exit 0`;
- with the reingests, Plan A's table, where the `Ingest throughput (vec/s)` row reads `better` or `within band`. Without them, the last command exits with `...bench-base: no run<N>-<engine>.json files`, which is expected.

If any line reads `- FAIL:`, stop here. Show the block to the user and do not go on to Step 14. The D2 results keep their `Pending:` line, and no test is appended. `D2-ivf256` failing means that the gain seen on seed 7 did not hold on seed 42; report it as a result, not as a fault. Never loosen a gate, and never rerun selectively to get a pass. A rerun is a new window: the whole publish again, with `~/planD/d2` moved aside.

- [ ] **Step 14: Write the failing gate test**

Only after Step 13's preview shows no `- FAIL:` line. Append at the very end of `tests/test_ingest_path.py`. The file is ASCII, so the heading's em dash is `chr(0x2014)`. `json`, `Path`, `Settings` and `Collection` come from the module head; `inspect` and `statistics` are imported inside the test. Task 6's `ADR` constant exists only if D1's publish ran, so this section builds its own path.

```python
# ---- ADR 0001 addendum: D2 DGX results

D2_RESULTS = "### Results " + chr(0x2014) + " DGX A/B (D2)"


def adr_d2_results():
    """(the D2 results subsection with \\r\\n normalized, its Measurements JSON or {}).
    The subsection ends at the next ### or ## heading, or at the end of the file."""
    adr = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0001-performance-optimization-decisions.md"
    text = adr.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index(D2_RESULTS)
    ends = [i for i in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if i != -1]
    section = text[start:min(ends, default=len(text))]
    fence = "`" * 3
    if "#### Measurements (D2)" not in section or fence + "json\n" not in section:
        return section, {}
    raw = section.split(fence + "json\n", 1)[1].split("\n" + fence, 1)[0]
    return section, json.loads(raw)


def test_adr_ingest_path_d2_results_pass_the_gates(monkeypatch):
    """Spec sections 6 and 7 D for D2 on gn100: N and T chosen on seed 7; on seed 42, ingest
    vec/s beyond the band on IVF nlist 256 and not worse on flat, every job done with no
    payload row left, one final state across arms; and the shipped defaults are the choice."""
    import inspect
    import statistics

    section, m = adr_d2_results()
    assert "Pending:" not in section
    assert "#### Measurements (D2)" in section and m, "no Measurements (D2) JSON block"
    assert sorted(m) == ["base_sha", "bench", "cand_sha", "date", "gates", "labels", "probe", "tuning"]
    assert m["base_sha"] != m["cand_sha"]

    def gain_band(base, cand, floor=1.0):  # Plan A's rule: band = max(both spreads, resolution)
        band = max(max(base) - min(base), max(cand) - min(cand), floor)
        return statistics.median(cand) - statistics.median(base), band

    def labelled(lab):  # spec section 6: each row names its regime, cap, sqlite and BLAS threads
        return (lab["regime"] == ["host-warm, uncapped host process"] and bool(lab["cap"])
                and lab["openblas_num_threads"] == ["1"] and len(lab["sqlite_version"]) == 1)

    # seed 7: four pairs x 3 runs per mode, and a batched choice that beats (1, 0) on ivf256
    t = m["tuning"]
    assert t["seed"] == 7 and t["complete"] and t["fingerprints_equal"] and t["labels_ok"]
    assert sorted(t["pairs"]) == ["n1-t0", "n32-t1000", "n32-t4000", "n8-t1000"]
    assert all(len(p[mode]) == 3 for p in t["pairs"].values() for mode in ("flat", "ivf256"))
    n, ms = t["choice"]
    assert (n, ms) != (1, 0), "the choice must batch"
    ref, pick = t["pairs"]["n1-t0"], t["pairs"][f"n{n}-t{ms}"]
    gain, band = gain_band(ref["ivf256"], pick["ivf256"])
    assert gain > band and pick["ivf256_vs_ref"] == "better", "seed 7: the ivf256 gain is within the band"
    gain, band = gain_band(ref["flat"], pick["flat"])
    assert -gain <= band and pick["flat_vs_ref"] in ("better", "within band"), "seed 7: flat is worse"
    assert f"Chosen: SYNC_BATCH_JOBS={n}, SYNC_BATCH_MS={ms}.\n" in section
    assert labelled(m["labels"]["tune"])

    # seed 42: base (main after D1, E and F) against D2 at the chosen defaults
    for mode in ("flat", "ivf256"):
        p = m["probe"][mode]
        assert len(p["base"]) == 3 and len(p["cand"]) == 3, mode
        assert p["fingerprints_equal"] and p["jobs_all_done"], mode
        assert set(p["payload_rows"]["base"]) == {0} and set(p["payload_rows"]["cand"]) == {0}, mode
        assert f"- PASS: D2-{mode}\n" in section
    ivf, flat = m["probe"]["ivf256"], m["probe"]["flat"]
    gain, band = gain_band(ivf["base"], ivf["cand"])
    assert gain > band and ivf["verdict"] == "better", "probe ivf256: the gain is within the band"
    gain, band = gain_band(flat["base"], flat["cand"])
    assert -gain <= band and flat["verdict"] in ("better", "within band"), "probe flat: worse"
    assert labelled(m["labels"]["probe"])
    b = m["bench"]
    if b is None:  # probe-only window, or not enough time left for the reingests
        assert "Not run: the bench reingest" in section and "- SKIP: D2-bench\n" in section
    else:
        assert len(b["base"]) == 2 and len(b["cand"]) == 2 and len(b["runs"]) == 4
        gain, band = gain_band(b["base"], b["cand"])
        assert -gain <= band and b["verdict"] in ("better", "within band"), "bench reingest: worse"
        for name, run in b["runs"].items():
            assert set(run["jobs"]) == {"done"} and run["payload_rows"] == 0, name  # no error job
        lb = m["labels"]["bench"]
        assert lb["regime"] == ["host-warm"] and lb["cap"] == ["4g"] and lb["openblas_num_threads"] == ["1"]
        assert all(len(v) == 1 for v in lb["sqlite_version"].values())
        assert "- PASS: D2-bench\n" in section
    for gate in ("D2-run", "D2-tuning", "D2-fingerprints", "D2-jobs", "D2-labels"):
        assert f"- PASS: {gate}\n" in section, gate
    assert "- FAIL:" not in section

    # what ships is what was measured: the defaults and the documented rows are the choice
    monkeypatch.delenv("SYNC_BATCH_JOBS", raising=False)
    monkeypatch.delenv("SYNC_BATCH_MS", raising=False)
    s = Settings()
    assert (s.sync_batch_jobs, s.sync_batch_ms) == (n, float(ms))
    params = inspect.signature(Collection).parameters
    assert (params["sync_batch_jobs"].default, params["sync_batch_ms"].default) == (n, float(ms))
    root = Path(__file__).resolve().parents[1]
    for doc in ("README.md", "docs/getting-started.md"):
        text = (root / doc).read_text(encoding="utf-8")
        assert f"| `SYNC_BATCH_JOBS` | `{n}` |" in text and f"| `SYNC_BATCH_MS` | `{ms}` |" in text, doc
```

- [ ] **Step 15: Run it to verify that it fails**

Run: `uv run pytest tests/test_ingest_path.py -v -k adr_ingest_path_d2`
Expected: `FAILED tests/test_ingest_path.py::test_adr_ingest_path_d2_results_pass_the_gates - AssertionError: assert 'Pending:' not in '### Results — DGX A/B (D2)\n\nPending: the gn100 A/B of Plan D Task 10 has not run yet.\n'` (pytest may shorten the quoted section), then `1 failed` and the rest deselected. The failure is on the first assert, because the ADR still holds Step 2's `Pending:` line.

- [ ] **Step 16: Record the results in the ADR**

Run from the repo root. The summary replaces only the `Pending:` line of `### Results — DGX A/B (D2)`, and only when no gate reads `FAIL`. It keeps the file's line endings.

```bash
cd D:/DEV/raggio
uv run python D:/DEV/_scratch/dgx/planD/planD-d2-summary.py publish D:/DEV/_scratch/dgx/planD/d2 --tune D:/DEV/_scratch/dgx/planD/d2tune --adr docs/adr/0001-performance-optimization-decisions.md
git diff --stat
```

Expected: the block from Step 13 again, then `planD-d2-summary: recorded in docs/adr/0001-performance-optimization-decisions.md`. `git diff --stat` lists only the ADR and `tests/test_ingest_path.py`. `git diff docs/adr/0001-performance-optimization-decisions.md` shows one removed line (`Pending: ...`) and the added block, and no change above `### Results — DGX A/B (D2)`.

If the summary exits 1 now, a result file changed after the preview. Stop and report to the user. Do not edit the ADR by hand, and revert the test (`git checkout -- tests/test_ingest_path.py`).

From here on, `planD-d2-check.py` fails its `check_adr` (the real ADR no longer holds the D2 `Pending:` line). That is expected: Step 4 runs it before this step only.

- [ ] **Step 17: Run the test to verify that it passes**

Run: `uv run pytest tests/test_ingest_path.py -v -k adr_ingest_path_d2`
Expected: `PASSED tests/test_ingest_path.py::test_adr_ingest_path_d2_results_pass_the_gates`, then `1 passed` and the rest deselected (`62 deselected` if Task 6's test landed, `61 deselected` if it did not).

Run: `uv run pytest tests/test_ingest_path.py -q -k adr_ingest_path_d1`
Expected: `1 passed, 62 deselected` if Task 6's run landed, or `62 deselected` if it did not. The D1 slice still ends at `\n### Batched sync (D2)`.

Run: `uv run pytest tests/test_cold_start.py -q -k acceptance_run`
Expected: `1 passed, 64 deselected`, or `64 deselected` if Plan C's Task 12 test did not land, unchanged from Step 2.

- [ ] **Step 18: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0' + 35 passed, 1 warning`. Only this task's test is new.

- [ ] **Step 19: Commit, then report to the user**

```bash
git add docs/adr/0001-performance-optimization-decisions.md tests/test_ingest_path.py
git commit -F - <<'EOF'
Record the D2 gn100 tuning and A/B results in ADR 0001

Replace the Pending line of "Results - DGX A/B (D2)" with the measured
block. The seed-7 tuning ran the D2 tree alone over (SYNC_BATCH_JOBS,
SYNC_BATCH_MS) = (1, 0), (8, 1000), (32, 1000), (32, 4000), flat and
IVF nlist 256, 3 rounds after a discarded warm-up, and chose the
defaults. The seed-42 publish ran main after D1, E and F against D2 at
those defaults: in-process ingest probe rows (3 interleaved rounds per
arm after a discarded warm-up) and, when they ran, the end-to-end bench
--reingest rows (2,549,119 x 1024, 4 GiB container, 2 interleaved runs
per arm). Every row is labelled with its regime, cap, sqlite_version
and OPENBLAS_NUM_THREADS, and a "Measurements (D2)" JSON block follows.

Add test_adr_ingest_path_d2_results_pass_the_gates. It re-checks each
gate from that JSON with Plan A's band rule (IVF ingest vec/s beyond
the band and flat not worse, on both seeds; every job done with no
payload row left; equal fingerprints across arms), and pins the
Settings and Collection defaults and both environment tables to the
choice.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

Then report to the user:
- the seed-7 tuning table and the choice, and whether Step 10 moved the defaults. State the shipped `SYNC_BATCH_JOBS` / `SYNC_BATCH_MS` values in one line that D2's PR description can quote, since the PR states them (spec §3.1 D1);
- the seed-42 flat and IVF probe medians, gains and bands;
- the bench reingest rows, or that they did not run;
- the `sqlite_version` per arm, and `OPENBLAS_NUM_THREADS=1` everywhere;
- the state the runs left on gn100: `~/raggio` holds D2's tree at `CAND_SHA`; `bench-tv` is stopped, and its volume is flat and drained, written by the D2 image (or untouched when the reingests were skipped);
- `~/planD/d2tune` and `~/planD/d2` kept on gn100 (plus any `*.prev-*` moved aside), and the local copies in `D:/DEV/_scratch/dgx/planD/`.

**Review Focus for Task 10:** the header assigns none of RF1-RF5 to this task. Its own pins are `test_adr_ingest_path_d2_results_pass_the_gates` (appended only after a completed publish) and Task 7's `test_settings_sync_batch_knobs` and `test_ingest_probe_accepts_batch_flags` (the default pins that Step 10 moves). Task 6's `test_adr_ingest_path_d1_results_pass_the_gates` and Plan C's `test_adr_records_the_acceptance_run` must stay green with the D2 subsections after them. When reviewing, check that:
- nothing ran on gn100 before the user's explicit window answer (Step 1), and the preflight ran at the start of each window. Both launches ran under `timeout` with the granted `TUNE_CAP` / `PUB_CAP`, and every `ssh` and `scp` uses the full `-F "$SSH_CONFIG" -o BatchMode=yes` form;
- the edited `planD-ab.sh` still passes Task 6's `check_text`: no `bench-wv`, `turborag`, `pkill`, `sudo` or `rm -rf`; `podman rm -f` only as `podman rm -f bench-tv`; `--reingest` only inside `reingest()`. `d2tune` runs only `~/raggio` at `--seed 7`, passes the batch flags only inside `tune_probe`, and exits before the A/B loop and the reingest phase. The seed-42 A/B passes no batch flag. `planD-d2-check.py` asserts all of this;
- seed 7 only chooses and seed 42 only publishes (spec §6). The publish cand runs at its defaults: Step 11 checks them with `inspect.signature` before the build, and `knobs_ok` checks every publish probe file (base `None`, cand the choice). A publish at (8, 1000) after seed 7 chose another pair fails `D2-tuning`;
- the choice rule (Step 3b) needs ivf256 `better` and flat `better` or `within band` against (1, 0), and keeps (8, 1000) unless the winner beats it on ivf256 beyond the band. `NO CHOICE`, or a tuning that is incomplete, mislabelled or has unequal fingerprints, stops the task before any default moves;
- Step 10 moves every default site in one commit before the images are built: `config.py`, `store.py`, both of Task 7's pins, both env rows and the ADR's knob row. The D2 test re-pins the `Settings` and `Collection` defaults and both env rows to the recorded choice, so a later default change without a new tuning fails the suite;
- the publish gates: `D2-ivf256` needs `better`; `D2-flat` and `D2-bench` need `better` or `within band`; `D2-jobs` needs 0 payload rows in both arms, because D2's base already has D1's `job_payloads` (Task 6's `None` rule does not apply), and bench jobs only `done`; the fingerprints must be equal. A `FAIL` records nothing, and no threshold is loosened. The test recomputes the bands from the same rounded values the JSON stores;
- every row, the tuning rows included, names its regime (`host-warm, uncapped host process` for the probes, `host-warm` for the bench), its cap (none, or `4g`), its `sqlite_version` and `OPENBLAS_NUM_THREADS`;
- the docs match the code: a job turns `done` only after its covering sync, a crash before a batch's sync replays the whole batch and one during its finishes replays only the jobs not yet `done`, a shutdown whose last sync fails logs it, still closes and replays that batch (the storage.md sentence and the ADR's `stop()` row, spec §3.1 D2), and so does one that finds a dead worker or fails a finish (the same two places, spec §3.1 D3), a close step that fails is logged and the shutdown goes on to the next step and the next collection (the same two places, spec §3.1 D4), the Backup sentence also covers the shard files under `ivf/`, and the README and getting-started rows agree on names, defaults and meaning. `adr_d1_results()` still ends at `\n### Batched sync (D2)`, and `#### Measurements (D2)` does not end the slice of `adr_d2_results()`;
- declined, tuning-only and probe-only windows are handled. The D2 results keep `Pending:` when the window is declined, when only the tuning ran, and on `NO CHOICE` or a gate `FAIL`. A probe-only publish records `Not run: the bench reingest ...` with `- SKIP: D2-bench`.

## Self-Review

The plan was checked against the spec (`docs/superpowers/specs/2026-09-28-raggio-performance-design.md`) and against Plans A, B, C, E, F and G at the merge order A, B, C, D1, E, F, D2, G. The check was done by reading only. Nothing was run; see "Validation status" below.

### Spec coverage (D-owned items)

| Spec item | What D owes | Task(s) |
|---|---|---|
| §2 row 4 (D1) | payload encode/decode off the loop | Task 1 (codec), Task 2 (`to_thread` wiring) |
| §2 row 4 (D1) | upsert ordering and rollback | Task 3 |
| §2 row 4 (D1) | `indexed_counts` after commit | Task 3 |
| §2 row 4 (D1) | binary job journal that still replays legacy TEXT JSON | Task 1 (`job_payloads`, codec), Task 2 (enqueue, claim, mixed-queue replay) |
| §2 row 4 (D1) | vacuum policy | Task 4 |
| §2 row 7 (D2) | batched worker and batched index sync | Task 7 (knobs, claim cursor, sync flag, removal cap), Task 8 (batch loop) |
| §2 row 7 (D2) | done-after-sync semantics | Task 8 |
| §2 row 7 (D2) | flush in `stop()` | Task 9 |
| §2 row 7 (D2) | the change at ADR 0001:50 | Task 10 |
| §3 D1 (ADR numbering) | no new ADR number; one `## Addendum 2026-09 — ingest path` in ADR 0001; the 0001:50 lever note | Task 6 (addendum, D1 results), Task 10 (D2 subsections, :50 note) |
| §3 D8 | job stays `processing` with its payload until its covering sync; N-job / T-ms bounds; `stop()` flush; crash replay | Task 8 (bounds, done-after-sync, crash-window test), Task 9 (retry, `stop()` flush) |
| §3.1 D1 | the seed-7 tuning may move the `SYNC_BATCH_JOBS` / `SYNC_BATCH_MS` defaults; seed 42 confirms the chosen values; the PR states them | Task 10 (Steps 3b and 10, the publish at the chosen defaults, and Step 19's report line for the PR) |
| §3.1 D2 | a failed final sync in `stop()` is logged, the shutdown completes, nothing is raised, and the jobs stay `processing` with their payloads and replay | Task 9 (`_sync_and_finish`, `test_stop_with_a_failing_sync_logs_it_and_still_closes`, `test_a_failed_stop_sync_replays_on_the_next_open`), Task 10 (the ADR's `stop()` row, the storage.md replay paragraph) |
| §3.1 D3 | a dead worker's exception and a finish that fails after the final sync landed are logged, the shutdown completes, and the jobs stay `processing` and replay | Task 9 (the `except Exception:` clause in `stop()`, the finish catch in `_sync_and_finish`, `test_stop_logs_a_dead_workers_exception_and_still_closes`, `test_a_failed_finish_in_stop_is_logged_and_replays`, `test_shutdown_closes_every_collection_after_a_failed_stop`), Task 10 (the ADR's `stop()` row, the storage.md replay paragraph) |
| §3.1 D4 | each close step in `stop()` (`_close_conns` under `db_lock`, the embedder's `aclose()`, F's `_shard_pool.close`) logs its own `Exception` with its traceback, and the next step still runs; only `Exception` is caught | Task 9 (`close_step` in `stop()`, `test_a_failing_embedder_close_is_logged_and_stop_goes_on`, `test_a_failing_close_conns_is_logged_and_the_later_steps_run`, `test_a_failing_shard_pool_close_is_logged`, and the embedder close that fails in `test_shutdown_closes_every_collection_after_a_failed_stop`), Task 10 (the ADR's `stop()` row, the storage.md replay paragraph) |
| §3 D15 / D16 | every DGX row records `sqlite_version` and `OPENBLAS_NUM_THREADS` | Task 5 (probe output), Tasks 6 and 10 (`*-labels` gates) |
| §4.1 `Collection.stop()` | flush inside B's `lock.write()` / `db_lock` section, before the connections close | Task 9 |
| §4.1 `indexed_counts` | update after commit, keep B's copy-on-write rebind | Task 3 |
| §4.1 `open_meta_db` indexes | the claim keeps the literal predicate `status IN ('pending','processing')`; `resume_pending` and `pending_jobs` untouched | Task 2 (C's text byte for byte), Task 7 (`INDEXED BY idx_jobs_open`, `AND id > ?`) |
| §4.1 D-owned surfaces | job journal format, `enqueue`, `_run_worker`, `_upsert_rows`, `_sync_index` batching | Tasks 1-3 (D1), Tasks 7-8 (D2) |
| §4.1 `_IvfIndex` (F) | use F's dirty-shard API when F merged first (it does: F precedes D2) | Task 8, through `_sync_index` only; `test_batched_ivf_sync_writes_each_dirty_shard_once` |
| §4.1 `bench/bench.py` (A) | add a probe file only, never edit bench.py | Task 5 (`bench/ingest_probe.py`); Tasks 6 and 10 call bench.py with A's flags |
| §4.2 knobs | `SYNC_BATCH_JOBS`, `SYNC_BATCH_MS` in `Settings`, passed through `CollectionManager` into `Collection` | Task 7 (defaults 8 / 1000); Task 10 may move the defaults after the seed-7 tuning |
| §4.2 new tests | own module | `tests/test_ingest_path.py`, every task |
| §4.3 single writer | every write transaction in `db_lock`, in a worker thread | Tasks 2, 3, 8 |
| §4.3 auto_vacuum | `incremental_vacuum` drained with `.fetchall()` | Task 4 (`_vacuum_after_finish`) |
| §4.3 job journal | `done` only after the covering sync | Task 8 (Task 9 keeps it under sync failure) |
| §4.3 memory | headroom guard unchanged | none (C's; D touches neither `_attach_index` nor `_detach_index`) |
| §5 findings | none assigned to D | none |
| §6 protocol | go-ahead first, fresh container per arm, interleaved rounds, seed 7 chooses and seed 42 publishes, `--reingest` last | Tasks 6 and 10 |
| §7 acceptance (D) | ingest vec/s beyond the band on IVF and flat; no lost or unsynced job under crash/replay | Tasks 6 and 10 (DGX gates), Tasks 2, 3 and 8 (replay and rollback tests); see open item 1 for D2's flat gate |

### Placeholder scan

Searched for `TBD`, `TODO`, `FIXME`, `XXX`, `...` used as elided code, "similar to Task", "add appropriate", "fill in" and "etc.". Nothing is left in any step. The remaining hits are intended:
- `b"XXXX"` in Task 1's and Task 2's tests is a deliberately bad magic value, used as test data;
- "and so on" appears only inside descriptions of expected output, never in place of code;
- `Pending:` in ADR 0001 is a real line that Tasks 6 and 10 write and later replace with measurements; the tests use it as a sentinel.

### Name consistency

Every name below is spelled the same way in each task that defines or uses it, and in the header's File Structure. A search for near variants (`_flush_batch`, `SYNC_BATCH_N`, `_removals_since_sync`, `planD-d2summary`, and so on) found none.

| Name | Defined in | Used in |
|---|---|---|
| `job_payloads(job_id INTEGER PRIMARY KEY, data BLOB)`, `_JOB_MAGIC = b"RGJ\x01"`, `_JOB_HEADER = struct.Struct("<4sIII")`, `_encode_payload`, `_decode_payload` | Task 1 | Tasks 2, 3, 6, 8, 10 |
| `_enqueue_row`, `_CLAIM_SQL`, `_claim_next` -> `(id, payload or None, error or None)` | Task 2 | Tasks 3, 7 (`after=0` cursor), 8 |
| `_payload_rows`, `_rows_matrix`, `_upsert_rows` -> `(ids, fresh, replaced)`, `_unindex`, `_delete_doc_rows` | Task 3 | Tasks 7, 8 |
| `VACUUM_FREELIST_PAGES = 16_384`, `VACUUM_CHUNK_PAGES = 2_048`, `_vacuum_after_finish` | Task 4 | Task 7 (anchor), Task 8 |
| `SYNC_MAX_REMOVALS = 512`, `Collection._removed_since_sync`, `_process_job(payload, *, sync=True)` | Task 7 | Tasks 8, 9 |
| `Collection._unsynced`, `Collection._batch_t0`, `_flush_unsynced` | Task 8 | Task 9 |
| `SYNC_RETRY_MIN_S = 1.0`, `SYNC_RETRY_MAX_S = 30.0`, `_sync_and_finish` | Task 9 | Task 10 (docs) |
| `close_step(what, step)`, local to `stop()`, and its three step names `"the SQLite connections"`, `"the embedder's HTTP client"`, `"the IVF shard pool"` (the log's second arg) | Task 9 | Task 9 (the D4 tests and the shutdown test assert the args) |
| Task 9's ten test helpers: `LogRecords`, `failing_sync`, `wait_for_retries`, `job_states`, `closed_state`, `disk_rows`, `stop_with_open_batch`, `stop_with_failing_sync`, `flaky_finish`, `EmbedderStub(fail)` (`aclose()`, `calls`) | Task 9 | Task 9 |
| Knobs: env `SYNC_BATCH_JOBS` / `SYNC_BATCH_MS` (spec §4.2); `Settings.sync_batch_jobs` / `sync_batch_ms`; `Collection(..., sync_batch_jobs=8, sync_batch_ms=1000.0)` | Task 7 | Task 10 (env rows, tuning, default moves) |
| Probe: `bench/ingest_probe.py`, `--batch-jobs` / `--batch-ms`, result keys `sync_batch_jobs` / `sync_batch_ms` | Task 5 (file), Task 7 (flags) | Tasks 6, 10 |
| Test module `tests/test_ingest_path.py`, one `# ---- <topic>` section per task, helpers `make_collection`, `vec`, `docs_payload`, `drain` | Task 1 | every task |
| Branches `ingest-path-d1` (base: main after A, B, C) and `ingest-path-d2` (base: main after D1, E, F) | phase headers | Tasks 6, 10 |
| ADR 0001: `## Addendum 2026-09 — ingest path`, `### Results — DGX A/B (D1)`, `#### Measurements (D1)`, `### Batched sync (D2)`, `### Results — DGX A/B (D2)`, `#### Measurements (D2)`, the line-50 lever note | Task 6 (addendum, D1), Task 10 (D2, line 50) | the D1 and D2 results tests |
| Scratch scripts in `D:/DEV/_scratch/dgx/planD/`: `planD-ab.sh` (modes `d1`, `d2tune`, `d2`), `planD-build.sh`, `planD-summary.py`, `planD-check.py`, `planD-d2-summary.py`, `planD-d2-check.py`; gn100 output dirs `~/planD/d1`, `~/planD/d2tune`, `~/planD/d2` | Task 6 (first four, `d1`), Task 10 (the rest) | Tasks 6, 10 |
| Gate names `D1-run`, `D1-flat`, `D1-ivf256`, `D1-fingerprints`, `D1-jobs`, `D1-bench`, `D1-labels`, and `D2-run`, `D2-tuning`, `D2-flat`, `D2-ivf256`, `D2-fingerprints`, `D2-jobs`, `D2-bench`, `D2-labels` | Task 6 (D1 set), Task 10 (D2 set) | the summary scripts, the check scripts, the ADR tests |

Names D takes from other plans, checked against those plans' text:
- A: `bench.py --limit 2549619 --caps-note --host --fingerprint-dir ~/raggio-bench/state --engine raggio --reingest --out`, the move of `results-partial.json`, and `compare.py`'s `summary`, `verdict` and `RESOLUTION["ingest_vps"]` (1.0). 2,549,119 is the ingested row count and is never passed as `--limit`.
- B: `lock.write()`, `db_lock`, `_closed`, `_close_conns` (which empties `_read_conns`), the `stop()` body, and `_delete_doc_rows` as B leaves it.
- C: `idx_jobs_open` and the claim text; `_construct` via `asyncio.to_thread`; the `malloc_trim(0)` `finally` in `_process_job`; `_log = logging.getLogger("uvicorn.error")`.
- E: the `native_bm25` keyword in `Collection.__init__` and `_construct`; the `Settings.native_bm25` line.
- F: `ivf_search_threads`, `_shard_pool` (a `_ShardPool` whose `close()` sets `_closed = True`), `_IvfIndex.sync(directory) -> int`, `dirty_shards`, `_invalidate_allow`, and the tests `test_one_row_job_syncs_one_shard` and `test_reload_then_stop_writes_no_shards`.

### Cross-plan check (merge order A, B, C, D1, E, F, D2, G)

- **A.** Tasks 6 and 10 call `bench.py` only with A's flags, and move `results-partial.json` as A does. The band rule is `compare.py`'s `verdict` with `RESOLUTION["ingest_vps"] = 1.0`. D adds `bench/ingest_probe.py` and does not edit `bench.py` (spec §4.1).
- **B.** D2's `stop()` text is B's body plus F's additions, word for word and in the same order, except for three changes in Task 9: its `_sync_index` line is swapped for `_sync_and_finish`, the worker await's `except asyncio.CancelledError:` gains an `except Exception:` sibling that logs, and each of the three close steps is awaited through the local `close_step` helper. The call stays inside `lock.write()`, after the worker cancel and before `_closed` and `_close_conns` (spec §4.1). A failing sync no longer propagates out of `stop()` as it did in B: `_sync_and_finish` logs it and returns, so `stop()` completes its close (spec §3.1 D2). Neither does a dead worker's exception nor a failed finish (spec §3.1 D3), nor a close step that fails, after which the next step still runs (spec §3.1 D4). So no `Exception` from the worker, the final sync, a finish or a close step leaves `stop()`, and B's `shutdown()` loop reaches every later collection and the catalog; `CancelledError` and other non-`Exception` `BaseException`s still propagate. B's `stop()` tests never fail that sync, kill the worker, fail a finish in `stop()` or fail a close, and none expects `stop()` to raise, so they pass unchanged. B's close-order spy records the lock state inside `close()` and is asserted after `stop()` returns, so `close_step` cannot hide a failure there. D's `_delete_doc_rows` anchor is B's text, and B's spy test still sees `[True, True]`.
- **C.** D1's `_CLAIM_SQL` is C's claim text byte for byte. D2 adds only `INDEXED BY idx_jobs_open` after the table name and `AND id > ?` after the predicate, and the predicate is unchanged. C must not add `INDEXED BY` itself, because D owns the claim text. C's claim test (`_claim_next()[0]` on a legacy `"{}"` payload) still holds with D2's default cursor `after=0`. D does not touch `resume_pending` or `pending_jobs`. D also leaves alone C's `_require_headroom` placement in `_attach_index` / `_detach_index`, the `malloc_trim(0)` `finally` in `_process_job`, and `_construct`'s `asyncio.to_thread` call; D2 only appends keyword arguments to it.
- **E.** E anchors nothing in `_upsert_rows`, `_delete_doc_rows`, `_sync_index` or `_process_job`. E's tests call `_process_job({"documents": [...]})` without the optional keys, and `_payload_rows` reads those keys with `.get`, as base does. Task 3 publishes `indexed_counts` with B's copy-on-write rebind after the commit, still under `db_lock`. So the `N` that E passes to `_bm25_rescore` is always a committed count, and D changes nothing on E's read side.
- **F, both orders.** At the spec's order F merges before D2. D2 syncs only through `_sync_index`, so F's dirty-shard `_IvfIndex.sync` writes each dirty shard once per batch (`test_batched_ivf_sync_writes_each_dirty_shard_once`). D2 does not use `sync()`'s `int` return. If D2 ever merged first, that test skips (it checks for `dirty_shards`), and the flat and whole-index sync paths behave as at base. F's own clause for "D2 first" covers its side. F's `test_one_row_job_syncs_one_shard` still holds, because the removal-cap pre-sync does not fire at counter 0. So does `test_reload_then_stop_writes_no_shards`: `_sync_and_finish` on an empty batch writes no shard. Task 9's `closed_state` helper reads F's `_shard_pool` through `getattr` and counts a missing pool as closed, so the spec §3.1 D2, D3 and D4 tests also hold if D2 merged first, and `test_a_failing_shard_pool_close_is_logged` skips (it checks for `_ShardPool`). In that order, D2's `stop()` has no pool step, and F's Task 1 Step 3j appends its plain `await asyncio.to_thread(self._shard_pool.close)`, which F's plan does not know to wrap: whoever applies F after D2 must append it as `await close_step("the IVF shard pool", asyncio.to_thread(self._shard_pool.close))` (Task 9 Step 3 (d)). A plain line would let a failing pool close raise out of `stop()` again, and `test_a_failing_shard_pool_close_is_logged`, which stops skipping once `_ShardPool` exists, would fail on it. F's plan is not edited for this, since the spec's order runs F first. F's `_allow_cache.clear()` to `_invalidate_allow()` edit is on lines D2 does not touch.
- **G.** G has no shared-state audit list, so D has nothing to add to one. D adds no `sqlite3.connect` site, so G's `cached_statements=` edit covers D's queries, `_CLAIM_SQL` included. G's Plan D expectation (G's cross-plan list: the `stop()` flush runs before the meta.db connections close) holds, per Task 9. See the next section for the free-threading check.
- **ADR numbering (spec D1).** D takes no new ADR number. Task 6 appends `## Addendum 2026-09 — ingest path` after C's addendum and its `### Acceptance run` block. Task 10 adds D2's subsections at the file's end and rewrites the line-50 lever note. F edits ADR 0001 lines 30, 48 and 49 in place and adds nothing after D1's addendum, so line 50 is D's alone.
- **Knobs (spec §4.2).** `SYNC_BATCH_JOBS` and `SYNC_BATCH_MS` are unprefixed and read in `Settings`, after F's `ivf_search_threads` line. `CollectionManager._construct` passes them into `Collection`. Algorithm constants (`VACUUM_*`, `SYNC_MAX_REMOVALS`, `SYNC_RETRY_*`) stay module-level in `store.py`.

Fixes made during this check:
- in this plan:
  - the claim wording in the Global Constraints, File Structure and Task 2 now names `INDEXED BY idx_jobs_open` along with `AND id > ?`;
  - the `_require_headroom` location in the Global Constraints is corrected;
  - the D2 flat-gate rationale is added to the Global Constraints;
  - F's clean-stop test is named;
  - the Task 10 script list in File Structure is completed;
  - three places in Task 10 said "2,549,619 rows"; they now give the ingested count, 2,549,119;
- in Plan F: line 578 (the `Settings` anchor) now says D1 adds no `Settings` line and D2 appends its `SYNC_BATCH_*` lines below F's.

### Free-threading (Plan G)

G merges last and runs the suite on 3.14t. D's new shared state is safe without the GIL, because each item has exactly one writer at a time and a happens-before edge:
- `Collection._unsynced` and `Collection._batch_t0` (Task 8) are read and written only by the worker coroutine on the event loop thread. `_finish_job` runs in a thread but gets the job's fields, not the list. The one other user is `stop()`'s `_sync_and_finish` (Task 9), a thread that runs under `lock.write()` after the worker has been cancelled or has died (spec §3.1 D3), and `stop()` has awaited it. From then on the worker has ended: its only handler is `except asyncio.CancelledError: raise`, and it does not touch either attribute again. A job that `_flush_unsynced` already popped belongs to the orphaned finish thread, and it is not in the list any more. The `asyncio.to_thread` submit and the future's result give happens-before in both directions.
- `Collection._removed_since_sync` (Task 7) is written only under `lock.write()`: counted in `_unindex`, reset in `_sync_index`, and read by `_process_job`'s cap check. `__init__` writes it once, single-threaded. Consecutive write sections run in different pool threads, and the lock handoff on the loop orders them.
- `indexed_counts` (Task 3) keeps B's copy-on-write rebind and is published after the commit, under `db_lock`. Readers take one local reference.
- `_encode_payload` and `_decode_payload` are pure functions. `_CLAIM_SQL`, `VACUUM_*`, `SYNC_MAX_REMOVALS` and `SYNC_RETRY_*` are immutable module constants.
- D opens no new SQLite connection and adds no module-level mutable state.

### Validation status

- Nothing in this plan has been executed. This self-review, and the earlier review passes over Tasks 1-6 (D1) and Tasks 7-10 (D2), were done by reading: every anchor was compared with the base `store.py` and with the text of Plans A, B, C, E and F that it quotes. Each test's expected failure message, pass count (`N0 + k`, `N0' + k`) and deselection count was traced by hand. No `pytest`, `py_compile` or shell step ran.
- N0 and N0' are measured on each branch base before the first task, and are never hard-coded. N0 is 253, or 252 if Plan C's Task 12 test did not land.
- The DGX results are `Pending:` by design until the user grants a window: Task 6 for D1, Task 10 for D2. Tasks 6 and 10 hold the gates, and no threshold can be loosened afterwards.
- To confirm on the first run:
  - Task 7's `test_claim_query_never_scans_the_journal` checks that `INDEXED BY idx_jobs_open` plans as one `SEARCH` on the SQLite the suite runs with. The DGX rows record each arm's `sqlite_version`. SQLite rejects a statement whose `INDEXED BY` cannot be honoured, so a wrong plan fails loudly rather than slowly;
  - Task 8's crash-window test runs at all three points.

### Open items for the user

1. **D2's flat and bench gates.** Spec §7 asks for ingest vec/s "beyond the noise band on the IVF and flat paths". D1 gates flat, IVF and the bench reingest on `better`. D2 gates IVF on `better`, but flat and the bench reingest only on `better` or `within band`. The reason: a flat sync is append-only and costs 7-9 ms, 2-5% of a job (item2a), so batching has almost nothing to save there. Either accept this reading of §7 for D2, or require `better` on flat. Requiring it would likely leave D2's results unpublishable.
2. **D1 window.** If the long bench-reingest part of the D1 window (about 3.5 h in total) is declined, is a probe-only result (`- SKIP: D1-bench`) an acceptable D1 claim?
3. **D2 defaults.** Decided (spec §3.1 D1): the seed-7 tuning may change the `SYNC_BATCH_JOBS=8` / `SYNC_BATCH_MS=1000` defaults, seed 42 confirms the chosen values, and the PR states them. Task 10 moves them in one commit (Step 10) before the seed-42 publish, and Step 19's report gives the line for D2's PR description.
4. **Downgrade.** A database written by D1 or later keeps the payloads of open jobs as binary rows in `job_payloads`, which older code cannot replay. The plan documents "drain the queue before downgrading" but does not enforce it.
5. **Finish transactions.** D2 keeps one `_finish_job` transaction per job, which keeps B's pinned behaviour and job-level replay. item2a suggested one finish transaction per batch, which means fewer commits but replays the whole batch after a crash during the finishes.
6. **`stop()` on a failing sync.** Decided (spec §3.1 D2): the failure is logged and the shutdown completes, nothing is raised, and the jobs stay `processing` with their payloads and replay on the next open (D8). Task 9's `_sync_and_finish` does this. Also decided (spec §3.1 D3): a dead worker's exception (logged in `stop()`'s `except Exception:` clause) and a finish that fails after the final sync landed (logged in `_sync_and_finish`) are handled the same way, and their jobs replay. And decided (spec §3.1 D4): each close step (`_close_conns` under `db_lock`, the embedder's `aclose()`, F's `_shard_pool.close`) logs its own failure through `close_step`, and the next step still runs. So `stop()` never raises an `Exception` from its worker, its final sync, a finish or a close step, and `shutdown()` reaches every collection and the catalog. Only `Exception` is caught: `CancelledError`, `KeyboardInterrupt` and `SystemExit` propagate. A failed `_close_conns` leaves the connections it did not reach to the garbage collector; D4 does not make B's method close each connection on its own.
