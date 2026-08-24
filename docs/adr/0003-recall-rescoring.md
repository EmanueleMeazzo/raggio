# ADR 0003 — Exact rescoring: fp16 re-rank for vectors, two-stage full-query BM25

Status: accepted · Date: 2026-08-23

## Context

The 2.55M-row arXiv benchmark (`bench/results-arxiv.md`, real abstracts, 1024-d) exposed two
recall gaps vs Weaviate that the 553k email benchmark (ADR 0001) never showed:

| Metric | raggio | raggio-ivf | weaviate |
|---|---|---|---|
| Recall@10 vs exact | 0.949 | 0.960 | 0.995 |
| Hybrid text-hit@10 | 0.732 | 0.732 | 0.978 |

Root causes, both confirmed with read-only probes against the live bench data (DGX):

1. **Vector recall loss is pure 4-bit quantization ranking error.** An offline rebuild of the
   flat 4-bit index scored base recall@10 = 0.9650; re-ranking the top-C quantized candidates
   against the retained fp16 originals reached **recall@10 = 1.0000 already at C=20** (stable
   through C=200) at ~0.1–0.3 ms/query. The fp16 blobs (ADR 0002, kept for index rebuilds) were
   never consulted at query time — the one step every comparable engine ships (Qdrant
   `rescore`+oversampling, Weaviate `rescoreLimit`, Milvus `refine_k`, ScaNN reordering, DiskANN).

2. **Hybrid text-hit loss is the BM25 token pruning.** The 2%-df budget (ADR 0001) was validated
   on the email corpus, whose text was constant boilerplate + path tokens; on real text it
   collapses: median arXiv title = 10 tokens, median kept after pruning = **3**; 32% of titles
   keep ≤2 tokens (300-title sample). The BM25 leg then ranks the target by 1–3 rare tokens.
   Weaviate never drops query *terms* — BlockMax WAND skips *documents* via score upper bounds —
   and fuses 100-deep hybrid legs (hard-coded `DefaultQueryHybridMaximumResults = 100`).

FTS5 facts that constrain the fix (verified against fts5 source + measured on a 300k-row table):

- **No WAND / dynamic pruning**: `ORDER BY rank LIMIT n` scores every row matching the OR
  expression; bm25() additionally walks each phrase's full posting list once per query for IDF.
  A ranked full-OR title query on the 2.5M-row corpus takes seconds-to-minutes. Query
  *structure* is the only latency lever FTS5 offers.
- **Never rank a rowid-restricted MATCH**: bm25()'s per-query IDF cache is rebuilt on every
  xFilter call, so `MATCH ? AND rowid IN (...) ORDER BY rank` re-walks every phrase's posting
  list per candidate (measured 400×: 1155 ms vs 2.7 ms for 200 candidates). Rowid pushdown is
  fast (~13 µs/candidate) for membership only.
- **Python-side BM25 over candidates is the sanctioned pattern** (SQLite maintainers' guidance;
  APSW ships a reference port). FTS5's bm25 is reproducible exactly: k1=1.2, b=0.75 hardcoded,
  IDF = ln((N−df+0.5)/(df+0.5)) clamped to 1e-6.
- **AND queries are intersection-bounded** (doclist rowid seeks): cost tracks the rarest term
  even when co-terms are near-universal — cheap, high-precision candidate generation.

## Decisions

| Decision | Rationale / evidence |
|---|---|
| **fp16 rescore of vector candidates** — the quantized index over-fetches `C = min(max(floor, 2n), 2000, N)` (floor 50 at 4-bit, 200 at 2-bit), candidates re-ranked by exact cosine against the fp16 blobs; missing/corrupt blobs keep their quantized score | Probe: top-20 already contains the exact top-10 at 2.55M rows (recall@10 1.000). Cross-engine defaults bracket the multiplier (Weaviate rescoreLimit 20 for 8-bit RQ, Qdrant 3–4× oversampling for 1-bit BQ) |
| Rescore inside the existing batched-scan thread with **one union blob fetch per batch** (chunked ≤512 params) | Per-waiter round-trips would serialize sqlite point lookups behind the shared kernel pass and cut concurrent QPS |
| Rescore applies to all three search paths (batched scan, allowlist/filtered, sibling expansion) | One helper, consistent scores; sibling allowlists are doc-sized so cost is ~zero |
| Decoded fp16 candidates are renormalized before the dot product | fp16 round-trip drifts norms ~1e-3 — sub-point recall noise for near-ties |
| **Two-stage BM25** when pruning dropped tokens: stage 1 = pruned-OR ranked LIMIT 500 ∪ AND-of-all-tokens unranked LIMIT 1000 (both under scope/filter); stage 2 = full-query BM25 in Python over the candidates (FTS5-parity k1/b/IDF, df from the shared df cache, avgdl from a churn-invalidated 256-doc sample) | End-to-end probe on the live 2.55M meta.db: target-doc hit@5 0.975 / hit@10 0.983 / hit@50 1.000 vs 0.732 today (Weaviate fused: 0.978). Unranked AND avoids the IDF-walk footgun; the AND cap is rowid-ordered (documented ceiling — stage 1a's rarest-token guarantee covers only-common-token queries) |
| Nothing-pruned queries (small corpora, trigram collections) keep the single FTS5 query | It already ranks the full query; zero added cost for the common small-collection case |
| **SDM-lite proximity bonus** in the Python scorer (ordered query bigrams, BM25-saturated, weight 0.2) | Metzler & Croft sequential-dependence: +5–9% MAP; recovers title-as-phrase known-item queries that pure BM25 legitimately ranks below term-stuffed docs. Caveat: canonical SDM uses a lower ordered-window ratio (~0.12) and true bigram statistics; 0.2 with mean-of-unigram-IDFs is unvalidated beyond this corpus and buys only ~+0.008 text-hit here |
| **Hybrid leg depth 50 → 100** | Weaviate parity (it fuses 100-deep even for k=10). Contributes ~zero to the text-hit metric (with disjoint legs, RRF k=60 decides the fused top-10 inside each leg's top-5); kept for general fused-tail quality at near-zero vector-leg cost |
| Pruning (ADR 0001) stays, demoted to candidate generation | Still the only bound on FTS5's rank-pass cost; it just no longer decides final ranking |

## Rejected

- **WAND / BlockMax WAND in Python** — FTS5 exposes no per-term cursor with skipping; a Python
  reimplementation over fts5vocab would be slower than FTS5's C scan.
- **APSW custom rank function** — swaps the sqlite driver to gain in-SQL ranking that the Python
  rescorer already provides.
- **BM25F title column / separate title FTS index** — schema change + 2.5M-row reindex; per-field
  weighting can be added to the Python rescorer later without reindexing.
- **Relative-score fusion / weighted RRF** — the fusion formula is not the gap (Weaviate's
  rankedFusion at alpha 0.5 is the same k=60 RRF); candidate future API, separate decision.
- **SOAR spilled assignments, per-cell shard calibration, kNN-graph expansion** — heavier IVF
  recall levers; the rescore already removes the quantization loss they mostly target.

## Post-implementation audit (anti-benchmaxxing)

An independent ablation on the live bench db (120 real titles, target-in-text-leg-top-5,
which is what the fused metric reduces to) decomposed the text-hit gain:

| Variant | top-5 rate |
|---|---|
| pre-change (pruned-token FTS5 ranking) | 0.732 |
| two-stage full-query BM25 only | 0.933 |
| + AND-of-all-tokens candidate branch | 0.975 |
| + SDM bigram bonus (shipped) | 0.983 |
| Weaviate (published) | 0.978 |

Honest readings that follow:

- **Full-query rescoring is the fix** (~80% of the gain) and generalizes; it repairs a
  self-inflicted defect, not a benchmark quirk.
- **The AND branch is at its best case here**: bench queries are exact titles and every
  doc's text starts with its title, so the AND match set has median size 1 (the target)
  and a single typo empties it. It remains a legitimate general candidate source
  (conjunctive retrieval), but expect its contribution to shrink on paraphrased/typo'd
  queries. A perturbed-query bench column is future work.
- **The margin over Weaviate (0.984 vs 0.978) is 3 queries of 500 — within noise.**
  Claim parity, not victory. The SDM bonus supplies that margin (+0.008) and its weight
  (0.2) exceeds the canonical SDM ordered-window ratio (~0.12); it awaits cross-corpus
  validation.
- **Recall@10 = 1.000 means "quantization no longer costs recall on this corpus"**, not
  "beats HNSW". The over-fetch floors were validated on one corpus/model/dim; a corpus
  whose quantized top-2n misses the exact top-n would degrade silently — a per-request
  rescore-depth knob is the designated escape hatch if a real corpus ever needs it.
- Tuning provenance: the two-stage constants were selected on an independent seed-7
  random-title sample (`bench/text_probe.py`), not on the published seed-42 bench
  queries; probe scripts are committed under `bench/`.
- The re-run's cold-start/memory numbers were reuse artifacts (warm page cache,
  compacted files); the published tables keep the original matched-conditions rows for
  metrics whose code did not change.

## Consequences

- Vector scores are exact cosine similarities; text scores are positive BM25 points (Python
  provenance on the two-stage path). Ordering conventions unchanged.
- Vector queries pay one round of fp16 point lookups (~50–200 blobs); text queries that trigger
  stage 2 pay candidate text fetch + Python scoring (~tens of ms at 2.5M rows). Benchmarked
  before/after in `bench/results-arxiv.md`.
- The pre-existing hybrid p99 tail (a near-universal guarantee token forcing a large stage-1a
  rank pass) is unchanged — known future latency item, deliberately not coupled to this change.
