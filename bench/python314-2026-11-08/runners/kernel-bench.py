"""Separate raw turbovec scan behavior from HTTP, SQLite, and async hand-offs."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import numpy as np

# A homogeneous mask removes the DGX's X925/A725 placement difference from this
# native-build diagnostic. The full HTTP measurements retain all 20 host CPUs.
os.sched_setaffinity(0,{5,6,7,8,9,15,16,17,18,19})
from turbovec import IdMapIndex


parser=argparse.ArgumentParser()
parser.add_argument('--out',required=True)
parser.add_argument('--arm',required=True)
parser.add_argument('--round',type=int,required=True)
args=parser.parse_args()
files=list(Path('/data/collections').glob('*/index.tvim'))
assert len(files)==1,files
index=IdMapIndex.load(str(files[0]))
index.prepare()
queries=np.load('/results/arxiv-fixture.npz')['queries']
result={'arm':args.arm,'round':args.round,'python':sys.version,
    'gil_enabled':getattr(sys,'_is_gil_enabled',lambda:True)(),'affinity':sorted(os.sched_getaffinity(0)),
    'rows':len(index),'cases':{}}
for batch in (1,8,16):
    for i in range(5): index.search(queries[i:i+batch],k=50)
    wall,cpu=[],[]
    for i in range(30):
        w,c=time.perf_counter(),time.process_time()
        index.search(queries[i:i+batch],k=50)
        wall.append((time.perf_counter()-w)*1000)
        cpu.append((time.process_time()-c)*1000)
    result['cases'][str(batch)]={'p50_ms':float(np.median(wall)), 'p95_ms':float(np.percentile(wall,95)),
        'cpu_ms':float(np.median(cpu)),'per_query_ms':float(np.median(wall))/batch}
Path(args.out).write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result),flush=True)
