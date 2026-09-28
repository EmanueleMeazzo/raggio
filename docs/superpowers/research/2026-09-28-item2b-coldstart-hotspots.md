# item2b-coldstart-hotspots

## Summary
Cold start is dominated by two full scans of the records table, not by turbovec. Collection.__init__ runs an indexed_counts GROUP BY (store.py:575-577) and then builds the live-id set in _reconcile_ghosts (store.py:643). Both have to read every records row, including the ~1.5 KB text column. On the laptop (Windows, warm OS cache, 300k x 1024 synthetic rows), Collection() takes 1582 ms and reads 1393 MB (median of 3). Of that, 1236 MB is the two records scans and 158 MB is index.tvim. The k=n probe costs only 41 ms and the Python set diff 86 ms.

At 2.55M rows the records table is about 4 GB, and the 4 GiB container cap cannot keep it cached between the two scans. My arithmetic (roughly 9 GB of reads, about 23 s of disk time at the throughput the 30.4 s implies) matches the published cold start, but it has not been measured on the DGX.

Recommended fix: replace idx_records_doc(doc_id) with a covering index on (doc_id, type, indexed). Both cold-start queries then become covering-index scans, with no other code change: 1582 ms / 1393 MB drops to 458 ms / 184 MB, and to 384 ms with a numpy set difference. The index shape introduces one query-plan regression, in the sorted list_records page query (939 ms to 1714 ms). A NOT INDEXED hint on that one query fixes it; no other query shape got slower. Predicted DGX cold start is about 5-8 s, down from 30.4 s (not verified). It needs a one-time index build on first open after upgrade, roughly one full records scan.

The same index makes stats() 633 ms -> 136 ms (it currently runs on the event loop), the unfiltered list_records count 503 ms -> 23 ms, and the scope-only allowlist 925 ms -> 193 ms. A partial index on open jobs cuts pending_jobs from 21.8 ms to 0.01 ms with a 300 x 5 MB backlog, where a plain status index would make the claim query slower. Separately from cold start, _vec_sample's ORDER BY RANDOM() costs 24.4 s for the 65,536-row IVF training sample at 300k; random-rowid sampling takes 0.78 s. turbovec 1.0.0 (still the latest on PyPI) has no ids() in Python, but that probe is not the bottleneck.

## Brief
# Item 2b: cold-start hot spots and low-risk hot paths (research brief)

All local numbers were measured on a Windows x86-64 laptop with a warm OS page cache, using SQLite 3.47.1, Python 3.12.8, numpy 2.5.2 and turbovec 1.0.0. They are labelled `[WIN]`. "Read" means the process's logical read bytes (GetProcessIoCounters). I use it as a stand-in for cold-disk cost.

The synthetic collection mirrors raggio's schema and was built with raggio's own `open_meta_db`:
- 300,000 chunks x 1024-d, 4-bit calibrated `IdMapIndex`
- about 1.5 KB of Zipf text per row, arXiv-like doc_id, `{"year"}` metadata
- meta.db 1909 MB, index.tvim 150 MB

Scratch scripts are in `D:\DEV\_scratch\probe-item2b\`:
- `build.py`, `coldstart.py`, `coldcompare.py`, `openprof.py`
- `hotpaths.py`, `covpaths.py`, `plans.py`, `plans2.py`, `plans3.py`
- `jobsprobe.py`, `vecblocks.py`

The data is in `data\c300000`, `data\c300000-cov` and `data\jobs_300_5.0`.

---

## 1. What cold start does today (code walk)

The bench's `cold_start()` (bench/bench.py:329-345) does `podman restart -t 2` and then polls a search every 0.5 s.

1. The lifespan runs `resume_pending` (app.py:86-92, store.py:1731-1740). It opens each meta.db and runs a `COUNT(*)` over jobs by status. This is cheap when there is no backlog.
2. The first search goes through `get_collection`, then `manager.touch()` (store.py:1694-1717). touch constructs `Collection(...)` synchronously on the event loop (:1710), inside `_load_lock`.
3. `Collection.__init__` (store.py:541-619) then does the following:
   - `open_meta_db` (:273-320): pragmas, IF NOT EXISTS DDL, `_ensure_fts`, fts5vocab. `[WIN]` about 3-5 ms per statement (openprof.py). An earlier reading of about 500 ms was an antivirus or first-open artefact.
   - `IdMapIndex.load` (:569): reads the whole .tvim. `[WIN]` 207-511 ms and 158 MB at 300k.
   - `self.index.prepare()` (:571): `[WIN]` 7 ms.
   - **`SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type`** (:575-577). The plan is `SCAN records`, a full table scan. `[WIN]` 615-654 ms, **618 MB read**.
   - `_reconcile_ghosts()` (:598, :628-648):
     - k=n search probe (:642): `[WIN]` 41 ms.
     - **`live = {r[0] for r in SELECT id FROM records WHERE indexed=1}`** (:643): `SCAN records` again. `[WIN]` 680-780 ms, **616 MB read**.
     - `[int(i) for i in all_ids if int(i) not in live]` (:644): `[WIN]` 86 ms.
   - Calibration reservoir arming (:606-612): trivial.

End to end, current code, `coldcompare.py` `[WIN]`, median of 3 after one warm-up: **1582 ms, 1393 MB read.** Of that, 1236 MB is the two records-table scans and 158 MB is index.tvim.

**Why the DGX shows 30.4 s (inference, not verified):**
- The arXiv volume is 16.47 GB (bench/results-arxiv.md). In `[WIN]` the records table scans at about 2.06 KB/row. Scaled to 2.55M arXiv rows that is roughly 4-5 GB of records.
- The container is capped at 4 GiB, and the anonymous memory includes the 1.3 GB codes. The page cache therefore cannot keep the records table between the GROUP BY and the live-set scan, so cold start reads it twice: about 8-10 GB plus the 1.3 GB .tvim.
- At the ~300-400 MB/s effective throughput that 30.4 s implies, this accounts for almost all of it.
- This is consistent with ADR 0002:35: moving the fp16 blobs into records raised cold start from 2.2 s to 25.8 s at 552k, which shows records scans dominate.
- It is also consistent with the warm-cache re-run measuring 5.1 s (results-arxiv.md provenance note; ADR 0003:107-109).

**turbovec (item task 2):**
- The Rust core has `IdMapIndex::iter_ids()` (D:\DEV\_scratch\turbovec\turbovec\src\id_map.rs:722; CHANGELOG.md:515). It returns slot_to_id.
- The core also keeps a `sorted_ids` copy (id_map.rs, `from_v7_load`).
- The Python binding (turbovec-python/src/lib.rs, IdMapIndex pymethods from :967) exposes no ids method. `dir(turbovec.IdMapIndex)` in the venv lists: add_with_ids, bit_width, calibrate, calibration_state, contains, dim, from_bytes, load, prepare, remove, search, sync, to_bytes, write.
- 1.0.0 is the latest on PyPI (https://pypi.org/pypi/turbovec/json, releases 0.6.0-1.0.0).
- ADR 0001:33 ("no API") is still correct. However, `[WIN]` shows the k=n probe is not the bottleneck: 41 ms, versus about 1.3 s of SQL.

## 2. Measurements: cold-start alternatives

`[WIN]`, 300k rows. `-cov` is a copy with `CREATE INDEX idx_records_doc_type ON records(doc_id, type, indexed)`.

| Variant | Time | Read |
|---|---|---|
| Current code, current schema | 1582 ms (median; runs 1742/1586/1582/1533) | 1393 MB |
| Current code, covering index present (DDL change only) | 458 ms (462/504/441/458) | 184 MB |
| Covering index + GROUP BY + `np.fromiter(chain.from_iterable(cur), uint64)` + `np.setdiff1d(assume_unique=True)` | 384 ms (405/397/369/384) | 184 MB |

The individual pieces with the covering index:
- GROUP BY: 654 ms / 618 MB becomes 82 ms / 14 MB.
- Live-id scan: 780 ms becomes 141 ms / 12 MB.
- One combined scan (`SELECT id, type='chunk'`): 214 ms.
- setdiff1d: 6 ms. `isin(kind='table')`: 5.6-7 ms. The Python list-comprehension diff: 86 ms.
- An upper-bound estimate for a future `ids()` binding is a u64 copy of n ids: about 0.5 ms.

The one-time `CREATE INDEX (doc_id,type,indexed)` at 300k takes `[WIN]` 0.79-0.87 s warm; one full records scan alone is 0.47 s. It took 5.7 s on a freshly copied, cold file. A covering scan over the index is about 14 MB at 300k, which projects to about 120 MB at 2.55M.

**Conclusion:** the index does about 90% of the work. The numpy diff adds about 16% on top, and merging the two scans into one saves only about 10 ms once they are index scans. I recommend keeping the two statements for clarity.

## 3. Query-plan hazard audit (EXPLAIN QUERY PLAN, SQLite 3.47.1, no sqlite_stat1)

I checked every records query shape in store.py (plans.py, plans2.py; the new index only, with the old one dropped inside a rolled-back transaction).

**Improved:**
- Cold-start GROUP BY, the live-id scan, the stats GROUP BY, unfiltered `COUNT(*) WHERE indexed=1`, and the scope-only allowlist now use `SCAN ... USING COVERING INDEX idx_records_doc_type`.
- Sibling ids (:1450) now use `SEARCH ... (doc_id=? AND type=? AND indexed=?)`.
- The summary lookup (:1474) and the delete lookup (:1510) are now covering.
- `COUNT(DISTINCT doc_id)`: 34.5 ms before, 28.9 ms after.

**Unchanged:**
- All FTS joins (text sibling :1437-1443, filtered and scope-only stage 1 :1372-1378, stage 1b :1390-1394) keep `SCAN records_fts VIRTUAL TABLE` as the outer loop with records probed by INTEGER PRIMARY KEY. So ADR 0003's rowid-restricted MATCH footgun (ADR 0003:37-40) is not triggered.
- The filtered allowlist (:1183), filtered IN, and the filtered list count still use `SCAN records`.
- The avgdl probe, backfill and delete-vecs subquery are unchanged.

**Regressed (one):**
- The list_records page query with scope plus a metadata sort (:1545), `... WHERE indexed = 1 AND type = 'chunk' ORDER BY json_extract(metadata, ?) DESC, id LIMIT`.
- The plan flips to `SCAN records USING INDEX idx_records_doc_type` + temp B-tree, which is doc_id-ordered random table lookups: 939 ms becomes 1714 ms `[WIN]` warm. It would be far worse cold.
- `FROM records NOT INDEXED` restores `SCAN records` at 831 ms. The `+indexed/+type` unary form also works (838 ms).
- NOT INDEXED still allows rowid lookups (https://www.sqlite.org/lang_indexedby.html), and the default `ORDER BY id LIMIT` path stays `SCAN records` (0.0 ms).

**Alternative shape, `(type, indexed)` added next to idx_records_doc (plans3.py):**
- Narrower: GROUP BY 17.5 ms, cold-start scan 237 ms.
- But it flips every scoped filtered query to `SEARCH records USING INDEX idx_t (type=? AND indexed=?)` with a per-row table lookup. On this rowid-ordered data it was not slower warm (789 vs 1043 ms), but it changes the plans of the bench's filtered path. It also keeps two indexes on the write path.
- I do not recommend it.

## 4. Other hot spots, measured and ranked by impact/risk

| # | Hot spot (file:line) | Cost today `[WIN]` 300k | Fix | After `[WIN]` | Risk | Metric moved |
|---|---|---|---|---|---|---|
| A | Cold-start records scans (store.py:575-577, :643) | 1582 ms / 1393 MB | Covering index (doc_id,type,indexed) replacing idx_records_doc (store.py:305) + NOT INDEXED at :1545 | 458 ms / 184 MB | Low-med (planner; see §3) | **Cold start 30.4 s** |
| B | Python set diff (:643-644) | 86 ms + set build | fromiter uint64 + `np.setdiff1d(assume_unique=True)` | 384 ms end to end | Low | Cold start |
| C | `stats()` runs on the event loop (app.py:146-150, store.py:1579-1589) | 633 ms blocking the loop (GROUP BY 615 + DISTINCT 65) | A + `await asyncio.to_thread(c.stats)` | 136 ms, off the loop | Low | GET /collections latency; loop stalls; bench reuse/ingest checks call it (bench.py:205, :221, :362) |
| D | `list_records` unfiltered COUNT (:1541) | 503-548 ms | A (automatic) | 23-35 ms | Low | Listing latency |
| E | Scope-only allowlist (:1183). The cache is cleared on every write (:854, :1521, :1576) | 925 ms | A (+ fromiter) | 193 ms (now dominated by building a 300k-element Python list) | Low | First scoped search after any write in mixed chunk/summary collections |
| F | Attach/detach swap live sets under the write lock (:993, :1050) | Full records scan while searches are blocked | A (automatic) + numpy diff | Same as the live-set scan: ~141 ms | Low | Search stall during IVF swap |
| G | `pending_jobs` / housekeeping / eviction on the loop (:690-693, :1703-1705, :1748); `resume_pending` (:1735) | 21.8 ms and 4.5 MB read per call with 300 pending x 5 MB jobs (status stored after payload) | Partial index `CREATE INDEX idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')` | 0.01 ms; `_claim_next` 8.6 → 5.5 ms | Low | Loop stalls during ingest backlog; boot time with backlog |
| H | `Collection()` built on the event loop in touch() (:1710) | Whole cold start (30 s on DGX) blocks /healthz and every other collection | `c = await asyncio.to_thread(Collection, ...)`; `start_worker()` stays on the loop | – | Low-med | Health checks and multi-collection availability during cold start/migration (not the bench number) |
| I | `_vec_sample` ORDER BY RANDOM() (:909-917) | k=1024: 1.9 s (one 30.8 s outlier); k=65536: **24.4 s** | Random-rowid IN-list sampling via `_rows_by_id`, fallback when ids are sparse | 23 ms / 784 ms | Low-med (uniformity, sparse ids) | IVF build 337 s, detach time. Not cold start. The project already avoids ORDER BY RANDOM in `_avgdl` (:1283). |
| J | `_hydrate` + FastAPI serialization on the loop, large k (:1422, :1077-1104) | k=10: 0.05 + 0.28 ms (noise). k=100: 0.41 + 2.53 ms. k=1000: 13.3 + 23.8 ms | Hydrate in `to_thread` when len(ids) > ~64; return `JSONResponse` directly to skip `jsonable_encoder` (1.43 of 2.53 ms at k=100; 13.9 of 23.8 ms at k=1000) | ~-57% serialization | Low | Large-k latency and loop stalls; bench (k=10) unaffected |
| K | `_search_rescored` per-element loop (:1155-1161) | 0.015 ms (k=10) to 0.57 ms (k=1000) | numpy mask + tolist | 4-8x faster, ≤0.5 ms saved | Trivial | Noise; drive-by only |

**About item J and ADR 0001:**
- ADR 0001:31 measured only `json.dumps` on 10 hits (0.028 ms). It did not measure `jsonable_encoder`, which FastAPI 0.141.1 runs for dict returns (.venv fastapi/routing.py:341 in `serialize_response`).
- That is the new evidence here. It matters only at k ≥ 100.
- This is not orjson and not request-parse work.

**Not worth doing (measured):**
- `_rrf(10,100,100)`: 0.047 ms.
- `_prune_common`, 14 tokens, warm df cache: 0.031 ms.
- Allowlist key `json.dumps`: 2-3 µs.
- `sorted(lambda)` vs `np.lexsort` in `_rescore_rows`: lexsort is slower (0.008 vs 0.007 ms at k=10).
- fromiter vs list-comp for filtered allowlists: SQL-bound, ±5%.
- `_rescore_rows` itself is dominated by the SQL blob fetch (nq=8: 4.4 of 8.7 ms), not Python.

**Side findings (outside the cold-start scope; route to the owner):**
- **vecs page waste.** A 2048-byte fp16 blob (1024-d) takes **4111 bytes/row** with 4 KB pages: one row per page, 50% efficiency. Measured with 20k-row tables:
  - page_size 8192: 2737 B/row
  - page_size 16384: 2345 B/row
  - 1536-d at 4 KB: 75% efficiency
- At 2.55M rows that is about 10.5 GB of the 16.47 GB footprint (not verified; it assumes the DGX db uses 4096-byte pages).
- A bigger page_size applies to the whole database. It also raises read amplification for random blob/hydrate reads, and it can only be changed with a journal-mode switch plus VACUUM. This is a user decision; it is not in this plan.
- `_iter_vec_blocks` (:890-907) also reads records leaf pages through the join: 2.47 s / 1848 MB versus 2.05 s / 1232 MB for a vecs-only scan. A 1.2x gain; defer.
- `PRAGMA mmap_size` sped warm point reads about 1.5x `[WIN]` (400-blob fetch 5.06 → 3.32 ms; hydrate k=100 1.53 → 1.06 ms). The Linux gain and the effect on the podman memory metric are unknown; defer.

## 5. Recommended design

1. **Schema.** In `open_meta_db` (store.py:305), replace `CREATE INDEX IF NOT EXISTS idx_records_doc ON records(doc_id);` with `CREATE INDEX IF NOT EXISTS idx_records_doc_type ON records(doc_id, type, indexed); DROP INDEX IF EXISTS idx_records_doc;`.
   - Create first, then drop, so doc_id lookups never lack an index. A crash between the two leaves both indexes, and the next open drops the old one.
   - It is idempotent and self-migrating.
   - Downgrade is safe: the old code re-creates idx_records_doc (a one-time build).
2. **Plan fix.** Change list_records' page query (:1545) to `FROM records NOT INDEXED`.
3. **Reconcile.** Keep the GROUP BY (now covering). In `_reconcile_ghosts`:
   - `live = np.fromiter(itertools.chain.from_iterable(self.db.execute("SELECT id FROM records WHERE indexed=1")), dtype=np.uint64)`
   - `ghosts = np.setdiff1d(np.asarray(all_ids, dtype=np.uint64), live, assume_unique=True)`
   - Remove and sync only if there are ghosts.
   - Both arrays must be uint64. Mixing uint64 and int64 promotes to float64.
   - Apply the same numpy diff to the attach/detach swaps (:993-995, :1050-1052).
4. **Observability.** Record `self.load_timings = {open_meta_db, index_load, prepare, counts, reconcile, total}` and log one line, e.g. `raggio coldstart <name>: ...`, through `logging.getLogger("uvicorn.error")`, which uvicorn already sends to stderr and so to `podman logs`. raggio has no logging today.
5. **Off-loop.** Run `stats()` in `asyncio.to_thread` in `collection_info`. Build `Collection` in `asyncio.to_thread` in `touch()`. `_RWLock`'s `asyncio.Condition` and `asyncio.Event` bind to the loop lazily on Python ≥3.10, so constructing them in a worker thread is safe. Test this.
6. **Jobs.** Add the partial index `idx_jobs_open`. The partial-index rule needs the query term to match the index term textually (https://www.sqlite.org/partialindex.html, Rule 1). All three call sites already use exactly `status IN ('pending','processing')` (:692, :699, :1736). A plain `jobs(status)` index is worse: `_claim_next` becomes index + temp B-tree and slows from 8.6 to 26.3 ms.
7. **Upstream (conditional).** Only if the DGX phase log shows the k=n probe at ≥0.5 s: ask turbovec for `IdMapIndex.ids() -> np.ndarray[uint64]` and feature-detect it with `getattr(self.index, "ids", None)`.

Upstream issue draft, for the maintainer to file:
- Title: "Python: expose IdMapIndex id enumeration (ids())".
- Body: "The core has `IdMapIndex::iter_ids()` (src/id_map.rs:722), but the PyO3 class has no way to list ids. Callers that reconcile an external store against a loaded index have to run `search(probe, k=len(index))` to recover every id. Request: `ids()` returning a uint64 numpy array copy of slot_to_id (or sorted_ids), releasing the GIL."

## 6. Ordered task list (TDD)

**T1: Cold-start phase timings**
- Files: src/raggio/store.py (`Collection.__init__` :541-619), tests/test_store_optimizations.py.
- Tests:
  - `test_collection_records_load_timings`: open a collection with 3 records and an index; `load_timings` has all the keys, floats ≥ 0, and total ≥ each phase.
  - With pytest's `caplog`, one INFO record contains "coldstart".
- Acceptance: the line appears in `podman logs bench-tv`.

**T2: Covering index + migration + plan guard**
- Files: store.py (:305 DDL, :1545 NOT INDEXED), tests/test_store_optimizations.py (or a new tests/test_schema_plans.py).
- Tests:
  - `test_fresh_db_has_covering_index_only`: `PRAGMA index_list(records)` contains idx_records_doc_type and not idx_records_doc.
  - `test_legacy_db_migrates_in_place`: create a db with the old DDL and rows via raw sqlite3; `open_meta_db` swaps the index; the rows, get_document, sibling expansion and delete still work; a second open is a no-op.
  - `test_query_plans_pinned` (EXPLAIN QUERY PLAN with the indexes as in production, no ANALYZE):
    - cold-start GROUP BY, live-id scan, stats GROUP BY and unfiltered COUNT contain `COVERING INDEX idx_records_doc_type`;
    - the doc_id queries (:1450, :1466, :1474, :1487, :1510, :1572) use idx_records_doc_type with `doc_id=?`;
    - filtered allowlist and filtered count plans are `SCAN records` with no INDEX;
    - list page with and without sort contains no idx_records_doc_type;
    - every FTS-join plan starts with `SCAN records_fts VIRTUAL TABLE` and probes records by INTEGER PRIMARY KEY.
  - The existing listing/sibling/delete/patch API tests stay green.
- Acceptance (local):
  - `[WIN]` 300k synthetic `Collection()` ≤ 600 ms and ≤ 250 MB read (from 1582 ms / 1393 MB), via `D:/DEV/raggio/.venv/Scripts/python.exe D:/DEV/_scratch/probe-item2b/coldcompare.py` or an equivalent.
  - The sorted list page is not slower than on main.

**T3: Numpy ghost diff**
- Files: store.py (:628-648, :993-995, :1050-1052).
- Tests:
  - Existing: `test_reconcile_evicts_ghosts_even_when_counts_match` and `test_reconcile_evicts_ivf_ghosts` (tests/test_ivf_index.py:105).
  - New, no ghosts: `_sync_index` is not called (monkeypatch a counter).
  - New, all ids ghosts: index empties.
  - New, empty records with a non-empty index.
  - New, ids > 2^32: no dtype promotion; assert the ghost dtype is uint64.
- Acceptance: a ghost set identical to the old implementation's on randomized fixtures.

**T4: stats() off the event loop**
- Files: app.py (:146-150).
- Test: monkeypatch `Collection.stats` to call `asyncio.get_running_loop()` and record a `RuntimeError`, which proves it ran in a worker thread. The response shape is unchanged.
- Acceptance: GET /collections/{name} fields are identical.

**T5: Open-jobs partial index**
- Files: store.py (open_meta_db DDL).
- Tests:
  - EXPLAIN plans for pending_jobs, `_claim_next` and the resume_pending query contain `idx_jobs_open`.
  - `pending_jobs()` is correct across enqueue, claim (processing), then done and error.
  - The existing crash-replay test (tests/test_api.py:165) and the payload-vacuum test (tests/test_store_optimizations.py:273) stay green.
- Acceptance: `[WIN]` `jobsprobe.py 300 5` pending count ≤ 0.1 ms.

**T6: Collection construction off the loop**
- Files: store.py (`touch` :1694-1717).
- Tests:
  - A manager-level test: while a monkeypatched slow `Collection` (0.5 s sleep in `__init__`) loads, a concurrent `asyncio.sleep(0.01)` probe completes in under 0.2 s.
  - The eviction round trip (tests/test_api.py:333) stays green.
  - The worker still starts on the loop.
- Acceptance: /healthz answers during a DGX cold start (protocol step 4).

**T7 (optional, separate PR, IVF build): `_vec_sample` without ORDER BY RANDOM()**
- Files: store.py (:909-917).
- Approach: draw about 1.3k unique random ids in [1, MAX(id)], fetch them through `_rows_by_id` joined on indexed records, and repeat until k are found. Fall back to the old query when density (indexed count / MAX(id)) is below about 0.2.
- Tests:
  - Returns min(k, n) distinct rows.
  - After deleting 90% of the docs it still returns k.
  - The sample mean of ids is near uniform.
  - The existing attach/detach tests stay green.
- Acceptance: `[WIN]` 65,536 sample at 300k ≤ 1.5 s (from 24.4 s). On the DGX, `index_build_s` drops.

**T8 (optional, low priority): large-k response path**
- Hydrate in `to_thread` over a threshold, and return `JSONResponse` for search and list.
- Test: JSON byte-equality against the current path for every search mode, and for list_records with `include_vector` (all values are Python-native: scores come from `float()` at store.py:1141/:1160, BM25 and RRF are Python floats, metadata comes from `json.loads`).

**T9 (conditional): upstream ids()**
- Only if the DGX phase log shows the k=n probe at ≥0.5 s.

## 7. DGX measurement protocol (for a human or executor; no agent SSH)

Preconditions:
- bench-tv holds the 2.55M arXiv `bench` collection with no IVF index. `bench.py --engine raggio` runs `ensure_no_index`.
- Record `git rev-parse HEAD` for both images.

0. **Facts to collect before starting:**
   - `podman exec bench-tv python -c "import sqlite3; print(sqlite3.sqlite_version)"` (expect 3.40.1 if the image is bookworm-based; not verified).
   - `podman exec bench-tv python -c "import sqlite3; db=sqlite3.connect('file:/data/collections/bench/meta.db?mode=ro', uri=True); print([db.execute(f'PRAGMA {p}').fetchone()[0] for p in ('page_size','page_count','freelist_count')])"`
   - `podman unshare du -sh $(podman volume inspect bench-tv --format '{{.Mountpoint}}')/collections/bench/*`
1. **Probe body (once):**
   `python3 -c "import json,random; random.seed(0); v=[random.gauss(0,1) for _ in range(1024)]; n=sum(x*x for x in v)**.5; json.dump({'query':{'vector':[x/n for x in v]},'k':10}, open('/tmp/probe.json','w'))"`
2. **Matched cold-start script** `/tmp/coldstart.sh`:
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   podman stop -t 2 bench-tv >/dev/null
   sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null
   t0=$(date +%s.%N); podman start bench-tv >/dev/null
   until [ "$(curl -s -o /dev/null -w '%{http_code}' -H 'x-api-key: bench' -H 'content-type: application/json' --data @/tmp/probe.json http://localhost:18000/collections/bench/search)" = 200 ]; do sleep 0.1; done
   t1=$(date +%s.%N); python3 -c "print('cold_start_s', round($t1-$t0, 2))"
   podman logs --since 10m bench-tv 2>&1 | grep -i coldstart | tail -1 || true
   ```
3. **Baseline (main image):**
   - Run `/tmp/coldstart.sh` 5 times and report the median and every value.
   - Also run 3 restart-only runs without drop_caches (`podman restart -t 2 bench-tv`, then the same poll) to match bench.py's method.
   - Time the stats route 5 times: `for i in 1 2 3 4 5; do curl -s -o /dev/null -w '%{time_total}\n' -H 'x-api-key: bench' http://localhost:18000/collections/bench; done`
4. **Upgrade:**
   - `podman build -t raggio:item2b .`
   - `podman rm -f bench-tv`
   - `podman run -d --name bench-tv --memory 4g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench raggio:item2b` (the same flags as docs/benchmark-arxiv.md:227)
   - Run `/tmp/coldstart.sh` once and record it as **first boot after upgrade (one-time migration)**.
   - During that run, poll `curl -s -o /dev/null -w '%{http_code} %{time_total}\n' http://localhost:18000/healthz` every second. With T6, /healthz should answer while the collection loads.
5. **After:**
   - Run `/tmp/coldstart.sh` 5 times and 3 restart-only runs, capturing each `coldstart` phase line.
   - Repeat the stats timing.
   - Plan check on the container's SQLite: `podman exec bench-tv python -c "import sqlite3; db=sqlite3.connect('file:/data/collections/bench/meta.db?mode=ro', uri=True); [print(q, '->', ' | '.join(r[3] for r in db.execute('EXPLAIN QUERY PLAN '+q, p))) for q,p in [(\"SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type\",()), (\"SELECT id FROM records WHERE indexed = 1 AND type = 'chunk' AND json_extract(metadata, ?) = ?\",('\$.year','2007')), (\"SELECT id FROM records NOT INDEXED WHERE indexed = 1 AND type = 'chunk' ORDER BY json_extract(metadata, ?) DESC, id LIMIT ? OFFSET ?\",('\$.year',20,0)), (\"SELECT r.id, -bm25(records_fts) FROM records_fts JOIN records r ON r.id = records_fts.rowid WHERE records_fts MATCH ? AND indexed = 1 AND type = 'chunk' AND json_extract(metadata, ?) = ? ORDER BY bm25(records_fts) LIMIT ?\",('\"x\"','\$.year','2007',5))]]"`
   - Expected: the first query uses a covering-index scan, the second and third use `SCAN records`, and the fourth starts with `SCAN records_fts VIRTUAL TABLE`.
6. **Full bench row, for comparability and the regression gate:**
   `uv run python bench/bench.py --engine raggio --limit 2549119 --out bench/results-arxiv-item2b.md --caps-note "raggio 4 GiB"`
   This runs the reuse path, and its cold-start row is warm-cache-biased (ADR 0003:107-109). Compare every row against bench/results-arxiv.md.
7. **Optional:**
   - Disk delta via bench's `disk_mb` or `du`.
   - Downgrade check: start the main image on the migrated volume and confirm it serves (it re-creates idx_records_doc once).
   - `raggio-ivf` column: IVF build time with T7.

**Acceptance targets (proposed):**
- Drop_caches cold start median ≤ 10 s (from about 30 s).
- The phase log shows counts + reconcile ≤ 3 s.
- Search/recall/hybrid rows within noise (p50 ±5%, filtered p50 ±10%, recall and text-hit unchanged).
- GET /collections/bench ≤ 1.5 s and no longer blocking the loop.
- One-time migration boot ≤ 60 s.

## 8. Risks

- **Planner drift.** The container probably runs SQLite 3.40.1 (not verified) and my runs used 3.47.1. Plan-pinning tests plus the step-5 in-container EXPLAIN catch drift. No ANALYZE is run, so plans rely on default heuristics, as today.
- **Scattered index pages.** In collections ingested with the new schema, the index grows interleaved with records, vecs and FTS pages. A cold index scan is then about 30k random 4 KB reads at 2.55M rather than a sequential read. This probably costs about 1-3 s at QD1 (not verified); still far below 8-10 GB of table scans. A migrated index is built contiguous.
- **One-time migration.** On an existing 2.55M database it costs about one full records scan plus a sort, on first open. Without T6 it blocks the event loop. The sorter spills to the container temp dir, roughly the size of the index (about 120 MB).
- **Correctness of the reconcile rewrite.** dtype promotion (uint64 vs int64) and assume_unique misuse. Covered by the T3 tests.
- **stats() semantics.** Unchanged (still SQL counts). I deliberately did not switch to `indexed_counts`, because the bench's ingest integrity assert (bench.py:205) depends on an independent count.
- **T6.** Exceptions from `Collection()` still propagate. `_load_lock` still serializes loads. Asyncio primitives built off-loop depend on Python ≥3.10 lazy loop binding (pyproject requires ≥3.12).
- **Partial index.** It silently stops being used if someone edits the status list text. The plan test guards this.
- **Metric honesty.** bench.py's cold start uses `podman restart` without dropping caches. Report both the matched drop_caches numbers and the bench row, as in the provenance note of results-arxiv.md.

## User decisions
- Accept a one-time index rebuild on the first open after upgrade. It costs about one full records-table scan plus a sort (0.8 s warm and 5.7 s cold-copy at 300k on the laptop; my estimate is 10-20 s at 2.55M on the DGX, not measured). Recommended: yes, run it in place inside open_meta_db, together with T6 so the event loop stays responsive. Alternative: an offline migration command.
- Which index shape. Recommended: (doc_id, type, indexed) replacing idx_records_doc, plus NOT INDEXED on list_records' page query. It adds no extra index on the write path and leaves filtered-query plans unchanged. The alternative is adding a narrower (type, indexed) index: its scans are about 4x faster, but it flips every scoped filtered query to index-driven lookups and keeps two indexes.
- Whether to keep the vestigial `indexed` column semantics. Nothing writes indexed=0 (store.py:843-844 always writes 1). Recommended: keep it in the index for now (1 byte per entry) and do the cleanup separately.
- Whether to file the upstream turbovec IdMapIndex.ids() request (draft is in the brief). Recommended: only if the DGX phase log shows the k=n probe at 0.5 s or more, and bundle it with item 3's upstream ask. Locally the probe is 41 ms at 300k.
- Whether to add a clean-shutdown marker so reconcile can be skipped. Recommended: no. Once the covering index is in place the reconcile SQL is cheap, and the marker adds crash-consistency surface.
- Scope of the optional tasks. Recommended: do T7 (_vec_sample without ORDER BY RANDOM: 24.4 s to 0.78 s for the IVF training sample at 300k) as a separate PR, because it moves IVF build time, not cold start. Defer T8 (large-k serialization and hydrate off the loop), because the bench's k=10 does not exercise it.
- Side finding for whoever owns disk footprint: at 1024-d with 4 KB pages, the vecs table stores one row per page (4111 bytes on disk per 2048-byte blob, 50% efficiency). That is about 10.5 GB of the 16.47 GB at 2.55M. A page_size of 8192 or 16384 for new collections would save about 3.5-4.5 GB but increases read amplification for random blob and hydrate reads. Recommended: investigate separately; not part of this item.

## Unverified
- That the two records-table scans account for most of the DGX's 30.4 s cold start. This is inferred from local byte counts (1236 of 1393 MB), ADR 0002:35, and the 30.4 s vs 5.1 s cold/warm gap. It needs the T1 phase log on the DGX.
- The predicted post-fix DGX cold start of about 5-8 s. The residual would be the 1.3 GB .tvim load, a covering-index scan of about 120 MB (possibly random 4 KB reads), the k=n probe, and Python/uvicorn startup.
- The arXiv records table size of about 4-5 GB and the vecs table of about 10.5 GB. These are extrapolated from the synthetic 2.06 KB/row table scan and the measured 4111 B/row vecs layout, and assume the DGX database uses page_size 4096.
- That the container SQLite is 3.40.1 (Debian bookworm libsqlite3-0 3.40.1-2+deb12u2 per packages.debian.org). The current uv Docker docs list trixie-based images, and I could not check the actual image because the local podman machine is not running.
- That query plans on SQLite 3.40.1 match the 3.47.1 plans I recorded, including the NOT INDEXED fix and the FTS-outer join order.
- The cost of the k=n search probe on the DGX at 2.55M rows. ADR 0001:33 says about 0.7 s at 553k; locally it is 41 ms at 300k.
- The one-time CREATE INDEX migration time at 2.55M with a cold cache (10-20 s is an estimate).
- That covering-index pages in collections ingested under the new schema are scattered, making a cold scan about 30k random reads costing about 1-3 s.
- That SQLite's autovacuum ptrmap overflow-page skipping is why pending_jobs read only about 4.5 MB for 300 pending 5 MB payloads.
- The effect of PRAGMA mmap_size on Linux read latency, and on the podman memory-usage metric.
- That page-cache pages survive `podman restart` (cgroup reparenting), which would make the bench's restart-only cold start partially warm.
