# Decompose raggio search latency: raw scan, FTS leg, hydrate, GIL behavior.
# Usage: python microbench.py <data_dir>   (dir with index.tvim + meta.db)
import re
import sqlite3
import statistics as st
import sys
import threading
import time

import numpy as np
from turbovec import IdMapIndex

D = sys.argv[1]
REP = 15


def timed(fn, rep=REP, warmup=3):
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(rep):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return st.median(ts)


t0 = time.perf_counter()
idx = IdMapIndex.load(f"{D}/index.tvim")
t_load = (time.perf_counter() - t0) * 1000
t0 = time.perf_counter()
idx.prepare()
t_prep = (time.perf_counter() - t0) * 1000
print(f"load={t_load:.0f}ms prepare={t_prep:.0f}ms n={len(idx)}")

db = sqlite3.connect(f"file:{D}/meta.db?immutable=1", uri=True)
rng = np.random.default_rng(7)
qs = rng.standard_normal((30, 1536)).astype(np.float32)
qs /= np.linalg.norm(qs, axis=1, keepdims=True)

# CPU/memory-bandwidth reference: 100 MB f32 matvec, environment-normalizer
A = rng.standard_normal((17066, 1536)).astype(np.float32)
print(f"numpy matvec 100MB {timed(lambda: A @ qs[0], rep=10):8.1f} ms")

# --- vector leg ---
print(f"vector nq=1        {timed(lambda: idx.search(qs[:1], k=50)):8.1f} ms")
b = timed(lambda: idx.search(qs, k=50), rep=5)
print(f"vector nq=30 batch {b:8.1f} ms total = {b/30:.1f} ms/query")

# GIL: two scans in two threads; parallel speedup => kernel releases the GIL
def two(par):
    if par:
        ths = [threading.Thread(target=idx.search, args=(qs[i:i+1],), kwargs={"k": 50}) for i in range(2)]
        [t.start() for t in ths]
        [t.join() for t in ths]
    else:
        idx.search(qs[:1], k=50)
        idx.search(qs[1:2], k=50)

ser, par = timed(lambda: two(False), rep=5), timed(lambda: two(True), rep=5)
print(f"2 scans serial {ser:.1f} ms, threaded {par:.1f} ms -> {'GIL RELEASED' if par < ser * 0.75 else 'gil-bound'}")

# --- text leg: real path-token queries from stored rows ---
texts = [
    "emails " + r[0].rsplit(" emails ", 1)[1]
    for r in db.execute("SELECT text FROM records WHERE id IN (1000,50000,150000,300000,500000)")
]
print("sample query:", texts[0])

def fts_query(text):
    return " OR ".join(f'"{t}"' for t in re.findall(r"\w+", text)[:100])

def fts_current(text):
    return db.execute(
        "SELECT r.id, -bm25(records_fts) FROM records_fts"
        " JOIN records r ON r.id = records_fts.rowid"
        " WHERE records_fts MATCH ? AND indexed = 1 AND type = 'chunk'"
        " ORDER BY bm25(records_fts) LIMIT 50",
        [fts_query(text)],
    ).fetchall()

def fts_bare(text):  # no join/scope: pure FTS rank cost
    return db.execute(
        "SELECT rowid, rank FROM records_fts WHERE records_fts MATCH ? ORDER BY rank LIMIT 50",
        [fts_query(text)],
    ).fetchall()

def fts_selective(text, cut):  # drop tokens with doc-freq above cut
    toks = [t for t in re.findall(r"\w+", text)[:100] if dfs.get(t.lower(), 0) <= cut]
    if not toks:
        return []
    return db.execute(
        "SELECT rowid, rank FROM records_fts WHERE records_fts MATCH ? ORDER BY rank LIMIT 50",
        [" OR ".join(f'"{t}"' for t in toks)],
    ).fetchall()

db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.v USING fts5vocab(main, 'records_fts', 'row')")
dfs = {}
for text in texts:
    for t in set(w.lower() for w in re.findall(r"\w+", text)):
        r = db.execute("SELECT doc FROM temp.v WHERE term=?", (t,)).fetchone()
        dfs[t] = r[0] if r else 0
print("doc-freqs:", {t: dfs[t] for t in sorted(set(re.findall(r"\w+", texts[0].lower())))})

n = len(idx)
for name, fn in [
    ("fts current shape", fts_current),
    ("fts bare rank    ", fts_bare),
    (f"fts df<=10%      ", lambda t: fts_selective(t, n // 10)),
    (f"fts df<=1%       ", lambda t: fts_selective(t, n // 100)),
]:
    ms = st.median([timed(lambda: fn(t), rep=3, warmup=1) for t in texts])
    print(f"{name} {ms:8.1f} ms")

# --- hydrate ---
ids = [r[0] for r in db.execute("SELECT id FROM records LIMIT 50")]
qm = ",".join("?" * len(ids))
print(f"hydrate 50 rows    {timed(lambda: db.execute(f'SELECT id, external_id, doc_id, type, position, text, metadata FROM records WHERE id IN ({qm})', ids).fetchall()):8.1f} ms")
