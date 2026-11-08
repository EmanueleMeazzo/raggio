"""Fixed-client runtime comparison on the existing held-out arXiv query set."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import httpx
import numpy as np
import orjson


def prepare(args):
    corpus = Path(args.corpus)
    vectors = np.load(corpus / 'embed-vecs.npy', mmap_mode='r')
    meta = json.loads((corpus / 'embed-meta.json').read_text())
    count, dim = vectors.shape
    assert count == 2549619 and dim == 1024 and len(meta['paths']) == count
    query_rows = np.sort(np.random.default_rng(42).choice(count, 500, replace=False))
    selected = np.ones(count, dtype=bool)
    selected[query_rows] = False
    ingested = np.nonzero(selected)[0]
    hybrid_rows = np.random.default_rng(42).choice(ingested, size=500, replace=False)
    wanted = set(int(i) for i in hybrid_rows)
    titles = {}
    with (corpus / 'abstracts.jsonl').open('rb') as stream:
        for row, line in enumerate(stream):
            if row in wanted:
                titles[row] = orjson.loads(line)['title']
    assert len(titles) == 500
    out = Path(args.fixture)
    np.savez(out.with_suffix('.npz'), queries=np.round(np.asarray(vectors[query_rows], dtype=np.float32), 5),
             gt=np.load(args.gt)['gt'])
    out.write_text(json.dumps({'count':count, 'dim':dim, 'seed':42,
        'titles':[titles[int(r)] for r in hybrid_rows],
        'target_paths':[meta['paths'][int(r)] for r in hybrid_rows],
        'paths':meta['paths']}))
    print('prepared 500 original held-out queries', flush=True)


def usage(directory):
    root = Path(directory)
    cpu = dict(line.split() for line in (root / 'cpu.stat').read_text().splitlines())
    return int(cpu['usage_usec']), int((root / 'memory.current').read_text())


def memory_parts(directory):
    values=dict(line.split() for line in (Path(directory)/'memory.stat').read_text().splitlines())
    return {key:int(values.get(key,0))/1024**2 for key in ('anon','file')}


async def measure(args):
    config = json.loads(Path(args.fixture).read_text())
    data = np.load(Path(args.fixture).with_suffix('.npz'))
    queries, gt = data['queries'], data['gt']
    cases = [('vector',1,500),('vector',8,500),('vector',16,500),
             ('hybrid',1,500),('hybrid',8,500),('hybrid',16,500),('filtered',1,200)]
    if args.limit_requests:
        cases=[(mode,concurrency,min(count,args.limit_requests)) for mode,concurrency,count in cases]
    if args.prewarm:
        cases=[('vector',1,500),('hybrid',1,500),('filtered',1,200)]
    results = {}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:28140', headers={'x-api-key':'bench','content-type':'application/json'}, timeout=120) as client:
        health = (await client.get('/healthz')).json()
        info = await client.get('/collections/bench')
        info.raise_for_status()
        assert info.json()['pending_jobs'] == 0
        assert info.json()['index']['type'] == args.index, info.json()
        assert health['gil_enabled'] is (args.arm != '314t'), health
        assert health['bm25'] == 'native', health
        print(json.dumps({'health':health,'collection':info.json()}), flush=True)
        for mode, concurrency, count in cases:
            bodies = []
            for i, q in enumerate(queries[:count]):
                body = {'query':{'vector':q}, 'k':10}
                if mode == 'hybrid':
                    body['mode'] = 'hybrid'
                    body['query']['text'] = config['titles'][i]
                if mode == 'filtered':
                    body['filter'] = {'year':'2020'}
                bodies.append(orjson.dumps(body,option=orjson.OPT_SERIALIZE_NUMPY))
            for body in bodies[:20]:
                response = await client.post('/collections/bench/search',content=body)
                assert response.status_code == 200, response.text
            semaphore = asyncio.Semaphore(concurrency)
            values = [None]*count
            async def one(i, body):
                async with semaphore:
                    start = time.perf_counter()
                    response = await client.post('/collections/bench/search',content=body)
                    elapsed = (time.perf_counter()-start)*1000
                    response.raise_for_status()
                    hits = response.json()['hits']
                    assert hits, (mode,i,response.text)
                    values[i] = elapsed,hits
            before,memory_before = usage(args.cgroup)
            parts_before=memory_parts(args.cgroup)
            started = time.perf_counter()
            await asyncio.gather(*(one(i,body) for i,body in enumerate(bodies)))
            elapsed = time.perf_counter()-started
            after,memory_after = usage(args.cgroup)
            parts_after=memory_parts(args.cgroup)
            latencies = [v[0] for v in values]
            ids = [[h['id'] for h in v[1]] for v in values]
            row = {'qps':count/elapsed,'p50_ms':float(np.percentile(latencies,50)),
                   'p95_ms':float(np.percentile(latencies,95)), 'p99_ms':float(np.percentile(latencies,99)),
                   'cpu_ms_per_query':(after-before)/1000/count,
                   'memory_before_mb':memory_before/1024**2,'memory_after_mb':memory_after/1024**2,
                   'anon_before_mb':parts_before['anon'],'anon_after_mb':parts_after['anon'],
                   'file_before_mb':parts_before['file'],'file_after_mb':parts_after['file'],
                   'hit_ids_sha256':hashlib.sha256(orjson.dumps(ids)).hexdigest(),
                   'requests':count,'elapsed_s':elapsed}
            if mode == 'vector':
                row['recall_at_10'] = float(np.mean([len(set(int(h[1:]) for h in hits)&set(g))/10 for hits,g in zip(ids,gt[:count])]))
            if mode == 'hybrid':
                row['text_hit_at_10'] = float(np.mean([any(config['paths'][int(h[1:])]==p for h in hits)
                                                     for hits,p in zip(ids,config['target_paths'][:count])]))
            results[f'{mode}-c{concurrency}'] = row
            print(json.dumps({'case':f'{mode}-c{concurrency}', **row}), flush=True)
        end_health = (await client.get('/healthz')).json()
        assert end_health['gil_enabled'] == health['gil_enabled']
    result={'arm':args.arm,'round':args.round,'index':args.index,'health':health,'end_health':end_health,
            'collection':info.json(),'client_python':sys.version,'cases':results,
            'source_commit':'7947eba','server_memory_cap_gib':4,'openblas_num_threads':1,
            'warmup_queries_per_case':20,'fixture_queries':500}
    Path(args.out).write_text(json.dumps(result,indent=2)+'\n')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--corpus',default='/corpus')
    parser.add_argument('--fixture',default='/results/arxiv-fixture.json')
    parser.add_argument('--gt',default='/eval/gt.npz')
    parser.add_argument('--cgroup')
    parser.add_argument('--arm')
    parser.add_argument('--round',type=int)
    parser.add_argument('--index')
    parser.add_argument('--out')
    parser.add_argument('--limit-requests',type=int,default=0)
    parser.add_argument('--prewarm',action='store_true')
    args=parser.parse_args()
    if args.prepare: prepare(args)
    else: asyncio.run(measure(args))
