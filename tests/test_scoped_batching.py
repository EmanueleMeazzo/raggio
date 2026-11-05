"""Scoped scan parity, compatibility grouping, resource bounds and lifecycle."""
import asyncio
import json
from pathlib import Path
import threading

import numpy as np
import pytest

import raggio.store as store

WAIT = 30


async def fixture_col(path, dim=32, bit_width=4, ivf=False):
    col = store.Collection(store.CollectionConfig('t', dim, bit_width, None, None, None),
                           Path(path), lambda: None)
    rng = np.random.default_rng(931)
    sizes = [4] * 300 + [250, 250]
    vectors = store._normalize(rng.standard_normal((sum(sizes) + len(sizes), dim), dtype=np.float32))
    vectors[:8] = vectors[0]  # real ties within and across documents
    docs, start = [], 0
    for d, size in enumerate(sizes):
        docs.append({'doc_id': f'd{d}', 'summary': {'text': 'planet summary', 'vector': vectors[start],
            'metadata': {'group': d % 6, 'nested': ['Σ', {'flag': True}]}},
            'chunks': [{'id': f'd{d}c{i}', 'position': i, 'text': 'planet star ' * (i % 3 + 1),
                'vector': vectors[start+i+1], 'metadata': {'group': d % 6, 'nested': ['Σ', {'flag': True}]}}
                for i in range(size)]})
        start += size + 1
    await col._process_job({'documents': docs})
    if ivf:
        await col._process_job({'op': 'attach_index', 'nlist': 4, 'nprobe': 2})
    return col, vectors[:8]


async def solo_scoped(col, qvec, n, nprobe, allow, depth):
    kw = {'nprobe': nprobe} if isinstance(col.index, store._IvfIndex) else {}
    return await asyncio.to_thread(col._search_allowed, qvec, n, allow, **kw)


async def request(col, qvec, k=10, scope='summaries', filt=None, mode='vector', nprobe=None,
                  fusion='rrf', alpha=.5):
    return await col.search(mode, qvec, 'planet' if mode != 'vector' else None,
                            k, scope, filt, None, nprobe=nprobe, fusion=fusion, alpha=alpha)


async def compare(col, queries, monkeypatch):
    # Establish warm allowlists with the original independent scan path.
    with monkeypatch.context() as ref:
        ref.setattr(store.Collection, '_scan_scoped', solo_scoped)
        expected = [await request(col, **query) for query in queries]
    actual = await asyncio.wait_for(asyncio.gather(*(request(col, **q) for q in queries)), WAIT)
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    assert col._scoped_scans == {}
    return actual


@pytest.mark.parametrize('dim', [24, 40, 1024, 3072])
@pytest.mark.parametrize('bit_width', [2, 4])
@pytest.mark.parametrize('ivf', [False, True])
def test_scoped_batches_preserve_exact_scores_order_and_query_options(tmp_path, monkeypatch, dim, bit_width, ivf):
    async def run():
        col, vectors = await fixture_col(tmp_path, dim, bit_width, ivf)
        original, shapes = col._search_rescored, []
        def observed(mat, ns, **kw):
            shapes.append(len(mat))
            return original(mat, ns, **kw)
        try:
            queries = [{'qvec': vectors[i:i+1]} for i in range(8)] + [
                {'qvec': vectors[:1], 'k': 5, 'nprobe': 1},
                {'qvec': vectors[:1], 'k': 10, 'nprobe': 1},
                {'qvec': vectors[:1], 'k': 30, 'nprobe': 1},
                {'qvec': vectors[:1], 'mode': 'hybrid', 'k': 5, 'nprobe': 1},
                {'qvec': vectors[:1], 'mode': 'hybrid', 'scope': 'chunks',
                 'filt': {'group': {'in': [1, 2]}}, 'fusion': 'relative', 'alpha': .25},
                {'qvec': vectors[:1], 'mode': 'hybrid', 'fusion': 'relative', 'alpha': 0},
                {'qvec': vectors[:1], 'scope': 'chunks', 'k': 5, 'filt': {'doc_id': 'd0'}},
                {'qvec': vectors[:1], 'scope': 'both', 'filt': {'doc_id': 'missing'}},
            ]
            with monkeypatch.context() as spy:
                spy.setattr(col, '_search_rescored', observed)
                await compare(col, queries, monkeypatch)
            assert max(shapes) > 1  # parity actually covers coalesced work
        finally:
            await col.stop()
    asyncio.run(run())


@pytest.mark.parametrize('ivf', [False, True])
@pytest.mark.parametrize('bit_width', [2, 4])
def test_scoped_batches_keep_legacy_and_unindexed_rows_compatible(tmp_path, monkeypatch, ivf, bit_width):
    async def run():
        col, vectors = await fixture_col(tmp_path, bit_width=bit_width, ivf=ivf)
        try:
            col.db.execute("DELETE FROM vecs WHERE id=(SELECT id FROM records WHERE external_id='d0')")
            col.db.execute("UPDATE vecs SET vec=x'00' WHERE id=(SELECT id FROM records WHERE external_id='d1')")
            rid = col.db.execute("SELECT id FROM records WHERE external_id='d2'").fetchone()[0]
            col.db.execute('UPDATE records SET indexed=0, position=NULL WHERE id=?', (rid,))
            col.index.remove(rid)
            col.db.commit()
            col._invalidate_allow()
            await compare(col, [{'qvec': vectors[i:i+1]} for i in range(8)], monkeypatch)
        finally:
            await col.stop()
    asyncio.run(run())


def test_scoped_cohorts_obey_row_and_payload_limits(tmp_path, monkeypatch):
    async def run():
        col, vectors = await fixture_col(tmp_path)
        shapes, original = [], col._search_rescored
        def observed(mat, ns, **kw):
            shapes.append(len(mat))
            return original(mat, ns, **kw)
        try:
            await request(col, vectors[:1])
            monkeypatch.setattr(store, 'SCOPED_SCAN_BATCH_ROWS', 3)
            monkeypatch.setattr(store, 'SCOPED_SCAN_BATCH_BYTES', 2 * col._rescore_k(10) * col.cfg.dim * 4)
            monkeypatch.setattr(col, '_search_rescored', observed)
            actual = await asyncio.gather(*(request(col, vectors[:1]) for _ in range(9)))
            assert all(row == actual[0] for row in actual)
            assert max(shapes) == 2 and len(shapes) == 5
            assert col._scoped_scans == {}
        finally:
            await col.stop()
    asyncio.run(run())


def test_scoped_groups_keep_depth_probe_and_allowlist_separate(tmp_path, monkeypatch):
    async def run():
        col, vectors = await fixture_col(tmp_path, ivf=True)
        calls, original = [], col._search_rescored
        def observed(mat, ns, **kw):
            calls.append((len(mat), tuple(col._rescore_k(n) for n in ns), kw['nprobe'],
                          tuple(kw['allowlist'].tolist())))
            return original(mat, ns, **kw)
        try:
            queries = []
            for k, npb, scope in ((5, 1, 'summaries'), (10, 1, 'summaries'), (30, 1, 'summaries'),
                                  (10, 2, 'summaries'), (10, 1, 'chunks')):
                queries.extend([{'qvec': vectors[:1], 'k': k, 'nprobe': npb, 'scope': scope}] * 2)
            with monkeypatch.context() as ref:
                ref.setattr(store.Collection, '_scan_scoped', solo_scoped)
                expected = [await request(col, **q) for q in queries]
            monkeypatch.setattr(col, '_search_rescored', observed)
            actual = await asyncio.gather(*(request(col, **q) for q in queries))
            assert actual == expected
            assert len(calls) == 4 and sorted(c[0] for c in calls) == [2, 2, 2, 4]
            assert all(len(set(depths)) == 1 for _, depths, _, _ in calls)
        finally:
            await col.stop()
    asyncio.run(run())


def test_cold_empty_and_small_allowlists_bypass_scoped_queue(tmp_path, monkeypatch):
    async def run():
        col, vectors = await fixture_col(tmp_path)
        async def unexpected(*a, **kw):
            raise AssertionError('cold/empty/small allowlist must retain independent search')
        try:
            monkeypatch.setattr(col, '_scan_scoped', unexpected)
            await request(col, vectors[:1])  # cold large summary scope
            for _ in range(2):
                assert await request(col, vectors[:1], filt={'doc_id': 'missing'}) == []
                await request(col, vectors[:1], scope='chunks', filt={'doc_id': 'd0'})
        finally:
            await col.stop()
    asyncio.run(run())


def test_scoped_results_have_independent_fresh_metadata(tmp_path, monkeypatch):
    async def run():
        col, vectors = await fixture_col(tmp_path)
        try:
            rows = await compare(col, [{'qvec': vectors[:1]}] * 4, monkeypatch)
            rows[0][0]['metadata']['nested'][1]['flag'] = False
            assert rows[1][0]['metadata']['nested'][1]['flag'] is True
            doc = rows[1][0]['doc_id']
            await col.patch_metadata(doc, {'revision': 1}, True)
            fresh = await compare(col, [{'qvec': vectors[:1]}] * 4, monkeypatch)
            assert next(h for h in fresh[0] if h['doc_id'] == doc)['metadata']['revision'] == 1
        finally:
            await col.stop()
    asyncio.run(run())


def test_scoped_worker_failure_reaches_all_waiters_and_cleans_state(tmp_path, monkeypatch):
    async def run():
        col, vectors = await fixture_col(tmp_path)
        try:
            await request(col, vectors[:1])
            original = col._search_rescored
            def fail(*a, **kw):
                raise RuntimeError('scoped worker failed')
            with monkeypatch.context() as broken:
                broken.setattr(col, '_search_rescored', fail)
                results = await asyncio.wait_for(asyncio.gather(
                    *(request(col, vectors[:1]) for _ in range(8)), return_exceptions=True), WAIT)
            assert all(isinstance(r, RuntimeError) and 'scoped worker failed' in str(r) for r in results)
            assert col._scoped_scans == {}
            await request(col, vectors[:1])
        finally:
            await col.stop()
    asyncio.run(run())


def test_partial_cancellation_keeps_writer_excluded_until_remaining_readers_finish(tmp_path, monkeypatch):
    async def run():
        col, vectors = await fixture_col(tmp_path)
        started, release = threading.Event(), threading.Event()
        original = col._search_rescored
        def blocked(*a, **kw):
            started.set()
            assert release.wait(WAIT)
            return original(*a, **kw)
        try:
            await request(col, vectors[:1])
            monkeypatch.setattr(col, '_search_rescored', blocked)
            queries = [asyncio.create_task(request(col, vectors[:1])) for _ in range(4)]
            assert await asyncio.to_thread(started.wait, WAIT)
            queries[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await queries[0]
            writer = asyncio.create_task(col.patch_metadata('d0', {'revision': 2}, True))
            await asyncio.sleep(0)
            assert not writer.done()
            release.set()
            before = await asyncio.wait_for(asyncio.gather(*queries[1:]), WAIT)
            assert all('revision' not in h['metadata'] for row in before for h in row)
            await asyncio.wait_for(writer, WAIT)
            assert col._scoped_scans == {}
        finally:
            release.set()
            await col.stop()
    asyncio.run(run())


@pytest.mark.parametrize('stop', [False, True])
def test_all_cancelled_scoped_workers_do_not_retain_queues_after_write_or_stop(tmp_path, monkeypatch, stop):
    async def run():
        col, vectors = await fixture_col(tmp_path)
        started, release = threading.Event(), threading.Event()
        original = col._search_rescored
        def blocked(*a, **kw):
            started.set()
            assert release.wait(WAIT)
            return original(*a, **kw)
        try:
            await request(col, vectors[:1])
            monkeypatch.setattr(col, '_search_rescored', blocked)
            queries = [asyncio.create_task(request(col, vectors[:1])) for _ in range(8)]
            assert await asyncio.to_thread(started.wait, WAIT)
            drains = [state[1] for state in col._scoped_scans.values()]
            for q in queries:
                q.cancel()
            results = await asyncio.gather(*queries, return_exceptions=True)
            assert all(isinstance(r, asyncio.CancelledError) for r in results)
            if stop:
                await asyncio.wait_for(col.stop(), WAIT)
            else:
                await asyncio.wait_for(col.patch_metadata('d0', {'revision': 1}, True), WAIT)
            release.set()
            await asyncio.wait_for(asyncio.gather(*drains), WAIT)
            assert col._scoped_scans == {}
        finally:
            release.set()
            await col.stop()
    asyncio.run(run())


def test_ivf_scoped_routing_matches_single_queries_at_centroid_boundaries(tmp_path):
    async def run():
        col, _ = await fixture_col(tmp_path, dim=40, ivf=True)
        try:
            ivf = col.index
            rng = np.random.default_rng(937)
            q = rng.standard_normal((128, col.cfg.dim), dtype=np.float32)
            difference = ivf.centroids[0] - ivf.centroids[1]
            q -= (q @ difference / (difference @ difference))[:, None] * difference
            q = store._normalize(q)
            allow = np.sort(np.array([r[0] for r in col.db.execute(
                "SELECT id FROM records WHERE type='summary' AND indexed=1")], dtype=np.uint64))
            scores, ids = ivf.search(q, 50, allowlist=allow, nprobe=1, _single_query_routing=True)
            for i in range(len(q)):
                ss, si = ivf.search(q[i:i+1], 50, allowlist=allow, nprobe=1)
                valid = np.isfinite(scores[i])
                assert np.array_equal(ids[i][valid], si[0])
                assert np.array_equal(scores[i][valid].view(np.uint32), ss[0].view(np.uint32))
        finally:
            await col.stop()
    asyncio.run(run())


@pytest.mark.parametrize('summary_rows', [350, 33000])
def test_flat_scoped_candidate_boundary_ties_match_solo_below_and_above_pool_threshold(tmp_path, monkeypatch, summary_rows):
    async def run():
        col = store.Collection(store.CollectionConfig('t', 8, 4, None, None, None),
                               Path(tmp_path), lambda: None)
        rng = np.random.default_rng(941)
        q = store._normalize(rng.standard_normal((1, 8), dtype=np.float32))
        col.index.calibrate(store._normalize(rng.standard_normal((256, 8), dtype=np.float32)))
        col._cal_reservoir = None
        try:
            # More identical scores than the 50-candidate cutoff; the larger case
            # also crosses turbovec's 32,768-row pooled-search threshold.
            await col._process_job({'documents': [{'doc_id': f'd{i}',
                'summary': {'text': 'tie', 'vector': q[0]},
                'chunks': [{'id': f'c{i}', 'text': 'tie', 'vector': q[0]}]}
                for i in range(summary_rows)]})
            rows = await compare(col, [{'qvec': q}] * 8, monkeypatch)
            assert all(rows[0] == row for row in rows)
        finally:
            await col.stop()
    asyncio.run(run())
