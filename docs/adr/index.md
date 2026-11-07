# Architectural decisions and optimization history

Architectural decisions are tracked in this directory. Each ADR records the
motivation, accepted/rejected options, behavior and compatibility constraints,
and evidence available at the time. Later ADRs or addenda explicitly identify
decisions they supersede. Benchmark dates and environments matter: historical
results describe their measured revisions, rather than the latest commit.

## Decision index

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-performance-optimization-decisions.md) | Search hot paths, filtering, batching, storage/concurrency, and later performance work | Accepted, with subsequent corrections/addenda |
| [0002](0002-optional-ivf-index.md) | Optional IVF indexing per collection | Accepted; fan-out cost corrected by 0005 |
| [0003](0003-recall-rescoring.md) | Exact fp16 rescoring and two-stage full-query BM25 | Accepted; native scorer in 0004, relative fusion in 0007 |
| [0004](0004-native-bm25.md) | Native BM25/tokenizer with Python reference and bit parity | Accepted |
| [0005](0005-ivf-fanout-and-upstream-stance.md) | Parallel IVF shard searches, intersections, and upstream stance | Accepted |
| [0006](0006-free-threaded-python.md) | Free-threaded Python evaluation and optional image | Experimental |
| [0007](0007-sofia-backend-requirements.md) | Relative fusion, document filters, small-set exact search, lifecycle and NFS contracts | Accepted |
| [0008](0008-native-ingest-json.md) | Pydantic native ingest JSON parsing with compatibility fallback | Accepted |
| [0009](0009-ingest-vector-conversion.md) | Reuse validated ingest vectors until journal encoding | Accepted |
| [0010](0010-search-response-encoding.md) | Encode hydrated search hits directly; retrieval-only optimization | Accepted |
| [0011](0011-expansion-read-batching-evaluation.md) | Evaluate expansion read batching and sibling-vector reuse | Archived; not adopted |
| [0012](0012-per-search-sibling-vector-reuse.md) | Evaluate bounded per-search sibling-vector reuse | Not adopted; PR #10 closed unmerged |
| [0013](0013-scoped-search-batching.md) | Bound and coalesce warm scope-only vector searches | Accepted; retrieval baseline from 2026-11-05 |
| [0014](0014-contains-any-metadata-filter.md) | Add bounded OR of metadata substring alternatives | Accepted; retrieval capability, PR #12 |
| [0015](0015-metadata-fts-keys.md) | Opt-in metadata keys in every BM25 path, with transactional PATCH | Accepted after Sofia DEV validation; merged in PR #14 |
| [0016](0016-native-search-json.md) | Reuse a shared native JSON request parser for search | Accepted; merged in PR #13 and included in 0.3.0 DEV acceptance |

ADRs 0011–0012 archive the expansion experiments and sibling-vector reuse from
[PR #10](https://github.com/EmanueleMeazzo/raggio/pull/10), closed without merging
on 2026-11-05 because Sofia does not use ranked sibling expansion and representative
deployment benefit remains unvalidated. The branch and raw evidence are retained;
its runtime and tests are not part of main. ADR 0013 independently targets Sofia's
summary-search path.

## Current retrieval baseline

The current accepted runtime is **0.3.0**, PR #14's `2939c3c` including native search
JSON parsing from PR #13, merged into main by `2e8082b` on 2026-11-05. The user
accepted [Sofia's DEV title-retrieval and copy-integrity checks](../research/sofia-fts-keys-acceptance-2026-11-05.md)
and authorized both merges. That report records the image, exact comparison counts,
metadata keys, exclusions and `legal_knowhow` IVF settings (256 partitions,
`nprobe=32`). Future retrieval work starts from this runtime and records equivalent
data/index/cache settings. The documentation addendum changes no runtime code.

On the public 2.55M-vector arxiv benchmark, 0.3.0 (`f1b0ab3`) was re-measured
against `19c3293` on 2026-11-07 with the 2026-10 end-to-end protocol, on images
that differ only in `src/`: over six runs per arm, no search, hybrid, recall,
memory, disk, cold-start or index-build figure moved beyond the run-to-run spread,
and ingest throughput stayed within it
([re-measurement](../benchmark-arxiv-2026-11.md), raw tables in
`bench/results-arxiv-2026-11.md`). The README and the current arxiv table now show
0.3.0's medians.

### Historical performance baseline

The user accepted [PR #11](https://github.com/EmanueleMeazzo/raggio/pull/11) on
2026-11-05 after its Sofia deployment evaluation. Tested runtime `1cdae1d` is
merged into main by `01b160f`; subsequent report updates change documentation only.
The [baseline report](../research/sofia-scoped-search-baseline-2026-11-05.md) retains
the screenshot, setup, exact displayed metrics and comparison limits: about 22%
more concurrent summary QPS and 27% less CPU/query, with no claimed single-query
or controlled working-set improvement. Keep these historical benchmarks labeled
by their measured revision and settings; the 0.3.0 acceptance adds quality evidence
without a new deployment performance measurement.

## Work from the November 2026 optimization review

| Work | Implementation / decision | Documentation and evidence |
|---|---|---|
| Rust rewrite assessment | Keep the Python/native architecture; no service rewrite | [Archived assessment](../research/rust-rewrite-assessment-2026-11-04.md), measured at `da432a6`; original raw experiments retained locally in `C:/DevOps/raggio-rust-evaluation-20261104/` |
| Native ingest JSON parsing | `b6311a5`, merged by `96b55ed` in [PR #7](https://github.com/EmanueleMeazzo/raggio/pull/7) | [ADR 0008](0008-native-ingest-json.md); `bench/results-ingest-json.json` |
| Avoid ingest vector-list copies | `7b3eb14`, merged by `813b6da` in [PR #8](https://github.com/EmanueleMeazzo/raggio/pull/8) | [ADR 0009](0009-ingest-vector-conversion.md); large/small raw results under `bench/` |
| Search response encoding | `a872161`, merged by `6327693` in [PR #9](https://github.com/EmanueleMeazzo/raggio/pull/9); only the search response path changes | [ADR 0010](0010-search-response-encoding.md), endpoint parity tests and paired search results under `bench/`; [Sofia deployment follow-up](../research/sofia-retrieval-benchmark-2026-11-04.md) |
| Expansion experiments and sibling reuse | [PR #10](https://github.com/EmanueleMeazzo/raggio/pull/10), closed without merging; branch `perf/sibling-vector-reuse` retained at `614c4b8` | Archived [ADR 0011](0011-expansion-read-batching-evaluation.md) and [ADR 0012](0012-per-search-sibling-vector-reuse.md), with original results/source links and closure rationale |
| Warm scope search batching | `1cdae1d`, merged by `01b160f` in [PR #11](https://github.com/EmanueleMeazzo/raggio/pull/11); bounded independent cohorts for cached scope allowlists | [ADR 0013](0013-scoped-search-batching.md), paired benchmarks, exact parity/resource/lifecycle tests and [accepted Sofia baseline](../research/sofia-scoped-search-baseline-2026-11-05.md) |
| Alternative title/source substrings | [PR #12](https://github.com/EmanueleMeazzo/raggio/pull/12); `contains_any` with 1–256 scalar alternatives, OR within a key and AND across keys | [ADR 0014](0014-contains-any-metadata-filter.md), API grammar and title/literal/validation regression tests |
| Metadata in keyword retrieval | `2939c3c`, merged by `2e8082b` in [PR #14](https://github.com/EmanueleMeazzo/raggio/pull/14); immutable `fts_keys`, stored suffix and generated single BM25 text | [ADR 0015](0015-metadata-fts-keys.md), [local cost evaluation](../research/fts-keys-evaluation-2026-11-05.md), [accepted DEV baseline](../research/sofia-fts-keys-acceptance-2026-11-05.md), integrity/parity/lifecycle tests and raw results |
| Document retrieval evaluation and native search parsing | `a197726`, merged by `e5d42be` in [PR #13](https://github.com/EmanueleMeazzo/raggio/pull/13); shared native JSON route helper and rejected persistent vector cache | [ADR 0016](0016-native-search-json.md), [evaluation report](../research/document-retrieval-evaluation-2026-11-05.md), parser/API parity tests and raw benchmarks |

From 2026-11-04, ingestion is considered sufficiently fast for its frequency.
Further optimization work focuses on search/retrieval latency, CPU and memory,
including vector, text, hybrid, filtering and result expansion.

Future performance changes must update an ADR (or add one), link it here, and
retain the comparison protocol, raw measurements and relevant correctness gates.
Investigations that reject a change should record that result too.
