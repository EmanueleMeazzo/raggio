# ADR 0017 — turbovec 1.1.2: staged 4-bit search and the v8 index format

Status: accepted; raggio 0.4.0 · Date: 2026-11-08

Builds on [ADR 0003](0003-recall-rescoring.md) (fp16 rescore of an over-fetched
candidate set) and [ADR 0005](0005-ivf-fanout-and-upstream-stance.md) (IVF shards).
Measured with the [arxiv](../benchmark-arxiv.md) and
[document](../benchmark-documents.md) benchmarks; raw tables in
`bench/results-turbovec-2026-11.md`.

## Context

raggio 0.3.0 locked turbovec 1.0.0. The 2026-10-03 comparison of turbovec's then
unreleased main against 1.0.0 found no change beyond noise. turbovec 1.1.0 to 1.1.2
have since been released. They bring two changes that matter here:

- **Staged 4-bit search.** It makes a first pass over each vector's sign bits, ranks
  the shortlist on the lower bit planes, then rescores it exactly. It replaces the
  whole-index scan when all of the following hold (`pack.rs`: `planes_wanted`,
  `use_vector_major`):
  - the index holds at least 32,768 vectors (`PLANES_MIN_VECTORS`);
  - the dimension is a multiple of 32;
  - the CPU is arm64 with `dotprod`, or x86-64 with AVX-512 F, BW, VBMI and VNNI.

  `TURBOVEC_4BIT_PLANES=0`, read once per process, keeps the whole-index scan.
  1.1.1 and 1.1.2 are fix releases: masked search, and recall on tightly clustered
  corpora.
- **The v8 file container (`TV8\0`).** 1.1 reads v7 files, and a `sync()` into a v7
  file rewrites it as v8. turbovec 1.0.0 cannot read v8.

1.1.2 publishes `cp39-abi3` wheels for manylinux x86-64 and aarch64, macOS arm64 and
Windows, plus an sdist that needs Rust 1.89, as 1.0.0 did. There is still no cp314t
wheel, so the [3.14t image](../free-threaded-python.md) keeps compiling turbovec
from source.

## Decision

- Lock turbovec 1.1.2 with a floor of `>=1.1.2`, and release raggio as 0.4.0, a
  minor version because the on-disk index format changes one way.
- No raggio code changes. The staged search stays on by default. Search, the fp16
  rescore, IVF, and the id enumeration behind ghost reconciliation and IVF
  allowlist intersections (`search(probe, k=len(index))`) are unchanged.
- **Upgrade notes** ([storage](../storage.md#upgrading)):
  - Back up `DATA_DIR` before upgrading if a downgrade must remain possible.
  - raggio syncs a flat collection's index file on every offload and shutdown, so
    each collection becomes v8 the first time 0.4.0 loads it, written to or not.
    IVF shard files become v8 as they are written.
  - Rollback options: restore the backup; or convert each file back with turbovec's
    `convert` example (`cargo run --example convert -- <in> <out> v7`, from the 1.1.2
    sources); or export through `GET /collections/{name}/documents?include_vector=true`
    and re-ingest.

## Gates

Planned before the run; the recall gate had no numeric threshold.

1. **Compatibility.** The full test suite on CPython 3.12 (781 passed, 74 skipped
   on this laptop). `tests/test_turbovec_upgrade.py` adds:
   - a `DATA_DIR` written by raggio 0.3.0 under turbovec 1.0.0
     (`tests/fixtures/datadir-turbovec-1.0.0`, from
     `tests/fixtures/make_turbovec_v7_datadir.py`). It returns the recorded results
     for eight searches: vector, hybrid, `doc_id` filter and summary scope. Its index
     is `TV8` after the first stop and gives the same results again after reopening;
   - after a write, the directory reloads with the right counts and the new record;
   - enumeration returns every id of a 40,000-vector index, with and without an
     allowlist. CI's arm64 runner and the DGX Spark take the staged path here; an
     x86 runner without AVX-512 VBMI takes the whole-index scan.
2. **Recall on real corpora decides.** It is read with the usual rule: a difference
   is real when it exceeds both arms' bands. The figures:
   - arxiv recall@10 against exact float32 search;
   - the document benchmark's own-target hit rates and recall@k at k=10, 100 and 1000.

   Recall was made the deciding gate because a synthetic probe on the DGX Spark
   found the staged search 1.9× faster than the whole-index scan but sharing only
   90% of its top 10 before raggio's rescore.
3. **Performance.** No row worse beyond both arms' bands, except as recorded below.

## Measurement

DGX Spark, 2026-11-08, 00:39Z–03:17Z, one boot.

- **Arms.**
  - A: `7947eba` (0.3.0, turbovec 1.0.0).
  - B: `5581e4d` (0.4.0, turbovec 1.1.2).

  Both images were built from archives. Their installed packages are identical
  except raggio and turbovec: CPython 3.12.15, SQLite 3.53.1, numpy 2.5.3,
  raggio-native 0.1.0. raggio ran in 4 GiB containers.
- **arxiv** (2,549,119 vectors, 1024 dims). The protocol is
  [the 0.3.0 re-measurement's](../benchmark-arxiv-2026-11.md#protocol-in-brief)
  without the ingests: each arm got its own byte copy of the existing collection,
  which turbovec 1.0.0 had written, and B's copy became v8 at its first shutdown.
  Eight sessions ran: A1, A1-c16, B1, B1-c16, A2, A2-c16, B2, B2-c16. Each built
  the IVF index, measured it, detached it and measured the flat index: one discarded
  run plus three measured runs per engine, a fresh container per run, and true-cold
  starts.
- **Documents** (25,000 Wikipedia articles, 173,320 records). The protocol is
  [the document benchmark's](../benchmark-documents.md#protocol): two ingests per
  arm and four sessions (A1, B1, A2, B2) at 8 clients, with the `doc_id` filter in
  both arms. Weaviate was not re-run.
- **Host guard.** As in both protocols. All 12 sessions passed at the first attempt,
  with mean foreign CPU of 0.003–0.022 cores.
- **Statistics.** Medians of the 6 measured runs per arm. A difference counts only
  when it exceeds both arms' bands (max minus min).

## Results

### arxiv, flat index (concurrency 8 unless noted)

| Metric | A: 0.3.0 | B: 0.4.0 | B/A | Beyond both bands |
|---|---|---|---|---|
| Search p50 / p95 / p99 (ms) | 18.5 / 19.8 / 20.6 | 8.0 / 9.3 / 9.8 | 2.3× / 2.1× / 2.1× faster | yes |
| QPS serial | 54 | 124 | 2.30× | yes |
| QPS, 8 clients / 16 clients | 149 / 206 | 321 / 418 | 2.16× / 2.03× | yes |
| p95 under concurrency, 8 / 16 clients (ms) | 68.6 / 94.6 | 31.2 / 54.9 | | yes |
| Server CPU per query, 8 / 16 clients (ms) | 98.8 / 69.0 | 28.8 / 22.1 | 3.4× / 3.1× less | yes |
| Filtered p50 / p95 (ms) | 26.3 / 31.5 | 21.7 / 24.9 | | p95 only |
| Hybrid p50 (ms) / QPS serial | 47.8 / 20.6 | 45.4 / 21.7 | 1.05× | yes |
| Hybrid CPU per query, 8 / 16 clients (ms) | 339 / 478 | 226 / 402 | 1.50× / 1.19× less | yes |
| Recall@10 vs exact / hybrid text-hit@10 | 1.000 / 0.984 | 1.000 / 0.984 | | identical (recall 0.9998 in every run) |
| Memory after a restart / under load (MB) | 1502 / 1506 | 1470 / 1479 | | yes (−32 / −27 MB) |
| Disk (MB) / true-cold start to first answer (s) | 16463 / 4.8 | 16463 / 5.0 | | no |

A's flat throughput at 8 clients is bimodal, as in every earlier run: 125–128 or
170–217 QPS, at 117 or 57–80 ms of CPU per query. B's runs sit at 309–335.

### arxiv, IVF index (`nlist` 256, `nprobe` 16)

No row moved beyond the bands:

| Metric | A | B |
|---|---|---|
| Serial p50 (ms) | 6.0 | 6.2 |
| QPS, 8 / 16 clients | 389 / 431 | 401 / 443 |
| CPU per query (ms) | 9.7 | 9.7 |
| Filtered p50 (ms) | 5.7 | 5.7 |
| Hybrid p50 (ms) | 44.4 | 44.6 |
| Recall@10 | 0.995 | 0.993 |
| Build / detach (s) | 172 / 102 | 178 / 94 |

- **Why IVF did not move:** each shard is its own index of about 10,000 vectors
  (2.55M / 256), below the 32,768-vector threshold, so shards always take the
  whole-index scan. With the default `nlist` (rows / 8192, at most 1024), shards
  reach the threshold only past about 33M rows.
- **Recall:** it varies with the index each session builds. B's runs read 0.992–0.995,
  A's 0.993–0.995.
- **Build times:** the first build in each arm took 40 and 46 s, the rest 168–180 s.
  The first ran right after the volume copy, with its files in the page cache; the
  0.3.0 re-measurement's builds took 180–185 s.

### Documents (flat index, 8 clients)

Beyond both bands, all in B's favour except memory (and flat recall@100, below):

| Workload | Metric | A | B |
|---|---|---|---|
| Summary vector search | p95 / p99 (ms) | 9.5 / 10.2 | 8.0 / 8.6 |
| Summary vector search | CPU per query (ms) | 7.0 | 4.5 |
| Summary hybrid search | QPS | 220 | 242 |
| Summary hybrid search | CPU per query (ms) | 13.6 | 12.7 |
| Server memory after the workload set | MB | 474–477 | 486–491 |

The memory row is worse in B by about 14 MB. Both arms start at about 150 MB
(B 148 MB); not investigated.

Within the spread:
- every one-document cell (p50 2.9 and 3.2 ms);
- every chunk latency, throughput and CPU cell at k=10, 100 and 1000;
- every IVF cell;
- ingest: 2,338 and 2,373 records/s, two runs each.

Quality, at full precision:
- **One real difference:** flat chunk recall@100 is 0.99988 in every A run against
  0.99986 (session B1) and 0.99984 (session B2). That is 6 against 7 or 8 misses
  among 50,000 expected hits, beyond both bands. It is deterministic for a given index
  and differs between B's two sessions, each of which rebuilds the flat index at its
  detach.
- **Identical everywhere else, both arms, both index types:**
  - summary hit rates 0.996 (vector) and 0.994 (hybrid);
  - one-document hit rates 0.993 and 1.000;
  - recall@10 1.0;
  - recall@1000 0.999895;
  - IVF recall@100 0.99988.
- **arxiv:** flat recall@10 is 0.9998 in every run of both arms (one miss in 5,000;
  published as 1.000).

The vector scan is a small share of these requests. At 2.55M vectors the staged
search saved about 10.5 ms per query; scaled linearly to 173k records that is about
0.7 ms, inside the spread of chunk requests that take 12–70 ms. The rest of each
request (record hydration, the BM25 leg of hybrid search, response encoding)
is unchanged.

## Consequences

- **Flat collections.** On a capable CPU, a flat collection of 32,768 vectors or more
  searches at roughly half the latency and a third of the CPU at 2.55M vectors, with
  unchanged recall@10 and 32 MB less memory. The one quality cost measured is 1–2
  more misses per 50,000 hits at k=100 on the document corpus.
- **IVF.** Its advantage at 2.55M narrows to 6.2 against 8.0 ms serial p50 and 1.25×
  the concurrent throughput, at recall 0.993. It still answers filtered searches
  3.8× faster (5.7 against 21.7 ms). See [indexing](../indexing.md).
- **Unmeasured.** x86 hosts without AVX-512 VBMI and VNNI keep the whole-index scan;
  1.1.2 there is unmeasured. So are collections under 32,768 vectors, IVF shards,
  and the small-allowlist exact path of [ADR 0007](0007-sofia-backend-requirements.md).
  A deployment that serves through IVF with small shards should see no change.
- **One-way format.** The upgrade is one-way on disk without a backup or a
  conversion.
- **3.14t evaluation.** The [Python 3.14 evaluation](../python-3.14-reevaluation-2026-11-07.md)
  measured with turbovec 1.0.0; its comparisons predate this change.

## Rejected

- **Staying on 1.0.0.** Rejected. Flat search on the arxiv corpus is 2.3× faster at a
  third of the CPU. Compatibility passes, and the only rows worse beyond the bands are
  document memory (+14 MB) and recall@100 (above).
- **Shipping with `TURBOVEC_4BIT_PLANES=0`** (exact parity with 0.3.0's results).
  Rejected. The approximate first stage feeds the same fp16 rescore as before, over at
  least 50 candidates (ADR 0003). Its measured cost is 1–2 more misses per 50,000
  hits at k=100 on one corpus; every other quality figure is unchanged, against 2.3×
  faster flat search. Operators who need bit-identical results to 0.3.0 can set the
  variable.
- **Keeping files readable by 0.3.0.** Not possible: the Python bindings' `write`,
  `sync` and `to_bytes` take no format version and always write v8. Converting files back is left to the backup and to
  turbovec's `convert` example.
