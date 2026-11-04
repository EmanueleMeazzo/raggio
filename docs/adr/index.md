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
| [0013](0013-scoped-search-batching.md) | Bound and coalesce warm scope-only vector searches | Accepted candidate implementation |

ADRs 0011–0012 cover expansion experiments and sibling-vector reuse in the separate
[draft PR #10](https://github.com/EmanueleMeazzo/raggio/pull/10). ADR 0013 branches
from main independently and targets Sofia's summary-search path.

## Work from the November 2026 optimization review

| Work | Implementation / decision | Documentation and evidence |
|---|---|---|
| Rust rewrite assessment | Keep the Python/native architecture; no service rewrite | [Archived assessment](../research/rust-rewrite-assessment-2026-11-04.md), measured at `da432a6`; original raw experiments retained locally in `C:/DevOps/raggio-rust-evaluation-20261104/` |
| Native ingest JSON parsing | `b6311a5`, merged by `96b55ed` in [PR #7](https://github.com/EmanueleMeazzo/raggio/pull/7) | [ADR 0008](0008-native-ingest-json.md); `bench/results-ingest-json.json` |
| Avoid ingest vector-list copies | `7b3eb14`, merged by `813b6da` in [PR #8](https://github.com/EmanueleMeazzo/raggio/pull/8) | [ADR 0009](0009-ingest-vector-conversion.md); large/small raw results under `bench/` |
| Search response encoding | `a872161`, merged by `6327693` in [PR #9](https://github.com/EmanueleMeazzo/raggio/pull/9); only the search response path changes | [ADR 0010](0010-search-response-encoding.md), endpoint parity tests and paired search results under `bench/`; [Sofia deployment follow-up](../research/sofia-retrieval-benchmark-2026-11-04.md) |
| Warm scope search batching | Branch `perf/scoped-search-batching`; bounded independent cohorts for cached scope allowlists | [ADR 0013](0013-scoped-search-batching.md), paired benchmarks and exact parity/resource/lifecycle tests |

From 2026-11-04, ingestion is considered sufficiently fast for its frequency.
Further optimization work focuses on search/retrieval latency, CPU and memory,
including vector, text, hybrid, filtering and result expansion.

Future performance changes must update an ADR (or add one), link it here, and
retain the comparison protocol, raw measurements and relevant correctness gates.
Investigations that reject a change should record that result too.
