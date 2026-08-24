# Path F probe: does TQ+ calibration survive the production ingest flow?
# The ideal (uniform sample over the final corpus, calibrate before adding) is a
# +0.5pt recall win here (0.9606 -> 0.9654, see ivf_probe). Production can only
# calibrate from vectors seen so far — on this corpus the first 10k rows are a
# clustered prefix (paths sorted by folder/date), the docstring's warning case.
# Flows: C) calibrate at 10k from a first-10k reservoir, then add the rest;
#        D) same + milestone recalibrations at 100k and 550k from a growing reservoir.
# Usage: uv run python bench/cal_probe.py
import os
import time

import numpy as np
from turbovec import IdMapIndex

LIMIT, NQ, SEED, K, DIM = 553_015, 500, 42, 10, 1536
BLOCK = 50_000
T00 = time.time()


def log(msg):
    print(f"[{time.time() - T00:7.1f}s] {msg}", flush=True)


vecs = np.load("bench/corpus/embed-vecs.npy", mmap_mode="r")
rng = np.random.default_rng(SEED)
query_rows = np.sort(rng.choice(LIMIT, size=NQ, replace=False))
is_q = np.zeros(LIMIT, bool)
is_q[query_rows] = True
ingest_rows = np.nonzero(~is_q)[0]
# bench.py now dim-suffixes the gt cache; accept either name
gt = np.load(next(p for p in ("bench/gt-553015-42-d1536.npz", "bench/gt-553015-42.npz")
             if os.path.exists(p)))["gt"]
gtsets = [set(map(int, row)) for row in gt]
queries = np.round(np.asarray(vecs[query_rows], np.float32), 5)
queries /= np.linalg.norm(queries, axis=1, keepdims=True)


def norm_rows(rows):
    b = np.asarray(vecs[rows], np.float32)
    b /= np.linalg.norm(b, axis=1, keepdims=True)
    return b


def recall(idx):
    out = []
    for i in range(0, NQ, 50):
        _, ids = idx.search(queries[i : i + 50], k=K)
        out.extend(ids)
    return float(np.mean([len(set(map(int, ids)) & gtsets[i]) / K for i, ids in enumerate(out)]))


def sample_of(rows, n, seed):
    r = np.random.default_rng(seed)
    return norm_rows(np.sort(r.choice(rows, min(n, len(rows)), replace=False)))


def add_range(idx, lo, hi):
    for s in range(lo, hi, BLOCK):
        rows = ingest_rows[s : min(s + BLOCK, hi)]
        idx.add_with_ids(norm_rows(rows), rows.astype(np.uint64))


# C: production flow, single calibration at the 10k threshold
idx = IdMapIndex(dim=DIM, bit_width=4)
add_range(idx, 0, 10_000)
idx.calibrate(sample_of(ingest_rows[:10_000], 2048, seed=11))
add_range(idx, 10_000, len(ingest_rows))
idx.prepare()
log(f"C calibrate@10k (clustered prefix), no refit: recall@10={recall(idx):.4f}")
del idx

# D: + milestone refits from a reservoir that is uniform over everything seen so far
idx = IdMapIndex(dim=DIM, bit_width=4)
add_range(idx, 0, 10_000)
idx.calibrate(sample_of(ingest_rows[:10_000], 2048, seed=11))
add_range(idx, 10_000, 100_000)
idx.calibrate(sample_of(ingest_rows[:100_000], 2048, seed=12))
log(f"D refit@100k: recall so far (partial index, no GT) n={len(idx)}")
add_range(idx, 100_000, len(ingest_rows))
idx.calibrate(sample_of(ingest_rows, 2048, seed=13))
idx.prepare()
log(f"D calibrate@10k + refits @100k/@end: recall@10={recall(idx):.4f}")
log("reference: uncal=0.9606, ideal-cal=0.9654 (from ivf_probe)")
