"""Validate completeness, runtime guards and semantic consistency of the idle run."""
import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET


def validate(root):
    problems=[]
    counts={}
    identities={}
    measured=0
    def check_row(path,case,row,requests):
        if row.get('requests')!=requests:
            problems.append(f'{path.name}/{case}: wrong request count')
        for key in ('qps','p50_ms','p95_ms','p99_ms','cpu_ms_per_query'):
            value=row.get(key)
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<0 or (key=='qps' and value==0):
                problems.append(f'{path.name}/{case}: invalid {key}')
    for index in ('flat','ivf'):
        for arm in ('312','314','314t'):
            for round in (1,2,3):
                path=root/f'arxiv-{index}-{round}-{arm}.json'
                if not path.exists():
                    problems.append(f'missing {path.name}')
                    continue
                value=json.loads(path.read_text())
                counts['arxiv']=counts.get('arxiv',0)+1
                expected=arm!='314t'
                for key in ('health','end_health'):
                    if value[key]['gil_enabled'] is not expected or value[key]['bm25']!='native':
                        problems.append(f'{path.name}: invalid {key}')
                if value['collection']['index']['type']!=index or value['collection']['chunks']!=2549119:
                    problems.append(f'{path.name}: wrong dataset/index')
                for case,row in value['cases'].items():
                    check_row(path,case,row,200 if case=='filtered-c1' else 500)
                    measured+=row['requests']
                    identities.setdefault(f'arxiv-{index}/{case}',set()).add(row['hit_ids_sha256'])
    for arm in ('312','314','314t'):
        for round in (1,2,3):
            path=root/f'document-{round}-{arm}.json'
            if not path.exists():
                problems.append(f'missing {path.name}')
                continue
            value=json.loads(path.read_text())
            counts['document']=counts.get('document',0)+1
            for key in ('health','end_health'):
                if value[key]['gil_enabled'] is not (arm!='314t') or value[key]['bm25']!='native':
                    problems.append(f'{path.name}: invalid {key}')
            for case,row in value['cases'].items():
                check_row(path,case,row,240)
                measured+=row['requests']
                identities.setdefault(f'document/{case}',set()).add(row['hit_ids_sha256'])
    fingerprints={}
    for index in ('flat','ivf'):
        for arm in ('312','314','314t'):
            for round in (1,2,3):
                path=root/f'ingest-{index}-{round}-{arm}.json'
                if not path.exists():
                    problems.append(f'missing {path.name}')
                    continue
                value=json.loads(path.read_text())
                counts['ingest']=counts.get('ingest',0)+1
                fingerprints.setdefault(index,set()).add(json.dumps(value['fingerprint'],sort_keys=True))
                if value['jobs']!={'done':40}:
                    problems.append(f'{path.name}: failed/incomplete jobs {value["jobs"]}')
    for arm in ('312','314','314t'):
        path=root/f'linux-{arm}.xml'
        if not path.exists():
            problems.append(f'missing {path.name}')
        else:
            for case in ET.parse(path).iter('testcase'):
                if case.find('error') is not None:
                    problems.append(f'{path.name}: error {case.attrib.get("name")}')
                if case.find('failure') is not None and not (
                    arm=='314' and case.attrib.get('name')=='test_check_image_refuses_python_gil_0'
                    and 'Disabling the GIL is not supported by this build' in case.find('failure').text):
                    problems.append(f'{path.name}: unexpected failure {case.attrib.get("name")}')
        for kind in ('handoff-linux','ingest-loaded-linux'):
            path=root/f'{kind}-{arm}.json'
            if not path.exists():
                problems.append(f'missing {path.name}')
            elif kind=='ingest-loaded-linux' and json.loads(path.read_text())['returncode']!=0:
                problems.append(f'{path.name}: loaded ingest tests failed')
    for round in (1,2,3):
        for arm in ('312','314','314t','314s'):
            if not (root/f'kernel-{round}-{arm}.json').exists():
                problems.append(f'missing kernel-{round}-{arm}.json')
    for index in ('flat','ivf'):
        for round in (1,2,3):
            for arm in ('314','314t','314s'):
                path=root/f'control-{index}-{round}-{arm}.json'
                if not path.exists():
                    problems.append(f'missing {path.name}')
                    continue
                value=json.loads(path.read_text())
                counts['control']=counts.get('control',0)+1
                for key in ('health','end_health'):
                    if value[key]['gil_enabled'] is not (arm!='314t'):
                        problems.append(f'{path.name}: invalid {key}')
                for case,row in value['cases'].items():
                    check_row(path,case,row,100)
                    measured+=row['requests']
                    identities.setdefault(f'control-{index}/{case}',set()).add(row['hit_ids_sha256'])
    for case,values in identities.items():
        if len(values)>1: problems.append(f'response identity mismatch {case}')
    for index,values in fingerprints.items():
        if len(values)>1: problems.append(f'ingest fingerprint mismatch {index}')
    return {'complete':not problems,'problems':problems,'completed_sessions':counts,
            'measured_http_requests':measured,'known_standard_314_assertion_failure':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path)
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    result=validate(args.root)
    if args.out: args.out.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
