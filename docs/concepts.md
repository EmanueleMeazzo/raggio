# Concepts & terminology

The rest of these docs assume some information-retrieval and vector-search
vocabulary. This page defines all of it, in reading order — each group builds
on the one before, and every term is explained by what raggio actually does
with it. Two words first: **RAG** (retrieval-augmented generation) is the
pattern of fetching the relevant pieces of your own data and handing them to
an LLM as context — raggio is the retrieval half. A **chunk** is one such
piece: you split documents into chunks before ingest, and the chunk is the
unit raggio stores, indexes, and returns
([Getting started](getting-started.md)).

## Embeddings & vector search

### Embedding vector

A list of numbers a machine-learning model produces from a piece of text,
built so that texts with similar *meaning* land at nearby points: "2026
payment terms" and "what do we owe next year" end up close together despite
sharing no words. raggio stores one embedding per chunk — either you ingest
a pre-computed `vector`, or raggio sends the chunk's `text` to an
OpenAI-compatible `/embeddings` endpoint and stores the reply. Searching by
meaning is then geometry: embed the query, find the closest stored vectors.

### Dimensions

How many numbers the vector has — fixed by the embedding model (1536 for
`text-embedding-3-small`, 1024 for Qwen3-Embedding-0.6B) and recorded as the
collection's `dim`. All vectors in a collection share one dimension, and both
memory and scan cost grow linearly with it ([Storage](storage.md)).

### Cosine similarity

raggio's vector score: the cosine of the angle between the query vector and
a stored vector. 1 means pointing the same way (same meaning), 0 unrelated,
−1 opposite; higher is better, and vector lengths don't matter — only
direction.

### L2 normalization

Scaling a vector to length 1. Once vectors are unit-length, cosine similarity
reduces to a plain dot product — one multiply-add per dimension, the cheapest
thing a CPU can stream. raggio normalizes vectors on the way in, and
renormalizes decoded fp16 candidates before rescoring because fp16 storage
drifts lengths by about a part in a thousand
([ADR 0003](adr/0003-recall-rescoring.md)).

### Top-k / k-nearest-neighbor (k-NN)

"Give me the `k` stored vectors most similar to the query" — the `k` field in
every search request, and the shape of every result list in these docs. The
returned hits are the query's k nearest neighbors under cosine similarity.

### Exact vs approximate (ANN) search

An **exact** search (also *brute-force* or *flat*) compares the query against
every stored vector, so it cannot miss the true top-k of the representation
it scans. An **approximate nearest neighbor (ANN)** search uses an index
structure (IVF, HNSW — [below](#index-structures)) to inspect only a subset,
trading a little correctness for a lot of speed. raggio's default is an
exact scan over quantized codes; attaching an [IVF index](indexing.md) makes
vector search approximate, with `nprobe` controlling how approximate.

### Recall@k and ground truth

Recall@k measures what an approximate (or quantized) search misses: the
fraction of the *true* top-k it returned. The truth — the **ground truth** —
is computed the slow, exact way: full-precision float32 cosine over the whole
corpus, done once offline in the [benchmarks](benchmark.md); no engine is
told it. Recall@10 = 0.976 means the engine's top-10 contained, on average,
9.76 of the true 10.

## Quantization

### Scalar quantization

Compressing each number in a vector from a 32-bit float down to a tiny
integer code — *scalar* because every component is quantized on its own.
raggio's engine, [turbovec](https://github.com/RyanCodrai/turbovec), does
this with TurboQuant: the searchable copy of every vector lives as quantized
codes, and the whole scan runs directly on them.

### 4-bit / 2-bit codes, fp16, float32

These are all just precisions. Embedding models emit **float32** (32 bits per
number); **fp16** (half precision) halves that with negligible loss for
similarity work; and the collection's `bit_width` sets the searchable codes
at **4 bits** (32/4 ≈ 8x smaller than float32) or **2 bits** (≈16x). That 8x
is the headline: 552,515 × 1536-dim vectors fit a ~500 MB resident index
instead of 3.4 GB of raw floats, which is how a real collection serves from a
1 GiB container ([Benchmarks](benchmark.md), [sizing](storage.md#sizing)).

### What quantization costs

Ranking error, nothing else. With 4 bits per number, two similarly-close
vectors can swap order under the coarser arithmetic — measured as recall@10
of 0.976 on the email corpus before query-time rescoring existed — but stored
data is never degraded: raggio keeps an fp16 copy of every ingested vector
on disk ([Storage](storage.md)). The next two entries are how that cost is
clawed back.

### Calibration (TQ+)

Tuning the quantizer's levels to the actual distribution of your vectors
instead of a generic assumption. raggio calibrates each collection once,
automatically, from a running random sample when it reaches 10k vectors —
worth recall@10 0.960 → 0.969 for free on the email corpus. The policy is
deliberately calibrate-early-or-never: re-calibrating an already-encoded
index re-quantizes the codes and measurably loses recall
([ADR 0001](adr/0001-performance-optimization-decisions.md)).

### Rescoring (over-fetch + exact re-rank)

The industry-standard repair for quantization's ranking error. The quantized
scan **over-fetches** (also called *oversampling*): asked for k, it keeps
roughly 2k candidates (never fewer than 50 at 4-bit, 200 at 2-bit; never more
than 2000), then re-ranks just those by exact cosine against the stored fp16
originals. Quantization then only decides which candidates are *considered*,
never how returned hits are *ordered* — and returned scores are exact cosine
similarities. On the 2.55M arXiv corpus this took recall@10 from 0.949 to
1.000 for ~0.1–0.3 ms per query. Qdrant (`rescore` + oversampling), Weaviate
(`rescoreLimit`), Milvus (`refine_k`), and ScaNN all ship the same pattern;
raggio's version is [ADR 0003](adr/0003-recall-rescoring.md).

## Index structures

### Flat index

No structure at all: the quantized codes sit in one contiguous array and
every query scans all of them with SIMD (single-instruction-multiple-data —
CPU instructions that process many values per cycle). The scan is linear in
collection size but streams memory sequentially, so it is
memory-bandwidth-bound rather than compute-bound, and raggio batches
concurrent queries into a single pass over the data. This is every
collection's default; below ~1M vectors it is as fast as (or faster than) a
graph index, at higher recall ([Vector indexing](indexing.md)). A metadata
`filter` compiles to an **allowlist** — the set of record ids the filter
permits — which the scan intersects on the fly.

### IVF (inverted file index)

The coarse-partitioning ANN scheme ScaNN popularized, and the optional index
raggio offers. At build time, **k-means** clustering groups the
collection's vectors into `nlist` clusters: each cluster's mean is a
**centroid**, and each cluster becomes a **cell** — raggio calls them
**shards**, each a small quantized flat index of its own. A query scores only
the `nlist` centroids, then scans the `nprobe` closest shards — roughly
`nprobe / nlist` of the flat scan's work, e.g. 5.1 ms instead of 18.1 ms at
2.2M vectors with the `nlist=256, nprobe=16` defaults. The price is recall
(true neighbors sitting in unprobed shards are invisible — about 1 point of
recall@10 at the defaults), plus per-shard fixed costs: ~0.4 ms per probed
shard, allowlists intersected per shard, and no query micro-batching — which
is why it only pays off past ~2M vectors ([Vector indexing](indexing.md)).

### HNSW

Hierarchical Navigable Small World
([Malkov & Yashunin](https://arxiv.org/abs/1603.09320)) — the graph-based ANN
index Weaviate uses. Every vector becomes a node linked to its near
neighbors across several layers (coarse routing on top, the full graph at the
bottom); a search greedily hops from node to ever-closer node. It delivers
excellent recall at low latency, but the graph plus (typically uncompressed
float32) vectors must live in RAM — 1.5–2x raw vector size — and traversal is
a chain of dependent random memory reads that neither bandwidth nor extra
cores accelerate. raggio chose flat-by-default with IVF-on-demand instead:
below ~1M the flat scan already wins, HNSW's RAM appetite is hostile to the
small-container goal, and turbovec exposes no graph hooks
([ADR 0002](adr/0002-optional-ivf-index.md)).

## Text search (BM25)

### Tokenization

Splitting text into the units the text index matches on. raggio offers two
per-collection tokenizers ([Search](search.md#tokenizers)): `unicode61`
(default) splits on word boundaries in any language and strips diacritics, so
`perche` matches `perché`; `trigram` indexes every 3-character window
instead, buying case-insensitive substring matching (`gresql` finds
`PostgreSQL`) at the price of a several-fold larger index and noisier
ranking.

### Inverted index and posting lists

The data structure behind all fast text search — the same idea as a book
index. For every token it stores a **posting list**: the ids of all records
containing that token, with positions and counts. Answering a query means
walking and intersecting a few posting lists instead of reading every
document.

### FTS5

SQLite's built-in full-text search engine: the inverted index plus a
`bm25()` ranking function. raggio keeps an FTS5 index inside each
collection's `meta.db`, synchronized with the records by triggers
([Storage](storage.md)); every `text` and `hybrid` search runs on it.

### Term frequency, document frequency, IDF

The three counts BM25 is made of. **Term frequency (tf)**: how often a term
occurs in one record — more occurrences suggest more relevance, with
diminishing returns. **Document frequency (df)**: how many records contain
the term at all. **IDF (inverse document frequency)** turns df into a rarity
weight — a term found in 12 records tells you far more than one found in
500,000 — using, in FTS5's exact form, `ln((N − df + 0.5) / (df + 0.5))`.

### BM25

The standard lexical ranking function, from Robertson and Spärck Jones's
probabilistic relevance framework
([Robertson & Zaragoza 2009](https://dl.acm.org/doi/10.1561/1500000019) is
the canonical survey). A record's score sums, per query term, IDF × a
*saturated* term frequency: the **k1** parameter (1.2 in FTS5) caps how much
repetition helps, so ten occurrences aren't ten times better than one; the
**b** parameter (0.75) applies **document-length normalization**, discounting
tf in records longer than the collection's average length (*avgdl*) — a match
in a two-line record means more than the same match in a ten-page one. Scores
are positive, unbounded, and only comparable within one query. raggio uses
FTS5's `bm25()` directly on small collections and reproduces it exactly
(same k1, b, IDF) in Python on large ones — see
[two-stage retrieval](#two-stage-retrieval-candidate-generation-rescoring).

### Stopwords

Extremely common words ("the", "of", "and") that classic engines strip using
a fixed, language-specific list. raggio keeps no stopword list; instead it
measures df at query time and prunes the query dynamically — rarest tokens
first, until they cumulatively cover 2% of the corpus — which does the same
job in any language and adapts to each collection's actual vocabulary
([ADR 0001](adr/0001-performance-optimization-decisions.md)).

### Why common words are expensive (and WAND)

To rank a query, FTS5 must score *every* record matching any query term — one
near-universal token drags most of the corpus into the rank pass (measured
540–670 ms per query on 553k emails; seconds at 2.5M rows). Big engines avoid
this with **WAND / BlockMax WAND**: dynamic pruning that skips whole blocks
of documents whose best-possible score provably cannot reach the current
top-k — which is why Weaviate never needs to drop query terms. FTS5 has no
WAND and no hook to add one, so the *structure* of the query is raggio's
only latency lever ([ADR 0003](adr/0003-recall-rescoring.md)).

### Two-stage retrieval (candidate generation + rescoring)

raggio's answer on large collections: find plausible records cheaply first,
then rank them properly. **Stage 1 — candidate generation** at bounded cost:
FTS5 ranks only the pruned rarest tokens (top 500), unioned with an unranked
AND-of-all-tokens query (top 1000 — intersections are cheap because their
cost tracks the rarest term). **Stage 2 — rescoring**: full-query BM25 is
recomputed in Python over just those candidates (FTS5-parity k1/b/IDF), plus
the proximity bonus below, so a record matching the whole query beats one
that merely repeats its rarest word. Small collections skip stage 2 — the
single FTS5 query already ranks the full query there. This lifted the arXiv
known-item hit rate from 0.732 to 0.983
([ADR 0003](adr/0003-recall-rescoring.md),
[Search](search.md#text-query-handling)).

## Proximity & term dependence

### SDM (Sequential Dependence Model)

BM25 is bag-of-words: "york new products" and "new products from York" score
identically.
[Metzler & Croft 2005](https://dl.acm.org/doi/10.1145/1076034.1076115) showed
that rewarding query-term *proximity* reliably helps: **canonical SDM**
scores, alongside the individual terms (*unigrams*), **ordered windows**
(adjacent query
word pairs appearing adjacent and in order in the document) and **unordered
windows** (both words near each other in any order), each with its own
collection statistics and a small weight — reporting +5–9% MAP over
bag-of-words baselines.

### raggio's SDM-lite bonus

The Python rescorer implements just the ordered half: each query **bigram**
(adjacent word pair) found adjacent and in order in a record adds a
BM25-saturated bonus at weight 0.2, using the mean of the two words' IDFs in
place of true bigram statistics. It exists to recover title-as-phrase queries
that pure BM25 legitimately ranks below term-stuffed records.
[ADR 0003](adr/0003-recall-rescoring.md) flags its limits: the 0.2 weight
exceeds canonical SDM's ~0.12 ordered ratio, and it is validated on one
corpus, where it buys ~+0.008 text-hit.

### MAP (Mean Average Precision)

A classic IR evaluation metric for tasks with many relevant documents per
query: for each query, average the precision measured at each relevant
result's rank (so relevant results placed early count for more), then average
over all queries. It is the metric behind the SDM paper's +5–9%. raggio's
own benchmarks use single-target metrics instead — recall@10 and
text-hit@10 — because their tasks have exactly one right answer.

## Hybrid search & fusion

### Why combine BM25 with vectors

The two legs fail differently. The vector leg finds paraphrases and concepts
but has no notion of exact strings — an identifier, error code, or legal
phrase can rank anywhere. The BM25 leg nails exact terms but is blind to
synonyms and rephrasing. Hybrid mode runs both legs (concurrently, under the
same scope and filter) and fuses the two rankings — the robust default for
user-facing search ([Search](search.md)).

### Reciprocal Rank Fusion (RRF)

raggio's fusion rule: `score(id) = Σ 1 / (60 + rank_in_leg)`. It is
**rank-based**, not score-based — only positions matter, so there is nothing
to reconcile between a bounded cosine and an unbounded BM25 score, and a
record found by both legs beats one found by either alone. The constant
k = 60 comes from
[Cormack, Clarke & Büttcher 2009](https://dl.acm.org/doi/10.1145/1571941.1572114)
and damps the top ranks so a leg's #1 doesn't automatically dominate the
fusion. Fused scores land in `(0, 2/61]`, and there is nothing to configure.

### Leg depth

How many candidates each leg contributes to the fusion — raggio fetches
`max(100, k)` per leg. Deeper legs cost almost nothing and improve the fused
tail: a record ranked 70th by *both* legs can still make a fused top-10. The
100 matches Weaviate, which hard-codes 100-deep hybrid legs.

### Weaviate's rankedFusion and alpha

For comparison shopping: Weaviate's `rankedFusion` is the same k = 60 RRF,
and its `alpha` parameter weights the two legs — 0.5 means equal, equivalent
to raggio's unweighted RRF and the setting the [benchmarks](benchmark.md)
use. raggio deliberately ships no weighting knob today; weighted RRF is
noted as a possible future API in [ADR 0003](adr/0003-recall-rescoring.md).

## Benchmark vocabulary

Terms used on [Benchmarks](benchmark.md) and the
[arXiv benchmark](benchmark-arxiv.md) pages.

### Recall@10 vs exact

Each engine's top-10 compared against the offline exact float32 cosine top-10
(the [ground truth](#recallk-and-ground-truth)). It prices approximation: for
raggio, the quantization and — on the IVF column — the unprobed shards; for
Weaviate, the HNSW graph search. After
[rescoring](#rescoring-over-fetch-exact-re-rank), raggio's flat scan
measures 1.000 on the arXiv corpus: quantization no longer costs recall
there.

### Hybrid text-hit@10

A **known-item** task: one specific record is the target (the paper whose
exact title is the lexical query), and the metric asks whether it surfaces in
the fused top-10 — under deliberate vector noise, since the paired query
vector is an unrelated held-out abstract. It measures exact-term robustness
of the hybrid pipeline, not general relevance; with disjoint legs and RRF
k = 60 it effectively asks whether the target reaches the text leg's top-5.

### p50 / p95 / p99 latency

Percentiles of per-query response time: p50 is the median experience; p95 and
p99 are the tail — "95% (99%) of queries were at least this fast". Tails
matter because an application issuing several queries per user action hits
its p99 regularly; a good engine keeps the tail close to the median.

### QPS

Queries per second — throughput. **Serial** QPS runs one query at a time
(≈ 1000 / mean latency in ms); **concurrent** QPS allows several in flight (8
in the benchmarks) and rewards engines that overlap work — raggio's flat
scan micro-batches concurrent queries into a single pass over the codes,
which an attached IVF index gives up ([Vector indexing](indexing.md)).

### Cold start

Time from container start to the first answered query — loading indexes and
reconciling state from disk. It is what a restart or upgrade costs in
downtime; at multi-million scale, reloading a large HNSW index is the number
to watch.

### Container memory caps

The hard memory limits enforced on each engine's container (1 GiB vs 8 GiB on
the email run, 4 GiB vs 32 GiB on arXiv). They are the point of the
comparison: fixed before the run, enforced by the container runtime, and an
engine that outgrows its cap fails loudly instead of quietly consuming
more — the caps show what you must actually provision to serve the same
data.
