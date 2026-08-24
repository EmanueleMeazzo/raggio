# ADR 0002 — Optional IVF index as an attachable per-collection object

Status: accepted · Date: 2026-08-22

## Context

ADR 0001 rejected IVF coarse partitioning **as a default** at the 553k benchmark
scale: the best recall-preserving cell was 1.63x, and ~0.4 ms fixed cost per probed
shard ate the savings. That rejection does not extend to larger collections — the flat
scan is linear in N while IVF scans ~nprobe/nlist of it — and HNSW-class alternatives
remain out of scope (no graph hooks in turbovec's public API; 1.5–2x raw-vector RAM is
hostile to the small-container goal). Requirements set by the maintainer: collections
must still be creatable **without** an index; the index is an **additional object**
clients can add to an existing collection or remove; when an index makes sense must be
clearly documented (README + docs).

## Evidence (all committed under `bench/`)

| Measurement | Source | Result |
|---|---|---|
| 553k real corpus, IVF sweep | `bench/ivf_probe.py`, `ivf-probe.log` | best ≥0.95-recall cell 1.63x → not worth it (ADR 0001) |
| 2.2M scaled corpus (4x, jittered near-dups, DGX) | `ivf-probe-s4.log` | flat 18.1 ms; nlist=256 nprobe=16 → 5.1 ms @ 0.980, nprobe=8 → 2.7 ms @ 0.974 |
| Per-shard fixed RAM | `bench/shard_mem_probe.py` | ~0.5–1 MB/shard (rotation is NOT a dense 9.4 MB d×d per shard); nlist=256 ≈ 1.14x flat at 2.2M |
| In-process RSS deltas | `ivf_probe.py` rss logging | unreliable across builds (allocator reuse) — hence the isolated probe above |

Scaled-corpus recall is flattered by near-duplicates (flat itself: 0.985 vs 0.965 on
real data); the honest recall anchor is the real 553k corpus: nprobe=16 costs ~1 pt.

## Decisions

| Decision | Rationale |
|---|---|
| Opt-in via `POST/DELETE /collections/{name}/index`; default stays flat | Rejected-as-default ≠ rejected-as-option; below ~1M the flat scan wins (and micro-batches concurrent queries, which shard probing breaks) |
| **Retain fp16 vector copies in `meta.db`** (always on) | turbovec cannot enumerate/reconstruct vectors (ADR 0001), so attach-to-existing and detach are impossible without originals. fp16 halves the cost (`dim × 2` B/record, disk-only); re-quantization error vs f32 is negligible for 4-bit codes |
| Blobs in a separate `vecs` table, not a `records` column | 3 KB/row inline blobs drag GBs through every `records` full-table scan: measured cold start 2.2 s → 25.8 s at 552k with an inline column; separate table restores lean scans (reconcile, indexed_counts, allowlists) |
| One live representation (flat **or** `ivf/` shards), catalog `index_config` decides | Keeping both would double RAM and complicate writes; a stale sibling dir after a crash is ignored and cleaned by the replayed job |
| Attach/detach are jobs on the existing ingest queue | Serializes them against ingest (worker is the only adder), makes them durable/replayable/idempotent, and gives free progress reporting |
| Build outside the write lock; ghost-diff concurrent deletes at swap time | Searches keep serving during the tens-of-seconds rebuild; deletes are the only concurrent mutation and are removed from the new shards under the lock |
| Shards calibrated from a fresh sample **before** rows are added | Calibrate-early-or-never (ADR 0001) applies cleanly because rebuilds re-encode from retained originals — no double quantization |
| Per-shard allowlist intersection via lazy per-shard id caches | turbovec raises on allowlist ids an index doesn't hold; caches (~8 B/row, built on demand, dropped per touched shard) also give small filters exact owner-shard probing — no recall loss for ≤128-id allowlists |
| `remove(id)` = contains-scan over shards | O(nlist) µs-scale calls beat maintaining a persistent id→shard map |
| nlist auto ≈ rows/8192 (power of two, 16–1024); nprobe default 16, per-query override, nprobe-only retune without rebuild | Matches the measured sweet spot; recall/speed stays a runtime knob |
| Centroids fixed after attach | Milestone-refit equivalents lose recall (cal_probe); heavy drift is handled by re-POSTing the index (full retrain) |
| Legacy rows without `vec`: backfill by re-embedding text, else fail with a count | Only correct when the configured endpoint produced the originals — documented; vector-only legacy rows require re-ingest |
| `_retry_fs` around dir/file swaps | Windows AV/indexer holds transient handles on fresh files; same class of retry turbovec's `_persist` does |
| cgroup headroom check before rebuilds | Attaching at 552k inside the 1 GiB bench container was SIGKILLed (2nd codes copy + 0.4 GB train sample); since the job replays on boot, an OOM becomes a crash loop — refuse with a job error instead |

## Rejected

- **HNSW / graph index on top** — no API surface for neighbor graphs; belongs in the
  turbovec kernel if ever (upstream feature, not ours).
- **Always-on IVF** — measurably worse at ≤553k (ADR 0001).
- **Retain f32 instead of fp16** — 2x disk for no measurable recall benefit at 4-bit.
- **Retention opt-in per collection** — would make "add an index to an existing
  collection" silently impossible for collections created without the flag.

## Verification (552k served, DGX Spark, `bench/results-*.md`)

- **No regression unindexed** (`results-vec-regression.md` vs the committed run):
  search p50 9.3 vs 9.0 ms, recall 0.976 (=), hybrid 15.3 vs 14.5 ms, ingest
  1,060 vs 1,105 vec/s (−4%, the fp16 blob writes), cold start 4.6 vs 2.2 s (larger
  meta.db; an earlier inline-blob-column draft measured 25.8 s — hence the separate
  `vecs` table).
- **Indexed at 552k, nlist=256 nprobe=16, 3 GiB container** (`results-ivf-553k.md`):
  p50 9.3 → 6.8 ms, serial QPS 101 → 144, concurrent QPS ~flat, filtered p50
  8.0 → 15.8 ms, recall 0.976 → 0.960. Attach 40 s, detach 24 s, cold restart with
  256 shards 1.9 s, all online through the job queue.
- **Headroom guard**: replaying the attach job in the 1 GiB container produced the
  clean job error ("needs ~1172 MB free, container has ~484 MB") instead of the
  SIGKILL loop it produced before the guard.

## Consequences

- `meta.db` grows by `dim × 2` bytes/record for every collection (1536-d ≈ +3 KB/rec).
- Ingest writes one more blob per record; measured impact within noise at bench scale.
- Batched concurrent scans degrade to per-query probing on indexed collections —
  documented; high-QPS small collections should stay unindexed.
- Docs: `docs/indexing.md` (when/how), README section, API reference, storage sizing.
