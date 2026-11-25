# ADR 0020 — A dot-product metric that keeps float32 vectors as they were sent

Status: proposed · Date: 2026-11-25 · Branch `feat/dot-metric-float32`, not merged ·
Builds on [ADR 0003](0003-recall-rescoring.md) (rescoring) and
[ADR 0007](0007-sofia-backend-requirements.md) (small allowlists scored directly)

## Context

raggio normalises every vector to unit length at ingest, searches 4-bit (or 2-bit)
codes of the unit vectors, and re-ranks the candidates by the cosine of an fp16 copy
kept in `meta.db` (ADR 0003). `GET /documents?include_vector=true` returns that copy.
For embeddings that are unit vectors, and for a client that only wants the nearest
records, nothing is lost.

A client that computes with its stored vectors needs two things this does not give:

1. **The values it wrote.** It reads vectors back, combines them by arithmetic and
   writes the results, and it compares stored bytes with the bytes it computed. An fp16
   copy of the normalised vector is neither.
2. **The dot product as the score.** Its vectors are unit vectors only up to the
   arithmetic that made them (lengths within about 0.4 % of 1 in the case that prompted
   this), and its ranking is defined on the dot product, which counts that length. The
   cosine drops it.

Such a client today keeps its own float32 file next to raggio, asks raggio for more
candidates than it needs and re-ranks them itself. This ADR lets a collection do both
things, so the file and the second ranking can go.

This is a capability, not a speed-up. The standing priority (search latency, CPU and
memory of existing collections) sets the constraint: a collection that does not ask
for the option must store, answer and cost what it did.

## Decision

A collection has a **`metric`**, set at creation and never changed: `"cosine"` (the
default, today's behaviour) or `"dot"`.

```text
POST /collections   {"name": "kb", "dim": 1024, "metric": "dot"}
```

The catalog stores it in a new column, `metric TEXT DEFAULT 'cosine'`; every collection
from before the option reads as `cosine`. `POST /collections` and
`GET /collections/{name}` return it.

One option, not two. "Rank by dot product" and "keep float32 as sent" are requested
together and depend on each other: a dot product over fp16 copies of unit vectors is
the cosine again, and float32 storage under the cosine is a different request (a more
exact cosine) that nobody has made.

In a collection with `metric: "dot"`:

| | `cosine` (default, unchanged) | `dot` |
|---|---|---|
| Kept in `vecs.vec` | fp16 copy of the unit vector, `dim × 2` bytes | the float32 values as sent, little-endian, `dim × 4` bytes |
| Given to the quantized index | the unit vector | a unit copy (the direction); the kept values are not touched |
| Query | normalised | scored as sent; its direction is what the index is asked with |
| Candidates | the scan's top `C = min(max(floor, 2n), 2000, N)` by quantized cosine (floor 50 at 4 bits, 200 at 2 bits; with IVF, of the probed shards) | the same |
| Re-rank and `score` | cosine of the fp16 copies | **dot product of the kept float32 values with the float32 query**, summed in float64 |
| `include_vector=true` | the fp16 copy, as float32 | the float32 values that were sent |
| Zero vector | job fails (`zero vector cannot be normalized`) | job fails (`zero vector: ...`) |

Details that follow from the table:

- **What "as sent" means.** JSON numbers are doubles. Each is rounded once to the
  nearest float32, at the request, and those 32 bits are what the ingest journal, the
  `vecs` row and the read-back hold. A client that sends float32 values (for instance
  `numpy.float32(...).tolist()`) reads the same bits back, negative zeros and
  subnormals included: `tolist()` of a float32 is the exact double, and JSON carries a
  double through unchanged. A vector embedded by the server from `text` is kept as the
  float32 nearest to what the embedding endpoint returned, not normalised.
- **The score.** `score = Σ q[i] · v[i]` over the float32 values, the products and the
  sum taken in float64 (`_dot_scores`). The product of two float32 values is exact in
  float64, so only the sum rounds, near 1e-16 relative. A float32 sum (`v @ q` on
  float32 arrays) rounds near 1e-7, and its last bits depend on the BLAS kernel and on
  how rows were batched; with the float64 sum, two candidates can change places between
  platforms or request shapes only if their dot products agree to about 15 digits. A
  client's own float32 `v @ q` agrees with the returned score to float32 rounding, and
  a float64 one to about 1e-15; neither is promised bit for bit. Ties go to the lower
  internal record id (the earlier ingest), as for the cosine.
- **Lengths.** Any finite, non-zero float32 vector is accepted. The unit copies for the
  index are computed with float64 norms (`_dot_unit`): a float32 sum of squares
  overflows for lengths past about 1e19 and is zero below about 1e-23, and this metric
  has no reason to refuse those.
- **Zero and non-finite vectors are refused**, stored and query alike. The index finds
  candidates by direction and such a vector has none; keeping the record out of the
  index instead would make it a record no vector search can return. At ingest the job
  ends as `error` and writes nothing (as a zero vector does today in every collection);
  a number past the float32 range (1e39) becomes infinite and is refused the same way.
  A query is refused with 400.
- **Small allowlists.** A filter, scope or sibling list no longer than `C` skips the
  scan and scores its kept vectors directly (ADR 0007). In a `dot` collection that is
  the dot product over the whole allowlist.
- **Hybrid and text.** `text` mode does not involve vectors and is the same BM25.
  In `hybrid` the vector leg is the dot-product ranking, `max(100, k)` deep: RRF takes
  its ranks, relative fusion min-max normalises its dot products to `[0, 1]` (they may
  be negative or above 1 before that; min-max does not care). `expand.siblings_topk`
  ranks siblings by dot product in `vector` and `hybrid` mode. No fusion code changed.
- **IVF.** Attach and detach rebuild the index from the kept vectors, taking their
  directions; the kept values are not rewritten. Centroid routing uses the query's
  direction. An attach that finds a record without a kept vector re-embeds its text, as
  it does in any collection, and keeps what the endpoint returns.
- **A record without a kept vector** does not arise in a `dot` collection: the record
  and its `vecs` row are written in one transaction, and there are no rows from before
  vectors were retained. The shared code keeps what it does for such a candidate in a
  cosine collection (its quantized score stands in, and it is not dropped); in a `dot`
  collection that would be a cosine estimate among dot products, and it would mean a
  damaged `meta.db`.
- **Changing the metric** is not possible: the metric decides what the `vecs` rows
  hold. `POST /collections` on an existing name answers 409 as before, and when the
  request names another metric than the collection has, the message says so
  (`collection 'kb' already exists with metric 'cosine'; the metric of a collection
  cannot be changed (delete the collection and create it again)`). An unknown value is
  a 422. A catalog row with a metric this release does not know (written by a later
  one) is not opened: `Collection` raises before it touches the directory.

## The guarantee

What a `dot` collection guarantees:

1. `include_vector=true` returns, for each record, the float32 values that were stored
   for it, bit for bit.
2. Every vector hit's `score` is the dot product of that record's stored float32
   values with the float32 query, as defined above, and hits are ordered by it.
3. Whenever the candidates cover everything that is searched, the result is the exact
   top `k` by dot product: a collection, a filter result, a scope or a sibling list of
   at most `C` records.

What it does not guarantee: **exact search**. It is an exact re-ranking of approximate
candidates. The candidates are the `C` nearest records by quantized cosine, and a
record outside them is not returned, whatever its dot product.
`test_candidates_are_the_nearest_by_angle` pins this: a vector of length 100 at 84° to
the query has ten times the dot product of anything else and is not among the 50
candidates of a `k=10` search; `k=60`, whose candidates cover that collection, returns
it first.

How far apart the two orders can be: with `c` a record's cosine to the query and `L`
its length, its dot product is `c · L · |q|`. A record outside the candidates has a
cosine no larger than the last candidate's (as far as the quantized scan's top `C` is
the cosine's, which ADR 0003 measured and does not guarantee), so it can enter the true
top `k` only if `c_last · L_max` exceeds the `k`-th best `c · L` among the candidates.
For lengths within ±0.4 % that needs the last of the `2k` candidates to be within about
0.8 % of the `k`-th in cosine. For vectors of very different lengths the candidates say
little about the dot product, and this metric is the wrong tool: it is built for
near-unit vectors whose length must not be thrown away.

Not measured here: recall of the dot-product top `k` on a real corpus. A client that
has been re-ranking with its own float32 copies can measure it before dropping them.
The only lever on the candidates is `k` itself (at 4 bits `C = 2k` from `k = 25`, at
most 2000): ask for more and cut the list. A per-request candidate depth, which ADR 0003 names as
the escape hatch, is not part of this change.

## Compatibility

- **Default collections.** Same files, same answers. The code they run differs by
  branches on one boolean (`Collection._dot`, false at class level) in `_process_job`,
  `_search_rescored`, `_rescore_rows`, `_search_allowed`, `_fp16_vecs`, `list_records`,
  the index-rebuild decode (`_index_rows`, `_backfill_vecs`) and the search route, and
  by reading the blob width and dtype from an attribute instead of the literals
  `dim * 2` and `float16`. The rescore expression, its operands and its order are the
  same. See the measurement below.
- **`meta.db` and the index.** No schema change: `vecs.vec` was a BLOB and is one. The
  quantized index of a `dot` collection is an ordinary index of unit vectors. Nothing
  migrates.
- **Catalog.** `catalog.db` gains the `metric` column on first start (`ALTER TABLE ...
  ADD COLUMN`, as `tokenizer`, `index_config` and `fts_keys` were added).
- **API.** The two collection responses gain a `metric` key. A request without
  `metric` behaves as before.
- **Older servers ignore the option silently.** The request models do not forbid
  unknown fields, so a raggio from before this change answers 201 to
  `{"metric": "dot"}` and creates a cosine collection. A client must check that the
  response says `"metric": "dot"`; an older server's response has no `metric` key.
- **Downgrade.** A release from before this change reads the catalog (it names its
  columns) but treats every collection as cosine. On a `dot` collection its blob-width
  check fails for every record: searches return the quantized scores,
  `include_vector` returns `null`, and a new ingest writes an fp16 row into it (all
  three seen by opening a `dot` collection with main's source at `31ab17e`). Export or
  delete `dot` collections before downgrading.
- **Disk.** A `dot` collection keeps `dim × 4` bytes of vector per record in `meta.db`
  where a default one keeps `dim × 2`. What the file grows by depends on how the rows
  fill SQLite's 4 KiB pages. Measured at 20,000 × 1024-d (the A/B data below):
  84,082,688 bytes for the default collection and 94,171,136 for the `dot` one, 12 %
  more and not twice. An fp16 row of 2,048 bytes already has a 4 KiB leaf page to
  itself (two do not fit); a float32 row of 4,096 bytes goes to one overflow page plus
  a share of a leaf (20,528 against 22,991 pages for the 20,000 rows). Other
  dimensions pack differently and were not measured. Resident memory is the same: the
  kept vectors are disk-only in both, and `index.tvim` is the same size (11,614,548
  bytes in both).
- **No native code.** `native/` holds the BM25 scorer and tokenizer (ADR 0004) and is
  not involved. The new arithmetic is one numpy product over at most 2000 candidate
  rows, where the fp16 path already has one; turbovec is used as it was.

## Alternatives considered

- **A storage option separate from the metric** (`vector_storage: "float32"`). Four
  combinations to define, test and document, two of them unrequested (see Decision).
- **Keep the fp16 copy and add a float32 column or file.** Six bytes per dimension
  instead of four, two representations to keep in step, and nothing reads the fp16 one:
  the float32 values serve the re-rank, the read-back and the index rebuilds.
- **Give the index the vectors as sent** (maximum inner product from the scan itself,
  so that long vectors become candidates). Not evaluated: raggio has only ever given
  the index unit vectors, so its calibration policy and ADR 0003's recall evidence are
  for those; turbovec refuses components of 1e16 and more (seen when a test fed it the
  vectors as sent); and an index of unit vectors is the index every other collection
  has. For near-unit vectors the gain would be small.
- **Sum in float32.** One less conversion per query; scores and near-tie order would
  depend on the BLAS build (see "The score").
- **Accept zero vectors.** Would need records that are stored but not indexed; every
  count and every allowlist assumes each indexed record is in the index.
- **A candidate-depth request field.** Useful, separable, not requested.

## Correctness and evidence

`tests/test_dot_metric.py` (40 tests): the option in create and info; bit-for-bit
read-back of vectors with lengths from 0.2 to 5, of the float32 edge values and of
lengths whose float32 square over- or underflows; the bytes in `vecs.vec` for both
metrics and the unchanged schema; ranking and scores where the cosine order differs,
on four constructed vectors and on 200 random ones; the query's length; the candidate
limit; small and large filters, scopes, the coalesced scoped scan, concurrent queries
sharing one scan; ranked siblings; text mode; hybrid with RRF and relative fusion at
four weights, against the documented formulas; server-side embedding; a default
collection's stored bytes, read-back and ranking; a catalog from before the option;
restart, crash replay of a journaled job, eviction and reload; unknown metric (422),
metric change (409), unknown metric in the catalog, zero and out-of-range vectors
(job error, nothing stored), zero and out-of-range queries (400); IVF attach and
detach; an attach that re-embeds a lost vector.

The tests were checked by breaking the code on purpose (scratch, not committed): unit
copies replaced by the vectors as sent at ingest (3 tests fail) or at an index rebuild
(the IVF test fails); the score replaced by a float32 sum (9 fail), by fp16 values
(14), by a normalised query (10).

The full suite on this branch: 955 passed, 6 skipped on Windows, CPython 3.12.8, with
`raggio_native` installed (`uv run --no-sync pytest -q`; 915 passed, 6 skipped on main
before the change). The six skips need POSIX file locks or a free-threaded build. Not
run here: the Linux x86_64 and arm64 CI jobs, the 3.14t job, the container image.

The suite found one regression while this was written. `bench/ivf_fanout_probe.py`
assembles a read-only `Collection` with `Collection.__new__` and sets only what a
search touches, so attributes set in `__init__` were missing
(`test_probe_stages_smoke`). The two switches therefore default to the cosine values
at class level and `__init__` overrides them for a `dot` collection.

## Measurement: a default collection before and after

Protocol, raw tables and limits: `bench/results-dot-metric-2026-11.md` (probe
`bench/dot_metric_ab.py`, raw output `bench/results-dot-metric-2026-11.json`). Local
and in-process, on a desktop (Xeon E-2226G, Windows 11): 20,000 × 1024-d vectors in a
default collection **built by main's source** (`31ab17e`) and read by both sources.

- **Answers.** Seven request shapes (the rescored scan at k=10 and k=100, a 40-record
  allowlist, `Collection.search` for the same three, a listing with vectors), 5,700
  responses per run, six runs per source: one hash per shape over all twelve runs.
- **Cost.** Between processes the p50 of a case moves 10 to 40 % from run to run on
  that machine, so the comparison was made inside one process: main's source loaded
  twice and the branch once, each call made on all three in random order, 1,500 calls
  per case, four runs. Median per-call difference as a share of the p50: two copies of
  main, −1.58 % to +0.31 %; the branch against main, −1.64 % to +0.28 %. No case or
  run separates the branch from main. The resolution is about 1.5 %; the change itself
  is a few attribute reads and branches per call.
- **The dot path**, same vectors, against the default path: +3.5 to +5.6 % on the
  rescored scan at k=10, +13 to +17 % at k=100, +51 to +56 % on the 40-record allowlist
  (about 2 to 5 µs per candidate at 1024-d: twice the bytes, an overflow page, the
  float64 conversion). On 20,000 vectors the scan is cheap and the share large; not
  measured at scale.

Not measured: the DGX benchmarks, concurrency, IVF, hybrid, memory. No published
figure changes, and none is claimed for the new path.
