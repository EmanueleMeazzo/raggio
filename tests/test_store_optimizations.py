"""Regression checks for the search-latency optimizations (allowlist skip, token
pruning, micro-batcher, RW lock) and the crash-consistency fixes around them."""
import asyncio
import tempfile
from pathlib import Path

import numpy as np
import pytest

import sys
sys.path.insert(0, "src")
import raggio.store as store
from raggio.store import Collection, CollectionConfig, _RWLock, _fold, open_meta_db
from turbovec import IdMapIndex


def make_collection(tmp_path, dim=8):
    return Collection(CollectionConfig("t", dim, 4, None, None, None), Path(tmp_path), lambda: None)


def vec(seed, dim=8):
    return np.random.default_rng(seed).standard_normal(dim).tolist()


def test_fold_matches_unicode61():
    assert _fold("Café") == "cafe"
    assert _fold("Ünïcode") == "unicode"


def test_fold_tokens_matches_unicode61():
    # unicode61 treats '_' as a separator and NFKD folds compatibility forms
    assert store._fold_tokens("Café_Bar x²") == ["cafe", "bar", "x2"]


def test_reconcile_evicts_ghosts_even_when_counts_match(tmp_path):
    db = open_meta_db(tmp_path / "meta.db")
    for i in (11, 12):
        db.execute(
            "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
            " VALUES (?,?,'d1','chunk',0,'hello','{}',1)", (i, f"c{i}"),
        )
    db.commit()
    db.close()
    idx = IdMapIndex(dim=8, bit_width=4)
    idx.add_with_ids(
        np.random.default_rng(0).standard_normal((2, 8)).astype(np.float32),
        np.array([1, 2], dtype=np.uint64),  # equal count, disjoint from records {11,12}
    )
    idx.sync(str(tmp_path / "index.tvim"))
    col = make_collection(tmp_path)
    assert len(col.index) == 0  # ghosts evicted despite matching counts


def test_duplicate_external_id_in_one_payload_leaves_no_ghost(tmp_path):
    col = make_collection(tmp_path)
    asyncio.run(col._process_job({"documents": [
        {"doc_id": "dA", "chunks": [{"id": "X", "text": "one", "vector": vec(1)}]},
        {"doc_id": "dB", "chunks": [{"id": "X", "text": "two", "vector": vec(2)}]},
    ]}))
    assert len(col.index) == col.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_prune_keeps_rarest_matching_token(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    asyncio.run(col._process_job({"documents": [
        {"doc_id": "d1", "chunks": [{"id": "c1", "text": "two words here", "vector": vec(3)}]},
    ]}))
    # zero budget: everything is over budget, yet a df>0 token must survive and a
    # df-0 token (typo) must not consume the guarantee
    monkeypatch.setattr(store, "FTS_SCAN_BUDGET_MIN_ROWS", 0)
    kept, toks = col._prune_common("zzzunknown two")
    assert "two" in kept and toks == ["zzzunknown", "two"]


def test_batched_scan_matches_solo(tmp_path):
    col = make_collection(tmp_path)
    docs = [{"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "text": f"chunk {i}", "vector": vec(i)}]}
            for i in range(30)]
    asyncio.run(col._process_job({"documents": docs}))

    async def run():
        q = np.array([vec(99)], dtype=np.float32)
        q /= np.linalg.norm(q)
        solo = await col.search("vector", q, None, 5, "chunks", None, None)
        many = await asyncio.gather(
            *(col.search("vector", q, None, 5, "chunks", None, None) for _ in range(8))
        )
        assert all([h["id"] for h in m] == [h["id"] for h in solo] for m in many)

    asyncio.run(run())


def test_rwlock_writer_cancelled_while_waiting_wakes_readers():
    async def run():
        lock = _RWLock()
        r1_in, r1_go = asyncio.Event(), asyncio.Event()

        async def holder():
            async with lock.read():
                r1_in.set()
                await r1_go.wait()

        t1 = asyncio.create_task(holder())
        await r1_in.wait()

        async def writer():
            async with lock.write():
                pass

        tw = asyncio.create_task(writer())
        await asyncio.sleep(0.01)  # writer queued: new readers now blocked

        done = asyncio.Event()

        async def late_reader():
            async with lock.read():
                done.set()

        t2 = asyncio.create_task(late_reader())
        await asyncio.sleep(0.01)
        tw.cancel()
        r1_go.set()
        await asyncio.wait_for(done.wait(), timeout=2)  # hangs without the fix
        await asyncio.gather(t1, t2)

    asyncio.run(run())


def test_df_cache_serves_repeat_lookups_and_refreshes_on_churn(tmp_path):
    col = make_collection(tmp_path)
    asyncio.run(col._process_job({"documents": [
        {"doc_id": "d1", "chunks": [{"id": "c1", "text": "hello world", "vector": vec(1)}]},
    ]}))
    col._df_cache_churn = 0  # the ingest above counted as churn; start clean
    assert col._prune_common("hello world")[0] == ["hello", "world"]
    col._rdb = lambda: (_ for _ in ()).throw(AssertionError("df must come from cache"))
    assert col._prune_common("hello world")[0] == ["hello", "world"]  # cache hit, no db touch
    # small churn on a large corpus must NOT invalidate (25% relative arm)
    col._df_cache["hello"] = 10**9
    col.indexed_counts = {"chunk": 50_000}
    col._df_cache_churn = 2000  # > 1000 absolute, < 12500 relative
    assert col._prune_common("hello world")[0] == ["world"]  # stale huge df still cached -> pruned
    # enough churn invalidates wholesale, even when the row count is unchanged
    col._df_cache_churn = 20_000
    del col._rdb  # restore the real method for the refreshed lookup
    col._prune_common("hello world")
    assert col._df_cache["hello"] < 10**9
    assert col._df_cache_churn == 0


def test_calibrates_once_after_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CAL_THRESHOLD", 6)
    monkeypatch.setattr(store, "CAL_SAMPLE", 4)
    col = make_collection(tmp_path)
    docs = lambda lo, hi: [{"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "text": "t", "vector": vec(i)}]}
                           for i in range(lo, hi)]
    asyncio.run(col._process_job({"documents": docs(0, 4)}))
    assert col.index.calibration_state == "uncalibrated"  # below threshold
    asyncio.run(col._process_job({"documents": docs(4, 8)}))
    assert col.index.calibration_state == "calibrated"
    assert col._cal_reservoir is None  # one shot: disarmed forever


def test_reload_below_threshold_stays_armed_and_calibrates(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CAL_THRESHOLD", 6)
    monkeypatch.setattr(store, "CAL_SAMPLE", 4)
    col = make_collection(tmp_path)
    asyncio.run(col._process_job({"documents": [
        {"doc_id": "d0", "chunks": [{"id": "c0", "text": "t", "vector": vec(0)}]},
    ]}))
    asyncio.run(col.stop())
    col2 = make_collection(tmp_path)  # eviction/restart below threshold must NOT disarm
    assert col2._cal_reservoir is not None and col2._cal_seen == 1
    docs = [{"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "text": "t", "vector": vec(i)}]}
            for i in range(1, 12)]
    asyncio.run(col2._process_job({"documents": docs}))
    assert col2.index.calibration_state == "calibrated"


def test_reload_at_threshold_never_calibrates(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CAL_THRESHOLD", 6)
    monkeypatch.setattr(store, "CAL_SAMPLE", 4)
    col = make_collection(tmp_path)
    docs = [{"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "text": "t", "vector": vec(i)}]}
            for i in range(8)]
    col._cal_reservoir = None  # simulate a pre-calibration-era collection filling up
    asyncio.run(col._process_job({"documents": docs}))
    asyncio.run(col.stop())
    col2 = make_collection(tmp_path)  # >= threshold uncalibrated: late re-encode loses recall
    assert col2._cal_reservoir is None
    asyncio.run(col2._process_job({"documents": [
        {"doc_id": "dx", "chunks": [{"id": "cx", "text": "t", "vector": vec(99)}]},
    ]}))
    assert col2.index.calibration_state == "uncalibrated"


def test_single_bulk_job_calibrates_at_threshold_not_after(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CAL_THRESHOLD", 6)
    monkeypatch.setattr(store, "CAL_SAMPLE", 4)
    col = make_collection(tmp_path)
    calls = []

    class Spy:  # C-extension methods are read-only; wrap to observe calibrate timing
        def __init__(self, inner):
            self._inner = inner

        def calibrate(self, s):
            calls.append(len(self._inner))
            return self._inner.calibrate(s)

        def __getattr__(self, n):
            return getattr(self._inner, n)

        def __len__(self):
            return len(self._inner)

    col.index = Spy(col.index)
    docs = [{"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "text": "t", "vector": vec(i)}]}
            for i in range(20)]
    asyncio.run(col._process_job({"documents": docs}))
    assert col.index.calibration_state == "calibrated"
    assert calls == [6]  # calibrated at the threshold slice, not after all 20 rows


def test_reupserts_do_not_skew_reservoir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CAL_THRESHOLD", 100)
    col = make_collection(tmp_path)
    job = {"documents": [{"doc_id": "d0", "chunks": [{"id": "c0", "text": "t", "vector": vec(0)}]}]}
    asyncio.run(col._process_job(job))
    asyncio.run(col._process_job(job))  # same external id: replacement, not a new vector
    asyncio.run(col._process_job(job))
    assert col._cal_seen == 1  # only the fresh insert was fed to the reservoir


def test_concurrent_enqueue_with_worker_loses_nothing(tmp_path):
    # regression: with loosely-shared or split write connections this either lost a
    # journaled job (202 + job_id returned, row gone) or starved into
    # "database is locked"; single-writer discipline must survive the overlap
    async def run():
        col = make_collection(tmp_path)
        col.start_worker()

        def job(i):
            return {"documents": [{"doc_id": f"d{i}", "chunks": [
                {"id": f"c{i}_{j}", "text": f"t {i} {j}", "vector": vec(i * 10 + j)}
                for j in range(10)]}]}

        async def enqueuer(base):
            return [await col.enqueue(job(base + i)) for i in range(10)]

        ids = await asyncio.gather(*(enqueuer(b) for b in (0, 100, 200)))
        assert len({j for sub in ids for j in sub}) == 30  # all job ids distinct
        for _ in range(300):
            if col.pending_jobs() == 0:
                break
            await asyncio.sleep(0.05)
        assert not col._worker.done()  # worker alive (a lock error would kill it)
        assert col._rdb().execute("SELECT COUNT(*) FROM records").fetchone()[0] == 300
        assert col._rdb().execute(
            "SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0] == 30
        await col.stop()

    asyncio.run(run())


def test_fresh_meta_db_has_incremental_autovacuum(tmp_path):
    # auto_vacuum is silently ignored once the file is initialized (journal_mode=WAL
    # does that), so a wrong pragma order leaks every page the job backlog ever used
    db = open_meta_db(tmp_path / "meta.db")
    assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # 2 = incremental


def test_job_payload_pages_are_returned_after_processing(tmp_path):
    # the journaled payload is bulky and transient; after the job completes its pages
    # must leave the freelist (incremental_vacuum frees per STEP — a bare execute()
    # frees one page and left a full-corpus meta.db at 7+ GB, 93% freelist)
    async def run():
        col = make_collection(tmp_path)
        col.start_worker()
        await col.enqueue({"documents": [
            {"doc_id": "d", "chunks": [{"id": f"c{j}", "text": "x" * 5000, "vector": vec(j)}
                                       for j in range(50)]}]})
        for _ in range(200):
            if col.pending_jobs() == 0:
                break
            await asyncio.sleep(0.05)
        assert col.db.execute("PRAGMA freelist_count").fetchone()[0] <= 1
        await col.stop()

    asyncio.run(run())


def test_closed_collection_fails_closed(tmp_path):
    col = make_collection(tmp_path)
    col._closed = True
    with pytest.raises(RuntimeError):
        col._rdb()

# ---- fp16 rescore + two-stage BM25 (ADR 0003) ----


class _FakeIndex:
    """Duck-types the IdMapIndex slice the search path uses, returning a fixed
    (deliberately wrong) candidate order so the fp16 rescore has work to do."""

    def __init__(self, ids, scores):
        self._ids = np.array(ids, dtype=np.uint64)
        self._scores = np.array(scores, dtype=np.float32)

    def __len__(self):
        return len(self._ids)

    def search(self, queries, k, allowlist=None, **kw):
        n = min(k, len(self._ids))
        return (np.tile(self._scores[:n], (len(queries), 1)),
                np.tile(self._ids[:n], (len(queries), 1)))


def _ingest_five(col):
    docs = [{"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "text": f"chunk {i}", "vector": vec(i)}]}
            for i in range(5)]
    asyncio.run(col._process_job({"documents": docs}))


def _exact_order(col, q):
    rows = col.db.execute("SELECT id, vec FROM vecs ORDER BY id").fetchall()
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1)
    mat = mat.astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    return [rows[i][0] for i in np.argsort(-(mat @ q[0]))]


def test_rescore_corrects_quantized_order(tmp_path):
    col = make_collection(tmp_path)
    _ingest_five(col)
    # fake index returns ids in id order with made-up descending "quantized" scores
    col.index = _FakeIndex([1, 2, 3, 4, 5], [0.9, 0.8, 0.7, 0.6, 0.5])
    q = np.array([vec(99)], dtype=np.float32)
    q /= np.linalg.norm(q)
    hits = asyncio.run(col.search("vector", q, None, 5, "chunks", None, None))
    assert [h["id"] for h in hits] == [f"c{rid - 1}" for rid in _exact_order(col, q)]
    assert all(-1.0001 <= h["score"] <= 1.0001 for h in hits)  # exact cosines, not fakes


def test_rescore_missing_blob_keeps_quantized_score(tmp_path):
    col = make_collection(tmp_path)
    _ingest_five(col)
    col.db.execute("DELETE FROM vecs WHERE id=1")
    col.db.commit()
    col.index = _FakeIndex([1, 2, 3, 4, 5], [2.0, 0.8, 0.7, 0.6, 0.5])  # 2.0 beats any cosine
    q = np.array([vec(99)], dtype=np.float32)
    q /= np.linalg.norm(q)
    hits = asyncio.run(col.search("vector", q, None, 5, "chunks", None, None))
    assert len(hits) == 5
    assert hits[0]["id"] == "c0" and hits[0]["score"] == 2.0  # kept, quantized score intact


def test_two_stage_restores_full_query_ranking(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    docs = [
        {"doc_id": "dA", "chunks": [{"id": "A", "text": "rare common common", "vector": vec(1)}]},
        {"doc_id": "dB", "chunks": [{"id": "B", "text": "rare", "vector": vec(2)}]},
    ]
    docs += [{"doc_id": f"dc{i}", "chunks": [
        {"id": f"c{i}", "text": f"common filler{i} pad{i}", "vector": vec(10 + i)}]}
        for i in range(3)]
    docs += [{"doc_id": f"df{i}", "chunks": [
        {"id": f"f{i}", "text": f"noise{i} words{i} pad{i}", "vector": vec(20 + i)}]}
        for i in range(7)]
    asyncio.run(col._process_job({"documents": docs}))
    # zero budget prunes down to the rarest token ("rare"). Stage 2 must still rank by
    # the FULL query: the doc containing both terms beats the shorter rare-only doc,
    # which wins under kept-tokens-only ranking (the pre-ADR-0003 behavior).
    monkeypatch.setattr(store, "FTS_SCAN_BUDGET_MIN_ROWS", 0)
    kept, toks = col._prune_common("rare common")
    assert kept == ["rare"] and len(toks) == 2  # precondition: pruning dropped a token
    hits = asyncio.run(col.search("text", None, "rare common", 2, "chunks", None, None))
    assert [h["id"] for h in hits] == ["A", "B"]


def test_python_bm25_matches_fts5_ranking(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    texts = {
        "p1": "alpha beta gamma delta",
        "p2": "alpha alpha alpha beta",
        "p3": "alpha epsilon zeta eta theta iota kappa",
        "p4": "beta beta beta beta mu nu xi",
    }
    for i in range(4):  # fillers keep df(alpha/beta) < N/2 so IDF stays positive
        texts[f"fill{i}"] = f"one{i} two{i} three{i} four{i} five{i}"
    docs = [{"doc_id": k, "chunks": [{"id": k, "text": t, "vector": vec(n)}]}
            for n, (k, t) in enumerate(texts.items())]
    asyncio.run(col._process_job({"documents": docs}))
    monkeypatch.setattr(store, "SDM_WEIGHT", 0.0)  # FTS5 has no proximity term
    fts = [r[0] for r in col._rdb().execute(
        "SELECT rowid FROM records_fts WHERE records_fts MATCH ? ORDER BY rank",
        ['"alpha" OR "beta"'])]
    cand = col._rdb().execute("SELECT id, text FROM records").fetchall()
    ids, _ = col._bm25_rescore("alpha beta", list(cand), 10)
    assert ids[: len(fts)] == fts


def test_unpruned_query_skips_stage_two(tmp_path, monkeypatch):
    col = make_collection(tmp_path)
    asyncio.run(col._process_job({"documents": [
        {"doc_id": "d1", "chunks": [{"id": "c1", "text": "hello world", "vector": vec(1)}]},
    ]}))
    monkeypatch.setattr(
        col, "_bm25_rescore",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stage 2 must not run")),
    )
    # default budget floor (1000 rows) >> corpus: nothing is pruned -> single FTS5 query
    hits = asyncio.run(col.search("text", None, "hello world", 5, "chunks", None, None))
    assert [h["id"] for h in hits] == ["c1"]
