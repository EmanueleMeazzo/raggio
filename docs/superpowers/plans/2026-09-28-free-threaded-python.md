# Free-threaded CPython 3.14t Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Erratum (2026-09-29, from plan A PR 2's final review):** plan A shipped uv **0.12.20**, not
> 0.12.19 (the maintainer: "always use the latest version"), and `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`, not
> `actions/checkout@v4`. The pins, anchors, CI job and prerequisites below are corrected to match. Where
> this plan reports p4 or its rehearsals (uv 0.12.19's PBS interpreters and their SQLite 3.53.1),
> 0.12.19 is history and stays. A's `tests.yml` also sets `UV_PYTHON_PREFERENCE: only-managed` in a
> workflow-level `env:`, so every job, this plan's included, runs uv's managed CPython, not the
> runner's. A's `tests/test_packaging.py` fails if any `uses:` in `tests.yml` is not commit-pinned
> (`@<40-hex> # vX.Y.Z`), or if any setup-uv `version:` differs from the Dockerfile's `ARG UV_VERSION`.
> The upstream-issue draft keeps "built from its sdist by uv 0.12.19" and `uv:0.12.19` in its Reproduce
> block only if the build it reports is p4's; for a build of this plan's image, write 0.12.20.

**Goal:** Build raggio on free-threaded CPython 3.14t from the one Dockerfile, as an experimental image. Guard it against a silent GIL re-enable at build time, at run time and in CI. Add the `GC_FREEZE` knob and a non-blocking 3.14t CI job. Measure 3.14t against 3.12 in an ABAB on the DGX, and record in ADR 0006 whether to continue.

**Architecture:**
- **Build.** One `PYTHON` build arg picks the interpreter. On 3.14t, uv builds turbovec from its sdist in the Rust builder stage; PyO3 0.29 declares `gil_used = false`, so no patch is needed. `docker/check_image.py` is the last builder step. It fails the build unless the interpreter is the flavour `PYTHON` names and nothing the app loads turns the GIL back on.
- **Guards.** The runtime image turns CPython's GIL-re-enable `RuntimeWarning` into an error. `/healthz` reports `gil_enabled` on every request.
- **Server changes for 3.14t.** On free-threaded builds, meta.db connections skip sqlite3's statement cache; with it, biased reference counting leaves zombie connections behind. `GC_FREEZE=1` freezes the startup heap, so the free-threaded collector's stop-the-world passes skip it.
- **Native code per interpreter.** `raggio_native` is rebuilt for each image's own interpreter (cp312, cp314t; never abi3), through Plan E's builder (`PYO3_PYTHON` names the uv-installed `/python`). turbovec is the one dependency without a cp314t wheel. Spec §8's probe p4 ran on gn100 on 2026-09-28 (`docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`), on the 19a8a2e tree, which has no `raggio_native`: turbovec 1.0.0 built from its sdist as `cp314-cp314t-linux_aarch64` with 0 builder-stage fixes and no build arg, and the GIL stayed off. So G2's failure paths below are unlikely for turbovec. Still open, and Task 11 runs it first: `raggio_native`'s cp314t build, and G's real `check_image.py` on G's real Dockerfile. If turbovec's sdist does not build, Task 11 fixes a build-dependency, toolchain or build-flag cause in the Dockerfile's builder stage only, in at most 3 attempts; if 3 attempts fail, or no builder-stage line can express a fix that needs no source change, it stops and asks the maintainer, and ADR 0006 stays `Pending:` (spec §3.1 G3); if only a change to turbovec's own source would fix it, it drafts an upstream issue for the maintainer to post and parks the evaluation without asking (spec §3.1 G2; raggio never forks turbovec, D6).
- **Evaluation.** A (3.12) and B (3.14t) are built from one commit and run as four sessions, A1 B1 A2 B2, on one boot, with Plan A's `session.sh` at `FLAT_RUNS=3` and `CONCURRENCY=8`. Each of them is followed, on the same image, by a c=16 session (`CONCURRENCY=16`, spec §3.1 G7), so the chain is eight sessions: A1 A1c16 B1 B1c16 A2 A2c16 B2 B2c16. The c=16 speed tables are reported, not gated; their GIL, Traceback and /healthz checks gate like any session's (G8). Every image runs `OPENBLAS_NUM_THREADS=1` (D16), and every table row carries its `sqlite3.sqlite_version` (D15). The decision reads the flat rows of Plan A's `compare.py` tables. It separates the CPU-bound vector rows from the SQLite-bound hybrid rows, because SQLite's global memstatus mutex caps concurrent text search whatever the GIL does. It is Continue, Hold or Park, under a rule ADR 0006 writes down before the run.

**Tech Stack:** CPython 3.12 and 3.14t (uv-managed), uv 0.12.20, turbovec 1.0.0 (abi3 wheels; built from its sdist on 3.14t, Rust >= 1.89), PyO3 0.29, FastAPI, sqlite3, pytest, GitHub Actions, MkDocs Material. On the DGX: rootless podman 4.9.3, bash, and GNU coreutils `timeout`.

**Spec:** `docs/superpowers/specs/2026-09-28-raggio-performance-design.md`:
- §1 item 4 and §2 order 8 (scope);
- §3 D1 (ADR 0006), D2 (one Dockerfile; G adds the 3.14t/turbovec-sdist path), D6 (experimental; not the default until turbovec ships cp314t wheels; never fork turbovec), D11 (a non-blocking 3.14t CI job), D15 (the SQLite memstatus mutex; G's verdict accounts for it), D16 (`OPENBLAS_NUM_THREADS=1`);
- §3.1, the maintainer's decisions for G: G1 (one DGX measurement session, an ABAB session, runs for up to 90 minutes), G2 (a turbovec sdist that fails to build for 3.14t in p4 is fixed in the builder stage with flags and build dependencies only; a needed source change is drafted upstream in `D:/DEV/_scratch/upstream-drafts/` for the maintainer to post, and G is parked), G3 (3 failed builder-stage attempts, or a fix no allowed Dockerfile line can express although turbovec's source needs no change, stop G and ask the maintainer; ADR 0006 stays Pending; only G2's source-change case parks without asking), G4 (build and fix windows, Task 11, have no time cap; G1's cap applies to measurement sessions only), G5 (the decision's hybrid p99 guard, and CPU per query, which classifies each flat run's fast/slow state and is reported, not gated) and G7 (a c=16 session after each arm session; its tables are reported, not gated);
- §4.1:
  - `/healthz` keys are additive only;
  - `bench/dgx/*` belongs to Plan A, and G adds only a `bench/<name>_probe.py`;
  - the Dockerfile and `tests.yml` are shared: A owns the base and the `OPENBLAS_NUM_THREADS` line, C the `MALLOC_TRIM_THRESHOLD_` line, E the Rust builder, G the 3.14t/turbovec-sdist path;
- §4.2 (`GC_FREEZE`, owner G; `raggio_native` built without abi3, `gil_used = false`);
- §6 (DGX protocol, including the noise band, and the go-ahead rule: any run over about 5 minutes needs the user's go-ahead with a time cap, except build-and-fix windows, whose go-ahead has none per §3.1 G4), §7 G (acceptance) and §8 (probe p4, ran 2026-09-28: 0 G2 fixes, the GIL stays off, SQLite 3.53.1).

Research:
- `docs/superpowers/research/2026-09-28-item4a-freethreading-deps.md` (wheel availability, turbovec's sdist, the laptop's 3.14t numbers);
- `…-item4b-freethreading-code.md` (races, `gc.freeze`, sqlite3 on 3.14t);
- `…-critic.md`;
- `…-dgx-baseline.md`:
  - hybrid QPS: 13.8 concurrent vs 8.6 serial;
  - IVF build at 4 GiB: 213 s;
  - the detach at 8 GiB: 285 s;
  - true-cold start: 128 s.
- `…-dgx-probe-p2-hybrid-native.md` (D15):
  - SQL-only text search on Debian's SQLite 3.40.1 (memstatus on): 27.9 / 36.5 / 18.4 / 16.8 q/s at T = 1 / 2 / 4 / 8;
  - on PBS SQLite 3.50.4, the SQLite of the uv interpreter p2 used: 28.6 / 47.6 / 45.4 / 39.4 q/s, a plateau at about 39–48 q/s. memstatus cannot be switched off there. uv 0.12.19's PBS interpreters (3.12.14, 3.14.7, 3.14.7t), which G's images run, bundle SQLite 3.53.1 (p4); these figures were not re-measured on 3.53.1;
  - `raggio_native` must be rebuilt per interpreter (a non-manylinux wheel, built on the runtime's Debian release);
  - between-server variance: identical code 6–7% apart at IVF warm p50, ≤1.3% on flat; vector concurrent QPS bimodal, 120–225.
- `…-dgx-probe-p3-ivf-fanout.md` (D16): numpy's default scipy-openblas starts 20 busy-spinning threads under search load. They burn about 16 cores and push query threads onto the A725 cores (cpus 0–4, 10–14): serial 4.06–4.08 ms on an X925 core (cpus 5–9, 15–19) against 8.31–8.33 ms on an A725, filtered 32.5 against 69.5 ms. `OPENBLAS_NUM_THREADS=1` removes that bimodality. Rootless podman cannot pin with `--cpuset-cpus`.
- `…-dgx-probe-p4-freethreaded.md` (the 3.14t aarch64 build and concurrency; ran 2026-09-28 19:00–20:10Z on gn100, at the 19a8a2e tree, which has no `raggio_native`, with a stand-in `check_image.py`):
  - the 3.14t image builds on aarch64 with 0 builder-stage fixes, in 28.7 s cold [28.2..29.3] (3 `--no-cache` builds, pre-pulled bases, no uv cache). The dependency step is 17–19 s of that, which is turbovec's compile. The 3.14 GIL image takes 10.6 s. The images are 323.6 MB (3.14t) and 320.9 MB (3.14);
  - turbovec 1.0.0 builds from its sdist as `cp314-cp314t-linux_aarch64` with no build arg: PyPI has only the `cp39-abi3` wheel, so uv falls back to the sdist on its own. `ARG UV_NO_BINARY_PACKAGE` matters only for the C1 arm, whose local 3.14 build is `cp39-abi3-linux_aarch64` because the sdist enables `abi3-py39`;
  - the GIL stays off at import, under pytest, under uvicorn load and in the race runs; the `PYTHONWARNINGS` guard was set in every probe container and never fired, and G keeps it. The suite gave 46 passed, 1 warning on every interpreter;
  - uv 0.12.19's PBS interpreters (3.12.14, 3.14.7, 3.14.7t) bundle SQLite 3.53.1, not 3.50.4;
  - still open, for Task 11: `raggio_native`'s cp314t build, whose compile time nobody has measured, and G's real `check_image.py` on G's real Dockerfile.

## Global Constraints

- **Base.** `main` after Plans A–F have merged (spec §2, order 8).
  - The sandbox base was 19a8a2e with A, B, E and F merged; C and D were absent.
  - Line numbers are orientation only: anchor every edit on the quoted text.
  - The Dockerfile's runtime stage carries A's `ENV OPENBLAS_NUM_THREADS=1` (D16) and C's single trim-threshold `ENV` line (D13), and its builder is E's Rust stage with `ENV PYO3_PYTHON=/usr/local/bin/python3`. Task 6 checks all three before it edits anything.
- **Two interpreters, two venvs.**
  - 3.12 is the default `.venv`. Run it with `uv run pytest -q`.
  - 3.14t lives in `.venv-ft`. Task 3 creates it with `UV_PROJECT_ENVIRONMENT=.venv-ft uv sync --frozen --python 3.14t --extra native`. That builds turbovec from its sdist and `raggio-native` from `native/`, so Rust >= 1.89 must be on `PATH`; Plan E's pin, 1.98.1, is enough.
  - Run 3.14t with:

    ```bash
    UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q
    ```

    It uses `--no-sync`, as the CI job does, so `uv run` never re-resolves that venv.
- **The test module.** Every new test goes in `tests/test_freethreading.py` (UTF-8, LF). Task 1 creates it and each later task appends one section.
  - The counts after Tasks 1–9 are 2, 8, 11, 12, 20, 23, 25, 28 and 29; after Task 12, 30.
  - Task 11 adds one test, `test_the_p4_turbovec_build_fix_stays_in_the_builder_stage`, only if probe p4 needs a builder-stage fix (spec §3.1 G2). The counts are then 30 after Task 11 and 31 after Task 12, and the full suites have one more test from Task 11 on.
  - On 3.12, 1 test skips from Task 3 on and 4 from Task 5 on. These are the GIL-guard tests, which need a free-threaded build.
  - On 3.14t nothing in the module skips.
  - Plan B's Task 6 test, `test_http_searches_at_c16_and_racing_a_collection_delete`, postdates the sandbox: the sandbox's absolute full-suite counts below are one lower than on the real tree. The relative counts (`N more than the base`) stand. That test has never run on 3.14t; if it fails there, "Any other failure is a free-threading bug: stop" applies.
  - The full suite grows by the same numbers over whatever the base has. On 3.14t, the base's bench-group test modules skip (orjson has no free-threaded wheel).
- **The GIL guard string** is exactly `error:The global interpreter lock (GIL) has been enabled:RuntimeWarning`.
  - It appears in three places: `docker/check_image.py` `GIL_WARNING_FILTER`, the runtime `ENV PYTHONWARNINGS`, and the CI job's env.
  - `PYTHON_GIL=0` and `-X gil=0` appear nowhere except in comments and in the check that refuses them.
- **`/healthz`** gains `gil_enabled`; `status`, `resident_collections` and `bm25` stay (spec §4.1).
- **`GC_FREEZE`** is `0` (default) or `1`. Anything else raises `ValueError` from `Settings()` at startup.
- **The default image stays `PYTHON=3.12`** (spec D6).
  - turbovec is never forked, patched or vendored, and no other turbovec version is pinned.
  - If turbovec's sdist does not build for cp314t in probe p4 (spec §3.1 G2), Task 11 classifies the cause from the build log. A missing system build dependency, a Rust or maturin toolchain version, or a missing interpreter-specific build flag or env is fixed in the Dockerfile's builder stage only, in at most 3 rebuild attempts, each recorded in the task log `D:/DEV/_scratch/dgx/planG/p4-attempts.md`. If 3 attempts do not fix it, or no builder-stage line can express the fix although turbovec's source needs no change, Task 11 stops and asks the user, and ADR 0006 stays `Pending:` until the user decides (spec §3.1 G3). A cause that only a change to turbovec's own source would fix gets an upstream issue draft in `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md`, which the maintainer posts (never the implementer, D7), and G is parked in ADR 0006. That is the only case that parks without asking (spec §3.1 G3).
  - `pyproject.toml` and `uv.lock` do not change.
  - Making 3.14t the default, and choosing `GC_FREEZE`'s default, are out of scope.
- **Shared files** (spec §4.1).
  - The Dockerfile and `.github/workflows/tests.yml` gain only the lines quoted in Tasks 6 and 7. The one exception is Task 11's probe p4 fix, only if p4 needs one: builder-stage lines of the kinds Task 11 Step 6 lists, and nothing outside the builder stage (spec §3.1 G2).
  - `bench/dgx/*` is Plan A's and is not edited. G adds `bench/unicode_drift_probe.py`.
- **DGX helpers** live outside the repo and are never committed.
  - On the laptop they are in `D:/DEV/_scratch/dgx/planG/`. On gn100 they are in `~/raggio-bench/`, all prefixed `g-`.
  - Fetched results go to `D:/DEV/_scratch/dgx/planG/results/`.
- **DGX rules** (spec §6).
  - Every ssh is `ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100`. `SSH_CONFIG` is the maintainer's local ssh config for gn100: set in the environment, never committed.
  - Local paths given to `scp` are relative, after a `cd`, because `scp` would read `D:` as a host name.
  - Only the `bench-tv` container is ever stopped or removed (`podman rm -f bench-tv`). Never touch `bench-wv` or its volume.
  - Never `pkill -f`. Long runs are `setsid -f` into a log, then polled. A stop recipe finds the PIDs first, leaves out the shell that runs it, and kills by PID.
  - Any DGX run over about 5 minutes needs the user's explicit go-ahead in the session. Tasks 11 and 12 are last, and each starts by asking. Task 12's go-ahead names a time cap (spec §6). Task 11's names none: spec §6 exempts build-and-fix windows from the cap (§3.1 G4).
  - One DGX measurement session, an ABAB session, runs for at most 90 minutes (spec §3.1 G1): Task 12 passes `90m` to `g-abab.sh` as the per-session cap, and `g-session.sh` enforces it with `timeout`. Each c=16 session (spec §3.1 G7) is a measurement session of its own, with its own 90 minutes.
  - Build and fix windows have no time cap (spec §3.1 G4). Task 11, the 3.14t image build with up to 3 fix attempts, is one: none of its steps stops for time. It ends at Step 10, after Step 9, a Park or a stop-and-ask at p4, a stop one of its steps names for a failure, or the user's request for gn100 back.
    - A stall is not a time cap. A deploy or build whose log has not grown for 30 consecutive minutes is hung: Task 11 stops it with Step 1's stop recipe and asks the user (the stall guard in Steps 4 and 6), and the hung build uses none of the 3 attempts.
  - No `sudo`. Never touch `~/turborag` except to read the corpus through Plan A's `deploy.sh` symlink.
- **Spec lines this plan is held to, verbatim:**
  - "Not the default until turbovec publishes cp314t wheels. raggio never maintains a turbovec fork." (D6)
  - "Concurrent hybrid is capped by SQLite's global memstatus malloc mutex, not only by the GIL." "Memstatus cannot be switched off there (`sqlite3_config` is not exported)." "Every DGX row records `sqlite3.sqlite_version`." "G's verdict must account for the SQLite mutex. Building a custom SQLite is out of scope." (D15)
  - "Set `ENV OPENBLAS_NUM_THREADS=1` in the image." "This is the main DGX noise source" (D16)
  - "It is built without abi3 and declares `gil_used = false`." (§4.2, the native module)
  - "Every run starts from a fresh container of the image under test." (§6)
  - "Vector concurrent QPS is bimodal (120–225) across identical configs, so it always needs at least 3 runs." (§6)
  - "The band must include **between-server** variance: every A/B arm runs on a fresh container. Identical code measured 6–7% apart at IVF warm p50 and ≤1.3% apart on flat (p2)." (§6)
  - "Both arms of an A/B run with the same `OPENBLAS_NUM_THREADS`, and every row records it. Rootless podman cannot pin with `--cpuset-cpus`. When a single-thread latency needs attributing, pin in-process with `os.sched_setaffinity` (X925 = cpus 5–9, 15–19)." (§6)
  - "Claim a win only if it exceeds the band." (§6)
  - "Every row also states its memory cap and `sqlite3.sqlite_version` (D15)." "Arms of one A/B share the same regime." (§6)
  - "G's decision rule gets a p99 guard and CPU per query. A `Hybrid QPS concurrent` win counts only when the hybrid p99 under concurrency is present and not `worse` in either cross table (A1 → B1, A2 → B2). CPU per query is recorded for every run and classifies each flat run's fast/slow state, so the bimodal flat `QPS concurrent` row (p4: ~134 vs ~250 QPS on every interpreter) cannot fake a win through an uneven split. CPU per query is reported, not gated." (§3.1 G5)
  - "Follow-up to G5: each ABAB arm session (A1, B1, A2, B2) is followed, on the same boot and image, by a **c=16 session**. It repeats that session's bench runs with `bench.py --concurrency 16`, in a fresh container per run. Each c=16 session is a measurement session with its own 90-minute cap (G1). Its pair tables (`A1 → B1 (c=16)`, `A2 → B2 (c=16)`, plus the same between-server checks) carry the usual verdicts and are reported in ADR 0006, not gated: the decision still reads the c=8 tables. C1 and D1 run at c=8 only. p4 saw 3.14t's hybrid gain only at c=16." (§3.1 G7)
  - "Follow-up to G7: the c=16 sessions' GIL and log checks gate like the c=8 sessions'. ADR 0006's `GIL and logs` table has a row for every session, the c=16 ones included. A Traceback or GIL line in any session's container logs, or a 3.14t `/healthz` answer without `gil_enabled` false, parks B. Only the c=16 speed tables are reported and not gated (G7). A crash under load is a correctness failure, which G exists to catch." (§3.1 G8)
  - "Follow-up to G5: an uneven or unknown fast/slow split fakes neither a win nor a loss. When either cross pair's flat runs split unevenly, or a flat run has no CPU per query, `QPS concurrent` and `p95 under concurrency (ms)`, both measured in those runs, count as neither a win nor a miss. The win check already skips `QPS concurrent` then; the "worse in both pairs" check skips both rows too." (§3.1 G9)
  - "**G:** the full test suite green on 3.14t with the GIL guard. QPS/latency ABAB on one boot. A hybrid win needs a hybrid p99 present and not `worse` in either cross table, and every run records CPU per query (§3.1 G5); an uneven or unknown fast/slow split counts neither way (§3.1 G9). Each ABAB session is followed by a c=16 session, whose tables are reported and not gated (§3.1 G7); its GIL and log checks gate like the others (§3.1 G8). The report states whether to continue; making it the default is out of scope." (§7)
- **How the ABAB meets them** (Tasks 9, 10 and 12):
  - The gate reads the CPU-bound flat rows. `QPS concurrent` (vector search and its rescoring) can win. `Hybrid QPS concurrent` can win only when A and B run the same `sqlite_version`. A hybrid row that does not scale, or gets worse, is recorded as SQLite-bound and never parks the evaluation.
  - A `Hybrid QPS concurrent` win also needs `Hybrid p99 under concurrency (ms)` present and not `worse` in either cross table (spec §3.1 G5). A `QPS concurrent` win needs the flat runs' fast/slow states, read from CPU per query, to split evenly on both sides of each cross pair. CPU per query is reported for every run and never gated.
  - Each arm session is followed by a c=16 session (spec §3.1 G7). Its four speed tables are reported in ADR 0006 and the decision does not read them.
  - The c=16 sessions' checks gate (spec §3.1 G8): `GIL and logs` has a row for each of the eight sessions, and a Traceback, a GIL line or a `/healthz` answer without `gil_enabled` false in any of them parks B.
  - An uneven or unknown fast/slow split counts neither way (spec §3.1 G9): `QPS concurrent` and `p95 under concurrency (ms)` are skipped by both the win check and the "worse in both pairs" check then.
  - Every session runs `FLAT_RUNS=3`, so the flat concurrent-QPS rows have 3 counted runs per side; IVF has 3 (`IVF_RUNS`). A table with Plan A's `Reported, not claimed` note fails the Task 12 test.
  - Every run is a fresh container: `session.sh` replaces it for every run, and each session's `g-ensure-ivf.sh` starts its own. The between-server variance shows in A1 → A2 and B1 → B2. A win row that moves beyond the band there is not claimed.
  - Every image inherits A's `ENV OPENBLAS_NUM_THREADS=1` from the one Dockerfile. `image.json` records it, and the Task 12 test requires `openblas_num_threads=1` on both `Regime` lines of every table.
  - Every table states its regime above the rows: `page_cache=host-warm`, `memory=4g`, the swap, `concurrency` (16 in the `(c=16)` tables, 8 in the others), `python`, `sqlite_version` and `openblas_num_threads`. The arms of a table may differ only in `python` and `sqlite_version`, and two sessions of one arm in nothing.
- **Windows.** Local `podman` in Git Bash needs `MSYS_NO_PATHCONV=1`.
- **Commit trailer**, on every commit: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

Five failure modes the spec implies, most likely first. Each has a pin test in its owning task. Every pin was checked in the sandbox against the named mutation: it passes on the task's code and fails on the mutated code. The exception is the part of item 3 added for D15, D16 and the per-interpreter `raggio_native`, after the DGX probes. The Self-Review lists it as not yet validated.

1. **Zombie meta.db connections on 3.14t (Task 4).**
   - What happens: every thread that served a read opened its own connection, and `stop()` closes them all from another thread. With biased reference counting, a statement another thread cached is freed only when that thread next runs Python. `close()` therefore leaves a zombie that keeps `meta.db`, `-wal` and `-shm` open. On Windows, deleting the collection then fails.
   - What must hold: both meta.db connection sites, `open_meta_db` and `Collection._rdb`, pass `cached_statements=SQLITE_CACHED_STATEMENTS`, which is 0 on free-threaded builds.
   - Pin: `test_stop_really_closes_meta_db_after_reads_on_many_threads`. It is red only on 3.14t.
   - Mutation: the unmodified connect calls (Task 4's red run), which fails with `['meta.db', 'meta.db-shm', 'meta.db-wal'] == ['meta.db']`. Plan B's `test_deleted_collection_leaves_no_files_and_its_name_can_be_reused` goes from red to green on 3.14t with the same change.
2. **A lazily imported extension re-enables the GIL (Tasks 5, 6).**
   - What happens: CPython only emits a `RuntimeWarning`, and anything that swallows warnings hides it.
   - What must hold: the builder's check imports every compiled module and reads the GIL state after each import. The runtime image turns the warning into an error.
   - Pins: `test_check_image_names_the_module_that_enabled_the_gil` and `test_the_image_warning_filter_fails_a_real_gil_re_enable`. Both use CPython's own `_testmultiphase` extension, copied under the name of its one module that declares no free-threading support. The third pin is `test_the_runtime_image_fails_loudly_on_a_gil_re_enable`.
   - Mutations: `if False:` in place of the per-import check in `import_all`, and the Dockerfile without the `ENV PYTHONWARNINGS` line (Task 6's red run).
3. **A difference between the arms that is not free threading, blamed on it (Tasks 5, 6, 8, 10).**
   - What happens, in four ways:
     - Unicode: 3.12 has Unicode 15.0.0 and 3.14 has 16.0.0. The tokenizer runs on `unicodedata`, and `raggio_native` builds its tables from the interpreter it is compiled for.
     - SQLite (D15): concurrent hybrid is capped by SQLite's global memstatus mutex, and the two interpreters may bundle different SQLite builds (p4 found 3.53.1 in uv 0.12.19's 3.12.14 and 3.14.7t, but a different uv or interpreter pin can change it). A hybrid leg that does not scale on B would read as "free threading failed".
     - BLAS threads (D16): one arm with OpenBLAS spinners runs its query threads on the slow A725 cores.
     - The native scorer: an abi3 or other-ABI `raggio_native` cannot load on 3.14t, and raggio silently falls back to the Python scorer.
   - What must hold:
     - the drift dump folds each code point exactly as the tokenizer does, and the diff tells newly assigned code points from changed ones;
     - the decision never parks on a SQLite-bound row, and counts a hybrid win only when A and B run the same `sqlite_version`;
     - every table of the ABAB has `openblas_num_threads=1` on both sides;
     - an uneven or unknown fast/slow split makes `QPS concurrent` and `p95 under concurrency (ms)` count neither as a win nor as a miss (`flat-split-uneven-worse`, spec §3.1 G9);
     - a Traceback or GIL line in a `-c16` session parks B like one in any other session (`c16-traceback`, spec §3.1 G8), while the `(c=16)` speed tables gate nothing (`c16-not-gated`);
     - the image build fails unless `raggio_native` carries the interpreter's own extension suffix and `bm25` is `native`.
   - Pins:
     - `test_drift_probe_dump_records_what_the_tokenizer_does_per_code_point` and `test_drift_probe_diff_separates_new_code_points_from_changed_ones` (Task 8);
     - `test_check_image_finds_every_compiled_module_it_can_import`, which also asserts `built_for_this_interpreter` (Task 5);
     - `g-check.py`'s `sqlite-bound`, `hybrid-win`, `hybrid-other-sqlite`, `hybrid-p99-worse`, `flat-split-uneven`, `flat-split-uneven-worse`, `flat-split-even`, `cpu-missing`, `c16-not-gated`, `c16-traceback`, `blas-differs` and `thin` scenarios, which run `test_adr_0006_records_the_dgx_abab_and_its_decision` (Task 10).
   - Mutations: `dump` without `_fold`, and `if False:` in place of the `Cn` newly-assigned test, which counts new code points as changed. For the D15/D16 part: `SQLITE_BOUND = ()`, which counts the hybrid win in `hybrid-other-sqlite`, and dropping the `openblas_num_threads` assert, which passes `blas-differs`.
4. **`GC_FREEZE` keeps an evicted or deleted collection alive (Task 2).**
   - What happens: the freeze runs after `resume_pending`, so a collection a replayed job loaded is in the frozen heap. The collector skips frozen objects, so a collection that only a cycle collection could free would stay until shutdown.
   - What must hold: deleting it frees it by reference counting alone.
   - Pin: `test_gc_freeze_still_frees_a_collection_loaded_before_the_freeze`.
   - Mutation: `self._cycle = self` in `Collection.__init__`. It fails on 3.12 and on 3.14t.
5. **A stale `/healthz` (Task 1).**
   - What happens: a lazy import can re-enable the GIL after startup.
   - What must hold: `/healthz` reads the state on every request.
   - Pin: `test_healthz_reads_the_gil_state_on_every_request`.
   - Mutation: compute `_gil_enabled()` once in `create_app`.

---

## File Structure

| Path | Change | Task |
|---|---|---|
| `src/raggio/app.py` | Modify, as follows: | 1, 2 |
| | `import sys`, `_gil_enabled()`, the `gil_enabled` key of `/healthz` | 1 |
| | `import gc`, the lifespan's freeze and unfreeze | 2 |
| `src/raggio/config.py` | Modify: `Settings.gc_freeze` | 2 |
| `.gitignore`, `.dockerignore` | Modify: `.venv-ft` | 3 |
| `src/raggio/store.py` | Modify: `import sysconfig`, `SQLITE_CACHED_STATEMENTS`, and both meta.db `sqlite3.connect` calls | 4 |
| `docker/check_image.py` | Create: the build-time image check | 5 |
| `Dockerfile` | Modify: the header comment, `ARG UV_NO_BINARY_PACKAGE`, the check step, the runtime `ENV PYTHONWARNINGS` | 6 |
| | Only if probe p4 needs it: the turbovec build fix, builder-stage lines only (spec §3.1 G2) | 11 |
| `.github/workflows/tests.yml` | Modify: the `freethreaded` job | 7 |
| `bench/unicode_drift_probe.py` | Create: `dump`, `diff` and `scan` | 8 |
| `docs/adr/0006-free-threaded-python.md` | Create. Task 12 replaces its `Pending:` Verification; Task 11 replaces it instead only if G parks at probe p4 | 9, 11 (p4 Park only), 12 |
| `README.md`, `docs/getting-started.md`, `docs/api.md` | Modify: the build arg, the `GC_FREEZE` row, the `/healthz` example | 9 |
| `tests/test_freethreading.py` | Create in Task 1; each later task appends one section (Task 11 only if probe p4 needs a builder-stage fix) | 1–9, 11 (p4 fix only), 12 |
| `D:/DEV/_scratch/dgx/planG/` (outside the repo) | `g-check.py`, `t12-section.py`, `g-render.py`, `g-decide.py`, `g-image-facts.py`, `g-session.sh`, `g-ensure-ivf.sh`, `g-abab.sh`, `g-build.sh`, `g-facts.sh`, `g-fanout.sh` | 10 |
| `D:/DEV/_scratch/dgx/planG/g.env`, `deploy.log`, `deploy.stall`, `g-build.stall`, `results/` (outside the repo) | The pinned SHA and arms; the deploy log; the stall guard's state (Steps 4 and 6); the fetched outputs | 11, 12 |
| `D:/DEV/_scratch/dgx/planG/hung-<UTC time>.log` (outside the repo) | Only if the stall guard fires: the hung deploy's or build's log. `p4-attempts.md` then also holds its `## p4 hung build, not counted` entry | 11 |
| `D:/DEV/_scratch/dgx/planG/p4-attempts.md`, `g-build-p4-<k>.log` (outside the repo) | Only if turbovec's sdist does not build, or a deploy or build hangs: the task log of p4 builds, and each failed build's log | 11 |
| `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md` (outside the repo) | Only if a turbovec source change is needed: the upstream issue draft, which the maintainer posts | 11 |
| gn100 `~/raggio-bench/g-*` | The scripts, the images' facts, the drift files, the fan-out outputs, the sessions and `g-verification.md` | 11, 12 |

### Task 1: Report `gil_enabled` in `/healthz`

**Files:**
- Modify: `src/raggio/app.py`: the imports, a new `_gil_enabled()` before `# ---- request models ----`, and `healthz()`
- Create: `tests/test_freethreading.py`

**Interfaces:**
- Consumes:
  - `create_app(settings: Settings | None = None, embedder_factory=None) -> FastAPI`;
  - the `/healthz` body, as left by Plan E: `{"status": "ok", "resident_collections": [...], "bm25": manager.bm25_backend()}`;
  - `sys._is_gil_enabled() -> bool` (CPython 3.13+).
- Produces:
  - `raggio.app._gil_enabled() -> bool`: `True` on every GIL build, and `False` only on a free-threaded build that is running without the GIL;
  - the `/healthz` key `"gil_enabled": bool`, read on every request;
  - the test module's helpers, which later tasks use: `DIM = 8`, `ROOT`, `gil_enabled()`, `FakeEmbedder`, and the `make_app` fixture (sets `ROOT_API_KEY=root-key` and `DATA_DIR`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_freethreading.py`. Its imports and docstring already cover the whole module, so the later tasks only append:

```python
"""Free-threaded CPython 3.14t (ADR 0006, spec D6): the GIL state /healthz reports, the
GC_FREEZE knob, meta.db connections that really close on a free-threaded build, the
build-time image check and the GIL guard (the guard skips on GIL builds), the Dockerfile's
3.14t path, the non-blocking 3.14t CI job, the Unicode drift probe and the docs."""
import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import zlib
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from raggio.app import create_app

DIM = 8
ROOT = Path(__file__).resolve().parent.parent


def gil_enabled() -> bool:
    # sys._is_gil_enabled exists from 3.13 on; every older build runs with the GIL
    return getattr(sys, "_is_gil_enabled", lambda: True)()


class FakeEmbedder:
    """Deterministic: same text -> same unit vector."""

    async def embed(self, texts):
        out = []
        for t in texts:
            v = np.random.default_rng(zlib.crc32(t.encode())).standard_normal(DIM)
            out.append((v / np.linalg.norm(v)).tolist())
        return out

    async def aclose(self):
        pass


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return lambda: create_app(embedder_factory=lambda cfg: FakeEmbedder())


# ---- /healthz ----


def test_healthz_reports_whether_the_gil_is_enabled(make_app):
    with TestClient(make_app()) as client:
        body = client.get("/healthz").json()
    assert body["gil_enabled"] is gil_enabled()
    # additive: the keys other plans report stay
    assert body["status"] == "ok" and body["resident_collections"] == [] and "bm25" in body


def test_healthz_reads_the_gil_state_on_every_request(make_app, monkeypatch):
    # a lazily imported extension can re-enable the GIL after startup: /healthz must say
    # so from then on, not repeat what it saw first
    state = {"enabled": False}
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: state["enabled"], raising=False)
    with TestClient(make_app()) as client:
        assert client.get("/healthz").json()["gil_enabled"] is False
        state["enabled"] = True
        assert client.get("/healthz").json()["gil_enabled"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_freethreading.py`

Expected: `2 failed`. Both fail with `KeyError: 'gil_enabled'`.

- [ ] **Step 3: Implement**

3a. In `src/raggio/app.py`, add `import sys` after `import json`:

```python
import asyncio
import hmac
import json
import sys
from contextlib import asynccontextmanager
```

3b. In `src/raggio/app.py`, insert the function right before the line `# ---- request models ----`, with two blank lines on each side:

```python
def _gil_enabled() -> bool:
    """False only on a free-threaded build (3.13t+) that is really running without the GIL."""
    return getattr(sys, "_is_gil_enabled", lambda: True)()
```

3c. In `healthz()` in `src/raggio/app.py`, add the key after `"bm25": manager.bm25_backend(),`. The result is:

```python
            "status": "ok",
            "resident_collections": list(manager.resident),
            "bm25": manager.bm25_backend(),
            "gil_enabled": _gil_enabled(),
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `2 passed`.

Run: `uv run pytest -q`. Expected: all green, 2 more tests than the base (sandbox: `284 passed`). Every absolute sandbox count in this plan is one lower than on the real tree, which has Plan B's Task 6 c=16 test (Global Constraints); the relative counts stand.

- [ ] **Step 5: Commit**

```bash
git add src/raggio/app.py tests/test_freethreading.py
git commit -m "Report gil_enabled in /healthz" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The `GC_FREEZE` knob

**Files:**
- Modify: `src/raggio/config.py`: `Settings.__init__`, after `self.ivf_search_threads = ...`
- Modify: `src/raggio/app.py`: the imports, and the lifespan
- Modify: `tests/test_freethreading.py` (append a section)

**Interfaces:**
- Consumes:
  - `Settings.__init__(self) -> None`, which reads env vars; its last line after Plan F is `self.ivf_search_threads = threads if threads > 0 else default_ivf_search_threads()`;
  - the lifespan in `create_app`: `await manager.resume_pending()` before `yield`, and `await manager.shutdown()` after it;
  - `CollectionManager.resume_pending()`, `CollectionManager.shutdown()` and `CollectionManager.touch(name) -> Collection`;
  - `DELETE /collections/{name}`.
- Produces:
  - `Settings.gc_freeze: bool`, from the env var `GC_FREEZE`: `"0"` or unset or empty means off, `"1"` means on, and anything else raises `ValueError` naming `GC_FREEZE`;
  - with `gc_freeze`, the lifespan calls `gc.collect()` then `gc.freeze()` after `resume_pending()`, and `gc.unfreeze()` after `shutdown()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_freethreading.py`:

```python
# ---- GC_FREEZE ----


def test_gc_freeze_defaults_off_and_rejects_other_values(monkeypatch):
    from raggio.config import Settings

    monkeypatch.delenv("GC_FREEZE", raising=False)
    assert Settings().gc_freeze is False
    monkeypatch.setenv("GC_FREEZE", "1")
    assert Settings().gc_freeze is True
    for bad in ("true", "yes", "2"):
        monkeypatch.setenv("GC_FREEZE", bad)
        with pytest.raises(ValueError, match="GC_FREEZE"):
            Settings()


@pytest.mark.parametrize("knob, expected", [
    ("1", ["resume_pending", "collect", "freeze", "shutdown", "unfreeze"]),
    ("0", ["resume_pending", "shutdown"]),
    ("", ["resume_pending", "shutdown"]),
])
def test_gc_freeze_runs_after_resume_pending_and_unfreezes_after_shutdown(
    make_app, monkeypatch, knob, expected
):
    import gc

    from raggio.store import CollectionManager

    calls = []
    real_resume, real_shutdown = CollectionManager.resume_pending, CollectionManager.shutdown

    async def resume_pending(self):
        calls.append("resume_pending")
        await real_resume(self)

    async def shutdown(self):
        await real_shutdown(self)
        calls.append("shutdown")

    monkeypatch.setattr(CollectionManager, "resume_pending", resume_pending)
    monkeypatch.setattr(CollectionManager, "shutdown", shutdown)
    for name in ("collect", "freeze", "unfreeze"):
        monkeypatch.setattr(gc, name, lambda *a, _n=name, **k: calls.append(_n) or 0)
    monkeypatch.setenv("GC_FREEZE", knob)
    with TestClient(make_app()):
        pass
    assert calls == expected


def test_gc_freeze_freezes_the_startup_heap_but_still_collects_new_cycles(
    make_app, monkeypatch
):
    import gc
    import weakref

    class Node:
        pass

    monkeypatch.setenv("GC_FREEZE", "1")
    gc.unfreeze()
    with TestClient(make_app()) as client:
        assert gc.get_freeze_count() > 0
        assert client.get("/healthz").status_code == 200
        a, b = Node(), Node()
        a.other, b.other = b, a
        alive = weakref.ref(a)
        del a, b
        gc.collect()
        assert alive() is None  # a cycle allocated after the freeze is still collected
    assert gc.get_freeze_count() == 0  # unfrozen on shutdown


def test_gc_freeze_still_frees_a_collection_loaded_before_the_freeze(make_app, monkeypatch):
    # the freeze runs after resume_pending, so a collection a replayed job loaded is part
    # of the frozen heap. Deleting it must still free it and its index: reference counting
    # does that, and the collector, which skips frozen objects, is never needed
    import gc
    import time
    import weakref

    from raggio.store import CollectionManager

    root = {"x-api-key": "root-key"}
    unit = (np.ones(DIM) / np.sqrt(DIM)).tolist()
    with TestClient(make_app()) as client:
        assert client.post("/collections", headers=root, json={"name": "kb", "dim": DIM}).status_code == 201
        job = client.post("/collections/kb/documents", headers=root, json={"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": unit}]}]}).json()["job_id"]
        while client.get(f"/collections/kb/jobs/{job}", headers=root).json()["status"] != "done":
            time.sleep(0.01)
    loaded = []
    real_resume = CollectionManager.resume_pending

    async def resume_pending(self):
        await real_resume(self)
        loaded.append(weakref.ref(await self.touch("kb")))  # as a replayed job's load does

    monkeypatch.setattr(CollectionManager, "resume_pending", resume_pending)
    monkeypatch.setenv("GC_FREEZE", "1")
    gc.unfreeze()
    with TestClient(make_app()) as client:
        assert gc.get_freeze_count() > 0 and loaded[0]() is not None
        assert client.delete("/collections/kb", headers=root).status_code == 200
        gc.collect()
        assert loaded[0]() is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_freethreading.py`

Expected: `4 failed, 4 passed`. The failures are:
- `test_gc_freeze_defaults_off_and_rejects_other_values`, with `AttributeError: 'Settings' object has no attribute 'gc_freeze'`;
- `test_gc_freeze_runs_after_resume_pending_and_unfreezes_after_shutdown[1-expected0]`, because the calls lack `collect`, `freeze` and `unfreeze`;
- `test_gc_freeze_freezes_the_startup_heap_but_still_collects_new_cycles` and `test_gc_freeze_still_frees_a_collection_loaded_before_the_freeze`, both on `gc.get_freeze_count() > 0`.

The `0` and empty-string cases of the parametrized test pass already, because the unmodified lifespan never touches `gc`.

- [ ] **Step 3: Implement**

3a. In `src/raggio/config.py`, append to `Settings.__init__`, after the line `self.ivf_search_threads = threads if threads > 0 else default_ivf_search_threads()`, at the same indentation:

```python
        # "1" collects and freezes the startup heap before serving (ADR 0006): a
        # free-threaded build's stop-the-world collections then skip it; "0" = off
        gc_freeze = os.environ.get("GC_FREEZE") or "0"
        if gc_freeze not in ("0", "1"):
            raise ValueError(f"GC_FREEZE must be '0' or '1', not {gc_freeze!r}")
        self.gc_freeze = gc_freeze == "1"
```

If Plan C or D appended settings after `ivf_search_threads`, append after the last of them instead.

3b. In `src/raggio/app.py`, add `import gc` after `import asyncio`:

```python
import asyncio
import gc
import hmac
```

3c. In the lifespan in `create_app` in `src/raggio/app.py`, add the freeze after `await manager.resume_pending()` and the unfreeze after `await manager.shutdown()`. The result is:

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.resume_pending()
        if settings.gc_freeze:
            # after resume_pending: what it loaded is startup heap too. A frozen
            # collection that is evicted or deleted is still freed, by reference counting
            gc.collect()
            gc.freeze()
        housekeeping = asyncio.create_task(manager.housekeeping())
        yield
        housekeeping.cancel()
        await manager.shutdown()
        if settings.gc_freeze:
            gc.unfreeze()
```

Only the two `if settings.gc_freeze:` blocks are new. If Plan C changed the lines in between, keep its lines and add only the two blocks.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `8 passed`.

Run: `uv run pytest -q`. Expected: all green, 8 more tests than the base (sandbox: `290 passed`).

- [ ] **Step 5: Commit**

```bash
git add src/raggio/config.py src/raggio/app.py tests/test_freethreading.py
git commit -m "Add the GC_FREEZE knob" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The local 3.14t venv, and the GIL guard in the suite

**Files:**
- Modify: `.gitignore`, `.dockerignore`
- Modify: `tests/test_freethreading.py` (append a section)

**Interfaces:**
- Consumes: uv's `UV_PROJECT_ENVIRONMENT`; a uv-managed CPython 3.14t (`--python 3.14t`); Rust >= 1.89 on `PATH`, for turbovec's sdist and for `raggio-native`.
- Produces:
  - the `.venv-ft` convention: the 3.14t venv next to `.venv`, kept out of git and out of the image's build context;
  - the test helpers `FT: bool` (a free-threaded build) and `needs_ft` (skips on GIL builds);
  - two guard tests. One fails whenever `PYTHON_GIL=0` or `-X gil=0` is set. The other, on a free-threaded build, fails if the GIL is on after pytest has imported every test module and run the tests before it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_freethreading.py`:

```python
# ---- the local free-threaded venv and the GIL guard ----


def test_the_314t_venv_stays_out_of_git_and_the_image_context():
    # UV_PROJECT_ENVIRONMENT=.venv-ft sits next to .venv in the checkout; podman build .
    # would otherwise send it along
    assert ".venv-ft/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".venv-ft" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


FT = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
needs_ft = pytest.mark.skipif(not FT, reason="the GIL guard needs a free-threaded build")


def test_the_suite_never_runs_with_the_gil_check_switched_off():
    # PYTHON_GIL=0 / -X gil=0 keep the GIL off even for an extension that needs it: the
    # free-threaded job would pass while hiding exactly what it exists to catch
    assert os.environ.get("PYTHON_GIL") != "0" and sys._xoptions.get("gil") != "0"


@needs_ft
def test_the_gil_is_still_disabled():
    # pytest imported every test module (raggio, raggio_native, turbovec, numpy, fastapi,
    # ...) before running any test, and the tests before this one ran on this interpreter
    assert sys._is_gil_enabled() is False
```

- [ ] **Step 2: Create the 3.14t venv and run the tests to verify they fail**

Create the venv. uv downloads CPython 3.14t if it is absent, builds turbovec from its sdist and `raggio-native` from `native/`, and does not touch `uv.lock`. That takes a few minutes cold, and about 20 s with a warm uv cache:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft uv sync --frozen --python 3.14t --extra native
UV_PROJECT_ENVIRONMENT=.venv-ft uv run --no-sync python -c "import sys, sysconfig; print(sys.version.split()[0], bool(sysconfig.get_config_var('Py_GIL_DISABLED')), sys._is_gil_enabled())"
```

Expected: the second command prints `3.14.<patch> True False`, for example `3.14.7 True False`.

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `1 failed, 9 passed, 1 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `1 failed, 10 passed`. On both interpreters the failure is `test_the_314t_venv_stays_out_of_git_and_the_image_context`, an `AssertionError` on `.gitignore`.

The two guard tests pass already. Confirm that each one catches what it guards, by running them with the guard violated:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync python -X gil=1 -m pytest -q tests/test_freethreading.py -k "gil_check or still_disabled"
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHON_GIL=0 PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py -k "gil_check or still_disabled"
```

Expected: `1 failed, 1 passed` each time.
- The first command fails `test_the_gil_is_still_disabled`.
- The second fails `test_the_suite_never_runs_with_the_gil_check_switched_off`.

- [ ] **Step 3: Implement**

3a. In `.gitignore`, add `.venv-ft/` after `.venv/`:

```text
.venv/
.venv-ft/
__pycache__/
```

3b. In `.dockerignore`, add `.venv-ft` after `.venv`:

```text
.git
.venv
.venv-ft
tests
```

Leave every other line of both files as it is.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `10 passed, 1 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `11 passed`.

Run: `uv run pytest -q`. Expected: all green, with 10 more passed and 1 more skipped than the base (sandbox: `292 passed, 1 skipped`).

Run the full suite on 3.14t:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q
```

Expected: green, except where a base test needs Task 4. The modules that need the `bench` group skip, because orjson has no free-threaded wheel. The sandbox, on Windows, gave `1 failed, 283 passed, 2 skipped`. The failure was Plan B's `test_deleted_collection_leaves_no_files_and_its_name_can_be_reused`, which Task 4 fixes. Any other failure is a free-threading bug: stop and investigate it before going on.

- [ ] **Step 5: Commit**

```bash
git add .gitignore .dockerignore tests/test_freethreading.py
git commit -m "Keep the 3.14t venv out of git and guard the GIL" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: meta.db connections that really close on a free-threaded build

**Files:**
- Modify: `src/raggio/store.py`: the imports, the new `SQLITE_CACHED_STATEMENTS` before `def open_meta_db`, the `sqlite3.connect` in `open_meta_db`, and the `sqlite3.connect` in `Collection._rdb`
- Modify: `tests/test_freethreading.py` (append a section)

**Interfaces:**
- Consumes:
  - `open_meta_db(path: Path, tokenizer: str = "unicode61") -> sqlite3.Connection`;
  - `Collection._rdb(self) -> sqlite3.Connection`, Plan B's per-thread read connection, which `Collection.stop()` closes from another thread;
  - `CollectionManager(settings, embedder_factory)`, `create_collection(name, dim, bit_width, model, base_url, collection_key)`, `touch(name)`, `shutdown()` and `_dir(name) -> Path`;
  - `Collection.enqueue(payload) -> int`, `pending_jobs() -> int`, `list_records(scope, filter, sort, limit, offset)` and `get_document(doc_id)`.
- Produces: `store.SQLITE_CACHED_STATEMENTS: int`, which is `0` on a free-threaded build and `128` otherwise. Every meta.db connection passes it as `cached_statements=`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_freethreading.py`:

```python
# ---- meta.db connections close for real on a free-threaded build ----


def test_stop_really_closes_meta_db_after_reads_on_many_threads(tmp_path, monkeypatch):
    # every thread that served a read opened its own connection, and stop() closes them
    # all from one other thread. On a free-threaded build a statement cached by its
    # owning thread is only freed when that thread next runs Python (biased reference
    # counting), so each cached statement kept its closed connection alive as a zombie:
    # meta.db-wal/-shm stayed on disk and, on Windows, delete could not remove the dir
    import asyncio

    from raggio.config import Settings
    from raggio.store import CollectionManager

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    m = CollectionManager(Settings(), lambda cfg: FakeEmbedder())
    unit = np.ones(DIM) / np.sqrt(DIM)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        await c.enqueue({"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": unit.tolist()}]}]})
        while c.pending_jobs():
            await asyncio.sleep(0.01)
        # the default executor's threads stay alive and idle across shutdown
        await asyncio.gather(*(asyncio.to_thread(c.list_records, "both", None, None, 10, 0)
                               for _ in range(8)))
        await asyncio.gather(*(asyncio.to_thread(c.get_document, "d") for _ in range(8)))
        await m.shutdown()
        return sorted(p.name for p in m._dir("x").glob("meta.db*"))

    assert asyncio.run(run()) == ["meta.db"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `11 passed, 1 skipped`. With the GIL, `close()` really closes, so the new test is a pin there, not a red test.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `1 failed, 11 passed`. `test_stop_really_closes_meta_db_after_reads_on_many_threads` fails with:

```text
assert ['meta.db', 'meta.db-shm', 'meta.db-wal'] == ['meta.db']
```

- [ ] **Step 3: Implement**

3a. In `src/raggio/store.py`, add `import sysconfig` after `import sqlite3`:

```python
import sqlite3
import sysconfig
import threading
```

3b. In `src/raggio/store.py`, insert this right before `def open_meta_db(`, after the class that precedes it (`CollectionConfig` on the sandbox base), with two blank lines on each side:

```python
# meta.db connections are used from many threads and closed from yet another one. On a
# free-threaded build (ADR 0006) a statement another thread cached is only freed when that
# thread next runs Python (biased reference counting), so close() leaves a zombie that keeps
# meta.db, -wal and -shm open: no statement cache there. 128 is sqlite3's own default.
SQLITE_CACHED_STATEMENTS = 0 if sysconfig.get_config_var("Py_GIL_DISABLED") else 128
```

3c. In `open_meta_db`, replace:

```python
    db = sqlite3.connect(path, check_same_thread=False)
```

with:

```python
    db = sqlite3.connect(path, check_same_thread=False,
                         cached_statements=SQLITE_CACHED_STATEMENTS)
```

3d. In `Collection._rdb`, replace:

```python
            db = sqlite3.connect(self.dir / "meta.db", check_same_thread=False)
```

with:

```python
            db = sqlite3.connect(self.dir / "meta.db", check_same_thread=False,
                                 cached_statements=SQLITE_CACHED_STATEMENTS)
```

3e. Check that no meta.db connection was missed:

```bash
grep -n "sqlite3.connect(" src/raggio/*.py
```

On the sandbox base there are four such calls, and two of them keep the default:
- `CollectionManager.__init__`'s catalog connection;
- `resume_pending`'s short-lived `sqlite3.connect(self._dir(name) / "meta.db")`, which is opened, used and closed on one thread.

Any other connection to a collection's `meta.db` that one thread uses and another thread closes needs `cached_statements=SQLITE_CACHED_STATEMENTS`. That includes any such connection Plan C or D added.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `11 passed, 1 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `12 passed`.

Run `uv run pytest -q`. Expected: all green, with 11 more passed and 1 more skipped than the base (sandbox: `293 passed, 1 skipped`).

Run the full suite on 3.14t:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q
```

Expected: all green. Plan B's delete test now passes too (sandbox: `285 passed, 2 skipped`).

- [ ] **Step 5: Commit**

```bash
git add src/raggio/store.py tests/test_freethreading.py
git commit -m "Close meta.db connections for real on free-threaded builds" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The build-time image check

**Files:**
- Create: `docker/check_image.py`
- Modify: `tests/test_freethreading.py` (append a section)

**Interfaces:**
- Consumes:
  - `raggio.app.create_app` and the `/healthz` keys `bm25` (Plan E) and `gil_enabled` (Task 1);
  - the HTTP API: create, ingest, jobs, get, list, the search modes, `POST /collections/{name}/index`, detach and delete, with `IVF_MIN_ROWS = 1024`;
  - uvicorn's `Config(...).load()`;
  - CPython's `sys._is_gil_enabled()`, `sysconfig.get_config_var("Py_GIL_DISABLED")` and `importlib.machinery.EXTENSION_SUFFIXES`.
- Produces `docker/check_image.py`:
  - CLI: `python docker/check_image.py --python <3.12|3.14t|...> [--require-native]`. On success it exits 0 and prints `check_image: Python <version> <free-threaded|with the GIL>, GIL <enabled|disabled>, <n> extension modules imported, bm25=<backend>`. On failure it exits 1, with `check_image: FAILED: <reason>` on stderr.
  - `--require-native` also fails unless every compiled `raggio_native` module that is loaded carries this interpreter's own extension suffix (spec §4.2: built per interpreter, never abi3). The reason reads `raggio_native is not built for this interpreter (<suffix>): [<files>]`.
  - Module API:
    - `GIL_WARNING: str` and `GIL_WARNING_FILTER: str`, which is exactly `error:The global interpreter lock (GIL) has been enabled:RuntimeWarning`;
    - `class CheckFailed(Exception)`;
    - `check_flavour(python: str) -> None`;
    - `extension_modules(site: Path, suffixes=...) -> list[str]`;
    - `built_for_this_interpreter(path: str, suffixes=...) -> bool`: the file ends with `suffixes[0]`, the interpreter's own tag (`.cpython-314t-aarch64-linux-gnu.so`, `.cp312-win_amd64.pyd`, ...), not `.abi3.so` or a bare `.so`;
    - `check_native() -> None`;
    - `import_all(names: list[str]) -> None`;
    - `exercise(require_native: bool) -> dict`;
    - `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_freethreading.py`:

```python
# ---- the build-time image check ----

CHECK_IMAGE = ROOT / "docker" / "check_image.py"
THIS_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}{'t' if FT else ''}"


def load_check_image():
    spec = importlib.util.spec_from_file_location("check_image", CHECK_IMAGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child(args, env=None):
    """A fresh interpreter without the caller's warning filter or GIL override."""
    clean = {k: v for k, v in os.environ.items() if k not in ("PYTHONWARNINGS", "PYTHON_GIL")}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=ROOT,
                          env={**clean, **(env or {})}, timeout=300)


def test_check_image_passes_on_this_interpreter():
    native = ["--require-native"] if os.environ.get("REQUIRE_NATIVE") == "1" else []
    r = child([str(CHECK_IMAGE), "--python", THIS_PYTHON, *native])
    assert r.returncode == 0, r.stderr
    assert f"GIL {'disabled' if FT else 'enabled'}," in r.stdout
    assert "extension modules imported" in r.stdout


@pytest.mark.parametrize("python", [
    f"3.{sys.version_info.minor}{'' if FT else 't'}",  # the other flavour
    f"3.{sys.version_info.minor - 1}{'t' if FT else ''}",  # another minor
])
def test_check_image_rejects_an_interpreter_the_build_arg_did_not_ask_for(python):
    r = child([str(CHECK_IMAGE), "--python", python])
    assert r.returncode == 1 and f"check_image: FAILED: PYTHON={python}, but" in r.stderr


def test_check_image_refuses_python_gil_0():
    r = child([str(CHECK_IMAGE), "--python", THIS_PYTHON], env={"PYTHON_GIL": "0"})
    assert r.returncode == 1 and "PYTHON_GIL=0 / -X gil=0 hides" in r.stderr


@needs_ft
def test_check_image_fails_when_the_gil_is_on():
    # the guard's red run: the same interpreter with the GIL forced on
    r = child(["-X", "gil=1", str(CHECK_IMAGE), "--python", THIS_PYTHON])
    assert r.returncode == 1 and "the GIL is enabled before any import" in r.stderr


def test_check_image_finds_every_compiled_module_it_can_import(tmp_path):
    check_image = load_check_image()
    suffixes = (".cpython-314t-x86_64-linux-gnu.so", ".so")
    for rel in ("numpy/_core/_multiarray_umath.cpython-314t-x86_64-linux-gnu.so",
                "raggio_native/raggio_native.cpython-314t-x86_64-linux-gnu.so",
                "yaml/_yaml.so",
                "numpy.libs/libscipy_openblas64_-6bb31eeb.so",  # a vendored lib, not a module
                "numpy/_core/multiarray.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"")
    assert check_image.extension_modules(tmp_path, suffixes) == [
        "numpy._core._multiarray_umath", "raggio_native.raggio_native", "yaml._yaml"]
    # spec §4.2: raggio_native is rebuilt per interpreter; an abi3 or bare .so build is refused
    assert check_image.built_for_this_interpreter(
        "raggio_native/raggio_native.cpython-314t-x86_64-linux-gnu.so", suffixes)
    assert not check_image.built_for_this_interpreter("raggio_native/raggio_native.abi3.so", suffixes)
    assert not check_image.built_for_this_interpreter("raggio_native/raggio_native.so", suffixes)


@pytest.fixture
def gil_needing_extension(tmp_path):
    """CPython's own _testmultiphase test extension, copied under the name of the one
    module in it that declares no free-threading support: importing that really
    re-enables the GIL. Returns the code that puts the copy on sys.path."""
    spec = importlib.util.find_spec("_testmultiphase")
    if spec is None:
        pytest.skip("this CPython build ships no _testmultiphase test extension")
    suffix = next(s for s in importlib.machinery.EXTENSION_SUFFIXES if spec.origin.endswith(s))
    shutil.copy(spec.origin, tmp_path / f"_testmultiphase_nonmodule{suffix}")
    return f"import sys; sys.path.insert(0, {str(tmp_path)!r}); "


@needs_ft
def test_the_image_warning_filter_fails_a_real_gil_re_enable(gil_needing_extension):
    probe = gil_needing_extension + "import _testmultiphase_nonmodule; print(sys._is_gil_enabled())"
    unguarded = child(["-c", probe])
    assert unguarded.returncode == 0 and unguarded.stdout.strip() == "True"  # the GIL came back
    # the string the Dockerfile's runtime ENV and the CI job put in PYTHONWARNINGS
    guarded = child(["-c", probe], env={"PYTHONWARNINGS": load_check_image().GIL_WARNING_FILTER})
    assert guarded.returncode == 1
    assert "RuntimeWarning: The global interpreter lock (GIL) has been enabled" in guarded.stderr


@needs_ft
def test_check_image_names_the_module_that_enabled_the_gil(gil_needing_extension):
    # even with the warning swallowed, the GIL state check after each import catches it
    probe = gil_needing_extension + (
        f"sys.path.insert(0, {str(CHECK_IMAGE.parent)!r}); import check_image, warnings; "
        "warnings.simplefilter('ignore'); check_image.import_all(['_testmultiphase_nonmodule'])")
    r = child(["-c", probe])
    assert r.returncode == 1
    assert "CheckFailed: importing _testmultiphase_nonmodule enabled the GIL" in r.stderr
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `5 failed, 11 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `8 failed, 12 passed`.

Every new test fails because there is no `docker/check_image.py`, in one of two ways:
- the tests that run the check as a script fail with `can't open file '...docker/check_image.py': [Errno 2] No such file or directory` and `assert 2 == 0` (or `== 1`);
- the tests that load it as a module fail with `FileNotFoundError`.

On 3.12, the three `needs_ft` tests of this section skip.

- [ ] **Step 3: Implement**

Create `docker/check_image.py`:

```python
"""Build-time image check (ADR 0006): the interpreter is the flavour PYTHON names, and on
a free-threaded build nothing raggio can load turns the GIL back on.

CPython 3.13t+ re-enables the GIL, with nothing but a RuntimeWarning, when it imports an
extension module that has not declared free-threading support. The filter below is
installed before any other import and makes that warning an error; the GIL state is also
checked after every import, in case a caller swallows the error. PYTHON_GIL=0 / -X gil=0
would hide exactly this and are refused.

The Dockerfile's builder stage runs it against /app/.venv; the free-threaded CI job and
tests/test_freethreading.py run it too:
    python docker/check_image.py --python 3.14t --require-native
"""
import re
import warnings

GIL_WARNING = "The global interpreter lock (GIL) has been enabled"
# the same filter as a -W / PYTHONWARNINGS value (that syntax escapes the message itself);
# the Dockerfile's runtime ENV and the CI job set exactly this string
GIL_WARNING_FILTER = f"error:{GIL_WARNING}:RuntimeWarning"
warnings.filterwarnings("error", message=re.escape(GIL_WARNING), category=RuntimeWarning)

import argparse  # noqa: E402
import importlib  # noqa: E402
import importlib.machinery  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import sys  # noqa: E402
import sysconfig  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import zlib  # noqa: E402
from pathlib import Path  # noqa: E402

DIM = 8
ROWS = 1100  # > IVF_MIN_ROWS (1024): the IVF attach and its shard fan-out run too
ROOT_KEY = "check-image-root-key"


class CheckFailed(Exception):
    pass


def free_threaded() -> bool:
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def gil_enabled() -> bool:
    return getattr(sys, "_is_gil_enabled", lambda: True)()


def check_flavour(python: str) -> None:
    """PYTHON=3.14t must give a free-threaded 3.14, PYTHON=3.12 a GIL 3.12."""
    want_ft = python.endswith("t")
    if free_threaded() != want_ft:
        kind = "free-threaded" if free_threaded() else "GIL"
        raise CheckFailed(f"PYTHON={python}, but this is a {kind} build ({platform.python_version()})")
    want = python.removesuffix("t").split(".")
    if platform.python_version().split(".")[: len(want)] != want:
        raise CheckFailed(f"PYTHON={python}, but this is Python {platform.python_version()}")
    if os.environ.get("PYTHON_GIL") == "0" or sys._xoptions.get("gil") == "0":
        raise CheckFailed("PYTHON_GIL=0 / -X gil=0 hides a GIL re-enable: unset it")
    if want_ft and gil_enabled():
        raise CheckFailed("the GIL is enabled before any import (PYTHON_GIL=1 or -X gil=1?)")


def extension_modules(site: Path, suffixes=tuple(importlib.machinery.EXTENSION_SUFFIXES)) -> list[str]:
    """Dotted names of the compiled extension modules installed under `site`. Files whose
    path is not an importable name (the vendored libs in numpy.libs/, ...) are skipped."""
    names = []
    for path in sorted(site.rglob("*")):
        suffix = next((s for s in suffixes if path.name.endswith(s)), None)
        if suffix is None or not path.is_file():
            continue
        parts = [*path.relative_to(site).parts[:-1], path.name[: -len(suffix)]]
        if all(p.isidentifier() for p in parts):
            names.append(".".join(parts))
    return names


def built_for_this_interpreter(path: str, suffixes=tuple(importlib.machinery.EXTENSION_SUFFIXES)) -> bool:
    """The file carries this interpreter's own tag, the first extension suffix
    (.cpython-314t-aarch64-linux-gnu.so, .cp312-win_amd64.pyd, ...): not .abi3.so, not a bare .so."""
    return str(path).endswith(suffixes[0])


def check_native() -> None:
    """spec §4.2: raggio_native is built for the image's own interpreter, without abi3. A
    build for another interpreter would not load on 3.14t, and raggio would fall back to the
    Python scorer without a word."""
    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    files = sorted(m.__file__ for name, m in list(sys.modules.items())
                   if name.split(".")[0] == "raggio_native"
                   and str(getattr(m, "__file__", None) or "").endswith(suffixes))
    if not files or not all(built_for_this_interpreter(f, suffixes) for f in files):
        raise CheckFailed(f"raggio_native is not built for this interpreter ({suffixes[0]}): {files}")


def import_all(names: list[str]) -> None:
    for name in names:
        importlib.import_module(name)
        if free_threaded() and gil_enabled():
            raise CheckFailed(f"importing {name} enabled the GIL")


def site_dirs() -> list[Path]:
    return sorted({Path(sysconfig.get_path("purelib")), Path(sysconfig.get_path("platlib"))})


class FakeEmbedder:
    async def embed(self, texts):
        import numpy as np

        out = []
        for t in texts:
            v = np.random.default_rng(zlib.crc32(t.encode())).standard_normal(DIM)
            out.append((v / np.linalg.norm(v)).tolist())
        return out

    async def aclose(self):
        pass


def _ok(response, status=200):
    if response.status_code != status:
        raise CheckFailed(f"{response.request.method} {response.request.url.path}: "
                          f"{response.status_code} {response.text}")
    return response.json()


def _wait(client, job_id, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = _ok(client.get(f"/collections/check/jobs/{job_id}"))
        if job["status"] == "done":
            return
        if job["status"] == "error":
            raise CheckFailed(f"job {job_id} failed: {job['error']}")
        time.sleep(0.05)
    raise CheckFailed(f"job {job_id} did not finish in {timeout:.0f}s")


def exercise(require_native: bool) -> dict:
    """uvicorn's own loading of the app, then one pass over the request path: create,
    ingest, get, list, vector/text/hybrid/filtered search, IVF attach, the same searches
    on the IVF index, detach, delete. Returns the final /healthz body."""
    import numpy as np
    import uvicorn
    from fastapi.testclient import TestClient

    from raggio.app import create_app

    config = uvicorn.Config("raggio.app:create_app", factory=True)
    config.load()  # the http/ws protocols and lifespan uvicorn would serve with
    config.get_loop_factory()  # and its event loop (uvloop where it is installed)
    import_all(sorted(m for m in sys.modules if m.startswith(("uvicorn", "uvloop"))))

    with TestClient(create_app(embedder_factory=lambda cfg: FakeEmbedder()),
                    headers={"x-api-key": ROOT_KEY}) as client:
        _ok(client.post("/collections", json={"name": "check", "dim": DIM}), 201)
        rng = np.random.default_rng(0)
        docs = [{"doc_id": f"d{i}", "chunks": [{
            "id": f"c{i}", "text": f"chunk {i} about {'solar wind' if i % 2 else 'deep sea'}",
            "vector": (v / np.linalg.norm(v)).tolist(),
            "metadata": {"group": "a" if i % 3 else "b", "n": i}}]}
            for i, v in enumerate(rng.standard_normal((ROWS, DIM)))]
        _wait(client, _ok(client.post("/collections/check/documents",
                                      json={"documents": docs}), 202)["job_id"])
        _ok(client.get("/collections/check/documents/d7"))
        _ok(client.get("/collections/check/documents",
                       params={"filter": '{"group": "b"}', "limit": 5}))
        queries = [
            {"query": {"text": "solar wind"}, "mode": "vector"},
            {"query": {"text": "solar wind"}, "mode": "text"},
            {"query": {"text": "deep sea"}, "mode": "hybrid"},
            {"query": {"text": "deep sea"}, "mode": "hybrid", "filter": {"group": "b"}},
            {"query": {"vector": docs[5]["chunks"][0]["vector"]}, "filter": {"n": {"gte": 3}}},
        ]

        def search_all():
            for q in queries:
                if not _ok(client.post("/collections/check/search", json={**q, "k": 5}))["hits"]:
                    raise CheckFailed(f"no hits for {q}")

        search_all()
        _wait(client, _ok(client.post("/collections/check/index",
                                      json={"nlist": 8, "nprobe": 4}), 202)["job_id"])
        if _ok(client.get("/collections/check"))["index"]["type"] != "ivf":
            raise CheckFailed("the IVF index did not attach")
        search_all()  # now through the IVF shard fan-out (IVF_SEARCH_THREADS)
        _wait(client, _ok(client.delete("/collections/check/index"), 202)["job_id"])
        if _ok(client.get("/collections/check"))["index"]["type"] != "flat":
            raise CheckFailed("the IVF index did not detach")
        _ok(client.delete("/collections/check"))
        health = _ok(client.get("/healthz"))
    if health["gil_enabled"] is not gil_enabled():
        raise CheckFailed(f"/healthz says gil_enabled={health['gil_enabled']}")
    if require_native and health["bm25"] != "native":
        raise CheckFailed(f"/healthz says bm25={health['bm25']!r}, the image needs 'native'")
    return health


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--python", required=True, help="the PYTHON build arg: 3.12, 3.14t, ...")
    parser.add_argument("--require-native", action="store_true",
                        help="fail unless the stage-2 BM25 scorer is raggio_native")
    args = parser.parse_args(argv)
    # ignore_cleanup_errors: on Windows the catalog.db of the app uvicorn.Config loaded is
    # still open (no lifespan ran for it)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
        os.environ.update(ROOT_API_KEY=ROOT_KEY, DATA_DIR=data_dir)
        try:
            check_flavour(args.python)
            names = [n for site in site_dirs() for n in extension_modules(site)]
            import_all(names)
            if args.require_native:
                import_all(["raggio_native"])
                check_native()
            health = exercise(args.require_native)
            if free_threaded() and gil_enabled():
                raise CheckFailed("the GIL was enabled while serving")
        except (CheckFailed, ImportError, RuntimeWarning) as e:
            print(f"check_image: FAILED: {e}", file=sys.stderr)
            return 1
    print(f"check_image: Python {platform.python_version()}"
          f" {'free-threaded' if free_threaded() else 'with the GIL'},"
          f" GIL {'enabled' if gil_enabled() else 'disabled'},"
          f" {len(names)} extension modules imported, bm25={health['bm25']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `16 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `20 passed`.

Run the check directly, as the image build and the CI job will:

```bash
uv run python docker/check_image.py --python 3.12
UV_PROJECT_ENVIRONMENT=.venv-ft uv run --no-sync python docker/check_image.py --python 3.14t --require-native
```

Expected, last line of each:
- `check_image: Python 3.12.<patch> with the GIL, GIL enabled, <n> extension modules imported, bm25=native`. It says `bm25=python` if the default `.venv` was synced without the `native` extra.
- `check_image: Python 3.14.<patch> free-threaded, GIL disabled, <n> extension modules imported, bm25=native`.

In the sandbox, `<n>` was 28 and 27. A `StarletteDeprecationWarning` on stderr is harmless.

Run `uv run pytest -q`. Expected: all green, with 16 more passed and 4 more skipped than the base (sandbox: `298 passed, 4 skipped`).

Run the full suite on 3.14t:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q
```

Expected: all green (sandbox: `293 passed, 2 skipped`).

- [ ] **Step 5: Commit**

```bash
git add docker/check_image.py tests/test_freethreading.py
git commit -m "Add the build-time image check" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The Dockerfile's 3.14t path, and the runtime GIL guard

**Files:**
- Modify: `Dockerfile`, in four places:
  - the header comment;
  - `ARG UV_NO_BINARY_PACKAGE` in the builder;
  - the check as the builder's last step;
  - `ENV PYTHONWARNINGS` in the runtime stage.
- Modify: `tests/test_freethreading.py` (append a section)

**Interfaces:**
- Consumes the Dockerfile as Plans A, C and E leave it:
  - global `ARG UV_VERSION=0.12.20`, `ARG PYTHON=3.12` and `ARG RUST_VERSION=1.98.1`;
  - E's builder stage `FROM docker.io/library/rust:${RUST_VERSION}-slim-trixie AS builder`, with `ARG PYTHON`, `ENV UV_PYTHON=${PYTHON}` and `CARGO_TARGET_DIR=/tmp/cargo-target`. `RUN uv python install ${PYTHON}` is followed by the `RUN bash -c 'set -euo pipefail; py="$(uv python find "${PYTHON}")"; ln -s "$py" /usr/local/bin/python3; ...'` link and `ENV PYO3_PYTHON=/usr/local/bin/python3`. Then come `COPY native ./native`, `RUN uv sync --frozen --no-install-project --no-dev --extra native`, `COPY src ./src`, and last `RUN uv sync --frozen --no-dev --extra native`. With `PYTHON=3.14t`, the link and `PYO3_PYTHON` name the free-threaded interpreter, so maturin builds `raggio_native` (and turbovec's sdist) as cp314t, not abi3 (spec §4.2);
  - A's `debian:trixie-slim` runtime stage, in which `USER app` is followed by A's D16 comment, `ENV OPENBLAS_NUM_THREADS=1`, `ENV LANG=C.UTF-8 DATA_DIR=/data PATH="/app/.venv/bin:$PATH"`, `VOLUME /data` and `EXPOSE 8000`;
  - C's block after `EXPOSE 8000`: four comment lines and the single `ENV MALLOC_TRIM_THRESHOLD_=134217728` (D13). This task writes that variable name nowhere in the Dockerfile, because C's test counts it;
  - `docker/check_image.py` (Task 5).
- Produces:
  - `podman build --build-arg PYTHON=3.14t .`: the free-threaded image. turbovec is built from its sdist, and the build fails if the check fails.
  - `--build-arg UV_NO_BINARY_PACKAGE=turbovec`: turbovec from its sdist on any interpreter. It is unset by default, and 3.14t does not need it: PyPI has only the `cp39-abi3` wheel, which cannot load on 3.14t, so uv falls back to the sdist on its own (p4). It matters only for the C1 arm (3.14 with the GIL). That build is still an abi3 wheel, `cp39-abi3-linux_aarch64`, because the sdist enables `abi3-py39`, while 3.14t builds `cp314-cp314t`.
  - Every image runs with `PYTHONWARNINGS` set to `GIL_WARNING_FILTER`.
  - Every image, whatever its `PYTHON`, keeps the one `ENV OPENBLAS_NUM_THREADS=1`, so the ABAB arms run the same BLAS threads (spec §6, D16).

- [ ] **Step 1: Check the Dockerfile this task edits**

Run:

```bash
grep -n -E "^ENV (PYO3_PYTHON=/usr/local/bin/python3|OPENBLAS_NUM_THREADS=1|MALLOC_TRIM_THRESHOLD_=134217728)$" Dockerfile
```

Expected: three lines, in this order: `ENV PYO3_PYTHON=/usr/local/bin/python3` (E's builder), `ENV OPENBLAS_NUM_THREADS=1` (A, D16) and `ENV MALLOC_TRIM_THRESHOLD_=134217728` (C, D13). If any is missing, stop and tell the user which plan's line is absent. Do not add it: spec §4.1 gives those lines to A, C and E, and the ABAB would otherwise compare arms that differ in more than the interpreter.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_freethreading.py`:

```python
# ---- the unified Dockerfile's 3.14t path (spec D2) ----

TURBOVEC_MIN_RUST = (1, 89)  # turbovec 1.0.0's rust-version: 3.14t builds it from its sdist


def _stages():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = re.split(r"^FROM ", dockerfile, flags=re.M)[1:]
    (builder,) = [s for s in stages if s.split("\n", 1)[0].endswith(" AS builder")]
    return dockerfile, builder, stages[-1]


def test_the_builder_checks_the_venv_it_built():
    _, builder, _ = _stages()
    sync = builder.index("RUN uv sync --frozen --no-dev --extra native")
    copy = builder.index("COPY docker/check_image.py /tmp/check_image.py")
    run = builder.index('RUN /app/.venv/bin/python /tmp/check_image.py --python "${PYTHON}" '
                        "--require-native")
    assert sync < copy < run  # the full venv exists, and the script stays out of /app


def test_the_runtime_image_fails_loudly_on_a_gil_re_enable():
    dockerfile, _, runtime = _stages()
    check_image = load_check_image()
    assert f'ENV PYTHONWARNINGS="{check_image.GIL_WARNING_FILTER}"' in runtime.splitlines()
    # nothing but comments may mention the override that would hide a re-enable
    code = [ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "PYTHON_GIL" in ln or "gil=" in ln.lower()]
    # the ABAB arms differ in the interpreter only (spec §6): every PYTHON gets A's single
    # OpenBLAS thread (D16), set once and by nothing else, and C's trim threshold (D13)
    assert [ln for ln in code if "OPENBLAS_NUM_THREADS" in ln] == ["ENV OPENBLAS_NUM_THREADS=1"]
    assert "ENV OPENBLAS_NUM_THREADS=1" in runtime.splitlines()
    assert "ENV MALLOC_TRIM_THRESHOLD_=134217728" in runtime.splitlines()


def test_the_builder_can_build_turbovec_from_its_sdist():
    dockerfile, builder, _ = _stages()
    rust = re.search(r"^ARG RUST_VERSION=(\d+)\.(\d+)\.\d+$", dockerfile, re.M)
    assert (int(rust[1]), int(rust[2])) >= TURBOVEC_MIN_RUST
    # --build-arg UV_NO_BINARY_PACKAGE=turbovec: a local sdist build on a GIL interpreter too
    # (C1), still abi3 (the sdist enables abi3-py39), so not 3.14t's cp314t build; unset, uv
    # installs PyPI's abi3 wheel as before, and 3.14t falls back to the sdist on its own
    assert re.search(r"^ARG UV_NO_BINARY_PACKAGE$", builder, re.M)
    assert builder.index("ARG UV_NO_BINARY_PACKAGE") < builder.index("RUN uv sync --frozen")
    # raggio_native is rebuilt for each image's own interpreter, never abi3 (spec §4.2):
    # PyO3 builds for the /python that uv installed for PYTHON (Plan E's builder)
    assert 'uv python find "${PYTHON}"' in builder
    assert "ENV PYO3_PYTHON=/usr/local/bin/python3" in builder.splitlines()
    syncs = re.findall(r"^RUN uv sync (.*)$", builder, re.M)
    assert len(syncs) == 2 and all("--extra native" in s for s in syncs)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `3 failed, 16 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `3 failed, 20 passed`. The failures are:
- `test_the_builder_checks_the_venv_it_built`: `ValueError: substring not found`;
- `test_the_runtime_image_fails_loudly_on_a_gil_re_enable`: an `AssertionError` on the `ENV PYTHONWARNINGS=...` line;
- `test_the_builder_can_build_turbovec_from_its_sdist`: `assert None`, from the `ARG UV_NO_BINARY_PACKAGE` search.

The asserts on A's, C's and E's lines come after those, and pass on the base that Step 1 checked.

- [ ] **Step 4: Implement**

4a. In `Dockerfile`, append four comment lines after the header comment's last line, `# same pin as the native job in .github/workflows/tests.yml.`, so that they directly precede `ARG UV_VERSION=0.12.20`:

```dockerfile
# PYTHON=3.14t is the experimental free-threaded image (ADR 0006): turbovec ships no
# cp314t wheel, so uv builds it from its sdist here (rust-version 1.89). The check at the
# end of the builder fails the build if the interpreter is not the flavour PYTHON names or
# anything the app loads re-enables the GIL.
```

4b. In the builder stage, add the ARG right after `ARG PYTHON`. The result is:

```dockerfile
FROM docker.io/library/rust:${RUST_VERSION}-slim-trixie AS builder
COPY --from=uv /uv /uvx /bin/
ARG PYTHON
# unset by default; 3.14t needs none. --build-arg UV_NO_BINARY_PACKAGE=turbovec (C1) builds
# the sdist on a GIL interpreter too: a local abi3 wheel (abi3-py39), not 3.14t's cp314t
ARG UV_NO_BINARY_PACKAGE
ENV UV_PYTHON_INSTALL_DIR=/python \
```

uv reads `UV_NO_BINARY_PACKAGE` from the environment of each `RUN`, and a declared build arg is set there only when it is passed.

Nothing else in the builder changes. E's `uv python find "${PYTHON}"` link and `ENV PYO3_PYTHON=/usr/local/bin/python3` already make every PyO3 build in both syncs, `raggio-native` from `native/` and turbovec's sdist, target the interpreter that `PYTHON` names. With `PYTHON=3.14t` that is `cp314t`; there is no abi3 build to fall back to (spec §4.2).

4c. In the builder stage, add two lines right after the second sync, `RUN uv sync --frozen --no-dev --extra native` (the one without `--no-install-project`). The result is:

```dockerfile
COPY src ./src
RUN uv sync --frozen --no-dev --extra native
COPY docker/check_image.py /tmp/check_image.py
RUN /app/.venv/bin/python /tmp/check_image.py --python "${PYTHON}" --require-native
```

The script goes to `/tmp`, so it stays out of the `/app` that the runtime stage copies. `.dockerignore` does not exclude `docker/`.

4d. In the runtime stage, add the guard right after `ENV LANG=C.UTF-8 DATA_DIR=/data PATH="/app/.venv/bin:$PATH"`. A's `ENV OPENBLAS_NUM_THREADS=1` above it, and C's block after `EXPOSE 8000`, stay as they are. The result is:

```dockerfile
USER app
# one OpenBLAS thread (spec D16): numpy's scipy-openblas otherwise starts one busy-spinning thread per core under search load
ENV OPENBLAS_NUM_THREADS=1
ENV LANG=C.UTF-8 DATA_DIR=/data PATH="/app/.venv/bin:$PATH"
# a lazy import that re-enables the GIL on 3.14t raises instead of silently serving with
# it (docker/check_image.py GIL_WARNING_FILTER); on a GIL build it never fires
ENV PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning"
VOLUME /data
EXPOSE 8000
```

The two new comment lines must not contain the words `rust` or `cargo`, because Plan E's test refuses them anywhere in the runtime stage.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `19 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `23 passed`.

Run `uv run pytest -q`. Expected: all green (sandbox: `301 passed, 4 skipped`).

Run the full suite on 3.14t, as before. Expected: all green (sandbox: `296 passed, 2 skipped`).

The next build is optional, and takes about 5 minutes per image on the laptop's podman machine. Build both images locally and check the guard:

```bash
MSYS_NO_PATHCONV=1 podman build -t raggio:planG-py312 . 2>&1 | grep -E "check_image|Building turbovec"
MSYS_NO_PATHCONV=1 podman build --build-arg PYTHON=3.14t -t raggio:planG-py314t . 2>&1 | grep -E "check_image|Building turbovec"
MSYS_NO_PATHCONV=1 podman run --rm raggio:planG-py314t python -c "import os, sys; print(os.environ['PYTHONWARNINGS'], os.environ['OPENBLAS_NUM_THREADS'], sys._is_gil_enabled())"
MSYS_NO_PATHCONV=1 podman rmi raggio:planG-py312 raggio:planG-py314t
```

Expected:
- the 3.12 build prints `check_image: Python 3.12.<patch> with the GIL, GIL enabled, ...`, and no `Building turbovec`;
- the 3.14t build prints `Building turbovec==1.0.0` and `check_image: Python 3.14.<patch> free-threaded, GIL disabled, <n> extension modules imported, bm25=native`;
- the run prints the filter string, then `1`, then `False`.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile tests/test_freethreading.py
git commit -m "Build the 3.14t image and fail on a GIL re-enable" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The non-blocking 3.14t CI job

**Files:**
- Modify: `.github/workflows/tests.yml`: append the `freethreaded` job after the `native` job
- Modify: `tests/test_freethreading.py` (append a section)

**Interfaces:**
- Consumes the jobs of `tests.yml` as Plans A and E leave them:
  - `test`, with `astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0`, `version: "0.12.20"` and `python-version: "3.12"`, after `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`;
  - `native`, with the matrix `runner: [ubuntu-latest, ubuntu-24.04-arm]`, the step `Install Rust (same pin as ARG RUST_VERSION in the Dockerfile)`, and `REQUIRE_NATIVE: "1"`.
- It also consumes Plan B's stress tests in `tests/test_concurrency.py`: the names match `mixed_workload` and `stop_under_load`, and they read `RAGGIO_STRESS_SECONDS`.
- Produces the job `freethreaded`:
  - it runs on both runners, with `continue-on-error: true` and `timeout-minutes: 25`;
  - its job-level env is `PYTHONWARNINGS: GIL_WARNING_FILTER`;
  - its steps are: build turbovec and `raggio-native` from source, run the suite with `REQUIRE_NATIVE=1`, run `docker/check_image.py --python 3.14t --require-native`, then run the stress tests for 30 s.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_freethreading.py`:

```python
# ---- the non-blocking 3.14t CI job (spec D11) ----


def _jobs():
    import yaml  # uvicorn[standard] installs PyYAML

    ci = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    return ci, yaml.safe_load(ci)["jobs"]


def _runs(job):
    return {s["run"].strip(): s.get("env", {}) for s in job["steps"] if "run" in s}


def test_ci_runs_the_suite_on_314t_without_blocking_the_workflow():
    ci, jobs = _jobs()
    ft = jobs["freethreaded"]
    assert ft["continue-on-error"] is True  # experimental until 3.14t is the default
    assert ft["timeout-minutes"] <= 30
    # x86_64 and aarch64 (the DGX), the same runners as the native job
    assert ft["runs-on"] == "${{ matrix.runner }}"
    assert ft["strategy"]["matrix"]["runner"] == jobs["native"]["strategy"]["matrix"]["runner"]
    # the same uv action, pin and uv version as the blocking job; only the interpreter differs
    (setup,) = [s for s in ft["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    (base,) = [s for s in jobs["test"]["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert setup["uses"] == base["uses"]
    assert setup["with"] == {**base["with"], "python-version": "3.14t"}
    # turbovec builds from its sdist and raggio-native from native/: the image's Rust pin
    rust = [s["run"] for s in jobs["native"]["steps"] if s.get("name", "").startswith("Install Rust")]
    assert rust and rust[0] in [s.get("run") for s in ft["steps"]]


def test_ci_314t_job_turns_a_gil_re_enable_into_a_failure():
    ci, jobs = _jobs()
    ft = jobs["freethreaded"]
    assert ft["env"]["PYTHONWARNINGS"] == load_check_image().GIL_WARNING_FILTER
    runs = _runs(ft)
    # no --group bench: orjson has no free-threaded wheel
    assert "uv sync --frozen --extra native" in runs
    assert runs["uv run --no-sync pytest -q"] == {"REQUIRE_NATIVE": "1"}
    assert "uv run --no-sync python docker/check_image.py --python 3.14t --require-native" in runs
    stress = runs['uv run --no-sync pytest -q tests/test_concurrency.py -k "mixed_workload or stop_under_load"']
    assert float(stress["RAGGIO_STRESS_SECONDS"]) >= 30
    assert "PYTHON_GIL" not in ci and "gil=0" not in ci.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `2 failed, 19 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `2 failed, 23 passed`. Both new tests fail with `KeyError: 'freethreaded'`.

- [ ] **Step 3: Implement**

Append this job to `.github/workflows/tests.yml`, after the `native` job's last step. On the sandbox base, the file ends with:

```yaml
      - name: Test with the extension required
        run: uv run --no-sync pytest -q
        env:
          REQUIRE_NATIVE: "1"
```

Add one blank line, then:

```yaml
  # experimental free-threaded CPython (ADR 0006), on x86_64 and aarch64 like the native
  # job: the suite on 3.14t with the extension required and a GIL re-enable turned into an
  # error, the image build's check, then the concurrency stress tests for longer.
  # Non-blocking until 3.14t is the default image.
  freethreaded:
    strategy:
      matrix:
        runner: [ubuntu-latest, ubuntu-24.04-arm]
    runs-on: ${{ matrix.runner }}
    timeout-minutes: 25
    continue-on-error: true
    env:
      PYTHONWARNINGS: "error:The global interpreter lock (GIL) has been enabled:RuntimeWarning"
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
        with:
          version: "0.12.20"  # same pin as ARG UV_VERSION in the Dockerfile
          python-version: "3.14t"
      - name: Install Rust (same pin as ARG RUST_VERSION in the Dockerfile)
        run: |
          if ! command -v rustup; then
            curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
            echo "$HOME/.cargo/bin" >> "$GITHUB_PATH"
            export PATH="$HOME/.cargo/bin:$PATH"
          fi
          rustup toolchain install 1.98.1 --profile minimal
          rustup default 1.98.1
      - name: Build turbovec (sdist, no cp314t wheel) and raggio-native
        run: uv sync --frozen --extra native
      - name: Test with the extension required
        run: uv run --no-sync pytest -q
        env:
          REQUIRE_NATIVE: "1"
      - name: Check the environment as the image build does
        run: uv run --no-sync python docker/check_image.py --python 3.14t --require-native
      - name: Stress the concurrency paths
        run: uv run --no-sync pytest -q tests/test_concurrency.py -k "mixed_workload or stop_under_load"
        env:
          RAGGIO_STRESS_SECONDS: "30"
```

Some fields must match the other jobs, and the test compares them against the file:
- the `Install Rust` step must be character-for-character the `native` job's;
- the `setup-uv` pin and `version` must be the `test` job's.

If Plan A or E changed any of these, copy their current text.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `21 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `25 passed`.

Run the job's stress step locally on 3.14t. It takes about 70 s:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" RAGGIO_STRESS_SECONDS=30 uv run --no-sync pytest -q tests/test_concurrency.py -k "mixed_workload or stop_under_load"
```

Expected: `2 passed`.

Run `uv run pytest -q`. Expected: all green (sandbox: `303 passed, 4 skipped`). Run the full 3.14t suite. Expected: all green (sandbox: `298 passed, 2 skipped`).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/tests.yml tests/test_freethreading.py
git commit -m "Add the non-blocking 3.14t CI job" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: The Unicode drift probe

**Files:**
- Create: `bench/unicode_drift_probe.py`
- Modify: `tests/test_freethreading.py` (append a section)

**Interfaces:**
- Consumes:
  - `raggio.store._fold(text: str) -> str` and `raggio.store._TOKEN_RE`, the Python-side BM25 tokenizer;
  - `unicodedata.unidata_version` and `unicodedata.category`;
  - the bench corpus `bench/corpus/abstracts.jsonl`, whose rows have `title` and `text`.
- Produces `bench/unicode_drift_probe.py`:
  - `dump(code_points=range(0x110000)) -> dict[str, list[str]]`;
  - `diff(a: dict, b: dict) -> dict`, with the keys `from`, `to`, `newly_assigned`, `changed` and `token_drift`;
  - `scan(drift: dict, corpus, limit: int | None = None) -> dict`, with the keys `rows`, `rows_with_drift`, `rows_with_token_drift` and `first_rows_with_drift`;
  - the CLI `dump`, `diff A B` and `scan DRIFT [--corpus PATH] [--limit N]`. `dump` prints `{"unidata": ..., "cp": {...}}`, and `diff` prints its JSON to stdout and a one-line summary to stderr. `dump` runs as `python - dump < bench/unicode_drift_probe.py` inside an image.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_freethreading.py`:

```python
# ---- Unicode drift between the arms (3.12: Unicode 15.0.0, 3.14t: 16.0.0) ----


def load_drift_probe():
    path = ROOT / "bench" / "unicode_drift_probe.py"
    spec = importlib.util.spec_from_file_location("unicode_drift_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_drift_probe_dump_records_what_the_tokenizer_does_per_code_point():
    probe = load_drift_probe()
    dump = probe.dump(range(0x41, 0x42))  # "A"
    dump.update(probe.dump([0x5F, 0xE9, 0x301, 0xFB01, 0x378]))
    assert dump["65"] == ["Lu", "a"]
    assert dump["95"] == ["Pc", " "]  # '_' separates tokens
    assert dump["233"] == ["Ll", "e"]  # NFKD folds the accent away
    assert dump["769"] == ["Mn", ""]  # a combining mark alone vanishes
    assert dump["64257"] == ["Ll", "fi"]
    assert "888" not in dump  # U+0378 is unassigned and no word character: not recorded


def test_drift_probe_diff_separates_new_code_points_from_changed_ones():
    probe = load_drift_probe()
    a = {"unidata": "15.0.0", "cp": {"65": ["Lu", "a"], "95": ["Pc", " "], "300": ["Lo", "x"]}}
    b = {"unidata": "16.0.0", "cp": {"65": ["Lu", "a"], "95": ["Pc", " "], "300": ["Lo", " "],
                                     "7000": ["Lo", "z"], "7001": ["So", " "]}}
    assert probe.diff(a, b) == {
        "from": "15.0.0", "to": "16.0.0",
        "newly_assigned": [7000, 7001],  # unassigned (Cn) in the first dump
        "changed": [300],  # assigned in both, recorded differently
        "token_drift": [300, 7000],  # the ones whose tokens differ
    }


def test_drift_probe_scan_counts_corpus_rows_with_a_drifted_code_point(tmp_path):
    import json

    probe = load_drift_probe()
    drift = {"from": "15.0.0", "to": "16.0.0", "newly_assigned": [0x1E5D0, 0x2B740],
             "changed": [], "token_drift": [0x1E5D0]}
    rows = [{"title": "plain", "text": "nothing new"},
            {"title": "Ol Onal \U0001E5D0", "text": "a new word character"},
            {"title": "t", "text": "a new symbol \U0002B740"},
            {"title": "beyond the limit \U0001E5D0", "text": ""}]
    corpus = tmp_path / "abstracts.jsonl"
    corpus.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert probe.scan(drift, corpus, limit=3) == {
        "rows": 3, "rows_with_drift": 2, "rows_with_token_drift": 1,
        "first_rows_with_drift": [1, 2]}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `3 failed, 21 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `3 failed, 25 passed`. All three new tests fail with `FileNotFoundError: [Errno 2] No such file or directory: '...bench/unicode_drift_probe.py'`.

- [ ] **Step 3: Implement**

Create `bench/unicode_drift_probe.py`:

```python
"""Unicode drift between two interpreters (ADR 0006): the 3.12 image has Unicode 15.0.0,
3.14t has 16.0.0, and the Python-side BM25 tokenizer (store._fold + store._TOKEN_RE) runs on
unicodedata at query time. This measures which code points tokenize differently between the
two, and how many corpus rows contain one, so a quality difference between the A/B arms
can be told apart from a threading one.

    # in each image (the dump needs raggio importable; diff and scan need only the stdlib)
    podman run --rm -i IMAGE python - dump < bench/unicode_drift_probe.py > u-IMAGE.json
    python bench/unicode_drift_probe.py diff u-a.json u-b.json > drift.json
    python bench/unicode_drift_probe.py scan drift.json --limit 2549619

Per code point only: context rules (Final_Sigma in str.lower, canonical reordering across
code points) are not modelled. SQLite's unicode61 tokenizer is compiled into the sqlite
library and does not move with the interpreter.
"""
import argparse
import json
import sys
import unicodedata

CORPUS = "bench/corpus/abstracts.jsonl"
FIRST_ROWS = 20  # how many drifted row numbers the scan lists
UNRECORDED = ["Cn", " "]  # what a dump leaves out: unassigned, and no word character


def dump(code_points=range(0x110000)) -> dict[str, list[str]]:
    """{str(cp): [general category, token signature]} for every code point that is
    assigned (not private use or a surrogate) or tokenizes to something. The signature is
    what the tokenizer makes of the code point alone: its _fold, with every character
    _TOKEN_RE would not keep as part of a token replaced by a space."""
    from raggio.store import _TOKEN_RE, _fold

    out = {}
    for cp in code_points:
        c = chr(cp)
        category = unicodedata.category(c)
        sig = "".join(ch if _TOKEN_RE.fullmatch(ch) else " " for ch in _fold(c))
        if category not in ("Cn", "Co", "Cs") or sig != " ":
            out[str(cp)] = [category, sig]
    return out


def diff(a: dict, b: dict) -> dict:
    """Code points new in b, code points recorded differently, and the ones whose token
    signature differs (the only ones that can move a BM25 score)."""
    ca, cb = a["cp"], b["cp"]
    keys = sorted({int(k) for k in (*ca, *cb)})
    newly, changed, drift = [], [], []
    for cp in keys:
        ra, rb = ca.get(str(cp), UNRECORDED), cb.get(str(cp), UNRECORDED)
        if ra[0] == "Cn" and rb[0] != "Cn":
            newly.append(cp)
        elif ra != rb:
            changed.append(cp)
        if ra[1] != rb[1]:
            drift.append(cp)
    return {"from": a["unidata"], "to": b["unidata"],
            "newly_assigned": newly, "changed": changed, "token_drift": drift}


def scan(drift: dict, corpus, limit: int | None = None) -> dict:
    """Rows of the bench corpus (title + text) that contain a code point of the diff."""
    any_drift = {chr(cp) for cp in (*drift["newly_assigned"], *drift["changed"], *drift["token_drift"])}
    token_drift = {chr(cp) for cp in drift["token_drift"]}
    rows = hits = token_hits = 0
    first = []
    with open(corpus, encoding="utf-8") as f:
        for line in f:
            if limit is not None and rows >= limit:
                break
            row = json.loads(line)
            chars = set(row.get("title") or "") | set(row.get("text") or "")
            if chars & any_drift:
                hits += 1
                if len(first) < FIRST_ROWS:
                    first.append(rows)
            if chars & token_drift:
                token_hits += 1
            rows += 1
    return {"rows": rows, "rows_with_drift": hits, "rows_with_token_drift": token_hits,
            "first_rows_with_drift": first}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("dump", help="this interpreter's per-code-point tokenization, as JSON")
    p = sub.add_parser("diff", help="compare two dumps")
    p.add_argument("a")
    p.add_argument("b")
    p = sub.add_parser("scan", help="count corpus rows touched by a diff")
    p.add_argument("drift")
    p.add_argument("--corpus", default=CORPUS)
    p.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    if args.mode == "dump":
        out = {"unidata": unicodedata.unidata_version, "python": sys.version, "cp": dump()}
    elif args.mode == "diff":
        with open(args.a, encoding="utf-8") as fa, open(args.b, encoding="utf-8") as fb:
            out = diff(json.load(fa), json.load(fb))
        print(f"{out['from']} -> {out['to']}: {len(out['newly_assigned'])} newly assigned, "
              f"{len(out['changed'])} changed, {len(out['token_drift'])} with token drift",
              file=sys.stderr)
    else:
        with open(args.drift, encoding="utf-8") as f:
            out = scan(json.load(f), args.corpus, args.limit)
    json.dump(out, sys.stdout, ensure_ascii=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `24 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `28 passed`.

Measure the laptop's drift between the two venvs, for comparison with the DGX images in Task 11:

```bash
mkdir -p D:/DEV/_scratch/dgx/planG
uv run python bench/unicode_drift_probe.py dump > D:/DEV/_scratch/dgx/planG/u-laptop-312.json
UV_PROJECT_ENVIRONMENT=.venv-ft uv run --no-sync python bench/unicode_drift_probe.py dump > D:/DEV/_scratch/dgx/planG/u-laptop-314t.json
uv run python bench/unicode_drift_probe.py diff D:/DEV/_scratch/dgx/planG/u-laptop-312.json D:/DEV/_scratch/dgx/planG/u-laptop-314t.json > D:/DEV/_scratch/dgx/planG/drift-laptop.json
```

Expected: the `diff` prints `15.0.0 -> 16.0.0: 5812 newly assigned, 1 changed, 5053 with token drift` to stderr.

Run `uv run pytest -q`. Expected: all green (sandbox: `306 passed, 4 skipped`). Run the full 3.14t suite. Expected: all green (sandbox: `301 passed, 2 skipped`).

- [ ] **Step 5: Commit**

```bash
git add bench/unicode_drift_probe.py tests/test_freethreading.py
git commit -m "Add the Unicode drift probe" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: ADR 0006 and the docs

**Files:**
- Create: `docs/adr/0006-free-threaded-python.md`
- Modify: `README.md`: a paragraph after the first `podman run` block, and an env-table row
- Modify: `docs/getting-started.md`: a note before `!!! tip "No embedding endpoint?"`, and an env-table row
- Modify: `docs/api.md`: the `/healthz` example
- Modify: `tests/test_freethreading.py` (append a section)

**Interfaces:**
- Consumes:
  - ADR 0004 (Plan E) and ADR 0005 (Plan F), which this ADR cites;
  - the research files `docs/superpowers/research/2026-09-28-item4a-freethreading-deps.md`, `…-item4b-freethreading-code.md`, `…-dgx-probe-p2-hybrid-native.md` (D15), `…-dgx-probe-p3-ivf-fanout.md` (D16) and `…-dgx-probe-p4-freethreaded.md` (the 3.14t aarch64 build, SQLite 3.53.1);
  - the env tables that Plan F's `IVF_SEARCH_THREADS` row ends.
- Produces:
  - ADR 0006, with `## Decision rule`, the outcomes **Continue**, **Hold** and **Park**, and a `## Verification` that starts with `Pending:`. Task 12 replaces it, or Task 11 does with the Park if turbovec's sdist cannot build for cp314t without a change to turbovec's own source (spec §3.1 G2); Task 12's test and `g-decide.py` split the ADR at `\n## Verification\n`;
  - in the rule: the two rows that can carry a win (`QPS concurrent`, CPU-bound; `Hybrid QPS concurrent`, SQLite-bound and counted only with equal `sqlite_version`s), the between-server check on A1 → A2 and B1 → B2, and the SQLite-bound costs that never park (spec D15, §6). Task 10's `abab_decision` implements exactly this rule;
  - the documented `GC_FREEZE` and `gil_enabled`, and the build command `podman build --build-arg PYTHON=3.14t -t raggio:py314t .`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_freethreading.py`:

```python
# ---- docs: ADR 0006, the knob, the /healthz example, the build arg ----


def test_docs_record_the_evaluation_and_document_the_knob(make_app):
    import json

    adr = (ROOT / "docs/adr/0006-free-threaded-python.md").read_text(encoding="utf-8")
    for needle in ("PYTHON=3.14t", "GC_FREEZE", "gil_enabled", "cp314t", "## Decision rule",
                   "**Continue**", "**Hold**", "**Park**", "## Verification",
                   "bench/unicode_drift_probe.py", "docker/check_image.py",
                   "sqlite_version", "OPENBLAS_NUM_THREADS=1"):  # spec D15, D16
        assert needle in adr, needle
    for path in ("README.md", "docs/getting-started.md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "| `GC_FREEZE` |" in text, path
        assert "podman build --build-arg PYTHON=3.14t -t raggio:py314t ." in text, path
    # the /healthz example in the API reference shows every key the endpoint returns
    api = (ROOT / "docs/api.md").read_text(encoding="utf-8")
    example = re.search(r"`GET /healthz`.*?\*\*200\*\* `(\{.*?\})`", api, re.S).group(1)
    with TestClient(make_app()) as client:
        assert set(json.loads(example)) == set(client.get("/healthz").json())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `1 failed, 24 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `1 failed, 28 passed`. `test_docs_record_the_evaluation_and_document_the_knob` fails with `FileNotFoundError: ... docs/adr/0006-free-threaded-python.md`.

- [ ] **Step 3: Implement**

3a. Create `docs/adr/0006-free-threaded-python.md`:

````markdown
# ADR 0006 — Free-threaded CPython 3.14t: an experimental image, evaluated

Status: experimental (evaluation only) · Date: 2026-09-28 · Spec D2, D6, D11, D15, D16; item 4

## Context

raggio serves every request from one process: the index is per process, so several uvicorn
workers would claim jobs twice and overwrite each other's index syncs. The heavy calls
already run without the GIL (turbovec searches, numpy, sqlite3 statements, `raggio_native`
BM25, ADR 0004), but the Python glue between them does not: request parsing, filter and
fusion code, `_fold_tokens`, the job worker. The research
(`docs/superpowers/research/2026-09-28-item4a-freethreading-deps.md`,
`…-item4b-freethreading-code.md`) measured on a 6-core laptop that the text leg scales
4.27x at 6 threads on 3.14t against 1.08–1.12x with the GIL, and served concurrent hybrid
search went from 63–109 to 585–606 QPS. On the DGX baseline, concurrent hybrid (13.8 QPS)
barely beat serial (8.6 QPS) on 20 cores. None of this has been measured on the DGX with
free threading.

The GIL is not the only lock on the hybrid path (spec D15). Every SQLite call takes
SQLite's global memstatus malloc mutex. On the DGX, SQL-only text search on Debian's SQLite
3.40.1 went 27.9 / 36.5 / 18.4 / 16.8 q/s at 1 / 2 / 4 / 8 threads; on SQLite 3.50.4, in
the uv-managed CPython that probe p2 used, 28.6 / 47.6 / 45.4 / 39.4 q/s, a plateau at about
39–48 q/s whatever the GIL does (`docs/superpowers/research/2026-09-28-dgx-probe-p2-hybrid-native.md`).
The interpreters these images run, uv 0.12.19's CPython 3.12.14, 3.14.7 and 3.14.7t, all
bundle SQLite 3.53.1 (probe p4, `docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`);
p2's figures were not re-measured on 3.53.1. Memstatus cannot be switched off from Python
(`sqlite3_config` is not exported), and building a custom SQLite is out of scope. So the
concurrent hybrid rows are SQLite-bound, and two interpreters can bundle different SQLite
builds: every table records each arm's `sqlite_version`.

The DGX's own noise is mostly BLAS threads (spec D16, `…-dgx-probe-p3-ivf-fanout.md`).
numpy's default OpenBLAS starts 20 busy-spinning threads under any search load; they burn
about 16 cores and push query threads onto the slower A725 cores (serial 4.1 against
8.3 ms, filtered 32.5 against 69.5 ms). Every image runs `OPENBLAS_NUM_THREADS=1`, which
removes that bimodality. Rootless podman cannot pin cores with `--cpuset-cpus`.

Every runtime dependency ships cp314t wheels at the locked version except turbovec 1.0.0,
which ships only `cp39-abi3` wheels. An abi3 wheel cannot load on a free-threaded build, so
uv builds turbovec from its sdist (Rust >= 1.89). The built module keeps the GIL off
(PyO3 0.29 modules default to `gil_used = false`), so no patch to turbovec is needed. On
the DGX (aarch64), probe p4 built it as `cp314-cp314t-linux_aarch64` with no build arg and
no builder-stage fix, and the GIL stayed off at import, under pytest, under uvicorn load
and in its race runs; the suite passed on every interpreter. p4 ran on a tree without
`raggio_native` and with a stand-in for the build-time check below.
`raggio_native` is never abi3: each image builds it for its own interpreter (cp312,
cp314t) in the Rust builder (ADR 0004), with `gil_used = false`, and the build-time check
refuses a `raggio_native` built for any other interpreter.

## Decisions

| Decision | Rationale |
|---|---|
| **`PYTHON=3.14t` build arg on the one Dockerfile** (`podman build --build-arg PYTHON=3.14t`); no `Dockerfile.ft` | Spec D2. The builder is already the Rust image (ADR 0004), so the turbovec sdist builds there with no extra stage; 3.14t needs no other build arg, because uv falls back to the sdist on its own (probe p4). `PYTHON=3.14 UV_NO_BINARY_PACKAGE=turbovec` builds the optional C1 control from the same file: 3.14 with the GIL, turbovec built locally from the same sdist with maturin, as an abi3 wheel (`cp39-abi3-linux_aarch64` on the DGX; the sdist enables `abi3-py39`), while 3.14t builds `cp314-cp314t`. So C1 is not the same turbovec build: it separates the 3.12 → 3.14 interpreter effect and a local build from PyPI's manylinux wheel (maturin 1.14.1) |
| **The image stays experimental.** The default stays `PYTHON=3.12` until turbovec publishes cp314t wheels; raggio never forks turbovec | Spec D6. A cp314t wheel request is one of the upstream drafts (ADR 0005). If turbovec's sdist does not build for cp314t, only the builder stage's build dependencies and flags may change; a fix that needs turbovec's own source is drafted upstream for the maintainer to post, and the evaluation parks (spec §3.1 G2) |
| **Build-time check** (`docker/check_image.py`, the last builder step): the interpreter is the flavour `PYTHON` names; every compiled module in the venv imports with the GIL still off; uvicorn's own loading of the app and one pass over the request path (ingest, get, list, vector/text/hybrid/filtered search, IVF attach and search, detach, delete) keep it off; `/healthz` agrees; `bm25` is `native`, from a `raggio_native` that carries the interpreter's own extension suffix (never abi3, spec §4.2) | CPython re-enables the GIL, with only a RuntimeWarning, when it imports an extension that has not declared free-threading support. The check makes that warning an error and also reads the GIL state after every import |
| **Runtime guard**: the image sets `PYTHONWARNINGS=error:The global interpreter lock (GIL) has been enabled:RuntimeWarning` | A lazy import that would re-enable the GIL fails loudly instead of silently serving with it. On a GIL build it never fires. `PYTHON_GIL=0` / `-X gil=0` hide exactly this and are refused by the check |
| **`/healthz` reports `"gil_enabled"`** (additive, spec §4.1) | What a deployment actually runs with, visible without a shell |
| **`GC_FREEZE` env var**, `0` (default) or `1`: after `resume_pending`, collect and `gc.freeze()` the startup heap, including the collections a replayed job loaded; unfreeze after shutdown | The free-threaded collector is non-generational and stop-the-world: every collection walks the whole heap. Frozen objects are skipped (+38% on the laptop's rescore bench). A frozen collection that is later evicted or deleted is still freed, by reference counting (tested); only a reference cycle created before the freeze waits for the unfreeze. A deployment knob, off until the DGX says otherwise |
| **meta.db connections without a statement cache on free-threaded builds** (`SQLITE_CACHED_STATEMENTS` in `store.py`: 0 there, sqlite3's own 128 otherwise) | With biased reference counting, a statement another thread cached is freed only when that thread next runs Python, so `stop()`'s `close()` left a zombie connection holding `meta.db`, `-wal` and `-shm` open (on Windows, deleting the collection failed). Preparing each statement again costs about 10 µs per statement on 3.14t (laptop: a 10-id hydrate 18–20 → 28–30 µs, an FTS query 151–157 → 160–176 µs), well under 1% of a search; the DGX A/B includes it |
| **Non-blocking CI job** on 3.14t, x86_64 and aarch64, with the extension required and the warning filter set; then the image check and the concurrency stress tests at 30 s | Spec D11. `continue-on-error` until 3.14t is the default image |
| **Unicode drift is measured, not fixed**: `bench/unicode_drift_probe.py` | 3.12 has Unicode 15.0.0 and 3.14 has 16.0.0. The tokenizer runs on `unicodedata` at query time, and `raggio_native` builds its tables from the interpreter it is compiled for, so each image is self-consistent but the two arms differ. On x86_64: 5,812 code points newly assigned, 1 changed category without token drift (U+1171E, Mn → Mc), 5,053 tokenize differently, all newly assigned. The probe counts the bench corpus rows that contain one, so a quality difference between arms can be attributed |

## Decision rule

The DGX A/B (spec §6, §7) runs `PYTHON=3.12` (A) and `PYTHON=3.14t` (B), built from the same
commit, as four sessions on one boot in the order A1 B1 A2 B2, each at 8 concurrent
clients (`CONCURRENCY=8`) and each followed, on the same image, by a c=16 session (spec §3.1
G7; the chain is A1 A1c16 B1 B1c16 A2 A2c16 B2 B2c16). Each session starts from the
IVF index, so every session runs the same order (IVF runs, the 8 GiB detach, flat runs), and
every run is a fresh container of its image. Each session measures 3 flat runs
(`FLAT_RUNS=3`) and 3 IVF runs after the warm-up, so the concurrent-QPS rows, which are
bimodal on the DGX, have the 3 runs a side that spec §6 asks for. `bench/dgx/compare.py`
gives the A1 → B1 and A2 → B2 tables, each headed by both arms' regime (memory cap,
`python`, `sqlite_version`, `openblas_num_threads`); a row counts as moved only where both
tables say so. A1 → A2 and B1 → B2 compare one image with itself in fresh containers: they
are the between-server variance that spec §6 puts inside the band. Each session rebuilds the
IVF index from a random sample, so the gate reads the flat (`raggio`) rows only.

Two rows can carry the win, and they are bound by different locks:

- `QPS concurrent` is vector search and its rescoring: turbovec, numpy and the Python glue
  between them. It is CPU-bound, and the GIL is the lock that free threading removes.
- `Hybrid QPS concurrent` adds the FTS5 text leg, which runs under SQLite's global memstatus
  mutex whatever the GIL does (spec D15). It carries a win only when A and B run the same
  `sqlite_version`; across two SQLite builds the difference cannot be credited to free
  threading.

B **wins** when all of these hold:

- one of those two rows is `better` in A1 → B1 and in A2 → B2 (spec §6: claim a win only if
  it exceeds the band), and neither `better` nor `worse` in A1 → A2 or B1 → B2; for
  `Hybrid QPS concurrent`, A and B also run the same `sqlite_version`, and
  `Hybrid p99 under concurrency (ms)` is present and not `worse` in either table (spec §3.1
  G5: p4's c=16 hybrid gain came with a p99 3.8–4.0x worse, and a throughput win that
  costs the tail is no win); for `QPS concurrent`, the flat runs' fast/slow states must
  split evenly (below);
- none of the flat rows QPS concurrent, p95 under concurrency, Recall@10 vs exact and
  Hybrid text-hit@10 is `worse` in both tables (QPS concurrent and p95 under concurrency
  are skipped on an uneven or unknown split, spec §3.1 G9);
- every B session's `/healthz` samples say `gil_enabled` false and every A sample says
  true, and no container log of any session has a `Traceback` or a GIL line.

The flat `QPS concurrent` row is bimodal on every interpreter (p4: about 134 against 250
QPS on 3.12, 3.14 and 3.14t alike), so a median over 3 runs can move with no interpreter
effect. CPU per query under concurrency is recorded for every run, medians included, and
classifies each flat run: high-CPU is at least 1.5x the lowest CPU per query in its
pair (p4: slow runs at 115.9–117.6 ms, fast ones at 50.8–56.8 ms). In each of A1 → B1 and
A2 → B2 the two sides must hold the same number of high-CPU runs, and every run needs a
known state. On an uneven split, or where no cpu.stat could be read, `QPS concurrent` and
`p95 under concurrency (ms)`, both measured in those flat runs, count as neither a win nor a
miss (spec §3.1 G9): the win check skips the first and the "worse in both pairs" check
skips both, and the decision records why. CPU per query is reported for every run and
never gated.

The c=16 tables (`A1 → B1 (c=16)`, `A2 → B2 (c=16)`, `A1 → A2 (c=16)`, `B1 → B2 (c=16)`)
carry the same verdicts and regime lines and are reported, not gated (spec §3.1 G7): the
decision reads the c=8 tables only. p4 saw 3.14t's hybrid gain only at c=16.
The `-c16` sessions' GIL, Traceback and `/healthz` checks are not speed tables: every
session's checks gate, c=16 included (spec §3.1 G8). Only the c=16 speed tables are
reported, not gated.

Any other flat row that is `worse` in both tables (free threading costs 1–8% of
single-thread speed, so the serial latencies may move) is listed in the decision as a
cost; it does not gate. The hybrid timing rows (Hybrid p50/p95/p99, Hybrid QPS serial,
Hybrid QPS concurrent) are SQLite-bound: one that does not scale, or is `worse` in both
tables, is listed as a SQLite-bound cost with both `sqlite_version`s. It is never read as a
free-threading failure and never parks the evaluation on its own (spec D15).

Noise (spec §6, D16): every image runs `OPENBLAS_NUM_THREADS=1`, and each table shows it
for both arms. Without it, numpy's OpenBLAS spinners push query threads onto the A725 cores,
the main DGX noise source. Rootless podman cannot pin cores with `--cpuset-cpus`, so thread
placement between the X925 and A725 cores still moves the serial rows. A serial-latency cost
is attributed to free threading only if both arms are re-measured pinned in-process with
`os.sched_setaffinity` to the X925 cores (cpus 5–9, 15–19); otherwise it is recorded as
unattributed.

The outcome:

- **Continue** when B wins and turbovec's latest PyPI release ships a cp314t wheel: keep the
  image and propose making it the default in a separate decision.
- **Hold** when B wins but no cp314t wheel exists: do not continue. Keep the build arg and
  the CI job, and re-run this A/B when turbovec releases a cp314t wheel; an own turbovec
  build never becomes the default (spec D6).
- **Park** otherwise: keep the build arg and the CI job, and stop measuring until a
  turbovec or CPython release changes the inputs. The evaluation also parks, with no A/B,
  when turbovec's sdist does not build for cp314t on the DGX and only a change to
  turbovec's own source would fix it (spec D6, §3.1 G2).

Recorded, not gated: the IVF rows; `bench/ivf_fanout_probe.py fanout` in both images on one
IVF index (whether 3.14t's shard pool oversubscribes the cores); the optional arms C1
(`PYTHON=3.14 UV_NO_BINARY_PACKAGE=turbovec`, a local abi3 turbovec build with the GIL) and
D1 (B with `GC_FREEZE=1`); each image's `sqlite_version`, `OPENBLAS_NUM_THREADS` and
`raggio_native` build. Making 3.14t the default is out of scope, and so is `GC_FREEZE`'s
default. So is building a custom SQLite (spec D15).

## Consequences

- `podman build --build-arg PYTHON=3.14t` compiles turbovec and `raggio_native` in the
  builder (about 2 minutes for the whole image on a 3-CPU x86_64 podman machine); the
  runtime stage copies no toolchain. On the DGX (aarch64), probe p4's 3.14t image, without
  `raggio_native`, built in 28.7 s [28.2..29.3] over 3 `--no-cache` builds with pre-pulled
  bases and no uv cache; the dependency step, turbovec's compile, took 17–19 s of that.
  The 3.14 GIL image built in 10.6 s. The images are 323.6 MB (3.14t) and 320.9 MB (3.14).
  `raggio_native`'s cp314t compile time on the DGX was not measured.
- The 3.14t image runs SQLite 3.53.1, the build uv 0.12.19's interpreters bundle (p4), as
  the 3.12 image does.
- The 3.14t CI job needs Rust on the runner and takes longer than the other jobs; it cannot
  fail the workflow.
- `GC_FREEZE=1` keeps the startup heap alive for the process lifetime (it would be anyway:
  modules, settings, the manager).

## Verification

Pending: Plan G's DGX ABAB records here the boot, the images (with each one's
`raggio_native` build, SQLite and `OPENBLAS_NUM_THREADS`), the `gil_enabled` and log checks
of every session, the `compare.py` tables A1 → B1, A2 → B2, A1 → A2 and B1 → B2 with their
regime lines and each flat run's CPU per query, their four `(c=16)` twins (reported, not
gated, spec §3.1 G7), the IVF fan-out probe in both images, the Unicode drift diff and scan
(`bench/unicode_drift_probe.py`), the upstream wheels, and the decision (Continue, Hold or
Park) under the rule above. If turbovec's sdist does not build for cp314t on the DGX and
only a change to turbovec's own source would fix it (spec §3.1 G2), probe p4's build
failure and the Park are recorded here instead.
````

3b. In `README.md`, insert this paragraph after the first podman block. That block ends with `podman run -p 8000:8000 -v raggio-data:/data -e ROOT_API_KEY=change-me raggio` and its closing fence. Leave a blank line on each side:

```markdown
The image runs CPython 3.12. An experimental free-threaded CPython 3.14t image builds from
the same Dockerfile, for evaluation only ([ADR 0006](docs/adr/0006-free-threaded-python.md)):
`podman build --build-arg PYTHON=3.14t -t raggio:py314t .`. It compiles turbovec from source
and fails the build if anything re-enables the GIL; `/healthz` reports `gil_enabled`.
```

3c. In `README.md`, append this row to the env table, after its last row (on the sandbox base, the `IVF_SEARCH_THREADS` row):

```markdown
| `GC_FREEZE` | `0` | `1` freezes the startup heap so garbage collections skip it (experimental 3.14t image) |
```

3d. In `docs/getting-started.md`, insert this note right before `!!! tip "No embedding endpoint?"`, followed by a blank line:

```markdown
!!! note "Experimental: free-threaded Python"
    `podman build --build-arg PYTHON=3.14t -t raggio:py314t .` builds the same image on
    free-threaded CPython 3.14t, for evaluation only
    ([ADR 0006](adr/0006-free-threaded-python.md)). It compiles turbovec from source and
    fails the build if anything re-enables the GIL; `/healthz` reports `gil_enabled`.
```

3e. In `docs/getting-started.md`, append this row to the env table, after its last row (on the sandbox base, the `IVF_SEARCH_THREADS` row):

```markdown
| `GC_FREEZE` | `0` | `1` collects and freezes the startup heap before serving, so later garbage collections skip it. Meant for the experimental 3.14t image, whose collector walks the whole heap every time |
```

3f. In `docs/api.md`, under `` `GET /healthz` — no auth ``, replace the `**200**` line with the lines below. On main it reads ``**200** `{"status": "ok", "resident_collections": ["kb"]}` ``; Plan E may already have added `bm25`.

```markdown
**200** `{"status": "ok", "resident_collections": ["kb"], "bm25": "native", "gil_enabled": true}`

`bm25` is `"native"` when BM25 rescoring runs in `raggio_native`, `"python"` otherwise.
`gil_enabled` is `false` only on the experimental free-threaded 3.14t image
([ADR 0006](adr/0006-free-threaded-python.md)).
```

If Plan E already added a sentence that explains `bm25`, keep that one and add only the `gil_enabled` sentence. The test needs the example to show exactly the keys that `/healthz` returns. If Plan C or D added a key, include it too.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `25 passed, 4 skipped`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `29 passed`.

Run: `uv run --group docs mkdocs build --strict -d "$(mktemp -d)"`. Expected: it exits 0 with no warnings. `mkdocs.yml` builds `docs/adr/` without listing it in the nav, so the ADR is reachable through the two links.

Run `uv run pytest -q`. Expected: all green (sandbox: `307 passed, 4 skipped`). Run the full 3.14t suite. Expected: all green (sandbox: `302 passed, 2 skipped`).

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0006-free-threaded-python.md README.md docs/getting-started.md docs/api.md tests/test_freethreading.py
git commit -m "Document the free-threaded image (ADR 0006)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: DGX scripts, checked on the laptop

Tasks 11 and 12 run on gn100 with these scripts. They live outside the repo, in `D:/DEV/_scratch/dgx/planG/`, and are never committed. Task 11 copies them to `~/raggio-bench/`, where every name Plan G creates starts with `g-`. `g-check.py` exercises the whole recording pipeline on the laptop with synthetic sessions:
- `g-render.py` turns the ABAB outputs into ADR 0006's Verification;
- `g-decide.py` adds the upstream wheels and applies the decision rule;
- the Task 12 test then reads the result.

The shell scripts cannot run on the laptop, because they need podman, the bench volume and Plan A's session. They were dry-run in the sandbox with stubs (see the Self-Review). Here they are syntax-checked only.

**Files (all outside the repo):**
- Create: `D:/DEV/_scratch/dgx/planG/g-check.py`, the check;
- Create: `D:/DEV/_scratch/dgx/planG/t12-section.py`, Task 12's test section, which Task 12 appends to `tests/test_freethreading.py`;
- Create: `D:/DEV/_scratch/dgx/planG/`, the nine scripts: `g-render.py`, `g-decide.py`, `g-image-facts.py`, `g-session.sh`, `g-ensure-ivf.sh`, `g-abab.sh`, `g-build.sh`, `g-facts.sh`, `g-fanout.sh`.

**Interfaces:**
- Consumes:
  - Plan A's `bench/dgx/lib.sh`:
    - `set -euo pipefail`, `log`;
    - `LIMIT=2549619`, `CONTAINER=bench-tv`, `VOLUME=bench-tv`, `PORT=18000`, `MEMORY=4g`, `API_KEY=bench`;
    - `RAGGIO_DIR=~/raggio`, `BENCH_HOME=~/raggio-bench`, `PENDING_WAIT_S`.
  - Plan A's `bench/dgx/session.sh`. It is run as `FLAT_RUNS=3 IMAGE=<image> [ADOPT=1] bash session.sh <label>`.
    - It refuses a label whose `~/raggio-bench/<label>` exists. It holds no lock file (Plan A has no `active-session`), so G finds a running session by its process, `pgrep -f 'bash ([^ ]*/)?bench/dgx/session[.]sh( |$)'`, and G's only lock file is its own `~/raggio-bench/g-active`.
    - From an IVF start it runs the IVF runs 0–3, the 8 GiB detach and the flat runs 0–3, into `~/raggio-bench/<label>/run<N>-<engine>.json`, and leaves `bench-tv` running. Every run is a fresh container of the image under test.
    - It writes `~/raggio-bench/<label>/regime.json` and `image.json` before the first run.
  - Plan A's `bench/dgx/compare.py`:
    - `render(base_dir, cand_dir) -> str`, a table with the columns `| Engine | Metric | base median | base band | cand median | cand band | verdict |` and the verdicts `better`, `worse`, `within band`, `no band` and `not claimable`;
    - `beyond_band(base_dir, cand_dir) -> {"better": [(engine, key)], "worse": [...]}`;
    - the flat engine name `raggio`;
    - the metric labels `Hybrid QPS concurrent`, `QPS concurrent`, `p95 under concurrency (ms)`, `Hybrid p99 under concurrency (ms)`, `CPU per query under concurrency (ms)`, `Hybrid CPU per query under concurrency (ms)` (all lower-is-better; the two CPU rows are absent where `cpu.stat` is unreadable), `Recall@10 vs exact`, `Hybrid text-hit@10` and `Search p50 (ms)`, and the hybrid timing labels of `SQLITE_BOUND` (Plan A Task 5 adds `Hybrid first 10 queries, slowest (ms)` and `Hybrid p99 without the first 10 (ms)`);
    - the lines `render` prints above an A/B table: `Regime (baseline): page_cache=…, first_start=…, memory=…, memory_swap=…, concurrency=…, python=…, sqlite_version=…, openblas_num_threads=…`, the same for `candidate`, and, when the arms differ outside `first_start`, `The arms differ in <keys>: a re-baseline across them, not a same-regime A/B (spec §6).`;
    - the note `Reported, not claimed (spec §6 needs >= 3 runs per side; FLAT_RUNS=3): …` under a table whose vector concurrent-QPS row has fewer than 3 counted runs on a side;
    - `regime()`, which reads `regime.json` and `image.json` from the session directory (`image.json` = `{"python", "sqlite_version", "turbovec", "openblas_num_threads"}`, read inside the image) and prints `unknown` for a missing label.
  - Plan A's `lib.sh` reads `FLAT_RUNS` from the environment (default 2). `g-session.sh` passes `FLAT_RUNS=3`.
  - Plan A's `lib.sh` reads `CONCURRENCY` from the environment (default 8) and passes `--concurrency "$CONCURRENCY"` to every `bench.py` run (spec §4.2). `g-abab.sh` sets `CONCURRENCY=16` for the c=16 sessions and 8 for the others, and `g-session.sh` hands it to `session.sh` (spec §3.1 G7).
  - Plan F's `bench/ivf_fanout_probe.py fanout --data PATH --threads LIST`: read-only, seed 7. It prints a `GATE grouped vs plain at N: WIN|LOSS` line.
  - This plan's pieces:
    - `/healthz`'s `gil_enabled` (Task 1);
    - `GC_FREEZE` (Task 2);
    - the `PYTHON` and `UV_NO_BINARY_PACKAGE` build args and the runtime guard (Task 6);
    - `bench/unicode_drift_probe.py` (Task 8);
    - ADR 0006 and its `Pending:` Verification (Task 9).
- Produces the scripts. On gn100, each detached script ends its log with one status line:

| Script | Last line |
|---|---|
| `g-build.sh <sha> <C:0\|1> <D:0\|1>` | `== BUILD_DONE` or `== BUILD_FAILED <what>` |
| `g-facts.sh <sha> <C> <D>` | `== FACTS_DONE` or `== FACTS_FAILED <what>` |
| `g-fanout.sh <sha>` | `== FANOUT_DONE` or `== FANOUT_FAILED <what>` |
| `g-session.sh <label> <image> <cap>` | `g-session <label>: exit <status>` |
| `g-abab.sh <sha> <cap> <C1:0\|1> <D1:0\|1>` | `<ts> g-abab: done`, or `<ts> g-abab: <label> ended with '<last line>': stopping` |

  - Image tags: A is `localhost/raggio:<sha>` (built by `deploy.sh`), B is `…:<sha>-py314t`, C is `…:<sha>-py314`, and D is `…:<sha>-py314t-gcfreeze`.
  - Session labels, in the chain's order: `g-a1-py312`, `g-a1-py312-c16`, `g-b1-py314t`, `g-b1-py314t-c16`, `g-a2-py312`, `g-a2-py312-c16`, `g-b2-py314t`, `g-b2-py314t-c16`, and optionally `g-c1-py314` and `g-d1-py314t-gcfreeze`. A `-c16` session runs its arm session's image at `CONCURRENCY=16` right after it, with its own cap (spec §3.1 G1, G7); every other session runs at `CONCURRENCY=8`.
  - `g-render.py` writes `~/raggio-bench/g-verification.md`, with these subsections:
    - `### Images`;
    - `### GIL and logs`, one row per session, the `-c16` ones included (spec §3.1 G8);
    - `### A1 → B1`, `### A2 → B2`, `### A1 → A2`, `### B1 → B2`, and optionally `### A2 → C1` and `### B2 → D1`;
    - `### A1 → B1 (c=16)`, `### A2 → B2 (c=16)`, `### A1 → A2 (c=16)`, `### B1 → B2 (c=16)`: the same pairs from the `-c16` sessions, reported and not gated (spec §3.1 G7); `abab_decision` drops every section whose title ends in ` (c=16)`;
    - `### IVF fan-out`;
    - `### Unicode drift`.
  - `g-decide.py` appends `### Upstream wheels` and `### Decision`.

- [ ] **Step 1: Write the failing check**

Create `D:/DEV/_scratch/dgx/planG/g-check.py`:

```python
"""Plan G Task 10: the DGX recording pipeline, checked on the laptop with synthetic sessions.

Per scenario: a synthetic ~/raggio-bench, g-render.py over it from a `git archive HEAD`
copy of the repo with t12-section.py appended to tests/test_freethreading.py, g-decide.py
with a synthetic PyPI record, then the Task 12 test. The repo itself is only read.
Run from the repo root:
    uv run python D:/DEV/_scratch/dgx/planG/g-check.py
"""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path.cwd()
BOOT = "0f6d1c5e-2b7a-4c1e-9d3f-5a8b7c6d4e21"
BASE = {"lat_p50": 21.0, "lat_p95": 30.0, "lat_p99": 40.0, "qps_serial": 47.0,
        "qps_concurrent": 116.0, "lat_c_p95": 60.0, "lat_filtered_p50": 28.0,
        "lat_filtered_p95": 40.0, "recall_at_10": 0.95, "hybrid_p50": 88.0, "hybrid_p95": 120.0,
        "hybrid_p99": 150.0, "hybrid_qps_serial": 9.8, "hybrid_qps_concurrent": 13.8,
        "hybrid_text_hit_rate": 0.984, "mem_under_load_mb": 2500.0, "cold_start_s": 30.0,
        # spec §3.1 G5: every flat run in its fast state (p4: 50.8-56.8 ms), so the split is even
        "hybrid_c_p99": 700.0, "cpu_ms_per_q_c": 54.0, "hybrid_cpu_ms_per_q_c": 360.0}
EXACT = ("recall_at_10", "hybrid_text_hit_rate")  # no run-to-run noise in the synthetic runs
JITTER = (-0.01, 0.0, 0.01)  # run n scales by 1 + JITTER[n % 3]: every session has the same band
WHEELS = ["turbovec-1.0.0-cp39-abi3-manylinux_2_28_aarch64.whl",
          "turbovec-1.0.0-cp39-abi3-manylinux_2_28_x86_64.whl", "turbovec-1.0.0.tar.gz"]
# a future PyPI upload, so manylinux: PyPI refuses a plain linux_aarch64 tag. The images'
# own sdist builds are linux_aarch64 (maturin, not audited), as the image lines below show
FT_WHEEL = "turbovec-1.0.0-cp314-cp314t-manylinux_2_28_aarch64.whl"
# name: (synthetic session knobs, cp314t wheel on PyPI, expected decision or None when the
# Task 12 test must fail, expected costs)
SCENARIOS = {
    "hold": ({"qps": 1.5}, False, "Hold", []),
    "continue": ({"qps": 1.5}, True, "Continue", []),
    "park": ({}, False, "Park", []),
    "cost": ({"qps": 1.5, "lat_p50": 1.2}, False, "Hold", ["Search p50 (ms)"]),
    "hitloss": ({"qps": 1.5, "hit": -0.05}, False, "Park", ["Hybrid text-hit@10"]),
    # spec D15: a hybrid row that does not scale is a SQLite-bound cost, never a Park reason
    "sqlite-bound": ({"qps": 1.5, "hybrid": 0.8}, False, "Hold", ["Hybrid QPS concurrent"]),
    "hybrid-win": ({"hybrid": 1.5}, False, "Hold", []),
    "hybrid-other-sqlite": ({"hybrid": 1.5, "b_sqlite": "3.51.0"}, False, "Park", []),
    # spec §6: a win inside the between-server variance (A1 → A2 moves too) is not claimed
    "noisy": ({"qps": 1.5, "a2_qps": 1.3}, False, "Park", []),
    "gil-on": ({"qps": 1.5, "b_gil": True}, False, "Park", []),
    "traceback": ({"qps": 1.5, "traceback": True}, False, "Park", []),
    "with-c1": ({"qps": 1.5, "c1": True}, False, "Hold", []),
    # spec §3.1 G5: a hybrid QPS win with a worse concurrent p99 is no win (p4: 3.8-4.0x worse)
    "hybrid-p99-worse": ({"hybrid": 1.5, "hybrid_c_p99": 4.0}, False, "Park",
                         ["Hybrid p99 under concurrency (ms)"]),
    # G5: two of A's three flat runs in the slow state, none of B's: the split, not the
    # interpreter, can move the median, so QPS concurrent is neither a win nor a miss
    "flat-split-uneven": ({"qps": 1.5, "slow": {"g-a1-py312": 2, "g-a2-py312": 2}}, False, "Park", []),
    # spec §3.1 G9: B's QPS concurrent and p95 under concurrency are worse in both pairs, but on
    # the same uneven split as above, so neither row is a cost: the hybrid win stands, Hold
    "flat-split-uneven-worse": ({"hybrid": 1.5, "qps": 0.5, "lat_c": 2.0,
                                 "slow": {"g-a1-py312": 2, "g-a2-py312": 2}}, False, "Hold", []),
    # G5: one slow run in every session, so each pair splits 1 high/2 low on both sides
    "flat-split-even": ({"qps": 1.5, "slow": dict.fromkeys(
        ("g-a1-py312", "g-b1-py314t", "g-a2-py312", "g-b2-py314t"), 1)}, False, "Hold", []),
    # G5 and spec §4.2: no readable cpu.stat, so no CPU rows and no known state
    "cpu-missing": ({"qps": 1.5, "cpu": False}, False, "Park", []),
    # spec §3.1 G7: the c=16 tables are reported, not gated. Here B is worse beyond the band in
    # all four of them (QPS concurrent, Hybrid QPS concurrent, and the hybrid p99 4x): read
    # as gate tables, "worse in both" would turn this Hold into a Park
    "c16-not-gated": ({"qps": 1.5, "c16": {"qps": 0.5, "hybrid": 0.5, "hybrid_c_p99": 4.0}},
                      False, "Hold", []),
    # spec §3.1 G8: the checks gate for every session. A Traceback only in B1's c=16 session
    # (its speed tables stay reported, not gated) still parks; the reason names that session
    "c16-traceback": ({"qps": 1.5, "traceback": "g-b1-py314t-c16"}, False, "Park", []),
    "two-boots": ({"qps": 1.5, "boot_after": "5b1e0c4d-9a8f-4e7d-8c6b-3a2f1e0d9c8b"}, False, None, []),
    "two-shas": ({"qps": 1.5, "b_sha": "def5678"}, False, None, []),
    "blas-differs": ({"qps": 1.5, "b_blas": "unset"}, False, None, []),  # spec D16
    "thin": ({"qps": 1.5, "flat_runs": 2}, False, None, []),  # spec §6: >= 3 runs a side
}
# what one of g-decide.py's reason lines must contain, for the scenarios that turn on one
REASONS = {"hybrid-p99-worse": "`Hybrid p99 under concurrency (ms)` is ['worse', 'worse']",
           "flat-split-uneven": "A1 → B1: A1 2 high/1 low, B1 0 high/3 low",
           "c16-traceback": "g-b1-py314t-c16: 1 Traceback or GIL lines",
           "cpu-missing": "A1 → B1: no CPU per query under concurrency for every flat run"}


def synth(home: Path, qps=1.0, hybrid=1.0, lat_p50=1.0, hit=0.0, a2_qps=1.0, b_sqlite="3.53.1",
          b_blas="1", flat_runs=3, b_gil=False, traceback=False, c1=False, boot_after=BOOT,
          b_sha="abc1234", hybrid_c_p99=1.0, slow=None, cpu=True, c16=None, lat_c=1.0):
    """What Task 12's sessions leave in ~/raggio-bench, with B's metrics scaled. `slow` puts
    the first n measured flat runs of a session in p4's slow state; with `cpu` false no run
    has the CPU rows. `c16` scales B's qps, hybrid and hybrid_c_p99 in the four -c16 sessions
    (spec §3.1 G7); by default they equal A's. `lat_c` scales B's lat_c_p95 in the c=8 sessions.
    `traceback` True puts a Traceback line in one log of every B session, the -c16 ones included
    (spec §3.1 G8); a session label puts it in that session only."""
    sessions = [("g-a1-py312", "A"), ("g-b1-py314t", "B"), ("g-a2-py312", "A"), ("g-b2-py314t", "B")]
    sessions += [(f"{label}-c16", arm) for label, arm in sessions]  # G8: they get /healthz and logs too
    sessions += [("g-c1-py314", "C")] if c1 else []
    c16 = {"qps": 1.0, "hybrid": 1.0, "hybrid_c_p99": 1.0, **(c16 or {})}
    for label, arm in sessions:
        k = c16 if label.endswith("-c16") else {"qps": qps, "hybrid": hybrid, "hybrid_c_p99": hybrid_c_p99}
        (home / label).mkdir(parents=True)
        # what Plan A's session.sh records next to the runs (compare.py's Regime lines)
        (home / label / "regime.json").write_text(json.dumps(
            {"page_cache": "host-warm", "first_start": "host-warm", "memory": "4g",
             "memory_swap": "host-default", "detach_memory": "8g",
             "concurrency": "16" if label.endswith("-c16") else "8"}), encoding="utf-8")
        (home / label / "image.json").write_text(json.dumps(
            {"python": "3.12.14" if arm == "A" else "3.14.7",
             "sqlite_version": b_sqlite if arm == "B" else "3.53.1", "turbovec": "1.0.0",
             "openblas_num_threads": b_blas if arm == "B" else "1"}), encoding="utf-8")
        # run0 is the warm-up; FLAT_RUNS and IVF_RUNS=3 measured runs follow it
        for engine, runs in (("raggio", flat_runs + 1), ("raggio-ivf", 4)):
            for n in range(runs):
                m = {k: v if k in EXACT else v * (1 + JITTER[n % 3]) for k, v in BASE.items()}
                if arm == "B":
                    m["qps_concurrent"] *= k["qps"]
                    m["hybrid_qps_concurrent"] *= k["hybrid"]
                    m["lat_p50"] *= lat_p50
                    m["lat_c_p95"] *= lat_c if k is not c16 else 1.0
                    m["hybrid_text_hit_rate"] += hit
                    m["hybrid_c_p99"] *= k["hybrid_c_p99"]
                if label == "g-a2-py312":
                    m["qps_concurrent"] *= a2_qps
                if not cpu:  # cpu.stat unreadable: the CPU rows are absent, never zero (spec §4.2)
                    del m["cpu_ms_per_q_c"], m["hybrid_cpu_ms_per_q_c"]
                elif engine == "raggio" and 1 <= n <= (slow or {}).get(label, 0):
                    m["cpu_ms_per_q_c"] = 116.0 * (1 + JITTER[n % 3])  # p4: 115.9-117.6 ms
                (home / label / f"run{n}-{engine}.json").write_text(json.dumps({engine: m}), encoding="utf-8")
        gil = "false" if arm == "B" and not b_gil else "true"
        body = '{"status":"ok","resident_collections":["bench"],"bm25":"native","gil_enabled":%s}' % gil
        (home / f"{label}.healthz.log").write_text(
            "".join(f"2026-10-01T10:{i:02d}:00Z {body if i % 7 else ''}\n" for i in range(40)),
            encoding="utf-8")
        (home / f"{label}.logs").mkdir()
        for i in range(8):
            log = "INFO:     Application startup complete.\n"
            if traceback in (True, label) and arm == "B" and i == 3:
                log += "Traceback (most recent call last):\n"
            (home / f"{label}.logs" / f"{i:012x}.log").write_text(log, encoding="utf-8")
    (home / "g-boot-before.txt").write_text(BOOT + "\n", encoding="utf-8")
    (home / "g-boot-after.txt").write_text(boot_after + "\n", encoding="utf-8")
    images = [("A", "localhost/raggio:abc1234", "Python 3.12.14, GIL enabled, turbovec 1.0.0"
               " (cp39-abi3-manylinux_2_28_aarch64), raggio_native 0.1.0"
               " (raggio_native.cpython-312-aarch64-linux-gnu.so), SQLite 3.53.1,"
               " OPENBLAS_NUM_THREADS 1, Unicode 15.0.0, raggio_native Unicode 15.0.0"),
              ("B", f"localhost/raggio:{b_sha}-py314t", "Python 3.14.7t, GIL disabled, turbovec 1.0.0"
               " (cp314-cp314t-linux_aarch64), raggio_native 0.1.0"
               f" (raggio_native.cpython-314t-aarch64-linux-gnu.so), SQLite {b_sqlite},"
               f" OPENBLAS_NUM_THREADS {b_blas}, Unicode 16.0.0, raggio_native Unicode 16.0.0")]
    images += [("C", "localhost/raggio:abc1234-py314", "Python 3.14.7, GIL enabled, turbovec 1.0.0"
                " (cp39-abi3-linux_aarch64), raggio_native 0.1.0"
                " (raggio_native.cpython-314-aarch64-linux-gnu.so), SQLite 3.53.1,"
                " OPENBLAS_NUM_THREADS 1, Unicode 16.0.0, raggio_native Unicode 16.0.0")] if c1 else []
    (home / "g-images.txt").write_text("".join("\t".join(i) + "\n" for i in images), encoding="utf-8")
    for arm in "AB":
        (home / f"g-fanout-{arm}.txt").write_text(
            "nq=8 nprobe=16 k=50: 128 plain calls/batch\n"
            "serial r1/r2 1.15/1.18 ms; chosen pool size 1 (default here 20);"
            " GATE grouped vs plain at 1: LOSS\n", encoding="utf-8")
    (home / "g-drift-summary.txt").write_text(
        "15.0.0 -> 16.0.0: 5812 newly assigned, 1 changed, 5053 with token drift\n", encoding="utf-8")
    (home / "g-drift-scan.json").write_text(json.dumps(
        {"rows": 2549619, "rows_with_drift": 12, "rows_with_token_drift": 9,
         "first_rows_with_drift": [5]}) + "\n", encoding="utf-8")


def run(args, cwd, env=None, stdin=None):
    return subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {})}, stdin=stdin,
                          capture_output=True, text=True, encoding="utf-8", timeout=300)


def check(name, knobs, ft_wheel, want, want_costs, tmp: Path) -> str:
    repo, home = tmp / "repo", tmp / "home"
    archive = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=REPO,
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(repo, filter="data")
    tests = repo / "tests" / "test_freethreading.py"
    tests.write_text(tests.read_text(encoding="utf-8")
                     + (HERE / "t12-section.py").read_text(encoding="utf-8"), encoding="utf-8")
    synth(home, **knobs)
    env = {"BENCH_HOME": str(home), "PYTHONPATH": str(repo / "src")}
    r = run([sys.executable, str(HERE / "g-render.py")], repo, env)
    assert r.returncode == 0, f"{name}: g-render.py failed\n{r.stderr}"
    rendered = (home / "g-verification.md").read_text(encoding="utf-8")
    for pair in ("A1 → B1", "A2 → B2", "A1 → A2", "B1 → B2"):  # spec §3.1 G7
        assert f"\n### {pair} (c=16)\n" in rendered, f"{name}: no {pair} (c=16) table"
    if name == "c16-not-gated":  # the tables that would flip the verdict are really there
        table = rendered.split("\n### A1 → B1 (c=16)\n", 1)[1].split("\n### ", 1)[0]
        assert any(line.startswith("| raggio | QPS concurrent |") and line.rstrip().endswith("| worse |")
                   for line in table.splitlines()), f"{name}: the c=16 table shows no worse QPS concurrent"
    pypi = tmp / "turbovec.json"
    files = WHEELS + ([FT_WHEEL] if ft_wheel else [])
    pypi.write_text(json.dumps({"info": {"version": "1.0.0"},
                                "releases": {"1.0.0": [{"filename": f} for f in files]}}), encoding="utf-8")
    with open(HERE / "g-decide.py", encoding="utf-8") as script:
        r = run([sys.executable, "-", str(home / "g-verification.md"), str(pypi)], repo, env, script)
    assert r.returncode == 0, f"{name}: g-decide.py failed\n{r.stderr}"
    decision, costs = r.stdout.splitlines()[0], r.stdout.splitlines()[-1]
    t = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "tests/test_freethreading.py::test_adr_0006_records_the_dgx_abab_and_its_decision"], repo, env)
    if want is None:
        assert t.returncode == 1, f"{name}: the Task 12 test passed on a broken record\n{t.stdout}"
        return f"{name}: Task 12 test fails, as it must"
    assert decision == want, f"{name}: decided {decision}, expected {want}\n{r.stdout}"
    assert costs == f"costs: {want_costs}", f"{name}: {costs}, expected {want_costs}"
    reason = REASONS.get(name)
    if reason:
        assert any(reason in line for line in r.stdout.splitlines()[1:-1]), \
            f"{name}: no reason names {reason!r}\n{r.stdout}"
    assert t.returncode == 0, f"{name}: the Task 12 test failed\n{t.stdout}"
    return f"{name}: {decision}, {costs}, Task 12 test passes" + (", reason checked" if reason else "")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    for name, (knobs, ft_wheel, want, want_costs) in SCENARIOS.items():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            print(check(name, knobs, ft_wheel, want, want_costs, Path(tmp)), flush=True)
    print(f"Plan G DGX scripts OK in {HERE}")


if __name__ == "__main__":
    main()
```

Create `D:/DEV/_scratch/dgx/planG/t12-section.py`. It starts with two empty lines, so that the appended section sits two blank lines below the Task 9 test:

````python


# ---- ADR 0006: the DGX ABAB and its decision ----

# the rows that can carry B's win: `better` in A1 → B1 and A2 → B2, and neither `better` nor
# `worse` in A1 → A2 and B1 → B2 (spec §6: the band includes between-server variance).
# QPS concurrent is vector search and its rescoring, CPU-bound: where the GIL was the lock.
GATE_BETTER = ("QPS concurrent", "Hybrid QPS concurrent")
GATE_NOT_WORSE = ("QPS concurrent", "p95 under concurrency (ms)", "Recall@10 vs exact",
                  "Hybrid text-hit@10")
# the two rows of the same flat concurrent runs, which the fast/slow split moves (spec §3.1 G9)
FLAT_CONCURRENT = ("QPS concurrent", "p95 under concurrency (ms)")
# the hybrid timing rows run FTS5 under SQLite's global memstatus mutex, whatever the GIL does
# (spec D15): never read as a free-threading failure, and a win only on equal sqlite_versions
SQLITE_BOUND = ("Hybrid p50 (ms)", "Hybrid p95 (ms)", "Hybrid p99 (ms)",
                "Hybrid first 10 queries, slowest (ms)", "Hybrid p99 without the first 10 (ms)",
                "Hybrid QPS serial", "Hybrid QPS concurrent")
# spec §3.1 G5: a Hybrid QPS concurrent win needs this row present and not `worse` in either
# cross pair (p4: 3.14t's c=16 hybrid gain came with a p99 3.8-4.0x worse)
HYBRID_P99 = "Hybrid p99 under concurrency (ms)"
# spec §3.1 G5: the flat QPS concurrent row is bimodal on every interpreter (p4: slow runs at
# 115.9-117.6 ms CPU per query, fast ones at 50.8-56.8 ms). A flat run is high-CPU at >= 1.5x
# the lowest CPU per query in its cross pair; the row wins only on an even split.
HIGH_CPU = 1.5
FREE_THREADED_ARMS = {"A": False, "B": True, "C": False, "D": True}  # C: 3.14, D: 3.14t GC_FREEZE=1
OUTCOMES = ("Continue", "Hold", "Park")
CROSS_PAIRS, SAME_ARM_PAIRS = ("A1 → B1", "A2 → B2"), ("A1 → A2", "B1 → B2")
C16 = " (c=16)"  # suffix of the c=16 tables' titles: reported, never read by the decision (G7)


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _flat(section):
    """{metric: (base median, cand median, verdict)}: the flat rows of a compare.py A/B table."""
    return {c[1]: (c[2], c[4], c[6]) for c in map(_cells, section.splitlines())
            if len(c) == 7 and c[0] == "raggio"}


def _gil_rows(section):
    """[(session, arm, /healthz answers, gil_enabled false, gil_enabled true, logs, bad lines)]"""
    return [(c[0], c[1], *map(int, c[2:])) for c in map(_cells, section.splitlines())
            if len(c) == 7 and c[1] in FREE_THREADED_ARMS]


def _json_block(section):
    import json

    return json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])


def _regimes(section):
    """{"baseline" | "candidate": {label: value}} from compare.py's `Regime (...)` lines."""
    return {side: dict(kv.split("=", 1) for kv in labels.split(", "))
            for side, labels in re.findall(r"^Regime \((baseline|candidate)\): (.+)$", section, re.M)}


def _differ(section):
    """The regime labels compare.py says the two arms of a table differ in."""
    line = re.search(r"^The arms differ in (.+?): a re-baseline", section, re.M)
    return line[1].split(", ") if line else []


def sqlite_versions(sections):
    """(A's, B's) sqlite_version over the A1 → B1 and A2 → B2 tables (spec D15)."""
    regimes = [_regimes(sections[pair]) for pair in CROSS_PAIRS]
    return tuple(", ".join(sorted({r[side]["sqlite_version"] for r in regimes}))
                 for side in ("baseline", "candidate"))


def abab_sections(adr):
    """{title: body} of the ### subsections of ADR 0006's Verification."""
    verification = adr.split("\n## Verification\n", 1)[1]
    return dict(re.findall(r"^### (.+?)\n(.*?)(?=^### |\Z)", verification, re.M | re.S))


def _flat_cpu(section):
    """{"baseline" | "candidate": [CPU per query under concurrency (ms) of each measured flat
    run, None where cpu.stat was unreadable]}: the json block g-render.py writes under a table.
    {} when the block is missing."""
    try:
        return _json_block(section)["cpu_ms_per_q_c"]
    except (IndexError, KeyError, TypeError, ValueError):
        return {}


def uneven_states(sections):
    """Why the flat runs' fast/slow state keeps `QPS concurrent` from counting (spec §3.1 G5):
    per cross pair, an unknown state or an uneven split of high-CPU runs."""
    why = []
    for pair in CROSS_PAIRS:
        cpu = _flat_cpu(sections[pair])
        runs = [cpu.get(side) or [] for side in ("baseline", "candidate")]
        values = runs[0] + runs[1]
        if not (runs[0] and runs[1]) or None in values:
            why.append(f"{pair}: no CPU per query under concurrency for every flat run, so their"
                       " fast/slow state is unknown")
            continue
        high = [sum(v >= HIGH_CPU * min(values) for v in r) for r in runs]
        if high[0] != high[1]:
            split = ", ".join(f"{s} {h} high/{len(r) - h} low"
                              for s, h, r in zip(pair.split(" → "), high, runs))
            why.append(f"{pair}: {split} (high-CPU: >= {HIGH_CPU}x the pair's lowest CPU per query)")
    return why


def worse_in_both(sections):
    """The flat metrics compare.py calls `worse` in both A1 → B1 and A2 → B2."""
    pairs = [_flat(sections[pair]) for pair in CROSS_PAIRS]
    # spec §3.1 G9: both rows come from the same flat runs, so on an uneven or unknown
    # fast/slow split (the one rule that keeps `QPS concurrent` from winning) they count
    # neither as a win nor as a cost; gate_rows, abab_decision and g-decide.py all read this
    skip = FLAT_CONCURRENT if uneven_states(sections) else ()
    return [m for m in pairs[0] if m not in skip
            and all(p.get(m, ("", "", ""))[2] == "worse" for p in pairs)]


def gate_rows(sections):
    """(the GATE_BETTER rows that carry B's win, why each other one does not)."""
    cross = [_flat(sections[pair]) for pair in CROSS_PAIRS]
    same = [_flat(sections[pair]) for pair in SAME_ARM_PAIRS]
    sqlite_a, sqlite_b = sqlite_versions(sections)
    p99 = [p.get(HYBRID_P99, ("", "", "missing"))[2] for p in cross]
    wins, misses = [], []
    for m in GATE_BETTER:
        verdicts = [p.get(m, ("", "", "-"))[2] for p in cross]
        noise = [p.get(m, ("", "", "-"))[2] for p in same]
        uneven = uneven_states(sections) if m == "QPS concurrent" else []
        if uneven:  # an uneven split moves the median with no interpreter effect (p4)
            misses.append(f"{m}: {'; '.join(uneven)}. The flat runs' fast/slow state must split"
                          " evenly on both sides of a pair; until it does, the pair is neither a"
                          " win nor a miss (spec §3.1 G5)")
        elif verdicts != ["better", "better"]:
            misses.append(f"{m}: {verdicts} in {', '.join(CROSS_PAIRS)}; a win needs `better`"
                          " (beyond the noise band) in both")
        elif {"better", "worse"} & set(noise):
            misses.append(f"{m}: {noise} in {', '.join(SAME_ARM_PAIRS)}: the same image moved beyond"
                          " the band between fresh containers, so the win is not beyond the"
                          " between-server variance (spec §6)")
        elif m in SQLITE_BOUND and sqlite_a != sqlite_b:
            misses.append(f"{m}: better in both, but A runs SQLite {sqlite_a} and B SQLite {sqlite_b};"
                          " a SQLite-bound row credits free threading only on one SQLite build (spec D15)")
        elif m == "Hybrid QPS concurrent" and {"worse", "missing"} & set(p99):
            misses.append(f"{m}: better in both, but `{HYBRID_P99}` is {p99} in"
                          f" {', '.join(CROSS_PAIRS)}; a hybrid win needs that row present and not"
                          " `worse` in either (spec §3.1 G5)")
        else:
            wins.append(m)
    return wins, misses


def abab_decision(sections):
    """(outcome, reasons B did not win): ADR 0006's decision rule over its Verification."""
    # the c=16 tables are reported, not gated (spec §3.1 G7): none of them is passed in below
    sections = {title: body for title, body in sections.items() if not title.endswith(C16)}
    wins, misses = gate_rows(sections)
    reasons = [] if wins else misses  # one winning row is a win; the other's miss is recorded
    reasons += [f"{m}: worse beyond the noise band in both pairs"
                for m in worse_in_both(sections) if m in GATE_NOT_WORSE]
    for session, arm, answers, off, on, _, bad in _gil_rows(sections["GIL and logs"]):
        if FREE_THREADED_ARMS[arm] and not (answers > 0 and off == answers):
            reasons.append(f"{session}: gil_enabled false in only {off} of {answers} /healthz answers")
        if bad:
            reasons.append(f"{session}: {bad} Traceback or GIL lines in the container logs")
    if reasons:
        return "Park", reasons
    return ("Continue" if _json_block(sections["Upstream wheels"])["cp314t_wheels"] else "Hold"), []


def test_adr_0006_records_the_dgx_abab_and_its_decision():
    adr = (ROOT / "docs/adr/0006-free-threaded-python.md").read_text(encoding="utf-8")
    verification = adr.split("\n## Verification\n", 1)[1]
    assert not verification.lstrip().startswith("Pending"), "Task 12 records the DGX ABAB"
    sections = abab_sections(adr)
    # ABAB on one boot
    boots = re.findall(r"boot_id\s+`([0-9a-f-]{36})`", verification)
    assert len(boots) == 2 and boots[0] == boots[1], boots
    # one commit; A is the abi3 turbovec wheel with the GIL, B the cp314t sdist build without
    images = re.findall(r"^- ([ABCD]): `localhost/raggio:([0-9a-f]{7,40})[^`]*`: (.+)$",
                        sections["Images"], re.M)
    assert len({sha for _, sha, _ in images}) == 1, images
    facts = {arm: text for arm, _, text in images}
    assert "GIL enabled" in facts["A"] and "abi3" in facts["A"], facts["A"]
    assert "GIL disabled" in facts["B"] and "cp314t" in facts["B"], facts["B"]
    # raggio_native is built for each image's own interpreter, never abi3 (spec §4.2); every
    # image runs one OpenBLAS thread (D16) and names its SQLite (D15)
    native = {arm: re.search(r"raggio_native \S+ \(([^)]*)\)", text)[1] for arm, text in facts.items()}
    assert "cpython-312-" in native["A"] and "cpython-314t-" in native["B"], native
    assert not [arm for arm, files in native.items() if "abi3" in files], native
    for arm, text in facts.items():
        assert ", OPENBLAS_NUM_THREADS 1," in text and ", SQLite 3." in text, (arm, text)
    # every session left /healthz samples and container logs, and each GIL arm really had it
    gil = _gil_rows(sections["GIL and logs"])
    # G8: the four c=8 sessions and the four -c16 ones each have a row
    assert sorted(arm for _, arm, *_ in gil if arm in "AB") == ["A"] * 4 + ["B"] * 4
    for session, arm, answers, off, on, logs, _ in gil:
        assert answers > 0 and logs > 0, session
        if not FREE_THREADED_ARMS[arm]:
            assert on == answers and off == 0, session
    for pair in (*CROSS_PAIRS, *SAME_ARM_PAIRS):
        assert set(GATE_BETTER) <= set(_flat(sections[pair])), pair
    # every A/B table states both regimes (spec §6). The arms run the same BLAS threads (D16),
    # every row has a sqlite_version (D15), and the concurrent-QPS rows have >= 3 runs a side.
    # A and B may differ only in the interpreter and the SQLite it bundles; two sessions of
    # one image in nothing.
    for title, body in sections.items():
        if " → " not in title:
            continue
        regimes = _regimes(body)
        assert sorted(regimes) == ["baseline", "candidate"], title
        for labels in regimes.values():
            assert labels["openblas_num_threads"] == "1", (title, labels)
            assert labels["sqlite_version"] != "unknown", (title, labels)
        base, cand = title.split(" → ")
        allowed = set() if base[0] == cand[0] else {"python", "sqlite_version"}
        assert set(_differ(body)) <= allowed, (title, _differ(body))
        assert "Reported, not claimed" not in body, title
    assert "#### A" in sections["IVF fan-out"] and "#### B" in sections["IVF fan-out"]
    assert _json_block(sections["Unicode drift"])["rows"] == 2549619
    assert _json_block(sections["Upstream wheels"])["turbovec"]
    decision, reasons = abab_decision(sections)
    assert f"**{decision}**" in sections["Decision"], (decision, reasons)
    for other in set(OUTCOMES) - {decision}:
        assert f"**{other}**" not in sections["Decision"], other
    for metric in worse_in_both(sections):  # the costs are on record
        assert metric in sections["Decision"], metric
    sqlite_a, sqlite_b = sqlite_versions(sections)  # the verdict accounts for SQLite (spec D15)
    assert f"A runs SQLite {sqlite_a}, B runs SQLite {sqlite_b}" in sections["Decision"]
````

- [ ] **Step 2: Run the check to verify it fails**

Run from the repo root: `uv run python D:/DEV/_scratch/dgx/planG/g-check.py`

Expected: a traceback ending in `AssertionError: hold: g-render.py failed`, followed by `can't open file '...g-render.py': [Errno 2] No such file or directory`.

- [ ] **Step 3: Write the scripts**

Create `D:/DEV/_scratch/dgx/planG/g-render.py`:

````python
"""Plan G (ADR 0006): the body of ADR 0006's Verification from the ABAB outputs in
~/raggio-bench, written to ~/raggio-bench/g-verification.md. Run from ~/raggio:
    .venv/bin/python ~/raggio-bench/g-render.py
The workstation adds the Upstream wheels and Decision subsections."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "bench/dgx")
import compare  # noqa: E402

B = Path(os.environ.get("BENCH_HOME", Path.home() / "raggio-bench"))
SESSIONS = [("g-a1-py312", "A"), ("g-b1-py314t", "B"), ("g-a2-py312", "A"), ("g-b2-py314t", "B"),
            ("g-c1-py314", "C"), ("g-d1-py314t-gcfreeze", "D")]  # C1 and D1 are optional
PAIRS = [("A1 → B1", "g-a1-py312", "g-b1-py314t"), ("A2 → B2", "g-a2-py312", "g-b2-py314t"),
         ("A1 → A2", "g-a1-py312", "g-a2-py312"), ("B1 → B2", "g-b1-py314t", "g-b2-py314t"),
         ("A2 → C1", "g-a2-py312", "g-c1-py314"), ("B2 → D1", "g-b2-py314t", "g-d1-py314t-gcfreeze")]
# the c=16 sessions (spec §3.1 G7): the same four pairs, reported and never gated
PAIRS += [(f"{title} (c=16)", f"{base}-c16", f"{cand}-c16") for title, base, cand in PAIRS[:4]]


def read(name):
    return (B / name).read_text(encoding="utf-8")


def healthz(label):
    """(/healthz answers, gil_enabled false, gil_enabled true) of g-session.sh's samples."""
    answers = off = on = 0
    for line in read(f"{label}.healthz.log").splitlines():
        try:
            body = json.loads(line.partition(" ")[2])
        except ValueError:  # no answer: the container was starting or stopping
            continue
        if body.get("status") == "ok":
            answers += 1
            off += body.get("gil_enabled") is False
            on += body.get("gil_enabled") is True
    return answers, off, on


def logs(label):
    """(container logs, lines with a Traceback or GIL) of g-session.sh's captures."""
    files = sorted((B / f"{label}.logs").glob("*.log"))
    bad = [line for f in files for line in f.read_text(encoding="utf-8", errors="replace").splitlines()
           if "Traceback" in line or "GIL" in line]
    return len(files), len(bad)


def flat_cpu(label):
    """CPU per query under concurrency (ms) of each measured flat run (run0 is the warm-up), None
    where cpu.stat was unreadable: the decision reads each run's fast/slow state (spec §3.1 G5)."""
    runs = sorted((B / label).glob("run*-raggio.json"), key=lambda f: int(f.name[3:].split("-")[0]))
    return [json.loads(f.read_text(encoding="utf-8"))["raggio"].get("cpu_ms_per_q_c") for f in runs[1:]]


ran = [(label, arm) for label, arm in SESSIONS if (B / label).is_dir()]
# every session's checks gate, the -c16 ones included (spec §3.1 G8); only their speed tables do not
gil_ran = [(name, arm) for label, arm in ran for name in (label, f"{label}-c16")
           if name == label or (B / name).is_dir()]
os.chdir(B)  # compare.render prints the directories as given
lines = [
    f"DGX ABAB on gn100 (spec §6): the sessions {', '.join(label for label, _ in ran)}, in this",
    "order, each started from the IVF index by `g-ensure-ivf.sh` and measured by",
    "`bench/dgx/session.sh` with `FLAT_RUNS=3` in a fresh 4 GiB container per run,",
    "`--limit 2549619`, seed 42. Every image runs `OPENBLAS_NUM_THREADS=1` (spec D16); each",
    "table's Regime lines give both arms' `sqlite_version` (D15). A and B differ in `python`",
    "by design, and in `sqlite_version` when their interpreters bundle different SQLite builds.",
    "The `(c=16)` tables are the same four pairs from the `-c16` sessions (`CONCURRENCY=16`, spec",
    "§3.1 G7): reported, and not read by the decision.",
    "On one boot:",
    f"boot_id `{read('g-boot-before.txt').strip()}` before the first session,",
    f"boot_id `{read('g-boot-after.txt').strip()}` after the last.", "",
    "### Images", "",
    *[f"- {arm}: `{ref}`: {facts}" for arm, ref, facts in
      (line.split("\t") for line in read("g-images.txt").splitlines() if line)], "",
    "### GIL and logs", "",
    "| Session | Arm | /healthz answers | gil_enabled false | gil_enabled true | container logs |"
    " Traceback or GIL lines |",
    "|---|---|---|---|---|---|---|",
    *[f"| {label} | {arm} | {' | '.join(map(str, (*healthz(label), *logs(label))))} |"
      for label, arm in gil_ran], "",
]
for title, base, cand in PAIRS:
    if (B / base).is_dir() and (B / cand).is_dir():
        beyond = compare.beyond_band(base, cand)
        lines += [f"### {title}", "", compare.render(base, cand), "",
                  f"Beyond the noise band: better {beyond['better']}, worse {beyond['worse']}.", "",
                  "```json", json.dumps({"cpu_ms_per_q_c": {"baseline": flat_cpu(base),
                                                            "candidate": flat_cpu(cand)}}), "```", ""]
lines += ["### IVF fan-out", "",
          "`bench/ivf_fanout_probe.py fanout` in each image, on the same IVF index, `bench-tv` stopped:", ""]
for arm in ("A", "B"):
    lines += [f"#### {arm}", "", "```text", *read(f"g-fanout-{arm}.txt").rstrip("\n").splitlines(), "```", ""]
lines += ["### Unicode drift", "",
          f"`bench/unicode_drift_probe.py` dumps of both images: {read('g-drift-summary.txt').strip()}.",
          "Scan of the bench corpus:", "",
          "```json", read("g-drift-scan.json").strip(), "```", ""]
(B / "g-verification.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
print(B / "g-verification.md")
````

Create `D:/DEV/_scratch/dgx/planG/g-decide.py`:

````python
"""Plan G Task 12: splice the DGX's g-verification.md and turbovec's PyPI record into ADR 0006's
Verification and decide under its rule. Run from the repo root:
    uv run python - <verification.md> <turbovec.json> < this file"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, "tests")
from test_freethreading import (  # noqa: E402
    SQLITE_BOUND, abab_decision, abab_sections, gate_rows, sqlite_versions, worse_in_both)

verification, pypi = map(Path, sys.argv[1:3])
record = json.loads(pypi.read_text(encoding="utf-8"))
version = record["info"]["version"]
wheels = sorted(f["filename"] for f in record["releases"].get(version, []) if "cp314t" in f["filename"])
upstream = json.dumps({"turbovec": version, "cp314t_wheels": wheels})
adr_path = Path("docs/adr/0006-free-threaded-python.md")
head = adr_path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n## Verification\n", 1)[0]
body = verification.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n")
adr = (f"{head}\n## Verification\n\n{body}\n\n### Upstream wheels\n\n"
       f"https://pypi.org/pypi/turbovec/json on {datetime.date.today()}: the latest release and its"
       f" cp314t wheels.\n\n```json\n{upstream}\n```\n")
sections = abab_sections(adr)
decision, reasons = abab_decision(sections)
wins, misses = gate_rows(sections)
sqlite_a, sqlite_b = sqlite_versions(sections)
text = {
    "Continue": "**Continue**: B won beyond the noise band on {rows} in both pairs, and turbovec"
                " {version} ships cp314t wheels. The next step is a separate decision on making"
                " `PYTHON=3.14t` the default image; until then the default stays 3.12.",
    "Hold": "**Hold**: B won beyond the noise band on {rows} in both pairs, but turbovec {version}"
            " ships no cp314t wheel, and an own turbovec build never becomes the default (spec D6)."
            " The build arg and the CI job stay; re-run this ABAB when turbovec releases a cp314t wheel.",
    "Park": "**Park**: B did not win. The build arg and the CI job stay; no more measuring until a"
            " turbovec or CPython release changes the inputs. What failed the rule:",
}[decision].format(version=version, rows=" and ".join(f"`{w}`" for w in wins))
costs = worse_in_both(sections)
adr += f"\n### Decision\n\n{text}\n" + "".join(f"\n- {r}" for r in reasons) + ("\n" if reasons else "")
if wins and misses:  # the row that did not win, on record but not a reason
    adr += "\nNot won:\n" + "".join(f"\n- {m}" for m in misses) + "\n"
adr += (f"\nSQLite (spec D15): A runs SQLite {sqlite_a}, B runs SQLite {sqlite_b}. The hybrid rows"
        " run under SQLite's global memstatus mutex whatever the GIL does: a hybrid row that does"
        " not scale is SQLite-bound, not a free-threading failure, and credits a win only on one"
        " SQLite build.\n")
adr += ("\nCosts (flat rows `worse` beyond the noise band in both pairs; they do not gate): "
        + (", ".join(f"{c} (SQLite-bound, D15)" if c in SQLITE_BOUND else c for c in costs)
           if costs else "none") + ".\n")
adr += ("\nNoise (spec §6, D16): both arms run `OPENBLAS_NUM_THREADS=1`; rootless podman cannot pin"
        " cores, so a serial-latency cost is not attributed to free threading unless both arms are"
        " re-measured pinned to the X925 cores with `os.sched_setaffinity`.\n")
adr_path.write_text(adr, encoding="utf-8", newline="\n")
sys.stdout.reconfigure(encoding="utf-8")  # the reasons name pairs like "A1 → B1"
print(decision, *reasons, f"costs: {costs}", sep="\n")
````

Create `D:/DEV/_scratch/dgx/planG/g-image-facts.py`:

```python
"""Plan G (ADR 0006): what one image runs. Piped into the image's own interpreter:
    podman run --rm -i IMAGE python - < ~/raggio-bench/g-image-facts.py
The image's PYTHONWARNINGS turns a GIL re-enable by these imports into an error. Besides the
interpreter and turbovec's wheel tag, it names raggio_native's compiled file (built for this
interpreter, never abi3: spec §4.2), the SQLite the interpreter bundles (D15) and the image's
OPENBLAS_NUM_THREADS (D16)."""
import importlib.machinery
import importlib.metadata
import os
import platform
import sqlite3
import sys
import sysconfig
import unicodedata
from pathlib import Path

import raggio.app  # noqa: F401  (everything the server imports)
import raggio_native

turbovec = importlib.metadata.distribution("turbovec")
tags = [line.split(": ", 1)[1] for line in (turbovec.read_text("WHEEL") or "").splitlines()
        if line.startswith("Tag: ")]
suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
native = sorted(Path(m.__file__).name for name, m in list(sys.modules.items())
                if name.split(".")[0] == "raggio_native"
                and str(getattr(m, "__file__", None) or "").endswith(suffixes))
ft = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
gil = getattr(sys, "_is_gil_enabled", lambda: True)()
print(f"Python {platform.python_version()}{'t' if ft else ''}, GIL {'enabled' if gil else 'disabled'},"
      f" turbovec {turbovec.version} ({', '.join(tags)}),"
      f" raggio_native {importlib.metadata.version('raggio-native')} ({', '.join(native)}),"
      f" SQLite {sqlite3.sqlite_version},"
      f" OPENBLAS_NUM_THREADS {os.environ.get('OPENBLAS_NUM_THREADS', 'unset')},"
      f" Unicode {unicodedata.unidata_version}, raggio_native Unicode {raggio_native.UNIDATA_VERSION}")
```

Create `D:/DEV/_scratch/dgx/planG/g-session.sh`:

```bash
#!/usr/bin/env bash
# Plan G (ADR 0006): one ABAB session, capped. g-ensure-ivf.sh starts it from the IVF index,
# then Plan A's session.sh measures <image>. Every 60 s meanwhile, while the container runs
# <image>, /healthz goes to <label>.healthz.log (gil_enabled is on record for every run's
# container) and the container's log to <label>.logs/<container id>.log (session.sh
# replaces the container for every run; the previous session's container is skipped).
# Every bench.py run uses Plan A's CONCURRENCY: lib.sh reads it from the environment (default
# 8), and g-abab.sh sets 16 for the c=16 sessions (spec §3.1 G7).
# The first line of its own log names the image and the concurrency; the last line is
# "g-session <label>: exit <status>".
# Usage: [CONCURRENCY=16] setsid -f bash ~/raggio-bench/g-session.sh <label> <image> \
#          <cap, e.g. 90m> > ~/raggio-bench/<label>.log 2>&1 < /dev/null
source "${LIB:-$HOME/raggio/bench/dgx/lib.sh}"
set +e  # a failed sample must not end the session
LABEL="${1:?label}" IMAGE="${2:?image}" CAP="${3:?cap, e.g. 90m}"
echo "g-session $LABEL: $IMAGE, CONCURRENCY=$CONCURRENCY"
if ! (set -o noclobber; echo "$LABEL" > "$BENCH_HOME/g-active") 2> /dev/null; then
  echo "g-session '$(cat "$BENCH_HOME/g-active")' is running" >&2
  exit 1
fi
trap 'rm -f "$BENCH_HOME/g-active"' EXIT
mkdir -p "$BENCH_HOME/$LABEL.logs"
current() {  # "<container id> <image>" of the container now running as $CONTAINER
  podman inspect --format '{{.Id}} {{.ImageName}}' "$CONTAINER" 2> /dev/null
}
capture_log() {  # $1 = container id: its whole log so far, replaced only on success
  local f="$BENCH_HOME/$LABEL.logs/${1:0:12}"
  podman logs "$1" > "$f.tmp" 2>&1 && mv "$f.tmp" "$f.log"
  rm -f "$f.tmp"
}
sample() {  # one /healthz answer and the log, only from a container running $IMAGE
  local before after body
  before=$(current) || return 0
  [ "${before#* }" = "$IMAGE" ] || return 0
  body=$(curl -s --max-time 5 "http://localhost:$PORT/healthz")
  after=$(current) || return 0
  [ "$after" = "$before" ] || return 0  # replaced meanwhile: this answer may be another's
  echo "$(date -u +%FT%TZ) $body" >> "$BENCH_HOME/$LABEL.healthz.log"
  capture_log "${before%% *}"
}
touch "$BENCH_HOME/$LABEL.healthz.log"
(
  while [ ! -e "$BENCH_HOME/$LABEL.end" ]; do
    sample
    sleep 60
  done
) &
# FLAT_RUNS=3: three measured flat runs, so the bimodal concurrent-QPS rows have the 3 runs a
# side that spec §6 needs before a win is claimed (IVF_RUNS is 3 already). CONCURRENCY is
# handed to session.sh by name, so its lib.sh passes the same --concurrency to every run
timeout --kill-after=60 "$CAP" bash -c \
  'bash "$1" "$2" && FLAT_RUNS=3 CONCURRENCY="$5" IMAGE="$2" bash "$3" "$4"' _ \
  "$BENCH_HOME/g-ensure-ivf.sh" "$IMAGE" "$RAGGIO_DIR/bench/dgx/session.sh" "$LABEL" \
  "$CONCURRENCY"
status=$?
touch "$BENCH_HOME/$LABEL.end"
wait
sample  # the last run's container is still up
echo "g-session $LABEL: exit $status"
```

Create `D:/DEV/_scratch/dgx/planG/g-ensure-ivf.sh`:

```bash
#!/usr/bin/env bash
# Plan G (ADR 0006): start an ABAB session from the IVF index, so every session runs the same
# order (IVF runs, the 8 GiB detach, flat runs) and the arms differ only in the image. On a
# flat volume the image about to be measured builds the index at $MEMORY, as bench.py's
# raggio-ivf run0 does (POST /index with an empty body: the default nlist and nprobe).
# Usage: bash ~/raggio-bench/g-ensure-ivf.sh <image>
source "${LIB:-$HOME/raggio/bench/dgx/lib.sh}"
IMAGE="${1:?usage: g-ensure-ivf.sh <image>}"
# Plan A's session.sh holds no lock file, so look for the process itself. The pattern needs
# "bash " right before the session.sh path, so neither this script nor g-session.sh's
# `timeout ... bash -c` wrapper (whose arguments carry that path after the image) matches it.
if pgrep -f 'bash ([^ ]*/)?bench/dgx/session[.]sh( |$)' > /dev/null; then
  log "a bench/dgx/session.sh run is in progress: not touching $CONTAINER" >&2
  exit 1
fi
URL="http://localhost:$PORT/collections/bench"
FIELD='import json, sys
info = json.load(sys.stdin)
print(info["pending_jobs"] if sys.argv[1] == "pending" else (info.get("index") or {}).get("type"))'
field() {
  curl -sf --max-time 1800 -H "x-api-key: $API_KEY" "$URL" | "$RAGGIO_DIR/.venv/bin/python" -c "$FIELD" "$1"
}
wait_idle() {
  local start=$SECONDS
  until [ "$(field pending)" = 0 ]; do
    if [ $((SECONDS - start)) -ge "$PENDING_WAIT_S" ]; then
      log "$IMAGE: jobs still pending after $PENDING_WAIT_S s: stopping" >&2
      exit 1
    fi
    sleep 15
  done
}
podman stop -t 60 --ignore "$CONTAINER"
podman rm -f --ignore "$CONTAINER"
podman run -d --name "$CONTAINER" --memory "$MEMORY" -p "$PORT:8000" -v "$VOLUME:/data" \
  -e "ROOT_API_KEY=$API_KEY" "$IMAGE"
for i in $(seq 1 1200); do
  if curl -sf -o /dev/null --max-time 2 "http://localhost:$PORT/healthz"; then break; fi
  if [ "$i" = 1200 ]; then log "$IMAGE: no /healthz after 600 s: stopping" >&2; exit 1; fi
  sleep 0.5
done
wait_idle
if [ "$(field index)" = flat ]; then
  log "$IMAGE: flat index, building the IVF index at $MEMORY"
  curl -sf -X POST -H "x-api-key: $API_KEY" -H "content-type: application/json" -d '{}' "$URL/index"
  echo
  sleep 5
  wait_idle
fi
if [ "$(field index)" != ivf ]; then
  log "$IMAGE: no IVF index: stopping" >&2
  exit 1
fi
log "$IMAGE: ready from the IVF index"
```

Create `D:/DEV/_scratch/dgx/planG/g-abab.sh`:

```bash
#!/usr/bin/env bash
# Plan G (ADR 0006): the ABAB on one boot. g-session.sh runs A1 B1 A2 B2 of one commit at
# CONCURRENCY=8, each followed on the same image by its c=16 session (<label>-c16,
# CONCURRENCY=16, spec §3.1 G7), then the optional C1 (3.14 with the GIL, turbovec from its
# sdist) and D1 (B with GC_FREEZE=1) at c=8 only: A1 A1c16 B1 B1c16 A2 A2c16 B2 B2c16 [C1]
# [D1], one after another, each with its own cap. The chain stops at the first session that
# does not end with "exit 0". The boot id is recorded before the first session and after
# the last.
# ADOPT=1 is passed to the first session only (no fingerprint in ~/raggio-bench/state yet).
# Usage: setsid -f bash ~/raggio-bench/g-abab.sh <sha> <per-session cap, e.g. 90m> <C1: 0|1> \
#          <D1: 0|1> > ~/raggio-bench/g-abab.log 2>&1 < /dev/null
source "${LIB:-$HOME/raggio/bench/dgx/lib.sh}"
SHA="${1:?sha}" CAP="${2:?per-session cap, e.g. 90m}" WITH_C1="${3:?C1: 0|1}" WITH_D1="${4:?D1: 0|1}"
SESSIONS=()  # "<label> <image> <concurrency>"
for arm in "g-a1-py312 localhost/raggio:$SHA" "g-b1-py314t localhost/raggio:$SHA-py314t" \
           "g-a2-py312 localhost/raggio:$SHA" "g-b2-py314t localhost/raggio:$SHA-py314t"; do
  SESSIONS+=("$arm 8" "${arm%% *}-c16 ${arm#* } 16")
done
if [ "$WITH_C1" = 1 ]; then SESSIONS+=("g-c1-py314 localhost/raggio:$SHA-py314 8"); fi
if [ "$WITH_D1" = 1 ]; then SESSIONS+=("g-d1-py314t-gcfreeze localhost/raggio:$SHA-py314t-gcfreeze 8"); fi
adopt="${ADOPT:-0}"
cat /proc/sys/kernel/random/boot_id > "$BENCH_HOME/g-boot-before.txt"
log "g-abab: $SHA, ${#SESSIONS[@]} sessions, $CAP each, boot $(cat "$BENCH_HOME/g-boot-before.txt")"
for session in "${SESSIONS[@]}"; do
  read -r label image concurrency <<< "$session"
  log "g-abab: $label ($image, c=$concurrency)"
  CONCURRENCY="$concurrency" ADOPT="$adopt" bash "$BENCH_HOME/g-session.sh" "$label" "$image" \
    "$CAP" > "$BENCH_HOME/$label.log" 2>&1 < /dev/null || true
  adopt=0
  last=$(tail -n 1 "$BENCH_HOME/$label.log")
  if [ "$last" != "g-session $label: exit 0" ]; then
    log "g-abab: $label ended with '$last': stopping"
    exit 1
  fi
done
cat /proc/sys/kernel/random/boot_id > "$BENCH_HOME/g-boot-after.txt"
log "g-abab: done"
```

Create `D:/DEV/_scratch/dgx/planG/g-build.sh`:

```bash
#!/usr/bin/env bash
# Plan G (ADR 0006): the images deploy.sh does not build, from the deployed tree, one at a
# time: B (PYTHON=3.14t), then the optional C (PYTHON=3.14 with turbovec built from its
# sdist) and D (B with GC_FREEZE=1). deploy.sh already built A as localhost/raggio:<sha>.
# The last line is "== BUILD_DONE" or "== BUILD_FAILED <what>".
# Usage: setsid -f bash ~/raggio-bench/g-build.sh <sha> <C: 0|1> <D: 0|1> \
#          > ~/raggio-bench/g-build.log 2>&1 < /dev/null
source "${LIB:-$HOME/raggio/bench/dgx/lib.sh}"
SHA="${1:?sha}" WITH_C="${2:?C: 0|1}" WITH_D="${3:?D: 0|1}"
cd "$RAGGIO_DIR"
if [ "$(cat .deployed-sha)" != "$SHA" ]; then
  log "$RAGGIO_DIR holds $(cat .deployed-sha), not $SHA: run deploy.sh first"
  echo "== BUILD_FAILED deploy"
  exit 1
fi
build() {  # <image> <podman build arguments, the context last>
  local image=$1 start=$SECONDS
  shift
  log "building $image"
  if ! podman build -t "$image" "$@"; then
    echo "== BUILD_FAILED $image"
    exit 1
  fi
  log "built $image in $((SECONDS - start)) s"  # spec §8 p4: the 3.14t aarch64 build time
}
build "localhost/raggio:$SHA-py314t" --build-arg PYTHON=3.14t .
if [ "$WITH_C" = 1 ]; then
  build "localhost/raggio:$SHA-py314" --build-arg PYTHON=3.14 --build-arg UV_NO_BINARY_PACKAGE=turbovec .
fi
if [ "$WITH_D" = 1 ]; then
  mkdir -p "$BENCH_HOME/g-gcfreeze"
  printf 'FROM localhost/raggio:%s-py314t\nENV GC_FREEZE=1\n' "$SHA" > "$BENCH_HOME/g-gcfreeze/Containerfile"
  build "localhost/raggio:$SHA-py314t-gcfreeze" "$BENCH_HOME/g-gcfreeze"
fi
echo "== BUILD_DONE"
```

Create `D:/DEV/_scratch/dgx/planG/g-facts.sh`:

```bash
#!/usr/bin/env bash
# Plan G (ADR 0006): what each image runs, and the Unicode drift between A and B. Writes, in
# ~/raggio-bench: g-images.txt (arm, image, the g-image-facts.py line), g-u-A.json and
# g-u-B.json (bench/unicode_drift_probe.py dumps), g-drift.json and g-drift-summary.txt (their
# diff) and g-drift-scan.json (the bench corpus rows it touches). The image's PYTHONWARNINGS
# turns a GIL re-enable by the imports into a failure. The last line is "== FACTS_DONE" or
# "== FACTS_FAILED <what>".
# Usage: setsid -f bash ~/raggio-bench/g-facts.sh <sha> <C: 0|1> <D: 0|1> \
#          > ~/raggio-bench/g-facts.log 2>&1 < /dev/null
source "${LIB:-$HOME/raggio/bench/dgx/lib.sh}"
SHA="${1:?sha}" WITH_C="${2:?C: 0|1}" WITH_D="${3:?D: 0|1}"
IMAGES=("A localhost/raggio:$SHA" "B localhost/raggio:$SHA-py314t")
if [ "$WITH_C" = 1 ]; then IMAGES+=("C localhost/raggio:$SHA-py314"); fi
if [ "$WITH_D" = 1 ]; then IMAGES+=("D localhost/raggio:$SHA-py314t-gcfreeze"); fi
fail() {
  echo "== FACTS_FAILED $1"
  exit 1
}
cd "$RAGGIO_DIR"
: > "$BENCH_HOME/g-images.txt.tmp"
for entry in "${IMAGES[@]}"; do
  arm="${entry%% *}" image="${entry#* }"
  facts=$(podman run --rm -i "$image" python - < "$BENCH_HOME/g-image-facts.py") || fail "facts $arm"
  printf '%s\t%s\t%s\n' "$arm" "$image" "$facts" >> "$BENCH_HOME/g-images.txt.tmp"
  log "$arm: $facts"
done
mv "$BENCH_HOME/g-images.txt.tmp" "$BENCH_HOME/g-images.txt"
for arm in A B; do
  image="localhost/raggio:$SHA"
  if [ "$arm" = B ]; then image="$image-py314t"; fi
  podman run --rm -i "$image" python - dump < bench/unicode_drift_probe.py > "$BENCH_HOME/g-u-$arm.json" \
    || fail "dump $arm"
done
.venv/bin/python bench/unicode_drift_probe.py diff "$BENCH_HOME/g-u-A.json" "$BENCH_HOME/g-u-B.json" \
  > "$BENCH_HOME/g-drift.json" 2> "$BENCH_HOME/g-drift-summary.txt" || fail diff
log "drift: $(cat "$BENCH_HOME/g-drift-summary.txt")"
.venv/bin/python bench/unicode_drift_probe.py scan "$BENCH_HOME/g-drift.json" --limit "$LIMIT" \
  > "$BENCH_HOME/g-drift-scan.json" || fail scan
log "scan: $(cat "$BENCH_HOME/g-drift-scan.json")"
echo "== FACTS_DONE"
```

Create `D:/DEV/_scratch/dgx/planG/g-fanout.sh`:

```bash
#!/usr/bin/env bash
# Plan G (ADR 0006): bench/ivf_fanout_probe.py fanout (read-only, seed 7) in image A, then in
# image B, on the IVF index of the bench-tv volume. Each runs in a transient 8 GiB container
# named bench-tv, so the server container must be gone first. Writes g-fanout-A.txt and
# g-fanout-B.txt; the last line is "== FANOUT_DONE" or "== FANOUT_FAILED <what>".
# Usage: setsid -f bash ~/raggio-bench/g-fanout.sh <sha> > ~/raggio-bench/g-fanout.log 2>&1 < /dev/null
source "${LIB:-$HOME/raggio/bench/dgx/lib.sh}"
SHA="${1:?sha}"
if podman container exists "$CONTAINER"; then
  log "$CONTAINER exists: stop and remove it first"
  echo "== FANOUT_FAILED $CONTAINER"
  exit 1
fi
for arm in A B; do
  image="localhost/raggio:$SHA"
  if [ "$arm" = B ]; then image="$image-py314t"; fi
  log "fanout probe in $image"
  if ! podman run --rm -i --name "$CONTAINER" --memory 8g -v "$VOLUME:/data" "$image" \
      python -u - fanout --data /data --threads 1,2,4,8,12,16,20,32 \
      < "$RAGGIO_DIR/bench/ivf_fanout_probe.py" > "$BENCH_HOME/g-fanout-$arm.txt" 2>&1; then
    echo "== FANOUT_FAILED $arm"
    exit 1
  fi
done
echo "== FANOUT_DONE"
```

bash on gn100 rejects carriage returns, so normalize the line endings:

```bash
sed -i 's/\r$//' D:/DEV/_scratch/dgx/planG/*.sh D:/DEV/_scratch/dgx/planG/*.py
```

- [ ] **Step 4: Run the check to verify it passes**

Run from the repo root: `uv run python D:/DEV/_scratch/dgx/planG/g-check.py`. It takes about 50 s.

Expected:

```text
hold: Hold, costs: [], Task 12 test passes
continue: Continue, costs: [], Task 12 test passes
park: Park, costs: [], Task 12 test passes
cost: Hold, costs: ['Search p50 (ms)'], Task 12 test passes
hitloss: Park, costs: ['Hybrid text-hit@10'], Task 12 test passes
sqlite-bound: Hold, costs: ['Hybrid QPS concurrent'], Task 12 test passes
hybrid-win: Hold, costs: [], Task 12 test passes
hybrid-other-sqlite: Park, costs: [], Task 12 test passes
noisy: Park, costs: [], Task 12 test passes
gil-on: Park, costs: [], Task 12 test passes
traceback: Park, costs: [], Task 12 test passes
with-c1: Hold, costs: [], Task 12 test passes
hybrid-p99-worse: Park, costs: ['Hybrid p99 under concurrency (ms)'], Task 12 test passes, reason checked
flat-split-uneven: Park, costs: [], Task 12 test passes, reason checked
flat-split-uneven-worse: Hold, costs: [], Task 12 test passes
flat-split-even: Hold, costs: [], Task 12 test passes
cpu-missing: Park, costs: [], Task 12 test passes, reason checked
c16-not-gated: Hold, costs: [], Task 12 test passes
c16-traceback: Park, costs: [], Task 12 test passes, reason checked
two-boots: Task 12 test fails, as it must
two-shas: Task 12 test fails, as it must
blas-differs: Task 12 test fails, as it must
thin: Task 12 test fails, as it must
Plan G DGX scripts OK in D:\DEV\_scratch\dgx\planG
```

What the new scenarios pin:
- `sqlite-bound`: B's hybrid concurrent QPS drops while vector QPS wins. The hybrid row is a cost, labelled SQLite-bound in the ADR, and does not park (spec D15).
- `hybrid-win` and `hybrid-other-sqlite`: a hybrid-only win counts on one SQLite build and is refused across two (D15).
- `noisy`: A2 moves beyond the band against A1, so B's win is inside the between-server variance and is not claimed (spec §6).
- `blas-differs` and `thin`: an arm without `OPENBLAS_NUM_THREADS=1` (D16), or a table with Plan A's `Reported, not claimed` note (fewer than 3 flat runs a side), fails the Task 12 test.
- `hybrid-p99-worse`: B's hybrid concurrent QPS is `better` in both pairs, but its `Hybrid p99 under concurrency (ms)` is 4x A's, `worse` in both. The hybrid row does not win, so B parks, with the p99 row as the cost (spec §3.1 G5; p4 saw 3.8-4.0x).
- `flat-split-uneven`: B's `QPS concurrent` is `better` in both pairs, but two of A's three flat runs are high-CPU (116 ms per query against 54) and none of B's. The reason names the split, `A1 2 high/1 low, B1 0 high/3 low`, and the row counts as neither a win nor a miss, so B parks (G5).
- `flat-split-uneven-worse`: the same uneven split, but B's `QPS concurrent` is 0.5x and its `p95 under concurrency (ms)` 2x A's, `worse` in both pairs, and `Hybrid QPS concurrent` is `better`. On an uneven split neither flat row counts a win or a cost (G9), so `worse_in_both` drops both: the hybrid win stands and the decision is Hold, with `costs: []`. Without the rule it parks on `QPS concurrent: worse beyond the noise band in both pairs`. No reason line is checked: a Hold has none, and the uneven-split text goes under `Not won`.
- `flat-split-even`: one high-CPU run in every session, so each side of each pair splits 1 high/2 low. The split is even, B's `better` counts, and the decision is Hold, as in `hold`.
- `cpu-missing`: no run has the CPU rows, as where `cpu.stat` is unreadable (spec §4.2). The fast/slow state is unknown, the reason says so, and B's `better` does not count.
- `c16-traceback`: `synth`'s `traceback` knob names one session, `g-b1-py314t-c16`, and puts a Traceback line in one of its logs; everything else is as in `hold`. The `GIL and logs` table has a row for every `-c16` session, so `abab_decision` reads it and parks; the reason line `g-b1-py314t-c16: 1 Traceback or GIL lines in the container logs` is checked through `REASONS`. The c=16 speed tables stay reported, not gated (spec §3.1 G7, G8).
- `c16-not-gated`: `synth`'s `c16` knob makes B worse in the four `-c16` sessions (`QPS concurrent` at 0.5x, `Hybrid QPS concurrent` at 0.5x, `Hybrid p99 under concurrency (ms)` at 4x), while the c=8 sessions are as in `hold`. Every scenario asserts that `g-render.py` wrote the four `(c=16)` tables; this one also asserts that the `A1 → B1 (c=16)` table's `QPS concurrent` row ends in `worse` (the line under a table, "Beyond the noise band: better ..., worse ...", always contains the word, so the assertion reads the row). Read as gate tables, "worse in both" would turn the Hold into a Park; the decision is Hold, because it reads only the c=8 titles and `abab_decision` also drops every `(c=16)` section (spec §3.1 G7).
- The reason checks: for `hybrid-p99-worse`, `flat-split-uneven`, `cpu-missing` and `c16-traceback`, one of `g-decide.py`'s reason lines must contain the scenario's `REASONS` text, and the line ends `, reason checked`.

Run:

```bash
for f in g-session.sh g-ensure-ivf.sh g-abab.sh g-build.sh g-facts.sh g-fanout.sh; do bash -n "D:/DEV/_scratch/dgx/planG/$f" && echo "$f: syntax OK"; done
grep -l $'\r' D:/DEV/_scratch/dgx/planG/*.sh D:/DEV/_scratch/dgx/planG/*.py || echo "LF only"
```

Expected: six `syntax OK` lines, then `LF only`.

- [ ] **Step 5: No commit**

Nothing in the repo changed. `git status --short --untracked-files=no` prints nothing.

---

### Task 11 (DGX, the first of two): deploy, probe p4 (the 3.14t aarch64 build), record the images' facts, run the fan-out probe

This task prepares Task 12's ABAB on gn100, under spec §6. It follows Tasks 1–10, and needs the user's explicit go-ahead, with no time cap: it is a build and fix window, which spec §6 exempts from the cap (§3.1 G4). A deploy or build whose log stops growing for 30 minutes is hung, which is not a cap: Task 11 stops it and asks the user (Steps 4 and 6). It is Plan G's first DGX task, and it opens with spec §8's probe p4 on G's own tree (Step 6). p4 ran on gn100 on 2026-09-28 (`docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`) at the 19a8a2e tree, which has no `raggio_native`, with a stand-in `check_image.py`: turbovec 1.0.0's sdist built for cp314t on aarch64 with 0 builder-stage fixes and no build arg, in a 28.7 s cold image build, and the GIL stayed off. Two things stay open, and Step 6 settles them: whether `raggio_native` builds for cp314t on aarch64, and whether G's real `check_image.py` passes on G's real Dockerfile. If turbovec's sdist does not build, which p4 makes unlikely, Step 6 classifies the cause from the build log (spec §3.1 G2). A missing system build dependency, a Rust or maturin toolchain version, or a missing interpreter-specific build flag or env is fixed in the Dockerfile's builder stage only, in at most 3 rebuild attempts, each recorded in the task log `D:/DEV/_scratch/dgx/planG/p4-attempts.md`. If 3 attempts do not fix it, or no builder-stage line can express the fix although turbovec's source needs no change, Task 11 stops and asks the user, and ADR 0006 stays `Pending:` (spec §3.1 G3). A cause that only a change to turbovec's own source would fix gets an upstream issue draft in `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md`, which the maintainer posts, and G is parked in ADR 0006, the only case that parks without asking (spec §3.1 G3). Nobody patches, forks or vendors turbovec (D6). After p4, it records what each image runs: interpreter, GIL, turbovec wheel tag, `raggio_native`'s compiled file, the bundled SQLite (D15), `OPENBLAS_NUM_THREADS` (D16) and the Unicode versions. It also records the Unicode drift between the arms, and runs Plan F's fan-out probe in both images on one IVF index. It changes nothing in the repo, except on p4's failure path: a builder-stage fix and its regression test, committed on G's branch, or the Park recorded in ADR 0006.

Every `ssh` and `scp` is written out in full, with `-F "$SSH_CONFIG" -o BatchMode=yes`. Rules for gn100:
- Local paths given to `scp` are relative, after a `cd`, because `scp` would read `D:` as a host name.
- Only the `bench-tv` container is ever stopped or removed.
- Never touch `bench-wv` or its volume.
- Never touch `~/turborag`, except where Plan A's `deploy.sh` symlink reads the corpus.
- No `sudo`.
- Never `pkill -f`.
- Every script runs detached with `setsid -f`, and is followed by polling its log.

**Files:**
- Outside the repo: create `D:/DEV/_scratch/dgx/planG/g.env` and `D:/DEV/_scratch/dgx/planG/deploy.log`, and the stall guard's state files `deploy.stall` and `g-build.stall` next to them (Steps 4 and 6).
- Only if the stall guard stops a hung deploy or build (Steps 4 and 6): outside the repo, `D:/DEV/_scratch/dgx/planG/hung-<UTC time>.log`, and a `## p4 hung build, not counted` entry in `p4-attempts.md`.
- Only if turbovec's sdist does not build (Step 6, spec §3.1 G2):
  - outside the repo: `D:/DEV/_scratch/dgx/planG/g-build.log`, `p4-attempts.md` and `g-build-p4-<k>.log` for each failed build `<k>`;
  - for a builder-stage cause: modify `Dockerfile` (builder-stage lines only) and `tests/test_freethreading.py` (append the probe p4 section);
  - for a turbovec source cause: create `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md` (never posted by the implementer), and modify `docs/adr/0006-free-threaded-python.md` (the `Pending:` Verification is replaced with the Park).
- On gn100:
  - `~/raggio`, replaced with this commit's tree by Plan A's `deploy.sh` (spec §6);
  - the images `localhost/raggio:<SHA>` (A), `…:<SHA>-py314t` (B), and optionally `…:<SHA>-py314` (C) and `…:<SHA>-py314t-gcfreeze` (D);
  - in `~/raggio-bench/`:
    - the scripts;
    - `g-build.log`, `g-gcfreeze/Containerfile`;
    - `g-images.txt`, `g-u-A.json`, `g-u-B.json`, `g-drift.json`, `g-drift-summary.txt`, `g-drift-scan.json`, `g-facts.log`;
    - `g-ensure-ivf.log`, `g-fanout.log`, `g-fanout-A.txt`, `g-fanout-B.txt`;
  - the `bench-tv` volume, left with the IVF index attached.

**Interfaces:**
- Consumes:
  - Tasks 1–9's commits and Task 10's scripts;
  - Plan A's `bench/dgx/deploy.sh`. It requires a clean tracked tree and names the image by `git rev-parse --short=7 HEAD`. It runs `setup.sh <SHA>` in the foreground: `uv sync --frozen --group bench`, `podman build -t localhost/raggio:<SHA> .`, and it writes `~/raggio/.deployed-sha`. Its last log line is `<ts> deployed <SHA> as localhost/raggio:<SHA>`.
  - on gn100:
    - the `bench-tv` volume, holding collection `bench` (2,549,619 rows);
    - `~/raggio-bench/state/fingerprint-raggio-2549619.json`, if Plan A's first session wrote it.
- Produces:
  - `g.env`, which holds `SHA`, `WITH_C1` and `WITH_D1`;
  - the images, and p4's answer in `g-build.log`: `built <image> in <N> s` for each image, or `== BUILD_FAILED <image>` with the failing stage above it;
  - only on p4's turbovec failure path (spec §3.1 G2):
    - `p4-attempts.md`, one `## p4 build <k>` entry per failed build;
    - for each builder-stage fix attempt `<k>` (at most 3): the constant `P4_BUILDER_FIX`, the pattern `P4_FIX_LINE`, the test `test_the_p4_turbovec_build_fix_stays_in_the_builder_stage`, and the commit `Fix the turbovec cp314t build in the builder stage (p4, attempt <k>)`. Each fix gives a new SHA, and the task restarts from Step 2 with it;
    - for a turbovec source cause: the draft `turbovec-cp314t-build.md`, ADR 0006's Verification with the p4 record and `**Park**`, and the commit `Park the free-threaded evaluation at probe p4 (ADR 0006)`. Task 12 does not run;
  - only if the stall guard stops a hung deploy or build: the `p4-attempts.md` entry `## p4 hung build, not counted`, whose last line is `- next: stop: build hung (no log output for 30 min)`. Task 12 does not run until the user decides;
  - `g-images.txt`, with one line per image: `<arm>\t<image>\tPython …, GIL …, turbovec … (<wheel tags>), raggio_native <version> (<compiled file>), SQLite …, OPENBLAS_NUM_THREADS …, Unicode …, raggio_native Unicode …`;
  - the drift files;
  - `g-fanout-A.txt` and `g-fanout-B.txt`, each with a `GATE` line.

- [ ] **Step 1: Ask the user for a run window (no time cap, spec §6 and §3.1 G4)**

Ask, and wait for an explicit answer. Do not run anything on gn100 before it:

> Plan G's DGX preparation needs a run window on gn100. It is a build and fix window, so it has no time cap (spec §6 exempts these windows, §3.1 G4); these estimates add up to about 50 minutes, plus about 15 min for each p4 fix attempt if turbovec's sdist needs one:
> - about 10 min to deploy this commit to `~/raggio` (spec §6) and build the 3.12 image;
> - about 5 min to build the free-threaded 3.14t image. This is spec §8's probe p4 on this commit: it compiles turbovec 1.0.0 from its sdist and `raggio_native` for cp314t on aarch64. p4 already built the image shape on gn100 on 2026-09-28 without `raggio_native`, with 0 fixes, in 28.7 s cold [28.2..29.3] over 3 builds, 17–19 s of it turbovec's compile. Nobody has measured `raggio_native`'s Rust compile for cp314t, so the estimate leaves minutes, not seconds, for it. What stays open is `raggio_native`'s build and the real build-time check. If turbovec's sdist does not build, which p4 makes unlikely, I classify the cause from the build log (spec §3.1 G2):
>   - a missing system build dependency, a Rust or maturin toolchain version, or a missing build flag or env: I fix it in the Dockerfile's builder stage only, with a regression test, commit it on G's branch, and redeploy and rebuild. At most 3 attempts, each about 15 min, with no time cap; every attempt is recorded in `D:/DEV/_scratch/dgx/planG/p4-attempts.md`. After 3 failed attempts, or if no builder-stage line of these kinds can express the fix, I stop, report, and ask you how to go on, and ADR 0006 stays `Pending:` (spec §3.1 G3);
>   - a cause only a change to turbovec's own source would fix: I write an upstream issue draft to `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md` for you to post (I never post it), park the evaluation in ADR 0006 without asking (spec §3.1 G3), and the task ends there. raggio never patches, vendors or forks turbovec (D6);
> - about 5 min to record what each image runs (interpreter, GIL, the turbovec and `raggio_native` builds, SQLite version, `OPENBLAS_NUM_THREADS`), and the Unicode drift between them;
> - up to 10 min to make sure the `bench-tv` volume has its IVF index; it builds one at 4 GiB if the volume is flat;
> - about 20 min for Plan F's read-only IVF fan-out probe, in both images, one after the other, in a transient 8 GiB container.
>
> Two optional arms for Task 12 can be built now:
> - C: 3.14 with the GIL, and turbovec built locally from the same sdist with maturin. That build is still an abi3 wheel (`cp39-abi3-linux_aarch64`), because the sdist enables `abi3-py39`, while 3.14t builds `cp314-cp314t`, so C is not the same turbovec build as B. It separates the 3.12 → 3.14 interpreter effect and a local build from PyPI's manylinux wheel (maturin 1.14.1). Its build adds about 5 min here (p4's took 29 s without `raggio_native`), and its session adds about 50 min to Task 12.
> - D: 3.14t with `GC_FREEZE=1`. Its build takes under a minute, and its session adds about 50 min to Task 12.
>
> The run uses only `bench-tv`, its volume, `~/raggio` and `~/raggio-bench/g-*`. It never touches `bench-wv`, reads `~/turborag` only through Plan A's corpus symlink, and needs no `sudo`.
>
> If a deploy's or a build's log shows no new output for 30 minutes, I treat the build as hung, which is not a time cap: I stop it, and ask you how to go on. A hung build uses none of the 3 fix attempts.
>
> May I run it, and with or without C and D? If you need gn100 back before it ends, for example for your own containers, tell me and I stop what runs on gn100, a deploy or build included.

If the user declines, stop here. Tasks 1–10 stand on their own, and ADR 0006 stays `Pending:`.

Note the start time, for the report; there is no cap to track (spec §6, §3.1 G4). The stop recipe below is kept, although there is no cap, because gn100 is shared: the maintainer's own containers may need it before Task 11 ends. The stall guard of Steps 4 and 6 uses it too. If the user asks for gn100 back before Step 9 sees `== FANOUT_DONE`, run this stop recipe, then go to Step 10:

```bash
lt() { local d p; for d in /proc/[0-9]*; do { read -r p < "$d/ppid"; } 2>/dev/null && [ "$p" = "$1" ] && lt "${d#/proc/}"; done; echo "$1"; }
for d in /proc/[0-9]*; do [ "${d#/proc/}" = "$$" ] || case "$(tr '\0' ' ' 2>/dev/null < "$d/cmdline")" in *bench/dgx/deploy[.]sh*) lt "${d#/proc/}";; esac; done | sort -un | while read -r p; do echo "laptop: stopping $p"; kill "$p" 2>/dev/null; done
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
tree() { local c; for c in $(pgrep -P "$1"); do tree "$c"; done; echo "$1"; }
for p in $(pgrep -f "raggio-bench/g-(build|facts|ensure-ivf|fanout)[.]sh"); do kill -TERM -- "-$p"; done
pids=$(for r in $(pgrep -f "raggio/bench/dgx/setup[.]sh"); do tree "$r"; done | sort -un | grep -vx "$$")
[ -z "$pids" ] || { ps -o pid=,args= -p "$(echo $pids | tr " " ,)" | cut -c1-100; kill -TERM $pids; }
podman stop -t 60 --ignore bench-tv
sleep 15
pgrep -af "raggio-bench/g-(build|facts|ensure-ivf|fanout)[.]sh|raggio/bench/dgx/setup[.]sh|podman[ ]build -t localhost/raggio:|[u]v sync --frozen|[r]ustc --crate-name" || echo "gn100: none left"
podman ps --format "{{.Names}} {{.Status}}"'
for d in /proc/[0-9]*; do case "$(tr '\0' ' ' 2>/dev/null < "$d/cmdline")" in *bench/dgx/deploy[.]sh*|*bench/dgx/setup[.]sh*) echo "laptop: left ${d#/proc/}";; esac; done
```

Expected:
- a `laptop: stopping <pid>` line for each laptop process of a running deploy: Step 4's background command, its `deploy.sh`, and their descendants (the `git archive` and `ssh` of the unpack, or the `ssh` that runs `setup.sh`);
- on gn100, the PID and command line of each process stopped with a `setup.sh`: `bash …/setup.sh <SHA>`, then `uv` or `podman build -t localhost/raggio:<SHA> .` and the build's children;
- `gn100: none left`, then `podman ps` without a `bench-tv` line. `bench-wv`, if listed, keeps its status;
- no `laptop: left` line.

If a line is left, run the recipe once more. If it is still there, report its PID and command line to the user, and signal nothing else.

The recipe never runs `pkill -f`: it finds the PIDs first, then kills by PID.
- **The laptop first.** Git Bash has no `pgrep`, so the recipe reads each process's `/proc/<pid>/cmdline`, skips its own shell (`$$`), and finds Step 4's `deploy.sh` and the command running it. It stops them before the gn100 part: otherwise a deploy between its two ssh calls would start `setup.sh` after the gn100 part has run. `lt` adds their descendants from `/proc/<pid>/ppid` before anything is killed, because a killed `deploy.sh` does not take its children with it: a hung unpack's `git archive` and `ssh` would go on, and no `laptop: left` line would show them.
- **The `g-` scripts.** Each was started with `setsid -f`, so it leads its own process group, and `kill -- -<pid>` also stops its `podman build` or `podman run` children.
- **`setup.sh`.** It leads no group: `deploy.sh` runs it over ssh in the foreground, in the ssh session's group. So the recipe collects each `setup.sh` process and all of its descendants with `pgrep -P` (`uv sync`, `podman build` and the build's own children), leaves out the remote shell that runs the recipe (`$$`), prints them, and TERMs them by PID. The ssh session then ends, and so does the laptop's ssh, which the last line checks.
- **Self-matching.** The `[.]` and `[ ]` keep the patterns from matching the shells that run the recipe, whose command lines hold the patterns themselves. So the recipe's code never names `deploy.sh` or `setup.sh` without the brackets.
- **Other containers.** Only those process trees are signalled, so `bench-wv`, any other container, and a `podman build` of the user's own are never touched. The one container the recipe names is `bench-tv`, which it stops and does not remove. A stopped build may leave its intermediate container behind, listed by `podman ps -a --external`: leave it, since `podman rm -f` may only name `bench-tv`.
- **Build steps left running.** The TERM may not reach a `RUN` step: its first process is the init of the build container's PID namespace, which ignores a TERM from outside unless it handles one. So the last `pgrep` also lists a `uv sync --frozen` or a `rustc --crate-name` that is still running. It lists them and never signals them, since such a process may also be the user's own: the rule above applies, so a line still there after the second run goes to the user.

Step 6's failure procedure (6a–6g) runs nothing on gn100, so a request that comes during it does not cut it short: finish it on the laptop, through 6d's `next:` line with its commit, 6f's Park commit, or 6g's stop, and then go to Step 10 instead of restarting at Step 2. A later go-ahead (this step's question again) restarts this task at Step 2, with the SHA of `HEAD`. `p4-attempts.md` goes on counting from its last `## p4 build` entry: a build the stop cut short never reached 6a, so it is not in the log, and it simply runs again. A hung build's `## p4 hung build, not counted` entry is not counted either (Step 6, "If a deploy or build hung"). After a Park nothing restarts, and after 6g's stop the user decides (spec §3.1 G3).

- [ ] **Step 2: Pin the commit and check the laptop side**

```bash
cd D:/DEV/raggio
git status --short --untracked-files=no
git log --oneline -10
uv run pytest -q
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q
uv run python D:/DEV/_scratch/dgx/planG/g-check.py
SHA=$(git rev-parse --short=7 HEAD)
WITH_C1=0  # 1 only if the user asked for C
WITH_D1=0  # 1 only if the user asked for D
printf 'SHA=%s\nWITH_C1=%s\nWITH_D1=%s\n' "$SHA" "$WITH_C1" "$WITH_D1" > D:/DEV/_scratch/dgx/planG/g.env
cat D:/DEV/_scratch/dgx/planG/g.env
```

Expected:
- `git status` prints nothing;
- the log's newest commit is `Document the free-threaded image (ADR 0006)`, or, after a probe p4 fix in Step 6, `Fix the turbovec cp314t build in the builder stage (p4, attempt <k>)`;
- both suites are green;
- the check ends with `Plan G DGX scripts OK in ...`;
- `g.env` shows the SHA and the two flags.

If any of these differ, stop and fix that first. The images are built from the commit, not from the working tree. Every later step, here and in Task 12, starts with `source D:/DEV/_scratch/dgx/planG/g.env`.

- [ ] **Step 3: Pre-flight on gn100 (read-only, under a minute)**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
uptime; free -m | head -2; df -h ~ | tail -1
pgrep -af "bench/bench[.]py" || echo "no bench.py running"
pgrep -af "raggio-bench/g-[a-z-]*[.]sh" || echo "no g- script running"
podman ps -a --format "{{.Names}} {{.Status}} {{.Image}}"
VOL=$(podman volume inspect bench-tv --format "{{.Mountpoint}}") || exit 1
if podman unshare test -d "$VOL/collections/bench/ivf"; then echo "volume: IVF attached"; else echo "volume: flat"; fi
ls ~/raggio-bench/state/fingerprint-raggio-2549619.json 2>&1
pgrep -af "bench/dgx/session[.]sh" || echo "no session.sh running"
ls ~/raggio-bench/g-active 2>&1
cat ~/raggio/.deployed-sha 2>&1'
```

Expected:
- `no bench.py running`, `no g- script running` and `no session.sh running`;
- no `bench-tv` line with status `Up`. `bench-wv` may be listed: leave it alone;
- one `volume:` line;
- `g-active`: `No such file or directory`. G's `g-active` is the only lock file; Plan A's `session.sh` holds none, so it shows only in `pgrep`.

Note whether the fingerprint exists. If it does not, Task 12 passes `ADOPT=1` to its first session.

Stop and ask the user, and never stop anything yourself, if:
- a bench.py, a `session.sh` or a `g-` script is running;
- `bench-tv` is up;
- `g-active` exists.

If there is no `bench-tv` volume, stop and ask. The 2.55M-row volume is a prerequisite, and re-ingesting it takes about 31 min. This stop stays although Task 11 has no cap: a re-ingest is neither a build nor a fix, so Step 1's go-ahead does not cover it, and spec §6 would need its own go-ahead for it, with a time cap.

- [ ] **Step 4: Deploy this commit (spec §6)**

Run this from the repo root, in the background. It takes about 10 minutes; wait for it to exit. Its `echo` line starts the stall guard's clock:

```bash
cd D:/DEV/raggio
echo "0 $(date +%s)" > D:/DEV/_scratch/dgx/planG/deploy.stall
bash bench/dgx/deploy.sh > D:/DEV/_scratch/dgx/planG/deploy.log 2>&1
tail -n 3 D:/DEV/_scratch/dgx/planG/deploy.log
```

While it runs, poll every 5 minutes with the stall guard, until the background command exits. `deploy.stall` holds the log's size and the time that size was first seen:

```bash
cd D:/DEV/_scratch/dgx/planG
now=$(date +%s); size=$(stat -c %s deploy.log); read -r last since < deploy.stall
[ "$size" = "$last" ] || since=$now
echo "$size $since" > deploy.stall
echo "deploy.log: $size bytes, no growth for $(( (now - since) / 60 )) min"
[ $((now - since)) -lt 1800 ] || echo "STALLED: no log output for 30 min"
tail -n 2 deploy.log
```

Expected while it runs: a `deploy.log:` line, and the log's last lines. `setup.sh`'s `podman build` streams its `STEP` lines through ssh into `deploy.log`; uv prints nothing while it compiles a Rust package, so a few quiet polls are normal. The guard is a clock, not a count of polls: it fires at the first poll that finds the size unchanged for 30 minutes or more, which at 5-minute polls is the sixth poll in a row without growth. That is not a time cap (spec §3.1 G4): a deploy whose log keeps growing runs as long as it needs. If a poll prints `STALLED: no log output for 30 min` before the command exits, the deploy is hung: go to "If a deploy or build hung" in Step 6, with `STEP=4`.

Expected when the command exits: the last line is `<ts> deployed <SHA> as localhost/raggio:<SHA>`, with the SHA from `g.env`. If it is not, read `deploy.log`, stop and report.

- [ ] **Step 5: Copy the scripts to gn100**

Earlier Plan G files in `~/raggio-bench` are moved aside, never deleted:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'cd ~/raggio-bench && shopt -s nullglob && old=(g-*) && if [ ${#old[@]} -gt 0 ]; then d=planG-prev-$(date +%Y%m%d-%H%M%S) && mkdir "$d" && mv "${old[@]}" "$d"/ && echo "moved ${#old[@]} to $d"; else echo "nothing to move"; fi'
cd D:/DEV/_scratch/dgx/planG
scp -F "$SSH_CONFIG" -o BatchMode=yes g-session.sh g-ensure-ivf.sh g-abab.sh g-build.sh g-facts.sh g-fanout.sh g-image-facts.py g-render.py gn100:raggio-bench/
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'cd ~/raggio-bench && for f in g-session.sh g-ensure-ivf.sh g-abab.sh g-build.sh g-facts.sh g-fanout.sh; do bash -n "$f" || exit 1; done && ls -l g-* && echo SYNTAX_OK'
```

Expected: the eight files are listed, then `SYNTAX_OK`.

- [ ] **Step 6: Probe p4: build B on aarch64, then C and D if asked for (detached, then poll)**

This is spec §8's probe p4 on G's own tree, and nothing later in this task runs unless it passes. p4 already compiled turbovec 1.0.0's sdist for cp314t on aarch64, on 2026-09-28 at the 19a8a2e tree: `cp314-cp314t-linux_aarch64`, 0 builder-stage fixes, no build arg (`docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`). B's build is the first time `raggio_native` is compiled for cp314t on aarch64, and the first time G's real `check_image.py` runs on G's real Dockerfile there. turbovec and `raggio_native` are both compiled in Plan E's builder stage, whose `PYO3_PYTHON` names the interpreter `PYTHON=3.14t` installed.

```bash
source D:/DEV/_scratch/dgx/planG/g.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "setsid -f bash ~/raggio-bench/g-build.sh $SHA $WITH_C1 $WITH_D1 > ~/raggio-bench/g-build.log 2>&1 < /dev/null"
echo "0 $(date +%s)" > D:/DEV/_scratch/dgx/planG/g-build.stall
```

The last line starts the stall guard's clock, as in Step 4. Poll every 5 minutes; each poll reads the log's tail, then its size, which `g-build.stall` compares with the size first seen:

```bash
cd D:/DEV/_scratch/dgx/planG
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tail -n 3 ~/raggio-bench/g-build.log'
if size=$(ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'stat -c %s ~/raggio-bench/g-build.log'); then
  now=$(date +%s); read -r last since < g-build.stall
  [ "$size" = "$last" ] || since=$now
  echo "$size $since" > g-build.stall
  echo "g-build.log: $size bytes, no growth for $(( (now - since) / 60 )) min"
  [ $((now - since)) -lt 1800 ] || echo "STALLED: no log output for 30 min"
fi
```

A failed `ssh` leaves `g-build.stall` as it was, so a network error neither resets the clock nor counts as a stall. uv prints nothing while it compiles turbovec and `raggio_native`, so quiet polls are normal; the guard fires only after 30 minutes without growth, at 5-minute polls the sixth poll in a row. That is not a time cap (spec §3.1 G4): a build whose log keeps growing runs as long as it needs.

Stop polling on either status line, or on the stall:
- `== BUILD_DONE`;
- `== BUILD_FAILED <what>`. Go to "If the build failed" below;
- `STALLED: no log output for 30 min`, before either status line. The build is hung: go to "If a deploy or build hung" below, with `STEP=6`.

On `== BUILD_DONE`, list the images and p4's lines:

```bash
source D:/DEV/_scratch/dgx/planG/g.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "podman images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -F $SHA; grep -E 'check_image|Building (turbovec|raggio-native)|built localhost' ~/raggio-bench/g-build.log"
```

Expected:
- `localhost/raggio:<SHA>` and `localhost/raggio:<SHA>-py314t`, plus `-py314` and `-py314t-gcfreeze` if their flags are 1;
- in the build log:
  - `Building turbovec==1.0.0` for B, and for C;
  - a `Building raggio-native` line for each of B and C. This is uv's progress wording, not a status line: if it is missing, `bm25=native` in the `check_image` line is enough;
  - `check_image: Python 3.14.<patch> free-threaded, GIL disabled, <n> extension modules imported, bm25=native` for B;
  - `built localhost/raggio:<SHA>-py314t in <N> s`, and the same for C and D if they were built. B's `<N>` is p4's build time on gn100 with `raggio_native`: note it for the report, next to p4's 28.7 s [28.2..29.3] cold without it.

`check_image` passing in B already proves the cp314t build of `raggio_native`. `check_native()` (Task 5) fails the build when the compiled module it imported is not `EXTENSION_SUFFIXES[0]`, which on 3.14t is `.cpython-314t-aarch64-linux-gnu.so`. Step 7 records the file name.

**If the build failed.** Fetch the log and find the failing stage:

```bash
cd D:/DEV/_scratch/dgx/planG
scp -F "$SSH_CONFIG" -o BatchMode=yes gn100:raggio-bench/g-build.log .
grep -n -E "^(\[[0-9]+/[0-9]+\] )?STEP [0-9]+/[0-9]+:|Failed to build|check_image|BUILD_FAILED" g-build.log | tail -n 8
grep -n -E "error(\[E[0-9]+\])?:" g-build.log | head -n 10
```

The first `grep` gives the last `STEP` line before `== BUILD_FAILED` (podman prefixes a multi-stage build's steps with the stage, as in `[2/3] STEP <i>/<n>:`; the builder is stage 2 of 3), and the uv or check lines under it. The second gives the first compiler or build errors. Act on the stage; in every case but turbovec's, report the stage, those errors, and the time used:
- **``Failed to build `turbovec==1.0.0` ``**, under the builder's `RUN uv sync --frozen --no-install-project --no-dev --extra native`: turbovec's sdist does not build for cp314t on aarch64. Do not run Steps 7–9; go to "turbovec's sdist did not build" below (spec §3.1 G2), which runs nothing on gn100 but 6d's rebuilds. Never patch turbovec, fork it, vendor it, or pin another turbovec version (D6). Tasks 1–10 still stand, and the default 3.12 image is unaffected.
- **``Failed to build `raggio-native @ …` ``**: `raggio_native`'s cp314t build (PyO3 and maturin) failed. That is raggio's own code: stop and report. The fix is a new commit on the laptop, and then this task restarts from Step 2 with its new SHA.
- **the `RUN /app/.venv/bin/python /tmp/check_image.py` step, with `check_image: FAILED: <reason>`**: both builds succeeded, but the check refused the venv. Either an import re-enabled the GIL, or the reason is `raggio_native is not built for this interpreter (.cpython-314t-aarch64-linux-gnu.so): [...]` and names a file that is not cp314t. Either one is a finding for ADR 0006: stop and report it.
- **`== BUILD_FAILED deploy`**: `~/raggio` does not hold `SHA`. Go back to Step 4.
- **anything else**, for example a base image pull or disk space: stop and report.

If `<what>` names C's or D's image, B built before it and p4 has passed: its `built … in <N> s` line is in the log. Stop and report, then ask the user whether to go on without that arm. If so, set its flag in `g.env` to 0 and continue at Step 7.

The builds leave no container behind, so the stop needs no `podman` clean-up.

**If a deploy or build hung.** The stall guard of Step 4 (`STEP=4`, `deploy.log`) or of this step (`STEP=6`, `g-build.log`) printed `STALLED: no log output for 30 min`: the log has not grown for 30 consecutive minutes, so the build is hung. This is not a time cap (spec §3.1 G4), and it is not a turbovec failure, so 6b does not classify it and nothing parks. Run Step 1's stop recipe first. It stops a running `deploy.sh` and `setup.sh`, or `g-build.sh`, together with their `podman build`. Then save the log and record the hang, with `STEP` set to the step that hung:

```bash
source D:/DEV/_scratch/dgx/planG/g.env
cd D:/DEV/_scratch/dgx/planG
STEP=6  # 4 if Step 4's deploy hung
h=$(date -u +%Y%m%dT%H%MZ)
if [ "$STEP" = 4 ]; then cp deploy.log "hung-$h.log"; else scp -F "$SSH_CONFIG" -o BatchMode=yes gn100:raggio-bench/g-build.log "hung-$h.log"; fi
printf '\n## p4 hung build, not counted\n\n- time (UTC): %s\n- SHA: %s\n- step: Step %s\n- log: hung-%s.log, last line: %s\n- next: stop: build hung (no log output for 30 min)\n' "$(date -u +%Y-%m-%dT%H:%MZ)" "$SHA" "$STEP" "$h" "$(tail -n 1 "hung-$h.log")" >> p4-attempts.md
grep -c "^## p4 build " p4-attempts.md
tail -n 7 p4-attempts.md
```

Expected:
- the `grep` prints the number of `## p4 build` entries, which this entry leaves unchanged: `0` if no turbovec build has failed yet;
- the tail shows the new entry: the heading, the time, the SHA from `g.env`, the step, the log's name and last line, and `- next: stop: build hung (no log output for 30 min)`.

Then go to 6g. The hung build uses none of the 3 attempts: its heading is not `## p4 build`, so 6a's count and 6c's bound skip it. A later go-ahead restarts this task at Step 2, as Step 1 says, and the build runs again under the same number.

**turbovec's sdist did not build (spec §3.1 G2).** p4 built the same sdist for cp314t on gn100 with 0 fixes, so this path is unlikely; it stays, because G's tree, not 19a8a2e's, is what builds here. This is a bounded procedure: record the build, classify its cause from the log, then fix it in the Dockerfile's builder stage (at most 3 attempts), stop and ask the user when the attempts run out or no allowed line can express the fix (spec §3.1 G3), or, for a turbovec source cause only, draft an upstream issue and park. Every p4 build of B that fails at turbovec gets one entry in the task log `D:/DEV/_scratch/dgx/planG/p4-attempts.md`. Build 1 is the first, with no fix; fix attempt `<k>` follows the failed build `<k>` and makes build `<k>+1`. So at most 4 builds run. turbovec's own source is never patched, vendored or forked (D6), and no other turbovec version is pinned.

6a. Record the build. The `scp` above fetched `g-build.log` into `D:/DEV/_scratch/dgx/planG/`:

```bash
source D:/DEV/_scratch/dgx/planG/g.env
cd D:/DEV/_scratch/dgx/planG
n=$(cat p4-attempts.md 2>/dev/null | grep -c "^## p4 build ")
k=$((n + 1))
cp g-build.log "g-build-p4-$k.log"
printf '\n## p4 build %s\n\n- time (UTC): %s\n- SHA: %s\n- log: g-build-p4-%s.log\n' "$k" "$(date -u +%Y-%m-%dT%H:%MZ)" "$SHA" "$k" >> p4-attempts.md
grep -E "^(\[[0-9]+/[0-9]+\] )?STEP [0-9]+/[0-9]+:" g-build.log | tail -n 1
grep -n -E "Caused by|error(\[E[0-9]+\])?:|not found|No such file|could not find|requires rustc|rust-version|maturin|pyo3|PyO3|Py_GIL_DISABLED|free-threaded" g-build.log | grep -v -E "^[0-9]+:[[:space:]]*(Compiling|Downloaded|Downloading|Fresh|Checking) " | head -n 40
tail -n 6 p4-attempts.md
```

Expected:
- the `STEP` line is `[2/3] STEP <i>/<n>: RUN uv sync --frozen --no-install-project --no-dev --extra native`;
- the second `grep` lists the build's error lines, without cargo's `Compiling pyo3 …`-style progress lines;
- the tail shows `## p4 build <k>` with the time, the SHA from `g.env` and the log's name. `<k>` is 1 on the first failure.

Then complete the entry with the Edit tool, below its `- log:` line:

````markdown
- builder-stage lines kept from earlier attempts: <none, or each line in backticks>
- failing step: <the STEP line>
- error excerpt:

  ```text
  <the error lines from the second grep, verbatim, at most 40>
  ```

- cause: <1, 2, 3 or 4, from the table in 6b>, because <the log line that shows it>
- next: <one of: attempt <k>: adds <lines>, removes <lines or nothing> | stop: 3 attempts used | stop: no allowed line can express the fix | upstream draft and Park>
````

6b. Classify the cause. Read the error excerpt against this table. The first row that matches is the cause:

| Cause | What the log shows | The fix, in the builder stage only |
|---|---|---|
| 1. A missing system build dependency | a tool, header or library that `rust:*-slim-trixie` lacks: `not found` for a command (`cc`, `pkg-config`, `cmake`, `protoc`), `No such file or directory` for a `.h` file, `could not find` a system library, or a `-l<name>` failing at link time | `RUN apt-get update && apt-get install -y --no-install-recommends <packages> && rm -rf /var/lib/apt/lists/*`, where `<packages>` are the Debian trixie package names that provide it, lowercase, separated by spaces |
| 2. A Rust or maturin toolchain version | `requires rustc <version>`, or a `rust-version` newer than 1.98.1 in a crate turbovec builds; or maturin refusing its own version | Rust: `RUN rustup toolchain install <version> --profile minimal`, then `ENV RUSTUP_TOOLCHAIN=<version>`, with `<version>` a full `X.Y.Z`. Leave Plan E's global `ARG RUST_VERSION` as it is: CI's native job mirrors it. `RUSTUP_TOOLCHAIN` switches the compiler of every later builder step, `raggio_native`'s build included, in every image: the report names it. maturin: an `ENV UV_<NAME>=<value>` that constrains build dependencies, only if the uv check below prints a line and the value is a literal setting, not the path of a file the builder does not already hold |
| 3. A missing interpreter-specific build flag or env | PyO3's or maturin's build script cannot configure the interpreter: an error that names a `PYO3_*`, `CARGO_*` or `MATURIN_*` variable, `Py_GIL_DISABLED` or the free-threaded build, and says what to set | `ENV <NAME>=<value>`, where `<NAME>` is `PYO3_*`, `CARGO_*`, `MATURIN_*`, `RUSTFLAGS`, or `UV_*` (the last only if the uv check below prints a line) |
| 4. A turbovec source incompatibility | anything that only a change in turbovec's own files would fix: a Rust compile error (`error[E…]`) in turbovec's crate; a PyO3 or other crate version that turbovec's `Cargo.lock` pins and that does not support cp314t; an error that only an `UNSAFE_PYO3_*` override would silence; or a setting in turbovec's `Cargo.toml` or `pyproject.toml` | none: go to 6e. This is the only cause that parks without asking the user (spec §3.1 G3) |

The uv check, for any `UV_*` line. It runs on the laptop, with the Dockerfile's uv version, and `<NAME>` is the variable's name:

```bash
uvx --from uv==0.12.20 uv sync --help | grep -F "[env: <NAME>="
```

Expected: one line, such as `[env: UV_NO_BINARY_PACKAGE=]` for the variable Task 6 uses. If nothing prints, `uv sync` does not read that variable, and the fix cannot be that line. A value that is the path of a file (a constraints file, for example) needs that file in the builder, and no allowed line adds one: such a fix cannot be expressed either.

A fix changes only builder-stage lines of the shapes in the table; `pyproject.toml`, `uv.lock` and the runtime stage stay as they are (Global Constraints). The fix applies to every image built from the Dockerfile, A's included, so A and B stay built the same way. A cause 1–3 whose fix no line of these shapes can express (for example, one that needs a file copied into the builder) is not a change to turbovec's source, so it is not parked either (spec §3.1 G3): write `next: stop: no allowed line can express the fix` in the entry, and go to 6g.

6c. Attempt a fix, for causes 1–3. First check the bound: if `<k>` is 4, the 3 attempts are used: write `next: stop: 3 attempts used` in the entry, and go to 6g. There is no time bound (spec §6, §3.1 G4): an attempt takes about 15 min, the fix and then Steps 2–6 again, and it runs whatever time has passed since Step 1. No clock stops its redeploy or rebuild: the stall guard of Steps 4 and 6 does, but only after 30 minutes without log output, and so does the user's request for gn100 back (Step 1).

Otherwise, attempt `<k>` fixes build `<k>` in the builder stage, test first. If build `<k>` shows the same error as build `<k>-1`, the previous attempt's lines did not help: this attempt removes them, from the Dockerfile and from `P4_BUILDER_FIX`. Lines from an attempt that changed the error stay.

6c.1. Write the failing test. On attempt 1, append this section to `tests/test_freethreading.py`. `_stages()` is Task 6's helper in this module, and `re` is imported at its top. `P4_BUILDER_FIX` holds the exact lines the fix adds, one string per Dockerfile line, without the comment line. The line below is an example of cause 1's shape: write the lines of your fix instead. On attempts 2 and 3 the section exists: edit only `P4_BUILDER_FIX`, adding the new lines and removing the ones this attempt removes.

```python
# ---- probe p4: turbovec's cp314t build fix, in the builder stage only (spec §3.1 G2, D6) ----

P4_BUILDER_FIX = (  # the exact Dockerfile lines of the fix (the example is cause 1's shape)
    "RUN apt-get update && apt-get install -y --no-install-recommends pkg-config"
    " && rm -rf /var/lib/apt/lists/*",
)
P4_FIX_LINE = re.compile(  # build dependencies, a toolchain pin and build flags only
    r"RUN apt-get update && apt-get install -y --no-install-recommends [a-z0-9][a-z0-9.+ -]*"
    r" && rm -rf /var/lib/apt/lists/\*"
    r"|RUN rustup toolchain install \d+\.\d+\.\d+ --profile minimal"
    r"|ENV (PYO3_[A-Z0-9_]+|CARGO_[A-Z0-9_]+|MATURIN_[A-Z0-9_]+|UV_[A-Z0-9_]+|RUSTFLAGS"
    r"|RUSTUP_TOOLCHAIN)=\S.*")


def test_the_p4_turbovec_build_fix_stays_in_the_builder_stage():
    dockerfile, builder, _ = _stages()
    lines = builder.splitlines()
    first_sync = lines.index("RUN uv sync --frozen --no-install-project --no-dev --extra native")
    # only the shapes spec §3.1 G2 allows: never a change to turbovec's source (D6)
    assert P4_BUILDER_FIX and not [ln for ln in P4_BUILDER_FIX if not P4_FIX_LINE.fullmatch(ln)]
    for fix in P4_BUILDER_FIX:
        assert dockerfile.splitlines().count(fix) == 1, fix  # once, so in no other stage
        assert fix in lines[:first_sync], fix  # in the builder, before turbovec's build
    code = [ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code
                if re.search(r"git clone|git apply|patch -p|cargo vendor|(COPY|ADD) .*turbovec", ln)]
```

6c.2. Run the test to verify it fails:

```bash
cd D:/DEV/raggio
uv run pytest -q tests/test_freethreading.py
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `1 failed, 25 passed, 4 skipped` on 3.12 and `1 failed, 29 passed` on 3.14t. `test_the_p4_turbovec_build_fix_stays_in_the_builder_stage` fails with an `AssertionError` that names the first line of `P4_BUILDER_FIX` not yet in the Dockerfile. If the first assert fails instead, a line is not one of the allowed shapes: it is not a builder-stage fix under spec §3.1 G2. Rewrite it in an allowed shape; if none can express the fix, undo this attempt's test edit with `git checkout -- tests/test_freethreading.py` (`git status --short --untracked-files=no` then prints nothing), and stop as the end of 6b says.

6c.3. Implement. In the Dockerfile's builder stage, right after `ENV PYO3_PYTHON=/usr/local/bin/python3` and before `WORKDIR /app`, insert one comment line and then the lines of `P4_BUILDER_FIX`, in the same order and byte for byte. With the example above, the result is:

```dockerfile
ENV PYO3_PYTHON=/usr/local/bin/python3
# probe p4 (ADR 0006, spec §3.1 G2): what turbovec 1.0.0's sdist needs to build for cp314t
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config && rm -rf /var/lib/apt/lists/*
WORKDIR /app
```

On attempts 2 and 3 the comment line exists: add and remove lines under it. The lines use `&&`, never a single `|`, so Plan E's `pipefail` assert holds; nothing else in the Dockerfile changes.

6c.4. Run the tests to verify they pass:

```bash
cd D:/DEV/raggio
uv run pytest -q tests/test_freethreading.py
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
uv run pytest -q
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q
```

Expected: `26 passed, 4 skipped` and `30 passed`; then both full suites green, one test more than after Task 9 (sandbox: `308 passed, 4 skipped` and `303 passed, 2 skipped`). Plans A, E and F's Dockerfile tests stay green.

Optionally, build the 3.14t image on the laptop's podman machine first (about 5 minutes): it catches a Dockerfile mistake before the DGX round trip, although it cannot reproduce an aarch64-only failure:

```bash
MSYS_NO_PATHCONV=1 podman build --build-arg PYTHON=3.14t -t raggio:p4-fix . 2>&1 | grep -E "check_image|Building turbovec"
MSYS_NO_PATHCONV=1 podman rmi raggio:p4-fix
```

Expected: `Building turbovec==1.0.0`, then `check_image: Python 3.14.<patch> free-threaded, GIL disabled, <n> extension modules imported, bm25=native`.

6c.5. Commit on G's branch, the branch Tasks 1–9 committed to:

```bash
cd D:/DEV/raggio
git add Dockerfile tests/test_freethreading.py
git commit -m "Fix the turbovec cp314t build in the builder stage (p4, attempt <k>)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short --untracked-files=no
```

Expected: `git status` prints nothing.

6d. Record the attempt and rebuild. In `p4-attempts.md`, complete build `<k>`'s `next:` line: `attempt <k>: adds <lines>, removes <lines or nothing>, commit <short SHA>`. Then restart this task from Step 2, without asking Step 1's question again: Step 1's go-ahead covers every attempt, with no cap (spec §6, §3.1 G4), and its start time still holds for the report. If the user has asked for gn100 back meanwhile, go to Step 10 instead, as Step 1 says. Step 2 pins the new SHA in `g.env`; Step 4 deploys it, and rebuilds A from it too, so A and B stay one commit; Step 5 moves the old `g-*` files aside; Step 6 builds B again, as build `<k>+1`. Steps 4 and 6 run their stall guard as before: if the redeploy or the rebuild hangs, go to "If a deploy or build hung", which uses none of the attempts.

6e. Cause 4: draft the upstream issue. Only a change to turbovec's own source would fix the build, so raggio cannot (D6). Write the draft for the maintainer to post (spec D7). Never post it: do not open an issue, a comment or a pull request anywhere. Run `mkdir -p D:/DEV/_scratch/upstream-drafts`, then create `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md`, filling every `<...>` slot from `p4-attempts.md` and the build log:

````markdown
# turbovec 1.0.0: the sdist does not build for free-threaded CPython 3.14t (cp314t) on aarch64

> Draft for the raggio maintainer to review and post (spec D7). Not posted.

## Environment

- turbovec 1.0.0, built from its sdist by uv 0.12.19 (`uv sync`, a PEP 517 build through
  maturin): PyPI has only `cp39-abi3` wheels, and an abi3 wheel cannot load on a
  free-threaded interpreter.
- CPython <the 3.14.x version>t, free-threaded, installed by `uv python install 3.14t`.
- Rust <the rustc version from the log>, in `docker.io/library/rust:1.98.1-slim-trixie`
  (Debian trixie), on aarch64 (NVIDIA DGX, Grace CPU).

## Error

```text
<the error excerpt of the last p4 build, verbatim>
```

## Reproduce

```dockerfile
FROM docker.io/library/rust:1.98.1-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
ENV UV_PYTHON_INSTALL_DIR=/python
RUN uv python install 3.14t
RUN uv venv --python 3.14t /venv
RUN VIRTUAL_ENV=/venv uv pip install --no-binary turbovec turbovec==1.0.0
```

This repro has not been built on its own: the error above comes from raggio's image build
(`podman build --build-arg PYTHON=3.14t`) on the machine above.

## Suspected cause

<one paragraph: the file or setting in turbovec's source that the error points at, and why
only a change there fixes it>

## What would help

A cp314t wheel on PyPI, or a source change that lets the sdist build for cp314t.
````

If earlier attempts kept builder-stage lines (`P4_BUILDER_FIX`), add them to the repro, in the same order, right before its last `RUN`, and name them in "Suspected cause". Then complete build `<k>`'s entry in `p4-attempts.md` with `next: upstream draft and Park`, and check the draft:

```bash
grep -c '^```' D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md
```

Expected: `4`, the error block and the repro, each opened and closed.

6f. Park G in ADR 0006. With the Edit tool, in `docs/adr/0006-free-threaded-python.md`, replace the `Pending:` paragraph under `## Verification` (from `Pending:` to the end of the file) with the text below, filling its `<...>` slots:

````markdown
Probe p4, <YYYY-MM-DD> on gn100 (aarch64), commit `<SHA>`: turbovec 1.0.0's sdist does not
build for cp314t, in the builder's `RUN uv sync --frozen --no-install-project --no-dev --extra native`.
Cause: <one sentence: what in turbovec's own source the error points at>. Builder-stage
fix attempts (spec §3.1 G2): <0 to 3>, <one clause per attempt: the lines it added, and what
the next build showed>. No A/B ran.

```text
<the error excerpt of the last p4 build, verbatim, as in the upstream draft>
```

An upstream issue is drafted for the maintainer to post (spec D7). raggio never patches,
vendors or forks turbovec (D6).

### Decision

**Park**: turbovec 1.0.0's sdist does not build for cp314t on aarch64, and only a change to
turbovec's own source would fix it (spec §3.1 G2). The build arg and the CI job stay; no
more measuring until a turbovec release builds for cp314t or ships a cp314t wheel.
````

Builder-stage lines that earlier attempts kept stay in the Dockerfile; the Park records them. Check and commit:

```bash
cd D:/DEV/raggio
uv run pytest -q tests/test_freethreading.py -k docs_record
uv run --group docs mkdocs build --strict -d "$(mktemp -d)"
grep -c "^Pending:" docs/adr/0006-free-threaded-python.md
git add docs/adr/0006-free-threaded-python.md
git commit -m "Park the free-threaded evaluation at probe p4 (ADR 0006)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short --untracked-files=no
```

Expected: `1 passed, 28 deselected` (`1 passed, 29 deselected` if an attempt added the probe p4 test); `mkdocs` exits 0 with no warnings; `grep` prints `0`; `git status` prints nothing. Task 12 does not run.

6g. Report and stop. Run nothing else on gn100, and go to Step 10. If 6b or 6c stopped (3 attempts used, or no allowed line can express the fix although turbovec's source needs no change), ADR 0006 stays `Pending:` until the user decides, and Task 12 does not run: report, and ask the user how to go on (spec §3.1 G3). A hung deploy or build ("If a deploy or build hung") ends the same way: ADR 0006 stays `Pending:`, Task 12 does not run, and the user is asked how to go on, with the step and the image that hung. None of these cases parks.

- [ ] **Step 7: Record what each image runs, and the Unicode drift (detached, then poll)**

```bash
source D:/DEV/_scratch/dgx/planG/g.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "setsid -f bash ~/raggio-bench/g-facts.sh $SHA $WITH_C1 $WITH_D1 > ~/raggio-bench/g-facts.log 2>&1 < /dev/null"
```

Poll every minute until the last line is `== FACTS_DONE` or `== FACTS_FAILED <what>`:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tail -n 8 ~/raggio-bench/g-facts.log'
```

Expected on `== FACTS_DONE`:
- A: `Python 3.12.<patch>, GIL enabled, turbovec 1.0.0 (cp39-abi3-manylinux_2_28_aarch64), raggio_native 0.1.0 (raggio_native.cpython-312-aarch64-linux-gnu.so), SQLite 3.53.1, OPENBLAS_NUM_THREADS 1, Unicode 15.0.0, raggio_native Unicode 15.0.0`;
- B: `Python 3.14.<patch>t, GIL disabled, turbovec 1.0.0 (cp314-cp314t-linux_aarch64), raggio_native 0.1.0 (raggio_native.cpython-314t-aarch64-linux-gnu.so), SQLite 3.53.1, OPENBLAS_NUM_THREADS 1, Unicode 16.0.0, raggio_native Unicode 16.0.0`;
- C, if built: `Python 3.14.<patch>, GIL enabled, turbovec 1.0.0 (cp39-abi3-linux_aarch64), raggio_native 0.1.0 (raggio_native.cpython-314-aarch64-linux-gnu.so), ...`;
- D, if built: as B;
- the drift line, `drift: 15.0.0 -> 16.0.0: 5812 newly assigned, 1 changed, 5053 with token drift`. These are the laptop's x86_64 numbers from Task 8, and aarch64 should give the same;
- the scan line: a JSON object with `"rows": 2549619` and the rows that contain a drifted code point.

The SQLite versions are the ones the uv-managed interpreters bundle. p4 read SQLite 3.53.1 in all three of uv 0.12.19's PBS interpreters (3.12.14, 3.14.7 and 3.14.7t), so A, B, C and D should all say 3.53.1. p2's scaling figures were measured on PBS SQLite 3.50.4 and not re-measured on 3.53.1. Note both versions for the report; another version than 3.53.1 is a finding. A difference is not a reason to stop, but it matters for the verdict: Task 12 counts a `Hybrid QPS concurrent` win only when A and B run the same `sqlite_version` (D15).

A different drift line is a finding: note it for the report and go on. Stop and report if:
- the status is `FACTS_FAILED`, for example a `RuntimeWarning` from the GIL guard;
- B says `GIL enabled`;
- A's turbovec wheel tag is not `abi3`;
- B's (or D's) `raggio_native` file is not `raggio_native.cpython-314t-…`, or any image's `raggio_native` file contains `abi3` (spec §4.2);
- any image says anything other than `OPENBLAS_NUM_THREADS 1` (D16). Task 12's arms must run the same OpenBLAS thread count, and `t12-section.py` refuses a table without it.

- [ ] **Step 8: Make sure the volume has its IVF index (detached, then poll)**

The fan-out probe reads the IVF index from the volume. On a flat volume, `g-ensure-ivf.sh` builds one at 4 GiB with image A, as `bench.py`'s IVF run0 does.

```bash
source D:/DEV/_scratch/dgx/planG/g.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "setsid -f bash ~/raggio-bench/g-ensure-ivf.sh localhost/raggio:$SHA > ~/raggio-bench/g-ensure-ivf.log 2>&1 < /dev/null"
```

Poll every 2 minutes until the log has `ready from the IVF index`, or a line that ends in `stopping`:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tail -n 3 ~/raggio-bench/g-ensure-ivf.log'
```

The probe runs in a container named `bench-tv`, so stop and remove the server first:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'podman stop -t 60 bench-tv && podman rm -f bench-tv && echo REMOVED'
```

Expected: `REMOVED`. On `stopping`, fetch `g-ensure-ivf.log`, stop `bench-tv` the same way, and report.

- [ ] **Step 9: The IVF fan-out probe in both images (detached, then poll)**

```bash
source D:/DEV/_scratch/dgx/planG/g.env
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "setsid -f bash ~/raggio-bench/g-fanout.sh $SHA > ~/raggio-bench/g-fanout.log 2>&1 < /dev/null"
```

Poll every 5 minutes until the last line of `g-fanout.log` is `== FANOUT_DONE` or `== FANOUT_FAILED <what>`:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tail -n 2 ~/raggio-bench/g-fanout.log; tail -n 3 ~/raggio-bench/g-fanout-A.txt ~/raggio-bench/g-fanout-B.txt 2>/dev/null'
```

Expected on `== FANOUT_DONE`: each of `g-fanout-A.txt` and `g-fanout-B.txt` ends with Plan F's gate line, `serial r1/r2 <a>/<b> ms; chosen pool size <T> (default here <D>); GATE grouped vs plain at <T>: WIN` or `LOSS`. On `FANOUT_FAILED`, fetch the two files and `g-fanout.log`, and report. The transient container was `--rm`, so nothing is left running.

- [ ] **Step 10: Report to the user**

Report:
- p4: B's build time (`built localhost/raggio:<SHA>-py314t in <N> s`), against p4's 28.7 s [28.2..29.3] cold without `raggio_native` on 2026-09-28, and whether turbovec's sdist and `raggio_native` built for cp314t on aarch64. If the task stopped at Step 4 or Step 6, this, the items below on turbovec's build and on a hang, and the time used are the whole report;
- if turbovec's sdist failed at least once (spec §3.1 G2), from `p4-attempts.md`:
  - each build's number, SHA, cause (1–4) and first error line;
  - each fix attempt's lines and commit, and whether the next build got further;
  - which ended it: a build that passed, the Park (with the path `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md`, which is not posted: the maintainer reviews and posts it, and I can build its repro Dockerfile on the laptop if asked), 3 attempts used, a fix no allowed line can express (name the fix it needs), or the user's request for gn100 back (Step 1's stop recipe; there is no cap, spec §6, §3.1 G4);
  - any `RUSTUP_TOOLCHAIN` line a fix kept: it also changes the compiler that builds `raggio_native` in every image;
- if a deploy or build hung (Steps 4 and 6), from the `## p4 hung build, not counted` entry: the step, the hung log's last line and its path `D:/DEV/_scratch/dgx/planG/hung-<UTC time>.log`, and that Step 1's stop recipe stopped it after 30 minutes without log output. It used none of the 3 attempts, and it is not a time cap (spec §3.1 G4);
- the four image lines (or two), with each image's `raggio_native` file, SQLite version and `OPENBLAS_NUM_THREADS`, and whether A and B run the same SQLite;
- the drift and scan lines, against the laptop's;
- the two `GATE` lines, and the pool sizes the probe chose in each image;
- the time used since Step 1's start time (Task 11 has no cap, spec §6, §3.1 G4).

Ask whether Task 12's ABAB should run now or in a later window; after a Park, a stop at Step 4 or Step 6, or Step 1's stop recipe, Task 12 does not run, so do not ask. After 6g's stop (3 attempts used, no allowed line can express the fix, or a hung deploy or build), ask the user how to go on instead; ADR 0006 stays `Pending:` until the user decides (spec §3.1 G3). Task 12 must use these images, so the repo's HEAD must stay at `SHA` until then. This step commits nothing: `git status --short --untracked-files=no` prints nothing. The only commits Task 11 makes are Step 6's, on p4's failure path.

---

### Task 12 (DGX, last): the ABAB, recorded in ADR 0006

This task runs A1 A1c16 B1 B1c16 A2 A2c16 B2 B2c16, plus C1 and D1 if Task 11 built them, as one detached chain on gn100, on one boot. Each `-c16` session repeats its arm session's runs on the same image at `CONCURRENCY=16` (spec §3.1 G7); every other session runs at `CONCURRENCY=8`. It renders the outputs into ADR 0006's Verification on gn100, then fetches them. It adds turbovec's PyPI record, and applies the decision rule ADR 0006 wrote down before the run. The test Task 10 prepared pins the result.

The noise band follows spec §6:
- every run gets a fresh 4 GiB container (Plan A's `session.sh`);
- `FLAT_RUNS=3`, so the bimodal concurrent-QPS rows have 3 counted runs a side;
- both arms run `OPENBLAS_NUM_THREADS=1`, from the one Dockerfile;
- A1 → A2 and B1 → B2 measure the between-server variance that a win in A1 → B1 and A2 → B2 must clear.

Every table states its regime, memory cap, `sqlite_version` and `openblas_num_threads`. The SQLite-bound hybrid rows are read apart from the CPU-bound ones (D15).

It needs Task 11's images, at the same commit, and the user's explicit go-ahead with a time cap. It runs only if Task 11's p4 passed, on the first build or after a builder-stage fix (spec §3.1 G2); never after a Park at p4. The DGX rules are those of Task 11 and the Global Constraints.

**Files:**
- Modify: `docs/adr/0006-free-threaded-python.md`. The `Pending:` Verification is replaced with the recorded one, plus `### Upstream wheels` and `### Decision`.
- Modify: `tests/test_freethreading.py`. The `ADR 0006: the DGX ABAB and its decision` section, from `D:/DEV/_scratch/dgx/planG/t12-section.py`, is appended.
- Outside the repo: create `D:/DEV/_scratch/dgx/planG/results/` with `g-verification.md`, `g-abab-outputs.tar.gz` and `turbovec.json`.
- On gn100, in `~/raggio-bench/`:
  - `g-abab.log`, `g-boot-before.txt`, `g-boot-after.txt`;
  - per session `<label>`:
    - `<label>/`, Plan A's run JSONs;
    - `<label>.log`, `<label>.healthz.log`, `<label>.logs/`, `<label>.end`;
  - `g-verification.md`.

**Interfaces:**
- Consumes:
  - `g.env` and the images from Task 11;
  - Task 10's scripts;
  - Plan A's `session.sh`, `compare.py` and the `ADOPT` convention: `ADOPT=1` only while `~/raggio-bench/state/fingerprint-raggio-2549619.json` does not exist;
  - ADR 0006's decision rule (Task 9).
- Produces:
  - ADR 0006 with a recorded Verification and one of **Continue**, **Hold** or **Park**;
  - `test_adr_0006_records_the_dgx_abab_and_its_decision`. It checks:
    - the two boot ids are equal;
    - the images come from one commit, A is abi3 with the GIL and B is cp314t without;
    - `raggio_native` is `cpython-312-` in A and `cpython-314t-` in B, never abi3, and every image line gives `OPENBLAS_NUM_THREADS 1` and a SQLite version;
    - every session has `/healthz` samples and container logs;
    - the four pair tables have both gate rows, `QPS concurrent` and `Hybrid QPS concurrent`;
    - the `GIL and logs` table has a row for each of the eight arm sessions, the four `-c16` ones included, and `abab_decision` gates all of them (spec §3.1 G8);
    - every pair table, the four `(c=16)` ones included, has both Regime lines, with `openblas_num_threads=1` and a known `sqlite_version`. Cross-arm tables differ at most in `python` and `sqlite_version`, same-arm tables in nothing, and no table has Plan A's `Reported, not claimed` note;
    - the fan-out and drift sections are present, and the scan covers 2,549,619 rows;
    - the turbovec record is present;
    - the Decision section names exactly the outcome `abab_decision` computes, lists every cost, and states `A runs SQLite <a>, B runs SQLite <b>`. `abab_decision` drops the `(c=16)` tables, so they are reported and never gate (spec §3.1 G7); it applies G5's hybrid p99 guard and its flat-run states (the rule text above).

- [ ] **Step 1: Ask the user for a run window (time cap)**

Ask, and wait for an explicit answer. Do not run anything on gn100 before it:

> Plan G's ABAB needs a run window on gn100 of about 7 hours:
> - eight sessions, one after another, about 50 min each: A1 (3.12), B1 (3.14t), A2 and B2 at concurrency 8, each followed on the same image by a concurrency-16 session (A1c16, B1c16, A2c16, B2c16). p4 saw 3.14t's hybrid gain only at c=16, so the c=16 tables are recorded in ADR 0006, but they are reported and do not gate: the decision reads the c=8 tables (spec §3.1 G7);
> - each session first rebuilds the IVF index at 4 GiB, then runs Plan A's IVF runs, the 8 GiB detach and the flat runs. The flat runs are `FLAT_RUNS=3`, each in a fresh container, so concurrent QPS has the 3 runs a side that spec §6 asks for;
> - A1 → A2 and B1 → B2 measure the between-server noise that a free-threading win must clear. Both arms run `OPENBLAS_NUM_THREADS=1`, and every table records its `sqlite_version`, because the hybrid rows are bound by SQLite's mutex (D15).
>
> Each session, the c=16 ones included, is capped at 90 min (spec §3.1 G1), so the worst case is 8 × 90 min = 12 hours, plus 90 min for each of C1 and D1: 15 hours with both. The chain stops at the first session that fails or reaches its cap.
>
> [If `WITH_C1=1`:] C1 (3.14 with the GIL, turbovec built locally from the same sdist with maturin, as an abi3 wheel, `cp39-abi3-linux_aarch64`, while B's is `cp314-cp314t`) adds about 50 min, at concurrency 8 only. It is not the same turbovec build as B: it separates the 3.12 → 3.14 interpreter effect and a local build from PyPI's manylinux wheel (maturin 1.14.1).
> [If `WITH_D1=1`:] D1 (3.14t with `GC_FREEZE=1`) adds about 50 min, at concurrency 8 only.
>
> Only `bench-tv`, its volume and `~/raggio-bench/g-*` are used. `bench-wv` is not touched, `~/turborag` is read only through Plan A's corpus symlink, and nothing needs `sudo`. The machine should not reboot, and nothing else should run on it meanwhile: the result needs one boot.
>
> May I start it? If so, with what time cap, and with or without C1 and D1?

If the user declines, stop. ADR 0006 stays `Pending:`, and Task 11's images stay for a later window.

If the user drops C1 or D1 now, set its flag in `D:/DEV/_scratch/dgx/planG/g.env` to 0. A flag can only go from 1 to 0 here, because an arm Task 11 did not build has no image.

Note the cap and the start time. If the cap is reached before Step 5 sees `g-abab: done`, use Step 5's stop recipe and go to the failure path at the end of this task.

- [ ] **Step 2: Check that the repo and gn100 are still at Task 11's commit**

```bash
source D:/DEV/_scratch/dgx/planG/g.env
cd D:/DEV/raggio
[ "$(git rev-parse --short=7 HEAD)" = "$SHA" ] && [ -z "$(git status --short --untracked-files=no)" ] && echo "HEAD is $SHA, clean"
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "echo deployed \$(cat ~/raggio/.deployed-sha); podman images --format '{{.Repository}}:{{.Tag}}' | grep -F $SHA"
```

Expected:
- `HEAD is <SHA>, clean`;
- `deployed <SHA>`;
- `localhost/raggio:<SHA>` and `localhost/raggio:<SHA>-py314t`, plus `-py314` if `WITH_C1=1` and `-py314t-gcfreeze` if `WITH_D1=1`.

If anything differs, stop and ask the user. The arms must come from the commit Task 11 deployed and built, and the Task 12 test checks that the images share one SHA.

- [ ] **Step 3: Pre-flight on gn100 (read-only, then a move-aside)**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
uptime; free -m | head -2; df -h ~ | tail -1
pgrep -af "bench/bench[.]py" || echo "no bench.py running"
pgrep -af "raggio-bench/g-[a-z-]*[.]sh" || echo "no g- script running"
podman ps -a --format "{{.Names}} {{.Status}} {{.Image}}"
pgrep -af "bench/dgx/session[.]sh" || echo "no session.sh running"
ls ~/raggio-bench/g-active 2>&1
cd ~/raggio-bench && ls g-images.txt g-drift-summary.txt g-drift-scan.json g-fanout-A.txt g-fanout-B.txt
ls ~/raggio-bench/state/fingerprint-raggio-2549619.json 2>&1'
```

Expected:
- `no bench.py running`, `no g- script running` and `no session.sh running`;
- no `bench-tv` line with status `Up`. `bench-wv` may be listed: leave it alone;
- `g-active`: `No such file or directory` (Plan A's `session.sh` holds no lock file);
- the five Task 11 outputs listed.

Stop and ask the user, and never stop anything yourself, if:
- a bench.py, a `session.sh` or a `g-` script is running;
- `bench-tv` is up;
- `g-active` exists;
- a Task 11 output is missing.

Set `ADOPT` from the last line: `ADOPT=0` if the fingerprint exists, `ADOPT=1` if it is `No such file or directory`.

Plan A's `session.sh` refuses a label whose directory exists, and a stale `<label>.end` would end a new session's sampler at once. So move the outputs of any earlier ABAB aside; nothing is deleted:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'cd ~/raggio-bench && shopt -s nullglob && old=(g-[abcd][12]-* g-boot-*.txt g-abab.log g-verification.md) && if [ ${#old[@]} -gt 0 ]; then d=planG-prev-$(date +%Y%m%d-%H%M%S) && mkdir "$d" && mv "${old[@]}" "$d"/ && echo "moved ${#old[@]} to $d"; else echo "nothing to move"; fi'
```

Expected: `nothing to move` on the first ABAB. The pattern `g-[abcd][12]-*` covers the `-c16` sessions too, such as `g-a1-py312-c16.log`.

- [ ] **Step 4: Start the ABAB (detached)**

```bash
source D:/DEV/_scratch/dgx/planG/g.env
ADOPT=0  # 1 only if Step 3 found no fingerprint
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 "ADOPT=$ADOPT setsid -f bash ~/raggio-bench/g-abab.sh $SHA 90m $WITH_C1 $WITH_D1 > ~/raggio-bench/g-abab.log 2>&1 < /dev/null"
sleep 60
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'cat ~/raggio-bench/g-abab.log; cat ~/raggio-bench/g-active'
```

Expected:
- `<ts> g-abab: <SHA>, 8 sessions, 90m each, boot <uuid>`, with 9 sessions if one of C1 and D1 is on, 10 if both are;
- `<ts> g-abab: g-a1-py312 (localhost/raggio:<SHA>, c=8)`;
- `g-a1-py312`, from the lock.

- [ ] **Step 5: Poll until the chain ends**

Poll every 10 minutes:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tail -n 3 ~/raggio-bench/g-abab.log; cat ~/raggio-bench/g-active 2>/dev/null; pgrep -af "bench/dgx/session[.]sh"; podman ps --format "{{.Names}} {{.Status}} {{.Image}}"'
```

The last line of `g-abab.log` ends the chain in one of two ways:
- `<ts> g-abab: done`: go to Step 6.
- `<ts> g-abab: <label> ended with '<last line>': stopping`: fetch `<label>.log`, which holds `g-ensure-ivf.sh`'s and `session.sh`'s output for that session, then go to the failure path. `g-session <label>: exit 124` means the session reached its 90 min cap (spec §3.1 G1).

**Stop recipe**, for the user's time cap or on the user's request. It was validated in a container, with the real `g-abab.sh` and `g-session.sh` over stub sessions: the chain stops, the running session's `timeout` passes the TERM on, and no `g-session.sh` is left after 70 s. The 70 s wait covers `--kill-after=60`. Then `bench-tv` is stopped.

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'for p in $(pgrep -f "raggio-bench/g-abab[.]sh"); do kill "$p"; done; for p in $(pgrep -f "timeout --kill-after=60 [0-9]+m bash -c"); do kill "$p"; done; sleep 70; podman stop -t 60 bench-tv; pgrep -af "bench/dgx/session[.]sh" || echo "no session.sh running"; ls ~/raggio-bench/g-active 2>&1'
```

Expected: `no session.sh running`, and `g-active`: `No such file or directory`. If a `session.sh` is still running or `g-active` remains, report it and ask; do not kill or remove anything yourself. Then go to the failure path.

- [ ] **Step 6: Render the Verification on gn100**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'podman stop -t 60 bench-tv; cd ~/raggio && .venv/bin/python ~/raggio-bench/g-render.py && cat ~/raggio-bench/g-boot-before.txt ~/raggio-bench/g-boot-after.txt'
```

Expected:
- `bench-tv`;
- the path `/home/<user>/raggio-bench/g-verification.md`;
- the same boot id twice.

Two different boot ids mean gn100 rebooted during the chain. The Task 12 test will then fail, as it must: take the failure path.

- [ ] **Step 7: Fetch the outputs**

```bash
mkdir -p D:/DEV/_scratch/dgx/planG/results
cd D:/DEV/_scratch/dgx/planG/results
scp -F "$SSH_CONFIG" -o BatchMode=yes gn100:raggio-bench/g-verification.md .
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'cd ~/raggio-bench && tar -czf - g-[abcd][12]-* g-boot-*.txt g-images.txt g-u-*.json g-drift* g-fanout-* g-abab.log g-build.log g-facts.log g-ensure-ivf.log g-fanout.log g-verification.md' > g-abab-outputs.tar.gz
tar -tzf g-abab-outputs.tar.gz | wc -l
head -n 20 g-verification.md
```

Expected: a non-zero file count, and the Verification's first lines:
- `DGX ABAB on gn100 (spec §6): the sessions g-a1-py312, g-b1-py314t, g-a2-py312, g-b2-py314t, in this ...`;
- the two `boot_id` lines;
- `### Images` with the lines Task 11 recorded.

- [ ] **Step 8: Fetch turbovec's PyPI record**

```bash
curl -sf https://pypi.org/pypi/turbovec/json -o D:/DEV/_scratch/dgx/planG/results/turbovec.json && echo fetched
```

Expected: `fetched`. This is a read-only GET; nothing is posted.

- [ ] **Step 9: Append the failing test**

```bash
cd D:/DEV/raggio
cat D:/DEV/_scratch/dgx/planG/t12-section.py >> tests/test_freethreading.py
```

The section is Task 10's `t12-section.py`, already shown in full there. It adds:
- `GATE_BETTER` (the two gate rows), `GATE_NOT_WORSE`, `SQLITE_BOUND`, `CROSS_PAIRS`, `SAME_ARM_PAIRS`, `FREE_THREADED_ARMS` and `OUTCOMES`;
- `abab_sections`, `worse_in_both`, `sqlite_versions`, `gate_rows` and `abab_decision`, which `g-decide.py` imports, and their helpers `_regimes` and `_differ`;
- `test_adr_0006_records_the_dgx_abab_and_its_decision`.

- [ ] **Step 10: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_freethreading.py -k adr_0006`

Expected: `1 failed, 29 deselected`, or `1 failed, 30 deselected` if Task 11 added the probe p4 test (spec §3.1 G2), with `AssertionError: Task 12 records the DGX ABAB`. The ADR still says `Pending:`.

- [ ] **Step 11: Record the Verification and the decision**

Run from the repo root:

```bash
uv run python - D:/DEV/_scratch/dgx/planG/results/g-verification.md D:/DEV/_scratch/dgx/planG/results/turbovec.json < D:/DEV/_scratch/dgx/planG/g-decide.py
```

It replaces everything after `## Verification` in `docs/adr/0006-free-threaded-python.md` with:
- the fetched Verification;
- `### Upstream wheels`: the date, turbovec's latest version and its cp314t wheels, as JSON;
- `### Decision`:
  - the outcome under the rule, naming the gate rows B won;
  - the reasons B did not win (Park only);
  - `Not won:`, the gate row that missed while the other won;
  - the SQLite paragraph, `A runs SQLite <a>, B runs SQLite <b>`;
  - the costs, with the SQLite-bound ones marked `(SQLite-bound, D15)`;
  - the Noise paragraph.

It prints the outcome, one line per reason, and `costs: [...]`.

Read the new Verification before going on:
- the eight pair tables (four c=8, four `(c=16)`), and the Regime lines above each. Cross-arm tables differ at most in `python` and `sqlite_version`; same-arm tables differ in nothing; `openblas_num_threads=1` everywhere. The test checks this; read it anyway, to know what the report compares;
- the two gate rows in A1 → B1 and A2 → B2, read apart. `QPS concurrent` is CPU-bound, and free threading can move it. `Hybrid QPS concurrent` runs under SQLite's global memstatus mutex (D15). If it does not scale, or gets worse, it is SQLite-bound, not a free-threading failure; and if A and B run different `sqlite_version`s, a hybrid gain cannot be credited to free threading either;
- whether the A1 → A2 or B1 → B2 table has a flat row `better` or `worse` beyond the band. Between two sessions of one arm, that is between-server variance (p2 saw identical code 6–7% apart at IVF warm p50, and vector concurrent QPS bimodal at 120–225). On a gate row it keeps B from winning; on any other row, say in the report that the band is too narrow to trust there;
- the `GIL and logs` table: every B row has `gil_enabled false` equal to its `/healthz answers`, and every row, the `-c16` ones included, has 0 `Traceback or GIL lines`.

- [ ] **Step 12: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_freethreading.py`. Expected: `26 passed, 4 skipped`, or `27 passed, 4 skipped` if Task 11 added the probe p4 test (spec §3.1 G2).

Run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-ft PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" REQUIRE_NATIVE=1 uv run --no-sync pytest -q tests/test_freethreading.py
```

Expected: `30 passed`, or `31 passed` with the probe p4 test.

Run `uv run pytest -q`. Expected: all green (sandbox: `308 passed, 4 skipped`; `309 passed, 4 skipped` with the probe p4 test). Run the full 3.14t suite. Expected: all green (sandbox: `303 passed, 2 skipped`; `304 passed, 2 skipped` with the probe p4 test).

Run: `uv run --group docs mkdocs build --strict -d "$(mktemp -d)"`. Expected: it exits 0 with no warnings. The ADR's new tables and code blocks render.

Run `git status --short`. Expected: only ` M docs/adr/0006-free-threaded-python.md` and ` M tests/test_freethreading.py`, plus whatever was untracked before Task 11.

- [ ] **Step 13: Commit**

```bash
git add docs/adr/0006-free-threaded-python.md tests/test_freethreading.py
git commit -m "Record the DGX ABAB in ADR 0006" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 14: Report to the user**

Report:
- the outcome (Continue, Hold or Park), the reasons and the costs, as `g-decide.py` printed them;
- both gate rows of A1 → B1 and A2 → B2, `QPS concurrent` (CPU-bound) and `Hybrid QPS concurrent` (SQLite-bound, D15), each with medians, bands and verdicts, and kept apart;
- the `sqlite_version` of each arm, and `OPENBLAS_NUM_THREADS=1` in both, from the Regime lines;
- `Hybrid p99 under concurrency (ms)` in A1 → B1 and A2 → B2 (G5's guard: a hybrid QPS win needs it not `worse` in either), and CPU per query for every flat run, medians included, with each run's fast/slow state and the split per side (reported, never gated; if the split was uneven, or a state unknown, say that `QPS concurrent` and `p95 under concurrency (ms)` counted as neither win nor miss, spec §3.1 G9);
- the four `(c=16)` tables, `A1 → B1 (c=16)` and the rest, each with medians, bands and verdicts. Their speed rows are reported, not gated (spec §3.1 G7). Say whether 3.14t's hybrid gain shows at c=16 as p4 saw, and whether its p99 came with it;
- the `GIL and logs` table's eight rows, the four `-c16` ones included. Every session's checks gate (spec §3.1 G8), so any Traceback, GIL line or 3.14t `/healthz` answer without `gil_enabled` false in a `-c16` session is named as a Park reason;
- the noise seen in A1 → A2 and B1 → B2;
- any serial-latency cost, marked as not attributed: rootless podman cannot pin cores, and p3 measured a 2x serial gap between the X925 and A725 cores (D16). Attributing one needs both arms re-measured pinned with `os.sched_setaffinity`, which is a new run window;
- the C1 and D1 tables, if they ran. They are recorded, not gated;
- the two fan-out `GATE` lines;
- the Unicode drift line, and the scan's row count;
- turbovec's latest version and its cp314t wheels;
- the time used against the cap.

Do not claim a cold-start difference: the sessions do not run true-cold (spec §6).

**Failure path.** This covers:
- the chain stopped early;
- the time cap was reached;
- two boot ids;
- the Task 12 test fails after Step 11.

In any of these cases:
1. Do not commit.
2. If Step 9 or Step 11 already ran, restore both files:

   ```bash
   cd D:/DEV/raggio
   git checkout -- docs/adr/0006-free-threaded-python.md tests/test_freethreading.py
   git status --short --untracked-files=no
   ```

   Expected: `git status` prints nothing. ADR 0006 stays `Pending:`.
3. Fetch whatever the sessions left (Step 7's `tar` line; missing names only print a warning).
4. Report what stopped the chain, with the last lines of `g-abab.log` and of the failing `<label>.log`.
5. Ask the user whether to re-run in a new window, and with what cap. A re-run starts again at Step 1. Step 3 moves the earlier outputs aside.

---

## Self-Review

### Spec coverage

| Spec item | Where | How it is checked |
|---|---|---|
| §1 item 4, §2 row 8: a `PYTHON=3.14t` build arg on the unified Dockerfile | Task 6 | The Dockerfile tests. In the sandbox, local podman builds of 3.12, 3.14t and 3.14 with the turbovec sdist all passed `check_image` |
| §2 row 8: the turbovec sdist build in the builder stage | Tasks 6, 11 | `test_the_builder_can_build_turbovec_from_its_sdist`. `Building turbovec==1.0.0` appears in the 3.14t build log, and Task 11 records B's `cp314-cp314t-linux_aarch64` tag, the tag p4 built on gn100 with no build arg |
| §8 probe p4: the 3.14t aarch64 build of turbovec's sdist and `raggio_native` (p4 ran 2026-09-28 on 19a8a2e, without `raggio_native`: turbovec built `cp314-cp314t-linux_aarch64` with 0 fixes, 28.7 s cold, the GIL stayed off; `raggio_native`'s cp314t build and G's real `check_image.py` on G's Dockerfile are still open) | Task 11 Step 6, the first DGX step after the deploy | `g-build.sh` logs `built <image> in <N> s`, noted next to p4's 28.7 s [28.2..29.3]. On `== BUILD_FAILED`, Step 6 names the stage from the log (turbovec sdist, `raggio-native`, `check_image`, deploy). A failing turbovec sdist runs the bounded procedure of spec §3.1 G2 (6a–6g): each build is logged in `p4-attempts.md` and its cause classified; a build-dependency, toolchain or build-flag cause is fixed in the builder stage only, in at most 3 attempts, each pinned by `test_the_p4_turbovec_build_fix_stays_in_the_builder_stage`; a turbovec source cause gets the upstream draft and the Park, and Task 12 does not run; 3 failed attempts, or a fix no allowed line can express, stop and ask the user, with ADR 0006 left `Pending:` (spec §3.1 G3); no time cap bounds the build or the attempts (spec §6, §3.1 G4). A deploy or build whose log has not grown for 30 minutes is hung, not capped: the stall guard of Steps 4 and 6 prints `STALLED: no log output for 30 min`, Step 1's stop recipe stops it, the `## p4 hung build, not counted` entry records it with `next: stop: build hung (no log output for 30 min)`, and 6g stops and asks, using none of the 3 attempts |
| §4.2: `raggio_native` built per interpreter, without abi3, `gil_used = false` | Tasks 5, 6, 11, 12 | `check_native()` in `check_image` fails the build when an imported `raggio_native` module is not `EXTENSION_SUFFIXES[0]` (`test_check_image_finds_every_compiled_module_it_can_import` pins `built_for_this_interpreter`). Task 6 asserts E's `uv python find "${PYTHON}"` link and `ENV PYO3_PYTHON=/usr/local/bin/python3`. Task 11 stops on a non-`cpython-314t-` file in B, and the Task 12 test requires `cpython-312-` in A, `cpython-314t-` in B, and no abi3 |
| §3 D15: the SQLite memstatus mutex; every row records `sqlite_version`; G's verdict accounts for it | Tasks 9, 10, 11, 12 | ADR 0006's rule and `abab_decision`: two gate rows; `Hybrid QPS concurrent` wins only on equal `sqlite_version`s; `SQLITE_BOUND` costs never park. `g-check.py`'s `sqlite-bound`, `hybrid-win` and `hybrid-other-sqlite` scenarios. The Task 12 test requires a known `sqlite_version` in every Regime line, SQLite in every image line, and the Decision's `A runs SQLite <a>, B runs SQLite <b>`. p4 read SQLite 3.53.1 in uv 0.12.19's 3.12.14, 3.14.7 and 3.14.7t, so Task 11 Step 7 expects 3.53.1 in every image line, `g-check.py`'s fixtures use it, and another version is a finding; p2's 3.50.4 figures are quoted as p2's, not re-measured on 3.53.1 (ADR 0006's Context) |
| §3 D16: `ENV OPENBLAS_NUM_THREADS=1` in the image; the same in both arms | Tasks 6, 10, 11, 12 | Task 6 Step 1 requires A's line on the base, and its test requires exactly one `ENV OPENBLAS_NUM_THREADS=1` in the runtime stage. Task 11 stops on any other value, and the Task 12 test requires `openblas_num_threads=1` in every Regime line and `OPENBLAS_NUM_THREADS 1` in every image line. `g-check.py`'s `blas-differs` scenario shows the test failing |
| §6: the noise band (a fresh container per arm, >= 3 runs for concurrent QPS, between-server variance, a win only beyond the band) | Tasks 9, 10, 12 | Plan A's `session.sh` (a fresh container per run) with `FLAT_RUNS=3` from `g-session.sh`. `gate_rows` needs `better` in A1 → B1 and A2 → B2 and neither better nor worse in A1 → A2 and B1 → B2. `g-check.py`'s `noisy` and `thin` scenarios. p3's X925/A725 split is in ADR 0006's Noise paragraph: serial costs stay unattributed unless re-measured pinned with `os.sched_setaffinity` |
| §2 row 8: the GIL guard | Tasks 3, 5, 6, 7 | The suite's guard tests, and `check_image`'s tests against a real extension that needs the GIL. Also `test_the_runtime_image_fails_loudly_on_a_gil_re_enable` and `test_ci_314t_job_turns_a_gil_re_enable_into_a_failure` |
| §2 row 8, §4.2: `GC_FREEZE`, owner G | Tasks 2, 9 | The four `GC_FREEZE` tests (default, order, cycles, a frozen collection freed); the env-table row in `docs/getting-started.md` |
| §2 row 8: the ABAB A/B on the DGX | Tasks 10, 11, 12 | `g-check.py`'s twenty-three scenarios; the Task 12 test |
| §3 D1: ADR 0006 | Tasks 9, 11 (p4 Park only), 12 | `test_docs_record_the_evaluation_and_document_the_knob`; `test_adr_0006_records_the_dgx_abab_and_its_decision` |
| §3 D2: one Dockerfile, no `Dockerfile.ft` | Task 6 | Only `Dockerfile` is edited, and the sandbox's `git diff --stat` has no new Dockerfile. `PYTHON=3.14 UV_NO_BINARY_PACKAGE=turbovec` builds the GIL control from the same file (C1, a local abi3 turbovec build, spec §3.1 G6); 3.14t needs no build arg besides `PYTHON` (p4) |
| §3 D6: experimental; not the default until cp314t wheels; never fork turbovec | Tasks 6, 9, 11, 12 | `ARG PYTHON=3.12` is unchanged, and `pyproject.toml` and `uv.lock` are untouched. ADR 0006's rule has a **Hold** outcome for a win without a cp314t wheel, and Task 12 reads the wheels from PyPI. Task 11 forbids a patch, fork, vendored copy or other pin of turbovec: a failing sdist may change only builder-stage lines of the allowed shapes (`P4_FIX_LINE`), and the p4 test also refuses `git clone`, `git apply`, `patch -p`, `cargo vendor` and a `COPY` or `ADD` of turbovec in the Dockerfile |
| §3.1 G1: one DGX measurement (ABAB) session runs for up to 90 minutes | Tasks 10, 12; Global Constraints | Task 12 Step 4 passes `90m` to `g-abab.sh`, which hands it to every `g-session.sh`, which runs the session under `timeout`; Step 1's window text states the 90 min cap and the eight sessions' 12-hour worst case (8 × 90 min, the c=16 sessions included), plus 90 min each for C1 and D1; `exit 124` reads as the cap in Step 5 |
| §3.1 G2: a failing turbovec sdist is fixed in the builder stage with flags and build dependencies only, never a fork; a source change is drafted upstream and G parks | Tasks 9, 11; Global Constraints | Task 11 Step 6's 6a–6g: the cause table (a cause 1–3 that no allowed line can express stops and asks, and is never parked, spec §3.1 G3), at most 3 attempts in `p4-attempts.md`, `test_the_p4_turbovec_build_fix_stays_in_the_builder_stage` per fix, the draft in `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md` (never posted by the implementer, D7), and the Park in ADR 0006's Verification. ADR 0006's Decisions row and **Park** outcome state the rule (Task 9) |
| §3.1 G3: 3 failed builder-stage attempts, or a fix no allowed Dockerfile line can express although turbovec's source needs no change, stop G and ask the maintainer; ADR 0006 stays Pending; only G2's source-change case parks without asking | Tasks 9, 11; Architecture; Global Constraints | The Architecture's native-code bullet, the Global Constraints' G2 summary, Task 11's intro, Step 1's window text, the opening of Step 6's failure procedure, 6b's cause 4 row and the paragraph after the table, 6c's bound, 6g and Step 10; ADR 0006's `Pending:` text records only the source-cause Park (Task 9). Both stops write their `next: stop: …` line, leave ADR 0006 `Pending:`, run no Task 12, and ask the user; only 6e–6f (cause 4) park, without asking. A hung deploy or build ends the same way through 6g, but it is not one of G3's cases: its entry is not a `## p4 build` entry, so it uses none of the 3 attempts |
| §3.1 G4: build and fix windows (Task 11) have no time cap; the 90 min cap is for measurement sessions only | Task 11; Global Constraints | Task 11's intro and Step 1 ask for the go-ahead with no cap, as spec §6 exempts build and fix windows from its cap rule; the `p4-attempts.md` entry has no cap-based `next:` option, and 6c, 6d, 6g and Step 10 have no cap check. Besides the stops its steps name for failures, two early stops remain, and neither is a time cap: Step 1's stop recipe, on the user's request for gn100 back, kept because gn100 is shared; and the stall guard of Steps 4 and 6, which stops a deploy or build only after 30 minutes without log output, uses none of the 3 attempts, and asks the user (6g). A build whose log keeps growing runs as long as it needs. Only Task 12's `g-abab.sh` / `g-session.sh` take the `90m` cap (G1) |
| §3.1 G5: a `Hybrid QPS concurrent` win counts only when the hybrid p99 under concurrency is present and not `worse` in either cross table (A1 → B1, A2 → B2); CPU per query is recorded for every run and classifies each flat run's fast/slow state, so an uneven split cannot fake a `QPS concurrent` win; CPU per query is reported, not gated | Tasks 9, 10, 12 | ADR 0006's rule text; `HYBRID_P99`, `HIGH_CPU`, `_flat_cpu`, `uneven_states` and `gate_rows` in `t12-section.py`; `g-render.py`'s `flat_cpu` json block. `g-check.py`'s `hybrid-p99-worse` (Park, the p99 named as a cost), `flat-split-uneven` (Park), `flat-split-uneven-worse` (Hold, spec §3.1 G9), `flat-split-even` (Hold) and `cpu-missing` (Park) scenarios. Run only against a stand-in `compare.py` (see Validated in the sandbox) |
| §3.1 G7: a c=16 session after each ABAB arm session, on the same boot and image, with its own 90-minute cap; its `(c=16)` pair tables are reported in ADR 0006, not gated; C1 and D1 run at c=8 only | Tasks 10, 12; Global Constraints | `g-abab.sh`'s eight-session chain and `CONCURRENCY=16` for the `-c16` labels, `g-session.sh`'s pass-through, `g-render.py`'s four `(c=16)` `PAIRS`, `abab_decision` dropping every title that ends in ` (c=16)`; `g-check.py`'s `c16-not-gated` scenario (c=16 tables worse, decision unchanged); the Task 12 test's Regime asserts over the `(c=16)` tables; G8: `g-render.py`'s `gil_ran` gives every `-c16` session a `GIL and logs` row, `abab_decision` gates it, and `g-check.py`'s `c16-traceback` scenario parks; Step 1's window text counts eight sessions |
| §3.1 G8: the c=16 sessions' GIL and log checks gate like the c=8 sessions'; `GIL and logs` has a row for every session, the c=16 ones included; a Traceback or GIL line in any session, or a 3.14t `/healthz` answer without `gil_enabled` false, parks B; only the c=16 speed tables are not gated | Tasks 9, 10, 12; Global Constraints | ADR 0006's rule text; `g-render.py`'s `gil_ran`, which gives every `-c16` session a row; `abab_decision`'s `_gil_rows` loop, which gates all eight; `synth`'s `traceback` knob (`True` or one session label); `g-check.py`'s `c16-traceback` scenario (Park); the Task 12 test's eight A/B arm rows |
| §3.1 G9: an uneven or unknown fast/slow split fakes neither a win nor a loss; `QPS concurrent` and `p95 under concurrency (ms)` count as neither a win nor a miss then | Tasks 9, 10, 12; Global Constraints | ADR 0006's rule text; `FLAT_CONCURRENT` and `worse_in_both` in `t12-section.py`, which skip both rows when `uneven_states` is non-empty, called by `abab_decision`, the Task 12 test and `g-decide.py`; `synth`'s `lat_c` knob; `g-check.py`'s `flat-split-uneven-worse` scenario (Hold, no cost) |
| §3.1 G6: the optional C1 arm is 3.14 with the GIL, turbovec built locally from the same sdist with maturin as an abi3 wheel (3.14t builds `cp314t`); it separates the 3.12 → 3.14 interpreter effect and a local build from PyPI's wheel, and is not B's turbovec build | Tasks 6, 9, 10, 11, 12 | Prose and fixtures, no test: ADR 0006's Decisions row for `PYTHON=3.14t` and its "Recorded, not gated" C1 (Task 9); Task 6's Interfaces bullet, test comment and Dockerfile comment; `g-check.py`'s C image line and Task 11 Step 7's expected C line, both `turbovec 1.0.0 (cp39-abi3-linux_aarch64)`; the C arm in Task 11 Step 1's prompt and C1 in Task 12 Step 1's. C1 stays recorded, not gated |
| §3 D11: a non-blocking 3.14t CI job with the GIL guard | Task 7 | `test_ci_runs_the_suite_on_314t_without_blocking_the_workflow`, `test_ci_314t_job_turns_a_gil_re_enable_into_a_failure` |
| §4.1: `/healthz` keys additive | Task 1 | `test_healthz_reports_whether_the_gil_is_enabled` checks that `status`, `resident_collections` and `bm25` stay |
| §4.1: `bench/dgx/*` is Plan A's; others add only `bench/<name>_probe.py` | Tasks 8, 10 | G adds `bench/unicode_drift_probe.py`. The DGX helpers live outside the repo and call Plan A's scripts unchanged |
| §4.1: the Dockerfile and `tests.yml` are shared | Tasks 6, 7, 11 (p4 fix only) | Only the quoted lines are added: the `freethreaded` job, and 12 Dockerfile lines in the sandbox. Only if probe p4 needs it, Task 11 adds builder-stage lines, and `test_the_p4_turbovec_build_fix_stays_in_the_builder_stage` keeps them in the builder stage before turbovec's build (spec §3.1 G2) |
| §4.1: G adds nothing to the default lock | all | In the sandbox, the 14 changed files include neither `pyproject.toml` nor `uv.lock` |
| §4.2: tests in their own module | Tasks 1–9, 11 (p4 fix only), 12 | Everything is in `tests/test_freethreading.py` |
| §4.3: single writer, per-thread read connections | Task 4 | Task 4 changes only the statement cache of the two meta.db connection sites. Both full suites stay green |
| §6: the DGX protocol | Tasks 11, 12 | Deploy via Plan A's `deploy.sh`; `--limit 2549619`, fresh 4 GiB containers and the discarded run0 via Plan A's `session.sh`; `setsid -f` plus polling; the noise band via `compare.py`; seed 7 for the fan-out probe; no cold-start claim; each window asked for first, Task 12's with a time cap and Task 11's build and fix window without one (spec §6's exception for build and fix windows, §3.1 G4); each ABAB session capped at 90 min (spec §3.1 G1); `bench-tv` only, no `sudo`, `~/turborag` read only through the corpus symlink; stop recipes find the PIDs first, leave out their own shell, and kill by PID, never with a self-matching `pkill -f`. Every table's Regime lines give the regime, memory cap, `sqlite_version` and `openblas_num_threads` (Plan A's `compare.py`), and the Task 12 test requires them |
| §7 G: the full suite green on 3.14t with the GIL guard | Tasks 3–9, 12 | From Task 3 on, every task's Step 4 runs the full suite on 3.14t with `PYTHONWARNINGS` set to the guard (sandbox: 303 passed, 2 skipped after Task 12; 304 if Task 11 added the probe p4 test) |
| §7 G: a QPS/latency ABAB on one boot | Tasks 11, 12 | `g-abab.sh` records the boot id before and after. The Task 12 test requires them equal, and `g-check.py`'s `two-boots` scenario shows it failing |
| §7 G: the report states whether to continue; the default is out of scope | Tasks 9, 12 | The Decision names exactly one of Continue, Hold or Park, the one `abab_decision` computes. ADR 0006 puts the default and `GC_FREEZE`'s default out of scope |

### Placeholder scan

`grep -nE "TBD|TODO|FIXME|similar to Task|fill in" docs/superpowers/plans/2026-09-28-free-threaded-python.md` matches one line: this sentence, which quotes the pattern. The build script asserts that no `@@` marker is left unfilled and that every code fence is closed. The blocks that contain fences of their own use four backticks: ADR 0006, `t12-section.py`, `g-render.py` and `g-decide.py`, and in Task 11 Step 6 the `p4-attempts.md` entry, the upstream draft and the Park text.

Angle-bracket tokens do appear, but only in these roles:
- values that a command prints at run time and a later step reads back: `<SHA>`, `<ts>`, `<n>`, `<N>`, `<patch>`, `<uuid>`, `<label>`, `<what>`, `<reason>`, `<T>`, `<D>`, `<a>`, `<b>`, `<A's>`, `<B's>`, `<user>`, `<last line>`, `<status>`, and the process id `<pid>` in Task 11 Step 1's stop recipe output;
- the UTC time `<UTC time>` in `hung-<UTC time>.log`, which Task 11 Step 6's "If a deploy or build hung" computes (`date -u +%Y%m%dT%H%MZ`) only if the stall guard fires;
- the fields of a line format that a script prints, in the Interfaces lists: `<arm>`, `<image>`, `<version>`, `<wheel tags>`, `<compiled file>`, `<keys>`, `<engine>`, `<container id>`, `<pid>`;
- usage lines of the scripts themselves;
- `compare.py`'s table header;
- Task 11 Step 6's probe p4 failure path (spec §3.1 G2), which runs only if turbovec's sdist does not build, so its values exist only then:
  - the build and attempt number `<k>` (6a computes it from `p4-attempts.md`), and `<i>`, `<short SHA>`;
  - the slots of the fix's line shapes, which 6b's classification of the build log fills: `<packages>`, `<version>`, `<NAME>`, `<value>`, `<name>`, `<lines>`, `<lines or nothing>`;
  - the slots of the `p4-attempts.md` entry, the upstream draft and the Park text, filled from the build log and the entries: `<none, or each line in backticks>`, `<the STEP line>`, the error excerpt, the cause, `<YYYY-MM-DD>`, `<0 to 3>`, the versions, the suspected cause. 6c.1's `P4_BUILDER_FIX` shows an example line, marked as one, in place of a slot.

Every step that needs one of these values computes it (Task 11 Step 2 writes `g.env`) or reads it from the previous step's output. The two optional arms are sentences in brackets in the Task 12 prompt, `[If WITH_C1=1:]`, and are kept only if their flag is 1.

### Type and name consistency

| Name | Defined | Used by |
|---|---|---|
| `_gil_enabled() -> bool` | Task 1, `app.py` | `/healthz`, on every request |
| `/healthz` `"gil_enabled"` | Task 1 | `check_image.exercise` (Task 5), `g-session.sh`'s samples, `g-render.py`'s `GIL and logs` table, the Task 12 test |
| `Settings.gc_freeze: bool`, from `GC_FREEZE` `0`/`1` | Task 2, `config.py` | the lifespan in `app.py`; image D (`ENV GC_FREEZE=1`, Task 10's `g-build.sh`) |
| `SQLITE_CACHED_STATEMENTS` | Task 4, `store.py` | `open_meta_db`, `Collection._rdb`; ADR 0006 |
| `GIL_WARNING_FILTER`, `check_flavour`, `extension_modules`, `import_all`, `exercise`, `main`; CLI `--python`, `--require-native` | Task 5, `docker/check_image.py` | the builder step (Task 6), the CI job (Task 7), Task 11's build log |
| `built_for_this_interpreter(path, suffixes=EXTENSION_SUFFIXES) -> bool`, `check_native() -> None` | Task 5, `docker/check_image.py` | `main()` after `import_all(["raggio_native"])`; `test_check_image_finds_every_compiled_module_it_can_import`; Task 11 Step 6's failure reading (`raggio_native is not built for this interpreter (...)`) |
| `ENV OPENBLAS_NUM_THREADS=1` (Plan A), `ENV MALLOC_TRIM_THRESHOLD_=134217728` (Plan C), `ENV PYO3_PYTHON=/usr/local/bin/python3` and `uv python find "${PYTHON}"` (Plan E) | the base, checked by Task 6 Step 1 | Task 6's Dockerfile tests; `g-image-facts.py`'s `OPENBLAS_NUM_THREADS` field |
| `ARG PYTHON`, `ARG UV_NO_BINARY_PACKAGE`, `ENV PYTHONWARNINGS` | Task 6, `Dockerfile` | `g-build.sh` (images B, C and D) |
| the `freethreaded` job | Task 7, `tests.yml` | its two tests |
| `bench/unicode_drift_probe.py dump`, `diff A B`, `scan DRIFT --limit N` | Task 8 | Task 8's laptop drift, `g-facts.sh` |
| ADR 0006 `## Verification` / `Pending:` | Task 9 | `g-decide.py`, the Task 12 test; Task 11 Step 6's Park (6f), only on a turbovec source cause |
| `GATE_BETTER = ("QPS concurrent", "Hybrid QPS concurrent")` (a tuple of the two gate rows), `GATE_NOT_WORSE`, `FLAT_CONCURRENT = ("QPS concurrent", "p95 under concurrency (ms)")`, `SQLITE_BOUND` (the seven hybrid timing labels), `CROSS_PAIRS = ("A1 → B1", "A2 → B2")`, `SAME_ARM_PAIRS = ("A1 → A2", "B1 → B2")`, `FREE_THREADED_ARMS`, `OUTCOMES`, `HYBRID_P99 = "Hybrid p99 under concurrency (ms)"` (G5's guard row), `HIGH_CPU = 1.5` (a flat run is high-CPU at >= 1.5x the lowest CPU per query in its cross pair), `C16 = " (c=16)"` (G7's title suffix) | Task 10's `t12-section.py`, appended in Task 12 | `gate_rows`, `abab_decision`, `g-decide.py`, `g-check.py`, the Task 12 test |
| `abab_sections`, `worse_in_both`, `_flat_cpu(section) -> {"baseline"|"candidate": [ms|None]}`, `uneven_states(sections) -> [reason]`, `_regimes`, `_differ`, `sqlite_versions(sections) -> (str, str)`, `gate_rows(sections) -> (wins, misses)`, `abab_decision(sections) -> (outcome, reasons)` | Task 10's `t12-section.py`, appended in Task 12 | `g-decide.py` (imports `SQLITE_BOUND`, `abab_decision`, `abab_sections`, `gate_rows`, `sqlite_versions`, `worse_in_both`), `g-check.py`, the Task 12 test |
| `synth(home, qps, hybrid, lat_p50, hit, a2_qps, b_sqlite="3.53.1", b_blas="1", flat_runs=3, ..., hybrid_c_p99=1.0, slow=None, cpu=True, c16=None, lat_c=1.0)`, `JITTER = (-0.01, 0.0, 0.01)`, `REASONS` | Task 10's `g-check.py` | its twenty-three scenarios |
| the image line `Python …, GIL …, turbovec … (<wheel tags>), raggio_native <version> (<compiled file>), SQLite …, OPENBLAS_NUM_THREADS …, Unicode …, raggio_native Unicode …` | Task 10's `g-image-facts.py` | `g-facts.sh` (`g-images.txt`), `g-render.py`'s `Images`, `g-check.py`'s synthetic lines, Task 11 Step 7, the Task 12 test's `raggio_native \S+ \(([^)]*)\)` regex |
| `built <image> in <N> s` | Task 10's `g-build.sh` | Task 11 Step 6 (p4's build time) |
| image tags `localhost/raggio:<sha>`, `-py314t`, `-py314`, `-py314t-gcfreeze` | `deploy.sh` (A); Task 10's `g-build.sh` | `g-facts.sh`, `g-fanout.sh`, `g-abab.sh`, Tasks 11 and 12 |
| session labels `g-a1-py312`, `g-a1-py312-c16`, `g-b1-py314t`, `g-b1-py314t-c16`, `g-a2-py312`, `g-a2-py312-c16`, `g-b2-py314t`, `g-b2-py314t-c16`, `g-c1-py314`, `g-d1-py314t-gcfreeze` | Task 10's `g-abab.sh` | `g-render.py`'s `SESSIONS` and `PAIRS`; Task 12's move-aside glob `g-[abcd][12]-*` |
| Verification subsections `Images`, `GIL and logs`, `A1 → B1`, `A2 → B2`, `A1 → A2`, `B1 → B2`, their four `(c=16)` twins, `IVF fan-out`, `Unicode drift`, `Upstream wheels`, `Decision` | `g-render.py`, `g-decide.py` | `abab_sections` and the Task 12 test |
| status lines `== BUILD_*`, `== FACTS_*`, `== FANOUT_*`, `g-session <label>: exit <status>`, `g-abab: done` | Task 10's scripts | the polls in Tasks 11 and 12 |
| `g.env`: `SHA`, `WITH_C1`, `WITH_D1` | Task 11 Step 2 | every later DGX step |
| `p4-attempts.md` (one `## p4 build <k>` entry per failed build), `g-build-p4-<k>.log` | Task 11 Step 6 (6a), only if turbovec's sdist does not build | 6b–6g, Step 10's report, the upstream draft, the Park text |
| `deploy.stall`, `g-build.stall` (one line: the log's size in bytes, and the epoch second that size was first seen), `STALLED: no log output for 30 min` | Task 11 Steps 4 and 6, the stall guard's launch and polls | Step 4's and Step 6's poll stops |
| `hung-<UTC time>.log`, the `## p4 hung build, not counted` entry with `next: stop: build hung (no log output for 30 min)` | Task 11 Step 6, "If a deploy or build hung", only if the stall guard fires | 6g, Step 10's report; 6a's count and 6c's bound skip it |
| `P4_BUILDER_FIX`, `P4_FIX_LINE`, `test_the_p4_turbovec_build_fix_stays_in_the_builder_stage` | Task 11 Step 6 (6c.1), only if probe p4 needs a builder-stage fix | the Dockerfile's builder stage (6c.3); Task 12 Steps 10 and 12's counts |
| `D:/DEV/_scratch/upstream-drafts/turbovec-cp314t-build.md` | Task 11 Step 6 (6e), only for a turbovec source cause | the maintainer, who posts it (spec D7); Step 10's report |

Commit subjects are unique:
1. `Report gil_enabled in /healthz`
2. `Add the GC_FREEZE knob`
3. `Keep the 3.14t venv out of git and guard the GIL`
4. `Close meta.db connections for real on free-threaded builds`
5. `Add the build-time image check`
6. `Build the 3.14t image and fail on a GIL re-enable`
7. `Add the non-blocking 3.14t CI job`
8. `Add the Unicode drift probe`
9. `Document the free-threaded image (ADR 0006)`
10. `Fix the turbovec cp314t build in the builder stage (p4, attempt <k>)`, with `<k>` 1 to 3, so each attempt's subject is unique
11. `Park the free-threaded evaluation at probe p4 (ADR 0006)`
12. `Record the DGX ABAB in ADR 0006`

Task 10 commits nothing. Task 11 commits only on probe p4's failure path (spec §3.1 G2): one commit per builder-stage fix attempt (subject 10), or the Park (subject 11), after which Task 12 does not run.

### Validated in the sandbox

The sandbox base is 19a8a2e with the sandbox branches of Plans A, B, E and F merged; C and D are absent. It is in `D:/DEV/_scratch/plan-sandbox/plan-G-v2`, with the tasks applied cumulatively, one commit each. The laptop is Windows x86_64: CPython 3.12.14 in `.venv`, and 3.14.7t in `.venv-ft`, uv-managed.

**This section predates the D15/D16/p4 revision.** Everything below was validated on the plan as it stood before the DGX probes p2 and p3 and spec D15 and D16. The revision has not been run anywhere. It added:
- `built_for_this_interpreter` and `check_native` (Task 5), and their asserts in an existing test;
- Task 6's Step 1 base check, and its asserts on A's `OPENBLAS_NUM_THREADS`, C's `MALLOC_TRIM_THRESHOLD_` and E's `uv python find`/`PYO3_PYTHON` lines. On the sandbox base, which lacks Plan C, the `MALLOC_TRIM_THRESHOLD_` assert would fail, and Step 1 would stop;
- ADR 0006's new needles and rule text (Task 9);
- `t12-section.py`'s two gate rows, `SQLITE_BOUND`, `gate_rows`, `sqlite_versions`, the Regime and image-line asserts, and `g-check.py`'s six new scenarios (sixteen in all; the later G5 revision adds `hybrid-p99-worse`, `flat-split-uneven`, `flat-split-even` and `cpu-missing`, the G7 revision `c16-not-gated`, the G9 revision `flat-split-uneven-worse`, and the G8 revision `c16-traceback`, for twenty-three), its deterministic `JITTER`, and the `regime.json`/`image.json` it writes (Task 10);
- `g-image-facts.py`'s `raggio_native`, SQLite and `OPENBLAS_NUM_THREADS` fields, and `g-build.sh`'s build-time line.

Every code block in Tasks 1–9, and the Task 12 test section, was taken verbatim from the sandbox commits by the build script before this revision. The revised blocks were edited in this file by hand. The RED/GREEN counts below keep their values: the revision adds asserts to existing tests and no new test functions, but that the counts still hold is unverified.

- **RED/GREEN, `tests/test_freethreading.py`.** Each task's new tests failed before the task and passed after it, on both interpreters:

  | Task | 3.12 RED | 3.12 GREEN | 3.14t RED | 3.14t GREEN |
  |---|---|---|---|---|
  | 1 | 2 failed | 2 passed | – | – |
  | 2 | 4 failed, 4 passed | 8 passed | – | – |
  | 3 | 1 failed, 9 passed, 1 skipped | 10 passed, 1 skipped | 1 failed, 10 passed | 11 passed |
  | 4 | none (11 passed, 1 skipped) | 11 passed, 1 skipped | 1 failed, 11 passed | 12 passed |
  | 5 | 5 failed, 11 passed, 4 skipped | 16 passed, 4 skipped | 8 failed, 12 passed | 20 passed |
  | 6 | 3 failed, 16 passed, 4 skipped | 19 passed, 4 skipped | 3 failed, 20 passed | 23 passed |
  | 7 | 2 failed, 19 passed, 4 skipped | 21 passed, 4 skipped | 2 failed, 23 passed | 25 passed |
  | 8 | 3 failed, 21 passed, 4 skipped | 24 passed, 4 skipped | 3 failed, 25 passed | 28 passed |
  | 9 | 1 failed, 24 passed, 4 skipped | 25 passed, 4 skipped | 1 failed, 28 passed | 29 passed |
  | 12 | 1 failed (`-k adr_0006`) | 26 passed, 4 skipped | – | 30 passed |

  The failure messages were:
  - Task 1: `KeyError: 'gil_enabled'`;
  - Task 2: an `AttributeError` for `gc_freeze`;
  - Task 3: the venv test;
  - Task 4: `['meta.db', 'meta.db-shm', 'meta.db-wal'] == ['meta.db']`, on 3.14t only;
  - Task 5: `can't open file` / `FileNotFoundError`;
  - Task 6: `ValueError: substring not found`, the `ENV PYTHONWARNINGS` assert, and `assert None`;
  - Task 7: `KeyError: 'freethreaded'`;
  - Tasks 8 and 9: `FileNotFoundError`;
  - Task 12: `AssertionError: Task 12 records the DGX ABAB`.
- **Full suites after each task.**
  - 3.12: 284, 290, 292 + 1 skipped, 293 + 1, then 298, 301, 303, 306, 307 and 308, each with 4 skipped.
  - 3.14t, with the guard: after Task 3, 283 passed, 1 failed, 2 skipped. The failure was Plan B's delete test on Windows, which Task 4 fixes. Then 285, 293, 296, 298, 301, 302 and 303 passed, each with 2 skipped.
- **Review Focus pins.** Each pin passes on the task's code and fails on its mutation:
  - `/healthz` computed once;
  - `self._cycle = self` in `Collection.__init__`;
  - `if False:` in `import_all`;
  - `dump` without `_fold`;
  - `if False:` in place of the `Cn` test in `diff`;
  - Task 4's and Task 6's red runs.
- **The GIL guard.** Its red runs went through `uv run`, with `-X gil=1` and with `PYTHON_GIL=0`.
- **Plan B's stress tests on 3.14t.** `mixed_workload` and `stop_under_load` gave 2 passed in 69 s.
- **The uv flow.** Syncing `.venv-ft` took 18 s warm, and the version check printed `3.14.7 True False`. uv's own `.gitignore` inside the venv was observed.
- **`check_image`.** It ran on both flavours and printed the lines shown in Task 5.
- **Docs.** `mkdocs build --strict` passes after Tasks 9 and 12.
- **Local podman images** (x86_64), in the sandbox:
  - the 3.12, 3.14t and 3.14-sdist builds all pass `check_image`;
  - `Building turbovec==1.0.0` appears in both sdist builds, and not in the 3.12 build;
  - the D image has `GC_FREEZE=1`;
  - Plan F's fan-out probe runs inside the images.
- **`g-facts.sh`, run for real against the local images,** in the line format before the revision (without the `raggio_native` file, SQLite and `OPENBLAS_NUM_THREADS` fields):
  - A: `Python 3.12.14, GIL enabled, turbovec 1.0.0 (cp39-abi3-manylinux_2_28_x86_64), Unicode 15.0.0, raggio_native Unicode 15.0.0`;
  - B: `Python 3.14.7t, GIL disabled, turbovec 1.0.0 (cp314-cp314t-linux_x86_64), Unicode 16.0.0, raggio_native Unicode 16.0.0`.
- **The laptop drift (Task 8).** `15.0.0 -> 16.0.0`: 5,812 newly assigned, 1 changed, 5,053 with token drift.
- **Task 10's pipeline, before the revision.** `g-check.py` was red without the scripts, then green over the ten scenarios it had then, in about 20 s. `bash -n` passes on the six shell scripts. The sixteen-, twenty-, twenty-one-, twenty-two- and twenty-three-scenario versions have not run through `g-check.py`; Task 10 Step 4 expects about 50 s.
- **Dry runs of the shell scripts,** in a Linux container with stub sessions:
  - `g-abab.sh`: all four sessions, all six, a failing B2 (the chain stops), and `ADOPT` going to the first session only;
  - `g-build.sh`, `g-facts.sh` and `g-fanout.sh`, with stubs;
  - Task 11's stop recipe, as revised to stop a running `deploy.sh` or `setup.sh` too, for the stall guard. Its gn100 part ran, as ssh runs it (`bash -c`), in a Debian trixie container with procps and stubs for `podman`, `uv` and `rustc`, against two running `setup.sh` (one under a `bash -c` wrapper, as ssh starts it), a `setsid -f` `g-build.sh`, and two decoys, a `bench-wv` stand-in and the user's own `podman build -t localhost/mine`. It stopped both `setup.sh` trees and `g-build.sh`'s process group, printed `gn100: none left`, and left both decoys running. A second run added orphaned `uv sync --frozen` and `rustc --crate-name` stand-ins and another account's `podman build -t localhost/raggio:zzz`: the last `pgrep` listed all three instead of `gn100: none left`, and signalled none. Its laptop part ran in Git Bash against a stand-in `bench/dgx/deploy.sh`, in both phases. At the setup `ssh`, it stopped the background command, `deploy.sh`, the stand-in `ssh` and its child; at the unpack, also the pipeline's subshell and a native Windows stand-in for `git archive`. Both printed no `laptop: left` line. The first revision, which stopped only `deploy.sh` and the command running it, left the unpack's pair running and also printed no `laptop: left` line;
  - the stall guard's state logic, against a local log: 0 min at launch, the clock reset when the log grew, `no growth for 29 min` without a stop, `STALLED: no log output for 30 min` at 30 min, and a reset on the next growth. "If a deploy or build hung"'s record step also ran, with `STEP=4`: it added the entry, and 6a's count skipped it;
  - Task 12's stop recipe, with the real `g-abab.sh` and `g-session.sh`.

### Not validatable in the sandbox, and why

- **Everything measured on gn100:**
  - spec §8's probe p4 on G's tree: whether `raggio_native` builds for cp314t on aarch64, whether G's real `check_image.py` passes on G's real Dockerfile, and how long B's build takes with `raggio_native`. Task 11 Step 6 runs it first. Probe p4 itself ran on gn100 on 2026-09-28, 19:00–20:10Z, at 19a8a2e, without `raggio_native` and with a stand-in check (`docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`): turbovec 1.0.0's sdist built as `cp314-cp314t-linux_aarch64` with no build arg and 0 fixes, the 3.14t image took 28.7 s [28.2..29.3] cold over 3 `--no-cache` builds (the 3.14 image 10.6 s), and the GIL stayed off at import, under pytest (46 passed, 1 warning, on every interpreter), under uvicorn load and in the race runs;
  - the SQLite version of G's own images. p4 read SQLite 3.53.1 in uv 0.12.19's PBS 3.12.14, 3.14.7 and 3.14.7t, in its probe images; Task 11 Step 7 reads G's. p2's figures were measured on PBS SQLite 3.50.4 and not re-measured on 3.53.1. If A's and B's versions differ, `Hybrid QPS concurrent` cannot carry a win (D15), and only `QPS concurrent` can;
  - the ABAB numbers and the decision;
  - the fan-out gate in both images;
  - the aarch64 `raggio_native` file names, and the wheel tags in G's images (p4's were turbovec `cp314-cp314t-linux_aarch64` on 3.14t and PyPI's `cp39-abi3-manylinux_2_28_aarch64` on 3.12);
  - whether aarch64 gives the laptop's drift.

  This plan's rules forbid touching the DGX outside Tasks 11 and 12's user-granted windows. `g-check.py` exercises the render, decide and test chain on synthetic sessions only.
- **The D15/D16/p4 revision, not executed.** This revision was written without running any code, so none of the following has been run on either interpreter:
  - Review Focus item 3's new pins and mutations (`SQLITE_BOUND = ()`, dropping the `openblas_num_threads` assert, `built_for_this_interpreter`);
  - the six new `g-check.py` scenarios (`sqlite-bound`, `hybrid-win`, `hybrid-other-sqlite`, `noisy`, `blas-differs`, `thin`);
  - the new asserts in Tasks 5, 6 and 12;
  - the new `g-image-facts.py` fields.
- **The G5 revision, run only against a stand-in.** It added `HYBRID_P99`, `HIGH_CPU`, `_flat_cpu`, `uneven_states` and their checks in `gate_rows`, `g-render.py`'s `flat_cpu` json block, and the scenarios `hybrid-p99-worse`, `flat-split-uneven`, `flat-split-even` and `cpu-missing` with `REASONS`. `bench/dgx/compare.py` is Plan A's and not in the repo yet, so the decision code ran on synthetic sessions against a stand-in `compare.py`, both with a min..max overlap verdict and with Plan A's rule (median gain beyond the larger max − min band). Under both, the sixteen scenarios that expect a decision gave the decisions, costs and reason texts of Task 10 Step 4. `g-check.py` itself, `g-decide.py` and the Task 12 test did not run, so the four scenarios that must fail that test are unchecked.

  The first task that runs each one is its check. Where a count or message differs from this plan, stop and fix the plan's code before going on.
- **The spec §3.1 G1–G4 revision, not executed.** It was written without running anything:
  - G1: the 90-min per-session cap (`g-abab.sh <SHA> 90m`, the `e.g. 90m` usage lines, Task 12's window text and its 6-hour worst case). Only the value changed; the scripts pass the cap through as before;
  - G2: Task 11 Step 6's failure procedure (6a–6g), `P4_BUILDER_FIX`, `P4_FIX_LINE` and `test_the_p4_turbovec_build_fix_stays_in_the_builder_stage`, the uv `--help` check, Step 6's and 6a's `grep` lines (the `[2/3] STEP` prefix is podman's multi-stage format, as Plan A quotes it; no failing p4 log has been grepped), the upstream draft and its repro Dockerfile (never built on its own), the Park text, and ADR 0006's new G2 sentences (Task 9). The p4 test exists only if probe p4 needs a fix, so its counts (RED `1 failed, 25 passed, 4 skipped` and `1 failed, 29 passed`; GREEN `26 passed, 4 skipped` and `30 passed`; full suites one more) are expectations;
  - G3 and G4: prose only. G3 names the two stop-and-ask cases and leaves ADR 0006 `Pending:`; G4 removed Task 11's time cap, its 30-min attempt rule and the `stop: under 30 min of the cap left` option, and turned Step 1's stop recipe into a stop on the user's request. No script or test changed: `g-build.sh`, `g-facts.sh`, `g-ensure-ivf.sh` and `g-fanout.sh` never took a cap. The stall guard (Steps 4 and 6) and the revised stop recipe ran only as the dry runs above: neither has run against the real `ssh`, `podman build` or `deploy.sh` on gn100, and whether a TERMed rootless `podman build` leaves an intermediate container, or its `RUN` step's `uv` or `rustc` running, is unobserved. The rule for that stop during 6a–6g (finish the laptop side, then Step 10; a later go-ahead restarts at Step 2 from `HEAD`) has not run.
- **The probe p4 results revision, not executed.** It changed facts, comments and fixture values only, and no test or script logic: the SQLite version in `g-check.py`'s `synth` default, `image.json` and image lines (3.53.1), C's turbovec tag in `g-check.py` and Task 11 Step 7 (`cp39-abi3-linux_aarch64`), `FT_WHEEL`'s comment, Task 6's test and Dockerfile comments (the Dockerfile keeps its 12 lines), Task 11's time estimates and ADR 0006's Context, Decisions and Consequences. `g-check.py` has not run on the new values; Task 10 Step 4 runs it. Only a hand check ran: `synth` with the new values, with and without C1 and with `b_sqlite="3.51.0"`, fed the Task 12 test's image-line asserts (the `Images` regex, `abi3` for A, `cp314t` for B, the `raggio_native` files, `SQLite 3.`), and all passed; `image.json` gave 3.53.1 on every session but B's 3.51.0 in the last case.
- **The G7 revision of `g-abab.sh` and `g-session.sh`, run only over stubs.** It adds the c=16 sessions and passes `CONCURRENCY` through. `bash -n` passes on both. A dry run with a stub `lib.sh` (`CONCURRENCY` defaulting to 8), a stub `g-session.sh` and a stub `session.sh` gave:
  - `8 sessions` in the order A1 A1c16 B1 B1c16 A2 A2c16 B2 B2c16, and `10 sessions` with C1 and D1 at the end;
  - `CONCURRENCY=16` reaching `session.sh` in the `-c16` sessions and 8 in the others, and `ADOPT` going to the first session only.

  Neither Plan A's real `lib.sh` with the `CONCURRENCY` knob nor Task 12's stop recipe has run against the revised scripts. The stop recipe's patterns name neither labels nor the concurrency, and the wrapper's command line still starts `timeout --kill-after=60 <cap> bash -c`, the prefix the recipe matches.
- **The real `ssh`, `scp`, `podman` and `deploy.sh` on gn100.** Plan A's `session.sh`, `deploy.sh` and `compare.py` were used as the sandbox's Plan A branch has them. Their behaviour on gn100 (the fingerprint, `ADOPT`, `session.sh`'s exit on TERM) is Plan A's. Plan A's `session.sh` holds no lock file; G's `pgrep` check for a running `session.sh` has not run on gn100.
- **The 3.14t CI job on GitHub's runners.** Its YAML is tested structurally. Its steps (the 3.14t suite, `check_image`, the stress tests) were run on the laptop, and `check_image` also ran inside the Linux image builds. The job itself, including the `ubuntu-24.04-arm` leg, first runs on push.
- **Task 4's zombie on Linux.** It was observed on Windows, where the open files block the delete. On Linux the same zombie keeps `-wal` and `-shm` in place. The test checks for those files, so it should be red there too, but that is unverified until the CI job runs.
- **Interplay with Plans C and D.** Neither is merged anywhere. The next section lists what this plan expects them to leave in place.
- **The optional arms C1 and D1.** Their images were built locally, but they have never run a session.

### Cross-plan assumptions

- **Plan A:**
  - uv 0.12.20 with `required-version`. Plan A upgraded the laptop's global uv from 0.5.24 to 0.12.20.
  - the Dockerfile layout:
    - the builder's `uv sync` into `/app/.venv`;
    - `ARG PYTHON=3.12` and `uv python install` into `/python`;
    - the `debian:trixie-slim` runtime stage.
  - the `tests.yml` jobs.
  - `bench/dgx/lib.sh`'s names.
  - `session.sh`:
    - its `IMAGE` and `ADOPT` variables;
    - its refusal of an existing label;
    - its command line, `bash <path>/bench/dgx/session.sh <label>`, which G's `pgrep` check matches (A has no lock file);
    - its run order from an IVF start, and leaving `bench-tv` running.
  - `compare.py`: `render` and `beyond_band`, the flat engine name `raggio`, and the metric labels, including Plan A Task 5's `Hybrid p99 under concurrency (ms)`, `CPU per query under concurrency (ms)` and `Hybrid CPU per query under concurrency (ms)` (all lower-is-better; the CPU rows are absent where `cpu.stat` is unreadable), `Hybrid first 10 queries, slowest (ms)` and `Hybrid p99 without the first 10 (ms)`, which `SQLITE_BOUND` names.
  - `compare.py`'s regime output (Plan A Task 4):
    - the `Regime (baseline): …` and `Regime (candidate): …` lines, with the keys `page_cache`, `first_start`, `memory`, `memory_swap`, `concurrency`, `python`, `sqlite_version`, `openblas_num_threads`, and `unknown` for a missing one;
    - `The arms differ in <keys>: …`, which leaves out `first_start`;
    - the `Reported, not claimed (spec §6 needs >= 3 runs per side; FLAT_RUNS=3): …` note, and `CLAIM_RUNS = {"qps_concurrent": 3}`.
  - `session.sh` writing `regime.json` (`page_cache`, `first_start`, `memory`, `memory_swap`, `detach_memory`, `concurrency`: `16` for the `-c16` labels, `8` otherwise) and `image.json` (`python`, `sqlite_version`, `turbovec`, `openblas_num_threads`, read inside the image) into each session directory. `g-check.py` writes both for its synthetic sessions.
  - `lib.sh`'s `FLAT_RUNS="${FLAT_RUNS:-2}"`, which `g-session.sh` overrides with `FLAT_RUNS=3`.
  - the runtime stage's `ENV OPENBLAS_NUM_THREADS=1` and its D16 comment, directly after `USER app` (Task 6 Step 1 checks it).
  - `deploy.sh`, `setup.sh` and `.deployed-sha`.
  - the fingerprint's name, `fingerprint-raggio-2549619.json`.
- **Plan B:**
  - `Collection.stop()`;
  - the stress tests `mixed_workload` and `stop_under_load`, and `RAGGIO_STRESS_SECONDS`;
  - `test_deleted_collection_leaves_no_files_and_its_name_can_be_reused`, which Task 4 turns green on 3.14t.
- **Plan C:**
  - C keeps the two meta.db `sqlite3.connect` sites (`open_meta_db`, `Collection._rdb`) as single calls that Task 4 can add `cached_statements=` to. C owns `open_meta_db`'s indexes, and Task 4 does not touch them.
  - the lifespan's `resume_pending` and `shutdown` calls stay where Task 2 anchors the freeze and the unfreeze.
  - the runtime stage's block after `EXPOSE 8000`: four comment lines and `ENV MALLOC_TRIM_THRESHOLD_=134217728`, exactly once in the Dockerfile (C's own test counts it). Task 6 Step 1 checks it, and Task 6's guard goes after `ENV LANG`, not near C's block.
- **Plan D:** D2's sync flush in `stop()` runs before the meta.db connections close (spec §4.1). Task 4 changes only the arguments those connections are opened with, so the order is kept.
- **Plan E:**
  - `/healthz`'s `bm25` key;
  - the `native` extra, and `REQUIRE_NATIVE`;
  - `raggio_native.UNIDATA_VERSION`;
  - the Rust builder stage and `ARG RUST_VERSION`;
  - the builder's `uv python find "${PYTHON}"` link to `/usr/local/bin/python3` and `ENV PYO3_PYTHON=/usr/local/bin/python3`, so that both PyO3 builds, `raggio-native` and turbovec's sdist, target the interpreter `PYTHON` names (Task 6 Step 1 checks the `ENV` line);
  - the two `RUN uv sync` lines, both with `--extra native`, and E's test that keeps `rust` and `cargo` out of the runtime stage;
  - `raggio-native` 0.1.0: maturin's module `raggio_native/raggio_native.<suffix>`, no abi3, `#[pymodule(gil_used = false)]`, and a wheel that is not manylinux-audited, so it is always built in the image, never downloaded;
  - the CI step `Install Rust (same pin as ARG RUST_VERSION in the Dockerfile)`;
  - ADR 0004.
- **Plan F:**
  - `bench/ivf_fanout_probe.py fanout --data --threads` and its `GATE` line;
  - `IVF_SEARCH_THREADS` and its env-table row, which Task 9 appends after;
  - ADR 0005, which holds the cp314t wheel request among its upstream drafts.
- **Line numbers.** Every line number quoted in this plan is for the sandbox base; the edits anchor on quoted text.
