# Concurrency Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make raggio's shared SQLite connections, its collection lifecycle and its per-type counts safe under concurrent requests. A search racing `DELETE /collections/{name}` answers 404, never 500. A concurrency test module backs this and stays green 50/50 runs, and p4's race phase is re-run on gn100 after B lands.

**Architecture:**
- **Catalog.** Every statement on the manager's single catalog connection runs under `_catalog_lock`.
- **Delete.** `delete_collection` evicts the collection and deletes its row under `_load_lock`, so a racing `touch()` cannot resurrect it.
- **Stop.** `Collection.stop()` takes the RW write lock first. It then cancels the worker, syncs, and closes every connection under `db_lock`. `_rdb` refuses to register a read connection once the collection is closed.
- **Counts.** `indexed_counts` becomes copy-on-write: writers rebind a fresh dict under `db_lock`.
- **Delete vs search.** `delete_collection` marks the resident collection `deleted` before `stop()`. A search queued for the read lock behind `stop()` then finds the collection closed and raises `CollectionDeletedError`. The search endpoint in `app.py` maps that one error to 404, the answer for a collection that does not exist (spec §3.1 B1).
- **Tests.** One new module, `tests/test_concurrency.py`, pins each race with a deterministic reproduction where the GIL allows one, or structurally where it doesn't. It also runs a mixed-workload stress test that is checked after quiescence, and in-process concurrent HTTP tests at c=16 (spec §3.1 B2).
- **DGX.** After merge, p4's race phase runs on gn100 against main before and after B, and both results are journaled (Task 8).

**Tech Stack:** Python 3.12, FastAPI/Starlette (sync dependencies run on the AnyIO threadpool), asyncio, sqlite3 (pysqlite, WAL), turbovec `IdMapIndex`, pytest, httpx `ASGITransport`, uv.

**Spec:** `docs/superpowers/specs/2026-09-28-raggio-performance-design.md`:
- §2: order 1, ships as its own PR.
- §3 D1: ADR 0001 addendum. D4: R1 is a correctness/availability fix.
- §4.1 ownership rows: `CollectionManager.get_config` / `list_collections` / `delete_collection` locking, `Collection.stop()`, `indexed_counts`.
- §4.2: new tests in their own module.
- §4.3: single writer, per-thread read connections.
- §3.1 B1: a search in flight on a collection that `DELETE /collections/{name}` is removing returns 404, never 500; B may change `app.py` for the error-to-404 mapping only. B2: an in-process concurrent HTTP test, and p4's race phase re-run on gn100 after B lands, with the before/after numbers journaled.
- §4.1: "The HTTP status of a search racing `DELETE /collections/{name}` (404, §3.1 B1)" belongs to B, in `app.py`, the error-to-404 mapping only.
- §7: "B: stress test green 50/50 runs; no performance claim. The in-process concurrent HTTP test passes: 0×401 on valid collection keys and 0×500 at c=16, and only 200 or 404 for searches racing `DELETE` (§3.1 B1, B2). After B lands, p4's race phase on gn100 shows 0 failures".

Briefs:
- `docs/superpowers/research/2026-09-28-item4b-freethreading-code.md`: §2 race inventory R1, R3, R4; §7 stage 1; §8 orphaned `to_thread` hazard.
- `docs/superpowers/research/2026-09-28-critic.md`: claim checks R1/R3/R4; cross-item dependencies (stop() with D2's flush, indexed_counts with 2a and item 1); the R1 framing decision.
- `docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`: the R1 hammer at c=16 and the R1–R4 race stress on gn100 at 19a8a2e; Conclusions 10 and 11.

## Global Constraints

- Base and branch: `main` @ 19a8a2e. Work on `git switch -c concurrency-correctness`; the plan ships as one PR (spec §2).
- Python: 3.12 via `uv run`. No new dependency, no `uv.lock` or `pyproject.toml` change.
- Files touched: `src/raggio/store.py`, `src/raggio/app.py` (Task 6: the search endpoint's error-to-404 mapping and its import, nothing else), the new `tests/test_concurrency.py`, and `docs/adr/0001-performance-optimization-decisions.md`. Nothing else in the PR: no existing tests, no `bench/`. After merge, Task 8 edits `docs/superpowers/journal/2026-09-performance-program.md` on a docs branch.
- Suite size: `uv run pytest -q` gives 46 passed on base. It grows to 50 / 52 / 57 / 60 / 62 / 63 passed after Tasks 1 / 2 / 3 / 4 / 5 / 6, and stays at 63 after Task 7 (docs only).
- Existing tests: all 46 keep passing at every commit. The one pre-existing Starlette deprecation warning stays.
- Test-only stress knobs are env vars read by the test module, not `Settings` fields:
  - `RAGGIO_STRESS_SECONDS`, default `3`;
  - `RAGGIO_STRESS_CLIENTS`, default `16`.
- Acceptance (§7):
  - `tests/test_concurrency.py` green in 50 of 50 consecutive runs, re-run after Task 6;
  - the in-process HTTP tests pass: 0×401 on valid collection keys and 0×500 at c=16 (Tasks 1 and 6), and only 200 or 404 for searches racing `DELETE` (Task 6);
  - after merge, p4's race phase on gn100 shows 0 failures on main with B (Task 8).
  - No performance claim anywhere (commit messages, ADR, PR, journal).
- Lock kinds: `_catalog_lock` and `db_lock` are `threading.Lock`, which is not reentrant; neither is ever held across an `await`. `_load_lock` is an `asyncio.Lock`.
- Lock order: `_load_lock` before `_catalog_lock`; `lock.write()` before `db_lock`.
- Encoding: new comments and the whole test module are ASCII. `store.py` and the ADR stay UTF-8 (they contain em dashes); edit them with a UTF-8 aware editor, never a Windows cp1252 shell redirect.
- Ownership boundaries (spec §4.1):
  - B edits only the `indexed_counts` lines inside `_upsert_rows`/`_delete_doc_rows` (D owns the rest of `_upsert_rows`, and `_run_worker`/`enqueue`);
  - B does not touch `_allow_cache`, `_vector_ids` or `_IvfIndex` (F owns them, with R2/R2b), nor `touch()` construction (C);
  - B edits `Collection.search` only for the `_closed` check at the top of its read section, and `app.py` only for the search endpoint's `CollectionDeletedError` → 404 mapping (§3.1 B1, §4.1).
- Commit trailer: every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- DGX: one task, Task 8, after B merges. It re-runs p4's race phase on gn100 as a correctness check (§3.1 B2, §7), not a measurement: no performance claim. It follows spec §6: a run window first, `setsid -f` into a log that is polled, never `pkill -f`, no sudo, never `bench-wv` or `~/turborag`, and nobody's DGX work stopped unasked.
- If a reviewer asks for a latency check of B's locks (for example, "did `_catalog_lock` or the `stop()` section slow the server?"), it is an A/B of the preceding main against main + B, both built from the same Dockerfile. It is reported, not claimed, and follows spec §6:
  - Both arms run on the same SQLite build and the same memstatus state. B merges at order 1, before A's PR 2 base migration, so both arms are on the Debian SQLite 3.40.1 image with memstatus on. Never compare a B arm on that image against a post-A arm on the PBS interpreter, which bundles SQLite 3.53.1 (p4, D15).
  - Every row records `sqlite3.sqlite_version` (read inside the container), `OPENBLAS_NUM_THREADS`, the memory cap and the page-cache regime. A's `bench/dgx/session.sh` (PR 1, order 0, so it is on main before B) already writes these into `image.json`/`regime.json`, and `compare.py` prints the arms' regimes above the table.
  - Concurrent hybrid at c=8 has a p99 of 3-7 s on Debian SQLite 3.40.1 with no Python lock involved (p2: 7.1 s IVF and 6.9 s flat, from SQLite's global memstatus malloc mutex). A p99 of that size is the SQLite convoy, not a regression of B's locks. Attribute a difference to B only if it exceeds the between-server band of two fresh containers of the same image.

## Review Focus

The spec implies five failure modes that no behaviour test in the original R1/R3/R4 scope exercises. They are listed most likely first, and each is now pinned by a test in the task that owns it. RF4 and RF5 already pass on base; they guard Tasks 2 and 3 and the later plans (C, D, F).

1. **RF1: D2 moves a flush or close out of `stop()`'s critical section.**
   - Implied by §4.1, `Collection.stop()` row: D2's flush goes "inside that same critical section, before the connections close".
   - If a later edit closes a connection outside `lock.write()` or outside `db_lock`, a search can be mid-query, or a write transaction mid-flight, on a connection that is being closed.
   - Pin: `test_stop_closes_connections_inside_the_write_lock_and_under_db_lock` (Task 3). A close spy records `(lock._writing, db_lock.locked())` at every connection's `close()`.
2. **RF2: D moves the `indexed_counts` publication after commit and outside `db_lock`.**
   - Implied by §4.1, `indexed_counts` row.
   - Two writers (the worker's upsert and a `delete_document`) then copy the same base, and one rebind drops the other's update. Counts drift, and search gating skips an allowlist it needs.
   - Pin: `test_indexed_counts_is_published_only_under_db_lock` (Task 4). A `__setattr__` spy asserts that every publication happens with `db_lock` held.
3. **RF3: a delete lands during an in-flight load of the same name.**
   - Implied by §4.1, manager row: C moves `Collection` construction off the loop on top of B's locking, which widens the window.
   - `touch()` yields mid-load (LRU eviction today, and construction once C lands). A delete landing there must wait for the load to finish; otherwise the load registers a collection whose row and files are gone.
   - Pin: `test_delete_waits_for_an_in_flight_load_of_the_same_collection` (Task 2).
4. **RF4: a threading lock is held across an `await`.**
   - Implied by §4.3 single-writer discipline. The two threading locks are also taken on the loop thread.
   - A coroutine suspended inside one parks every pool thread. A second coroutine that takes the lock on the loop then deadlocks the server.
   - Pin: `test_no_threading_lock_is_held_across_an_await` (Task 1). An AST scan of `store.py` for `await` / `async with` / `async for` inside any `with <expr>._catalog_lock` / `with <expr>.db_lock` block.
5. **RF5: a leaked connection keeps a deleted collection's directory alive.**
   - Implied by §4.1, `stop()` row, together with the Windows note in `Collection.__init__`.
   - One unclosed per-thread read connection makes `delete_collection`'s `rmtree(ignore_errors=True)` silently leave the directory behind. Re-creating the name then reopens old data.
   - Pin: `test_deleted_collection_leaves_no_files_and_its_name_can_be_reused` (Task 3).

---

## File Structure

- `src/raggio/store.py` (modify):
  - `CollectionManager`: catalog locking, `delete_collection` under `_load_lock`, `shutdown`.
  - `Collection.stop()`, the new `Collection._close_conns()`, and `Collection._rdb()`.
  - The `indexed_counts` writers in `_upsert_rows` and `_delete_doc_rows`.
  - The new `CollectionDeletedError`, `Collection.deleted`, the `_closed` check at the top of `Collection.search`'s read section, and `delete_collection` marking the collection before its eviction.
- `src/raggio/app.py` (modify): the `store` import line, and the search endpoint's `except CollectionDeletedError` → 404. Nothing else.
- `tests/test_concurrency.py` (create): one section per task, in this order: R1, delete_collection, R3, R4, stress, HTTP searches (B1/B2). Shared helpers sit at the top.
- `docs/adr/0001-performance-optimization-decisions.md` (modify): the `## Addendum 2026-09 — concurrency correctness` section is appended at the end.
- After merge, on a docs branch: `docs/superpowers/journal/2026-09-performance-program.md` (modify, Task 8): the before and after entries of the gn100 race re-run.

---

### Task 1: Catalog statements under `_catalog_lock` (R1)

**Files:**
- Modify: `src/raggio/store.py:1613-1615` (the comment above `self._catalog_lock`), `:1630-1640` (`get_config`), `:1650-1651` (`list_collections`), `:1751-1754` (`shutdown`)
- Create: `tests/test_concurrency.py`

**Interfaces:**
- Consumes:
  - `CollectionManager.catalog` (one `sqlite3.Connection`, `check_same_thread=False`) and `CollectionManager._catalog_lock` (an existing `threading.Lock`);
  - `raggio.store.hash_key`;
  - `raggio.app.create_app(embedder_factory=lambda cfg: FakeEmbedder())`.
- Produces:
  - `CollectionManager.get_config(name: str) -> CollectionConfig | None` and `CollectionManager.list_collections() -> list[str]`, with unchanged signatures, now safe from any thread;
  - the test helpers `DIM`, `ROOT`, `FakeEmbedder`, `make_manager(tmp_path, monkeypatch)` and the `busy_switching` fixture, which Tasks 2-5 reuse.

- [ ] **Step 1: Write the failing test**

Create `tests/test_concurrency.py` with exactly this content:

```python
"""Concurrency correctness (ADR 0001, addendum 2026-09): the shared catalog connection,
delete_collection vs a racing touch(), Collection.stop() vs in-flight threads,
copy-on-write indexed_counts, and a mixed-workload stress run checked after
quiescence. Races that can't be forced deterministically under the GIL are pinned
structurally (lock spies, identity checks) instead of by timing."""
import ast
import asyncio
import collections
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
from raggio.store import CollectionManager

DIM = 8
ROOT = {"x-api-key": "root-key"}


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


def make_manager(tmp_path, monkeypatch) -> CollectionManager:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    return CollectionManager(Settings(), lambda cfg: FakeEmbedder())


@pytest.fixture
def busy_switching():
    """Force a thread switch every ~1us so unlocked shared state interleaves at
    statement granularity instead of once per 5ms default switch interval."""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


# ---- R1: the shared catalog connection ----


class _LockAsserting:
    """Wraps a sqlite3 connection (and the cursors it returns): every call must run
    with `lock` held: pysqlite objects are not safe to share across threads."""

    def __init__(self, inner, lock):
        self._inner, self._lock = inner, lock

    def _check(self):
        assert self._lock.locked(), "catalog touched outside _catalog_lock"

    def execute(self, *args):
        self._check()
        return _LockAsserting(self._inner.execute(*args), self._lock)

    def fetchone(self):
        self._check()
        return self._inner.fetchone()

    def fetchall(self):
        self._check()
        return self._inner.fetchall()

    def commit(self):
        self._check()
        return self._inner.commit()

    def close(self):
        self._check()
        return self._inner.close()

    def __iter__(self):
        return self

    def __next__(self):
        self._check()
        return next(self._inner)


def test_every_catalog_statement_runs_under_the_catalog_lock(tmp_path, monkeypatch):
    m = make_manager(tmp_path, monkeypatch)
    m.catalog = _LockAsserting(m.catalog, m._catalog_lock)

    async def run():
        await m.create_collection("a", DIM, 4, None, None, "key-a")
        assert m.get_config("a").name == "a"
        assert m.get_config("missing") is None
        assert m.list_collections() == ["a"]
        m.set_index_config("a", {"nlist": 2, "nprobe": 1})
        await m.touch("a")
        await m.delete_collection("a")
        assert m.list_collections() == []
        await m.shutdown()

    asyncio.run(run())


def test_concurrent_catalog_reads_return_the_requested_row(tmp_path, monkeypatch, busy_switching):
    # require_collection is a sync dependency: FastAPI runs it on threadpool threads,
    # one per in-flight request, all reading the manager's single catalog connection
    m = make_manager(tmp_path, monkeypatch)
    names = [f"c{i}" for i in range(8)]

    async def create():
        for n in names:
            await m.create_collection(n, DIM, 4, None, None, f"key-{n}")

    asyncio.run(create())
    bad = []
    start = threading.Barrier(16)

    def hammer(t):
        start.wait()
        deadline = time.monotonic() + 1.0
        k = 0
        while time.monotonic() < deadline and len(bad) < 20:
            n = names[(t + k) % len(names)]
            k += 1
            try:
                cfg = m.get_config(n)
                if cfg is None or cfg.name != n or cfg.key_hash != store.hash_key(f"key-{n}"):
                    bad.append((n, cfg and cfg.name))
                if k % 8 == 0 and m.list_collections() != names:
                    bad.append(("list_collections", n))
            except Exception as e:  # InterfaceError / IndexError / SystemError on main
                bad.append((n, repr(e)))

    threads = [threading.Thread(target=hammer, args=(t,)) for t in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    asyncio.run(m.shutdown())
    assert bad == []


def test_concurrent_collection_key_checks_are_consistent(tmp_path, monkeypatch, busy_switching):
    # end to end: collection B's key must get exactly 401 on A and 200 on B, never a
    # 500 (InterfaceError) or a decision made against the other collection's row
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    app = create_app(embedder_factory=lambda cfg: FakeEmbedder())

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                for n in ("A", "B"):
                    r = await c.post("/collections", headers=ROOT,
                                     json={"name": n, "collection_key": f"key{n}"})
                    assert r.status_code == 201, r.text
                seen = collections.Counter()

                async def hit(i):
                    target = "A" if i % 2 else "B"
                    r = await c.get(f"/collections/{target}", headers={"x-api-key": "keyB"})
                    seen[f"{target}:{r.status_code}"] += 1

                # 16 concurrent requests x 80 rounds: c=16, as in p4's R1 hammer on gn100
                for _ in range(80):
                    await asyncio.gather(*(hit(i) for i in range(16)))
        return seen

    assert asyncio.run(run()) == {"A:401": 640, "B:200": 640}


def test_no_threading_lock_is_held_across_an_await():
    # _catalog_lock and db_lock are threading locks the loop thread takes too: an
    # await inside one parks every pool thread behind a suspended coroutine, and a
    # second coroutine taking it on the loop deadlocks the server (not reentrant)
    tree = ast.parse(Path(store.__file__).read_text(encoding="utf-8"))
    sites, awaits = collections.Counter(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        locks = [i.context_expr.attr for i in node.items
                 if isinstance(i.context_expr, ast.Attribute)
                 and i.context_expr.attr in ("_catalog_lock", "db_lock")]
        if locks:
            sites.update(locks)
            awaits += [(locks[0], n.lineno) for stmt in node.body for n in ast.walk(stmt)
                       if isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor))]
    assert sites["_catalog_lock"] and sites["db_lock"]  # the scan still sees the locks
    assert awaits == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_concurrency.py -v`

Expected: `3 failed, 1 passed`.
- `test_every_catalog_statement_runs_under_the_catalog_lock` fails with `AssertionError: catalog touched outside _catalog_lock`.
- `test_concurrent_catalog_reads_return_the_requested_row` fails with `assert [...] == []`. The list holds wrong rows such as `('c6', None)` or `('c0', 'c3')`, or `InterfaceError(...)` / `IndexError` reprs; the mix varies per run.
- `test_concurrent_collection_key_checks_are_consistent` fails with `assert Counter({...}) == {'A:401': 640, 'B:200': 640}`. The Counter carries `A:500`/`B:500`, spurious `B:401`, and usually `A:200`, meaning A accepted B's key. At c=16 on base it failed 16 of 16 sandbox runs, with `B:401` 26–61, `B:500` 12–49 and `A:500` 11–34 per run, and `A:200` in 15 of the 16.
  - The 500s are the pair that p4 logged on gn100 at 19a8a2e: `sqlite3.InterfaceError: bad parameter or other API misuse` at store.py:1631 in `get_config`, and `IndexError: tuple index out of range` on `row[7]` at store.py:1639. The `IndexError` means another statement's row, shorter than the 8 columns `get_config` selects, came back on the shared cursor.
  - The spurious 401s are silent wrong-row decisions: no traceback is logged.
- `test_no_threading_lock_is_held_across_an_await` PASSES. It is the RF4 pin that guards Tasks 2-3 and the later plans.

The two thread-race tests are probabilistic but failed on every sandbox run: 3/3, and 16/16 for the key check at c=16. `busy_switching` forces a thread switch every ~1 µs, so the interleavings happen at statement granularity.

- [ ] **Step 3: Minimal implementation**

In `src/raggio/store.py`, `CollectionManager.__init__`, replace the two-line comment above `self._catalog_lock = threading.Lock()` (it wrongly says index_config updates run on worker threads) so the block reads:

```python
        # EVERY catalog statement (reads too) runs under this lock: require_collection
        # is a sync FastAPI dependency, so get_config runs on threadpool threads while
        # the loop creates/deletes rows and attach/detach jobs update index_config.
        # One pysqlite connection used concurrently returns another query's row or
        # raises InterfaceError (ADR 0001, addendum 2026-09)
        self._catalog_lock = threading.Lock()
```

Replace `get_config`, `list_collections` and `shutdown` with:

```python
    def get_config(self, name: str) -> CollectionConfig | None:
        with self._catalog_lock:
            row = self.catalog.execute(
                "SELECT name, dim, bit_width, model, base_url, key_hash, tokenizer, index_config"
                " FROM collections WHERE name=?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return CollectionConfig(
            *row[:6], row[6] or "unicode61", json.loads(row[7]) if row[7] else None
        )
```

```python
    def list_collections(self) -> list[str]:
        with self._catalog_lock:  # fetchall inside: the cursor is the shared state
            rows = self.catalog.execute("SELECT name FROM collections ORDER BY name").fetchall()
        return [r[0] for r in rows]
```

```python
    async def shutdown(self) -> None:
        for name in list(self.resident):
            await self._evict(name)
        with self._catalog_lock:  # a threadpool get_config may still be mid-query
            self.catalog.close()
```

Leave `set_index_config`, `create_collection` and `delete_collection` as they are: they already take `_catalog_lock` around their statements.

- [ ] **Step 4: Run the new test and the full suite**

Run: `uv run pytest tests/test_concurrency.py -v`. Expected: `4 passed`.
Run: `uv run pytest -q`. Expected: `50 passed, 1 warning`.

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py tests/test_concurrency.py
git commit -m "Serialize every catalog statement under _catalog_lock" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `delete_collection` under `_load_lock`

**Files:**
- Modify: `src/raggio/store.py:1724-1729` (`delete_collection`)
- Modify: `tests/test_concurrency.py` (append a section)

**Interfaces:**
- Consumes:
  - `CollectionManager._load_lock` (`asyncio.Lock`, already held by `touch()` and by `housekeeping()`'s evictions);
  - `CollectionManager._evict(name)` and `_catalog_lock` from Task 1.
- Produces:
  - `CollectionManager.delete_collection(name: str) -> None`, signature unchanged. A `touch(name)` that races it either finishes loading first, and is then evicted, or raises `KeyError` (404) once the row is gone.
  - Plan C's off-loop construction builds on this.
  - Task 6 marks the resident collection `deleted` inside the same `_load_lock` section, before `_evict`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_concurrency.py`:

```python
# ---- delete_collection vs a racing touch() ----


def test_delete_collection_is_not_resurrected_by_a_racing_touch(tmp_path, monkeypatch):
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        in_stop, release = asyncio.Event(), asyncio.Event()
        real_stop = c.stop

        async def slow_stop():  # eviction yields to the loop (sync, worker cancel)
            in_stop.set()
            await release.wait()
            await real_stop()

        c.stop = slow_stop
        deleter = asyncio.create_task(m.delete_collection("x"))
        await in_stop.wait()
        toucher = asyncio.create_task(m.touch("x"))  # a request arriving mid-delete
        await asyncio.sleep(0.05)
        release.set()
        await deleter
        with pytest.raises(KeyError):
            await toucher  # the collection is gone: 404, never a reload of its files
        assert "x" not in m.resident
        await m.shutdown()

    asyncio.run(run())


def test_delete_waits_for_an_in_flight_load_of_the_same_collection(tmp_path, monkeypatch):
    # touch() yields mid-load while it LRU-evicts another collection (and, once
    # plan C builds Collection off the loop, while it constructs): a delete landing
    # there must wait, or the load registers a collection whose row and files are gone
    monkeypatch.setenv("MAX_RESIDENT_COLLECTIONS", "1")
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        for n in ("x", "y"):
            await m.create_collection(n, DIM, 4, None, None, None)
        x = await m.touch("x")
        in_stop, release = asyncio.Event(), asyncio.Event()
        real_stop = x.stop

        async def slow_stop():
            in_stop.set()
            await release.wait()
            await real_stop()

        x.stop = slow_stop
        loader = asyncio.create_task(m.touch("y"))  # budget 1: evicts x first
        await in_stop.wait()
        deleter = asyncio.create_task(m.delete_collection("y"))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(loader, deleter, return_exceptions=True)
        state = ("y" in m.resident, m._dir("y").exists(), m.get_config("y"),
                 loader.exception(), deleter.exception())
        await m.shutdown()
        return state

    assert asyncio.run(run()) == (False, False, None, None, None)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_concurrency.py -v -k "resurrected or in_flight_load"`

Expected: `2 failed`.
- `test_delete_collection_is_not_resurrected_by_a_racing_touch` fails with `Failed: DID NOT RAISE <class 'KeyError'>`: the touch reloaded the collection from the still-present row.
- `test_delete_waits_for_an_in_flight_load_of_the_same_collection` fails with `At index 3 diff: OperationalError('unable to open database file') != None`: the load opened the collection after its files were removed.

Both are deterministic. The tests hold `stop()` open with an `asyncio.Event`.

- [ ] **Step 3: Minimal implementation**

Replace `delete_collection` with:

```python
    async def delete_collection(self, name: str) -> None:
        # evict + catalog delete under _load_lock, like touch() and housekeeping's
        # evictions: stop() yields, and a touch() landing there would reload the
        # collection from the still-present row and files (a resident zombie on
        # deleted files that would also shadow a re-created collection of the same
        # name). Once the row is gone a racing touch() raises KeyError (404), so the
        # rmtree can run unlocked.
        async with self._load_lock:
            await self._evict(name)
            with self._catalog_lock:
                self.catalog.execute("DELETE FROM collections WHERE name=?", (name,))
                self.catalog.commit()
        await asyncio.to_thread(shutil.rmtree, self._dir(name), True)
```

- [ ] **Step 4: Run the new test and the full suite**

Run: `uv run pytest tests/test_concurrency.py -v`. Expected: `6 passed`.
Run: `uv run pytest -q`. Expected: `52 passed, 1 warning`.

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py tests/test_concurrency.py
git commit -m "Delete collections under _load_lock so a racing touch can't reload them" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `Collection.stop()` closes under the write lock and `db_lock` (R3)

**Files:**
- Modify: `src/raggio/store.py:655-669` (`stop`, plus a new `_close_conns` directly below it) and `:1065-1075` (`_rdb`)
- Modify: `tests/test_concurrency.py` (imports, two helpers, one appended section)

**Interfaces:**
- Consumes:
  - `Collection.lock` (`_RWLock`: `write()`, `_writing`) and `Collection.db_lock` (`threading.Lock`);
  - `_worker`, `_sync_index()`, `_read_conns`, `_read_local`, `_closed`.
- Produces:
  - The `Collection.stop() -> None` contract. Everything runs in one write-locked section: cancel the worker, then `_sync_index`, then `_closed = True`, then `_close_conns()`. Plan D2's flush goes inside this section, after the cancel and before `_close_conns`.
  - The new `Collection._close_conns() -> None`. It is sync, runs in a thread and holds `db_lock`.
  - `Collection._rdb()` raises `RuntimeError("collection '<name>' is closed")` on every thread once the collection is stopped, and never registers a connection after the sweep.
  - The test helpers `vec(seed)` and `make_collection(tmp_path)`.

- [ ] **Step 1: Write the failing test**

In `tests/test_concurrency.py`:
1. Add `import sqlite3` after `import collections`.
2. Change `from raggio.store import CollectionManager` to `from raggio.store import Collection, CollectionConfig, CollectionManager`.
3. Insert these helpers between the `FakeEmbedder` class and `make_manager`:

```python
def vec(seed):
    return np.random.default_rng(seed).standard_normal(DIM).tolist()


def make_collection(tmp_path) -> Collection:
    return Collection(CollectionConfig("t", DIM, 4, None, None, None), Path(tmp_path), FakeEmbedder)
```

Then append:

```python
# ---- R3: Collection.stop() vs in-flight threads ----


def test_stop_does_not_cancel_the_worker_inside_its_write_section(tmp_path):
    # cancelling first released the write lock mid-upsert while the orphaned to_thread
    # body went on to commit indexed=1 rows the index never received: a filtered search
    # in that window built an allowlist with ids the index lacks (turbovec KeyError)
    col = make_collection(tmp_path)
    inside, release = threading.Event(), threading.Event()
    real_upsert = col._upsert_rows

    def slow_upsert(rows, mat):
        inside.set()
        release.wait(5)
        return real_upsert(rows, mat)

    col._upsert_rows = slow_upsert

    async def run():
        col.start_worker()
        await col.enqueue({"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": vec(1)}]}]})
        assert await asyncio.to_thread(inside.wait, 5)
        stopper = asyncio.create_task(col.stop())
        await asyncio.sleep(0.1)
        cancelled_mid_write = col._worker.done()
        release.set()
        await asyncio.wait_for(stopper, 5)
        return cancelled_mid_write

    assert asyncio.run(run()) is False  # stop() waited for upsert -> add -> sync
    reopened = make_collection(tmp_path)  # the row reached the index before the close
    try:
        n = reopened._rdb().execute("SELECT COUNT(*) FROM records WHERE indexed=1").fetchone()[0]
        assert (n, len(reopened.index)) == (1, 1)
    finally:
        asyncio.run(reopened.stop())


def test_stop_waits_for_a_write_transaction_its_cancelled_worker_left_running(tmp_path):
    col = make_collection(tmp_path)
    inside, release = threading.Event(), threading.Event()
    outcome = {}

    def slow_finish(job_id, status, error):  # the worker's to_thread body, still running
        with col.db_lock:  # once stop() cancels the task that awaited it
            inside.set()
            release.wait(5)
            try:
                col.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
                col.db.commit()
                outcome["committed"] = status
            except sqlite3.ProgrammingError as e:  # "Cannot operate on a closed database"
                outcome["error"] = e

    col._finish_job = slow_finish

    async def run():
        col.start_worker()
        await col.enqueue({"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": vec(1)}]}]})
        assert await asyncio.to_thread(inside.wait, 5)
        stopper = asyncio.create_task(col.stop())
        for _ in range(500):  # until stop() has marked the collection closed
            if col._closed:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        closed_under_the_writer = stopper.done()
        release.set()
        await asyncio.wait_for(stopper, 5)
        return closed_under_the_writer

    assert asyncio.run(run()) is False  # stop() waited for db_lock before closing
    assert outcome == {"committed": "done"}


def test_read_connection_opened_during_stop_is_closed_not_leaked(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    opened = []
    connecting, release = threading.Event(), threading.Event()
    real_connect = sqlite3.connect

    def spy_connect(*args, **kw):
        conn = real_connect(*args, **kw)
        opened.append(conn)
        if threading.current_thread().name == "late-reader":
            connecting.set()  # past _rdb's _closed check, not yet registered
            release.wait(5)
        return conn

    monkeypatch.setattr(store.sqlite3, "connect", spy_connect)
    outcome = {}

    def late_reader():
        try:
            outcome["conn"] = col._rdb()
        except RuntimeError as e:
            outcome["error"] = e

    async def run():
        reader = threading.Thread(target=late_reader, name="late-reader")
        reader.start()
        assert await asyncio.to_thread(connecting.wait, 5)
        await col.stop()
        release.set()
        await asyncio.to_thread(reader.join, 5)

    asyncio.run(run())
    assert "error" in outcome  # a closed collection hands out no connection
    for conn in opened:  # and every connection it opened is closed (Windows: the
        with pytest.raises(sqlite3.ProgrammingError):  # dir stays deletable)
            conn.execute("SELECT 1")


class _CloseSpy:
    """Stands in for a sqlite3 connection; records the collection's lock state at close()."""

    def __init__(self, inner, col, seen):
        self._inner, self._col, self._seen = inner, col, seen

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self):
        self._seen.append((self._col.lock._writing, self._col.db_lock.locked()))
        self._inner.close()


def test_stop_closes_connections_inside_the_write_lock_and_under_db_lock(tmp_path):
    # the stop() contract plan D2 builds on (its flush goes in the same section, before
    # the close): no search mid-query on a read connection (write lock), no write
    # transaction mid-flight on self.db (db_lock) when the connections close
    col = make_collection(tmp_path)
    seen = []

    async def run():
        col.start_worker()
        await asyncio.to_thread(col.stats)  # a pool thread registers a read connection
        col.db = _CloseSpy(col.db, col, seen)
        col._read_conns[:] = [_CloseSpy(c, col, seen) for c in col._read_conns]
        await col.stop()

    asyncio.run(run())
    assert len(seen) >= 2
    assert set(seen) == {(True, True)}


def test_deleted_collection_leaves_no_files_and_its_name_can_be_reused(tmp_path, monkeypatch):
    # stop() must close every connection the collection opened (the write connection
    # and one read connection per thread that served it): on Windows one leaked handle
    # makes delete's rmtree(ignore_errors=True) leave the directory behind
    m = make_manager(tmp_path, monkeypatch)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        await c.enqueue({"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": vec(1)}]}]})
        while c.pending_jobs():
            await asyncio.sleep(0.01)
        await asyncio.gather(*(asyncio.to_thread(c.list_records, "both", None, None, 10, 0)
                               for _ in range(8)))
        assert c.get_document("d") is not None
        await m.delete_collection("x")
        gone = not m._dir("x").exists()
        await m.create_collection("x", DIM, 4, None, None, None)
        docs = (await m.touch("x")).stats()["documents"]
        await m.shutdown()
        return gone, docs

    assert asyncio.run(run()) == (True, 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_concurrency.py -v -k "stop or read_connection or deleted_collection"`

Expected: `4 failed, 1 passed`.
- `test_stop_does_not_cancel_the_worker_inside_its_write_section` fails with `assert True is False`. `stop()` cancelled the worker mid-upsert. The orphaned upsert then fails on the closed database, or, with the close merely moved under `db_lock`, it commits a row the index never receives.
- `test_stop_waits_for_a_write_transaction_its_cancelled_worker_left_running` fails with `assert True is False`: `stop()` returned while the orphaned `_finish_job` still held `db_lock`.
- `test_read_connection_opened_during_stop_is_closed_not_leaked` fails with `AssertionError: assert 'error' in {'conn': <sqlite3.Connection ...>}`.
- `test_stop_closes_connections_inside_the_write_lock_and_under_db_lock` fails with `assert {(False, False)} == {(True, True)}`.
- `test_deleted_collection_leaves_no_files_and_its_name_can_be_reused` PASSES. It is the RF5 pin; main already closes each registered connection.

All of these are deterministic: they use `threading.Event` gates, a connect spy and a close spy.

- [ ] **Step 3: Minimal implementation**

Replace `stop` with the following, and add `_close_conns` directly below it:

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

    def _close_conns(self) -> None:
        # under db_lock: a write transaction still in flight on self.db (a request's
        # enqueue, or the worker's claim/finish orphaned by its cancellation) commits
        # before the close, and _rdb re-checks _closed here, so no read connection
        # registers after this sweep
        with self.db_lock:
            conns, self._read_conns = self._read_conns, []
            for c in conns:
                c.close()
            self.db.close()
```

Replace `_rdb` with the following. The `_closed` re-check under `db_lock` pairs with `_close_conns`' sweep, and the thread-local is set only once the connection is registered:

```python
    def _rdb(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError(f"collection '{self.cfg.name}' is closed")
        db = getattr(self._read_local, "db", None)
        if db is None:
            db = sqlite3.connect(self.dir / "meta.db", check_same_thread=False)
            db.execute("PRAGMA query_only=1")
            with self.db_lock:
                if self._closed:  # stop() swept the registry while this one opened
                    db.close()
                    raise RuntimeError(f"collection '{self.cfg.name}' is closed")
                self._read_conns.append(db)
            self._read_local.db = db
        return db
```

- [ ] **Step 4: Run the new test and the full suite**

Run: `uv run pytest tests/test_concurrency.py -v`. Expected: `11 passed`.
Run: `uv run pytest -q`. Expected: `57 passed, 1 warning`.

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py tests/test_concurrency.py
git commit -m "Stop collections under the write lock, closing connections under db_lock" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Copy-on-write `indexed_counts` (R4)

**Files:**
- Modify: `src/raggio/store.py:572-577` (the comment on `indexed_counts` in `Collection.__init__`), `:822-856` (`_upsert_rows`, the `indexed_counts` lines only) and `:1507-1523` (`_delete_doc_rows`)
- Audit, no change: the readers at `store.py:878` (`request_index`), `:1175` (`_vector_ids`), `:1254` (`_prune_common`), `:1314` (`_bm25_rescore`) and `:1359` (`_text_ids`)
- Modify: `tests/test_concurrency.py` (one import, one appended section)

**Interfaces:**
- Consumes: `Collection.db_lock`, which every writer already holds.
- Produces the `Collection.indexed_counts: dict[str, int]` contract:
  - it is replaced wholesale, under `db_lock`, once per write transaction, and never mutated in place;
  - readers dereference the attribute once per use.
- Plan D moves the rebind after the commit but keeps it inside `db_lock`. Plan E receives `N = sum(counts.values())` as a parameter.

- [ ] **Step 1: Write the failing test**

In `tests/test_concurrency.py`, add `import re` after `import collections`, then append:

```python
# ---- R4: copy-on-write indexed_counts ----


def test_indexed_counts_writers_rebind_never_mutate_the_published_dict(tmp_path):
    # readers (search gating, BM25's N, request_index) iterate the dict from other
    # threads; on free-threaded builds an in-place insert races that iteration
    # ("dictionary changed size during iteration"), so writers must publish a new dict
    col = make_collection(tmp_path)

    def chunk(i):
        return {"id": f"c{i}", "text": f"t {i}", "vector": vec(i)}

    asyncio.run(col._process_job({"documents": [
        {"doc_id": "d1", "chunks": [chunk(1)]}, {"doc_id": "d2", "chunks": [chunk(2)]}]}))
    writes = {
        # first summary: a key the dict never had, plus a re-upsert (-1 then +1)
        "upsert": lambda: asyncio.run(col._process_job({"documents": [
            {"doc_id": "d1", "summary": {"text": "s", "vector": vec(9)}, "chunks": [chunk(1)]}]})),
        "delete": lambda: asyncio.run(col.delete_document("d2")),
    }
    for name, write in writes.items():
        published = col.indexed_counts
        before = dict(published)
        write()
        assert published == before, f"{name} mutated the published dict in place"
        assert col.indexed_counts is not published, f"{name} did not publish a new dict"
    assert col.indexed_counts == {"chunk": 1, "summary": 1}


def test_indexed_counts_is_published_only_under_db_lock(tmp_path):
    # plan D moves the publication after the commit: it must stay inside db_lock, or
    # the worker's upsert and a delete_document copy the same base and one rebind
    # drops the other's update (counts drift; search gating skips a needed allowlist)
    col = make_collection(tmp_path)
    seen = []

    class Spy(Collection):
        def __setattr__(self, name, value):
            if name == "indexed_counts":
                seen.append(self.db_lock.locked())
            super().__setattr__(name, value)

    col.__class__ = Spy
    asyncio.run(col._process_job({"documents": [
        {"doc_id": "d1", "chunks": [{"id": "c1", "text": "t", "vector": vec(1)}]}]}))
    asyncio.run(col.delete_document("d1"))
    assert seen == [True, True]  # one publication per write, each under db_lock


def test_no_in_place_indexed_counts_mutation_in_store_source():
    # tripwire for future writers (the ingest path is rewritten by later plans)
    src = Path(store.__file__).read_text(encoding="utf-8")
    assert not re.search(r"indexed_counts\[[^\n]*?\]\s*(=(?!=)|\+=|-=)", src)
    assert not re.search(r"del\s+[\w.]*indexed_counts\[", src)
    assert not re.search(r"indexed_counts\.(update|pop|popitem|setdefault|clear)\(", src)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_concurrency.py -v -k indexed_counts`

Expected: `3 failed`.
- `test_indexed_counts_writers_rebind_never_mutate_the_published_dict` fails with `AssertionError: upsert mutated the published dict in place`, followed by `assert {'chunk': 2, 'summary': 1} == {'chunk': 2}`.
- `test_indexed_counts_is_published_only_under_db_lock` fails with `assert [] == [True, True]`: there has been no rebind yet.
- `test_no_in_place_indexed_counts_mutation_in_store_source` fails with `assert not <re.Match object; span=(...), match='indexed_counts[old[2]] -='>`.

All three are deterministic. Under the GIL the race itself can't be forced, because `sum()` over dict values is one C call. The tests therefore pin the discipline: object identity, a lock spy and a source tripwire.

- [ ] **Step 3: Minimal implementation**

In `Collection.__init__`, extend the comment above `self.indexed_counts` so the block reads:

```python
        # per-type indexed-row counts, kept in step by _upsert_rows/_delete_doc_rows:
        # lets search skip the allowlist (and its full-table id fetch) when nothing
        # would be excluded — the allowlist path costs ~15x a plain scan at 500k rows.
        # Copy-on-write: writers (under db_lock) build a new dict and rebind it, never
        # mutate the published one, so a reader thread iterating it (sum(.values()))
        # can't hit "dictionary changed size during iteration" on free-threaded builds
        self.indexed_counts: dict[str, int] = dict(
            self.db.execute("SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type")
        )
```

Replace `_upsert_rows` with the following. Only the four `counts` lines differ from main:

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

Replace `_delete_doc_rows` with:

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

Reader audit, no change needed. Each reader reads `self.indexed_counts` once per call, so it sees exactly one published version, and the only writers are the two functions above:

| Reader (main line) | Thread | Access |
|---|---|---|
| `request_index` (878) | loop | `sum(self.indexed_counts.values())` |
| `_vector_ids` (1175) | loop | `self.indexed_counts.get(other, 0)` |
| `_prune_common` (1254) | read thread | `total = sum(self.indexed_counts.values())` |
| `_bm25_rescore` (1314) | read thread | `total = sum(self.indexed_counts.values())` |
| `_text_ids` (1359) | loop | `self.indexed_counts.get(other, 0)` |

- [ ] **Step 4: Run the new test and the full suite**

Run: `uv run pytest tests/test_concurrency.py -v`. Expected: `14 passed`.
Run: `uv run pytest -q`. Expected: `60 passed, 1 warning`.

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py tests/test_concurrency.py
git commit -m "Make indexed_counts copy-on-write" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Mixed-workload concurrency stress test

**Files:**
- Modify: `tests/test_concurrency.py` (three imports, one appended section)

**Interfaces:**
- Consumes:
  - `Collection.search(mode, qvec, qtext, k, scope, filt, expand)`, `enqueue`, `patch_metadata(doc_id, patch, apply_to_chunks)`, `delete_document`;
  - `list_records(scope, filt, sort, limit, offset, include_vector)`, `get_document`, `stats`, `pending_jobs`, `stop`;
  - `store._normalize`, plus `make_collection`, `vec` and `FakeEmbedder` from the earlier tasks.
- Produces:
  - the env knobs `RAGGIO_STRESS_SECONDS` and `RAGGIO_STRESS_CLIENTS`;
  - the helpers `_seeded(tmp_path)` and `_storm(col, seed, stop_after=None) -> (errors, ops)`.

- [ ] **Step 1: Write the failing test**

In `tests/test_concurrency.py`, add `import os` and `import random` after `import collections`, and `from types import SimpleNamespace` after `from pathlib import Path`. Then append:

```python
# ---- mixed-workload stress: a regression guard, checked after quiescence ----

# test-only knobs (not Settings): a longer soak is RAGGIO_STRESS_SECONDS=60
STRESS_SECONDS = float(os.environ.get("RAGGIO_STRESS_SECONDS", "3"))
STRESS_CLIENTS = int(os.environ.get("RAGGIO_STRESS_CLIENTS", "16"))
SEED_DOCS = 300
WORDS = [f"w{i}" for i in range(400)] + ["alpha", "beta", "gamma", "delta", "planet", "star"]
_CLOSED = re.compile(
    r"RuntimeError: collection '\w+' is closed|ProgrammingError: Cannot operate on a closed database"
)


def _text(rng) -> str:
    # Zipf-ish: low word ids are common, so the pruner hits its budget (two-stage BM25)
    return " ".join(WORDS[min(int(rng.zipf(1.3)), len(WORDS) - 1)] for _ in range(rng.integers(8, 40)))


def _doc(rng, i: int) -> dict:
    g = int(rng.integers(0, 30))
    return {
        "doc_id": f"d{i}",
        "summary": {"text": _text(rng), "metadata": {"g": g}} if rng.random() < 0.5 else None,
        "chunks": [
            {"id": f"d{i}c{j}", "position": j, "text": _text(rng), "metadata": {"g": g, "p": j}}
            for j in range(int(rng.integers(1, 5)))
        ],
    }


async def _drain(col: Collection, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while col.pending_jobs():
        assert time.monotonic() < deadline, "ingest jobs did not drain"
        await asyncio.sleep(0.05)


async def _seeded(tmp_path) -> Collection:
    col = make_collection(tmp_path)
    col.start_worker()
    rng = np.random.default_rng(1)
    for s in range(0, SEED_DOCS, 100):
        await col.enqueue({"documents": [_doc(rng, i) for i in range(s, s + 100)]})
    await _drain(col)
    return col


async def _storm(col: Collection, seed: int, stop_after: float | None = None):
    """STRESS_CLIENTS clients for STRESS_SECONDS: searches (15% cancelled mid-flight,
    leaving orphaned to_thread work), upserts and re-upserts, metadata patches,
    deletes, and the unlocked reads (list_records, get_document, stats). With
    stop_after, the collection is stopped mid-storm. Returns (errors, ops)."""
    rng, prng = np.random.default_rng(seed), random.Random(seed)
    emb = FakeEmbedder()
    errors, ops = collections.Counter(), collections.Counter()
    next_doc = SEED_DOCS
    deadline = time.monotonic() + STRESS_SECONDS

    async def search():
        mode = prng.choice(["vector", "vector", "text", "hybrid"])
        q = " ".join(prng.choice(WORDS[:60]) for _ in range(prng.randint(2, 6)))
        qvec = None
        if mode != "text":
            qvec = store._normalize(np.array(await emb.embed([q]), dtype=np.float32))
        expand = None
        if prng.random() < 0.2:
            expand = SimpleNamespace(siblings_topk=prng.choice([None, 3]),
                                     siblings_all=prng.random() < 0.3, summary=prng.random() < 0.5)
        filt = prng.choice([None, {"g": prng.randrange(30)},
                            {"g": {"in": [prng.randrange(30), prng.randrange(30)]}}])
        coro = col.search(mode, qvec, q, prng.choice([5, 10, 50]),
                          prng.choice(["chunks", "summaries", "both"]), filt, expand)
        if prng.random() < 0.15:
            try:
                await asyncio.wait_for(coro, prng.choice([0.0005, 0.002, 0.01]))
            except TimeoutError:  # cancelled; its to_thread body runs on, unlocked
                ops["search_cancelled"] += 1
            return
        await coro
        ops["search"] += 1

    async def write():
        nonlocal next_doc
        r = prng.random()
        if r < 0.45:
            docs = [_doc(rng, next_doc)]
            if prng.random() < 0.5:  # re-upsert an existing doc: the remove + add path
                docs.append(_doc(rng, prng.randrange(next_doc)))
            next_doc += 1
            await col.enqueue({"documents": docs})
        elif r < 0.75:
            await col.patch_metadata(f"d{prng.randrange(next_doc)}", {"g": prng.randrange(30)}, True)
        else:
            await col.delete_document(f"d{prng.randrange(next_doc)}")
        ops["write"] += 1

    async def read():
        r = prng.random()
        if r < 0.5:
            await asyncio.to_thread(
                col.list_records, prng.choice(["chunks", "both"]),
                prng.choice([None, {"g": prng.randrange(30)}]), prng.choice([None, "-g", "p"]),
                20, 0, prng.random() < 0.3,
            )
        elif r < 0.8:
            col.get_document(f"d{prng.randrange(next_doc)}")
        else:
            col.stats()
        ops["read"] += 1

    async def client():
        while time.monotonic() < deadline:
            r = prng.random()
            try:
                await (search() if r < 0.70 else write() if r < 0.85 else read())
            except Exception as e:
                errors[f"{type(e).__name__}: {str(e)[:80]}"] += 1
            await asyncio.sleep(0)

    async def stopper():
        await asyncio.sleep(stop_after)
        await col.stop()
        ops["stopped"] += 1

    await asyncio.gather(*(client() for _ in range(STRESS_CLIENTS)),
                         *([stopper()] if stop_after is not None else []))
    return errors, ops


def test_mixed_workload_keeps_index_and_counts_consistent(tmp_path):
    async def run():
        col = await _seeded(tmp_path)
        errors, ops = await _storm(col, seed=7)
        await _drain(col)
        await asyncio.sleep(0.5)  # orphaned to_thread bodies of cancelled searches land
        db = col._rdb()
        indexed = db.execute("SELECT COUNT(*) FROM records WHERE indexed=1").fetchone()[0]
        by_type = dict(db.execute(
            "SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type").fetchall())
        state = len(col.index), indexed, {k: v for k, v in col.indexed_counts.items() if v}, by_type
        await col.stop()
        return errors, ops, state

    errors, ops, (index_len, indexed, counts, by_type) = asyncio.run(run())
    assert errors == {}
    assert ops["search"] and ops["search_cancelled"] and ops["write"] and ops["read"]
    assert index_len == indexed  # every indexed row is in the vector index, nothing else
    assert counts == by_type  # indexed_counts never drifted from the table


def test_stop_under_load_fails_closed_and_closes_every_connection(tmp_path, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kw):
        conn = real_connect(*args, **kw)
        opened.append(conn)
        return conn

    monkeypatch.setattr(store.sqlite3, "connect", spy_connect)

    async def run():
        col = await _seeded(tmp_path)
        out = await _storm(col, seed=11, stop_after=STRESS_SECONDS / 2)
        await asyncio.sleep(0.5)  # orphaned to_thread bodies finish (and fail closed)
        return out

    errors, ops = asyncio.run(run())
    assert ops["stopped"] == 1
    assert errors, "no operation ran against the stopped collection"
    assert {k: v for k, v in errors.items() if not _CLOSED.match(k)} == {}
    for conn in opened:  # the write connection and every per-thread read connection
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
```

- [ ] **Step 2: Run it to verify it fails**

This task adds a regression guard over Tasks 1-4 and no new behaviour, so on the Task 4 code it is expected to PASS. To confirm the guard bites, run it once against two planted bugs, then revert each:

1. Delete the line `counts[old[2]] -= 1` in `_upsert_rows`.
   - Run: `uv run pytest tests/test_concurrency.py -v -k mixed_workload`.
   - Expected: FAIL, `AssertionError: assert {'chunk': 881, 'summary': 171} == {'chunk': 830, 'summary': 163}`. The numbers vary; re-upserts make the counts drift.
   - Revert: `git checkout -- src/raggio/store.py`.
2. Delete the two lines `for c in conns:` / `c.close()` in `_close_conns`.
   - Run: `uv run pytest tests/test_concurrency.py -v -k stop_under_load`.
   - Expected: FAIL, `Failed: DID NOT RAISE <class 'sqlite3.ProgrammingError'>`: a read connection was left open.
   - Revert: `git checkout -- src/raggio/store.py`.

Before the Task 3 reorder, the unguarded cancel-first `stop()` failed `test_stop_under_load_fails_closed_and_closes_every_connection` in 1 of 50 runs, with `KeyError: allowlist contains id(s) not present in index`. That is why Task 3 carries a deterministic test for the ordering.

- [ ] **Step 3: Minimal implementation**

None: the implementation is Tasks 1-4. This task adds only the test section above.

- [ ] **Step 4: Run the new test and the full suite**

Run: `uv run pytest tests/test_concurrency.py -v`. Expected: `16 passed`, in about 12 s.
Run: `uv run pytest -q`. Expected: `62 passed, 1 warning`.

Acceptance (§7): 50 consecutive green runs of the module. The loop takes about 12-15 minutes. Run it in the background, or as two batches of 25 (`seq 1 25`, then `seq 26 50`) where a tool call is capped at 10 minutes:

```bash
logs=$(mktemp -d); pass=0; fail=0
for i in $(seq 1 50); do
  if uv run pytest tests/test_concurrency.py -q -p no:cacheprovider > "$logs/run-$i.log" 2>&1; then
    pass=$((pass+1))
  else
    fail=$((fail+1)); echo "run $i failed: $logs/run-$i.log"
  fi
done
echo "PASS=$pass FAIL=$fail"
```

Expected: `PASS=50 FAIL=0`.

Optional soak: `RAGGIO_STRESS_SECONDS=60 uv run pytest tests/test_concurrency.py -q -k "mixed_workload or stop_under_load"`. Expected: `2 passed`, in about 130 s.

- [ ] **Step 5: Commit**

```bash
git add tests/test_concurrency.py
git commit -m "Add mixed-workload concurrency stress test" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Searches racing a collection delete answer 404 (B1, B2)

**Files:**
- Modify: `src/raggio/store.py`: a new `CollectionDeletedError` directly above `class Collection:`, `Collection.__init__` (one attribute), `Collection.search` (the top of its read section) and `CollectionManager.delete_collection`.
- Modify: `src/raggio/app.py:12` (the `store` import) and `:278-280` (the search endpoint's `except` clauses). Tasks 1-5 leave `app.py` untouched, so these are base line numbers.
- Modify: `tests/test_concurrency.py` (append a section)

**Interfaces:**
- Consumes:
  - Task 2's `delete_collection`, which evicts and deletes the row under `_load_lock`;
  - Task 3's `stop()`, which sets `_closed = True` inside the write lock, and `_rdb`'s `RuntimeError: collection '<name>' is closed`;
  - `_RWLock`'s writer preference: readers wait while `_writing or _writers_waiting`, so a queued `stop()` runs before the readers queued with it.
- Produces:
  - `store.CollectionDeletedError(Exception)`. `Collection.search` raises it, with the collection name, when it enters the read section of a collection that `delete_collection` closed;
  - `Collection.deleted: bool`, `False` until `delete_collection` sets it, before `stop()`;
  - `POST /collections/{name}/search` answers 404 `collection '<name>' not found` for it, the answer a root-key request gets once the row is gone. A collection-key request that arrives after that gets 401, since its key can no longer be checked; that is unchanged.
  - Nothing else changes status. A search on a collection that an LRU or idle eviction closed still raises `RuntimeError` (500): that collection exists, so 404 would be wrong. No SQLite error is ever mapped to 404: on a live collection it is a real fault.

**Why a 500 is left after Tasks 1-5.** In probe p4 on gn100 at 19a8a2e, searches racing `DELETE /collections/{name}` got 171–704 500s per run. The logged errors (`unable to open database file`, `no such table`, `disk I/O error`, a `FileNotFoundError` on the IVF directory) are those of a collection working on deleted files. That is either the resurrected collection that Task 2 removes, or a search that reopened a read connection on a stopped collection, which Task 3 makes fail closed. p4 did not separate the two. One interleaving survives Tasks 2 and 3:
1. A search already holds the `Collection` and is queued for the read lock behind a writer: the ingest worker's write section, a metadata PATCH, a document delete or an index swap.
2. `DELETE` arrives. `stop()` queues for the write lock too.
3. The writer finishes. Writer preference runs `stop()` before the queued readers, and it closes the collection.
4. The readers enter the closed collection. `_rdb` raises `RuntimeError: collection 'x' is closed`, a 500.

A vector or hybrid query sent as text (no supplied vector) takes the same path when its embedding call completes after `stop()`'s write section and before the embedder closes. Task 6 turns these into 404. The paths still left at 500 are in the ADR's Deferred row (Task 7).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_concurrency.py`:

```python
# ---- B1/B2: searches over HTTP at c=16, and racing DELETE /collections/{name} ----


def test_http_searches_at_c16_and_racing_a_collection_delete(tmp_path, monkeypatch):
    # spec 3.1 B2, in process: (1) 16 clients search, half with the collection
    # key: every one 200 (R1: no 401 on a valid key, no 500); (2) 16 clients search
    # while DELETE /collections/x lands behind a writer: 200 before it, 404 after
    # it, never 500 (B1)
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    app = create_app(embedder_factory=lambda cfg: FakeEmbedder())
    key = {"x-api-key": "key-x"}
    inside, release, gate = threading.Event(), threading.Event(), {}
    real_patch = Collection._patch_rows

    def gated_patch(self, *args):  # holds a PATCH inside its write section
        gate["col"] = self
        inside.set()
        release.wait(5)
        return real_patch(self, *args)

    def body(i):
        mode = ("vector", "text", "hybrid")[i % 3]
        q = {} if mode == "vector" else {"text": f"w{i % 7} w{i % 5}"}
        if mode != "text":
            q["vector"] = vec(i)
        return {"query": q, "mode": mode, "k": 5}

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post("/collections", headers=ROOT,
                                 json={"name": "x", "collection_key": "key-x"})
                assert r.status_code == 201, r.text
                docs = [{"doc_id": f"d{i}", "chunks": [
                    {"id": f"d{i}c{j}", "text": f"w{i % 7} w{j}", "vector": vec(10 * i + j)}
                    for j in range(3)]} for i in range(40)]
                r = await c.post("/collections/x/documents", headers=ROOT, json={"documents": docs})
                job = r.json()["job_id"]
                for _ in range(500):
                    r = await c.get(f"/collections/x/jobs/{job}", headers=ROOT)
                    if r.json()["status"] == "done":
                        break
                    await asyncio.sleep(0.01)
                phase1, phase2 = collections.Counter(), collections.Counter()

                async def client(t):  # phase 1: 20 searches each, alternating keys
                    for n in range(20):
                        hdr, who = (key, "key") if n % 2 else (ROOT, "root")
                        r = await c.post("/collections/x/search", headers=hdr, json=body(t + n))
                        phase1[f"{who}:{r.status_code}"] += 1

                await asyncio.gather(*(client(t) for t in range(16)))

                async def looper(t):  # phase 2: search until the delete reaches us
                    for n in range(10_000):
                        r = await c.post("/collections/x/search", headers=ROOT, json=body(t + n))
                        phase2[r.status_code] += 1
                        if r.status_code != 200:
                            return

                monkeypatch.setattr(Collection, "_patch_rows", gated_patch)
                loopers = [asyncio.create_task(looper(t)) for t in range(16)]
                await asyncio.sleep(0.05)
                patch = asyncio.create_task(c.patch("/collections/x/documents/d0", headers=ROOT,
                                                    json={"metadata": {"g": 1}}))
                assert await asyncio.to_thread(inside.wait, 5)
                await asyncio.sleep(0.1)  # the loopers' next searches queue behind it
                delete = asyncio.create_task(c.delete("/collections/x", headers=ROOT))
                for _ in range(500):  # until stop() waits for the write lock too
                    if gate["col"].lock._writers_waiting:
                        break
                    await asyncio.sleep(0.01)
                release.set()
                statuses = [(await patch).status_code, (await delete).status_code]
                await asyncio.gather(*loopers)
                r = await c.post("/collections/x/search", headers=ROOT, json=body(0))
                statuses.append(r.status_code)
        return dict(phase1), sorted(phase2), phase2[404], statuses

    assert asyncio.run(run()) == (
        {"key:200": 160, "root:200": 160}, [200, 404], 16, [200, 200, 404])
```

- The searches use supplied vectors in vector and hybrid mode, so `FakeEmbedder` is never awaited while the collection closes.
- `gated_patch` holds a PATCH inside its write section, on a pool thread, until `stop()` is queued for the write lock (`_writers_waiting`). Every looper's next search then queues behind both writers, as in step 1 of the interleaving above.
- The expected tuple is `(phase 1, the phase 2 statuses seen, loopers that ended on 404, [PATCH, DELETE, a search after the DELETE])`.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_concurrency.py -v -k racing_a_collection_delete`

Expected: `1 failed`, with `At index 1 diff: [200, 500] != [200, 404]`. The 16 searches queued behind the PATCH ran after `stop()` and got 500s (`RuntimeError: collection 'x' is closed`). Phase 1 already passes: Task 1 fixed R1. The failure is deterministic: 3/3 in the sandbox.

- [ ] **Step 3: Minimal implementation**

In `src/raggio/store.py`, insert this class directly above `class Collection:`, followed by two blank lines:

```python
class CollectionDeletedError(Exception):
    """A request reached a collection that DELETE /collections/{name} removed while
    it waited; the app answers 404, as for a name that never existed (spec 3.1 B1)."""
```

In `Collection.__init__`, add two lines after the `self._closed = False` comment block, so it reads:

```python
        self._closed = False  # set by stop(); makes stale searches fail closed instead
        # of resurrecting connections on a dead collection (leaks the handle and, on
        # Windows, keeps the deleted collection dir undeletable)
        self.deleted = False  # set by delete_collection before stop(); a search that
        # then finds the collection closed raises CollectionDeletedError (404)
```

In `Collection.search`, add the check as the first statement of the read section, so its start reads:

```python
        n = max(HYBRID_DEPTH, k) if mode == "hybrid" else k  # per-leg depth so RRF sees the tail
        async with self.lock.read():
            if self._closed:  # queued for the read lock behind stop()'s write section
                if self.deleted:
                    raise CollectionDeletedError(self.cfg.name)
                raise RuntimeError(f"collection '{self.cfg.name}' is closed")
            if mode == "vector":
                ids, scores = await self._vector_ids(qvec, n, scope, filt, nprobe)
```

The eviction case keeps `_rdb`'s message, so Task 5's `_CLOSED` pattern still matches it.

Replace `delete_collection` (Task 2's version) with:

```python
    async def delete_collection(self, name: str) -> None:
        # evict + catalog delete under _load_lock, like touch() and housekeeping's
        # evictions: stop() yields, and a touch() landing there would reload the
        # collection from the still-present row and files (a resident zombie on
        # deleted files that would also shadow a re-created collection of the same
        # name). Once the row is gone a racing touch() raises KeyError (404), so the
        # rmtree can run unlocked.
        async with self._load_lock:
            c = self.resident.get(name)
            if c is not None:  # its queued searches answer 404 once stop() closes it
                c.deleted = True
            await self._evict(name)
            with self._catalog_lock:
                self.catalog.execute("DELETE FROM collections WHERE name=?", (name,))
                self.catalog.commit()
        await asyncio.to_thread(shutil.rmtree, self._dir(name), True)
```

In `src/raggio/app.py`, change line 12 to:

```python
from .store import CollectionDeletedError, CollectionManager, _normalize, hash_key
```

and add an `except CollectionDeletedError` clause after `except ValueError` in the search endpoint (the `try` block at lines 270-280), so the block reads:

```python
        try:
            qvec = None
            if body.mode != "text":
                # in hybrid mode a supplied vector skips the embedding call
                vec = q.vector if q.vector is not None else (await c.embedder.embed([q.text]))[0]
                qvec = _normalize(np.array([vec], dtype=np.float32))
            hits = await c.search(body.mode, qvec, q.text, body.k, body.scope, body.filter,
                                  body.expand, body.nprobe)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except CollectionDeletedError:  # DELETE /collections/{name} landed first
            raise HTTPException(404, f"collection '{c.cfg.name}' not found")
        return {"hits": hits}
```

Map nothing else. `sqlite3` errors and the eviction `RuntimeError` keep their 500.

- [ ] **Step 4: Run the new test and the full suite**

Run: `uv run pytest tests/test_concurrency.py -v`. Expected: `17 passed`, in about 14 s.
Run: `uv run pytest -q`. Expected: `63 passed, 1 warning`.

Re-run Task 5's acceptance loop (Step 4 there) on the final module, in the background or as two batches of 25. Expected: `PASS=50 FAIL=0`, in about 12-15 minutes.

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py src/raggio/app.py tests/test_concurrency.py
git commit -m "Answer 404 to searches racing a collection delete" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: ADR 0001 addendum

**Files:**
- Modify: `docs/adr/0001-performance-optimization-decisions.md` (append after line 50, the end of "## Standing constraints these decisions respect")

**Interfaces:**
- Consumes:
  - the fixes and test names from Tasks 1-6;
  - the acceptance result of Task 5's loop, re-run after Task 6: `PASS=50 FAIL=0`.
- Produces:
  - the section `## Addendum 2026-09 — concurrency correctness`;
  - plans C and D append their own `## Addendum 2026-09 — <topic>` sections after it (spec D1).

- [ ] **Step 1: Write the failing test**

The check for this docs task is a heading count:

```bash
grep -c "^## Addendum 2026-09 — concurrency correctness" docs/adr/0001-performance-optimization-decisions.md
```

- [ ] **Step 2: Run it to verify it fails**

Run the command above. Expected: it prints `0` and exits with status 1.

- [ ] **Step 3: Minimal implementation**

Append this block to the end of the ADR, keeping one blank line after the last bullet of "Standing constraints". The file is UTF-8 with em dashes and `×`/`·`, so append with the editor, not a shell redirect.

If the loop result after Task 6 differs from 50/50, write the measured count in the stress-test row. Do not merge the plan until it reads 50/50 (§7).

```markdown
## Addendum 2026-09 — concurrency correctness

Date: 2026-09-28 · Plan B of the 2026-09-28 performance program (`docs/superpowers/specs/2026-09-28-raggio-performance-design.md`). Correctness and availability fixes only, no performance claim. Every row is pinned by `tests/test_concurrency.py`.

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| Every catalog statement (reads too: `get_config`, `list_collections`, the close in `shutdown`) runs under `_catalog_lock` | **Accepted (correctness)** | `require_collection` is a sync FastAPI dependency, so `get_config` runs on threadpool threads, concurrently with each other and with the loop's catalog writes, on one pysqlite connection. A 16-thread hammer on 3.12 (item 4b brief) got 11,895 wrong rows and 1,962 `InterfaceError`s in 5 s. Over HTTP at c=16 (16 concurrent requests × 80 rounds), a collection key drew 500s and spurious 401s in 16 of 16 sandbox runs (per run, of 640 each: `B:401` 26–61, `B:500` 12–49, `A:500` 11–34). The key check also ran against another collection's row: in 15 of the 16 runs, requests to collection A were accepted with collection B's key. On gn100 at 19a8a2e (probe p4, c=16), collection-key requests failed at 5.4–6.8 % on the GIL builds and 0.46 % on 3.14t: `InterfaceError` at store.py:1631, `IndexError: tuple index out of range` on `row[7]` at store.py:1639 (another statement's row came back), and silent 401s on valid keys. After the fix: the exact 401/200 split, with 0 errors, and 0×401/0×500 for collection-key searches at c=16. `async def require_collection` was not adopted; it would rely on every catalog caller staying on the loop, while the lock holds wherever the caller runs |
| `delete_collection` evicts the collection and deletes its catalog row under `_load_lock` | **Accepted (bug fix)** | Eviction's `stop()` yields. A `touch()` that landed there reloaded the collection from the still-present row and files. The result was a resident collection on deleted files, which also shadowed a re-created collection of the same name. When the delete landed during a load, the result was a 500 (`unable to open database file`). The rmtree stays outside the lock: once the row is gone, a racing `touch()` gets a 404 |
| `Collection.stop()` takes the write lock, then cancels the worker, syncs, and closes every connection under `db_lock` (`_close_conns`). `_rdb` re-checks `_closed` under `db_lock` before it registers a connection | **Accepted (bug fix)** | Cancelling the worker first released the write lock mid-upsert while the orphaned `to_thread` body kept running (asyncio locks don't bind threads). That body committed `indexed=1` rows the index never received, until the job's replay re-upserted them. A filtered search in that window raised turbovec `KeyError: allowlist contains id(s) not present in index`; this was seen in 1 of 50 stress runs of an interim fix that kept the cancel-first order. Closing outside `db_lock` pulled `self.db` out from under an in-flight write transaction. A read connection opened during `stop()` was never closed, which leaked a handle and, on Windows, left the directory undeletable. The worker can now be cancelled only outside its write section. Plan D2's batched-sync flush goes in the same section, before `_close_conns` |
| A search racing `DELETE /collections/{name}` answers 404: `delete_collection` sets `Collection.deleted` before `stop()`, a search that enters the closed collection raises `CollectionDeletedError`, and the search endpoint maps that one error to 404 | **Accepted (availability, spec §3.1 B1)** | On gn100 at 19a8a2e (probe p4), searches racing `DELETE` got 171–704 500s per run on every build, with the errors of a collection working on deleted files: `unable to open database file`, `no such table`, `disk I/O error`, and an IVF `FileNotFoundError`. The `_load_lock` row removes the resurrected collection, and the `stop()` row makes a stopped collection fail closed. After those two fixes, a search queued for the read lock behind a writer (ingest, PATCH, document delete, index swap) still ran after `stop()`, because writer preference lets `stop()` go first, and got a 500 on the closed collection. Only `CollectionDeletedError` is mapped. Broad SQLite errors are never turned into 404: on a live collection they are real faults. The in-process HTTP test gives 0×401 and 0×500 for 320 searches at c=16, half with the collection key, and only 200 or 404 for 16 searches queued behind a PATCH when `DELETE` lands. The gn100 re-run of p4's race phase is journaled after merge (plan B, Task 8) |
| `indexed_counts` is copy-on-write: writers copy, update and rebind it under `db_lock`, and readers read the attribute once | **Accepted (free-threading readiness)** | An in-place insert of a new type key races `sum(indexed_counts.values())` on reader threads under free-threading ("dictionary changed size during iteration"). With the GIL, `sum()` is one C call and can't fail, so the tests pin the discipline structurally: an identity check, a `db_lock` spy, and a source tripwire. Pre-seeding the key set (the item 4b option) was not adopted: copy-on-write also gives readers one consistent version of all counts and needs no list of types |
| Mixed-workload stress test: 16 clients × 3 s of searches (15% cancelled mid-flight), upserts and re-upserts, metadata patches, deletes and unlocked reads, plus a `stop()` mid-storm | **Accepted (regression guard)** | Checked after quiescence: the vector index size equals the number of indexed rows, `indexed_counts` equals `GROUP BY type`, and `stop()` fails closed (only "collection is closed" or "closed database" errors) and closes every connection it opened. After the fixes above: 50/50 green runs (Windows, 3.12) plus one 60 s soak (`RAGGIO_STRESS_SECONDS=60`) |
| Orphaned `to_thread` work from cancelled searches (stale allowlist R2, IVF id cache R2b); the request reads that take no RW lock (`list_records`, `get_document`, job status), which fail with "closed database" 500s during an eviction; and the requests other than a queued search that race `DELETE /collections/{name}` | **Deferred** | R2 and R2b belong to plan F, together with `_allow_cache` and `_IvfIndex`. Probe p4 did not observe R2 over HTTP on gn100: 0 stale documents in 6 runs. The 404 row above covers only a search that reaches the read lock after `stop()`. These still answer 500: writes racing the delete (ingest, metadata PATCH, document delete, index requests); an embedding call still in flight when `stop()` closes the embedder, which fails after about 7 s of retries; the unlocked reads; and a search on a collection that an LRU or idle eviction closed, which stays a `RuntimeError` 500 and never becomes a 404, because that collection still exists. All of them fail closed (an error, never a wrong answer). Whether they should take the RW lock, or map to 404 or 503, is an open review question |
```

- [ ] **Step 4: Run the new test and the full suite**

Run the `grep -c` command from Step 1. Expected: `1`.
Run: `uv run pytest -q`. Expected: `63 passed, 1 warning`.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0001-performance-optimization-decisions.md
git commit -m "Record the concurrency fixes in an ADR 0001 addendum" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Re-run p4's race phase on gn100 after B lands (B2)

This task runs once B's PR has merged into `main`. It re-runs probe p4's race stress (`docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`, "Race stress, R1–R4") on gn100 against two images built from the repository's `Dockerfile`: `pre`, which is main just before B, and `post`, which is main with B. It is a correctness check with no performance claim. p4's run at 19a8a2e is the reference, and `pre` repeats it on the same main that B merged onto. The DGX rules of spec §6 hold throughout:
- `ssh gn100` below stands for spec §6's access command. Spell it out in full in each command you run.
- No sudo.
- Nothing touches `bench-tv` or `bench-wv` (container or volume), `~/raggio` or `~/turborag`. The task works in its own `~/probe/planB-race` and runs one container at a time: p4's `probe-p4-race`.
- No `podman rm -f`: the copied script removes `probe-p4-race` with `podman rm --ignore` instead.
- Builds and runs are detached with `setsid -f` into a log that is polled. Nothing kills processes by a `pkill -f` pattern.
- Nobody's DGX work is stopped or deleted without asking.

**Files:**
- Modify, after merge, on the docs branch `docs/planB-race-rerun`: `docs/superpowers/journal/2026-09-performance-program.md`. It gets a before entry and an after entry in §2 Timeline, a dated note under §5.15 and a dated note in R-B2's Status cell.
- Outside the repo:
  - gn100 `~/probe/planB-race/`: the two source trees, the copied scripts, `build.sh`, `logs/` and `out/race/`;
  - two images, `localhost/raggio:planB-pre-<sha7>` and `localhost/raggio:planB-post-<sha7>`, kept and recorded in the journal;
  - `D:/DEV/_scratch/dgx/planB-race/`: the fetched `logs/` and `out/`.

**Interfaces:**
- Consumes:
  - p4's scripts on gn100, `~/probe/p4-freethreaded-build/scripts/` (`racestress.sh`, `racestress.py`, `srvwrap.py`), and its driver venv `~/probe/p4-freethreaded-build/drv` (httpx and numpy);
  - p4's figures at 19a8a2e (the p4 note's "Race stress, R1–R4"; journal §4.7 and §5.15);
  - `main` with B merged.
- Produces:
  - the before and after numbers per arm, rep and phase, in the journal;
  - the §7 B verdict: 0 failures on `post`.

- [ ] **Step 1: Ask for the run window**

The run takes about 15 minutes: two image builds, then 4 race runs of about 65 s each (2 reps, `pre` and `post` alternating). `timeout` caps the race script at 20 minutes, and the task stays within a 30-minute window.

If the executing session already holds the maintainer's go-ahead for DGX work (such as the standing "you can continue the DGX tasks too with no limits" for this program, 09-28 01:11:33Z, journal §7.7 and register W-2), cite it in the before entry, with the 30-minute cap. Otherwise send this, and run nothing on gn100 until the answer:

> Plan B's race re-run needs gn100 for about 15 minutes, capped at 30. It builds two images (main before and after B), then runs p4's race stress, 2 reps per image, in its own `~/probe/planB-race` directory and `probe-p4-race` container. It does not touch `bench-tv`, `bench-wv`, `~/raggio` or `~/turborag`, and it keeps the two images. When may it run?

If the maintainer declines, stop here and report Task 8 as not run.

- [ ] **Step 2: Laptop checks**

Run, in Git Bash from the repo root, right after B merges:

```bash
git switch main && git pull --ff-only
POST=$(git rev-parse --short=7 HEAD)
PRE=$(git rev-parse --short=7 HEAD^1)
echo "pre=$PRE post=$POST"
git diff --stat $PRE $POST -- src
git grep -c CollectionDeletedError $POST -- src
git grep -c CollectionDeletedError $PRE -- src || echo "none at pre"
```

Expected:
- `pre=<sha7> post=<sha7>`, two different SHAs;
- the diff stat lists only `src/raggio/app.py` and `src/raggio/store.py`, ending `2 files changed`;
- `<post sha7>:src/raggio/app.py:2` and `<post sha7>:src/raggio/store.py:3`, then `none at pre`.

`HEAD^1` is main before B after a merge or squash commit. After a rebase merge, set `PRE` to the parent of B's first commit. If another PR landed after B, set `POST` to B's last commit on main. Either way the three checks above must hold.

- [ ] **Step 3: Read-only preflight on gn100**

Run:

```bash
ssh gn100 '
uptime; free -m
podman ps -a --format "{{.Names}} | {{.Status}} | {{.Image}}"
ls ~/probe/p4-freethreaded-build/scripts/racestress.sh ~/probe/p4-freethreaded-build/scripts/racestress.py ~/probe/p4-freethreaded-build/scripts/srvwrap.py
~/probe/p4-freethreaded-build/drv/bin/python -c "import httpx, numpy" && echo "driver ok"
ls -d ~/probe/planB-race 2>/dev/null || echo "no planB-race yet"'
```

Expected:
- no container named `probe-p4-race`;
- the three script paths, then `driver ok`;
- `no planB-race yet`;
- `free -m` showing at least 8 GB available: each run caps the server at 4 GiB.

If `probe-p4-race` exists, or a bench or probe is running, it is someone's work: stop, report what you saw, and ask. Do not stop it. If p4's scripts or driver venv are gone, stop and report; restoring them is outside this task.

- [ ] **Step 4: Stage the scripts, sources and images**

Run, in Git Bash from the repo root, with `PRE` and `POST` from Step 2:

```bash
ssh gn100 'mkdir -p ~/probe/planB-race/logs && cd ~/probe/planB-race &&
cp -r ~/probe/p4-freethreaded-build/scripts scripts && chmod -R a+rX scripts &&
ln -s ~/probe/p4-freethreaded-build/drv drv &&
sed -i -e "s#^P=~/probe/p4-freethreaded-build\$#P=~/probe/planB-race#" \
  -e "s#podman rm -f probe-p4-race#podman rm --ignore probe-p4-race#" scripts/racestress.sh &&
grep -c "^P=~/probe/planB-race$" scripts/racestress.sh; grep -c "rm -f" scripts/racestress.sh'
for s in $PRE $POST; do
  git archive $s | ssh gn100 "mkdir ~/probe/planB-race/src-$s && tar -x -C ~/probe/planB-race/src-$s"
done
ssh gn100 'cat > ~/probe/planB-race/build.sh' <<'EOF'
#!/bin/bash
# plan B race re-run: build the pre- and post-B images from their git archives
# usage: setsid -f bash build.sh "pre=<sha7> post=<sha7>" </dev/null >logs/build.log 2>&1
set -u
cd ~/probe/planB-race || exit 1
for spec in $1; do
  arm=${spec%%=*} sha=${spec#*=}
  echo "== BUILD $arm $sha start $(date -Is)"
  podman build -t "localhost/raggio:planB-$arm-$sha" "src-$sha" > "logs/build-$arm.log" 2>&1
  rc=$?
  echo "rc=$rc $(podman image inspect "localhost/raggio:planB-$arm-$sha" --format '{{.Id}} {{.Size}}' 2>/dev/null)"
done
echo "== BUILD_DONE $(date -Is)"
EOF
ssh gn100 "cd ~/probe/planB-race && setsid -f bash build.sh 'pre=$PRE post=$POST' </dev/null >logs/build.log 2>&1"
```

Expected: `1`, then `0` (no `podman rm -f` left in the copy). Nothing else prints.

Every 1-2 minutes, run `ssh gn100 'tail -n 4 ~/probe/planB-race/logs/build.log'` until the last line is `== BUILD_DONE <time>`. Expected: two `rc=0` lines, each with an image id and size. On any other `rc`, read `logs/build-<arm>.log`, stop and report.

- [ ] **Step 5: Journal the before entry**

Before the launch, add an entry to `docs/superpowers/journal/2026-09-performance-program.md` §2 Timeline, under the run's date heading (`### <YYYY-MM-DD>`; add the heading if it is a new day). Follow the journal's entry convention: UTC time, what happens, the alternatives, evidence with conditions and its source file, who decided, status. It says:
- **`<HH:MM>Z. Plan B's race re-run on gn100 (DGX Spark) starts.`** B merged as `<post sha7>` onto `<pre sha7>`. The images are `localhost/raggio:planB-pre-<sha7>` and `localhost/raggio:planB-post-<sha7>`, from the repository `Dockerfile`.
- The go-ahead used (Step 1) and the 30-minute cap.
- The conditions: p4's scripts unchanged except the directory and `podman rm --ignore`; `--memory 4g`; `OPENBLAS_NUM_THREADS=1`; a fresh data directory per run; 45 s mixed phase; 2 reps, `pre` and `post` alternating.
- The reference at 19a8a2e (p4): collection-key requests failed at 5.4–6.8 % on the GIL builds and 0.46 % on 3.14t; base312's mixed-phase searches got 61 and 66 401s and 33 and 47 500s; searches racing `DELETE` got 464–704 500s on base312 and 171–704 on every build; R2 and R4 were not observed. Source: [the p4 note](../research/2026-09-28-dgx-probe-p4-freethreaded.md), "Race stress, R1–R4".
- Alternatives: none; spec §3.1 B2 prescribes this re-run.
- The expectation (spec §7 B): 0 failures on `post`.
- Status: running.

- [ ] **Step 6: Launch the race runs and poll**

Run:

```bash
ssh gn100 "cd ~/probe/planB-race && setsid -f timeout 20m bash scripts/racestress.sh 'pre=localhost/raggio:planB-pre-$PRE post=localhost/raggio:planB-post-$POST' 45 </dev/null >logs/racestress.log 2>&1; sleep 5; head -n 2 logs/racestress.log"
```

Expected: `== RACE start <time>`, usually followed by `== pre-race-r1 <time>`.

Every minute, run `ssh gn100 'grep -E "^== |^!! |RUN_FAILED" ~/probe/planB-race/logs/racestress.log | tail -n 3'`. The runs go `pre-race-r1`, `post-race-r1`, `pre-race-r2`, `post-race-r2`, then `== RACE_DONE <time>`, about 5 minutes after the start. If there is no `== RACE_DONE` after 20 minutes, `timeout` has ended the script: that is a finding (a possible deadlock), not something to retry. Record it, stop the task's own container with `ssh gn100 'podman stop -t 30 probe-p4-race; podman rm --ignore probe-p4-race'`, and go to Step 7.

Then fetch the logs and read the results:

```bash
mkdir -p /d/DEV/_scratch/dgx/planB-race
ssh gn100 'cd ~/probe/planB-race && tar -c logs out' | tar -x -C /d/DEV/_scratch/dgx/planB-race
cat /d/DEV/_scratch/dgx/planB-race/logs/racestress.log
grep -hF "[srvwrap] python" /d/DEV/_scratch/dgx/planB-race/logs/srv/race/*.log
```

Expected, for both `post` reps (lines `[post-race-r1] …` and `[post-race-r2] …`):
- `mixed:` has the one key `http200`: 0×401 and 0×500 on valid keys (R1), 0 transport errors;
- `mixed-writes:` only `ingest:http202`, `delete:http200`, `delete:http404`, `patch:http200` and `patch:http404`. A 404 is a document whose 202 ingest had not landed yet;
- `mixed-list:` only `http200`;
- `stale-check:` only `http200`, plus possibly `late_ingest_of_404_deleted_doc(expected)`. R2 is not B's (spec §2 row 6): a `STALE_deleted200_doc_returned` or `stale_ids` above 0 is recorded for F and does not fail B;
- `delete-under-load:` only `http200`, `http404` and `http401`: no `http500`, `RemoteProtocolError` or `ReadError`. A 401 there is a collection-key reader whose key can no longer be checked once the row is gone; R1 is judged in the mixed phase;
- `delete_collection=200 healthz_after=200 body500=''`;
- `state: exit=0 oomkilled=false`, and nothing after `server exceptions:`;
- no `changed size`, `Segmentation`, `Fatal Python` or `deadlock` line (R4), and no `!! driver rc=` line.

Expected, for both `pre` reps: failures like p4's base312 at 19a8a2e, that is 401s and 500s in `mixed:` and hundreds of `http500` in `delete-under-load:`. If `pre` shows none, the journal says the before arm did not reproduce; the verdict on `post` stands.

The four `srvwrap` lines print the same `python=` and `sqlite=`: 3.40.1 on the Debian image while B precedes A's PR 2, or 3.53.1 if A's PR 2 (the PBS interpreter) has landed. If `pre` and `post` differ, stop: the arms are not comparable (D15).

§7 B needs 0 failures only in the classes B owns: R1 in the mixed phase, 500s and dropped connections on searches racing `DELETE`, and R4. Any of those on `post` fails §7 B. An R2 stale read is journalled as a finding for F (spec §2 row 6) and does not fail B. Journal a failure with the server log excerpt, open an issue (`docs/agents/issue-tracker.md`), and do not re-tune or re-run until B is fixed.

- [ ] **Step 7: Journal the after entry**

Add to the journal:
- **§2 Timeline:** `**<HH:MM>Z. Plan B's race re-run on gn100 (DGX Spark) done.**` Give per arm and rep: the `mixed:`, `mixed-writes:`, `stale-check:` and `delete-under-load:` counts; `delete_collection` and `healthz_after`; the server exceptions; the `sqlite=` version. Set them against p4's 19a8a2e figures from the before entry. State the §7 B verdict (0 failures on `post`, or the failures). State that p4 observed neither R2 (a deleted document returned stale) nor R4 (a "changed size" `RuntimeError`) and whether this run did. Name the kept images with their ids, and the source: the logs in `~/probe/planB-race/logs/` on gn100. Status: done.
- **§5.15:** a dated note, `(Dated note, <YYYY-MM-DD HH:MM>Z: after B, …)`, with the `post` result in one or two sentences.
- **Register R-B2:** a dated note in its Status cell pointing to the §2 entry.

Keep every earlier entry as it is: the journal is append-only.

- [ ] **Step 8: Commit the journal**

```bash
git switch -c docs/planB-race-rerun
git add docs/superpowers/journal/2026-09-performance-program.md
git commit -m "Journal plan B's race re-run on gn100" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected: `1 file changed`. The PR from this branch is the maintainer's call.

---

## Self-Review

**Spec coverage**

| Spec requirement | Source | Task(s) | Pinned by |
|---|---|---|---|
| R1: `get_config` and `list_collections` under `_catalog_lock` | §2 order 1, §4.1, brief 4b §2 R1 | 1 | lock spy, 16-thread hammer, HTTP key check |
| R1 is framed as a correctness/availability fix | §3 D4 | 1 (comment), 7 (ADR row) | — |
| `delete_collection` takes `_load_lock` | §2, §4.1 | 2 | racing-touch test, RF3 |
| C's off-loop construction builds on B's locking | §4.1 | 2 | RF3 (delete during an in-flight load) |
| R3: `stop()` closes under `db_lock`, after the write-lock acquire | §4.1, brief 4b §2 R3, critic claim checks | 3 | orphaned `_finish_job`, late reader, RF1 close spy |
| D2's flush has a slot inside `stop()`'s section, before the close | §4.1, critic cross-item dependencies | 3 | RF1 |
| No orphaned worker write behind a released lock (found by the stress run) | §4.3 single writer, brief 4b §8 | 3 | write-section test |
| R4: `indexed_counts` copy-on-write; readers take one reference | §4.1, brief 4b §2 R4 | 4 | identity test, tripwire, reader audit table |
| D moves the update after commit and keeps copy-on-write | §4.1 | 4 | RF2 (published under `db_lock`) |
| New tests in their own module | §4.2 | 1-6 | `tests/test_concurrency.py` |
| Single writer; per-thread read connections | §4.3 | 1, 3 | RF4, RF5 |
| Stress test green 50/50; no performance claim | §7 | 5, re-run in 6 | 50-run loop |
| B1: a search racing `DELETE /collections/{name}` answers 404, never 500; `app.py` changes only for the error-to-404 mapping; broad SQLite errors are never mapped | §3.1 B1, §4.1 | 6, 7 (ADR row) | HTTP delete-race test |
| B2 (1): in-process concurrent HTTP test: 0×401 on valid collection keys and 0×500 at c=16, only 200 or 404 for searches racing `DELETE` | §3.1 B2, §7 | 1 (key check at c=16), 6 | HTTP key check, HTTP delete-race test |
| B2 (2): p4's race phase re-run on gn100 after B lands, 0 failures, before and after journaled | §3.1 B2, §7 | 8 | the `post` arm's race log |
| ADR 0001 addendum | §3 D1 | 7 | heading check |
| R2/R2b stay out of scope (plan F); the other requests racing `DELETE` stay 500 | §4.1, §3.1 B1 | — | ADR "Deferred" row |

**Placeholder scan:**
- No TBD, TODO, "similar to Task N", or elided code.
- The only `...` / `(...)` left are pytest's own truncated reprs, quoted in expected-failure lines whose exact contents vary per run.
- In Task 8, `<sha7>`, `<time>`, `<HH:MM>`, `<YYYY-MM-DD>` and `…` stand for values that exist only at run time (SHAs, times, the logged counts), as in plan C's DGX task.
- Every test and implementation step carries complete code.
- Every run step names the command and the expected result.

**Type and name consistency:**
- Every test named in Review Focus and in the Step 2 lists exists in the module text of Tasks 1-6.
- Names are spelled identically throughout: `_close_conns`, `_catalog_lock`, `_load_lock`, `db_lock`, `indexed_counts`, `lock._writing`, `lock._writers_waiting`, `CollectionDeletedError`, `Collection.deleted`, `RAGGIO_STRESS_SECONDS` and `RAGGIO_STRESS_CLIENTS`.
- The Task 8 names match p4's scripts: `racestress.sh`, `racestress.py`, `srvwrap.py`, the `probe-p4-race` container, the phase keys `mixed`, `mixed-writes`, `mixed-list`, `stale-check` and `delete-under-load`, and the `pre-race-r1` style labels.
- `make_collection` builds `CollectionConfig("t", DIM, 4, None, None, None)`, which fills its six required fields (`name, dim, bit_width, model, base_url, key_hash`).
- The module totals 17 tests (4 + 2 + 5 + 3 + 2 + 1), which with 46 existing gives 63.

**Validated in a sandbox:** an archive of `main` @ 19a8a2e, with the tasks applied cumulatively.
- Each task's new tests fail with the messages quoted in its Step 2, and pass after its Step 3.
- The full suite gives 50 / 52 / 57 / 60 / 62 / 63 passed.
- Task 1's key check at c=16 failed 16/16 on base, and passed in every run with Task 1 applied.
- Task 6's test failed 3/3 on the Task 5 code with `[200, 500] != [200, 404]`, and the 500 was `RuntimeError: collection 'x' is closed` from `_rdb`. It passed 3/3 after Task 6's Step 3.
- The Task 5 module was green 50/50: two parallel batches of 25, which is harder than sequential runs because they compete for CPU. The final module, with Task 6 (17 tests), was green 50/50 again in sequential runs: `PASS=50 FAIL=0` in 743 s. A verifier's 12 further sequential runs gave `PASS=12 FAIL=0`.
- One 60 s soak passed.
- Both planted bugs in Task 5 fail as quoted.
- The ADR addendum appends cleanly (UTF-8, 4 pipes per table row).

**Not validated in the sandbox, and why:**
- Only Windows 11 / CPython 3.12 was run. There is no Linux or aarch64 run of the suite and no CI run (A creates CI).
- Task 8 was not run: it needs B merged. Its commands reuse p4's scripts, which ran on gn100 at 19a8a2e; the copy, the two `sed` edits, `build.sh` and the expected lines were checked against those scripts, not executed. The timing gates (0.05-0.1 s sleeps, 5 s event waits) are generous but were tuned on one machine.
- Free-threaded 3.14t behaviour of R4 is plan G's suite run.
- The commit steps were not executed verbatim: the sandbox committed with scratch messages.
- The HTTP key-check RED is probabilistic. It failed 16/16 at c=16 in the sandbox, with a different Counter each time.
