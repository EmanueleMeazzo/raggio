# DGX baseline of main @19a8a2e — 2026-09-28 (baseline3)

- **Host and dataset:** gn100 (DGX Spark, GB10, 20 aarch64 cores), rootless podman 4.9.3, image `localhost/raggio:baseline` (sha256:66aadc92…, built 2026-09-27 23:10 UTC from a git-archive of 19a8a2e), turbovec 1.0.0. Corpus: arXiv 2,549,119 × 1024 (`--limit 2549619`), seed 42, 500 queries, concurrency 8, 4 GiB cap on every measured run.
- **State at start:** 03:14 CEST, uptime 1:49, 6.1 GB page cache (mostly cold: meta.db alone is 15.1 GB), volume `bench-tv` with IVF attached (nlist 256, nprobe 16), no pending jobs.
- **Bench client:** scratch-patched on the DGX copy only (the server code is untouched):
  - the reuse-check httpx timeout goes from 30 to 900 s;
  - `ingest_raggio` raises instead of DELETE+re-ingest;
  - the email-corpus `fingerprint-raggio.json` is replaced with the arXiv one.

  The script is `D:/DEV/_scratch/dgx/dgx-baseline3.sh`. Plan A turns these patches into real bench hardening.
- **Sequence** (each step on a fresh container unless noted):
  1. 4g, IVF: run1, run2.
  2. 8g throwaway: detach IVF. `_require_headroom` refuses the detach at 4g with IVF loaded.
  3. 4g, flat: run1, run2.
  4. 4g: IVF build + run3.

  Each bench run ends with the bench's own `podman restart` cold-start measurement.

## Startup observations

| Event | Value |
|---|---|
| First container start → `/healthz` | 1.4 s |
| First `GET /collections/bench` after start (lazy cold load + `stats()`, **on the event loop**), IVF, mostly-cold page cache | **128.0 s** |
| Same, later starts (warm page cache): IVF / flat | 4.9 s / 4.7–4.8 s |
| Bench cold-start metric (restart → first query): run1-ivf (partly warm) | 31.0 s |
| Bench cold-start metric, warm runs (flat / ivf) | 4.3–4.4 s / 4.5–4.9 s |
| IVF detach (index removal) at 8 GiB | 285 s |
| IVF build at 4 GiB (from flat, warm) | 213 s (published: 337 s) |
| RSS with IVF loaded from disk vs right after building it | 1.51 GB vs **2.72 GB** (retained heap, spec §5 finding 4) |

## Results (bench metrics)

### raggio (flat)
| metric | published | run1 | run2 | noise (run1 vs run2) |
|---|---|---|---|---|
| Search p50 (ms) | 23.0 | 20.7 | 21.3 | 3.1% |
| Search p95 (ms) | 28.7 | 23.5 | 23.4 | 0.1% |
| Search p99 (ms) | 33.9 | 25.4 | 24.2 | 5.0% |
| QPS serial | 42 | 47.5 | 46.7 | 1.8% |
| QPS concurrent | 129 | 116.5 | 116.9 | 0.3% |
| p95 under concurrency (ms) | 68.7 | 73.6 | 71.4 | 3.1% |
| Filtered p50 (ms) | 29.3 | 28.0 | 25.2 | 10.3% |
| Filtered p95 (ms) | 36.2 | 30.4 | 29.4 | 3.2% |
| Recall@10 | 1.000 | 1.000 | 1.000 | — |
| Hybrid p50 (ms) | 104.0 | 88.7 | 86.2 | 2.8% |
| Hybrid p95 (ms) | 172.2 | 162.3 | 160.4 | 1.2% |
| Hybrid p99 (ms) | 588.3 | 559.3 | 554.9 | 0.8% |
| Hybrid QPS serial | 8.6 | 9.8 | 10.0 | 2.5% |
| Hybrid QPS concurrent | 13.8 | 13.9 | 13.7 | 1.7% |
| Hybrid text-hit@10 | 0.984 | 0.984 | 0.984 | — |
| Memory under load (MB) | 1761 | 1552 | 1558 | 0.4% |

### raggio-ivf
| metric | published | run1 (first after cold load) | run2 | run3 (after 4g build) | spread run2 vs run3 |
|---|---|---|---|---|---|
| Search p50 (ms) | 11.2 | 15.5 | 11.2 | 11.1 | 0.9% |
| Search p95 (ms) | 12.5 | 17.1 | 12.2 | 13.6 | 10.9% |
| QPS serial | 88 | 63.2 | 87.6 | 87.6 | 0% |
| QPS concurrent | 116 | 214.3 | 166.5 | 195.4 | 16.0% |
| p95 under concurrency (ms) | 77.8 | 39.6 | 52.3 | 79.5 | 41.3% |
| Filtered p50 (ms) | 72.3 | 43.7 | 70.5 | 55.4 | 24.0% |
| Filtered p95 (ms) | 77.8 | 77.7 | 75.7 | 78.6 | 3.8% |
| Recall@10 | 0.994 | 0.994 | 0.994 | 0.993 | — |
| Hybrid p50 (ms) | 91.4 | 134.3 | 90.5 | 85.2 | 6.0% |
| Hybrid p99 (ms) | 592.6 | 647.5 | 556.3 | 557.2 | 0.2% |
| Hybrid QPS concurrent | 13.3 | 13.4 | 13.5 | 13.4 | 0.7% |
| Memory under load (MB) | 3071 | 1510 | 1513 | 2757 | — |

## Protocol lessons (folded into spec §6)

1. The first run after a cold load is ~30–45% slower on IVF serial and hybrid, and its concurrent numbers are erratic. Discard a warm-up run0.
2. IVF concurrent and filtered metrics vary by 16–24% between warm runs. Report the median of ≥3 measured runs, and use the max–min spread as the noise band.
3. Flat metrics are stable: ≤3% spread, except filtered p50 at 10%.
4. The published hybrid numbers (104 ms p50) predate later commits on main. This baseline is the new reference for main @19a8a2e.
5. Until Plan C releases memory after a build, IVF → flat detach needs a transient 8 GiB container.
6. Cold start has three regimes: true-cold 128 s (IVF), partly-warm ~31 s, and warm ~4.5 s. Every cold-start claim must say which one it is.
