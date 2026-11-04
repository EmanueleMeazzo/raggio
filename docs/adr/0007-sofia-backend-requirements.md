# ADR 0007 — For the S.O.F.I.A. backend: relative fusion, a doc_id filter, exact small allowlists, a document-scoped BM25 leg, one process per data directory, SQLite on NFS

Status: accepted · Date: 2026-11-03 · Takes up the relative-score fusion ADR 0003 left as a candidate future API

## Context

raggio is about to replace Weaviate as the vector store behind the S.O.F.I.A. chat
backend. Three things that backend relies on had no raggio equivalent, and its DEV spike
found a fourth gap, in deployment, and two more on real data:

1. **Hybrid scores on Weaviate's scale.** The backend runs one hybrid query shape,
   Weaviate's default `relativeScoreFusion` (the default since Weaviate 1.24) with an
   explicit `alpha` (its own default 0.5, an admin slider), and acts on the scores: a
   hard-coded "top score >= 0.85" shortcut. raggio fused with RRF only, scores in
   `(0, 2/61]`, with no weighting knob.
2. **Document-scoped search.** "Search only the chunks of document X" (hybrid, `doc_id`
   == X, scope chunks, k=5) runs constantly, and so does paging one document's chunks.
   raggio filters only metadata, through `json_extract`, and listed `doc_id` as not
   filterable. Copying the id into metadata works, but every such search then builds
   its allowlist with a `json_extract` over every record (76 ms p50 at 100k chunks,
   below), linear in collection size and redone after every write (the allowlist cache
   holds 8 entries and every write clears it).
3. **Small allowlists.** `_vector_ids` hands every allowlist to a masked turbovec search
   and fp16-rescores what it returns, and `_expand` does the same once per hit for
   `siblings_topk`. A comment in `store.py` puts the masked search at ~15x a plain scan.
4. **Two processes on one `DATA_DIR`.** On Azure Container Apps (one replica, `DATA_DIR`
   on an Azure Files NFS 4.1 share mounted `hard,local_lock=none`), a revision restart
   is rolling: the platform starts the new replica, waits for its readiness probe, then
   sends SIGTERM to the old one. In the DEV logs the new replica finished start-up
   (`resume_pending` opened a collection and replayed its pending jobs) at 09:27:26;
   the old one, still serving, then failed three ingest enqueues with
   `sqlite3.DatabaseError: file is not a database` (`_enqueue_row`) at 09:27:27.9, :29
   and :34, got SIGTERM at :35, and its shutdown flush failed the same way. Two hosts
   had one WAL database open, and WAL's `-shm` wal-index is not coherent across hosts.
   Deploying a new revision overlaps the same way. Nothing in raggio stopped a second
   process from opening a `DATA_DIR`.
5. **The BM25 leg of a one-document search.** With 1-4 in place, the DEV spike ran real
   Sofia data (text-embedding-3-large, chunks of ~1000 tokens of legal and business
   prose, summaries and chunks in one collection; Azure Container Apps, 2 vCPU / 4 GiB,
   NFS 4.1 data dir, native scorer, warm; measured from a laptop with ~28 ms RTT).
   On `legal_knowhow` (22,339 summaries + 143,236 chunks), hybrid relative, alpha 0.5,
   scope chunks, `{"doc_id": X}`, k=5: 99 / 114 ms p50/p95 (main with a metadata
   `parent_id` filter: 544 / 620; Weaviate: 32 / 36). Scope summaries, k=10, no
   filter: 124 / 156 ms (Weaviate 54 / 56). `datascience` (2,983 + 40,634): 96 / 103
   in one document, 105 / 111 summaries (Weaviate 31 / 36). About 70-85 ms of the
   one-document figure is server time, and the BM25 leg dominates it: FTS5 ranks the
   pruned query over the whole corpus and filters by `doc_id` only after the join.
6. **SQLite's file locks on NFS.** With 1-5 deployed, the same collection and image,
   measured inside the container against 127.0.0.1 (p50 / p95 ms), on the NFS share
   (`vers=4.1,hard,local_lock=none,acregmin=30`) and on a local-disk copy of the same
   files: `/healthz` 0.6 / 0.7 and 0.5 / 0.5; `GET /collections/{name}` 117 / 130 and
   65 / 71; one-document vector 48 / 55 and 3.5 / 3.9, text 45 / 51 and 1.2 / 2.3,
   hybrid 35 / 49 and 3.8 / 4.6; summaries vector 66 / 74 and 25 / 34, text 74 / 98 and
   12 / 25, hybrid 80 / 101 and 42 / 53. Every request touching SQLite paid about
   40 ms on NFS. SQLite's default unix VFS takes and drops `fcntl` locks on the
   database and on its `-shm` file around every transaction; with `local_lock=none` each
   is an NFSv4 LOCK/LOCKU round trip, and the Linux client also flushes and drops the
   file's page cache when it takes a lock (locking is a cache-coherency point), so the
   pages come from the server again.

Weaviate's behaviour, read from its source (`usecases/traverser/hybrid/hybrid_fusion.go`,
`FusionRelativeScore`, and `searcher.go`, weaviate/weaviate main, 2026-11-03):

- per result set, `score = weight × (s − min) / (max − min)`; when `max == min` (all
  scores equal, one candidate included) the score is the weight itself, i.e. a
  normalised 1.0 ("If all scores are identical min and max are the same => just set
  score to the weight");
- an object's scores from both sets are summed, an absent set contributing nothing;
  the result is sorted by score, ties broken on the original score, then the UUID;
- the searcher runs the sparse (BM25) search only when `alpha < 1` and the dense search
  only when `alpha > 0`; weights are `alpha` (dense) and `1 − alpha` (sparse);
- the server's default alpha is 0.75 (`DefaultAlpha`,
  `adapters/handlers/graphql/local/common_filters/hybrid.go`); Weaviate fuses 100-deep
  legs, as raggio already does (ADR 0003).

## Decisions

| Decision | Rationale / evidence |
|---|---|
| **`fusion`: `"rrf"` \| `"relative"` on `SearchIn`, default `rrf`** | Existing clients see no change; relative fusion is opt-in per request |
| **Relative fusion is Weaviate's formula** (`_relative` in `store.py`): each leg min-max normalised to [0, 1], a leg of equal scores (one candidate included) → 1.0, `alpha × vector + (1 − alpha) × bm25`, a record a leg did not return adds 0; same `max(100, k)` leg depth as RRF | The backend's thresholds were tuned against these numbers. Normalisation is affine-invariant, so raggio's cosine and Weaviate's `1 − distance` normalise identically |
| **`alpha`: float in [0, 1], default 0.5; ignored by `rrf`, and `fusion` ignored outside `hybrid`** (not rejected) | The convention `nprobe` already follows ("ignored without an index"), and the least code. 0.5 is the backend's own default, and it always sends `alpha` (its `src/tools/weaviate_rag.py`); a client relying on Weaviate's server default must send 0.75 |
| Out-of-range `alpha`, unknown `fusion` → **422**, from the pydantic schema | The same response an out-of-range `k` already gets |
| **Relative fusion skips a zero-weight leg** | Weaviate does; it saves the leg, and `alpha` 1 / 0 then reproduce the vector-only / BM25-only ranking exactly (no ties at 0 with the skipped leg's records) |
| Ties keep first-seen order, vector leg first (stable sort, as `_rrf`) | Deterministic, and it reproduces the single-leg order at `alpha` 0 / 1. Weaviate's original-score-then-UUID tie-break has no raggio equivalent (no UUIDs) |
| **`doc_id` is a reserved filter key** (`_filter_sql`): the `records.doc_id` column, equality (scalar) and `in` (list or `{"in": [...]}`) only; any other operator → 400 | `idx_records_doc_type (doc_id, type, indexed)` answers the allowlist as a covering-index search; pinned for the allowlist, both listing queries and the BM25 leg (`tests/test_cold_start.py`). Equality and `in` are what the backend needs; ranges or `contains` on an id have no known use |
| `list_records` drops `NOT INDEXED` when the filter holds `doc_id` | `NOT INDEXED` (plan C, D9) forces the rowid-order table scan; with a `doc_id` filter that read every record to page a few: 100 ms against 0.06 ms through the index at 300k rows (probe) |
| BM25 leg unchanged: `records_fts` stays the outer loop under a `doc_id` filter | The planner keeps `SCAN records_fts` then rowid lookups (ADR 0003's rule: never a rowid-restricted MATCH); pinned |
| **Exact path for allowlists no larger than the rescore depth** (`_search_allowed`: `len(allow) <= _rescore_k(n)`, 50 for k ≤ 25 at 4-bit, 200 for a hybrid leg): score the records' fp16 originals directly, `(-score, id)` order, no index call | Measured (100k × 3072, below): the masked search costs 0.3-0.7 ms for small allowlists *whatever the index size* (its cost tracks the allowlist, not the index); what dominates is the rescore's fetch and decode of `min(\|allow\|, depth)` fp16 blobs, ~20 µs each at 3072-d. The exact path pays the same per id, for every allowlisted id. So it wins while `\|allow\|` ≤ depth (the same fetch; on a flat index the masked search returns every id anyway, so the result is identical, and on IVF it can only improve) and loses linearly beyond |
| The same path serves `_expand`'s `siblings_topk` | Same shape: a doc-sized allowlist, one per hit |
| A missing or invalid blob → the masked path | Only the index can score rows ingested before vector retention (ADR 0002); `_rescore_rows` keeps their quantized score the same way |
| `_fp16_vecs` extracted from `_rescore_rows` | One fetch/decode/renormalise routine for both paths; every read still runs in one `_reading()` section (ADR 0001), so a closed collection fails closed as before; the allow-cache generation counter (ADR 0005) is untouched, since the allowlist is built exactly as before |
| **An exclusive `fcntl` lock on `DATA_DIR/raggio.lock` before anything in `DATA_DIR` opens** (`open_data_dir_lock`, `lock_data_dir`), released after shutdown has closed every database | `lockf` record locks are what NFSv4 enforces across hosts with `local_lock=none`, and an ordinary lock on a local disk. The catalog used to open in `create_app`; it now opens in start-up, after the lock |
| **The lifespan never waits for the lock**: one non-blocking try; free (the usual start), start-up finishes before the server answers, as it always has; held, start-up goes on in a background task and the server binds at once | A lifespan that blocked would keep uvicorn from binding: the platform never sees the new replica ready, never stops the old one, and both wait until the startup probe kills the new one. Finishing an uncontended start-up before serving keeps every client (and the test suite, `docker/check_image.py`) seeing a ready server, as before |
| A held lock is retried every 0.5 s (`DATA_DIR_LOCK_POLL_S`), each try in a worker thread | A blocking `lockf` in a thread cannot be cancelled: a process told to stop while it waits would hang at exit (the default executor joins its threads) until SIGKILL. A try is µs locally and one lock RPC on NFS |
| Until start-up has finished, one app-level dependency (`require_ready`) answers every request but `/healthz` with 503 and `Retry-After: 2`; `/healthz` answers 200 `{"status": "starting"}` | 200 lets the rolling restart go on to stop the old replica; clients see a few seconds of 503s during a handover |
| A start-up that raises turns `/healthz` into 503 `{"status": "failed"}` | A failed process must not report healthy: the platform restarts it |
| Logs: `waiting for data-dir lock <path>`, `acquired data-dir lock after N s` (INFO) | The handover is visible in the platform logs |
| Windows (no `fcntl`): no lock, one warning | The tests run there; `msvcrt` locking would be a second implementation for a platform the image never runs on |
| **A document-scoped BM25 leg** (`_text_ids`): with `doc_id` in the filter and a unicode61 collection, count the matching rows through `idx_records_doc_type`; at most `TEXT_DIRECT_MAX` (1000) are scored directly by the stage-2 scorer (`_bm25_rescore`: corpus-wide idf from the df cache, the sampled avgdl, `raggio_native.bm25_topn` or its Python reference), rows scoring 0 (no query term) dropped as FTS5 MATCH drops them | The two-stage path's scores, without its corpus-wide stage 1 and its candidate caps, so a document's rows holding only pruned (common) query terms are found too. `_prune_common` still runs first, so the df cache refreshes after write churn exactly as before. At 165k records (below) the leg went from 22.8 / 69.5 to 1.8 / 7.6 ms p50/p95 |
| `TEXT_DIRECT_MAX = 1000` | The direct path costs ~37 µs per ~1000-word row, linear (1000 rows: 37 / 42 ms p50/p95); the FTS5 path cost 9-14 ms p50 and 48-270 ms p95 whatever the row count, on the synthetic 165k corpus. So the p50 crossover is ~350 rows and the p95 one beyond 1000. FTS5's cost grows with the corpus (the 2% pruning budget) and was ~5x heavier on the DEV data (about 60 ms of its ~70-85 ms server time), which puts the real-data p50 crossover near 1,600 rows; 1000 stays under it. A `doc_id` list over the cut-off keeps the FTS5 path |
| Trigram collections keep the FTS5 path | Trigram matches substrings; the stage-2 tokenizer splits words, so it would miss them (pinned by a test) |
| **Every SQLite connection through the `unix-excl` VFS on POSIX** (`connect_db`: `file:<path>?vfs=unix-excl`): `catalog.db`, the write connection and the per-thread read connections of each `meta.db`, and `resume_pending`'s check | `unix-excl` takes the database file's `fcntl` lock once, at the first transaction, and holds it until the last connection closes; the process's connections coordinate through SQLite's in-memory per-inode lock state, and the WAL index is allocated on the heap instead of an mmap'd `-shm` file. Counted with an `LD_PRELOAD` shim around `fcntl` (SQLite 3.50.4): 2 lock calls per read transaction and 6 per write with the default VFS, 0 with `unix-excl`. WAL concurrency is unchanged: a reader inside a transaction keeps its snapshot while the writer commits, then sees the commit; four reader threads run in parallel (0.06 s, against 0.12 s one after another). R5's lock already makes the process the only one in `DATA_DIR`, which is what `unix-excl` requires |
| The path goes in as `Path.absolute().as_uri()` | A `file:` URI: spaces, `%`, `#` and `?` in `DATA_DIR` must be escaped, or SQLite reads them as URI syntax (a `?` would start the query string); a test runs a data dir named with all four |
| Windows keeps the default VFS | No `unix-excl` there; same branch as the `fcntl` lock |
| R5's lock on `raggio.lock` stays | Under `unix-excl` a second process would fail with `database is locked` (after the 30 s busy timeout) rather than wait; R5 keeps the clean 503 handover. The two locks are on different files |
| Data dirs written by the default VFS open as they are | Checked: a database left with committed but un-checkpointed WAL frames (`-wal` and `-shm` from a process that exited without closing) opens through `unix-excl` with every committed row; the heap WAL index is rebuilt from the `-wal` file, and the stale `-shm` is ignored (pinned by a test) |

## Rejected

- **A fixed size threshold** (`EXACT_MAX_IDS`, with 4096 as the starting guess). The
  measured crossover is the rescore depth, which depends on `n`: at k=5 the exact
  path is already slower at 100 contiguous ids (1.83 vs 1.41 ms), and at 4096 random
  ids it is 8x slower (109 vs 13 ms).
- **Rejecting `alpha` with `rrf` (400).** It needs `alpha` to become optional plus a
  check in the endpoint; ignoring it is what `nprobe` does on a flat collection.
- **Weighted RRF** (`alpha` applied to RRF, as Weaviate's `rankedFusion` does). The
  backend does not use it; ADR 0003's stance on it stands.
- **Skipping the embedding call at `alpha: 0`.** The query vector still ranks
  `siblings_topk` in expansion; a separate change if it ever matters.
- **A blocking lock in a thread**, as first sketched: cannot be cancelled (above).
- **Always starting in the background**: every client, and the 30-odd test sites that
  use the app right after its lifespan starts, would race the start-up and get 503s on
  an ordinary start.
- **`flock`**: on Linux NFS it is emulated with `fcntl` locks anyway, and locally it does
  not see them; `lockf` behaves the same on both.
- **Speeding up `scope: summaries`** (profile below): its cost is a masked search over
  the 22k summary ids plus the 200-blob rescore of a 100-deep hybrid leg, and FTS5
  candidate queries over every record before the type join. Cheaper would take scope
  allowlists maintained across writes (every write clears the cache today; 15 ms to
  rebuild) or a type column in the FTS5 index (a rebuild of every existing index):
  neither is cheap or low-risk, and nothing was built.

## Measured

In-process store calls (no HTTP), Windows laptop (Intel Core Ultra 9 185H, 22 logical
CPUs, 32 GB), CPython 3.12.11, turbovec 1.0.0, SQLite 3.49.1, numpy 2.5.3,
`NATIVE_BM25=0` (pure-Python stage 2: the BM25 leg is slower than in the image).
Synthetic collections, dim 3072, 4-bit, documents of 20-50 chunks (100k chunks: 2,832
documents; 300k: 8,581), chunk text 120 Zipf(1.15) words over a 30k vocabulary, chunk
vector = document centre + noise; queries = a document's centre + noise and 8 Zipf
words. Scope chunks, k=5. *Before* is main @efa0bc1 filtering a metadata copy of the id
(`{"doc": X}`), the only way to scope to a document there; *after* is this change
filtering `{"doc_id": X}`.

100k chunks, idle machine, one process per arm, 200 queries on distinct documents
(195 timed):

| p50 / p95 ms | before | after |
|---|---|---|
| Vector, one document | 76.0 / 106.3 | **0.98 / 1.74** |
| Hybrid (RRF), one document | 84.9 / 151.8 | 25.9 / 138.3 |
| Hybrid (relative), one document | — | 25.2 / 133.2 |
| Vector, unfiltered | 7.4 / 9.4 | 7.1 / 9.3 |
| Hybrid (RRF), unfiltered | 49.4 / 119.5 | 45.5 / 111.9 |
| Hybrid (relative), unfiltered | — | 46.7 / 120.8 |
| Vector, unfiltered, `siblings_topk: 3` | 18.0 / 25.3 | 14.1 / 18.2 |

Separate processes drift by ±15% here (identical sibling-expansion code measured 18.0
and 15.5 ms p50 in two runs), and the 300k runs had other applications holding the CPU
at ~85%: unpaired runs of identical unfiltered code spread from 16.7 to 45 ms p50. So
at 300k both arms ran in one process on the same collection, alternating per query
(150 timed each):

| 300k, paired, p50 / p95 ms | before | after |
|---|---|---|
| Vector, one document | 360.4 / 507.1 | **2.76 / 22.8** |
| Hybrid (RRF), one document | 372.5 / 622.5 | 136.3 / 568.0 |
| Vector, unfiltered | 17.62 / 21.09 | 17.62 / 21.10 |
| Hybrid (RRF), unfiltered | 112.4 / 319.1 | 104.5 / 293.4 |

The exact path's own effect, the same queries with it on and off, alternating (100k
idle; 300k under the same load):

| p50 / p95 ms, paired | masked | exact |
|---|---|---|
| 100k: vector, one document, k=5 | 1.98 / 3.21 | 0.99 / 1.77 |
| 100k: a hybrid's vector leg, one document (n=100) | 1.78 / 3.44 | 1.01 / 2.09 |
| 100k: vector, unfiltered, `siblings_topk: 3` | 20.51 / 27.15 | 17.43 / 21.84 |
| 300k: vector, one document, k=5 | 7.54 / 22.18 | 3.04 / 19.82 |
| 300k: a hybrid's vector leg, one document (n=100) | 7.51 / 22.26 | 2.25 / 19.02 |
| 300k: vector, unfiltered, `siblings_topk: 3` | 38.47 / 54.58 | 32.24 / 48.38 |

The crossover (store level, 100k, one query per allowlist, contiguous ids as a
document's are):

| allowlist ids | 16 | 50 | 100 | 200 | 512 | 1024 |
|---|---|---|---|---|---|---|
| masked + rescore, n=5 (depth 50) | 0.65 | 1.40 | 1.41 | 1.58 | 1.57 | 1.54 |
| exact, n=5 | 0.20 | 0.95 | 1.83 | 3.94 | 11.32 | 25.28 |
| masked + rescore, n=100 (depth 200) | 0.62 | 1.06 | 2.77 | 5.43 | 5.66 | 5.65 |
| exact, n=100 | 0.18 | 0.68 | 1.71 | 3.83 | 11.44 | 25.79 |

The masked turbovec search alone took 0.25-0.70 ms in every row. With random ids
(worse SQLite locality) the exact path costs ~25 µs per id: 109 ms at 4096 ids, against
13 ms masked. The one-document BM25 leg, profiled at 100k: 31 ms p50 and 169 ms p95,
nearly all in the two-stage FTS5 candidate queries, for a median of 0 matching chunks
in the document.

**165k records shaped like `legal_knowhow`** (added with the document-scoped BM25 leg):
22,018 documents of one ~300-word summary and 3-10 chunks of ~1000 words (13.3%
summaries), Zipf(1.1) over a 50k vocabulary, dim 3072; queries of 4 words from the
target document plus 6 Zipf words. WSL Ubuntu on the same laptop (4 vCPU, the host
busy with other applications), CPython 3.12.13, the native scorer. *main* filters a
metadata copy of the id with RRF; *0007 before the BM25 leg* (f530504) and *after*
filter `{"doc_id": X}` with relative fusion, alpha 0.5. All three ran in one process
on the same collection, alternating per query (100 timed each):

| p50 / p95 ms | main | 0007, before the BM25 leg | after |
|---|---|---|---|
| Hybrid, one document, k=5 | 223.5 / 320.5 | 24.6 / 84.0 | **5.0 / 19.1** |
| BM25 leg, one document (n=100) | 17.6 / 203.9 | 22.8 / 69.5 | **1.8 / 7.6** |
| Vector, one document, k=5 | 226.2 / 259.6 | 0.83 / 1.41 | 0.83 / 3.07 |
| Hybrid, summaries, k=10 | 70.9 / 180.4 | 71.5 / 166.3 | 70.0 / 175.8 |
| Vector, unfiltered, k=5 | 26.9 / 55.2 | 24.9 / 44.5 | 26.8 / 46.4 |

The BM25 leg's cut-off, same collection, a `doc_id` list of growing size (30 queries
each, warm page cache):

| rows | 7 | 50 | 100 | 200 | 300 | 500 | 1000 |
|---|---|---|---|---|---|---|---|
| FTS5 path p50 / p95 | 30.2 / 132 | 11.1 / 184 | 11.6 / 48 | 12.4 / 122 | 14.5 / 174 | 9.0 / 66 | 13.8 / 79 |
| direct p50 / p95 | 1.1 / 13 | 3.0 / 10 | 3.7 / 7 | 8.7 / 14 | 11.7 / 15 | 18.0 / 22 | 37.0 / 42 |

`scope: summaries` hybrid, per step (every query two-stage):

| step | p50 / p95 ms |
|---|---|
| summaries allowlist, `SELECT` + sort (only after a write clears the cache) | 14.6 / 19.6 |
| masked search, 22,018 ids, k=200 | 20.6 / 30.9 |
| fp16 rescore of 200 | 9.1 / 13.3 |
| BM25: prune (df lookups) | 4.2 / 17.6 |
| BM25: FTS5 OR ranked, LIMIT 500, type join | 5.7 / 29.5 |
| BM25: FTS5 AND, LIMIT 1000, type join | 10.9 / 55.7 |
| BM25: candidate texts + stage 2 | 1.5 / 6.1 |
| plain scan of all 165k, k=200, for comparison | 39.2 / 58.7 |

The two legs run concurrently, so the vector leg sets the median and the BM25 leg's
candidate queries the tail. The masked search over 13% of the rows already beats the
plain scan. A check of `_vector_ids` against a direct call of the same masked search
and rescore found no overhead between them.

**`unix-excl` on a local disk** (the 165k collection rebuilt; WSL ext4, native scorer;
the change before `unix-excl` (a3d8573) and after it in one process, each on its own
copy of the collection so the two VFSes never share a file, alternating per query, 100
timed each; `fcntl` lock calls per request counted with the `LD_PRELOAD` shim):

| p50 / p95 ms, lock calls per request | default VFS | `unix-excl` |
|---|---|---|
| `GET /collections/{name}` (`stats()`) | 39.9 / 92.1, 6 | 39.5 / 95.6, 0 |
| Vector, one document, k=5 | 0.39 / 0.55, 6 | 0.39 / 0.57, 0 |
| Text, one document, k=5 | 2.30 / 6.01, 12 | 2.46 / 6.71, 0 |
| Hybrid relative, one document, k=5 | 1.93 / 4.51, 12 | 1.92 / 3.85, 0 |
| Vector, summaries, k=10 | 18.1 / 25.7, 4 | 18.0 / 24.5, 0 |
| Text, summaries, k=10 | 14.5 / 160.8, 12 | 15.4 / 130.2, 0 |
| Hybrid relative, summaries, k=10 | 58.3 / 123.8, 14 | 59.5 / 132.7, 0 |

Neutral on a local disk, as expected. On NFS each of those 4-14 lock calls was a server
round trip plus a page-cache drop; the DEV figures with `unix-excl` are to be measured.

## Consequences

- A metadata key named `doc_id` can no longer be filtered: the key now means the
  document id. Records keep such metadata; only the filter changed meaning.
- A one-document vector search no longer depends on collection size: an index lookup
  and at most the rescore depth of fp16 blobs.
- A one-document hybrid search no longer depends on corpus size either. Before the
  document-scoped BM25 leg it was bounded by that leg (25 / 138 ms at 100k, 136 / 568
  ms at 300k under load, ~70-85 ms server time on DEV); at 165k it is now 5.0 / 19.1
  ms. A `doc_id` list matching more than 1000 rows, and trigram collections, still pay
  the corpus-wide FTS5 leg.
- A document-scoped text or hybrid query now always gets the stage-2 scores. Before,
  a query nothing was pruned from (small collections, queries of rare terms) was
  ranked by FTS5's own `bm25()`: same k1, b and idf, but the exact average length
  instead of the sampled one, and no SDM-lite bigram bonus. Scores move slightly and
  phrase matches can move up; pruned queries, the common case on large collections,
  score exactly as before.
- `scope: summaries` hybrid is unchanged (70 / 176 ms at 165k here; 124 / 156 ms with
  RTT on DEV, against Weaviate's 54 / 56).
- With `unix-excl`, a request makes no `fcntl` calls on SQLite's files, so on NFS it no
  longer pays the ~40 ms of lock round trips and re-reads measured above. The Linux NFS
  client still revalidates its cache on its own schedule (`acregmin`, close-to-open),
  which this does not change; DEV measures the result.
- While raggio runs, no other process can open its databases (`database is locked`),
  integrity checks and the `sqlite3` shell included: they need a stopped container or
  a copy of `DATA_DIR`. There is no `-shm` file next to a database any more on POSIX.
- Relative scores are relative to each query's candidates, as Weaviate's are: a hit
  that tops both legs scores 1.0 on any query, so a "top score >= 0.85" shortcut means
  "leads both rankings", not "relevant". On a one-document search the candidates are
  that document's chunks only: a chunk that tops both legs there scores 1.0 however
  weakly it matches, and with no BM25 match in the document the best score is `alpha`.
  This is Weaviate parity, not a raggio property.
- Metadata filters other than `doc_id` still build their allowlist with a
  `json_extract` scan (the "before" column), cached 8 deep and cleared on every write.
- One process per `DATA_DIR` is enforced where the filesystem's locks reach every host
  that mounts it: a local disk, or NFSv4 with `local_lock=none`. With local locks
  (`local_lock=all`, NFSv3 `nolock`) the lock is per host and does not prevent the
  corruption above; on Windows it is not taken. If the lock holder's host dies, the
  NFSv4 server frees the lock when that client's lease expires.
- A rolling restart on a shared volume now hands over: the new replica binds, answers
  `/healthz` "starting" and 503s until the old one exits, then opens the data. Requests
  in that window fail fast with `Retry-After` instead of corrupting the database.
- Tests: `tests/test_api.py` covers the wait (503s, `/healthz` "starting", nothing in
  `DATA_DIR` opened), the failed start-up (503 "failed") with the lock simulated, and,
  POSIX only, a real second process holding `raggio.lock`; also a `DATA_DIR` named with
  a space, `%`, `#` and `?`. `tests/test_store_optimizations.py` pins `unix-excl` (POSIX:
  no `-shm`, another process locked out of `meta.db` and `catalog.db` until they close)
  and the open of a default-VFS leftover.
- Docs: `docs/search.md` (fusion, the `doc_id` key, the exact path, the document-scoped
  BM25 leg), `docs/api.md`,
  `docs/concepts.md`, `docs/storage.md` (one process per data directory, `unix-excl`),
  README.
