"""Summarize the measurements without treating repeat spread as statistical certainty."""
import argparse
import json
from pathlib import Path
import statistics
import xml.etree.ElementTree as ET


def summary(root):
    result={'tests':{},'arxiv':{},'document':{},'ingest':{},'handoff':{},'loaded_ingest':{},'facts':{},'control':{},'kernel':{}}
    for path in root.glob('linux-*.xml'):
        result['tests'][path.stem]=[x.attrib for x in ET.parse(path).iter('testsuite')]
    for arm in ('312','314','314t'):
        path=root/f'facts-{arm}.json'
        if path.exists(): result['facts'][arm]=json.loads(path.read_text())
    for workload in ('arxiv-flat','arxiv-ivf','document'):
        arms={}
        for arm in ('312','314','314t'):
            samples=[json.loads(p.read_text()) for p in sorted(root.glob(f'{workload}-*-{arm}.json'))]
            if not samples: continue
            arms[arm]={}
            for case in samples[0]['cases']:
                rows=[s['cases'][case] for s in samples]
                numbers={key for key,value in rows[0].items() if isinstance(value,(int,float))}
                arms[arm][case]={'repeats':len(rows),'medians':{k:statistics.median(r[k] for r in rows) for k in numbers},
                    'ranges':{k:[min(r[k] for r in rows),max(r[k] for r in rows)] for k in numbers},
                    'hit_ids_sha256':sorted({r['hit_ids_sha256'] for r in rows})}
        target=result['document'] if workload=='document' else result['arxiv'].setdefault(workload,{})
        target.update(arms)
    for index in ('flat','ivf'):
        for arm in ('312','314','314t'):
            samples=[json.loads(p.read_text()) for p in sorted(root.glob(f'ingest-{index}-*-{arm}.json'))]
            if samples:
                result['ingest'].setdefault(index,{})[arm]={'repeats':len(samples),
                    'medians':{k:statistics.median(s[k] for s in samples) for k in ('ingest_vps','drain_s','loop_lag_max_ms','loop_stall_s')},
                    'ranges':{k:[min(s[k] for s in samples),max(s[k] for s in samples)] for k in ('ingest_vps','drain_s')},
                    'fingerprints':[s['fingerprint'] for s in samples], 'jobs':[s['jobs'] for s in samples]}
    for path in root.glob('handoff-*.json'): result['handoff'][path.stem]=json.loads(path.read_text())
    for path in root.glob('ingest-loaded-*.json'): result['loaded_ingest'][path.stem]=json.loads(path.read_text())
    for workload in ('control-flat','control-ivf','kernel'):
        arms={}
        for arm in ('312','314','314s','314t'):
            samples=[json.loads(p.read_text()) for p in sorted(root.glob(f'{workload}-*-{arm}.json'))]
            if not samples: continue
            arms[arm]={case:{'repeats':len(samples),'medians':{key:statistics.median(s['cases'][case][key] for s in samples)
                for key,value in samples[0]['cases'][case].items() if isinstance(value,(int,float))}}
                for case in samples[0]['cases']}
        if workload=='kernel': result['kernel']=arms
        else: result['control'][workload]=arms
    result['response_hash_mismatches']=[]
    for workload,arms in [*result['arxiv'].items(),('document',result['document'])]:
        if len(arms)!=3: continue
        for case in arms['312']:
            hashes={h for arm in arms.values() for h in arm[case]['hit_ids_sha256']}
            if len(hashes)!=1: result['response_hash_mismatches'].append({'workload':workload,'case':case,'hashes':sorted(hashes)})
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path)
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    result=summary(args.root)
    if args.out: args.out.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('tests','response_hash_mismatches','loaded_ingest')},indent=2))
