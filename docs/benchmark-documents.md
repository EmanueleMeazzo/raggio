# Benchmark: document retrieval (Wikipedia, 2026-11)

The [arxiv benchmark](benchmark-arxiv.md) stores one chunk per document and asks for
10 hits, so it never runs the queries a document store spends its time on: search
within one document, search over document summaries, and large result sets. Release
0.3.0's changes target exactly those (ADRs [0007](adr/0007-sofia-backend-requirements.md),
[0010](adr/0010-search-response-encoding.md), [0013](adr/0013-scoped-search-batching.md),
[0016](adr/0016-native-search-json.md)), and
[re-measured on arxiv](benchmark-arxiv-2026-11.md) they moved nothing. This benchmark
uses a corpus shaped like a document store and measures:

- **A:** raggio `19c3293`, the code before PRs #6–#14 (version 0.1.0).
- **B:** raggio 0.3.0 (`5da2b28`).
- **Weaviate 1.38.11** as a reference, on the same corpus, queries and client.

The first section has the current version's figures.

## Current version: 0.4.0 (turbovec 1.1.2), 2026-11-08

- **Source:** medians of 6 runs at concurrency 8, from the turbovec 1.1.2 A/B in
  [ADR 0017](adr/0017-turbovec-1-1.md) (`5581e4d`, the same host, corpus, queries and
  client; raw tables in `bench/results-turbovec-2026-11.md`).
- **Weaviate:** in parentheses, from the 2026-11-07 run below. The A/B's 0.3.0 arm
  reproduced that day's 0.3.0 figures within the run-to-run spread.
- **What moved:** on this corpus 0.4.0 moved only a few figures beyond the spread:
  summary vector p95 and CPU per query, summary hybrid throughput, about 14 MB more
  memory after the workload set, and flat recall@100 (below).

### Flat (4-bit scan + fp16 rescore)

| Query | p50 (ms) | QPS serial | QPS, 8 clients | p95, 8 clients (ms) | Server CPU per query, 8 clients (ms) | Quality |
|---|---|---|---|---|---|---|
| Summaries, vector, k=10 | **6.9** (21.2) | **145** (51.7) | **522** (285) | **21.0** (33.5) | **4.5** (24.9) | 0.996 (0.996) |
| Summaries, hybrid, k=10 | **9.2** (19.6) | **87.7** (50.7) | 242 (**262**) | 42.6 (**35.7**) | **12.7** (30.1) | 0.994 (0.994) |
| One document, vector, k=5 | **3.2** (5.9) | **295** (177) | 1,076 (**1,201**) | 13.7 (**11.3**) | **1.2** (4.4) | 0.993 (0.993) |
| One document, hybrid, k=5 | **5.7** (7.3) | **160** (134) | 613 (**1,011**) | 19.1 (**12.2**) | **2.5** (6.7) | 1.000 (1.000) |
| Chunks, k=10 | 12.2 (**8.1**) | 81.6 (**131**) | 338 (**986**) | 26.9 (**11.8**) | **6.1** (6.6) | **1.000** (0.979) |
| Chunks, k=100 | 19.4 (**17.2**) | 49.9 (**55.7**) | 179 (**396**) | 55.3 (**25.7**) | **10.0** (18.6) | **1.000** (0.982) |
| Chunks, k=1000 | **67.9** (74.9) | **13.0** (12.3) | 21.4 (**87.7**) | 390 (**110**) | **61.0** (83.3) | **1.000** (0.949) |

### IVF index (nlist 256, nprobe 16)

| Query | p50 (ms) | QPS serial | QPS, 8 clients | p95, 8 clients (ms) | Server CPU per query, 8 clients (ms) | Quality |
|---|---|---|---|---|---|---|
| Summaries, vector, k=10 | **6.4** (21.2) | **153** (51.7) | **324** (285) | **27.8** (33.5) | **13.2** (24.9) | 0.996 (0.996) |
| Summaries, hybrid, k=10 | **9.3** (19.6) | **84.7** (50.7) | 182 (**262**) | 61.8 (**35.7**) | **24.4** (30.1) | 0.994 (0.994) |
| One document, vector, k=5 | **3.4** (5.9) | **286** (177) | 847 (**1,201**) | 13.4 (**11.3**) | **1.6** (4.4) | 0.993 (0.993) |
| One document, hybrid, k=5 | **5.7** (7.3) | **157** (134) | 683 (**1,011**) | 17.4 (**12.2**) | **2.6** (6.7) | 1.000 (1.000) |
| Chunks, k=10 | **7.0** (8.1) | **138** (131) | 306 (**986**) | 31.4 (**11.8**) | 15.8 (**6.6**) | **1.000** (0.979) |
| Chunks, k=100 | **13.2** (17.2) | **71.7** (55.7) | 144 (**396**) | 69.5 (**25.7**) | 22.6 (**18.6**) | **1.000** (0.982) |
| Chunks, k=1000 | **62.3** (74.9) | **14.6** (12.3) | 20.6 (**87.7**) | 411 (**110**) | 139 (**83.3**) | **1.000** (0.949) |

- **Quality** is the own-target hit rate (summaries, one document) or recall@k against
  exact search (chunks), rounded. At full precision:
  - flat recall@100 is 0.99984–0.99986 (0.3.0: 0.99988, 6 misses in 50,000);
  - IVF recall@100 is 0.99988;
  - recall@1000 is 0.999895 on both indexes.
- **Ingest:** 2,373 records/s (two runs: 2,392 and 2,354).
- **Memory and disk:** after the workload set, 489 MB (flat) and 481 MB (IVF), from
  148–151 MB before the queries. Disk: 2,041 MB (flat) and 2,058 MB (IVF).
- **IVF:** builds in 4.1 s and detaches in 2.1 s.

## 0.3.0 against `19c3293` (2026-11-07)

Medians, concurrency 8, A → **B** (Weaviate), flat index. One-document search uses
each version's way of scoping to a document: `19c3293` has no document filter, so it
filters on a `doc` metadata key every record carries; 0.3.0 uses the `doc_id` filter.

| Query | p50 | QPS, 8 clients | Server CPU per query, 8 clients |
|---|---|---|---|
| One document, vector, k=5 | 197 ms → **2.9 ms** (5.9 ms) | 29 → **902** (1,201) | 265 ms → **1.4 ms** (4.4 ms) |
| One document, hybrid, k=5 | 200 ms → **5.5 ms** (7.3 ms) | 24 → **607** (1,011) | 349 ms → **2.6 ms** (6.7 ms) |
| Summaries, vector, k=10 | 7.6 ms → **7.5 ms** (21.2 ms) | 393 → **461** (285) | 17.6 ms → **7.7 ms** (24.9 ms) |
| Summaries, hybrid, k=10 | 10.3 ms → **9.9 ms** (19.6 ms) | 135 → **222** (262) | 39.2 ms → **13.7 ms** (30.1 ms) |
| Chunks, k=100 | 18.7 ms → **18.1 ms** (17.2 ms) | 117 → **166** (396) | 31.3 ms → **12.0 ms** (18.6 ms) |
| Chunks, k=1000 | 73.7 ms → **68.7 ms** (74.9 ms) | 14.2 → **21.2** (87.7) | 204 ms → **65.7 ms** (83.3 ms) |

- **One document:** 68× lower p50 for vector search and 36× for hybrid, 25–31× the
  throughput, and about 1/130 to 1/190 of the server CPU per query.
- **Summaries and large result sets:** throughput under concurrency rises 17–64%
  (summaries) and 42–49% (k=100 and k=1000) at a third to a half of the server CPU per
  query, and p95 under load falls (summary hybrid 136 → 44 ms, k=1000 812 → 399 ms).
  Median single-query latency does not move beyond the run-to-run spread on the flat
  index, as [ADR 0013](adr/0013-scoped-search-batching.md) found for one client.
- **Quality is identical** between the two raggio versions: the own summary in the top
  10 for 99.6% (vector) and 99.4% (hybrid) of title queries, the source chunk in the top 5 for 99.3% (vector) and
  100% (hybrid) of in-document queries, and recall@k of 1.000 at k=10 and 100 (0.9999
  at k=1000). Weaviate's HNSW recall is 0.979, 0.982 and 0.949 at those k.
- **Against Weaviate:** 0.3.0 answers single summary and one-document queries faster
  (7.5 against 21.2 ms; 2.9 against 5.9 ms); Weaviate serves more concurrent queries in
  every row but summary vector search, and 4× the throughput at k=1000, at lower recall.
- **One row is worse in 0.3.0:** after the workload set, the server holds about 474 MB
  against 366 MB for `19c3293` (both start at 151 MB; Weaviate holds about 2.1 GB).
  The ~110 MB difference repeats in every run and was not investigated.

### Flat (exact 4-bit scan + fp16 rescore)

Each cell: `19c3293` → **0.3.0** (Weaviate). Quality is the own-target hit rate
(summaries, one document) or recall@k against exact search (chunks).

| Query | p50 (ms) | QPS serial | QPS, 8 clients | p95, 8 clients (ms) | Server CPU per query, 8 clients (ms) | Quality |
|---|---|---|---|---|---|---|
| Summaries, vector, k=10 | 7.6 → **7.5** (21.2) | 128 → **131** (51.7) | 393 → **461** (285) | 28.4 → **23.0** (33.5) | 17.6 → **7.7** (24.9) | 0.996 → **0.996** (0.996) |
| Summaries, hybrid, k=10 | 10.3 → **9.9** (19.6) | 79.2 → **83.4** (50.7) | 135 → **222** (262) | 136 → **44.1** (35.7) | 39.2 → **13.7** (30.1) | 0.994 → **0.994** (0.994) |
| One document, vector, k=5 | 197 → **2.9** (5.9) | 5.1 → **338** (177) | 28.8 → **902** (1,201) | 307 → **12.5** (11.3) | 265 → **1.4** (4.4) | 0.993 → **0.993** (0.993) |
| One document, hybrid, k=5 | 200 → **5.5** (7.3) | 5.0 → **160** (134) | 24.2 → **607** (1,011) | 366 → **20.4** (12.2) | 349 → **2.6** (6.7) | 1.000 → **1.000** (1.000) |
| Chunks, k=10 | 12.8 → **12.1** (8.1) | 76.7 → **80.4** (131) | 319 → **356** (986) | 35.9 → **26.0** (11.8) | 20.9 → **7.6** (6.6) | 1.000 → **1.000** (0.979) |
| Chunks, k=100 | 18.7 → **18.1** (17.2) | 51.4 → **53.3** (55.7) | 117 → **166** (396) | 89.8 → **65.4** (25.7) | 31.3 → **12.0** (18.6) | 1.000 → **1.000** (0.982) |
| Chunks, k=1000 | 73.7 → **68.7** (74.9) | 12.2 → **13.0** (12.3) | 14.2 → **21.2** (87.7) | 812 → **399** (110) | 204 → **65.7** (83.3) | 1.000 → **1.000** (0.949) |

Beyond both versions' run-to-run spread: every one-document latency, throughput and CPU
cell; QPS, p95 and CPU at 8 clients in every other row, except summary vector p95 and
chunk QPS at k=10 (+11%), which are within it; the serial p95 at k=1000. The other serial
cells and every quality cell are within the spread.

### IVF index (nlist 256, nprobe 16)

| Query | p50 (ms) | QPS serial | QPS, 8 clients | p95, 8 clients (ms) | Server CPU per query, 8 clients (ms) | Quality |
|---|---|---|---|---|---|---|
| Summaries, vector, k=10 | 6.6 → **6.5** (21.2) | 149 → **151** (51.7) | 257 → **313** (285) | 36.4 → **29.7** (33.5) | 16.3 → **13.8** (24.9) | 0.996 → **0.996** (0.996) |
| Summaries, hybrid, k=10 | 9.6 → **8.8** (19.6) | 83.3 → **90.9** (50.7) | 102 → **180** (262) | 192 → **58.7** (35.7) | 39.0 → **24.3** (30.1) | 0.994 → **0.994** (0.994) |
| One document, vector, k=5 | 197 → **3.0** (5.9) | 5.1 → **323** (177) | 29.7 → **776** (1,201) | 294 → **13.6** (11.3) | 254 → **1.7** (4.4) | 0.993 → **0.993** (0.993) |
| One document, hybrid, k=5 | 199 → **5.7** (7.3) | 5.1 → **156** (134) | 24.1 → **600** (1,011) | 368 → **17.3** (12.2) | 345 → **2.8** (6.7) | 1.000 → **1.000** (1.000) |
| Chunks, k=10 | 7.2 → **7.0** (8.1) | 135 → **137** (131) | 248 → **295** (986) | 40.0 → **32.0** (11.8) | 19.0 → **16.3** (6.6) | 1.000 → **1.000** (0.979) |
| Chunks, k=100 | 13.4 → **13.0** (17.2) | 70.3 → **72.8** (55.7) | 110 → **145** (396) | 90.9 → **69.1** (25.7) | 31.1 → **23.3** (18.6) | 1.000 → **1.000** (0.982) |
| Chunks, k=1000 | 66.9 → **61.9** (74.9) | 13.5 → **14.5** (12.3) | 14.4 → **20.2** (87.7) | 724 → **419** (110) | 206 → **140** (83.3) | 1.000 → **1.000** (0.949) |

The Weaviate column repeats the same reference figures in both tables. Beyond both
versions' spread: every one-document latency, throughput and CPU cell; QPS, p95 and CPU
at 8 clients in every other
row; the serial p50 of summary hybrid search (9.6 → 8.8 ms) and of k=1000 (66.9 → 61.9 ms)
and summary hybrid serial QPS. Quality is within the spread everywhere. The IVF index
builds in 4–12 s and detaches in about 2 s on this corpus, in both versions.

### Ingest, memory and disk

| | `19c3293` | 0.3.0 | Weaviate |
|---|---|---|---|
| Ingest, 173,320 records (records/s; two runs per raggio version) | 2,147 (2,152 and 2,142) | **2,441** (2,464 and 2,417) | 1,033 (one run) |
| Memory after a restart, before queries (MB) | 151 | 151 | about 1,800 |
| Memory after the workload set (MB) | **366** | 474 | about 2,100 |
| Disk (MB) | 2,041 (flat), 2,058 (IVF) | 2,041 (flat), 2,058 (IVF) | 2,523 |

Memory is `podman stats` of the server container (cgroup memory, page cache
included). Ingest is the time for the client's batched document posts plus the server's
job queue to drain, as in the arxiv benchmark.

## What changed in 0.3.0, per row

- **One-document search:** `19c3293` can only scope to a document through a metadata
  filter, which builds its allowlist with a `json_extract` scan over every record
  (about 200 ms per query at 173k records, whatever k is). 0.3.0's
  `doc_id` filter reads the document's records through an index, scores allowlists no
  larger than the rescore depth from their fp16 originals directly, and runs a
  document-scoped BM25 leg instead of a corpus-wide one
  ([ADR 0007](adr/0007-sofia-backend-requirements.md)).
- **Summaries and chunk scope under concurrency:** both are scope-only searches (the
  collection holds summaries and chunks), which 0.3.0 coalesces into bounded batches
  when they run concurrently ([ADR 0013](adr/0013-scoped-search-batching.md)). That
  lowers server CPU per query; a single client sees no change.
- **Large result sets:** 0.3.0 encodes hydrated hits directly instead of through
  FastAPI's recursive encoder ([ADR 0010](adr/0010-search-response-encoding.md)).
  Chunk-scope searches also go through ADR 0013's batching, so the k=100 and k=1000
  gains mix both changes; this benchmark does not separate them.
- **Ingest:** native JSON parsing and no vector copies on admission
  ([ADR 0008](adr/0008-native-ingest-json.md), [ADR 0009](adr/0009-ingest-vector-conversion.md));
  two runs per version, so the 14% is indicative.
- Not exercised here: the `unix-excl` SQLite VFS (its gain is on NFS; this ran on a
  local disk), relative fusion, `contains_any`, metadata keys in BM25 (`fts_keys`).

## Setup

- **Corpus** (`bench/prep_wiki.py`): the English Wikipedia dump `20220301.en` of the
  Hugging Face dataset `legacy-datasets/wikipedia` (revision `97a0b05`), read in dump
  order; the first 25,000 articles with a lead of at least 60 words and a body of at
  least 2,400 words, list pages excluded (all from the first of 41 shards). Each
  article is one document: a **summary** record (the lead, the paragraphs before the
  first section heading, cut to 300 words; mean 219) and up to 10 **chunks** of the
  body in ~800-word windows on paragraph boundaries (mean 822 words). 25,000 summaries
  and 148,320 chunks, 173,320 records, the shape of a document store (S.O.F.I.A.'s
  `legal_knowhow` collection holds about 22,300 summaries and 143,200 chunks). Every record
  carries the metadata `{"doc", "title", "kind"}`.
- **Embeddings:** Qwen3-Embedding-0.6B (1024 dims) on vLLM 0.27.1, as for the arxiv
  corpus; queries with the model's query instruction. 181.8M tokens, 73 minutes.
- **Queries** (seed 42, drawn from the stored documents): 500 **title** queries (an
  article's title; target: its summary) used for summary search and for chunk search
  at k=10 and k=100 (200 of them at k=1000); 300 **in-document** queries (a sentence of
  8–40 words from one chunk; target: that chunk, searched within its document).
  Hybrid queries send the same text to BM25 (RRF in raggio; Weaviate `rankedFusion`,
  alpha 0.5, over the text property).
- **Engines:** raggio in a **4 GiB** container, flat and with its IVF index (`nlist`
  256, `nprobe` 16); the two images differ only in `src/` (same CPython 3.12.15,
  SQLite 3.53.1, turbovec 1.0.0, native BM25 scorer). Weaviate 1.38.11 in an **8 GiB**
  container, HNSW defaults, the same records as objects with `kind` and `docId`
  properties for scoping. Every engine returns the same fields per hit (id, document,
  kind, title, text): 15 KB for 10 summaries, 24 KB for 5 chunks of one document,
  51 KB for 10 chunks, about 520 KB at k=100 and 5.2 MB at k=1000.
- **Host:** NVIDIA DGX Spark (GB10, 20 Arm cores), native Linux, rootless podman,
  one boot. The client (`bench/bench_docs.py`) runs on the same host.

## Protocol

1. **Ingest:** two ingests per raggio version, ordered A B A B, each into a fresh
   4 GiB container.
2. **Search sessions:** A1, B1, A2, B2 at concurrency 8. Each session builds the IVF
   index, runs a discarded run0 plus three measured runs of the IVF engine, detaches the
   index, then run0 plus three measured flat runs. Every run starts a fresh container,
   waits for the collection to load and its job queue to drain, then runs every
   workload's queries serially and then with 8 clients.
3. **Weaviate:** a fresh volume, one ingest, then run0 plus three measured runs, the
   container restarted before each.
4. **Host guard:** a 1 Hz sampler classed every process on the host; before each step
   the run waited for 120 s without other load, and a session counted only if other
   processes averaged under 0.25 cores over it, with no 10-second mean above 1 core
   (fixed before the run). The first A1 session overlapped another job on the host
   (1.5 cores on average) and was repeated after a 2 h 52 min wait; every session used
   here passed (0.011–0.016 cores).
5. **Statistics:** medians of the 6 measured runs per version and engine; a difference
   counts as real only when it exceeds both versions' bands (max minus min of their
   runs). Weaviate: the median of its 3 runs. Raw per-row values and bands:
   `bench/results-docs-2026-11.md`.

## Caveats

- One corpus: Wikipedia prose, 1024-dim vectors. Sofia's documents are legal and
  business text with 3072-dim vectors, where per-request parsing costs more
  ([ADR 0016](adr/0016-native-search-json.md)).
- `19c3293`'s one-document path is the metadata filter; a deployment that kept a
  separate collection per document, or filtered client-side, would see different
  "before" numbers.
- Weaviate ran with defaults and three runs; its HNSW trades recall for speed, and its
  ingest ran once.
- Concurrency 8 only; client and server share the host.

## Running it

```bash
# 1. the corpus (vLLM serving Qwen/Qwen3-Embedding-0.6B on :8001, as for the arxiv corpus,
#    with --max-num-batched-tokens 131072 --max-num-seqs 1024: chunks are long)
uv run --group bench --with pyarrow --with huggingface_hub python bench/prep_wiki.py --stage all

# 2. a raggio server on :18000 (ROOT_API_KEY=bench), then
uv run --group bench python bench/bench_docs.py --engine raggio --mode ingest --out ingest.json
uv run --group bench python bench/bench_docs.py --engine raggio --mode search --out flat.json
#    --doc-filter metadata for releases before 0.2.0; --engine raggio-ivf once the index is
#    built (POST /collections/docs/index); --engine weaviate against Weaviate on :18080
```
