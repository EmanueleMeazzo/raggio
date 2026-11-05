"""Retrieval-only investigation; prototypes never modify the production service.

Run inside the frozen native image with local temporary storage and one BLAS thread.
HTTP clients run in this process, uvicorn in a separate spawned process. CPU/query
therefore measures the server, not client JSON decoding or TestClient overhead.
The generated corpus follows Sofia's query shape, not its private corpus or traffic.
"""
import argparse
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
from pathlib import Path
import platform
import resource
import socket
import sqlite3
import statistics
import sys
import tempfile
import threading
import time

import httpx
import numpy as np
import uvicorn
from fastapi import Request
from pydantic import ValidationError

import raggio.app as api
import raggio.store as store
from raggio.config import Settings

BASELINE = 'daaab6d27b9f9aceadd86b8a82be0e53d9fb50d0'
SOURCE_SHA256 = hashlib.sha256(Path(store.__file__).read_bytes()).hexdigest()
API_SHA256 = hashlib.sha256(Path(api.__file__).read_bytes()).hexdigest()
STORE_LF_SHA256 = hashlib.sha256(Path(store.__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
API_LF_SHA256 = hashlib.sha256(Path(api.__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
CACHE_BYTES = 8 * 1024 * 1024
CACHE_SETS = 32


async def prepare(directory, docs, dim, words):
    settings = Settings()
    settings.data_dir = directory
    manager = store.CollectionManager(settings)
    rng = np.random.default_rng(1105)
    vocab = np.array([f'word{i}' for i in range(4096)])
    try:
        await manager.create_collection('probe', dim, 4, None, None, None)
        col = await manager.touch('probe')
        for start in range(0, docs, 50):
            payload = []
            for d in range(start, min(start + 50, docs)):
                count = 3 + d % 8
                vectors = store._normalize(rng.standard_normal((count + 1, dim), dtype=np.float32))
                metadata = {'title': f'Report {d} - café Σ', 'source': f'https://example.test/docs/{d}',
                            'tags': 'legal, scientific', 'group': d % 8,
                            'nested': {'language': 'it', 'values': [d, None, True]}}
                def text(n):
                    return f'topic{d} scientific retrieval café Σ ' + ' '.join(rng.choice(vocab, n))
                payload.append({'doc_id': f'd{d}', 'summary': {'vector': vectors[0],
                    'text': text(300), 'metadata': metadata}, 'chunks': [
                    {'id': f'c{d}-{i}', 'position': i, 'vector': vectors[i + 1],
                     'text': text(words), 'metadata': metadata} for i in range(count)]})
            await col._process_job({'documents': payload})
    finally:
        await manager.shutdown()


def serve(directory, port):
    settings = Settings()
    settings.data_dir, settings.root_api_key = directory, 'probe'
    state = {'arm': 'baseline', 'profile': False, 'samples': {}, 'hits': 0, 'misses': 0}
    cache, mutex = OrderedDict(), threading.Lock()
    managers = []
    manager_type = api.CollectionManager
    def capture(*a, **kw):
        manager = manager_type(*a, **kw)
        managers.append(manager)
        return manager
    api.CollectionManager = capture
    app = api.create_app(settings)
    original_allowed = store.Collection._search_allowed
    original_json = Request.json
    original_to_thread = asyncio.to_thread

    def record(name, wall, cpu):
        with mutex:
            state['samples'].setdefault(name, []).append([wall * 1000, cpu * 1000])

    def timed(name, fn):
        def call(*a, **kw):
            if not state['profile']:
                return fn(*a, **kw)
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return fn(*a, **kw)
            finally:
                record(name, time.perf_counter() - wall, time.thread_time() - cpu)
        return call

    def allowed(col, qvec, n, allow, **kw):
        if state['arm'] != 'vectors' or len(allow) > col._rescore_k(n):
            return original_allowed(col, qvec, n, allow, **kw)
        gen = col._allow_gen
        key = (id(col), gen, tuple(int(i) for i in allow))
        with mutex:
            entry = cache.get(key)
            if entry is not None:
                cache.move_to_end(key)
                state['hits'] += 1
            else:
                state['misses'] += 1
        if entry is None:
            ids, mat = col._fp16_vecs(allow.tolist())
            if len(ids) != len(allow):
                return original_allowed(col, qvec, n, allow, **kw)
            mat.flags.writeable = False
            entry = ids, mat
            with mutex:
                if gen == col._allow_gen and mat.nbytes <= CACHE_BYTES:
                    cache[key] = entry
                    while len(cache) > CACHE_SETS or sum(v[1].nbytes for v in cache.values()) > CACHE_BYTES:
                        cache.popitem(last=False)
        else:
            # Keep the collection's guarded-read/closed check on hits too. This is
            # a read-only experimental cache, not a production lifecycle contract.
            with col._reading():
                pass
        ids, mat = entry
        scores = mat @ qvec[0]
        top = np.lexsort((ids, -scores))[:n]
        return [ids[j] for j in top], [float(scores[j]) for j in top]

    async def request_json(request):
        wall, cpu = time.perf_counter(), time.thread_time()
        try:
            if state['arm'] == 'parse' and request.url.path.endswith('/search'):
                if not hasattr(request, '_json'):
                    try:
                        request._json = api.SearchIn.model_validate_json(await request.body())
                    except ValidationError:
                        request._json = await original_json(request)
                return request._json
            return await original_json(request)
        finally:
            if state['profile'] and request.url.path.endswith('/search'):
                record('request_json', time.perf_counter() - wall, time.thread_time() - cpu)

    async def to_thread(fn, *a, **kw):
        if not state['profile']:
            return await original_to_thread(fn, *a, **kw)
        queued = time.perf_counter()
        def execute():
            record('worker_queue', time.perf_counter() - queued, 0)
            return fn(*a, **kw)
        return await original_to_thread(execute)

    store.Collection._search_allowed = timed('allowed_search', allowed)
    for name in ('_fp16_vecs', '_text_ids', '_bm25_rescore', '_hydrate'):
        setattr(store.Collection, name, timed(name, getattr(store.Collection, name)))
    api._search_response = timed('response_encode', api._search_response)
    api._normalize = timed('query_normalize', api._normalize)
    import fastapi.dependencies.utils as dependencies
    dependencies._validate_value_with_model_field = timed('validation', dependencies._validate_value_with_model_field)
    Request.json, asyncio.to_thread = request_json, to_thread
    if hasattr(api, '_ValidatedJSONRequest'):
        production_json = api._ValidatedJSONRequest.json
        async def validated_json(request):
            if state['arm'] == 'parse':
                return await production_json(request)
            return await request._original.json()
        api._ValidatedJSONRequest.json = validated_json

    @app.get('/__probe__')
    async def control(action: str = 'stats', arm: str = 'baseline'):
        if action == 'arm':
            state['arm'], state['profile'] = arm, False
            with mutex:
                cache.clear()
                state['hits'], state['misses'] = 0, 0
        elif action == 'profile':
            state['arm'], state['profile'], state['samples'] = 'baseline', True, {}
        elif action == 'profile_stop':
            state['profile'] = False
        status = Path('/proc/self/status').read_text()
        rss = int(next(line.split()[1] for line in status.splitlines() if line.startswith('VmRSS:'))) * 1024
        return {'cpu_s': time.process_time(), 'rss_bytes': rss,
            'maxrss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            'cache_bytes': sum(v[1].nbytes for v in cache.values()),
            'cache_entries': len(cache), 'cache_hits': state['hits'], 'cache_misses': state['misses'],
            'samples': state['samples'] if action == 'profile_stop' else {},
            'native': bool(store._native), 'sqlite': sqlite3.sqlite_version}

    uvicorn.run(app, host='127.0.0.1', port=port, loop='asyncio', log_level='error', access_log=False)


def percentile(values, p):
    return float(np.percentile(values, p))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docs', type=int, default=320)
    parser.add_argument('--dim', type=int, default=3072)
    parser.add_argument('--words', type=int, default=1000)
    parser.add_argument('--pairs', type=int, default=5)
    parser.add_argument('--requests', type=int, default=240)
    parser.add_argument('--candidate', choices=['vectors', 'parse', 'control'], default='vectors')
    parser.add_argument('--with-control', action='store_true')
    parser.add_argument('--block', type=int, default=32)
    parser.add_argument('--precision', choices=['five', 'full'], default='five')
    parser.add_argument('--concurrency', nargs='+', type=int, default=[1, 8, 16])
    parser.add_argument('--cases', nargs='+', default=['vector-hot', 'hybrid-hot', 'hybrid-wide', 'hybrid-cold', 'text-hot', 'summary'])
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.docs < args.requests + 64 or args.dim % 8:
        parser.error('docs >= requests + 64; dimension divisible by 8')
    rng = np.random.default_rng(1106)
    vectors = store._normalize(rng.standard_normal((args.requests, args.dim), dtype=np.float32))
    if args.precision == 'five':
        vectors = np.round(vectors, 5)
    vectors = vectors.tolist()
    headers = {'x-api-key': 'probe', 'content-type': 'application/json'}
    runs, profiles, payload_bytes = {}, {}, {}
    with tempfile.TemporaryDirectory(prefix='raggio-document-') as directory:
        asyncio.run(prepare(directory, args.docs, args.dim, args.words))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = multiprocessing.get_context('spawn').Process(target=serve, args=(directory, port))
        process.start()
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', headers=headers, timeout=60,
                              limits=httpx.Limits(max_connections=32, max_keepalive_connections=32)) as client:
                for _ in range(600):
                    try:
                        if client.get('/healthz').status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError('server did not start')
                def control(**kw):
                    response = client.get('/__probe__', params=kw)
                    response.raise_for_status()
                    return response.json()
                environment = control()
                assert environment['native'], 'native scorer required'
                def post(raw):
                    start = time.perf_counter()
                    response = client.post('/collections/probe/search', content=raw)
                    response.raise_for_status()
                    return (time.perf_counter() - start) * 1000, response.content
                for case in args.cases:
                    bodies = []
                    for i, vector in enumerate(vectors):
                        d = i % (8 if case.endswith('hot') else 64) if not case.endswith('cold') else i + 64
                        mode = 'text' if case.startswith('text') else 'vector' if case.startswith('vector') else 'hybrid'
                        query = {'text': f'topic{d} scientific retrieval word17 word43 café Σ'} if mode != 'vector' else {}
                        if mode != 'text':
                            query['vector'] = vector
                        body = {'query': query, 'mode': mode, 'fusion': 'relative', 'alpha': 0.5,
                                'scope': 'summaries' if case == 'summary' else 'chunks', 'k': 10 if case == 'summary' else 5}
                        if case != 'summary':
                            body['filter'] = {'doc_id': f'd{d}'}
                        bodies.append(json.dumps(body, separators=(',', ':')).encode())
                    payload_bytes[case] = {'min': min(map(len, bodies)), 'max': max(map(len, bodies))}
                    control(action='arm', arm='baseline')
                    for raw in bodies[:32]:
                        post(raw)
                    control(action='profile')
                    for raw in bodies[:20]:
                        post(raw)
                    samples = control(action='profile_stop')['samples']
                    profiles[case] = {name: {'calls': len(values),
                        'p50_wall_ms': statistics.median(v[0] for v in values),
                        'mean_wall_ms': statistics.mean(v[0] for v in values),
                        'mean_thread_cpu_ms': statistics.mean(v[1] for v in values)} for name, values in samples.items()}
                    for concurrency in args.concurrency:
                        pairs = []
                        with ThreadPoolExecutor(max_workers=concurrency) as pool:
                            for pair in range(args.pairs):
                                arms = ['baseline', args.candidate]
                                if args.with_control and args.candidate != 'control':
                                    arms.append('control')
                                for arm in arms:
                                    control(action='arm', arm=arm)
                                    # Warm the same documents; cold case deliberately cycles
                                    # over more than the 32-entry cache before measurement.
                                    list(pool.map(post, bodies))
                                measured = {arm: [] for arm in arms}
                                resources = {arm: [] for arm in arms}
                                for block, start_at in enumerate(range(0, len(bodies), args.block)):
                                    rotate = (pair + block) % len(arms)
                                    order = arms[rotate:] + arms[:rotate]
                                    for arm in (order if pair % 2 == 0 else order[::-1]):
                                        control(action='arm', arm=arm)
                                        before = control()
                                        start = time.perf_counter()
                                        values = list(pool.map(post, bodies[start_at:start_at + args.block]))
                                        elapsed = time.perf_counter() - start
                                        after = control()
                                        measured[arm].extend(values)
                                        resources[arm].append((before, after, elapsed))
                                row, outputs = {}, {}
                                for arm in arms:
                                    values = measured[arm]
                                    before, after = resources[arm][0][0], resources[arm][-1][1]
                                    elapsed = sum(r[2] for r in resources[arm])
                                    lat = [v[0] for v in values]
                                    outputs[arm] = [v[1] for v in values]
                                    row[arm] = {'p50_ms': statistics.median(lat), 'p95_ms': percentile(lat, 95),
                                        'qps': len(values) / elapsed,
                                        'cpu_per_query_ms': sum(b['cpu_s'] - a['cpu_s'] for a, b, _ in resources[arm]) * 1000 / len(values),
                                        'rss_before_bytes': before['rss_bytes'], 'rss_after_bytes': after['rss_bytes'],
                                        'cache_bytes': after['cache_bytes'],
                                        'cache_hits': sum(b['cache_hits'] - a['cache_hits'] for a, b, _ in resources[arm]),
                                        'cache_misses': sum(b['cache_misses'] - a['cache_misses'] for a, b, _ in resources[arm])}
                                assert outputs['baseline'] == outputs[args.candidate], (case, concurrency, pair)
                                if args.with_control:
                                    assert outputs['baseline'] == outputs['control'], (case, concurrency, pair, 'control')
                                pairs.append(row)
                                print(json.dumps({'case': case, 'c': concurrency, 'pair': pair, 'results': row}), flush=True)
                        runs[f'{case}-c{concurrency}'] = pairs
        finally:
            process.terminate()
            process.join(timeout=10)
    ratios = {case: {metric: [100 * (pair[args.candidate][metric] / pair['baseline'][metric] - 1) for pair in pairs]
                    for metric in ('p50_ms', 'p95_ms', 'qps', 'cpu_per_query_ms')} for case, pairs in runs.items()}
    controls = {case: {metric: [100 * (pair['control'][metric] / pair['baseline'][metric] - 1) for pair in pairs]
                      for metric in ('p50_ms', 'p95_ms', 'qps', 'cpu_per_query_ms')} for case, pairs in runs.items()} if args.with_control else {}
    result = {'baseline_revision': BASELINE, 'store_sha256': SOURCE_SHA256, 'api_sha256': API_SHA256,
        'store_lf_sha256': STORE_LF_SHA256, 'api_lf_sha256': API_LF_SHA256,
        'python': sys.version, 'platform': platform.platform(), 'server': environment,
        'versions': {n: importlib.metadata.version(n) for n in ('numpy', 'fastapi', 'pydantic', 'turbovec', 'uvicorn')},
        'openblas_threads': os.environ.get('OPENBLAS_NUM_THREADS'), 'config': vars(args) | {'out': str(args.out)},
        'transport': 'loopback HTTP; separate uvicorn process; pre-encoded requests; no embeddings',
        'corpus': 'generated, 3-10 chunks/document; 300-word summaries; no supplied traffic or private corpus',
        'request_bytes': payload_bytes, 'order': 'alternate paired blocks; reverse first arm by pair and block',
        'cache_limit_bytes': CACHE_BYTES, 'cache_max_sets': CACHE_SETS,
        'same_response_bytes': True, 'runs': runs, 'profiles': profiles, 'paired_changes_percent': ratios,
        'unchanged_control_changes_percent': controls,
        'paired_medians_percent': {case: {metric: statistics.median(values) for metric, values in metrics.items()} for case, metrics in ratios.items()}}
    args.out.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result['paired_medians_percent'], indent=2), flush=True)


if __name__ == '__main__':
    main()
