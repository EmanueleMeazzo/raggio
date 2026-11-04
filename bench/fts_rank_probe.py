"""Rejected filtered FTS rank-order experiment.

Run: uv run --group bench python bench/fts_rank_probe.py --out results.json
Generated data, temporary local storage; protocol and limits in ADR 0010.
"""
import argparse
import sys
import json
from pathlib import Path
import statistics
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from raggio.store import open_meta_db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='raggio-fts-rank-') as directory:
        db = open_meta_db(Path(directory) / 'meta.db')
        count = 100000
        db.executemany(
            'INSERT INTO records(id,external_id,doc_id,type,position,text,metadata,indexed) VALUES (?,?,?,?,?,?,?,?)',
            ((i + 1, f'c{i}', f'd{i // 20}', 'summary' if i % 5 == 0 else 'chunk', i % 20,
              f'common retrieval scientific benchmark topic{i % 1000} ' + 'other words ' * (i % 9),
              json.dumps({'group': i % 10}), 1) for i in range(count)))
        db.commit()
        results = []
        for match in ('"topic7"', '"common"', '"topic7" OR "common"', '"retrieval" OR "scientific"'):
            for where, params in (("indexed=1 AND type='chunk'", []),
                                  ("indexed=1 AND json_extract(metadata,?)=?", ['$.group', 7]),
                                  ("indexed=1 AND doc_id=? AND type='chunk'", ['d0'])):
                sqls = {kind: f'SELECT r.id, -{rank} FROM records_fts JOIN records r ON r.id=records_fts.rowid '
                        f'WHERE records_fts MATCH ? AND {where} ORDER BY {rank} LIMIT ?'
                        for kind, rank in (('bm25', 'bm25(records_fts)'), ('rank', 'records_fts.rank'))}
                args = [match, *params, 50]
                outputs = {name: db.execute(sql, args).fetchall() for name, sql in sqls.items()}
                assert outputs['bm25'] == outputs['rank'], (match, where, outputs)
                samples = {name: [] for name in sqls}
                for repeat in range(6):
                    for name in (tuple(sqls) if repeat % 2 == 0 else tuple(sqls)[::-1]):
                        wall, cpu = time.perf_counter(), time.process_time()
                        db.execute(sqls[name], args).fetchall()
                        samples[name].append(((time.perf_counter() - wall) * 1000,
                                              (time.process_time() - cpu) * 1000))
                row = {'match': match, 'where': where, 'same': True, 'hits': len(outputs['rank']),
                       'arms': {name: {'wall_ms': statistics.median(x[0] for x in samples[name]),
                                       'cpu_ms': statistics.median(x[1] for x in samples[name]),
                                       'plan': db.execute('EXPLAIN QUERY PLAN ' + sql, args).fetchall()}
                                for name, sql in sqls.items()}}
                results.append(row)
                print(json.dumps(row), flush=True)
        if args.out:
            args.out.write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
        db.close()


if __name__ == "__main__":
    main()
