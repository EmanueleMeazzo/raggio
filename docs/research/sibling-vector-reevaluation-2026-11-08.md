# Sibling-vector reuse: current-main reevaluation

Date: 2026-11-08. Result: a fresh performance case for ranked sibling expansion;
the candidate remains separate from main for review.

## What this establishes

The current-main port reduces CPU on eligible ranked expansion with repeated
document hits. The most convincing Wikipedia case at eight clients requests k=10
within a document: paired median p50 falls 7.5%, server CPU per request 12.6%, and
throughput rises 8.8%. This case clears both its matched A/A noise envelope and
paired confidence intervals. The gains are specific to ranked expansion.

The old ranked-summary control slowdown did not reproduce in the controlled
comparison. This does not identify the cause of the old result. Ordinary Sofia
queries still do not use this feature; their controls supply regression evidence,
not a new benefit claim or proof of equivalence.

## Frozen sources and environment

- Baseline main: `37c686eb1c1ee89f31f91b71d133b25a772f7011`, raggio 0.4.0.
- Historical candidate retained at `614c4b87f6411a449fe9ab8bc10e0cc666b7233c`.
- Runtime image source: `f9a6561cee158c51a3e50660f9dff2c0fc3dbc6c`; the port keeps
  current scoped batching, metadata FTS, parser and lifecycle fixes.
- HTTP harness correction: `81b7490`, using three frozen baseline methods and
  counterbalanced source selection within one HTTP process, outside the clocks.
- GN100/DGX Spark, Linux arm64, CPython 3.12.15, SQLite 3.53.1, NumPy 2.5.3,
  turbovec 1.1.2, FastAPI 0.142.2 and the native BM25 extension.
- Server: 3 CPU quota, 4 GiB limit, fast cores 15–17. Fixed external client: core 19.
  `OPENBLAS_NUM_THREADS=1`. Requests carry precomputed vectors; embedding service
  costs are excluded. HTTP uses host-local networking and local dataset storage.

The primary corpus is the first 5,000 articles of the cached Wikipedia benchmark:
32,967 chunks and 5,000 summaries, dimension 1024, Qwen3-Embedding-0.6B embeddings.
Sixteen title queries and sixteen in-document queries generate the request shapes.
Both arms use identical data and query bytes, with `fts_keys=["title"]`.
The source corpus revision and preparation parameters are in `wiki-corpus-meta.json`.

Requested k=10 does not guarantee ten available chunks: the selected document
queries return 3–10 hits (mean 6.5). Global queries return ten hits and average
74.4% of hits in repeated-document groups. These are controlled proxies for feature
usage, not evidence of a particular caller's traffic mix.

## Protocol and calibration

The [plan](sibling-vector-reevaluation-2026-11-08-plan.md) predates measurements.
The primary comparison has seven alternating paired rounds, at least two seconds
per arm, at concurrency 1 and 8. Every measured response must equal the frozen
reference bytes. CPU is read from the server process, separately from client CPU.
RSS is sampled independently; SQL and allocation traces run outside latency timings.

Initial separate-process A/A showed material same-code differences. It is retained
as a diagnostic, not the causal comparison. The primary A/A and A/B therefore use
one server process and data/index state, selecting baseline/candidate methods only
after prior requests drain. A/A median changes are close to zero, but individual
paired-round variation can be wider. The analysis conservatively requires a gain
beyond the 95th percentile of absolute A/A changes and an interval excluding zero.

## Primary Wikipedia results

Percentages are medians of paired percentage changes, not ratios of independently
computed arm medians. Negative latency/CPU values mean less time/work.

| Request shape | Clients | p50 change | Server CPU change | QPS change |
|---|---:|---:|---:|---:|
| In-document vector, k=5, ranked expansion | 1 | -0.3% | -3.4% | +1.5% |
| In-document vector, k=10, ranked expansion | 1 | -5.6% | -8.9% | +4.5% |
| Global vector, k=10, ranked expansion | 1 | -8.1% | -6.8% | +5.6% |
| Global hybrid, k=10, ranked expansion | 1 | -5.5% | -5.6% | +5.5% |
| In-document vector, k=5, ranked expansion | 8 | -5.7% | -6.6% | +5.3% |
| In-document vector, k=10, ranked expansion | 8 | -7.5% | -12.6% | +8.8% |
| Global vector, k=10, ranked expansion | 8 | -3.6% | -4.8% | +3.4% |
| Global hybrid, k=10, ranked expansion | 8 | -4.8% | -3.7% | +3.4% |
| Ranked-summary control, no reuse | 8 | +0.5% | +0.1% | +0.2% |
| Ordinary summary control | 8 | +3.0% | -0.3% | -2.4% |
| Ordinary in-document vector control | 8 | +1.9% | +0.1% | -1.5% |
| Ordinary in-document hybrid control | 8 | -1.7% | -0.6% | +1.0% |
| All-siblings control | 8 | -0.0% | +0.2% | +0.0% |
| Summary-only expansion control | 8 | -1.5% | -0.3% | +0.4% |

CPU gains clear the noise gate for each k=10 ranked case at both concurrency levels.
The in-document k=5/k=10 latency and throughput gains at eight clients
also clear it. Other endpoint latency/throughput gains have wider A/A noise, even
where their paired intervals exclude zero; do not advertise a universal speedup.
Primary unaffected-control p50, server CPU and QPS pass the declared regression gate.
The ordinary summary latency interval was wide, prompting the longer follow-up.

## Separate follow-ups

The longer Wikipedia control comparison has seven paired eight-second blocks at
eight clients. Ordinary summary p50 changes +0.26% (95% paired interval -9.11% to
+2.38%), CPU +0.85% (-0.85% to +2.68%) and QPS +0.13%. In-document vector p50
changes +0.58% (-0.74% to +2.30%), CPU -0.12% and QPS -0.50%.
These measurements support absence of a detected material loss in these runs;
they do not establish that all unaffected workloads are equivalent.
The primary summary p95/p99 point estimates also rose (+3.55%/+5.23%) with wide
intervals crossing zero. The longer repeat changes p95 +0.10% (-4.53% to +0.93%)
and p99 -1.57% (-2.87% to +0.08%); retain the uncertainty rather than declaring
tail-latency equivalence.

Resource audit found heavy unrelated host load only during the first long A/A
calibration. That recording is retained as contaminated and excluded from noise
estimation. Its clean replacement stayed below 9.3% total host CPU busy and showed
median same-code p50 change -0.71%, CPU +0.84% and QPS +1.45%. Primary A/A/A/B, the longer A/B
and the synthetic sensitivity remain separately identified recordings.

The planned 3072-dimension sensitivity uses 500 generated documents with 20 chunks
each, eight clients and seven paired rounds. The in-document k=10 case reduces
p50 by 26.9% and CPU by 37.5%; global hybrid ranked expansion reduces p50 by 19.2%
and CPU by 27.7%. Its ranked-summary control changes p50 +0.52% and CPU +0.92%,
with intervals crossing zero. This is a favorable repeated-document synthetic
case, separate from the smaller benefits on real Wikipedia. Its 10,500-record
index is below turbovec's staged-search size threshold.

## Mechanism, resources and correctness

On the first profiled Wikipedia in-document query, five hits share a document:
vector-blob reads fall from 6 to 2. On the first global query, ten hits span four
documents, and reads fall from 11 to 4. The ranked-summary control remains 11;
ordinary Sofia controls remain 1. Unsupported requests introduce no vector fetches.

Temporary Python allocation peaks are effectively unchanged on these samples:
about 0.74 MB for in-document ranked expansion and 2.18–2.20 MB for global ranked
expansion, with only small byte differences. Tracing includes the in-process
client and returned data. It measures neither all native allocations nor process
RSS. Same-process retained RSS cannot be interpreted as an independent per-arm
memory saving. The cache retains its 8 MiB float32 and 4,096-row bounds per request;
bookkeeping, transient copies and response memory are additional.

- Windows native CPython 3.12.15 full suite: **911 passed, 6 skipped**.
- Linux arm64 native full suite: **913 passed, 4 skipped**; JUnit retained.
- Candidate-specific differential checks: **32 passed**.
- Windows CPython 3.14.10t candidate and concurrency checks: **34 passed**, with
  the GIL staying disabled. No free-threaded performance gain is inferred.
- All primary and follow-up HTTP comparisons retain exact response parity.
- Four analysis tests verify rejection of incomplete runs, correct regression
  direction/tolerance, and that confidence intervals cannot override A/A noise.

Usable performance recordings contain **579,758 measured HTTP requests**. A further
27,276 requests in the contaminated calibration also passed response parity but are
excluded from performance analysis. Maximum total host CPU busy in the usable
recordings was 21.0%; the excluded calibration reached 95.2%.

## Disposition and evidence

Reopen the performance case for the public ranked-expansion feature using this
current-main candidate. Keep the old branch and its historical disposition intact.
The immediate next decision is whether the modest real-data gains and larger
repeated-document gains justify the additional request-scoped code for raggio's
supported feature set. Sofia's recorded unexpanded flow receives no claimed benefit.
Deployment-specific storage, query mix and free-threaded performance remain limits.

Raw evidence lives in `bench/sibling-reevaluation-2026-11-08/`: primary and follow-up
JSON recordings are losslessly gzip-compressed, with SHA-256 digests in
`raw-manifest.json`; summaries, gate analysis, profiles, environment facts, corpus
provenance and JUnit remain directly readable. `bench/sibling_reeval_summary.py`
reproduces the declared noise/gate analysis. Remote source, datasets and logs are
retained in the isolated GN100 directory `~/raggio-bench/sibling-reuse-reevaluation-20261108/`.
