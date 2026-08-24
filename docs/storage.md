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
   response is sent.
2. A per-collection worker embeds missing vectors, writes records, and
   updates both indexes.
3. The job is marked `done` only after the vector index is synced to disk.
   Successful payloads are cleared so vector-heavy jobs don't accumulate;
   failed jobs keep their payload for diagnosis.

After a crash, any `pending` or `processing` job is replayed on boot.
Replays are idempotent: records are upserted by id.

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
