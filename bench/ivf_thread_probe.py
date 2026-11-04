"""IVF thread-count experiment under a CPU quota.

Run: uv run --group bench python bench/ivf_thread_probe.py --out results.json
Generated data, temporary local storage; protocol and limits in ADR 0010.
"""
import argparse
import sys
import json
import os
from pathlib import Path
import statistics
import time

import numpy as np
from turbovec import IdMapIndex
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from raggio.store import _IvfIndex, _normalize, _ShardPool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    rng = np.random.default_rng(719)
    dim, count, nlist = 1024, 65536, 16
    vectors = _normalize(rng.standard_normal((count, dim), dtype=np.float32))
    centroids = _normalize(rng.standard_normal((nlist, dim), dtype=np.float32))
    shards = []
    for j in range(nlist):
        index = IdMapIndex(dim, bit_width=4)
        ids = np.arange(j * (count // nlist) + 1, (j + 1) * (count // nlist) + 1, dtype=np.uint64)
        index.add_with_ids(vectors[j * (count // nlist):(j + 1) * (count // nlist)], ids)
        index.prepare()
        shards.append(index)
    del vectors
    ivf = _IvfIndex(centroids, shards, 8)
    queries = _normalize(rng.standard_normal((160, dim), dtype=np.float32))
    results = []
    for nq in (1, 8, 16):
        pools = {threads: _ShardPool(threads, 'quota-probe') for threads in (1, 3, 12)}
        samples = {threads: [] for threads in pools}
        batches = [queries[b * nq:(b + 1) * nq] for b in range(10)]
        ref = []
        ivf._pool = pools[1]
        for batch in batches:
            ref.append(ivf.search(batch, 50))
        for threads, pool in pools.items():
            ivf._pool = pool
            for batch, expected in zip(batches, ref):
                actual = ivf.search(batch, 50)
                assert np.array_equal(actual[0].view(np.uint32), expected[0].view(np.uint32))
                assert np.array_equal(actual[1], expected[1])
        for repeat in range(3):
            for threads in (tuple(pools) if repeat % 2 == 0 else tuple(pools)[::-1]):
                ivf._pool = pools[threads]
                wall, cpu = time.perf_counter(), time.process_time()
                for batch in batches:
                    ivf.search(batch, 50)
                samples[threads].append({'wall_ms_per_query': (time.perf_counter() - wall) * 1000 / (len(batches) * nq),
                                         'cpu_ms_per_query': (time.process_time() - cpu) * 1000 / (len(batches) * nq)})
        results.append({'nq': nq, 'arms': {threads: {key: statistics.median(row[key] for row in rows)
                                                    for key in rows[0]} for threads, rows in samples.items()},
                        'samples': samples, 'same_ids_and_score_bits': True})
        print(json.dumps(results[-1]), flush=True)
        for pool in pools.values():
            pool.close()
    out = {'rows': count, 'dim': dim, 'nlist': nlist, 'nprobe': 8, 'cpu_max': (Path('/sys/fs/cgroup/cpu.max').read_text().strip() if Path('/sys/fs/cgroup/cpu.max').exists() else None),
           'host_cpus': os.cpu_count(), 'results': results}
    if args.out:
        args.out.write_text(json.dumps(out, indent=2) + '\n', encoding='utf-8')


if __name__ == "__main__":
    main()
