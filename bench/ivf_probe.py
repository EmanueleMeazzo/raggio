# Feasibility probe for IVF coarse partitioning + TQ+ calibration, on the real
# bench corpus with the cached exact ground truth. Answers, per configuration:
# median per-query scan time, recall@10, and process RSS (per-shard index
# overhead like rotation matrices could sink the 1 GiB container budget).
# Usage: uv run python bench/ivf_probe.py   (from repo root; needs bench/corpus)
# IVF_SCALE=4 replicates the corpus with jittered near-duplicate rows (cos~0.93,
# like related emails) to probe the >2M regime where IVF should amortize its
# per-shard fixed cost; scaled ground truth + samples are computed once, cached.
import ctypes
import os
import time

import numpy as np
from turbovec import IdMapIndex

LIMIT, SEED, K, DIM = 553_015, 42, 10, 1536
SCALE = int(os.environ.get("IVF_SCALE", "1"))
NQ = int(os.environ.get("IVF_NQ", "500" if SCALE == 1 else "200"))
SIGMA = 0.4  # replica jitter: cos(orig, replica) ~ 1/sqrt(1+SIGMA^2) ~ 0.93
MEM_GUARD_MB = float(os.environ.get("IVF_MEM_GUARD_MB", "12000"))
BLOCK = 50_000
T00 = time.time()


def log(msg):
    print(f"[{time.time() - T00:7.1f}s] {msg}", flush=True)


def rss_mb():
    try:  # Linux (the container is where memory answers count); RssAnon excludes mmapped corpus pages
        st = open("/proc/self/status").read()
        for key in ("RssAnon:", "VmRSS:"):
            for ln in st.splitlines():
                if ln.startswith(key):
                    return int(ln.split()[1]) / 1024
    except OSError:
        pass

    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32)] + [
            (n, ctypes.c_size_t)
            for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                      "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                      "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

    # via psapi with explicit handle type; the kernel32.K32* route silently returned 0
    kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ok = psapi.GetProcessMemoryInfo(
        ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.byref(pmc), pmc.cb)
    return pmc.WorkingSetSize / 1e6 if ok else 0.0


# ---- corpus / queries: replicates bench.py load_corpus exactly (at SCALE=1) ----
vecs = np.load("bench/corpus/embed-vecs.npy", mmap_mode="r")
rng = np.random.default_rng(SEED)
query_rows = np.sort(rng.choice(LIMIT, size=NQ, replace=False))
is_q = np.zeros(LIMIT, bool)
is_q[query_rows] = True
ingest_rows = np.nonzero(~is_q)[0]
queries = np.round(np.asarray(vecs[query_rows], np.float32), 5)
queries /= np.linalg.norm(queries, axis=1, keepdims=True)
log(f"corpus ready: {LIMIT * SCALE - NQ} ingest rows ({SCALE}x scale), {NQ} queries")


def blocks():
    for s in range(0, len(ingest_rows), BLOCK):  # replica 0: originals, query rows held out
        rows = ingest_rows[s : s + BLOCK]
        b = np.asarray(vecs[rows], np.float32)
        b /= np.linalg.norm(b, axis=1, keepdims=True)
        yield rows, b
    for r in range(1, SCALE):  # jittered replicas (incl. of query rows: realistic near-dups)
        for s in range(0, LIMIT, BLOCK):
            rows = np.arange(s, min(s + BLOCK, LIMIT))
            b = np.asarray(vecs[rows], np.float32)
            b /= np.linalg.norm(b, axis=1, keepdims=True)
            g = np.random.default_rng((r << 24) ^ s).standard_normal(b.shape, dtype=np.float32)
            b += g * (SIGMA / np.sqrt(DIM))
            b /= np.linalg.norm(b, axis=1, keepdims=True)
            yield rows + r * LIMIT, b


# ---- ground truth + calibration/k-means samples ----
if SCALE == 1:
    # bench.py now dim-suffixes the gt cache; accept either name
    gt = np.load(next(p for p in ("bench/gt-553015-42-d1536.npz", "bench/gt-553015-42.npz")
                 if os.path.exists(p)))["gt"]
    srng = np.random.default_rng(7)
    cal_sample = np.asarray(vecs[np.sort(srng.choice(ingest_rows, 4096, replace=False))], np.float32)
    cal_sample /= np.linalg.norm(cal_sample, axis=1, keepdims=True)
    trng = np.random.default_rng(1)
    train = np.asarray(vecs[np.sort(trng.choice(ingest_rows, 65_536, replace=False))], np.float32)
    train /= np.linalg.norm(train, axis=1, keepdims=True)
else:
    GT_PATH = f"bench/gt-s{SCALE}-{LIMIT}-{SEED}-{NQ}.npz"  # generated, not for commit
    if os.path.exists(GT_PATH):
        z = np.load(GT_PATH)
        gt, cal_sample, train = z["gt"], z["cal"], z["train"]
        log(f"scaled gt + samples loaded from {GT_PATH}")
    else:
        n_total = LIMIT * SCALE - NQ
        cal_pick = np.sort(np.random.default_rng(7).choice(n_total, 4096, replace=False))
        train_pick = np.sort(np.random.default_rng(1).choice(n_total, 65_536, replace=False))
        cal_sample = np.empty((len(cal_pick), DIM), np.float32)
        train = np.empty((len(train_pick), DIM), np.float32)
        bs = np.full((2 * K, NQ), -np.inf, np.float32)  # running exact top-K per query
        bi = np.zeros((2 * K, NQ), np.int64)
        off = 0
        for ids, b in blocks():
            sims = b @ queries.T
            part = np.argpartition(-sims, K - 1, axis=0)[:K]
            bs[K:] = np.take_along_axis(sims, part, 0)
            bi[K:] = ids[part]
            keep = np.argpartition(-bs, K - 1, axis=0)[:K]
            bs[:K], bi[:K] = np.take_along_axis(bs, keep, 0), np.take_along_axis(bi, keep, 0)
            bs[K:] = -np.inf
            for pick, dest in ((cal_pick, cal_sample), (train_pick, train)):
                m = (pick >= off) & (pick < off + len(b))
                dest[np.nonzero(m)[0]] = b[pick[m] - off]
            off += len(b)
        gt = bi[:K].T.copy()
        np.savez(GT_PATH, gt=gt, cal=cal_sample, train=train)
        log(f"scaled gt + samples computed over {off} rows, cached to {GT_PATH}")
gtsets = [set(map(int, row)) for row in gt]


def recall(top_ids):
    return float(np.mean([len(set(map(int, ids)) & gtsets[i]) / K for i, ids in enumerate(top_ids)]))


# ---- full-scan baselines (uncalibrated = today's production; calibrated = path F) ----
def build_full(calibrated):
    idx = IdMapIndex(dim=DIM, bit_width=4)
    if calibrated:
        idx.calibrate(cal_sample)
    for rows, b in blocks():
        idx.add_with_ids(b, rows.astype(np.uint64))
    idx.prepare()
    return idx


for cal in (False, True) if SCALE == 1 else (True,):  # prod is calibrated; skip uncal at scale
    t0, r0 = time.time(), rss_mb()
    idx = build_full(cal)
    flat_delta = rss_mb() - r0
    log(f"full cal={cal} built in {time.time() - t0:.0f}s, state={idx.calibration_state},"
        f" rss={rss_mb():.0f}MB (+{flat_delta:.0f})")
    ts, out = [], []
    for i in range(NQ):
        t0 = time.perf_counter()
        _, ids = idx.search(queries[i : i + 1], k=K)
        ts.append(time.perf_counter() - t0)
        out.append(ids[0])
    log(f"FULL cal={cal}: p50={np.median(ts) * 1000:.2f}ms recall@10={recall(out):.4f}")
    del idx


# ---- IVF sweep ----
def kmeans(sample, nlist, iters=8):
    r = np.random.default_rng(0)
    C = sample[r.choice(len(sample), nlist, replace=False)].copy()
    for _ in range(iters):
        asg = assign(sample, C)
        for j in range(nlist):
            m = asg == j
            if m.any():
                C[j] = sample[m].mean(0)
        C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-12
    return C


def assign(mat, C):
    return np.concatenate([(mat[i : i + 8192] @ C.T).argmax(1) for i in range(0, len(mat), 8192)])


def build_ivf(nlist, C, cal_mode, train_asg):
    shards = [IdMapIndex(dim=DIM, bit_width=4) for _ in range(nlist)]
    for j, sh in enumerate(shards):
        if cal_mode == "global":
            sh.calibrate(cal_sample)
        elif cal_mode == "local":
            m = train_asg == j
            sh.calibrate(np.ascontiguousarray(train[m]) if m.sum() >= 64 else cal_sample)
    for rows, b in blocks():
        asg = assign(b, C)
        for j in np.unique(asg):
            m = asg == j
            shards[j].add_with_ids(np.ascontiguousarray(b[m]), rows[m].astype(np.uint64))
    for sh in shards:
        if len(sh):
            sh.prepare()
    return shards


deltas = {}  # nlist -> anon-RSS delta of the global-cal build, for per-shard overhead + guard
for nlist in (16, 64, 256, 1024) if SCALE == 1 else (64, 256, 1024):
    if len(deltas) >= 2:  # project this build from the per-shard slope; skip if it would OOM
        ns = sorted(deltas)
        slope = (deltas[ns[-1]] - deltas[ns[-2]]) / (ns[-1] - ns[-2])
        proj = rss_mb() + deltas[ns[0]] + slope * (nlist - ns[0])
        if proj > MEM_GUARD_MB:
            log(f"skip nlist={nlist}: projected rss {proj:.0f}MB > guard {MEM_GUARD_MB:.0f}MB")
            continue
    t0 = time.time()
    C = kmeans(train, nlist)
    train_asg = assign(train, C)
    log(f"kmeans nlist={nlist} trained in {time.time() - t0:.0f}s")
    for cal_mode in ("global", "local", "none") if nlist == 256 and SCALE == 1 else ("global",):
        t0, r0 = time.time(), rss_mb()
        shards = build_ivf(nlist, C, cal_mode, train_asg)
        sizes = np.array([len(sh) for sh in shards])
        if cal_mode == "global":
            deltas[nlist] = rss_mb() - r0
        log(f"built nlist={nlist} cal={cal_mode} in {time.time() - t0:.0f}s, "
            f"shard rows p50={np.median(sizes):.0f} max={sizes.max()},"
            f" rss={rss_mb():.0f}MB (+{rss_mb() - r0:.0f})")
        for nprobe in (1, 2, 4, 8, 16, 32):
            if nprobe > nlist:
                break
            ts, out = [], []
            for i in range(NQ):
                q = queries[i : i + 1]
                t0 = time.perf_counter()
                probe = np.argpartition(-(C @ q[0]), min(nprobe, nlist - 1))[:nprobe]
                alls, alli = [], []
                for j in probe:
                    if len(shards[j]):
                        s, ids = shards[j].search(q, k=K)
                        alls.append(s[0])
                        alli.append(ids[0])
                s, ids = np.concatenate(alls), np.concatenate(alli)
                top = np.argsort(-s)[:K]
                ts.append(time.perf_counter() - t0)
                out.append(ids[top])
            log(f"IVF nlist={nlist} cal={cal_mode} nprobe={nprobe}: "
                f"p50={np.median(ts) * 1000:.2f}ms recall@10={recall(out):.4f}")
        del shards

log("done")
