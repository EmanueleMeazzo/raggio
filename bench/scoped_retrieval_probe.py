"""Paired Sofia-shaped retrieval experiments on generated local collections.

Prototype hooks are confined to this benchmark. No corpus or embedding service is
needed. Responses are checked byte for byte; timing excludes preparation/profiling.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import gc
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import textwrap
import time
import tracemalloc
from unittest.mock import patch

import numpy as np
import orjson
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import raggio.store as store
import raggio.app as app_module
from raggio.config import Settings


PRODUCTION_VECTOR_IDS = store.Collection._vector_ids
PRODUCTION_SCOPED_SCAN = store.Collection._scan_scoped
ORIGINAL_SEARCH = store.Collection.search


async def reference_vector_ids(col, qvec, n, scope, filt, nprobe=None):
    """Replay main 6327693's independent filtered path; shared engine stays current."""
    if len(col.index) == 0:
        return [], []
    other = {'chunks': 'summary', 'summaries': 'chunk'}.get(scope)
    if not filt and not (other and col.indexed_counts.get(other, 0)):
        return await col._scan_batched(qvec, n, nprobe)
    def run():
        key = f'{scope}|{json.dumps(filt, sort_keys=True)}'
        allow = col._allow_cache.get(key)
        if allow is None:
            gen = col._allow_gen
            where, params = store._filter_sql(scope, filt)
            with col._reading() as db:
                ids = [r[0] for r in db.execute(f'SELECT id FROM records WHERE {where}', params)]
            allow = np.sort(np.array(ids, dtype=np.uint64))
            with col._allow_lock:
                if gen == col._allow_gen:
                    if len(col._allow_cache) >= 8:
                        col._allow_cache.pop(next(iter(col._allow_cache)))
                    col._allow_cache[key] = allow
        if len(allow) == 0:
            return [], []
        kw = {'nprobe': nprobe} if isinstance(col.index, store._IvfIndex) else {}
        return col._search_allowed(qvec, n, allow, **kw)
    return await asyncio.to_thread(run)


ORIGINAL_VECTOR_IDS = reference_vector_ids


async def independent_scoped(col, qvec, n, nprobe, allow, depth):
    kw = {'nprobe': nprobe} if isinstance(col.index, store._IvfIndex) else {}
    return await asyncio.to_thread(col._search_allowed, qvec, n, allow, **kw)


async def production_scoped(col, qvec, n, nprobe, allow, depth):
    # Both arms install a wrapper outside the clocks, including on control paths.
    return await PRODUCTION_SCOPED_SCAN(col, qvec, n, nprobe, allow, depth)


async def scoped_batch(col, qvec, n, scope, filt, nprobe=None):
    other = {'chunks': 'summary', 'summaries': 'chunk'}.get(scope)
    if not filt and not (other and col.indexed_counts.get(other, 0)):
        return await ORIGINAL_VECTOR_IDS(col, qvec, n, scope, filt, nprobe)
    key = f'{scope}|{json.dumps(filt, sort_keys=True)}'
    allow = col._allow_cache.get(key)
    if allow is None or len(allow) <= col._rescore_k(n):
        return await ORIGINAL_VECTOR_IDS(col, qvec, n, scope, filt, nprobe)
    fut = asyncio.get_running_loop().create_future()
    if not hasattr(col, '_probe_scoped_queue'):
        col._probe_scoped_queue, col._probe_scoped_task = [], None
    effective_probe = (nprobe or col.index.nprobe) if isinstance(col.index, store._IvfIndex) else None
    col._probe_scoped_queue.append((qvec, n, effective_probe, allow, fut))
    if col._probe_scoped_task is None or col._probe_scoped_task.done():
        col._probe_scoped_task = asyncio.create_task(drain_scoped(col))
    return await fut


async def drain_scoped(col):
    while col._probe_scoped_queue:
        batch, col._probe_scoped_queue = col._probe_scoped_queue[:16], col._probe_scoped_queue[16:]
        groups = {}
        for item in batch:
            q, n, npb, allow, fut = item
            if not fut.done():
                groups.setdefault((id(allow), col._rescore_k(n), npb), []).append(item)
        for group in groups.values():
            try:
                mat = np.vstack([item[0] for item in group])
                ns = [item[1] for item in group]
                kw = {'allowlist': group[0][3]}
                if group[0][2] is not None:
                    kw['nprobe'] = group[0][2]
                results = await asyncio.to_thread(col._search_rescored, mat, ns, **kw)
                for item, result in zip(group, results):
                    if not item[4].done():
                        item[4].set_result(result)
            except Exception as exc:
                for item in group:
                    if not item[4].done():
                        item[4].set_exception(exc)


def offloop_search():
    source = textwrap.dedent(inspect.getsource(ORIGINAL_SEARCH))
    before = 'hits = self._hydrate(ids, scores)'
    assert source.count(before) == 1
    source = source.replace(before, 'hits = await asyncio.to_thread(self._hydrate, ids, scores)')
    namespace = vars(store).copy()
    exec(compile(source, __file__, 'exec'), namespace)
    return namespace['search']


async def prepare(settings, rows, dim, ivf):
    manager = store.CollectionManager(settings)
    rng = np.random.default_rng(927)
    try:
        await manager.create_collection('bench', dim, 4, None, None, None)
        col = await manager.touch('bench')
        vectors = store._normalize(rng.standard_normal((rows, dim), dtype=np.float32))
        docs = []
        for start in range(0, rows, 20):
            d = start // 20
            docs.append({'doc_id': f'd{d}', 'summary': {
                'vector': vectors[start], 'text': f'Scientific summary topic{d}',
                'metadata': {'group': d % 10, 'nested': {'tags': ['Σ', d]}}},
                'chunks': [{'id': f'c{i}', 'position': i-start, 'vector': vectors[i],
                    'text': f'scientific retrieval topic{d} café Σ漢字 ' * 12,
                    'metadata': {'group': d % 10, 'nested': {'tags': ['Σ', i]}}}
                    for i in range(start, min(start+20, rows))]})
        await col._process_job({'documents': docs})
        if ivf:
            with patch.object(store, 'IVF_MIN_ROWS', 1):
                await col._process_job({'op': 'attach_index', 'nlist': 32, 'nprobe': 16})
    finally:
        await manager.shutdown()


def post(client, raw):
    response = client.post('/collections/bench/search', content=raw,
        headers={'x-api-key': 'benchmark', 'content-type': 'application/json'})
    assert response.status_code == 200, response.text
    return response


async def profile(col, vec, case):
    timing = {}
    def wrap(name, original):
        def run(*a, **kw):
            wall = time.perf_counter()
            result = original(*a, **kw)
            timing.setdefault(name, []).append((time.perf_counter()-wall)*1000)
            return result
        return run
    # Wall times are nested, not additive; a separate diagnostic, never in endpoint timing.
    with patch.object(col, '_fp16_vecs', wrap('fp16_fetch_decode_normalize', col._fp16_vecs)), \
         patch.object(col, '_hydrate', wrap('hydrate', col._hydrate)), \
         patch.object(col, '_search_allowed', wrap('allowed_search_total', col._search_allowed)):
        for _ in range(20):
            await col.search('vector', vec, None, case['k'], case.get('scope', 'chunks'), case.get('filter'), None)
    return {name: {'calls': len(values), 'p50_ms': statistics.median(values)}
            for name, values in timing.items()}


async def cohort_diagnostics(col, vecs, case, method, search):
    fetched, shapes = [], []
    fetch, rescore = col._fp16_vecs, col._search_rescored
    def counted(ids):
        fetched.append(len(ids))
        return fetch(ids)
    def shaped(mat, ns, **kw):
        shapes.append(len(mat))
        return rescore(mat, ns, **kw)
    async def run():
        return await asyncio.gather(*(col.search('vector', vec[None, :], None,
            case['k'], case.get('scope', 'chunks'), case.get('filter'), None) for vec in vecs))
    with patch.object(store.Collection, '_vector_ids', method), patch.object(store.Collection, 'search', search):
        expected = await run()
        with patch.object(col, '_fp16_vecs', counted), patch.object(col, '_search_rescored', shaped):
            actual = await run()
        assert actual == expected
        gc.collect()
        tracemalloc.start()
        actual = await run()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert actual == expected
    return {'vector_fetch_calls': len(fetched), 'requested_vector_rows': sum(fetched),
            'scan_cohort_sizes': shapes, 'store_temporary_peak_bytes': peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=20000)
    parser.add_argument('--dim', type=int, default=1024)
    parser.add_argument('--pairs', type=int, default=5)
    parser.add_argument('--iterations', type=int, default=20)
    parser.add_argument('--batches', type=int, default=5)
    parser.add_argument('--concurrency', type=int, nargs='+', default=[8])
    parser.add_argument('--flat', action='store_true')
    parser.add_argument('--arms', nargs='+', choices=['batch', 'offloop', 'production'], default=['production'])
    parser.add_argument('--baseline', choices=['shared', 'main'], default='shared',
                        help='Shared request path with coalescing bypassed, or replay the main vector method')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.rows < 640 or args.dim < 8 or args.dim % 8 or min(args.pairs, args.iterations, args.batches, *args.concurrency) < 1:
        parser.error('rows >= 640, dim divisible by 8 and positive repetitions/concurrency required')
    searches = {'baseline': ORIGINAL_SEARCH}
    methods = {'baseline': ORIGINAL_VECTOR_IDS}
    if 'batch' in args.arms:
        searches['batch'], methods['batch'] = ORIGINAL_SEARCH, scoped_batch
    if 'offloop' in args.arms:
        searches['offloop'], methods['offloop'] = offloop_search(), ORIGINAL_VECTOR_IDS
    if 'production' in args.arms:
        searches['production'], methods['production'] = ORIGINAL_SEARCH, PRODUCTION_VECTOR_IDS
    if args.baseline == 'shared':
        methods['baseline'] = PRODUCTION_VECTOR_IDS
    handlers = {arm: production_scoped if arm == 'production' else independent_scoped for arm in searches}
    @contextmanager
    def patch_arm(arm):
        with patch.object(store.Collection, 'search', searches[arm]), \
             patch.object(store.Collection, '_vector_ids', methods[arm]), \
             patch.object(store.Collection, '_scan_scoped', handlers[arm]):
            yield
    cases = {
        'summary-k10': {'mode': 'vector', 'scope': 'summaries', 'k': 10},
        'document-k5': {'mode': 'vector', 'scope': 'chunks', 'k': 5, 'filter': {'doc_id': 'd0'}},
        'metadata-k10': {'mode': 'vector', 'scope': 'chunks', 'k': 10, 'filter': {'group': 3}},
        'plain-k10': {'mode': 'vector', 'scope': 'both', 'k': 10},
    }
    vecs = store._normalize(np.random.default_rng(929).standard_normal((args.iterations, args.dim), dtype=np.float32))
    runs, profiles = {}, {}
    with tempfile.TemporaryDirectory(prefix='raggio-scoped-') as directory:
        settings = Settings()
        settings.data_dir, settings.root_api_key = directory, 'benchmark'
        asyncio.run(prepare(settings, args.rows, args.dim, not args.flat))
        managers = []
        def capture_manager(*a, **kw):
            manager = store.CollectionManager(*a, **kw)
            managers.append(manager)
            return manager
        with patch.object(app_module, 'CollectionManager', capture_manager), \
             TestClient(app_module.create_app(settings)) as client:
            for label, case in cases.items():
                bodies = [orjson.dumps({**case, 'query': {'vector': np.round(vec, 5)}},
                          option=orjson.OPT_SERIALIZE_NUMPY) for vec in vecs]
                for c in dict.fromkeys([1, *args.concurrency]):
                    rounds = []
                    if c == 1:
                        for arm in searches:
                            with patch_arm(arm):
                                for raw in bodies:
                                    post(client, raw)
                        for pair in range(args.pairs):
                            samples = {arm: [] for arm in searches}
                            for repeat in range(args.batches):
                                for i, raw in enumerate(bodies):
                                    outputs = []
                                    order = list(searches) if (pair+repeat+i) % 2 == 0 else list(searches)[::-1]
                                    for arm in order:
                                        with patch_arm(arm):
                                            wall, cpu = time.perf_counter(), time.process_time()
                                            response = post(client, raw)
                                            samples[arm].append(((time.perf_counter()-wall)*1000,
                                                                (time.process_time()-cpu)*1000))
                                        outputs.append(response.content)
                                    assert all(output == outputs[0] for output in outputs), (label, pair, i)
                            rounds.append({arm: {'qps': 1000/statistics.mean(t for t, _ in values),
                                'cpu_per_request_ms': statistics.mean(cpu for _, cpu in values),
                                'p50_ms': statistics.median(t for t, _ in values),
                                'p95_ms': float(np.percentile([t for t, _ in values], 95))}
                                for arm, values in samples.items()})
                        runs[f'{label}-c{c}'] = rounds
                        print(json.dumps({'case': label, 'concurrency': c, 'rounds': rounds}), flush=True)
                        continue
                    with ThreadPoolExecutor(max_workers=c) as pool:
                        for arm in searches:
                            with patch_arm(arm):
                                list(pool.map(lambda raw: post(client, raw), bodies))
                        for pair in range(args.pairs):
                            arms = list(searches) if pair % 2 == 0 else list(searches)[::-1]
                            row, outputs = {}, []
                            for arm in arms:
                                with patch_arm(arm):
                                    wall, cpu = time.perf_counter(), time.process_time()
                                    lat, responses = [], []
                                    def timed(raw):
                                        t0 = time.perf_counter()
                                        response = post(client, raw)
                                        return (time.perf_counter()-t0)*1000, response.content
                                    for _ in range(args.batches):
                                        for t, response in pool.map(timed, bodies):
                                            lat.append(t)
                                            responses.append(response)
                                    row[arm] = {'qps': len(responses)/(time.perf_counter()-wall),
                                        'cpu_per_request_ms': (time.process_time()-cpu)*1000/len(responses),
                                        'p50_ms': statistics.median(lat), 'p95_ms': float(np.percentile(lat, 95))}
                                outputs.append(responses)
                            assert all(output == outputs[0] for output in outputs), (label, c, pair)
                            rounds.append(row)
                    runs[f'{label}-c{c}'] = rounds
                    print(json.dumps({'case': label, 'concurrency': c, 'rounds': rounds}), flush=True)
                async def diagnostic():
                    col = await managers[-1].touch('bench')
                    with patch.object(store.Collection, '_vector_ids', ORIGINAL_VECTOR_IDS):
                        single = await profile(col, vecs[:1], case)
                    cohorts = {}
                    for arm in searches:
                        with patch_arm(arm):
                            cohorts[arm] = await cohort_diagnostics(col, vecs[:8], case, methods[arm], searches[arm])
                    return {'single_baseline_stages': single, 'eight_query_cohorts': cohorts}
                profiles[label] = client.portal.call(diagnostic)
    medians = {case: {arm: {key: statistics.median(r[arm][key] for r in rows)
                               for key in rows[0][arm]} for arm in searches} for case, rows in runs.items()}
    result = {'baseline_revision': '632769359c783435cdfdf3ed916110560ab5608b',
        'store_source_sha256': hashlib.sha256(Path(store.__file__).read_bytes()).hexdigest(),
        'python': sys.version, 'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version,
        'versions': {name: importlib.metadata.version(name) for name in ('numpy', 'fastapi', 'pydantic', 'turbovec')},
        'openblas_threads': os.environ.get('OPENBLAS_NUM_THREADS'), 'chunk_rows': args.rows,
        'summary_rows': (args.rows+19)//20, 'dim': args.dim, 'nlist': None if args.flat else 32,
        'nprobe': None if args.flat else 16, 'seed_data': 927, 'seed_queries': 929,
        'pairs': args.pairs, 'iterations': args.iterations, 'concurrency': args.concurrency,
        'batches': args.batches, 'requests_per_arm_per_pair': args.batches*args.iterations,
        'baseline_mode': args.baseline,
        'single_client_order': 'per-request alternating', 'concurrent_order': 'per-round alternating',
        'transport': 'ASGI TestClient',
        'same_response_bytes': True, 'medians': medians, 'runs': runs, 'profiles': profiles}
    if args.out:
        args.out.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'medians': medians, 'profiles': profiles}, indent=2))


if __name__ == '__main__':
    main()
