"""Measure hand-offs below and at CPU saturation; optionally stress real ingest tests."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time


def percentile(values, fraction):
    values=sorted(values)
    return values[min(len(values)-1,int((len(values)-1)*fraction))]


def load(count):
    workers=[subprocess.Popen([sys.executable,'-c','while True: pass'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
             for _ in range(count)]
    time.sleep(0.5)
    return workers


def stop(workers):
    for worker in workers: worker.terminate()
    for worker in workers: worker.wait(timeout=15)


def pingpong(samples):
    request,reply=threading.Event(),threading.Event()
    errors=[]
    def responder():
        for _ in range(samples+5):
            if not request.wait(30):
                errors.append('request timeout')
                return
            request.clear()
            reply.set()
    thread=threading.Thread(target=responder,daemon=True)
    thread.start()
    values=[]
    for i in range(samples+5):
        start=time.perf_counter()
        request.set()
        assert reply.wait(30),'reply timeout'
        reply.clear()
        if i>=5: values.append((time.perf_counter()-start)*1000)
    thread.join(30)
    assert not thread.is_alive() and not errors,errors
    return {'p50_ms':statistics.median(values),'p95_ms':percentile(values,.95),'samples':samples}


async def to_thread(samples):
    values=[]
    for i in range(samples+5):
        start=time.perf_counter()
        await asyncio.to_thread(lambda:None)
        if i>=5: values.append((time.perf_counter()-start)*1000)
    return {'p50_ms':statistics.median(values),'p95_ms':percentile(values,.95),'samples':samples}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cpus',default='')
    parser.add_argument('--samples',type=int,default=30)
    parser.add_argument('--out',required=True)
    parser.add_argument('--pytest',action='store_true')
    args=parser.parse_args()
    if args.cpus:
        affinity={int(c) for c in args.cpus.split(',')}
        os.sched_setaffinity(0,affinity)
    else:
        affinity=set(range(os.cpu_count()))
    result={'python':sys.version,'platform':platform.platform(),'gil_enabled':getattr(sys,'_is_gil_enabled',lambda:True)(),
            'cpus':sorted(affinity),'load':'external independent CPU busy-loop processes','cases':{}}
    if args.pytest:
        workers=load(len(affinity))
        try:
            started=time.perf_counter()
            command=[sys.executable,'-m','pytest','-q','-o','cache_dir=/tmp/pytest-cache',
                'tests/test_ingest_path.py::test_batch_syncs_once_for_many_jobs',
                'tests/test_ingest_path.py::test_idle_queue_flushes_the_open_batch']
            proc=subprocess.run(command,capture_output=True,text=True,timeout=600)
            result.update(elapsed_s=time.perf_counter()-started,returncode=proc.returncode,stdout=proc.stdout,stderr=proc.stderr)
        finally: stop(workers)
    else:
        for count in (0,len(affinity)-1,len(affinity)):
            workers=load(count)
            try:
                result['cases'][str(count)]={'pingpong':pingpong(args.samples),'to_thread':asyncio.run(to_thread(args.samples))}
                print(json.dumps({'load':count,**result['cases'][str(count)]}),flush=True)
            finally: stop(workers)
    Path(args.out).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)


if __name__=='__main__': main()
