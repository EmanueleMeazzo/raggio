"""Offline 4-bit flat index + fp16-rescore recall probe (read-only on corpus files).

Evidence for ADR 0003: rebuilds the 4-bit flat index from the bench corpus, searches
the 500 held-out queries at k=200, and measures recall@10 before and after re-ranking
the top-C candidates against fp16-precision originals. Measured on the 2.55M arXiv
corpus: base 0.9650, C=20..200 all 1.0000 at ~0.1-0.3 ms/query.
Run on the bench host (paths below assume the staged repo layout).
"""
import time
import numpy as np
from turbovec import IdMapIndex

VEC = "/home/emeazzo/raggio/bench/corpus/embed-vecs.npy"
GT = "/home/emeazzo/raggio/bench/gt-2549619-42-d1024.npz"
N, DIM, NQ = 2_549_619, 1024, 500

vecs = np.load(VEC, mmap_mode="r")
rng = np.random.default_rng(42)
qrows = np.sort(rng.choice(N, size=NQ, replace=False))
mask = np.zeros(N, bool)
mask[qrows] = True
ingest = np.nonzero(~mask)[0]
gt = np.load(GT)["gt"]
print(f"ingest={len(ingest)} queries={len(qrows)} gt={gt.shape}", flush=True)

def norm_block(rows):
    b = np.round(np.asarray(vecs[rows], dtype=np.float32), 5)
    b /= np.linalg.norm(b, axis=1, keepdims=True)
    return np.ascontiguousarray(b)

idx = IdMapIndex(dim=DIM, bit_width=4)
cal_rows = np.sort(np.random.default_rng(0).choice(ingest, 1024, replace=False))
idx.calibrate(norm_block(cal_rows))
t0 = time.time()
for s in range(0, len(ingest), 100_000):
    rows = ingest[s : s + 100_000]
    idx.add_with_ids(norm_block(rows), rows.astype(np.uint64))
    print(f"  added {s + len(rows)}/{len(ingest)} ({time.time()-t0:.0f}s)", flush=True)
idx.prepare()

q = norm_block(qrows)
t0 = time.time()
scores, ids = idx.search(q, k=200)
print(f"search 500 x k=200: {time.time()-t0:.1f}s", flush=True)

truth = [set(map(int, row)) for row in gt]
def recall10(idmat):
    return float(np.mean([len(set(map(int, idmat[i, :10])) & truth[i]) / 10 for i in range(NQ)]))

print(f"BASE recall@10 (4-bit top-10): {recall10(ids):.4f}")
# rescore top-C with fp16-precision originals (mirrors the stored fp16 blobs)
for C in (20, 30, 50, 100, 200):
    out = np.zeros((NQ, 10), dtype=np.int64)
    t0 = time.time()
    for i in range(NQ):
        cand = ids[i, :C].astype(np.int64)
        cv = np.asarray(vecs[cand], dtype=np.float32)
        cv /= np.linalg.norm(cv, axis=1, keepdims=True)
        cv = cv.astype(np.float16).astype(np.float32)  # fp16 storage round-trip
        sims = cv @ q[i]
        out[i] = cand[np.argsort(-sims)[:10]]
    ms = (time.time() - t0) * 1000 / NQ
    print(f"RESCORE C={C:4d}: recall@10={recall10(out):.4f} rescore-only={ms:.2f} ms/query (mmap reads)")
print("DONE")
