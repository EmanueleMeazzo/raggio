"""Current scoped retrieval routes with a fixed 3.12 HTTP client."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

import httpx
import numpy as np
import orjson


def cpu(path):
    return int(dict(line.split() for line in (Path(path)/'cpu.stat').read_text().splitlines())['usage_usec'])


def identity(hit):
    value={key:hit.get(key) for key in ('id','doc_id','type')}
    expansion=hit.get('expansion') or {}
    if 'siblings' in expansion:
        value['siblings']=[identity(s) for s in expansion['siblings']]
    if 'summary' in expansion:
        value['summary']=identity(expansion['summary']) if expansion['summary'] else None
    return value


async def measure(args):
    vectors=np.random.default_rng(1107).standard_normal((240,3072),dtype=np.float32)
    vectors=np.round(vectors/np.linalg.norm(vectors,axis=1,keepdims=True),5)
    result={'arm':args.arm,'round':args.round,'client_python':sys.version,'docs':320,'dim':3072,'words_per_chunk':500,'cases':{}}
    cases=['summary','doc-vector','doc-hybrid','doc-hybrid-cold','doc-k1000','doc-expand']
    async with httpx.AsyncClient(base_url='http://127.0.0.1:28140',headers={'x-api-key':'bench','content-type':'application/json'},timeout=120) as client:
        result['health']=(await client.get('/healthz')).json()
        assert result['health']['gil_enabled'] is (args.arm!='314t')
        for case in cases:
            for concurrency in (1,8,16):
                bodies=[]
                for i,vector in enumerate(vectors):
                    doc=i%320 if case=='doc-hybrid-cold' else 42
                    body={'query':{'vector':vector},'k':10}
                    if case=='summary': body['scope']='summaries'
                    else: body['filter']={'doc_id':f'd{doc}'}
                    if 'hybrid' in case or case=='doc-expand':
                        body['mode']='hybrid'
                        body['query']['text']=f'topic{doc} scientific retrieval'
                    if case=='doc-k1000': body['k']=1000
                    if case=='doc-expand': body['expand']={'siblings_all':True,'summary':True}
                    bodies.append(orjson.dumps(body,option=orjson.OPT_SERIALIZE_NUMPY))
                for body in bodies[:16]:
                    response=await client.post('/collections/probe/search',content=body)
                    assert response.status_code == 200,response.text
                values=[None]*len(bodies)
                semaphore=asyncio.Semaphore(concurrency)
                async def one(i,body):
                    async with semaphore:
                        start=time.perf_counter()
                        response=await client.post('/collections/probe/search',content=body)
                        elapsed=(time.perf_counter()-start)*1000
                        response.raise_for_status()
                        hits=response.json()['hits']
                        assert hits,(case,i,response.text)
                        values[i]=(elapsed,[identity(h) for h in hits],len(response.content))
                before=cpu(args.cgroup)
                start=time.perf_counter()
                await asyncio.gather(*(one(i,b) for i,b in enumerate(bodies)))
                elapsed=time.perf_counter()-start
                after=cpu(args.cgroup)
                lat=[v[0] for v in values]
                row={'qps':len(values)/elapsed,'cpu_ms_per_query':(after-before)/1000/len(values),
                    'p50_ms':float(np.percentile(lat,50)),'p95_ms':float(np.percentile(lat,95)),
                    'p99_ms':float(np.percentile(lat,99)), 'response_bytes_mean':sum(v[2] for v in values)/len(values),
                    'hit_ids_sha256':hashlib.sha256(orjson.dumps([v[1] for v in values])).hexdigest(),'requests':len(values)}
                result['cases'][f'{case}-c{concurrency}']=row
                print(json.dumps({'case':f'{case}-c{concurrency}',**row}),flush=True)
        result['end_health']=(await client.get('/healthz')).json()
    Path(args.out).write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--out')
    parser.add_argument('--arm')
    parser.add_argument('--round',type=int)
    parser.add_argument('--cgroup')
    args=parser.parse_args()
    if args.prepare:
        sys.path.insert(0,'/project/bench')
        import document_retrieval_probe as probe
        asyncio.run(probe.prepare(Path('/data'),320,3072,500))
    else: asyncio.run(measure(args))
