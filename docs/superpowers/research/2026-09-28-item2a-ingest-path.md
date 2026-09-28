# item2a-ingest-path

## Summary
Item 2a (ingest path): the biggest cost is not index syncing. It is stdlib JSON of the vectors on the enqueue side (json.dumps, 74-124 ms per 250x1024 job) and in the worker (json.loads, 41-68 ms, run ON the event loop at store.py:734). The second cost is the claim UPDATE, which rewrites the whole 2.7 MB payload overflow chain because 'pending' and 'processing' differ in length (40-66 ms per job).

I prototyped all three designs as a scratch Collection subclass (D:\DEV\_scratch\probe-item2a\proto.py):
- (a) set-based upserts with rollback and index removal after commit;
- (b) a binary journal in a new job_payloads side table, with legacy JSON rows still replayed;
- (c) a batched-sync worker with a claim cursor, plus IVF sync limited to dirty shards and vacuum run only when the queue is idle or the freelist is large.

The full raggio suite (46 tests) passes with every feature combination. A crash-snapshot probe covers five crash windows (mid-batch x3, before sync, after sync but before finish) and each ends in exactly the clean end state, with 0 ghosts and 0 missing rows.

A/B on the real worker (Windows x86-64 laptop, not DGX; 40 jobs of 250x1024; state after drain is bit-identical across variants):
- Flat drain: 468-483 vec/s to 995-1046 vec/s (about 2.1x). Most of the gain is the binary journal; batching the sync adds about 0 for flat collections.
- Event-loop stalls over 20 ms: 10.5-11.4 s down to 0.15-0.6 s.
- IVF nlist=64: 254 to 802 vec/s (3.2x). Batching is the lever there, because each job's sync is 64 fsyncs.

Expected DGX effect (UNVERIFIED): bench ingest 1.4-2x on both corpora. The next ceiling becomes request JSON parsing on the loop (about 88 ms per request); raw-body model_validate_json is an optional follow-up.

Recommended order, lowest risk first: decode off the loop, upsert hardening, binary side-table journal, vacuum policy, IVF ingest harness, dirty-shard sync, batched worker, then an ADR.

## Brief
# Item 2a research brief: faster ingest path (batched upserts, binary job payload, batched index syncs)

All timings come from a **Windows x86-64 laptop, not the DGX**. The D: drive is shared with other work, so timings vary by about ±30%; I report ranges from repeated runs. Nothing under D:\DEV\raggio or D:\DEV\_scratch\turbovec was modified. All probes and the prototype are in `D:\DEV\_scratch\probe-item2a\`.

---

## 1. How ingest works today, end to end (file:line)

**API side (runs on the event loop):**
1. FastAPI 0.141.1 reads the request body with starlette `json.loads`, then validates it against `IngestIn` (app.py:17-35).
   - `RecordIn.vector` is `list[float] | None` (app.py:17-20).
2. The route (app.py:188-196) checks vector dimensions with `_check_record` (app.py:182-186).
3. The route then calls `c.enqueue(body.model_dump())`.

**Journal:**
4. `enqueue` (store.py:671-676) runs `_enqueue_row(json.dumps(payload))` in `to_thread`, but `json.dumps` holds the GIL.
5. `_enqueue_row` (store.py:678-688) picks the job id as `MAX(id)+1` under `db_lock`, INSERTs the job, and commits.
   - Schema: `jobs(id, payload TEXT, status, error, created_at, updated_at)` at store.py:306-313.

**Worker loop** (`_run_worker`, store.py:723-740):
6. Claim: `_claim_next` (store.py:695-706) runs `SELECT id, payload ... WHERE status IN ('pending','processing') ORDER BY id LIMIT 1`, then `UPDATE status='processing'` and commits.
   - The status gets longer (7 to 10 chars), so the record size changes. SQLite then rewrites the whole cell, including the 2.7 MB overflow chain.
7. Decode: `json.loads(payload)` runs **on the event loop** at store.py:734.

**Processing** (`_process_job`, store.py:742-803):
8. Attach/detach dispatch (:744-749), flatten (:751-758), then dedup where the last occurrence wins (:764).
9. Embed in batches of 64 (:765-770), then `_normalize(np.array(..., float32))` (:771).
10. Under `lock.write` (:773):
    - `_upsert_rows` runs in a thread (:777).
    - `add_with_ids`, sliced at CAL_THRESHOLD while the calibration reservoir is armed (:783-801).
    - `# ponytail: sync after every job` (:802), then `_sync_index` (:803; store.py:860-864).

**Upsert** (`_upsert_rows`, store.py:822-856). For each row:
- SELECT the old row by external_id;
- `index.remove(old)` **before commit**, and `indexed_counts` is decremented at the same time;
- DELETE from records and vecs, then INSERT into records and vecs (FTS triggers fire per row, :333-338);
- one commit at the end, then the caches are cleared.

**Finish** (`_finish_job`, store.py:708-721): an UPDATE that sets the status and sets `payload=NULL` on done, a commit, then `PRAGMA incremental_vacuum` drained with `.fetchall()` (ADR 0001 bug fix).

**Other paths that touch the same state:**
- `delete_document` (store.py:1499-1505) and `_delete_doc_rows` (:1507-1523): remove from the index, commit, then sync.
- `stop()` (store.py:655-669): cancels the worker, then syncs under `lock.write`.
- Load:
  - `_reconcile_ghosts` (store.py:628-648) evicts index ids that have no record. It does not repair records that are missing from the index.
  - `indexed_counts` are rebuilt from records (:575-577).
- Boot: `resume_pending` (:1731-1740).
- Eviction guards on `pending_jobs()==0` (:1694-1717, :1742-1749).
- `IdMapIndex.remove(absent_id)` returns False and does not raise (checked in the venv). This is why replaying after a crash between commit and sync works.

**IVF:**
- `_IvfIndex.sync` (store.py:422-429) syncs **every** shard on every job.
- `add_with_ids` (:448-453) and `remove` (:455-461).
- Auto nlist is about rows/8192, which gives 64 at 553k rows and 256 at 2.55M (:356-359).

## 2. Documented behaviour this work must keep

**ADR 0001** (docs/adr/0001-performance-optimization-decisions.md):
- :41 Single-writer discipline: every write transaction runs inside `db_lock`, in a thread. Ingest cost 785 to 483 vec/s, accepted for durability. Two write connections and loose connection sharing were rejected.
- :42-43 `auto_vacuum` ordering and the `fetchall` drain are bug fixes and must stay.
- :50 "known lever: batch index syncs / vacuum every N jobs instead of per job".
- :31 orjson was rejected as a runtime dependency (for responses).

**ADR 0002:** attach/detach are jobs on the same queue (serialized, replayable). fp16 vecs cost 4% of ingest.

**README:148-151 and docs/storage.md:25-35:**
- The job is journaled before the 202.
- `pending` and `processing` jobs replay on boot, and upserts are idempotent.
- "a job is marked done only after the index is synced".
- Failed jobs keep their payload.

**Tests that pin this behaviour:**
- test_store_optimizations.py:
  - reconcile ghosts :35
  - duplicate external_id :54
  - calibration :151, :164, :180, :197 (Spy, `calls==[6]`), :225
  - stress test :235 (3 enqueuers x 10 jobs, asserts 300 records and 30 done)
  - auto_vacuum :266
  - payload pages freed :273 (freelist ≤ 1)
  - fail-closed :293
- test_api.py:
  - test_persistence_and_crash_replay :165. It inserts a **legacy JSON TEXT payload with status 'processing'**; this is the backward-compatibility pin.
  - also :73, :133, :333
- test_ivf_index.py: :56, :87, :105, :191

## 3. Measurements (laptop, not DGX)

### 3.1 API boundary cost per 250x1024 job (`boundary_probe.py`, best of 5; bench shape: vectors rounded to 5 decimals, 2.44 MB body)

| Step | Cost |
|---|---|
| starlette `json.loads(body)` (loop) | 88 ms (earlier runs about 55) |
| `IngestIn.model_validate` (loop) | 11 ms |
| `model_dump` (loop) | 10 ms |
| **`json.dumps` journal**, 2.71 MB TEXT | **74-124 ms** |
| **worker `json.loads`** (on the loop) | **41-68 ms** |
| `np.array(list-of-lists)` | 13 ms |
| Binary split: `model_dump(exclude=vectors)` + `np.array(f32)` + `tobytes` | **11-13 ms** (0.30 MB JSON + 1.02 MB f32) |
| Binary decode: `json.loads(meta)` + `frombuffer` | **0.7-1 ms** |
| Alternative: `model_validate_json(raw bytes)` | 44 ms, vs 99 ms for loads + validate |

The binary path is bit-exact: today the worker converts the parsed doubles to float32 anyway (store.py:771). A/B hashes of the vecs blobs are identical (3.4).

### 3.2 Journal SQL cost (`journal_probe.py`; 30-job backlog; per job)

| Variant | enqueue | claim | vacuum |
|---|---|---|---|
| inline JSON (today) | 71-94 ms | **46-48 ms** | 79 ms |
| inline BLOB | 41-46 ms | 40-66 ms (the size-change rewrite remains) | 18-33 ms |
| **side-table BLOB** | 41 ms (noisy rep: 118) | **3-10 ms** | 38-54 ms per job; 7-20 ms if vacuumed only at idle |

### 3.3 turbovec 1.0.0 sync costs

**Protocol** (io_v7.rs:1-82, :100-111; plan_incremental :674-730):
- Appends are written whole.
- A removal is committed as a redo op in the header.
- The **next** sync materializes that op by rewriting the 32-row unit.
- If a header would carry more than MAX_OPS=1024 ops, turbovec falls back to a full temp-file rewrite plus rename.
- Loading a claimed file binds the sync cursor (id_map.rs:883-890). A sync to a different path is a full write.

**Flat index, fresh rows** (`steady_probe.py`, 200k x 1024): 7-9 ms per sync (fsync-bound). That is about 2-5% of per-job time, so **batching buys nothing for flat, append-only ingest**.

**Removals (re-upserts), steady state:** about **1 ms per removal per sync** from materialization.

| Removals per sync | Sync time |
|---|---|
| 64 | 71-84 ms |
| 250 | 244-259 ms |
| 500 | 452-480 ms |
| 2000 | 1.35-1.41 s (full rewrite) |

- Full rewrite costs about 7 µs per indexed row: 1.4 s at 200k, 2.9-3.0 s at 400k. Extrapolated about 18-20 s at 2.55M (UNVERIFIED), held under `lock.write`.
- Chunking one big removal set into syncs of ≤512 is **not** cheaper at these sizes: R=5000 took 4.8 s chunked vs 1.4-2.9 s for one full rewrite (`chunk_probe.py`, `chunk_probe2.py`).
- Consequences:
  - A single job that replaces 1024 or more indexed rows already triggers a full rewrite **today**. Leave that as is.
  - Batching must never **merge** removals from several jobs past about 512 in one sync, or it would create full rewrites that per-job syncing never hit.
  - The bench path has no removals (`--reingest` deletes the collection first; bench.py:195-196).

**IVF sync fan-out:** every job syncs all nlist shards at about 8 ms per shard sync. In the end-to-end A/B, about 480 ms of the 980 ms per job at nlist=64. With a random 250-row job, about 160 of 256 shards are dirty at nlist=256; a batch of 2000 rows dirties all of them.

### 3.4 End-to-end A/B of the prototype (`ab_probe.py`; real worker, backlog of 40 bench-shaped jobs pushed by 2 concurrent enqueuers, 12k prefill, calibrated, every 5th job a 250-row re-upsert; 2 reps)

| Variant | Drain vec/s | Enqueue done (40 jobs) | Loop stalls >20 ms (sum) |
|---|---|---|---|
| base (main) | 468-483 | 8.1-8.2 s | 10.5-11.4 s |
| + bin (side-table binary journal, decode off the loop) | 810-849 | 3.6-4.2 s | 0.4-0.5 s |
| + ups (set-based upsert) | 815-955 | 3.5-4.3 s | 0.15-0.3 s |
| + vac (vacuum at idle or freelist ≥ 16384) | 995-1046 | 3.3-3.9 s | 0.15-0.6 s |
| + batch (8 jobs / 8192 rows / 1 s / 512 removals) | 1001-1038 | 3.6-4.0 s | 0.5 s |

- **Equivalence: IDENTICAL for every variant.** This covers hashes of records, vecs blobs, the FTS hit set, top-10 of 20 queries, counts, calibration state, and the reloaded fingerprint. Reload time is unchanged (80-110 ms).
- **IVF nlist=64** (`IVF=64`; 24 jobs):

  | Variant | Drain vec/s |
  |---|---|
  | base | 254 |
  | bin+ups+vac | 408 |
  | + dirty | 404 |
  | + batch | **802** |

  - IVF top-10 differs between *any* two runs, including base vs base, because `_vec_sample` trains on `ORDER BY RANDOM()` (store.py:909-913). Records, vecs and FTS were identical.
  - Dirty-only sync does not help 250-row random jobs, but it turns a one-row trickle job from nlist fsyncs into one.

### 3.5 Crash windows (`crash_probe.py`)

**Method:** copy the collection dir (meta.db + WAL + tvim) under `db_lock` at the crash point, reopen the copy (reconcile + replay), drain it, and compare with a crash-free run. The backlog was 6 jobs in one batch, including a re-upsert of synced rows and a re-upsert of rows from the same batch.

| Crash point | Jobs at restart | Index at restart | Result |
|---|---|---|---|
| mid-batch, after 1 job | 1 processing | 3000 | identical |
| mid-batch, after 3 jobs | 3 processing | 2800 (200 ghosts evicted) | identical |
| mid-batch, after 5 jobs | 5 processing | 2800 | identical |
| before sync | 6 processing | 2800 | identical |
| after sync, before finish | 6 processing | 3800 | identical |

All five: **0 ghosts, 0 missing, 6 done**.

### 3.6 Test suite against the prototype

A pytest plugin (`proto_plugin.py`) swaps in `store.Collection` and patches `_IvfIndex`. **46/46 tests pass** for these feature sets: bin+ups+batch+vac (+dirty), bin+batch, ups, dirty. Baseline is also 46/46.

Command:
```
PYTHONPATH="D:/DEV/raggio/src;D:/DEV/_scratch/probe-item2a" PROTO_FEATURES=... python -m pytest -p proto_plugin -p no:cacheprovider D:/DEV/raggio/tests
```

## 4. Designs

### (b) Binary job payload: RECOMMENDED side table

- **Schema:** `job_payloads(job_id INTEGER PRIMARY KEY, data BLOB)`, created in `open_meta_db` with CREATE IF NOT EXISTS. The change is additive.
- **Enqueue:** INSERT the job row with `payload=NULL` and the payload row in **one transaction** under `db_lock`.
- **Format:** `struct '<4sIII'` = magic `RGJ\x01`, json_len, n_vecs, dim; then the metadata JSON; then little-endian float32 `n_vecs x dim`.
  - In the JSON, each supplied `"vector"` is replaced by its row index; `null` means "embed this".
  - Explicit `<f4` makes the format safe on aarch64 and x86.
- **Route:** build the split from the pydantic model (`model_dump(exclude=vectors)` plus the vector lists into one `np.float32` array), then `enqueue(payload, vectors)`. Dimensions are already validated at app.py:182-186, so the matrix is always rectangular.
- **Claim:** `SELECT j.id, j.payload, p.data FROM jobs j LEFT JOIN job_payloads p ...`, decoded **in the claim thread**.
  - If `data` is present, decode binary.
  - Otherwise, if `payload` is a str, it is a legacy JSON job (pending at upgrade time).
  - If both are NULL, mark the job error with a clear message.
- **Finish:** on done, DELETE the payload row. On error, keep it (storage.md:32-33 stays true).
- **Why a side table rather than a BLOB in the existing column:** a BLOB in `jobs.payload` needs no schema change (TEXT affinity does not coerce BLOBs; type tells old from new), but it keeps the 40-66 ms claim rewrite.
- **Downgrade:** older code would `json.loads(None)` and mark new-format pending jobs error. Document "drain the queue before downgrading" (user decision).

### (a) Upserts: set-based and failure-safe

- Wrap the transaction in `try/except BaseException: db.rollback(); raise`.
- Look up old rows in chunks of 512 with `IN (...)` (same pattern as `_rows_by_id`, store.py:167-172).
- Use `executemany` for the DELETEs and INSERTs.
- Move `index.remove(old)` and the `indexed_counts` updates to **after the commit**.
- Return the number of indexed removals so the caller can enforce the removal cap.
- Gain is small (0-12%). The main value is correctness; see the bug below.
- Merging several jobs into one SQLite transaction was considered and rejected: it holds `db_lock` longer (enqueues block) and saves about one fsync per job.

**Latent bug in today's code (store.py:822-856):** a failure mid-loop (for example SQLITE_FULL or an I/O error) leaves the transaction open. pysqlite's legacy implicit transactions stay open, and there is no rollback. The index removals for old rows have already happened. `_run_worker` then calls `_finish_job` ('error'), and its `commit()` **commits the half-applied upsert**. The result is deleted or replaced records without index entries, under a terminal 'error' job that never replays. Reconcile does not repair missing rows.

### (c) Batched index sync

**Worker loop:**
1. Claim job ids strictly after the last id in the open batch. This claim cursor is REQUIRED:
   ```
   WHERE status IN ('pending','processing') AND id > ? ORDER BY id LIMIT 1
   ```
   Without it, the claim returns the batch's own still-'processing' job again. The prototype first re-processed the same job about 8 times; it was silently idempotent and 3x slower.
2. Process the job without syncing. Take `lock.write` per job for upsert + add + calibration only.
3. Stop the batch when the queue is empty, or when a cap is hit:
   - jobs cap;
   - elapsed ≥ max(1 s, 2 × last sync duration) (recommended adaptive rule; the prototype used fixed caps of 8 jobs / 8192 rows / 1 s);
   - **removal cap:** before calling `index.remove` for a job, if `removed_since_sync > 0` and `removed_since_sync + len(old) > 512`, sync first, under the same lock. Batching then never creates a full rewrite that per-job syncing would not have. `_sync_index` resets the counter, which also covers `delete_document`'s syncs.
4. Flush: one `_sync_index` under `lock.write`, then **one finish transaction** for all jobs in the batch (done or error), then the vacuum policy.
5. Attach/detach are barriers: flush the open batch, then run the index job alone with its own sync (the existing semantics).
6. **Sync failure:** today a sync exception makes the job 'error' although its records are committed. In the prototype an exception in `_flush` would kill the worker task; this must be handled. Recommended: log, keep the batch 'processing', back off (1 s to 30 s) and retry. turbovec drops the cursor on a failed sync (lib.rs sync_v7_impl), so the retry is a full write.

**IVF dirty-only sync:**
- Keep a `_dirty` set, initialised to all shards on construction.
- `add_with_ids` marks `np.unique(asg)`; `remove` marks the owning shard.
- `sync(dir)` syncs only dirty shards, or all of them when `dir` differs from the last synced dir (attach tmp dir, then rename).
- Clear a shard's flag only **after** its sync succeeds.

**Vacuum policy:**
- After the finish commit, run a full `incremental_vacuum` only if no jobs are open.
- Otherwise, if `freelist_count ≥ 16384` (64 MB), run a chunked `incremental_vacuum(2048)`.
- Keep the `.fetchall()` drain.
- Freed payload pages are then reused by records, vecs and FTS pages instead of forcing tail relocation on every job.

### Crash-window analysis (batched worker; verified in 3.5)

**W0: crash before any record commit in the batch.** The claimed jobs are still 'processing' and everything replays. The on-disk index is the last sync. Consistent.

**W1: crash after job k's records commit, before the sync.** Two leftovers:
- Replaced rows' old ids are ghosts in the .tvim; reconcile evicts them.
- Committed new rows are missing from the index; the replay's upsert finds them, `remove` returns False, and it re-inserts them under fresh ids and adds them.

Verified equal to a clean run.

**W2: crash after the sync, before finish.** The whole batch replays, which is redo work only. The removal cap also holds during the replay, so no full-rewrite cliff.

**W3: crash during finish.** A single transaction, so all or nothing.

**Also covered:**
- A `delete_document` sync in the middle of a batch is only an earlier sync of the same state, so it is safe.
- `stop()` syncs, and unfinished jobs replay.
- The eviction guard holds because batch jobs stay 'processing'.
- Calibration is unchanged: it stays in memory, and the next sync after `calibrate` is a full write. A crash before that sync reloads the index uncalibrated below the threshold (last sync < 10k) and re-arms.

**Semantic change:** a job becomes 'done' up to one batch later, and rows are searchable from memory before 'done'. Today that window already exists between the lock release and the finish.

## 5. Expected gains

**Laptop (drain phase, flat):**
- about 2.1x;
- the loop is almost stall-free;
- the enqueue side is about 2x faster;
- IVF nlist=64 ingest about 3.2x.

**DGX (UNVERIFIED):**
- The email bench shows POSTs done at about 256 s of 500 s, then queue drain (docs/benchmark.md:72-75). So drain is the bottleneck, and doubling it moves the limit to the request side.
- Estimate: ingest 1105 to about 1600-2200 vec/s (email; 1536-d makes the JSON savings bigger), and 1357 to about 1800-2500 vec/s (arxiv; real abstracts make FTS a larger share).
- The next ceiling is request parsing on the loop (about 99 ms per request); optional T9 cuts it to about 44 ms.
- Batched sync and dirty-only sync do not show on bench.py at all: the raggio column is flat, and raggio-ivf only measures the index build (bench.py:219-230, :399-402). That is why T5 below exists.

## 6. Ordered task list (lowest risk first)

### T1. Decode the job payload off the event loop
- **Files:** store.py:695-740. `_claim_next` returns `(id, decoded dict)` from inside the thread; a decode failure becomes an 'error' finish.
- **Tests:**
  - full existing suite;
  - new `test_claim_returns_decoded_payload`.
- **Accept:** no behaviour change; laptop drain improves and the worker decode no longer stalls the loop.

### T2. Upsert hardening and set-based SQL
- **Files:** store.py:822-856; `_process_job` uses the returned removal count.
- **Tests:**
  - New `test_upsert_failure_rolls_back_and_keeps_index`. Install a connection-scoped `CREATE TEMP TRIGGER ... BEFORE INSERT ON vecs WHEN NEW.id=X BEGIN SELECT RAISE(ABORT,'boom'); END`, ingest a re-upsert, then assert:
    - job 'error';
    - old records and vecs intact;
    - index still contains the old ids;
    - `indexed_counts` unchanged;
    - the next job's commit does not persist partial rows.
  - Existing :54, :225, test_ivf :87 and :105.
- **Accept:** A/B fingerprint identical (`ab_probe.py`); throughput ≥ base.

### T3. Binary side-table journal
- **Files:**
  - app.py:188-196: split the vectors out;
  - store.py:306-313: new table;
  - store.py:671-721: enqueue, claim, finish;
  - `request_index` and `request_index_drop` (:875-888) enqueue with n_vecs=0.
- **Tests:**
  - test_persistence_and_crash_replay unchanged (legacy JSON 'processing' row);
  - new `test_binary_journal_bit_exact` (same doc via a legacy JSON row and via binary; vecs blobs and top-k equal);
  - new `test_mixed_legacy_and_binary_queue_in_order`;
  - new `test_error_job_keeps_payload_row`;
  - new `test_enqueue_writes_job_and_payload_atomically`;
  - :273 adapted (freelist ≤ 1 after idle).
- **Accept:** equivalence; laptop drain ≥ +60%; DGX bench ingest vec/s up with zero error jobs.

### T4. Vacuum policy
- **Files:** store.py:708-721 (constants next to CAL_*).
- **Tests:**
  - :273 (idle → freelist ≤ 1);
  - new `test_freelist_bounded_during_backlog` (monkeypatch the threshold small, then assert `freelist ≤ threshold + one job's pages` throughout).
- **Accept:** laptop +5-15%; DGX bench `disk_mb` ≤ base + 64 MB; cold start not worse.

### T5. IVF ingest harness `bench/ingest_probe.py`
- A port of `D:\DEV\_scratch\probe-item2a\ab_probe.py`: in-process worker drain, optional `--ivf NLIST`, `--prefill`, `--jobs`, and a fingerprint.
- **Why:** bench.py never ingests into an IVF collection. Land it before T6/T7 to capture the main baseline.
- **Accept:** runs on the DGX host with `uv run`.

### T6. IVF dirty-only shard sync
- **Files:** store.py:362-461.
- **Tests:**
  - new spy test: a 1-row job syncs exactly one shard;
  - first sync after load or attach syncs all shards;
  - a shard whose sync fails stays dirty;
  - existing test_ivf :56, :87, :105, :191.
- **Accept:** trickle-ingest per-job sync cost proportional to dirty shards.

### T7. Batched-sync worker
- **Files:** store.py:723-803 (claim cursor, caps, barriers, one finish transaction, sync retry), :860-864 (reset the removal counter in `_sync_index`).
- **Tests:**
  - crash windows mid / before sync / after sync (snapshot technique from `crash_probe.py`: copytree under `db_lock`, reopen, drain, compare);
  - error isolation (one bad job among good jobs in one batch: only it is 'error', the others done, a single sync);
  - calibration at threshold when several small jobs cross 10k inside one batch (Spy as in :197);
  - stress test :235;
  - removal cap (3 re-upsert jobs of 300 rows: spy asserts no sync carries more than 512 removals);
  - attach barrier (ingest, attach, ingest: the attach runs alone);
  - sync failure keeps jobs 'processing' and retries;
  - claim cursor (no job is processed twice; spy on `_process_job`).
- **Accept:** laptop IVF drain ≥ 1.8x over T6; flat not worse; DGX IVF harness ≥ 1.5x.

### T8. Docs and ADR
- New ADR 0004 "Ingest journal format and batched index sync".
- Update storage.md:25-35 (done after the batch sync; side-table payload; error keeps the binary payload).
- Point ADR 0001:50 at the lever now used.
- Fix storage.md:37-38 "index.tvim is rewritten on every sync", which is stale with v7 incremental sync.

### T9 (optional; user decision)
- Parse the raw body with `IngestIn.model_validate_json` and map `ValidationError` to FastAPI's 422 shape.
- Partial index on open job statuses.
- orjson.

## 7. DGX measurement protocol (to be run by a human)

**Build:**
```
git checkout main && podman build -t raggio:base .
git checkout <item2a-branch> && podman build -t raggio:item2a .
```
Stop vLLM first; keep the host otherwise idle.

**Corpus selection:** bench.py reads fixed paths `bench/corpus/embed-vecs.npy|embed-meta.json|abstracts.jsonl` (bench.py:26-32). Place the right corpus there before each set, and run `rm -f bench/fingerprint-*.json bench/results-partial.json` when switching (docs/benchmark-arxiv.md:235).

**Email corpus (553k x 1536, 1 GiB cap)**, alternating base, item2a, base, item2a, with `TAG` in {base, item2a} and `R` the repetition:
```
podman rm -f bench-tv; podman volume rm bench-tv
podman run -d --name bench-tv --memory 1g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench raggio:$TAG
rm -f bench/results-partial.json
uv run python bench/bench.py --engine raggio --reingest --out bench/results-item2a-email-$TAG-$R.md
podman exec bench-tv python -c "import sqlite3;c=sqlite3.connect('file:/data/collections/bench/meta.db?mode=ro',uri=True);print(c.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status').fetchall(),c.execute('PRAGMA freelist_count').fetchone()[0])"
```

**Arxiv corpus (2.55M x 1024, 4 GiB cap):** smoke first, then one full run per tag (two if time allows):
```
podman rm -f bench-tv; podman volume rm bench-tv
podman run -d --name bench-tv --memory 4g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench raggio:$TAG
rm -f bench/results-partial.json
uv run python bench/bench.py --engine raggio --reingest --limit 20000 --queries 100 --out bench/results-item2a-arxiv-smoke-$TAG.md
nohup env PYTHONUNBUFFERED=1 uv run python bench/bench.py --engine raggio --reingest --limit <N> --out bench/results-item2a-arxiv-$TAG.md --caps-note "raggio 4 GiB" > bench/item2a-arxiv-$TAG.log 2>&1 &
```
`<N>` is the kept count from the arxiv prep; the documented run is 2,549,619.

**IVF ingest (after T5):** run on each checkout (copy `ingest_probe.py` into the main checkout):
```
uv run python bench/ingest_probe.py --prefill 100000 --ivf 256 --jobs 200
uv run python bench/ingest_probe.py --prefill 100000 --ivf 64 --jobs 200
uv run python bench/ingest_probe.py --prefill 100000 --jobs 200
```
The last command is the flat run.

**Acceptance (per corpus, item2a vs base):**
- `ingest_vps` up. Target ≥ 1.3x; the floor is above base's run-to-run spread.
- **Zero lost jobs:** the run completes (`tr_wait_job` raises on 'error', bench.py:184-190; count assert bench.py:392) and the jobs table shows only 'done'.
- `recall_at_10` within ±0.002 of base. Stored vectors are bit-identical by construction; only tie order may differ.
- `cold_start_s` ≤ base × 1.1.
- `disk_mb` ≤ base + 64 MB.
- `mem_after_ingest_mb` ≤ base × 1.05.
- IVF harness: item2a ≥ 1.5x base at nlist 64 and 256.

## 8. Risks

1. **The batched worker restructure is the riskiest change:**
   - the claim-cursor bug is silent (idempotent, only slow);
   - an unhandled flush exception kills the worker;
   - merging removals across jobs can hit the full-rewrite cliff (up to about 20 s under `lock.write` at 2.55M, UNVERIFIED).

   The removal cap, the retry path and the tests above are mandatory.
2. **Downgrade:** the binary journal cannot be read by older builds; the queue must be drained first.
3. **Deferred vacuum:** a larger single vacuum under `db_lock` blocks enqueues. Use chunked `incremental_vacuum(2048)`.
4. **Crash redo grows** to one batch.
5. **Existing issues not fixed here:**
   - removal-heavy (re-upsert) workloads cost about 1 ms per replaced row per sync (laptop);
   - a single job with 1024 or more replacements full-rewrites;
   - the first sync after IVF attach full-writes every shard (tmp-path binding);
   - a cancelled `to_thread` keeps running after `stop()`.
6. **Laptop noise is ±30%.** Absolute DGX gains are unknown; the email corpus is 1536-d, the probes were 1024-d.
7. **Bench blind spots:** bench.py does not exercise IVF ingest, removals, or search latency under ingest. Hence T5 and the unit tests.

## 9. Scratch artifacts (D:\DEV\_scratch\probe-item2a\)

**Prototype and harness:**
- `proto.py` — the prototype subclass; features bin/ups/batch/vac/dirty.
- `proto_plugin.py` — pytest swap.
- `ab_probe.py` — A/B plus equivalence; `IVF=nlist` env var.
- `crash_probe.py` — crash windows.

**Measurement probes:**
- `boundary_probe.py`
- `journal_probe.py`
- `worker_probe.py`
- `steady_probe.py`
- `chunk_probe.py`
- `chunk_probe2.py`
- `cliff_probe.py`
- `sync_probe.py`
- `wal_probe.py`
- `ivf_sync_probe.py`

## User decisions
- What to do when a batched index sync fails. Options: (a) keep the batch's jobs 'processing', log, and retry with backoff (1 s to 30 s); (b) mark every job in the batch 'error', which matches today's per-job behaviour but leaves committed rows under terminal error jobs. Recommended: (a).
- Downgrade compatibility of the binary journal. Options: document 'drain the ingest queue (no pending or processing jobs) before downgrading'; dual-write JSON as well (doubles journal bytes, rejected); or ship a decode shim in a patch release of the previous version. Recommended: document the drain requirement in ADR 0004 and the release notes.
- Batch caps for the batched-sync worker. Recommended adaptive rule: flush when the queue is empty, or when elapsed time ≥ max(1 s, 2 x last sync duration), or at 64 jobs, or before a job's removals would take the unsynced removal count past 512; attach/detach always run alone. Simpler alternative: fixed 8 jobs / 1 s / 512 removals (what the prototype measured).
- Whether to ship the batched-sync worker (T7) at all, given it gives about 0 gain for flat collections and bench.py cannot see it. It doubles to triples IVF-attached ingest and is the ADR 0001 lever. Recommended: yes, after T1-T6 have landed and been measured, and gated on the IVF ingest harness (T5) showing ≥ 1.5x on the DGX.
- Vacuum policy thresholds. Recommended: full incremental_vacuum when the queue is idle; otherwise chunked incremental_vacuum(2048) when freelist ≥ 16384 pages (64 MB). This bounds transient disk growth to +64 MB over today.
- Request-side parsing (next ceiling). Recommended: parse the raw body with IngestIn.model_validate_json (no new dependency; about 99 ms to about 44 ms per 250x1024 request on the laptop) and keep FastAPI's 422 error shape. Keep orjson out of runtime dependencies, consistent with ADR 0001:31.
- Where to document the change. Recommended: a new ADR 0004 'Ingest journal format and batched index sync' rather than amending ADR 0001; also update docs/storage.md:25-35.
- Keep synchronous=FULL and the per-commit durability contract; do not adopt synchronous=NORMAL or a separate queue.db in this item. Recommended: keep as is.

## Unverified
- All absolute throughput numbers are from a Windows x86-64 laptop with a contended disk (±30%), not the DGX. The DGX gain range (email about 1600-2200 vec/s, arxiv about 1800-2500 vec/s) is an extrapolation.
- That the arxiv bench ingest is drain-bound like the email one. docs/benchmark.md:72-75 states it only for the email run.
- DGX fsync and per-shard sync cost (about 8 ms per shard sync on the laptop), so the IVF gain magnitude on the DGX is unknown.
- Full-rewrite cost at 2.55M rows (about 18-20 s) is extrapolated from 1.4 s at 200k and 2.9-3.0 s at 400k on the laptop.
- Removal materialization cost (about 1 ms per removal per sync) on DGX NVMe. It may be much lower there.
- How many IVF shards a real-corpus 250-row job dirties (random data dirties about 160 of 256). This decides the benefit of dirty-only sync for large batches.
- How the DGX keeps the two corpora on disk. bench.py reads fixed bench/corpus/* paths (bench.py:26-32), so the operator must swap them.
- The adaptive batch rule (max(1 s, 2 x last sync)), the pre-removal flush placement, the sync-failure retry path, and chunked incremental_vacuum(2048) were designed but not prototyped. The prototype used fixed caps with a post-job removal check, full vacuum above the threshold, and no retry: a flush exception would kill the worker.
- model_validate_json on the raw body preserving FastAPI's 422 error shape was not prototyped.
- The 44 ms model_validate_json figure and the 88 ms starlette json.loads figure come from single laptop runs; earlier runs showed about 55 ms for json.loads.
