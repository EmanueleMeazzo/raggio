"""Two-stage BM25 probe: pruned-OR + unranked-AND candidates, full-query Python rescore.

Evidence for ADR 0003: replicates _prune_common against the LIVE bench meta.db, then
runs the proposed candidate-generation + Python BM25 pipeline over random ingested-doc
titles (seed 7 -- independent of the seed-42 bench queries) and reports the target doc's
rank. Measured on the 2.55M arXiv corpus: hit@5 0.975 / hit@10 0.983 / hit@50 1.000,
median 85 ms. Read-only; run on the bench host via `podman unshare` (volume file perms).
"""
import sqlite3, re, unicodedata, random, statistics, time, math

DB = "/home/emeazzo/.local/share/containers/storage/volumes/bench-tv/_data/collections/bench/meta.db"
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
N_ROWS = db.execute("SELECT COUNT(*) FROM records WHERE indexed=1").fetchone()[0]
budget = max(1000, int(0.02 * N_ROWS))
K1, B = 1.2, 0.75

def fold(t):
    return "".join(c for c in unicodedata.normalize("NFKD", t.lower()) if not unicodedata.combining(c))

dfc = {}
def df(k):
    if k not in dfc:
        r = db.execute("SELECT doc FROM records_fts_v WHERE term=?", (k,)).fetchone()
        dfc[k] = r[0] if r else 0
    return dfc[k]

def prune(toks):
    dfs = {}
    for t in toks:
        k = fold(t)
        if k not in dfs:
            dfs[k] = df(k)
    spent, kept, have = 0, set(), False
    for k in sorted(dfs, key=dfs.get):
        d = dfs[k]
        if d and have and spent + d > budget:
            break
        spent += d
        kept.add(k)
        have = have or d > 0
    return [t for t in toks if fold(t) in kept]

# avgdl from a 256-doc sample
random.seed(11)
maxid = db.execute("SELECT MAX(id) FROM records").fetchone()[0]
dls = []
while len(dls) < 256:
    g = random.randint(1, maxid)
    r = db.execute("SELECT text FROM records WHERE id>=? AND indexed=1 LIMIT 1", (g,)).fetchone()
    if r:
        dls.append(len(re.findall(r"\w+", r[0])))
AVGDL = statistics.mean(dls)
print(f"rows={N_ROWS} budget={budget} avgdl~{AVGDL:.0f}", flush=True)

random.seed(7)  # same sample stream as probeA2
samples, seen = [], set()
while len(samples) < 300:
    g = random.randint(1, maxid)
    row = db.execute("SELECT id, text FROM records WHERE id>=? AND indexed=1 LIMIT 1", (g,)).fetchone()
    if row and row[0] not in seen:
        seen.add(row[0])
        samples.append((row[0], row[1].split("\n\n", 1)[0]))
print("sampled", flush=True)

def pybm25(qtoks, cand_rows, n):
    """cand_rows: list of (id, text). Score full query, return top-n ids."""
    qkeys = list(dict.fromkeys(fold(t) for t in qtoks))
    idfs = {}
    for k in qkeys:
        d = df(k)
        idfs[k] = max(math.log((N_ROWS - d + 0.5) / (d + 0.5)), 1e-6) if d else 0.0
    scored = []
    for rid, text in cand_rows:
        toks = [fold(t) for t in re.findall(r"\w+", text)]
        dl = len(toks)
        tf = {}
        for t in toks:
            if t in idfs:
                tf[t] = tf.get(t, 0) + 1
        s = sum(idfs[t] * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / AVGDL)) for t, f in tf.items())
        scored.append((s, rid))
    scored.sort(key=lambda x: -x[0])
    return [rid for _, rid in scored[:n]]

N = 120
ranks, mss, cand_sizes = [], [], []
for i, (rid, title) in enumerate(samples[:N]):
    toks = re.findall(r"\w+", title)[:100]
    t0 = time.perf_counter()
    kept = prune(toks)
    m_cur = " OR ".join(f'"{t}"' for t in kept)
    m_and = " AND ".join(f'"{t}"' for t in toks)
    c1 = [r[0] for r in db.execute(
        "SELECT rowid FROM records_fts WHERE records_fts MATCH ? ORDER BY rank LIMIT 500", (m_cur,))] if kept else []
    c2 = [r[0] for r in db.execute(
        "SELECT rowid FROM records_fts WHERE records_fts MATCH ? LIMIT 1000", (m_and,))]
    cand = list(dict.fromkeys(c1 + c2))
    qm = ",".join("?" * len(cand))
    rows = db.execute(f"SELECT id, text FROM records WHERE id IN ({qm})", cand).fetchall()
    top = pybm25(toks, rows, 50)
    ms = (time.perf_counter() - t0) * 1000
    ranks.append(top.index(rid) + 1 if rid in top else None)
    mss.append(ms)
    cand_sizes.append(len(cand))
    if (i + 1) % 20 == 0:
        print(f"  {i+1}/{N} last_ms={ms:.0f}", flush=True)

n = len(ranks)
h1 = sum(1 for x in ranks if x == 1) / n
h5 = sum(1 for x in ranks if x and x <= 5) / n
h10 = sum(1 for x in ranks if x and x <= 10) / n
h50 = sum(1 for x in ranks if x) / n
lat = sorted(mss)
print(f"PROPOSED hit@1={h1:.3f} hit@5={h5:.3f} hit@10={h10:.3f} hit@50={h50:.3f}")
print(f"PROPOSED ms_med={lat[n//2]:.1f} ms_p90={lat[int(n*0.9)]:.1f} ms_max={lat[-1]:.1f}")
print(f"CAND median={statistics.median(cand_sizes)} max={max(cand_sizes)}")
misses = [(samples[i][1], ranks[i]) for i in range(n) if not ranks[i] or ranks[i] > 5]
print(f"MISSES (not top-5): {len(misses)}")
for t, r in misses[:10]:
    print(f"  rank={r} title={t[:90]}")
print("DONE")
