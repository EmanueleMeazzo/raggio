# Python 3.14 reevaluation — completed on the idle DGX

**Python 3.14.10t works and provides useful performance gains for concurrent retrieval.** The earlier decision was **Hold** because turbovec lacked a free-threaded wheel, despite measured benefits. This reevaluation supports keeping the free-threaded image available for concurrent workloads, while retaining the current deployment gate and avoiding a blanket switch of every workload.

The clean run completed on 8 November 2026 local time, from **22:53:23 to 23:34:13 UTC on 7 November**, after the maintainer rebooted GN100 and confirmed it was idle. Earlier measurements from the shared host were archived and excluded. The run covered **109,080 measured HTTP searches**, the full Linux test suites, extended concurrency stress, ingest, CPU-saturation diagnostics, and a native source-build control.

## What changed the answer

The largest repeatable gains are on document-scoped requests at eight concurrent clients. Their main work includes the Python request/response and hydration paths around cached vectors, rather than a large raw vector scan. The measurements establish gains for the complete free-threaded deployment; they do not isolate every interpreter, extension-ABI, and configuration contribution.

| Workload, concurrency 8 | Python 3.12 QPS | Standard 3.14 QPS | Free-threaded 3.14t QPS | 3.14t vs 3.12 |
|---|---:|---:|---:|---:|
| Document vector search | 976 | 889 | 1,433 | +47% |
| Document hybrid search, repeated document | 812 | 846 | 1,454 | +79% |
| Document hybrid search, rotating documents | 718 | 576 | 1,344 | +87% |
| Document chunks, k=1000 | 969 | 981 | 1,627 | +68% |
| Document search with siblings/summary expansion | 618 | 549 | 884 | +43% |
| Summary search | 570 | 516 | 737 | +29% |

These are medians of three independent server sessions in rotating order. The 3.14t throughput ranges for these six cases were above the corresponding 3.12 ranges. This is evidence of a substantial gain on this workload and hardware, not a general statistical guarantee for other deployments.

At concurrency 8, document vector p99 fell from **13.74 to 10.66 ms**, hot-document hybrid p99 from **19.99 to 11.22 ms**, and expanded search p99 from **23.96 to 13.07 ms**. Server CPU/query also fell on these cases.

Large-corpus hybrid gains were smaller but repeated, with better tails:

| arXiv workload | 3.12 QPS | 3.14t QPS | Throughput change | p99, 3.12 → 3.14t |
|---|---:|---:|---:|---:|
| Flat hybrid, c=8 | 37.26 | 39.30 | +5.5% | 459.74 → 360.98 ms |
| Flat hybrid, c=16 | 33.08 | 35.35 | +6.9% | 1,074.81 → 796.11 ms |
| IVF hybrid, c=8 | 39.33 | 42.23 | +7.4% | 455.96 → 339.21 ms |
| IVF hybrid, c=16 | 37.11 | 39.75 | +7.1% | 990.14 → 715.83 ms |

The flat c=8 hybrid QPS ranges were **37.23–37.66** on 3.12 and **38.68–40.16** on 3.14t. SQLite was **3.53.1 on every interpreter**, so a SQLite version change does not explain the comparison.

## Costs and limits

**Single-client expansion is slower on 3.14t.** Expanded document search fell from **248 to 205 QPS**, with p50 rising from **3.83 to 4.76 ms** and p99 from **5.46 to 6.38 ms**. Its throughput ranges did not overlap: 3.12 **227–257 QPS**, 3.14t **203–211 QPS**. The free-threaded image therefore benefits a concurrent server much more than an individual serial caller on this path.

**Increasing concurrency does not improve every route.** At c=16, document vector throughput was essentially level, **1,207 vs 1,222 QPS**. Document-scoped k=1000 was lower on 3.14t, **1,237 vs 1,115 QPS**, with p99 **31.68 vs 42.35 ms**. Hybrid and expansion still gained throughput. Treat concurrency 8 and 16 as distinct operating points.

**CPU-saturated blocking hand-offs remain a problem.** The standalone Linux probe pinned four X925 cores and ran four independent CPU busy-loop processes. Event ping-pong had:

| Interpreter | p50 | p95 |
|---|---:|---:|
| 3.12.15 | 2.00 ms | 2.00 ms |
| Standard 3.14.10 | 2.00 ms | 2.00 ms |
| Free-threaded 3.14.10t | 111.00 ms | 114.00 ms |

Below saturation, the free-threaded ping-pong p95 was under 0.04 ms. The two real ingest-worker tests passed under full CPU load on all interpreters, but took **0.59 / 0.60 / 1.57 seconds** of pytest time on 3.12 / 3.14 / 3.14t. That is a result for those tests, not a production ingest-rate estimate.

The Linux `asyncio.to_thread` probe did **not** reproduce a large tail penalty in this sample: p95 was about 2 ms on all builds at saturation. The separate Windows probe observed 3.14t p95 of **808 ms** for `to_thread` and **1,287 ms** for event ping-pong. CPython 3.14.10 still contains the 40-yield free-threaded mutex spin loop; these probes did not instrument individual yields, so they are not new causal proof of every pause. Reserve CPU headroom for a free-threaded deployment.

**Memory overhead was modest when filesystem cache was separated.** At flat vector c=8, anonymous memory was **1,437 / 1,442 / 1,462 MiB** for 3.12 / 3.14 / 3.14t. At IVF hybrid c=16 it was **1,537 / 1,531 / 1,599 MiB**. Total cgroup memory sometimes approached the 4 GiB cap because of charged filesystem cache, especially during post-reboot warmup. Those total-memory differences must not be described as Python heap overhead.

## Raw vector scans and the native-build control

The main IVF vector matrix measured **387 vs 511 QPS** at c=8 and **503 vs 626 QPS** at c=16 for 3.12 vs 3.14t. However, the extra matching source-build control weakens an attribution of the entire IVF gain to removing the GIL.

All four builds ran raw turbovec scans with the caller restricted to ten X925 cores, k=50. Median single-query scan times were **15.052 / 15.139 / 15.121 / 15.071 ms** for 3.12, standard 3.14 with its published wheel, standard 3.14 compiled from source, and 3.14t compiled from source. Batch medians were also within about 3%. There is no large raw-kernel acceleration in this probe.

The 100-query HTTP control used identical query samples and three rotating rounds:

| IVF vector control | Standard 3.14 wheel | Standard 3.14 source | 3.14t source |
|---|---:|---:|---:|
| c=8 QPS | 408 | 456 | 449 |
| c=16 QPS | 432 | 466 | 571 |

The source-built GIL image matched 3.14t at c=8. The main matrix's +32% IVF c=8 result is therefore an observed deployment/run comparison, **not a clean isolated GIL win**. The heterogeneous host, fresh-server thread placement, and shorter control samples limit that interpretation. The hybrid result was more consistent: IVF hybrid c=8 in this control was **37.47 / 36.94 / 41.09 QPS**, and flat hybrid was **35.56 / 35.33 / 38.80 QPS**.

Binary provenance confirmed that 3.12 and standard 3.14 used the same turbovec abi3 wheel hash, the source-built GIL binary was different, and 3.14t used its own cp314t binary. Application source hashes matched across all four images. The GIL builds used SQLite's 128-statement cache; 3.14t used the repository's existing zero-cache workaround. These are complete application-build comparisons, not a pure GIL-toggle experiment.

## Standard Python 3.14 and ingest

Standard 3.14 has mixed performance results rather than a broad retrieval speedup: flat vector search is effectively level, several document cases are lower, and IVF results include considerable placement/run variation.

It did show a useful gain in the short in-process flat ingest test:

| Ingest probe | 3.12 rows/s | Standard 3.14 rows/s | 3.14t rows/s |
|---|---:|---:|---:|
| Flat | 5,311 | 7,062 | 6,916 |
| IVF | 3,730 | 3,712 | 3,643 |

The flat 3.14 median was +33%; its range, **6,211–7,205**, exceeded 3.12's **5,236–5,458**. The 3.14t flat range was wider and overlapped the baseline. These drains took about 0.28–0.55 seconds and exclude HTTP admission: they do not establish a full-corpus ingest gain. Every run completed all 40 jobs and produced the same final data fingerprints. Event-loop stalls above the probe's 20 ms threshold were absent.

## Tests and compatibility

The complete Linux suites were rerun after the reboot with the native scorer required:

| Interpreter | Passed | Failed | Skipped |
|---|---:|---:|---:|
| 3.12.15 | 847 | 0 | 4 |
| Standard 3.14.10 | 846 | 1 | 4 |
| Free-threaded 3.14.10t | 818 | 0 | 5 |

The standard-3.14 failure is `test_check_image_refuses_python_gil_0`. The deliberately invalid `PYTHON_GIL=0` request exits with status 1 **before the application starts**, with CPython's message “Disabling the GIL is not supported by this build.” The assertion expects the application's own rejection text. The refusal works; the expected-message assertion needs updating. It was preserved during this frozen-code evaluation.

The GIL builds skip four free-threading-only tests. On 3.14t, five benchmark-harness checks skip because locked orjson does not support free-threaded Python; those checks ran on the GIL builds. The 30-second 3.14t mixed-workload and stop-under-load soak passed both tests. Four production images built and passed the Dockerfile's interpreter, extension, GIL and request-path checks.

The earlier Windows checks also supported compatibility. After the Git Bash runbook reruns, combined totals were **845 passed / 6 skipped** on 3.12, **844 passed / 1 known assertion failure / 6 skipped** on standard 3.14, and **816 passed / 7 skipped** on 3.14t. The original 23 Bash integration failures were environmental and resolved by Git Bash. Windows was not rerun merely because GN100 rebooted.

## Recommendation and the earlier Hold

**Keep 3.14t experimental under the existing policy, but regard it as a useful option for concurrent retrieval with CPU headroom.** It provides real benefits, especially for document-scoped vector/hybrid requests at c=8. A serial-heavy expansion workload, a CPU-saturated host, or a different concurrency level can lose those advantages. Standard 3.14 alone does not justify a universal performance upgrade from 3.12 on this evidence.

The historical [ADR 0006](adr/0006-free-threaded-python.md) explicitly said **Hold because B won but turbovec had no cp314t wheel**. It did not record a build incompatibility or absence of benefits. The refreshed PyPI check still found **turbovec 1.1.2** as latest, with **no cp314t wheels** for either 1.1.2 or locked 1.0.0. Source builds work, but the project's wheel gate remains unmet. No runtime default, application code, lockfile, Dockerfile or CI configuration was changed by this evaluation.

## Method and evidence

- Frozen code: raggio 0.3.0, commit `7947eba378a56225f366ff61cd716300c9305882`, in the isolated `python314-reevaluation` worktree.
- Managed CPython 3.12.15 / 3.14.10 / 3.14.10t, uv 0.13.7 build override, Rust 1.99.0. All Linux interpreters bundle SQLite 3.53.1. Locked NumPy 2.5.3, turbovec 1.0.0, FastAPI 0.142.2, Pydantic 2.13.5, raggio-native 0.1.0.
- GN100: DGX Spark, 20 heterogeneous aarch64 cores. Full HTTP runs retain all 20 CPUs; raw kernel controls use ten X925 cores, saturation probes four X925 cores.
- Same boot throughout, private copies of the corpus, 4 GiB serving memory/swap caps, one OpenBLAS thread, fresh server containers, fixed Python 3.12 HTTP client, rotating order and three repeats per build. A full-query unmeasured warmup of both indexes followed the reboot. Ingest is separately labelled in-process and uncapped.
- arXiv: 2,549,119 stored chunks at 1024 dimensions, 500 original held-out queries, 200 filtered queries, the same cached exact ground truth, and one shared IVF index with nlist=256/nprobe=16. Flat recall@10 was **0.9998**, IVF **0.9922**, and exact-title known-item text-hit@10 **0.984**, identical across runtimes. These quality numbers describe this fixed corpus/index and title task, not general hybrid relevance.
- Document corpus: deterministic 320 documents, 2,080 chunks plus 320 summaries, 3072-dimensional vectors, 500-word chunks, 240 requests per case. It reproduces query shapes, not a private real-world relevance corpus. Identity hashes include document/type identity and expanded siblings/summary.
- Validation: **18 arXiv sessions, 9 document sessions, 18 ingest sessions, 18 HTTP controls, and 12 raw-kernel sessions completed**. All expected files exist, measured values are finite, GIL/native checks pass, result identities match, and ingest fingerprints/job states match. The only suite failure is the known standard-3.14 invalid-mode message assertion.
- One machine, one idle boot and three repeats limit generalization. Reported ranges describe observed variation, not formal equivalence or confidence intervals. The fixed HTTP client can limit peak rates; CPU/query is measured from the server cgroup only. The 100-query controls and 30-sample hand-off probes are diagnostics, with limited tail precision.

Full tables: [supporting measurements](python-3.14-reevaluation-measurements-2026-11-08.md).

Portable numerical evidence and runners: [evaluation archive](https://github.com/EmanueleMeazzo/raggio/blob/89acfe9c3547a1c6b8a4f58e2af33076ca16cb78/bench/python314-2026-11-08/README.md),
[validation](https://github.com/EmanueleMeazzo/raggio/blob/89acfe9c3547a1c6b8a4f58e2af33076ca16cb78/bench/python314-2026-11-08/validation-idle.json), and
[aggregate results](https://github.com/EmanueleMeazzo/raggio/blob/89acfe9c3547a1c6b8a4f58e2af33076ca16cb78/bench/python314-2026-11-08/idle-summary.json). These links pin the saved evidence commit so they also work from the documentation site. The original
local archive remains at `D:/DEV/_scratch/python314-reevaluation/`; remote evidence,
query fixture and private data copies remain at
`~/raggio-bench/python314-reevaluation-20261107/` on GN100. This evaluation's
containers and runners finished; no benchmark containers remain running.

References: [Python free-threading documentation](https://docs.python.org/3.14/howto/free-threading-python.html), [CPython 3.14.10 lock implementation](https://github.com/python/cpython/blob/v3.14.10/Python/lock.c), [turbovec release metadata](https://pypi.org/pypi/turbovec/json).
