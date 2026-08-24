# Isolates the per-shard fixed memory overhead of IdMapIndex (rotation matrix,
# calibration state, scan caches) that ivf_probe.py's in-process RSS deltas can't
# see through allocator reuse. Builds N calibrated shards with a few rows each.
# Usage: python bench/shard_mem_probe.py   (Linux; RssAnon needed)
import numpy as np
from turbovec import IdMapIndex

DIM = 1536


def rss_mb():
    for ln in open("/proc/self/status"):
        if ln.startswith("RssAnon:"):
            return int(ln.split()[1]) / 1024


r = np.random.default_rng(0)
sample = r.standard_normal((1024, DIM), dtype=np.float32)
sample /= np.linalg.norm(sample, axis=1, keepdims=True)
rows = r.standard_normal((64, DIM), dtype=np.float32)
rows /= np.linalg.norm(rows, axis=1, keepdims=True)

base = rss_mb()
print(f"base {base:.0f}MB", flush=True)
idxs = []
for target in (64, 256, 1024):
    while len(idxs) < target:
        ix = IdMapIndex(dim=DIM, bit_width=4)
        ix.calibrate(sample)  # forces rotation + calibration state
        ix.add_with_ids(rows, np.arange(64, dtype=np.uint64) + len(idxs) * 64)
        ix.prepare()  # forces scan caches
        idxs.append(ix)
    d = rss_mb() - base
    print(f"{target:5d} shards: +{d:.0f}MB total = {d / target:.3f}MB/shard", flush=True)
