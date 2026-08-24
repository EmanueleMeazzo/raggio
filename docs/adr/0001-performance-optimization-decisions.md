# ADR 0001 — Performance optimization decisions (552k-vector benchmark rounds)

Status: accepted · Date: 2026-08-22

Every path below was direction-tested on the real benchmark corpus (552,515
email embeddings × 1536 dims, 1 GiB container) before being adopted or
rejected. Full results: `bench/results.md`; docs page: `docs/benchmark.md`.
Probe tooling referenced here is committed under `bench/`.

## Round 1 — search latency (390.7 → ~34 ms vector p50)

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| Skip the allowlist when the scope excludes nothing | **Accepted** | Every default-scope query built a 553k-id allowlist (244 ms SQL + 9.5 ms convert) and forced a ~2x slower masked scan — to exclude zero rows. Gated on in-memory per-type counts (`bench/microbench.py`) |
| BM25 token pruning, cumulative doc-frequency budget (rarest-first, 2% of corpus, 1000-row floor) | **Accepted** | Constant tokens ("emails", df 538k) made FTS5 score the whole corpus (540–670 ms/query). Budget swept over 0.02/0.03/0.05/0.08: all 0 fused misses, 0.02 fastest |
| BM25 pruning via hard per-token df cap (drop df > 1%) | **Rejected** | Caused the only hybrid text-hit miss (0.998): dropped 4 of 5 mid-frequency meaningful tokens on one query. Replaced by the cumulative budget above |
| Bare-FTS fast path (skip the records join when nothing is excluded) | **Accepted** | ~40% of the pruned text-leg cost |
| Hybrid legs run concurrently (`asyncio.gather`) | **Accepted** | SQLite releases the GIL; legs overlap fully |
| Readers-writer lock + micro-batched scans (concurrent full scans stack into one kernel call) | **Accepted** | Store-level probe: 347 qps at 32 in-flight vs 3 fully serialized; batched kernel 3.2 vs 11.9 ms/query |
| Allowlist cache (FIFO 8) + `prepare()` at load | **Accepted** | Warm filtered p50 5.6 ms; first query no longer pays one-time kernel init |
| Per-thread read-only SQLite connections | **Accepted (correctness)** | Concurrent readers on one shared connection raise SQLITE_MISUSE under the new RW lock |

## Round 2 — "give it all" (34.5 → 32.8 ms vector, 48.3 → 41.3 ms hybrid, recall 0.960 → 0.969)

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| TQ+ calibration (turbovec `calibrate()`, one-shot at 10k vectors from a reservoir sample) | **Accepted** | recall@10 0.960 → 0.969 for free. `bench/cal_probe.py`: calibrate-once-at-threshold beats ideal uniform calibration; milestone refits LOSE recall (re-encode is a second quantization); late calibration of a fully-ingested index is WORSE than staying uncalibrated (0.9596 vs 0.9606) — hence calibrate-early-or-never |
| df-lookup cache for BM25 pruning (folded token → doc frequency, churn-based invalidation) | **Accepted** | The `fts5vocab` df lookup walks the term's whole posting list: 15–30 ms per hybrid query just to *decide* pruning. Hybrid p50 48.3 → 41.3 ms |
| uvicorn `--no-access-log` | **Accepted** | Per-request stdout line through the container log pipe cost ~1.7 ms/query: served floor 4.19 → 2.52 ms p50 (`bench/floor_probe.py`). Weaviate doesn't per-request-log at default verbosity, so also fairness-correct |
| IVF coarse partitioning on top of `IdMapIndex` (k-means shards, nprobe search) | **Rejected** | `bench/ivf_probe.py`, full sweep (nlist 16/64/256/1024 × nprobe 1–32 × 3 calibration modes): best cell 1.63x speedup at −1 pt recall, below the ≥2x-at-≥0.95 gate; every higher-recall cell is slower than the full scan. ~0.4 ms fixed cost per shard search call eats the savings. Closing the vector-latency gap vs HNSW needs an ANN index inside the turbovec kernel |
| orjson response serialization | **Rejected** | Real 10-hit response: stdlib `json.dumps` 0.028 ms, orjson 0.003 ms — 26 µs/query is noise; not worth a runtime dependency |
| Request-path overhead (pydantic parse of the 1536-float query vector, etc.) | **Rejected (exhausted)** | Server-side parse+validate+normalize is 0.35 ms total; the remaining ~2 ms floor is transport that both engines pay equally |
| Cheaper cold-start ghost reconciliation | **Rejected (no API)** | `IdMapIndex` exposes no id enumeration or reconstruction; the k=n probe stays (~0.7 s at 553k), correctness over cold-start |

## Storage & concurrency design (bugs found by the re-runs, fixed in `c6b97ff`)

| Decision | Verdict | Evidence / rationale |
|---|---|---|
| One meta.db write connection loosely shared between event loop and worker threads (commits locked, statements not) | **Rejected** | Cross-thread statements join each other's in-flight transactions: a full-corpus ingest LOST 1 of 2,211 journaled jobs (202 + job_id returned, row gone, ids contiguous, integrity ok) |
| Two independent write connections (jobs on the loop, records in threads) | **Rejected** | SQLite busy handlers starve under a hot writer loop: "database is locked" past a 30 s timeout, dead worker, 500s on ingest. Reproduced in isolation with `incremental_vacuum` churn |
| **Single-writer discipline**: every write transaction wholly inside `db_lock`, in a worker thread, never on the event loop; loop-side reads on per-thread read connections | **Accepted** | Stress test (3 concurrent enqueuers × 30 jobs vs draining worker): previously lost a job or wedged in 90 s+, now drains 4,500 rows in 1.2 s. Cost: ingest 785 → 483 vec/s together with the vacuum fix below — accepted for durability (still 1.7x Weaviate) |
| `PRAGMA auto_vacuum=INCREMENTAL` ordered *before* `journal_mode=WAL` | **Accepted (bug fix)** | journal_mode initializes the db file, after which auto_vacuum is a silent no-op: a full ingest left meta.db at 7.3 GB, 93% freelist, 26 s cold start |
| `incremental_vacuum` exhausted with `.fetchall()` | **Accepted (bug fix)** | The pragma frees pages per cursor STEP; pysqlite's `execute()` steps once = frees exactly one page. Measured: 4,900-page freelist → 0 and the file truncates. End state: meta.db 990 MB, cold start 5.1 s |
| Calibration policy details: arm only below the 10k threshold (re-arm on reload below it), feed only fresh rows, slice bulk jobs to calibrate AT the threshold, never fail the ingest job on calibrate errors | **Accepted** | Adversarial review findings: memory-only reservoir + born-empty arming silently forfeited calibration after any pre-threshold eviction/restart; upsert churn skewed the sample; a single bulk job calibrated too late; a calibrate error made the job terminally `error` with committed rows left unsynced |

## Standing constraints these decisions respect

- turbovec is an external dependency — kernel changes (SIMD, ANN, multi-partition search) are out of scope; everything layers on `IdMapIndex`.
- Remaining known headroom needs one of: an ANN index in the kernel, more CPUs, or free-threaded Python. Application-level paths are exhausted as of this ADR.
- Ingest throughput has a known lever if it ever matters: batch index syncs / vacuum every N jobs instead of per job (`ponytail:` comment in `store.py`).
