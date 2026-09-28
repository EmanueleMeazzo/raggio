# IVF Shard Fan-out and Upstream Stance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the probed IVF shard searches in parallel instead of one after another. Make filtered IVF search cheap. Close the two orphaned-search cache races (R2, R2b). Sync only the IVF shards a job wrote. Draft the turbovec upstream asks locally. Prove all of it on the DGX against the preceding main.

**Architecture:** `_IvfIndex.search` builds one task per (query, shard) search, in the old serial loop's order. It runs them on a per-`Collection` `_ShardPool` whose threads are owned by the collection and joined by `stop()`, then merges the results in that same order, so any pool size returns the serial result bit-for-bit. The default pool size is `min(12, os.cpu_count())` (spec §4.2, p3). A filtered search cuts each shard's allowlist slice inside its own pool task, not up front on the caller (p3: 3.7 ms against 6.4–7.0 ms). The slice is intersected shard-side, binary-searching the smaller side into the larger, against allowlists sorted once at cache fill (p3: 29.8 → 2.0 ms). D3 is settled by p3: plain per-(query, shard) nq=1 fan-out, and no audience batching. The probe keeps a grouped arm only as a regression guard. Generation counters keep a scan that predates a write from caching its snapshot. Per-shard dirty flags make `sync()` write only what changed. The 32,768-row pooled-path cliff of turbovec is documented in `_ShardPool`, in ADR 0005 and in the DGX run, which reports the largest IVF list size.

**Tech Stack:** Python 3.12, FastAPI, turbovec 1.0.0 (`IdMapIndex`), numpy, SQLite, `concurrent.futures.ThreadPoolExecutor`, pytest. On the DGX: rootless podman 4.9.3 and bash.

**Spec:** `docs/superpowers/specs/2026-09-28-raggio-performance-design.md`:
- §1 item 3 and §2 row 6 (scope);
- §3 D1 (ADR 0005), D3 (fan-out grouping, settled by p3: plain q1, no audience batching), D6 and D7 (upstream stance, drafts outside the repo), D15 (SQLite build, `sqlite_version` on every row), D16 (BLAS threads: the image `ENV` is owned by A);
- §4.1 (F owns `_IvfIndex`, `_vector_ids`, `_allow_cache`; D2 consumes the dirty-shard API);
- §4.2 (`IVF_SEARCH_THREADS`, default 12; never `RAYON_NUM_THREADS=1`; `OPENBLAS_NUM_THREADS=1` is A's image `ENV`);
- §6 (DGX protocol) and §7 F (acceptance items 1–7).

Research:
- `docs/superpowers/research/2026-09-28-item3a-turbovec-internals.md` (inline nq=1 path, fan-out model);
- `…-item3b-upstream-landscape.md` (upstream asks, audience batching);
- `…-item4b-freethreading-code.md` (R2, R2b);
- `…-item2a-ingest-path.md` (dirty-shard sync);
- `…-critic.md`;
- `…-dgx-baseline.md` (the numbers ADR 0005 cites);
- `…-dgx-probe-p3-ivf-fanout.md` (pool size 12, in-task intersection, q1 over grouped, the 32,768-row cliff, the OpenBLAS spinners, the server A/B brackets in §7 F);
- `…-dgx-probe-p2-hybrid-native.md` (the SQLite memstatus mutex, a confounder for concurrent numbers).

## Global Constraints

- Base: `main` after Plans B, A (rest), C, D1 and E have merged (spec §2, order 6). Line numbers quoted below are for 19a8a2e and are orientation only: anchor every edit on the quoted text.
- Test command: `uv run pytest -q`. Every new test goes in `tests/test_ivf_fanout.py` (spec §4.2), UTF-8 with LF line endings.
- Test count: `tests/test_ivf_fanout.py` holds 15 tests after Task 1, 20 after Task 2, 22 after Task 3, 24 after Task 4, 29 after Task 5, 32 after Task 6, 33 after Task 7 and 34 after Task 10. The full suite grows by the same numbers over whatever the base has. The sandbox base had 46, so the full-suite totals quoted below are 46 plus the file's count.
- Spec §4.2, verbatim: "`IVF_SEARCH_THREADS`: default `min(12, os.cpu_count())` (D3, p3); `1` means the serial path. Owner F. Never set `RAYON_NUM_THREADS=1`: it flattens turbovec's pooled path to about 1,440 calls/s."
- Spec §4.2, verbatim: "`OPENBLAS_NUM_THREADS=1` is an image `ENV` (D16, owner A), not a `Settings` knob." This plan never edits the `Dockerfile`. Its DGX arms still pass `-e OPENBLAS_NUM_THREADS=1` explicitly, so both arms match even if the base image predates A's line.
- `IVF_SEARCH_THREADS`: unset, `0` or negative means `min(12, os.cpu_count() or 1)`. `1` means the serial loop, and no thread is ever started. Nothing in this plan (code, tests, scripts, docs) sets `RAYON_NUM_THREADS`. The one exception is the standalone upstream repro in Task 8, which lives outside the repo and never runs inside raggio.
- Pool threads are named `raggio-ivf-<collection name>`. A pool is built on the first search with more than one task and closed at the end of `Collection.stop()`.
- Merge order: tasks are built query-major in probe order (the 19a8a2e loop's order), and results are consumed in that order.
- Unchanged constants: the tiny-allowlist threshold `len(allowlist) <= 128` (probe the owning shards), the `_allow_cache` FIFO of 8 entries, `IVF_DEFAULT_NPROBE = 16`, and turbovec 1.0.0 (no `pyproject.toml` or `uv.lock` change).
- `bench/bench.py` is Plan A's: this plan adds `bench/ivf_fanout_probe.py` and never edits bench.py in the repo. The DGX session patches only its own DGX copy, in legacy mode.
- Spec §3 D3, verbatim: "**Settled by p3: plain per-(query, shard) nq=1 fan-out on a thread pool (3a). Audience batching (3b) is not shipped.** Grouping never beat q1 beyond noise on aarch64: best grp 570.6 vs q1:12 610.4 QPS, and grp:1 is −39 % vs serial. The mean audience group at nq=8 is 1.40 queries, where nq=2–3 calls cost 2–5× per query. Default pool size is 12, chosen on seed 7 and held on seed 42. T=20 loses 14–17 %. Filtered searches intersect the allowlist inside each (query, shard) task: 3.7 ms / 2.2 ms, against 6.4–7.0 / 3.9–4.4 ms up front on the caller. The results are bit-identical to serial, including tie order."
- There is no D3 gate. The probe's grouped arm is only a regression guard: it prints `OK` or `REVISIT`, and a `REVISIT` is reported to the user as a reason to reopen D3. It never ships anything.
- Filtered IVF search cuts each shard's allowlist slice inside the (query, shard) pool task that uses it, never up front on the calling thread. The tiny-allowlist path (≤128 ids) still finds the owning shards on the caller, because it needs them to build the probe list, and hands each task its already-cut slice.
- The 32,768-row pooled-path cliff: in turbovec 1.0.0, an nq=1 search on a shard under 32,768 rows (1,024 blocks) runs inline on the calling thread with the GIL released. From 32,768 rows up it takes turbovec's pooled rayon path, where all T fan-out threads share one rayon pool and the fan-out ceiling falls to about 2.45× (p3). On the DGX corpus (nlist 256, 2.55M rows) the largest list is 30,443 rows, 93 % of the cliff. The cliff is documented in the `_ShardPool` docstring and in ADR 0005, and the DGX run reports the largest list size (spec §7 F item 7). This plan adds no health or API field for it.
- Spec §3 D15: every DGX row records `sqlite3.sqlite_version`. F's pool tasks never touch SQLite: the allowlist SELECT runs on the caller once per cache miss, and the shard-id cache is a turbovec probe. The request path around them does touch it (`_vector_ids`'s SELECT, `_rescore_rows`'s fp16 blob fetch, `_hydrate`), so SQLite's global memstatus malloc mutex (p2) is a confounder for concurrent numbers. Both arms run the same SQLite build, and a concurrent-QPS gap between arms with different `sqlite_version` values is not attributed to F.
- Upstream drafts live in `D:/DEV/_scratch/upstream-drafts/` and are never committed or posted. The maintainer posts them personally (spec D7).
- DGX scripts live in `D:/DEV/_scratch/dgx/` on the laptop and in `~/planF/` on gn100. Fetched results go to `D:/DEV/_scratch/dgx/planF/`.
- Tunables are chosen on seed 7 (the probe's default) and published on seed 42 (the bench's). p3 chose the pool size, 12, on seed 7 and held it on seed 42. The probe's `fanout` mode re-measures it, and Task 10 reports the result. If the probe's chosen size differs from 12 beyond the band, that is a finding for the user, and this plan does not change the default on its own.
- Spec §6, verbatim: "Vector concurrent QPS is bimodal (120–225) across identical configs, so it always needs at least 3 runs." / "The band must include **between-server** variance: every A/B arm runs on a fresh container." / "Both arms of an A/B run with the same `OPENBLAS_NUM_THREADS`, and every row records it." / "Every row also states its memory cap and `sqlite3.sqlite_version` (D15)."
- Spec §7 F, verbatim, is the acceptance of Task 10. It is quoted in full there. The after-F note is quoted too: "After F, the server limit is the HTTP/event-loop pipeline, about 3.1 ms per request, against an in-process ceiling of 610–662 QPS. Follow-ups outside A–G: two `_drain_scans` batches in flight, and `_hydrate` off the loop." p3 adds a third follow-up: a cheaper parse of the vector body. All three are out of scope here.
- Commit trailer, on every commit: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

These are five failure modes the spec implies but that no task's main tests exercise, most likely first. Each has a pin test in its owning task. Before the p3 revision, every pin was checked in the sandbox against its named mutation: the pin passed on the task's code and failed on the mutated code. The p3 revision moved pin 3's guard into the pool task. That new mutation was traced by hand against the source and has not been re-run, so the implementer runs it once (see pin 3).

1. **`stop()` racing a fanned-out search whose request was cancelled (Task 1).**
   - What happens: `asyncio.to_thread` bodies outlive their cancelled requests. `stop()` may close the pool while such a search still has shard tasks queued behind busy threads.
   - What must hold: `close()` must let the queued tasks run (`shutdown(wait=True)`). With `cancel_futures=True` the orphaned search raises `CancelledError` part-way.
   - Pin: `test_close_during_a_fanned_out_search_lets_it_finish`. Mutation: `ex.shutdown(wait=False, cancel_futures=True)`.
2. **A query that contributes no task (Task 1).**
   - What happens: a query whose probed shards are all empty, or whose allowlist slices are all empty, adds no task.
   - What goes wrong: regrouping results by position ("npb results per query") shifts every later query's hits onto the wrong row.
   - Pin: `test_queries_without_shard_tasks_keep_their_rows`. Mutation: group by `n // len(probes[0])`.
3. **An allowlist of more than 128 ids that lives in one shard (Task 2).**
   - What happens: on the nprobe path, every other probed shard gets an empty slice. turbovec raises `ValueError` on an empty allowlist, so empty slices must be skipped, not searched. The slice is cut inside the pool task (spec §2 row 6), so the task itself must skip the call and return `None`, and the merge must drop the `None` without shifting any other result.
   - Pin: `test_large_allowlist_inside_one_shard_skips_the_other_probed_shards`. Mutation: drop `if not len(allow): return None` in `_IvfIndex.search`'s `run`. The `ValueError` then reaches the caller, directly on the serial path and through `f.result()` on the pool. Run this mutation once by hand: it was traced against the source, not re-run in the sandbox.
4. **A re-upsert that moves a row to another shard (Task 5).**
   - What happens: the job removes the old id from shard A and adds the new id to shard B, and both files must be rewritten.
   - What goes wrong: if `remove()` loses its dirty flag, A's file keeps the old id. The reload's ghost reconcile then hides it, so the pin reads the files directly.
   - Pin: `test_reupsert_that_moves_a_row_syncs_both_shards`. Mutation: `remove()` bumps the id generation without `_dirty.add(j)`.
5. **Thread cost of one pool per collection (Task 1).**
   - What must hold: flat collections never start pool threads, and an LRU eviction (`MAX_RESIDENT_COLLECTIONS`) joins the evicted collection's threads.
   - Pin: `test_flat_collections_start_no_threads_and_eviction_joins_them`. Mutations: an eager `ThreadPoolExecutor` in `_ShardPool.__init__`, and removing the pool close from `stop()`.

---

## File Structure

| Path | Change | Task |
|---|---|---|
| `src/raggio/config.py` | Modify: `default_ivf_search_threads()`, `Settings.ivf_search_threads` | 1 |
| `src/raggio/store.py` | Modify, as follows: | 1–5, 7 |
| | `_ShardPool`; `_IvfIndex.__init__`/`train`/`load`/`search` gain `pool`; `Collection(..., ivf_search_threads=)`; `stop()` closes the pool; the attach path passes it; `touch()` passes the setting | 1 |
| | `_sorted_ids`, `_IvfIndex._intersect`, the sorted allowlists in `_vector_ids` | 2 |
| | `_allow_gen`, `_allow_lock`, `_invalidate_allow` | 3 |
| | `_id_gen`, `_id_lock`, `_shard_written` | 4 |
| | `_dirty`, `dirty_shards`, `sync() -> int` | 5 |
| | IVF comments | 7 |
| `tests/test_ivf_fanout.py` | Create in Task 1; each later task appends one section | 1–7, 10 |
| `bench/ivf_fanout_probe.py` | Create: `stages`, `fanout` (with the grouped regression guard) and `exact` (seed-42 bit identity against the serial path, plus the largest list size) modes | 6 |
| `docs/adr/0005-ivf-fanout-and-upstream-stance.md` | Create. Task 10 replaces its "Pending:" Verification | 7, 10 |
| `README.md`, `docs/getting-started.md`, `docs/concepts.md`, `docs/indexing.md`, `docs/adr/0001-performance-optimization-decisions.md`, `docs/adr/0002-optional-ivf-index.md` | Modify: the knob row, the corrected fixed-cost claim, ADR 0005 cross-references | 7 |
| `D:/DEV/_scratch/upstream-drafts/` (outside the repo) | Six `turbovec-*.md` drafts, `neon_small_nq_repro.py`, `check_drafts.py`. Task 10 appends the DGX data | 8, 10 |
| `D:/DEV/_scratch/dgx/` (outside the repo) | `dgx-planF-ab.sh`, `planF-build.sh`, `planF-summary.py`, `planF-verify.py`, `planF-check.py` | 9 |
| `D:/DEV/_scratch/dgx/planF/`, `D:/DEV/_scratch/dgx/planF-shas.env` (outside the repo) | Fetched `ab.log`, `results/`, `summary.md`; the pinned base and F SHAs | 10 |

### Task 1: Per-collection shard pool and parallel IVF fan-out

**Files:**
- Modify: `src/raggio/config.py`
- Modify: `src/raggio/store.py`:
  - the imports;
  - the new `_ShardPool`, before `class _IvfIndex:`;
  - `_IvfIndex.__init__`, `train`, `load` and `search`;
  - `Collection.__init__`, `Collection.stop`, and the `_IvfIndex.train(` call in the attach path;
  - `CollectionManager.touch`.
- Create: `tests/test_ivf_fanout.py`

**Interfaces:**
- Consumes:
  - `Settings.__init__(self) -> None`, which reads env vars;
  - `IdMapIndex.search(q: np.ndarray, k: int, allowlist=None) -> tuple[np.ndarray, np.ndarray]` (turbovec 1.0.0; nq=1 on a shard under 32,768 rows runs inline with the GIL released);
  - `_IvfIndex._intersect(allow: np.ndarray, j: int) -> np.ndarray` (unchanged here);
  - `Collection.__init__(self, cfg: CollectionConfig, directory: Path, embedder_factory, set_index_config=None, ...)` as left by Plans C and E (E adds `native_bm25`);
  - `Collection.stop(self) -> None` as left by Plan B;
  - `CollectionManager.touch(self, name: str) -> Collection` as left by Plan C.
- Produces:
  - `default_ivf_search_threads() -> int` and `Settings.ivf_search_threads: int` (config.py);
  - `class _ShardPool(threads: int, name: str = "")`, with `.threads: int`, `.map(fn, tasks: list) -> list` (order-preserving) and `.close() -> None` (idempotent);
  - `_IvfIndex.__init__(self, centroids: np.ndarray, shards: list, nprobe: int, pool: _ShardPool | None = None)` and the attribute `_IvfIndex._pool`;
  - `_IvfIndex.train(cls, sample, nlist, dim, bit_width, nprobe, pool: _ShardPool | None = None)`;
  - `_IvfIndex.load(cls, directory, dim, bit_width, nprobe, pool: _ShardPool | None = None)`;
  - `Collection.__init__(..., ivf_search_threads: int | None = None)` and the attribute `Collection._shard_pool: _ShardPool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ivf_fanout.py` with exactly this content (UTF-8, LF). It includes the Review Focus pins 1, 2 and 5 (`test_close_during_a_fanned_out_search_lets_it_finish`, `test_queries_without_shard_tasks_keep_their_rows`, `test_flat_collections_start_no_threads_and_eviction_joins_them`):

```python
"""IVF shard fan-out (ADR 0005): pooled per-(query, shard) searches return the serial
result bit-for-bit (tie order included), the pool's lifecycle follows
Collection.stop(), and the IVF_SEARCH_THREADS knob reaches every collection."""
import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

import sys
sys.path.insert(0, "src")
import raggio.store as store
from raggio.config import Settings
from raggio.store import Collection, CollectionConfig, CollectionManager, _IvfIndex

DIM = 8


def unit(n, seed):
    v = np.random.default_rng(seed).standard_normal((n, DIM)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def build_ivf(n=3000, nlist=8, nprobe=3, threads=1, seed=0):
    X = unit(n, seed)
    ivf = _IvfIndex.train(X, nlist, DIM, 4, nprobe, pool=store._ShardPool(threads))
    ivf.add_with_ids(X, np.arange(1, n + 1, dtype=np.uint64))
    return ivf, X


def shard_ids(sh):
    probe = np.zeros((1, DIM), dtype=np.float32)
    probe[0, 0] = 1.0
    return np.sort(sh.search(probe, k=len(sh))[1][0])


def reference_search(ivf, queries, k, allowlist=None, nprobe=None):
    """Frozen copy of the serial _IvfIndex.search at 19a8a2e (store.py:485-530), with
    its own shard-id probe so it shares no helper with the code under test."""
    if allowlist is not None and len(allowlist) <= 128:
        owners = [
            j for j in range(ivf.nlist)
            if len(ivf.shards[j]) and len(np.intersect1d(allowlist, shard_ids(ivf.shards[j])))
        ]
        probes = [owners] * len(queries)
    else:
        npb = min(nprobe or ivf.nprobe, ivf.nlist)
        sims = queries @ ivf.centroids.T
        probes = [np.argpartition(-sims[qi], npb - 1)[:npb] for qi in range(len(queries))]
    out = []
    for qi, probe in enumerate(probes):
        q = np.ascontiguousarray(queries[qi : qi + 1])
        parts_s, parts_i = [], []
        for j in probe:
            sh = ivf.shards[j]
            if not len(sh):
                continue
            allow = allowlist
            if allow is not None:
                sids = shard_ids(sh)
                pos = np.minimum(np.searchsorted(sids, allow), len(sids) - 1)
                allow = allow[sids[pos] == allow]
                if not len(allow):
                    continue
            s, i = sh.search(q, k=min(k, len(sh)), allowlist=allow)
            parts_s.append(s[0])
            parts_i.append(i[0])
        if parts_s:
            s, i = np.concatenate(parts_s), np.concatenate(parts_i)
            top = np.argsort(-s)[:k]
            out.append((s[top], i[top]))
        else:
            out.append((np.empty(0, np.float32), np.empty(0, np.uint64)))
    width = max(len(s) for s, _ in out)
    scores = np.full((len(out), width), -np.inf, np.float32)
    ids = np.zeros((len(out), width), np.uint64)
    for qi, (s, i) in enumerate(out):
        scores[qi, : len(s)], ids[qi, : len(i)] = s, i
    return scores, ids


def assert_same(a, b):
    assert a[0].dtype == b[0].dtype and a[1].dtype == b[1].dtype
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


class ThreadSpy:
    """Delegating shard proxy (IdMapIndex is a frozen pyclass: no monkeypatching)."""

    def __init__(self, inner, seen):
        self.inner, self.seen = inner, seen

    def __len__(self):
        return len(self.inner)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def search(self, q, k, allowlist=None):
        self.seen.append(threading.current_thread().name)
        return self.inner.search(q, k=k, allowlist=allowlist)


class Boom(ThreadSpy):
    def search(self, q, k, allowlist=None):
        raise RuntimeError("boom")


def make_collection(tmp_path, name="t", threads=None):
    return Collection(
        CollectionConfig(name, DIM, 4, None, None, None), Path(tmp_path), lambda: None,
        ivf_search_threads=threads,
    )


def rowvec(i):
    v = np.random.default_rng(1000 + i).standard_normal(DIM)
    return (v / np.linalg.norm(v)).tolist()


def ingest(col, n):
    docs = [
        {"doc_id": f"d{i}", "chunks": [
            {"id": f"c{i}", "text": f"chunk {i}", "vector": rowvec(i), "metadata": {"g": i % 2}},
        ]}
        for i in range(n)
    ]
    asyncio.run(col._process_job({"documents": docs}))


def attach(col, nlist=8, nprobe=None):
    asyncio.run(col._process_job({"op": "attach_index", "nlist": nlist, "nprobe": nprobe}))


@pytest.fixture(autouse=True)
def small_min_rows(monkeypatch):
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)


def test_settings_ivf_search_threads(monkeypatch):
    # spec §4.2: default min(12, os.cpu_count()) (D3, p3); 1 = the serial path
    monkeypatch.delenv("IVF_SEARCH_THREADS", raising=False)
    assert Settings().ivf_search_threads == min(12, os.cpu_count() or 1)
    monkeypatch.setenv("IVF_SEARCH_THREADS", "3")
    assert Settings().ivf_search_threads == 3
    monkeypatch.setenv("IVF_SEARCH_THREADS", "1")
    assert Settings().ivf_search_threads == 1
    monkeypatch.setenv("IVF_SEARCH_THREADS", "0")  # 0 / unset: the default
    assert Settings().ivf_search_threads == min(12, os.cpu_count() or 1)


@pytest.mark.parametrize("threads", [1, 4])
def test_pooled_search_matches_serial_reference(threads):
    ivf, X = build_ivf(threads=threads)
    rng = np.random.default_rng(5)
    small = np.sort(rng.choice(np.arange(1, 3001, dtype=np.uint64), 40, replace=False))
    large = np.sort(rng.choice(np.arange(1, 3001, dtype=np.uint64), 900, replace=False))
    for nq in (1, 3, 8):
        q = np.ascontiguousarray(X[10 : 10 + nq])
        for nprobe in (1, 3, 8):
            for allow in (None, small, large):
                got = ivf.search(q, 20, allowlist=allow, nprobe=nprobe)
                assert_same(got, reference_search(ivf, q, 20, allowlist=allow, nprobe=nprobe))


def test_tie_order_is_identical_across_pool_sizes():
    ivf, X = build_ivf(threads=1)
    v = unit(1, 99)
    # the same vector under a different id in EVERY shard: identical calibration,
    # identical codes, identical scores — the merge's tie order is all that differs
    for j in range(ivf.nlist):
        ivf.shards[j].add_with_ids(v, np.array([50_000 + j], dtype=np.uint64))
    ref = reference_search(ivf, v, 30, nprobe=ivf.nlist)
    tied = ref[0][0][np.isin(ref[1][0], np.arange(50_000, 50_000 + ivf.nlist))]
    assert len(tied) == ivf.nlist and len(np.unique(tied)) == 1  # the ties are real
    for threads in (1, 2, 4, 8):
        ivf._pool = store._ShardPool(threads)
        assert_same(ivf.search(v, 30, nprobe=ivf.nlist), ref)
        ivf._pool.close()


def test_filtered_tie_order_is_identical_across_pool_sizes():
    # spec §7 F item 1, filtered: the same cross-shard ties, reached through the
    # in-task allowlist cut (>128 ids) and through the tiny owners path (<=128 ids)
    ivf, X = build_ivf(threads=1)
    v = unit(1, 99)
    tied_ids = np.arange(50_000, 50_000 + ivf.nlist, dtype=np.uint64)
    for j in range(ivf.nlist):
        ivf.shards[j].add_with_ids(v, tied_ids[j : j + 1])
    others = np.random.default_rng(11).choice(np.arange(1, 3001, dtype=np.uint64), 400, replace=False)
    large = np.sort(np.concatenate([others, tied_ids]))
    tiny = np.sort(np.concatenate([others[:60], tied_ids]))
    assert len(large) > 128 >= len(tiny)
    for allow in (large, tiny):
        ref = reference_search(ivf, v, 30, allowlist=allow, nprobe=ivf.nlist)
        tied = ref[0][0][np.isin(ref[1][0], tied_ids)]
        assert len(tied) == ivf.nlist and len(np.unique(tied)) == 1  # the ties are real
        for threads in (1, 2, 4, 8):
            ivf._pool = store._ShardPool(threads)
            assert_same(ivf.search(v, 30, allowlist=allow, nprobe=ivf.nlist), ref)
            ivf._pool.close()


def test_shard_calls_run_on_pool_threads():
    ivf, X = build_ivf(threads=4)
    seen = []
    ivf.shards = [ThreadSpy(sh, seen) for sh in ivf.shards]
    ivf.search(np.ascontiguousarray(X[:2]), 10, nprobe=4)
    assert len(seen) == 8 and all(n.startswith("raggio-ivf") for n in seen)
    seen.clear()
    ivf._pool.close()  # closed pool: the serial loop on the calling thread
    ivf.search(np.ascontiguousarray(X[:2]), 10, nprobe=4)
    assert seen == [threading.current_thread().name] * 8
    serial, _ = build_ivf(threads=1)
    serial.shards = [ThreadSpy(sh, seen) for sh in serial.shards]
    seen.clear()
    serial.search(np.ascontiguousarray(X[:2]), 10, nprobe=4)
    assert seen == [threading.current_thread().name] * 8
    assert serial._pool._ex is None  # threads=1 never builds an executor


def test_large_shard_concurrent_callers_match_serial():
    # shards above 32,768 rows leave turbovec's inline nq=1 path for its own rayon
    # pool (SINGLE_QUERY_PARALLEL_MIN_BLOCKS); concurrent callers must still merge
    # exactly as the serial loop does
    ivf, X = build_ivf(n=70_000, nlist=2, nprobe=2, threads=4, seed=3)
    assert max(len(sh) for sh in ivf.shards) > 32_768
    q = np.ascontiguousarray(X[:8])
    ref = reference_search(ivf, q, 50, nprobe=2)
    errors, results = [], []

    def caller():
        try:
            for _ in range(5):
                results.append(ivf.search(q, 50, nprobe=2))
        except Exception as e:  # surfaced below
            errors.append(e)

    threads = [threading.Thread(target=caller) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors and len(results) == 20
    for got in results:
        assert_same(got, ref)
    ivf._pool.close()


def test_shard_error_reaches_every_waiter(tmp_path):
    col = make_collection(tmp_path, threads=4)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=8)
    col.index.shards[3] = Boom(col.index.shards[3], [])

    async def go():
        qs = [np.array([rowvec(i)], dtype=np.float32) for i in range(3)]
        return await asyncio.gather(
            *[col._scan_batched(q, 5) for q in qs], return_exceptions=True
        )

    res = asyncio.run(go())
    assert all(isinstance(r, RuntimeError) and str(r) == "boom" for r in res)
    asyncio.run(col.stop())


def test_single_worker_default_executor_does_not_deadlock(tmp_path):
    col = make_collection(tmp_path, threads=4)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=8)

    async def go():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(1))
        q = lambda i: np.array([rowvec(i)], dtype=np.float32)
        searches = [
            col.search("vector", q(i), None, 5, "chunks", {"g": i % 2} if i % 2 else None, None)
            for i in range(16)
        ]
        return await asyncio.wait_for(asyncio.gather(*searches), 10)

    hits = asyncio.run(go())
    assert all(hits) and hits[0][0]["id"] == "c0"
    asyncio.run(col.stop())


def test_stop_closes_pool_and_late_search_runs_serially(tmp_path):
    col = make_collection(tmp_path, name="stopme", threads=4)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=8)
    q = np.array([rowvec(7)], dtype=np.float32)
    before = col.index.search(q, 5, nprobe=8)
    ex = col._shard_pool._ex
    assert ex is not None  # a multi-shard search built the pool lazily
    asyncio.run(col.stop())
    assert col._shard_pool._ex is None
    assert not [t for t in threading.enumerate() if t.name.startswith("raggio-ivf-stopme")]
    # an orphaned search (its task was cancelled, so it holds no lock) finishing
    # after stop() runs serially instead of raising from a shut-down executor
    assert_same(col.index.search(q, 5, nprobe=8), before)
    assert col._shard_pool._ex is None


def test_threads_one_is_the_serial_path(tmp_path):
    col = make_collection(tmp_path, threads=1)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=4)
    q = np.array([rowvec(i) for i in range(6)], dtype=np.float32)
    assert_same(col.index.search(q, 10), reference_search(col.index, q, 10))
    assert col._shard_pool.threads == 1 and col._shard_pool._ex is None
    asyncio.run(col.stop())


# ---- Review Focus pins (Task 1) ----


class Held(ThreadSpy):
    """Delegating shard proxy: every search waits until the test releases it."""

    def __init__(self, inner):
        super().__init__(inner, [])
        self.entered, self.release = threading.Event(), threading.Event()

    def search(self, q, k, allowlist=None):
        self.entered.set()
        assert self.release.wait(5)
        return self.inner.search(q, k=k, allowlist=allowlist)


def test_close_during_a_fanned_out_search_lets_it_finish():
    # RF1: stop() closes the pool while a search orphaned by a cancelled request still
    # has shard tasks queued behind busy threads; they must run, not be cancelled
    ivf, X = build_ivf(threads=2)
    q = np.ascontiguousarray(X[:4])
    ref = reference_search(ivf, q, 10, nprobe=8)
    held = Held(ivf.shards[0])
    ivf.shards[0] = held
    got, errors = [], []

    def search():
        try:
            got.append(ivf.search(q, 10, nprobe=8))
        except BaseException as e:  # CancelledError is a BaseException
            errors.append(e)

    t = threading.Thread(target=search)
    t.start()
    assert held.entered.wait(5)  # a pool thread sits in shard 0; the rest of the 32 queue
    closer = threading.Thread(target=ivf._pool.close)
    closer.start()
    closer.join(0.1)  # close() is now blocked waiting on the pool
    held.release.set()
    t.join(10)
    closer.join(10)
    assert not t.is_alive() and not closer.is_alive() and not errors
    assert_same(got[0], ref)


def test_queries_without_shard_tasks_keep_their_rows():
    # RF2: a query whose probed shard is empty contributes no task; its neighbours'
    # results must land on their own rows and its row must be pure padding
    ivf, X = build_ivf(threads=4)
    empty = 5
    gone = {int(r) for r in ivf._shard_ids(empty)}
    for rid in gone:
        ivf.remove(rid)
    assert len(ivf.shards[empty]) == 0
    a, b = [i for i in range(len(X)) if i + 1 not in gone][:2]
    q = np.ascontiguousarray(np.vstack([X[a : a + 1], ivf.centroids[empty : empty + 1], X[b : b + 1]]))
    got = ivf.search(q, 10, nprobe=1)
    assert_same(got, reference_search(ivf, q, 10, nprobe=1))
    assert got[1][0][0] == a + 1 and got[1][2][0] == b + 1  # each query finds itself
    assert (got[0][1] == -np.inf).all() and (got[1][1] == 0).all()
    ivf._pool.close()


def pool_threads(name):
    return [t for t in threading.enumerate() if t.name.startswith(f"raggio-ivf-{name}")]


def test_flat_collections_start_no_threads_and_eviction_joins_them(tmp_path, monkeypatch):
    # RF5: the pool exists per Collection but only IVF searches start threads, and a
    # collection evicted by MAX_RESIDENT_COLLECTIONS takes its threads down with it
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_RESIDENT_COLLECTIONS", "1")
    monkeypatch.setenv("IVF_SEARCH_THREADS", "4")
    docs = [
        {"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "text": f"chunk {i}", "vector": rowvec(i)}]}
        for i in range(300)
    ]
    q = np.array([rowvec(4)], dtype=np.float32)

    async def go():
        mgr = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        for name in ("flatc", "ivfc"):
            await mgr.create_collection(name, DIM, 4, None, None, None)
        flat = await mgr.touch("flatc")
        await flat._process_job({"documents": docs})
        assert (await flat.search("vector", q, None, 5, "chunks", None, None))[0]["id"] == "c4"
        assert flat._shard_pool._ex is None and not pool_threads("flatc")
        ivf = await mgr.touch("ivfc")  # cap 1: evicts flatc
        await ivf._process_job({"documents": docs})
        await ivf._process_job({"op": "attach_index", "nlist": 8, "nprobe": 8})
        assert (await ivf.search("vector", q, None, 5, "chunks", None, None))[0]["id"] == "c4"
        assert pool_threads("ivfc")
        await mgr.touch("flatc")  # evicts ivfc: its stop() joins the pool
        assert "ivfc" not in mgr.resident and not pool_threads("ivfc")
        await mgr.shutdown()

    asyncio.run(go())


def test_manager_passes_setting_to_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IVF_SEARCH_THREADS", "3")

    async def go():
        mgr = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        await mgr.create_collection("m", DIM, 4, None, None, None)
        col = await mgr.touch("m")
        threads = col._shard_pool.threads
        await mgr.shutdown()
        return threads

    assert asyncio.run(go()) == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ivf_fanout.py -q`

Expected: `15 failed`. The failures are `AttributeError` (no `Settings.ivf_search_threads`, no `store._ShardPool`) and `TypeError` (`train()` / `Collection()` got an unexpected keyword argument `pool` / `ivf_search_threads`), in:

`test_settings_ivf_search_threads`, `test_pooled_search_matches_serial_reference[1]`, `test_pooled_search_matches_serial_reference[4]`, `test_tie_order_is_identical_across_pool_sizes`, `test_filtered_tie_order_is_identical_across_pool_sizes`, `test_shard_calls_run_on_pool_threads`, `test_large_shard_concurrent_callers_match_serial`, `test_shard_error_reaches_every_waiter`, `test_single_worker_default_executor_does_not_deadlock`, `test_stop_closes_pool_and_late_search_runs_serially`, `test_threads_one_is_the_serial_path`, `test_close_during_a_fanned_out_search_lets_it_finish`, `test_queries_without_shard_tasks_keep_their_rows`, `test_flat_collections_start_no_threads_and_eviction_joins_them`, `test_manager_passes_setting_to_collection`.

- [ ] **Step 3: Implement**

3a. In `src/raggio/config.py`, insert this function between `import os` and `class Settings:`, with two blank lines on each side:

```python
def default_ivf_search_threads() -> int:
    """IVF shard fan-out pool size when IVF_SEARCH_THREADS is unset (ADR 0005). 12 is
    p3's best on the 20-core DGX, chosen on seed 7 and held on seed 42; 20 lost 14-17 %."""
    return min(12, os.cpu_count() or 1)
```

Then append these lines at the end of `Settings.__init__`, after the last existing assignment. At 19a8a2e that is `self.collection_idle_ttl = ...`; Plan E adds `self.native_bm25`, so keep it above. Plan D1 adds no `Settings` line; Plan D2 merges after this plan and appends its `SYNC_BATCH_*` lines below these:

```python
        # per-collection pool for IVF shard searches; 1 = the serial loop, unset/0 = default
        threads = int(os.environ.get("IVF_SEARCH_THREADS", "0") or 0)
        self.ivf_search_threads = threads if threads > 0 else default_ivf_search_threads()
```

3b. In `src/raggio/store.py`, make two import changes:
- add `from concurrent.futures import ThreadPoolExecutor` directly after `import unicodedata`;
- change `from .config import Settings` to `from .config import Settings, default_ivf_search_threads`.

3c. Insert the pool class directly before `class _IvfIndex:` (after `_ivf_auto_nlist`, with two blank lines on each side):

```python
class _ShardPool:
    """Order-preserving map for _IvfIndex's per-(query, shard) searches, on threads
    owned by one Collection and shut down by its stop() (ADR 0005). Never the asyncio
    default executor: callers already run inside it (to_thread), so waiting on it
    from there can deadlock. turbovec releases the GIL for a whole search (py.detach)
    and runs nq=1 on shards under 32,768 rows inline on the calling thread, so these
    threads scan in parallel.

    The 32,768-row cliff (1,024 blocks, turbovec 1.0.0): from that size up, an nq=1
    search takes turbovec's pooled path instead, where every thread of this pool shares
    one rayon pool and the fan-out ceiling falls to about 2.45x (p3). The DGX corpus's
    largest list (nlist 256, 2.55M rows) is 30,443 rows, 93 % of the cliff, and the DGX
    run reports it (spec §7 F item 7). Never set RAYON_NUM_THREADS=1: it flattens the
    pooled path to about 1,440 calls/s from T=2 (spec §4.2)."""

    def __init__(self, threads: int, name: str = "") -> None:
        self.threads = max(1, int(threads))
        self._prefix = f"raggio-ivf-{name}" if name else "raggio-ivf"
        self._ex: ThreadPoolExecutor | None = None  # built on the first fanned-out call
        self._lock = threading.Lock()
        self._closed = False

    def map(self, fn, tasks: list) -> list:
        # 1 thread, 1 task, or closed: the plain loop on the calling thread (a pool of 1
        # measured 0.89x of serial). IVF_SEARCH_THREADS=1 is the pre-0005 path bit-for-bit.
        if self.threads <= 1 or len(tasks) <= 1 or self._closed:
            return [fn(t) for t in tasks]
        with self._lock:
            if not self._closed and self._ex is None:
                self._ex = ThreadPoolExecutor(self.threads, thread_name_prefix=self._prefix)
            ex = self._ex
        if ex is None:  # closed while we waited for the lock
            return [fn(t) for t in tasks]
        try:
            futs = [ex.submit(fn, t) for t in tasks]
        except RuntimeError:  # shut down between the check and submit (an orphaned search)
            return [fn(t) for t in tasks]
        return [f.result() for f in futs]

    def close(self) -> None:
        """Idempotent. Waits for running shard searches; later map() calls run serially."""
        with self._lock:
            self._closed = True
            ex, self._ex = self._ex, None
        if ex is not None:
            ex.shutdown(wait=True)
```

3d. Replace `_IvfIndex.__init__` (19a8a2e: `def __init__(self, centroids: np.ndarray, shards: list, nprobe: int) -> None:` and its body, up to `@property` / `def nlist`) with:

```python
    def __init__(
        self, centroids: np.ndarray, shards: list, nprobe: int, pool: _ShardPool | None = None
    ) -> None:
        self.centroids = centroids  # (nlist, dim) f32, unit norm, static after train
        self.shards = shards
        self.nprobe = nprobe
        self._pool = pool or _ShardPool(1)  # the owning Collection's; default: serial
        # per-shard id arrays for allowlist intersection (turbovec rejects allowlist
        # ids an index doesn't hold): built lazily via a full-k probe, dropped for any
        # shard a write touches. ~8 bytes/row when filtered queries occur, else nothing.
        self._id_cache: list = [None] * len(shards)
```

3e. Replace the `train` signature (19a8a2e: `@classmethod` / `def train(cls, sample: np.ndarray, nlist: int, dim: int, bit_width: int, nprobe: int):`) with the following. Keep its docstring and body:

```python
    @classmethod
    def train(
        cls, sample: np.ndarray, nlist: int, dim: int, bit_width: int, nprobe: int,
        pool: _ShardPool | None = None,
    ):
```

Then change `train`'s last line from `return cls(np.ascontiguousarray(C), shards, nprobe)` to `return cls(np.ascontiguousarray(C), shards, nprobe, pool)`.

3f. Replace the whole `load` classmethod (from `@classmethod` / `def load(cls, directory: Path, dim: int, bit_width: int, nprobe: int):` to just before `def sync(`) with:

```python
    @classmethod
    def load(
        cls, directory: Path, dim: int, bit_width: int, nprobe: int,
        pool: _ShardPool | None = None,
    ):
        C = np.load(directory / "centroids.npy")
        shards = []
        for j in range(len(C)):
            p = directory / f"shard-{j:04d}.tvim"
            shards.append(
                IdMapIndex.load(str(p)) if p.exists() else IdMapIndex(dim=dim, bit_width=bit_width)
            )
        return cls(C, shards, nprobe, pool)
```

3g. Replace the whole `_IvfIndex.search` method (from `def search(self, queries: np.ndarray, k: int, allowlist=None, nprobe: int | None = None):` to just before `def all_ids(`) with:

```python
    def search(self, queries: np.ndarray, k: int, allowlist=None, nprobe: int | None = None):
        """Merged top-k over probed shards. Mirrors IdMapIndex.search: single-query
        results are trimmed exactly; a batch is rectangular, short rows padded with
        id 0 / -inf score (record ids start at 1, so padding never hydrates).
        Every (query, shard) search is one pool task (ADR 0005); the merge consumes
        them in the serial loop's order, so any pool size returns the serial result
        bit-for-bit, tie order included. A filtered task cuts its own allowlist slice
        on the pool thread (spec §2 row 6; p3: 3.7 ms against 6.4-7.0 ms cut up front
        on the caller), so the intersections run in parallel too."""
        tiny = allowlist is not None and len(allowlist) <= 128
        owned: dict[int, np.ndarray] = {}  # tiny allowlists only: shard j -> its slice
        if tiny:
            # tiny allowlists (metadata filters, sibling expansion) must not lose
            # rows to unprobed shards: probe exactly the shards that own them. The
            # owners are needed for the probe list itself, so this cut stays here.
            for j in range(self.nlist):
                if len(self.shards[j]):
                    a = self._intersect(allowlist, j)
                    if len(a):
                        owned[j] = a
            probes = [list(owned)] * len(queries)
        else:
            npb = min(nprobe or self.nprobe, self.nlist)
            sims = queries @ self.centroids.T
            probes = [
                np.argpartition(-sims[qi], npb - 1)[:npb] for qi in range(len(queries))
            ]
        tasks = []  # (qi, query row, shard index, shard, k), serial-loop order
        for qi, probe in enumerate(probes):
            q = np.ascontiguousarray(queries[qi : qi + 1])
            for j in probe:
                sh = self.shards[j]
                if len(sh):
                    tasks.append((qi, q, int(j), sh, min(k, len(sh))))

        def run(task):
            _, q, j, sh, kk = task
            allow = None
            if allowlist is not None:  # per-shard slice: turbovec rejects foreign ids
                allow = owned[j] if tiny else self._intersect(allowlist, j)
                if not len(allow):
                    return None  # turbovec rejects an empty allowlist; adds nothing
            s, i = sh.search(q, k=kk, allowlist=allow)
            return s[0], i[0]

        parts: list = [([], []) for _ in probes]
        for task, res in zip(tasks, self._pool.map(run, tasks)):
            if res is not None:
                parts[task[0]][0].append(res[0])
                parts[task[0]][1].append(res[1])
        out = []
        for parts_s, parts_i in parts:
            if parts_s:
                s, i = np.concatenate(parts_s), np.concatenate(parts_i)
                top = np.argsort(-s)[:k]
                out.append((s[top], i[top]))
            else:
                out.append((np.empty(0, np.float32), np.empty(0, np.uint64)))
        width = max(len(s) for s, _ in out)
        scores = np.full((len(out), width), -np.inf, np.float32)
        ids = np.zeros((len(out), width), np.uint64)
        for qi, (s, i) in enumerate(out):
            scores[qi, : len(s)], ids[qi, : len(i)] = s, i
        return scores, ids
```

3h. `Collection.__init__`. Add the keyword parameter `ivf_search_threads: int | None = None` as the last parameter, after Plan E's `native_bm25` if present. At 19a8a2e the result is:

```python
    def __init__(
        self, cfg: CollectionConfig, directory: Path, embedder_factory, set_index_config=None,
        ivf_search_threads: int | None = None,
    ) -> None:
```

Directly after the line `self.db = open_meta_db(directory / "meta.db", cfg.tokenizer)`, create the pool. Then pass it to the IVF load that follows. The block becomes (19a8a2e shape; keep any lines Plan C added around it):

```python
        self.db = open_meta_db(directory / "meta.db", cfg.tokenizer)
        # IVF shard fan-out (ADR 0005): threads start on the first multi-shard search and
        # stop with the collection; flat collections never start any. 1 = serial loop.
        self._shard_pool = _ShardPool(ivf_search_threads or default_ivf_search_threads(), cfg.name)
        # the catalog decides which representation is live; a stale sibling on disk
        # (crashed attach/detach) is ignored and rebuilt by the replayed job
        if cfg.index_config and (self.ivf_dir / "centroids.npy").exists():
            self.index = _IvfIndex.load(
                self.ivf_dir, cfg.dim, cfg.bit_width,
                cfg.index_config.get("nprobe", IVF_DEFAULT_NPROBE), pool=self._shard_pool,
            )
```

3i. In the attach path (`_attach_index`, 19a8a2e line ~979), pass the pool to `_IvfIndex.train`:

```python
            ivf = _IvfIndex.train(
                self._vec_sample(IVF_TRAIN_SAMPLE), nlist, self.cfg.dim, self.cfg.bit_width,
                int(nprobe_req or IVF_DEFAULT_NPROBE), pool=self._shard_pool,
            )
```

3j. At the very end of `Collection.stop()`, after the `async with self.lock.write():` block and after the embedder close, append these two lines. The pool closes last, outside the write lock: a close waits for running shard searches, and none of them needs the lock.

```python
        # after _closed: an orphaned search still running falls back to the serial loop
        await asyncio.to_thread(self._shard_pool.close)
```

For orientation, this is `stop()` as Plan B leaves it, with the two lines added. Plan D2 later adds its flush inside the write-locked section; the two lines stay last.

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

3k. In `CollectionManager.touch()`, pass the setting to the `Collection(...)` construction. Plan C builds it off the loop with `asyncio.to_thread`, and Plan E passes `native_bm25` next to it: add the keyword to whichever call constructs the `Collection`. At 19a8a2e the result is:

```python
                c = Collection(
                    cfg, self._dir(name), lambda: self.embedder_factory(cfg),
                    lambda ic, name=name: self.set_index_config(name, ic),
                    ivf_search_threads=self.settings.ivf_search_threads,
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `15 passed`.

Run: `uv run pytest -q`. Expected: all green, with 15 more tests than the base (on the sandbox base of 46: `61 passed`).

- [ ] **Step 5: Commit**

```bash
git add src/raggio/config.py src/raggio/store.py tests/test_ivf_fanout.py
git commit -m "Fan out IVF shard searches on a per-collection thread pool

IVF_SEARCH_THREADS (default min(12, cpu_count), 1 = the serial loop) sizes a pool
owned by each Collection and closed by stop(). Every (query, shard) search is one
task, and a filtered task cuts its own allowlist slice on the pool thread (p3:
3.7 ms against 6.4-7.0 ms up front). Results merge in the serial loop's order, so
any pool size is bit-identical, filtered or not, tie order included.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: Shard-side allowlist intersection

**Files:**
- Modify: `src/raggio/store.py`:
  - the new `_sorted_ids`, before `class _ShardPool:`;
  - `_IvfIndex._intersect`;
  - the head of `_IvfIndex.search`;
  - the allowlist construction in `Collection._vector_ids`.
- Modify: `tests/test_ivf_fanout.py` (append a section)

**Interfaces:**
- Consumes:
  - `_IvfIndex._shard_ids(self, j: int) -> np.ndarray`, which returns sorted uint64;
  - `_IvfIndex.search` from Task 1;
  - `Collection._vector_ids(...)`, whose `_allow_cache` holds `dict[str, np.ndarray]`.
- Produces:
  - `_sorted_ids(ids) -> np.ndarray`: uint64, strictly increasing; an already-sorted uint64 array is returned as-is, with no copy.
  - `_IvfIndex._intersect(self, allow: np.ndarray, j: int) -> np.ndarray`: requires `allow` sorted and unique, and returns the sorted intersection. An empty side gives an empty uint64 array.
  - `_vector_ids` caches and returns allowlists sorted ascending.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ivf_fanout.py`, after the last test and separated by two blank lines. The section includes Review Focus pin 3 (`test_large_allowlist_inside_one_shard_skips_the_other_probed_shards`):

```python
# ---- shard-side allowlist intersection (sorted allowlists) ----


def test_sorted_ids_normalizes():
    out = store._sorted_ids([9, 3, 5, 3])
    assert out.dtype == np.uint64 and out.tolist() == [3, 5, 9]
    ready = np.array([1, 4, 7], dtype=np.uint64)
    assert store._sorted_ids(ready) is ready  # already sorted: no copy, O(n) check only


def test_intersect_both_directions_match_intersect1d():
    ivf, _ = build_ivf()
    rng = np.random.default_rng(11)
    universe = np.arange(1, 3001, dtype=np.uint64)
    foreign = np.arange(90_000, 90_050, dtype=np.uint64)
    for size in (1, 5, 128, 129, 300, 2000, 3000):
        raw = np.concatenate([rng.choice(universe, size, replace=False), foreign, universe[:3]])
        allow = store._sorted_ids(rng.permutation(raw))
        for j in range(ivf.nlist):
            got = ivf._intersect(allow, j)
            assert np.array_equal(got, np.intersect1d(raw, shard_ids(ivf.shards[j])))


def test_unsorted_duplicate_large_allowlist_matches_reference():
    # _expand passes sibling ids unsorted; the shard-side direction binary-searches
    # the allowlist, so search() must normalize it first
    ivf, X = build_ivf(threads=4)
    rng = np.random.default_rng(12)
    raw = rng.choice(np.arange(1, 3001, dtype=np.uint64), 700, replace=False)
    raw = np.concatenate([raw, raw[:50], np.arange(80_000, 80_010, dtype=np.uint64)])
    q = np.ascontiguousarray(X[:4])
    for nprobe in (2, 8):
        got = ivf.search(q, 25, allowlist=rng.permutation(raw), nprobe=nprobe)
        assert_same(got, reference_search(ivf, q, 25, allowlist=np.unique(raw), nprobe=nprobe))


def test_large_allowlist_inside_one_shard_skips_the_other_probed_shards():
    # RF3: more than 128 ids (the nprobe path), all held by one shard: every other probed
    # shard's slice is empty and must be skipped (turbovec rejects an empty allowlist)
    ivf, X = build_ivf(threads=4)
    j = max(range(ivf.nlist), key=lambda s: len(ivf.shards[s]))
    allow = ivf._shard_ids(j)
    assert len(allow) > 128
    q = np.ascontiguousarray(X[:6])
    for nprobe in (1, 3, 8):
        got = ivf.search(q, 15, allowlist=allow, nprobe=nprobe)
        assert_same(got, reference_search(ivf, q, 15, allowlist=allow, nprobe=nprobe))
        assert np.isin(got[1][got[0] > -np.inf], allow).all()
    ivf._pool.close()


def test_allow_cache_holds_sorted_arrays(tmp_path):
    col = make_collection(tmp_path, threads=4)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=8)
    real_rdb = col._rdb

    class Reversed:  # a query plan that returns ids out of rowid order
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *a):
            cur = self.db.execute(sql, *a)
            if sql.startswith("SELECT id FROM records WHERE"):
                return iter(cur.fetchall()[::-1])
            return cur

    col._rdb = lambda: Reversed(real_rdb())
    q = np.array([rowvec(1)], dtype=np.float32)
    hits = asyncio.run(col.search("vector", q, None, 5, "chunks", {"g": 1}, None))
    col._rdb = real_rdb
    assert hits[0]["id"] == "c1"
    (allow,) = col._allow_cache.values()
    assert len(allow) == 150 and bool((allow[1:] > allow[:-1]).all())
    asyncio.run(col.stop())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `3 failed, 17 passed`:
- `test_sorted_ids_normalizes` fails with `AttributeError: module 'raggio.store' has no attribute '_sorted_ids'`;
- `test_intersect_both_directions_match_intersect1d` fails the same way;
- `test_allow_cache_holds_sorted_arrays` fails with `AssertionError`, because the cached allowlist is in query-plan order.

Two tests pass here, and that is expected: `test_unsorted_duplicate_large_allowlist_matches_reference` and pin 3 are regression pins for the rewrite. Before the p3 revision, the sandbox showed they fail on the mutations "drop the `_sorted_ids` normalization in `search`" and "drop the empty-slice skip". The skip now lives in Task 1's `run` as `if not len(allow): return None`. After Step 3, apply that mutation once by hand, check that pin 3 fails with turbovec's `ValueError`, and revert it.

- [ ] **Step 3: Implement**

3a. Insert this helper directly before `class _ShardPool:`, with two blank lines on each side:

```python
def _sorted_ids(ids) -> np.ndarray:
    """uint64, strictly increasing: the form _IvfIndex._intersect binary-searches.
    Cached filter allowlists arrive sorted (an O(n) check, no copy); unsorted or
    duplicated input (_expand's sibling lists) pays one np.unique."""
    a = np.asarray(ids, dtype=np.uint64)
    if len(a) > 1 and not bool((a[1:] > a[:-1]).all()):
        a = np.unique(a)
    return a
```

3b. Replace the whole `_IvfIndex._intersect` method (19a8a2e: `"""allow ∩ shard j's ids (both uint64; shard side pre-sorted)."""` and its four-line body) with:

```python
    def _intersect(self, allow: np.ndarray, j: int) -> np.ndarray:
        """allow ∩ shard j's ids, sorted. Both sides are sorted and unique (allow via
        _sorted_ids), so binary-search the smaller side into the larger: a 255k-id
        filter against an 8k-row shard costs O(8k log 255k), not O(255k log 8k)
        (laptop, 16 probed shards of 2.55M rows: 94 ms -> 5 ms per query; DGX p3:
        29.8 -> 2.0 ms). search() calls this inside each pool task, so the pool's
        threads may run it concurrently: it only reads allow and the shard-id cache."""
        sids = self._shard_ids(j)
        if not len(sids) or not len(allow):
            return sids[:0]
        small, big = (allow, sids) if len(allow) <= len(sids) else (sids, allow)
        pos = np.minimum(np.searchsorted(big, small), len(big) - 1)
        return small[big[pos] == small]
```

3c. In `_IvfIndex.search`, normalize the allowlist once on the caller, before any task cuts a slice from it. The pool tasks then share one sorted, read-only array. The head of the method becomes the following; the rest of Task 1's body is unchanged:

```python
    def search(self, queries: np.ndarray, k: int, allowlist=None, nprobe: int | None = None):
        """Merged top-k over probed shards. Mirrors IdMapIndex.search: single-query
        results are trimmed exactly; a batch is rectangular, short rows padded with
        id 0 / -inf score (record ids start at 1, so padding never hydrates).
        Every (query, shard) search is one pool task (ADR 0005); the merge consumes
        them in the serial loop's order, so any pool size returns the serial result
        bit-for-bit, tie order included. A filtered task cuts its own allowlist slice
        on the pool thread (spec §2 row 6; p3: 3.7 ms against 6.4-7.0 ms cut up front
        on the caller), so the intersections run in parallel too."""
        if allowlist is not None:  # sorted, unique: the form _intersect binary-searches
            allowlist = _sorted_ids(allowlist)
        tiny = allowlist is not None and len(allowlist) <= 128
```

The `_sorted_ids` call runs before `len(allowlist)` is taken. Deduplication can therefore move a list with duplicates from just above 128 to 128 or fewer, and so onto the owners path. `reference_search` uses the same rule when it is given `np.unique(raw)`, as `test_unsorted_duplicate_large_allowlist_matches_reference` does.

3d. In `Collection._vector_ids`, sort the allowlist once per cache miss. Replace `allow = np.array(ids, dtype=np.uint64)` (the line after the `SELECT id FROM records WHERE {where}` query) so the block reads:

```python
                ids = [r[0] for r in self._rdb().execute(f"SELECT id FROM records WHERE {where}", params)]
                # sorted once per miss, whatever order the query plan returns: the IVF
                # path intersects by binary search (flat turbovec ignores the order)
                allow = np.sort(np.array(ids, dtype=np.uint64))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `20 passed`.

Run: `uv run pytest -q`. Expected: all green (on the sandbox base of 46: `66 passed`).

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py tests/test_ivf_fanout.py
git commit -m "Intersect IVF allowlists from the smaller side

Allowlists are sorted once per cache miss (and normalized in _IvfIndex.search),
so each probed shard binary-searches the smaller side into the larger, inside its
own pool task: a 255k-id filter over 16 shards of 2.55M rows drops from 94 ms to
5 ms per query on the laptop, and the intersect from 29.8 to 2.0 ms on the DGX (p3).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3: R2, a generation counter on the filter allowlist cache

**Files:**
- Modify: `src/raggio/store.py`:
  - the `_allow_cache` fields in `Collection.__init__`;
  - the new `Collection._invalidate_allow`;
  - every `self._allow_cache.clear()` call;
  - the cache-miss path of `_vector_ids`.
- Modify: `tests/test_ivf_fanout.py`: add `import time` and append a section.

**Interfaces:**
- Consumes: `Collection._allow_cache: dict[str, np.ndarray]`, and the write paths that clear it. At 19a8a2e those are `_upsert_rows`, `delete_document` and `patch_metadata`. Plans B and D may have moved the calls, but not removed them.
- Produces:
  - `Collection._allow_gen: int` (read lock-free; bumped only under `_allow_lock`);
  - `Collection._allow_lock: threading.Lock`;
  - `Collection._invalidate_allow(self) -> None`: every metadata or membership write calls it after its commit;
  - `_vector_ids` caches an allowlist only if no write landed since its SELECT began.

- [ ] **Step 1: Write the failing tests**

In `tests/test_ivf_fanout.py`, add `import time` on its own line directly after `import threading` in the import block. Then append:

```python
# ---- R2: filter allowlist cache generation ----


class SlowFilterScan:
    """Stands in for a big-collection filter scan: the SELECT snapshots, then stalls."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, *a):
        cur = self.db.execute(sql, *a)
        if sql.startswith("SELECT id FROM records WHERE"):
            rows = cur.fetchall()
            time.sleep(0.3)
            return iter(rows)
        return cur


def test_orphaned_filter_scan_cannot_cache_a_stale_allowlist(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 20)  # metadata g = i % 2: d3 starts in g=1
    real_rdb = col._rdb
    q = np.array([rowvec(3)], dtype=np.float32)

    async def go():
        col._rdb = lambda: SlowFilterScan(real_rdb())
        t = asyncio.create_task(col.search("vector", q, None, 50, "chunks", {"g": 1}, None))
        await asyncio.sleep(0.05)
        t.cancel()  # client timeout: the task drops its read lock, its thread runs on
        try:
            await t
        except asyncio.CancelledError:
            pass
        col._rdb = real_rdb
        assert await col.patch_metadata("d3", {"g": 0}, True) == 1
        await asyncio.sleep(0.5)  # the orphaned scan finishes and offers its allowlist
        return await col.search("vector", q, None, 50, "chunks", {"g": 1}, None)

    hits = asyncio.run(go())
    assert "d3" not in {h["doc_id"] for h in hits}
    assert {h["doc_id"] for h in hits} == {f"d{i}" for i in range(1, 20, 2)} - {"d3"}
    asyncio.run(col.stop())


def test_every_membership_write_bumps_the_allow_generation(tmp_path):
    col = make_collection(tmp_path)
    ingest(col, 10)
    gen = col._allow_gen
    asyncio.run(col.patch_metadata("d1", {"g": 5}, True))
    assert col._allow_gen == gen + 1
    asyncio.run(col.delete_document("d2"))
    assert col._allow_gen == gen + 2
    ingest(col, 3)  # upserts d0..d2
    assert col._allow_gen == gen + 3
    asyncio.run(col.stop())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `2 failed, 20 passed`:
- `test_orphaned_filter_scan_cannot_cache_a_stale_allowlist` fails with `AssertionError: assert 'd3' not in {...}`: the stale snapshot was cached and served after the write;
- `test_every_membership_write_bumps_the_allow_generation` fails with `AttributeError: 'Collection' object has no attribute '_allow_gen'`.

- [ ] **Step 3: Implement**

3a. In `Collection.__init__`, extend the `_allow_cache` field so it reads:

```python
        self._allow_cache: dict[str, np.ndarray] = {}  # (scope, filter) -> allowlist ids
        # bumped by every metadata/membership write; a filter scan that started before
        # a write may not cache its (older) snapshot. Read lock-free, bumped under the lock.
        self._allow_gen = 0
        self._allow_lock = threading.Lock()
```

3b. Add this method to `Collection`, directly before `def _upsert_rows(`:

```python
    def _invalidate_allow(self) -> None:
        """Every metadata/membership write calls this AFTER its commit."""
        with self._allow_lock:
            self._allow_gen += 1
            self._allow_cache.clear()
```

3c. Replace every `self._allow_cache.clear()` call in `Collection` with `self._invalidate_allow()`, keeping any trailing comment. At 19a8a2e there are three: in `_upsert_rows` after its commit, in `delete_document`, and in `patch_metadata`. Afterwards, `grep -n "_allow_cache.clear()" src/raggio/store.py` must print exactly one line: the one inside `_invalidate_allow`.

3d. In `Collection._vector_ids`, rewrite the cache-miss block, from `key = f"{scope}|...` down to (but not including) `if len(allow) == 0:`, as follows. It reads the generation before the SELECT, and caches under the lock only if the generation is unchanged. The old `try/except (StopIteration, RuntimeError)` FIFO eviction goes away, because eviction now happens under the lock:

```python
            key = f"{scope}|{json.dumps(filt, sort_keys=True)}"
            allow = self._allow_cache.get(key)
            if allow is None:
                gen = self._allow_gen  # read BEFORE the SELECT takes its snapshot
                where, params = _filter_sql(scope, filt)
                ids = [r[0] for r in self._rdb().execute(f"SELECT id FROM records WHERE {where}", params)]
                # sorted once per miss, whatever order the query plan returns: the IVF
                # path intersects by binary search (flat turbovec ignores the order)
                allow = np.sort(np.array(ids, dtype=np.uint64))
                with self._allow_lock:
                    # a write since `gen` may postdate our snapshot (an orphaned scan whose
                    # task was cancelled holds no read lock): use it once, never cache it
                    if gen == self._allow_gen:
                        if len(self._allow_cache) >= 8:  # ponytail: tiny FIFO; LRU if filters vary widely
                            self._allow_cache.pop(next(iter(self._allow_cache)))
                        self._allow_cache[key] = allow
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `22 passed`.

Run: `uv run pytest -q`. Expected: all green (on the sandbox base of 46: `68 passed`).

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py tests/test_ivf_fanout.py
git commit -m "Never cache a filter allowlist that predates a write

A filter scan orphaned by a cancelled request holds no read lock, so a write can
land between its SELECT and its cache store. _allow_gen (bumped by every
membership/metadata write via _invalidate_allow) lets it use that snapshot once
but never cache it (R2).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4: R2b, a per-shard generation counter on the IVF id cache

**Files:**
- Modify: `src/raggio/store.py`:
  - `_IvfIndex.__init__`;
  - the new `_IvfIndex._shard_written`;
  - `add_with_ids`, `remove` and `_shard_ids`.
- Modify: `tests/test_ivf_fanout.py` (append a section)

**Interfaces:**
- Consumes: `_IvfIndex._id_cache: list` (Task 1's `__init__`).
- Produces:
  - `_IvfIndex._id_gen: list[int]` (one per shard; read lock-free, bumped under `_id_lock`);
  - `_IvfIndex._id_lock: threading.Lock`;
  - `_IvfIndex._shard_written(self, j: int) -> None`: every write to shard j calls it after modifying the shard. Task 5 extends it.
- Concurrency note: since Task 1 cuts allowlist slices inside pool tasks, several pool threads (of one search or of concurrent searches) may call `_shard_ids(j)` for the same cold shard at once. Each computes the same array. The generation check below still keeps every one of them from caching a snapshot that predates a write.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ivf_fanout.py`:

```python
# ---- R2b: IVF shard id-cache generation ----


class GatedProbe:
    """Delegating shard proxy: a search snapshots the shard first, then waits."""

    def __init__(self, inner):
        self.inner = inner
        self.started, self.release = threading.Event(), threading.Event()

    def __len__(self):
        return len(self.inner)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def search(self, q, k, allowlist=None):
        res = self.inner.search(q, k=k, allowlist=allowlist)
        self.started.set()
        assert self.release.wait(5)
        return res


def test_orphaned_id_probe_cannot_cache_stale_shard_ids():
    ivf, _ = build_ivf()
    j, new_id = 2, 70_000
    gate = GatedProbe(ivf.shards[j])
    ivf.shards[j] = gate
    got = []
    t = threading.Thread(target=lambda: got.append(ivf._shard_ids(j)))
    t.start()
    assert gate.started.wait(5)  # the probe holds a pre-write snapshot of shard j
    ivf.add_with_ids(ivf.centroids[j : j + 1].copy(), np.array([new_id], dtype=np.uint64))
    assert gate.inner.contains(new_id)  # routed to shard j
    gate.release.set()
    t.join(5)
    assert new_id not in got[0]  # the racing caller used its own snapshot once...
    assert ivf._id_cache[j] is None  # ...but did not cache it over the write
    assert new_id in ivf._shard_ids(j)
    allow = store._sorted_ids([new_id, 1, 2])
    assert new_id in ivf._intersect(allow, j)


def test_remove_invalidates_shard_ids():
    ivf, _ = build_ivf()
    j = next(j for j in range(ivf.nlist) if len(ivf.shards[j]))
    victim = int(ivf._shard_ids(j)[0])
    gen = ivf._id_gen[j]
    ivf.remove(victim)
    assert ivf._id_gen[j] == gen + 1 and victim not in ivf._shard_ids(j)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `2 failed, 22 passed`:
- `test_orphaned_id_probe_cannot_cache_stale_shard_ids` fails with `AssertionError`: the probe's pre-write id array was cached;
- `test_remove_invalidates_shard_ids` fails with `AttributeError: '_IvfIndex' object has no attribute '_id_gen'`.

- [ ] **Step 3: Implement**

3a. Replace `_IvfIndex.__init__` (Task 1's version) with:

```python
    def __init__(
        self, centroids: np.ndarray, shards: list, nprobe: int, pool: _ShardPool | None = None
    ) -> None:
        self.centroids = centroids  # (nlist, dim) f32, unit norm, static after train
        self.shards = shards
        self.nprobe = nprobe
        self._pool = pool or _ShardPool(1)  # the owning Collection's; default: serial
        # per-shard id arrays for allowlist intersection (turbovec rejects allowlist
        # ids an index doesn't hold): built lazily via a full-k probe, dropped for any
        # shard a write touches. ~8 bytes/row when filtered queries occur, else nothing.
        self._id_cache: list = [None] * len(shards)
        # per-shard write generation: an id probe that started before a write to its
        # shard may not cache its (older) snapshot. Read lock-free, bumped under the lock.
        self._id_gen = [0] * len(shards)
        self._id_lock = threading.Lock()
```

3b. Replace the block from `def add_with_ids(` through the end of `_shard_ids` (just before `def _intersect(`) with the following. The new `_shard_written` comes first:

```python
    def _shard_written(self, j: int) -> None:
        """Every write to shard j calls this AFTER modifying the shard."""
        with self._id_lock:
            self._id_gen[j] += 1
            self._id_cache[j] = None

    def add_with_ids(self, mat: np.ndarray, ids: np.ndarray) -> None:
        asg = self._assign(mat, self.centroids)
        for j in np.unique(asg):
            m = asg == j
            self.shards[j].add_with_ids(np.ascontiguousarray(mat[m]), ids[m])
            self._shard_written(int(j))

    def remove(self, rid: int) -> None:
        # ponytail: O(nlist) contains scan (~us each) beats maintaining an id->shard map
        for j, sh in enumerate(self.shards):
            if sh.contains(rid):
                sh.remove(rid)
                self._shard_written(j)
                return

    def _shard_ids(self, j: int) -> np.ndarray:
        ids = self._id_cache[j]
        if ids is None:
            gen = self._id_gen[j]  # read BEFORE the probe snapshots the shard
            sh = self.shards[j]
            if len(sh):
                probe = np.zeros((1, self.centroids.shape[1]), dtype=np.float32)
                probe[0, 0] = 1.0
                ids = np.sort(sh.search(probe, k=len(sh))[1][0])  # sorted: intersections
                # use searchsorted instead of a per-call re-sort
            else:
                ids = np.empty(0, np.uint64)
            with self._id_lock:
                # a write since `gen` may postdate our snapshot (an orphaned search whose
                # task was cancelled holds no read lock): use it once, never cache it
                if gen == self._id_gen[j]:
                    self._id_cache[j] = ids
        return ids
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `24 passed`.

Run: `uv run pytest -q`. Expected: all green (on the sandbox base of 46: `70 passed`).

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py tests/test_ivf_fanout.py
git commit -m "Never cache IVF shard ids that predate a write

_shard_ids reads the shard's generation before its full-k probe and caches the
result only if no add/remove bumped it meanwhile, so an orphaned search cannot
pin a stale id array that later intersections would trust (R2b).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5: Dirty-shard sync

**Files:**
- Modify: `src/raggio/store.py`:
  - `_IvfIndex.__init__`;
  - the end of `_IvfIndex.train`;
  - `_IvfIndex.sync`, plus the new `dirty_shards` property;
  - `_IvfIndex._shard_written`.
- Modify: `tests/test_ivf_fanout.py` (append a section)

**Interfaces:**
- Consumes:
  - `_IvfIndex._shard_written(j)` and `_id_gen` / `_id_lock` (Task 4);
  - `IdMapIndex.sync(path: str) -> None`. In turbovec 1.0.0, syncing a clean shard still writes a header and fsyncs, so skipping clean shards saves real I/O.
  - The existing `_IvfIndex.sync(directory)` callers. At 19a8a2e those are the per-job sync in `_process_job`, the attach swap, and `stop()`. They ignore the return value and stay unchanged.
- Produces:
  - `_IvfIndex._dirty: set[int]`;
  - `_IvfIndex.dirty_shards -> frozenset[int]` (property);
  - `_IvfIndex.sync(self, directory) -> int`, which returns the number of shard files written.
  - Plan D2's batched sync consumes `dirty_shards` and the `int` return (spec §4.1).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ivf_fanout.py`. The section includes Review Focus pin 4 (`test_reupsert_that_moves_a_row_syncs_both_shards`):

```python
# ---- dirty-shard sync ----


class SyncSpy:
    """Delegating shard proxy that logs (or fails) sync calls."""

    def __init__(self, inner, log, j, fail=False):
        self.inner, self.log, self.j, self.fail = inner, log, j, fail

    def __len__(self):
        return len(self.inner)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def sync(self, path):
        if self.fail:
            raise OSError("disk full")
        self.log.append(self.j)
        return self.inner.sync(path)


def spy_syncs(ivf, fail=()):
    log = []
    ivf.shards = [
        SyncSpy(getattr(sh, "inner", sh), log, j, j in fail) for j, sh in enumerate(ivf.shards)
    ]
    return log


def test_one_row_job_syncs_one_shard(tmp_path):
    col = make_collection(tmp_path, threads=1)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=8)
    assert col.index.dirty_shards == frozenset()  # the attach swap synced every shard
    log = spy_syncs(col.index)
    doc = {"doc_id": "n1", "chunks": [{"id": "n1", "text": "new", "vector": rowvec(5000)}]}
    asyncio.run(col._process_job({"documents": [doc]}))
    assert len(log) == 1 and col.index.dirty_shards == frozenset()
    log.clear()
    doc = {"doc_id": "d5", "chunks": [{"id": "c5", "text": "again", "vector": rowvec(5)}]}
    asyncio.run(col._process_job({"documents": [doc]}))  # re-upsert: remove + add, same shard
    assert len(log) == 1
    asyncio.run(col.stop())


def test_reload_then_stop_writes_no_shards(tmp_path):
    col = make_collection(tmp_path, threads=1)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=8)
    asyncio.run(col.stop())
    col2 = Collection(col.cfg, Path(tmp_path), lambda: None, ivf_search_threads=1)
    assert isinstance(col2.index, _IvfIndex) and col2.index.dirty_shards == frozenset()
    log = spy_syncs(col2.index)
    asyncio.run(col2.stop())
    assert log == []


def test_first_sync_to_a_fresh_dir_writes_every_shard(tmp_path):
    ivf, _ = build_ivf()
    assert ivf.dirty_shards == frozenset(range(8))  # trained: nothing on disk yet
    log = spy_syncs(ivf)
    assert ivf.sync(tmp_path / "a") == 8
    assert sorted(log) == list(range(8)) and ivf.dirty_shards == frozenset()
    log.clear()
    assert ivf.sync(tmp_path / "a") == 0 and log == []  # clean and every file present
    assert ivf.sync(tmp_path / "b") == 8  # a new directory lacks every shard file
    loaded = _IvfIndex.load(tmp_path / "b", DIM, 4, 3)
    assert loaded.dirty_shards == frozenset() and len(loaded) == len(ivf)


def test_reupsert_that_moves_a_row_syncs_both_shards(tmp_path):
    # RF4: a re-upsert whose new vector routes to another shard removes the old id from
    # shard A and adds the new id to shard B; both files must be rewritten, or the files
    # hold the old id too (the reload's ghost reconcile would hide it, so read the files)
    col = make_collection(tmp_path, threads=1)
    ingest(col, 300)
    attach(col, nlist=8, nprobe=8)
    ivf = col.index
    (old_id,) = col.db.execute("SELECT id FROM records WHERE external_id='c5'").fetchone()
    a = next(j for j in range(ivf.nlist) if ivf.shards[j].contains(old_id))
    b = (a + 1) % ivf.nlist
    log = spy_syncs(ivf)
    doc = {"doc_id": "d5", "chunks": [{"id": "c5", "text": "moved", "vector": ivf.centroids[b].tolist()}]}
    asyncio.run(col._process_job({"documents": [doc]}))
    (new_id,) = col.db.execute("SELECT id FROM records WHERE external_id='c5'").fetchone()
    assert ivf.shards[b].contains(new_id) and sorted(log) == sorted({a, b})
    asyncio.run(col.stop())
    disk = _IvfIndex.load(col.ivf_dir, DIM, 4, 8)
    assert [j for j in range(8) if disk.shards[j].contains(old_id)] == []
    assert [j for j in range(8) if disk.shards[j].contains(new_id)] == [b]
    assert len(disk) == 300


def test_failed_shard_sync_stays_dirty(tmp_path):
    ivf, _ = build_ivf()
    ivf.sync(tmp_path)
    ivf.add_with_ids(ivf.centroids[[1, 4]], np.array([80_001, 80_002], dtype=np.uint64))
    assert ivf.dirty_shards == frozenset({1, 4})
    spy_syncs(ivf, fail={4})
    with pytest.raises(OSError):
        ivf.sync(tmp_path)
    assert ivf.dirty_shards == frozenset({4})  # shard 1 landed; shard 4 retries next time
    log = spy_syncs(ivf)
    assert ivf.sync(tmp_path) == 1 and log == [4] and ivf.dirty_shards == frozenset()
    reloaded = _IvfIndex.load(tmp_path, DIM, 4, 3)
    assert reloaded.shards[1].contains(80_001) and reloaded.shards[4].contains(80_002)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `5 failed, 24 passed`:
- `test_one_row_job_syncs_one_shard`, `test_reload_then_stop_writes_no_shards`, `test_first_sync_to_a_fresh_dir_writes_every_shard` and `test_failed_shard_sync_stays_dirty` fail with `AttributeError: '_IvfIndex' object has no attribute 'dirty_shards'`;
- `test_reupsert_that_moves_a_row_syncs_both_shards` fails with `AssertionError`: the job synced all 8 shards, not just `[a, b]`.

- [ ] **Step 3: Implement**

3a. Replace `_IvfIndex.__init__` (Task 4's version) with:

```python
    def __init__(
        self, centroids: np.ndarray, shards: list, nprobe: int, pool: _ShardPool | None = None
    ) -> None:
        self.centroids = centroids  # (nlist, dim) f32, unit norm, static after train
        self.shards = shards
        self.nprobe = nprobe
        self._pool = pool or _ShardPool(1)  # the owning Collection's; default: serial
        # per-shard id arrays for allowlist intersection (turbovec rejects allowlist
        # ids an index doesn't hold): built lazily via a full-k probe, dropped for any
        # shard a write touches. ~8 bytes/row when filtered queries occur, else nothing.
        self._id_cache: list = [None] * len(shards)
        # per-shard write generation: an id probe that started before a write to its
        # shard may not cache its (older) snapshot. Read lock-free, bumped under the lock.
        self._id_gen = [0] * len(shards)
        self._id_lock = threading.Lock()
        # shards whose file lags memory: sync() writes only these (plus any shard file
        # the target dir lacks). train() marks every shard, load() none.
        self._dirty: set[int] = set()
```

3b. In `_IvfIndex.train`, replace its last line `return cls(np.ascontiguousarray(C), shards, nprobe, pool)` (Task 1) with:

```python
        ivf = cls(np.ascontiguousarray(C), shards, nprobe, pool)
        ivf._dirty.update(range(nlist))  # nothing on disk yet
        return ivf
```

3c. Replace the whole `_IvfIndex.sync` method (from `def sync(self, directory) -> None:` to just before `# ---- the IdMapIndex surface Collection uses ----`) with:

```python
    @property
    def dirty_shards(self) -> frozenset[int]:
        """Shards with writes not yet synced to disk (empty: sync() is a no-op)."""
        with self._id_lock:
            return frozenset(self._dirty)

    def sync(self, directory) -> int:
        """Persist to `directory`: centroids once, then every dirty shard and every
        shard whose file the directory lacks (a fresh attach tmp dir gets them all).
        A shard's flag clears only after its own sync lands and no write raced it, so
        a failure part-way leaves the rest dirty for the next call. Returns the number
        of shard files written."""
        d = Path(directory)
        d.mkdir(exist_ok=True)
        cpath = d / "centroids.npy"
        if not cpath.exists():  # static after train; shard syncs are incremental
            np.save(cpath, self.centroids)
        written = 0
        for j, sh in enumerate(self.shards):
            path = d / f"shard-{j:04d}.tvim"
            if j not in self._dirty and path.exists():
                continue
            gen = self._id_gen[j]
            sh.sync(str(path))
            written += 1
            with self._id_lock:
                if gen == self._id_gen[j]:
                    self._dirty.discard(j)
        return written
```

3d. Replace `_IvfIndex._shard_written` (Task 4) with:

```python
    def _shard_written(self, j: int) -> None:
        """Every write to shard j calls this AFTER modifying the shard."""
        with self._id_lock:
            self._id_gen[j] += 1
            self._id_cache[j] = None
            self._dirty.add(j)
```

`load()` builds its index through `cls(...)` and never marks anything, so a reloaded index starts clean. `sync()` still writes any shard file that is missing from the target directory, so the attach swap's fresh tmp directory gets every shard.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `29 passed`.

Run: `uv run pytest -q`. Expected: all green (on the sandbox base of 46: `75 passed`).

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py tests/test_ivf_fanout.py
git commit -m "Sync only dirty IVF shards

Every write to a shard marks it dirty; sync() writes only dirty shards plus any
shard file the target directory lacks, returns the count, and clears a flag only
if no write raced that shard's sync. A one-row job rewrites one shard file
instead of all nlist. dirty_shards exposes the set for Plan D2.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 6: The IVF stage and fan-out strategy probe

**Files:**
- Create: `bench/ivf_fanout_probe.py`
- Modify: `tests/test_ivf_fanout.py` (append a section)

**Interfaces:**
- Consumes these, by name:
  - from `raggio.store`: `IVF_DEFAULT_NPROBE`, `Collection`, `CollectionConfig`, `_filter_sql`, `_IvfIndex`, `_normalize`, `_rows_by_id`, `_ShardPool`;
  - `raggio.app.SearchIn`;
  - `raggio.config.default_ivf_search_threads`;
  - on a `Collection` shell built with `Collection.__new__`: `cfg`, `dir`, `index`, `_closed`, `_rdb` (the per-thread read connection the probe installs), `_rescore_k`, `_rescore_rows` and `_hydrate`;
  - on `_IvfIndex`: `centroids`, `shards`, `nlist`, `nprobe`, `search`, `_pool` and `_shard_ids`.
  - The probe copies the unpack loop of `Collection._search_rescored` and the allowlist SELECT of `_vector_ids` verbatim. If an earlier plan renamed any of these names, update the probe to match: the two smoke tests fail if one is missing.
- Produces:
  - `bench/ivf_fanout_probe.py` with `main(argv: list[str])`:
    - `stages` returns a `list[dict]`, one row per (path, nq, threads);
    - `fanout` returns a `dict` with the keys `hist`, `cells`, `best_threads` and `guard` (`"OK"` or `"REVISIT"`);
    - `exact` returns a `dict` with the keys `threads`, `seed`, `queries`, `batches`, `unfiltered_same`, `filter`, `filter_ids`, `filtered_same`, `tiny_ids`, `tiny_same`, `max_list`, `cliff` and `verdict` (`"PASS"` or `"FAIL"`).
  - `POOLED_CLIFF = 32_768` and `largest_list(ivf) -> int`. Every mode prints, right after loading, the line `LISTS largest N rows, P% of the 32,768-row pooled-path cliff`, with ` -- AT OR ABOVE THE CLIFF` appended when N ≥ 32,768.
  - The printed `fanout` output ends with the line `serial r1/r2 ... ms; chosen pool size T (default here D); GUARD grouped vs plain at T: OK|REVISIT`. The printed `exact` output ends with the line `EXACT seed S T=T vs IVF_SEARCH_THREADS=1: unfiltered u/b batches, filtered f/n, tiny t/n (<filter note>; ids, float32 score bits and tie order): PASS|FAIL`. Task 9's `planF-summary.py` and `planF-verify.py` parse all three lines.
  - The grouped arm (`grouped_search`) is a regression guard for spec D3, which p3 settled. Nothing reads `guard` to ship anything.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ivf_fanout.py`:

```python
# ---- bench/ivf_fanout_probe.py smoke (the DGX stage/fan-out probe) ----


def load_probe():
    import importlib.util

    spec = importlib.util.spec_from_file_location("ivf_fanout_probe", "bench/ivf_fanout_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def probe_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def go():
        mgr = CollectionManager(Settings(), embedder_factory=lambda cfg: None)
        await mgr.create_collection("p", DIM, 4, None, None, None)
        col = await mgr.touch("p")
        docs = [
            {"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "text": f"chunk {i}",
                                            "vector": rowvec(i), "metadata": {"year": str(2000 + i % 3)}}]}
            for i in range(400)
        ]
        await col._process_job({"documents": docs})
        await col._process_job({"op": "attach_index", "nlist": 8, "nprobe": 3})
        await mgr.shutdown()

    asyncio.run(go())


def test_probe_stages_smoke(tmp_path, monkeypatch):
    probe_collection(tmp_path, monkeypatch)
    rows = load_probe().main([
        "stages", "--data", str(tmp_path), "--collection", "p", "--nq", "1,8",
        "--threads", "1,4", "--batches", "3", "--filter-key", "year",
    ])
    assert [(r["path"], r["nq"], r["threads"]) for r in rows] == [
        ("vector", 1, 1), ("vector", 1, 4), ("vector", 8, 1), ("vector", 8, 4),
        ("filtered", 1, 1), ("filtered", 1, 4),
    ]
    for r in rows:
        assert r["calls"] == r["nq"] * 3 and r["total"] >= r["index"] > 0


def test_probe_fanout_smoke(tmp_path, monkeypatch):
    probe_collection(tmp_path, monkeypatch)
    out = load_probe().main([
        "fanout", "--data", str(tmp_path), "--collection", "p", "--threads", "1,4",
        "--batches", "4",
    ])
    assert sum(size * n for size, n in enumerate(out["hist"], 1)) == 8 * 3 * 4  # every call
    assert all(c["plain_exact"] and c["grouped_same"] == 4 for c in out["cells"])
    assert out["best_threads"] in (1, 4) and out["guard"] in ("OK", "REVISIT")


def test_probe_exact_smoke(tmp_path, monkeypatch):
    # spec §7 F items 1 and 7: bit identity against IVF_SEARCH_THREADS=1 on bench.py's
    # own query selection (seed 42), unfiltered, filtered (>128 ids) and tiny (<=128)
    probe_collection(tmp_path, monkeypatch)
    np.save(tmp_path / "v.npy", unit(50, 3))
    out = load_probe().main([
        "exact", "--data", str(tmp_path), "--collection", "p", "--threads", "4",
        "--vectors", str(tmp_path / "v.npy"), "--limit", "50", "--queries", "16",
        "--seed", "42", "--filter-key", "year",
    ])
    assert out["threads"] == 4 and out["seed"] == 42 and out["queries"] == 16
    assert out["batches"] == 2 and out["unfiltered_same"] == 2
    assert out["filtered_same"] == 16 and out["tiny_same"] == 16 and out["verdict"] == "PASS"
    assert out["filter_ids"] > 128 >= out["tiny_ids"] > 0
    assert 0 < out["max_list"] < out["cliff"] == 32_768
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `3 failed, 29 passed`. `test_probe_stages_smoke`, `test_probe_fanout_smoke` and `test_probe_exact_smoke` all fail with `FileNotFoundError` for `bench/ivf_fanout_probe.py`.

- [ ] **Step 3: Implement**

Create `bench/ivf_fanout_probe.py` with exactly this content:

```python
"""IVF fan-out probe for ADR 0005: read-only, in-process, against a live collection.

  stages   Where one search micro-batch spends its time. 3a's DGX cycle model puts
           ~31 ms of non-kernel work in every 8-query IVF cycle; 3b blames the serial
           drain of nq x nprobe shard calls. This times each stage of the real path:
             parse    json.loads + SearchIn + _normalize, per request (summed)
             index    _IvfIndex.search wall: route + shard calls + merge
               route    queries @ centroids.T + argpartition (also inside index)
               kernel   sum of the per-(query, shard) turbovec call times
             unpack   _search_rescored's -inf filter loop
             rescore  _rescore_rows: fp16 blob fetch + exact cosine
             hydrate  _hydrate per request (summed; on the event loop in the server)
             encode   jsonable_encoder + json.dumps per request (summed)
           --filter-key adds the filtered path (nq=1, the key's most common value):
           its one-off allowlist SELECT, then per query the same stages.
           A bench cycle (1000 * 8 / QPS-concurrent ms) minus the nq=8 `total` is the
           HTTP/asyncio residual this probe cannot see.
  fanout   serial vs plain (shipped) vs grouped (audience batching, NOT shipped) over
           pool sizes on nq=8 micro-batches, ABBA-timed so every cell has a run1/run2
           noise band, plus the real group-size histogram. Spec D3 is settled by p3
           (plain q1 fan-out), so grouped is a regression guard only: GUARD prints
           REVISIT if grouped's slower run beats plain's faster run at the chosen pool
           size, which reopens D3 with the user. Nothing ships on it.
  exact    Spec §7 F item 1: ids and float32 score bits identical to
           IVF_SEARCH_THREADS=1 (the serial path) at the largest --threads, compared
           position by position so tie order counts. Unfiltered in nq=8 batches;
           filtered at nq=1 on --filter-key's most common value (the in-task cut,
           >128 ids) and on a 100-id slice of it (the tiny owners path).

Every mode first prints LISTS: the largest IVF list against turbovec's 32,768-row
pooled-path cliff (spec §7 F item 7). Never set RAYON_NUM_THREADS=1 (spec §4.2).

Queries: stages and fanout use stored fp16 vectors of random rows, --seed 7 (tunables
are chosen on seed 7). exact with --vectors uses bench.py's own query selection
(load_corpus: default_rng(seed).choice(limit, queries), sorted, rounded to 5 decimals),
so `--seed 42` is the bench's set; without --vectors it falls back to stored rows.

DGX (bench-tv container removed, so nothing else holds the volume; `podman unshare`
for the volume's file ownership; OPENBLAS_NUM_THREADS=1 as in both A/B arms, D16):
  VOL=$(podman volume inspect bench-tv --format '{{.Mountpoint}}')
  podman unshare env OPENBLAS_NUM_THREADS=1 .venv/bin/python -u bench/ivf_fanout_probe.py \\
    stages --data "$VOL" --threads 1,12 --filter-key year
  podman unshare env OPENBLAS_NUM_THREADS=1 .venv/bin/python -u bench/ivf_fanout_probe.py \\
    fanout --data "$VOL" --threads 1,2,4,8,12,16,20
  podman unshare env OPENBLAS_NUM_THREADS=1 .venv/bin/python -u bench/ivf_fanout_probe.py \\
    exact --data "$VOL" --threads 12 --vectors bench/corpus/embed-vecs.npy \\
    --limit 2549619 --queries 500 --seed 42 --filter-key year
"""
import argparse
import json
import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path

import numpy as np
from fastapi.encoders import jsonable_encoder

from raggio.app import SearchIn
from raggio.config import default_ivf_search_threads
from raggio.store import (
    IVF_DEFAULT_NPROBE,
    Collection,
    CollectionConfig,
    _filter_sql,
    _IvfIndex,
    _normalize,
    _rows_by_id,
    _ShardPool,
)

STAGES = ("parse", "route", "index", "kernel", "unpack", "rescore", "hydrate", "encode", "total")
# turbovec 1.0.0: nq=1 on a shard of this many rows (1,024 blocks) or more leaves the
# inline path for the shared rayon pool, and fan-out tops out near 2.45x (p3)
POOLED_CLIFF = 32_768


def largest_list(ivf: _IvfIndex) -> int:
    return max((len(sh) for sh in ivf.shards), default=0)


def open_collection(data: Path, name: str) -> Collection:
    """A read-only Collection shell holding exactly what the search path touches."""
    cat = sqlite3.connect(f"file:{data / 'catalog.db'}?mode=ro", uri=True)
    row = cat.execute(
        "SELECT name, dim, bit_width, model, base_url, key_hash, tokenizer, index_config"
        " FROM collections WHERE name=?",
        (name,),
    ).fetchone()
    cat.close()
    if row is None:
        sys.exit(f"no collection {name!r} in {data / 'catalog.db'}")
    cfg = CollectionConfig(*row[:6], row[6] or "unicode61", json.loads(row[7]) if row[7] else None)
    if not cfg.index_config:
        sys.exit(f"collection {name!r} has no IVF index attached")
    col = Collection.__new__(Collection)
    col.cfg, col.dir, col._closed = cfg, data / "collections" / name, False
    meta, local = col.dir / "meta.db", threading.local()

    def rdb() -> sqlite3.Connection:  # per thread, mode=ro: never writes the live volume
        db = getattr(local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{meta}?mode=ro", uri=True, check_same_thread=False)
            local.db = db
        return db

    col._rdb = rdb
    t = time.perf_counter()
    col.index = _IvfIndex.load(
        col.dir / "ivf", cfg.dim, cfg.bit_width, cfg.index_config.get("nprobe", IVF_DEFAULT_NPROBE)
    )
    col.index.prepare()
    print(
        f"loaded {name}: {len(col.index)} rows, nlist {col.index.nlist}, nprobe "
        f"{col.index.nprobe}, dim {cfg.dim}, {cfg.bit_width}-bit, {time.perf_counter() - t:.1f}s",
        flush=True,
    )
    big = largest_list(col.index)
    print(
        f"LISTS largest {big} rows, {100 * big / POOLED_CLIFF:.0f}% of the "
        f"{POOLED_CLIFF:,}-row pooled-path cliff"
        + (" -- AT OR ABOVE THE CLIFF" if big >= POOLED_CLIFF else ""),
        flush=True,
    )
    return col


def sample_queries(col: Collection, n: int, seed: int) -> np.ndarray:
    """n stored vectors of random rows (fp16 blobs, renormalized), in id order."""
    db, dim = col._rdb(), col.cfg.dim
    hi = db.execute("SELECT MAX(id) FROM vecs").fetchone()[0] or 0
    ids = np.random.default_rng(seed).permutation(np.arange(1, hi + 1))
    out: list[np.ndarray] = []
    for s in range(0, len(ids), 4 * n):
        chunk = [int(i) for i in ids[s : s + 4 * n]]
        for _, blob in _rows_by_id(db, "SELECT id, vec FROM vecs WHERE id IN ({})", chunk):
            if blob is not None and len(blob) == dim * 2:
                out.append(np.frombuffer(blob, dtype=np.float16).astype(np.float32))
        if len(out) >= n:
            break
    if len(out) < n:
        sys.exit(f"only {len(out)} stored vectors, need {n}")
    return _normalize(np.stack(out[:n]))


def bench_queries(path: Path, limit: int, n: int, seed: int) -> np.ndarray:
    """bench.py's own query set (load_corpus): n corpus rows drawn with
    default_rng(seed).choice(limit, n, replace=False), sorted, rounded to 5 decimals
    as the bench sends them, then normalized as the server does."""
    vecs = np.load(path, mmap_mode="r")
    limit = min(limit, vecs.shape[0])
    rows = np.sort(np.random.default_rng(seed).choice(limit, size=n, replace=False))
    return _normalize(np.round(np.asarray(vecs[rows], dtype=np.float32), 5))


def filter_allowlist(col: Collection, key: str):
    """The key's most common stored value (bench.py filters on the mode of `year`) and
    its allowlist, selected and sorted as _vector_ids does; plus the SELECT+sort ms."""
    where, params = _filter_sql("chunks", None)
    val = col._rdb().execute(
        f"SELECT json_extract(metadata, ?) v FROM records WHERE {where} AND v IS NOT NULL"
        " GROUP BY v ORDER BY COUNT(*) DESC LIMIT 1",
        ["$." + key, *params],
    ).fetchone()[0]
    where, params = _filter_sql("chunks", {key: val})
    t = time.perf_counter()  # the allow-cache miss, paid once per filter per write
    ids = [r[0] for r in col._rdb().execute(f"SELECT id FROM records WHERE {where}", params)]
    allow = np.sort(np.array(ids, dtype=np.uint64))
    return val, allow, (time.perf_counter() - t) * 1e3


class Timed:
    """Delegating shard proxy (IdMapIndex is a frozen pyclass) logging call seconds."""

    def __init__(self, inner, log: list) -> None:
        self.inner, self.log = inner, log

    def __len__(self) -> int:
        return len(self.inner)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def search(self, q, k, allowlist=None):
        t = time.perf_counter()
        try:
            return self.inner.search(q, k=k, allowlist=allowlist)
        finally:
            self.log.append(time.perf_counter() - t)


def one_batch(col: Collection, qs: np.ndarray, k: int, allow, log: list) -> dict:
    """One micro-batch through the server's search path; seconds per stage."""
    ivf = col.index
    bodies = [json.dumps({"query": {"vector": np.round(q, 5).tolist()}, "k": k}) for q in qs]
    t0 = time.perf_counter()
    rows = [SearchIn.model_validate(json.loads(b)).query.vector for b in bodies]
    mat = np.vstack([_normalize(np.array([v], dtype=np.float32)) for v in rows])
    t1 = time.perf_counter()
    npb = min(ivf.nprobe, ivf.nlist)
    sims = mat @ ivf.centroids.T
    [np.argpartition(-sims[qi], npb - 1)[:npb] for qi in range(len(mat))]
    t2 = time.perf_counter()
    log.clear()
    kw = {"allowlist": allow} if allow is not None else {}
    t3 = time.perf_counter()
    scores, ids = ivf.search(mat, k=col._rescore_k(k), nprobe=None, **kw)
    t4 = time.perf_counter()
    unpacked = []  # Collection._search_rescored's unpack, verbatim
    for row in range(len(mat)):
        r_ids, r_scores = [], []
        for i, s in zip(ids[row], scores[row]):
            if s > -np.inf:
                r_ids.append(int(i))
                r_scores.append(float(s))
        unpacked.append((r_ids, r_scores, k))
    t5 = time.perf_counter()
    res = col._rescore_rows(mat, unpacked)
    t6 = time.perf_counter()
    hits = [col._hydrate(i, s) for i, s in res]
    t7 = time.perf_counter()
    for h in hits:
        json.dumps(jsonable_encoder({"hits": h}))
    t8 = time.perf_counter()
    return {
        "parse": t1 - t0, "route": t2 - t1, "index": t4 - t3, "kernel": sum(log),
        "calls": len(log), "unpack": t5 - t4, "rescore": t6 - t5, "hydrate": t7 - t6,
        "encode": t8 - t7, "total": (t1 - t0) + (t8 - t3),
    }


def cell(col, qs, nq, batches, k, allow, log) -> dict:
    one_batch(col, qs[:nq], k, allow, log)  # warm: page in shards, start the pool
    runs = [one_batch(col, qs[b * nq : (b + 1) * nq], k, allow, log) for b in range(batches)]
    out = {s: statistics.median(r[s] for r in runs) * 1e3 for s in STAGES}
    out["calls"] = statistics.median(r["calls"] for r in runs)
    return out


def run_stages(col: Collection, args) -> list[dict]:
    ivf, log = col.index, []
    ivf.shards = [Timed(sh, log) for sh in ivf.shards]
    nqs = [int(x) for x in args.nq.split(",")]
    qs = sample_queries(col, max(nqs) * args.batches, args.seed)
    paths = [("vector", None, nqs)]
    if args.filter_key:
        val, allow, sql_ms = filter_allowlist(col, args.filter_key)
        t = time.perf_counter()  # _intersect's shard-id cache, refilled per write to a shard
        for j in range(ivf.nlist):
            ivf._shard_ids(j)
        print(
            f"filter {args.filter_key}={val!r}: {len(allow)} ids; allowlist SELECT+sort "
            f"{sql_ms:.0f} ms, shard-id cache fill {(time.perf_counter() - t) * 1e3:.0f} ms "
            "(each once per cache miss, not in the rows below)",
            flush=True,
        )
        paths.append(("filtered", allow, [1]))
    head = f"{'path':9s} {'nq':>3s} {'T':>3s} " + " ".join(f"{s:>8s}" for s in STAGES)
    print(head + f" {'calls':>6s} {'other':>8s}   (ms, median of {args.batches})", flush=True)
    rows = []
    for label, allow, path_nqs in paths:
        for nq in path_nqs:
            for threads in [int(x) for x in args.threads.split(",")]:
                ivf._pool.close()
                ivf._pool = _ShardPool(threads, "probe")
                r = cell(col, qs, nq, args.batches, 10, allow, log)
                r.update(path=label, nq=nq, threads=threads, other=r["total"] - r["index"])
                rows.append(r)
                print(
                    f"{label:9s} {nq:3d} {threads:3d} "
                    + " ".join(f"{r[s]:8.2f}" for s in STAGES)
                    + f" {r['calls']:6.0f} {r['other']:8.2f}",
                    flush=True,
                )
    ivf._pool.close()
    return rows


def grouped_search(ivf: _IvfIndex, queries: np.ndarray, k: int, pool: _ShardPool):
    """Audience batching (NOT shipped; a regression guard for the settled spec D3): one
    call per distinct probed shard carrying every query that probes it, on the same
    pool, merged like _IvfIndex.search."""
    npb = min(ivf.nprobe, ivf.nlist)
    sims = queries @ ivf.centroids.T
    groups: dict[int, list[int]] = {}
    for qi in range(len(queries)):
        for j in np.argpartition(-sims[qi], npb - 1)[:npb]:
            if len(ivf.shards[j]):
                groups.setdefault(int(j), []).append(qi)

    def run(item):
        j, qis = item
        sh = ivf.shards[j]
        s, i = sh.search(np.ascontiguousarray(queries[qis]), k=min(k, len(sh)))
        return qis, s, i

    parts: list = [([], []) for _ in range(len(queries))]
    for qis, s, i in pool.map(run, list(groups.items())):
        for r, qi in enumerate(qis):
            parts[qi][0].append(s[r])
            parts[qi][1].append(i[r])
    width = min(k, max(sum(len(p) for p in ps) for ps, _ in parts))
    out_s = np.full((len(queries), width), -np.inf, np.float32)
    out_i = np.zeros((len(queries), width), np.uint64)
    for qi, (ps, pi) in enumerate(parts):
        s, i = np.concatenate(ps), np.concatenate(pi)
        top = np.argsort(-s)[:k]
        out_s[qi, : len(top)], out_i[qi, : len(top)] = s[top], i[top]
    return out_s, out_i, [len(q) for q in groups.values()]


def same_topk(a, b) -> bool:
    """Same (score, id) multiset per query row: the tie ORDER may differ."""
    return all(
        np.array_equal(np.sort(sa), np.sort(sb)) and np.array_equal(np.sort(ia), np.sort(ib))
        for sa, ia, sb, ib in zip(a[0], a[1], b[0], b[1])
    )


def median_ms(fn, batches) -> float:
    fn(batches[0])  # warm
    ts = []
    for b in batches:
        t = time.perf_counter()
        fn(b)
        ts.append(time.perf_counter() - t)
    return statistics.median(ts) * 1e3


def run_fanout(col: Collection, args) -> dict:
    ivf, nq = col.index, int(args.nq)
    k = col._rescore_k(10)  # the server's over-fetch depth for k=10
    qs = sample_queries(col, nq * args.batches, args.seed)
    batches = [np.ascontiguousarray(qs[b * nq : (b + 1) * nq]) for b in range(args.batches)]
    serial_pool = _ShardPool(1)
    sizes: list[int] = []
    for b in batches:
        sizes += grouped_search(ivf, b, k, serial_pool)[2]
    hist = np.bincount(sizes)[1:].tolist()
    print(
        f"nq={nq} nprobe={ivf.nprobe} k={k}: {nq * min(ivf.nprobe, ivf.nlist)} plain calls/batch, "
        f"{len(sizes) / len(batches):.1f} grouped calls/batch; audience mean "
        f"{np.mean(sizes):.2f}, histogram (size 1..) {hist}",
        flush=True,
    )

    def serial(b):
        ivf._pool = serial_pool
        return ivf.search(b, k)

    s1 = median_ms(serial, batches)
    ref = [serial(b) for b in batches]
    out = {"hist": hist, "k": k, "nq": nq, "serial": [s1], "cells": []}
    print(f"{'T':>3s} {'plain r1/r2 ms':>17s} {'grouped r1/r2 ms':>18s} {'plain x':>8s} {'grp x':>6s}  verdict")
    for threads in [int(x) for x in args.threads.split(",")]:
        pool = _ShardPool(threads, "probe")

        def plain(b):
            ivf._pool = pool
            return ivf.search(b, k)

        def grouped(b):
            return grouped_search(ivf, b, k, pool)[:2]

        plain_exact = all(
            np.array_equal(p[0], r[0]) and np.array_equal(p[1], r[1])
            for p, r in zip(map(plain, batches), ref)
        )
        grouped_same = sum(same_topk(grouped(b), r) for b, r in zip(batches, ref))
        p1, g1, g2, p2 = (median_ms(f, batches) for f in (plain, grouped, grouped, plain))
        wins = max(g1, g2) < min(p1, p2)
        c = {"threads": threads, "plain": [p1, p2], "grouped": [g1, g2], "plain_exact": plain_exact,
             "grouped_same": grouped_same, "grouped_wins": wins}
        out["cells"].append(c)
        pool.close()
        print(
            f"{threads:3d} {p1:8.2f}/{p2:8.2f} {g1:8.2f}/{g2:8.2f} {s1 / ((p1 + p2) / 2):8.2f} "
            f"{((p1 + p2) / 2) / ((g1 + g2) / 2):6.2f}  "
            f"{'GROUPED WINS' if wins else 'plain'}  (plain==serial: {plain_exact}; "
            f"grouped same top-k {grouped_same}/{len(batches)})",
            flush=True,
        )
    out["serial"].append(median_ms(serial, batches))
    means = {c["threads"]: sum(c["plain"]) / 2 for c in out["cells"]}
    bands = {c["threads"]: abs(c["plain"][0] - c["plain"][1]) for c in out["cells"]}
    best = min(means, key=means.get)
    # the smallest pool within noise of the fastest: fewer threads for the same QPS
    out["best_threads"] = min(
        t for t in means if means[t] - means[best] <= max(bands[t], bands[best])
    )
    at = next(c for c in out["cells"] if c["threads"] == out["best_threads"])
    # spec D3 is settled (p3): plain q1 ships. grouped only guards that decision: a
    # REVISIT is reported to the user, and nothing ships on it
    out["guard"] = "REVISIT" if at["grouped_wins"] else "OK"
    print(
        f"serial r1/r2 {out['serial'][0]:.2f}/{out['serial'][1]:.2f} ms; chosen pool size "
        f"{out['best_threads']} (default here {default_ivf_search_threads()}); "
        f"GUARD grouped vs plain at {out['best_threads']}: {out['guard']}",
        flush=True,
    )
    return out


def run_exact(col: Collection, args) -> dict:
    """Spec §7 F item 1: every result at the largest --threads is compared with the
    serial path (_ShardPool(1), i.e. IVF_SEARCH_THREADS=1) position by position, ids
    and float32 score bits, so a changed tie order fails too."""
    ivf, k, seed = col.index, col._rescore_k(10), args.seed
    threads = max(int(x) for x in args.threads.split(","))
    if args.vectors:
        qs = bench_queries(args.vectors, args.limit, args.queries, seed)
    else:
        qs = sample_queries(col, args.queries, seed)
    serial, pooled = _ShardPool(1), _ShardPool(threads, "probe")

    def same(kw: dict, q: np.ndarray) -> bool:
        ivf._pool = serial
        s1, i1 = ivf.search(q, k, **kw)
        ivf._pool = pooled
        s2, i2 = ivf.search(q, k, **kw)
        return (
            s1.dtype == s2.dtype == np.float32 and i1.dtype == i2.dtype
            and np.array_equal(s1.view(np.uint32), s2.view(np.uint32))
            and np.array_equal(i1, i2)
        )

    batches = [np.ascontiguousarray(qs[s : s + 8]) for s in range(0, len(qs), 8)]
    singles = [np.ascontiguousarray(qs[i : i + 1]) for i in range(len(qs))]
    out = {
        "threads": threads, "seed": seed, "queries": len(qs), "batches": len(batches),
        "unfiltered_same": sum(same({}, b) for b in batches), "filter": None,
        "filter_ids": 0, "filtered_same": 0, "tiny_ids": 0, "tiny_same": 0,
        "max_list": largest_list(ivf), "cliff": POOLED_CLIFF,
    }
    ok = out["unfiltered_same"] == len(batches)
    note = "no --filter-key"
    if args.filter_key:
        val, allow, _ = filter_allowlist(col, args.filter_key)
        tiny = allow[:: max(1, len(allow) // 100)][:100]  # <=128: the owners path
        out.update(
            filter=f"{args.filter_key}={val}", filter_ids=len(allow), tiny_ids=len(tiny),
            filtered_same=sum(same({"allowlist": allow}, q) for q in singles),
            tiny_same=sum(same({"allowlist": tiny}, q) for q in singles),
        )
        ok = ok and out["filtered_same"] == out["tiny_same"] == len(singles)
        note = f"{args.filter_key}={val!r}, {len(allow)} and {len(tiny)} ids"
    pooled.close()
    out["verdict"] = "PASS" if ok else "FAIL"
    print(
        f"EXACT seed {seed} T={threads} vs IVF_SEARCH_THREADS=1: unfiltered "
        f"{out['unfiltered_same']}/{len(batches)} batches, filtered {out['filtered_same']}"
        f"/{len(singles)}, tiny {out['tiny_same']}/{len(singles)} ({note}; ids, float32 "
        f"score bits and tie order): {out['verdict']}",
        flush=True,
    )
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["stages", "fanout", "exact"])
    ap.add_argument("--data", type=Path, required=True, help="DATA_DIR: holds catalog.db")
    ap.add_argument("--collection", default="bench")
    ap.add_argument("--seed", type=int, default=7, help="exact on the bench's set: 42")
    ap.add_argument("--batches", type=int, default=40)
    ap.add_argument("--nq", default=None, help="stages: list (default 1,8); fanout: one (8)")
    ap.add_argument("--threads", default=f"1,{default_ivf_search_threads()}",
                    help="exact: the largest value is compared with the serial path")
    ap.add_argument("--filter-key", default=None,
                    help="stages: also time this key's mode value; exact: also check it")
    ap.add_argument("--vectors", type=Path, default=None,
                    help="exact: bench.py's corpus .npy, for its own query selection")
    ap.add_argument("--limit", type=int, default=2549619, help="exact: bench.py --limit")
    ap.add_argument("--queries", type=int, default=500, help="exact: bench.py --queries")
    args = ap.parse_args(argv)
    col = open_collection(args.data, args.collection)
    if args.mode == "exact":
        return run_exact(col, args)
    if args.mode == "stages":
        args.nq = args.nq or "1,8"
        return run_stages(col, args)
    args.nq = args.nq or "8"
    return run_fanout(col, args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `32 passed`.

Run: `uv run pytest -q`. Expected: all green (on the sandbox base of 46: `78 passed`).

- [ ] **Step 5: Commit**

```bash
git add bench/ivf_fanout_probe.py tests/test_ivf_fanout.py
git commit -m "Add the IVF stage, fan-out and exactness probe

bench/ivf_fanout_probe.py times each stage of the real IVF search path (stages),
compares serial and plain fan-out over pool sizes with ABBA-timed run1/run2 bands
and keeps audience batching only as a D3 regression guard (fanout), and checks
ids and float32 score bits against IVF_SEARCH_THREADS=1 on bench.py's seed-42
queries, unfiltered and filtered (exact). Every mode reports the largest IVF list
against turbovec's 32,768-row pooled-path cliff. Read-only, in-process.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 7: ADR 0005, and the corrected fixed-cost claim in code and docs

**Files:**
- Create: `docs/adr/0005-ivf-fanout-and-upstream-stance.md`
- Modify: `src/raggio/store.py`: the IVF comment block above `IVF_DEFAULT_NPROBE`, and `_ivf_auto_nlist`'s comment
- Modify: `README.md` and `docs/getting-started.md`: the env-var table row
- Modify: `docs/concepts.md` (the IVF paragraph) and `docs/indexing.md` ("When to add an index")
- Modify: `docs/adr/0001-performance-optimization-decisions.md`: line 30, the IVF row; lines 48–49, the standing constraints
- Modify: `docs/adr/0002-optional-ivf-index.md`: the Status line, and the Consequences bullet on batched scans
- Modify: `tests/test_ivf_fanout.py` (append a section)

**Interfaces:**
- Consumes:
  - Task 1's `IVF_SEARCH_THREADS` semantics;
  - Task 2's intersection numbers;
  - Task 5's `dirty_shards` / `sync() -> int`;
  - Plan E's ADR 0004, which ADR 0005 cross-references as the first-party native BM25 exception to ADR 0001:48.
- Produces:
  - ADR 0005 with a `## Verification` section whose body starts with `Pending:`. Task 10's `planF-verify.py` replaces it and asserts the prefix.
  - The cross-reference `0001:48`, which the Task 7 test pins.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ivf_fanout.py`:

```python
# ---- docs: ADR 0005 replaces the "~0.4 ms fixed cost" reading and documents the knob ----


def test_docs_drop_the_fixed_cost_claim_and_document_the_knob():
    import re

    for path in ("src/raggio/store.py", "docs/concepts.md", "docs/indexing.md"):
        text = Path(path).read_text(encoding="utf-8")
        assert not re.search(r"0\.4 ?ms", text), path
    adr = Path("docs/adr/0005-ivf-fanout-and-upstream-stance.md").read_text(encoding="utf-8")
    assert "IVF_SEARCH_THREADS" in adr and "0001:48" in adr and "## Verification" in adr
    # spec §4.2 and §2 row 6: the default, the pooled-path cliff, the rayon warning
    assert "min(12, os.cpu_count())" in adr and "min(32" not in adr
    assert "32,768" in adr and "RAYON_NUM_THREADS=1" in adr
    for path in ("docs/adr/0001-performance-optimization-decisions.md",
                 "docs/adr/0002-optional-ivf-index.md"):
        assert "ADR 0005" in Path(path).read_text(encoding="utf-8"), path
    for path in ("README.md", "docs/getting-started.md"):
        text = Path(path).read_text(encoding="utf-8")
        assert "| `IVF_SEARCH_THREADS` | `min(12, CPUs)` |" in text, path
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `1 failed, 32 passed`. `test_docs_drop_the_fixed_cost_claim_and_document_the_knob` fails with `AssertionError: src/raggio/store.py`, because the comment still says `~0.4ms fixed cost per probed shard`.

- [ ] **Step 3: Implement**

3a. Create `docs/adr/0005-ivf-fanout-and-upstream-stance.md` with exactly this content:

````markdown
# ADR 0005 — IVF shard fan-out, and the turbovec upstream stance

Status: accepted · Date: 2026-09-28 · Partly supersedes ADR 0001:48–49; corrects ADR 0001:30 and ADR 0002:7-9

## Context

The 2026-09-28 DGX baseline (main @19a8a2e, 2,549,119 × 1024 arXiv vectors, 4 GiB cap,
`docs/superpowers/research/2026-09-28-dgx-baseline.md`) had the optional IVF index
(nlist 256, nprobe 16, recall@10 0.994) scan 1/16 of the flat scan's bytes, yet reach
only 166-214 QPS at concurrency 8 against flat's 116.5-116.9 (a 16% band between warm
IVF runs; the published run had IVF *behind* flat, 116 vs 129). Filtered IVF search was
slower than flat outright: p50 43.7-70.5 ms vs 25.2-28.0 ms.

Reading turbovec 1.0.0 and raggio together
(`docs/superpowers/research/2026-09-28-item3a-turbovec-internals.md`,
`docs/superpowers/research/2026-09-28-item3b-upstream-landscape.md`) found two things:

1. **The "~0.4 ms fixed cost per shard search call" (ADR 0001:30, ADR 0002:7-9) was a
   misread.** An `IdMapIndex` search of one query on a shard under 32,768 rows (1,024
   blocks, `SINGLE_QUERY_PARALLEL_MIN_BLOCKS`, turbovec/src/search.rs:30) runs inline on
   the calling thread with the GIL released (`py.detach`, turbovec-python/src/lib.rs:1177;
   `with_pool_if`, lib.rs:1211 and 1863). The extension pins rayon's global pool to a
   one-thread sentinel (lib.rs:1879, called at 2153-2154), so no other thread helps. Such a
   call costs the shard's bytes at single-core speed. On the DGX, ADR 0002's own numbers
   give 5.1 ms / 16 probes = 0.32 ms per call *including* 6.6 MB of codes, and nprobe 8
   vs 16 (2.7 vs 5.1 ms) has an intercept of about zero. IVF read 6.25% of the bytes at
   about 1/4.5 of the flat scan's bandwidth (20.7 vs 94 GB/s): one core against all 20.
2. **The serial loop was the bottleneck.** `_IvfIndex.search` ran every (query, shard)
   call one after another on one thread: 128 calls per 8-query micro-batch at nprobe 16,
   while the flat scan runs one 8-query pass on every core. ADR 0001:49 ("application-level
   paths are exhausted") never tried running those calls in parallel.

Filtered IVF search had a serial cost of its own: `_intersect` binary-searched every
allowlist id into each probed shard's ids, O(|allow| log |shard|) per shard. For a
255k-id filter over 16 probed shards of 2.55M rows that was 94 ms per query (laptop).

DGX probe p3 (`docs/superpowers/research/2026-09-28-dgx-probe-p3-ivf-fanout.md`)
measured a prototype of these decisions on the same corpus. In-process, fan-out on 12
threads reached 610.4 QPS (661.7 with `OPENBLAS_NUM_THREADS=1`). At the HTTP server (c=8,
4 GiB cap, warm, `OPENBLAS_NUM_THREADS=1` in both arms), concurrent QPS rose from 189.2
[162.8..207.3] to 324.7 [308.6..381.4], filtered p50 fell from 36.6 to 6.41 ms, and serial
p50 fell from 8.44 to 6.50 ms. The memory peak was 1.82 GB in both arms.

## Decisions

| Decision | Rationale |
|---|---|
| **Fan out every (query, shard) search as its own one-query task** on a per-collection thread pool (`_ShardPool` in `store.py`) | Each task runs inline and GIL-free inside turbovec, so T threads scan T shards at once. Laptop (6 CPUs; 262,144 × 1024, nlist 32, nprobe 16, 8-query batches): 118 ms serial → 60 ms at T=2, 31 ms at T=6 (3.75x) |
| Merge in the serial loop's order | Tasks are built in the old loop's order and results are consumed in that order, so every pool size returns the serial result bit-for-bit, tie order included (`tests/test_ivf_fanout.py`) |
| `IVF_SEARCH_THREADS` env var, default `min(12, os.cpu_count())`; `1` = serial | A deployment knob, not an algorithm constant. 12 is p3's best pool size on the 20-core DGX: chosen on seed 7 and held on seed 42, with kernel throughput flat from 12 threads up (19.2k–19.6k calls/s). The earlier draft default, capped at 32 threads, would resolve to 20 there, and 20 threads lose 14–17 %. `1` never starts a thread and is the pre-0005 code path bit-for-bit: the escape hatch |
| Pool owned by the `Collection`: built on the first multi-shard search, closed at the end of `stop()` | Never the asyncio default executor: searches already run inside it (`asyncio.to_thread`), and waiting on it from there can deadlock. Flat collections never start threads. A search orphaned by a cancelled request that outlives `stop()` falls back to the serial loop instead of raising |
| **The 32,768-row pooled-path cliff** is documented and reported, not engineered around | From 32,768 rows (1,024 blocks) up, a one-query shard search leaves the inline path for turbovec's pooled one. All fan-out threads then share one rayon pool, and the fan-out ceiling falls to about 2.45× (p3: 1,436 → 3,525 calls/s on 40k-row shards). Results stay identical (tested with 35k-row shards and 4 concurrent callers). The default `nlist` aims at about 8k rows per shard, but k-means lists are uneven: at nlist 256 on the 2.55M DGX corpus the largest list is 30,443 rows, 93 % of the cliff. `bench/ivf_fanout_probe.py` prints the largest list on every run. Growing collections may need a larger `nlist` to stay under the cliff; that is a future, measured change. **Never set `RAYON_NUM_THREADS=1`**: it flattens the pooled path to about 1,440 calls/s from 2 threads on |
| Audience batching (one call per shard carrying every query that probes it) is **not shipped** (spec D3, settled by p3) | Grouping never beat plain one-query fan-out beyond noise on aarch64: best grouped 570.6 vs 610.4 QPS at 12 threads (640.7 vs 661.7 with `OPENBLAS_NUM_THREADS=1`), and grouped without threads is −39 % against serial. The mean audience at nq=8 and nprobe 16 of 256 is 1.40 queries, where 2–3-query calls cost 2–5× per query (on NEON they share nothing, turbovec/src/search.rs:3717-3760, and each pays a ~70 µs pool install, lib.rs:224-234). `bench/ivf_fanout_probe.py fanout` keeps the grouped arm only as a regression guard |
| **Shard-side allowlist intersection**: binary-search the smaller side into the larger; allowlists are sorted once per cache miss | 255k-id filter, 16 shards of 2.55M rows (laptop): 94 → 5 ms per query; 765k ids: 345 → 8 ms. DGX (p3): the intersect falls from 29.8 to 2.0 ms per query |
| **Each filtered (query, shard) task cuts its own allowlist slice** on its pool thread; only tiny allowlists (≤128 ids) find their owning shards on the caller | p3, filtered p50 in-process: 3.7 ms (2.2 ms with `OPENBLAS_NUM_THREADS=1`) cut inside the tasks, against 6.4–7.0 ms (3.9–4.4 ms) cut up front on the caller. Filtered searches are nq=1, so a caller-side cut shared across queries bought nothing. Results are bit-identical to serial, including tie order |
| **Generation counters** on `_allow_cache` (per collection) and on the IVF shard-id cache (per shard) | A search whose request was cancelled keeps running without a read lock. If a write lands mid-scan, its older snapshot may serve that search once but is never cached |
| **Dirty-shard sync**: `_IvfIndex.sync()` writes only shards written since their last sync, plus any shard file the target directory lacks, and returns the count; `dirty_shards` exposes the set | A one-row job used to rewrite all 256 shard files; now it writes one. Plan D2's batched sync builds on this API |

## Upstream stance (partly supersedes ADR 0001:48–49)

ADR 0001:48 put turbovec kernel changes (SIMD, ANN, multi-partition search) out of scope.
For **code** that still holds: raggio never forks turbovec and carries no turbovec kernel
code. What changes:

- **Upstream proposals are in scope.** Short, measured, plain-English issue drafts live
  outside the repo (`D:/DEV/_scratch/upstream-drafts/turbovec-*.md`). raggio's maintainer
  posts them personally, if at all. Each draft names a raggio-side acceptance test, so an
  upstream change is adopted only when it measurably deletes raggio code or beats this
  ADR's fan-out: `search_many` (one call for many shards), `ids()`, an allowlist intersect
  mode, native partitioned/IVF search (building on the maintainer's `feat/ivf-residual`
  branch, 1452b6e), cp314t wheels, and the NEON 2–3-query tail.
- ADR 0001:49's "application-level paths are exhausted" is withdrawn: parallel shard
  fan-out was one. (ADR 0004 records the first-party native BM25 exception to 0001:48.)

## Consequences

- Up to `IVF_SEARCH_THREADS` threads per resident indexed collection, so at most
  `MAX_RESIDENT_COLLECTIONS` pools. Idle threads cost only their stacks.
- `os.cpu_count()` ignores cgroup CPU quotas: containers started with `--cpus` should set
  `IVF_SEARCH_THREADS` explicitly.
- ADR 0002:76-77 ("batched concurrent scans degrade to per-query probing") still describes
  the task granularity, but no longer the throughput: the probed shards now scan in parallel.
- The fan-out's server gain depends on `OPENBLAS_NUM_THREADS=1`, which is an image `ENV`
  owned by the Dockerfile change of the performance program's Plan A (spec D16), not a
  raggio setting. With numpy's default OpenBLAS, 20 busy-spinning threads burn about 16
  cores under any search load. They push query and event-loop threads onto the slow cores
  and cap concurrent IVF QPS at about 203 instead of about 325 (p3). Deployments outside
  the image should set it too.
- Never set `RAYON_NUM_THREADS=1` (see the cliff above).
- After the fan-out, the server's limit is the HTTP/event-loop pipeline: about 3.1 ms per
  request, against an in-process ceiling of 610–662 QPS. The follow-ups (two
  `_drain_scans` batches in flight, `_hydrate` off the loop, a cheaper vector-body parse)
  are outside this ADR.

## Verification

Pending: Plan F's DGX A/B appends the results here (base vs F default vs
`IVF_SEARCH_THREADS=1`, seed 42, spec §6 noise bands, spec §7 F items 1–7), the stage
attribution from `bench/ivf_fanout_probe.py stages`, the seed-7 pool-size sweep with the
grouped regression guard, the seed-42 exactness check, and the largest IVF list size.
````

3b. `src/raggio/store.py`: replace the five-line comment above `IVF_DEFAULT_NPROBE = 16`, from `# Optional ScaNN-style IVF index, attached/removed per collection via the index API.` to `# on the real corpus. Fixed RAM cost is ~0.5-1 MB per shard (bench/shard_mem_probe.py).`, with:

```python
# Optional ScaNN-style IVF index, attached/removed per collection via the index API.
# Measured (bench/ivf_probe.py, ADR 0002): at ~550k rows every recall-preserving cell
# is slower or barely faster than the flat scan (each probed shard is a single-core scan
# of its bytes, ADR 0005), so it stays opt-in; at 2.2M rows it wins 3.5-6.8x.
# nprobe=16 keeps recall@10 >=0.95 on the real corpus. Fixed RAM cost is ~0.5-1 MB per
# shard (bench/shard_mem_probe.py).
```

Replace the whole `_ivf_auto_nlist` function (19a8a2e line 356; its two-line comment says `per-shard fixed search cost (~0.4ms each)`) with:

```python
def _ivf_auto_nlist(n: int) -> int:
    # ~8k rows per shard, power of two (ADR 0002's measured sweet spot); below 32,768
    # rows turbovec scans a one-query search inline on the calling thread (ADR 0005)
    return int(np.clip(2 ** round(np.log2(max(n, 1) / 8192)), 16, 1024))
```

3c. In `README.md`, append this row to the env-var table as its last row. At 19a8a2e that means directly after the `| \`COLLECTION_IDLE_TTL\` | \`900\` | seconds before ...` row (line 73); if Plan E added `NATIVE_BM25` there, put it after that row:

```markdown
| `IVF_SEARCH_THREADS` | `min(12, CPUs)` | threads per indexed collection scanning IVF shards in parallel; `1` = serial |
```

In `docs/getting-started.md`, append this row as the last row of the env-var table. At 19a8a2e that is after the `COLLECTION_IDLE_TTL` row (line 37), or after Plan E's `NATIVE_BM25` row if present:

```markdown
| `IVF_SEARCH_THREADS` | `min(12, CPUs)` | Threads per indexed collection that scan IVF shards in parallel; `1` restores the serial loop |
```

3d. In `docs/concepts.md`, the IVF paragraph (19a8a2e lines 150–153) runs from `(true neighbors sitting in unprobed shards are invisible — about 1 point of` to `is why it only pays off past ~2M vectors ([Vector indexing](indexing.md)).`. Replace those four lines with:

```markdown
(true neighbors sitting in unprobed shards are invisible — about 1 point of
recall@10 at the defaults), plus per-shard work: each probed shard is its own
single-core scan (raggio runs them on parallel threads,
[ADR 0005](adr/0005-ivf-fanout-and-upstream-stance.md)) and filters are
intersected per shard — which is why it only pays off past ~2M vectors
([Vector indexing](indexing.md)).
```

3e. In `docs/indexing.md`, replace the paragraph from `**Don't index below ~1M vectors.** Measured on the real 553k × 1536 benchmark corpus,` down to `more.` (19a8a2e lines 16–21) with:

```markdown
**Don't index below ~1M vectors.** Measured on the real 553k × 1536 benchmark corpus
with the serial shard loop that ADR 0005 later replaced, every IVF configuration that
preserved recall@10 ≥ 0.95 was at best 1.6x faster than the flat scan — and most were
slower (`bench/ivf_probe.py`, ADR 0001). A probed shard costs its bytes scanned on one
core, while the flat scan reads every byte on all cores at once. Since ADR 0005 the
probed shards of a query batch are scanned in parallel on `IVF_SEARCH_THREADS` threads;
the flat scan still answers a batch of concurrent queries in one kernel pass, so
high-QPS workloads on small collections still favour it.
```

3f. `docs/adr/0001-performance-optimization-decisions.md` gets three edits. Each one keeps the old text and appends an italic cross-reference:
- Line 30, the table row starting `| IVF coarse partitioning on top of \`IdMapIndex\``, becomes:

```markdown
| IVF coarse partitioning on top of `IdMapIndex` (k-means shards, nprobe search) | **Rejected** | `bench/ivf_probe.py`, full sweep (nlist 16/64/256/1024 × nprobe 1–32 × 3 calibration modes): best cell 1.63x speedup at −1 pt recall, below the ≥2x-at-≥0.95 gate; every higher-recall cell is slower than the full scan. ~0.4 ms fixed cost per shard search call eats the savings. Closing the vector-latency gap vs HNSW needs an ANN index inside the turbovec kernel. *Corrected by ADR 0005: not a fixed cost; each one-query shard call is a single-core scan of the shard's bytes, and the calls ran one after another* |
```

- Line 48, the bullet starting `- turbovec is an external dependency`, becomes:

```markdown
- turbovec is an external dependency — kernel changes (SIMD, ANN, multi-partition search) are out of scope; everything layers on `IdMapIndex`. *Partly superseded by ADR 0005: upstream turbovec proposals are in scope; raggio still carries no turbovec kernel code and never forks it.*
```

- Line 49, the bullet starting `- Remaining known headroom needs one of:`, becomes:

```markdown
- Remaining known headroom needs one of: an ANN index in the kernel, more CPUs, or free-threaded Python. Application-level paths are exhausted as of this ADR. *Withdrawn by ADR 0005: parallel IVF shard fan-out was an untried application-level path.*
```

3g. In `docs/adr/0002-optional-ivf-index.md`, line 3 (`Status: accepted · Date: 2026-08-22`) becomes:

```markdown
Status: accepted · Date: 2026-08-22 · The "~0.4 ms fixed cost" in Context is corrected by ADR 0005
```

In its Consequences list, the two-line bullet `- Batched concurrent scans degrade to per-query probing on indexed collections —` / `  documented; high-QPS small collections should stay unindexed.` becomes:

```markdown
- Batched concurrent scans degrade to per-query probing on indexed collections —
  documented; high-QPS small collections should stay unindexed. Since ADR 0005 the
  per-query shard probes of a batch run in parallel threads.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `33 passed`.

Run: `uv run pytest -q`. Expected: all green (on the sandbox base of 46: `79 passed`).

Run: `uv run --group docs mkdocs build --strict -d "$(mktemp -d)"`. Expected: exit 0. The `adr/` pages are built without being in the nav (mkdocs.yml line 10), so ADR 0005 shows up only in the INFO list of pages outside the nav, and the new link in `docs/concepts.md` resolves.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0005-ivf-fanout-and-upstream-stance.md src/raggio/store.py README.md docs/getting-started.md docs/concepts.md docs/indexing.md docs/adr/0001-performance-optimization-decisions.md docs/adr/0002-optional-ivf-index.md tests/test_ivf_fanout.py
git commit -m "Record IVF fan-out in ADR 0005 and correct the fixed-cost claim

The ~0.4 ms per-shard 'fixed cost' was a single-core scan of each shard's bytes,
run one call after another. ADR 0005 records the fan-out (default 12 threads, no
audience batching, per spec D3), the in-task allowlist intersection, the cache and
sync changes, the 32,768-row pooled-path cliff, and the upstream stance (proposals
in scope, no fork); ADR 0001 and 0002, the IVF docs and the store.py comments point
to it. IVF_SEARCH_THREADS joins the env-var tables. Verification stays pending until
the DGX A/B (Task 10).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 8: Upstream drafts, outside the repo, not posted

Spec D6 and D7: raggio's maintainer posts upstream issues personally, if at all. This task writes six short, measured drafts, and a standalone repro for the NEON one, into `D:/DEV/_scratch/upstream-drafts/`. It creates nothing in the repo and makes no commit. It never posts, comments or opens anything on GitHub. Task 10 appends the DGX numbers to three of the drafts.

**Files (all outside the repo):**
- Create: `D:/DEV/_scratch/upstream-drafts/check_drafts.py`
- Create: `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-wheels.md`, `turbovec-ids.md`, `turbovec-search-many.md`, `turbovec-allowlist-intersect.md`, `turbovec-partitioned-ivf.md`, `turbovec-neon-small-nq.md`
- Create: `D:/DEV/_scratch/upstream-drafts/neon_small_nq_repro.py`

**Interfaces:**
- Consumes:
  - Task 2's intersection numbers (94 → 5 ms and 345 → 8 ms);
  - Task 1's fan-out numbers (118 ms serial → 31 ms at T=6);
  - ADR 0005's turbovec 1.0.0 source references.
- Produces:
  - The six drafts. Each has `# Draft (not posted):`, the target URL, `order: N of 6`, `## Title`, `## Acceptance (raggio side)` and `## Disclosure`. Task 10's `planF-verify.py` inserts `## DGX data (<date>, gn100)` before `## Disclosure` in `turbovec-search-many.md`, `turbovec-partitioned-ivf.md` and `turbovec-neon-small-nq.md`.
  - `neon_small_nq_repro.py`, which Task 10's A/B script runs on gn100 from `~/planF/`.

- [ ] **Step 1: Write the failing check**

Create `D:/DEV/_scratch/upstream-drafts/check_drafts.py` with exactly this content:

```python
import sys
from pathlib import Path

d = Path(sys.argv[1])
names = [
    "turbovec-cp314t-wheels.md", "turbovec-ids.md", "turbovec-search-many.md",
    "turbovec-allowlist-intersect.md", "turbovec-partitioned-ivf.md", "turbovec-neon-small-nq.md",
]
for order, name in enumerate(names, 1):
    text = (d / name).read_text(encoding="utf-8")
    for needle in ("# Draft (not posted):", "- Target: https://github.com/RyanCodrai/turbovec/issues/new",
                   f"order: {order} of 6", "## Title", "## Acceptance (raggio side)", "## Disclosure"):
        assert needle in text, (name, needle)
compile((d / "neon_small_nq_repro.py").read_text(encoding="utf-8"), "neon_small_nq_repro.py", "exec")
print(f"{len(names)} drafts and the repro OK in {d}")
```

- [ ] **Step 2: Run the check to verify it fails**

Run from the repo root: `uv run python D:/DEV/_scratch/upstream-drafts/check_drafts.py D:/DEV/_scratch/upstream-drafts`

Expected: it exits non-zero with `FileNotFoundError` naming `turbovec-cp314t-wheels.md`.

- [ ] **Step 3: Write the drafts and the repro**

Create each file below with exactly the content shown.

`D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-wheels.md`:

```markdown
# Draft (not posted): publish free-threaded (cp314t) wheels

- Target: https://github.com/RyanCodrai/turbovec/issues/new (packaging request; reference #163)
- Poster: raggio's maintainer, from their own account. Do not ask for contributor access unless invited.
- Suggested posting order: 1 of 6 (smallest, most self-contained).

## Title

Publish free-threaded (cp314t) wheels

## Summary

The 1.0.0 wheels are `cp39-abi3`. A free-threaded CPython 3.14t cannot load abi3 wheels, so
`pip install turbovec` there falls back to the sdist and needs a Rust toolchain. #163
anticipated this.

turbovec looks ready for it:

- pyo3 0.29 treats modules as GIL-free by default since 0.28, and the `#[pymodule]` at
  turbovec-python/src/lib.rs:2151 declares nothing that re-enables the GIL.
- We built the 1.0.0 sdist on CPython 3.14.7t (Windows x64, Rust 1.98.1) in 57 s.
  `sys._is_gil_enabled()` stayed `False` after import, with RuntimeWarnings turned into errors.
- A concurrent stress test (4 writers doing `add_with_ids`/`remove`, 8 readers searching,
  3 runs) passed. That is a smoke test, not a proof.
- CPython 3.13t is not possible: PyO3 refuses free-threaded builds below 3.14.

## The ask

1. Add `-i python3.14t` legs to `release-pypi.yml` for manylinux x86_64 and aarch64, macOS
   arm64 and Windows x64 (the maturin `args:` at lines 63, 149 and 184 today build one abi3
   wheel each). PyO3 ignores `abi3` for free-threaded targets and builds
   version-specific wheels.
2. Add a smoke test that runs `python -c "import turbovec, sys; assert not sys._is_gil_enabled()"`.
3. Optionally, add the threaded stress test to the Python tests. We can share ours.

## Acceptance (raggio side)

We count this done when all of these hold:

1. `uv pip install turbovec==<the release>` on CPython 3.14t for linux aarch64 and x86_64
   installs a `cp314t` wheel with no Rust toolchain on the machine.
2. `python -c "import turbovec, sys; assert not sys._is_gil_enabled()"` passes there.
3. raggio's experimental 3.14t image (Plan G) drops its turbovec-from-sdist build step, and
   `uv run pytest -q` stays green inside it.

## Disclosure

This issue was drafted with AI assistance (Claude) from measurements we ran ourselves.
```

`D:/DEV/_scratch/upstream-drafts/turbovec-ids.md`:

```markdown
# Draft (not posted): expose the ids an IdMapIndex holds

- Target: https://github.com/RyanCodrai/turbovec/issues/new (feature request)
- Poster: raggio's maintainer, from their own account. Do not ask for contributor access unless invited.
- Suggested posting order: 2 of 6. Mention #547 (its `slot_to_id()` covers the same need in Rust).

## Title

Feature request: `IdMapIndex.ids()` in Python

## Summary

The Rust `IdMapIndex` already has `iter_ids()` (turbovec/src/id_map.rs:722). Python has no way
to list the ids an index holds. raggio needs that list in two places:

- On startup, to drop ids whose database rows were deleted while the process was down.
- In its IVF index, to split a filter's allowlist into per-part slices, because `search`
  rejects allowlist ids the part does not hold.

Today we get the list by searching with `k = len(index)` and a dummy query, which scores
every row to read back the ids. At 553k rows that took about 0.7 s on every cold start.

## The ask

`IdMapIndex.ids() -> numpy.ndarray[uint64]`, in slot order (any order is fine for us), built
from `iter_ids()` under the read lock. About 30 lines in the binding.

## Acceptance (raggio side)

We adopt `ids()` only if all of these hold:

1. raggio's `_IvfIndex._shard_ids` and the flat-index ghost reconcile (`_reconcile_ghosts`)
   call `ids()` instead of the `k = len` probe, and `np.array_equal(np.sort(shard.ids()),
   probe_ids)` holds for every shard in `tests/test_ivf_fanout.py`'s fixtures.
2. In one process on the DGX bench volume (2,549,119 rows, 4-bit), listing the flat
   index's ids with `ids()` takes under 5% of the `k = len` probe's time.

## Disclosure

This issue was drafted with AI assistance (Claude) from measurements we ran ourselves.
```

`D:/DEV/_scratch/upstream-drafts/turbovec-search-many.md` (it contains a fenced code block, hence the four-backtick fence here):

````markdown
# Draft (not posted): search many IdMapIndex parts in one call

- Target: https://github.com/RyanCodrai/turbovec/issues/new (feature request)
- Poster: raggio's maintainer, from their own account. Do not ask for contributor access unless invited.
- Suggested posting order: 3 of 6, after the cp314t and ids() issues.

## Title

Feature request: `search_many` — one search over several IdMapIndex parts, each query probing its own parts

## Summary

raggio is a self-hosted RAG server (Apache-2.0) built on `IdMapIndex`. For collections of
millions of vectors it keeps an optional IVF index as many small `IdMapIndex` parts: k-means
cells of about 8,000-10,000 rows each (2.55M x 1024-d, 4-bit, 256 parts). A query scores the
centroids and searches its 16 nearest parts. So a batch of 8 queries is 128 separate
one-query `search` calls, and every call repeats the same per-query preparation (rotation,
query calibration, lookup tables) that an ordinary 8-query search does once.

We now run those calls on a Python thread pool. Each call releases the GIL and runs inline
on the calling thread (parts are under 1,024 blocks), so this works and scales with cores.
But it is the most work a caller can remove from outside turbovec. The rest (preparing each
query once, scanning a part once for all queries that probe it, one pool handoff per batch)
can only happen inside the library.

## What we measured

- Per-(query, part) cost on one 8,192 x 1024 4-bit `IdMapIndex` (x86 laptop, AVX2), by
  queries per call: 1: 0.891 ms, 2: 0.485, 4: 0.294, 8: 0.282, 16: 0.127, 64: 0.077.
  Batching queries per part is up to 11x cheaper per query than one-query calls.
- Our Python thread fan-out of one-query calls (6-CPU laptop; 262,144 x 1024, 32 parts,
  16 probed, 8-query batches): 118 ms per batch serial, 31 ms with 6 threads (3.75x).
  Grouping the queries that share a part, on the same pool, reached 6.5x in a separate
  laptop test (48 parts, random probes).
- On a 20-core NVIDIA GB10 (aarch64), our serial IVF read its probed parts at about
  20.7 GB/s against about 94 GB/s for the flat scan: one core against all of them.

## The ask

A function that searches several `IdMapIndex` parts in one call, with a per-query probe list:

```python
scores, ids = turbovec.search_many(
    parts,            # list[IdMapIndex], same dim / bit width / calibration
    queries,          # float32 (nq, dim)
    k,
    probes=probes,    # per query: the indices of the parts it searches (CSR or list of arrays)
    allowlist=None,   # optional ids; see the allowlist-intersect issue
)                     # (nq, k), merged across each query's parts, short rows padded
```

Suggested behaviour:

1. Check that the parts share their quantizer (dim, bit width, calibration); otherwise raise.
2. Prepare each query once.
3. Scan each part once for all the queries that probe it, using the existing multi-query tiles.
4. Run everything under one `py.detach` and one pool handoff, and merge per query.

Open question for you: the padding value for short rows (0, `u64::MAX`, or counts returned
alongside).

## Acceptance (raggio side)

We adopt `search_many` only if all of these hold:

1. `_IvfIndex.search` becomes one `search_many` call per micro-batch, and raggio's
   `tests/test_ivf_fanout.py::test_pooled_search_matches_serial_reference` passes against it
   unchanged (same ids and scores for nq 1/3/8, nprobe 1/3/8, with and without allowlists).
2. On the DGX A/B (2,549,119 x 1024 arXiv, 4 GiB container, seed 42, raggio spec §6), IVF
   concurrent QPS beats raggio's thread fan-out (ADR 0005) median by more than its noise
   band, with recall@10 unchanged (0.994 at nprobe 16), running with `IVF_SEARCH_THREADS=1`.

## Disclosure

This issue was drafted with AI assistance (Claude) from measurements we ran ourselves; the
numbers above come from scripts we can share.
````

`D:/DEV/_scratch/upstream-drafts/turbovec-allowlist-intersect.md`:

```markdown
# Draft (not posted): an allowlist mode that ignores ids the index does not hold

- Target: https://github.com/RyanCodrai/turbovec/issues/new (feature request)
- Poster: raggio's maintainer, from their own account. Do not ask for contributor access unless invited.
- Suggested posting order: 4 of 6.

## Title

Feature request: `search(..., allowlist=ids, allowlist_mode="intersect")` that skips unknown ids

## Summary

`IdMapIndex.search` with an allowlist raises `KeyError` on any id the index does not hold,
and builds a `vec![false; len]` mask on every call (turbovec/src/id_map.rs:664-678). That is
the right default for one index. But when a collection is split over many `IdMapIndex` parts
(raggio's IVF index: 256 parts), one metadata filter becomes one allowlist for every part,
and each part only holds a slice of it.

So raggio keeps a sorted id list per part and intersects the allowlist with it before every
call. It is correct but pure overhead. A 255k-id filter over 16 probed parts of a 2.55M-row
collection cost 94 ms per query with our first version (binary-searching the allowlist into
each part) and still costs about 5 ms with the faster one (binary-searching the part into the
allowlist). It also means keeping every part's id list in memory and invalidating it on
every write.

## The ask

An opt-in mode where allowlist ids the index does not hold are ignored instead of raising:

- `search(queries, k, allowlist=ids, allowlist_mode="intersect")` (default stays `"strict"`).
- The mask built from the ids the index holds, keeping the existing 32-row block skip.
- If possible, a mask sized to the allowlist rather than the index (or a reusable buffer),
  since a small filter on a big part pays the full-length allocation every call.

## Acceptance (raggio side)

We adopt the mode only if all of these hold:

1. raggio deletes `_IvfIndex._intersect` and passes the whole allowlist to every probed
   part with `allowlist_mode="intersect"`. `tests/test_ivf_fanout.py`'s intersect and
   unsorted-allowlist tests, and `tests/test_ivf_index.py::test_filtered_search_uses_owning_shards`,
   still pass.
2. On the DGX A/B (raggio spec §6, seed 42), IVF filtered p50 is no worse than raggio's
   ADR 0005 median beyond its noise band.

## Disclosure

This issue was drafted with AI assistance (Claude) from measurements we ran ourselves.
```

`D:/DEV/_scratch/upstream-drafts/turbovec-partitioned-ivf.md`:

```markdown
# Draft (not posted): feat/ivf-residual for 1-3M-vector Python users

- Target: https://github.com/RyanCodrai/turbovec/issues/new (discussion / RFC; reference #182)
- Poster: raggio's maintainer, from their own account. Do not ask for contributor access unless invited.
- Suggested posting order: 5 of 6, after search_many, ids() and the intersect mode.

## Title

IVF (`feat/ivf-residual`) for 1-3M-vector Python users: our use case, aarch64 data, and the surface we would need

## Summary

raggio is a self-hosted RAG server on `IdMapIndex`. At 2.55M x 1024-d (4-bit) on a 20-core
NVIDIA GB10, the flat scan answers a query in about 21 ms p50. Our Python IVF, built from
256 `IdMapIndex` parts, runs into the costs your `LOG_ivf.md` describes (per-cell query
preparation, many small calls). We read `feat/ivf-residual` (1452b6e): cell-major search,
one lookup table per batch, spill, 17k QPS at 0.91 recall on 500k x 1536. We would like to
move to it if it reaches Python.

You said on #182 that turbovec suits <100k vectors today and you would like <1M. Our scale is
above that, so this is a use case to weigh, not a request to change your plans.

## What we have measured

- Our Python IVF (nlist 256, nprobe 16, exact fp16 rescore): recall@10 0.994, serial p50
  11.2 ms on the DGX before we ran the parts in parallel threads (raggio ADR 0005).
- The serial part scans ran at about 20.7 GB/s against about 94 GB/s for the flat scan:
  one core against all of them.

## What raggio would need from a Python `IvfIndex`

1. Caller-supplied `u64` ids (`add_with_ids`) and `remove(id)`.
2. `search(queries, k, nprobe, allowlist=None)`, batched, with the GIL released.
3. Persistence (`sync`/`load`), and listing the ids it holds.
4. An explicit `fit(sample)`, and ideally the TQ+ calibration you already offer for flat
   indexes. raggio calibrates once, early, and never refits.

Nice to have: a configurable spill tau and `cell_sizes`.

## Questions

1. Is IVF headed for main?
2. Is persistence planned (a v7 extension or a separate file)?
3. How will cell-local deletes interact with spilled copies?
4. Would you rather write the Python binding yourself, or accept one once the Rust API settles?

Non-goals: HNSW, distribution, sharding.

## Acceptance (raggio side)

We adopt a native IVF only if all of these hold:

1. raggio's `_IvfIndex` (centroids, parts, thread pool, intersection, dirty-part sync) is
   replaced behind the same `Collection` API, and the IVF tests (`tests/test_ivf_index.py`,
   plus the caller-id, remove, allowlist and persistence behaviours in
   `tests/test_ivf_fanout.py`) pass.
2. On the DGX A/B (2,549,119 x 1024 arXiv, 4 GiB container, seed 42, raggio spec §6),
   recall@10 is at least 0.994 after raggio's fp16 rescore at the chosen probe setting, and
   IVF concurrent QPS and serial p50 beat raggio's ADR 0005 thread fan-out beyond its noise
   band, with memory under load inside the 4 GiB cap.

## Disclosure

This issue was drafted with AI assistance (Claude) from measurements we ran ourselves.
```

`D:/DEV/_scratch/upstream-drafts/turbovec-neon-small-nq.md`:

```markdown
# Draft (not posted): on NEON, searches of 2-3 queries share no work

- Target: https://github.com/RyanCodrai/turbovec/issues/new (performance issue)
- Poster: raggio's maintainer, from their own account. Do not ask for contributor access unless invited.
- Suggested posting order: 6 of 6. Attach `neon_small_nq_repro.py` (same folder) and its DGX output.

## Title

aarch64: a search with 2 or 3 queries costs at least 2x or 3x one query (tail path skips the 4-query tile)

## Summary

On aarch64, a batch smaller than 4 queries takes the tail path in turbovec/src/search.rs:3717-3760:
it runs the single-query kernel once per query and rebuilds `vm8_single` for each. So 2 or 3
queries cost at least 2x or 3x one query (on GB10 we measured more, see Repro). From 4 queries up, a tile shares the work (tile widths
12/8/4, search.rs:3539-3547). On x86 the AVX2 path pads a short batch with its last query
(search.rs:4059-4062), so 2 and 3 queries cost about what 4 costs.

Your own H101 numbers (Axion) show it, in ms per pass by queries: 1: 0.560, 2: 1.046,
4: 0.943, 8: 1.235, 12: 1.618. Two queries cost more than four.

## Why it matters to us

raggio splits big collections into many `IdMapIndex` parts (IVF). When several queries in a
batch probe the same part, one call with all of them should be cheaper than one call each.
On aarch64 that only pays off for 8 or more queries sharing a part. With 256 parts and 8
queries of 16 probes each, a probed part serves 1.40 queries on average, and the shared
parts mostly get 2-3. So raggio does not group queries per part (raggio ADR 0005). On our
GB10 the best grouped layout lost 3-7 % of QPS against one query per call (570.6 vs 610.4,
and 640.7 vs 661.7 with OPENBLAS_NUM_THREADS=1).

## Repro

`neon_small_nq_repro.py` (numpy + turbovec only, single-threaded, about 30 s) times one
8,192 x 1024 4-bit `IdMapIndex` by queries per call and prints the per-query cost relative
to one query. It also checks that the batched rows equal the one-query rows.

On an x86 laptop (AVX2), two runs printed per-query cost vs nq=1 of nq=2 0.58-0.59,
nq=3 0.45-0.50 and nq=4 0.37-0.41.
On aarch64 (DGX Spark, GB10, turbovec 1.0.0) we measured per-query cost vs nq=1 of
nq=2 2.05x and 2.61x, nq=3 4.89x and 1.95x, and nq=8 0.87x and 0.82x, on 8,192-row and
10,000-row parts respectively. Break-even comes only at nq=8. The batched rows were
bit-identical to the one-query rows.

## The ask

Pad 2-3 query batches into the 4-query tile on NEON (as AVX2 does), or add a 2-query tile.

## Acceptance (raggio side)

We act on a fix only if all of these hold:

1. `neon_small_nq_repro.py` on the DGX (GB10) reports per-query cost at nq=2 and nq=3 of at
   most 0.6x of nq=1.
2. raggio's `bench/ivf_fanout_probe.py fanout` on the DGX bench volume then prints
   `GUARD grouped vs plain at <T>: REVISIT`. Only then would raggio reopen its decision
   not to group queries per part (raggio spec D3), and only with its maintainer.

## Disclosure

This issue was drafted with AI assistance (Claude) from measurements we ran ourselves.
```

`D:/DEV/_scratch/upstream-drafts/neon_small_nq_repro.py`:

```python
"""turbovec 1.0.0 small-batch repro: cost of one IdMapIndex.search call by query count.

On aarch64 (NEON) a batch of 2 or 3 queries takes the tail path in
turbovec/src/search.rs:3717-3760: one single-query kernel per query, with the
vm8_single table rebuilt for each. So 2 or 3 queries cost about 2x or 3x one query,
while 4 queries share one qbs=4 tile. On x86 (AVX2) 2 and 3 are padded up to a tile
of 4, so they cost about what 4 does.

Standalone: needs only numpy and turbovec. Runs single-threaded (RAYON_NUM_THREADS=1,
set before the import) so the numbers are per-core kernel cost, not pool scheduling.
That setting is for this repro only: an application must not set it, because it
flattens turbovec's pooled path (parts of 32,768 rows or more) to about 1,440 calls/s.
About 30 s. Usage:  python neon_small_nq_repro.py
"""
import os

os.environ.setdefault("RAYON_NUM_THREADS", "1")

import importlib.metadata  # noqa: E402
import platform  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402

import numpy as np  # noqa: E402
from turbovec import IdMapIndex  # noqa: E402

DIM, BITS, ROWS, K, REPS = 1024, 4, 8192, 50, 300
NQS = (1, 2, 3, 4, 5, 8, 12)


def unit(rng, n):
    x = rng.standard_normal((n, DIM), dtype=np.float32)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def median_us(fn):
    for _ in range(20):
        fn()
    ts = []
    for _ in range(REPS):
        t = time.perf_counter_ns()
        fn()
        ts.append(time.perf_counter_ns() - t)
    return statistics.median(ts) / 1e3


def main():
    rng = np.random.default_rng(0)
    ix = IdMapIndex(dim=DIM, bit_width=BITS)
    ix.calibrate(unit(rng, 1024))
    ix.add_with_ids(unit(rng, ROWS), np.arange(1, ROWS + 1, dtype=np.uint64))
    ix.prepare()
    qs = unit(rng, max(NQS))
    print(
        f"turbovec {importlib.metadata.version('turbovec')}, {platform.machine()}, "
        f"{platform.python_version()}, RAYON_NUM_THREADS={os.environ['RAYON_NUM_THREADS']}; "
        f"IdMapIndex {ROWS} x {DIM}, {BITS}-bit, k={K}, median of {REPS}"
    )
    print(f"{'nq':>3s} {'us/call':>9s} {'us/query':>9s} {'vs nq=1':>8s}")
    one = None
    rows = {}
    for nq in NQS:
        q = np.ascontiguousarray(qs[:nq])
        _, ids = ix.search(q, K)
        for r in range(nq):  # batched rows equal the single-query rows
            assert np.array_equal(ix.search(q[r : r + 1], K)[1][0], ids[r])
        us = median_us(lambda: ix.search(q, K))
        per_q = us / nq
        one = one or per_q
        rows[nq] = per_q / one
        print(f"{nq:3d} {us:9.1f} {per_q:9.1f} {per_q / one:8.2f}", flush=True)
    print(
        f"per-query cost vs nq=1: nq=2 {rows[2]:.2f}, nq=3 {rows[3]:.2f}, nq=4 {rows[4]:.2f}. "
        "Measured on GB10 NEON: nq=2 2.05, nq=3 4.89 (no sharing); on x86 AVX2 all below 0.7."
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the check to verify it passes**

Run from the repo root: `uv run python D:/DEV/_scratch/upstream-drafts/check_drafts.py D:/DEV/_scratch/upstream-drafts`

Expected: `6 drafts and the repro OK in D:/DEV/_scratch/upstream-drafts` (Windows may print the path with backslashes).

Run the repro once on the laptop: `uv run python D:/DEV/_scratch/upstream-drafts/neon_small_nq_repro.py`. It takes about 30 s. Expected on x86: the last line starts `per-query cost vs nq=1:`, with nq=2 near 0.6, nq=3 near 0.5 and nq=4 near 0.4. The sandbox laptop measured 0.62, 0.49 and 0.42, because AVX2 pads 2 and 3 queries up to a 4-query tile. The NEON numbers come from gn100 in Task 10.

- [ ] **Step 5: No commit**

Run: `git status --short`. Expected: nothing from this task, because the drafts live outside the repo. Do not post, comment on, or open any GitHub issue or PR. The drafts are handed to the maintainer as files.

### Task 9: DGX session scripts, checked on the laptop

This task writes the scripts that Task 10 runs, and checks them end to end on synthetic results before any DGX time is spent. It works outside the repo and makes no commit. It touches nothing on gn100.

**Files (all outside the repo):**
- Create: `D:/DEV/_scratch/dgx/planF-check.py`
- Create: `D:/DEV/_scratch/dgx/dgx-planF-ab.sh`, `D:/DEV/_scratch/dgx/planF-build.sh`
- Create: `D:/DEV/_scratch/dgx/planF-summary.py`, `D:/DEV/_scratch/dgx/planF-verify.py`

**Interfaces:**
- Consumes:
  - spec §6: the deploy layout (`~/raggio`), container `bench-tv` with volume `bench-tv`, collection `bench`, `--limit 2549619`, the run order, the noise bands (at least 3 IVF runs, between-server variance from a fresh container per run, the same `OPENBLAS_NUM_THREADS` in both arms) and the row labels (regime, cap, `sqlite3.sqlite_version`);
  - spec §7 F (acceptance items 1–7, quoted in Task 10);
  - `bench/bench.py`'s `results-partial.json` keys (`qps_concurrent`, `lat_filtered_p50`, `lat_p50`, `qps_serial`, `lat_c_p95`, `lat_filtered_p95`, `recall_at_10`, `hybrid_p50`, `mem_under_load_mb`, plus `index_build_s` / `ingest_s`, which appear only when a run rebuilt or re-ingested). `mem_under_load_mb` is one `podman stats` sample, not a peak, so item 5 reads the container's cgroup v2 `memory.peak` instead;
  - the `--fingerprint-dir` / `--adopt` flags, if Plan A's hardened bench is present;
  - Task 6's probe CLI and its `LISTS`, `GUARD` and `EXACT` lines;
  - Task 7's ADR 0005 `Pending:` Verification;
  - Task 8's drafts and `neon_small_nq_repro.py`.
- Produces:
  - `dgx-planF-ab.sh <base tag> <F tag> <0|1>`, which writes `~/planF/ab.log` and `~/planF/results/` on gn100 and ends its log with `== PLANF AB DONE <ts> ==` or `== PLANF AB FAILED: <reason> (<ts>) ==`. Every container gets `-e OPENBLAS_NUM_THREADS=1`. After every bench run it writes `meta-planF-<label>.json` with the keys `label`, `regime` (`host-warm`), `cap`, `sqlite_version`, `openblas_num_threads` and `memory_peak_mb`;
  - `planF-build.sh <source dir> <tag>`;
  - `planF-summary.py <results dir>`, which prints the Verification markdown: the A/B table, the row-label table, the guard line, and one `- PASS:` or `- FAIL:` line per spec §7 F item (`F1` to `F7`) plus one `labels` line. It reads `probe-exact.txt` and `probe-fanout.txt` from the results dir, and exits 1 if any line is `- FAIL:`;
  - `planF-verify.py <fetched dir> <base sha> <F sha> <drafts dir>`, run from the repo root. It never writes `min(32` into ADR 0005, because Task 7's test forbids it.

- [ ] **Step 1: Write the failing check**

Create `D:/DEV/_scratch/dgx/planF-check.py` with exactly this content:

```python
"""Plan F Task 9: check the DGX session scripts on the laptop before any DGX time is spent.
Syntax-checks both shell scripts, then runs planF-summary.py and planF-verify.py end to
end on synthetic results (one passing A/B, one that fails spec §7 F item 2, one whose
base arm lacks OPENBLAS_NUM_THREADS=1) against a temporary copy of ADR 0005 and of the
upstream drafts. Nothing in the repo or the drafts dir is modified.

Usage: python D:/DEV/_scratch/dgx/planF-check.py [<repo> [<drafts dir>]]
       (defaults: D:/DEV/raggio and D:/DEV/_scratch/upstream-drafts)
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(sys.argv[1] if len(sys.argv) > 1 else "D:/DEV/raggio")
DRAFTS = Path(sys.argv[2] if len(sys.argv) > 2 else "D:/DEV/_scratch/upstream-drafts")
ADR = "docs/adr/0005-ivf-fanout-and-upstream-stance.md"
BASE = {"qps_concurrent": 181.0, "lat_filtered_p50": 36.6, "lat_p50": 8.4, "recall_at_10": 0.993}
F = dict(BASE, lat_filtered_p50=6.4, lat_p50=6.5)
META = {"regime": "host-warm", "cap": "4g", "sqlite_version": "3.46.1", "openblas_num_threads": "1",
        "memory_peak_mb": 1820.0}
LISTS = "LISTS largest 30443 rows, 93% of the 32,768-row pooled-path cliff"
EXACT = ("EXACT seed 42 T=12 vs IVF_SEARCH_THREADS=1: unfiltered 63/63 batches, filtered 500/500, "
         "tiny 500/500 (year=2024, 212331 and 100 ids; ids, float32 score bits and tie order): PASS")
GUARD = "serial r1/r2 118.00/117.50 ms; chosen pool size 12 (default here 12); GUARD grouped vs plain at 12: OK"


def run(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")


def write_results(d: Path, f_qps: float, base_blas: str = "1") -> None:
    """run0 plus 3 measured runs per arm, each with its row labels (meta-*.json);
    every arm's QPS band is 2.0 (runs 1..3)."""
    d.mkdir(parents=True)
    arms = {"base": BASE, "f": dict(F, qps_concurrent=f_qps), "f1": BASE}
    for arm, m in arms.items():
        for r in range(4):
            vals = dict(m, qps_concurrent=m["qps_concurrent"] + max(r - 1, 0))
            (d / f"results-planF-ivf-{arm}-run{r}.json").write_text(json.dumps({"raggio-ivf": vals}))
            meta = dict(META, label=f"ivf-{arm}-run{r}",
                        openblas_num_threads=base_blas if arm == "base" else "1")
            (d / f"meta-planF-ivf-{arm}-run{r}.json").write_text(json.dumps(meta))


for sh in ("dgx-planF-ab.sh", "planF-build.sh"):
    r = run("bash", "-n", str(HERE / sh))
    assert r.returncode == 0, (sh, r.stderr)
for py in ("planF-summary.py", "planF-verify.py"):
    compile((HERE / py).read_text(encoding="utf-8"), py, "exec")
assert (DRAFTS / "neon_small_nq_repro.py").exists(), "Task 8's repro is missing: the DGX session copies it"

with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    got = tmp / "fetched"
    res = got / "results"
    write_results(res, 422.0)
    (res / "probe-fanout.txt").write_text(LISTS + "\n" + GUARD + "\n", encoding="utf-8")
    (res / "probe-exact.txt").write_text(LISTS + "\n" + EXACT + "\n", encoding="utf-8")
    (res / "probe-stages.txt").write_text(LISTS + "\nstages fixture\n", encoding="utf-8")
    (res / "neon-repro.txt").write_text("neon fixture\n", encoding="utf-8")
    (got / "ab.log").write_text("== PLANF AB START 2026-10-01 09:00:00 base=aaa f=bbb flat_check=0 ==\n")
    r = run(sys.executable, str(HERE / "planF-summary.py"), str(res))
    assert r.returncode == 0 and "- FAIL:" not in r.stdout, r.stdout + r.stderr
    assert r.stdout.count("- PASS: F") == 7 and "- PASS: labels" in r.stdout, r.stdout
    assert "GUARD grouped vs plain at 12: OK" in r.stdout and "30,443 rows" in r.stdout, r.stdout
    (got / "summary.md").write_text(r.stdout, encoding="utf-8")

    write_results(tmp / "lose", 182.0)  # 183 median: below 1.5x base, and inside base's range
    r = run(sys.executable, str(HERE / "planF-summary.py"), str(tmp / "lose"))
    assert r.returncode == 1 and "- FAIL: F2 concurrent QPS" in r.stdout, r.stdout + r.stderr

    write_results(tmp / "blas", 422.0, base_blas="unset")  # spec §7 F: OPENBLAS_NUM_THREADS=1 in both arms
    r = run(sys.executable, str(HERE / "planF-summary.py"), str(tmp / "blas"))
    assert r.returncode == 1 and "- FAIL: labels" in r.stdout, r.stdout + r.stderr

    work = tmp / "repo"
    (work / "docs" / "adr").mkdir(parents=True)
    shutil.copy(REPO / ADR, work / ADR)
    shutil.copytree(DRAFTS, tmp / "drafts")
    r = run(sys.executable, str(HERE / "planF-verify.py"), str(got), "aaa", "bbb", str(tmp / "drafts"), cwd=work)
    assert r.returncode == 0, r.stdout + r.stderr
    adr = (work / ADR).read_text(encoding="utf-8")
    ver = adr.split("## Verification", 1)[1]
    for needle in ("IVF_SEARCH_THREADS=1", "QPS concurrent", "Filtered p50", "Recall@10",
                   "Memory peak", "sqlite_version", "OPENBLAS_NUM_THREADS=1", "### §7 F acceptance",
                   "GUARD grouped vs plain", "Batching guard: **OK** at T=12", "EXACT seed 42",
                   "LISTS largest 30443", "3.1 ms per request", "memstatus"):
        assert needle in ver, needle
    assert "Pending:" not in ver and "- FAIL:" not in ver
    assert "min(32" not in adr, "Task 7's test forbids min(32 anywhere in ADR 0005"
    for name in ("turbovec-search-many.md", "turbovec-partitioned-ivf.md", "turbovec-neon-small-nq.md"):
        assert "## DGX data (2026-10-01, gn100)" in (tmp / "drafts" / name).read_text(encoding="utf-8"), name
print(f"Plan F DGX scripts OK in {HERE} (ADR from {REPO}, drafts from {DRAFTS})")
```

- [ ] **Step 2: Run the check to verify it fails**

Run from the repo root: `uv run python D:/DEV/_scratch/dgx/planF-check.py`

Expected: exit 1 with `AssertionError: ('dgx-planF-ab.sh', 'bash: ...dgx-planF-ab.sh: No such file or directory\n')`.

- [ ] **Step 3: Write the scripts**

Create each file below with exactly the content shown, with LF line endings: the two shell scripts run under bash on gn100.

`D:/DEV/_scratch/dgx/dgx-planF-ab.sh`:

```bash
#!/bin/bash
# Plan F DGX A/B (spec §6 protocol, §7 F acceptance) on arXiv 2,549,119 x 1024 (--limit 2549619), 4 GiB cap.
# Arms: base = the preceding main; f = Plan F at the default IVF_SEARCH_THREADS; f1 = Plan F
# with IVF_SEARCH_THREADS=1. IVF phase: one discarded run0, then 3 interleaved rounds of
# base/f/f1, every run in a fresh 4 GiB container, so the bands include between-server
# variance (spec §6). Every container and probe runs with OPENBLAS_NUM_THREADS=1 (spec §7 F;
# the image ENV is Plan A's, D16, so it is passed explicitly in case the base predates it).
# After every run, meta-planF-<label>.json records the row labels: regime host-warm, the
# cap, sqlite3.sqlite_version (D15), OPENBLAS_NUM_THREADS, and the cgroup memory.peak.
# Then the seed-7 probes and the seed-42 exactness check (container removed), the NEON
# repro, and with FLAT_CHECK=1 one IVF<->flat transition plus a base/f flat
# no-regression pair (run0 + 2 rounds). Nothing here sets RAYON_NUM_THREADS (spec §4.2).
# Usage (on gn100; ~/raggio holds Plan F's tree, both images are built):
#   setsid -f bash ~/planF/dgx-planF-ab.sh <base tag> <F tag> <0|1> >/dev/null 2>&1 </dev/null
set -uo pipefail
BASE_TAG=$1
F_TAG=$2
FLAT_CHECK=${3:-0}
export OPENBLAS_NUM_THREADS=1
CUR_MEM=none
cd ~/raggio || exit 1
OUT=~/planF/results
mkdir -p "$OUT"
exec >>~/planF/ab.log 2>&1
H=(-H x-api-key:bench)
URL=localhost:18000/collections/bench
CAPS="raggio container capped at 4 GiB (4-bit quantized flat index; IVF column adds the optional index)."
HOST="NVIDIA DGX Spark - GB10 Grace, 20 aarch64 cores, 122 GB unified LPDDR5x, native Linux, rootless podman"
ts() { date '+%F %T'; }
fail() { echo "== PLANF AB FAILED: $* ($(ts)) =="; exit 1; }

start_ctr() {  # $1 = image tag, $2 = memory cap, $3... = extra podman run args
  local tag=$1 mem=$2
  shift 2
  podman rm -f bench-tv >/dev/null 2>&1
  podman run -d --name bench-tv --memory "$mem" -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench \
    -e OPENBLAS_NUM_THREADS=1 "$@" "localhost/raggio:$tag" >/dev/null || fail "podman run $tag"
  CUR_MEM=$mem
  local t0 t1 t2 info
  t0=$(date +%s.%N)
  for _ in $(seq 1 1200); do curl -sf localhost:18000/healthz >/dev/null && break; sleep 0.5; done
  t1=$(date +%s.%N)
  info=$(curl -s --max-time 1800 "${H[@]}" $URL)
  t2=$(date +%s.%N)
  echo "[$(ts)] ctr up ($tag, mem $mem, extra: ${*:-none}): healthz $(awk "BEGIN{print $t1-$t0}") s, first GET /collections/bench $(awk "BEGIN{print $t2-$t1}") s"
  echo "  $info"
}
wait_pending() {
  local p
  for _ in $(seq 1 1440); do
    p=$(curl -s --max-time 900 "${H[@]}" $URL | python3 -c 'import sys,json; print(json.load(sys.stdin)["pending_jobs"])' 2>/dev/null)
    [ "$p" = "0" ] && { echo "[$(ts)] pending_jobs=0"; return 0; }
    sleep 5
  done
  fail "pending jobs never drained"
}
index_type() {
  curl -s --max-time 900 "${H[@]}" $URL | python3 -c 'import sys,json; print((json.load(sys.stdin).get("index") or {}).get("type", "flat"))'
}
B() { .venv/bin/python -u bench/bench.py --limit 2549619 --caps-note "$CAPS" --host "$HOST" "${FP_ARGS[@]}" "$@" || fail "bench $*"; }
meta() {  # $1 = label: the spec §6 row labels of the run just measured, read from its container
  local pk sv ob
  # memory.peak first: the two python execs below add to the cgroup, but stay far below its peak
  pk=$(podman exec bench-tv python -c 'print(open("/sys/fs/cgroup/memory.peak").read().strip())') || fail "memory.peak in $1"
  sv=$(podman exec bench-tv python -c 'import sqlite3; print(sqlite3.sqlite_version)') || fail "sqlite_version in $1"
  ob=$(podman exec bench-tv python -c 'import os; print(os.environ.get("OPENBLAS_NUM_THREADS", "unset"))') || fail "OPENBLAS_NUM_THREADS in $1"
  printf '{"label": "%s", "regime": "host-warm", "cap": "%s", "sqlite_version": "%s", "openblas_num_threads": "%s", "memory_peak_mb": %s}\n' \
    "$1" "$CUR_MEM" "$sv" "$ob" "$(awk "BEGIN{printf \"%.1f\", $pk/1048576}")" >"$OUT/meta-planF-$1.json"
  echo "  labels $1: host-warm, cap $CUR_MEM, sqlite_version $sv, OPENBLAS_NUM_THREADS=$ob, memory.peak $pk bytes"
}
run_one() {  # $1 = label, $2 = engine; measures the running container
  rm -f bench/results-partial.json
  echo "== $1 $2 ($(ts)) =="
  local adopt=()
  # hardened bench (Plan A): each run0 adopts the volume's data and records its fingerprint
  case $1 in *-run0) [ "$BENCH_MODE" = hardened ] && adopt=(--adopt) ;; esac
  B --engine "$2" "${adopt[@]}" --out "$OUT/results-planF-$1.md"
  mv bench/results-partial.json "$OUT/results-planF-$1.json"
  case $1 in
    *-run0) ;;
    *)  # a measured run must reuse the data and the index exactly as they are
      python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(any("index_build_s" in v or "ingest_s" in v for v in r.values()))' \
        "$OUT/results-planF-$1.json" || fail "$1 rebuilt or re-ingested instead of reusing" ;;
  esac
  podman stats --no-stream --format '{{.Name}} {{.MemUsage}}' bench-tv
  meta "$1"
}
arm_ctr() {  # $1 = arm: a fresh 4 GiB container of that arm's image and env
  case $1 in
    base) start_ctr "$BASE_TAG" 4g ;;
    f) start_ctr "$F_TAG" 4g ;;
    f1) start_ctr "$F_TAG" 4g -e IVF_SEARCH_THREADS=1 ;;
    *) fail "unknown arm $1" ;;
  esac
  wait_pending
}
flat_phase() {  # the volume must already be flat
  arm_ctr base
  [ "$(index_type)" = flat ] || fail "flat phase on an indexed volume"
  run_one flat-base-run0 raggio
  for r in 1 2; do
    for arm in base f; do
      arm_ctr "$arm"
      run_one "flat-$arm-run$r" raggio
    done
  done
}
detach_8g() {  # not measured: the IVF -> flat detach needs a transient 8 GiB container until Plan C
  start_ctr "$F_TAG" 8g
  wait_pending
  local t0 jid
  t0=$(date +%s)
  jid=$(curl -s --max-time 900 -X DELETE "${H[@]}" $URL/index | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])') || fail "detach request"
  echo "[$(ts)] detach job $jid"
  wait_pending
  echo "[$(ts)] detach done in $(( $(date +%s) - t0 )) s at 8 GiB: $(curl -s --max-time 900 "${H[@]}" $URL)"
  podman stop -t 60 bench-tv >/dev/null
}

echo "== PLANF AB START $(ts) base=$BASE_TAG f=$F_TAG flat_check=$FLAT_CHECK =="
pgrep -af 'bench/bench.py' && fail "another bench.py run is in flight"
uptime
free -m | head -2
grep -E '^Cached' /proc/meminfo
podman ps -a
for t in "$BASE_TAG" "$F_TAG"; do
  podman image inspect "localhost/raggio:$t" --format "image $t id {{.Id}} digest {{.Digest}} created {{.Created}}" || fail "no image $t"
  podman run --rm --entrypoint python "localhost/raggio:$t" -c 'import importlib.metadata as m; print("turbovec", m.version("turbovec"))'
done

# --- bench client mode. Plan A's hardened bench (fingerprints keyed by corpus and limit,
# kept outside the checkout; loud aborts instead of a re-ingest) needs no patch. The pre-A
# bench gets the baseline3 scratch patches on this DGX copy only (spec §5): the arXiv
# fingerprint for the flat engine and the reuse-check timeout. Both modes get a guard so
# bench.py can never DELETE and re-ingest the bench collection. ---
if grep -q '"--fingerprint-dir"' bench/bench.py; then
  BENCH_MODE=hardened
  FP_ARGS=(--fingerprint-dir "$HOME/raggio-bench/state")
  mkdir -p "$HOME/raggio-bench/state"
else
  BENCH_MODE=legacy
  FP_ARGS=()
  [ -f bench/fingerprint-raggio-ivf.json ] || fail "bench/fingerprint-raggio-ivf.json missing: copy ~/turborag/bench/fingerprint-turborag-ivf.json there first"
  cp bench/fingerprint-raggio-ivf.json bench/fingerprint-raggio.json
  sed -i 's/ENGINES\[name\]\["hdrs"\], timeout=30)/ENGINES[name]["hdrs"], timeout=900)/' bench/bench.py
  grep -q 'timeout=900' bench/bench.py || fail "timeout patch"
fi
echo "bench client mode: $BENCH_MODE"
python3 - <<'EOF'
import pathlib, re
p = pathlib.Path("bench/bench.py")
s = p.read_text()
if "planF: refusing" not in s:
    m = re.search(r"^async def ingest_raggio\(.*\):\n", s, re.M)
    assert m, "ingest_raggio is not a one-line def any more: port the guard by hand"
    guard = '    raise SystemExit("planF: refusing to DELETE/re-ingest the bench collection")\n'
    p.write_text(s[:m.end()] + guard + s[m.end():])
EOF
grep -q 'planF: refusing' bench/bench.py || fail "ingest guard patch"

# first start: the base image; its healthz / first-GET times are the session's first-start record
arm_ctr base
# spec §7 F item 5 needs the cgroup v2 memory.peak inside the container: fail now, not after run0
podman exec bench-tv python -c 'open("/sys/fs/cgroup/memory.peak").read()' \
  || fail "no readable /sys/fs/cgroup/memory.peak in bench-tv: item 5 cannot be measured"
echo "host venv sqlite_version (probe rows): $(.venv/bin/python -c 'import sqlite3; print(sqlite3.sqlite_version)')"
START=$(index_type)
echo "[$(ts)] volume index state at start: $START"
if [ "$START" = flat ] && [ "$FLAT_CHECK" = 1 ]; then
  flat_phase  # flat first, so the run0 below is the session's one flat -> IVF transition
  arm_ctr base
fi

# --- IVF phase: run0 is discarded (it builds the index at 4 GiB if the volume is flat) ---
run_one ivf-base-run0 raggio-ivf
[ "$(index_type)" = ivf ] || fail "no IVF index after run0"
for r in 1 2 3; do
  for arm in base f f1; do
    arm_ctr "$arm"
    run_one "ivf-$arm-run$r" raggio-ivf
  done
done

# --- seed-7 probes and the seed-42 exactness check, in-process on the IVF volume with no
# container holding it. These rows are host-warm and uncapped (a host process), and use
# the host venv's sqlite_version; each output file opens with that label. ---
podman stop -t 60 bench-tv >/dev/null
podman rm -f bench-tv >/dev/null 2>&1
VOL=$(podman volume inspect bench-tv --format '{{.Mountpoint}}')
PY=(podman unshare env OPENBLAS_NUM_THREADS=1 .venv/bin/python -u)
PROBE_LABEL="# rows: host-warm, uncapped (in-process on the host), sqlite_version $(.venv/bin/python -c 'import sqlite3; print(sqlite3.sqlite_version)'), OPENBLAS_NUM_THREADS=1"
echo "== probe stages ($(ts)) =="
{ echo "$PROBE_LABEL"; "${PY[@]}" bench/ivf_fanout_probe.py stages \
  --data "$VOL" --threads 1,12 --filter-key year; } | tee "$OUT/probe-stages.txt" || fail "probe stages"
echo "== probe fanout ($(ts)) =="
{ echo "$PROBE_LABEL"; "${PY[@]}" bench/ivf_fanout_probe.py fanout \
  --data "$VOL" --threads 1,2,4,8,12,16,20; } | tee "$OUT/probe-fanout.txt" || fail "probe fanout"
echo "== probe exact ($(ts)) =="  # spec §7 F items 1 (bit identity) and 7 (largest list)
{ echo "$PROBE_LABEL"; "${PY[@]}" bench/ivf_fanout_probe.py exact --data "$VOL" --threads 12 \
  --vectors bench/corpus/embed-vecs.npy --limit 2549619 --queries 500 --seed 42 --filter-key year; } \
  | tee "$OUT/probe-exact.txt" || fail "probe exact"
echo "== NEON small-nq repro ($(ts)) =="
.venv/bin/python -u ~/planF/neon_small_nq_repro.py | tee "$OUT/neon-repro.txt" || fail "neon repro"

# --- optional flat no-regression pair: the session's one IVF -> flat transition ---
if [ "$START" = ivf ] && [ "$FLAT_CHECK" = 1 ]; then
  detach_8g
  flat_phase
fi

free -m | head -2
podman ps -a --filter name=bench-tv
podman stop -t 60 bench-tv >/dev/null 2>&1
echo "== PLANF AB DONE $(ts) =="
```

`D:/DEV/_scratch/dgx/planF-build.sh`:

```bash
#!/bin/bash
# Plan F: build localhost/raggio:<tag> from <source dir> unless that image already exists.
# Usage (on gn100): bash ~/planF/planF-build.sh <source dir> <tag>
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

`D:/DEV/_scratch/dgx/planF-summary.py`:

```python
"""Plan F DGX A/B summary (spec §6 noise bands and row labels, §7 F acceptance).

Reads from a results directory:
- results-planF-<phase>-<arm>-run<N>.json: bench.py's results-partial.json, one engine per file;
- meta-planF-<phase>-<arm>-run<N>.json: dgx-planF-ab.sh's row labels for that run
  (regime, cap, sqlite_version, openblas_num_threads, memory_peak_mb);
- probe-exact.txt (its EXACT and LISTS lines) and probe-fanout.txt (its GUARD line).
It ignores run0 and prints the markdown that goes under ADR 0005's "## Verification",
ending with one PASS/FAIL line per spec §7 F item (F1..F7) and one for the row labels.

Usage: python planF-summary.py <results dir>
"""
import json
import re
import statistics
import sys
from pathlib import Path

# (key, label, higher is better, decimals); memory_peak_mb comes from the meta files
METRICS = [
    ("qps_concurrent", "QPS concurrent", True, 1),
    ("lat_filtered_p50", "Filtered p50 (ms)", False, 1),
    ("lat_p50", "Search p50 (ms)", False, 1),
    ("qps_serial", "QPS serial", True, 1),
    ("lat_c_p95", "p95 under concurrency (ms)", False, 1),
    ("lat_filtered_p95", "Filtered p95 (ms)", False, 1),
    ("recall_at_10", "Recall@10", True, 3),
    ("hybrid_p50", "Hybrid p50 (ms)", False, 1),
    ("mem_under_load_mb", "Memory under load, one sample (MB)", False, 0),
    ("memory_peak_mb", "Memory peak, cgroup (MB)", False, 0),
]
LABELS = ("regime", "cap", "sqlite_version", "openblas_num_threads")
WANT = {"regime": ["host-warm"], "cap": ["4g"], "openblas_num_threads": ["1"]}
ARMS = {"base": "base (preceding main)", "f": "F, default threads", "f1": "F, IVF_SEARCH_THREADS=1"}
NAME = re.compile(r"results-planF-(ivf|flat)-(base|f|f1)-run(\d+)\.json$")
CLIFF = 32_768  # turbovec 1.0.0's pooled-path threshold for nq=1 (spec §7 F item 7)


def load(results: Path) -> dict:
    """{(phase, arm): [bench metrics merged with the run's meta labels, in run order]}"""
    runs: dict = {}
    for p in sorted(results.glob("results-planF-*.json")):
        m = NAME.search(p.name)
        if not m or m.group(3) == "0":
            continue  # run0 is the discarded warm-up
        (res,) = json.loads(p.read_text()).values()
        meta = p.with_name(p.name.replace("results-", "meta-", 1))
        if meta.exists():
            res = {**res, **json.loads(meta.read_text())}
        runs.setdefault((m.group(1), m.group(2)), []).append((int(m.group(3)), res))
    return {k: [r for _, r in sorted(v, key=lambda t: t[0])] for k, v in runs.items()}


def labels_of(runs: dict, phase: str) -> dict:
    """{arm: {label: sorted distinct values over its measured runs}}; "missing" if absent."""
    return {
        a: {lab: sorted({str(r.get(lab, "missing")) for r in runs[(phase, a)]}) for lab in LABELS}
        for a in ARMS if (phase, a) in runs
    }


def label_table(labels: dict) -> list:
    arms = list(labels)
    lines = ["| Row label | " + " | ".join(ARMS[a] for a in arms) + " |", "|---|" + "---|" * len(arms)]
    for lab in LABELS:
        lines.append(f"| {lab} | " + " | ".join(", ".join(labels[a][lab]) for a in arms) + " |")
    return lines


def probe_line(path: Path, marker: str) -> str:
    """The last line of a probe output that contains marker, or ''."""
    if not path.exists():
        return ""
    hits = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if marker in ln]
    return hits[-1] if hits else ""


def stats(vals: list) -> tuple:
    return statistics.median(vals), max(vals) - min(vals)


def table(runs: dict, phase: str) -> tuple[list, dict]:
    arms = [a for a in ARMS if (phase, a) in runs]
    out = {}
    lines = [
        "| Metric | " + " | ".join(f"{ARMS[a]}: median (band, n)" for a in arms) + " |",
        "|---|" + "---|" * len(arms),
    ]
    for key, label, _, dec in METRICS:
        cells = []
        for a in arms:
            vals = [r[key] for r in runs[(phase, a)] if key in r]
            if not vals:
                cells.append("—")
                continue
            med, band = stats(vals)
            out[(a, key)] = (med, band, vals)
            cells.append(f"{med:.{dec}f} ({band:.{dec}f}, {len(vals)})")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return lines, out


def beats(out, a, b, key, higher) -> tuple[bool, str]:
    """a beats b on key by more than the larger of the two noise bands."""
    (ma, ba, _), (mb, bb, _) = out[(a, key)], out[(b, key)]
    gain = (ma - mb) if higher else (mb - ma)
    band = max(ba, bb)
    return gain > band, f"{ma:.2f} vs {mb:.2f}: gain {gain:+.2f}, band {band:.2f}"


def within(out, a, b, key) -> tuple[bool, str]:
    (ma, ba, _), (mb, bb, _) = out[(a, key)], out[(b, key)]
    band = max(ba, bb)
    return abs(ma - mb) <= band, f"{ma:.2f} vs {mb:.2f}: diff {ma - mb:+.2f}, band {band:.2f}"


def main(argv: list) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # the tables use — and §
    sys.stderr.reconfigure(encoding="utf-8")
    results = Path(argv[1])
    runs = load(results)
    for arm in ARMS:
        n = len(runs.get(("ivf", arm), []))
        if n < 3:
            sys.exit(f"ivf/{arm}: {n} measured runs, spec §6 needs at least 3")
    print("### IVF A/B (seed 42, 2,549,119 x 1024, nlist 256, nprobe 16, c=8, host-warm, "
          "--memory 4g, OPENBLAS_NUM_THREADS=1 in every arm)\n")
    lines, ivf = table(runs, "ivf")
    print("\n".join(lines))
    labels = labels_of(runs, "ivf")
    print("\n### Row labels (spec §6: regime, cap, sqlite3.sqlite_version, OPENBLAS_NUM_THREADS)\n")
    print("\n".join(label_table(labels)))
    if any(p == "flat" for p, _ in runs):
        print("\n### Flat no-regression check\n")
        lines, flat = table(runs, "flat")
        print("\n".join(lines))
    else:
        flat = None

    verdicts = []
    exact = probe_line(results / "probe-exact.txt", "EXACT seed")
    lists = probe_line(results / "probe-exact.txt", "LISTS largest")
    guard = probe_line(results / "probe-fanout.txt", "GUARD grouped vs plain")
    # F1: recall unchanged, and ids + float32 score bits identical to IVF_SEARCH_THREADS=1
    rec = {round(v, 3) for a in ARMS for v in ivf[(a, "recall_at_10")][2]}
    ok = len(rec) == 1 and exact.endswith(": PASS")
    verdicts.append((ok, f"F1 recall@10 unchanged at the default nprobe: {sorted(rec)} [0.993]; "
                         f"{exact or 'no EXACT line in probe-exact.txt'}"))
    # F2: median of >=3 runs at least 1.5x base, and F's minimum above base's maximum
    (mf, _, vf), (mb, _, vb) = ivf[("f", "qps_concurrent")], ivf[("base", "qps_concurrent")]
    same_sqlite = labels["f"]["sqlite_version"] == labels["base"]["sqlite_version"]
    ok = mf >= 1.5 * mb and min(vf) > max(vb) and same_sqlite
    text = (f"F2 concurrent QPS (c=8, medians of {len(vf)} and {len(vb)} runs): F {mf:.1f} "
            f"[{min(vf):.1f}..{max(vf):.1f}] vs base {mb:.1f} [{min(vb):.1f}..{max(vb):.1f}], "
            f"{mf / mb:.2f}x; needs >= 1.5x and F's min above base's max "
            "[p3: 324.7 [308.6..381.4] vs 189.2 [162.8..207.3]]")
    if not same_sqlite:
        text += "; the arms differ in sqlite_version, so the gap is not attributed to F (spec D15)"
    verdicts.append((ok, text))
    # F3: filtered p50 <= 10 ms
    fp, bp = ivf[("f", "lat_filtered_p50")][0], ivf[("base", "lat_filtered_p50")][0]
    verdicts.append((fp <= 10.0, f"F3 filtered p50 {fp:.2f} ms, needs <= 10 (base {bp:.2f}) [p3: 6.41 vs 36.6]"))
    # F4: serial p50 better beyond the band
    ok, why = beats(ivf, "f", "base", "lat_p50", False)
    verdicts.append((ok, f"F4 serial p50 better beyond the band: {why} ms [p3: 6.50 vs 8.44]"))
    # F5: container memory peak within 50 MB of base
    if ("f", "memory_peak_mb") in ivf and ("base", "memory_peak_mb") in ivf:
        pf, pb = ivf[("f", "memory_peak_mb")][0], ivf[("base", "memory_peak_mb")][0]
        verdicts.append((abs(pf - pb) <= 50, f"F5 container memory peak (cgroup memory.peak, median) "
                                             f"{pf:.0f} vs base {pb:.0f} MB, needs within 50 [p3: 1.82 GB in both]"))
    else:
        verdicts.append((False, "F5 no memory_peak_mb in the meta files"))
    # F6: IVF_SEARCH_THREADS=1 reproduces the serial path (base is the serial path)
    ok1, why1 = within(ivf, "f1", "base", "lat_p50")
    ok2, why2 = within(ivf, "f1", "base", "qps_concurrent")
    verdicts.append((ok1 and ok2, f"F6 IVF_SEARCH_THREADS=1 reproduces the serial path within the band: "
                                  f"serial p50 {why1}; concurrent QPS {why2}"))
    # F7: the largest IVF list, against the 32,768-row pooled-path cliff
    m = re.search(r"LISTS largest (\d+) rows", lists)
    if m:
        big = int(m.group(1))
        text = f"F7 largest IVF list {big:,} rows, {100 * big / CLIFF:.0f}% of the {CLIFF:,}-row pooled-path cliff [30,443]"
        if big >= CLIFF:
            text += " -- AT OR ABOVE THE CLIFF: report it to the user (fan-out ceiling about 2.45x there)"
        verdicts.append((True, text))
    else:
        verdicts.append((False, "F7 no LISTS line in probe-exact.txt"))
    wrong = [f"{a} {lab}={','.join(v)}" for a, labs in labels.items() for lab, v in labs.items()
             if (lab in WANT and v != WANT[lab]) or len(v) != 1 or "missing" in v]
    sv = "; ".join(f"{a} {','.join(labels[a]['sqlite_version'])}" for a in labels)
    verdicts.append((not wrong, "labels: every measured IVF run is host-warm, cap 4g, "
                                f"OPENBLAS_NUM_THREADS=1, one sqlite_version per arm ({sv})"
                                + (f"; wrong: {'; '.join(wrong)}" if wrong else "")))
    if flat is not None:
        for key, higher in (("qps_concurrent", True), ("lat_filtered_p50", False), ("lat_p50", False)):
            ok, why = within(flat, "f", "base", key)
            if not ok:  # outside the band: fine only if it is an improvement
                ok = beats(flat, "f", "base", key, higher)[0]
            verdicts.append((ok, f"flat {key} no regression: {why}"))
    print("\n### Seed-7 pool-size sweep and grouped regression guard\n")
    print("`bench/ivf_fanout_probe.py fanout` (seed 7): " + (guard or "no GUARD line"))
    print("\n### §7 F acceptance\n")
    for ok, text in verdicts:
        print(f"- {'PASS' if ok else 'FAIL'}: {text}")
    return 0 if all(ok for ok, _ in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

`D:/DEV/_scratch/dgx/planF-verify.py` (it contains fenced code blocks, hence the four-backtick fence here):

````python
"""Plan F Task 10: replace ADR 0005's "Pending:" paragraph with the DGX A/B record, and
append the DGX numbers to three upstream drafts. Run from the repo root:

  python D:/DEV/_scratch/dgx/planF-verify.py D:/DEV/_scratch/dgx/planF <base sha> <F sha> D:/DEV/_scratch/upstream-drafts

<fetched dir> holds ab.log, summary.md (planF-summary.py's output) and results/ with
probe-stages.txt, probe-fanout.txt, probe-exact.txt and neon-repro.txt.
It never writes "min(32" (Task 7's test forbids it in ADR 0005).
"""
import re
import sys
from pathlib import Path

ADR = Path("docs/adr/0005-ivf-fanout-and-upstream-stance.md")


def main(argv: list) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    got, base, f, drafts = Path(argv[1]), argv[2], argv[3], Path(argv[4])
    log = (got / "ab.log").read_text(encoding="utf-8")
    date = re.search(r"== PLANF AB START (\S+)", log).group(1)
    summary = (got / "summary.md").read_text(encoding="utf-8").strip()
    stages = (got / "results" / "probe-stages.txt").read_text(encoding="utf-8").strip()
    fanout = (got / "results" / "probe-fanout.txt").read_text(encoding="utf-8").strip()
    exact = (got / "results" / "probe-exact.txt").read_text(encoding="utf-8").strip()
    neon = (got / "results" / "neon-repro.txt").read_text(encoding="utf-8").strip()
    assert "- FAIL:" not in summary, "a §7 F line failed: report to the user instead of recording"
    m = re.search(
        r"chosen pool size (\d+) \(default here (\d+)\); GUARD grouped vs plain at (\d+): (OK|REVISIT)",
        fanout,
    )
    assert m, "no GUARD line in probe-fanout.txt"
    t_star, t_def, guard = int(m.group(1)), int(m.group(2)), m.group(4)
    guard_line = m.group(0)
    if guard == "OK":
        guard_text = (
            f"Batching guard: **OK** at T={t_star}. Audience batching stays unshipped (spec D3, "
            "settled by p3); the plain per-(query, shard) fan-out is the only path."
        )
    else:
        guard_text = (
            f"Batching guard: **REVISIT** at T={t_star}: grouped beat plain beyond its run1/run2 "
            "band on seed 7. Nothing ships on it; it is reported to the user as a reason to "
            "reopen spec D3."
        )
    if t_star == t_def:
        pool_text = f"The default pool on gn100 (T={t_def}) is the smallest within noise of the fastest on seed 7."
    else:
        pool_text = (
            f"On seed 7 a pool of {t_star} threads is the smallest within noise of the fastest on "
            f"gn100 (default {t_def}). That is a finding for the user: this ADR keeps the default "
            "`min(12, os.cpu_count())` (spec §4.2)."
        )
    body = f"""DGX A/B, {date}: gn100 (GB10, 20 aarch64 cores). Base `{base}` (the preceding main) vs
Plan F `{f}` at the default `IVF_SEARCH_THREADS` ({t_def} on gn100) and at `IVF_SEARCH_THREADS=1`.
arXiv 2,549,119 × 1024, seed 42, 500 queries, concurrency 8 (the concurrent phase repeats
the same 500 queries, so its caches are warm). Host-warm, `--memory 4g`, and
`OPENBLAS_NUM_THREADS=1` in every arm (spec §7 F, D16). Every run is a fresh container, so
the bands include between-server variance: a discarded run0, then 3 interleaved rounds per
arm (median, band = max − min; spec §6). The row-label table gives each arm's
`sqlite3.sqlite_version` (D15). Raw results: `D:/DEV/_scratch/dgx/planF/`.

{summary}

{guard_text} {pool_text}

After F, the server limit is the HTTP/event-loop pipeline, about 3.1 ms per request, against
an in-process ceiling of 610–662 QPS (spec §7 F, p3). The follow-ups (two `_drain_scans`
batches in flight, `_hydrate` off the event loop, a cheaper parse of the vector body) are
outside this ADR. Concurrent numbers also carry SQLite's global memstatus malloc mutex (p2,
D15), so arms are compared only at the same `sqlite_version`.

### Seed-42 exactness and the largest IVF list (`bench/ivf_fanout_probe.py exact`, in-process)

```text
{exact}
```

### Stage attribution (`bench/ivf_fanout_probe.py stages`, seed 7, in-process)

```text
{stages}
```

### Pool-size sweep and grouped regression guard (`bench/ivf_fanout_probe.py fanout`, seed 7)

```text
{fanout}
```

### turbovec small-batch repro on GB10 (`neon_small_nq_repro.py`)

```text
{neon}
```
"""
    s = ADR.read_text(encoding="utf-8")
    head, sep, tail = s.partition("## Verification\n")
    assert sep and tail.lstrip().startswith("Pending:"), "ADR 0005's Verification is not pending"
    ADR.write_text(head + sep + "\n" + body, encoding="utf-8")

    ivf_table = summary
    for cut in ("### Flat no-regression check", "### Seed-7", "### §7 F acceptance"):
        ivf_table = ivf_table.split(cut)[0]
    ivf_table = ivf_table.strip()
    extra = {
        "turbovec-search-many.md": f"{ivf_table}\n\nThese medians and bands are what acceptance item 2 compares against.",
        "turbovec-partitioned-ivf.md": f"{ivf_table}\n\nThese medians and bands are what acceptance item 2 compares against.",
        "turbovec-neon-small-nq.md": f"```text\n{neon}\n```\n\nraggio's fan-out probe on the same host: `{guard_line}`.",
    }
    for name, text in extra.items():
        p = drafts / name
        d = p.read_text(encoding="utf-8")
        if "## DGX data" not in d:
            head, sep, tail = d.partition("## Disclosure")
            assert sep, name
            p.write_text(f"{head}## DGX data ({date}, gn100)\n\n{text}\n\n{sep}{tail}", encoding="utf-8")
    print(f"ADR 0005 Verification recorded ({date}); guard {guard} at T={t_star}; drafts updated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
````

- [ ] **Step 4: Run the check to verify it passes**

Run from the repo root: `uv run python D:/DEV/_scratch/dgx/planF-check.py`

Expected: `Plan F DGX scripts OK in D:\DEV\_scratch\dgx (ADR from D:\DEV\raggio, drafts from D:\DEV\_scratch\upstream-drafts)`. The check syntax-checks both shell scripts, then runs:
- a passing synthetic A/B: seven `- PASS: F1` to `- PASS: F7` lines, a `- PASS: labels` line, the guard line and the `30,443 rows` list size;
- one that fails item 2: exit 1 with `- FAIL: F2 concurrent QPS` (its missing probe files also fail F1 and F7);
- one whose base arm has `OPENBLAS_NUM_THREADS` unset: exit 1 with `- FAIL: labels`;
- `planF-verify.py`, on temporary copies of ADR 0005 and the drafts. It asserts every Verification needle, that the ADR holds no `min(32`, and the `## DGX data (2026-10-01, gn100)` sections.

Neither the repo nor the drafts directory is modified.

Run: `grep -l $'\r' D:/DEV/_scratch/dgx/dgx-planF-ab.sh D:/DEV/_scratch/dgx/planF-build.sh D:/DEV/_scratch/dgx/planF-summary.py D:/DEV/_scratch/dgx/planF-verify.py D:/DEV/_scratch/upstream-drafts/neon_small_nq_repro.py`. Expected: no output (exit 1), which means no CRLF line endings.

- [ ] **Step 5: No commit**

Run: `git status --short`. Expected: nothing from this task, because the scripts live outside the repo. Nothing has been copied to gn100 yet: Task 10 does that inside the user's run window.

### Task 10 (DGX, last): A/B on gn100, then record it in ADR 0005

This task measures Plan F against the preceding main on gn100 (spec §6 protocol, §7 F acceptance), then records the result in ADR 0005 and in three upstream drafts. It follows Tasks 1–9, and needs the user's explicit go-ahead with a time cap (spec §6).

The acceptance is spec §7 F, verbatim:

> - **F:** gn100, 2.55M IVF (nlist 256, nprobe 16), `--memory 4g`, warm, `OPENBLAS_NUM_THREADS=1` in both arms. The p3 prototype value is in brackets, against base with OPENBLAS=1.
>   1. recall@10 unchanged at the default nprobe [0.993]. Ids and float32 score bits are identical to `IVF_SEARCH_THREADS=1` on seed 42, unfiltered and filtered, including tie order.
>   2. Concurrent QPS (c=8, median of at least 3 runs) is at least 1.5× the re-baselined main, and F's minimum is above base's maximum. p3 measured 324.7 [308.6..381.4] against 189.2 [162.8..207.3].
>   3. Filtered p50 ≤10 ms [6.41, against 36.6].
>   4. Serial p50 better beyond the band [6.50, against 8.44].
>   5. Container memory peak within 50 MB of base [1.82 GB in both].
>   6. `IVF_SEARCH_THREADS=1` reproduces the serial path.
>   7. The run reports the largest IVF list size, against the 32,768-row pooled-path cliff [30,443].
>   - After F, the server limit is the HTTP/event-loop pipeline, about 3.1 ms per request, against an in-process ceiling of 610–662 QPS. Follow-ups outside A–G: two `_drain_scans` batches in flight, and `_hydrate` off the loop.

How each item is measured:
- Item 1: `recall_at_10` from every bench run, plus the probe's `exact` mode on seed 42 (the bench's own 500 queries from `bench/corpus/embed-vecs.npy`). It compares T=12 with `_ShardPool(1)`, which is what `IVF_SEARCH_THREADS=1` builds, on ids and float32 score bits position by position: unfiltered, filtered on `year` (>128 ids), and on a ≤128-id slice of that filter.
- Item 2: `qps_concurrent`, 3 measured runs per arm after a discarded run0, each in a fresh container, so the range includes between-server variance (spec §6).
- Items 3, 4 and 6: `lat_filtered_p50` and `lat_p50` from the same runs. Item 6 compares the `IVF_SEARCH_THREADS=1` arm with base, which is the serial path, within the band.
- Item 5: the container's cgroup v2 `memory.peak`, read after every run (`meta-planF-<label>.json`). bench.py's `mem_under_load_mb` is one `podman stats` sample, not a peak.
- Item 7: the probe's `LISTS` line.
- Every row: host-warm, `--memory 4g`, `sqlite3.sqlite_version` and `OPENBLAS_NUM_THREADS=1`, all recorded per run by the A/B script, and checked by the summary's `labels` line. Probe rows are host-warm, uncapped host processes; each probe file opens with its label line.

Two things are out of scope, and the report says so rather than chasing them:
- After F the limit is the HTTP/event-loop pipeline (about 3.1 ms per request, of which the loop burns 1.2–1.5 ms of CPU). Its follow-ups are two `_drain_scans` batches in flight, `_hydrate` off the event loop, and a cheaper parse of the vector body (p3). None is in Plans A–G.
- SQLite's global memstatus malloc mutex (p2, D15) is a confounder for concurrent numbers. F's pool tasks do not touch SQLite, but the request path does. Both arms must show the same `sqlite_version`. If they differ, the summary fails item 2 rather than attribute the gap to F.

Every `ssh` and `scp` below is written out in full with `-F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes`. Local paths given to `scp` are relative, after a `cd`, because `scp` would read `D:` as a host name. Never run `podman rm -f` on anything but `bench-tv`, never touch `bench-wv` or its volume, and never `pkill -f`.

**Files:**
- Modify: `docs/adr/0005-ivf-fanout-and-upstream-stance.md`: `planF-verify.py` replaces the `Pending:` Verification.
- Modify: `tests/test_ivf_fanout.py` (append a section)
- Outside the repo:
  - create `D:/DEV/_scratch/dgx/planF-shas.env` and `D:/DEV/_scratch/dgx/planF/` (`ab.log`, `results/`, `summary.md`);
  - modify `D:/DEV/_scratch/upstream-drafts/turbovec-search-many.md`, `turbovec-partitioned-ivf.md` and `turbovec-neon-small-nq.md` (a `## DGX data` section).
- On gn100:
  - `~/raggio` (Plan F's tree, per spec §6);
  - `~/planF/` (scripts, `base-src/`, `build.log`, `ab.log`, `results/`);
  - images `localhost/raggio:<BASE_SHA>` and `localhost/raggio:<F_SHA>`;
  - the `bench-tv` container and volume.

**Interfaces:**
- Consumes:
  - Tasks 1–7's commits;
  - Task 8's drafts and repro;
  - Task 9's scripts;
  - on gn100: `~/turborag/bench/corpus`, `~/turborag/bench/gt-2549619-42-d1024.npz`, `~/turborag/bench/fingerprint-turborag-ivf.json` (used by the pre-A bench only), and the `bench-tv` volume holding collection `bench` (2,549,119 rows).
- Produces:
  - ADR 0005's `## Verification`, with the A/B table, the row-label table, the §7 F PASS lines (items 1–7), the seed-42 exactness check and the largest IVF list, the stage attribution, the seed-7 pool-size sweep with the grouped regression guard, the after-F note, and the NEON repro output;
  - the Task 10 test.

- [ ] **Step 1: Ask the user for a run window (time cap)**

Ask, and wait for an explicit answer. Do not run anything on gn100 before it:

> Plan F's DGX A/B needs a run window on gn100. The estimate is about 95 minutes:
> - up to 20 min to build two images, if they are absent;
> - about 50 min for 10 IVF bench runs of ~4.5 min each (one discarded run0, then 3 rounds of base / F / F with `IVF_SEARCH_THREADS=1`), each in a fresh 4 GiB `bench-tv` container with `OPENBLAS_NUM_THREADS=1`;
> - about 20 min for three in-process probes (stage attribution, pool-size sweep, seed-42 exactness check) and a 30 s turbovec repro.
>
> Add about 20 min (115 min in all) for the optional flat no-regression check: one IVF-to-flat detach in a transient 8 GiB container, then 5 flat runs. Add about 6 min if the volume is flat at the start, because run0 then builds the index.
>
> It uses only `bench-tv`, `~/raggio` and `~/planF/`. It replaces `~/raggio` with Plan F's tree (spec §6 deploy), and never touches `bench-wv`.
>
> May I run it? If so, with what time cap, and with or without the flat check?

If the user declines, stop here. ADR 0005 stays `Pending:`, and Tasks 1–9 stand on their own. Note the cap and the start time. If the cap is reached before Step 8 sees `PLANF AB DONE`, go to Step 8's stop command.

- [ ] **Step 2: Pin the SHAs and check the laptop side**

```bash
cd D:/DEV/raggio
git status --short --untracked-files=no
T1=$(git log --format=%H -n 1 --grep='^Fan out IVF shard searches on a per-collection thread pool$')
BASE_SHA=$(git rev-parse --short=12 "$T1^")
F_SHA=$(git rev-parse --short=12 HEAD)
FLAT_CHECK=0  # set to 1 only if the user granted the window with the flat check
printf 'BASE_SHA=%s\nF_SHA=%s\nFLAT_CHECK=%s\n' "$BASE_SHA" "$F_SHA" "$FLAT_CHECK" > D:/DEV/_scratch/dgx/planF-shas.env
cat D:/DEV/_scratch/dgx/planF-shas.env
git log --oneline "$BASE_SHA..$F_SHA"
uv run pytest -q
uv run python D:/DEV/_scratch/dgx/planF-check.py
```

Expected:
- `git status --short --untracked-files=no` prints nothing;
- the log lists exactly Tasks 1–7's seven commits, newest `Record IVF fan-out in ADR 0005 and correct the fixed-cost claim`;
- the suite is all green;
- the check prints `Plan F DGX scripts OK in ...`.

If any of these differ, stop and fix that first: the image is built from the commit, not from the working tree.

- [ ] **Step 3: Pre-flight on gn100 (read-only, under a minute)**

```bash
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 '
uptime; free -m | head -2
pgrep -af "bench/bench[.]py" || echo "no bench.py running"
podman ps -a --format "{{.Names}} {{.Status}} {{.Image}}"
VOL=$(podman volume inspect bench-tv --format "{{.Mountpoint}}") || exit 1
if podman unshare test -d "$VOL/collections/bench/ivf"; then echo "volume: IVF attached"; else echo "volume: flat (run0 builds the index)"; fi
ls -d ~/raggio ~/planF 2>&1'
```

Expected: `no bench.py running`, no `bench-tv` line with status `Up`, and one `volume:` line.

If a bench.py is running, or `bench-tv` is up, that is someone's work in progress. Stop and ask the user; never stop it yourself.

If the volume line errors (there is no `bench-tv` volume), stop and ask: the 2.55M-row volume is a prerequisite, and re-ingesting it takes 31 min outside this window.

- [ ] **Step 4: Stage `~/planF` and copy the scripts**

An existing `~/planF` holds an earlier session's results. It is moved aside, never deleted.

```bash
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 'if [ -e ~/planF ]; then mv ~/planF ~/planF.prev-$(date +%Y%m%d-%H%M%S); fi; mkdir -p ~/planF/results'
cd D:/DEV/_scratch/dgx
scp -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes dgx-planF-ab.sh planF-build.sh gn100:planF/
cd D:/DEV/_scratch/upstream-drafts
scp -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes neon_small_nq_repro.py gn100:planF/
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 'ls -l ~/planF; bash -n ~/planF/dgx-planF-ab.sh && bash -n ~/planF/planF-build.sh && echo SYNTAX_OK'
```

Expected: the three files are listed, then `SYNTAX_OK`.

- [ ] **Step 5: Deploy both trees (spec §6)**

Plan F's tree goes to `~/raggio`, with the corpus link, the ground truth, the fingerprint for the pre-A bench, and the bench venv. The preceding main's tree goes to `~/planF/base-src`, and is used only to build the base image.

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planF-shas.env
git archive "$F_SHA" | ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 'rm -rf ~/raggio && mkdir ~/raggio && tar -x -C ~/raggio'
git archive "$BASE_SHA" | ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 'mkdir ~/planF/base-src && tar -x -C ~/planF/base-src'
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 '
cd ~/raggio || exit 1
ln -sfn ~/turborag/bench/corpus bench/corpus
cp ~/turborag/bench/gt-2549619-42-d1024.npz bench/
cp ~/turborag/bench/fingerprint-turborag-ivf.json bench/fingerprint-raggio-ivf.json
if grep -q "^bench = \[" pyproject.toml; then ~/.local/bin/uv sync --frozen --group bench; else ~/.local/bin/uv sync --frozen; fi 2>&1 | tail -2
.venv/bin/python -c "import raggio.store as s; print(\"F tree OK\", hasattr(s, \"_ShardPool\"))"
test -f bench/ivf_fanout_probe.py && echo "probe present"'
```

Expected: `F tree OK True` and `probe present`.

The fingerprint copy only matters in the pre-A (legacy) bench mode. Plan A's hardened bench ignores it and uses `--fingerprint-dir`, which the A/B script detects by itself.

- [ ] **Step 6: Build both images (detached, then poll)**

`planF-build.sh` skips an image that already exists under its tag.

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planF-shas.env
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 "setsid -f bash -c 'bash ~/planF/planF-build.sh ~/planF/base-src $BASE_SHA && bash ~/planF/planF-build.sh ~/raggio $F_SHA' >>~/planF/build.log 2>&1 </dev/null"
```

Poll every 2 minutes:

```bash
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 'grep -E "^== BUILD" ~/planF/build.log; tail -n 2 ~/planF/build.log'
```

Done when there are two `== BUILD_DONE <tag>` lines, one for `$BASE_SHA` and one for `$F_SHA`. If a `== BUILD_FAILED` line appears, stop and report the tail of `~/planF/build.log` to the user.

- [ ] **Step 7: Launch the A/B (detached)**

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planF-shas.env
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 "setsid -f bash ~/planF/dgx-planF-ab.sh $BASE_SHA $F_SHA $FLAT_CHECK >/dev/null 2>&1 </dev/null"
sleep 60
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 'head -n 30 ~/planF/ab.log'
```

Expected: the log opens with:
- `== PLANF AB START <date> <time> base=<BASE_SHA> f=<F_SHA> flat_check=<0|1> ==`;
- the uptime, memory and podman lines;
- two `image ... id ... digest ...` lines, each followed by `turbovec 1.0.0`;
- `bench client mode: hardened` or `bench client mode: legacy`;
- `[<ts>] ctr up (<BASE_SHA>, mem 4g, ...)`;
- `host venv sqlite_version (probe rows): <version>`.

If it instead ends with `== PLANF AB FAILED: no readable /sys/fs/cgroup/memory.peak in bench-tv ...`, item 5 cannot be measured on this host. Stop and ask the user how to measure it; do not substitute `mem_under_load_mb`.

- [ ] **Step 8: Poll until done (every 5 minutes)**

```bash
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 'grep -E "^== (ivf-|flat-|probe|NEON|PLANF AB)" ~/planF/ab.log | tail -n 4; date'
```

Progress runs as follows:
- `== ivf-base-run0 raggio-ivf`;
- then `ivf-base-run1`, `ivf-f-run1`, `ivf-f1-run1` and so on through `ivf-f1-run3`, each followed by a `labels <label>: host-warm, cap 4g, sqlite_version ..., OPENBLAS_NUM_THREADS=1, memory.peak ...` line;
- `== probe stages`, `== probe fanout`, `== probe exact`, `== NEON small-nq repro`;
- with the flat check, the `flat-*` runs;
- finally `== PLANF AB DONE <ts> ==`.

If `== PLANF AB FAILED: <reason>` appears, go to Step 9 to fetch the log. Then report the reason and the log tail to the user, and stop: do not record anything.

If the time cap is reached first, stop only this session's processes. The bracketed patterns keep `pgrep` from matching its own command line. Then report to the user, and stop:

```bash
ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100 '
for p in $(pgrep -f "planF/dgx-planF-a[b][.]sh"); do kill "$p"; done
sleep 2
for p in $(pgrep -f "bench/bench[.]py|ivf_fanout_probe[.]py|neon_small_nq_rep[r]o[.]py"); do kill "$p"; done
podman stop -t 60 bench-tv
podman ps -a --filter name=bench-tv'
```

- [ ] **Step 9: Fetch the results**

```bash
mkdir -p D:/DEV/_scratch/dgx/planF
cd D:/DEV/_scratch/dgx/planF
scp -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100:planF/ab.log .
scp -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes -r gn100:planF/results .
ls results
```

Expected in `results/`:
- `results-planF-ivf-base-run0.json`, plus `results-planF-ivf-{base,f,f1}-run{1,2,3}.json`, each with its `.md` and its `meta-planF-ivf-<arm>-run<N>.json`;
- with the flat check, `results-planF-flat-base-run0` and `results-planF-flat-{base,f}-run{1,2}`, with their meta files;
- `probe-stages.txt`, `probe-fanout.txt`, `probe-exact.txt` and `neon-repro.txt`.

- [ ] **Step 10: Summarize against spec §7 F**

```bash
cd D:/DEV/raggio
uv run python D:/DEV/_scratch/dgx/planF-summary.py D:/DEV/_scratch/dgx/planF/results > D:/DEV/_scratch/dgx/planF/summary.md; echo "exit $?"
cat D:/DEV/_scratch/dgx/planF/summary.md
```

Expected: `exit 0` and no `- FAIL:` line. There are 8 `- PASS:` lines, or 11 with the flat check:
- `F1`: recall@10 unchanged across all arms, and the `EXACT seed 42 T=12 vs IVF_SEARCH_THREADS=1: ... : PASS` line;
- `F2`: F's median concurrent QPS at least 1.5× base's, F's minimum above base's maximum, and one `sqlite_version` for both arms;
- `F3`: F's filtered p50 at most 10 ms;
- `F4`: F's serial p50 better than base's beyond the band;
- `F5`: F's median cgroup memory peak within 50 MB of base's;
- `F6`: the `IVF_SEARCH_THREADS=1` arm within the band of base, on serial p50 and on concurrent QPS;
- `F7`: `largest IVF list N rows, P% of the 32,768-row pooled-path cliff`;
- `labels`: every measured IVF run host-warm, cap 4g, `OPENBLAS_NUM_THREADS=1`, one `sqlite_version` per arm;
- with the flat check, three flat no-regression lines.

The file also holds the row-label table and the guard line `... chosen pool size T (default here 12); GUARD grouped vs plain at T: OK|REVISIT`.

If the exit code is 1, or any line reads `- FAIL:`, stop. Report `summary.md` to the user, and do not record, commit or re-run to chase a pass. A failed §7 F item is a finding for the user. Three findings go to the user even when every line passes:
- F7 ends with `AT OR ABOVE THE CLIFF`;
- the guard reads `REVISIT`;
- the chosen pool size T differs from 12. T is already the smallest pool within noise of the fastest, so any difference is a finding (Global Constraints: the default does not change here).

- [ ] **Step 11: Write the failing test**

Append to `tests/test_ivf_fanout.py`:

```python
# ---- ADR 0005 records the DGX A/B (Plan F Task 10) ----


def test_adr0005_records_the_dgx_verification():
    adr = Path("docs/adr/0005-ivf-fanout-and-upstream-stance.md").read_text(encoding="utf-8")
    ver = adr.split("## Verification", 1)[1]
    assert "Pending:" not in ver
    # spec §7 F items 1-7, the §6 row labels, the guard, and the after-F note
    for needle in ("IVF_SEARCH_THREADS=1", "QPS concurrent", "Filtered p50", "Recall@10",
                   "Memory peak", "sqlite_version", "OPENBLAS_NUM_THREADS=1", "### §7 F acceptance",
                   "- PASS: F1", "- PASS: F7", "EXACT seed 42", "LISTS largest",
                   "GUARD grouped vs plain", "Batching guard:", "3.1 ms per request"):
        assert needle in ver, needle
    assert "- FAIL:" not in ver
```

- [ ] **Step 12: Run the test to verify it fails**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `1 failed, 33 passed`. `test_adr0005_records_the_dgx_verification` fails with `AssertionError: assert 'Pending:' not in '...'`.

- [ ] **Step 13: Record the A/B in ADR 0005 and in the drafts**

```bash
cd D:/DEV/raggio
source D:/DEV/_scratch/dgx/planF-shas.env
uv run python D:/DEV/_scratch/dgx/planF-verify.py D:/DEV/_scratch/dgx/planF "$BASE_SHA" "$F_SHA" D:/DEV/_scratch/upstream-drafts
```

Expected: `ADR 0005 Verification recorded (<date>); guard OK at T=<n>; drafts updated`, or `guard REVISIT`.

On a REVISIT, the ADR records that the grouped arm beat plain fan-out on seed 7, and audience batching stays unshipped. D3 is settled by p3; reopening it is the user's call, and this plan does not make that change.

Where the seed-7 pool size differs from the default, the ADR says so as a finding for the user. The default stays `min(12, os.cpu_count())` (spec §4.2).

- [ ] **Step 14: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ivf_fanout.py -q`. Expected: `34 passed`.

Run: `uv run pytest -q`. Expected: all green (on the sandbox base of 46: `80 passed`).

Run: `uv run --group docs mkdocs build --strict -d "$(mktemp -d)"`. Expected: exit 0.

Run: `uv run python D:/DEV/_scratch/upstream-drafts/check_drafts.py D:/DEV/_scratch/upstream-drafts`. Expected: `6 drafts and the repro OK in ...`.

Run: `git diff --stat`. Expected: exactly two files, `docs/adr/0005-ivf-fanout-and-upstream-stance.md` and `tests/test_ivf_fanout.py`.

- [ ] **Step 15: Commit**

```bash
git add docs/adr/0005-ivf-fanout-and-upstream-stance.md tests/test_ivf_fanout.py
git commit -m "Record Plan F's DGX A/B in ADR 0005

gn100, arXiv 2,549,119 x 1024, seed 42: base vs Plan F at the default
IVF_SEARCH_THREADS and at 1, 3 interleaved rounds per arm in fresh 4 GiB
containers, host-warm, OPENBLAS_NUM_THREADS=1 in every arm, sqlite_version
recorded per run. Spec 7 F items 1-7 all pass. Adds the seed-42 exactness
check, the largest IVF list against the 32,768-row cliff, the stage
attribution, the seed-7 pool-size sweep with the grouped regression guard,
and the NEON repro. Raw results: D:/DEV/_scratch/dgx/planF/.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 16: Report to the user**

Report the following:
- the `summary.md` tables (A/B and row labels) and its PASS lines for items 1–7;
- the largest IVF list against the 32,768-row cliff (item 7);
- the guard (OK or REVISIT at T), and the seed-7 pool size against the default of 12;
- that after F the server limit is the HTTP/event-loop pipeline (about 3.1 ms per request), and that its follow-ups (two `_drain_scans` batches in flight, `_hydrate` off the loop, a cheaper vector-body parse) are out of scope here;
- that concurrent numbers carry the SQLite memstatus mutex (p2, D15), with each arm's `sqlite_version`;
- how long the session ran against the cap;
- the state left on gn100: `bench-tv` stopped, both images kept, and `~/planF` kept;
- the six upstream drafts in `D:/DEV/_scratch/upstream-drafts/`, three of them with DGX data.

Say plainly that the drafts are not posted. Posting them, if at all, is the maintainer's call (spec D7).

---

## Self-Review

### Spec coverage

| Spec item | Where | How it is checked |
|---|---|---|
| §1 item 3, §2 row 6: parallel IVF shard fan-out | Task 1 | Bit-identical results (ids, score bits, tie order) against a serial reference and across pool sizes, unfiltered (`test_tie_order_is_identical_across_pool_sizes` and the other Task 1 tests) and filtered with >128 and ≤128 ids (`test_filtered_tie_order_is_identical_across_pool_sizes`); calls run on pool threads |
| §2 row 6: plain per-(query, shard) fan-out by default | Task 1 | `_IvfIndex.search` builds one nq=1 task per (query, shard) |
| §2 row 6, D3: the allowlist is intersected inside each (query, shard) task, not up front | Tasks 1, 2 | `run` cuts `self._intersect(allowlist, j)` itself and returns `None` on an empty slice; the ≤128 owners path hands over pre-cut slices; pin 3 and the filtered tie-order test |
| §2 row 6: shard-side allowlist intersection, allowlists sorted at cache fill | Task 2 | `_intersect` in both directions against `np.intersect1d`; unsorted and duplicate allowlists; the >128-in-one-shard pin |
| §2 row 6: R2 and R2b generation counters | Tasks 3, 4 | Orphaned-scan tests that interleave a write between the snapshot and the cache store |
| §2 row 6: dirty-shard sync | Task 5 | Spy-counted shard syncs: one-row job, reload-then-stop, fresh dir, moved row (pin), failed shard |
| §3 D1: ADR 0005 | Tasks 7, 10 | Task 7's docs test; Task 10's Verification test |
| §3 D1: 0001:48 partly superseded, 0001:30 and 0002 corrected | Task 7 | `0001:48` in ADR 0005; `ADR 0005` in ADRs 0001 and 0002; no `0.4 ms` in `store.py`, `concepts.md` or `indexing.md` |
| §3 D3: settled by p3, plain q1 fan-out, no audience batching | Tasks 1, 6, 7, 9, 10 | Only the plain path exists in `store.py`; the probe keeps `grouped_search` as a regression guard whose `GUARD` line (`OK` or `REVISIT`) `planF-verify.py` records; a `REVISIT` goes to the user and ships nothing (Global Constraints); ADR 0005 records "not shipped (spec D3, settled by p3)" |
| §3 D15: `sqlite3.sqlite_version` on every row; the memstatus mutex confounder | Tasks 9, 10 | `meta-planF-<label>.json` per run, the probe label lines, the summary's row-label table and `labels` line; F2 fails if the arms differ in `sqlite_version`; the Verification's confounder paragraph |
| §3 D16, §4.2: `OPENBLAS_NUM_THREADS=1` is A's image `ENV` | Tasks 7, 9, 10 | This plan never edits the `Dockerfile`; every container and probe gets `OPENBLAS_NUM_THREADS=1` explicitly; the summary's `labels` line fails otherwise (planF-check's `blas` case); ADR 0005's Consequences |
| §3 D6, D7: upstream drafts outside the repo, never posted, no fork | Task 8 | `check_drafts.py`; Step 5 checks `git status` |
| §4.1: F owns `_IvfIndex`, `_vector_ids` and `_allow_cache` | Tasks 1–5 | Only these, plus the `Collection` plumbing for the pool, are edited |
| §4.1: D2 consumes the dirty-shard API | Task 5 | `dirty_shards -> frozenset[int]` and `sync() -> int` are tested directly |
| §4.2: `IVF_SEARCH_THREADS` in `Settings`, passed via `CollectionManager` to `Collection` | Task 1 | `test_settings_ivf_search_threads`, `test_manager_passes_setting_to_collection` |
| §4.2: default `min(12, os.cpu_count())` | Tasks 1, 7 | `test_settings_ivf_search_threads` expects `min(12, os.cpu_count() or 1)`; Task 7's test requires `min(12, os.cpu_count())` in ADR 0005, no `min(32` there, and `min(12, CPUs)` in the README and getting-started env tables; `planF-check.py` asserts `planF-verify.py` writes no `min(32` |
| §4.2: never set `RAYON_NUM_THREADS=1`; the 32,768-row pooled-path cliff | Tasks 1, 6, 7, 9, 10 | The `_ShardPool` docstring; ADR 0005's cliff row (Task 7's test asserts `32,768` and `RAYON_NUM_THREADS=1`); the probe's `LISTS` line in every mode; nothing in the repo or the DGX scripts sets `RAYON_NUM_THREADS` (only Task 8's standalone repro, with a caveat) |
| §4.2: tests in their own module | Tasks 1–7, 10 | Everything is in `tests/test_ivf_fanout.py` |
| §4.3: single writer, job journal done after its sync | Task 5 | Shard syncs still run inside the job, and a failing shard sync raises (so the job is not `done`) and leaves the shard dirty |
| §4.3: ghost reconciliation, rescoring, calibration unchanged | Tasks 1–5 | The full suite stays green after every task |
| §6: DGX protocol | Tasks 9, 10 | Step 1 asks for a run window with a time cap; ssh only via the NVIDIA Sync config with `BatchMode=yes`; `podman rm -f` names only `bench-tv`; `bench-wv` and `~/turborag` are never written; no sudo; deploy per §6; a fresh 4 GiB container per run (between-server variance), discarded run0, 3 interleaved IVF rounds with median and band; `OPENBLAS_NUM_THREADS=1` in every arm; regime, cap, `sqlite_version` and `OPENBLAS_NUM_THREADS` on every row; the one IVF-to-flat transition; `setsid -f` plus log polling; seed 7 for tunables, seed 42 for the published numbers |
| §7 F item 1: recall@10 unchanged [0.993]; bit identity to `IVF_SEARCH_THREADS=1` on seed 42, unfiltered and filtered, with tie order | Tasks 1, 6, 10 | The Task 1 tie-order tests; the probe's `exact` mode (`test_probe_exact_smoke`); summary line `F1` |
| §7 F item 2: concurrent QPS ≥1.5× base (median of ≥3), F's min above base's max | Task 10 | Summary line `F2` (planF-check's `lose` case fails it) |
| §7 F item 3: filtered p50 ≤10 ms | Task 10 | Summary line `F3` |
| §7 F item 4: serial p50 better beyond the band | Task 10 | Summary line `F4` |
| §7 F item 5: container memory peak within 50 MB of base | Tasks 9, 10 | cgroup `memory.peak` per run in the meta files, checked readable before run0; summary line `F5` |
| §7 F item 6: `IVF_SEARCH_THREADS=1` reproduces the serial path | Tasks 1, 10 | `test_threads_one_is_the_serial_path` (no thread, same result); summary line `F6` |
| §7 F item 7: the largest IVF list against the 32,768-row cliff [30,443] | Tasks 6, 10 | The probe's `LISTS` line and `exact`'s `max_list`/`cliff` keys; summary line `F7` |
| §7 F: the after-F note (HTTP/event-loop limit, follow-ups outside A–G) | Tasks 7, 10 | Global Constraints and Task 10 state it; `planF-verify.py` writes it into the Verification; Task 10's test checks `3.1 ms per request` |

### Placeholder scan

`grep -nE "TBD|TODO|FIXME|similar to Task|fill in|\{\{[A-Za-z0-9_-]+\}\}" docs/superpowers/plans/2026-09-28-ivf-fanout-and-upstream.md` matches one line: this sentence, which quotes the pattern. Before the p3 revision, the build script also asserted that every snippet was used, that no marker was left unfilled, and that all 80 code fences were closed. The p3 revision was edited by hand; its fences were re-checked by reading, and the blocks that contain fences of their own use four backticks.

Angle-bracket tokens do appear, but only in two roles:
- values that a command prints at run time and a later step reads back: `<BASE_SHA>`, `<F_SHA>`, `<date>`, `<ts>`, `<n>`, `<reason>`;
- usage lines of the scripts themselves.

Every step that needs one of these values computes it (Task 10 Step 2 writes `planF-shas.env`) or reads it from the previous step's output.

### Type and name consistency

| Name | Defined | Used by |
|---|---|---|
| `default_ivf_search_threads() -> int` | Task 1, `config.py` | `Settings`, `Collection.__init__`, the probe |
| `Settings.ivf_search_threads: int` | Task 1 | `CollectionManager.touch` → `Collection(..., ivf_search_threads=)` |
| `_ShardPool(threads, name)`, `.threads`, `.map`, `.close` | Task 1 | `_IvfIndex`, `Collection.stop`, the probe |
| `_IvfIndex(centroids, shards, nprobe, pool=None)`; `train(..., pool=None)`; `load(..., pool=None)`; `._pool` | Task 1 | the attach path, `Collection.__init__`, the probe; Tasks 4 and 5 re-state `__init__` |
| `_sorted_ids(ids) -> np.ndarray` | Task 2 | `_IvfIndex.search`, `Collection._vector_ids` |
| `_IvfIndex._intersect(allow, j)` | Task 2 | `_IvfIndex.search` |
| `Collection._allow_gen`, `_allow_lock`, `_invalidate_allow()` | Task 3 | the three write paths, `_vector_ids` |
| `_IvfIndex._id_gen`, `_id_lock`, `_shard_written(j)` | Task 4 | `add_with_ids`, `remove`, `_shard_ids`; Task 5's `sync` |
| `_IvfIndex._dirty`, `dirty_shards`, `sync(directory) -> int` | Task 5 | Plan D2 |
| `bench/ivf_fanout_probe.py main(argv)`, modes `stages`, `fanout`, `exact` | Task 6 | Task 9's `dgx-planF-ab.sh`, Task 10 |
| `POOLED_CLIFF = 32_768`, `largest_list(ivf) -> int`, the `LISTS largest N rows, P% of the 32,768-row pooled-path cliff` line | Task 6 | `run_exact`'s `max_list`/`cliff`; `planF-summary.py` (`F7`), `planF-check.py` |
| `bench_queries(path, limit, n, seed)`, `filter_allowlist(col, key) -> (val, allow, sql_ms)` | Task 6 | `run_exact`, `run_stages` |
| `fanout`'s `... chosen pool size T (default here D); GUARD grouped vs plain at T: ...` line, ending `OK` or `REVISIT`, and `out["guard"]` | Task 6 | `test_probe_fanout_smoke`, `planF-summary.py`, `planF-verify.py`, `planF-check.py` |
| `exact`'s `EXACT seed S T=T vs IVF_SEARCH_THREADS=1: ...` line, ending `PASS` or `FAIL`, and its dict keys | Task 6 | `test_probe_exact_smoke`, `planF-summary.py` (`F1`), `planF-check.py` |
| ADR 0005 `## Verification` / `Pending:` | Task 7 | `planF-verify.py`, Task 10's test |
| `results-planF-<phase>-<arm>-run<N>.json` | Task 9, `dgx-planF-ab.sh` | `planF-summary.py` |
| `meta-planF-<phase>-<arm>-run<N>.json` (`label`, `regime`, `cap`, `sqlite_version`, `openblas_num_threads`, `memory_peak_mb`) | Task 9, `dgx-planF-ab.sh`'s `meta()` | `planF-summary.py` (`F5`, `labels`), `planF-check.py` |
| `probe-stages.txt`, `probe-fanout.txt`, `probe-exact.txt`, `neon-repro.txt` | Task 9, `dgx-planF-ab.sh` | `planF-summary.py`, `planF-verify.py` |

Commit subjects are unique. Task 10 Step 2 finds Task 1's commit by its exact subject, `Fan out IVF shard searches on a per-collection thread pool`.

### Validated in the sandbox

The pre-p3 version of this plan was validated on a Plan F series applied to 19a8a2e, in `D:/DEV/_scratch/plan-sandbox/plan-F-rf`, with the tasks applied cumulatively. Every code block in Tasks 1–7 and in Task 10's test was then taken verbatim from those commits by the build script.

**The p3 revision was NOT re-run in the sandbox.** Its edits were written by hand and reasoned against the 19a8a2e source and p3's measurements only. The implementer is the first to run them:
- the in-task allowlist cut in `_IvfIndex.search` (Task 1 Step 3g, Task 2 Step 3c) and pin 3's new mutation;
- the default `min(12, ...)` in `config.py`, the tests, ADR 0005 and the env tables;
- the `_ShardPool` cliff docstring;
- Task 1's `test_filtered_tie_order_is_identical_across_pool_sizes`;
- Task 6's guard (in place of the gate), `exact` mode, `LISTS` line, `bench_queries`, `filter_allowlist` and `test_probe_exact_smoke`;
- Task 7's new assertions and ADR text;
- Task 8's NEON draft and repro text;
- Task 9's scripts (OPENBLAS in every arm, the meta labels and `memory.peak`, the `exact` probe, items F1–F7, the `labels` line) and `planF-check.py`;
- Task 10's test needles and counts.

The counts in Global Constraints and in each task's steps are derived from the pre-p3 counts plus the two added tests, not observed.

- **RED/GREEN counts, pre-p3.** Each task's new tests failed before the task and passed after it. The `test_ivf_fanout.py` counts were:
  - RED: 14 failed; 3 failed / 16 passed; 2/19; 2/21; 5/23; 2/28; 1/30;
  - GREEN, full suite: 60, 65, 67, 69, 74, 76, 77.

  The p3 revision adds Task 1's filtered tie-order test and Task 6's `test_probe_exact_smoke`, which gives the expected counts 15, 20, 22, 24, 29, 32, 33 and 34 (full suite 61, 66, 68, 70, 75, 78, 79 and 80 on the sandbox base of 46).
- **Task 2's regression pins, pre-p3.** The two tests that pass before Task 2 failed on their named mutations. Pin 3's mutation has since moved into Task 1's `run` and is traced, not re-run.
- **Review Focus pins, pre-p3.** Each pin passed on the task's code and failed on its mutation.
- **Task 1 on Plan B's `stop()`.** Task 1 was also applied on top of Plan B's `stop()` shape, which gives this plan's Step 3j snippet.
- **Docs build, pre-p3.** `mkdocs build --strict` passed.
- **Tasks 8 and 9 checks, pre-p3.** Both checks passed on the pre-p3 drafts and scripts, and the NEON repro ran on x86.
- **Task 10's test, pre-p3.** It failed on the pending ADR. After `planF-verify.py` recorded a synthetic A/B, it passed, and so did the full suite (then 78).

### Not validatable in the sandbox, and why

- **Everything measured on gn100:** the DGX numbers, the §7 F verdicts, the seed-7 pool size, the grouped regression guard, the seed-42 exactness on the real corpus and the largest list size. The rules for this plan forbid touching the DGX outside Task 10's user-granted window. Task 9 exercises the summary and verify scripts on synthetic results only. The pre-p3 A/B script was syntax-checked, then dry-run on the laptop with stubbed `podman`, `curl`, `bench.py` and probe, three ways:
  - hardened bench mode with the flat check, which gave 8 PASS lines through the pre-p3 `planF-summary.py`;
  - legacy mode starting from a flat volume;
  - a measured run that rebuilt the index, which aborts with `PLANF AB FAILED: ... rebuilt or re-ingested`.

  The p3 additions to it (`-e OPENBLAS_NUM_THREADS=1`, `meta()`, the `memory.peak` pre-check, the `exact` probe, `podman unshare env ...`) were not dry-run. Two host facts are assumed and checked at run time: that `podman exec bench-tv` can read the container's cgroup v2 `/sys/fs/cgroup/memory.peak` under rootless podman (the script fails before run0 if not), and that `podman unshare env OPENBLAS_NUM_THREADS=1 ...` reaches the probe (the probe label line records it). The real `ssh`/`scp` commands, the podman builds and the fingerprint copy were not run.
- **aarch64/NEON behaviour.** The laptop is x86 (AVX2). p3 measured the NEON small-batch cost on gn100 (nq=2 2.05×/2.61×, nq=3 4.89×/1.95×, nq=8 0.87×/0.82× per query against nq=1), and grouping lost to plain there. Task 10 re-measures both on Plan F's code.
- **Interplay with Plans B, C, D1 and E.** Those plans are not merged anywhere yet. The sandbox base is 19a8a2e. Only Task 1 was also replayed on Plan B's sandbox branch. That is why the tasks above anchor every edit on quoted text; the next section lists what this plan expects those plans to leave in place.
- **The real 2.55M-row corpus.** The laptop numbers used synthetic 262,144 × 1024 vectors, and synthetic 2.55M-id allowlists for the intersection timings.

### Cross-plan assumptions

- **Plan B.** `Collection.stop()` has the shape shown in Task 1 Step 3j:
  - under the write lock: cancel the worker, run `_sync_index`, set `_closed`, close the read connections;
  - then, outside the lock, close the embedder.

  Task 1 appends the pool close after all of it.
- **Plan C.**
  - `CollectionManager.touch()` still constructs `Collection(...)` in one call, possibly inside `asyncio.to_thread`, so Task 1 Step 3k adds one keyword.
  - The 8 GiB detach workaround in `dgx-planF-ab.sh` is harmless after C.
- **Plan E.**
  - `Settings.native_bm25` and a `native_bm25` keyword on `Collection.__init__` may exist, and `ivf_search_threads` goes after them.
  - E's ADR 0004 exists, because ADR 0005 cites it.
  - E may add a `NATIVE_BM25` env-table row, and Task 7 appends after it.
- **Plan D.**
  - D1 leaves the three `self._allow_cache.clear()` calls in place, possibly moved. Task 3 finds them by text.
  - D2 consumes `dirty_shards` and `sync() -> int` (spec §4.1). If D2 merged first, it calls the existing `_sync_index`, and Task 5 keeps that working.
- **Plan A.**
  - A owns `bench/bench.py`, and its hardened mode is detected by the string `"--fingerprint-dir"`.
  - If A adds a `bench` dependency group, Task 10 uses it.
  - A owns the image's `ENV OPENBLAS_NUM_THREADS=1` (D16). This plan never edits the `Dockerfile`; its DGX arms pass `-e OPENBLAS_NUM_THREADS=1` anyway, so a base image without A's line still matches F.
  - The image's `python` can `import sqlite3` and read `/sys/fs/cgroup/memory.peak` (the A/B script's `meta()`).
- **The probe (Task 6).** It imports `_filter_sql`, `_normalize`, `_rows_by_id`, `SearchIn`, `_ShardPool`, `Collection`, `CollectionConfig`, `_IvfIndex` and `IVF_DEFAULT_NPROBE`, and relies on `Collection._rdb`, `_rescore_k`, `_rescore_rows` and `_hydrate` keeping their 19a8a2e signatures.
- **Line numbers.** Every line number quoted in this plan is for 19a8a2e.
