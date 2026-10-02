"""IVF shard fan-out (ADR 0005): pooled per-(query, shard) searches return the serial
result bit-for-bit (tie order included), the pool's lifecycle follows
Collection.stop(), and the IVF_SEARCH_THREADS knob reaches every collection."""
import asyncio
import os
import threading
import time
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

# the deadline of each wait for something that must happen. Generous on purpose: with
# every CPU busy, a thread hand-off on the free-threaded build can take a second or more
# (a contended PyMutex yields the CPU up to 40 times before it parks)
WAIT_SECONDS = 360


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
        t.join(WAIT_SECONDS)
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
        return await asyncio.wait_for(asyncio.gather(*searches), WAIT_SECONDS)

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
        assert self.release.wait(WAIT_SECONDS)
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
    assert held.entered.wait(WAIT_SECONDS)  # a pool thread sits in shard 0; the rest of the 32 queue
    closer = threading.Thread(target=ivf._pool.close)
    closer.start()
    closer.join(0.1)  # close() is now blocked waiting on the pool
    held.release.set()
    t.join(WAIT_SECONDS)
    closer.join(WAIT_SECONDS)
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
        assert self.release.wait(WAIT_SECONDS)
        return res


def test_orphaned_id_probe_cannot_cache_stale_shard_ids():
    ivf, _ = build_ivf()
    j, new_id = 2, 70_000
    gate = GatedProbe(ivf.shards[j])
    ivf.shards[j] = gate
    got = []
    t = threading.Thread(target=lambda: got.append(ivf._shard_ids(j)))
    t.start()
    assert gate.started.wait(WAIT_SECONDS)  # the probe holds a pre-write snapshot of shard j
    ivf.add_with_ids(ivf.centroids[j : j + 1].copy(), np.array([new_id], dtype=np.uint64))
    assert gate.inner.contains(new_id)  # routed to shard j
    gate.release.set()
    t.join(WAIT_SECONDS)
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


# ---- ADR 0005 records the DGX A/B (Plan F Task 10) ----


def test_adr0005_records_the_dgx_verification():
    adr = Path("docs/adr/0005-ivf-fanout-and-upstream-stance.md").read_text(encoding="utf-8")
    ver = adr.split("## Verification", 1)[1]
    assert "Pending:" not in ver
    # the repo is public: no local or remote home paths in the record
    assert "D:/" not in ver and "/home/" not in ver
    # spec §7 F items 1-7, the §6 row labels, the guard, the after-F note, and the
    # record-only c=16 cores-busy table (p4)
    for needle in ("IVF_SEARCH_THREADS=1", "QPS concurrent", "Filtered p50", "Recall@10",
                   "Memory peak", "sqlite_version", "OPENBLAS_NUM_THREADS=1", "### §7 F acceptance",
                   "- PASS: F1", "- PASS: F7", "EXACT seed 42", "LISTS largest",
                   "GUARD grouped vs plain", "Batching guard:", "3.1 ms per request",
                   "Cores busy at c=16", "taskset -c 5"):
        assert needle in ver, needle
    # item 6 failed on gn100 and is recorded as failed, with the maintainer's override;
    # it is the only failed item
    fails = [ln.split()[2] for ln in ver.splitlines() if ln.startswith("- FAIL: ")]
    assert fails == ["F6"]
    assert "- OVERRIDE: F6, " in ver
