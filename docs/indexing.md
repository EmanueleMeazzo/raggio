# Vector indexing (optional)

Every collection starts — and can happily stay — **index-free**: vector search is a
SIMD brute-force scan over the 4-bit quantized codes, and up to roughly a million
vectors that scan is as fast as (or faster than) a graph index while using a fraction
of the memory. See the [benchmarks](benchmark.md).

For collections that grow into the multi-million range, raggio offers an **optional
IVF index** — an additional object you attach to a collection and can remove again at
any time. It is ScaNN-style coarse partitioning: k-means splits the collection into
`nlist` shards, each a quantized turbovec index of its own; a query scores the query
against the centroids and scans only the `nprobe` closest shards.

## When to add an index

**Don't index below ~1M vectors.** Measured on the real 553k × 1536 benchmark corpus,
every IVF configuration that preserved recall@10 ≥ 0.95 was at best 1.6x faster than
the flat scan — and most were slower, because each probed shard carries a fixed
~0.4 ms search cost (`bench/ivf_probe.py`, ADR 0001). The flat scan also batches
concurrent queries into one kernel pass; the index cannot, so high-QPS workloads lose
more.

**Consider indexing above ~2M vectors.** The flat scan is linear in collection size
while the indexed scan is roughly `nprobe / nlist` of it. On the same corpus scaled to
2.2M vectors (jittered near-duplicates), measured per-query scan latency:

| Configuration | p50 | recall@10 | speedup |
|---|---|---|---|
| flat scan | 18.1 ms | 0.985 | 1x |
| IVF nlist=256, nprobe=16 (default) | 5.1 ms | 0.980 | 3.5x |
| IVF nlist=256, nprobe=8 | 2.7 ms | 0.974 | 6.8x |
| IVF nlist=256, nprobe=4 | 1.3 ms | 0.957 | 13.6x |

On the harder unscaled real corpus the recall cost of `nprobe=16` was about 1 point
(0.951 vs 0.961 flat). Treat these as the shape of the curve, not a guarantee — the
`nprobe` search parameter lets you trade recall against speed per query.

For calibration, the full served picture at 552k (REST round-trip, DGX Spark,
`nlist=256 nprobe=16` — `bench/results-vec-regression.md` vs
`bench/results-ivf-553k.md`): serial p50 improves 9.3 → 6.8 ms and serial QPS
101 → 144, but recall@10 drops 0.976 → 0.960, concurrent QPS stays flat (~300, the
index gives up the flat scan's query micro-batching), and large filtered searches
get slower (8.0 → 15.8 ms p50 — big allowlists are intersected per probed shard).
Attach took 40 s, detach 24 s, both online.

Rules of thumb:

- ≲ 1M vectors, or recall matters more than milliseconds → **no index**.
- ≳ 2M vectors in a larger container, single-query latency dominates → **attach one**.
- Heavy concurrent or filtered load on ≲ 1M vectors → no index.

## Costs

- **Memory**: ~0.5–1 MB fixed per shard (`bench/shard_mem_probe.py`) on top of the
  same quantized codes — e.g. `nlist=256` on 2.2M × 1536-d ≈ 1.14x the flat index.
- **Disk**: attach/detach rebuilds need the original vectors, so raggio retains an
  fp16 copy of every ingested vector in `meta.db` (`dim × 2` bytes per record —
  3 KB/record at 1536 dims). This is always on, disk-only, and also what makes the
  index fully reversible.
- **Rebuild**: attaching/removing streams the collection through a rebuild — tens of
  seconds per million vectors, run as a background job. Searches keep serving from the
  old representation until the swap; expect transiently ~2x index RAM plus ~0.4 GB
  for the k-means training sample at 1536 dims. If the container lacks that headroom
  the job fails with a clear error (instead of an OOM kill) — raise the memory limit
  and re-attach.

## API

```bash
# attach (or rebuild) — 202 + job id; poll /jobs/{id}
curl -X POST :8000/collections/mail/index -H "x-api-key: $KEY" --json '{}'
# optional parameters: {"nlist": 256, "nprobe": 16}
# defaults: nlist ≈ rows/8192 rounded to a power of two (16..1024), nprobe 16

# check
curl :8000/collections/mail -H "x-api-key: $KEY"
# -> ..., "index": {"type": "ivf", "nlist": 256, "nprobe": 16}

# per-query recall/speed knob (ignored on unindexed collections)
curl :8000/collections/mail/search -H "x-api-key: $KEY" \
  --json '{"query": {"vector": [...]}, "k": 10, "nprobe": 32}'

# retune the default nprobe without rebuilding (indexed collection, nprobe only)
curl -X POST :8000/collections/mail/index -H "x-api-key: $KEY" --json '{"nprobe": 8}'

# remove — rebuilds the flat index from retained vectors, 202 + job id
curl -X DELETE :8000/collections/mail/index -H "x-api-key: $KEY"
```

Attach and detach run through the same durable job queue as ingest: they survive
crashes (the job replays), are idempotent, and `GET /collections/{name}/jobs/{job_id}`
reports progress. Ingest, deletes, and upserts keep working while an index is
attached — new vectors are routed to their nearest shard.

## Caveats

- **Filtered search**: metadata filters and scopes work unchanged. Small result sets
  (≤128 candidates) probe exactly the shards that own them — no recall loss. Larger
  filtered sets are subject to the same `nprobe` recall trade as plain searches.
- **Centroids are fixed at attach time.** After heavy churn (say, the collection
  doubles or its content distribution shifts), re-POST the index to retrain.
- **Legacy rows**: collections ingested before vector retention lack the fp16 copy.
  Attach backfills them by re-embedding text through the collection's embedding
  endpoint — correct only if that endpoint produced the original vectors. Vector-only
  legacy rows make the attach job fail with a count; re-ingest them first.
