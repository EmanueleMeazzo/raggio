"""Render the complete numeric tables for the Python runtime evaluation."""
import json
from pathlib import Path

s=json.loads(Path('idle-summary.json').read_text())
lines=['# Supporting measurements','','All main serving values are medians of three independent server sessions. QPS = queries per second. CPU values aggregate all server CPU cores.','']


def comparison(title,arms,metric,unit):
    if not all(a in arms for a in ('312','314','314t')): return
    lines.extend([f'## {title}','',f'| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |','|---|---:|---:|---:|---:|'])
    for case in arms['312']:
        vals=[arms[a][case]['medians'][metric] for a in ('312','314','314t')]
        change=(vals[2]/vals[0]-1)*100 if vals[0] else 0
        lines.append(f'| {case} | {vals[0]:.2f} | {vals[1]:.2f} | {vals[2]:.2f} | {change:+.1f}% |')
    lines.extend(['',unit,''])


for name,arms in s['arxiv'].items():
    comparison(f'{name}: throughput',arms,'qps','Queries/second; higher is better.')
    comparison(f'{name}: p99 latency',arms,'p99_ms','Milliseconds; lower is better.')
    comparison(f'{name}: CPU cost',arms,'cpu_ms_per_query','Server CPU milliseconds/query; lower is better.')
    lines.extend([f'## {name}: repeat ranges','','| Case | 3.12 QPS range | 3.14 QPS range | 3.14t QPS range |','|---|---:|---:|---:|'])
    for case in arms.get('312',{}):
        ranges=[arms[a][case]['ranges']['qps'] for a in ('312','314','314t')]
        lines.append('| '+case+' | '+' | '.join(f'{lo:.2f}–{hi:.2f}' for lo,hi in ranges)+' |')
    lines.append('')
comparison('Document-scoped retrieval: throughput',s['document'],'qps','Queries/second; higher is better.')
comparison('Document-scoped retrieval: p99',s['document'],'p99_ms','Milliseconds; lower is better.')
comparison('Document-scoped retrieval: CPU',s['document'],'cpu_ms_per_query','Server CPU milliseconds/query; lower is better.')
lines.extend(['## Ingest','','| Index | 3.12 rows/s [range] | 3.14 rows/s [range] | 3.14t rows/s [range] |','|---|---:|---:|---:|'])
for index,arms in s['ingest'].items():
    values=[]
    for arm in ('312','314','314t'):
        row=arms[arm]
        lo,hi=row['ranges']['ingest_vps']
        values.append(f'{row["medians"]["ingest_vps"]:.0f} [{lo:.0f}–{hi:.0f}]')
    lines.append('| '+index+' | '+' | '.join(values)+' |')
lines.extend(['','## Pinned raw native kernel','','All values are milliseconds per call; ten X925 cores, k=50.','',
    '| Batch queries | 3.12 | 3.14 wheel | 3.14 source | 3.14t source |','|---|---:|---:|---:|---:|'])
if len(s['kernel'])==4:
    for case in ('1','8','16'):
        values=[s['kernel'][a][case]['medians']['p50_ms'] for a in ('312','314','314s','314t')]
        lines.append('| '+case+' | '+' | '.join(f'{v:.3f}' for v in values)+' |')
for name,arms in s['control'].items():
    if not all(a in arms for a in ('314','314s','314t')): continue
    lines.extend(['',f'## {name}: matching 100-query source-build control','',
        '| Case | 3.14 wheel QPS | 3.14 source QPS | 3.14t source QPS |','|---|---:|---:|---:|'])
    for case in arms['314']:
        values=[arms[a][case]['medians']['qps'] for a in ('314','314s','314t')]
        lines.append('| '+case+' | '+' | '.join(f'{v:.2f}' for v in values)+' |')
Path('tables-idle.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print('Wrote tables-idle.md')
