# item3b-upstream-landscape

## Summary
1. The maintainer has already written most of the IVF kernel. Branch `feat/ivf-residual` (head 1452b6e, 2026-08-21, 36 commits) adds a Rust-only `turbovec::ivf::IvfIndex`: residual cells, a centroid index scored by the same kernel, the query LUT built once per batch, cell-major batched search and margin spill (tau 0.05). His own log reports up to 17,439 QPS at recall 0.911 (500k x 1536, 8 vCPU), and 1.5x to 5x FAISS IVFPQFastScan. It is not merged: `main` has not moved since the 1.0.0 release on 2026-08-18.
2. What the branch lacks for raggio: a Python binding, caller-chosen ids, `remove`, allowlist/mask filtering, persistence, calibration and id enumeration. On 2026-09-12 the maintainer said turbovec suits "<100k vectors" today and he wants to reach "<1m". raggio's arXiv corpus is 2.55M, above that target. A third-party test (krickert, #182) did not meet their recall/speed gate.
3. Contributing works like this: open an issue first, then explicitly ask for contributor access; PRs are by invitation, and only the maintainer merges. Data points so far: about 12 weeks from issue to PyPI for #70 (he implemented it himself); PyPI releases come in bursts, and nothing has shipped since 2026-08-18. Several external proposals and access requests (#336, #463, #545 to #547) have had no reply for 2 days to 3 weeks. Plan raggio as if upstream IVF never arrives.
4. The biggest lever needs no upstream change. `_IvfIndex.search` (store.py:504-518) makes one nq=1 call per query per probed shard, all serially on the single drain thread. A laptop model of 8 queries x nprobe 16 over 8,192-row 1024-d shards (Windows, 6 CPUs) measured 76 q/s for today's serial loop, 298 with the calls spread over a thread pool, 159 with one call per shard for all queries that probe it, and 490 with both (6.5x). This likely explains why raggio-ivf's concurrent QPS (116) is below flat's (129), and it contradicts ADR 0001's "application-level paths are exhausted". Needs a DGX run to confirm.
5. Free-threading, measured on the laptop: PyPI 1.0.0 ships only cp39-abi3 wheels plus the sdist, and abi3 wheels do not load on free-threaded Python. PyO3 0.29 assumes modules don't need the GIL by default, so turbovec's plain `#[pymodule]` already qualifies. The sdist builds on CPython 3.14.7t in 57 s and imports with the GIL still disabled; a 3-run concurrent add/remove/search stress test passed. 3.13t is refused by PyO3 0.29. raggio and all its other dependencies import on 3.14t, and each has cp314t manylinux aarch64 wheels. It helps raggio's Python-side work (pure-Python threads 3.2x faster), not the kernel scan (search throughput stays flat as threads are added, see #336).
6. Recommendation, in order: (a) pooled plus shard-grouped `_IvfIndex.search` in raggio now; (b) the DGX measurements below, including the upstream branch at 2.55M x 1024 on Grace; (c) if the user agrees, two plain-English upstream issues: a small one for cp314t wheels, and a use-case-plus-data RFC on the IVF branch. Do not ask for contributor access first. Fork only if both upstream and raggio-side options fail.

## Brief
# Item 3B: turbovec upstream, how to contribute, prior art, free-threading

All local numbers come from a **Windows 11 x86-64 laptop with 6 logical CPUs** (labelled [laptop]). None were run on the DGX. All GitHub, PyPI and web access was read-only; nothing was posted.

Scratch work is in `D:\DEV\_scratch\probe-item3b\`:
- `gh\*.json`: issue and comment dumps
- `tv-branches\`: upstream branches, fetched
- `ft\`: free-threaded venvs, build logs and benchmark scripts `ftbench.py`, `ftstress.py`, `shardbench.py`, `audbench.py`, `ivfpar.py`

---
## 1. Upstream state

### 1.1 Repository facts
- **Repo:** https://github.com/RyanCodrai/turbovec. MIT licence. Created 2026-03-26. About 17.3k stars, 539 issues+PRs (up to #547). No GitHub Discussions.
- **Authorship:** the maintainer wrote 449 of the 539. Of 263 PRs, 31 were external; merged external PRs are only #3, #41, #57, #277 and #323, plus dependabot.
- **Branches** (`git ls-remote`, 2026-09-28):
  - `main` = ccab9f3 = the 1.0.0 release (2026-08-18). **Nothing has merged in the 6 weeks since.**
  - `feat/ivf-residual` = 1452b6e (2026-08-21).
  - `perf/2bit-hillclimb-2` = 03fc2a2 (2026-09-06).
- **Releases:** PyPI has 19 releases between 2026-04-13 and 2026-08-18, in bursts (0.4.0 to 0.5.1 in 2 days). Before 1.0.0 there was a 10-week gap (06-10 to 08-18). 1.0.0 was cut 2 days after #530 asked "Any plan to cut a release?".
- **1.0.0 wheels:** only `cp39-abi3` for macosx_11_0_arm64, manylinux_2_28_aarch64, manylinux_2_28_x86_64 and win_amd64, plus the sdist (https://pypi.org/pypi/turbovec/json). There are no free-threaded (cp313t/cp314t) wheels.

### 1.2 The maintainer's unmerged IVF (the key finding)
Branch `feat/ivf-residual` diff against 1.0.0:
- `turbovec/src/ivf.rs` +629
- `search.rs` +232/−49
- `lib.rs` +36: a `pub(crate) fn scan_with_luts`
- `tests/ivf.rs` +209 (6 tests)
- `examples/ivf_vs_flat.rs` (dimension hard-coded to 1536)
- `benchmarks/hillclimb/LOG_ivf.md` +497

It makes **no change to `turbovec-python`**. All 36 commits are by Ryan Codrai, dated 2026-08-20 to 08-21.

**API** (`ivf.rs`):
- `IvfIndex::new(dim, bit_width, nlist)`, `with_fit_threshold(n)` (default 40,000), `add(&[f32]) -> Vec<u64>` (ids auto-assigned by `next_id`), `fit()`
- `search(queries, k, nprobe) -> (Vec<f32>, Vec<u64>)`
- `len`, `is_fitted`, `nlist`, `cell_sizes`, `dim`, `bit_width`

**Design:**
- Cells store residuals from their centroid. Scores are rebuilt as q·c + q·r.
- Centroids come from a spherical k-means with 10 Lloyd iterations over the first `fit_threshold` rows. Until the fit, rows sit in a buffer that is scanned exhaustively. After it, new rows are assigned without any rebuild.
- Cells use one shared rotation and codebook and **no TQ+ calibration**. That is what allows the query LUT to be built once per batch (H3).
- Probe selection scores the centroids through the same kernel (a small quantized centroid index, H9), then recomputes exact q·c for the probed cells.
- Search is cell-major: each cell gets one kernel call covering its whole "audience" of queries, cells run in parallel on rayon, and the results merge with id de-duplication.
- Margin spill (H7): a vector goes into both of its top-2 cells when their scores differ by less than `SPILL_TAU = 0.05`. The tau is also read from `TV_IVF_SPILL_TAU` inside the per-vector assignment path — prototype quality.
- There is a `cell_bound` field (maximum residual norm per cell) that is maintained but not yet used for pruning.

**Missing for raggio:**
- caller-supplied u64 ids
- `remove` (the module docs promise swap-and-pop, but no method exists)
- allowlist/mask filtering
- persistence
- calibration
- id enumeration
- Python binding
- environment-variable reads and profiling code are still in hot paths

**Results** from `LOG_ivf.md` (OpenAI-1536 embeddings, 500k base, 500 queries, nlist 707 = √N, 4-bit, batched multi-threaded search, GCP c3-standard-8):

| Point | recall@10 | batch QPS |
|---|---|---|
| Baseline, nprobe 8 | 0.8328 | 3,317 |
| Baseline, nprobe 16 | 0.8820 | 2,069 |
| Baseline, nprobe 32 | 0.9254 | 1,203 |
| Baseline, nprobe 128 | 0.9600 | 353 |
| Flat scan | 0.9730 | 1,242 |
| Final (H16), nprobe 16 | 0.9112 | 17,255–17,439 |
| Final (H16), nprobe 8 | — | 25,560 |

- Probing all cells tops out at recall 0.9698.
- **Head-to-head with FAISS IVFPQFastScan** (M=1536, 4-bit, nlist 707, same machine), TQ versus FAISS:
  - nprobe 8: 0.8696 @ 17,097 vs 0.8156 @ 11,446
  - nprobe 32: 0.9394 @ 8,356 vs 0.9144 @ 5,235
  - nprobe 128: 0.9634 @ 2,707 vs 0.9448 @ 1,604
  - 2.5x to 5.2x faster at matched recall. Build time is about 160 s versus FAISS's 70 s.
- **Where the time goes at nprobe 16** (500-query batch): prep 2.9 ms, scan 20.5 ms (about 1.4x the VNNI roofline), audience 2.5 ms, coarse selection + merge 1.6 ms.
- **Refuted or neutral:** register tiling, nlist 1414/354/500, 16-query batches (QBS16). √N remained the best nlist.
- **Single-query latency** was only logged at baseline: 1.17 to 4.62 ms on GCP.
- **Not measured upstream:** aarch64 Grace, 1024 dimensions, N > 800k, and the effect of cell-local deletes.

**Third-party evidence (UNVERIFIED):** krickert (#182, 2026-09-16) tested the "September 1 revision" on court-opinion embeddings. It "gave substantial speedups at lower recall, but no tested setting met our flat-recall/performance gate. Interestingly, probing all lists still left a recall gap versus flat."

### 1.3 Scope statements over time
- **#30** (2026-05-17): "A real vector DB avoids this via cluster pruning / IVF lists. We don't have those" and "we're not a vector DB". This shipped block-level mask early-exit instead.
- **#202** (2026-07-25): "if you want scale you should be thinking of something like HNSW" and "Sharding and distribution is an extremely large feature". A day later, softer: "you actually might be onto a very cool idea here".
- **#182** (2026-09-12): "I think it's well-suited to <100k vectors rn but would like to extend that to <1m ... speed increases now become about how you can avoid scanning vectors altogether. Will most likely be a case of implementing existing methods in a smart way". He then asked krickert whether there are "novel discoveries" he might be missing.
- **README:** "Online ingest ... no train step"; exhaustive scan; benchmarks at 100k.
- **#112** (pgvector HNSW comparison): labelled needs-human-decision, "scope/roadmap rather than a defect".

### 1.4 Issues and PRs relevant to raggio

| # | State | What | Maintainer response |
|---|---|---|---|
| 30 | closed | mask selectivity; "no IVF lists" | shipped 32-vector block skip (6.4x ARM / 12.7x x86 at 1% clustered selectivity) |
| 53 | closed, unmerged | external HybridIndex coarse pre-filter PR | "No attached issue. Please see the contribution guidelines." |
| 70 | closed | pg_turbovec from_parts / I/O | "currently travelling" (06-15); implemented himself in #204/#210; shipped in 1.0.0, about 12 weeks after the issue opened |
| 112 | open | pgvector HNSW at 8.8M | needs-human-decision |
| 121, 289, 319, 354, 484 | closed | GIL release and lock fixes | `py.detach` is now used on every heavy path |
| 163 | closed | release gaps, incl. "future free-threaded builds" fall back to sdist | fixed via #194 (abi3 smoke tests on 3.9 and 3.14, sdist install) |
| 182 | open | gRPC server (krickert); IVF and Pareto discussion | "big feature"; 09-12 quote above |
| 201, 203 | closed by author | large-k select, seeded threshold | not on main; lives on ai-pipestream forks |
| 202 | closed | sharding / external calibration | resolved via explicit `calibrate`, `from_parts` and getters |
| 336 | open, needs-human-decision | concurrency ceiling: one process-wide rayon pool; nq=1 saturates at 2.42x on 14 cores; p999 32x | 09-06 external fix proposal (2,233 → 11,908 searches/s at 8 threads) plus access request; **no reply** |
| 351 | open | Rust crate as a downstream dependency | derives and `try_search` fixed; MSRV 1.89 confirmed as the true x86 floor (AVX-512 intrinsics) |
| 455 | closed | per-block TQ+ calibration | implemented in PR #457 but closed unmerged; policy (#531) is that calibration only runs when the user calls it |
| 530 | closed | "cut a release?" | 1.0.0 released 2 days later |
| 541 | — | turbovec-sqlite (bryanhelmig) | — |
| 545, 546, 547 | open (gburd, 09-26) | parallel `repack`; `pub repack`; IdMapIndex parts API incl. `slot_to_id()`, which is the id enumeration ADR 0001 lacks | no reply yet; Rust-only |
| 463 | open | mutants; external access request 09-14 | no reply |

`emanuelemeazzo` has no issues or PRs in the repo.

---
## 2. Contribution process and realistic timeline

**Process** (`CONTRIBUTING.md`):
- Issue first.
- "**Request contributor access** ... say so explicitly" (line 9). PRs are by invitation only.
- "There is no size-based fast path" (line 13).
- "Only I merge to `main`" (line 17).
- One logical change per PR, `Closes #N` plus a test plan. Co-Authored-By lines are fine. The maintainer criticises AI-assisted PRs that lack design context.
- **Changelog gate** on `turbovec/src`, `turbovec-python/src`, `python/turbovec` and the manifests. Skip with the `skip-changelog` label or a `[skip changelog]` line.
- **Mutation gate** on changed lines under `turbovec/src`. Skip with `skip-mutants` or `[skip mutants]`.
- **CI:** release test matrix, a debug-profile leg, pinned clippy with an allow-list on x86_64 and aarch64 targets, integration extras at their minimum versions. `cargo fmt --check` is deliberately not run.
- **MSRV** is 1.89 (x86 AVX-512 intrinsics, #351).
- **`deny.toml`:** advisories (yanked = deny), bans, and no git dependencies. Licences are not checked.
- No CLA or DCO.

**Maintainer style:**
- Heavy Claude automation: `claude.yml`, `intake.yml`, `pr-review.yml`, tq-bot `/summary`, adversarial-review bot.
- Asks for "plain english" explanations.
- Often implements requests himself (#70, #202) rather than merging external code. #6 was credited through a maintainer-created branch.

**Timeline estimates** (derived from few data points, so UNVERIFIED as forecasts):
- **Small, well-scoped ask** (for example cp314t wheel legs): decision in 1–4 weeks; on PyPI at the next release, which given the cadence could be 2–12 weeks.
- **IVF with a Python surface:** gated on the maintainer's own IVF plans. The branch has been quiet for 5 weeks, and his stated target (<1M) is below raggio's scale. Realistically 2–6 months, possibly never.
- **External PR:** requires contributor access first. No evidence of a fast path.

---
## 3. Free-threading facts
1. **Build config** (`turbovec-python/Cargo.toml:31`): `pyo3 = { version = "0.29.0", features = ["abi3-py39"] }`. The `#[pymodule]` at `turbovec-python/src/lib.rs:2151` has no `gil_used` argument.
   - PyO3 docs (Context7, `/websites/pyo3_rs_main`): "Since PyO3 0.28, modules are assumed to be thread-safe by default." So turbovec already declares `Py_MOD_GIL_NOT_USED`.
   - The free-threaded ABI is incompatible with abi3: PyO3 ignores `abi3` and builds a version-specific wheel. The `abi3t` ABI (PEP 803) only exists for 3.15+.
2. **Binding thread-safety design:** `#[pyclass(frozen, ...)]` with `std::sync::RwLock` state and a `Mutex` numpy snapshot copied before the GIL is released (lib.rs:416, 952; locking discipline at 354–380). `py.detach` wraps every heavy call (for example lib.rs:1177, IdMap search).
3. **[laptop] Measured:**
   - CPython 3.13.1t: the build fails with "PyO3 does not support the free-threaded build of CPython versions below 3.14". **3.14t is the only free-threaded target.**
   - CPython 3.14.7t: `uv pip install turbovec==1.0.0 --no-binary turbovec` produced `turbovec-1.0.0-cp314-cp314t-win_amd64.whl` in 57 s with Rust 1.98.1. After import, `sys._is_gil_enabled()` is False, with RuntimeWarnings promoted to errors.
   - Concurrent stress test (4 writers doing add_with_ids/remove, 8 readers searching, 3 runs): final length correct, no errors. This is a smoke test, not a proof.
4. **[laptop] raggio on 3.14t:** fastapi, pydantic-core 2.46.5, uvicorn[standard], httptools, watchfiles, websockets, numpy, sqlite3, `raggio.store` and `raggio.app` all import with the GIL still disabled (`PYTHONPATH=src`, bytecode writing disabled).
   - PyPI has cp314t manylinux aarch64 wheels for uvloop 0.22.1, httptools 0.8.0, watchfiles 1.3.0, pydantic-core 2.49.0, numpy 2.5.3, websockets 17.1 and pyyaml 6.0.3.
   - **turbovec is the only dependency that would build from sdist on the DGX.** That needs Rust ≥1.89 in the image. No BLAS is needed: ci.yml:49 says the BLAS dependency was removed; the openblas install in `release-pypi.yml` is vestigial.
5. **[laptop] What free-threading buys** (100k x 1024, 4-bit, nq=1 searches, 200 per thread):

   | Build | 1 thread | 2 | 4 | 8 |
   |---|---|---|---|---|
   | 3.14t (searches/s) | 321 | 348 | 436 | 379 |
   | 3.12 GIL (searches/s) | 369 | 375 | 376 | 442 |

   - Search does not scale with Python threads because each nq=1 search above the pool threshold already fans out on the shared rayon pool (#336). The threshold is 1,024 blocks according to a #336 commenter (UNVERIFIED).
   - Pure-Python CPU work on 4 threads: 0.15 s on 3.14t versus 0.48 s on the GIL build.
   - **Conclusion:** free-threading helps raggio's Python per-request work (rescoring, fusion, glue), not the kernel scan. Kernel concurrency depends on #336.

---
## 4. Prior art: design points

| System | Partitioning | Codes / quantization | Batching and refine | Numbers (vendor unless noted) |
|---|---|---|---|---|
| FAISS IVF-FastScan — [wiki](https://github.com/facebookresearch/faiss/wiki/Fast-accumulation-of-PQ-and-AQ-codes-(FastScan)), [IVFPQFastScan](https://faiss.ai/cpp_api/struct/structfaiss_1_1IndexIVFPQFastScan.html) | IVF, residual PQ with an "IVF bias term" | 4-bit PQ, LUT in SIMD registers, blocks of 32 codes | multi-query kernel ("a single scan through the codes of a list can compute several distance tables"); `IndexRefine`/`RFlat` with k_factor | turbovec log: TQ-IVF 1.5x to 5x FAISS at matched recall, 500k x 1536 |
| André et al.: PQ Fast Scan (VLDB'15), Quick ADC (ICMR'17), [Quicker ADC](https://arxiv.org/abs/1812.09162) | combinable with IVF | 4-bit sub-quantizers, register LUTs, AVX-512 | — | Quick ADC 4–6x faster than ADC |
| FAISS `IndexIVFRaBitQ` / `IVFRaBitQFastScan` — [PR #4235](https://github.com/facebookresearch/faiss/pull/4235), [changelog](https://github.com/facebookresearch/faiss/blob/main/CHANGELOG.md) | IVF, per-centroid residual | RaBitQ 1-bit plus multi-bit (2–9); query bits `qb` | Refine(SQ8/Flat) | [PR #5341](https://github.com/facebookresearch/faiss/pull/5341): IVF256,RaBitQfs4 about 3 points better recall than SQ4fs but 5x slower |
| ScaNN + SOAR — [arXiv 2404.00774](https://arxiv.org/abs/2404.00774), [blog](https://research.google/blog/soar-new-algorithms-for-even-faster-vector-search-with-scann/) | partitions plus spilled secondary assignment with an orthogonality-amplified residual loss | anisotropic quantization | reorder with exact vectors | SOAR about 2x throughput for 7.7–17.3% extra memory; won Big-ANN'23 OOD and streaming tracks |
| RaBitQ — [2405.12497](https://arxiv.org/abs/2405.12497), [Extended RaBitQ 2409.09913](https://arxiv.org/abs/2409.09913) | designed for IVF | random rotation, D-bit codes with an error bound; extended multi-bit | FastScan on the most significant bit, then refine | RaBitQ authors' comparison [2604.19528](https://arxiv.org/abs/2604.19528): "TurboQuant performs worse than RaBitQ in most tested settings" |
| Milvus IVF_RABITQ — [docs](https://milvus.io/docs/ivf-rabitq.md), [blog](https://milvus.io/blog/turboquant-rabitq-vector-database-cost.md) | IVF (FAISS factory `RR,IVF,RaBitQ,Refine(...)`) | 1-bit, SQ1–8 queries | refine SQ8/FP16 | 10M x 768: IVF_FLAT 236 QPS @0.952; IVF_RABITQ+SQ8 refine 864 QPS @0.947; recall about 0.76 without refine |
| LanceDB — [vector index docs](https://docs.lancedb.com/indexing/vector-index), [RaBitQ blog](https://www.lancedb.com/blog/feature-rabitq-quantization) | IVF_PQ (default), IVF_SQ, IVF_RQ, IVF_HNSW_* | RaBitQ 1/2/4/8 bits | `nprobes` default 20 (JS SDK), `refine_factor`; [lance #7640](https://github.com/lance-format/lance/pull/7640) (merged 2026-09-12): shared partition scans for batched queries, about 2.5x | [#9280](https://github.com/lance-format/lance/issues/9280): IVF_RQ recall collapses above about 1,500 dims from u16 FastScan accumulator overflow |
| Qdrant — [1.19 blog](https://qdrant.tech/blog/qdrant-1.19.x/), [TurboQuant](https://qdrant.tech/articles/turboquant-quantization/) | HNSW only, no IVF | TurboQuant 4/2/1.5/1-bit since 1.18; Turbo4 storage datatype in 1.19 | rescore + oversampling | 1.19.1: "Rework and batch 4-bit TurboQuant SIMD" |
| Weaviate — [RQ](https://docs.weaviate.io/weaviate/configuration/compression/rq-compression), [HFresh](https://weaviate.io/blog/weaviate-1-38-release) | HNSW; HFresh (SPFresh-style postings, HNSW over centroids, replicas = spill) GA in 1.38 | RQ 8/4/1-bit (RaBitQ-inspired) | `rescoreLimit` (20 for rq-4) | HFresh trades lower peak QPS for memory |

**Design points distilled:**
1. **List-major storage plus query batching by list** (FAISS multi-query, turbovec H3/cell-major, Lance #7640) is the consensus throughput lever.
2. **Residual versus non-residual codes:** residual raises precision per bit and needs a per-cell q·c offset (FAISS bias term, RaBitQ, turbovec branch).
3. **Global versus per-list rotation/calibration:** a global rotation is what enables one LUT per query. Per-list calibration forces per-(query, list) LUTs; raggio's shards pay about 89 µs per query-cell prep at 1536-d according to upstream H3.
4. **Spill/SOAR:** buys 3–4pp recall at small nprobe for about 13% QPS (turbovec H7) or 8–17% memory (SOAR).
5. **Refine/rescore** with higher-precision vectors is universal. raggio already does this (ADR 0003).
6. **nlist/nprobe defaults:** nlist ≈ √N is the upstream optimum; LanceDB `nprobes` default 20; raggio uses N/8192 and nprobe 16.

---
## 5. [laptop] raggio-side IVF measurements (the actionable finding)
Current code:
- `src/raggio/store.py:504-518` loops `for qi ... for j in probe: sh.search(q[1 row], k=min(k,len(sh)), allowlist=...)`.
- `_drain_scans` runs a whole batch in one `asyncio.to_thread` call (store.py:1224) and only widens nprobe (1221–1223).
- Shards of about 8,192 rows are 256 blocks, below the pool threshold, so each nq=1 call is effectively single-threaded.

**Per-(query, shard) wall cost** on an 8,192 x 1024 4-bit IdMapIndex shard, by queries per call (`audbench.py`):

| queries per call | 1 | 2 | 4 | 8 | 16 | 64 |
|---|---|---|---|---|---|---|
| ms per (query, shard) | 0.891 | 0.485 | 0.294 | 0.282 | 0.127 | 0.077 |

**Batch of 8 queries, nprobe 16, 48 shards, k=50** (random probes, mean audience 2.67; `ivfpar.py`):

| Execution | q/s |
|---|---|
| Serial nq=1 calls (today) | 76 |
| Thread-pooled nq=1 calls (6 workers; GIL released in search) | 298 |
| Audience-batched, serial | 159 |
| **Audience-batched + pooled** | **490 (6.5x)** |

- This is a consistent explanation for raggio-ivf concurrent QPS 116 being below flat's 129 on the DGX: one thread executes about 128 small single-core calls per 8-query batch, while flat runs one nq=8 call across all 20 cores.
- ADR 0001:49 ("Application-level paths are exhausted") did not test this.
- A list-major single `TurboQuantIndex` with a contiguous bool mask (16 of 32 lists) cost 6.19 ms versus 9.66 ms flat per nq=1 call. Only one mask is allowed per batch, so it cannot serve batches with different probes. Rejected as an interim layout.

---
## 6. Options

| Option | Gain | Cost / risk |
|---|---|---|
| **A. raggio: pooled + audience-batched `_IvfIndex.search`** (recommended now) | [laptop] 6.5x batch throughput; expect more on 20 cores | Small change inside store.py; thread pool may contend with rayon when shards exceed 1,024 blocks. Keep results identical, including allowlist and tiny-allowlist owner paths. |
| B. Upstream RFC on `feat/ivf-residual` (Python surface) | Kernel-level IVF (hoisted LUT, multi-query cells) | Months; maintainer targets <1M; binding needs ids, remove, filter, persistence, which are major design work for him |
| C. Upstream small issue: cp314t wheels | Removes the Rust toolchain from a free-threaded raggio image | Low effort for him; benefit only on the Python side |
| D. Fork: `feat/ivf-residual` + own PyO3 binding as a vendored aarch64 wheel | Kernel IVF now | Must fork the crate itself, because `scan_with_luts` and `prepare_query_luts` are crate-internal; own persistence format; ongoing rebase cost. MIT permits it. |
| E. Residual encoding per shard in raggio (store v − c_j, add q·c_j at merge) | Possibly more recall per probe | Unmeasured; rescore already removes quantization loss (ADR 0003); krickert saw a gap versus flat |
| F. Free-threaded 3.14t raggio image (sdist-built turbovec) | Python-side parallelism | Build stage needs rustup; deployment change; kernel concurrency is still bounded by #336 |

**Recommended sequence:** A, then the DGX protocol (§9), then decide B/C (a posting decision), and consider D only if A plus upstream both fail.

---
## 7. RFC outline (draft only — do not post)
**Issue 1 (small, first). Title:** "Publish free-threaded (cp314t) wheels"
- **Plain-English summary:** abi3 wheels can't load on free-threaded 3.14t, so pip falls back to the sdist and needs Rust.
- **Evidence:** PyO3 0.29 treats modules as GIL-free by default; the 1.0.0 sdist builds on 3.14.7t and imports without re-enabling the GIL; the concurrent stress test passes.
- **Ask:** `-i python3.14t` legs in `release-pypi.yml` for manylinux x86_64/aarch64, macOS arm64 and Windows; a smoke test asserting `not sys._is_gil_enabled()` after import; optionally a threaded stress test in the Python tests.
- **Cross-reference:** #163 anticipated this.
- **Offer:** the stress test and CI job text. Do not request access unless asked.

**Issue 2 (RFC). Title:** "IVF (feat/ivf-residual) for 1–3M-vector Python users: use case, aarch64 data, and the surface we'd need"
- **Summary, 3 plain lines:** raggio (self-hosted RAG) at 0.5–2.55M x 1024 uses IdMapIndex. Flat vector latency is 23 ms p50 at 2.55M on a 20-core Grace. Our Python IVF over IdMapIndex shards hit the per-call costs your LOG_ivf H1/H3 describe.
- **Data:** raggio flat vs Python-IVF vs Weaviate table (bench/results-arxiv.md); the pooled/audience numbers; results of `ivf_vs_flat` run on the DGX at 2.55M x 1024 (§9).
- **What we'd need:**
  - `add_with_ids(u64)` and `remove(id)` (cell-local swap-and-pop)
  - `search(queries, k, nprobe, allowlist=None)`, batched with the GIL released
  - persistence, id enumeration, explicit `fit(sample)`
- **Nice to have:** configurable spill tau, `cell_sizes`.
- **Questions:**
  - Is IVF headed for main?
  - Is persistence planned (v7 extension or a separate file)?
  - How will cell-local deletes interact with spill copies?
  - Would you prefer to write the Python binding yourself, or accept one once the Rust API settles?
- **Non-goals:** HNSW, distribution, sharding.
- **Tone:** lead with plain English and measured data; disclose AI assistance.

**Optional comments:** +1 with the raggio use case on #336 (concurrency) and #547 (a Python `slot_to_id` would replace the k=n ghost-reconcile probe).

---
## 8. Proposed ordered tasks

**T1. Shard-major + pooled `_IvfIndex.search`**
- **Files:** `src/raggio/store.py` 485–530 (plus an executor owned by `_IvfIndex` or the Collection, sized from `os.cpu_count()`); `tests/test_ivf_index.py`.
- **Tests first:**
  - new search is identical (ids and scores) to the old per-query loop on random data for nq ∈ {1, 8, 33}
  - allowlist ≤128 still probes only owner shards; allowlist >128 is intersected per shard
  - empty shards; k > shard length; padding keeps id 0 / −inf
  - `nprobe` override
  - `_scan_batched` widest-nprobe behaviour is unchanged
- **Acceptance:** all existing IVF tests green; [laptop] `ivfpar.py`-style microbench at least 3x over serial.

**T2. DGX measurement of T1** (protocol §9, step 2).
- **Acceptance:** raggio-ivf concurrent QPS above flat's 129, at recall no worse than the ADR 0002/0003 values. Update ADR 0001/0002 with the result.

**T3. DGX measurement of upstream `feat/ivf-residual`** at 2.55M x 1024 on Grace (§9, step 3). Data only; no raggio code change.
- **Acceptance:** a recall/QPS/single-ms table for nprobe {8, 16, 32, 64} and nlist {√N, N/8192}.

**T4. (Posting decision) Draft Issues 1 and 2** from §7 with T2/T3 data; the user posts, if they choose to.

**T5. (Optional) Free-threaded image experiment:** 3.14t container with turbovec built from sdist.
- **Test:** the server starts with the GIL disabled; the concurrent bench is re-run.
- **Acceptance:** concurrent hybrid/vector QPS change reported. Keep it only if it wins by more than 1.2x.

**T6. (Contingent) Fork plan (option D)** only if T2 falls short and the upstream answer is no or none after about 8 weeks.

---
## 9. DGX measurement protocol (human-run)
1. **Baseline:** `cd ~/raggio && git log -1`. Confirm `bench/corpus/embed-vecs.npy` and `bench/gt-*-42-d1024.npz` exist (`ls bench/gt-*`).
2. **T1 before/after:** run the existing `bench/bench.py` arXiv configuration that produced `bench/results-arxiv.md`, with the IVF column (same flags as that run, concurrency 8, seed 42), on main and on the T1 branch. Report vector p50/p99, concurrent QPS and recall@10 for raggio-ivf.
   - Also run `RAYON_NUM_THREADS=20` and the default. Record `nproc` and `lscpu`.
3. **Upstream IVF kernel at 1024-d**, outside the repo:
   - `git clone https://github.com/RyanCodrai/turbovec ~/scratch/tv && cd ~/scratch/tv && git checkout feat/ivf-residual`
   - Locally edit `turbovec/examples/ivf_vs_flat.rs` to read `dim` from the environment instead of `1536`.
   - Export, from the raggio venv at the repo root: `base_N.f32` = L2-normalised float32 ingest rows (bench's `ingest_rows` for seed 42); `queries.f32` = the 500 held-out queries, normalised; `gt_N.i64` = the gt rows remapped to positions within base (`np.searchsorted(ingest_rows, gt)`), as int64.
   - Run `cargo run --release --example ivf_vs_flat -- /path/to/files 2549119`, with `TV_IVF_NLIST` ∈ {1596, 311} and `TV_IVF_PROFILE=1`, logged with `/usr/bin/time -v`.
   - Needs Rust ≥1.89 (`rustup`); no BLAS. Memory: base is about 10.4 GB f32; the process peak is probably about 2x.
4. **Free-threaded check:** `uv python install 3.14t` into a scratch dir; `uv venv -p 3.14t`; `uv pip install numpy turbovec==1.0.0 --no-binary turbovec`; run `ftstress.py` and `ftbench.py` copied from `D:\DEV\_scratch\probe-item3b\ft\`.

---
## 10. Risks
- The upstream IVF may be abandoned or redesigned: no Python surface, cells uncalibrated (conflicts with raggio's calibrate-early policy), and third-party recall concerns.
- The maintainer's stated scale target (<1M) is below raggio's 2.55M.
- Fork maintenance: the crate is about 21.8k lines, IVF would be a new on-disk format, and the MSRV/clippy gates would have to be reproduced.
- T1 thread pool versus the rayon pool: over-subscription if shards grow past 1,024 blocks (for example nlist small relative to N). Measure, and cap pool size.
- Free-threaded: binding thread-safety was only smoke-tested; FT still limited by #336; 3.13t unsupported.
- Laptop numbers may not transfer to 20-core Grace with LPDDR5x unified memory.

## User decisions
- Engage upstream at all, and in what order? Recommended: yes, after T1-T3 data exists. First post the small cp314t-wheel issue, then the IVF use-case RFC with DGX aarch64 numbers. Do not ask for contributor access up front; offer it only if the maintainer invites. The user posts under their own account and discloses AI assistance, since CONTRIBUTING criticises AI-assisted PRs that lack design context.
- Amend ADR 0001's standing constraint ('kernel changes out of scope'; 'application-level paths are exhausted')? Recommended: amend it to 'raggio will not carry kernel code; upstream proposals allowed'. Also record that pooled plus audience-batched shard search is an application-level path that was not previously tried.
- Accept a forked, vendored turbovec wheel (feat/ivf-residual plus our own binding)? Recommended: no for now. Revisit only if T1 misses the flat-QPS bar on the DGX AND upstream gives no path within about 8 weeks. Note that a fork must patch the crate itself, not just add a downstream crate.
- Target free-threaded CPython 3.14t for the raggio container? Recommended: experiment only (T5) after T1. It helps Python-side work, not the kernel scan, and needs Rust in the image build until upstream ships cp314t wheels.
- Scope of T1 relative to item 3 part A (the raggio-side IVF work planned elsewhere)? Recommended: merge T1 into part A's plan as its first task, because it is the highest-measured lever and needs no upstream change.

## Unverified
- The 6.5x throughput gain from pooled plus audience-batched _IvfIndex.search was measured only on the 6-CPU Windows laptop with random probes (mean audience 2.67). On the DGX with nlist of about 311, mean audience would be about 1.3, so most of the gain must come from pooling. Not measured on the DGX.
- That raggio-ivf concurrent QPS (116) below flat (129) is caused by the single-threaded serial drain of nq=1 shard calls: a consistent model, not profiled on the DGX.
- The #336 nq=1 pooled threshold of 1,024 blocks comes from an external commenter, not verified in code.
- The upstream feat/ivf-residual performance on aarch64 Grace, at 1024 dimensions, and at N=2.55M: the upstream log covers only x86 GCP / M3 at 500k-800k x 1536.
- Whether the maintainer intends to merge feat/ivf-residual: there is no statement. The branch has been idle since 2026-08-21, and the 09-12 #182 comment suggests exploration toward '<1m'.
- krickert's claim that the residual IVF missed their recall gate even with all lists probed: their linked report was not read.
- Timeline estimates (issue decision 1-4 weeks, IVF with Python 2-6 months) are extrapolated from a handful of past issues (#70, #202, #530) and the current lack of replies on #336, #463 and #545-547.
- Thread safety of the turbovec binding under free-threaded Python beyond a 3-run add/remove/search smoke test; only the Windows x86-64 3.14t build was performed, not a Linux aarch64 sdist build.
- Vendor-reported prior-art numbers (Milvus IVF_RABITQ 864 QPS at 0.947 on 10M x 768, Lance #7640 about 2.5x, Qdrant 1.19.1 speedups, SOAR about 2x) were not reproduced.
- The TurboQuant-vs-RaBitQ conclusions (arXiv 2604.19528) were read at abstract level only.
