# DGX probe p4 — free-threaded CPython 3.14t build and concurrency on aarch64 (2026-09-28)

This probe ran on gn100 from 21:00 to 22:10 CEST, about 1 h 10 min, under the 3 h soft cap. It built Plan G's multi-stage image shape from the 19a8a2e tree for three uv-managed interpreters. It then compared them with the Debian baseline image `localhost/raggio:baseline` (19a8a2e, Debian python 3.12.12, SQLite 3.40.1).

**Every number is DGX-measured and warm; there are no cold runs.** Every server ran at `--memory 4g` with `OPENBLAS_NUM_THREADS=1`. Values are the median, with [min..max] over the stated reps.

Raw evidence:

- laptop: `D:/DEV/_scratch/dgx-probes/p4-freethreaded-build/{scripts,logs,out}`, with the machine summary in `logs/summary.txt`;
- DGX mirror: `~/probe/p4-freethreaded-build/{src,scripts,logs,out,sdist}`.

There is no `report.md`: the harness rejects report files from subagents, so the probe's structured output is the report. Facts about the turbovec sdist's contents come from that report (inspected on gn100); the laptop logs do not contain them.

**The probe image is not Plan G's Dockerfile.** `scripts/Dockerfile.p4` copies G's unified multi-stage shape onto 19a8a2e, which has neither Plan E's `native/` crate nor Plan G's `docker/check_image.py`. E's lines are dropped, and `p4/check_image.py` stands in for G's check. 19a8a2e also has none of Plans B–F.

## Verdict

- **The 3.14t image builds on aarch64 as designed, with 0 G2 fixes.** A cold build takes 28.7 s [28.2..29.3] over 3 reps. turbovec 1.0.0 builds from its sdist with no build arg. No upstream draft exists.
- **The GIL stays off.** No compiled dependency of the serving stack re-enables it, at import, under pytest, under uvicorn load or in the race runs. The `PYTHONWARNINGS` error filter raised nothing anywhere.
- **The test suite is green on every interpreter:** 46/46 on 3.14t, 3.14, PBS 3.12 and Debian 3.12.
- **At 19a8a2e, IVF vector concurrency favours free threading beyond the band:** +14–31 % QPS at c=8 and c=16, and 3.14t's minimum is above every GIL arm's maximum. Single-thread cost is about +3 % latency and +5 % CPU.
- **Hybrid is not a clean win.**
  - The c=8 gain belongs to the PBS interpreter plus SQLite 3.53.1, not to free threading.
  - At c=16, 3.14t gives +51 % QPS, but CPU per query rises 77–79 % and p99 is 3.8–4.0× worse.
- **G's verdict must be measured after B and F.** At 19a8a2e, IVF is bound by the single `_drain_scans` pipeline (1.1–1.25 cores busy at c=16), not by the GIL.
- **Two correctness findings belong to Plan B, not G.**
  - R1 reproduces over HTTP on every build: 5.4–6.8 % of collection-key requests fail at c=16 on the GIL builds, and 0.46 % on 3.14t.
  - Searches racing `DELETE /collections/{name}` return 500 on every build.
- **Spec fact change:** uv 0.12.19's PBS interpreters bundle SQLite **3.53.1**, not 3.50.4.

## Method

### Host state (DGX-measured)

| | Start, 21:00:55 | End, 22:06:28 |
|---|---|---|
| uptime | 19:35 | 20:40 (same boot) |
| load average | 0.02 / 0.03 / 0.00 | 0.25 / 1.59 / 2.67 |
| `free -m` used / buff-cache / available | 3,040 / 39,331 / 121,503 MB | 3,008 / 41,859 / 121,535 MB |
| swap | 16,383 MB, 0 used | 16,383 MB, 0 used |

- At start: total 124,544 MB, `Cached` 39,047,416 kB, `nproc` 20, boot_id `753165e3-da82-460f-8812-e6e0835e3055`.
- Rootless podman 4.9.3. `bench-tv` was Exited and untouched throughout.
- The base images were pulled before the builds, in 10 s total: `uv:0.12.19` 2 s, `rust:1-slim-trixie` 7 s, `debian:trixie-slim` 1 s. There was no uv cache.

### Timeline (DGX-measured, CEST)

| Step | Time |
|---|---|
| Cold builds, 3.14t / 3.14 / PBS 3.12 | 21:04:14–21:05:04 |
| 3.14t build reps 2 and 3 | 21:05:38–21:06:36 |
| Data prep | 21:07:21–21:08:00 |
| Tests, 4 arms | 21:08:02–21:08:18 |
| 24 bench sessions | 21:13:25–21:38:18 |
| loopdiag | 21:38:36–21:41:37 |
| clientcheck | 21:42:25–21:46:44 |
| Race stress v1 (artifact, see Caveats) | 21:49:28–21:56:00 |
| Race stress | 21:56:45–22:03:15 |
| Optional C arm build, then removal | ended 22:08:40 |

### Images and versions (DGX-measured)

| Arm | Image | Python | SQLite | `unidata_version` | turbovec | Size |
|---|---|---|---|---|---|---|
| base312 | `localhost/raggio:baseline` | Debian 3.12.12 (GCC 12.2.0) | 3.40.1 | 15.0.0 | PyPI `cp39-abi3` wheel | 408 MB |
| pbs312 | `localhost/probe-py312` | PBS 3.12.14 | 3.53.1 | 15.0.0 | PyPI `cp39-abi3` wheel | 317.4 MB (since removed) |
| py314 | `localhost/probe-py314` | PBS 3.14.7 | 3.53.1 | 16.0.0 | PyPI `cp39-abi3` wheel | 320.9 MB |
| ft314t | `localhost/probe-ft314t` | PBS 3.14.7t | 3.53.1 | 16.0.0 | built from sdist, `cp314-cp314t` | 323.6 MB |

- **Image IDs:** probe-ft314t `11dd6cbad0e7`, probe-py314 `ae348168c92f`, probe-py312 `1017155babd9`. The baseline image's ID and digest are **missing** from the laptop logs.
- **Interpreters:** uv 0.12.19's python-build-standalone builds ("main, Sep 24 2026", Clang 22.1.3), installed into `/python`.
- **Builder:** `rust:1-slim-trixie` with rustc 1.98.1, cargo 1.98.1 and gcc 14.2.0 (Debian 14.2.0-19).
- **Runtime:** `debian:trixie-slim`. The venv is synced with `--no-dev`, so orjson is not in the runtime. `check_image.py` found no dev packages in any venv.
- **Compiled wheels on the PBS arms**, all interpreter-specific (`cp314-cp314t` on 3.14t): httptools 0.8.0, numpy 2.5.2, pydantic_core 2.46.4, PyYAML 6.0.3, uvloop 0.22.1, watchfiles 1.2.0, websockets 17.0.1. turbovec is the only exception.
- **Runtime ENV of the probe images:** `OPENBLAS_NUM_THREADS=1` and `PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning"`. Every probe container also passed both with `-e`, including the baseline's.

### Data (DGX-measured)

- Two private copies of `~/probe/data-ivf` (IVF, nlist 256, nprobe 16), each copied in 3 s. The shared `data-ivf` was never written.
  - `data-ivfx`, the IVF copy. The collection key `p4colkey` was set on its catalog row for the R1 hammer.
  - `data-flatx`, detached to flat (`DELETE /collections/bench/index`) in an 8 GiB baseline container. The job took 25.0 s, 30 s in total with the load, at a cgroup memory peak of 5,026,668,544 B. The result is `meta.db` 15,132,643,328 B and `index.tvim` 1,336,856,659 B.
- Every bench session reused one of these two copies, one session at a time.

### Sessions (DGX-measured protocol)

- One fresh `--memory 4g` container per session. The server runs through `srvwrap.py`: the image's uvicorn command plus a thread that logs `sys._is_gil_enabled()` at start, every 15 s and at exit.
- **Order:** for each of 3 reps, arms base312, ft314t, py314, pbs312 (ABCD ABCD ABCD). Each arm ran an IVF session and then a flat session: 24 sessions in all.
- **Driver:** `drv.py` on the same host, with httpx and bench-like requests (k=10, root key, `asyncio.Semaphore(c)`), on p2's 500-query set (`~/probe/p2-hybrid-native/q`).
- **Phases:**
  1. first search, which is the collection load;
  2. a discarded warm-up: vector 100 at c=1 and 200 at c=8, plus hybrid 40 at c=1 and 80 at c=8 in IVF sessions;
  3. vector at c=1 / 8 / 16: 500 / 1,500 / 1,500 requests;
  4. IVF sessions only: hybrid at c=1 / 8 / 16, 150 / 300 / 300 requests;
  5. IVF sessions only: the R1 hammer, 800 requests at c=16 with the collection key;
  6. `/healthz`.
- Phases longer than 500 requests cycle the same 500 queries (warm caches).
- Server CPU per query comes from the container cgroup's `cpu.stat`. A 1,500 s driver `timeout` served as the deadlock detector.
- **Regime of every concurrency row:** `page_cache=host-warm`, `memory=4g` with no `--memory-swap` (spec §6: this grants 4 GiB of swap), `openblas_num_threads=1`. Python and SQLite per arm as in the table above.

## Results

### Build (DGX-measured)

Every build used `podman build --no-cache` with pre-pulled base images and no uv cache.

| Image | Reps | Build wall | Dependency `uv sync` step | uv "Prepared 24 packages" |
|---|---|---|---|---|
| probe-ft314t (3.14t) | 3 | **28.7 [28.2..29.3] s** | 17 / 17 / 19 s | 17.03 / 16.73 / 18.00 s |
| probe-py314 (3.14) | 1 | 10.6 s | 1 s | 0.81 s |
| probe-py312 (PBS 3.12) | 1 | 10.2 s | 0 s | 0.51 s |
| probe-py314src (3.14, C arm) | 1 | 29 s | 18 s | 18.17 s |

- The dependency step's time on 3.14t is turbovec's compile. The only packages built from source were turbovec 1.0.0 and raggio itself; raggio's own sync took 379–488 ms on every image.
- The C arm was built with `--build-arg UV_NO_BINARY_PACKAGE=turbovec`. Its layers from the build arg on could not reuse a builder cache.

**turbovec builds (DGX-measured)**

| Build | Wheel tag | Built by | Extension file |
|---|---|---|---|
| 3.14t, from the sdist | `cp314-cp314t-linux_aarch64` | maturin 1.15.0 | `_turbovec.cpython-314t-aarch64-linux-gnu.so` |
| 3.14 and 3.12, the PyPI wheel | `cp39-abi3-manylinux_2_28_aarch64` | maturin 1.14.1 | `_turbovec.abi3.so` |
| C arm: 3.14, from the sdist | `cp39-abi3-linux_aarch64`, **not** `cp314-cp314` | maturin 1.15.0 | `_turbovec.abi3.so` |

- **No build arg was needed on 3.14t.** PyPI has only the `cp39-abi3` wheel, and an abi3 wheel cannot load on 3.14t, so uv falls back to the sdist on its own.
- **The sdist** (from the report's inspection on gn100):
  - its sha matches `uv.lock`;
  - it pins pyo3 0.29.0, the numpy crate 0.29.0 and rayon 1.12.0, with the feature `abi3-py39`;
  - it declares a bare `#[pymodule]` (lib.rs:2151), so it runs with `gil_used=false`, the default since PyO3 0.28.
- The `abi3-py39` feature is why the C arm's own build is still an abi3 wheel on a GIL interpreter.
- **G2 fixes needed: 0.** There is no `p4-attempts.md` and no upstream draft.
- **Not measured:** `raggio_native`'s compile time on cp314t. Plan E's crate is not in 19a8a2e.

### GIL (DGX-measured)

Every check ran with `PYTHONWARNINGS='error:The global interpreter lock (GIL) has been enabled:RuntimeWarning'`.

| Check | Scope | Result on 3.14t |
|---|---|---|
| `check_image.py`, last builder step | 3 builds; 8 extension packages imported: httptools, numpy, pydantic_core, turbovec, uvloop, watchfiles, websockets, yaml | GIL disabled |
| `check_image.py` in the runtime image | tests container | GIL disabled |
| `gil_probe.py`, a fresh interpreter per module | 23 modules, including `turbovec._turbovec`, `raggio.store`, `raggio.app`, `_sqlite3`, `unicodedata` | none re-enabled the GIL or failed to import |
| `gilcheck` pytest plugin | end of the pytest session | `gil_enabled_at_session_end=False` |
| `srvwrap` start and exit lines | 6 bench sessions | 12/12 False |
| every `srvwrap` line (start, every 15 s, exit) | 6 bench sessions | 32 False, 0 True |
| `srvwrap` lines | 2 race runs | 6 False, 0 True each |

No `RuntimeWarning` appeared anywhere. The GIL arms logged `gil_enabled=True`, as expected.

### Tests (DGX-measured)

The suite ran in a throwaway `--rm` root container per image. pytest 9.1.1 was installed with the host's uv 0.12.5, so the images themselves carry no dev packages.

| Arm | Result | pytest time | ResourceWarnings |
|---|---|---|---|
| ft314t | 46 passed, 1 warning | 2.53 s | 0 |
| py314 | 46 passed, 1 warning | 2.31 s | 0 |
| base312 | 46 passed, 1 warning | 2.33 s | 0 |
| pbs312 | 46 passed, 1 warning | 2.23 s | 0 |

The warning is Starlette's `StarletteDeprecationWarning`: "Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead."

### Interpreter facts (DGX-measured)

- **`unicodedata.unidata_version`:** 15.0.0 on base312 (3.12.12) and pbs312 (3.12.14); 16.0.0 on py314 (3.14.7) and ft314t (3.14.7t).
- **SQLite:** all three PBS interpreters bundle 3.53.1, not 3.50.4. The Debian baseline has 3.40.1.
- Plan G's Unicode drift probe (Task 8) was not run.

### Session health (DGX-measured)

- All 24 sessions: no crash, no OOM (`memory.events` max 0, oom 0, oom_kill 0), no driver timeout, and `/healthz` 200 afterwards.
- The first search, which loads the collection, took 3.7–4.2 s on every arm.

`memory.peak` per session in GB (r1 / r2 / r3). The IVF sessions include the hybrid and R1 phases:

| Arm | IVF | flat |
|---|---|---|
| base312 | 2.01 / 1.96 / 1.98 | 1.83 / 1.84 / 1.83 |
| ft314t | 1.88 / 1.88 / 1.88 | 1.91 / 1.87 / 1.86 |
| py314 | 1.95 / 1.99 / 1.98 | 1.84 / 1.84 / 1.84 |
| pbs312 | 2.02 / 2.03 / 1.96 | 1.86 / 1.84 / 1.84 |

### IVF vector (DGX-measured, 3 reps per arm, 4g)

**c=1**, 500 requests:

| Arm | p50 ms | QPS | server CPU ms/query |
|---|---|---|---|
| base312 | 8.6 [8.5..9.1] | 115.5 [108.7..116.7] | 6.3 [6.0..6.4] |
| ft314t | 8.9 [8.8..9.4] | 111.5 [105.1..112.9] | 6.6 [6.5..6.7] |
| pbs312 | 8.6 [8.6..9.2] | 114.4 [104.5..114.8] | 6.3 [6.2..6.7] |
| py314 | 8.7 [8.2..9.1] | 108.8 [108.2..114.4] | 6.2 [6.0..7.6] |

3.14t's single-thread cost against base312 is about +3 % p50 and +5 % CPU per query. The p50 ranges overlap, so it is inside the band.

**c=8**, 1,500 requests:

| Arm | QPS | p50 ms | p99 ms | CPU ms/query |
|---|---|---|---|---|
| base312 | 180.6 [170.6..187.2] | 42.2 [40.4..43.8] | 67.4 [65.4..73.6] | 5.9 [5.9..6.4] |
| **ft314t** | **225.1 [209.0..228.8]** | **34.0 [33.9..38.6]** | 51.2 [42.8..59.8] | 5.5 [5.4..5.7] |
| pbs312 | 197.7 [168.5..199.5] | 38.7 [38.5..42.7] | 54.5 [52.0..74.6] | 5.6 [5.6..6.4] |
| py314 | 171.5 [165.0..199.4] | 42.6 [42.0..45.6] | 75.5 [50.7..79.5] | 6.3 [5.0..6.6] |

**c=16**, 1,500 requests:

| Arm | QPS | p50 ms | p99 ms | CPU ms/query |
|---|---|---|---|---|
| base312 | 208.2 [206.6..213.1] | 74.0 [71.2..75.2] | 109.1 [90.3..130.6] | 5.4 [5.2..5.4] |
| **ft314t** | **240.2 [238.8..240.9]** | **63.3 [63.1..63.9]** | **84.8 [80.9..97.2]** | 5.5 [5.1..5.5] |
| pbs312 | 200.1 [188.4..217.0] | 74.8 [69.7..83.4] | 113.2 [112.0..114.7] | 5.7 [5.1..5.9] |
| py314 | 211.3 [202.3..220.3] | 70.4 [68.6..73.6] | 114.5 [104.0..116.0] | 5.3 [5.1..5.6] |

- ft314t's QPS gain on the medians: at c=8, +25 % over base312, +14 % over pbs312 and +31 % over py314; at c=16, +15 %, +20 % and +14 %.
- ft314t's minimum is above every GIL arm's maximum at both levels: 209.0 against at most 199.5 at c=8, and 238.8 against at most 220.3 at c=16.
- CPU per query is 5.3–5.7 ms at c=16 on every arm, so 3.14t does not buy its QPS with extra CPU here.

### Hybrid on the IVF data (DGX-measured, 3 reps per arm, 4g)

**c=1**, 150 requests:

| Arm | p50 ms | p99 ms | QPS | CPU ms/query |
|---|---|---|---|---|
| base312 | 78.9 [78.5..79.1] | 546.4 [546.0..559.4] | 11.2 [11.1..11.2] | 93.3 [93.2..93.4] |
| ft314t | 76.2 [76.2..76.6] | 497.5 [493.0..499.3] | 11.5 [11.5..11.6] | 90.1 [89.9..90.4] |
| pbs312 | 75.0 [74.8..75.8] | 493.8 [490.5..493.9] | 11.8 [11.8..11.8] | 88.3 [88.1..90.8] |
| py314 | 76.3 [75.1..76.4] | 492.1 [491.5..493.1] | 11.6 [11.6..11.8] | 89.5 [89.1..90.9] |

There is no single-thread penalty on 3.14t.

**c=8**, 300 requests:

| Arm | QPS | p50 ms | p99 ms | CPU ms/query |
|---|---|---|---|---|
| base312 | 12.7 [12.6..12.8] | 497.0 [479.9..499.9] | 6,731 [6,443..6,900] | 556.5 [543.3..556.8] |
| ft314t | 18.1 [18.1..18.5] | 343.7 [338.2..345.3] | 4,976 [4,824..4,983] | 418.3 [408.9..419.2] |
| pbs312 | 17.9 [17.8..20.1] | 355.9 [355.1..368.0] | 3,981 [1,653..4,097] | 343.0 [216.9..355.0] |
| py314 | 18.2 [18.2..18.7] | 365.1 [357.8..381.3] | 3,478 [2,217..3,611] | 315.6 [286.0..332.4] |

**c=16**, 300 requests:

| Arm | QPS | p50 ms | p99 ms | CPU ms/query |
|---|---|---|---|---|
| base312 | 12.1 [11.8..12.5] | 1,151.6 [1,115.8..1,212.7] | 8,894 [4,503..10,559] | 872.3 [706.8..883.6] |
| **ft314t** | **22.4 [22.3..23.0]** | **508.8 [503.2..520.3]** | 7,584 [7,575..9,502] | 647.2 [621.7..650.3] |
| pbs312 | 14.8 [14.8..15.3] | 1,076.1 [1,053.1..1,081.7] | **1,890 [1,861..2,290]** | 365.1 [332.0..372.5] |
| py314 | 14.8 [14.3..14.9] | 1,101.4 [1,056.6..1,154.7] | 2,000 [1,872..2,111] | 361.9 [343.0..372.1] |

- **c=8:** the three PBS arms run about 18 QPS whatever the GIL, against 12.7 on the Debian baseline. The gain comes from the interpreter and SQLite 3.53.1, not from free threading.
- **c=16:** 3.14t gives +51 % QPS over the GIL arms on the same SQLite (22.4 against 14.8), and its minimum clears their maximum. The price:
  - CPU per query +77–79 % (647.2 against 361.9–365.1 ms);
  - p99 3.8–4.0× worse (7.6 s against 1.9–2.0 s).
- Every arm has multi-second p99 tails at c=8 and c=16. Their cause was not investigated.

### Flat vector (DGX-measured, 3 reps per arm, 4g)

**c=1:** about 53–54 QPS and a p50 of about 18.5–18.8 ms on every arm, at about 207 ms CPU per query. turbovec's rayon search runs on many cores.

| Arm | p50 ms | QPS | CPU ms/query |
|---|---|---|---|
| base312 | 18.6 [18.4..18.7] | 53.6 [53.1..53.9] | 207.4 [205.9..207.6] |
| ft314t | 18.8 [18.7..18.8] | 52.9 [52.9..53.3] | 207.0 [206.5..208.2] |
| pbs312 | 18.5 [18.4..18.5] | 53.7 [53.7..54.2] | 206.8 [205.9..207.7] |
| py314 | 18.6 [18.5..18.7] | 53.6 [53.1..53.7] | 207.1 [206.5..207.2] |

**c=8 and c=16 flip between two states, whatever the arm.** QPS per rep (r1 / r2 / r3):

| Arm | c=8 | c=16 |
|---|---|---|
| base312 | 134.4 / **238.0** / 133.0 | 221.3 / 218.0 / 218.2 |
| ft314t | **253.2** / 133.2 / **257.8** | 273.1 / 238.8 / 218.8 |
| py314 | **234.3** / 132.7 / 133.1 | 332.2 / 219.1 / 245.5 |
| pbs312 | 135.8 / 134.1 / 132.8 | 217.3 / **394.4** / 219.6 |

- **Slow state at c=8:** 132.7–135.8 QPS at 115.9–117.6 ms CPU per query.
- **Fast state at c=8** (bold): 234.3–257.8 QPS at 50.8–56.8 ms CPU per query.
- Runs in the fast state at c=8: base312 1/3, ft314t 2/3, pbs312 0/3, py314 1/3.
- c=16 ranges over 217.3–394.4 QPS. The state can change between phases of one session: pbs312 r2 was slow at c=8 and gave 394.4 at c=16.
- The medians therefore reflect how many reps landed in the fast state, not the interpreter. No interpreter effect can be claimed on these rows. The cause of the two states was not investigated.

### Result identity (DGX-measured)

Top-10 ids were compared with base312 rep 1. Every arm, rep and phase matched exactly and in order:

- vector, IVF and flat, every concurrency: 500/500;
- hybrid: 150/150 at c=1, 300/300 at c=8 and c=16.

This holds despite Unicode 15.0.0 → 16.0.0 and SQLite 3.40.1 → 3.53.1, on these bench queries.

### Where the time goes at c=16 (DGX-measured, 1 run per arm and leg)

**loopdiag:** per-thread CPU from `top -H` on the server process (two 6 s samples, top 12 threads), with one driver at c=16.

| Arm, leg | Threads | Result |
|---|---|---|
| base312, IVF vector | main (event loop) 22.8 %; 5 workers at 16.5–18.2 %; the rest ≤0.3 % | about 1.1 cores busy; 204.7 QPS, p99 107.2 ms |
| ft314t, IVF vector | main 24.7 %; 9 `ThreadPoolExecutor` workers at 10.3–11.5 %; 2 AnyIO at 0.7 % | about 1.25 cores busy; 242.6 QPS, p99 82.4 ms |
| base312, hybrid | the 12 threads shown at 61.1–93.2 % | 12.2 QPS, 824.4 ms CPU per query, p99 8.2 s |
| ft314t, hybrid | the 12 pool threads shown at 87.7–90.0 % | 28.3 QPS, 518.7 ms CPU per query, p99 6.0 s |

On IVF vector no thread is saturated on either interpreter. That points to the single `_drain_scans` batch pipeline, as in p3.

**clientcheck:** one driver at c=16 against two drivers at c=8 each, 2 trials per arm. Driver CPU is over 6 s.

| Arm | Trial | One driver: QPS (driver CPU) | Two drivers: aggregate QPS (driver CPU) |
|---|---|---|---|
| ft314t | 1 | 242.2 (34.1 %) | 245.3 (11.5 % + 8.2 %) |
| ft314t | 2 | 244.0 (35.1 %) | 246.0 (14.0 % + 9.3 %) |
| base312 | 1 | 193.7 (27.2 %) | 206.4 (12.1 % + 9.7 %) |
| base312 | 2 | 199.7 (22.5 %) | 195.9 (9.3 % + 7.2 %) |

Splitting the client does not raise the aggregate beyond trial-to-trial spread, so **the client is not the bottleneck**. Neither run logged a server traceback.

### R1 auth hammer (DGX-measured, 800 requests per rep at c=16, collection key, 3 reps)

| Arm | 401 | 500 | Failed | Failure rate | Failed per rep | Hammer QPS |
|---|---|---|---|---|---|---|
| base312 | 101 | 62 | 164/2,400 | 6.8 % | 46 / 68 / 50 | 187.1 [168.7..192.0] |
| pbs312 | 89 | 41 | 130/2,400 | 5.4 % | 42 / 51 / 37 | 192.1 [178.0..196.3] |
| py314 | 82 | 52 | 135/2,400 | 5.6 % | 53 / 37 / 45 | 183.7 [183.3..190.0] |
| **ft314t** | 10 | 1 | **11/2,400** | **0.46 %** | 0 / 6 / 5 | 238.2 [236.4..238.7] |

- base312's and py314's totals each include 1 client-side `RemoteProtocolError`.
- **The symptoms, from the server logs:**
  - 500 with `sqlite3.InterfaceError: bad parameter or other API misuse` at store.py:1631 in `get_config` (155 tracebacks over the 12 sessions);
  - 500 with `IndexError: tuple index out of range` on `row[7]` at store.py:1639 (1, py314 r3), meaning another statement's row came back;
  - 401 on a valid key: a silent wrong-row auth decision.
- The bench phases use the root key, so they never see R1.

### Race stress, R1–R4 (DGX-measured, 2 reps per arm, 4g, a fresh data directory per run)

**The collection** is tiny: `race`, dim 64, 400 documents of 1 summary + 10 chunks each (about 4.4k records), IVF nlist 16, nprobe 4. These runs are therefore Python-bound.

**The phases:**

1. 45 s mixed: 12 readers (vector, text and hybrid, filtered and unfiltered; half with the collection key) against 2 writers (ingest, delete, metadata PATCH) and a filtered document lister;
2. quiesce, then a stale check by 8 readers;
3. `DELETE /collections/race` while 12 readers still run.

Mixed-phase searches, 200 / 401 / 500 per run:

| Arm | r1 | r2 |
|---|---|---|
| base312 | 3,987 / 61 / 33 | 4,105 / 66 / 47 |
| py314 | 3,823 / 29 / 20 | 4,129 / 43 / 26 |
| **ft314t** | **11,931 / 2 / 0** | **12,115 / 1 / 0** |

`RemoteProtocolError` in the mixed phase: base312 2 and 2, py314 1 and 0, ft314t 0 and 0.

**Stale check:** 0 documents came back whose delete had returned 200, in any of the 6 runs. Deletes that got 200 per run: 399–645. Deletes that got 404 because the 202 ingest job had not landed yet: 16–42; those documents can legitimately appear later.

**Delete under load:** every arm returned 500s to searches racing `DELETE /collections/race`.

| Arm | 500s (r1, r2) | `RemoteProtocolError` (r1, r2) | `/healthz` after |
|---|---|---|---|
| base312 | 464, 704 | 96, 165 | 200, 200 |
| py314 | 294, 418 | 36, 54 | 200, 200 |
| ft314t | 193, 171 | 3, 3 | 200, 200 |

**The server exceptions** over the whole run. Counts are per run, over the runs that logged them:

- `sqlite3.OperationalError`:
  - `unable to open database file`: 170–322, in 4 runs;
  - `no such table: records` / `records_fts` / `records_fts_v` / `vecs`: in base312 r1 and py314 r2;
  - `disk I/O error`: 47 and 229, in 2 runs;
- `FileNotFoundError` on `/data/collections/race/ivf`: 1 each in ft314t r1, ft314t r2 and py314 r1;
- the R1 pair: `InterfaceError` 1–70 in 5 runs (none in ft314t r2), and `IndexError` 1–7 in all 6.

**Not seen on any build, including 3.14t:** a crash, a deadlock, a process death, an OOM, or a "changed size" `RuntimeError` (R4).

## Conclusions

**Build feasibility is settled: Plan G's 3.14t image builds on aarch64 as it stands.**

- The multi-stage shape works: uv 0.12.19; `uv python install 3.14t` into `/python`; turbovec 1.0.0 from its sdist in `rust:1-slim-trixie` with rustc 1.98.1; runtime on `debian:trixie-slim`; no orjson in the runtime.
- A cold build takes about 29 s. No fix attempt was needed, so there is no upstream draft.
- The one gap: `raggio_native`'s cp314t compile time, because Plan E's crate is not in 19a8a2e.

**The GIL stays off, and the guard has no false positives.**

- None of numpy, pydantic_core, turbovec, uvloop, httptools, watchfiles, websockets or yaml re-enables it.
- The `PYTHONWARNINGS` error filter raised nothing at import, under pytest or under uvicorn load.
- Known-bad dependencies: none.

**The suite is green on every interpreter:** 46/46 on 3.14t, 3.14, PBS 3.12 and Debian 3.12.

**3.14t's single-thread cost is small.** IVF vector p50 is about +3 % and CPU per query about +5 %, inside the band. Flat vector at c=1 overlaps every GIL arm. Hybrid at c=1 has a p50 of 76.2 ms on 3.14t, against 75.0–78.9 ms on the GIL arms.

**IVF vector concurrency favours free threading beyond the band, but the GIL is not the bound.**

- At c=8 and c=16, 3.14t gives +14–31 % QPS over the GIL arms, and a lower p99 at c=16: 84.8 ms against 109–115 ms.
- Only 1.1–1.25 cores are busy at c=16, and the client is not the limit.
- At 19a8a2e the bound is the single `_drain_scans` batch pipeline, whose kernel calls p3 found to run one after another. Plan F changes that picture, so G's verdict must be measured after F.

**Hybrid is not a clean win.**

- At c=8 the gain comes from the PBS interpreter and SQLite 3.53.1, not from free threading. pbs312, py314 and ft314t all run about 18 QPS, against 12.7 on the Debian baseline with SQLite 3.40.1.
- At c=16, 3.14t gives +51 % QPS over the GIL arms on the same SQLite, but CPU per query rises 77–79 % and p99 is 3.8–4.0× worse.
- This fits contention in SQLite's global memstatus mutex (D15) under true parallelism, but **no profile was taken**. By the spec's own criteria this is not a clean win.

**Flat vector at c=8 and c=16 is bimodal on all four interpreters.** No interpreter effect can be claimed there. This confirms spec §6's rule of at least 3 runs.

**Results do not change with the interpreter.** Top-10 ids match base312 exactly and in order on every arm and mode, despite Unicode 15.0.0 → 16.0.0 and SQLite 3.40.1 → 3.53.1, on these bench queries.

**R1 reproduces over HTTP on every build.**

- R1 is the shared catalog connection: `get_config` runs outside `_catalog_lock`.
- 5.4–6.8 % of collection-key requests fail at c=16 on the GIL builds: 500s (`InterfaceError`, `IndexError` on another statement's row) and silent wrong-row 401s.
- 3.14t shows it less often (0.46 %), but not zero.

**R3 is not specific to 3.14t.** Searches racing `DELETE /collections/{name}` return 500 on every build. The errors are `sqlite3.OperationalError` (unable to open database file, no such table, disk I/O error), `FileNotFoundError` on the IVF directory, and dropped connections. There was no crash, deadlock or process death, and `/healthz` stayed 200.

**R2 and R4 were not observed over HTTP.** No deleted document came back stale, and no "changed size" `RuntimeError` appeared on any build, including 3.14t, over 2 × 45 s of mixed ingest, delete, patch, filtered list and search.

## Caveats

**The image**

- **The probe image is not Plan G's Dockerfile.** It has no `raggio_native`, no G `check_image.py` (`p4/check_image.py` stands in), no `GC_FREEZE` knob and no Task 4 meta.db connection change.
- **The code is 19a8a2e**, before Plans B, C, D, E and F. Every concurrency number describes that code, not the code G will measure.
- **The sdist facts are from the report.** The pyo3, numpy-crate and rayon pins, the `abi3-py39` feature and the bare `#[pymodule]` at lib.rs:2151 come from the report's inspection on gn100; the laptop logs do not contain them.
- **The baseline image's ID and digest are missing** from the laptop logs. Its 408 MB size comes from the report.

**The measurements**

- **Warm runs only.** There are no cold runs and no true-cold start.
- **No pinning.** Rootless podman cannot use `--cpuset-cpus`, so thread placement between the X925 and A725 cores was not controlled. The driver ran on the same host as the server.
- **The number of reps varies.**
  - The bench sessions have 3 reps per arm.
  - The py314 and PBS 3.12 builds, the C arm build, loopdiag and each race arm have 1 or 2.
  - clientcheck has 2 trials per arm.
- **The bottleneck is inferred.** loopdiag is 1 run per arm and leg, with two 6 s `top -H` samples of the top 12 threads. The `_drain_scans` attribution rests on per-thread CPU, not on a per-stage trace.
- **The hybrid CPU rise on 3.14t was not profiled.** Its attribution to SQLite's memstatus mutex (D15) is inferred. The multi-second p99 tails on every arm were not investigated either.
- **Hybrid was measured on the IVF data only.** G's gate reads the flat engine's hybrid rows, which p4 did not measure.
- **The cause of the flat bimodality** was not investigated.
- **R1 counts on 3.14t are small:** 11 failures in 2,400 requests. The 0.46 % rate is a point estimate.
- **`--memory 4g` was set without `--memory-swap`**, which grants 4 GiB of swap (spec §6). `memory.peak` stayed at 1.83–2.03 GB, but no memory claim is made.

**The race runs**

- **The race collection is tiny:** dim 64, about 4.4k records, IVF nlist 16. Those runs are Python-bound and are not a performance measurement.
- **Race stress v1 had a checker artifact.** Its stale check counted documents whose delete had returned 404 because their 202 ingest had not landed yet. It reported 34–537 "stale" documents in 5 of 6 runs.
  - The fixed run separates them as `late_ingest_of_404_deleted_doc (expected)`: 33–526 in 5 of 6 runs.
  - Only the fixed run's numbers are quoted above.
- **R2 was checked only after quiescence**, by 8 readers. A stale read during the mixed phase would not be counted.
- **R4 was checked only through the server logs.** p4 has no in-process reproduction.

## Plan impacts

These are proposals. This probe edited no plan or spec file.

### Plan G

1. **Task 11's p4 branch does not trigger.** turbovec 1.0.0 at `uv.lock`'s sha builds for cp314t on aarch64 with 0 builder-stage fixes. None of these is needed:
   - `test_the_p4_turbovec_build_fix_stays_in_the_builder_stage`, and the test counts that assume it;
   - `D:/DEV/_scratch/dgx/planG/p4-attempts.md`;
   - the upstream draft and the Park at p4.

   What stays open for Task 11:
   - `raggio_native`'s cp314t build, which p4 could not run;
   - G's real `check_image.py` on G's real Dockerfile.

   Plan G's lines 11 and 42 and the spec's §8 row can point here instead of "pending".
2. **No build arg is needed for 3.14t.** uv falls back to the sdist on its own, because PyPI's only wheel is `cp39-abi3`. Task 6's `ARG UV_NO_BINARY_PACKAGE` matters only for the C1 arm.
3. **The C1 arm is not "the same turbovec build".**
   - The ADR 0006 Decisions row (line 1971) and the Task 6 comment say that `PYTHON=3.14 UV_NO_BINARY_PACKAGE=turbovec` gives a GIL control with the same turbovec build.
   - On gn100 it gives `cp39-abi3-linux_aarch64` (`_turbovec.abi3.so`), because the sdist enables `abi3-py39`. 3.14t gives `cp314-cp314t`.
   - Both come from the same source with maturin 1.15.0, but the ABI differs. The C1 control still separates a local build from PyPI's maturin 1.14.1 manylinux wheel.
   - Reword the row, or drop C1: the maintainer's choice.
4. **Build time for the ADR's Consequences and the G4 window:**
   - 28.7 s [28.2..29.3] cold on gn100, without `raggio_native`;
   - the dependency step takes 17–19 s of that, which is turbovec's compile;
   - the 3.14 GIL image takes 10.6 s;
   - the images are 323.6 MB (3.14t) and 320.9 MB (3.14).
5. **The hybrid half of the decision rule can pass a regression.**
   - G's A (`PYTHON=3.12`, PBS) and B (3.14t) both bundle SQLite 3.53.1, so `Hybrid QPS concurrent` can carry the win under the current rule.
   - `bench.py`'s `--concurrency` defaults to 8. At c=8, p4 saw no free-threading gain on hybrid: 18.1 against 17.9 and 18.2 QPS.
   - At c=16, 3.14t's +51 % QPS came with p99 3.8–4.0× worse and CPU per query +77–79 %. Under the rule, those are listed as costs and never gate.
   - Suggestions:
     - a p99 or CPU-per-query guard on a hybrid win;
     - a profile of 3.14t hybrid at c=16 before crediting or blaming D15.
   - p4 measured hybrid on the IVF data. G reads the flat engine's hybrid rows, which p4 did not measure.
6. **The flat `QPS concurrent` row is bimodal on every interpreter.**
   - The two states:
     - slow: 132.7–135.8 QPS at 115.9–117.6 ms CPU per query;
     - fast: 234.3–257.8 QPS at 50.8–56.8 ms.
   - The state can flip within one session.
   - With 3 runs a side, a 2/3 against 0/3 split moves the median about 1.9× with no interpreter effect. In p4, ft314t gave 253.2 and pbs312 gave 134.1.
   - G's A1 → A2 and B1 → B2 checks catch this only when the split differs between sessions of one image.
   - Suggestions:
     - record CPU per query per run and classify each run's state, since CPU per query separates the two states cleanly;
     - or add runs.
   - p4's IVF rows were stable, but G does not gate on IVF because each session rebuilds it from a random sample.
7. **Keep the `PYTHONWARNINGS` guard.** It gave no false positive at import, under pytest, under uvicorn load or in the race runs. The list of GIL-enabling dependencies is empty for this lock. 19a8a2e has no `/healthz` `gil_enabled` (Task 1), so `srvwrap`'s `sys._is_gil_enabled()` lines stood in for it.

### The spec and the other plans

8. **SQLite 3.53.1, not 3.50.4 (spec D15; Plans A, G, E, C and B).**
   - uv 0.12.19's PBS interpreters (3.12.14, 3.14.7, 3.14.7t) bundle SQLite 3.53.1.
   - The repo's docs say 3.50.4 in the spec (2 lines), Plan A (9), Plan G (9), Plan E (4), Plan C (1, a test label) and Plan B (1).
   - p2's PBS scaling figures (28.6 / 47.6 / 45.4 / 39.4 q/s) and E's "native 2.1× Python" were measured on 3.50.4. They were not re-measured on 3.53.1.
   - Plan A's new reference baseline will record 3.53.1 if uv 0.12.19 stays pinned.
9. **Plan A: the base migration alone moves hybrid**, as D15 predicts. Debian 3.12.12 with SQLite 3.40.1 against PBS 3.12.14 with SQLite 3.53.1, on the IVF data:
   - c=8: 12.7 → 17.9 QPS;
   - c=16: 12.1 → 14.8 QPS, p99 8.9 → 1.9 s, CPU per query 872.3 → 365.1 ms.

   Other points for Plan A:
   - The image goes from 408 MB to 317.4 MB. The probe image has no `raggio_native` and no orjson.
   - Every interpreter's suite shows Starlette's `httpx` deprecation warning, which asks for `httpx2`. It matters to whoever re-locks the test dependencies.
10. **Plan B, R1: DGX evidence over HTTP at 19a8a2e.**
    - Collection-key requests fail at 5.4–6.8 % on the GIL builds and 0.46 % on 3.14t. The failures include silent wrong-row 401s.
    - The bench phases use the root key and never see R1.
    - Suggestion: a collection-key hammer phase in the post-B benches or in G's ABAB, as an end-to-end regression check. After B, every build should show 0 failures.
11. **Plan B, R3 and delete: 500s on every build.** Searches racing `DELETE /collections/{name}` returned 171–704 500s per run.
    - B's Task 2 pins a racing `touch()` to 404, and Task 3 moves `stop()` under the write lock. p4 did not separate in-flight searches from new `touch()` calls.
    - **Open question for the maintainer:** should a search in flight on a collection being deleted end in 404 (or another 4xx/503) rather than 500?
    - Re-run p4's race phase 3 after B.
12. **Plan F:** at 19a8a2e, concurrent IVF is bound by `_drain_scans`: 1.1–1.25 cores are busy at c=16, and the client is not the limit.
    - 3.14t's +14–31 % sits on top of the pipeline that F replaces.
    - G's ABAB after F, and G's recorded-not-gated `bench/ivf_fanout_probe.py fanout` run in both images (whether 3.14t's shard pool oversubscribes the cores), are where 3.14t may diverge.
13. **Plan E: the builder's link works on 3.14t aarch64 for a PyO3 crate.** The link is `ln -s "$(uv python find ${PYTHON})" /usr/local/bin/python3` with `PYO3_PYTHON=/usr/local/bin/python3` (Dockerfile.p4 lines 24–26).
    - It built turbovec as `cp314-cp314t`.
    - `raggio_native` itself was not built.

## DGX cleanup state

As of `host-end.txt`, 22:06:28, plus the 22:08:40 C arm line:

- **Retained:**
  - `localhost/probe-ft314t` `11dd6cbad0e7` (324 MB);
  - `localhost/probe-py314` `ae348168c92f` (321 MB).
  - Dated note, 2026-09-28 20:42:36Z: the orchestrator checked that no container used them, then removed both with `podman rmi`. The untagged images below were left alone.
- **Removed:**
  - `localhost/probe-py314src` (C arm, `c4e65720bc91`), at 22:08:40 after the tag check;
  - `localhost/probe-py312`, which is not listed at 22:06:28. The removal itself is not logged.
- **Untagged images** also listed at 22:06:28, whose origin the logs do not record:
  - six at 408 MB: `367d3df300ba`, `85b07bb12509`, `3ac2336d572e`, `58935cd4831d`, `7724fae60f2e`, `ef218613a8c4`;
  - `8f80fbd1829d` at 1.17 GB;
  - `a77025ce78ee` at 1.15 GB.
- **Containers:**
  - `bench-tv` stayed Exited and untouched;
  - `racestress.sh` removes each race data directory after its run.
- **Data:** whether `~/probe/p4-freethreaded-build/data-ivfx` and `data-flatx` were removed is **not recorded**.
  - Dated note, 2026-09-28 about 20:25Z: the orchestrator checked on gn100 that both are gone; `~/probe/p4-freethreaded-build` holds 50 MB.
  - `data-flatx` alone holds at least 16.5 GB (`meta.db` plus `index.tvim`). `data-ivfx`'s size is not logged.
  - If they are still there, remove them with `podman unshare rm -rf ~/probe/p4-freethreaded-build/data-ivfx ~/probe/p4-freethreaded-build/data-flatx`.
  - The shared `~/probe/data-ivf` was never written.
