# Storage & durability

## Layout

```
/data/catalog.db                     collections registry + key hashes
/data/collections/<name>/index.tvim  turbovec quantized vector index (4-bit ≈ 8x smaller)
/data/collections/<name>/meta.db     text, metadata, fp16 vector copies, FTS5 (BM25) index, job queue
/data/collections/<name>/ivf/        IVF shards + centroids (only when an index is attached)
```

Each collection is physically separate: its vector index and SQLite database
live in their own directory, and deleting a collection removes the directory.

`meta.db` holds a `records` table (chunk/summary text, positions, JSON
metadata), a `vecs` table with an fp16 copy of each vector, an FTS5 full-text
index kept in sync by triggers, and the ingest job queue. Searchable vectors
live in the turbovec index, quantized to the collection's `bit_width`; the
fp16 copies are disk-only and exist so the index representation can be rebuilt — they are
what makes [attaching or removing an IVF index](indexing.md) possible, since
the quantized index cannot reconstruct its vectors. When an IVF index is
attached, `ivf/` replaces `index.tvim` as the live representation (the catalog
records which one is current).

## Ingest durability

1. `POST /documents` journals the job to `meta.db` **before** the `202`
   response is sent: a row in `jobs` (status and timestamps) and, in the
   same transaction, the payload as one binary row in `job_payloads` (the
   request's JSON fields, with the vectors stored as little-endian float32).
2. A per-collection worker embeds missing vectors, writes records, and
   updates both indexes.
3. The job is marked `done` only after the index sync that covers it has
   written its vectors to disk. One sync covers a batch of up to
   `SYNC_BATCH_JOBS` jobs, or the jobs of `SYNC_BATCH_MS` milliseconds (see
   [Configuration](getting-started.md#configuration)), so a finished job can
   stay `processing` that long plus the time the batch's next job takes (the
   time cap is checked as each job joins), and for as long as a failing sync
   is retried.
   A successful job's payload row is deleted so vector-heavy jobs don't
   accumulate; a failed job keeps it for diagnosis. A payload that cannot
   be read fails its job with a `bad job payload: ...` error. Freed pages
   go back to the OS once the queue is empty; while a backlog drains,
   `meta.db` keeps at most 16,384 free pages (64 MB at SQLite's default
   4 KiB page size) and trims the excess as each job finishes.

After a crash, any `pending` or `processing` job is replayed on boot. A
crash before a batch's sync lands leaves all of the batch's jobs
`processing`, so the whole batch replays; a crash while its jobs finish
replays only the ones not yet `done`. A shutdown whose last index sync
fails (a full disk, say) logs the error and still closes; that batch's
jobs stay `processing` and replay the same way. When the shutdown finds
the ingest worker stopped by an error, it logs the error and still syncs
and finishes the worker's open batch; only a job the worker was finishing
when it failed stays `processing` and replays. When the shutdown cannot
mark synced jobs `done`, it logs the error, and that job and the rest of
its batch replay. Either way every collection still closes. A collection
whose database connections, embedding client or IVF thread pool fail to
close logs the error, and the shutdown goes on to its next close step and
the next collection. Replays are idempotent: records are upserted by id.
A document deleted or patched while a job that wrote it is still
`processing` comes back or reverts if that job replays. Jobs journaled by a
release from before the binary job journal, whose JSON payload sits in
`jobs.payload`, still replay after an upgrade. The reverse does not hold: an
older release cannot read `job_payloads`, so drain the ingest queue
(`pending_jobs` is 0 in `GET /collections/{name}` for every collection)
before downgrading.

## Backup

There is no backup API; a backup is a copy of `DATA_DIR`. `meta.db` is a WAL
SQLite database, and every index sync writes to `index.tvim` (or to the
changed shard files under `ivf/`), so copy while
nothing writes: stop the container, or copy a collection's directory while it
is offloaded (idle past `COLLECTION_IDLE_TTL` with no pending jobs — `/healthz`
lists the resident ones). Restore by placing the directory back on the volume
before start; interrupted jobs replay as after a crash. For a logical export
that survives version changes, page through
`GET /collections/{name}/documents?include_vector=true` and re-ingest.

## Upgrading

**2026-09 release (cold start and loop hygiene).** The first open of each
collection after the upgrade runs a one-time migration in its `meta.db`: it
builds the covering index `idx_records_doc_type` (doc_id, type, indexed), then
drops the old `idx_records_doc`, and adds a small partial index over open jobs.

- **Time:** about 20 s per 1M records when the file is not in the page cache
  (49.6 s at 2.55M records on a DGX Spark under a 4 GiB cap), 1.5 s at 2.55M
  when it is. Requests for any collection that is not loaded yet wait for it
  (loads and migrations run one at a time); `/healthz` and collections that are
  already loaded keep answering. A collection with unfinished jobs is loaded, and
  migrated, at start-up, before the server begins answering.
- **Disk:** `meta.db` grows by the new index (+92 MB at 2.55M records). About
  82 MB of free pages from the dropped index stay in the file until a finished
  job leaves the queue empty (`PRAGMA incremental_vacuum`; while a backlog
  drains, each finished job trims them to at most 64 MB); a `meta.db` created
  before incremental auto-vacuum keeps them until a `VACUUM`. The WAL peaks
  around 105 MB during the build.
- **Logs:** the start (`one-time migration`), a heartbeat every 10 s
  (`still building`) and the finish (`built idx_records_doc_type in`) are logged
  at INFO.
- **Interruption is safe:** the old index is dropped only after the new one is
  built, and the next open finishes whatever is missing.

## Memory management

Collections load into memory on first touch and are offloaded (synced +
dropped) by two policies:

- **LRU cap** — loading a collection beyond `MAX_RESIDENT_COLLECTIONS` evicts
  the least-recently-used idle one first. Collections with pending ingest jobs
  are never evicted, so the cap can be temporarily exceeded while every
  resident collection is busy ingesting.
- **Idle TTL** — a collection untouched for `COLLECTION_IDLE_TTL` seconds is
  offloaded by a background sweep.

Total stored data can therefore far exceed container memory; only actively
used collections pay the RAM cost.

!!! note "Scaling is per collection"
    A *resident* collection's vector index lives fully in RAM, so out-of-memory
    scaling applies **across** collections, not within one. Size individual
    collections to fit memory and spread data over multiple collections.

Index jobs (attach and remove) hold a second copy of the index while they build.
When a job ends, raggio calls glibc's `malloc_trim(0)`, and the image sets
`ENV MALLOC_TRIM_THRESHOLD_=134217728`, so the build's buffers go back to the OS
instead of staying in the process heap. Without them, a 2.55M × 1024-d collection
stayed at about 3.0 GiB after a build against 1.5 GiB loaded, the next index job was
refused, and a container without swap was OOM-killed mid-build. Images built
without the shipped `Dockerfile` should set the same variable.

## Sizing

A resident collection's index needs roughly
`dim × bit_width / 8` bytes per record, plus index overhead:

| dim | bit_width | RAM per 1M records |
|---|---|---|
| 1536 | 4 | ≈ 0.77 GB |
| 1536 | 2 | ≈ 0.38 GB |
| 768 | 4 | ≈ 0.38 GB |

An attached [IVF index](indexing.md) holds the same codes plus ~0.5–1 MB fixed
RAM per shard (e.g. `nlist=256` ≈ +0.15–0.25 GB). On disk, the retained fp16
vector copies add `dim × 2` bytes per record to `meta.db` (1536 dims ≈ 3 GB per
1M records) whether or not an index is attached.

Budget: `MAX_RESIDENT_COLLECTIONS × (largest collection's index)` must fit in
container memory, with headroom for SQLite page cache and request handling.
Text, metadata, and the FTS5 index are disk-backed SQLite and don't need to be
resident. The `trigram` tokenizer grows the on-disk FTS index several-fold
compared to `unicode61` — prefer the default unless substring matching is
required.
