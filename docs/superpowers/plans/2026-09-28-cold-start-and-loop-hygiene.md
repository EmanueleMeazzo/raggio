# Cold Start and Loop Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a 2.55M-chunk collection under a 4 GiB cap:
- load in seconds without blocking the event loop;
- answer `GET /collections/{name}` in under a second;
- build and drop an IVF index without being OOM-killed in a swapless container, and without minutes of cold pre-work.

**Architecture:**
- **Indexes (D9).** `open_meta_db` replaces `idx_records_doc` with the covering `idx_records_doc_type ON records(doc_id, type, indexed)`. It also adds the partial `idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')`.
  - The one-time migration builds the new index before it drops the old one, and logs start, a heartbeat and the finish.
  - The listing page query is pinned to a table scan with `NOT INDEXED`, so the covering index cannot slow down the sorted listing.
- **Id-set diffs.** Ghost reconciliation and the attach/detach swap compare id sets with `np.setdiff1d` over `uint64` arrays. The live ids are read through the covering index by one helper, `_live_ids`.
- **Off the loop.**
  - `CollectionManager.touch()` builds `Collection(...)` on a worker thread while it holds `_load_lock`. A cancelled request never orphans a half-registered load, and a collection that is already resident is served without waiting on the lock.
  - `GET /collections/{name}` runs `stats()` in a thread.
- **Sampling.** `_vec_sample` point-reads random rowids when ids are dense, and falls back to `ORDER BY RANDOM()` when they are sparse.
- **Index jobs.**
  - The headroom guard runs first, before any SQL, with the need scaled to the measured growth.
  - `malloc_trim(0)` runs after every index job, and the image sets `ENV MALLOC_TRIM_THRESHOLD_=134217728` (D13).
  - Rows without a retained vector are found from the build stream itself. The full `records` × `vecs` anti-join no longer runs before each build.

**Tech Stack:**
- Python 3.12, asyncio, sqlite3 (WAL, partial and covering indexes, `NOT INDEXED`, progress handler)
- numpy (`setdiff1d`, `fromiter`), turbovec `IdMapIndex`
- ctypes (glibc `malloc_trim`), logging via the `uvicorn.error` logger
- FastAPI, pytest with `caplog` and httpx `ASGITransport`, uv
- DGX gn100 with rootless podman

**Spec:** `docs/superpowers/specs/2026-09-28-raggio-performance-design.md`. Sections used:
- §2 row 3 (plan C scope);
- §3 D9, D12, D13;
- §4.1, the rows owned by C;
- §4.2 and §4.3;
- §5.1, §5.2, §5.4, §5.5;
- §6 (DGX protocol);
- §7 C thresholds 1–9.

**Briefs:**
- `docs/superpowers/research/2026-09-28-dgx-probe-p1-coldstart-sqlite-memory.md` (p1: every bracketed number below).
- `docs/superpowers/research/2026-09-28-dgx-baseline.md` (baseline3).
- The p1 prototype generator `D:/DEV/_scratch/dgx-probes/p1-coldstart-sqlite-memory/scripts/mkplanc.py`. It is evidence only (spec §8); this plan carries the real code.

**Depends on:** Plan A and Plan B merged. This plan consumes the following from B:
- `delete_collection` runs under `_load_lock`;
- `get_config` and `list_collections` run under `_catalog_lock`;
- `stop()` runs under `lock.write()`;
- `indexed_counts` is copy-on-write: take one reference, then read it.

**Validation status:** nothing in this plan has been executed.
- The plan-C sandbox `D:/DEV/_scratch/plan-sandbox/plan-C` holds only the base commit 19a8a2e, and no code, test or command below was run while writing it.
- The code shapes that ran before are:
  - covering index, partial index, numpy reconcile, `to_thread` construction, `stats()` in a thread and rowid sampling ran on gn100 as the p1 prototype, 2.55M rows, SQLite 3.40.1;
  - the index-choice EXPLAIN facts in Task 2 were measured on SQLite 3.47.1 without `sqlite_stat1` (brief 2b).
- Everything else is unexecuted: the migration logging, the cancel-safe `touch()`, the guard order, the stream-based missing-vector detection, every test, and the DGX acceptance scripts of Tasks 11–12 (their laptop check included).

## Global Constraints

These are copied from the spec, one requirement per line:

- "Every plan ships as its own branch/PR. It must leave `uv run pytest -q` green and must not depend on a later plan."
- "It is a one-time migration inside `open_meta_db`, so it runs inside the off-loop `Collection()` construction and logs its progress. Ship a release note. No lazy background build." (D9)
- "C pins that query with `NOT INDEXED` or unary `+`, and adds an EXPLAIN-plan regression test." (D9)
- "Evict the collection's files from the page cache with `os.posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)` on every file in the data dir. Verify the eviction with `fincore`/mincore. Run it via `podman unshare`. No sudo or reboot is needed." (D12)
- "Set `ENV MALLOC_TRIM_THRESHOLD_=134217728` in the image, and call `malloc_trim(0)` (ctypes, glibc only, no-op elsewhere) after every index job. No `MALLOC_ARENA_MAX`." (D13)
- "Run `_require_headroom` **before** the backfill/COUNT pre-work, and raise the attach need to the measured growth (about 2.2 GiB)." (D13)
- "The release gate is a swapless run (`--memory 4g --memory-swap 4g`). Owner C, including the one ENV line in the Dockerfile." (D13)
- "`open_meta_db` indexes: C. `idx_records_doc_type ON records(doc_id, type, indexed)` replaces `idx_records_doc`, and a partial `idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')`." (§4.1)
- "D's claim query, `resume_pending` and `pending_jobs` keep the literal predicate `status IN ('pending','processing')`." (§4.1)
- "`_reconcile_ghosts`, `stats()`, `CollectionManager.touch()` construction, `_vec_sample`, `_require_headroom` / memory release: C" (§4.1)
- "`_backfill_vecs` and the attach/detach pre-work order (guard first): C" (§4.1)
- "C's off-loop construction builds on B's locking." (§4.1)
- "`_IvfIndex` (search, remove, sync), `_vector_ids`, `_allow_cache`: F". C calls `_IvfIndex.train/add_with_ids/remove/sync/prepare` unchanged and never edits them.
- "Algorithm constants stay module-level in `store.py`, where tests monkeypatch them." (§4.2)
- "New tests: each plan adds its own module (`tests/test_<plan-topic>.py`). Plans don't reorganize existing test files." (§4.2) This plan's module is `tests/test_cold_start.py`.
- "Single writer: every write transaction runs wholly inside `db_lock`, in a worker thread, never on the event loop." (§4.3)
- "Reads: loop-side reads use per-thread read connections." (§4.3)
- "auto_vacuum: `PRAGMA auto_vacuum=INCREMENTAL` comes before `journal_mode=WAL`, and `incremental_vacuum` is drained with `.fetchall()`." (§4.3)
- "Ghost reconciliation: it only evicts index ids that have no DB row, and never re-adds." (§4.3)
- "Job journal: a job is `done` only after the index sync covering it." (§4.3)
- "Memory: the cgroup headroom guard refuses the job rather than letting the container OOM." (§4.3)
- "The asyncio primitives created in `__init__` must be safe to create off-loop; verify each one." (§5.1)
- "Also audit the other request paths that scan `records`, such as filtered/sorted listings and filtered allowlists. They take 43–54 s cold at 4 GiB and must stay off the loop. An expression index for them is a follow-up, not part of C." (§5.2)
- "The guard stays conservative; the swapless run is the gate." (§5.4)
- "Owner C: rowid sampling, a `_backfill_vecs` that avoids the full `records` scan, and the swap scan on the covering index. The DGX runbook times one cold attach." (§5.5)
  - p1 names two ways to avoid the scan: an anti-join over the covering index, or skipping the backfill when `COUNT(vecs)` equals the indexed count.
  - Task 9 uses a third way instead. The rows missing a vector are the live ids (read from the covering index) minus the ids the build stream already read, so an index job runs no anti-join and no `COUNT`.
  - The full anti-join still runs in one case: the training sample comes back short, which only happens on a pre-retention collection.
  - This deviates from p1's wording but not from the spec's requirement. Task 12 measures it (threshold 9).
- "Any DGX run over about 5 minutes needs the user's explicit go-ahead in the session, with a time cap." (§6)
- "`podman rm -f` may only name `bench-tv`, never `bench-wv` (the Weaviate container, whose `bench-wv` volume must be preserved)." (§6)
- "Every row also states its memory cap and `sqlite3.sqlite_version` (D15)." Every row also records `OPENBLAS_NUM_THREADS` and its page-cache regime. (§6)
- "launch long runs with `setsid -f` into a log file; poll the log; never `pkill -f` a pattern that matches the polling command itself; never stop or delete the user's DGX work unasked." (§6)

The following are harness and repository rules:

- Tests pass on Windows (dev) and Linux (CI). Code that needs glibc, ctypes or POSIX is guarded, and so are the tests that exercise it.
- DGX access is only through `ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100`. `SSH_CONFIG` is the maintainer's local ssh config for gn100: set in the environment, never committed.
- On the DGX: never touch `~/turborag` (read-only use of its corpus symlink target, as §6 prescribes, is allowed), and no `sudo`.
- Line numbers are those of base 19a8a2e. After A and B they shift, so every Modify step names the function and quotes the exact text it replaces.
- **Suite count.** Before Task 1, run `uv run pytest -q` on the branch base and record the pass count as `N0`. After Plans A and B it is 188 (`188 passed, 1 warning`: A's 162+9 = 171 plus B's 17, per Plan A's suite-count note), so after Task 9 it is 252. Each task states its expected total as `N0 + k`; the one Starlette deprecation warning stays throughout.
- Every commit ends with `-m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`.

## Review Focus

These are the five input classes or failure modes most likely to slip through. Each one is pinned by a named test in its owning task.

1. **A request cancelled mid-load (Task 4).**
   - The failure: a client disconnects while `Collection(...)` builds on a worker thread. A naive `await asyncio.to_thread(...)` releases `_load_lock` at once while the thread keeps building. The next request then builds a second copy. The first copy is orphaned, holding open SQLite handles, and its replayed jobs have no worker.
   - Pin: `test_cancelled_load_still_registers_the_built_collection`.
2. **The page query plan across every scope × sort × filter (Task 2).**
   - The failure: the covering index makes the planner walk `idx_records_doc_type` for the sorted listing (p1: 0.91 → 2.73 s warm), and `ANALYZE` does not fix it.
   - Pin: `test_listing_page_query_never_uses_the_covering_index`. It runs `EXPLAIN QUERY PLAN` on the SQL that `list_records` actually executes, for all 18 combinations.
3. **An interrupted or repeated migration (Task 1).**
   - The failure: the process is killed after `CREATE INDEX` and before `DROP INDEX`, or an upgraded file is reopened.
   - Pins: `test_interrupted_migration_drops_the_old_index_on_reopen` and `test_legacy_db_migrates_once_and_logs`. The second one checks that the second open is silent.
4. **`_vec_sample` inputs (Task 6).**
   - The inputs: sparse ids (density below 0.2 after mass deletes), `k ≥ n/2`, records without a retained vector, and bias toward low ids.
   - Pins: `test_vec_sample_sparse_ids_fall_back`, `test_vec_sample_k_at_least_half_returns_all_rows`, `test_vec_sample_never_returns_rows_without_vectors`, `test_vec_sample_is_uniform_over_ids`.
5. **Legacy rows without a vector once the pre-scan is gone (Tasks 8–9).**
   - The failure: missing vectors are now found after the stream, not before the build. A text-backed legacy row could be silently left out of the new index, and a text-less one must still fail the job. A guard refusal must do zero SQL.
   - Pins: `test_attach_backfills_rows_the_stream_missed`, `test_attach_fails_after_the_stream_for_textless_rows`, `test_detach_refuses_rows_without_vectors`, `test_attach_refusal_does_no_prework`.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/raggio/store.py` | Modify | Imports: `ctypes`, `itertools`, `logging`, `sys`. |
| | | Constants, after `IVF_BUILD_BLOCK` (line 78): `MIGRATION_LOG_EVERY_S`, `MIGRATION_PROGRESS_OPS`, `VEC_SAMPLE_MIN_DENSITY`, `VEC_SAMPLE_ROUNDS`, `ATTACH_NEED_FACTOR`, `_log`. |
| | | Helpers `_attach_need` and `_malloc_trim`, after `_require_headroom` (lines 99–108). Helper `_live_ids`, after `_rows_by_id` (lines 167–172). |
| | | `open_meta_db` (273–320) and the new `_ensure_indexes` after it. |
| | | Methods: `_reconcile_ghosts` (628–648), `_process_job` (742–749), `_vec_sample` (909–917), `_backfill_vecs` (919–951), `_attach_index` (953–1012), `_detach_index` (1014–1061), `list_records` (1525–1560). |
| | | `CollectionManager.touch` (1694–1717) and the new `_construct`. |
| `src/raggio/app.py` | Modify | `collection_info` (146–150): `stats()` runs in a thread. |
| `Dockerfile` | Modify | Runtime stage: one `ENV MALLOC_TRIM_THRESHOLD_=134217728` line (D13). |
| `tests/test_cold_start.py` | Create (Task 1), then append (Tasks 2–9, 12) | Every new test of this plan. |
| `docs/adr/0001-performance-optimization-decisions.md` | Modify | Line 33 correction, plus a new `## Addendum 2026-09 — cold start and loop hygiene` at the end (Task 10), and its `### Acceptance run` subsection (Task 12). |
| `docs/storage.md` | Modify | New `## Upgrading` release note and a memory-release note. |
| `docs/indexing.md` | Modify | Lines 60–65 (rebuild memory) and 102–105 (legacy rows). |
| `D:/DEV/_scratch/dgx/planC/c_check.py`, `c_evict.py`, `c_probe.py`, `c_session.sh`, `c_verdict.py` | Create (outside the repo, Task 11); used by Task 12 | DGX acceptance runbook: the laptop check, D12 eviction, in-image probes, the gn100 session and the §7 C verdict. |

---

### Task 1: Covering and partial indexes, and the one-time migration

**Files:**
- Modify: `src/raggio/store.py`:
  - lines 1–21, imports: add `import logging`;
  - lines 75–78, after `IVF_BUILD_BLOCK`: add the migration constants and `_log`;
  - lines 273–320, `open_meta_db`: remove line 305 and call `_ensure_indexes`;
  - new `_ensure_indexes`, directly after `open_meta_db`.
- Create: `tests/test_cold_start.py` (shared header and helpers, plus this task's 5 tests).

**Interfaces:**
- Consumes:
  - `open_meta_db(path: Path, tokenizer: str = "unicode61") -> sqlite3.Connection`
  - `Collection(cfg, directory, embedder_factory, set_index_config=None)`
  - `Collection.pending_jobs() -> int`
  - `Collection._claim_next() -> tuple | None`
  - `Collection._rdb() -> sqlite3.Connection`
- Produces:
  - `MIGRATION_LOG_EVERY_S: float = 10.0`
  - `MIGRATION_PROGRESS_OPS: int = 1_000_000`
  - `_log = logging.getLogger("uvicorn.error")`
  - `_ensure_indexes(db: sqlite3.Connection, label: str) -> None`
  - Indexes `idx_records_doc_type ON records(doc_id, type, indexed)` and `idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')`; `idx_records_doc` is gone.
  - Test helpers used by Tasks 2–9: `FakeEmbedder`, `make_collection`, `rowvec`, `ingest`, `plan`, `Recorder`, `record_sql`, `make_manager`, `index_names`, `make_legacy_db`, and the autouse fixture `small_ivf`.

- [ ] **Step 0: Record the suite baseline**

Run: `uv run pytest -q`. Write down the pass count as `N0`; every later task states its expected total relative to it. (After Plans A and B it is `188 passed, 1 warning`. If it differs, record what you see: only the relative `N0 + k` counts matter.)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cold_start.py`:

```python
"""Plan C: cold start and loop hygiene. The meta.db indexes and their one-time
migration, pinned query plans, the numpy ghost diff, off-loop collection
construction, rowid vector sampling, memory release, and the index-job pre-work
order. The checks are structural (EXPLAIN plans, SQL traces, thread identity), not
timings, so the suite stays deterministic on Windows and Linux."""
import asyncio
import ctypes
import logging
import sqlite3
import sys
import threading
import time
import zlib
from pathlib import Path

import httpx
import numpy as np
import pytest

import raggio.store as store
from raggio.app import create_app
from raggio.config import Settings
from raggio.store import (
    Collection,
    CollectionConfig,
    CollectionManager,
    IdMapIndex,
    _IvfIndex,
    open_meta_db,
)

DIM = 8
ROOT = {"x-api-key": "root-key"}
LOG = "uvicorn.error"  # the logger plan C logs through (uvicorn's handler in the container)


class FakeEmbedder:
    """Deterministic: same text -> same unit vector."""

    async def embed(self, texts):
        out = []
        for t in texts:
            v = np.random.default_rng(zlib.crc32(t.encode())).standard_normal(DIM)
            out.append((v / np.linalg.norm(v)).tolist())
        return out

    async def aclose(self):
        pass


def make_collection(tmp_path, embedder=None) -> Collection:
    return Collection(
        CollectionConfig("t", DIM, 4, None, None, None), Path(tmp_path),
        lambda: embedder or FakeEmbedder(),
    )


def rowvec(i):
    v = np.random.default_rng(1000 + i).standard_normal(DIM)
    return (v / np.linalg.norm(v)).tolist()


def ingest(col, n, start=0, meta=None):
    docs = [
        {"doc_id": f"d{i}", "chunks": [
            {"id": f"c{i}", "text": f"chunk {i}", "vector": rowvec(i),
             "metadata": (meta or {}).get(i)},
        ]}
        for i in range(start, start + n)
    ]
    asyncio.run(col._process_job({"documents": docs}))


def plan(db, sql, params=()) -> str:
    """EXPLAIN QUERY PLAN details joined with ' | ' (rows are id, parent, notused, detail)."""
    return " | ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, list(params)))


class Recorder:
    """Proxy for a sqlite3 connection that records every execute() as (sql, params)."""

    def __init__(self, db, log):
        self._db, self.log = db, log

    def execute(self, sql, params=()):
        self.log.append((sql, list(params)))
        return self._db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._db, name)


def record_sql(col) -> list:
    """Trace every statement the collection runs on its read connections."""
    log, real = [], col._rdb
    col._rdb = lambda: Recorder(real(), log)
    return log


def make_manager(tmp_path, monkeypatch) -> CollectionManager:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    return CollectionManager(Settings(), lambda cfg: FakeEmbedder())


@pytest.fixture(autouse=True)
def small_ivf(monkeypatch):
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)


@pytest.fixture(autouse=True)
def uvicorn_logs_reach_caplog(monkeypatch):
    # any test in the session that builds a uvicorn.Config applies uvicorn's
    # LOGGING_CONFIG, which sets propagate=False on "uvicorn"; caplog listens on the
    # root logger, so without this the caplog tests would depend on test order
    for name in ("uvicorn", LOG):
        monkeypatch.setattr(logging.getLogger(name), "propagate", True)


def index_names(db) -> set:
    # sql IS NULL filters out sqlite_autoindex_* (the external_id UNIQUE constraint)
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    )}


def make_legacy_db(path: Path, rows: int = 50) -> None:
    """A meta.db in the pre-plan-C layout: idx_records_doc, neither new index."""
    db = open_meta_db(path)
    db.execute("DROP INDEX IF EXISTS idx_records_doc_type")
    db.execute("DROP INDEX IF EXISTS idx_jobs_open")
    db.execute("CREATE INDEX IF NOT EXISTS idx_records_doc ON records(doc_id)")
    db.executemany(
        "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
        " VALUES (?,?,?,?,?,?,?,1)",
        [(i, f"c{i}", f"d{i % 7}", "chunk", i, f"chunk {i}", "{}") for i in range(1, rows + 1)],
    )
    db.commit()
    db.close()


# ---- Task 1: indexes and the one-time migration (D9) ----


def test_fresh_meta_db_has_the_covering_and_partial_indexes(tmp_path):
    db = open_meta_db(tmp_path / "meta.db")
    names = index_names(db)
    assert {"idx_records_doc_type", "idx_jobs_open"} <= names
    assert "idx_records_doc" not in names  # its (doc_id) prefix lives on in the covering index
    cols = [r[2] for r in db.execute("PRAGMA index_info(idx_records_doc_type)")]
    assert cols == ["doc_id", "type", "indexed"]
    partial = {r[1]: r[4] for r in db.execute("PRAGMA index_list(jobs)")}
    assert partial["idx_jobs_open"] == 1
    db.close()


def test_legacy_db_migrates_once_and_logs(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOG)
    path = tmp_path / "meta.db"
    make_legacy_db(path)
    db = open_meta_db(path)
    assert "one-time migration" in caplog.text
    assert "built idx_records_doc_type" in caplog.text
    assert "idx_records_doc" not in index_names(db)
    assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 50
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()
    caplog.clear()
    open_meta_db(path).close()  # already migrated: a silent no-op
    assert "idx_records_doc_type" not in caplog.text


def test_interrupted_migration_drops_the_old_index_on_reopen(tmp_path):
    path = tmp_path / "meta.db"
    make_legacy_db(path)
    db = sqlite3.connect(path)  # the process died after CREATE INDEX, before DROP INDEX
    db.execute("CREATE INDEX idx_records_doc_type ON records(doc_id, type, indexed)")
    db.close()
    db = open_meta_db(path)
    names = index_names(db)
    assert "idx_records_doc" not in names
    assert {"idx_records_doc_type", "idx_jobs_open"} <= names
    db.close()


def test_migration_heartbeat_logs_progress(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(store, "MIGRATION_LOG_EVERY_S", 0.0)
    monkeypatch.setattr(store, "MIGRATION_PROGRESS_OPS", 100)
    caplog.set_level(logging.INFO, logger=LOG)
    path = tmp_path / "meta.db"
    make_legacy_db(path, rows=2000)
    open_meta_db(path).close()
    assert "still building idx_records_doc_type" in caplog.text


def test_open_job_queries_use_the_partial_index(tmp_path):
    col = make_collection(tmp_path)
    with col.db_lock:
        col.db.executemany(
            "INSERT INTO jobs(id, payload, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            [(i, "{}", "done", "", "") for i in range(1, 51)]
            + [(51, "{}", "processing", "", ""), (52, "{}", "pending", "", ""),
               (53, "{}", "error", "", "")],
        )
        col.db.commit()
    db = col._rdb()
    # the literal predicate D's claim query, pending_jobs and resume_pending keep (§4.1)
    count = "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
    claim = ("SELECT id, payload FROM jobs WHERE status IN ('pending','processing')"
             " ORDER BY id LIMIT 1")
    assert "idx_jobs_open" in plan(db, count), plan(db, count)
    assert "idx_jobs_open" in plan(db, claim), plan(db, claim)
    assert col.pending_jobs() == 2
    assert col._claim_next()[0] == 51  # lowest open id, 'processing' replays first
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cold_start.py -v`
Expected: `5 failed`:
- `test_fresh_meta_db_has_the_covering_and_partial_indexes`, on `assert {"idx_records_doc_type", "idx_jobs_open"} <= names`;
- `test_legacy_db_migrates_once_and_logs`, on `assert "one-time migration" in caplog.text`;
- `test_interrupted_migration_drops_the_old_index_on_reopen`, on `assert "idx_records_doc" not in names`;
- `test_migration_heartbeat_logs_progress`, with `AttributeError: <module 'raggio.store' ...> has no attribute 'MIGRATION_LOG_EVERY_S'`;
- `test_open_job_queries_use_the_partial_index`, on `assert "idx_jobs_open" in plan(db, count)`.

- [ ] **Step 3: Minimal implementation**

In `src/raggio/store.py`, replace the import line

```python
import json
```

with

```python
import json
import logging
```

Replace

```python
IVF_BUILD_BLOCK = 16_384  # rows per streamed rebuild block (~100 MB f32 at 1536-d)
```

with

```python
IVF_BUILD_BLOCK = 16_384  # rows per streamed rebuild block (~100 MB f32 at 1536-d)

# one-time meta.db index migration (D9): heartbeat cadence of the progress log, and
# how many SQLite VM instructions run between progress-handler checks
MIGRATION_LOG_EVERY_S = 10.0
MIGRATION_PROGRESS_OPS = 1_000_000

# uvicorn configures this logger with its stderr handler, so these lines reach
# `podman logs`; under pytest the records propagate to the root logger (caplog)
_log = logging.getLogger("uvicorn.error")
```

In `open_meta_db`, delete the line

```python
        CREATE INDEX IF NOT EXISTS idx_records_doc ON records(doc_id);
```

and replace

```python
            updated_at TEXT
        );
        """
    )
    _ensure_fts(db, tokenizer)
```

with

```python
            updated_at TEXT
        );
        """
    )
    _ensure_indexes(db, str(path))
    _ensure_fts(db, tokenizer)
```

Directly after `open_meta_db` (before `def _ensure_fts`), add:

```python
def _ensure_indexes(db: sqlite3.Connection, label: str) -> None:
    """Secondary indexes, plus the one-time migration from the pre-2026-09 layout (D9).
    idx_records_doc_type covers every doc_id/type/indexed scan (cold start's per-type
    counts and live-id set, stats(), the per-document lookups) without reading the
    records table's text pages, and replaces idx_records_doc, whose (doc_id) prefix it
    contains. Build first, drop second: a kill in between leaves both, and the next
    open drops the old one. idx_jobs_open is partial, so the open-job lookups stop
    scanning the whole job journal. Runs inside Collection() construction, which is
    off the event loop (CollectionManager._construct)."""
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if "idx_records_doc_type" not in have:
        rows = db.execute("SELECT MAX(rowid) FROM records").fetchone()[0]
        t0 = last = time.monotonic()
        if rows:
            _log.info(
                "meta.db %s: one-time migration, building idx_records_doc_type over"
                " ~%d records (~16 s per 1M rows cold)", label, rows,
            )

            def beat() -> int:
                nonlocal last
                now = time.monotonic()
                if now - last >= MIGRATION_LOG_EVERY_S:
                    last = now
                    _log.info(
                        "meta.db %s: still building idx_records_doc_type (%.0f s)", label, now - t0
                    )
                return 0  # non-zero would abort the CREATE INDEX

            db.set_progress_handler(beat, MIGRATION_PROGRESS_OPS)
        try:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_doc_type ON records(doc_id, type, indexed)"
            )
        finally:
            db.set_progress_handler(None, 0)
        if rows:
            _log.info(
                "meta.db %s: built idx_records_doc_type in %.1f s", label, time.monotonic() - t0
            )
    if "idx_records_doc" in have:
        db.execute("DROP INDEX idx_records_doc")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_open ON jobs(id)"
        " WHERE status IN ('pending','processing')"
    )
```

pysqlite runs DDL in autocommit when no transaction is open (Python ≥ 3.6 never opens one implicitly for DDL). Each statement is therefore durable on its own, which is what makes the build-then-drop order crash-safe.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cold_start.py -v`
Expected: `5 passed`.

If only the `claim` assertion fails, stop and report the `plan(db, claim)` output. D owns the claim query's text (§4.1), so this plan must not add `INDEXED BY` or reword it. The COUNT pin and the functional assertions still have to pass.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 5 passed`. `tests/test_ivf_index.py`, `tests/test_api.py` and `tests/test_store_optimizations.py` (the sibling, delete and patch paths that used `idx_records_doc`) stay green.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_cold_start.py
git commit -m "Replace idx_records_doc with a covering index and add a partial open-jobs index" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Pin the sorted-listing plan, and pin every other records scan

**Files:**
- Modify: `src/raggio/store.py:1525-1560` (`list_records`: the comment and the page query).
- Modify: `tests/test_cold_start.py` (append a section).

**Interfaces:**
- Consumes:
  - `Collection.list_records(scope, filt, sort, limit, offset, include_vector=False) -> dict`
  - the test helpers `plan`, `record_sql`, `ingest`, `make_collection` (Task 1)
- Produces: the page query `SELECT id FROM records NOT INDEXED WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?`. The return shape is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cold_start.py`:

```python
# ---- Task 2: pinned query plans (D9) ----

LIST_META = {i: {"year": 2000 + i % 5, "src": "wiki" if i % 2 else "arxiv"} for i in range(30)}


@pytest.mark.parametrize("scope", ["chunks", "summaries", "both"])
@pytest.mark.parametrize("sort", [None, "year", "-year"])
@pytest.mark.parametrize("filt", [None, {"src": "wiki"}])
def test_listing_page_query_never_uses_the_covering_index(tmp_path, scope, sort, filt):
    # p1: with idx_records_doc_type the planner walks the index in doc_id order and
    # fetches every row by rowid (0.91 -> 2.73 s warm at 2.55M rows); EXPLAIN the SQL
    # list_records really runs, for every page shape
    col = make_collection(tmp_path)
    ingest(col, 30, meta=LIST_META)
    log = record_sql(col)
    col.list_records(scope, filt, sort, 20, 0)
    page = [(s, p) for s, p in log if "LIMIT ? OFFSET ?" in s]
    assert len(page) == 1
    detail = plan(col.db, *page[0])
    assert "idx_records_doc_type" not in detail, detail


def test_records_scans_use_the_covering_index(tmp_path):
    # regression pins (brief 2b §3, SQLite 3.47.1, no sqlite_stat1)
    col = make_collection(tmp_path)
    ingest(col, 30, meta=LIST_META)
    db = col.db
    for sql in (
        "SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type",  # Collection.__init__
        "SELECT type, COUNT(*) FROM records GROUP BY type",  # stats()
        "SELECT COUNT(DISTINCT doc_id) FROM records",  # stats()
        "SELECT id FROM records WHERE indexed=1",  # the live-id scan (_live_ids, Task 3)
        "SELECT COUNT(*) FROM records WHERE indexed = 1",  # unfiltered list count
    ):
        assert "COVERING INDEX idx_records_doc_type" in plan(db, sql), (sql, plan(db, sql))
    for sql in (
        "SELECT id FROM records WHERE doc_id=? AND type='chunk' AND indexed=1",  # siblings
        "SELECT id FROM records WHERE doc_id=? AND type='summary'",  # summary expansion
        "SELECT id FROM records WHERE doc_id=? ORDER BY type DESC, position, id",  # get_document
        "SELECT id, indexed, type FROM records WHERE doc_id=?",  # _delete_doc_rows
    ):
        detail = plan(db, sql, ["d1"])
        assert "idx_records_doc_type (doc_id=?" in detail, (sql, detail)
    filtered = "SELECT COUNT(*) FROM records WHERE indexed = 1 AND json_extract(metadata, ?) = ?"
    assert "idx_records_doc_type" not in plan(db, filtered, ["$.src", "wiki"])
    fts = (
        "SELECT r.id, -bm25(records_fts) FROM records_fts"
        " JOIN records r ON r.id = records_fts.rowid"
        " WHERE records_fts MATCH ? AND doc_id=? AND type='chunk' AND indexed=1"
        " ORDER BY bm25(records_fts) LIMIT ?"
    )
    # ADR 0003: records_fts must stay the outer loop (never a rowid-restricted MATCH)
    assert plan(db, fts, ['"chunk"', "d1", 5]).startswith("SCAN records_fts VIRTUAL TABLE")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cold_start.py -v -k "listing_page_query or records_scans"`
Expected:
- `test_records_scans_use_the_covering_index` PASSES. It is a regression pin, and Task 1 already produced these plans.
- `test_listing_page_query_never_uses_the_covering_index` FAILS for at least `scope="chunks"` with `sort="-year"` and `filt=None` (pytest id `[None--year-chunks]`), with a detail like `SCAN records USING INDEX idx_records_doc_type | USE TEMP B-TREE FOR ORDER BY`. Brief 2b §3 measured this flip for "scope plus a metadata sort".
- The other page shapes may already pass on this SQLite. They are pinned so that a planner change cannot bring the flip back.

- [ ] **Step 3: Minimal implementation**

In `list_records`, replace

```python
            # ponytail: ORDER BY json_extract scans the filtered set (no expression index);
            # add one per hot sort key if listing large collections gets slow
```

with

```python
            # ponytail: ORDER BY json_extract scans the filtered set (no expression index);
            # an expression index per hot sort key is the follow-up (spec §5.2), not C
```

and replace

```python
                f"SELECT id FROM records WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
```

with

```python
                # NOT INDEXED (D9): with idx_records_doc_type present the planner walks
                # the covering index in doc_id order and fetches each row by rowid (p1:
                # 0.91 -> 2.73 s warm at 2.55M; ANALYZE does not fix it). NOT INDEXED
                # keeps the rowid-order table scan and still allows rowid lookups, so
                # the default ORDER BY id page still stops after LIMIT rows.
                f"SELECT id FROM records NOT INDEXED WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
```

The `total` COUNT above it stays unhinted. Unfiltered, it is a covering-index scan (pinned above). Filtered, it is `SCAN records` (also pinned).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cold_start.py -v -k "listing_page_query or records_scans"`
Expected: `19 passed`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 24 passed`. The existing listing tests in `tests/test_api.py` (filtered, sorted and paged listing) stay green, which proves the result order is unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_cold_start.py
git commit -m "Pin the listing page query to a table scan and add EXPLAIN regression tests" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Numpy id-set diffs over the covering index

**Files:**
- Modify: `src/raggio/store.py`:
  - imports: add `import itertools`;
  - new `_live_ids`, after `_rows_by_id` (lines 167–172);
  - `_reconcile_ghosts` (lines 628–648);
  - the `build()` and `swap()` closures of `_attach_index` (lines 982–995) and `_detach_index` (lines 1039–1052).
- Modify: `tests/test_cold_start.py` (append a section).

**Interfaces:**
- Consumes:
  - `IdMapIndex.search(q, k) -> (scores, ids)`
  - `_IvfIndex.all_ids() -> np.ndarray[uint64]`
  - `index.remove(rid: int)` and `index.contains(rid: int) -> bool`
  - `Collection._iter_vec_blocks()`, which yields `(np.ndarray[uint64], np.ndarray[float32])`
- Produces:
  - `_live_ids(db) -> np.ndarray` (dtype `uint64`, unique, in index order rather than sorted)
  - Inside `_attach_index` and `_detach_index`, `build()` now returns `seen` as a `np.ndarray[uint64]` instead of a `set[int]`. Task 9 relies on this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cold_start.py`:

```python
# ---- Task 3: numpy id-set diffs ----


def seed(tmp_path, record_ids, index_ids):
    """A collection dir whose meta.db holds `record_ids` and whose synced flat index
    holds `index_ids`: the state a crash between a db commit and index.sync leaves."""
    db = open_meta_db(tmp_path / "meta.db")
    db.executemany(
        "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
        " VALUES (?,?,'d1','chunk',0,'hello','{}',1)",
        [(i, f"c{i}") for i in record_ids],
    )
    db.commit()
    db.close()
    idx = IdMapIndex(dim=DIM, bit_width=4)
    if index_ids:
        idx.add_with_ids(
            np.random.default_rng(0).standard_normal((len(index_ids), DIM)).astype(np.float32),
            np.array(index_ids, dtype=np.uint64),
        )
    idx.sync(str(tmp_path / "index.tvim"))


def test_live_ids_is_uint64_and_handles_empty(tmp_path):
    col = make_collection(tmp_path)
    empty = store._live_ids(col.db)
    assert empty.dtype == np.uint64 and len(empty) == 0
    ingest(col, 5)
    live = store._live_ids(col.db)
    assert live.dtype == np.uint64
    assert sorted(live.tolist()) == [1, 2, 3, 4, 5]


def test_reconcile_reads_live_ids_once_through_the_helper(tmp_path, monkeypatch):
    seed(tmp_path, [1, 2], [1, 2, 3])
    calls, real = [], store._live_ids
    monkeypatch.setattr(store, "_live_ids", lambda db: calls.append(1) or real(db))
    col = make_collection(tmp_path)
    assert calls == [1]
    assert len(col.index) == 2 and not col.index.contains(3)


def test_reconcile_keeps_live_ids_above_2_32(tmp_path):
    big = [2**40 + 1, 2**40 + 2, 2**40 + 3]  # a int64/uint64 mix would promote to float64
    seed(tmp_path, big[:2], big)
    col = make_collection(tmp_path)
    assert len(col.index) == 2
    assert col.index.contains(big[0]) and col.index.contains(big[1])
    assert not col.index.contains(big[2])


def test_reconcile_without_ghosts_does_not_sync(tmp_path, monkeypatch):
    seed(tmp_path, [1, 2, 3], [1, 2, 3])
    synced = []
    monkeypatch.setattr(Collection, "_sync_index", lambda self: synced.append(1))
    col = make_collection(tmp_path)
    assert len(col.index) == 3 and synced == []


def test_reconcile_all_ghosts_empties_the_index(tmp_path):
    seed(tmp_path, [], [5, 6, 7])  # empty records, non-empty index
    col = make_collection(tmp_path)
    assert len(col.index) == 0


def test_reconcile_never_re_adds(tmp_path):
    seed(tmp_path, [1, 2, 3, 4], [1, 2])  # records the index lacks stay out (§4.3)
    col = make_collection(tmp_path)
    assert len(col.index) == 2
    assert not col.index.contains(3) and not col.index.contains(4)


@pytest.mark.parametrize("seed_", range(5))
def test_reconcile_matches_the_set_semantics(tmp_path, seed_):
    rng = np.random.default_rng(seed_)
    records = sorted(rng.choice(np.arange(1, 200), size=int(rng.integers(0, 150)), replace=False).tolist())
    in_index = sorted(rng.choice(np.arange(1, 200), size=int(rng.integers(1, 150)), replace=False).tolist())
    seed(tmp_path, records, in_index)
    col = make_collection(tmp_path)
    expect = set(in_index) & set(records)  # the pre-plan-C set logic
    assert len(col.index) == len(expect)
    assert all(col.index.contains(i) == (i in expect) for i in in_index)


def test_rows_deleted_during_a_build_are_diffed_out(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    real = col._iter_vec_blocks
    victims = iter(["c7", "c8"])

    def stream_then_delete():
        yield from real()
        with col.db_lock:  # a delete that commits while the build streams
            col.db.execute("DELETE FROM records WHERE external_id=?", (next(victims),))
            col.db.commit()

    col._iter_vec_blocks = stream_then_delete
    asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    assert isinstance(col.index, _IvfIndex) and len(col.index) == 299
    asyncio.run(col._process_job({"op": "detach_index"}))
    assert isinstance(col.index, IdMapIndex) and len(col.index) == 298
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cold_start.py -v -k "live_ids or reconcile or diffed_out"`
Expected:
- 2 fail: `test_live_ids_is_uint64_and_handles_empty` and `test_reconcile_reads_live_ids_once_through_the_helper`, both with `AttributeError: module 'raggio.store' has no attribute '_live_ids'`.
- The other 10 pass. They pin the behaviour the numpy rewrite must keep: the >2^32 ids, the no-ghost path, all ghosts, never re-adding, the randomized equivalence (5 cases) and the build-time delete.

- [ ] **Step 3: Minimal implementation**

In `src/raggio/store.py`, replace the import line

```python
import hashlib
```

with

```python
import hashlib
import itertools
```

After `_rows_by_id`, add:

```python
def _live_ids(db) -> np.ndarray:
    """Every indexed record id as a uint64 array. The planner answers this from the
    covering idx_records_doc_type (pinned in tests), never the text-heavy records
    pages. The order is the index's (doc_id, type, id), not id order;
    np.setdiff1d(assume_unique=True) needs no sort, and both sides must be uint64 (an
    int64/uint64 mix promotes to float64 and loses ids above 2**53)."""
    return np.fromiter(
        itertools.chain.from_iterable(db.execute("SELECT id FROM records WHERE indexed=1")),
        dtype=np.uint64,
    )
```

In `_reconcile_ghosts`, replace

```python
        live = {r[0] for r in self.db.execute("SELECT id FROM records WHERE indexed=1")}
        ghosts = [int(i) for i in all_ids if int(i) not in live]
        for g in ghosts:
            self.index.remove(g)
        if ghosts:
            self._sync_index()
```

with

```python
        # numpy set difference: a 2.55M-entry Python set plus a per-id membership loop
        # cost seconds on every cold start (brief 2b; the SQL scan is covering, Task 1)
        ghosts = np.setdiff1d(
            np.asarray(all_ids, dtype=np.uint64), _live_ids(self.db), assume_unique=True
        )
        for g in ghosts:  # evict only: a record the index lacks is never re-added (§4.3)
            self.index.remove(int(g))
        if len(ghosts):
            self._sync_index()
```

In `_attach_index`, replace

```python
            seen = []
            for ids, mat in self._iter_vec_blocks():
                ivf.add_with_ids(mat, ids)
                seen.append(ids)
            return ivf, set(map(int, np.concatenate(seen)))
```

with

```python
            seen = [np.empty(0, dtype=np.uint64)]
            for ids, mat in self._iter_vec_blocks():
                ivf.add_with_ids(mat, ids)
                seen.append(ids)
            return ivf, np.concatenate(seen)
```

and replace

```python
                live = {r[0] for r in self._rdb().execute("SELECT id FROM records WHERE indexed=1")}
                for gone in seen - live:  # deleted while the build streamed
                    ivf.remove(gone)
```

with

```python
                # deleted while the build streamed
                for gone in np.setdiff1d(seen, _live_ids(self._rdb()), assume_unique=True):
                    ivf.remove(int(gone))
```

In `_detach_index`, replace

```python
            seen = []
            for ids, mat in self._iter_vec_blocks():
                flat.add_with_ids(mat, ids)
                seen.append(ids)
            return flat, set(map(int, np.concatenate(seen))) if seen else set()
```

with

```python
            seen = [np.empty(0, dtype=np.uint64)]
            for ids, mat in self._iter_vec_blocks():
                flat.add_with_ids(mat, ids)
                seen.append(ids)
            return flat, np.concatenate(seen)
```

and replace

```python
                live = {r[0] for r in self._rdb().execute("SELECT id FROM records WHERE indexed=1")}
                for gone in seen - live:
                    flat.remove(gone)
```

with

```python
                for gone in np.setdiff1d(seen, _live_ids(self._rdb()), assume_unique=True):
                    flat.remove(int(gone))
```

The k=n probe in `_reconcile_ghosts` stays unchanged: `IdMapIndex` exposes no id enumeration (ADR 0001).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cold_start.py -v -k "live_ids or reconcile or diffed_out"`
Expected: `12 passed`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 36 passed`. `test_reconcile_evicts_ghosts_even_when_counts_match` (`tests/test_store_optimizations.py`) and `test_reconcile_evicts_ivf_ghosts` (`tests/test_ivf_index.py`) stay green.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_cold_start.py
git commit -m "Diff index and live id sets in numpy over the covering index" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Construct collections off the event loop, cancel-safe

**Files:**
- Modify: `src/raggio/store.py:1694-1717` (`CollectionManager.touch`), plus the new `_construct` and `_register` directly after it.
- Modify: `tests/test_cold_start.py` (append a section).

**Interfaces:**
- Consumes:
  - `Collection(cfg, directory, embedder_factory, set_index_config)`. Its `__init__` creates `asyncio.Condition()` (in `_RWLock`) and `asyncio.Event()`. In Python ≥ 3.10 both bind to a loop lazily, on first contended use, so creating them on a worker thread is safe (§5.1). The next bullet verifies the Collection this produces still works.
  - `Collection.start_worker()`, which must run on the loop (`asyncio.create_task`).
  - B's `delete_collection`, which evicts under `_load_lock`.
- Produces:
  - `CollectionManager.touch(name) -> Collection`. When the collection is already resident, it returns without taking `_load_lock`.
  - `async CollectionManager._construct(name: str, cfg: CollectionConfig) -> Collection`. The caller holds `_load_lock`. It builds the Collection in `asyncio.to_thread`, then registers it. If the awaiting task is cancelled, it still registers the finished build before re-raising.
  - `CollectionManager._register(name: str, c: Collection) -> None`, which runs `start_worker()` and then `resident[name] = c`.
  - The log line `collection <name> loaded in <s> s`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cold_start.py`:

```python
# ---- Task 4: off-loop, cancel-safe construction (§5.1) ----

GATE_TIMEOUT = 3.0


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.done = {"n": 0, "thread": None}


@pytest.fixture
def gated(monkeypatch):
    """Swap store.Collection for a subclass whose construction parks on `release`,
    so a test can hold a load mid-flight and see which thread built it."""
    gate, real = Gate(), store.Collection

    class GatedCollection(real):
        def __init__(self, *args, **kwargs):
            gate.entered.set()
            if not gate.release.wait(GATE_TIMEOUT):
                raise RuntimeError("gate never released")
            super().__init__(*args, **kwargs)
            gate.done["n"] += 1
            gate.done["thread"] = threading.get_ident()

    monkeypatch.setattr(store, "Collection", GatedCollection)
    return gate


def test_touch_builds_the_collection_off_the_event_loop(tmp_path, monkeypatch, gated):
    m = make_manager(tmp_path, monkeypatch)
    gated.release.set()

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        return threading.get_ident(), c

    loop_thread, c = asyncio.run(run())
    assert gated.done["n"] == 1
    assert gated.done["thread"] != loop_thread
    assert m.resident["x"] is c and c._worker is not None


def test_off_loop_built_collection_serves_ingest_and_search(tmp_path, monkeypatch):
    # its asyncio.Condition/Event were created on a worker thread: they must still work
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        await c.enqueue({"documents": [
            {"doc_id": "d1", "chunks": [{"id": "c1", "text": "one", "vector": rowvec(1)}]},
        ]})
        for _ in range(500):
            if c.pending_jobs() == 0:
                break
            await asyncio.sleep(0.01)
        q = np.array([rowvec(1)], dtype=np.float32)
        hits = await c.search("vector", q, None, 5, "chunks", None, None)
        return c.pending_jobs(), [h["id"] for h in hits]

    assert asyncio.run(run()) == (0, ["c1"])


def test_healthz_answers_while_a_collection_loads(tmp_path, monkeypatch, gated):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    app = create_app(embedder_factory=lambda cfg: FakeEmbedder())

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post("/collections", headers=ROOT, json={"name": "x"})
                assert r.status_code == 201, r.text
                load = asyncio.create_task(c.get("/collections/x", headers=ROOT))
                await asyncio.to_thread(gated.entered.wait, GATE_TIMEOUT)
                h = await asyncio.wait_for(c.get("/healthz"), 1.0)
                gated.release.set()
                r = await load
                return (h.status_code, h.json()["resident_collections"],
                        gated.entered.is_set(), r.status_code)

    assert asyncio.run(run()) == (200, [], True, 200)


def test_resident_collection_is_served_while_another_loads(tmp_path, monkeypatch, gated):
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        for n in ("x", "y"):
            await m.create_collection(n, DIM, 4, None, None, None)
        gated.release.set()
        x = await m.touch("x")
        gated.release.clear()
        gated.entered.clear()
        load = asyncio.create_task(m.touch("y"))
        await asyncio.to_thread(gated.entered.wait, GATE_TIMEOUT)
        again = await asyncio.wait_for(m.touch("x"), 1.0)  # must not queue behind y's load
        gated.release.set()
        y = await load
        return again is x, m.resident["y"] is y

    assert asyncio.run(run()) == (True, True)


def test_cancelled_load_still_registers_the_built_collection(tmp_path, monkeypatch, gated):
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        first = asyncio.create_task(m.touch("x"))
        await asyncio.to_thread(gated.entered.wait, GATE_TIMEOUT)
        first.cancel()  # the client disconnected mid-load
        await asyncio.sleep(0)
        second = asyncio.create_task(m.touch("x"))
        await asyncio.sleep(0.05)  # second is now queued on _load_lock
        gated.release.set()
        c = await second
        with pytest.raises(asyncio.CancelledError):
            await first
        worker_live = c._worker is not None and not c._worker.done()
        return m.resident.get("x") is c, gated.done["n"], worker_live

    assert asyncio.run(run()) == (True, 1, True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cold_start.py -v -k "off_the_event_loop or off_loop_built or healthz_answers or resident_collection_is_served or cancelled_load"`
Expected, which takes about 12 s because on base the gate blocks the loop thread until `GATE_TIMEOUT`:
- `test_touch_builds_the_collection_off_the_event_loop` FAILS on `assert gated.done["thread"] != loop_thread`.
- `test_off_loop_built_collection_serves_ingest_and_search` PASSES. It pins that the construction path still yields a working collection.
- `test_healthz_answers_while_a_collection_loads` FAILS with `(200, [], True, 500) == (200, [], True, 200)`. On base the construction blocks the loop thread until the gate times out, the load returns 500, and `/healthz` is only served afterwards.
- `test_resident_collection_is_served_while_another_loads` FAILS with `RuntimeError: gate never released`.
- `test_cancelled_load_still_registers_the_built_collection` FAILS with `RuntimeError: gate never released`.

- [ ] **Step 3: Minimal implementation**

In `CollectionManager`, replace `touch` in full:

```python
    async def touch(self, name: str) -> Collection:
        """Return the resident collection, loading (and LRU-evicting) as needed."""
        async with self._load_lock:
            c = self.resident.get(name)
            if c is None:
                cfg = self.get_config(name)
                if cfg is None:
                    raise KeyError(name)
                while len(self.resident) >= self.settings.max_resident_collections:
                    victims = sorted(
                        (v for v in self.resident.values() if v.pending_jobs() == 0),
                        key=lambda v: v.last_used,
                    )
                    if not victims:
                        break  # everyone is busy ingesting; allow going over budget
                    await self._evict(victims[0].cfg.name)
                c = Collection(
                    cfg, self._dir(name), lambda: self.embedder_factory(cfg),
                    lambda ic, name=name: self.set_index_config(name, ic),
                )
                c.start_worker()
                self.resident[name] = c
            c.last_used = time.monotonic()
            return c
```

with

```python
    async def touch(self, name: str) -> Collection:
        """Return the resident collection, loading (and LRU-evicting) as needed."""
        c = self.resident.get(name)
        if c is not None:  # resident: never queue behind another collection's load
            c.last_used = time.monotonic()
            return c
        async with self._load_lock:
            c = self.resident.get(name)
            if c is None:
                cfg = self.get_config(name)
                if cfg is None:
                    raise KeyError(name)
                while len(self.resident) >= self.settings.max_resident_collections:
                    victims = sorted(
                        (v for v in self.resident.values() if v.pending_jobs() == 0),
                        key=lambda v: v.last_used,
                    )
                    if not victims:
                        break  # everyone is busy ingesting; allow going over budget
                    await self._evict(victims[0].cfg.name)
                c = await self._construct(name, cfg)
            c.last_used = time.monotonic()
            return c

    async def _construct(self, name: str, cfg: CollectionConfig) -> Collection:
        """Build a Collection on a worker thread (index load, the one-time meta.db
        migration, the reconcile scans: seconds to minutes cold), so the loop keeps
        serving /healthz and the resident collections. The caller holds _load_lock.
        If the awaiting request is cancelled mid-build, the build still finishes and
        is registered before the cancellation propagates. Releasing the lock early
        would let the next request build a second copy, and the orphan would keep open
        SQLite handles while its replayed jobs had no worker."""
        t0 = time.monotonic()
        build = asyncio.ensure_future(asyncio.to_thread(
            Collection, cfg, self._dir(name), lambda: self.embedder_factory(cfg),
            lambda ic, name=name: self.set_index_config(name, ic),
        ))
        try:
            c = await asyncio.shield(build)
        except asyncio.CancelledError:
            while not build.done():
                try:
                    await asyncio.wait({build})
                except asyncio.CancelledError:
                    pass  # cancelled again: still must not orphan the build
            if build.cancelled() or build.exception() is not None:
                raise
            self._register(name, build.result())
            raise
        self._register(name, c)
        _log.info("collection %s loaded in %.2f s", name, time.monotonic() - t0)
        return c

    def _register(self, name: str, c: Collection) -> None:
        c.start_worker()  # on the loop: create_task needs the running loop
        self.resident[name] = c
```

The fast path reads `resident` without the lock. That is safe because `_evict` pops the entry synchronously before its first `await`, so a collection that is being evicted or deleted (B's `delete_collection` holds `_load_lock`) is never returned from the dict. A caller that took the reference just before the pop of an eviction sees the `closed` error it could already get on main after `touch()` returned. If the pop was B's `delete_collection`, a search gets B's 404 `collection '<name>' not found` (`CollectionDeletedError`, plan B Task 6, spec 3.1 B1) instead of a 500.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cold_start.py -v -k "off_the_event_loop or off_loop_built or healthz_answers or resident_collection_is_served or cancelled_load"`
Expected: `5 passed`, in about 1 s.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 41 passed`. The crash-replay and eviction tests in `tests/test_api.py`, and B's `tests/test_concurrency.py` (which includes the racing touch/delete test and the AST check that no `with db_lock` or `with _catalog_lock` block contains an `await`), stay green.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_cold_start.py
git commit -m "Build collections off the event loop and keep cancelled loads registered" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `stats()` off the event loop, plus the records-scan request-path audit

**Files:**
- Modify: `src/raggio/app.py:146-150` (`collection_info`).
- Modify: `tests/test_cold_start.py` (append a section).

**Interfaces:**
- Consumes:
  - `Collection.stats() -> dict` with keys `documents`, `chunks`, `summaries`, `pending_jobs`
  - `store._filter_sql(scope, filt) -> tuple[str, list]`
  - the routes `GET /collections/{name}`, `GET /collections/{name}/documents`, `POST /collections/{name}/search`, `GET /collections/{name}/jobs/{id}`
- Produces: `collection_info` awaits `asyncio.to_thread(c.stats)`. The response shape is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cold_start.py`:

```python
# ---- Task 5: records scans on request paths stay off the loop (§5.2) ----


def _off_loop() -> bool:
    """True inside an asyncio.to_thread worker: no running loop in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def api_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    return create_app(embedder_factory=lambda cfg: FakeEmbedder())


async def wait_job(c, name, job_id):
    for _ in range(500):
        r = await c.get(f"/collections/{name}/jobs/{job_id}", headers=ROOT)
        if r.json()["status"] in ("done", "error"):
            return r.json()
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished")


def api_docs(n):
    return [
        {"doc_id": f"d{i}", "chunks": [{
            "id": f"c{i}", "text": f"chunk {i}", "vector": rowvec(i),
            "metadata": {"src": "wiki" if i % 2 else "arxiv", "year": 2000 + i},
        }]}
        for i in range(n)
    ]


def test_collection_info_runs_stats_off_the_loop(tmp_path, monkeypatch):
    where, real = [], store.Collection.stats

    def spy(self):
        where.append(_off_loop())
        return real(self)

    monkeypatch.setattr(store.Collection, "stats", spy)
    app = api_app(tmp_path, monkeypatch)

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await c.post("/collections", headers=ROOT, json={"name": "x"})
                r = await c.post("/collections/x/documents", headers=ROOT,
                                 json={"documents": api_docs(1)})
                assert (await wait_job(c, "x", r.json()["job_id"]))["status"] == "done"
                return (await c.get("/collections/x", headers=ROOT)).json()

    info = asyncio.run(run())
    assert where == [True]
    assert (info["documents"], info["chunks"], info["summaries"], info["pending_jobs"]) == (1, 1, 0, 0)
    assert info["name"] == "x" and info["index"] == {"type": "flat"}


def test_request_paths_that_scan_records_run_off_the_loop(tmp_path, monkeypatch):
    # §5.2 audit: filtered/sorted listings and filtered allowlists take 43-54 s cold at
    # 4 GiB, so every request-path _filter_sql caller must run in a worker thread
    where, real = [], store._filter_sql

    def spy(scope, filt):
        where.append(_off_loop())
        return real(scope, filt)

    monkeypatch.setattr(store, "_filter_sql", spy)
    app = api_app(tmp_path, monkeypatch)

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await c.post("/collections", headers=ROOT, json={"name": "x"})
                r = await c.post("/collections/x/documents", headers=ROOT,
                                 json={"documents": api_docs(6)})
                assert (await wait_job(c, "x", r.json()["job_id"]))["status"] == "done"
                listing = await c.get("/collections/x/documents", headers=ROOT, params={
                    "scope": "chunks", "filter": '{"src": "wiki"}', "sort": "-year"})
                vector = await c.post("/collections/x/search", headers=ROOT, json={
                    "query": {"vector": rowvec(1)}, "filter": {"src": "wiki"}, "k": 3})
                text = await c.post("/collections/x/search", headers=ROOT, json={
                    "query": {"text": "chunk"}, "mode": "text", "filter": {"src": "wiki"}, "k": 3})
                return [r.status_code for r in (listing, vector, text)]

    assert asyncio.run(run()) == [200, 200, 200]
    assert len(where) >= 3 and all(where), where
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cold_start.py -v -k "stats_off_the_loop or scan_records_run_off"`
Expected:
- `test_collection_info_runs_stats_off_the_loop` FAILS on `assert where == [True]` (it is `[False]` on base).
- `test_request_paths_that_scan_records_run_off_the_loop` PASSES. It is the §5.2 audit pin: the listing already runs through `to_thread(c.list_records, ...)`, the vector allowlist through `to_thread(run)`, and text search through `to_thread(self._text_ids, ...)`.

- [ ] **Step 3: Minimal implementation**

In `src/raggio/app.py`, replace

```python
    @app.get("/collections/{name}")
    async def collection_info(c=Depends(get_collection)):
        return {"name": c.cfg.name, "dim": c.cfg.dim, "bit_width": c.cfg.bit_width,
                "model": c.cfg.model, "tokenizer": c.cfg.tokenizer,
                "index": c.index_info(), **c.stats()}
```

with

```python
    @app.get("/collections/{name}")
    async def collection_info(c=Depends(get_collection)):
        # off the loop: the GROUP BY and COUNT(DISTINCT doc_id) walk every record
        # (the covering index since plan C: about 96 MB read cold at 2.55M rows, p1)
        stats = await asyncio.to_thread(c.stats)
        return {"name": c.cfg.name, "dim": c.cfg.dim, "bit_width": c.cfg.bit_width,
                "model": c.cfg.model, "tokenizer": c.cfg.tokenizer,
                "index": c.index_info(), **stats}
```

The audit found no other request path that scans `records` on the loop. `get_document`, `job_status` and `_expand`'s per-document lookups stay on the loop: they are indexed point lookups (`doc_id=?` through `idx_records_doc_type`, which Task 2 pins, and `jobs.id` through the primary key).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cold_start.py -v -k "stats_off_the_loop or scan_records_run_off"`
Expected: `2 passed`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 43 passed`. `test_full_lifecycle` (`tests/test_api.py`), which reads the `/collections/{name}` fields, stays green.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/app.py tests/test_cold_start.py
git commit -m "Run collection stats off the event loop and pin the records-scan audit" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `_vec_sample` by random rowids

**Files:**
- Modify: `src/raggio/store.py`:
  - add constants after `MIGRATION_PROGRESS_OPS` (Task 1);
  - `_vec_sample` (lines 909–917).
- Modify: `tests/test_cold_start.py` (append a section).

**Interfaces:**
- Consumes:
  - `_rows_by_id(db, sql, ids)`
  - `Collection.indexed_counts: dict[str, int]`. It is copy-on-write after B, so read the reference once.
  - `Collection._rdb()`
- Produces:
  - `VEC_SAMPLE_MIN_DENSITY: float = 0.2`
  - `VEC_SAMPLE_ROUNDS: int = 8`
  - `Collection._vec_sample(k: int) -> np.ndarray`. The signature and contract are unchanged: up to `k` distinct, uniformly random retained vectors of indexed records, shaped `(m, dim)` float32. `_attach_index` (training) and `_detach_index` (calibration) keep calling it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cold_start.py`:

```python
# ---- Task 6: rowid sampling ----


def vec_ids(col, mat) -> list:
    """Map sampled f32 rows back to record ids via their stored fp16 bytes
    (fp16 -> f32 -> fp16 round-trips exactly)."""
    by_blob = {bytes(b): i for i, b in col.db.execute("SELECT id, vec FROM vecs")}
    return [by_blob[row.astype(np.float16).tobytes()] for row in mat]


def test_vec_sample_dense_ids_point_read_without_random(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 2000)
    log = record_sql(col)
    mat = col._vec_sample(256)
    assert mat.shape == (256, DIM) and mat.dtype == np.float32
    assert len(set(vec_ids(col, mat))) == 256
    assert not any("RANDOM()" in s for s, _ in log)  # p1: 152 s cold at 2.55M rows


def test_vec_sample_sparse_ids_fall_back(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 2000)
    with col.db_lock:  # mass delete: 200 of 2000 ids left, density 0.1
        col.db.execute("DELETE FROM records WHERE id % 10 != 0")
        col.db.commit()
    col.indexed_counts = {"chunk": 200}  # what _delete_doc_rows keeps in step
    log = record_sql(col)
    mat = col._vec_sample(50)
    assert len(mat) == 50
    assert all(i % 10 == 0 for i in vec_ids(col, mat))
    assert any("RANDOM()" in s for s, _ in log)


def test_vec_sample_k_at_least_half_returns_all_rows(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    assert sorted(vec_ids(col, col._vec_sample(65_536))) == list(range(1, 301))
    assert len(set(vec_ids(col, col._vec_sample(150)))) == 150  # k == n/2: rowid path
    assert len(set(vec_ids(col, col._vec_sample(151)))) == 151  # k > n/2: full scan


def test_vec_sample_never_returns_rows_without_vectors(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 2000)
    with col.db_lock:  # legacy rows: indexed, but no retained fp16 original
        col.db.execute("DELETE FROM vecs WHERE id <= 1000")
        col.db.commit()
    for k in (100, 900):
        ids = vec_ids(col, col._vec_sample(k))
        assert len(ids) == k and len(set(ids)) == k and min(ids) > 1000


def test_vec_sample_is_uniform_over_ids(tmp_path):
    # k-means trains on this sample: a bias toward low (old) ids would skew the shards.
    # One draw's mean has sd ~34; the mean of 20 draws has sd ~8, so 60 is ~8 sd
    col = make_collection(tmp_path)
    ingest(col, 2000)
    means = [np.mean(vec_ids(col, col._vec_sample(250))) for _ in range(20)]
    assert abs(np.mean(means) - 1000.5) < 60
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cold_start.py -v -k vec_sample`
Expected:
- `test_vec_sample_dense_ids_point_read_without_random` FAILS on `assert not any("RANDOM()" in s for s, _ in log)`.
- The other 4 PASS. They pin the contract that the rewrite must keep: the sparse fallback, `k ≥ n/2`, missing vectors, and uniformity.

- [ ] **Step 3: Minimal implementation**

In `src/raggio/store.py`, replace

```python
MIGRATION_PROGRESS_OPS = 1_000_000
```

with

```python
MIGRATION_PROGRESS_OPS = 1_000_000

# _vec_sample (k-means training / calibration sample): ids are MAX+1-allocated, so
# random rowids hit live rows at rate n/max_id. Point-read those while that rate is at
# least VEC_SAMPLE_MIN_DENSITY; mass deletes below it fall back to ORDER BY RANDOM()
VEC_SAMPLE_MIN_DENSITY = 0.2
VEC_SAMPLE_ROUNDS = 8
```

Replace `_vec_sample` in full:

```python
    def _vec_sample(self, k: int) -> np.ndarray:
        rows = self._rdb().execute(
            "SELECT v.vec FROM vecs v JOIN records r ON r.id=v.id WHERE r.indexed=1"
            " ORDER BY RANDOM() LIMIT ?", (k,),
        ).fetchall()
        if not rows:
            return np.empty((0, self.cfg.dim), np.float32)
        mat = np.frombuffer(b"".join(r[0] for r in rows), dtype=np.float16)
        return mat.reshape(len(rows), -1).astype(np.float32)
```

with

```python
    def _vec_sample(self, k: int) -> np.ndarray:
        """Up to k distinct, uniformly random retained vectors of indexed records.
        ORDER BY RANDOM() reads and sorts every vecs page (p1: 152 s cold at 2.55M rows),
        so while ids are dense, draw random rowids and point-read only those. Every
        live id is equally likely to be drawn, so the pick stays uniform. Too sparse,
        k above n/2, or too few hits after VEC_SAMPLE_ROUNDS falls back to the scan."""
        db = self._rdb()
        n = sum(self.indexed_counts.values())
        max_id = db.execute("SELECT MAX(id) FROM records").fetchone()[0] or 0
        if n and max_id and n / max_id >= VEC_SAMPLE_MIN_DENSITY and 2 * k <= n:
            rng = np.random.default_rng()
            got: dict[int, bytes] = {}
            tried = np.empty(0, dtype=np.int64)
            for _ in range(VEC_SAMPLE_ROUNDS):
                draws = rng.integers(1, max_id + 1, size=int((k - len(got)) * max_id / n * 1.3) + 64)
                cand = np.setdiff1d(draws, tried)
                tried = np.union1d(tried, cand)
                got.update(_rows_by_id(
                    db,
                    "SELECT v.id, v.vec FROM vecs v JOIN records r ON r.id=v.id"
                    " WHERE r.indexed=1 AND v.id IN ({})",
                    cand.tolist(),
                ))
                if len(got) >= k:
                    keys = list(got)
                    pick = rng.choice(len(keys), k, replace=False)
                    blob = b"".join(got[keys[i]] for i in pick)
                    mat = np.frombuffer(blob, dtype=np.float16)
                    return mat.reshape(k, self.cfg.dim).astype(np.float32)
        rows = db.execute(
            "SELECT v.vec FROM vecs v JOIN records r ON r.id=v.id WHERE r.indexed=1"
            " ORDER BY RANDOM() LIMIT ?", (k,),
        ).fetchall()
        if not rows:
            return np.empty((0, self.cfg.dim), np.float32)
        mat = np.frombuffer(b"".join(r[0] for r in rows), dtype=np.float16)
        return mat.reshape(len(rows), -1).astype(np.float32)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cold_start.py -v -k vec_sample`
Expected: `5 passed`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 48 passed`. The IVF recall tests in `tests/test_ivf_index.py` (`test_attach_matches_flat_and_survives_reload` and the other k-means-trained shard tests) stay green.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_cold_start.py
git commit -m "Sample training vectors by random rowid instead of ORDER BY RANDOM()" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Return index-job memory to the OS (D13)

**Files:**
- Modify: `src/raggio/store.py`:
  - imports: add `import ctypes` and `import sys`;
  - new `_malloc_trim`, after `_require_headroom` (lines 99–108);
  - `_process_job` (lines 742–749).
- Modify: `Dockerfile`, the runtime stage of Plan A's final layout: new lines after `EXPOSE 8000`. Plan A's Task 9 names `VOLUME /data` + `EXPOSE 8000` as C's anchor. Plan G's `PYTHONWARNINGS` edit shows `ENV LANG=…` directly followed by `VOLUME /data`, so C must not insert anything between those two lines.
- Modify: `tests/test_cold_start.py` (append a section).

**Interfaces:**
- Consumes: `Collection._attach_index(payload)` and `Collection._detach_index()`.
- Produces:
  - `_malloc_trim() -> bool`. It returns True when glibc released memory. It returns False, without raising, off Linux or without glibc.
  - `_process_job` calls `await asyncio.to_thread(_malloc_trim)` in a `finally` after every `attach_index` or `detach_index` job, whether it succeeds or fails.
  - The image sets `ENV MALLOC_TRIM_THRESHOLD_=134217728`. There is no `MALLOC_ARENA_MAX`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cold_start.py`:

```python
# ---- Task 7: memory release (D13) ----


def test_malloc_trim_returns_a_bool_and_never_raises():
    assert isinstance(store._malloc_trim(), bool)  # real call: glibc on Linux CI, no-op on Windows


def test_malloc_trim_is_a_no_op_off_glibc(monkeypatch):
    def missing(name):
        raise OSError(f"{name}: cannot open shared object file")  # musl, or no libc.so.6

    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setattr(store.ctypes, "CDLL", missing)
    assert store._malloc_trim() is False
    monkeypatch.setattr(store.sys, "platform", "win32")
    monkeypatch.setattr(store.ctypes, "CDLL", lambda name: pytest.fail("CDLL loaded off Linux"))
    assert store._malloc_trim() is False


def test_malloc_trim_calls_glibc_with_size_t(monkeypatch):
    calls = []

    class FakeTrim:
        argtypes = restype = None

        def __call__(self, pad):
            calls.append(pad)
            return 1

    trim = FakeTrim()
    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setattr(store.ctypes, "CDLL", lambda name: type("Libc", (), {"malloc_trim": trim})())
    assert store._malloc_trim() is True
    assert calls == [0]
    assert trim.argtypes == [ctypes.c_size_t] and trim.restype is ctypes.c_int


def test_malloc_trim_runs_after_every_index_job(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "_malloc_trim", lambda: calls.append(1) or False)
    col = make_collection(tmp_path)
    ingest(col, 300)
    assert calls == []  # ingest jobs don't trim
    asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    assert len(calls) == 1
    asyncio.run(col._process_job({"op": "detach_index"}))
    assert len(calls) == 2
    with pytest.raises(ValueError, match="too large"):
        asyncio.run(col._process_job({"op": "attach_index", "nlist": 64}))  # 300 < 8*64
    assert len(calls) == 3  # a failed job's transient memory is released too


def test_dockerfile_sets_the_trim_threshold_in_the_runtime_stage():
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    runtime = text.rsplit("\nFROM ", 1)[1]  # the last stage is the image that runs
    assert runtime.count("ENV MALLOC_TRIM_THRESHOLD_=134217728") == 1
    assert text.count("MALLOC_TRIM_THRESHOLD_=") == 1
    assert "MALLOC_ARENA_MAX=" not in text  # spec D13: never set (the comment names it)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cold_start.py -v -k "malloc_trim or trim_threshold"`
Expected: `5 failed`:
- the first four with `AttributeError: module 'raggio.store' has no attribute '_malloc_trim'` (or `'sys'` / `'ctypes'`);
- the Dockerfile test on `assert runtime.count("ENV MALLOC_TRIM_THRESHOLD_=134217728") == 1`.

- [ ] **Step 3: Minimal implementation**

In `src/raggio/store.py`, replace the import line

```python
import asyncio
```

with

```python
import asyncio
import ctypes
```

and replace

```python
import sqlite3
```

with

```python
import sqlite3
import sys
```

After `_require_headroom`, add:

```python
def _malloc_trim() -> bool:
    """Hand freed heap back to the OS after an index job (D13). A build frees GBs of
    transient buffers (the second code copy, the f32 sample, streamed blocks), and
    glibc keeps them in its arenas, so without this the container's RSS stays at the
    build's peak until restart. glibc-only: a no-op returning False on Windows, macOS
    and musl."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        trim = ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):
        return False
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return bool(trim(0))
```

In `_process_job`, replace

```python
        op = payload.get("op")
        if op == "attach_index":
            await self._attach_index(payload)
            return
        if op == "detach_index":
            await self._detach_index()
            return
```

with

```python
        op = payload.get("op")
        if op in ("attach_index", "detach_index"):
            try:
                if op == "attach_index":
                    await self._attach_index(payload)
                else:
                    await self._detach_index()
            finally:
                await asyncio.to_thread(_malloc_trim)  # D13: failed builds free memory too
            return
```

In `Dockerfile` (the runtime stage of Plan A's layout), replace

```dockerfile
VOLUME /data
EXPOSE 8000
```

with

```dockerfile
VOLUME /data
EXPOSE 8000

# D13: an explicit trim threshold makes glibc return heap above 128 MiB to the OS at
# free() time and stops its dynamic mmap-threshold growth, so the multi-GB transient
# buffers of an index job are unmapped when freed. raggio also calls malloc_trim(0)
# after every index job. MALLOC_ARENA_MAX stays unset (spec D13).
ENV MALLOC_TRIM_THRESHOLD_=134217728
```

The new block goes after `EXPOSE 8000`, so `ENV LANG=…` is still directly followed by `VOLUME /data` (Plan G's anchor), and the stage still ends with the access-log comment and the unchanged `CMD` (Plan A's `test_image_keeps_the_runtime_contract`). If Plan A's final Dockerfile does not contain the two quoted lines back to back, stop and report the mismatch: do not guess an anchor.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cold_start.py -v -k "malloc_trim or trim_threshold"`
Expected: `5 passed`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 53 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py Dockerfile tests/test_cold_start.py
git commit -m "Trim the glibc heap after index jobs and set MALLOC_TRIM_THRESHOLD_ in the image" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Headroom guard first, sized to the measured growth (D13)

**Files:**
- Modify: `src/raggio/store.py`:
  - a constant after `VEC_SAMPLE_ROUNDS` (Task 6);
  - new `_attach_need`, after `_require_headroom`;
  - `_attach_index` (lines 953–988): the validation and the guard move before `_backfill_vecs`;
  - `_detach_index` (lines 1022–1034): the guard moves before the missing-vector scan.
- Modify: `tests/test_cold_start.py` (append a section).

**Interfaces:**
- Consumes:
  - `_require_headroom(need: int, what: str) -> None`
  - `_cgroup_mem_free() -> int | None`
  - `_ivf_auto_nlist(n) -> int`
  - `Collection.indexed_counts`
- Produces:
  - `ATTACH_NEED_FACTOR: float = 1.25`
  - `_attach_need(n: int, dim: int, bit_width: int, nlist: int) -> int` (bytes)
  - `_attach_index` validates and guards before any SQL. `n` comes from `sum(self.indexed_counts.values())`, and the error texts `index needs at least ...` and `nlist=... too large ...` are unchanged.
  - `_detach_index` guards before any SQL.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cold_start.py`:

```python
# ---- Task 8: guard first, measured need (D13) ----


def count_rdb(col) -> list:
    calls, real = [], col._rdb
    col._rdb = lambda: calls.append(1) or real()
    return calls


def test_attach_refusal_does_no_prework(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    ingest(col, 300)
    monkeypatch.setattr(store, "_cgroup_mem_free", lambda: 0)  # a full container
    calls = count_rdb(col)
    with pytest.raises(ValueError, match="raise the memory limit"):
        asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    assert calls == []  # no backfill anti-join, no COUNT(*), no sample: minutes cold (p1)


def test_detach_refusal_does_no_prework(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    ingest(col, 300)
    asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    monkeypatch.setattr(store, "_cgroup_mem_free", lambda: 0)
    calls = count_rdb(col)
    with pytest.raises(ValueError, match="raise the memory limit"):
        asyncio.run(col._process_job({"op": "detach_index"}))
    assert calls == [] and isinstance(col.index, _IvfIndex)


def test_attach_need_matches_the_measured_growth():
    # p1, 2.55M x 1024-d, 4-bit, nlist 256: growth 2176-2186 MiB; the unscaled sum
    # was 1757 MiB (1884 with the 128 MiB margin) and under-reserved
    # (2,549,119 rows is the bench-tv volume; --limit 2549619 is only the bench flag)
    need_mib = store._attach_need(2_549_119, 1024, 4, 256) >> 20
    assert 2176 <= need_mib <= 2400
    assert need_mib == 2195  # (1305148928 + 268435456 + 268435456) * 1.25 >> 20


def test_attach_guard_receives_the_scaled_need(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    ingest(col, 300)
    seen = []

    class Refused(Exception):
        pass

    def guard(need, what):
        seen.append((need, what))
        raise Refused

    monkeypatch.setattr(store, "_require_headroom", guard)
    with pytest.raises(Refused):
        asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    assert seen == [(store._attach_need(300, DIM, 4, 8), "index build")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cold_start.py -v -k "refusal_does_no_prework or attach_need or scaled_need"`
Expected: `4 failed`:
- `test_attach_refusal_does_no_prework` on `assert calls == []` (the backfill anti-join ran first);
- `test_detach_refusal_does_no_prework` on `assert calls == []` (the missing-vector COUNT ran first);
- the other two with `AttributeError: module 'raggio.store' has no attribute '_attach_need'`.

- [ ] **Step 3: Minimal implementation**

In `src/raggio/store.py`, replace

```python
VEC_SAMPLE_ROUNDS = 8
```

with

```python
VEC_SAMPLE_ROUNDS = 8

# attach headroom (D13): the summed transient terms under-reserve. p1 measured 2176-2186
# MiB of growth at 2.55M x 1024-d 4-bit (nlist 256) against 1757 MiB summed; x1.25
# covers it. The guard stays conservative; the swapless DGX run is the gate (§5.4)
ATTACH_NEED_FACTOR = 1.25
```

After `_require_headroom`, add:

```python
def _attach_need(n: int, dim: int, bit_width: int, nlist: int) -> int:
    """Bytes an IVF build adds on top of the loaded collection: a second copy of the
    codes, the f32 k-means sample, ~1 MB fixed per shard, scaled to the measured
    growth by ATTACH_NEED_FACTOR."""
    return int(
        (n * dim * bit_width // 8 + min(n, IVF_TRAIN_SAMPLE) * dim * 4 + nlist * (1 << 20))
        * ATTACH_NEED_FACTOR
    )
```

In `_attach_index`, replace

```python
        await self._backfill_vecs()

        def build():
            n = self._rdb().execute("SELECT COUNT(*) FROM records WHERE indexed=1").fetchone()[0]
            if n < IVF_MIN_ROWS:
                raise ValueError(f"index needs at least {IVF_MIN_ROWS} indexed records, have {n}")
            nlist = nlist_req or _ivf_auto_nlist(n)
            if n < 8 * nlist:
                raise ValueError(f"nlist={nlist} too large for {n} records (need >=8 rows per shard)")
            _require_headroom(
                n * self.cfg.dim * self.cfg.bit_width // 8  # second copy of the codes
                + min(n, IVF_TRAIN_SAMPLE) * self.cfg.dim * 4  # f32 k-means sample
                + nlist * (1 << 20),  # per-shard fixed overhead
                "index build",
            )
            ivf = _IvfIndex.train(
```

with

```python
        # guard first (D13): validate and refuse before any SQL, so a full container
        # fails in milliseconds instead of after minutes of cold pre-work. n comes from
        # the in-memory per-type counts, which equal COUNT(*) WHERE indexed=1
        n = sum(self.indexed_counts.values())
        if n < IVF_MIN_ROWS:
            raise ValueError(f"index needs at least {IVF_MIN_ROWS} indexed records, have {n}")
        nlist = nlist_req or _ivf_auto_nlist(n)
        if n < 8 * nlist:
            raise ValueError(f"nlist={nlist} too large for {n} records (need >=8 rows per shard)")
        _require_headroom(_attach_need(n, self.cfg.dim, self.cfg.bit_width, nlist), "index build")
        await self._backfill_vecs()

        def build():
            ivf = _IvfIndex.train(
```

In `_detach_index`, replace

```python
        missing = await asyncio.to_thread(
```

with

```python
        # guard first (D13): a refusal must not cost the missing-vector scan
        _require_headroom(
            len(self.index) * self.cfg.dim * self.cfg.bit_width // 8, "index removal"
        )
        missing = await asyncio.to_thread(
```

and replace

```python
        def build():
            _require_headroom(
                len(self.index) * self.cfg.dim * self.cfg.bit_width // 8, "index removal"
            )
            flat = IdMapIndex(dim=self.cfg.dim, bit_width=self.cfg.bit_width)
```

with

```python
        def build():
            flat = IdMapIndex(dim=self.cfg.dim, bit_width=self.cfg.bit_width)
```

The detach need stays unscaled (the flat rebuild's second copy). In p1's swapless run the detach passed this unscaled guard (need 1,372 MiB) and the run's final VmHWM, 3,646 MiB, bounds its peak; p1 did not measure the detach's own growth, so there is no measurement to scale it by. §7 threshold 7 on the DGX checks it: if the swapless detach is refused or OOM-killed, Task 12's session ends in `== PLANC FAILED: detach: error ==` (a guard refusal) or `== PLANC FAILED: detach: the server died ==` (an OOM kill), and Task 12 Step 7 reports that to the user as an open finding instead of changing the factor.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cold_start.py -v -k "refusal_does_no_prework or attach_need or scaled_need"`
Expected: `4 passed`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 57 passed`. In `tests/test_ivf_index.py`, `test_attach_fails_without_vectors_or_text` ("re-ingest") and `test_request_validation` stay green.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_cold_start.py
git commit -m "Run the index headroom guard before any pre-work and size it to measured growth" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Find rows without a vector from the build stream, not a pre-scan

**Files:**
- Modify: `src/raggio/store.py`:
  - `_backfill_vecs` (lines 919–951): an optional id list and a per-batch `add` sink;
  - `_attach_index` (lines 953–1012): sample first, stream, then diff the live ids against the streamed ids; phase timings logged;
  - `_detach_index` (lines 1014–1061): the missing-vector check moves after the stream.
- Modify: `tests/test_cold_start.py` (append a section).

**Interfaces:**
- Consumes:
  - `_live_ids(db) -> np.ndarray[uint64]` (Task 3)
  - `_rows_by_id(db, sql, ids)`
  - `Collection._vec_sample(k)` (Task 6)
  - `_attach_need` and the guard order (Task 8)
  - `_log` (Task 1)
- Produces:
  - `Collection._backfill_vecs(ids: np.ndarray | None = None, add=None) -> int`.
    - `ids=None` keeps today's anti-join: the legacy path.
    - An id array point-reads only those rows.
    - `add(ids: np.ndarray[uint64], mat: np.ndarray[float32])` is called on a worker thread after each written batch of 64.
    - It returns the number of rows written. The error `"{k} records have neither a retained vector nor text; re-ingest them before attaching an index"` is unchanged.
  - `_attach_index`:
    - It runs no `records` × `vecs` anti-join and no `COUNT(*)`, unless the training sample comes back short (fewer than `min(n, IVF_TRAIN_SAMPLE)` rows). That only happens on a collection ingested before vector retention, and it then backfills everything first, as today.
    - It logs one INFO line on `uvicorn.error`: `attach_index <name>: n=... nlist=... prework=...s sample=...s legacy_backfill=... build=...s live_scan=...s backfill=... rows ...s swap=...s total=...s`. Task 11's `c_verdict.py` reads `prework`, `live_scan` and the backfill seconds from it (threshold 9, as their sum). `sample` is only reported: `c_probe.py load` measures threshold 6.
  - `_detach_index` raises `"{k} records lack a retained vector; re-ingest them first"` after the stream and before the swap, with the index unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cold_start.py`:

```python
# ---- Task 9: missing vectors found from the stream ----


def delete_vecs(col, ids, drop_text=False):
    """Make `ids` legacy rows: indexed, but without a retained fp16 original."""
    marks = ",".join("?" * len(ids))
    with col.db_lock:
        col.db.execute(f"DELETE FROM vecs WHERE id IN ({marks})", list(ids))
        if drop_text:
            col.db.execute(f"UPDATE records SET text=NULL WHERE id IN ({marks})", list(ids))
        col.db.commit()


def test_backfill_by_ids_embeds_only_those_rows_and_feeds_add(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    delete_vecs(col, [4, 5, 6])
    got = []
    wrote = asyncio.run(col._backfill_vecs(
        np.array([4, 6], dtype=np.uint64), lambda i, m: got.append((i, m)),
    ))
    assert wrote == 2
    ids = np.concatenate([i for i, _ in got])
    mats = np.concatenate([m for _, m in got])
    assert ids.dtype == np.uint64 and sorted(ids.tolist()) == [4, 6]
    assert mats.dtype == np.float32 and mats.shape == (2, DIM)
    for rid, row in zip(ids.tolist(), mats):  # add() sees exactly what a stream would read
        blob = col.db.execute("SELECT vec FROM vecs WHERE id=?", (rid,)).fetchone()[0]
        assert np.array_equal(row, np.frombuffer(blob, dtype=np.float16).astype(np.float32))
    assert col.db.execute("SELECT COUNT(*) FROM vecs WHERE id=5").fetchone()[0] == 0


def test_attach_backfills_rows_the_stream_missed(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "IVF_TRAIN_SAMPLE", 128)  # n=300: the sample fills from 295 rows
    col = make_collection(tmp_path)
    ingest(col, 300)
    legacy = [3, 50, 150, 222, 300]
    delete_vecs(col, legacy)
    log = record_sql(col)
    asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    assert isinstance(col.index, _IvfIndex) and len(col.index) == 300
    assert all(col.index.contains(i) for i in legacy)
    assert col.db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0] == 300
    assert not any("LEFT JOIN" in s for s, _ in log)  # found from the stream, no pre-scan


def test_attach_backfills_a_legacy_collection_before_training(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    delete_vecs(col, range(1, 301))  # every row predates vector retention
    asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    assert isinstance(col.index, _IvfIndex) and len(col.index) == 300
    assert col.db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0] == 300


def test_attach_fails_after_the_stream_for_textless_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "IVF_TRAIN_SAMPLE", 128)
    col = make_collection(tmp_path)
    ingest(col, 300)
    delete_vecs(col, [7, 8], drop_text=True)
    streamed, real = [], col._iter_vec_blocks
    col._iter_vec_blocks = lambda: streamed.append(1) or real()
    with pytest.raises(ValueError, match="2 records have neither a retained vector nor text"):
        asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    assert streamed == [1]  # detected from the stream, not a records x vecs pre-scan
    assert isinstance(col.index, IdMapIndex) and len(col.index) == 300
    assert not col.ivf_dir.exists() and col.cfg.index_config is None


def test_detach_refuses_rows_without_vectors(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    delete_vecs(col, [11, 12, 13])
    with pytest.raises(ValueError, match="3 records lack a retained vector; re-ingest them first"):
        asyncio.run(col._process_job({"op": "detach_index"}))
    assert isinstance(col.index, _IvfIndex) and len(col.index) == 300
    assert col.ivf_dir.exists() and col.cfg.index_config == {"nlist": 8, "nprobe": 16}
    assert not col.index_path.exists()


def test_index_jobs_run_no_anti_join_or_count(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 300)
    log = record_sql(col)
    asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    asyncio.run(col._process_job({"op": "detach_index"}))
    sqls = [s for s, _ in log]
    assert not any("LEFT JOIN" in s or "COUNT(" in s for s in sqls)  # p1: 178 s cold
    # per job: one live scan after the stream (missing vectors), one under the lock (swap)
    assert sqls.count("SELECT id FROM records WHERE indexed=1") == 4


def test_attach_logs_its_phase_timings(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOG)
    col = make_collection(tmp_path)
    ingest(col, 300)
    asyncio.run(col._process_job({"op": "attach_index", "nlist": 8}))
    msgs = [r.getMessage() for r in caplog.records
            if r.name == LOG and r.getMessage().startswith("attach_index t:")]
    assert len(msgs) == 1
    for field in ("n=300", "nlist=8", "prework=", "sample=", "legacy_backfill=0", "build=",
                  "live_scan=", "backfill=0 rows", "swap=", "total="):
        assert field in msgs[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cold_start.py -v -k "backfill or textless or refuses_rows or anti_join or phase_timings"`
Expected:
- `5 failed`:
  - `test_backfill_by_ids_embeds_only_those_rows_and_feeds_add` with `TypeError: Collection._backfill_vecs() takes 1 positional argument but 3 were given`;
  - `test_attach_backfills_rows_the_stream_missed` on `assert not any("LEFT JOIN" in s ...)`, because the pre-scan ran;
  - `test_attach_fails_after_the_stream_for_textless_rows` on `assert streamed == [1]`, because the pre-scan raised before the stream;
  - `test_index_jobs_run_no_anti_join_or_count` on the `LEFT JOIN` / `COUNT(` assertion;
  - `test_attach_logs_its_phase_timings` on `assert len(msgs) == 1`.
- `2 passed`: `test_attach_backfills_a_legacy_collection_before_training` and `test_detach_refuses_rows_without_vectors`. They pin the behaviour the rewrite must keep.

- [ ] **Step 3: Minimal implementation**

In `src/raggio/store.py`, in `_backfill_vecs`, replace

```python
    async def _backfill_vecs(self) -> None:
        """Records ingested before vector retention lack the fp16 original a rebuild
        needs. Re-embed from text where possible (assumes the collection's configured
        embedding model produced the stored vectors); otherwise fail the job with a
        count so the caller knows to re-ingest."""
        rows = await asyncio.to_thread(
            lambda: self._rdb().execute(
                "SELECT r.id, r.text FROM records r LEFT JOIN vecs v ON v.id=r.id"
                " WHERE r.indexed=1 AND v.id IS NULL"
            ).fetchall()
        )
        if not rows:
            return
```

with

```python
    async def _backfill_vecs(self, ids: np.ndarray | None = None, add=None) -> int:
        """Records ingested before vector retention lack the fp16 original a rebuild
        needs. Re-embed from text where possible (assumes the collection's configured
        embedding model produced the stored vectors); otherwise fail the job with a
        count so the caller knows to re-ingest.

        ids=None finds them with the records x vecs anti-join, a full records scan
        (p1: minutes cold at 2.55M rows) that attach only runs when too few retained
        vectors exist to train on. Otherwise `ids` are the rows the attach stream
        skipped, point-read by id. add(ids, f32 matrix) receives each written batch on
        a worker thread, so the caller never holds them all. Returns rows written."""
        if ids is None:
            rows = await asyncio.to_thread(
                lambda: self._rdb().execute(
                    "SELECT r.id, r.text FROM records r LEFT JOIN vecs v ON v.id=r.id"
                    " WHERE r.indexed=1 AND v.id IS NULL"
                ).fetchall()
            )
        else:
            rows = await asyncio.to_thread(
                lambda: list(_rows_by_id(
                    self._rdb(), "SELECT id, text FROM records WHERE indexed=1 AND id IN ({})",
                    ids.tolist(),
                ))
            )
        if not rows:
            return 0
```

and replace the end of `_backfill_vecs` together with the start of `_attach_index`

```python
            await asyncio.to_thread(write)

    async def _attach_index(self, payload: dict) -> None:
```

with

```python
            await asyncio.to_thread(write)
            if add is not None:
                await asyncio.to_thread(
                    add,
                    np.array([rid for rid, _ in batch], dtype=np.uint64),
                    vecs16.astype(np.float32),  # the fp16 round trip a stream would read
                )
        return len(rows)

    async def _attach_index(self, payload: dict) -> None:
```

In `_attach_index`, replace

```python
        # guard first (D13): validate and refuse before any SQL, so a full container
```

with

```python
        t0 = time.monotonic()
        # guard first (D13): validate and refuse before any SQL, so a full container
```

and replace

```python
        _require_headroom(_attach_need(n, self.cfg.dim, self.cfg.bit_width, nlist), "index build")
        await self._backfill_vecs()

        def build():
            ivf = _IvfIndex.train(
                self._vec_sample(IVF_TRAIN_SAMPLE), nlist, self.cfg.dim, self.cfg.bit_width,
                int(nprobe_req or IVF_DEFAULT_NPROBE),
            )
            seen = [np.empty(0, dtype=np.uint64)]
            for ids, mat in self._iter_vec_blocks():
                ivf.add_with_ids(mat, ids)
                seen.append(ids)
            return ivf, np.concatenate(seen)

        ivf, seen = await asyncio.to_thread(build)
        tmp = self.dir / "ivf.tmp"
```

with

```python
        _require_headroom(_attach_need(n, self.cfg.dim, self.cfg.bit_width, nlist), "index build")
        t1 = time.monotonic()
        # no records x vecs anti-join before the build (p1: 178 s cold with the COUNT):
        # rows without a retained vector are found from the stream below. A short sample
        # means retained vectors are scarce (ingested before retention): backfill them all
        # first, as before plan C, so k-means trains on a full sample
        sample = await asyncio.to_thread(self._vec_sample, IVF_TRAIN_SAMPLE)
        legacy = 0
        if len(sample) < min(n, IVF_TRAIN_SAMPLE):
            legacy = await self._backfill_vecs()
            sample = await asyncio.to_thread(self._vec_sample, IVF_TRAIN_SAMPLE)
        t2 = time.monotonic()

        def build():
            nonlocal sample
            tb = time.monotonic()
            ivf = _IvfIndex.train(
                sample, nlist, self.cfg.dim, self.cfg.bit_width,
                int(nprobe_req or IVF_DEFAULT_NPROBE),
            )
            sample = None  # free the f32 sample before the stream, as before plan C
            seen = [np.empty(0, dtype=np.uint64)]
            for ids, mat in self._iter_vec_blocks():
                ivf.add_with_ids(mat, ids)
                seen.append(ids)
            seen = np.concatenate(seen)
            tl = time.monotonic()
            # the indexed rows the stream skipped are exactly those without a retained
            # vector (this worker is the only adder); the live ids come from the covering
            # index, never the records pages
            missing = np.setdiff1d(_live_ids(self._rdb()), seen, assume_unique=True)
            return ivf, seen, missing, tl - tb, time.monotonic() - tl

        ivf, seen, missing, build_s, live_s = await asyncio.to_thread(build)
        t3 = time.monotonic()
        if len(missing):
            parts = [seen]

            def add(ids, mat):  # each re-embedded batch goes straight into the new index
                ivf.add_with_ids(mat, ids)
                parts.append(ids)

            await self._backfill_vecs(missing, add)
            seen = np.concatenate(parts)  # the swap diff covers them too
        t4 = time.monotonic()
        tmp = self.dir / "ivf.tmp"
```

and replace the end of the attach swap block

```python
            self._cal_reservoir = None  # shards were calibrated at train time
            self.index_path.unlink(missing_ok=True)  # flat file is stale from here on
```

with

```python
            self._cal_reservoir = None  # shards were calibrated at train time
            self.index_path.unlink(missing_ok=True)  # flat file is stale from here on
        t5 = time.monotonic()
        _log.info(
            "attach_index %s: n=%d nlist=%d prework=%.2fs sample=%.2fs legacy_backfill=%d"
            " build=%.2fs live_scan=%.2fs backfill=%d rows %.2fs swap=%.2fs total=%.2fs",
            self.cfg.name, n, nlist, t1 - t0, t2 - t1, legacy, build_s, live_s,
            len(missing), t4 - t3, t5 - t4, t5 - t0,
        )
```

In `_detach_index`, replace

```python
        # guard first (D13): a refusal must not cost the missing-vector scan
        _require_headroom(
            len(self.index) * self.cfg.dim * self.cfg.bit_width // 8, "index removal"
        )
        missing = await asyncio.to_thread(
            lambda: self._rdb().execute(
                "SELECT COUNT(*) FROM records r LEFT JOIN vecs v ON v.id=r.id"
                " WHERE r.indexed=1 AND v.id IS NULL"
            ).fetchone()[0]
        )
        if missing:
            raise ValueError(f"{missing} records lack a retained vector; re-ingest them first")
```

with

```python
        # guard first (D13): a refusal costs no SQL
        _require_headroom(
            len(self.index) * self.cfg.dim * self.cfg.bit_width // 8, "index removal"
        )
```

and replace

```python
            seen = [np.empty(0, dtype=np.uint64)]
            for ids, mat in self._iter_vec_blocks():
                flat.add_with_ids(mat, ids)
                seen.append(ids)
            return flat, np.concatenate(seen)
```

with

```python
            seen = [np.empty(0, dtype=np.uint64)]
            for ids, mat in self._iter_vec_blocks():
                flat.add_with_ids(mat, ids)
                seen.append(ids)
            seen = np.concatenate(seen)
            # rows the stream skipped have no retained vector. Every attach backfills
            # them all, so on an IVF collection this only trips if vecs rows were lost;
            # a stream before the refusal beats an anti-join on every detach
            missing = len(np.setdiff1d(_live_ids(self._rdb()), seen, assume_unique=True))
            if missing:
                raise ValueError(f"{missing} records lack a retained vector; re-ingest them first")
            return flat, seen
```

Notes for the implementer:
- `sample` is freed through `nonlocal` because a local `del` would leave the closure cell holding the 256 MiB f32 sample for the whole stream at 2.55M × 1024-d. That would push the swapless peak (§7 C threshold 7) above the p1 measurement the guard was sized on.
- The re-embedded rows are added in batches of 64 and never collected into one matrix, so a collection with many legacy rows cannot hold them all at once.
- A row deleted while its re-embedding is in flight is still diffed out by the swap, because `seen` includes it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cold_start.py -v -k "backfill or textless or refuses_rows or anti_join or phase_timings"`
Expected: `7 passed`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: `N0 + 64 passed`. These stay green:
- in `tests/test_ivf_index.py`: `test_attach_fails_without_vectors_or_text` (n=300 ≤ `IVF_TRAIN_SAMPLE`, so the short sample takes the legacy path and raises "re-ingest" before the build), `test_detach_restores_flat` and `test_index_api_lifecycle`;
- in this module: Task 3's `test_rows_deleted_during_a_build_are_diffed_out` and Task 8's refusal tests.

- [ ] **Step 6: Commit**

```bash
git add src/raggio/store.py tests/test_cold_start.py
git commit -m "Find rows without a retained vector from the build stream instead of a pre-scan" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: ADR correction, addendum, release note and index docs

**Files:**
- Modify: `docs/adr/0001-performance-optimization-decisions.md`:
  - line 33, the "Cheaper cold-start ghost reconciliation" row;
  - append at the very end, after Plan B's `## Addendum 2026-09 — concurrency correctness` (spec D1: C's addendum follows B's).
- Modify: `docs/storage.md`: a new `## Upgrading` section before `## Memory management` (line 49), and a memory-release paragraph at the end of that section, before `## Sizing` (line 69).
- Modify: `docs/indexing.md`: lines 60–65 (the "Rebuild" bullet) and 102–105 (the "Legacy rows" bullet).

**Interfaces:**
- Consumes:
  - the p1 figures (brief `docs/superpowers/research/2026-09-28-dgx-probe-p1-coldstart-sqlite-memory.md`);
  - the log messages of Tasks 1 and 9;
  - `ATTACH_NEED_FACTOR` (Task 8) and the Dockerfile line (Task 7).
- Produces:
  - the section `## Addendum 2026-09 — cold start and loop hygiene`, which Task 12 extends with a `### Acceptance run` subsection;
  - the release note `## Upgrading` in `docs/storage.md`.

- [ ] **Step 1: Write the failing check**

This docs task is checked with heading and phrase counts:

```bash
grep -c "^## Addendum 2026-09 — cold start and loop hygiene" docs/adr/0001-performance-optimization-decisions.md
grep -c "Correction (2026-09" docs/adr/0001-performance-optimization-decisions.md
grep -c "^## Upgrading" docs/storage.md
grep -c "MALLOC_TRIM_THRESHOLD_=134217728" docs/storage.md
grep -c "before any pre-work" docs/indexing.md
grep -c "found from the rebuild itself" docs/indexing.md
```

- [ ] **Step 2: Run it to verify it fails**

Run the six commands above. Expected: each prints `0` and exits with status 1.

- [ ] **Step 3: Minimal implementation**

The files are UTF-8 with em dashes, `×` and `·`: edit them with the editor, not a shell redirect.

In `docs/adr/0001-performance-optimization-decisions.md`, replace line 33

```markdown
| Cheaper cold-start ghost reconciliation | **Rejected (no API)** | `IdMapIndex` exposes no id enumeration or reconstruction; the k=n probe stays (~0.7 s at 553k), correctness over cold-start |
```

with

```markdown
| Cheaper cold-start ghost reconciliation | **Rejected (no API)**; corrected 2026-09 | `IdMapIndex` exposes no id enumeration or reconstruction; the k=n probe stays (~0.7 s at 553k), correctness over cold-start. **Correction (2026-09, see the cold-start addendum):** the probe was not the cost. At 2.55M rows, true-cold under a 4 GiB cap, the `indexed_counts` GROUP BY (39.4 s) and the reconcile's live-set scan (38.3 s) over the `records` pages were. Both now read a covering index and the diff runs in numpy; the k=n probe stays |
```

Append at the end of the file, after Plan B's addendum and one blank line:

```markdown
## Addendum 2026-09 — cold start and loop hygiene

Date: 2026-09-28 · Plan C of the 2026-09-28 performance program (`docs/superpowers/specs/2026-09-28-raggio-performance-design.md`). Direction-tested on gn100 (DGX Spark, aarch64) at 2,549,119 chunks × 1024 dims, 4-bit, `--memory 4g`, SQLite 3.40.1, by the p1 prototype (`docs/superpowers/research/2026-09-28-dgx-probe-p1-coldstart-sqlite-memory.md`). "True-cold" means the collection's files were evicted from the page cache with `POSIX_FADV_DONTNEED` (spec D12). The shipped code's acceptance run is recorded under "Acceptance run" below. Every row is pinned by `tests/test_cold_start.py`.

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| Covering index `idx_records_doc_type(doc_id, type, indexed)` replaces `idx_records_doc`; partial `idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')` | **Accepted** | `Collection()` true-cold at 4 GiB: 80.1 s → 3.4 s (IVF), reading 8.8 → 1.7 GB. Every later `GET /collections/{name}`: about 41 s → 0.57–0.59 s. Pending-jobs query true-cold 0.31 → 0.04 s |
| Build the covering index as a one-time migration inside `open_meta_db` (off the loop, progress logged, build before drop) | **Accepted** | 40.8 s true-cold, 1.73 s warm at 2.55M rows (about 16 s per 1M cold); meta.db +96 MB, 86 MB freelist left until `VACUUM`, WAL peak about 105 MB. A lazy background build was rejected: every scan would stay on the slow plan until it finished, and it adds a second writer during load |
| Pin the listing page query with `NOT INDEXED` | **Accepted (bug fix)** | After the migration the planner walks `idx_records_doc_type` for a sorted `type='chunk'` page: 0.91 → 2.73 s warm. `ANALYZE` leaves the plan unchanged (2.60 s); `NOT INDEXED` gives 0.76 s |
| Id-set diffs in numpy (`np.fromiter` + `np.setdiff1d(assume_unique=True)` over `uint64`) for ghost reconcile and the attach/detach swap | **Accepted** | 0.02 s against 0.3–0.73 s for the Python set diff, identical ghost sets. Both sides stay `uint64`: an `int64`/`uint64` mix promotes to `float64` |
| `Collection(...)` constructed in a worker thread under `_load_lock`, cancel-safe; `stats()` in a thread | **Accepted** | Baseline3: the first GET after a start blocked `/healthz` and every request for the whole 128 s load. The p1 prototype kept `/healthz` at ≤ 3 ms during a cold load. A cancelled request no longer orphans a half-built collection |
| `_vec_sample` by random rowids, with an `ORDER BY RANDOM()` fallback below id density 0.2 or when k > n/2 | **Accepted** | k = 65,536: 110.5 s true-cold (16.9 GB read) → 8.05 s (829 MB); 9.35 → 0.39 s warm |
| `ENV MALLOC_TRIM_THRESHOLD_=134217728` in the image plus `malloc_trim(0)` after every index job | **Accepted** | With default glibc a swapless 4 GiB `POST /index` was OOM-killed mid-build after the headroom guard had passed, and with swap the post-job RSS stayed at 3.0 GiB against 1.5 GiB loaded, so the next job was refused. With both: HWM 3,631–3,646 MiB, RSS 1,506–1,707 MiB after each job |
| `malloc_trim(0)` alone, or `MALLOC_ARENA_MAX=2` | **Rejected** | Trim alone was OOM-killed like default glibc. `MALLOC_ARENA_MAX=2` gave no memory benefit and 10–20 % slower builds (57–63 s against 52 s) |
| Headroom guard before any SQL, need × `ATTACH_NEED_FACTOR` (1.25) | **Accepted** | Cold refusals wasted 139 s (detach) and 194 s (attach) of pre-work before the guard ran. The summed need (1,757 MiB, 1,884 MiB with the margin) was below the measured growth of 2,176–2,186 MiB; × 1.25 gives 2,195 MiB. The guard reads `memory.max − VmRSS` and ignores swap, so it stays conservative and the swapless run is the gate |
| Rows without a retained vector found from the build stream (live ids − streamed ids) instead of an anti-join before the build | **Accepted** | The pre-work (backfill `LEFT JOIN` + `COUNT`) took 178 s true-cold. At 1024-d each ~2 KB fp16 vector fills its own 4 KiB `vecs` page, so any existence check over `vecs` visits every leaf, about 10.5 GB (derived, not measured); the stream reads exactly those pages anyway. A collection with too few retained vectors to fill the training sample still backfills everything first. A detach that finds missing vectors now fails after its stream rather than before; an attach backfills every such row, so that only happens if `vecs` rows were lost |
| The k=n probe in `_reconcile_ghosts` | **Unchanged** | `IdMapIndex` still exposes no id enumeration |
```

In `docs/storage.md`, replace

```markdown
`GET /collections/{name}/documents?include_vector=true` and re-ingest.

## Memory management
```

with

```markdown
`GET /collections/{name}/documents?include_vector=true` and re-ingest.

## Upgrading

**2026-09 release (cold start and loop hygiene).** The first open of each
collection after the upgrade runs a one-time migration in its `meta.db`: it
builds the covering index `idx_records_doc_type` (doc_id, type, indexed), then
drops the old `idx_records_doc`, and adds a small partial index over open jobs.

- **Time:** about 16 s per 1M records when the file is not in the page cache
  (40.8 s at 2.55M records on a DGX Spark under a 4 GiB cap), 1.7 s at 2.55M
  when it is. Requests to that collection wait for it; `/healthz` and collections
  that are already loaded keep answering.
- **Disk:** `meta.db` grows by the new index (+96 MB at 2.55M records). About
  86 MB of free pages from the dropped index stay in the file until a `VACUUM`,
  and the WAL peaks around 105 MB during the build.
- **Logs:** the start (`one-time migration`), a heartbeat every 10 s
  (`still building`) and the finish (`built idx_records_doc_type in`) are logged
  at INFO.
- **Interruption is safe:** the old index is dropped only after the new one is
  built, and the next open finishes whatever is missing.

## Memory management
```

and replace

```markdown
    collections to fit memory and spread data over multiple collections.

## Sizing
```

with

```markdown
    collections to fit memory and spread data over multiple collections.

Index jobs (attach and remove) hold a second copy of the index while they build.
When a job ends, raggio calls glibc's `malloc_trim(0)`, and the image sets
`ENV MALLOC_TRIM_THRESHOLD_=134217728`, so the build's buffers go back to the OS
instead of staying in the process heap. Without them, a 2.55M × 1024-d collection
stayed at about 3.0 GiB after a build against 1.5 GiB loaded, the next index job was
refused, and a container without swap was OOM-killed mid-build. Images built
without the shipped `Dockerfile` should set the same variable.

## Sizing
```

In `docs/indexing.md`, replace

```markdown
  old representation until the swap; expect transiently ~2x index RAM plus ~0.4 GB
  for the k-means training sample at 1536 dims. If the container lacks that headroom
  the job fails with a clear error (instead of an OOM kill) — raise the memory limit
  and re-attach.
```

with

```markdown
  old representation until the swap; expect transiently ~2x index RAM plus ~0.4 GB
  for the k-means training sample at 1536 dims. The memory check runs first,
  before any pre-work, and reserves the measured growth (about 2.2 GB for 2.55M ×
  1024-d at 4 bits with `nlist=256`). If the container lacks that headroom the job fails at
  once with a clear error (instead of an OOM kill) — raise the memory limit and
  re-attach. After each index job raggio hands the freed build memory back to the
  OS, so a following remove or re-attach finds it free again.
```

and replace

```markdown
  endpoint — correct only if that endpoint produced the original vectors. Vector-only
  legacy rows make the attach job fail with a count; re-ingest them first.
```

with

```markdown
  endpoint — correct only if that endpoint produced the original vectors. They are
  found from the rebuild itself: a few legacy rows are re-embedded after the rest has
  streamed, and a collection with too few retained vectors to train on backfills them
  all first. Vector-only legacy rows make the attach job fail with a count, after the
  stream and before anything is swapped; re-ingest them first.
```

- [ ] **Step 4: Run the check and the full suite**

Run the six `grep -c` commands from Step 1. Expected: each prints `1`.
Run: `uv run pytest -q`. Expected: `N0 + 64 passed` (docs only, so no change).

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0001-performance-optimization-decisions.md docs/storage.md docs/indexing.md
git commit -m "Document the cold-start migration, memory release and index-job pre-work" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: DGX acceptance scripts, checked on the laptop

This task writes the five scripts that Task 12 runs, and checks them on the laptop. They live outside the repo, next to the other DGX runbooks, so this task has no commit. It never contacts gn100.

What the scripts implement, from the spec:
- **True-cold (D12):** `c_evict.py` runs under `podman unshare` on gn100. It fsyncs each file of the `bench-tv` volume, then calls `posix_fadvise(POSIX_FADV_DONTNEED)` on it, and verifies the result with `mincore`. The step fails if more than 16 MB stays resident.
  - The loaded index lives in anonymous memory: p1 measured `RssFile` at 25 MiB. The volume can therefore be evicted while a server runs, and P4 relies on that to make its first attach true-cold.
- **Swapless gate (D13):** the memory run uses `--memory 4g --memory-swap 4g`. Each snapshot records `memory.swap.max`, which must read `0`.
- **Labels (§6):** every row records `sqlite3.sqlite_version` and `OPENBLAS_NUM_THREADS`, both read inside the image. Every measurement row also records its regime (`true-cold` or `host-warm`) and its memory cap (`4g` or `4g-swapless`).
- **Thresholds (§7 C 1–9):** `c_verdict.py` computes them from the rows, as listed under Interfaces.

**Files (all outside the repo, in `D:/DEV/_scratch/dgx/planC/`, LF line endings):**
- Create: `c_check.py`, the laptop check (this task's test).
- Create: `c_evict.py`, D12 eviction, warm-up and check (host side, gn100).
- Create: `c_probe.py`, the in-image probes `layout`, `legacy`, `load [--list]`.
- Create: `c_session.sh`, the gn100 session.
- Create: `c_verdict.py`, the verdict and the ADR insert.

**Interfaces:**
- Consumes:
  - the migration log line `built idx_records_doc_type in %.1f s` (Task 1);
  - the NOT INDEXED page query (Task 2);
  - `CollectionManager.touch` (Task 4), `Collection._vec_sample` (Task 6), `Collection.list_records`, `Collection._rdb`;
  - the `attach_index <name>: ... prework=... live_scan=... backfill=N rows S s ...` log line (Task 9);
  - the addendum heading of Task 10;
  - Plan A's image facts snippet: `python`, `sqlite_version`, `turbovec`, `openblas_num_threads`.
- Produces, for Task 12:
  - `c_session.sh LABEL IMAGE` writes `~/planC/LABEL/` with:
    - `rows.jsonl`: one JSON object per line, `kind` ∈ `session`, `migrate`, `start`, `load`, `job`, `mem`, `end`, `layout`;
    - `server-*.log`, `hz-*.txt`, `get-*.json`, `evict.log`, `probe.log`, `facts.txt`, `image.json` and `session.pgid`.
    - The session log ends with `== PLANC DONE ==` or `== PLANC FAILED: <reason> ==`.
  - `c_verdict.py DIR [--adr ADR]` prints a `### Acceptance run` markdown block ending in ten lines `- PASS|FAIL: <C1…C9|labels> — <detail>`. It exits 1 on any FAIL. `--adr` inserts the block only when every line passes.
  - The verdict rules:
    - **C1:** per index, the median over the three true-cold `p2` starts of start → first GET is ≤ 6 s (IVF) and ≤ 5 s (flat).
    - **C2:** the slowest `/healthz` answer seen during those six loads is ≤ 0.1 s, and every load was probed at least once.
    - **C3:** per index, the median over the runs of each run's slowest later GET (3 per run) is ≤ 1.0 s.
    - **C4:** each true-cold `load` read (`/proc/self/io` `read_bytes` over `touch()`) is ≤ its index files + 150 MB, for both indexes.
    - **C5:**
      - cold CREATE ≤ 60 s and warm CREATE ≤ 3 s, from the servers' migration log lines;
      - meta.db growth ≤ 120 MB. The gate takes the larger of two measures:
        - the file's growth over the true-cold migration;
        - the covering index's size, measured as the pages its drop frees.

        A re-migration reuses free pages, so its file growth alone would understate the index.
    - **C6:** every true-cold `_vec_sample(65536)` is ≤ 15 s and every host-warm one ≤ 1 s, each returning 65,536 rows.
    - **C7:** in the swapless memory run (POST → DELETE → POST):
      - all three jobs are `done`, so the detach and the second attach passed the guard;
      - every snapshot shows `oom_kill` 0, VmSwap 0 and `memory.swap.max` 0;
      - the container still runs and was not OOM-killed;
      - max VmHWM ≤ 3.8 GiB (3,891 MiB);
      - VmRSS 15 s after each job is at most 512 MiB above the loaded VmRSS.
    - **C8:** per index, the median of three host-warm `list_records("chunks", None, "-year", 20, 0)` page-query times is ≤ 1.0 s, with the page SQL carrying `NOT INDEXED` each time.
    - **C9:** from the first `attach_index bench:` line of the memory run (a true-cold attach), `prework` + `live_scan` + backfill seconds ≤ 10 s. These are the guard plus the work that replaced the 178 s pre-scan.
    - **labels:** every row has `sqlite_version` and `openblas_num_threads`, and every `start`, `load`, `migrate`, `job` and `mem` row has `regime` and `cap`.

- [ ] **Step 1: Write the failing check**

Create `D:/DEV/_scratch/dgx/planC/c_check.py`:

```python
"""Laptop check of the Plan C DGX acceptance scripts (plan Task 11).

Run it from the raggio repo root on the Plan C branch, in Git Bash:

    uv run python D:/DEV/_scratch/dgx/planC/c_check.py

It never contacts gn100. It checks the session script's syntax and every script's
line endings, compiles the Python scripts, runs c_probe.py against a 60-row `bench`
collection built with the branch's code, and runs c_verdict.py on synthetic rows:
a pass, two fails, and the ADR insert into LF and CRLF files."""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
NAMES = ("c_evict.py", "c_probe.py", "c_session.sh", "c_verdict.py")
ADR = Path("docs/adr/0001-performance-optimization-decisions.md")
ADDENDUM = "## Addendum 2026-09 — cold start and loop hygiene"
HEADING = "### Acceptance run"
LINES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "labels")
DIM = 8
ATTACH_LINE = (
    "INFO:     attach_index bench: n=2549119 nlist=256 prework=0.01s sample=8.10s"
    " legacy_backfill=0 build=182.40s live_scan=1.20s backfill=0 rows 0.00s swap=4.10s"
    " total=195.81s\n"
)


def fail(msg):
    raise SystemExit(f"FAILED: {msg}")


def expect(ok, msg):
    if not ok:
        fail(msg)


def run(*args, env=None):
    return subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )


def check_files():
    missing = [n for n in NAMES if not (HERE / n).exists()]
    if missing:
        fail("missing " + ", ".join(missing))
    for n in (*NAMES, "c_check.py"):
        expect(b"\r" not in (HERE / n).read_bytes(), f"{n} has CR line endings; gn100 needs LF")
    bash = shutil.which("bash")
    expect(bash is not None, "bash is not on PATH: run this from Git Bash")
    r = run(bash, "-n", (HERE / "c_session.sh").as_posix())
    expect(r.returncode == 0, f"bash -n c_session.sh: {r.stderr.strip()}")
    for n in NAMES:
        if n.endswith(".py"):
            compile((HERE / n).read_text(encoding="utf-8"), str(HERE / n), "exec")
    print("syntax: bash -n c_session.sh and compile() of the three .py scripts ok")


class Emb:
    """Stand-in embedder: every chunk below carries its vector, so it is never asked."""

    async def embed(self, texts):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    async def aclose(self):
        pass


async def build(data):
    from raggio.config import Settings
    from raggio.store import CollectionManager

    os.environ["DATA_DIR"] = str(data)
    mgr = CollectionManager(Settings(), lambda cfg: Emb())
    try:
        await mgr.create_collection("bench", DIM, 4, None, None, None)
        c = await mgr.touch("bench")
        vecs = np.random.default_rng(7).standard_normal((60, DIM))
        docs = [
            {"doc_id": f"d{i}", "chunks": [{
                "id": f"c{i}", "text": f"chunk {i}", "vector": (v / np.linalg.norm(v)).tolist(),
                "metadata": {"year": 1990 + i % 30},
            }]}
            for i, v in enumerate(vecs)
        ]
        await c._process_job({"documents": docs})
    finally:
        await mgr.shutdown()


def probe(data, *args):
    r = run(sys.executable, HERE / "c_probe.py", *args, env={**os.environ, "DATA_DIR": str(data)})
    name = " ".join(args)
    expect(r.returncode == 0, f"c_probe.py {name} exited {r.returncode}:\n{r.stderr[-3000:]}")
    out = r.stdout.splitlines()
    expect(len(out) == 1, f"c_probe.py {name} printed {len(out)} stdout lines, not 1:\n{r.stdout}")
    return json.loads(out[0]), r.stderr


def check_probe(tmp):
    data = tmp / "data"
    asyncio.run(build(data))
    lay, _ = probe(data, "layout")
    expect(lay["migrated"] is True and lay["open_jobs"] == 0 and lay["index"] == "flat",
           f"layout of a new collection: {lay}")
    leg, _ = probe(data, "legacy")
    expect("idx_records_doc" in leg["indexes"] and "idx_records_doc_type" not in leg["indexes"]
           and "idx_jobs_open" not in leg["indexes"] and leg["freed_mb"] >= 0, f"legacy: {leg}")
    lay, _ = probe(data, "layout")
    expect(lay["migrated"] is False, f"layout after legacy: {lay}")
    load, err = probe(data, "load", "--list")
    expect("built idx_records_doc_type in" in err, f"load logged no migration:\n{err[-3000:]}")
    runs = load["list"]
    expect(load["index"] == "flat" and load["vec_sample_rows"] == 60 and len(runs) == 3
           and all(x["pinned"] and x["rows"] == 20 and x["total"] == 60 for x in runs),
           f"load --list: {load}")
    expect(load["sqlite_version"] == sqlite3.sqlite_version, f"load: {load}")
    lay, _ = probe(data, "layout")
    expect(lay["migrated"] is True, f"layout after load: {lay}")
    r = run(sys.executable, HERE / "c_probe.py", "bogus")
    expect(r.returncode == 2, f"c_probe.py bogus exited {r.returncode}, not 2")
    print("c_probe.py: layout, legacy, load --list and the usage error ok on a 60-row collection")


def synthetic(hwm=3646, pinned=True):
    """Rows shaped like c_session.sh's, with p1's values: all ten lines pass."""
    lab = {"sqlite_version": "3.53.1", "openblas_num_threads": "1",
           "image": "localhost/raggio:0000000"}
    rows = [
        {"kind": "session", "label": "check", "date": "2026-09-28T00:00:00Z",
         "image_id": "sha256:" + "0" * 64, "python": "3.12.14", "turbovec": "1.0.0"},
        {"kind": "migrate", "regime": "true-cold", "cap": "4g", "create_s": 40.8,
         "first_migration": True, "growth_mb": 96.0, "covering_mb": 96.1, "freelist_mb": 86.0},
        {"kind": "migrate", "regime": "host-warm", "cap": "4g", "create_s": 1.7,
         "first_migration": False},
    ]
    for s, ms, index_mb, read_mb in (("ivf", 4320, 1626.0, 1700.0), ("flat", 3340, 1306.0, 1380.0)):
        for i in range(3):
            rows.append({
                "kind": "start", "run": f"p2-{s}-{i + 1}", "phase": "p2", "regime": "true-cold",
                "cap": "4g", "index": s, "chunks": 2549119, "dim": 1024, "pending_jobs": 0,
                "healthz_ms": 600, "first_get_ms": ms - 600 + i, "start_to_get_ms": ms + i,
                "hz_n": 12, "hz_max_s": 0.003, "later_ms": [570, 590, 580],
            })
        base = {"index": s, "index_mb": index_mb, "touch_s": 3.4, "vec_sample_rows": 65536}
        rows.append({"kind": "load", "index_state": s, "regime": "true-cold", "cap": "4g",
                     "probe": {**base, "touch_read_mb": read_mb, "vec_sample_s": 8.05}})
        rows.append({"kind": "load", "index_state": s, "regime": "host-warm", "cap": "4g",
                     "probe": {**base, "touch_read_mb": 0.0, "vec_sample_s": 0.39, "list": [
                         {"page_s": 0.76, "call_s": 0.84, "pinned": pinned, "rows": 20,
                          "total": 2549119}] * 3}})
    snaps = (("loaded", 1455, 1737, "true-cold"), ("after_attach1", 1706, 3631, "true-cold"),
             ("after_attach1_15s", 1724, 3631, "true-cold"), ("after_detach", 1506, hwm, "host-warm"),
             ("after_detach_15s", 1532, hwm, "host-warm"), ("after_attach2", 1662, hwm, "host-warm"),
             ("after_attach2_15s", 1673, hwm, "host-warm"))
    for phase, rss, peak, regime in snaps:
        rows.append({"kind": "mem", "run": "p4", "phase": phase, "regime": regime,
                     "cap": "4g-swapless", "vmrss": rss, "vmhwm": peak, "vmswap": 0, "oom_kill": 0,
                     "cg_current": rss + 520, "swap_max": 0})
    for name, regime, wall in (("attach1", "true-cold", 205000), ("detach", "host-warm", 25900),
                               ("attach2", "host-warm", 52000)):
        rows.append({"kind": "job", "name": name, "regime": regime, "cap": "4g-swapless",
                     "status": "done", "error": None, "wall_ms": wall})
    rows.append({"kind": "end", "run": "p4", "regime": "host-warm", "cap": "4g-swapless",
                 "running": True, "oom_flag": False})
    return [{**r, **lab} for r in rows]


def write_run(d, rows):
    d.mkdir()
    (d / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (d / "server-p4.log").write_text(ATTACH_LINE, encoding="utf-8")
    return d


def verdict(*args):
    return run(sys.executable, HERE / "c_verdict.py", *args)


def check_verdict(tmp):
    good = write_run(tmp / "good", synthetic())
    r = verdict(good)
    expect(r.returncode == 0, f"verdict on passing rows exited {r.returncode}:\n{r.stdout}{r.stderr}")
    expect(r.stdout.startswith(HEADING + "\n"), f"the verdict starts {r.stdout[:60]!r}")
    for name in LINES:
        expect(f"- PASS: {name} — " in r.stdout, f"no '- PASS: {name} — ' line:\n{r.stdout}")
    for phrase in ("sqlite3.sqlite_version", "OPENBLAS_NUM_THREADS", "true-cold", "--memory-swap 4g"):
        expect(phrase in r.stdout, f"the verdict text lacks {phrase!r}")
    bad = {}
    for name, rows in (("C7", synthetic(hwm=3950)), ("C8", synthetic(pinned=False))):
        bad[name] = write_run(tmp / f"bad-{name}", rows)
        r = verdict(bad[name])
        expect(r.returncode == 1 and f"- FAIL: {name} — " in r.stdout and r.stdout.count("- FAIL:") == 1,
               f"verdict on the {name} case:\n{r.stdout}{r.stderr}")
    print("c_verdict.py: all-pass rows, the C7 (VmHWM 3,950 MiB) and C8 (unpinned) fails ok")
    return good, bad["C7"]


def check_adr(tmp, good, bad):
    expect(ADR.exists(), f"{ADR} not found: run this from the repo root")
    text = ADR.read_text(encoding="utf-8")
    expect(ADDENDUM in text, f"{ADR} has no '{ADDENDUM}' (Task 10)")
    expect(HEADING not in text[text.index(ADDENDUM):], f"{ADR} already records an acceptance run")
    lf = tmp / "adr-lf.md"
    lf.write_bytes(text.encode("utf-8"))
    r = verdict(good, "--adr", lf)
    new = lf.read_bytes()
    body = new.decode("utf-8")
    expect(r.returncode == 0 and b"\r" not in new and body.count(HEADING) == 1
           and body.index(ADDENDUM) < body.index(HEADING), f"LF insert: {r.returncode} {r.stderr}")
    r = verdict(good, "--adr", lf)
    expect(r.returncode != 0 and "already" in r.stderr and lf.read_bytes() == new,
           f"a second --adr must refuse: {r.returncode} {r.stderr}")
    crlf = tmp / "adr-crlf.md"
    later = text.rstrip("\n") + "\n\n## Later\n\nText.\n"
    crlf.write_bytes(later.replace("\n", "\r\n").encode("utf-8"))
    r = verdict(good, "--adr", crlf)
    raw = crlf.read_bytes()
    body = raw.decode("utf-8")
    expect(r.returncode == 0 and raw.count(b"\n") == raw.count(b"\r\n")
           and body.index(ADDENDUM) < body.index(HEADING) < body.index("## Later"),
           f"CRLF insert before the next section: {r.returncode} {r.stderr}")
    kept = tmp / "adr-kept.md"
    kept.write_bytes(text.encode("utf-8"))
    r = verdict(bad, "--adr", kept)
    expect(r.returncode == 1 and kept.read_bytes() == text.encode("utf-8"),
           f"a failing verdict must leave the ADR alone: {r.returncode} {r.stderr}")
    print("c_verdict.py --adr: LF and CRLF inserts, no second insert, no insert on FAIL ok")


def main():
    expect(Path("src/raggio/store.py").exists(), "run this from the raggio repo root")
    check_files()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        tmp = Path(t)
        check_probe(tmp)
        good, bad = check_verdict(tmp)
        check_adr(tmp, good, bad)
    print("Plan C DGX scripts OK")


if __name__ == "__main__":
    main()
```

Write it with LF line endings: the Write tool writes the text as given, and `c_check.py` checks itself.

- [ ] **Step 2: Run it to verify it fails**

From the repo root in Git Bash:

Run: `uv run python D:/DEV/_scratch/dgx/planC/c_check.py`
Expected: exit status 1, with the single line `FAILED: missing c_evict.py, c_probe.py, c_session.sh, c_verdict.py`.

- [ ] **Step 3: Write the D12 eviction script**

Create `D:/DEV/_scratch/dgx/planC/c_evict.py`:

```python
"""D12 page-cache control for the bench-tv volume (plan Task 11). Run on gn100 as

    podman unshare python3 ~/planC/c_evict.py [--warm|--check] DIR

Default (evict): fsync, then posix_fadvise(POSIX_FADV_DONTNEED) every regular file under
DIR, and verify with mincore(2) that at most 16 MB stays resident (exit 1 otherwise).
--warm reads every file once (host-warm); --check only reports. Prints one line:
EVICT|WARM|CHECK {"files", "total_mb", "resident_mb", "resident_pct", "s"}.
A running server may keep the volume open: the index it serves is anonymous memory,
so only SQLite's small mapped -shm file cannot be dropped."""
import ctypes
import json
import os
import stat
import sys
import time

PAGE = os.sysconf("SC_PAGE_SIZE")
LIMIT_MB = 16
MB = 1 << 20
PROT_READ = 1
MAP_SHARED = 1

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      ctypes.c_long]
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
MAP_FAILED = ctypes.c_void_p(-1).value
LOW_BIT = bytes(i & 1 for i in range(256))  # mincore: bit 0 of each byte = page resident


def resident_pages(fd, size):
    """Pages of the file in the page cache. The mapping is never touched, so it faults nothing in."""
    if size == 0:
        return 0
    addr = libc.mmap(None, size, PROT_READ, MAP_SHARED, fd, 0)
    if addr in (None, MAP_FAILED):
        err = ctypes.get_errno()
        raise OSError(err, "mmap: " + os.strerror(err))
    try:
        vec = (ctypes.c_ubyte * ((size + PAGE - 1) // PAGE))()
        if libc.mincore(addr, size, vec) != 0:
            err = ctypes.get_errno()
            raise OSError(err, "mincore: " + os.strerror(err))
        return bytes(vec).translate(LOW_BIT).count(1)
    finally:
        libc.munmap(addr, size)


def main(argv):
    mode = "evict"
    if argv and argv[0] in ("--warm", "--check"):
        mode = argv.pop(0)[2:]
    if len(argv) != 1 or not os.path.isdir(argv[0]):
        print(__doc__, file=sys.stderr)
        return 2
    t0 = time.monotonic()
    files = total = resident = 0
    for root, _dirs, names in os.walk(argv[0]):
        for name in names:
            path = os.path.join(root, name)
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                fd = os.open(path, os.O_RDONLY)
            except FileNotFoundError:  # a WAL or temp file that went away
                continue
            try:
                if mode == "evict":
                    try:
                        os.fsync(fd)  # DONTNEED skips dirty pages
                    except OSError:
                        pass
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                elif mode == "warm":
                    while os.read(fd, 16 << 20):
                        pass
                size = os.fstat(fd).st_size
                resident += min(resident_pages(fd, size) * PAGE, size)
                total += size
                files += 1
            finally:
                os.close(fd)
    out = {
        "files": files,
        "total_mb": round(total / MB, 1),
        "resident_mb": round(resident / MB, 1),
        "resident_pct": round(100 * resident / total, 2) if total else 0.0,
        "s": round(time.monotonic() - t0, 2),
    }
    print(mode.upper(), json.dumps(out), flush=True)
    return 1 if mode == "evict" and resident / MB > LIMIT_MB else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

This one cannot run on Windows (`posix_fadvise`, `libc.so.6`), so the laptop check only compiles it. Task 12 Step 4 smoke-tests it on gn100 with `--check` before the session.

- [ ] **Step 4: Write the in-image probes**

Create `D:/DEV/_scratch/dgx/planC/c_probe.py`:

```python
"""In-image probes of the Plan C acceptance run (plan Task 11). Run in the image, with the
server stopped (the container takes the server's name, so both cannot run at once):

    podman run --rm --name bench-tv --memory 4g -v bench-tv:/data -v ~/planC:/p:ro \
        --entrypoint python IMAGE /p/c_probe.py layout|legacy|load [--list]

layout  meta.db's indexes, open jobs, page counts and file sizes; index type and size
legacy  put meta.db back on the pre-Plan C indexes, reporting the pages the drop frees
load    CollectionManager.touch() (time, bytes read), _vec_sample(65536), and with
        --list three list_records("chunks", None, "-year", 20, 0) page queries timed
        at the SQL

It prints exactly one JSON line on stdout. Logs, including the migration's, go to stderr."""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
NAME = "bench"
COL = DATA / "collections" / NAME
MB = 1 << 20
NEW = ("idx_records_doc_type", "idx_jobs_open")
OLD = "idx_records_doc"


def mb(n):
    return round(n / MB, 1)


def read_bytes():
    """Bytes this process caused to be read from storage (Linux), else None."""
    try:
        for line in Path("/proc/self/io").read_text().splitlines():
            if line.startswith("read_bytes:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def delta_mb(a, b):
    return None if a is None or b is None else mb(b - a)


def status_mb():
    try:
        text = Path("/proc/self/status").read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("VmRSS", "VmHWM"):
            out[key.lower() + "_mb"] = int(rest.split()[0]) // 1024
    return out


def memory_max():
    try:
        return Path("/sys/fs/cgroup/memory.max").read_text().strip()
    except OSError:
        return None


def index_state():
    cat = sqlite3.connect(DATA / "catalog.db")
    try:
        row = cat.execute("SELECT index_config FROM collections WHERE name=?", (NAME,)).fetchone()
    finally:
        cat.close()
    if row is None:
        raise SystemExit(f"no collection {NAME!r} under {DATA}")
    ivf = COL / "ivf"
    if row[0] and (ivf / "centroids.npy").exists():  # the rule Collection() applies
        return "ivf", mb(sum(p.stat().st_size for p in ivf.rglob("*") if p.is_file()))
    flat = COL / "index.tvim"
    return "flat", mb(flat.stat().st_size) if flat.exists() else 0.0


def pages(db):
    return {k: db.execute(f"PRAGMA {k}").fetchone()[0] for k in ("page_size", "page_count", "freelist_count")}


def indexes(db):
    return sorted(r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name IN ('records','jobs')"
        " AND sql IS NOT NULL"
    ))


def layout():
    db = sqlite3.connect(COL / "meta.db")
    try:
        names = indexes(db)
        open_jobs = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
        ).fetchone()[0]
        p = pages(db)
    finally:
        db.close()
    index, index_mb = index_state()
    size = p["page_size"]
    return {
        "indexes": names,
        "migrated": set(NEW) <= set(names) and OLD not in names,
        "open_jobs": open_jobs,
        **p,
        "db_mb": mb(p["page_count"] * size),
        "used_mb": mb((p["page_count"] - p["freelist_count"]) * size),
        "freelist_mb": mb(p["freelist_count"] * size),
        "files_mb": mb(sum(f.stat().st_size for f in COL.glob("meta.db*"))),
        "index": index,
        "index_mb": index_mb,
    }


def legacy():
    """The pre-Plan C index set, so the next open runs the one-time migration again.
    Each DDL statement autocommits (pysqlite opens no transaction for DDL)."""
    db = sqlite3.connect(COL / "meta.db")
    try:
        before = pages(db)
        t0 = time.perf_counter()
        db.execute("DROP INDEX IF EXISTS idx_records_doc_type")
        db.execute("DROP INDEX IF EXISTS idx_jobs_open")
        drop_s = time.perf_counter() - t0
        after = pages(db)
        t0 = time.perf_counter()
        db.execute("CREATE INDEX IF NOT EXISTS idx_records_doc ON records(doc_id)")
        create_s = time.perf_counter() - t0
        names = indexes(db)
    finally:
        db.close()
    used = lambda p: p["page_count"] - p["freelist_count"]  # noqa: E731
    return {
        "drop_s": round(drop_s, 2),
        "create_legacy_s": round(create_s, 2),
        "freed_mb": mb((used(before) - used(after)) * before["page_size"]),
        "indexes": names,
    }


class Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class TimedConn:
    """Stands in for the collection's read connection: runs and fully fetches each
    statement, recording (sql, seconds)."""

    def __init__(self, db):
        self.db = db
        self.calls = []

    def execute(self, sql, params=()):
        t0 = time.perf_counter()
        rows = Rows(self.db.execute(sql, params).fetchall())
        self.calls.append((sql, time.perf_counter() - t0))
        return rows


def listing(c):
    tc = TimedConn(c._rdb())
    c._rdb = lambda: tc  # instance attribute shadows the method for this call only
    try:
        t0 = time.perf_counter()
        out = c.list_records("chunks", None, "-year", 20, 0)
        call_s = time.perf_counter() - t0
    finally:
        del c._rdb
    page = [(sql, s) for sql, s in tc.calls if "ORDER BY" in sql and "LIMIT" in sql]
    if len(page) != 1:
        raise RuntimeError(f"expected one page query, saw: {[sql for sql, _ in tc.calls]}")
    sql, page_s = page[0]
    return {
        "page_s": round(page_s, 3),
        "call_s": round(call_s, 3),
        "pinned": "NOT INDEXED" in sql,
        "rows": len(out["records"]),
        "total": out["total"],
    }


async def load(with_list):
    from raggio.config import Settings
    from raggio.store import CollectionManager

    index, index_mb = index_state()
    mgr = CollectionManager(Settings())
    try:
        r0, t0 = read_bytes(), time.perf_counter()
        c = await mgr.touch(NAME)
        touch_s, r1 = time.perf_counter() - t0, read_bytes()
        t0 = time.perf_counter()
        sample = await asyncio.to_thread(c._vec_sample, 65536)
        sample_s, r2 = time.perf_counter() - t0, read_bytes()
        out = {
            "index": index,
            "index_mb": index_mb,
            "touch_s": round(touch_s, 2),
            "touch_read_mb": delta_mb(r0, r1),
            "vec_sample_s": round(sample_s, 2),
            "vec_sample_rows": int(len(sample)),
            "vec_sample_read_mb": delta_mb(r1, r2),
        }
        if with_list:
            out["list"] = [await asyncio.to_thread(listing, c) for _ in range(3)]
        out.update(status_mb())
    finally:
        await mgr.shutdown()
    out.update(
        memory_max=memory_max(),
        sqlite_version=sqlite3.sqlite_version,
        openblas_num_threads=os.environ.get("OPENBLAS_NUM_THREADS"),
    )
    return out


def main(argv):
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if argv == ["layout"]:
        out = layout()
    elif argv == ["legacy"]:
        out = legacy()
    elif argv[:1] == ["load"] and argv[1:] in ([], ["--list"]):
        out = asyncio.run(load(argv[1:] == ["--list"]))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps({"mode": argv[0], **out}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

`layout` and `legacy` open `meta.db` with plain `sqlite3`, not `open_meta_db`, so they never trigger the migration themselves. The migration's log lines use the `uvicorn.error` logger (Task 1). Here they reach stderr through the root handler, which is how `c_check.py` sees `built idx_records_doc_type in`.

- [ ] **Step 5: Write the gn100 session**

Create `D:/DEV/_scratch/dgx/planC/c_session.sh`:

```bash
#!/usr/bin/env bash
# Plan C acceptance session on gn100 (plan Task 11): the rows behind spec §7 C 1-9.
# Launch it detached, then poll its log:
#   setsid -f timeout --kill-after=60 60m bash ~/planC/c_session.sh LABEL IMAGE \
#       > ~/planC/LABEL.log 2>&1 < /dev/null
# Stop it early:
#   kill -TERM -- -"$(cat ~/planC/LABEL/session.pgid)"; podman stop -t 60 --ignore bench-tv
# It uses only the bench-tv container and the bench-tv volume, and writes ~/planC/LABEL/.
# Order: the one-time migration (true-cold, then host-warm); per index state three
# true-cold starts, a true-cold and a host-warm load probe; the swapless
# POST -> DELETE -> POST run on the flat state. The volume ends migrated, IVF, nlist 256.
set -uo pipefail

LABEL=${1:?usage: c_session.sh LABEL IMAGE}
IMAGE=${2:?usage: c_session.sh LABEL IMAGE}
CTR=bench-tv
VOL=bench-tv
PORT=18000
KEY=bench
BASE=http://127.0.0.1:$PORT
HERE=$HOME/planC
OUT=$HERE/$LABEL
CLEAN=0       # 1 once this session owns bench-tv: only then may cleanup stop it
DIED=0
PROBER=       # pid of the /healthz prober subshell
SERVED=       # index type the last start_get served
CAP=          # memory-cap label of the running server
JOB_STATUS=   # final status of the last job
SQLITE=
BLAS=
MNT=
export IMAGE SQLITE BLAS
IMAGE_FACTS='import json, os, sys, sqlite3, importlib.metadata as m
print(json.dumps({"python": sys.version.split()[0], "sqlite_version": sqlite3.sqlite_version, "turbovec": m.version("turbovec"), "openblas_num_threads": os.environ.get("OPENBLAS_NUM_THREADS", "unset")}))'

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
ms_since() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }

cleanup() {
  touch "$OUT/hz.stop" 2>/dev/null
  if [ -n "$PROBER" ]; then kill "$PROBER" 2>/dev/null; wait "$PROBER" 2>/dev/null; PROBER=; fi
  if [ "$CLEAN" = 1 ]; then podman stop -t 30 --ignore "$CTR" >/dev/null 2>&1; fi
}
die() {
  DIED=1
  trap - TERM INT
  log "== PLANC FAILED: $* =="
  cleanup
  exit 1
}
trap 'die "interrupted"' TERM INT
trap 'rc=$?; [ "$rc" = 0 ] || [ "$DIED" = 1 ] || { log "== PLANC FAILED: exit $rc =="; cleanup; }' EXIT

jget() {  # jget KEY...: fields of the JSON object on stdin, one per line (dotted keys descend)
  python3 -c '
import json, sys
d = json.load(sys.stdin)
for key in sys.argv[1:]:
    v = d
    for part in key.split("."):
        v = v.get(part) if isinstance(v, dict) else None
    print(v if isinstance(v, (str, int, float)) and not isinstance(v, bool) else json.dumps(v))
' "$@"
}

row() {  # row KIND KEY=VALUE...: append one JSON row; values parse as JSON, else stay strings
  local line
  line=$(python3 - "$@" <<'PY'
import json, os, sys
row = {"kind": sys.argv[1]}
for arg in sys.argv[2:]:
    key, _, value = arg.partition("=")
    try:
        row[key] = json.loads(value)
    except ValueError:
        row[key] = value
row.update(sqlite_version=os.environ.get("SQLITE", ""), image=os.environ.get("IMAGE", ""),
           openblas_num_threads=os.environ.get("BLAS", ""))
print(json.dumps(row))
PY
) || return 1
  echo "$line" >> "$OUT/rows.jsonl"
  log "ROW $line"
}

evict() {  # evict [--warm]: D12 true-cold (default) or host-warm page cache of the volume
  local rc
  podman unshare python3 "$HERE/c_evict.py" "$@" "$MNT" | tee -a "$OUT/evict.log"
  rc=${PIPESTATUS[0]}
  [ "$rc" = 0 ] || die "c_evict.py $* exited $rc"
}

probe() {  # probe ARGS...: c_probe.py in the image, prints its JSON line. Never dies: X=$(probe ..) || die
  podman run --rm --name "$CTR" --memory 4g -v "$VOL:/data" -v "$HERE:/p:ro" \
    --entrypoint python "$IMAGE" /p/c_probe.py "$@" 2>>"$OUT/probe.log" | tail -n 1
  return "${PIPESTATUS[0]}"
}

mem() {  # memory facts of the server (PID 1 of bench-tv) in MiB, with sh and awk only
  podman exec -i "$CTR" sh -s <<'SH'
awk '/^VmRSS:/ {r = int($2 / 1024)} /^VmHWM:/ {h = int($2 / 1024)} /^VmSwap:/ {s = int($2 / 1024)}
     END {printf "vmrss=%d vmhwm=%d vmswap=%d", r, h, s}' /proc/1/status
awk '$1 == "oom_kill" {k = $2} END {printf " oom_kill=%d", k}' /sys/fs/cgroup/memory.events
awk '{printf " cg_current=%d", int($1 / 1048576)}' /sys/fs/cgroup/memory.current
awk '{printf " swap_max=%s\n", $1}' /sys/fs/cgroup/memory.swap.max
SH
}

snap() {  # snap PHASE REGIME: one memory row of the p4 run
  local m
  m=$(mem) || die "memory snapshot $1 failed"
  # shellcheck disable=SC2086  # m is KEY=VALUE words
  row mem run=p4 phase="$1" regime="$2" cap="$CAP" $m || die "row failed"
}

start_get() {  # start_get RUN PHASE REGIME CAP [PODMAN_RUN_ARGS...]: start, time /healthz and GETs
  local run=$1 phase=$2 regime=$3 t0 t1 i code hz_ms first_ms get_ms hz m later=() f
  CAP=$4
  shift 4
  podman rm -f --ignore "$CTR" >/dev/null
  t0=$(date +%s%N)
  podman run -d --name "$CTR" --memory 4g "$@" -p "127.0.0.1:$PORT:8000" -v "$VOL:/data" \
    -e ROOT_API_KEY="$KEY" "$IMAGE" >/dev/null || die "$run: podman run failed"
  for i in $(seq 1200); do
    curl -sf -o /dev/null --max-time 1 "$BASE/healthz" && break
    [ "$i" = 1200 ] && die "$run: no /healthz after 1200 polls"
    sleep 0.1
  done
  hz_ms=$(ms_since "$t0")
  rm -f "$OUT/hz.stop"
  (
    while [ ! -e "$OUT/hz.stop" ]; do
      curl -s -o /dev/null --max-time 5 -w '%{time_total}\n' "$BASE/healthz" || echo 99
      sleep 0.25
    done
  ) > "$OUT/hz-$run.txt" &
  PROBER=$!
  t1=$(date +%s%N)
  code=$(curl -s -o "$OUT/get-$run.json" -w '%{http_code}' --max-time 300 \
    -H "X-API-Key: $KEY" "$BASE/collections/bench")
  first_ms=$(ms_since "$t1")
  get_ms=$(ms_since "$t0")
  touch "$OUT/hz.stop"
  wait "$PROBER"
  PROBER=
  [ "$code" = 200 ] || die "$run: first GET returned ${code:-nothing}"
  for i in 1 2 3; do
    t1=$(date +%s%N)
    curl -sf -o /dev/null --max-time 60 -H "X-API-Key: $KEY" "$BASE/collections/bench" \
      || die "$run: later GET $i failed"
    later+=("$(ms_since "$t1")")
  done
  hz=$(awk 'BEGIN {m = 0} NF {n++; if ($1 + 0 > m) m = $1 + 0}
            END {printf "hz_n=%d hz_max_s=%.3f", n, m}' "$OUT/hz-$run.txt")
  mapfile -t f < <(jget index.type chunks dim pending_jobs < "$OUT/get-$run.json")
  SERVED=${f[0]:-}
  m=$(mem) || die "$run: memory snapshot failed"
  # shellcheck disable=SC2086  # hz and m are KEY=VALUE words
  row start run="$run" phase="$phase" regime="$regime" cap="$CAP" index="$SERVED" \
    chunks="${f[1]:-}" dim="${f[2]:-}" pending_jobs="${f[3]:-}" healthz_ms="$hz_ms" \
    first_get_ms="$first_ms" start_to_get_ms="$get_ms" \
    later_ms="[$(IFS=,; echo "${later[*]}")]" $hz $m || die "row failed"
}

stop_server() {  # stop_server RUN: clean stop, keep the server log, remove the container
  podman stop -t 60 --ignore "$CTR" >/dev/null
  podman logs "$CTR" > "$OUT/server-$1.log" 2>&1
  podman rm -f --ignore "$CTR" >/dev/null
}

built_s() {  # seconds of the last migration line in a server log, or nothing
  sed -n 's/.*built idx_records_doc_type in \([0-9.]*\) s.*/\1/p' "$1" | tail -n 1
}

jdiff() {  # jdiff KEY AFTER_JSON BEFORE_JSON: AFTER[KEY] - BEFORE[KEY], one decimal
  awk -v a="$(jget "$1" <<<"$2")" -v b="$(jget "$1" <<<"$3")" 'BEGIN {printf "%.1f", a - b}'
}

job() {  # job NAME REGIME METHOD [BODY]: enqueue an index job on bench, wait for it, record it
  local name=$1 regime=$2 method=$3 body=${4:-} t0 resp id i st=() args
  args=(-s --max-time 60 -X "$method" -H "X-API-Key: $KEY")
  if [ -n "$body" ]; then args+=(-H "Content-Type: application/json" -d "$body"); fi
  t0=$(date +%s%N)
  resp=$(curl "${args[@]}" "$BASE/collections/bench/index") || die "$name: enqueue failed"
  id=$(jget job_id <<<"$resp")
  case $id in ''|*[!0-9]*) die "$name: bad enqueue reply: $resp" ;; esac
  log "$name: job $id enqueued"
  JOB_STATUS=
  for i in $(seq 360); do
    sleep 5
    if [ "$(podman inspect --format '{{.State.Running}}' "$CTR" 2>/dev/null)" != true ]; then
      podman logs "$CTR" > "$OUT/server-died-$name.log" 2>&1
      row job name="$name" regime="$regime" cap="$CAP" status=died wall_ms="$(ms_since "$t0")" \
        oom_flag="$(podman inspect --format '{{.State.OOMKilled}}' "$CTR")"
      die "$name: the server died"
    fi
    resp=$(curl -s --max-time 30 -H "X-API-Key: $KEY" "$BASE/collections/bench/jobs/$id") || continue
    mapfile -t st < <(jget status error created_at updated_at <<<"$resp")
    JOB_STATUS=${st[0]:-}
    case $JOB_STATUS in done|error) break ;; esac  # _run_worker's two final states
  done
  row job name="$name" regime="$regime" cap="$CAP" status="${JOB_STATUS:-timeout}" \
    error="${st[1]:-null}" created_at="${st[2]:-}" updated_at="${st[3]:-}" \
    wall_ms="$(ms_since "$t0")" || die "row failed"
  log "$name: ${JOB_STATUS:-timeout}"
}

migrate_phase() {  # migrate_phase LAYOUT_JSON: threshold 5, true-cold then host-warm
  local before=$1 first=true after leg cold warm
  if [ "$(jget migrated <<<"$before")" = true ]; then  # revert so the image migrates again
    first=false
    leg=$(probe legacy) || die "legacy probe failed"
    row layout phase=revert probe="$leg" || die "row failed"
    before=$(probe layout) || die "layout probe failed"
  fi
  [ "$(jget migrated <<<"$before")" = false ] || die "meta.db did not revert"
  evict
  start_get migrate-cold migrate true-cold 4g
  stop_server migrate-cold
  cold=$(built_s "$OUT/server-migrate-cold.log")
  [ -n "$cold" ] || die "no migration line in server-migrate-cold.log: not a Plan C image?"
  after=$(probe layout) || die "layout probe failed"
  [ "$(jget migrated <<<"$after")" = true ] || die "the server did not migrate meta.db"
  leg=$(probe legacy) || die "legacy probe failed"  # measures the covering index, and re-arms
  row migrate regime=true-cold cap=4g create_s="$cold" first_migration="$first" \
    growth_mb="$(jdiff db_mb "$after" "$before")" covering_mb="$(jget freed_mb <<<"$leg")" \
    freelist_mb="$(jget freelist_mb <<<"$after")" before="$before" after="$after" || die "row failed"
  evict --warm
  start_get migrate-warm migrate host-warm 4g
  stop_server migrate-warm
  warm=$(built_s "$OUT/server-migrate-warm.log")
  [ -n "$warm" ] || die "no migration line in server-migrate-warm.log"
  row migrate regime=host-warm cap=4g create_s="$warm" first_migration=false || die "row failed"
}

measure_state() {  # measure_state ivf|flat: thresholds 1-4, 6 and 8 for the served index state
  local s=$1 i p
  for i in 1 2 3; do
    evict
    start_get "p2-$s-$i" p2 true-cold 4g
    stop_server "p2-$s-$i"
    [ "$SERVED" = "$s" ] || die "p2-$s-$i served '$SERVED', not $s"
  done
  evict
  p=$(probe load) || die "load probe ($s, true-cold) failed"
  row load index_state="$s" regime=true-cold cap=4g probe="$p" || die "row failed"
  evict --warm
  p=$(probe load --list) || die "load probe ($s, host-warm) failed"
  row load index_state="$s" regime=host-warm cap=4g probe="$p" || die "row failed"
}

to_flat() {  # detach the IVF index (host-warm, not gated) so the flat state can be measured
  evict --warm
  start_get detach-prep prep host-warm 4g
  job prep-detach host-warm DELETE
  [ "$JOB_STATUS" = done ] || die "prep detach: ${JOB_STATUS:-timeout}"
  stop_server detach-prep
}

memory_run() {  # threshold 7 (D13) and threshold 9's attach line: swapless POST -> DELETE -> POST
  evict
  start_get p4 p4 true-cold 4g-swapless --memory-swap 4g
  [ "$SERVED" = flat ] || die "p4 served '$SERVED', not flat"
  snap loaded true-cold
  evict  # the index is anonymous memory: this makes attach1's reads true-cold
  job attach1 true-cold POST '{}'
  snap after_attach1 true-cold
  sleep 15
  snap after_attach1_15s true-cold
  [ "$JOB_STATUS" = done ] || die "attach1: ${JOB_STATUS:-timeout}"
  evict --warm
  job detach host-warm DELETE
  snap after_detach host-warm
  sleep 15
  snap after_detach_15s host-warm
  [ "$JOB_STATUS" = done ] || die "detach: ${JOB_STATUS:-timeout}"
  job attach2 host-warm POST '{}'
  snap after_attach2 host-warm
  sleep 15
  snap after_attach2_15s host-warm
  [ "$JOB_STATUS" = done ] || die "attach2: ${JOB_STATUS:-timeout}"
  row end run=p4 regime=host-warm cap="$CAP" \
    running="$(podman inspect --format '{{.State.Running}}' "$CTR")" \
    oom_flag="$(podman inspect --format '{{.State.OOMKilled}}' "$CTR")" || die "row failed"
  stop_server p4
}

mkdir -p "$OUT" || exit 1
ps -o pgid= -p $$ | tr -d ' ' > "$OUT/session.pgid"
log "== PLANC START $LABEL $IMAGE =="
podman image exists "$IMAGE" || die "no image $IMAGE"
if [ "$(podman inspect --format '{{.State.Running}}' "$CTR" 2>/dev/null)" = true ]; then
  die "$CTR is running: find out whose it is before starting"
fi
podman volume exists "$VOL" || die "no volume $VOL"
MNT=$(podman volume inspect "$VOL" --format '{{.Mountpoint}}') || die "no mountpoint for $VOL"
CLEAN=1
podman rm -f --ignore "$CTR" >/dev/null
{
  date -u; uptime; free -m; grep -E '^(Cached|SwapTotal|SwapFree):' /proc/meminfo; podman ps -a
  podman image inspect --format '{{.Id}} {{.Created}}' "$IMAGE"
} > "$OUT/facts.txt" 2>&1
podman run --rm --entrypoint python "$IMAGE" -c "$IMAGE_FACTS" > "$OUT/image.json" \
  2>>"$OUT/probe.log" || die "image facts failed"
mapfile -t f < <(jget python sqlite_version turbovec openblas_num_threads < "$OUT/image.json")
SQLITE=${f[1]:-}
BLAS=${f[3]:-}
[ -n "$SQLITE" ] && [ -n "$BLAS" ] || die "incomplete image facts: $(cat "$OUT/image.json")"
row session label="$LABEL" date="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  image_id="sha256:$(podman image inspect --format '{{.Id}}' "$IMAGE")" \
  python="${f[0]:-}" turbovec="${f[2]:-}" || die "row failed"
lay=$(probe layout) || die "layout probe failed"
row layout phase=start probe="$lay" || die "row failed"
[ "$(jget open_jobs <<<"$lay")" = 0 ] || die "meta.db has open jobs: let them finish first"
START=$(jget index <<<"$lay")
migrate_phase "$lay"
case $START in
  ivf) measure_state ivf; to_flat; measure_state flat; memory_run ;;
  flat) measure_state flat; memory_run; measure_state ivf ;;
  *) die "unknown index state '$START'" ;;
esac
lay=$(probe layout) || die "layout probe failed"
row layout phase=end probe="$lay" || die "row failed"
log "== PLANC DONE =="
```

Notes on the choices:
- **`die` and command substitution.** `die` inside `$( … )` would only exit the subshell. So `probe`, `mem`, `jget`, `built_s` and `jdiff` never call it; their callers write `X=$(…) || die …`. The `EXIT` trap catches anything else that ends the script, such as a `set -u` abort, so the log still ends in `== PLANC FAILED`.
- **Cleanup.** It stops `bench-tv` only once this session has checked that no one else was running it (`CLEAN=1`). The `/healthz` prober stops through a stop file and `wait`, not a kill, so no orphaned `curl` is left.
- **Memory snapshots.** They run `sh` and `awk` in the container. A Python process would add its own memory to the swapless cgroup it measures.
- **Regimes.** `migrate-cold`, every `p2` start, the true-cold `load` probes, `p4`'s start and attach1 are true-cold. Everything after `evict --warm` is host-warm.
- **The prep detach.** It is not gated: it runs at `--memory 4g` with podman's default swap.
- **Expected duration.** About 25 minutes with the p1 figures. Most of it is attach1, whose true-cold stream-and-add read p1 measured at 170 s, plus up to seven 60 s stops.

- [ ] **Step 6: Write the verdict**

Create `D:/DEV/_scratch/dgx/planC/c_verdict.py`:

```python
"""Verdict of a Plan C acceptance session (plan Task 11): spec §7 C thresholds 1-9.

    uv run python D:/DEV/_scratch/dgx/planC/c_verdict.py DIR [--adr ADR]

DIR is a fetched ~/planC/LABEL directory (rows.jsonl, server-p4.log). It prints the
'### Acceptance run' markdown and exits 1 if any check fails. With --adr, and only when
every check passes, it also appends that markdown to the ADR's Plan C addendum."""
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ADDENDUM = "## Addendum 2026-09 — cold start and loop hygiene"
HEADING = "### Acceptance run"
HWM_LIMIT_MIB = 3.8 * 1024
RSS_SLACK_MIB = 512
FIRST_GET_S = {"ivf": 6.0, "flat": 5.0}
STATES = ("ivf", "flat")
JOBS = ("attach1", "detach", "attach2")
SNAPS = ("loaded", "after_attach1", "after_attach1_15s", "after_detach", "after_detach_15s",
         "after_attach2", "after_attach2_15s")
LABELED = ("start", "load", "migrate", "job", "mem")
ATTACH_RE = re.compile(
    r"attach_index bench: n=(?P<n>\d+) nlist=(?P<nlist>\d+) prework=(?P<prework>[\d.]+)s"
    r" sample=(?P<sample>[\d.]+)s legacy_backfill=(?P<legacy>\d+) build=(?P<build>[\d.]+)s"
    r" live_scan=(?P<live_scan>[\d.]+)s backfill=(?P<backfill>\d+) rows (?P<backfill_s>[\d.]+)s"
    r" swap=(?P<swap>[\d.]+)s total=(?P<total>[\d.]+)s"
)
INTS = ("n", "nlist", "legacy", "backfill")


def num(x, nd=1):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return "–"
    return f"{x:,.{nd}f}"


def median(xs):
    return statistics.median(xs) if xs else None


def secs(ms):
    return None if ms is None else ms / 1000


def read_rows(d):
    text = (d / "rows.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    by = defaultdict(list)
    for r in rows:
        by[r.get("kind")].append(r)
    return rows, by


def read_attach(d):
    """The first attach_index line of the memory run's server: attach1, the true-cold one."""
    log = d / "server-p4.log"
    m = ATTACH_RE.search(log.read_text(encoding="utf-8", errors="replace")) if log.exists() else None
    if m is None:
        return None
    return {k: int(v) if k in INTS else float(v) for k, v in m.groupdict().items()}


def p2(by, s=None):
    return [r for r in by["start"] if r.get("phase") == "p2" and s in (None, r.get("index"))]


def loads(by, regime, s=None):
    return [r["probe"] for r in by["load"]
            if r.get("regime") == regime and s in (None, r.get("index_state"))]


def c1(rows, by, attach):
    ok, parts = True, []
    for s in STATES:
        runs = p2(by, s)
        m = median([r["start_to_get_ms"] for r in runs])
        ok = ok and len(runs) >= 3 and m is not None and m / 1000 <= FIRST_GET_S[s]
        parts.append(f"{s} {num(secs(m), 2)} s (≤ {FIRST_GET_S[s]:.0f} s, median of {len(runs)})")
    return ok, "start → first `GET /collections/bench`, true-cold: " + ", ".join(parts)


def c2(rows, by, attach):
    runs = p2(by)
    worst = max((r["hz_max_s"] for r in runs), default=None)
    ok = len(runs) >= 6 and all(r["hz_n"] >= 1 for r in runs) and worst is not None and worst <= 0.1
    probes = sum(r["hz_n"] for r in runs)
    return ok, (f"slowest `/healthz` while the first GET loaded the collection {num(worst, 3)} s"
                f" (≤ 0.1 s; {probes} probes over {len(runs)} starts)")


def c3(rows, by, attach):
    ok, parts = True, []
    for s in STATES:
        worst = [max(r["later_ms"]) for r in p2(by, s)]
        m = median(worst)
        ok = ok and len(worst) >= 3 and m is not None and m <= 1000
        parts.append(f"{s} {num(secs(m), 2)} s")
    return ok, "later GETs, median over starts of each start's slowest of 3: " + ", ".join(parts) + " (≤ 1.0 s)"


def c4(rows, by, attach):
    ok, parts = True, []
    for s in STATES:
        got = loads(by, "true-cold", s)
        if not got:
            ok = False
            parts.append(f"{s}: no true-cold load")
        for p in got:
            read, limit = p["touch_read_mb"], p["index_mb"] + 150
            ok = ok and read is not None and read <= limit
            parts.append(f"{s} {num(read, 0)} MB with {num(p['index_mb'], 0)} MB of index files"
                         f" (≤ {num(limit, 0)} MB) in {num(p['touch_s'], 2)} s")
    return ok, "`Collection()` true-cold read: " + "; ".join(parts)


def c5(rows, by, attach):
    cold = [r for r in by["migrate"] if r.get("regime") == "true-cold"][0]
    warm = [r for r in by["migrate"] if r.get("regime") == "host-warm"][0]
    growth = max(cold["growth_mb"], cold["covering_mb"])
    ok = cold["create_s"] <= 60 and warm["create_s"] <= 3 and growth <= 120
    return ok, (
        f"migration `CREATE INDEX` {num(cold['create_s'])} s true-cold (≤ 60 s),"
        f" {num(warm['create_s'])} s host-warm (≤ 3 s); growth {num(growth)} MB (≤ 120 MB;"
        f" file {num(cold['growth_mb'])} MB, covering index {num(cold['covering_mb'])} MB,"
        f" first migration of this volume: {'yes' if cold['first_migration'] else 'no'})"
    )


def c6(rows, by, attach):
    cold, warm = loads(by, "true-cold"), loads(by, "host-warm")
    rows_ok = all(p["vec_sample_rows"] == 65536 for p in cold + warm)
    c = max(p["vec_sample_s"] for p in cold)
    w = max(p["vec_sample_s"] for p in warm)
    ok = len(cold) >= 2 and len(warm) >= 2 and rows_ok and c <= 15 and w <= 1
    return ok, (f"`_vec_sample(65536)` slowest {num(c, 2)} s true-cold (≤ 15 s),"
                f" {num(w, 2)} s host-warm (≤ 1 s)" + ("" if rows_ok else "; a sample lacked 65,536 rows"))


def c7(rows, by, attach):
    jobs = {r["name"]: r for r in by["job"]}
    snaps = {r["phase"]: r for r in by["mem"] if r.get("run") == "p4"}
    end = by["end"][0]
    status = [jobs.get(n, {}).get("status", "missing") for n in JOBS]
    clean = all(snaps[p]["oom_kill"] == 0 and snaps[p]["vmswap"] == 0 and snaps[p]["swap_max"] == 0
                for p in SNAPS)
    alive = end["running"] is True and end["oom_flag"] is False
    hwm = max(snaps[p]["vmhwm"] for p in SNAPS)
    base = snaps["loaded"]["vmrss"]
    deltas = [snaps[f"after_{n}_15s"]["vmrss"] - base for n in JOBS]
    ok = (status == ["done"] * 3 and clean and alive and hwm <= HWM_LIMIT_MIB
          and max(deltas) <= RSS_SLACK_MIB)
    return ok, (
        f"swapless POST → DELETE → POST: jobs {'/'.join(status)}; no swap and no OOM kill:"
        f" {'yes' if clean and alive else 'no'}; max VmHWM {hwm:,} MiB (≤ 3,891 MiB);"
        f" VmRSS 15 s after each job vs loaded {base:,} MiB: "
        + ", ".join(f"{d:+,}" for d in deltas) + " MiB (≤ +512 MiB)"
    )


def c8(rows, by, attach):
    ok, parts = True, []
    for s in STATES:
        runs = [x for p in loads(by, "host-warm", s) for x in p.get("list", [])]
        m = median([x["page_s"] for x in runs])
        pinned = all(x["pinned"] for x in runs)
        ok = (ok and len(runs) >= 3 and m is not None and m <= 1.0 and pinned
              and all(x["rows"] == 20 for x in runs))
        parts.append(f"{s} {num(m, 2)} s over {len(runs)} pages"
                     + ("" if pinned else ", page SQL without `NOT INDEXED`"))
    return ok, ("host-warm list page (`chunks`, sort `-year`, 20 rows), median: "
                + "; ".join(parts) + " (≤ 1.0 s)")


def c9(rows, by, attach):
    if attach is None:
        return False, "no `attach_index bench:` line in server-p4.log"
    total = attach["prework"] + attach["live_scan"] + attach["backfill_s"]
    return total <= 10, (
        f"true-cold `POST /index` pre-work {total:.2f} s (≤ 10 s): guard {attach['prework']:.2f} s"
        f" + live-id diff {attach['live_scan']:.2f} s + backfill of {attach['backfill']:,} rows"
        f" {attach['backfill_s']:.2f} s"
    )


def labels(rows, by, attach):
    bare = sorted({str(r.get("kind")) for r in rows
                   if not r.get("sqlite_version") or not r.get("openblas_num_threads")})
    unlabeled = sorted({r["kind"] for r in rows
                        if r.get("kind") in LABELED and not (r.get("regime") and r.get("cap"))})
    ok = bool(rows) and not bare and not unlabeled
    versions = ", ".join(sorted({str(r.get("sqlite_version")) for r in rows}))
    blas = ", ".join(sorted({str(r.get("openblas_num_threads")) for r in rows}))
    detail = f"{len(rows)} rows, SQLite {versions}, OPENBLAS_NUM_THREADS={blas}"
    if bare:
        detail += "; rows without versions: " + ", ".join(bare)
    if unlabeled:
        detail += "; rows without regime or cap: " + ", ".join(unlabeled)
    return ok, detail + ("; every measurement row states its regime and memory cap" if ok else "")


CHECKS = (("C1", c1), ("C2", c2), ("C3", c3), ("C4", c4), ("C5", c5), ("C6", c6), ("C7", c7),
          ("C8", c8), ("C9", c9), ("labels", labels))


def run_checks(rows, by, attach):
    lines, failed = [], []
    for name, fn in CHECKS:
        try:
            ok, detail = fn(rows, by, attach)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            ok, detail = False, f"incomplete rows ({type(e).__name__}: {e})"
        lines.append(f"- {'PASS' if ok else 'FAIL'}: {name} — {detail}")
        if not ok:
            failed.append(name)
    return lines, failed


def job_s(r):
    try:
        return (datetime.fromisoformat(r["updated_at"])
                - datetime.fromisoformat(r["created_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return secs(r.get("wall_ms"))


def render(rows, by, attach, lines):
    s = (by["session"] or [{}])[0]
    lab = rows[0] if rows else {}
    first = (by["start"] or [{}])[0]
    out = [HEADING, "", (
        f"Session `{s.get('label', '?')}`, {str(s.get('date', '?'))[:10]}, on gn100 (DGX Spark, aarch64)."
        f" Image `{s.get('image', '?')}` (`{str(s.get('image_id', '?'))[:19]}`): Python {s.get('python', '?')},"
        f" SQLite {lab.get('sqlite_version', '?')} (`sqlite3.sqlite_version`),"
        f" `OPENBLAS_NUM_THREADS={lab.get('openblas_num_threads', '?')}`, turbovec {s.get('turbovec', '?')}."
        f" Collection `bench`: {num(first.get('chunks'), 0)} chunks × {first.get('dim', '?')} dims."
        " Every container ran with `--memory 4g`; the memory run added `--memory-swap 4g`, so it had"
        " no swap. **true-cold**: every file of the volume fsynced and dropped with"
        " `posix_fadvise(POSIX_FADV_DONTNEED)`, with `mincore` confirming at most 16 MB still cached (D12)."
        " **host-warm**: every file of the volume read once just before. Each row states its regime."
    )]
    out += ["", "| Index | Start (true-cold) | → `/healthz` | → first GET | Slowest `/healthz` | Later GETs | VmRSS |",
            "|---|---|---|---|---|---|---|"]
    for r in p2(by):
        later = " / ".join(num(secs(x), 2) for x in r.get("later_ms", []))
        out.append(f"| {r.get('index')} | `{r.get('run')}` | {num(secs(r.get('healthz_ms')), 2)} s"
                   f" | {num(secs(r.get('start_to_get_ms')), 2)} s | {num(r.get('hz_max_s'), 3)} s"
                   f" ({r.get('hz_n')} probes) | {later} s | {num(r.get('vmrss'), 0)} MiB |")
    out += ["", "| Index | Regime | `touch()` | Read | Index files | `_vec_sample(65536)` | List page, median of 3 |",
            "|---|---|---|---|---|---|---|"]
    for r in by["load"]:
        p = r.get("probe", {})
        pages = [x["page_s"] for x in p.get("list", [])]
        out.append(f"| {r.get('index_state')} | {r.get('regime')} | {num(p.get('touch_s'), 2)} s"
                   f" | {num(p.get('touch_read_mb'), 0)} MB | {num(p.get('index_mb'), 0)} MB"
                   f" | {num(p.get('vec_sample_s'), 2)} s | {num(median(pages), 2) + ' s' if pages else '–'} |")
    out += ["", "Memory run (`--memory 4g --memory-swap 4g`, starting from the flat state):", "",
            "| Snapshot | Regime | VmRSS | VmHWM | cgroup | VmSwap | `oom_kill` |", "|---|---|---|---|---|---|---|"]
    for r in by["mem"]:
        out.append(f"| {r.get('phase')} | {r.get('regime')} | {num(r.get('vmrss'), 0)} MiB"
                   f" | {num(r.get('vmhwm'), 0)} MiB | {num(r.get('cg_current'), 0)} MiB"
                   f" | {num(r.get('vmswap'), 0)} MiB | {r.get('oom_kill')} |")
    jobs = [f"{r['name']} {r.get('status')} in {num(job_s(r))} s ({r.get('regime')})"
            for r in by["job"] if r.get("name") in JOBS]
    out += ["", "Jobs: " + "; ".join(jobs) + "."]
    if attach:
        out.append(
            f"First attach (true-cold), from its `attach_index` line: n={attach['n']:,}, nlist {attach['nlist']};"
            f" pre-work {attach['prework']:.2f} s, sample {attach['sample']:.2f} s, build {attach['build']:.2f} s,"
            f" live-id diff {attach['live_scan']:.2f} s, backfill {attach['backfill']:,} rows"
            f" {attach['backfill_s']:.2f} s, swap {attach['swap']:.2f} s, total {attach['total']:.2f} s."
        )
    migs = [f"{num(r.get('create_s'))} s {r.get('regime')}" for r in by["migrate"]]
    out.append("Migration `CREATE INDEX idx_records_doc_type`: " + ", ".join(migs) + ".")
    out += ["", "Checks (spec §7 C):", "", *lines]
    return "\n".join(out)


def insert(adr, md):
    """Append md to the end of the Plan C addendum, keeping the file's line endings."""
    raw = adr.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")
    key = "\n" + ADDENDUM + "\n"
    at = text.find(key)
    if at < 0:
        raise SystemExit(f"{adr}: no '{ADDENDUM}' section")
    end = text.find("\n## ", at + len(key))
    end = len(text) if end < 0 else end
    section, rest = text[at:end], text[end:]
    if HEADING in section:
        raise SystemExit(f"{adr}: the addendum already has '{HEADING}'")
    new = text[:at] + section.rstrip() + "\n\n" + md + "\n" + ("\n" + rest.lstrip("\n") if rest else "")
    if crlf:
        new = new.replace("\n", "\r\n")
    adr.write_bytes(new.encode("utf-8"))
    print(f"recorded in {adr}", file=sys.stderr)


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != "--adr"):
        print(__doc__, file=sys.stderr)
        return 2
    d = Path(argv[0])
    rows, by = read_rows(d)
    attach = read_attach(d)
    lines, failed = run_checks(rows, by, attach)
    md = render(rows, by, attach, lines)
    print(md)
    if failed:
        print("FAIL: " + ", ".join(failed), file=sys.stderr)
        if len(argv) == 3:
            print(f"{argv[2]}: not recorded", file=sys.stderr)
        return 1
    if len(argv) == 3:
        insert(Path(argv[2]), md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

The verdict reads only `rows.jsonl` and `server-p4.log`. The `≤` signs are why it reconfigures stdout to UTF-8: the Windows console code page cannot print them.

- [ ] **Step 7: Run the check to verify it passes**

From the repo root in Git Bash:

Run: `uv run python D:/DEV/_scratch/dgx/planC/c_check.py`
Expected: exit status 0. The last lines are:

```
syntax: bash -n c_session.sh and compile() of the three .py scripts ok
c_probe.py: layout, legacy, load --list and the usage error ok on a 60-row collection
c_verdict.py: all-pass rows, the C7 (VmHWM 3,950 MiB) and C8 (unpinned) fails ok
c_verdict.py --adr: LF and CRLF inserts, no second insert, no insert on FAIL ok
Plan C DGX scripts OK
```

If a line ends in `FAILED: ...`, fix the script it names and run the check again.

- [ ] **Step 8: Confirm the repo is untouched**

Run: `uv run pytest -q`
Expected: the same count as after Task 10, N0 + 64 passed, where N0 is the count before Task 1. This task changes no repo file.

Run: `git status --short`
Expected: the same output as before Step 1, with no `src/`, `tests/`, `docs/adr/` or `Dockerfile` entries. The scripts live outside the repo, so this task has no commit.

---

### Task 12: Acceptance run on gn100 (spec §7 C), recorded in ADR 0001

This is the last task. It deploys the branch's `HEAD` to gn100 and runs Task 11's session there. If every §7 C threshold passes, it records the verdict in the Plan C addendum. The DGX rules of spec §6 hold throughout:
- ssh only as `ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100`;
- no sudo;
- `podman rm -f` names only `bench-tv`, and nothing touches `bench-wv` (container or volume) or writes `~/turborag`;
- the session runs detached with `setsid -f` into a log that is polled;
- nothing kills processes by a `pkill -f` pattern;
- nobody's DGX work is stopped or deleted without asking.

Below, `DGX` stands for that ssh command. Spell it out in full in each command you run.

**Files:**
- Modify: `docs/adr/0001-performance-optimization-decisions.md`: a `### Acceptance run` subsection at the end of `## Addendum 2026-09 — cold start and loop hygiene`, written by `c_verdict.py --adr`.
- Modify: `tests/test_cold_start.py`: append `test_adr_records_the_acceptance_run`.
- Outside the repo:
  - gn100 `~/planC/`: the three gn100 scripts, `c1/` and `c1.log`;
  - `D:/DEV/_scratch/dgx/planC/c1/` and `c1.log`, fetched.

**Interfaces:**
- Consumes:
  - Task 11's scripts and verdict rules;
  - Plan A's `bench/dgx/deploy.sh`, which ships the committed `HEAD` and builds `localhost/raggio:<sha7>`;
  - gn100's `bench-tv` volume: collection `bench`, 2,549,119 chunks × 1024 dims, IVF (nlist 256) or flat.
- Produces:
  - the recorded acceptance run, whose ten check lines all read `PASS`;
  - gn100 left with no `bench-tv` container, and the `bench-tv` volume migrated (covering index) with an IVF index, nlist 256.

- [ ] **Step 1: Ask for the run window**

Send the user this, and run nothing on gn100 until they answer:

> Plan C's acceptance run needs gn100 for about 30 minutes: about 5 minutes to deploy this branch, then a session of about 25 minutes that `timeout` caps at 60 minutes.
> - The deploy replaces `~/raggio`. `unpack.sh` first copies its bench results to `~/raggio-bench/pre-deploy-<UTC time>/`.
> - The session uses only the `bench-tv` container and volume, never `bench-wv`. It:
>   - reverts and re-runs the one-time index migration of the volume's `meta.db`;
>   - detaches and re-attaches its index, including once under `--memory 4g --memory-swap 4g`;
>   - evicts the volume's files from the page cache several times, without sudo.
> - It leaves the volume migrated, with an IVF index (nlist 256).
>
> When may it run, and is a 60-minute cap OK?

- If the user offers a shorter cap, it must be at least 35 minutes; use it in Step 6 in place of `60m`. Below 35 minutes, ask for a longer window.
- If the user declines, stop here. Report Tasks 1–11 as done and this task as not run.

- [ ] **Step 2: Laptop checks**

From the repo root, on the Plan C branch with Tasks 1–10 committed:

Run: `git status --short -- src tests docs/adr docs/storage.md docs/indexing.md Dockerfile`
Expected: no output. `deploy.sh` ships `HEAD` only and refuses a dirty tracked tree.

Run: `uv run pytest -q`
Expected: N0 + 64 passed.

Run: `uv run python D:/DEV/_scratch/dgx/planC/c_check.py`
Expected: exit status 0, ending in `Plan C DGX scripts OK`.

- [ ] **Step 3: Read-only preflight on gn100**

Run:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
uptime; free -m
podman ps -a --format "{{.Names}} | {{.Status}} | {{.Image}}"
pgrep -af "[s]ession\.sh|[b]ench\.py" || echo "no sessions running"
podman volume exists bench-tv && echo "volume bench-tv present"
MNT=$(podman volume inspect bench-tv --format "{{.Mountpoint}}")
podman unshare test -f "$MNT/collections/bench/meta.db" && echo "bench meta.db present"
python3 --version
ls -la ~/planC 2>/dev/null || echo "no ~/planC yet"'
```

The bracketed patterns keep `pgrep` from matching this command's own shell.

Expected:
- `no sessions running`;
- no `bench-tv` line in the `Up` state;
- `volume bench-tv present` and `bench meta.db present`;
- a Python 3 version;
- `free -m` showing at least 8 GB available.

If `bench-tv` is `Up`, or a session is listed, it is someone's work. Stop, report what you saw, and ask. Do not stop it. If the volume or `meta.db` is missing, stop and report: the run needs the 2.55M-chunk collection.

- [ ] **Step 4: Stage the scripts on gn100**

Run, in Git Bash from the repo root:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 \
  'if [ -e ~/planC ]; then mv ~/planC ~/planC.prev-$(date -u +%Y%m%dT%H%M%SZ); fi; mkdir ~/planC && chmod 755 ~/planC'
(cd /d/DEV/_scratch/dgx/planC && scp -F "$SSH_CONFIG" \
  -o BatchMode=yes c_evict.py c_probe.py c_session.sh gn100:planC/)
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
cd ~/planC && chmod 644 c_evict.py c_probe.py c_session.sh && bash -n c_session.sh && echo "syntax ok"
MNT=$(podman volume inspect bench-tv --format "{{.Mountpoint}}")
podman unshare python3 ~/planC/c_evict.py --check "$MNT"'
```

- A previous `~/planC` is kept, renamed `~/planC.prev-<UTC time>`, never deleted.
- The container reads the probe script through the read-only bind mount, as the image's `app` user. The `chmod` lines make it readable.

Expected: `syntax ok`, then one line `CHECK {"files": …, "total_mb": …, "resident_mb": …, "resident_pct": …, "s": …}`. That line proves `mmap`, `mincore` and the volume path work under `podman unshare` on gn100. Any Python traceback here means `c_evict.py` needs fixing in Task 11 before the session.

- [ ] **Step 5: Deploy**

Run, from the repo root: `bash bench/dgx/deploy.sh`
Expected: it starts with `deploying <sha7> to gn100`, ends with `<UTC time> deployed <sha7> as localhost/raggio:<sha7>`, and exits with status 0. `<sha7>` is `git rev-parse --short=7 HEAD`.

- [ ] **Step 6: Launch the session**

Run, from the repo root:

```bash
SHA=$(git rev-parse --short=7 HEAD)
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 \
  "setsid -f timeout --kill-after=60 60m bash ~/planC/c_session.sh c1 localhost/raggio:$SHA > ~/planC/c1.log 2>&1 < /dev/null; sleep 5; head -n 3 ~/planC/c1.log"
```

Expected: `[hh:mm:ss] == PLANC START c1 localhost/raggio:<sha7> ==`, then a `ROW {"kind": "session", …}` line. That row carries `"sqlite_version"` and `"openblas_num_threads": "1"`.

- [ ] **Step 7: Poll until it ends**

Every 3–5 minutes, run:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 \
  'grep -E "== PLANC|ROW" ~/planC/c1.log | tail -n 4 | cut -c1-200'
```

The session is over when the last line is `== PLANC DONE ==` or `== PLANC FAILED: <reason> ==`. The row kinds show progress: `migrate`, then `start` and `load` per index state, then `job` and `mem` for the memory run.

- To stop it early (the user asks, or the window closes), run:

  ```bash
  ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 \
    'kill -TERM -- -"$(cat ~/planC/c1/session.pgid)"; podman stop -t 60 --ignore bench-tv'
  ```

- On `== PLANC FAILED`:
  1. Fetch as in Step 8.
  2. Read the reason with `c1/probe.log` and the `c1/server-*.log` files.
  3. Stop and report to the user.
- A relaunch needs the user's OK within the window, and a new label (`c2`, then `c3`). The session appends to `rows.jsonl`, so a label is never reused. The later steps then read `c2` in place of `c1`.

- [ ] **Step 8: Fetch the session**

Run:

```bash
(cd /d/DEV/_scratch/dgx/planC && scp -r -F "$SSH_CONFIG" \
  -o BatchMode=yes gn100:planC/c1 gn100:planC/c1.log .)
```

The subshell keeps the shell at the repo root, which Step 12's relative `--adr` path needs.

Expected: `D:/DEV/_scratch/dgx/planC/c1/rows.jsonl` and `server-p4.log` exist, and `c1.log` ends in `== PLANC DONE ==`.

- [ ] **Step 9: Read the verdict**

From the repo root:

Run: `uv run python D:/DEV/_scratch/dgx/planC/c_verdict.py D:/DEV/_scratch/dgx/planC/c1`
Expected: exit status 0. The printed `### Acceptance run` block ends in ten lines, `- PASS: C1 — …` through `- PASS: C9 — …` and `- PASS: labels — …`.

On any `- FAIL:` line, the command exits with status 1 and prints `FAIL: <names>` on stderr. Then:
- do not record anything, and do not commit;
- report the failing lines, the block and `c1.log` to the user;
- remember that a FAIL is a finding about Plan C. Never loosen a threshold or edit the rows to pass.

- [ ] **Step 10: Write the failing test**

Append to `tests/test_cold_start.py`:

```python
def test_adr_records_the_acceptance_run():
    adr = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0001-performance-optimization-decisions.md"
    text = adr.read_text(encoding="utf-8")
    head = "## Addendum 2026-09 — cold start and loop hygiene"
    start = text.index(head)
    end = text.find("\n## ", start + len(head))
    section = text[start:] if end < 0 else text[start:end]
    assert "### Acceptance run" in section
    for n in range(1, 10):
        assert f"- PASS: C{n} —" in section
    assert "- FAIL:" not in section
    for phrase in ("sqlite3.sqlite_version", "OPENBLAS_NUM_THREADS", "true-cold", "--memory-swap 4g"):
        assert phrase in section
```

- [ ] **Step 11: Run it to verify it fails**

Run: `uv run pytest tests/test_cold_start.py -v -k acceptance_run`
Expected: `1 failed, 64 deselected`. The failure is `AssertionError: assert '### Acceptance run' in '## Addendum 2026-09 — cold start and loop hygiene\n\nDate: 2026-09-28 · Plan C …'`.

- [ ] **Step 12: Record the run**

Run: `uv run python D:/DEV/_scratch/dgx/planC/c_verdict.py D:/DEV/_scratch/dgx/planC/c1 --adr docs/adr/0001-performance-optimization-decisions.md`
Expected: the same block as Step 9, exit status 0, and on stderr `recorded in docs\adr\0001-performance-optimization-decisions.md`. The block lands at the end of the Plan C addendum, keeping the file's line endings.

Run: `git diff --stat`
Expected: only `docs/adr/0001-performance-optimization-decisions.md` changed (plus `tests/test_cold_start.py` from Step 10).

- [ ] **Step 13: Run it to verify it passes**

Run: `uv run pytest tests/test_cold_start.py -v -k acceptance_run`
Expected: `1 passed, 64 deselected`.

- [ ] **Step 14: Run the full suite**

Run: `uv run pytest -q`
Expected: N0 + 65 passed.

- [ ] **Step 15: Commit**

```bash
git add docs/adr/0001-performance-optimization-decisions.md tests/test_cold_start.py
git commit -m "Record the Plan C acceptance run on gn100 in ADR 0001" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 16: Report**

Report the verdict block and the state left behind:
- gn100 has no `bench-tv` container.
- The `bench-tv` volume is migrated, with an IVF index (nlist 256).
- `~/planC/` holds the scripts and `c1/`.
- `~/raggio` is this branch's `HEAD`. Its previous bench files are under `~/raggio-bench/pre-deploy-<UTC time>/`.
- The page cache was only evicted with `posix_fadvise`, which needs no cleanup.

Put the block in the PR description, and add these follow-ups:
- **Plan A's `DETACH_MEMORY=8g`.** It was only there "until Plan C lands". The swapless run (C7) shows a 4 GiB detach passes, so Plan A's session can drop the override. That change belongs to Plan A's runbook, not to this PR.
- **The volume now carries the covering index.** An image from before Plan C would still open it, but that image recreates `idx_records_doc` on open (a CREATE INDEX over 2.55M rows). Before any pre-Plan C baseline session on this volume, run `c_probe.py legacy` with that image, as in `c_session.sh`'s `probe`, so its first start is not charged with the rebuild.

---

## Self-Review

**Spec coverage.** This maps the spec §2 row 3 scope and §3 decisions to tasks:

| Spec item | Task |
|---|---|
| Covering and partial indexes, one-time migration inside `open_meta_db` (D9) | 1 |
| Sorted-listing plan pin (`NOT INDEXED`) and the EXPLAIN-plan regression test (D9) | 2 |
| Numpy ghost diff | 3 |
| `Collection(...)` built off the loop, cancel-safe | 4 |
| `stats()` off the loop and cheap | 5 |
| `_vec_sample` by rowid sampling, with a sparse-id fallback | 6 |
| `ENV MALLOC_TRIM_THRESHOLD_` plus `malloc_trim(0)` after each index job (D13) | 7 |
| Headroom guard before the pre-work, sized to the measured need (D13) | 8 |
| `_backfill_vecs` without the full `records` scan, and the attach-replay cost | 9 |
| ADR 0001:33 correction, addendum, release note (D1, D9) | 10 |
| DGX protocol labels (§6: regime, cap, `sqlite_version`, `OPENBLAS_NUM_THREADS`) and true-cold eviction (D12) | 11 |
| §7 C thresholds 1–9 | 11 (`c_verdict.py` C1–C9), 12 (run) |

**Placeholder scan.** No `TBD`, `TODO`, "similar to Task N" or elided code. Every code step shows its code.

**Type consistency.** These names match the tasks that use them:
- `_ensure_indexes`;
- `idx_records_doc_type`;
- `idx_jobs_open` (partial, `status IN ('pending','processing')`, which D2's `_CLAIM_SQL` depends on);
- `_malloc_trim`;
- `ATTACH_NEED_FACTOR`.

**Review Focus.** Each line in the Review Focus section has its test in the owning task. The unscaled detach headroom is left as a user decision at plan review.

