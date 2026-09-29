# Bench and Packaging Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the benchmark harness fail loudly instead of silently re-ingesting. Commit the DGX runbook that every later plan's A/B uses, with regime labels on every row. Move packaging to uv 0.12 with orjson in a `bench` group, add a minimal CI, and migrate the Dockerfile to python-build-standalone CPython 3.12 on Debian trixie with `OPENBLAS_NUM_THREADS=1`. Run two DGX steps: an equivalence re-baseline before the migration and a new reference baseline after it.

**Architecture:**
- **Reuse decision.** `bench/bench_reuse.py` is a new stdlib + httpx module that owns the reuse-or-ingest decision.
  - An unreachable engine, a 404 the collection list does not confirm, a chunk count that doesn't match `--limit`, or a fingerprint that doesn't match all raise `BenchAbort` before anything is deleted.
  - Fingerprints are keyed by data set and limit (`fingerprint-<data>-<limit>.json`) and written atomically.
  - `wait_idle` waits for `pending_jobs == 0`.
- **bench.py wiring.** `bench/bench.py` runs a `preflight` over every engine before the ground-truth pass.
  - It gains `--adopt`, `--fingerprint-dir` and `--state-timeout` (900 s).
  - `raggio-ivf` builds its index through its own `build_index` step on the flat collection's data.
  - The committed email-corpus fingerprints are untracked.
  - It reports the concurrent hybrid p99 (`hybrid_c_p99`) and, with `--cpu-container NAME`, the server's CPU per query in both concurrent phases (`cpu_ms_per_q_c`, `hybrid_cpu_ms_per_q_c`), read from the container's cgroup `cpu.stat`. With no cgroup read, the CPU rows are absent, never 0, and one log line says why (spec §3.1 G5).
- **DGX runbook.** `bench/dgx/` holds the runbook scripts:
  - `deploy.sh` (workstation), with `unpack.sh` and `setup.sh` on the DGX side;
  - `session.sh`, one measurement session of one image in the spec §6 order;
  - `lib.sh`, the shared settings and guard rails;
  - `compare.py`, the median / max − min noise band and the A/B verdicts.
  - `session.sh` also writes `regime.json` and `image.json`, and `compare.py` prints them for each side. It flags arms whose regimes differ and marks concurrent-QPS rows with fewer than 3 runs as "reported, not claimed".
  - `CONCURRENCY` (default 8) is every run's `--concurrency`, recorded in `regime.json`, so `compare.py` flags arms that ran at different concurrency (spec §3.1 G7).
  - `IVF_BUILDS=2` makes a flat-start session build the IVF index a second time after its measured runs: the 8 GiB detach, then a build at 4 GiB (`build2-raggio-ivf`). `compare.py` counts only that run's build time, so the build row gets two values per side, a median and a band (spec §3.1 A1).
- **Packaging.**
  - orjson moves to a `bench` dependency group. The runtime stays on stdlib JSON (ADR 0001:31).
  - `[tool.uv] required-version = ">=0.12"`, and `uv.lock` is re-locked with uv 0.12.19.
  - `.github/workflows/tests.yml` checks the lock and runs the suite on 3.12 with setup-uv pinned by SHA.
- **Dockerfile.** Takes the uv binary from `ghcr.io/astral-sh/uv:0.12.19` and runs `uv python install` into `/python` on a trixie builder. The runtime is `debian:trixie-slim` (uid 1000, `LANG=C.UTF-8`) with `ENV OPENBLAS_NUM_THREADS=1`.

**Tech Stack:**
- Python 3.12, pytest, httpx (`MockTransport`), numpy.
- bash (DGX runbook: podman 4.9.3 rootless, ssh), uv 0.12.19.
- GitHub Actions: `astral-sh/setup-uv` v10.2.0 pinned by commit SHA.
- OCI Dockerfile built with podman: python-build-standalone CPython via `uv python install`, Debian trixie.

**Spec:** `docs/superpowers/specs/2026-09-28-raggio-performance-design.md`
- §2: order 0 (bench tasks, equivalence re-baseline) and order 2 (the rest, new reference baseline).
- §3: D2, D5, D11, D15, D16.
- §3.1: A1 (the IVF build gate: two builds per arm), A2 (the deploy keeps the previous bench files), G5 (the concurrent hybrid p99 and CPU per query rows) and G7 (the `CONCURRENCY` session knob).
- §4.1: ownership of `bench/bench.py`, `bench/dgx/*.sh`, `pyproject.toml` groups, `uv.lock`, `required-version`, `Dockerfile` and `tests.yml`.
- §4.2: `OPENBLAS_NUM_THREADS=1` is an image `ENV`; each plan uses its own test modules; the bench rows for G5 and the session knob for G7 land in A's PR 1.
- §5.3: bench fragility.
- §6: the DGX benchmark protocol.

**Ships as two PRs, in spec §2 order:**
- **PR 1:** Tasks 1–6 on `git switch -c bench-hardening` from `main` @ 19a8a2e.
- **PR 2:** Tasks 7–10 on `git switch -c packaging-foundation` from `main`, once PR 1 and Plan B have merged.
- **Suite counts.** Counts are given for A's tests alone: 46 on base, then 101 / 109 / 139 / 150 / 161 after Tasks 1 / 2 / 3 / 4 / 5, and 166 / 168 / 171 after Tasks 7 / 8 / 9. Plan B adds 17 tests (46 → 63), so on PR 2's branch every count is 17 higher: 183 / 185 / 188. The one Starlette deprecation warning stays throughout.

**Validation status:**
- **Sandbox.** `D:/DEV/_scratch/plan-sandbox/plan-A-final` @ cd633f5, whose base d9f2c77 equals `main` @ 19a8a2e. The replay log is `D:/DEV/_scratch/plan-sandbox/plan-A-tools/verify_final.out`, run on Windows 11 with CPython 3.12 and uv 0.12.19.
- **Reproduced from the sandbox, with exact code and commands.** Each task's RED and GREEN counts were replayed:
  - Task 1 (bench_reuse);
  - Task 2 (bench.py wiring);
  - Task 3 (DGX runbook);
  - Task 7 (packaging, apart from the dev-box uv upgrade step);
  - Task 8 (CI);
  - Task 9 (Dockerfile, apart from the D16 line and its test).
- **Also run in the sandbox:**
  - a podman build of Task 9's Dockerfile, without the D16 line;
  - a Linux CI simulation with uv 0.12.19 on an earlier revision of the test set;
  - an end-to-end `bench.py` run against a local raggio server, covering every reuse, abort, adopt, build-index and reingest path of Tasks 1–2.
- **Not executed in the sandbox, and labelled so where they appear:**
  - Tasks 4, 5, 6 and 10 in full (Task 5's two-file RED and GREEN counts were replayed on the workstation outside the sandbox, as Task 5 says);
  - Task 7 Step 3 (the dev-box uv upgrade);
  - the D16 line and its test in Task 9;
  - every suite count after Task 3, which are derived by adding the new tests to the last replayed count.

## Global Constraints

These are copied from the spec, one requirement per line:

- "Every plan ships as its own branch/PR. It must leave `uv run pytest -q` green and must not depend on a later plan." (§2)
- "Before each plan's DGX A/B, the preceding `main` is re-baselined; A/Bs compare against that, never against the step-0 numbers." (§2)
- "Harden `bench/bench.py`. Commit the DGX runbook scripts. The baseline of 19a8a2e was already captured on 2026-09-28 with a scratch-patched bench (`research/2026-09-28-dgx-baseline.md`). A's DGX step is an **equivalence re-baseline**: the hardened bench on the same image must land inside the baseline's noise band." (§2, order 0)
- "uv ≥ 0.12 + `required-version`, re-lock, orjson → `bench` group, minimal CI, Dockerfile base migration (see §4.2) plus `ENV OPENBLAS_NUM_THREADS=1` (D16). The DGX step records a **new reference baseline** for the PBS interpreter, SQLite 3.53.1 and single-threaded BLAS (D15, D16). The IVF build time and the flat vector rows must not regress beyond the band. Hybrid rows and IVF concurrent/filtered rows are expected to move and are reported." (§2, order 2)
- "One parameterized multi-stage Dockerfile. `ARG PYTHON=3.12` and `ARG UV_VERSION` pinned. uv binary from `ghcr.io/astral-sh/uv:<UV_VERSION>`. `uv python install` into `/python`. Runtime `debian:trixie-slim`. The bookworm uv tags are frozen at 0.9.30. No separate `Dockerfile.ft`. A migrates the base, E adds the Rust builder stage, G adds the 3.14t/turbovec-sdist path." (D2)
- "Move to a `bench` dependency group, no bump. Runtime JSON stays stdlib (ADR 0001:31)." (D5)
- "A minimal `tests.yml`: 3.12 running the Python-fallback suite, plus a native job once E lands. A non-blocking 3.14t job with the GIL guard is added by G." (D11)
- "A's base migration is itself a performance change, so A records a **new reference baseline** after it and does not claim equivalence on hybrid rows. Every DGX row records `sqlite3.sqlite_version`." (D15)
- "Set `ENV OPENBLAS_NUM_THREADS=1` in the image." "Owner A, as part of the base migration, so the one new reference baseline (D15) absorbs it. A's DGX step checks that the IVF build (k-means + attach) and the ingest-free rows do not regress beyond the band; p3 did not measure these. If the build regresses, the fallback is to limit BLAS threads on the search path only with threadpoolctl, still owned by A." (D16)
- "The IVF build-time gate builds **twice per arm** and compares medians, against the band. There is no single-build tolerance and no rerun rule. The threadpoolctl fallback (D16) is added only if this gate fails." (§3.1 A1)
- "The deploy may replace `~/raggio` on gn100 after moving the previous bench files to `~/raggio-bench/pre-deploy-<UTC time>/`. It never touches `bench-wv` or `~/turborag`." (§3.1 A2)
- "`bench/bench.py`, `bench/dgx/*.sh` runbook: A. Add new probe files only (`bench/<name>_probe.py`); never edit bench.py." (§4.1)
- "`pyproject.toml` groups, `uv.lock`, `required-version`: A first." (§4.1)
- "`.github/workflows/tests.yml`: A creates it. E adds the native job, G the 3.14t job." (§4.1)
- "`OPENBLAS_NUM_THREADS=1` is an image `ENV` (D16, owner A), not a `Settings` knob." (§4.2)
- "New tests: each plan adds its own module (`tests/test_<plan-topic>.py`). Plans don't reorganize existing test files." (§4.2)
- "`bench.py`'s `current_count` uses a 30 s httpx timeout. A timeout is read as "no reuse", so the bench rebuilds or re-ingests." (§5.3)
- "A reuse check that cannot reach the server must fail loudly. It must never re-ingest." (§5.3)
- "Fingerprints must be keyed by corpus/limit, or be untracked." (§5.3)
- "The bench must wait for `pending_jobs == 0` before measuring." (§5.3)
- "Access: `ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100`. `SSH_CONFIG` is the maintainer's local ssh config for gn100: set in the environment, never committed." (§6)
- "**Any DGX run over about 5 minutes needs the user's explicit go-ahead in the session, with a time cap.** A plan's DGX task is written as runnable steps, but its first step is "ask the user for a run window"." (§6)
- "`podman rm -f` may only name `bench-tv`, never `bench-wv` (the Weaviate container, whose `bench-wv` volume must be preserved)." (§6)
- "Every run starts from a fresh container of the image under test." (§6)
- "Use **`--limit 2549619`**. 2,549,119 is the ingested row count, and passing it forces a 31-min reingest and a different query set." (§6)
- "Never pass `--reingest` except in an ingest-measurement run, which comes last because it rewrites and compacts files." (§6)
- "Vector concurrent QPS is bimodal (120–225) across identical configs, so it always needs at least 3 runs." (§6)
- "Both arms of an A/B run with the same `OPENBLAS_NUM_THREADS`, and every row records it." (§6)
- "Every reported row states its page-cache regime: true-cold (fadvise-evicted, D12) or host-warm." "Every row also states its memory cap and `sqlite3.sqlite_version` (D15)." "Arms of one A/B share the same regime." (§6)
- "Report the first hybrid pass after a load separately: its first ~10 queries take 470–980 ms." (§6)
- "Any memory claim (D13) is measured with `--memory 4g --memory-swap 4g`." (§6)
- "**Cold start** is claimed only from a true-cold start (reboot or D12 fadvise eviction), never from within an A/B session." (§6)
- "launch long runs with `setsid -f` into a log file; poll the log; never `pkill -f` a pattern that matches the polling command itself; never stop or delete the user's DGX work unasked." (§6)

These are harness and repository rules:
- Commit trailer: every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests pass on Windows (the dev box, Git Bash present) and on Linux (CI). A test that needs bash is marked `needs_bash`, and a test that imports `bench.py` does `pytest.importorskip("orjson")`.
- DGX: never `sudo`. `~/turborag` and the `bench-wv` container and volume are read or left alone, never modified.
- Encoding: `bench/dgx/*.sh` are LF-only (`.gitattributes`). `compare.py`, `session.sh` and the docs contain `§` / `−` and are UTF-8.

## Review Focus

These are the five input classes most likely to slip past the plan, most likely first. Each is pinned by a test in the task that owns it.

1. **RF1: an engine that is slow or down is read as "no data".**
   - At 2.5M rows the first GET after a start takes about 128 s. The old harness read a timeout as "absent", then DELETEd the collection and re-ingested it (31 min).
   - Pins: `test_unreachable_raggio_aborts_instead_of_reporting_no_data` and `test_404_the_collection_list_does_not_confirm_aborts` (Task 1), and `test_unreachable_engine_aborts_before_ground_truth` (Task 2).
2. **RF2: `--limit 2549119` (the ingested count) instead of 2549619.**
   - It changes the query set and the expected count, and the old harness re-ingested.
   - Pins: `test_wrong_limit_aborts_with_both_counts_and_the_flag` (Task 1), `test_wrong_limit_aborts_before_ground_truth` (Task 2), and `test_guard_rails`, which forbids the string in every script (Task 3).
3. **RF3: a fingerprint from another corpus vouches for the volume.**
   - The committed email-corpus `bench/fingerprint-raggio.json` is the case, and so is `--adopt` overriding a fingerprint that is on file.
   - Pins: `test_adopt_never_overrides_a_fingerprint_on_file` and `test_unreadable_fingerprint_never_matches` (Task 1), and `test_legacy_unkeyed_fingerprint_is_not_trusted` (Task 2).
4. **RF4: an A/B across two regimes is read as a same-regime result.**
   - Examples: baseline on SQLite 3.40.1 with default BLAS against a candidate on 3.53.1 with `OPENBLAS_NUM_THREADS=1`, or a swapless arm against a default-swap arm, or 2 flat runs claiming concurrent QPS.
   - Pins: `test_compare_flags_arms_that_differ_in_regime`, `test_compare_reports_thin_concurrent_qps_as_not_claimed`, `test_session_records_the_regime_labels` and `test_image_facts_snippet_reports_sqlite_and_blas_threads` (Task 4).
5. **RF5: a later Dockerfile or lock edit (C, E, G) silently drops the image contract.**
   - At risk: the uv pin, 3.12 on trixie, uid 1000, `LANG`, the CMD, `OPENBLAS_NUM_THREADS=1`, or the lock matching the groups.
   - Pins: `test_image_keeps_the_runtime_contract`, `test_dockerfile_pins_the_ci_uv_and_defaults_to_312_on_trixie` and `test_image_runs_blas_single_threaded` (Task 9), and `test_lock_matches_the_dependency_groups` (Task 7).

---

## File Structure

| Path | Action | Task | Responsibility |
|---|---|---|---|
| `bench/bench_reuse.py` | Create | 1 | Reuse-or-ingest decision: `BenchAbort`, `EngineState`, engine state reads, `wait_idle`, keyed atomic fingerprints, `decide`, `prepare_engine`. Stdlib + httpx only. |
| `tests/test_bench_reuse.py` | Create | 1 | 55 tests of the decision, with `httpx.MockTransport` engines. |
| `bench/bench.py` | Modify | 2, 5, 7 | Task 2: `preflight` before ground truth, `build_parser()`, `main(argv=None)`, `ENGINES` `fp` / `build_index` keys, `build_ivf_index()`, the new flags, `BenchAbort` → `SystemExit`. Task 5: the first-hybrid-pass rows and the warm-cache note; `hybrid_c_p99`, and `cpu_ms_per_q_c` / `hybrid_cpu_ms_per_q_c` from the cgroup `cpu.stat` (`cpu_stat_path`, `read_usage_usec`, `cpu_phase`, `--cpu-container`), absent when unreadable. Task 7: the docstring's `--group bench`. |
| `tests/test_bench_harness.py` | Create (2), append (5) | 2, 5 | bench.py wiring: preflight order, flags, keyed fingerprints; then the Task 5 rows, the cgroup CPU reads and the absent CPU rows. |
| `.gitignore` | Modify | 2 | Ignore `bench/fingerprint-*.json` and `bench/results-partial.json`. |
| `bench/fingerprint-raggio.json`, `bench/fingerprint-weaviate.json` | Untrack | 2 | Email-corpus fingerprints: renamed locally to `*-553015.json`, never committed again. |
| `docs/benchmark.md`, `docs/benchmark-arxiv.md` | Modify | 2, 7 | The preflight, `--adopt` / `--reingest`, then `--group bench`. |
| `.gitattributes` | Create | 3 | `*.sh text eol=lf`. |
| `bench/dgx/lib.sh` | Create (3), modify (4, 5) | 3, 4, 5 | Shared settings (`LIMIT`, `CONTAINER`, `VOLUME`, `MEMORY`, …), `log`, `run`. Task 4: `FLAT_RUNS`, `MEMORY_SWAP` and `FIRST_START` become overridable, and `IVF_BUILDS` (1 or 2) is added. Task 5: `CONCURRENCY` (default 8). |
| `bench/dgx/deploy.sh`, `bench/dgx/unpack.sh`, `bench/dgx/setup.sh` | Create | 3 (setup.sh modified in 7) | Workstation → DGX deploy of `HEAD`; keep the old checkout's results; corpus link, ground truth, `uv sync`, `podman build`, `.deployed-sha`. |
| `bench/dgx/session.sh` | Create (3), modify (4, 5) | 3, 4, 5 | One session of one image in the §6 order. Task 4: `regime.json`, `image.json`, the swapless gate, the first-start label, the second IVF build (`IVF_BUILDS=2`). Task 5: `--concurrency "$CONCURRENCY" --cpu-container "$CONTAINER"` on every run, and the regime's `concurrency` label. |
| `bench/dgx/compare.py` | Create (3), modify (4, 5) | 3, 4, 5 | Median / max − min band, verdicts, `render`, `beyond_band`. Task 4: `regime()`, regime lines, the arms-differ line, the thin-QPS note, `BUILD_FILE` (a second build adds only its build time). Task 5: the five new metric rows (all lower-is-better; absent rows print `-`) and the `concurrency` regime key. |
| `tests/test_bench_dgx.py` | Create (3), append (4, 5) | 3, 4, 5 | Runbook: syntax, LF, guard rails, dry runs, unpack, compare. Task 5: the `CONCURRENCY` knob, the concurrency regime label, absent CPU rows in compare. |
| `pyproject.toml`, `uv.lock` | Modify | 7 | `bench = ["orjson>=3.12.0"]`, `[tool.uv] required-version = ">=0.12"`, re-lock with uv 0.12.19 (lock revision 3). |
| `README.md`, `docs/getting-started.md`, `bench/floor_probe.py`, `bench/prep_arxiv.py` | Modify | 7 | `--group bench` in every orjson script's run line; the uv floor note. |
| `tests/test_packaging.py` | Create (7), append (8, 9) | 7, 8, 9 | Groups, uv floor, lock, docs, CI, Dockerfile contract, D16. |
| `.github/workflows/tests.yml` | Create | 8 | Minimal CI: lock check and suite on 3.12. |
| `Dockerfile` | Modify | 9 | uv 0.12.19 binary stage, trixie builder with `uv python install`, `debian:trixie-slim` runtime, `ENV OPENBLAS_NUM_THREADS=1`. |
| gn100 `~/raggio-bench/{baseline3,a-equiv,a-main,a-ref}/` | Create (outside the repo) | 6, 10 | Session outputs: the equivalence re-baseline and the new reference baseline. |
| gn100 `~/raggio-bench/detach.sh` | Create (outside the repo) | 10 | Leaves `bench-tv` flat before a session, so both Task 10 sessions measure the IVF build. |

---

### Task 1: The reuse-or-ingest decision fails loudly (`bench/bench_reuse.py`)

Branch: `git switch -c bench-hardening` (from `main` @ 19a8a2e).

**Files:**
- Create: `bench/bench_reuse.py`
- Test: `tests/test_bench_reuse.py` (create)

**Interfaces:**
- Consumes: `httpx.AsyncClient`, and raggio's REST shapes:
  - `GET /collections/{name}` → `{"chunks", "pending_jobs", "index": {"type"}}`;
  - `GET /collections` → `{"collections": [...]}`.
  - Weaviate: `GET /v1/schema/{class}`, and `POST /v1/graphql` with an Aggregate query.
- Produces (consumed by Task 2):
  - Constants `STATE_TIMEOUT_S = 900.0`, `PENDING_POLL_S = 5.0` and `PENDING_WAIT_S = 3600.0`.
  - `class BenchAbort(RuntimeError)`.
  - `@dataclass(frozen=True) class EngineState: chunks: int | None; ivf: bool = False; pending_jobs: int = 0`.
  - `fingerprint_path(fp_dir, key: str, limit: int) -> Path` gives `<fp_dir>/fingerprint-<key>-<limit>.json`.
  - `read_fingerprint(path) -> dict | None` returns None when no file exists and `{}` when the file is unreadable. `write_fingerprint(path, fp: dict) -> None` writes a temp file and then `os.replace`.
  - `async raggio_state(client, base, coll) -> EngineState`, `async weaviate_state(client, base, wclass) -> EngineState`, and `async wait_idle(client, base, coll, *, poll_s=PENDING_POLL_S, max_wait_s=PENDING_WAIT_S) -> EngineState`.
  - `decide(name, state, expected, saved_fp, current_fp, *, reingest, adopt, limit, fp_path="fingerprint") -> str` returns `"reuse" | "build-index" | "ingest"` or raises `BenchAbort`.
  - `async prepare_engine(name, *, state, ingest, build_index=None, fp_path, expected, fp_now, reingest, adopt, limit) -> dict` returns `{}`, `{"ingest_s", "ingest_vps"}` or `{"index_build_s"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bench_reuse.py` with exactly this content:

```python
"""The bench harness's reuse-or-ingest decision (spec §5 finding 3): an engine that
doesn't answer, a chunk count that doesn't match, or a fingerprint that doesn't match
must abort the run, never fall through to the DELETE + 31-minute re-ingest path."""
import asyncio
import json

import httpx
import pytest

import sys
sys.path.insert(0, "bench")
from bench_reuse import (STATE_TIMEOUT_S, BenchAbort, EngineState, decide, fingerprint_path,
                         prepare_engine, raggio_state, read_fingerprint, wait_idle,
                         weaviate_state, write_fingerprint)

BASE = "http://engine"
FP = {"limit": 2549619, "seed": 42, "queries": 500, "text_v": "real abstracts, m @abcd1234/d1024"}
EXPECTED = 2549619 - 500


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=STATE_TIMEOUT_S)


def raggio_handler(info=None, info_status=200, names=("bench",), list_status=200):
    def handler(request):
        if request.url.path == "/collections":
            return httpx.Response(list_status, json={"collections": list(names)})
        if request.url.path == "/collections/bench":
            if info is None:
                return httpx.Response(info_status, json={"detail": "collection 'bench' not found"})
            return httpx.Response(info_status, json=info)
        return httpx.Response(500)
    return handler


def state_of(handler):
    async def go():
        async with client_for(handler) as c:
            return await raggio_state(c, BASE, "bench")
    return asyncio.run(go())


def test_state_timeout_covers_a_cold_collection_open():
    # the first GET after a container start builds the collection on the event loop
    # (128 s on the DGX at 2.5M rows); the old 30 s read that as "no data" and re-ingested
    assert STATE_TIMEOUT_S >= 900


def test_raggio_state_parses_count_index_and_pending():
    info = {"chunks": 12, "pending_jobs": 3, "index": {"type": "ivf", "nlist": 4, "nprobe": 2}}
    assert state_of(raggio_handler(info)) == EngineState(chunks=12, ivf=True, pending_jobs=3)
    flat = {"chunks": 12, "pending_jobs": 0, "index": {"type": "flat"}}
    assert state_of(raggio_handler(flat)) == EngineState(chunks=12, ivf=False, pending_jobs=0)


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
def test_unreachable_raggio_aborts_instead_of_reporting_no_data(exc):
    def handler(request):
        raise exc("engine not answering", request=request)
    with pytest.raises(BenchAbort, match="--state-timeout"):
        state_of(handler)


def test_404_confirmed_by_the_collection_list_means_absent():
    assert state_of(raggio_handler(None, 404, names=("other",))) == EngineState(chunks=None)


@pytest.mark.parametrize("names,list_status", [(("bench",), 200), ((), 401), ((), 500)])
def test_404_the_collection_list_does_not_confirm_aborts(names, list_status):
    with pytest.raises(BenchAbort, match="does not confirm"):
        state_of(raggio_handler(None, 404, names=names, list_status=list_status))


def test_error_status_aborts():
    with pytest.raises(BenchAbort, match="HTTP 503"):
        state_of(raggio_handler({"detail": "busy"}, 503))


def test_unexpected_body_aborts():
    with pytest.raises(BenchAbort, match="unexpected body"):
        state_of(raggio_handler({"name": "bench"}))


def test_wait_idle_polls_until_the_job_queue_drains():
    pending = iter([3, 1, 0])

    def handler(request):
        return httpx.Response(200, json={"chunks": 5, "pending_jobs": next(pending),
                                         "index": {"type": "flat"}})

    async def go():
        async with client_for(handler) as c:
            return await wait_idle(c, BASE, "bench", poll_s=0)
    assert asyncio.run(go()) == EngineState(chunks=5, pending_jobs=0)


def test_wait_idle_gives_up_loudly():
    def handler(request):
        return httpx.Response(200, json={"chunks": 5, "pending_jobs": 2, "index": {"type": "flat"}})

    async def go():
        async with client_for(handler) as c:
            return await wait_idle(c, BASE, "bench", poll_s=0, max_wait_s=0)
    with pytest.raises(BenchAbort, match="2 jobs still pending"):
        asyncio.run(go())


def test_wait_idle_returns_at_once_for_an_absent_collection():
    async def go():
        async with client_for(raggio_handler(None, 404, names=())) as c:
            return await wait_idle(c, BASE, "bench", poll_s=0, max_wait_s=0)
    assert asyncio.run(go()) == EngineState(chunks=None)


def weaviate_of(handler):
    async def go():
        async with client_for(handler) as c:
            return await weaviate_state(c, BASE, "Email")
    return asyncio.run(go())


def test_weaviate_state_counts_and_absent_class():
    def present(request):
        if request.url.path == "/v1/schema/Email":
            return httpx.Response(200, json={"class": "Email"})
        return httpx.Response(200, json={"data": {"Aggregate": {"Email": [{"meta": {"count": 7}}]}}})
    assert weaviate_of(present) == EngineState(chunks=7)
    assert weaviate_of(lambda request: httpx.Response(404)) == EngineState(chunks=None)


def test_weaviate_graphql_error_with_http_200_aborts():
    def handler(request):
        if request.url.path == "/v1/schema/Email":
            return httpx.Response(200, json={"class": "Email"})
        return httpx.Response(200, json={"errors": [{"message": "shard not ready"}]})
    with pytest.raises(BenchAbort, match="shard not ready"):
        weaviate_of(handler)


def test_weaviate_unreachable_aborts():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)
    with pytest.raises(BenchAbort, match="unreachable"):
        weaviate_of(handler)


def test_fingerprint_path_is_keyed_by_engine_data_and_limit(tmp_path):
    assert fingerprint_path(tmp_path, "raggio", 2549619) == tmp_path / "fingerprint-raggio-2549619.json"
    assert fingerprint_path(tmp_path, "raggio", 553015) != fingerprint_path(tmp_path, "raggio", 2549619)


def test_fingerprint_roundtrip_is_atomic(tmp_path):
    path = fingerprint_path(tmp_path / "state", "raggio", 2549619)  # directory created on write
    assert read_fingerprint(path) is None
    write_fingerprint(path, FP)
    assert read_fingerprint(path) == FP
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left behind


@pytest.mark.parametrize("content", ["", '{"limit": 25496', "[1, 2]", "null"])
def test_unreadable_fingerprint_never_matches(tmp_path, content):
    path = tmp_path / "fingerprint-raggio-2549619.json"
    path.write_text(content)
    saved = read_fingerprint(path)
    assert saved is not None and saved != FP


FLAT = EngineState(chunks=EXPECTED)
IVF = EngineState(chunks=EXPECTED, ivf=True)
NONE = EngineState(chunks=None)
EMPTY = EngineState(chunks=0)
OTHER_FP = {**FP, "text_v": "real email embeddings"}


@pytest.mark.parametrize("name,state,saved,reingest,adopt,action", [
    ("raggio", NONE, None, False, False, "ingest"),
    ("raggio", EMPTY, None, False, False, "ingest"),
    ("raggio", FLAT, FP, False, False, "reuse"),
    ("raggio", IVF, FP, False, False, "reuse"),        # bench.py drops the index for the flat column
    ("raggio", FLAT, None, False, True, "reuse"),      # --adopt vouches for unfingerprinted data
    ("raggio", FLAT, FP, False, True, "reuse"),
    ("raggio", FLAT, FP, True, False, "ingest"),
    ("weaviate", FLAT, FP, False, False, "reuse"),
    ("weaviate", NONE, None, False, False, "ingest"),
    ("raggio-ivf", NONE, None, False, False, "ingest"),
    ("raggio-ivf", FLAT, FP, False, False, "build-index"),
    ("raggio-ivf", IVF, FP, False, False, "reuse"),
    ("raggio-ivf", IVF, FP, True, False, "build-index"),       # --reingest rebuilds the index only
    ("raggio-ivf", IVF, OTHER_FP, True, False, "ingest"),      # ... unless the data isn't this corpus
    ("raggio-ivf", EngineState(chunks=7), FP, True, False, "ingest"),
])
def test_decide(name, state, saved, reingest, adopt, action):
    assert decide(name, state, EXPECTED, saved, FP, reingest=reingest, adopt=adopt,
                  limit=2549619) == action


@pytest.mark.parametrize("name", ["raggio", "raggio-ivf", "weaviate"])
def test_wrong_limit_aborts_with_both_counts_and_the_flag(name):
    # --limit 2549119 (the ingested count) instead of 2549619 expects 500 fewer chunks
    with pytest.raises(BenchAbort) as e:
        decide(name, EngineState(chunks=2549119), 2549119 - 500, FP, FP,
               reingest=False, adopt=False, limit=2549119)
    msg = str(e.value)
    assert "2,549,119" in msg and "2,548,619" in msg and "--limit 2549119" in msg


@pytest.mark.parametrize("saved", [None, {}, OTHER_FP])
def test_fingerprint_mismatch_aborts_with_adopt_hint(saved):
    with pytest.raises(BenchAbort, match="--adopt"):
        decide("raggio", FLAT, EXPECTED, saved, FP, reingest=False, adopt=False, limit=2549619)


@pytest.mark.parametrize("saved", [{}, OTHER_FP])
def test_adopt_never_overrides_a_fingerprint_on_file(saved):
    # a different (or unreadable) fingerprint is evidence the volume holds other data
    with pytest.raises(BenchAbort, match="delete .* and pass --adopt"):
        decide("raggio", FLAT, EXPECTED, saved, FP, reingest=False, adopt=True, limit=2549619)


def test_adopt_never_overrides_a_count_mismatch():
    with pytest.raises(BenchAbort, match="--reingest"):
        decide("raggio", EngineState(chunks=10), EXPECTED, None, FP,
               reingest=False, adopt=True, limit=2549619)


class Calls:
    def __init__(self, state, ingested=EXPECTED):
        self.state_value, self.ingested, self.log = state, ingested, []

    async def state(self):
        self.log.append("state")
        if isinstance(self.state_value, Exception):
            raise self.state_value
        return self.state_value

    async def ingest(self):
        self.log.append("ingest")
        return 100.0, self.ingested

    async def build_index(self):
        self.log.append("build-index")
        return 42.0


def prepare(calls, name, fp_path, *, ivf=False, reingest=False, adopt=False):
    return asyncio.run(prepare_engine(
        name, state=calls.state, ingest=calls.ingest,
        build_index=calls.build_index if ivf else None, fp_path=fp_path, expected=EXPECTED,
        fp_now=FP, reingest=reingest, adopt=adopt, limit=2549619))


def test_prepare_reuse_touches_nothing(tmp_path):
    fp_path = tmp_path / "fp.json"
    write_fingerprint(fp_path, FP)
    calls = Calls(FLAT)
    assert prepare(calls, "raggio", fp_path) == {}
    assert calls.log == ["state"]


def test_prepare_ingest_times_it_and_records_the_fingerprint(tmp_path):
    fp_path = tmp_path / "fp.json"
    calls = Calls(NONE)
    res = prepare(calls, "raggio", fp_path)
    assert calls.log == ["state", "ingest"]
    assert res == {"ingest_s": 100.0, "ingest_vps": EXPECTED / 100.0}
    assert read_fingerprint(fp_path) == FP


def test_prepare_short_ingest_aborts_without_a_fingerprint(tmp_path):
    fp_path = tmp_path / "fp.json"
    with pytest.raises(BenchAbort, match="ingested 10"):
        prepare(Calls(NONE, ingested=10), "raggio", fp_path)
    assert read_fingerprint(fp_path) is None


def test_prepare_ivf_builds_the_index_on_reused_data(tmp_path):
    fp_path = tmp_path / "fp.json"
    write_fingerprint(fp_path, FP)
    calls = Calls(FLAT)
    assert prepare(calls, "raggio-ivf", fp_path, ivf=True) == {"index_build_s": 42.0}
    assert calls.log == ["state", "build-index"]


def test_prepare_ivf_on_an_empty_engine_ingests_then_builds(tmp_path):
    calls = Calls(NONE)
    assert prepare(calls, "raggio-ivf", tmp_path / "fp.json", ivf=True) == {"index_build_s": 42.0}
    assert calls.log == ["state", "ingest", "build-index"]


def test_prepare_adopt_records_the_fingerprint_without_ingesting(tmp_path):
    fp_path = tmp_path / "fp.json"
    calls = Calls(FLAT)
    assert prepare(calls, "raggio", fp_path, adopt=True) == {}
    assert calls.log == ["state"]
    assert read_fingerprint(fp_path) == FP


def test_prepare_adopt_keeps_a_different_fingerprint_and_touches_nothing(tmp_path):
    fp_path = tmp_path / "fp.json"
    write_fingerprint(fp_path, OTHER_FP)
    calls = Calls(FLAT)
    with pytest.raises(BenchAbort):
        prepare(calls, "raggio", fp_path, adopt=True)
    assert calls.log == ["state"]
    assert read_fingerprint(fp_path) == OTHER_FP


def test_prepare_unreachable_engine_never_ingests(tmp_path):
    calls = Calls(BenchAbort("unreachable"))
    with pytest.raises(BenchAbort):
        prepare(calls, "raggio", tmp_path / "fp.json")
    assert calls.log == ["state"]


def test_fingerprint_file_content_is_plain_json(tmp_path):
    fp_path = tmp_path / "fp.json"
    write_fingerprint(fp_path, FP)
    assert json.loads(fp_path.read_text()) == FP
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_bench_reuse.py -q`

Expected:
```
E   ModuleNotFoundError: No module named 'bench_reuse'
ERROR tests/test_bench_reuse.py
1 error
```

- [ ] **Step 3: Minimal implementation**

Create `bench/bench_reuse.py` with exactly this content:

```python
"""Reuse-or-ingest decisions for bench/bench.py.

At 2.5M rows a benchmark run reuses the vectors already in the engine volume. A wrong
"no data" answer DELETEs that collection and re-ingests it (~31 min), so everything here
fails loudly instead of guessing: an engine that doesn't answer, a chunk count that
doesn't match the run, or a fingerprint that doesn't match aborts before anything is
touched. Stdlib + httpx only, so the tests import it without the bench dependency group.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

# the first GET after a container start opens the collection on the event loop (128 s
# on the DGX from a cold page cache at 2.5M rows) and stats() runs there too; the old
# 30 s read that as "no data". 900 s is what the 2026-09-28 baseline ran with.
STATE_TIMEOUT_S = 900.0
PENDING_POLL_S = 5.0     # between pending_jobs polls while the job queue drains
PENDING_WAIT_S = 3600.0  # abort if the queue is still busy after this long


class BenchAbort(RuntimeError):
    """The harness can't safely choose between reuse and ingest; nothing was changed."""


@dataclass(frozen=True)
class EngineState:
    chunks: int | None     # None: the collection (Weaviate: class) does not exist
    ivf: bool = False      # raggio: an IVF index is attached
    pending_jobs: int = 0  # raggio: pending + processing jobs


def fingerprint_path(fp_dir, key: str, limit: int) -> Path:
    """One file per engine data set and corpus size: a 553k email run and a 2.5M arXiv
    run never read each other's fingerprint."""
    return Path(fp_dir) / f"fingerprint-{key}-{limit}.json"


def read_fingerprint(path) -> dict | None:
    """The saved fingerprint; None if none was written, {} if the file is unreadable
    (an empty dict never equals a real fingerprint, so it reads as a mismatch)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        fp = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return fp if isinstance(fp, dict) else {}


def write_fingerprint(path, fp: dict) -> None:
    """Write via a temp file + rename so a crash never leaves a truncated fingerprint."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(fp, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _unreachable(url: str, e: Exception) -> BenchAbort:
    return BenchAbort(f"{url} unreachable ({type(e).__name__}: {e}). Nothing was ingested or "
                      f"deleted. If the engine is up but slow to open the collection, raise "
                      f"--state-timeout (default {STATE_TIMEOUT_S:.0f}s).")


def _collection_names(r: httpx.Response) -> list | None:
    if r.status_code != 200:
        return None
    try:
        names = r.json()["collections"]
    except (ValueError, KeyError, TypeError):
        return None
    return names if isinstance(names, list) else None


async def raggio_state(client: httpx.AsyncClient, base: str, coll: str) -> EngineState:
    """What the raggio engine holds for `coll`. A 404 only counts as "absent" when the
    root collection list agrees; anything else that isn't a clean answer aborts."""
    url = f"{base}/collections/{coll}"
    try:
        r = await client.get(url)
        listing = await client.get(f"{base}/collections") if r.status_code == 404 else None
    except httpx.HTTPError as e:
        raise _unreachable(url, e) from e
    if listing is not None:
        names = _collection_names(listing)
        if names is not None and coll not in names:
            return EngineState(chunks=None)
        raise BenchAbort(f"GET {url} answered 404 but GET {base}/collections "
                         f"(HTTP {listing.status_code}) does not confirm '{coll}' is absent")
    if r.status_code != 200:
        raise BenchAbort(f"GET {url} -> HTTP {r.status_code}: {r.text[:200]}")
    try:
        info = r.json()
        return EngineState(chunks=int(info["chunks"]),
                           ivf=(info.get("index") or {}).get("type") == "ivf",
                           pending_jobs=int(info.get("pending_jobs", 0)))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise BenchAbort(f"GET {url}: unexpected body {r.text[:200]!r}") from e


async def weaviate_state(client: httpx.AsyncClient, base: str, wclass: str) -> EngineState:
    """Object count of `wclass`; None when the class does not exist."""
    url = f"{base}/v1/schema/{wclass}"
    agg = {"query": f"{{Aggregate {{{wclass} {{meta {{count}}}}}}}}"}
    try:
        r = await client.get(url)
        if r.status_code == 404:
            return EngineState(chunks=None)
        if r.status_code != 200:
            raise BenchAbort(f"GET {url} -> HTTP {r.status_code}: {r.text[:200]}")
        a = await client.post(f"{base}/v1/graphql", json=agg)
    except httpx.HTTPError as e:
        raise _unreachable(url, e) from e
    try:
        body = a.json()
    except ValueError as e:
        raise BenchAbort(f"weaviate aggregate: HTTP {a.status_code}, unexpected body {a.text[:200]!r}") from e
    if a.status_code != 200 or not isinstance(body, dict) or body.get("errors"):
        # weaviate answers HTTP 200 for graphql errors (e.g. a shard still loading)
        raise BenchAbort(f"weaviate aggregate failed: HTTP {a.status_code} {a.text[:300]}")
    try:
        return EngineState(chunks=int(body["data"]["Aggregate"][wclass][0]["meta"]["count"]))
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise BenchAbort(f"weaviate aggregate: unexpected body {a.text[:200]!r}") from e


async def wait_idle(client: httpx.AsyncClient, base: str, coll: str, *,
                    poll_s: float = PENDING_POLL_S, max_wait_s: float = PENDING_WAIT_S) -> EngineState:
    """raggio state once its job queue is empty: a half-applied ingest or index build
    must neither be mistaken for the corpus nor be measured."""
    t0 = time.monotonic()
    while True:
        st = await raggio_state(client, base, coll)
        if st.chunks is None or st.pending_jobs == 0:
            return st
        waited = time.monotonic() - t0
        if waited >= max_wait_s:
            raise BenchAbort(f"{coll}: {st.pending_jobs} jobs still pending after {waited:.0f}s")
        print(f"  {coll}: {st.pending_jobs} jobs pending, waiting ({waited:.0f}s)")
        await asyncio.sleep(poll_s)


def decide(name: str, state: EngineState, expected: int, saved_fp: dict | None,
           current_fp: dict, *, reingest: bool, adopt: bool, limit: int,
           fp_path="fingerprint") -> str:
    """'reuse', 'build-index' (raggio-ivf: keep the data, build the index) or 'ingest'
    (wipes the engine's copy). Raises BenchAbort when the engine holds data this run
    can't vouch for; only an empty engine or an explicit --reingest ever re-ingests.
    --adopt vouches for data that has no fingerprint on file, never against one."""
    if not state.chunks:  # absent or empty: nothing to lose
        return "ingest"
    counts_match = state.chunks == expected
    fp_ok = saved_fp == current_fp or (adopt and saved_fp is None)
    if reingest and name != "raggio-ivf":
        return "ingest"
    if reingest:  # raggio-ivf measures the index build: rebuild it on matching data only
        return "build-index" if counts_match and fp_ok else "ingest"
    if not counts_match:
        raise BenchAbort(
            f"[{name}] the engine holds {state.chunks:,} chunks but this run expects {expected:,} "
            f"(--limit {limit} minus the held-out queries). --limit is the corpus size, not the "
            f"ingested count, and a wrong value also changes the query set: fix --limit, or pass "
            f"--reingest to wipe and re-ingest (~31 min at 2.5M rows).")
    if not fp_ok and saved_fp is None:
        raise BenchAbort(
            f"[{name}] the chunk count matches but there is no fingerprint at {fp_path}. Pass "
            f"--adopt if the engine was ingested from this corpus, seed and query count (e.g. a "
            f"volume carried over from another checkout), or --reingest to rebuild it.")
    if not fp_ok:
        raise BenchAbort(
            f"[{name}] the chunk count matches but {fp_path} records another run (saved "
            f"{saved_fp}, this run {current_fp}), so the engine may hold other data. Pass "
            f"--reingest to rebuild it; if you know it holds this corpus, delete {fp_path} "
            f"and pass --adopt.")
    if name == "raggio-ivf" and not state.ivf:
        return "build-index"
    return "reuse"


async def prepare_engine(name: str, *, state, ingest, build_index=None, fp_path, expected: int,
                         fp_now: dict, reingest: bool, adopt: bool, limit: int) -> dict:
    """Decide, then reuse / ingest / build the index. `state()` -> EngineState,
    `ingest()` -> (seconds, chunk count), `build_index()` -> seconds. Returns the timings
    to merge into the results: ingest_s + ingest_vps, or index_build_s for raggio-ivf."""
    st = await state()
    saved = read_fingerprint(fp_path)
    action = decide(name, st, expected, saved, fp_now,
                    reingest=reingest, adopt=adopt, limit=limit, fp_path=fp_path)
    res = {}
    if action == "ingest":
        print(f"[{name}] ingest {expected} vectors...")
        elapsed, count = await ingest()
        if count != expected:
            raise BenchAbort(f"[{name}] ingested {count} chunks, expected {expected}")
        write_fingerprint(fp_path, fp_now)
        if build_index is None:
            res.update(ingest_s=elapsed, ingest_vps=expected / elapsed)
    else:
        if adopt and saved is None:
            write_fingerprint(fp_path, fp_now)
            print(f"[{name}] adopted the engine's {st.chunks} chunks as this corpus ({fp_path})")
        print(f"[{name}] reusing {expected} ingested chunks"
              + (", building the IVF index" if action == "build-index" else " (pass --reingest to rebuild)"))
    if action != "reuse" and build_index is not None:
        res["index_build_s"] = await build_index()
    return res
```

- [ ] **Step 4: Run the new tests and the full suite**

Run: `uv run pytest tests/test_bench_reuse.py -q`. Expected: `55 passed`.
Run: `uv run pytest -q`. Expected: `101 passed, 1 warning`.

- [ ] **Step 5: Commit**

```bash
git add bench/bench_reuse.py tests/test_bench_reuse.py
git commit -m "bench: fail loudly instead of re-ingesting when reuse can't be proven" -m "An unreachable engine, an unconfirmed 404, a chunk count that doesn't match --limit or a fingerprint that doesn't match now raise BenchAbort before anything is deleted (spec 5.3). Fingerprints are keyed by data set and limit and written atomically; wait_idle waits for pending_jobs == 0." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Wire the decision into `bench.py`: preflight, keyed fingerprints, new flags

**Files:**
- Modify: `bench/bench.py`:
  - line 22 (`import orjson`): the import block after it;
  - lines 218-231: `ingest_raggio_ivf` becomes `build_ivf_index`;
  - lines 350-374: `ENGINES` and `current_count`;
  - lines 383-402: the head of `bench_engine`;
  - lines 481-499: `main` and its parser;
  - line 514: the engine loop;
  - lines 524-525: `__main__`.
- Modify: `.gitignore:7`, `docs/benchmark.md:105-107`, `docs/benchmark-arxiv.md:234-235`, `:245` and `:252-254`.
- Untrack: `bench/fingerprint-raggio.json` and `bench/fingerprint-weaviate.json`.
- Test: `tests/test_bench_harness.py` (create).

**Interfaces:**
- Consumes: Task 1's `STATE_TIMEOUT_S`, `BenchAbort`, `decide`, `fingerprint_path`, `prepare_engine`, `read_fingerprint`, `wait_idle` and `weaviate_state`.
- Produces. Task 3's `session.sh` and plans E, F and G rely on these:
  - `bench.build_parser() -> argparse.ArgumentParser` and `async bench.main(argv=None)`;
  - `async bench.preflight(names, expected, args)`;
  - `async bench.engine_state(name, timeout) -> EngineState`;
  - `bench.fp_file(name, args) -> Path`;
  - `async bench.build_ivf_index() -> float`;
  - the `ENGINES[name]` keys `"fp"` (`raggio` for both raggio columns) and `"build_index"`;
  - the CLI flags `--adopt`, `--fingerprint-dir` (default `bench`) and `--state-timeout` (default 900). Plan F detects the hardened bench by the literal string `"--fingerprint-dir"`;
  - a `BenchAbort` from `main` exits with `SystemExit("bench aborted: …")`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bench_harness.py` with exactly this content:

```python
"""bench/bench.py wiring of the reuse decision: the preflight runs before the
ground-truth pass, fingerprints are keyed by data set and --limit, and the flags the
DGX runbook passes exist with the documented defaults."""
import asyncio

import httpx
import numpy as np
import pytest

pytest.importorskip("orjson", reason="bench.py needs the bench dependency group")

import sys
sys.path.insert(0, "bench")
import bench
from bench_reuse import BenchAbort, write_fingerprint

LIMIT, QUERIES = 2549619, 500


class ReachedGroundTruth(Exception):
    pass


@pytest.fixture
def corpus(monkeypatch):
    """A fake corpus of the arXiv shape; ground truth raises so a test can see the
    preflight let the run through."""
    def load_corpus(limit, n_queries, seed):
        return None, [], [], np.arange(limit - n_queries), None, np.zeros((n_queries, 4), np.float32)

    def ground_truth(*a, **kw):
        raise ReachedGroundTruth

    monkeypatch.setattr(bench, "DIM", 4)
    monkeypatch.setattr(bench, "load_corpus", load_corpus)
    monkeypatch.setattr(bench, "ground_truth", ground_truth)


def fake_engine(monkeypatch, handler):
    """Route every httpx.AsyncClient bench.py opens to `handler`; returns the request log."""
    seen, real = [], httpx.AsyncClient

    def record(request):
        seen.append(f"{request.method} {request.url.path}")
        return handler(request)

    monkeypatch.setattr(bench.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(record), **kw))
    return seen


def holding(chunks, index="flat"):
    return lambda request: httpx.Response(
        200, json={"chunks": chunks, "pending_jobs": 0, "index": {"type": index}})


def run(*argv):
    return asyncio.run(bench.main(list(argv)))


def test_parser_defaults():
    args = bench.build_parser().parse_args([])
    assert args.fingerprint_dir == "bench"
    assert args.state_timeout == 900
    assert not args.adopt and not args.reingest


def test_flat_and_ivf_share_one_fingerprint_per_limit(tmp_path):
    args = bench.build_parser().parse_args(["--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path)])
    assert bench.fp_file("raggio", args) == tmp_path / f"fingerprint-raggio-{LIMIT}.json"
    assert bench.fp_file("raggio-ivf", args) == bench.fp_file("raggio", args)
    assert bench.fp_file("weaviate", args) == tmp_path / f"fingerprint-weaviate-{LIMIT}.json"


def test_ivf_engine_builds_through_its_own_step():
    assert bench.ENGINES["raggio-ivf"]["ingest"] is bench.ingest_raggio
    assert bench.ENGINES["raggio-ivf"]["build_index"] is bench.build_ivf_index
    assert bench.ENGINES["raggio"]["build_index"] is None


def test_wrong_limit_aborts_before_ground_truth(monkeypatch, corpus, tmp_path):
    # the DGX volume holds 2,549,119 chunks; --limit 2549119 expects 2,548,619
    seen = fake_engine(monkeypatch, holding(2549119, "ivf"))
    with pytest.raises(BenchAbort, match="--limit 2549119"):
        run("--engine", "raggio-ivf", "--limit", "2549119", "--fingerprint-dir", str(tmp_path))
    assert all(s.startswith("GET ") for s in seen)  # no DELETE, no ingest


def test_unreachable_engine_aborts_before_ground_truth(monkeypatch, corpus, tmp_path):
    def down(request):
        raise httpx.ReadTimeout("timed out", request=request)
    seen = fake_engine(monkeypatch, down)
    with pytest.raises(BenchAbort, match="unreachable"):
        run("--engine", "raggio", "--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path))
    assert seen == ["GET /collections/bench"]


def test_legacy_unkeyed_fingerprint_is_not_trusted(monkeypatch, corpus, tmp_path):
    # bench/fingerprint-raggio.json (email corpus, or a copy from another checkout)
    # must not vouch for the arXiv volume
    args = bench.build_parser().parse_args(["--limit", str(LIMIT)])
    write_fingerprint(tmp_path / "fingerprint-raggio.json", bench.fingerprint(args))
    fake_engine(monkeypatch, holding(LIMIT - QUERIES))
    with pytest.raises(BenchAbort, match="--adopt"):
        run("--engine", "raggio", "--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path))


def test_matching_fingerprint_passes_preflight(monkeypatch, corpus, tmp_path):
    args = bench.build_parser().parse_args(["--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path)])
    write_fingerprint(bench.fp_file("raggio", args), bench.fingerprint(args))
    seen = fake_engine(monkeypatch, holding(LIMIT - QUERIES))
    with pytest.raises(ReachedGroundTruth):
        run("--engine", "raggio-ivf", "--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path))
    assert not any(s.startswith("DELETE") for s in seen)


def test_adopt_passes_preflight_and_records_nothing_yet(monkeypatch, corpus, tmp_path):
    fake_engine(monkeypatch, holding(LIMIT - QUERIES))
    with pytest.raises(ReachedGroundTruth):
        run("--engine", "raggio-ivf", "--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path), "--adopt")
    assert list(tmp_path.iterdir()) == []  # written by the engine's own step, not the preflight
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_bench_harness.py -q`

Expected: `8 failed`. The errors are:
- `AttributeError: module 'bench' has no attribute 'build_parser'` (4 tests);
- `TypeError: main() takes 0 positional arguments but 1 was given` (3 tests);
- `assert <function ingest_raggio_ivf ...> is <function ingest_raggio ...>` (`test_ivf_engine_builds_through_its_own_step`).

- [ ] **Step 3: Minimal implementation in `bench/bench.py`**

3a. After line 22 (`import orjson`), add a blank line and the import:

```python
import orjson

from bench_reuse import (STATE_TIMEOUT_S, BenchAbort, decide, fingerprint_path, prepare_engine,
                         read_fingerprint, wait_idle, weaviate_state)
```

3b. Replace the whole `ingest_raggio_ivf` function (lines 218-231, from `async def ingest_raggio_ivf(vecs, paths, years, ingest_rows, batch_size, concurrency):` to `        return elapsed, info["chunks"]`) with:

```python
async def build_ivf_index():
    """The measured raggio-ivf step: (re)build the IVF index over the flat collection's data."""
    async with httpx.AsyncClient(headers=TR_HDRS, timeout=STATE_TIMEOUT_S) as client:
        t0 = time.time()
        r = await client.post(f"{TR}/collections/{COLL}/index", json={})
        r.raise_for_status()
        await tr_wait_job(client, r.json()["job_id"])
        elapsed = time.time() - t0
        info = (await client.get(f"{TR}/collections/{COLL}")).json()
        print(f"  ivf index built in {elapsed:.0f}s: {info.get('index')}")
        return elapsed
```

3c. Replace lines 350-374, the `ENGINES = {` dict and the whole `async def current_count(name):` function down to its `        return None`, with:

```python
# "fp": whose data the column measures. raggio-ivf runs on the flat collection's data
# (plus an index), so both raggio columns share one fingerprint.
ENGINES = {
    "raggio": {"container": "bench-tv", "hdrs": TR_HDRS, "volume": "bench-tv", "fp": "raggio",
               "ingest": ingest_raggio, "build_index": None},
    "raggio-ivf": {"container": "bench-tv", "hdrs": TR_HDRS, "volume": "bench-tv", "fp": "raggio",
                   "ingest": ingest_raggio, "build_index": build_ivf_index},
    "weaviate": {"container": "bench-wv", "hdrs": WV_HDRS, "volume": "bench-wv", "fp": "weaviate",
                 "ingest": ingest_weaviate, "build_index": None},
}


async def engine_state(name, timeout):
    """What the engine holds; raggio is read once its job queue has drained."""
    async with httpx.AsyncClient(headers=ENGINES[name]["hdrs"], timeout=timeout) as client:
        if name.startswith("raggio"):
            return await wait_idle(client, TR, COLL)
        return await weaviate_state(client, WV, WCLASS)


def fp_file(name, args):
    return fingerprint_path(args.fingerprint_dir, ENGINES[name]["fp"], args.limit)
```

3d. `fingerprint(args)` stays as it is. Replace the head of `bench_engine` (lines 383-402) with `preflight` and the new head. The lines replaced run from `async def bench_engine(name, vecs, paths, years, ingest_rows, queries, gt_rows, hybrid_texts, hybrid_paths, args):` down to `        res.pop("ingest_vps", None)`. Everything from `    res["mem_after_ingest_mb"] = podman_mem(e["container"])` onward stays.

```python
async def preflight(names, expected, args):
    """Every engine's reuse decision before the ground-truth pass (minutes at 2.5M rows):
    a wrong --limit, a missing fingerprint or an unreachable engine aborts here."""
    for name in names:
        st = await engine_state(name, args.state_timeout)
        fp_path = fp_file(name, args)
        action = decide(name, st, expected, read_fingerprint(fp_path), fingerprint(args),
                        reingest=args.reingest, adopt=args.adopt, limit=args.limit, fp_path=fp_path)
        print(f"[{name}] preflight: {st.chunks} chunks, ivf={st.ivf} -> {action}")


async def bench_engine(name, vecs, paths, years, ingest_rows, queries, gt_rows, hybrid_texts, hybrid_paths, args):
    e = ENGINES[name]
    res = await prepare_engine(
        name, state=lambda: engine_state(name, args.state_timeout),
        ingest=lambda: e["ingest"](vecs, paths, years, ingest_rows, args.batch_size, args.ingest_concurrency),
        build_index=e["build_index"], fp_path=fp_file(name, args), expected=len(ingest_rows),
        fp_now=fingerprint(args), reingest=args.reingest, adopt=args.adopt, limit=args.limit)
    if name == "raggio":
        await ensure_no_index()  # flat column must measure the brute-force scan, never a leftover index
    if name.startswith("raggio"):
        await engine_state(name, args.state_timeout)  # measure only once the job queue is empty
```

3e. Split `main` into a parser builder and `main(argv=None)`. Replace

```python
async def main():
    ap = argparse.ArgumentParser()
```

with

```python
def build_parser():
    ap = argparse.ArgumentParser()
```

Then replace

```python
    ap.add_argument("--reingest", action="store_true",
                    help="wipe and re-ingest even if the engine already holds the corpus")
```

with

```python
    ap.add_argument("--reingest", action="store_true",
                    help="wipe and re-ingest even if the engine already holds the corpus "
                         "(raggio-ivf: rebuild only the index when the data matches)")
    ap.add_argument("--adopt", action="store_true",
                    help="trust the engine's data when its chunk count matches and no fingerprint "
                         "is on file, e.g. a volume carried over from another checkout; records "
                         "this run's fingerprint (a different fingerprint on file still aborts)")
    ap.add_argument("--fingerprint-dir", default="bench",
                    help="directory for fingerprint-<data>-<limit>.json; keep it outside a checkout "
                         "that a deploy wipes")
    ap.add_argument("--state-timeout", type=float, default=STATE_TIMEOUT_S,
                    help="seconds to wait for an engine's state (the first request after a "
                         "container start opens the collection)")
```

Then replace

```python
    args = ap.parse_args()

    vecs, paths, years, ingest_rows, _, queries = load_corpus(args.limit, args.queries, args.seed)
    print(f"corpus: {len(ingest_rows)} ingest rows, {len(queries)} queries")
```

with

```python
    return ap


async def main(argv=None):
    args = build_parser().parse_args(argv)
    names = list(ENGINES) if args.engine == "all" else [args.engine]

    vecs, paths, years, ingest_rows, _, queries = load_corpus(args.limit, args.queries, args.seed)
    print(f"corpus: {len(ingest_rows)} ingest rows, {len(queries)} queries")
    await preflight(names, len(ingest_rows), args)
```

3f. Replace `    for name in (list(ENGINES) if args.engine == "all" else [args.engine]):` with `    for name in names:`.

3g. Replace the last two lines

```python
if __name__ == "__main__":
    asyncio.run(main())
```

with

```python
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BenchAbort as e:
        raise SystemExit(f"bench aborted: {e}")
```

The docstring's `uv run python bench/bench.py` lines stay as they are until Task 7.

3h. `.gitignore`: after line 7 (`bench/snapshot/`) add two lines:

```
bench/snapshot/
bench/fingerprint-*.json
bench/results-partial.json
```

3i. Stop tracking the two unkeyed fingerprints. The email corpus's own fingerprints move to the keyed names so a local `--limit 553015` run still reuses its volumes:

```bash
git rm -q --cached bench/fingerprint-raggio.json bench/fingerprint-weaviate.json
mv bench/fingerprint-raggio.json bench/fingerprint-raggio-553015.json
mv bench/fingerprint-weaviate.json bench/fingerprint-weaviate-553015.json
git status --short --ignored bench/ | grep fingerprint
```

Expected:

```
D  bench/fingerprint-raggio.json
D  bench/fingerprint-weaviate.json
!! bench/fingerprint-raggio-553015.json
!! bench/fingerprint-weaviate-553015.json
```

3j. `docs/benchmark.md`: replace lines 106-107

```
the volumes and is fingerprinted, so repeat runs start straight at the search
phases; ingest wall time is measured on `--reingest` runs. Supporting
```

with

```
the volumes and is fingerprinted per corpus size
(`bench/fingerprint-<engine>-<limit>.json`, untracked), so repeat runs start
straight at the search phases; ingest wall time is measured on `--reingest`
runs. Before the ground-truth pass the harness checks every engine: if one
doesn't answer, holds a different chunk count (a wrong `--limit`, or the
smoke run's data), or has no matching fingerprint, it stops instead of
re-ingesting. Pass `--reingest` to wipe and re-ingest, or `--adopt` to accept
a volume that has no fingerprint on file yet but was ingested from the same
corpus (e.g. by another checkout). Supporting
```

3k. `docs/benchmark-arxiv.md` has three edits.

Replace lines 234-235

```
# if you previously ran the email benchmark, clear its checkpoints
rm -f bench/fingerprint-*.json bench/results-partial.json
```

with

```
# if you previously ran the email benchmark, clear its result checkpoint
# (fingerprints are per corpus size, so they never mix)
rm -f bench/results-partial.json
```

Replace line 245

```
nohup env PYTHONUNBUFFERED=1 uv run python bench/bench.py --limit <N> \
```

with

```
nohup env PYTHONUNBUFFERED=1 uv run python bench/bench.py --limit <N> --reingest \
```

Replace lines 252-254

```
`bench/results-arxiv.md`. It checkpoints `bench/results-partial.json` after
each phase and fingerprints ingested data, so a re-run skips straight to the
phases that remain.
```

with

```
`bench/results-arxiv.md`. The volumes still hold the smoke run's chunks, so
the first full run passes `--reingest`; without it the harness stops at its
preflight check rather than wipe data it can't vouch for. It checkpoints
`bench/results-partial.json` after each phase and fingerprints ingested data,
so a re-run (without `--reingest`) skips straight to the phases that remain.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bench_harness.py -q`
Expected: `8 passed`

Run: `uv run pytest -q`
Expected: `109 passed, 1 warning`

- [ ] **Step 5: Commit**

The `git rm --cached` from 3i is already staged.

```bash
git add bench/bench.py tests/test_bench_harness.py .gitignore docs/benchmark.md docs/benchmark-arxiv.md
git commit -m "$(cat <<'EOF'
bench: preflight every engine before ground truth, key fingerprints by --limit

A wrong --limit, an unreachable engine or data without a matching
fingerprint now aborts before the ground-truth pass instead of silently
re-ingesting (or reporting no data). raggio-ivf reuses the flat
collection's data and measures only its index build. Fingerprints are
untracked and live under --fingerprint-dir.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: The DGX runbook (`bench/dgx/`): deploy, one session in the §6 order, noise band

**Files:**
- Create: `.gitattributes`.
- Create: `bench/dgx/lib.sh`, `bench/dgx/deploy.sh`, `bench/dgx/unpack.sh`, `bench/dgx/setup.sh`, `bench/dgx/session.sh` and `bench/dgx/compare.py`.
- Test: `tests/test_bench_dgx.py` (create).

**Interfaces:**
- Consumes: Task 2's CLI (`--limit`, `--engine`, `--fingerprint-dir`, `--adopt`, `--out`, `--caps-note`, `--host`), the `bench/results-partial.json` checkpoint, and `bench.ROWS`.
- Produces. Tasks 4, 6 and 10 and plans B–G rely on these:
  - `bash bench/dgx/deploy.sh` (workstation, repo root). It ships `git archive HEAD` and tags `localhost/raggio:<sha7>` on gn100.
  - `bash ~/raggio/bench/dgx/session.sh <label>` (gn100).
    - Environment: `IMAGE=<ref>`, `ADOPT=1`, `DRY_RUN=1`, `DRY_INDEX=ivf|flat`, `BENCH_HOME`, `RAGGIO_DIR`.
    - Writes `~/raggio-bench/<label>/{facts.txt,session.pgid,run<N>-<engine>.{md,json},bench.py}`.
  - `python bench/dgx/compare.py <baseline-dir> [<candidate-dir>]`.
  - In `compare.py`:
    - `load(dir) -> {engine: {run: {metric: value}}}`;
    - `values(engine_runs, key) -> list[float]`;
    - `summary(vals) -> (median, max - min)`;
    - `verdict(key, base, cand) -> "better" | "worse" | "within band" | "no band" | "not claimable"`;
    - `render(base_dir, cand_dir=None) -> str`;
    - `beyond_band(base_dir, cand_dir) -> {"better": [(engine, key)], "worse": [(engine, key)]}`;
    - `METRICS`: `(key, label, fmt, higher_is_better)` rows in `bench.ROWS` order.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bench_dgx.py` with exactly this content:

```python
"""The DGX runbook (bench/dgx/, spec §6): shell syntax and line endings, the guard rails
(only bench-tv is ever stopped or removed, --limit 2549619, no --reingest, state kept
outside the deploy-replaced checkout), dry runs of both session orders, the deploy's
unpack step, and the median / max - min noise band of compare.py."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, "bench/dgx")
import compare

DGX = Path("bench/dgx")
SCRIPTS = sorted(DGX.glob("*.sh"))
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")
BENCH_CMD = ".venv/bin/python -u bench/bench.py "
IMAGE = "localhost/raggio:abc1234"
STOP = "podman stop -t 60 --ignore bench-tv"
RM = "podman rm -f --ignore bench-tv"
STARTED = "wait for /healthz and the first GET /collections/bench"
IDLE = "wait for pending_jobs == 0"
DETACH = "curl -sf --max-time 900 -X DELETE -H x-api-key: bench http://localhost:18000/collections/bench/index"


def podman_run(memory, image=IMAGE):
    return (f"podman run -d --name bench-tv --memory {memory} -p 18000:8000 -v bench-tv:/data "
            f"-e ROOT_API_KEY=bench {image}")


def test_runbook_scripts_exist():
    assert [p.name for p in SCRIPTS] == ["deploy.sh", "lib.sh", "session.sh", "setup.sh", "unpack.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_use_lf_line_endings(script):
    # deploy ships `git archive HEAD`; a CRLF script dies on the DGX with $'\r': command not found
    assert b"\r" not in script.read_bytes()


def test_gitattributes_forces_lf_for_shell_scripts():
    # core.autocrlf=true on the workstation converts the archive too, unless the path says eol=lf
    lines = Path(".gitattributes").read_text().splitlines()
    assert "*.sh text eol=lf" in lines


@needs_bash
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script):
    subprocess.run([BASH, "-n", str(script)], check=True)


def test_guard_rails():
    text = {p.name: p.read_text(encoding="utf-8") for p in SCRIPTS}
    joined = "\n".join(text.values())
    assert "pkill" not in joined
    assert "volume rm" not in joined
    assert "--reingest" not in joined
    assert "2549119" not in joined  # the ingested count, never a --limit
    assert "LIMIT=2549619" in text["lib.sh"]
    assert "CONTAINER=bench-tv" in text["lib.sh"]
    for line in joined.splitlines():
        code = line.split("#")[0]
        if "podman rm" in code:
            assert code.split("podman rm", 1)[1].split() == ["-f", "--ignore", '"$CONTAINER"']
        if "podman stop" in code:
            assert code.split("podman stop", 1)[1].split() == ["-t", "60", "--ignore", '"$CONTAINER"']
    # fingerprints and results live under BENCH_HOME, outside the ~/raggio that deploy replaces
    assert 'BENCH_HOME="${BENCH_HOME:-$HOME/raggio-bench}"' in text["lib.sh"]
    assert "rm -rf ~/raggio && mkdir ~/raggio && tar -x -C ~/raggio" in text["unpack.sh"]
    # an early stop signals the session's process group, recorded here, never a pattern
    assert "ps -o pgid= -p $$ | tr -d ' ' > \"$OUT/session.pgid\"" in text["session.sh"]


def dry_session(tmp_path, *env_pairs):
    home, raggio = tmp_path / "bench-home", tmp_path / "raggio"
    raggio.mkdir()
    (raggio / ".deployed-sha").write_text("abc1234\n")
    env = {**os.environ, "DRY_RUN": "1", "BENCH_HOME": home.as_posix(), "RAGGIO_DIR": raggio.as_posix(),
           **dict(env_pairs)}
    out = subprocess.run([BASH, (DGX / "session.sh").resolve().as_posix(), "s1"], env=env,
                         capture_output=True, text=True, check=True).stdout
    return home, [ln[2:] for ln in out.splitlines() if ln.startswith("+ ")]


def bench_runs(cmds):
    """[(index, run tag, engine)] of the bench.py invocations."""
    return [(i, re.search(r"--out \S+/(run\d+)-", c).group(1), re.search(r"--engine (\S+)", c).group(1))
            for i, c in enumerate(cmds) if c.startswith(BENCH_CMD)]


@needs_bash
def test_session_starting_on_ivf_detaches_once_between_the_groups(tmp_path):
    _, cmds = dry_session(tmp_path, ("DRY_INDEX", "ivf"))
    runs = bench_runs(cmds)
    assert [(tag, engine) for _, tag, engine in runs] == [
        ("run0", "raggio-ivf"), ("run1", "raggio-ivf"), ("run2", "raggio-ivf"), ("run3", "raggio-ivf"),
        ("run0", "raggio"), ("run1", "raggio"), ("run2", "raggio")]
    # the one transition: after the IVF group, in a transient 8 GiB container, waited out
    (d,) = [i for i, c in enumerate(cmds) if "-X DELETE" in c]
    assert cmds[d] == DETACH
    assert runs[3][0] < d < runs[4][0]
    assert cmds[d - 5:d] == [STOP, RM, podman_run("8g"), STARTED, IDLE]
    assert cmds[d + 1] == IDLE
    assert [c for c in cmds if "--memory 8g" in c] == [podman_run("8g")]


@needs_bash
def test_session_starting_flat_builds_the_index_in_the_ivf_warm_up(tmp_path):
    _, cmds = dry_session(tmp_path, ("DRY_INDEX", "flat"))
    assert [(tag, engine) for _, tag, engine in bench_runs(cmds)] == [
        ("run0", "raggio"), ("run1", "raggio"), ("run2", "raggio"),
        ("run0", "raggio-ivf"), ("run1", "raggio-ivf"), ("run2", "raggio-ivf"), ("run3", "raggio-ivf")]
    # raggio-ivf run0 finds a flat index and builds it (bench.py's build-index path): no detach
    assert not [c for c in cmds if "-X DELETE" in c or "--memory 8g" in c]


@needs_bash
def test_every_run_starts_from_a_fresh_idle_4g_container(tmp_path):
    home, cmds = dry_session(tmp_path)
    out, state = (home / "s1").as_posix(), (home / "state").as_posix()
    assert cmds[:5] == [STOP, RM, podman_run("4g"), STARTED, IDLE]  # the first start
    for i, tag, engine in bench_runs(cmds):
        assert cmds[i - 6:i] == [STOP, RM, podman_run("4g"), STARTED, IDLE, "rm -f bench/results-partial.json"]
        assert cmds[i + 1] == f"mv bench/results-partial.json {out}/{tag}-{engine}.json"
        assert f"--limit 2549619 " in cmds[i] and f"--fingerprint-dir {state} " in cmds[i]
        assert cmds[i].endswith(f"--out {out}/{tag}-{engine}.md")  # no --adopt without ADOPT=1


@needs_bash
def test_session_records_the_host_facts_first(tmp_path):
    home, _ = dry_session(tmp_path)
    facts = (home / "s1" / "facts.txt").read_text().splitlines()
    assert facts[0].endswith(f"session s1: image {IMAGE}, bench checkout abc1234")
    for cmd in ["uptime", "free -m", "grep -E ^(Cached|Buffers): /proc/meminfo", "podman ps -a",
                "podman image inspect --format {{.Id}} {{.Digest}} " + IMAGE]:
        assert f"+ {cmd}" in facts
    assert any(f.startswith(f"+ podman run --rm {IMAGE} python -c import sys, sqlite3") for f in facts)


@needs_bash
def test_session_can_measure_another_image(tmp_path):
    _, cmds = dry_session(tmp_path, ("IMAGE", "localhost/raggio:baseline"))
    starts = [c for c in cmds if c.startswith("podman run -d ")]
    assert starts and all(c.endswith(" localhost/raggio:baseline") for c in starts)


@needs_bash
def test_session_adopt_applies_to_the_first_run_only(tmp_path):
    _, cmds = dry_session(tmp_path, ("ADOPT", "1"))
    runs = [c for c in cmds if c.startswith(BENCH_CMD)]
    assert ["--adopt" in r for r in runs] == [True] + [False] * 6


@needs_bash
def test_session_refuses_to_overwrite_a_label(tmp_path):
    home, raggio = tmp_path / "bench-home", tmp_path / "raggio"
    (home / "s1").mkdir(parents=True)
    raggio.mkdir()
    (raggio / ".deployed-sha").write_text("abc1234\n")
    env = {**os.environ, "DRY_RUN": "1", "BENCH_HOME": home.as_posix(), "RAGGIO_DIR": raggio.as_posix()}
    r = subprocess.run([BASH, (DGX / "session.sh").resolve().as_posix(), "s1"], env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "exists" in r.stderr


def archive(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack(home, files):
    env = {**os.environ, "HOME": home.as_posix()}
    subprocess.run([BASH, (DGX / "unpack.sh").resolve().as_posix()], input=archive(files), env=env,
                   check=True, capture_output=True)


@needs_bash
def test_unpack_keeps_the_previous_checkouts_results_then_replaces_it(tmp_path):
    bench = tmp_path / "raggio" / "bench"
    bench.mkdir(parents=True)
    for name in ["results-baseline3-run1-raggio.json", "baseline3.log", "fingerprint-raggio.json",
                 "gt-2549619-42-d1024.npz", "bench.py"]:
        (bench / name).write_text(name)
    unpack(tmp_path, {"README.md": b"new\n", "bench/dgx/lib.sh": b"# lib\n"})
    (kept,) = (tmp_path / "raggio-bench").glob("pre-deploy-*")
    assert sorted(p.name for p in kept.iterdir()) == [
        "baseline3.log", "fingerprint-raggio.json", "results-baseline3-run1-raggio.json"]
    assert sorted(p.relative_to(tmp_path / "raggio").as_posix()
                  for p in (tmp_path / "raggio").rglob("*") if p.is_file()) == ["README.md", "bench/dgx/lib.sh"]


@needs_bash
def test_unpack_on_a_fresh_host_keeps_nothing(tmp_path):
    unpack(tmp_path, {"README.md": b"new\n"})
    assert (tmp_path / "raggio" / "README.md").read_text() == "new\n"
    assert not (tmp_path / "raggio-bench").exists()


def write_session(d, runs):
    """runs: {"run1-raggio": {metric: value}} -> run1-raggio.json = {"raggio": {...}}"""
    d.mkdir(parents=True)
    for stem, metrics in runs.items():
        engine = stem.split("-", 1)[1]
        (d / f"{stem}.json").write_text(json.dumps({engine: metrics}))


def test_summary_is_the_median_and_max_minus_min():
    assert compare.summary([10.0, 12.0]) == (11.0, 2.0)
    mid, band = compare.summary([166.5, 214.3, 195.4])
    assert mid == 195.4 and band == pytest.approx(47.8)


def test_verdict_uses_the_noise_band_and_metric_direction():
    # latency: lower is better; baseline band 12 - 10 = 2 around a median of 11
    assert compare.verdict("lat_p50", [10.0, 12.0], [12.5, 12.5]) == "within band"
    assert compare.verdict("lat_p50", [10.0, 12.0], [13.5, 13.5]) == "worse"
    assert compare.verdict("lat_p50", [10.0, 12.0], [8.5, 8.5]) == "better"
    # throughput: higher is better
    assert compare.verdict("qps_concurrent", [100.0, 110.0], [90.0, 90.0]) == "worse"
    assert compare.verdict("qps_concurrent", [100.0, 110.0], [120.0, 120.0]) == "better"
    # identical runs: a zero band still accepts exact equality
    assert compare.verdict("recall_at_10", [0.9, 0.9], [0.9, 0.9]) == "within band"
    # a noisy candidate widens the band: median 12 is 2 worse, inside its own band of 2
    assert compare.verdict("lat_p50", [10.0, 10.0], [11.0, 13.0]) == "within band"
    # IVF: medians of three, the 16-24% warm spread is the band
    assert compare.verdict("qps_concurrent", [166.5, 195.4, 214.3], [150.0, 170.0, 200.0]) == "within band"
    # the band is never narrower than the table shows: disk is printed in whole MB
    assert compare.verdict("disk_mb", [16757.87, 16757.87], [16758.2, 16758.2]) == "within band"
    assert compare.verdict("disk_mb", [16757.87, 16757.87], [16760.0, 16760.0]) == "worse"


def test_verdict_needs_two_values_and_never_claims_the_warm_restart():
    assert compare.verdict("index_build_s", [213.0], [150.0]) == "no band"
    assert compare.verdict("cold_start_s", [4.3, 4.4], [30.0, 31.0]) == "not claimable"


def test_the_warm_up_counts_only_for_one_shot_metrics(tmp_path):
    write_session(tmp_path / "s", {
        "run0-raggio-ivf": {"index_build_s": 213.0, "lat_p50": 15.5},
        "run1-raggio-ivf": {"lat_p50": 11.2}, "run2-raggio-ivf": {"lat_p50": 11.1},
        "run3-raggio-ivf": {"lat_p50": 12.0}})
    runs = compare.load(tmp_path / "s")["raggio-ivf"]
    assert compare.values(runs, "lat_p50") == [11.2, 11.1, 12.0]
    assert compare.values(runs, "index_build_s") == [213.0]
    out = compare.render(tmp_path / "s")
    assert "| raggio-ivf | Search p50 (ms) | 11.2 / 11.1 / 12.0 | 11.2 | 0.9 |" in out
    assert "| raggio-ivf | IVF index build (s) | 213 | 213 | - |" in out


def test_compare_table_for_one_session_and_an_ab(tmp_path):
    base, cand = tmp_path / "main", tmp_path / "cand"
    write_session(base, {"run1-raggio": {"lat_p50": 23.0, "qps_concurrent": 129.0},
                         "run2-raggio": {"lat_p50": 24.0, "qps_concurrent": 131.0}})
    write_session(cand, {"run1-raggio": {"lat_p50": 30.0, "qps_concurrent": 130.0},
                         "run2-raggio": {"lat_p50": 30.0, "qps_concurrent": 130.0}})
    assert "| raggio | Search p50 (ms) | 23.0 / 24.0 | 23.5 | 1.0 |" in compare.render(base)
    ab = compare.render(base, cand)
    assert "| raggio | Search p50 (ms) | 23.5 | 1.0 | 30.0 | 0.0 | worse |" in ab
    assert "| raggio | QPS concurrent | 130 | 2 | 130 | 0 | within band |" in ab
    assert compare.beyond_band(base, cand) == {"better": [], "worse": [("raggio", "lat_p50")]}


def test_compare_marks_metrics_missing_from_a_session(tmp_path):
    # the IVF build is measured only by a flat-first session
    base, cand = tmp_path / "main", tmp_path / "cand"
    write_session(base, {"run0-raggio-ivf": {"index_build_s": 213.0},
                         "run1-raggio-ivf": {"lat_p50": 11.0}, "run2-raggio-ivf": {"lat_p50": 12.0}})
    write_session(cand, {"run1-raggio-ivf": {"lat_p50": 11.5}, "run2-raggio-ivf": {"lat_p50": 11.5}})
    ab = compare.render(base, cand)
    assert "| raggio-ivf | IVF index build (s) | 213 | - | - | - | - |" in ab
    assert "| raggio-ivf | Search p50 (ms) | 11.5 | 1.0 | 11.5 | 0.0 | within band |" in ab


def test_compare_refuses_a_directory_without_runs(tmp_path):
    with pytest.raises(SystemExit, match="no run<N>-<engine>.json"):
        compare.load(tmp_path)


def test_compare_covers_every_bench_metric():
    pytest.importorskip("orjson", reason="bench.py needs the bench dependency group")
    sys.path.insert(0, "bench")
    import bench
    assert [(key, label, fmt) for label, key, fmt in bench.ROWS] == \
        [(key, label, fmt) for key, label, fmt, _ in compare.METRICS]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_bench_dgx.py -q`

Expected: `1 error`, with `ModuleNotFoundError: No module named 'compare'` from `ERROR tests/test_bench_dgx.py`.

- [ ] **Step 3: Minimal implementation**

Write every `bench/dgx/*.sh` file with LF line endings, and create `.gitattributes` first. With `core.autocrlf=true` it keeps them LF on checkout and in `git archive`. The LF test reads the working-tree bytes, so an editor that writes CRLF fails it.

3a. Create `.gitattributes`:

```
*.sh text eol=lf
```

3b. Create `bench/dgx/lib.sh`:

```bash
# Shared settings for the DGX runbook (spec §6). Sourced by setup.sh and session.sh.
# shellcheck shell=bash
set -euo pipefail

LIMIT=2549619         # corpus size; the volume holds 2,549,119 chunks (LIMIT minus 500 held-out queries)
CONTAINER=bench-tv    # the only container this runbook ever removes; bench-wv is never touched
VOLUME=bench-tv       # reused across sessions and images; never removed
PORT=18000
MEMORY=4g             # every measured run
DETACH_MEMORY=8g      # IVF -> flat detach: refused at 4 GiB until plan C releases build memory
FLAT_RUNS=2           # measured runs after the warm-up run0: flat band = run1 vs run2
IVF_RUNS=3            # IVF: median of >= 3 runs, band = max - min
API_KEY=bench
RAGGIO_DIR="${RAGGIO_DIR:-$HOME/raggio}"          # deploy.sh wipes and re-creates this
BENCH_HOME="${BENCH_HOME:-$HOME/raggio-bench}"   # survives deploys: fingerprints + session outputs
STATE="$BENCH_HOME/state"
PENDING_WAIT_S=3600
CAPS="raggio container capped at 4 GiB (4-bit quantized flat index; IVF column adds the optional index)."
HOST_NOTE="NVIDIA DGX Spark - GB10 Grace, 20 aarch64 cores (10x Cortex-X925 + 10x Cortex-A725), 122 GB unified LPDDR5x, native Linux, rootless podman"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

# print a command, then run it unless DRY_RUN=1
run() {
  printf '+ %s\n' "$*"
  if [ "${DRY_RUN:-0}" != 1 ]; then "$@"; fi
}
```

3c. Create `bench/dgx/deploy.sh`:

```bash
#!/usr/bin/env bash
# Ship the committed HEAD to the DGX and build its image (spec §6 "Deploy").
# Run on the workstation from the repo root:
#   SSH_CONFIG=<ssh config that reaches the DGX> bash bench/dgx/deploy.sh
# The ssh config path is the operator's, so the repo carries no default for it.
# ~/raggio on the DGX is replaced (unpack.sh keeps a copy of its bench results and logs);
# bench state and session outputs live in ~/raggio-bench (lib.sh).
set -euo pipefail

SSH_CONFIG="${SSH_CONFIG:?set SSH_CONFIG to the ssh config file that reaches the DGX}"
DGX_HOST="${DGX_HOST:-gn100}"
ssh_dgx() { ssh -F "$SSH_CONFIG" -o BatchMode=yes "$DGX_HOST" "$@"; }

if ! git diff --quiet HEAD --; then
  echo "tracked files differ from HEAD; deploy ships HEAD only: commit or stash first" >&2
  exit 1
fi
SHA=$(git rev-parse --short=7 HEAD)
echo "deploying $SHA to $DGX_HOST"
git -c core.autocrlf=false archive HEAD | ssh_dgx "$(cat "$(dirname "$0")/unpack.sh")"
ssh_dgx "bash ~/raggio/bench/dgx/setup.sh $SHA"
```

3d. Create `bench/dgx/unpack.sh`:

```bash
# DGX side of deploy.sh (spec §6 "Deploy"): stdin is `git archive HEAD`. Results, logs and
# fingerprints a previous checkout left in ~/raggio/bench are copied to
# ~/raggio-bench/pre-deploy-<UTC time>/ first; then ~/raggio is replaced by the archive.
# deploy.sh sends this text as the ssh command, so it must not refer to its own path.
set -euo pipefail
if [ -d ~/raggio/bench ]; then
  keep=~/raggio-bench/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p "$keep"
  find ~/raggio/bench -maxdepth 1 -type f \
    \( -name 'results-*' -o -name '*.log' -o -name 'fingerprint-*.json' \) -exec cp -p {} "$keep"/ \;
  rmdir "$keep" 2>/dev/null || echo "kept $(ls "$keep" | wc -l) file(s) of the previous checkout in $keep"
fi
rm -rf ~/raggio && mkdir ~/raggio && tar -x -C ~/raggio
```

3e. Create `bench/dgx/setup.sh`. `~/turborag` is only read: its corpus is symlinked and its ground-truth cache copied. The `grep` lets this PR 1 script sync a checkout from before or after Task 7 moves orjson; Task 7 replaces it with the plain `--group bench` line.

```bash
#!/usr/bin/env bash
# DGX side of a deploy (deploy.sh runs it): corpus symlink, cached ground truth, host
# venv for bench.py, and the image under test tagged with the deployed SHA.
# Usage: bash ~/raggio/bench/dgx/setup.sh <sha>
source "$(dirname "$0")/lib.sh"
SHA="${1:?usage: setup.sh <git sha>}"

cd "$RAGGIO_DIR"
echo "$SHA" > .deployed-sha
run ln -sfn "$HOME/turborag/bench/corpus" bench/corpus
run cp "$HOME/turborag/bench/gt-$LIMIT-42-d1024.npz" bench/
run "$HOME/.local/bin/uv" --version
# orjson sits in the default dev group until the packaging change moves it to "bench"
if grep -q '^bench = \[' pyproject.toml; then GROUP=(--group bench); else GROUP=(); fi
run "$HOME/.local/bin/uv" sync --frozen "${GROUP[@]}"
run podman build -t "localhost/raggio:$SHA" .
mkdir -p "$STATE"
log "deployed $SHA as localhost/raggio:$SHA"
```

3f. Create `bench/dgx/session.sh`:

```bash
#!/usr/bin/env bash
# One DGX measurement session of one image (spec §6 "Order in a session"):
#   1. host facts: uptime, free -m, Cached/Buffers from /proc/meminfo, podman ps -a,
#      image id/digest, bench SHA, python/sqlite/turbovec versions
#   2. first container start: time to /healthz and to the first GET /collections/bench
#   3. wait for pending_jobs == 0, then read the index state
#   4-5. both engine groups, the one matching the index state first, so the session makes
#      one IVF <-> flat transition. Per engine a discarded warm-up run0, then the measured
#      runs (flat run1-run2, IVF run1-run3; compare.py takes the median and max - min).
#      IVF first: the detach runs in a transient 8 GiB container (refused at 4 GiB until
#      plan C). Flat first: raggio-ivf run0 builds the index at 4 GiB and measures the build.
#   Every run starts from a fresh 4 GiB bench-tv container and waits for pending_jobs == 0.
# Outputs go to ~/raggio-bench/<label>/: facts.txt and <run>-<engine>.md/.json. Launch
# detached, capped at the agreed run window (timeout stops the harness and bench.py, not
# the container), and poll the log:
#   mkdir -p ~/raggio-bench && setsid -f timeout --kill-after=60 <cap, e.g. 60m> \
#     bash ~/raggio/bench/dgx/session.sh <label> > ~/raggio-bench/<label>.log 2>&1 < /dev/null
# To stop it early, signal its process group (no name pattern that could match the caller):
#   kill -TERM -- -"$(cat ~/raggio-bench/<label>/session.pgid)"
# IMAGE=<ref>: measure another image (default localhost/raggio:<deployed sha>).
# ADOPT=1: pass --adopt to the first run (first session on a volume with no fingerprint yet).
# DRY_RUN=1: print the commands without running them; DRY_INDEX=ivf|flat is the index
# state a dry run assumes (default ivf).
source "$(dirname "$0")/lib.sh"
LABEL="${1:?usage: session.sh <label>}"

cd "$RAGGIO_DIR"
SHA=$(cat .deployed-sha)
IMAGE="${IMAGE:-localhost/raggio:$SHA}"
if [ "${DRY_RUN:-0}" != 1 ] && ! podman image exists "$IMAGE"; then
  echo "image $IMAGE not found: deploy first, or set IMAGE" >&2
  exit 1
fi
OUT="$BENCH_HOME/$LABEL"
if [ -e "$OUT" ]; then
  echo "$OUT exists: pick a new label" >&2
  exit 1
fi
mkdir -p "$OUT" "$STATE"
if [ "${DRY_RUN:-0}" != 1 ]; then
  ps -o pgid= -p $$ | tr -d ' ' > "$OUT/session.pgid"
fi
URL="http://localhost:$PORT/collections/bench"
VERSIONS='import sys, sqlite3, importlib.metadata as m
print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version, "turbovec", m.version("turbovec"))'
FIELD='import json, sys
info = json.load(sys.stdin)
print(info["pending_jobs"] if sys.argv[1] == "pending" else (info.get("index") or {}).get("type"))'
EXTRA=()
if [ "${ADOPT:-0}" = 1 ]; then EXTRA=(--adopt); fi

ms_since() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }

# GET /collections/bench; the first one after a start loads the collection (minutes when cold)
info() { curl -sf --max-time 1800 -H "x-api-key: $API_KEY" "$URL"; }
field() { info | .venv/bin/python -c "$FIELD" "$1"; }

index_type() {  # prints ivf or flat
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "${DRY_INDEX:-ivf}"; return; fi
  field index
}

wait_started() {  # $1 = label, $2 = container start time in ns
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ wait for /healthz and the first GET /collections/bench"; return; fi
  local i
  for i in $(seq 1 1200); do
    if curl -sf -o /dev/null --max-time 2 "http://localhost:$PORT/healthz"; then break; fi
    if [ "$i" = 1200 ]; then log "$1: no /healthz after 600 s" >&2; exit 1; fi
    sleep 0.5
  done
  log "$1: /healthz after $(ms_since "$2") ms" | tee -a "$OUT/facts.txt"
  info > /dev/null
  log "$1: first GET /collections/bench answered after $(ms_since "$2") ms" | tee -a "$OUT/facts.txt"
}

wait_pending() {
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ wait for pending_jobs == 0"; return; fi
  local start=$SECONDS n
  while :; do
    n=$(field pending) || n=unknown
    if [ "$n" = 0 ]; then
      log "pending_jobs == 0 after $((SECONDS - start)) s" | tee -a "$OUT/facts.txt"
      return
    fi
    if [ $((SECONDS - start)) -ge "$PENDING_WAIT_S" ]; then
      log "pending_jobs=$n after ${PENDING_WAIT_S} s: giving up" >&2
      exit 1
    fi
    log "pending_jobs=$n, waiting"
    sleep 15
  done
}

fresh_container() {  # $1 = label for facts.txt, $2 = memory cap
  local t0
  run podman stop -t 60 --ignore "$CONTAINER"  # clean shutdown of the previous run's server
  run podman rm -f --ignore "$CONTAINER"
  t0=$(date +%s%N)
  run podman run -d --name "$CONTAINER" --memory "$2" -p "$PORT:8000" -v "$VOLUME:/data" \
    -e "ROOT_API_KEY=$API_KEY" "$IMAGE"
  wait_started "$1" "$t0"
  wait_pending
}

bench_run() {  # bench_run <run0|run1|...> <engine>
  local tag=$1 engine=$2
  fresh_container "$tag $engine" "$MEMORY"
  log "$tag $engine"
  run rm -f bench/results-partial.json
  run .venv/bin/python -u bench/bench.py --limit "$LIMIT" --caps-note "$CAPS" --host "$HOST_NOTE" \
    --engine "$engine" --fingerprint-dir "$STATE" --out "$OUT/$tag-$engine.md" "${EXTRA[@]}"
  EXTRA=()
  run mv bench/results-partial.json "$OUT/$tag-$engine.json"
}

engine_group() {  # engine_group <engine> <measured runs>: warm-up run0, then run1..runN
  local i
  for i in $(seq 0 "$2"); do bench_run "run$i" "$1"; done
}

detach_index() {  # the session's one IVF -> flat transition
  local start
  fresh_container "detach at $DETACH_MEMORY" "$DETACH_MEMORY"
  start=$SECONDS
  run curl -sf --max-time 900 -X DELETE -H "x-api-key: $API_KEY" "$URL/index"
  echo
  wait_pending
  if [ "${DRY_RUN:-0}" != 1 ] && [ "$(index_type)" != flat ]; then
    log "the detach did not leave a flat index: stopping" >&2
    exit 1
  fi
  log "IVF detach at $DETACH_MEMORY took $((SECONDS - start)) s" | tee -a "$OUT/facts.txt"
}

log "session $LABEL: image $IMAGE, bench checkout $SHA" | tee "$OUT/facts.txt"
{
  run uptime
  run free -m
  run grep -E '^(Cached|Buffers):' /proc/meminfo
  run podman ps -a
  run podman image inspect --format '{{.Id}} {{.Digest}}' "$IMAGE"
  run podman run --rm "$IMAGE" python -c "$VERSIONS"
} >> "$OUT/facts.txt" 2>&1 || true  # facts are informational: a missing one never stops the session

fresh_container "first start" "$MEMORY"
START_INDEX=$(index_type)
log "index at start: $START_INDEX" | tee -a "$OUT/facts.txt"
case "$START_INDEX" in
  ivf)
    engine_group raggio-ivf "$IVF_RUNS"
    detach_index
    engine_group raggio "$FLAT_RUNS"
    ;;
  flat)
    engine_group raggio "$FLAT_RUNS"
    engine_group raggio-ivf "$IVF_RUNS"  # run0 builds the index: index_build_s at 4 GiB
    ;;
  *)
    log "unexpected index state '$START_INDEX': stopping" >&2
    exit 1
    ;;
esac
run cp bench/bench.py "$OUT/"
log "session $LABEL done"
```

3g. Create `bench/dgx/compare.py`:

```python
"""Noise band and A/B table for DGX sessions (spec §6 "Noise band").

A session directory (~/raggio-bench/<label>/, written by session.sh) holds one
run<N>-<engine>.json per bench run, each {engine: {metric: value}}. run0 is the discarded
warm-up: its query metrics are ignored, but a one-shot metric it measured (the IVF build
of a flat-first session) counts. The measured runs are run1..runN (flat two, IVF three);
per metric the central value is their median and the noise band is max - min, never
narrower than the table's display resolution (1 MB, 0.1 ms, 0.001 recall).

One directory: every counted value, the median and the band. Two (baseline first): both
medians and bands and a verdict, "better" or "worse" only when the medians differ by more
than the wider band; "no band" when a side has a single value; "not claimable" for the
bench's cold-start metric, a warm restart (spec §6: cold start is claimed only true-cold).

Usage: python bench/dgx/compare.py <baseline-dir> [<candidate-dir>]
"""

import json
import re
import statistics
import sys
from pathlib import Path

# (results key, label, format, higher is better): same keys, labels and formats as bench.ROWS
METRICS = [
    ("ingest_s", "Ingest wall time (s)", "{:.0f}", False),
    ("ingest_vps", "Ingest throughput (vec/s)", "{:.0f}", True),
    ("index_build_s", "IVF index build (s)", "{:.0f}", False),
    ("mem_after_ingest_mb", "Memory after ingest (MB)", "{:.0f}", False),
    ("mem_under_load_mb", "Memory under query load (MB)", "{:.0f}", False),
    ("disk_mb", "Disk footprint (MB)", "{:.0f}", False),
    ("lat_p50", "Search p50 (ms)", "{:.1f}", False),
    ("lat_p95", "Search p95 (ms)", "{:.1f}", False),
    ("lat_p99", "Search p99 (ms)", "{:.1f}", False),
    ("qps_serial", "QPS serial", "{:.0f}", True),
    ("qps_concurrent", "QPS concurrent", "{:.0f}", True),
    ("lat_c_p95", "p95 under concurrency (ms)", "{:.1f}", False),
    ("lat_filtered_p50", "Filtered p50 (ms)", "{:.1f}", False),
    ("lat_filtered_p95", "Filtered p95 (ms)", "{:.1f}", False),
    ("recall_at_10", "Recall@10 vs exact", "{:.3f}", True),
    ("hybrid_p50", "Hybrid p50 (ms)", "{:.1f}", False),
    ("hybrid_p95", "Hybrid p95 (ms)", "{:.1f}", False),
    ("hybrid_p99", "Hybrid p99 (ms)", "{:.1f}", False),
    ("hybrid_qps_serial", "Hybrid QPS serial", "{:.1f}", True),
    ("hybrid_qps_concurrent", "Hybrid QPS concurrent", "{:.1f}", True),
    ("hybrid_text_hit_rate", "Hybrid text-hit@10", "{:.3f}", True),
    ("cold_start_s", "Cold start to first query (s)", "{:.1f}", False),
]
HIGHER_IS_BETTER = {key: hib for key, _, _, hib in METRICS}
RESOLUTION = {key: 10.0 ** -int(re.search(r"\.(\d)f", fmt).group(1)) for key, _, fmt, _ in METRICS}
ONE_SHOT = {"ingest_s", "ingest_vps", "index_build_s"}  # measured once, possibly by the warm-up
NOT_CLAIMABLE = {"cold_start_s"}
ENGINE_ORDER = ["raggio", "raggio-ivf", "weaviate"]
RUN_FILE = re.compile(r"run(\d+)-(raggio-ivf|raggio|weaviate)\.json")


def load(session_dir):
    """{engine: {run number: {metric: value}}} from a session directory."""
    runs = {}
    for p in sorted(Path(session_dir).iterdir()):
        m = RUN_FILE.fullmatch(p.name)
        if m:
            data = json.loads(p.read_text(encoding="utf-8"))
            runs.setdefault(m.group(2), {})[int(m.group(1))] = data.get(m.group(2), {})
    if not runs:
        raise SystemExit(f"{session_dir}: no run<N>-<engine>.json files")
    return runs


def values(engine_runs, key):
    """The values that count for one engine's metric, in run order."""
    return [engine_runs[n][key] for n in sorted(engine_runs)
            if key in engine_runs[n] and (n >= 1 or key in ONE_SHOT)]


def summary(vals):
    """(median, max - min) of the counted values."""
    return statistics.median(vals), max(vals) - min(vals)


def verdict(key, base, cand):
    if key in NOT_CLAIMABLE:
        return "not claimable"
    if len(base) < 2 or len(cand) < 2:
        return "no band"
    (base_mid, base_band), (cand_mid, cand_band) = summary(base), summary(cand)
    gain = cand_mid - base_mid
    if not HIGHER_IS_BETTER[key]:
        gain = -gain
    band = max(base_band, cand_band, RESOLUTION[key])
    if gain > band:
        return "better"
    if -gain > band:
        return "worse"
    return "within band"


def _rows(base, cand=None):
    """(engine, key, label, fmt, base values, cand values) for every metric either side has."""
    sessions = [base] + ([cand] if cand is not None else [])
    for engine in [e for e in ENGINE_ORDER if any(e in s for s in sessions)]:
        for key, label, fmt, _ in METRICS:
            b = values(base.get(engine, {}), key)
            c = values(cand.get(engine, {}), key) if cand is not None else []
            if b or c:
                yield engine, key, label, fmt, b, c


def _mid_band(fmt, vals):
    if not vals:
        return ["-", "-"]
    mid, band = summary(vals)
    return [fmt.format(mid), fmt.format(band) if len(vals) > 1 else "-"]


def render(base_dir, cand_dir=None):
    base = load(base_dir)
    cand = load(cand_dir) if cand_dir else None
    if cand is None:
        lines = [f"Noise band: {base_dir}", "",
                 "| Engine | Metric | runs | median | band |", "|---|---|---|---|---|"]
    else:
        lines = [f"A/B: baseline {base_dir} vs candidate {cand_dir}", "",
                 "| Engine | Metric | base median | base band | cand median | cand band | verdict |",
                 "|---|---|---|---|---|---|---|"]
    for engine, key, label, fmt, b, c in _rows(base, cand):
        if cand is None:
            cells = [engine, label, " / ".join(fmt.format(v) for v in b), *_mid_band(fmt, b)]
        else:
            cells = [engine, label, *_mid_band(fmt, b), *_mid_band(fmt, c),
                     verdict(key, b, c) if b and c else "-"]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def beyond_band(base_dir, cand_dir):
    """{"better": [(engine, key)], "worse": [(engine, key)]}: the rows outside the noise band."""
    out = {"better": [], "worse": []}
    for engine, key, _, _, b, c in _rows(load(base_dir), load(cand_dir)):
        v = verdict(key, b, c) if b and c else "-"
        if v in out:
            out[v].append((engine, key))
    return out


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    print(render(*sys.argv[1:]))
    if len(sys.argv) == 3:
        for name, rows in beyond_band(*sys.argv[1:]).items():
            print(f"\n{name} beyond the noise band: {len(rows)} row(s) {rows}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bench_dgx.py -q`
Expected: `30 passed`. On a host without bash, the 14 `needs_bash` tests skip instead.

Run: `uv run pytest -q`
Expected: `139 passed, 1 warning`

- [ ] **Step 5: Commit**

```bash
git add .gitattributes bench/dgx/lib.sh bench/dgx/deploy.sh bench/dgx/unpack.sh bench/dgx/setup.sh bench/dgx/session.sh bench/dgx/compare.py tests/test_bench_dgx.py
git commit -m "$(cat <<'EOF'
bench: commit the DGX runbook (deploy, session, noise band)

deploy.sh ships HEAD to gn100 and builds localhost/raggio:<sha>;
session.sh runs one image in the spec §6 order from fresh 4 GiB bench-tv
containers (only bench-tv is ever stopped or removed, --limit 2549619,
no --reingest, state under ~/raggio-bench); compare.py reports the
median and max - min band and the A/B verdicts.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Every row states its regime; thin concurrent QPS is not claimed; the IVF build can run twice (not executed in the sandbox)

This task implements spec §6's labelling rules on top of Task 3. It also adds the second IVF build that spec §3.1 A1 requires: "The IVF build-time gate builds **twice per arm** and compares medians, against the band."
- With `IVF_BUILDS=2`, a flat-start session runs its measured runs as before. Then it detaches the index in the transient 8 GiB container and runs `build2 raggio-ivf` at 4 GiB, which builds the index from flat again.
- The measured runs therefore keep the session's one IVF ↔ flat transition. The extra detach and build come after all of them.
- `compare.py` counts only `build2`'s one-shot build time, next to run0's. Its query metrics never count. The build row then has two values per side, a median and a band, and a verdict like every other row.
- An IVF-start session with `IVF_BUILDS=2` stops before its first bench run. It would need a second transition before the extra build.

Task 6 does not use `IVF_BUILDS`. Its equivalence gate does not gate the build time: baseline3 holds one build, and Task 6 already treats the build row's `no band` and `-` as non-failures. Only Task 10's Gate 1 is the IVF build-time gate of spec §3.1 A1.

This task lands in PR 1, so both of Task 10's arms can build twice: `a-main` runs `main`'s `session.sh` (PR 1 and Plan B merged), and `a-ref` runs PR 2's. None of it was run in the sandbox. The code, the RED and GREEN counts and the suite count are derived from Task 3's replayed state plus the 11 new tests.

**Files:**
- Modify: `bench/dgx/lib.sh:9-11`.
- Modify: `bench/dgx/session.sh`:
  - header lines 20-23;
  - after line 25 (`LABEL=`);
  - lines 46-49 (`FIELD` / `EXTRA`);
  - after `index_type`;
  - `fresh_container` lines 94-100;
  - line 121 (the `detach_index` comment);
  - lines 143-145 (after the facts block);
  - lines 148-162 (the `case "$START_INDEX"` block).
- Modify: `bench/dgx/compare.py`: the docstring, the constants after `RUN_FILE`, `load` and `values`, `regime()` after `load`, and `render`.
- Test: `tests/test_bench_dgx.py` (append).

**Interfaces:**
- Consumes: Task 3's `session.sh`, `lib.sh` and `compare.py`.
- Produces. Tasks 6 and 10 and plans C–G rely on these:
  - `session.sh` environment:
    - `FLAT_RUNS=<n>` (default 2);
    - `MEMORY_SWAP=<size>` (default empty = podman's default; `4g` = the swapless gate, applied to every 4 GiB start, never the 8 GiB detach);
    - `FIRST_START=host-warm|true-cold` (default `host-warm`; anything else exits 1 before the label directory is created);
    - `IVF_BUILDS=1|2` (default 1; anything else exits 1 before the label directory is created). With 2, a flat-start session ends with the 8 GiB detach and `build2 raggio-ivf`: a bench run at 4 GiB that finds a flat index and builds it, as run0 did, and writes `~/raggio-bench/<label>/build2-raggio-ivf.{md,json}`. The session still ends IVF. An IVF-start session with 2 exits 1 after the first start, before any bench run.
  - `~/raggio-bench/<label>/regime.json` = `{"page_cache": "host-warm", "first_start": …, "memory": "4g", "memory_swap": "<size>|host-default", "detach_memory": "8g"}`. Task 5 appends `"concurrency": "<n>"`.
  - `~/raggio-bench/<label>/image.json` = `{"python", "sqlite_version", "turbovec", "openblas_num_threads"}`, read inside the image under test. It is absent if the probe fails.
  - In `compare.py`:
    - `REGIME_KEYS`, and `SAME_REGIME` (`REGIME_KEYS` minus `first_start`);
    - `CLAIM_RUNS = {"qps_concurrent": 3}`;
    - `regime(session_dir) -> {key: str}`, where a missing label is `"unknown"`;
    - `BUILD_FILE = re.compile(r"build([1-9]\d*)-(raggio-ivf)\.json")`. `load` stores a `build<N>` file as run `-N` of `raggio-ivf`, and `values` counts it for the `ONE_SHOT` metrics only, after the runs. The build row of an `IVF_BUILDS=2` session therefore has two values (run0's, then build2's), a median, a band and a verdict. `values` is unchanged for sessions without a `build<N>` file, which is every Plan D and Plan G caller.
  - `render` prints `Regime (session): …`, or `Regime (baseline): …` and `Regime (candidate): …`, as the lines after the title.
    - When `SAME_REGIME` labels differ, it adds `The arms differ in <keys>: a re-baseline across them, not a same-regime A/B (spec §6).`
    - If any vector concurrent-QPS row has fewer than 3 counted runs on a side, the last line is `Reported, not claimed (spec §6 needs >= 3 runs per side; FLAT_RUNS=3): <engine> <label>, …`.
  - `verdict`, `summary` and `beyond_band` are unchanged. With two build values per side, the build row gets `better`, `within band` or `worse` like every other row, and `beyond_band` lists `('raggio-ivf', 'index_build_s')` when it is worse (Task 10's Gate 1, spec §3.1 A1). The table rows keep 7 cells (A/B) or 5 cells (one session).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench_dgx.py`:

```python


# ---- regime labels (Task 4): spec §6, every row states its regime ------------------------

REGIME = {"page_cache": "host-warm", "first_start": "host-warm", "memory": "4g",
          "memory_swap": "host-default", "detach_memory": "8g"}
BOOKWORM = {"python": "3.12.11", "sqlite_version": "3.40.1", "turbovec": "1.0.0",
            "openblas_num_threads": "unset"}
TRIXIE = {"python": "3.12.14", "sqlite_version": "3.53.1", "turbovec": "1.0.0",
          "openblas_num_threads": "1"}


def write_labels(d, regime=None, image=None):
    """regime.json and image.json as session.sh writes them."""
    if regime is not None:
        (d / "regime.json").write_text(json.dumps(regime))
    if image is not None:
        (d / "image.json").write_text(json.dumps(image))


def flat_session(d, lat):
    write_session(d, {"run1-raggio": {"lat_p50": lat[0]}, "run2-raggio": {"lat_p50": lat[1]}})


@needs_bash
def test_session_records_the_regime_labels(tmp_path):
    home, _ = dry_session(tmp_path)
    out = home / "s1"
    assert json.loads((out / "regime.json").read_text()) == REGIME
    assert json.loads((out / "image.json").read_text()) == {
        "python": "dry-run", "sqlite_version": "dry-run", "turbovec": "dry-run",
        "openblas_num_threads": "dry-run"}


@needs_bash
def test_swapless_gate_caps_swap_on_the_measured_runs_only(tmp_path):
    home, cmds = dry_session(tmp_path, ("MEMORY_SWAP", "4g"))
    starts = [c for c in cmds if c.startswith("podman run -d ")]
    # the first start and 4 IVF runs, the 8 GiB detach (podman's default swap), 3 flat runs
    assert [" --memory 4g --memory-swap 4g " in c for c in starts] == [True] * 5 + [False] + [True] * 3
    assert starts[5] == podman_run("8g")
    assert json.loads((home / "s1" / "regime.json").read_text())["memory_swap"] == "4g"
    runs = [c for c in cmds if c.startswith(BENCH_CMD)]
    assert len(runs) == 7 and all("Swap capped too: --memory-swap 4g." in r for r in runs)


@needs_bash
def test_first_start_regime_is_validated(tmp_path):
    home, raggio = tmp_path / "bench-home", tmp_path / "raggio"
    raggio.mkdir()
    (raggio / ".deployed-sha").write_text("abc1234\n")
    env = {**os.environ, "DRY_RUN": "1", "BENCH_HOME": home.as_posix(), "RAGGIO_DIR": raggio.as_posix()}
    session = [BASH, (DGX / "session.sh").resolve().as_posix()]
    r = subprocess.run([*session, "s1"], env={**env, "FIRST_START": "cold"}, capture_output=True, text=True)
    assert r.returncode != 0 and "use host-warm or true-cold" in r.stderr
    assert not (home / "s1").exists()
    subprocess.run([*session, "s2"], env={**env, "FIRST_START": "true-cold"}, capture_output=True, check=True)
    assert json.loads((home / "s2" / "regime.json").read_text())["first_start"] == "true-cold"


@needs_bash
def test_flat_runs_can_be_raised_for_a_concurrent_qps_claim(tmp_path):
    _, cmds = dry_session(tmp_path, ("FLAT_RUNS", "3"))
    assert [tag for _, tag, engine in bench_runs(cmds) if engine == "raggio"] == ["run0", "run1", "run2", "run3"]


def test_image_facts_snippet_reports_sqlite_and_blas_threads():
    import sqlite3
    text = (DGX / "session.sh").read_text(encoding="utf-8")
    (snippet,) = re.findall(r"^IMAGE_FACTS='(.*?)'$", text, re.S | re.M)
    out = subprocess.run([sys.executable, "-c", snippet], env={**os.environ, "OPENBLAS_NUM_THREADS": "1"},
                         capture_output=True, text=True, check=True).stdout
    facts = json.loads(out)
    assert sorted(facts) == ["openblas_num_threads", "python", "sqlite_version", "turbovec"]
    assert facts["sqlite_version"] == sqlite3.sqlite_version
    assert facts["openblas_num_threads"] == "1"


def test_compare_prints_each_sides_regime(tmp_path):
    base, cand = tmp_path / "main", tmp_path / "cand"
    flat_session(base, (23.0, 24.0))
    flat_session(cand, (23.5, 23.5))
    write_labels(base, REGIME, BOOKWORM)
    write_labels(cand, REGIME, BOOKWORM)
    assert compare.render(base).splitlines()[1] == (
        "Regime (session): page_cache=host-warm, first_start=host-warm, memory=4g, "
        "memory_swap=host-default, python=3.12.11, sqlite_version=3.40.1, openblas_num_threads=unset")
    ab = compare.render(base, cand).splitlines()
    assert ab[1].startswith("Regime (baseline): page_cache=host-warm, ")
    assert ab[2].startswith("Regime (candidate): page_cache=host-warm, ")
    assert not [ln for ln in ab if ln.startswith("The arms differ")]
    # a hand-assembled session without labels reads "unknown", never a guess
    (base / "regime.json").unlink()
    (base / "image.json").unlink()
    assert compare.regime(base) == {key: "unknown" for key in compare.REGIME_KEYS}


def test_compare_flags_arms_that_differ_in_regime(tmp_path):
    base, cand = tmp_path / "main", tmp_path / "cand"
    flat_session(base, (23.0, 24.0))
    flat_session(cand, (23.5, 23.5))
    write_labels(base, REGIME, BOOKWORM)
    write_labels(cand, REGIME, TRIXIE)
    assert ("The arms differ in python, sqlite_version, openblas_num_threads: a re-baseline across "
            "them, not a same-regime A/B (spec §6).") in compare.render(base, cand)
    write_labels(cand, {**REGIME, "memory_swap": "4g"}, BOOKWORM)  # swapless vs default swap
    assert "The arms differ in memory_swap: " in compare.render(base, cand)
    write_labels(cand, {**REGIME, "first_start": "true-cold"}, BOOKWORM)  # labels the first start only
    assert "The arms differ" not in compare.render(base, cand)
    assert compare.beyond_band(base, cand) == {"better": [], "worse": []}  # verdicts unchanged


def test_compare_reports_thin_concurrent_qps_as_not_claimed(tmp_path):
    two = {"run1-raggio": {"qps_concurrent": 129.0}, "run2-raggio": {"qps_concurrent": 131.0}}
    three = {**two, "run3-raggio": {"qps_concurrent": 200.0}}
    thin, full, cand = tmp_path / "thin", tmp_path / "full", tmp_path / "cand"
    write_session(thin, two)
    write_session(full, three)
    write_session(cand, three)
    note = "Reported, not claimed (spec §6 needs >= 3 runs per side; FLAT_RUNS=3): raggio QPS concurrent"
    assert compare.render(thin).splitlines()[-1] == note
    assert compare.render(thin, cand).splitlines()[-1] == note
    assert "Reported, not claimed" not in compare.render(full, cand)


# ---- the second IVF build (Task 4): spec §3.1 A1, the build gate builds twice per arm ----

@needs_bash
def test_a_second_ivf_build_runs_after_every_measured_run(tmp_path):
    home, cmds = dry_session(tmp_path, ("DRY_INDEX", "flat"), ("IVF_BUILDS", "2"))
    out = (home / "s1").as_posix()
    runs = [(i, *re.search(r"--out \S+/(\w+)-(raggio-ivf|raggio)\.md", c).groups())
            for i, c in enumerate(cmds) if c.startswith(BENCH_CMD)]
    assert [(tag, engine) for _, tag, engine in runs] == [
        ("run0", "raggio"), ("run1", "raggio"), ("run2", "raggio"),
        ("run0", "raggio-ivf"), ("run1", "raggio-ivf"), ("run2", "raggio-ivf"), ("run3", "raggio-ivf"),
        ("build2", "raggio-ivf")]
    # the measured runs keep the one transition (run0's build); the extra detach comes after them
    (d,) = [i for i, c in enumerate(cmds) if "-X DELETE" in c]
    assert runs[6][0] < d < runs[7][0]
    assert cmds[d - 5:d + 1] == [STOP, RM, podman_run("8g"), STARTED, IDLE, DETACH]
    assert [c for c in cmds if "--memory 8g" in c] == [podman_run("8g")]
    # build2 builds from flat in a fresh 4 GiB container, like any bench run
    b = runs[7][0]
    assert cmds[b - 6:b] == [STOP, RM, podman_run("4g"), STARTED, IDLE, "rm -f bench/results-partial.json"]
    assert "--engine raggio-ivf " in cmds[b] and cmds[b].endswith(f"--out {out}/build2-raggio-ivf.md")
    assert cmds[b + 1] == f"mv bench/results-partial.json {out}/build2-raggio-ivf.json"


@needs_bash
def test_ivf_builds_is_validated_and_needs_a_flat_start(tmp_path):
    home, raggio = tmp_path / "bench-home", tmp_path / "raggio"
    raggio.mkdir()
    (raggio / ".deployed-sha").write_text("abc1234\n")
    env = {**os.environ, "DRY_RUN": "1", "BENCH_HOME": home.as_posix(), "RAGGIO_DIR": raggio.as_posix()}
    session = [BASH, (DGX / "session.sh").resolve().as_posix()]
    r = subprocess.run([*session, "s1"], env={**env, "IVF_BUILDS": "3"}, capture_output=True, text=True)
    assert r.returncode != 0 and "IVF_BUILDS=3: use 1 or 2" in r.stderr
    assert not (home / "s1").exists()
    # an IVF start would need a second IVF <-> flat transition before the extra build
    r = subprocess.run([*session, "s2"], env={**env, "IVF_BUILDS": "2", "DRY_INDEX": "ivf"},
                       capture_output=True, text=True)
    assert r.returncode != 0 and "IVF_BUILDS=2 needs a flat start" in r.stderr
    assert BENCH_CMD not in r.stdout


def ivf_builds(d, first, second):
    """An IVF_BUILDS=2 session: run0 builds the index, build2 builds it again after the runs."""
    write_session(d, {
        "run0-raggio-ivf": {"index_build_s": first, "lat_p50": 15.5},
        "run1-raggio-ivf": {"lat_p50": 11.0}, "run2-raggio-ivf": {"lat_p50": 12.0},
        "build2-raggio-ivf": {"index_build_s": second, "lat_p50": 30.0}})


def test_the_second_ivf_build_counts_for_the_build_row_only(tmp_path):
    base, cand, cand2 = tmp_path / "main", tmp_path / "cand", tmp_path / "cand2"
    ivf_builds(base, 213.0, 221.0)
    ivf_builds(cand, 227.0, 233.0)
    ivf_builds(cand2, 215.0, 225.0)
    runs = compare.load(base)["raggio-ivf"]
    assert compare.values(runs, "index_build_s") == [213.0, 221.0]  # run0's build, then build2's
    assert compare.values(runs, "lat_p50") == [11.0, 12.0]  # build2's queries never count
    assert "| raggio-ivf | IVF index build (s) | 213 / 221 | 217 | 8 |" in compare.render(base)
    # medians against the band (spec §3.1 A1): 230 - 217 = 13 is beyond the wider band of 8
    assert "| raggio-ivf | IVF index build (s) | 217 | 8 | 230 | 6 | worse |" in compare.render(base, cand)
    assert compare.beyond_band(base, cand) == {"better": [], "worse": [("raggio-ivf", "index_build_s")]}
    # 220 - 217 = 3 is inside the candidate's band of 10: no single-build tolerance decides it
    assert "| raggio-ivf | IVF index build (s) | 217 | 8 | 220 | 10 | within band |" in compare.render(base, cand2)
    assert compare.beyond_band(base, cand2) == {"better": [], "worse": []}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_bench_dgx.py -q`

Expected (derived, not executed): `11 failed, 30 passed`.
- `test_session_records_the_regime_labels`: `FileNotFoundError` for `regime.json`.
- The swapless test: its first `assert` fails because no start carries `--memory-swap`.
- The `FIRST_START` test: `assert r.returncode != 0` fails, because a dry run ignores the variable.
- The `FLAT_RUNS` test: the hard-coded `FLAT_RUNS=2` gives `run0`–`run2` only.
- The snippet test: `ValueError: not enough values to unpack (expected 1, got 0)`.
- The three regime compare tests: `AssertionError` on the missing regime lines and the missing note.
- `test_a_second_ivf_build_runs_after_every_measured_run`: `AssertionError` on the run list, which ends at raggio-ivf `run3` because the session ignores `IVF_BUILDS`.
- `test_ivf_builds_is_validated_and_needs_a_flat_start`: `assert r.returncode != 0` fails for `IVF_BUILDS=3`.
- `test_the_second_ivf_build_counts_for_the_build_row_only`: `AssertionError`, `[213.0] == [213.0, 221.0]`: Task 3's `load` skips `build2-raggio-ivf.json`.

- [ ] **Step 3: Minimal implementation**

3a. `bench/dgx/lib.sh`: replace lines 9-11

```bash
MEMORY=4g             # every measured run
DETACH_MEMORY=8g      # IVF -> flat detach: refused at 4 GiB until plan C releases build memory
FLAT_RUNS=2           # measured runs after the warm-up run0: flat band = run1 vs run2
```

with

```bash
MEMORY=4g             # every measured run
MEMORY_SWAP="${MEMORY_SWAP:-}"           # empty: podman's default swap; 4g = the swapless memory gate (spec §6, D13)
FIRST_START="${FIRST_START:-host-warm}"  # host-warm, or true-cold after a reboot or an fadvise eviction (D12)
DETACH_MEMORY=8g      # IVF -> flat detach: refused at 4 GiB until plan C releases build memory
FLAT_RUNS="${FLAT_RUNS:-2}"  # measured flat runs after run0; 3 when a concurrent-QPS row is claimed (spec §6)
IVF_BUILDS="${IVF_BUILDS:-1}"  # IVF builds of a flat-start session; 2 = one more after the measured runs (spec §3.1 A1)
```

3b. `bench/dgx/session.sh` has eight edits.

(1) Replace the header lines 20-23

```bash
# IMAGE=<ref>: measure another image (default localhost/raggio:<deployed sha>).
# ADOPT=1: pass --adopt to the first run (first session on a volume with no fingerprint yet).
# DRY_RUN=1: print the commands without running them; DRY_INDEX=ivf|flat is the index
# state a dry run assumes (default ivf).
```

with

```bash
# IMAGE=<ref>: measure another image (default localhost/raggio:<deployed sha>).
# ADOPT=1: pass --adopt to the first run (first session on a volume with no fingerprint yet).
# FLAT_RUNS=3: a third measured flat run, needed before a vector concurrent-QPS row is claimed.
# MEMORY_SWAP=4g: the swapless memory gate (--memory-swap on every 4 GiB start, not the detach).
# FIRST_START=true-cold: label the first start true-cold (host rebooted, or the volume's files
# fadvise-evicted, before the session); default host-warm.
# IVF_BUILDS=2 (flat start only): after every measured run, detach at 8 GiB and build the IVF
# index once more at 4 GiB (build2-raggio-ivf); compare.py counts only its build time (spec §3.1 A1).
# regime.json (page cache, first start, memory, swap) and image.json (python, sqlite,
# turbovec, OPENBLAS_NUM_THREADS inside the image) label every row; compare.py prints them.
# DRY_RUN=1: print the commands without running them; DRY_INDEX=ivf|flat is the index
# state a dry run assumes (default ivf).
```

(2) After line 25, `LABEL="${1:?usage: session.sh <label>}"`, insert:

```bash
case "$FIRST_START" in
  host-warm|true-cold) ;;
  *) echo "FIRST_START=$FIRST_START: use host-warm or true-cold" >&2; exit 1 ;;
esac
case "$IVF_BUILDS" in
  1|2) ;;
  *) echo "IVF_BUILDS=$IVF_BUILDS: use 1 or 2" >&2; exit 1 ;;
esac
if [ -n "$MEMORY_SWAP" ]; then CAPS="$CAPS Swap capped too: --memory-swap $MEMORY_SWAP."; fi
```

(3) Replace lines 46-49

```bash
FIELD='import json, sys
info = json.load(sys.stdin)
print(info["pending_jobs"] if sys.argv[1] == "pending" else (info.get("index") or {}).get("type"))'
EXTRA=()
```

with

```bash
FIELD='import json, sys
info = json.load(sys.stdin)
print(info["pending_jobs"] if sys.argv[1] == "pending" else (info.get("index") or {}).get("type"))'
IMAGE_FACTS='import json, os, sys, sqlite3, importlib.metadata as m
print(json.dumps({"python": sys.version.split()[0], "sqlite_version": sqlite3.sqlite_version, "turbovec": m.version("turbovec"), "openblas_num_threads": os.environ.get("OPENBLAS_NUM_THREADS", "unset")}))'
EXTRA=()
```

(4) After the `index_type` function (the line `}` that closes it, after `  field index`), insert:

```bash

image_facts() {  # the image's python, sqlite, turbovec and BLAS threads as JSON (spec §6, D15, D16)
  if [ "${DRY_RUN:-0}" = 1 ]; then
    echo '{"python": "dry-run", "sqlite_version": "dry-run", "turbovec": "dry-run", "openblas_num_threads": "dry-run"}'
    return
  fi
  podman run --rm "$IMAGE" python -c "$IMAGE_FACTS"
}
```

(5) In `fresh_container`, replace

```bash
  local t0
  run podman stop -t 60 --ignore "$CONTAINER"  # clean shutdown of the previous run's server
  run podman rm -f --ignore "$CONTAINER"
  t0=$(date +%s%N)
  run podman run -d --name "$CONTAINER" --memory "$2" -p "$PORT:8000" -v "$VOLUME:/data" \
```

with

```bash
  local t0 swap=()
  # the swapless gate caps the measured 4 GiB starts; the transient 8 GiB detach keeps the default
  if [ -n "$MEMORY_SWAP" ] && [ "$2" = "$MEMORY" ]; then swap=(--memory-swap "$MEMORY_SWAP"); fi
  run podman stop -t 60 --ignore "$CONTAINER"  # clean shutdown of the previous run's server
  run podman rm -f --ignore "$CONTAINER"
  t0=$(date +%s%N)
  run podman run -d --name "$CONTAINER" --memory "$2" "${swap[@]}" -p "$PORT:8000" -v "$VOLUME:/data" \
```

An empty `swap` array adds no words, so the printed command without `MEMORY_SWAP` is byte-identical to Task 3's. Task 3's tests keep passing.

(6) Replace

```bash
} >> "$OUT/facts.txt" 2>&1 || true  # facts are informational: a missing one never stops the session

fresh_container "first start" "$MEMORY"
```

with

```bash
} >> "$OUT/facts.txt" 2>&1 || true  # facts are informational: a missing one never stops the session
image_facts > "$OUT/image.json" || { rm -f "$OUT/image.json"; log "no image facts: compare.py reads them as unknown" >&2; }
# every measured run follows the first start, so it reads a host-warm page cache
printf '{"page_cache": "host-warm", "first_start": "%s", "memory": "%s", "memory_swap": "%s", "detach_memory": "%s"}\n' \
  "$FIRST_START" "$MEMORY" "${MEMORY_SWAP:-host-default}" "$DETACH_MEMORY" > "$OUT/regime.json"

fresh_container "first start ($FIRST_START)" "$MEMORY"
```

(7) Replace the `detach_index` line

```bash
detach_index() {  # the session's one IVF -> flat transition
```

with

```bash
detach_index() {  # IVF -> flat: the session's one transition, or the detach before build2 (IVF_BUILDS=2)
```

(8) In the `case "$START_INDEX" in` block, replace the `ivf)` and `flat)` branches

```bash
  ivf)
    engine_group raggio-ivf "$IVF_RUNS"
    detach_index
    engine_group raggio "$FLAT_RUNS"
    ;;
  flat)
    engine_group raggio "$FLAT_RUNS"
    engine_group raggio-ivf "$IVF_RUNS"  # run0 builds the index: index_build_s at 4 GiB
    ;;
```

with

```bash
  ivf)
    if [ "$IVF_BUILDS" = 2 ]; then
      log "IVF_BUILDS=2 needs a flat start (run0 builds, then build2 once more): leave the volume flat first" >&2
      exit 1
    fi
    engine_group raggio-ivf "$IVF_RUNS"
    detach_index
    engine_group raggio "$FLAT_RUNS"
    ;;
  flat)
    engine_group raggio "$FLAT_RUNS"
    engine_group raggio-ivf "$IVF_RUNS"  # run0 builds the index: index_build_s at 4 GiB
    if [ "$IVF_BUILDS" = 2 ]; then
      # after every measured run, so they keep the one transition; only build2's build time counts
      detach_index
      bench_run build2 raggio-ivf  # finds a flat index and builds it again at 4 GiB
    fi
    ;;
```

With the default `IVF_BUILDS=1`, the printed commands are the same as Task 3's, so Task 3's session tests keep passing.

3c. `bench/dgx/compare.py` has six edits.

(1) In the docstring, replace

```
bench's cold-start metric, a warm restart (spec §6: cold start is claimed only true-cold).
```

with

```
bench's cold-start metric, a warm restart (spec §6: cold start is claimed only true-cold).

Each side's regime (regime.json and image.json from session.sh, "unknown" when absent) is
printed above the table. Arms that differ in a label of the measured rows get a line saying
the A/B is a re-baseline, not a same-regime comparison, and a vector concurrent-QPS row with
fewer than 3 counted runs on a side is listed as reported, not claimed (spec §6).

A build<N>-raggio-ivf.json (session.sh IVF_BUILDS=2) is an extra IVF build after the measured
runs: only its one-shot metrics count, after run0's, so the build row gets two values per
side, a median and a band (spec §3.1 A1).
```

(2) After the line `RUN_FILE = re.compile(r"run(\d+)-(raggio-ivf|raggio|weaviate)\.json")`, insert:

```python
BUILD_FILE = re.compile(r"build([1-9]\d*)-(raggio-ivf)\.json")  # IVF_BUILDS=2: a build after the runs
# spec §6: every row states its page-cache regime, memory cap, sqlite3.sqlite_version and
# OPENBLAS_NUM_THREADS; the arms of one A/B share them (the first start is not a measured row)
REGIME_KEYS = ["page_cache", "first_start", "memory", "memory_swap", "python", "sqlite_version",
               "openblas_num_threads"]
SAME_REGIME = [k for k in REGIME_KEYS if k != "first_start"]
CLAIM_RUNS = {"qps_concurrent": 3}  # vector concurrent QPS is bimodal: >= 3 runs per side
```

(3) After the `load` function, insert:

```python


def regime(session_dir):
    """{label: value} from regime.json and image.json; a missing label reads "unknown"."""
    labels = {}
    for name in ("regime.json", "image.json"):
        p = Path(session_dir) / name
        if p.exists():
            labels.update(json.loads(p.read_text(encoding="utf-8")))
    return {k: str(labels.get(k, "unknown")) for k in REGIME_KEYS}
```

(4) Replace the whole `render` function with `_regime_line`, `_thin` and the new `render`:

```python
def _regime_line(side, labels):
    return f"Regime ({side}): " + ", ".join(f"{k}={labels[k]}" for k in REGIME_KEYS)


def _thin(key, *sides):
    """A claimed metric with fewer counted runs than spec §6 needs on some side."""
    return key in CLAIM_RUNS and any(0 < len(v) < CLAIM_RUNS[key] for v in sides)


def render(base_dir, cand_dir=None):
    base = load(base_dir)
    cand = load(cand_dir) if cand_dir else None
    if cand is None:
        lines = [f"Noise band: {base_dir}", _regime_line("session", regime(base_dir)), "",
                 "| Engine | Metric | runs | median | band |", "|---|---|---|---|---|"]
    else:
        base_regime, cand_regime = regime(base_dir), regime(cand_dir)
        lines = [f"A/B: baseline {base_dir} vs candidate {cand_dir}",
                 _regime_line("baseline", base_regime), _regime_line("candidate", cand_regime)]
        differ = [k for k in SAME_REGIME if base_regime[k] != cand_regime[k]]
        if differ:
            lines.append(f"The arms differ in {', '.join(differ)}: a re-baseline across them, "
                         "not a same-regime A/B (spec §6).")
        lines += ["", "| Engine | Metric | base median | base band | cand median | cand band | verdict |",
                  "|---|---|---|---|---|---|---|"]
    thin = []
    for engine, key, label, fmt, b, c in _rows(base, cand):
        if cand is None:
            cells = [engine, label, " / ".join(fmt.format(v) for v in b), *_mid_band(fmt, b)]
        else:
            cells = [engine, label, *_mid_band(fmt, b), *_mid_band(fmt, c),
                     verdict(key, b, c) if b and c else "-"]
        lines.append("| " + " | ".join(cells) + " |")
        if _thin(key, b, c):
            thin.append(f"{engine} {label}")
    if thin:
        lines += ["", "Reported, not claimed (spec §6 needs >= 3 runs per side; FLAT_RUNS=3): "
                  + ", ".join(thin)]
    return "\n".join(lines)
```

(5) Replace the whole `load` function with:

```python
def load(session_dir):
    """{engine: {run number: {metric: value}}} from a session directory. A build<N> file
    (IVF_BUILDS=2) is stored as run -N: only its one-shot metrics count (values)."""
    runs = {}
    for p in sorted(Path(session_dir).iterdir()):
        m, sign = RUN_FILE.fullmatch(p.name), 1
        if m is None:
            m, sign = BUILD_FILE.fullmatch(p.name), -1
        if m:
            data = json.loads(p.read_text(encoding="utf-8"))
            runs.setdefault(m.group(2), {})[sign * int(m.group(1))] = data.get(m.group(2), {})
    if not runs:
        raise SystemExit(f"{session_dir}: no run<N>-<engine>.json files")
    return runs
```

(6) Replace the whole `values` function with:

```python
def values(engine_runs, key):
    """The values that count for one engine's metric: the runs in order, then the extra builds."""
    return [engine_runs[n][key] for n in sorted(engine_runs, key=lambda n: (n < 0, abs(n)))
            if key in engine_runs[n] and (n >= 1 or key in ONE_SHOT)]
```

A negative run number fails `n >= 1`, so a `build<N>` file adds only `ONE_SHOT` values. Without one, the order is Task 3's.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bench_dgx.py -q`
Expected (derived): `41 passed`.

Run: `uv run pytest -q`
Expected (derived): `150 passed, 1 warning`.

- [ ] **Step 5: Commit**

```bash
git add bench/dgx/lib.sh bench/dgx/session.sh bench/dgx/compare.py tests/test_bench_dgx.py
git commit -m "$(cat <<'EOF'
bench: label every DGX row with its regime (spec §6)

session.sh writes regime.json (page cache, first start, memory, swap) and
image.json (python, sqlite, turbovec, OPENBLAS_NUM_THREADS read inside the
image); MEMORY_SWAP=4g runs the swapless gate on the measured starts,
FLAT_RUNS=3 gives concurrent QPS its third run. compare.py prints both
sides' regime, flags arms that differ, and lists thin concurrent-QPS rows
as reported, not claimed.

IVF_BUILDS=2 builds the IVF index a second time after a flat-start
session's measured runs (8 GiB detach, build2-raggio-ivf at 4 GiB), and
compare.py counts only that run's build time, so the build row gets a
median and a band (spec §3.1 A1).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: The first hybrid pass, the concurrent hybrid p99 and server CPU per query are reported; the `CONCURRENCY` knob (not executed in the sandbox)

Spec §6 says: "Report the first hybrid pass after a load separately: its first ~10 queries take 470–980 ms." It also says: "Report the concurrent phase as repeating the same 500 queries (warm caches)." bench.py's serial hybrid phase is the first text query after the container start: the vector, concurrent and filtered phases before it never touch FTS5. So its first 10 queries are that warm-up, and its p99 includes them.

Spec §4.2 names two more things for A to own, which Plan G reads and sets:
- **Bench rows for §3.1 G5.**
  - `hybrid_c_p99`, "Hybrid p99 under concurrency (ms)": the p99 of the hybrid concurrent phase. G5's p99 guard reads it; bench.py's `hybrid_p99` is a serial row.
  - `cpu_ms_per_q_c`, "CPU per query under concurrency (ms)", and `hybrid_cpu_ms_per_q_c`, "Hybrid CPU per query under concurrency (ms)". Each is the server container's cgroup `cpu.stat` `usage_usec` delta over that concurrent phase, divided by the phase's requests. The cgroup is found as DGX probe p4 did: `/sys/fs/cgroup` plus `podman inspect <container> --format '{{.State.CgroupPath}}'`. p4 measured 50.8–56.8 ms per vector query in the fast state at c=8, and 115.9–117.6 ms in the slow state; G5 uses this row to classify each flat run's state.
  - Where `cpu.stat` is not readable (no `--cpu-container`, a laptop, a failed inspect or read), the CPU rows are absent, never 0: no key in the results JSON, a `—` cell in the report for an engine without one, and no report row at all when no engine has one. bench.py logs one line saying why.
- **Session knob for §3.1 G7.** `CONCURRENCY` is read from the environment like `FLAT_RUNS`, with default 8. `session.sh` passes it to every bench.py run as `--concurrency "$CONCURRENCY"`, together with `--cpu-container "$CONTAINER"`. G's c=16 sessions set `CONCURRENCY=16`. `regime.json` records it, so compare.py flags arms that ran at different concurrency.

This task adds five keys, a flag, a knob, a regime label and a note. It changes no existing key, so baseline3 and every Task 3/4 session stay comparable: the new rows read `-` on a side that lacks them, and they get no verdict.

This task was not run in the sandbox. The counts are derived from Task 4's state plus 11 tests (2 for the first hybrid pass, 9 for G5 and G7), and 2 Task 4 tests that gain the `concurrency` label. The two-file RED and GREEN counts (Steps 2 and 4) were replayed on the workstation, in a scratch directory, against `bench.py`, `bench_reuse.py`, `compare.py` and the `bench/dgx/*.sh` scripts assembled from this plan's text for Tasks 1–5. The suite count is derived.

**Files:**
- Modify: `bench/bench.py`:
  - after `podman_disk` (lines 127-130);
  - after `pct` (lines 325-326);
  - the concurrent phase (lines 413-417);
  - the hybrid phase (lines 424-427);
  - the hybrid concurrent phase (lines 434-436);
  - `ROWS` (lines 450, 453 and 455);
  - the Notes in `report` (lines 475-476);
  - `build_parser`, after `--concurrency` (Task 2's parser).
- Modify: `bench/dgx/compare.py`: `METRICS`, after the `lat_c_p95`, `hybrid_p99` and `hybrid_qps_concurrent` lines, and Task 4's `REGIME_KEYS`.
- Modify: `bench/dgx/lib.sh`: after Task 4's `FLAT_RUNS` line.
- Modify: `bench/dgx/session.sh`: `bench_run`'s bench.py line, and Task 4's `regime.json` `printf`.
- Test: `tests/test_bench_harness.py` (append).
- Test: `tests/test_bench_dgx.py` (append, and two Task 4 tests gain the `concurrency` label).

**Interfaces:**
- Consumes: Task 2's `bench.py` (`build_parser`, and `bench_engine`'s head with `prepare_engine` and `engine_state`), Task 3's `compare.METRICS` and `bench_run`, and Task 4's `lib.sh`, `regime.json` and `REGIME_KEYS`. `test_compare_covers_every_bench_metric` pins `METRICS` to `bench.ROWS` in order, label and format, so both files change together.
- Produces:
  - `bench.hybrid_first_pass(lat_h, n=10) -> dict`. It returns `{"hybrid_first10_max_ms": max(lat_h[:n]), "hybrid_p99_after10": pct(lat_h[n:], 99)}`, or `{}` when there are fewer than `n + 2` latencies (`statistics.quantiles` needs 2 points).
  - "First 10, slowest" is the definition Plan E's hybrid driver uses (`first10_max`). A and E report the same quantity.
  - `hybrid_p99_after10` is the warm-pass p99 that spec §7's prune-arm gate names ("hybrid p99 (warm pass)").
  - Two new `ROWS` entries and two new `compare.METRICS` entries, straight after `hybrid_p99`. Both are lower-is-better, and Task 3's band and verdict rules apply unchanged.
  - The report's Notes gain two sentences: the concurrent phases repeat the serial queries, and the first hybrid queries include the text index's warm-up.
  - `bench.cpu_stat_path(container) -> str`: `/sys/fs/cgroup` + `podman inspect <container> --format '{{.State.CgroupPath}}'` + `/cpu.stat`. It raises `OSError` when podman is missing, the inspect fails or prints no absolute path.
  - `bench.read_usage_usec(path) -> int`: the `usage_usec` line of a cgroup v2 `cpu.stat`. It raises `OSError` when the file or the line is missing.
  - `async bench.cpu_phase(name, key, container, phase) -> (phase's result, dict)`. It reads `usage_usec` just before and just after `await phase()`. The dict is `{key: delta / 1000 / requests}` (ms per request; requests = `len(result[0])`), or `{}` with one printed line `[<name>] <key> absent: <why>` when `container` is empty, is not `ENGINES[name]["container"]`, or a read fails. It never returns 0 or `None` for a missing read.
  - `bench.py --cpu-container NAME` (default none): the container whose cgroup CPU the two concurrent phases read. `session.sh` passes `"$CONTAINER"` (`bench-tv`).
  - Three new result keys and `ROWS` entries, each with its `compare.METRICS` entry in the same place, all lower-is-better:
    - `cpu_ms_per_q_c`, "CPU per query under concurrency (ms)", after `lat_c_p95`;
    - `hybrid_c_p99`, "Hybrid p99 under concurrency (ms)", then `hybrid_cpu_ms_per_q_c`, "Hybrid CPU per query under concurrency (ms)", after `hybrid_qps_concurrent`.
  - `hybrid_c_p99` is present in every run. The two CPU keys are present only when the reads succeed. compare.py already handles a row that one or both sides lack: `_rows` skips a row neither side has, a one-sided row prints `-` cells and the verdict `-`, and `beyond_band` ignores `-`.
  - `session.sh` environment: `CONCURRENCY=<n>` (default 8), passed to every bench.py run as `--concurrency "$CONCURRENCY" --cpu-container "$CONTAINER"`. It is not validated, like `FLAT_RUNS`.
  - `regime.json` gains `"concurrency": "<n>"` after `detach_memory`, and `compare.REGIME_KEYS` gains `concurrency` after `memory_swap`. `SAME_REGIME` follows, so arms at different concurrency get Task 4's `The arms differ in concurrency: …` line. A session directory without the label reads `unknown`, as Task 4 does for every label.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench_harness.py`:

```python


# ---- first hybrid pass (Task 5): spec §6 reports it separately ----------------------------

def test_first_hybrid_pass_is_reported_separately():
    lat = [900.0, 700.0, 600.0, 500.0, 480.0, 300.0, 200.0, 100.0, 50.0, 20.0] + [10.0] * 90
    assert bench.hybrid_first_pass(lat) == {"hybrid_first10_max_ms": 900.0, "hybrid_p99_after10": 10.0}
    assert bench.hybrid_first_pass(lat[:11]) == {}  # too short for a p99 of the rest
    keys = [key for _, key, _ in bench.ROWS]
    at = keys.index("hybrid_p99")
    assert keys[at + 1: at + 3] == ["hybrid_first10_max_ms", "hybrid_p99_after10"]


def test_report_says_the_concurrent_phase_repeats_the_queries():
    text = bench.report({"raggio": {"hybrid_first10_max_ms": 612.3}}, bench.build_parser().parse_args([]), 1000)
    assert "| Hybrid first 10 queries, slowest (ms) | 612.3 |" in text
    assert "| Hybrid p99 without the first 10 (ms) | — |" in text
    assert "The concurrent phases repeat the serial phases' queries (warm caches)." in text


# ---- spec §3.1 G5 rows (Task 5): the concurrent hybrid p99 and the server CPU per query ----

import subprocess

N = 100  # requests per phase in these tests


def cpu_stat(path, usage_usec):
    """A cgroup v2 cpu.stat as the kernel writes it."""
    path.write_text(f"usage_usec {usage_usec}\nuser_usec {usage_usec // 2}\n"
                    f"system_usec {usage_usec // 2}\nnr_periods 0\nnr_throttled 0\nthrottled_usec 0\n")


def test_cpu_stat_path_is_the_containers_cgroup(monkeypatch):
    seen = []

    def inspect(stdout, code=0):
        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")
        monkeypatch.setattr(bench.subprocess, "run", run)

    inspect("/user.slice/libpod-abc123.scope\n")
    assert bench.cpu_stat_path("bench-tv") == "/sys/fs/cgroup/user.slice/libpod-abc123.scope/cpu.stat"
    assert seen == [["podman", "inspect", "bench-tv", "--format", "{{.State.CgroupPath}}"]]
    inspect("", 125)  # no such container
    with pytest.raises(OSError, match="gave no cgroup path"):
        bench.cpu_stat_path("bench-tv")

    def no_podman(cmd, **kw):
        raise FileNotFoundError("podman")

    monkeypatch.setattr(bench.subprocess, "run", no_podman)
    with pytest.raises(OSError):
        bench.cpu_stat_path("bench-tv")


def test_read_usage_usec_reads_cpu_stat(tmp_path):
    stat = tmp_path / "cpu.stat"
    cpu_stat(stat, 123_456_789)
    assert bench.read_usage_usec(stat) == 123_456_789
    stat.write_text("user_usec 1\nsystem_usec 2\n")
    with pytest.raises(OSError, match="no usage_usec line"):
        bench.read_usage_usec(stat)
    with pytest.raises(OSError):  # a laptop: no such cgroup file
        bench.read_usage_usec(tmp_path / "missing" / "cpu.stat")


def test_cpu_rows_are_absent_never_zero(monkeypatch, tmp_path, capsys):
    stat = tmp_path / "cpu.stat"
    monkeypatch.setattr(bench, "cpu_stat_path", lambda container: str(stat))

    def phase(usec=0, delete=False):
        async def run():
            if delete:
                stat.unlink()
            elif usec:
                cpu_stat(stat, bench.read_usage_usec(stat) + usec)
            return [5.0] * N, 1.0, [["r0"]] * N
        return run

    def measure(container, run):
        out, cpu = asyncio.run(bench.cpu_phase("raggio", "cpu_ms_per_q_c", container, run))
        assert out[0] == [5.0] * N  # the phase's own result, untouched
        return cpu

    cpu_stat(stat, 1_000_000)
    assert measure("bench-tv", phase(usec=50_800 * N)) == {"cpu_ms_per_q_c": pytest.approx(50.8)}
    assert capsys.readouterr().out == ""
    assert measure(None, phase()) == {}  # no --cpu-container
    assert measure("bench-wv", phase()) == {}  # not the engine's container
    assert measure("bench-tv", phase(delete=True)) == {}  # cpu.stat went away mid-phase
    assert measure("bench-tv", phase()) == {}  # no cpu.stat at all
    stat.write_text("user_usec 1\n")
    assert measure("bench-tv", phase()) == {}  # no usage_usec line

    def no_cgroup(container):
        raise OSError(f"podman inspect {container} gave no cgroup path (exit 125)")

    monkeypatch.setattr(bench, "cpu_stat_path", no_cgroup)
    assert measure("bench-tv", phase()) == {}
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 6 and all(ln.startswith("[raggio] cpu_ms_per_q_c absent: ") for ln in lines)
    assert lines[0] == "[raggio] cpu_ms_per_q_c absent: no --cpu-container"
    assert lines[1] == "[raggio] cpu_ms_per_q_c absent: --cpu-container bench-wv is not raggio's container"
    assert lines[5] == "[raggio] cpu_ms_per_q_c absent: podman inspect bench-tv gave no cgroup path (exit 125)"


def test_bench_engine_measures_both_concurrent_phases(monkeypatch, tmp_path, capsys):
    stat, usage = tmp_path / "cpu.stat", [1_000_000]
    cpu_stat(stat, usage[0])

    async def run_queries(engine, queries, concurrency, headers, filt=None, k=10, texts=None):
        n = len(queries)
        if concurrency > 1:  # server CPU per request, p4's c=8 figures: 50.8 ms flat, 343.0 hybrid
            usage[0] += (343_000 if texts else 50_800) * n
            cpu_stat(stat, usage[0])
        lat = [10.0] * (n - 1) + [500.0] if texts and concurrency > 1 else [5.0] * n
        return lat, 1.0, [[f"r{i}"] for i in range(n)]

    async def nothing(*a, **kw):
        return {}

    for attr, fake in [("cpu_stat_path", lambda container: str(stat)), ("run_queries", run_queries),
                       ("prepare_engine", nothing), ("ensure_no_index", nothing), ("engine_state", nothing),
                       ("podman_mem", lambda container: 512.0), ("podman_disk", lambda volume: 100.0),
                       ("cold_start", lambda *a: 4.2)]:
        monkeypatch.setattr(bench, attr, fake)
    rows, paths = list(range(N)), [f"p{i}" for i in range(N)]

    def measure(*argv):
        args = bench.build_parser().parse_args(list(argv))
        return asyncio.run(bench.bench_engine("raggio", None, paths, [2000] * N, rows, [None] * N,
                                              [[i] for i in rows], ["text"] * N, paths, args))

    res = measure("--cpu-container", "bench-tv")
    assert res["cpu_ms_per_q_c"] == pytest.approx(50.8)
    assert res["hybrid_cpu_ms_per_q_c"] == pytest.approx(343.0)
    assert res["hybrid_c_p99"] == pytest.approx(495.1)  # the hybrid concurrent phase's own p99
    assert res["hybrid_p99"] == 5.0  # the serial row is unchanged
    assert "absent" not in capsys.readouterr().out
    res = measure()  # no --cpu-container: the CPU keys are absent, the p99 is not
    assert "cpu_ms_per_q_c" not in res and "hybrid_cpu_ms_per_q_c" not in res
    assert res["hybrid_c_p99"] == pytest.approx(495.1)
    out = capsys.readouterr().out
    assert "[raggio] cpu_ms_per_q_c absent: no --cpu-container" in out
    assert "[raggio] hybrid_cpu_ms_per_q_c absent: no --cpu-container" in out


def test_concurrent_rows_sit_next_to_their_siblings():
    keys = [key for _, key, _ in bench.ROWS]
    at = keys.index("lat_c_p95")
    assert keys[at + 1] == "cpu_ms_per_q_c"
    at = keys.index("hybrid_qps_concurrent")
    assert keys[at + 1: at + 3] == ["hybrid_c_p99", "hybrid_cpu_ms_per_q_c"]
    args = bench.build_parser().parse_args([])
    assert args.cpu_container is None and args.concurrency == 8
    text = bench.report({"raggio": {"hybrid_c_p99": 612.3}}, args, 1000)
    assert "| Hybrid p99 under concurrency (ms) | 612.3 |" in text
    # no engine has a CPU read: the rows are absent, never 0.0
    assert "| CPU per query under concurrency (ms) |" not in text
    assert "| Hybrid CPU per query under concurrency (ms) |" not in text
    text = bench.report({"raggio": {"cpu_ms_per_q_c": 50.8}, "weaviate": {}}, args, 1000)
    assert "| CPU per query under concurrency (ms) | 50.8 | — |" in text
    assert "| Hybrid CPU per query under concurrency (ms) |" not in text
```

Append to `tests/test_bench_dgx.py`:

```python


# ---- spec §3.1 G7 knob and G5 rows (Task 5): CONCURRENCY in every run and in the regime ----

def test_lib_reads_the_concurrency_from_the_environment():
    lib = (DGX / "lib.sh").read_text(encoding="utf-8")
    session = (DGX / "session.sh").read_text(encoding="utf-8")
    assert 'CONCURRENCY="${CONCURRENCY:-8}"' in lib
    assert '--concurrency "$CONCURRENCY" --cpu-container "$CONTAINER"' in session
    assert '"concurrency": "%s"' in session


@needs_bash
def test_session_passes_the_concurrency_and_the_cpu_container(tmp_path):
    home, cmds = dry_session(tmp_path)
    runs = [c for c in cmds if c.startswith(BENCH_CMD)]
    assert len(runs) == 7 and all(" --concurrency 8 --cpu-container bench-tv " in r for r in runs)
    assert json.loads((home / "s1" / "regime.json").read_text())["concurrency"] == "8"
    c16 = tmp_path / "c16"  # a spec §3.1 G7 c=16 session
    c16.mkdir()
    home, cmds = dry_session(c16, ("CONCURRENCY", "16"))
    runs = [c for c in cmds if c.startswith(BENCH_CMD)]
    assert len(runs) == 7 and all(" --concurrency 16 --cpu-container bench-tv " in r for r in runs)
    assert json.loads((home / "s1" / "regime.json").read_text())["concurrency"] == "16"


def test_compare_flags_arms_that_ran_at_different_concurrency(tmp_path):
    base, cand = tmp_path / "main", tmp_path / "cand"
    flat_session(base, (23.0, 24.0))
    flat_session(cand, (23.5, 23.5))
    write_labels(base, REGIME, BOOKWORM)
    write_labels(cand, {**REGIME, "concurrency": "16"}, BOOKWORM)
    ab = compare.render(base, cand)
    assert ("The arms differ in concurrency: a re-baseline across them, not a same-regime A/B "
            "(spec §6).") in ab
    assert "memory_swap=host-default, concurrency=16, python=3.12.11" in ab.splitlines()[2]
    # a session recorded before the label existed reads "unknown", which differs too
    write_labels(cand, {k: v for k, v in REGIME.items() if k != "concurrency"}, BOOKWORM)
    assert "The arms differ in concurrency: " in compare.render(base, cand)


def test_compare_leaves_absent_cpu_rows_out_of_the_verdicts(tmp_path):
    base, cand, none = tmp_path / "main", tmp_path / "cand", tmp_path / "none"
    write_session(base, {"run1-raggio": {"lat_p50": 23.0, "hybrid_c_p99": 900.0},
                         "run2-raggio": {"lat_p50": 24.0, "hybrid_c_p99": 950.0}})
    write_session(cand, {
        "run1-raggio": {"lat_p50": 23.5, "hybrid_c_p99": 700.0, "cpu_ms_per_q_c": 50.8,
                        "hybrid_cpu_ms_per_q_c": 365.1},
        "run2-raggio": {"lat_p50": 23.5, "hybrid_c_p99": 720.0, "cpu_ms_per_q_c": 116.0,
                        "hybrid_cpu_ms_per_q_c": 361.9}})
    write_session(none, {"run1-raggio": {"lat_p50": 23.0}, "run2-raggio": {"lat_p50": 24.0}})
    ab = compare.render(base, cand)
    # one side lacks the CPU rows (a laptop, or a session before Task 5): "-", never 0, no verdict
    assert "| raggio | CPU per query under concurrency (ms) | - | - | 83.4 | 65.2 | - |" in ab
    assert "| raggio | Hybrid CPU per query under concurrency (ms) | - | - | 363.5 | 3.2 | - |" in ab
    assert "| raggio | Hybrid p99 under concurrency (ms) | 925.0 | 50.0 | 710.0 | 20.0 | better |" in ab
    assert compare.beyond_band(base, cand) == {"better": [("raggio", "hybrid_c_p99")], "worse": []}
    # neither side has them: no row at all
    assert "CPU per query" not in compare.render(base, none)
    assert "CPU per query" not in compare.render(none)
    # all three are lower-is-better: p4's fast state (50.8-56.8 ms) beats its slow one (115.9-117.6)
    assert [compare.HIGHER_IS_BETTER[k] for k in
            ("cpu_ms_per_q_c", "hybrid_c_p99", "hybrid_cpu_ms_per_q_c")] == [False] * 3
    assert compare.verdict("cpu_ms_per_q_c", [115.9, 117.6], [50.8, 56.8]) == "better"
```

In Task 4's block of `tests/test_bench_dgx.py`, the regime gains the label. Replace

```python
REGIME = {"page_cache": "host-warm", "first_start": "host-warm", "memory": "4g",
          "memory_swap": "host-default", "detach_memory": "8g"}
```

with

```python
REGIME = {"page_cache": "host-warm", "first_start": "host-warm", "memory": "4g",
          "memory_swap": "host-default", "detach_memory": "8g", "concurrency": "8"}
```

and, in `test_compare_prints_each_sides_regime`, replace

```python
        "Regime (session): page_cache=host-warm, first_start=host-warm, memory=4g, "
        "memory_swap=host-default, python=3.12.11, sqlite_version=3.40.1, openblas_num_threads=unset")
```

with

```python
        "Regime (session): page_cache=host-warm, first_start=host-warm, memory=4g, "
        "memory_swap=host-default, concurrency=8, python=3.12.11, sqlite_version=3.40.1, "
        "openblas_num_threads=unset")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_bench_harness.py tests/test_bench_dgx.py -q`

Expected (replayed in the scratch directory): `13 failed, 47 passed`. `test_bench_harness.py` has 7 failed and 8 passed; `test_bench_dgx.py` has 6 failed and 39 passed.
- `test_first_hybrid_pass_is_reported_separately` fails with `AttributeError: module 'bench' has no attribute 'hybrid_first_pass'`.
- `test_report_says_the_concurrent_phase_repeats_the_queries` fails with `AssertionError`, because the row is not in the report.
- `test_cpu_stat_path_is_the_containers_cgroup` and `test_read_usage_usec_reads_cpu_stat` fail with `AttributeError: module 'bench' has no attribute 'cpu_stat_path'` and `… 'read_usage_usec'`.
- `test_cpu_rows_are_absent_never_zero` and `test_bench_engine_measures_both_concurrent_phases` fail in `monkeypatch.setattr` with `AttributeError: <module 'bench' …> has no attribute 'cpu_stat_path'`.
- `test_concurrent_rows_sit_next_to_their_siblings` fails with `AssertionError`: the row after `lat_c_p95` is `lat_filtered_p50`.
- `test_lib_reads_the_concurrency_from_the_environment` and `test_session_passes_the_concurrency_and_the_cpu_container` fail with `AssertionError`: lib.sh has no `CONCURRENCY`, and no bench.py command carries `--concurrency`.
- `test_compare_flags_arms_that_ran_at_different_concurrency` fails with `AssertionError`: `REGIME_KEYS` has no `concurrency`, so no line says the arms differ.
- `test_compare_leaves_absent_cpu_rows_out_of_the_verdicts` fails with `AssertionError`: `METRICS` has no CPU row.
- The two Task 4 tests with the new label fail with `AssertionError`. `test_session_records_the_regime_labels` reads a `regime.json` without `concurrency`, and `test_compare_prints_each_sides_regime` reads a regime line without `concurrency=8`.
- `test_compare_covers_every_bench_metric` still passes: neither `ROWS` nor `METRICS` has changed yet.

- [ ] **Step 3: Minimal implementation**

3a. `bench/bench.py`. Replace

```python
def pct(lat, p):
    return statistics.quantiles(lat, n=100)[p - 1]
```

with

```python
def pct(lat, p):
    return statistics.quantiles(lat, n=100)[p - 1]


def hybrid_first_pass(lat_h, n=10):
    """The first n serial hybrid queries apart from the rest (spec §6: after a load the first
    ~10 take 470-980 ms). With concurrency 1 the queries run in list order, so lat_h[:n] are
    the first n sent."""
    if len(lat_h) < n + 2:
        return {}
    return {"hybrid_first10_max_ms": max(lat_h[:n]), "hybrid_p99_after10": pct(lat_h[n:], 99)}
```

3b. In `bench_engine`, replace

```python
    res.update(hybrid_p50=pct(lat_h, 50), hybrid_p95=pct(lat_h, 95), hybrid_p99=pct(lat_h, 99),
               hybrid_qps_serial=len(lat_h) / wall_h)
```

with

```python
    res.update(hybrid_p50=pct(lat_h, 50), hybrid_p95=pct(lat_h, 95), hybrid_p99=pct(lat_h, 99),
               hybrid_qps_serial=len(lat_h) / wall_h)
    res.update(hybrid_first_pass(lat_h))  # the text index's warm-up, reported on its own
```

3c. In `ROWS`, replace

```python
        ("Hybrid p99 (ms)", "hybrid_p99", "{:.1f}"), ("Hybrid QPS serial", "hybrid_qps_serial", "{:.1f}"),
```

with

```python
        ("Hybrid p99 (ms)", "hybrid_p99", "{:.1f}"),
        ("Hybrid first 10 queries, slowest (ms)", "hybrid_first10_max_ms", "{:.1f}"),
        ("Hybrid p99 without the first 10 (ms)", "hybrid_p99_after10", "{:.1f}"),
        ("Hybrid QPS serial", "hybrid_qps_serial", "{:.1f}"),
```

3d. In `report`, replace

```python
              "Weaviate = Go, HNSW approximate index, uncompressed vectors. All queried via REST with "
              "client-supplied vectors; identical stored payloads."]
```

with

```python
              "Weaviate = Go, HNSW approximate index, uncompressed vectors. All queried via REST with "
              "client-supplied vectors; identical stored payloads. The concurrent phases repeat the "
              "serial phases' queries (warm caches). The first 10 serial hybrid queries are also "
              "reported on their own: after a container start they include the text index's warm-up. "
              "CPU per query is the server container's cgroup CPU time over a concurrent phase, "
              "divided by its requests; its rows are absent where the cgroup is not readable."]
```

In the same function, replace

```python
    for label, key, fmt in ROWS:
        vals = [fmt.format(results[n][key]) if key in results[n] else "—" for n in names]
```

with

```python
    for label, key, fmt in ROWS:
        if key in CPU_KEYS and not any(key in results[n] for n in names):
            continue  # no cgroup read on this host: the row is absent, never 0
        vals = [fmt.format(results[n][key]) if key in results[n] else "—" for n in names]
```

3e. `bench/dgx/compare.py`. In `METRICS`, replace

```python
    ("hybrid_p99", "Hybrid p99 (ms)", "{:.1f}", False),
```

with

```python
    ("hybrid_p99", "Hybrid p99 (ms)", "{:.1f}", False),
    ("hybrid_first10_max_ms", "Hybrid first 10 queries, slowest (ms)", "{:.1f}", False),
    ("hybrid_p99_after10", "Hybrid p99 without the first 10 (ms)", "{:.1f}", False),
```

3f. `bench/bench.py`: the cgroup CPU helpers. After `podman_disk`, that is after the line `    return int(out.split()[0]) / 1e6  # MB`, insert:

```python


CGROUP_ROOT = "/sys/fs/cgroup"
CPU_KEYS = {"cpu_ms_per_q_c", "hybrid_cpu_ms_per_q_c"}  # absent, never 0, without a cgroup read


def cpu_stat_path(container):
    """The container's cgroup v2 cpu.stat, found as DGX probe p4 did: /sys/fs/cgroup plus
    `podman inspect --format {{.State.CgroupPath}}`. OSError when podman or the inspect fails."""
    r = subprocess.run(["podman", "inspect", container, "--format", "{{.State.CgroupPath}}"],
                       capture_output=True, text=True)
    cgroup = r.stdout.strip()
    if r.returncode != 0 or not cgroup.startswith("/"):
        raise OSError(f"podman inspect {container} gave no cgroup path (exit {r.returncode})")
    return f"{CGROUP_ROOT}{cgroup}/cpu.stat"


def read_usage_usec(path):
    """usage_usec (the cgroup's total CPU time, microseconds) from a cgroup v2 cpu.stat."""
    for line in Path(path).read_text().splitlines():
        field, _, value = line.partition(" ")
        if field == "usage_usec":
            return int(value)
    raise OSError(f"{path} has no usage_usec line")


async def cpu_phase(name, key, container, phase):
    """Run one concurrent phase and read the server container's cgroup CPU around it (spec §3.1
    G5). Returns (the phase's result, {key: CPU ms per request}); the dict is empty, with one
    line saying why, when --cpu-container is unset or another engine's, or a read fails."""
    before = why = None
    if not container:
        why = "no --cpu-container"
    elif container != ENGINES[name]["container"]:
        why = f"--cpu-container {container} is not {name}'s container"
    else:
        try:
            path = cpu_stat_path(container)
            before = read_usage_usec(path)
        except (OSError, ValueError) as e:
            why = str(e)
    out = await phase()
    if why is None:
        try:
            return out, {key: (read_usage_usec(path) - before) / 1000 / len(out[0])}
        except (OSError, ValueError) as e:
            why = str(e)
    print(f"[{name}] {key} absent: {why}")
    return out, {}
```

The blocking `podman inspect` runs before the phase starts, so it is outside the phase's timed window. The second read follows the last response.

3g. In `bench_engine`, the vector concurrent phase. Replace

```python
    lat_c, wall_c, _ = await run_queries(name, queries, args.concurrency, e["hdrs"])
    res.update(qps_concurrent=len(lat_c) / wall_c, lat_c_p95=pct(lat_c, 95),
               mem_under_load_mb=await mem_probe)
```

with

```python
    (lat_c, wall_c, _), cpu = await cpu_phase(
        name, "cpu_ms_per_q_c", args.cpu_container,
        lambda: run_queries(name, queries, args.concurrency, e["hdrs"]))
    res.update(qps_concurrent=len(lat_c) / wall_c, lat_c_p95=pct(lat_c, 95),
               mem_under_load_mb=await mem_probe, **cpu)
```

`mem_probe` still starts when the phase's first request yields, as before.

3h. The hybrid concurrent phase. Replace

```python
    lat_hc, wall_hc, _ = await run_queries(name, queries, args.concurrency, e["hdrs"], texts=hybrid_texts)
    res["hybrid_qps_concurrent"] = len(lat_hc) / wall_hc
```

with

```python
    (lat_hc, wall_hc, _), cpu = await cpu_phase(
        name, "hybrid_cpu_ms_per_q_c", args.cpu_container,
        lambda: run_queries(name, queries, args.concurrency, e["hdrs"], texts=hybrid_texts))
    res.update(hybrid_qps_concurrent=len(lat_hc) / wall_hc, hybrid_c_p99=pct(lat_hc, 99), **cpu)
```

3i. In `ROWS`, replace

```python
        ("QPS concurrent", "qps_concurrent", "{:.0f}"), ("p95 under concurrency (ms)", "lat_c_p95", "{:.1f}"),
```

with

```python
        ("QPS concurrent", "qps_concurrent", "{:.0f}"), ("p95 under concurrency (ms)", "lat_c_p95", "{:.1f}"),
        ("CPU per query under concurrency (ms)", "cpu_ms_per_q_c", "{:.1f}"),
```

and replace

```python
        ("Hybrid QPS concurrent", "hybrid_qps_concurrent", "{:.1f}"),
```

with

```python
        ("Hybrid QPS concurrent", "hybrid_qps_concurrent", "{:.1f}"),
        ("Hybrid p99 under concurrency (ms)", "hybrid_c_p99", "{:.1f}"),
        ("Hybrid CPU per query under concurrency (ms)", "hybrid_cpu_ms_per_q_c", "{:.1f}"),
```

3j. In `build_parser`, after the line `    ap.add_argument("--concurrency", type=int, default=8)`, insert:

```python
    ap.add_argument("--cpu-container", metavar="NAME",
                    help="container whose cgroup cpu.stat gives the server CPU per query of the "
                         "concurrent phases (the DGX runbook passes bench-tv); without it, or when "
                         "the cgroup is not readable, the CPU rows are absent")
```

3k. `bench/dgx/compare.py`. In `METRICS`, replace

```python
    ("lat_c_p95", "p95 under concurrency (ms)", "{:.1f}", False),
```

with

```python
    ("lat_c_p95", "p95 under concurrency (ms)", "{:.1f}", False),
    ("cpu_ms_per_q_c", "CPU per query under concurrency (ms)", "{:.1f}", False),
```

and replace

```python
    ("hybrid_qps_concurrent", "Hybrid QPS concurrent", "{:.1f}", True),
```

with

```python
    ("hybrid_qps_concurrent", "Hybrid QPS concurrent", "{:.1f}", True),
    ("hybrid_c_p99", "Hybrid p99 under concurrency (ms)", "{:.1f}", False),
    ("hybrid_cpu_ms_per_q_c", "Hybrid CPU per query under concurrency (ms)", "{:.1f}", False),
```

Then replace Task 4's

```python
REGIME_KEYS = ["page_cache", "first_start", "memory", "memory_swap", "python", "sqlite_version",
               "openblas_num_threads"]
```

with

```python
REGIME_KEYS = ["page_cache", "first_start", "memory", "memory_swap", "concurrency", "python",
               "sqlite_version", "openblas_num_threads"]
```

`_rows`, `render`, `verdict` and `beyond_band` need no change. A row neither side has is skipped, and a row one side lacks prints `-` and gets the verdict `-`, which `beyond_band` ignores.

3l. `bench/dgx/lib.sh`: after Task 4's line `FLAT_RUNS="${FLAT_RUNS:-2}"  # measured flat runs after run0; 3 when a concurrent-QPS row is claimed (spec §6)`, insert:

```bash
CONCURRENCY="${CONCURRENCY:-8}"  # bench.py --concurrency of every run; G's c=16 sessions set 16 (spec §3.1 G7)
```

3m. `bench/dgx/session.sh` has three edits.

(1) In the header, replace Task 4's

```bash
# regime.json (page cache, first start, memory, swap) and image.json (python, sqlite,
# turbovec, OPENBLAS_NUM_THREADS inside the image) label every row; compare.py prints them.
```

with

```bash
# CONCURRENCY=16: bench.py --concurrency of every run (default 8; spec §3.1 G7). Every run also
# passes --cpu-container bench-tv, for the server CPU per query of the concurrent phases.
# regime.json (page cache, first start, memory, swap, concurrency) and image.json (python,
# sqlite, turbovec, OPENBLAS_NUM_THREADS inside the image) label every row; compare.py prints them.
```

(2) In `bench_run`, replace

```bash
  run .venv/bin/python -u bench/bench.py --limit "$LIMIT" --caps-note "$CAPS" --host "$HOST_NOTE" \
    --engine "$engine" --fingerprint-dir "$STATE" --out "$OUT/$tag-$engine.md" "${EXTRA[@]}"
```

with

```bash
  run .venv/bin/python -u bench/bench.py --limit "$LIMIT" --caps-note "$CAPS" --host "$HOST_NOTE" \
    --engine "$engine" --concurrency "$CONCURRENCY" --cpu-container "$CONTAINER" \
    --fingerprint-dir "$STATE" --out "$OUT/$tag-$engine.md" "${EXTRA[@]}"
```

Task 3's run test still holds: the command keeps `--limit 2549619 ` and `--fingerprint-dir <state> `, and ends with `--out <out>/<tag>-<engine>.md`.

(3) Replace Task 4's `regime.json` line

```bash
printf '{"page_cache": "host-warm", "first_start": "%s", "memory": "%s", "memory_swap": "%s", "detach_memory": "%s"}\n' \
  "$FIRST_START" "$MEMORY" "${MEMORY_SWAP:-host-default}" "$DETACH_MEMORY" > "$OUT/regime.json"
```

with

```bash
printf '{"page_cache": "host-warm", "first_start": "%s", "memory": "%s", "memory_swap": "%s", "detach_memory": "%s", "concurrency": "%s"}\n' \
  "$FIRST_START" "$MEMORY" "${MEMORY_SWAP:-host-default}" "$DETACH_MEMORY" "$CONCURRENCY" > "$OUT/regime.json"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bench_harness.py tests/test_bench_dgx.py -q`
Expected (replayed in the scratch directory): `60 passed` (15 + 45). `test_compare_covers_every_bench_metric` is among them, so `ROWS` and `METRICS` agree on all five new rows. On a host without bash, the `needs_bash` tests skip instead; this task adds one.

Run: `uv run pytest -q`
Expected (derived): `161 passed, 1 warning`.

- [ ] **Step 5: Commit**

```bash
git add bench/bench.py bench/dgx/compare.py bench/dgx/lib.sh bench/dgx/session.sh \
  tests/test_bench_harness.py tests/test_bench_dgx.py
git commit -m "$(cat <<'EOF'
bench: report warm-up, concurrent p99 and CPU per query (spec §6, G5)

The serial hybrid phase is the first text query after a container start;
its first 10 queries carry the FTS warm-up. bench.py and compare.py gain
the slowest of the first 10 and the p99 of the rest (the warm pass), and
the report notes that the concurrent phases repeat the serial queries.

For spec §3.1 G5, bench.py adds the hybrid concurrent phase's p99 and,
with --cpu-container, the server container's cgroup CPU per request of
both concurrent phases. Without a readable cpu.stat the CPU rows are
absent, never 0, and one log line says why.

For G7, session.sh passes CONCURRENCY (default 8) and --cpu-container to
every run, and regime.json records it, so compare.py flags arms that ran
at different concurrency.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: DGX equivalence re-baseline on PR 1 (not executed in the sandbox)

Spec §2, order 0: "the hardened bench on the same image must land inside the baseline's noise band." This task measures `localhost/raggio:baseline` (19a8a2e, the image baseline3 measured) with PR 1's bench and runbook. It then compares the result with baseline3.

This is a DGX task. Nothing in it was run in the sandbox, and no command here has contacted gn100 yet. It makes no code change and has no test cycle. What it produces is data, and the gate below decides whether PR 1 merges.

Every command runs from the workstation repo root in Git Bash. `DGX_SSH` below stands for exactly this command, written out in full in every step:

`ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100`

**Files:**
- No repo file changes.
- On gn100:
  - Create `~/raggio-bench/baseline3/`, holding baseline3's five result files under the runbook names, plus `regime.json` and `image.json`.
  - Create `~/raggio-bench/a-equiv/`, the session output.
  - Create `~/raggio-bench/state/fingerprint-raggio-2549619.json`, adopted on the first run.
  - Replace `~/raggio`: the deploy wipes it, after `unpack.sh` copies its results, logs and fingerprints to `~/raggio-bench/pre-deploy-<UTC time>/`.
- On the workstation: create `D:/DEV/_scratch/dgx/a-equiv/` and `D:/DEV/_scratch/dgx/baseline3/` (copies, Step 9). `D:/DEV/_scratch/dgx/baseline3-session/` is created only by Step 4's fallback.

**Interfaces:**
- Consumes:
  - PR 1's head (Tasks 1–5 committed on `bench-hardening`);
  - baseline3's result files, `~/raggio/bench/results-baseline3-*.json` on gn100, with `D:/DEV/_scratch/dgx/baseline3-results.txt` as the local copy;
  - the `localhost/raggio:baseline` image (`sha256:66aadc92…`);
  - the `bench-tv` volume (2,549,119 chunks; IVF attached when baseline3 ended).
- Produces, for Task 10:
  - the adopted keyed fingerprint;
  - the session directories;
  - the volume's final index state. An IVF-first session ends flat, and a flat-first one ends IVF. Task 10 reads the state; it doesn't assume it.

- [ ] **Step 1: Ask the user for a run window**

Ask, and wait for an explicit yes:

> Task 6 needs gn100 for about 45 minutes: a deploy (~5 min), then one bench session of `localhost/raggio:baseline`, capped at 50 min and detached with `setsid`.
>
> The first deploy replaces `~/raggio`. Its `bench/results-*`, `*.log` and `fingerprint-*.json` files are copied to `~/raggio-bench/pre-deploy-<UTC time>/` first; any other file there is lost.
>
> `bench-wv`, its volume and `~/turborag` are not touched: turborag is only read, for the corpus link and the ground-truth copy.
>
> May I use gn100 for up to 75 minutes from now?

If the answer names a shorter cap, pass it to `timeout` in Step 5. It must be at least 45m; below that the session will not finish, so tell the user so.

- [ ] **Step 2: Read-only pre-checks**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
uptime; free -m | head -2
podman ps -a --format "{{.Names}} {{.Status}} {{.Image}}"
podman image inspect --format "{{.Id}}" localhost/raggio:baseline || echo "NO BASELINE IMAGE"
pgrep -af "[s]ession\.sh|[b]ench/bench\.py|[d]gx-baseline" || echo "no bench running"
ls -la ~/raggio ~/raggio/bench
ls ~/raggio/bench/results-baseline3-*.json
ls ~/raggio-bench/state 2>/dev/null || echo "no runbook state yet"
~/.local/bin/uv --version'
```

Expected:
- `no bench running`;
- the image id starting `66aadc92`;
- the five `results-baseline3-{run1-raggio,run2-raggio,run1-raggio-ivf,run2-raggio-ivf,run3build-raggio-ivf}.json` files;
- `no runbook state yet`.

Checked read-only on 2026-09-28 at 20:55:
- `localhost/raggio:baseline` was `66aadc92a7b1`;
- `results-baseline3-run{1,2}-raggio{,-ivf}.{json,md}` were in `~/raggio/bench`;
- `~/raggio-bench` did not exist yet;
- uv was 0.12.5, from the standalone installer, with its receipt present.

That check did not list `results-baseline3-run3build-raggio-ivf.json`. If this step's `ls` does not show it, Step 4's `cp` stops at that file, and Step 4's fallback applies.

Stop and ask the user if any of these holds:
- a bench process is running;
- `bench-tv` is running an image other than a `localhost/raggio` tag;
- `~/raggio` holds files other than the git checkout, `.venv`, the corpus link, the ground-truth `.npz` and baseline3's results, logs and patched bench (the deploy would lose them).

If the output shows `NO BASELINE IMAGE`, use the fallback in Step 5.

- [ ] **Step 3: Deploy PR 1's head**

```bash
git switch bench-hardening
git status --short
bash bench/dgx/deploy.sh
```

Expected:
- `git status --short` prints nothing. deploy.sh refuses a tree whose tracked files differ from HEAD; untracked files are not shipped.
- deploy.sh prints `deploying <7-char sha> to gn100`, then unpack's line `kept <n> file(s) of the previous checkout in /home/<user>/raggio-bench/pre-deploy-<UTC time>`.
- Then comes setup.sh's `+ …` command trace: `ln`, `cp` of `gt-2549619-42-d1024.npz`, `uv --version`, `uv sync --frozen`, and `podman build`.
- The last line is `<UTC time> deployed <sha> as localhost/raggio:<sha>`.
- On PR 1, `pyproject.toml` has no `bench` group yet, so setup.sh's `grep` runs the plain `uv sync --frozen`, whose default `dev` group still carries orjson.

- [ ] **Step 4: Put baseline3 into the runbook's layout**

baseline3's run1-ivf followed the 128 s cold load, so it becomes the discarded warm-up run0. The flat runs keep their numbers. The last IVF run (built at 4 GiB in the same container) becomes run2, and its `index_build_s` counts as the one-shot build.

This is the mapping of `D:/DEV/_scratch/plan-sandbox/plan-A-tools/b3convert.py`.

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
set -euo pipefail
src=$(ls -d ~/raggio-bench/pre-deploy-* | tail -1)
dst=~/raggio-bench/baseline3
mkdir "$dst"
cp -p "$src/results-baseline3-run1-raggio.json" "$dst/run1-raggio.json"
cp -p "$src/results-baseline3-run2-raggio.json" "$dst/run2-raggio.json"
cp -p "$src/results-baseline3-run1-raggio-ivf.json" "$dst/run0-raggio-ivf.json"
cp -p "$src/results-baseline3-run2-raggio-ivf.json" "$dst/run1-raggio-ivf.json"
cp -p "$src/results-baseline3-run3build-raggio-ivf.json" "$dst/run2-raggio-ivf.json"
printf "{\"page_cache\": \"host-warm\", \"first_start\": \"mostly-cold\", \"memory\": \"4g\", \"memory_swap\": \"host-default\", \"detach_memory\": \"8g\", \"concurrency\": \"8\"}\n" > "$dst/regime.json"
cp -p "$src/baseline3.log" "$dst/facts.txt" || echo "no baseline3.log: facts.txt left out"
ls "$dst"'
```

Expected: `facts.txt regime.json run0-raggio-ivf.json run1-raggio-ivf.json run1-raggio.json run2-raggio-ivf.json run2-raggio.json`, on one line or several. `facts.txt` is informational, and compare.py doesn't read it.

The first start is labelled `mostly-cold`: 128 s, with 6.1 GB cached against a 15.1 GB meta.db. It is neither of session.sh's two values, and compare.py prints it as-is. `first_start` is not in `SAME_REGIME`, so it does not trigger the arms-differ line.

baseline3 ran bench.py at its default `--concurrency 8`, so its label is `"concurrency": "8"`, the value a-equiv records with the default `CONCURRENCY`. Without the label, baseline3 would read `unknown` and compare.py would print `The arms differ in concurrency`.

If the pre-deploy copy lacks the files, build the directory locally, then copy it:
1. Run `uv run python D:/DEV/_scratch/plan-sandbox/plan-A-tools/b3convert.py D:/DEV/_scratch/dgx/baseline3-session`.
2. Write the same `regime.json` into that directory.
3. Run `ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'mkdir -p ~/raggio-bench/baseline3'`. The `-p` matters: a Step 4 that stopped at a missing file has already created the directory and copied part of it. scp in the next step overwrites those files with the same content.
4. Run `scp -F "$SSH_CONFIG" -o BatchMode=yes /d/DEV/_scratch/dgx/baseline3-session/* gn100:raggio-bench/baseline3/`. The local path is in the `/d/…` form: Git Bash's scp reads `D:/…` as a host named `D`.

- [ ] **Step 5: Launch the session, detached**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
adopt=1; [ -e ~/raggio-bench/state/fingerprint-raggio-2549619.json ] && adopt=0
setsid -f timeout --kill-after=60 50m env IMAGE=localhost/raggio:baseline FLAT_RUNS=3 ADOPT=$adopt \
  bash ~/raggio/bench/dgx/session.sh a-equiv > ~/raggio-bench/a-equiv.log 2>&1 < /dev/null
sleep 5; head -n 3 ~/raggio-bench/a-equiv.log'
```

Expected first line: `<UTC time> session a-equiv: image localhost/raggio:baseline, bench checkout <sha>`.
- `ADOPT=1` applies to the first run only. It records `~/raggio-bench/state/fingerprint-raggio-2549619.json` for the volume that baseline3 already vouched for, with 2,549,119 chunks.
- `FLAT_RUNS=3` gives the candidate side its third flat run.
- The regime defaults apply: `FIRST_START=host-warm`, and podman's default swap.

Fallback when Step 2 printed `NO BASELINE IMAGE`: drop `IMAGE=…`. The session then measures `localhost/raggio:<PR 1 sha>`, which PR 1 built from the same `src/` as 19a8a2e (PR 1 changes no server file).
- Its `image.json` holds that image's own facts, so skip Step 7 and leave baseline3 without an `image.json`.
- compare.py then reads baseline3's image labels as `unknown` and prints `The arms differ in python, sqlite_version, openblas_num_threads`.
- Report the result as a re-baseline of the same source on a rebuilt image, not an equivalence on the same image. The user decides whether it meets §2 order 0.

- [ ] **Step 6: Poll until it ends**

Every 5 minutes:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tail -n 4 ~/raggio-bench/a-equiv.log'
```

The session is done when the last line is `<UTC time> session a-equiv done`, about 35 minutes in.
- If the volume started IVF, the log shows `index at start: ivf`, then raggio-ivf run0–run3, `IVF detach at 8g took <s> s`, and raggio run0–run3.
- A flat start runs raggio run0–run3 first, then raggio-ivf run0, whose build is measured, then run1–run3.
- A line starting `bench aborted:` or `pending_jobs=… giving up` ends the session early. Read the log, and ask the user before relaunching; a relaunch needs a new label.
- A line `[raggio] cpu_ms_per_q_c absent: <why>`, `[raggio-ivf] hybrid_cpu_ms_per_q_c absent: <why>` or the like means bench.py could not read `bench-tv`'s cgroup `cpu.stat`. The CPU rows must be present on gn100, because spec §3.1 G5 classifies Plan G's flat runs by them. Stop the session at the first such line, report the `<why>` to the user, and fix the cause in PR 1 before relaunching: redeploy (Step 3), then use a new label (`a-equiv2`) within the agreed window, or ask for a new one.
- To stop it early: `kill -TERM -- -"$(cat ~/raggio-bench/a-equiv/session.pgid)"`, run in the same ssh form. Never use a `pkill -f` pattern.

- [ ] **Step 7: Give baseline3 its image labels**

a-equiv read the facts inside `localhost/raggio:baseline`, the image baseline3 ran. Copy them only after checking that it is that image:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
set -euo pipefail
id=$(podman image inspect --format "{{.Id}}" localhost/raggio:baseline)
case "$id" in 66aadc92*) ;; *) echo "not the baseline3 image: $id" >&2; exit 1 ;; esac
cp -p ~/raggio-bench/a-equiv/image.json ~/raggio-bench/baseline3/image.json
cat ~/raggio-bench/a-equiv/image.json ~/raggio-bench/a-equiv/regime.json'
```

Expected:
- `{"python": "3.12.<n>", "sqlite_version": "3.40.1", "turbovec": "1.0.0", "openblas_num_threads": "unset"}`;
- then `{"page_cache": "host-warm", "first_start": "host-warm", "memory": "4g", "memory_swap": "host-default", "detach_memory": "8g", "concurrency": "8"}`.

If the python version is not 3.12.x or sqlite is not 3.40.1, the image is not the one baseline3 ran. Stop and report.

- [ ] **Step 8: Check the G5 rows, compare, and apply the gate**

The command first checks that every raggio and raggio-ivf run file holds the three spec §3.1 G5 keys, and that no bench.py run logged an absent CPU row. Then it compares.

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
cd ~/raggio-bench/a-equiv
for f in run*-raggio*.json; do
  for k in hybrid_c_p99 cpu_ms_per_q_c hybrid_cpu_ms_per_q_c; do grep -q "\"$k\"" "$f" || echo "$f: no $k"; done
done
ls run*-raggio*.json | wc -l
grep -h " absent: " ~/raggio-bench/a-equiv.log || echo "no absent row"
cd ~/raggio && .venv/bin/python bench/dgx/compare.py ~/raggio-bench/baseline3 ~/raggio-bench/a-equiv \
  | tee ~/raggio-bench/a-equiv/compare-baseline3.txt'
```

Expected from the check: `8` (raggio and raggio-ivf run0–run3), then `no absent row`, with no `<file>: no <key>` line before them. Any `no <key>` line or `absent:` line means the G5 rows are missing on gn100. Stop there, whatever the table says: report it, and fix it in PR 1 as Step 6 says, because Plan G's G5 needs these rows.

Expected shape of the comparison:
- `A/B: baseline … vs candidate …`.
- Two `Regime (…)` lines that differ only in `first_start`. There is no `The arms differ` line.
- The 7-cell table.
- The note `Reported, not claimed (spec §6 needs >= 3 runs per side; FLAT_RUNS=3): raggio QPS concurrent, raggio-ivf QPS concurrent`. baseline3 has only 2 counted runs per engine.
- Then `better beyond the noise band: <n> row(s) […]` and `worse beyond the noise band: <n> row(s) […]`.

**Gate (equivalence):** every row listed as better or worse must be one of these exemptions:
- a row named in the `Reported, not claimed` note;
- a `raggio-ivf` memory row. baseline3's counted IVF runs mix a disk-loaded and a just-built index (1.51 vs 2.72 GB, spec §5 finding 4).

Any other row outside the band fails the gate. A verdict of `-` is not a failure: it marks a row one side lacks. These are the build, when the session started IVF, and the rows baseline3 predates: the two first-hybrid rows, `Hybrid p99 under concurrency (ms)`, `CPU per query under concurrency (ms)` and `Hybrid CPU per query under concurrency (ms)`. The three G5 rows have no baseline3 counterpart, so they are reported, not gated. `no band` and `not claimable` rows are not failures either.

If the gate fails, do not merge PR 1:
1. Read both `facts.txt` for host differences: load average, cached pages, other containers.
2. If the window still allows it, run one more session under a new label (`a-equiv2`, same launch line) and compare it with baseline3 too.
3. Report both tables to the user, who decides.

- [ ] **Step 9: Stop the container, keep the results, and record them**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'podman stop -t 60 --ignore bench-tv; podman ps -a --format "{{.Names}} {{.Status}}"'
scp -r -F "$SSH_CONFIG" -o BatchMode=yes gn100:raggio-bench/a-equiv gn100:raggio-bench/baseline3 /d/DEV/_scratch/dgx/
```

The target is in the `/d/…` form because Git Bash's scp reads `D:/…` as a host named `D`.

Stopping `bench-tv` frees its 4 GiB; the next session removes it anyway. `bench-wv` is left as it was.

Put `compare-baseline3.txt` in PR 1's description under a `DGX equivalence re-baseline` heading, with:
- both regime lines;
- the image id;
- the index state at start;
- the detach time, when the session started IVF;
- the first-hybrid-pass values (`Hybrid first 10 queries, slowest`: the first A measurement of spec §6's 470–980 ms);
- a-equiv's three G5 rows, `Hybrid p99 under concurrency`, `CPU per query under concurrency` and `Hybrid CPU per query under concurrency`, marked reported, not gated. Note per flat run whether its CPU per query sits near p4's c=8 fast state (50.8–56.8 ms) or slow state (115.9–117.6 ms): this is the first gn100 read of the classifier G5 uses.

---

### Task 7: orjson moves to a `bench` group, uv 0.12 floor, re-lock (PR 2)

This task was replayed in the sandbox as its packaging step (section `===== T4` of `verify_final.out`), except Step 3. The regenerated `uv.lock` matched the sandbox commit c97aaf5.

**Files:**
- Create: `tests/test_packaging.py`.
- Modify:
  - `pyproject.toml` (lines 14-21);
  - `uv.lock` (regenerated);
  - `README.md:187`;
  - `docs/getting-started.md:110-115`;
  - `docs/benchmark.md:98-100`;
  - `docs/benchmark-arxiv.md` (lines 184, 205, 243 and 246, as numbered after Task 2);
  - `bench/bench.py:4-5`;
  - `bench/floor_probe.py:4`;
  - `bench/prep_arxiv.py:10`;
  - `bench/dgx/setup.sh`, the three `GROUP` lines from Task 3.

**Interfaces:**
- Consumes:
  - `main` after PR 1 (Tasks 1–6) and Plan B have merged. Plan B changes no `pyproject.toml` or `uv.lock`.
  - Task 3's `setup.sh`.
- Produces, relied on by Tasks 8–10 and plans C–G:
  - the dependency groups: `bench = ["orjson>=3.12.0"]`, `dev = ["pytest>=8"]` and `docs` unchanged;
  - `[tool.uv] required-version = ">=0.12"`;
  - `uv.lock` at revision 3;
  - `uv sync --group bench` as the documented develop command.
  - `tests/test_packaging.py` helpers that Tasks 8 and 9 extend: `ROOT`, `PYPROJECT`, `LOCK`, `_name(req)` and `_shell_lines(text)`.
  - Plan F's setup detection (`^bench = \[`) and Plan G's `uv sync --frozen --group bench` read these names.

- [ ] **Step 1: Branch, and write the failing tests**

```bash
git switch main
git pull --ff-only
git switch -c packaging-foundation
```

Create `tests/test_packaging.py`:

```python
"""Packaging (spec D2, D5): orjson is a bench-only dependency, uv >= 0.12 is required to
touch the lock, the lock matches pyproject, and every documented way to run an
orjson-importing bench script asks for the bench group."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
LOCK = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def _name(req):
    return re.match(r"[A-Za-z0-9._-]+", req).group(0).lower()


def test_orjson_is_a_bench_only_dependency():
    groups = PYPROJECT["dependency-groups"]
    assert "orjson" in [_name(r) for r in groups["bench"]]
    assert "orjson" not in [_name(r) for r in PYPROJECT["project"]["dependencies"]]
    assert "orjson" not in [_name(r) for r in groups["dev"]]
    # runtime JSON stays on the stdlib (ADR 0001:31)
    assert [p for p in (ROOT / "src").rglob("*.py") if "orjson" in p.read_text(encoding="utf-8")] == []


def test_uv_floor_is_declared():
    assert PYPROJECT["tool"]["uv"]["required-version"] == ">=0.12"


def test_lock_matches_the_dependency_groups():
    (raggio,) = [p for p in LOCK["package"] if p["name"] == "raggio"]
    locked = {g: sorted((d["name"], d.get("specifier", "")) for d in deps)
              for g, deps in raggio["metadata"]["requires-dev"].items()}
    declared = {g: sorted((_name(r), r[len(_name(r)):]) for r in reqs)
                for g, reqs in PYPROJECT["dependency-groups"].items()}
    assert locked == declared
    assert LOCK["revision"] >= 3  # a uv >= 0.12 re-lock writes revision 3; the old lock is 2


def _shell_lines(text):
    """Lines with backslash continuations joined, so a wrapped command reads as one."""
    return re.sub(r"\\\n\s*", " ", text).splitlines()


def test_orjson_scripts_are_documented_with_the_bench_group():
    # a plain `uv sync` no longer installs orjson: a command without --group bench dies
    # with ModuleNotFoundError before it measures anything
    scripts = [p.name for p in (ROOT / "bench").glob("*.py") if "import orjson" in p.read_text(encoding="utf-8")]
    assert "bench.py" in scripts
    sources = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "bench").glob("*.py"))]
    missing = [f"{p.relative_to(ROOT).as_posix()}: {line.strip()}"
               for p in sources for line in _shell_lines(p.read_text(encoding="utf-8"))
               if "uv run" in line and any(f"bench/{s}" in line for s in scripts)
               and "--group bench" not in line]
    assert missing == []


def test_develop_instructions_and_the_dgx_setup_sync_the_bench_group():
    # without the group, the bench-harness tests skip and the DGX venv can't run bench.py
    for doc in ("README.md", "docs/getting-started.md"):
        assert "uv sync --group bench" in (ROOT / doc).read_text(encoding="utf-8").splitlines()
    setup = (ROOT / "bench/dgx/setup.sh").read_text(encoding="utf-8")
    assert 'run "$HOME/.local/bin/uv" sync --frozen --group bench' in setup.splitlines()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_packaging.py -q`

Expected (replayed):

```
E       KeyError: 'bench'
E       KeyError: 'uv'
E       assert 2 >= 3
E       AssertionError: assert ['docs/benchm...e first', ...] == []
E         Left contains 11 more items, first extra item: 'docs/benchmark-arxiv.md: nohup env PYTHONUNBUFFERED=1 uv run --with pyarrow --with huggingface_hub  python bench/prep_arxiv.py --stage all > bench/prep.log 2>&1 &'
E           AssertionError: assert 'uv sync --group bench' in ['# raggio', '', '![raggio logo](docs/raggio-logo.png)', ...]
FAILED tests/test_packaging.py::test_orjson_is_a_bench_only_dependency - KeyE...
FAILED tests/test_packaging.py::test_uv_floor_is_declared - KeyError: 'uv'
FAILED tests/test_packaging.py::test_lock_matches_the_dependency_groups - ass...
FAILED tests/test_packaging.py::test_orjson_scripts_are_documented_with_the_bench_group
FAILED tests/test_packaging.py::test_develop_instructions_and_the_dgx_setup_sync_the_bench_group
5 failed
```

The 11 undocumented lines are:
- `docs/benchmark-arxiv.md`: 4 lines;
- `docs/benchmark.md`: 3 lines;
- the `bench.py` docstring: 2 lines;
- `floor_probe.py` and `prep_arxiv.py`: one line each.

- [ ] **Step 3: Put the dev box on uv 0.12.19 (not executed in the sandbox)**

The sandbox ran every command of this task with uv 0.12.19. The dev box has uv 0.5.24 on PATH. Once Step 4 declares the floor, that uv refuses the project; the replay recorded this error:

```
error: Required uv version `>=0.12` does not match the running version `0.5.24`
```

Run: `uv --version`

If it prints a version below 0.12, ask the user before changing their tools:

> The uv floor needs uv >= 0.12 on this machine; `uv --version` shows `<version>`. May I run `uv self update 0.12.19`? It replaces the standalone uv in `~/.local/bin`.

After a yes, run `uv self update 0.12.19`. Then `uv --version` must print `uv 0.12.19 (…)`.

`uv self update` works only for the standalone installer. If uv came from pip or winget, ask the user to upgrade it that way.

- [ ] **Step 4: Minimal implementation**

4a. `pyproject.toml`. Replace

```toml
[dependency-groups]
dev = [
    "orjson>=3.12.0",
    "pytest>=8",
]
docs = [
    "mkdocs-material>=9",
]
```

with

```toml
[dependency-groups]
dev = [
    "pytest>=8",
]
# bench/ harness only (bench.py, floor_probe.py, prep_arxiv.py): no free-threaded
# wheel exists, so it stays out of the default sync (ADR 0001:31 keeps runtime JSON on stdlib)
bench = [
    "orjson>=3.12.0",
]
docs = [
    "mkdocs-material>=9",
]

[tool.uv]
# floor for every uv that touches uv.lock; the exact pin (0.12.19) lives in the
# Dockerfile (ARG UV_VERSION) and .github/workflows/tests.yml (setup-uv version)
required-version = ">=0.12"
```

4b. `README.md`. In the `## Develop` block, replace the line `uv sync` (line 187) with `uv sync --group bench`. The block becomes:

````markdown
## Develop

```bash
uv sync --group bench
uv run pytest
```
````

4c. `docs/getting-started.md`. Replace

````markdown
## Develop

```bash
uv sync
uv run pytest
```
````

with

````markdown
## Develop

```bash
uv sync --group bench
uv run pytest
```

The project needs uv 0.12 or newer (`required-version` in `pyproject.toml`). The
`bench` group adds orjson, which the benchmark harness in `bench/` (and its
tests) imports; the server itself never needs it.
````

4d. `docs/benchmark.md`. Replace lines 98-100

```bash
uv run python bench/bench.py                  # full run, both engines
uv run python bench/bench.py --engine raggio --reingest   # one engine, wipe first
uv run python bench/bench.py --limit 20000 --queries 100    # quick smoke
```

with

```bash
uv run --group bench python bench/bench.py                  # full run, both engines
uv run --group bench python bench/bench.py --engine raggio --reingest   # one engine, wipe first
uv run --group bench python bench/bench.py --limit 20000 --queries 100    # quick smoke
```

4e. `docs/benchmark-arxiv.md` has four one-line replacements. The line numbers are those after Task 2.

| Line | Old | New |
|---|---|---|
| 184 | `nohup env PYTHONUNBUFFERED=1 uv run --with pyarrow --with huggingface_hub \` | `nohup env PYTHONUNBUFFERED=1 uv run --group bench --with pyarrow --with huggingface_hub \` |
| 205 | `uv run --with pyarrow --with huggingface_hub \` | `uv run --group bench --with pyarrow --with huggingface_hub \` |
| 243 | `uv run python bench/bench.py --limit 20000 --queries 100 \` | `uv run --group bench python bench/bench.py --limit 20000 --queries 100 \` |
| 246 | `nohup env PYTHONUNBUFFERED=1 uv run python bench/bench.py --limit <N> --reingest \` | `nohup env PYTHONUNBUFFERED=1 uv run --group bench python bench/bench.py --limit <N> --reingest \` |

4f. `bench/bench.py`. Replace the docstring lines 4-5

```
Run:  uv run python bench/bench.py --limit 20000          # smoke
      uv run python bench/bench.py                         # full run
```

with

```
Run:  uv run --group bench python bench/bench.py --limit 20000   # smoke
      uv run --group bench python bench/bench.py                  # full run
```

4g. `bench/floor_probe.py`. Replace line 4

```python
# Usage: uv run python bench/floor_probe.py   (bench-tv container must be up)
```

with

```python
# Usage: uv run --group bench python bench/floor_probe.py   (bench-tv container must be up)
```

4h. `bench/prep_arxiv.py`. Replace line 10

```
Run:  uv run --with pyarrow --with huggingface_hub python bench/prep_arxiv.py --stage all
```

with

```
Run:  uv run --group bench --with pyarrow --with huggingface_hub python bench/prep_arxiv.py --stage all
```

4i. `bench/dgx/setup.sh`. The `bench` group now exists on every checkout this script deploys. Replace Task 3's

```bash
# orjson sits in the default dev group until the packaging change moves it to "bench"
if grep -q '^bench = \[' pyproject.toml; then GROUP=(--group bench); else GROUP=(); fi
run "$HOME/.local/bin/uv" sync --frozen "${GROUP[@]}"
```

with

```bash
run "$HOME/.local/bin/uv" sync --frozen --group bench
```

- [ ] **Step 5: Re-lock with uv 0.12.19**

```bash
uv lock
git diff --stat main -- uv.lock
uv lock --check
uv sync --frozen --group bench
```

Expected (replayed; the timings vary):
- `uv lock` prints `Resolved 54 packages in 264ms`.
- The diff stat prints ` 1 file changed, 6 insertions(+), 6 deletions(-)`. On Windows, Git first warns `in the working copy of 'uv.lock', LF will be replaced by CRLF the next time Git touches it`; the warning is harmless.
- `uv lock --check` prints `Resolved 54 packages in 0.90ms`.
- The sync's last line is ` ~ raggio==0.1.0 (from file:///…)`.

The lock changes these three things, and resolves the same 54 packages:
- `revision = 2` becomes `revision = 3`;
- `[package.dev-dependencies]` gains `bench = [{ name = "orjson" }]`, and `dev` keeps only pytest;
- `[package.metadata.requires-dev]` becomes:

```toml
[package.metadata.requires-dev]
bench = [{ name = "orjson", specifier = ">=3.12.0" }]
dev = [{ name = "pytest", specifier = ">=8" }]
docs = [{ name = "mkdocs-material", specifier = ">=9" }]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_packaging.py -q`
Expected (replayed): `5 passed`.

Run: `uv run pytest -q`
- Expected: `166 passed, 1 warning` for A's tests alone, and `183 passed, 1 warning` on this branch with Plan B's 17.
- The sandbox, without Tasks 4–5, replayed `144 passed, 1 warning`.
- The bench-harness tests run rather than skip, because the `bench` group is synced.
- The one warning is Starlette's `StarletteDeprecationWarning`: "Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead." The re-lock keeps the same 54 packages, so it stays. DGX probe p4 saw it on every interpreter: `46 passed, 1 warning` on Debian 3.12.12, PBS 3.12.14, 3.14.7 and 3.14.7t. Moving the tests to httpx2 is not part of this plan.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock README.md docs/getting-started.md docs/benchmark.md docs/benchmark-arxiv.md bench/bench.py bench/floor_probe.py bench/prep_arxiv.py bench/dgx/setup.sh tests/test_packaging.py
git commit -m "$(cat <<'EOF'
packaging: orjson to a bench group, uv >= 0.12, re-lock (spec D5)

orjson is only imported by the bench harness (runtime JSON stays stdlib,
ADR 0001:31), so it leaves the default dev group for "bench". The uv floor
is declared as required-version and uv.lock is re-locked with uv 0.12.19
(revision 3, same 54 packages). Every documented run line of an orjson
script, the develop instructions and the DGX setup sync the bench group.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Minimal CI, `tests.yml` (PR 2)

This task was replayed in the sandbox as its CI step (section `===== T5` of `verify_final.out`), except the SHA check in Step 3. The workflow also ran in a local Linux simulation with uv 0.12.19 (`D:/DEV/_scratch/plan-sandbox/plan-A-tools/ci_sim_v5.out`: `156 passed, 1 warning`, on an earlier revision of the test set). It has not run on GitHub.

**Files:**
- Create: `.github/workflows/tests.yml`.
- Test: `tests/test_packaging.py` (append).

**Interfaces:**
- Consumes: Task 7's `PYPROJECT` floor and the `bench` group.
- Produces:
  - one job, `test`: ubuntu-latest, 15 min, setup-uv pinned by commit SHA with `version: "0.12.19"` and `python-version: "3.12"`, then the steps `uv lock --check`, `uv sync --frozen --group bench` and `uv run --no-sync pytest -q`;
  - triggers: push to main, pull_request and workflow_dispatch;
  - `permissions: contents: read`.
  - `_ci()` and `_ci_job(name)` in the tests read one job's lines, so E's native job and G's 3.14t job can sit next to `test` without breaking these tests. Task 9 reads the uv pin through `_ci_job("test")`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_packaging.py`:

```python


def _ci():
    return (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")


def _ci_job(name):
    """One job's lines of tests.yml: the jobs later plans add (native, 3.14t) carry their
    own steps and pins."""
    lines = _ci().splitlines()
    start = lines.index(f"  {name}:")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^ {0,2}\S", lines[i])), len(lines))
    return "\n".join(lines[start:end])


def _floor():
    return tuple(int(x) for x in PYPROJECT["tool"]["uv"]["required-version"].removeprefix(">=").split("."))


def test_ci_pins_uv_above_the_floor_and_python_312():
    job = _ci_job("test")
    assert re.search(r"^    runs-on: ubuntu-latest$", job, re.M)
    # third-party action pinned by commit, tag in the comment
    assert re.search(r"^\s+- uses: astral-sh/setup-uv@[0-9a-f]{40} # v\d+\.\d+\.\d+$", job, re.M)
    uv = re.search(r'^\s+version: "(\d+\.\d+\.\d+)"', job, re.M).group(1)
    assert tuple(int(x) for x in uv.split(".")) >= _floor()
    assert re.search(r'^\s+python-version: "3\.12"$', job, re.M)


def test_ci_checks_the_lock_and_runs_the_suite_with_the_bench_group():
    job = _ci_job("test")
    steps = [ln.strip().removeprefix("- run: ") for ln in job.splitlines() if ln.strip().startswith("- run: ")]
    # without --group bench the bench-harness tests would skip instead of run
    assert steps == ["uv lock --check", "uv sync --frozen --group bench", "uv run --no-sync pytest -q"]
    ci = _ci()
    assert re.search(r"^  pull_request:", ci, re.M) and re.search(r"^  push:\n    branches: \[main\]", ci, re.M)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_packaging.py -q`

Expected (replayed):

```
E       FileNotFoundError: [Errno 2] No such file or directory: '…\\.github\\workflows\\tests.yml'
E       FileNotFoundError: [Errno 2] No such file or directory: '…\\.github\\workflows\\tests.yml'
FAILED tests/test_packaging.py::test_ci_pins_uv_above_the_floor_and_python_312
FAILED tests/test_packaging.py::test_ci_checks_the_lock_and_runs_the_suite_with_the_bench_group
2 failed, 5 passed
```

- [ ] **Step 3: Minimal implementation**

Create `.github/workflows/tests.yml`:

```yaml
name: tests

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  # the Python suite on the production interpreter (spec D11); the native-extension job
  # and the non-blocking 3.14t job are added next to this one by their own plans
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
        with:
          version: "0.12.19"  # same pin as ARG UV_VERSION in the Dockerfile
          python-version: "3.12"
      - run: uv lock --check
      - run: uv sync --frozen --group bench
      - run: uv run --no-sync pytest -q
```

Check that the pinned SHA is the tag's commit (not executed in the sandbox):

Run: `gh api repos/astral-sh/setup-uv/commits/v10.2.0 --jq .sha`
Expected: `c18668ad3cf93ea998bef934396af7bb5c839dc7`.

If it prints another SHA, use the printed one in the `uses:` line and keep the `# v10.2.0` comment.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_packaging.py -q`
Expected (replayed): `7 passed`.

Run: `uv run pytest -q`
Expected:
- `168 passed, 1 warning` for A's tests alone, and `185 passed, 1 warning` with Plan B's 17.
- The sandbox, without Tasks 4–5, replayed `146 passed, 1 warning`.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/tests.yml tests/test_packaging.py
git commit -m "$(cat <<'EOF'
ci: minimal tests workflow on 3.12 (spec D11)

One job: setup-uv pinned by commit SHA with uv 0.12.19 and Python 3.12,
then uv lock --check, uv sync --frozen --group bench (so the bench-harness
tests run instead of skipping) and the suite. E adds the native job and G
the 3.14t job next to it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

When PR 2 is pushed, the workflow runs on the pull request. Its `test` job must pass before the merge.

---

### Task 9: Dockerfile: PBS CPython 3.12 on trixie, one BLAS thread (PR 2)

This task was replayed in the sandbox as its Dockerfile step (section `===== T6` of `verify_final.out`), and the image was built with podman (`D:/DEV/_scratch/plan-sandbox/plan-A-tools/img5_build.out`, exit 0). Some parts were not executed in the sandbox:
- the D16 line `ENV OPENBLAS_NUM_THREADS=1` and its comment;
- the test `test_image_runs_blas_single_threaded`;
- the counts that include them (RED 3 failed, GREEN 10 passed, suite 171);
- the image checks in Steps 5 and 6.

**Files:**
- Modify: `Dockerfile` (whole file, 20 lines).
- Test: `tests/test_packaging.py` (append).

**Interfaces:**
- Consumes: Task 8's `_ci_job("test")` (the uv pin) and Task 7's lock.
- Produces the image layout that C, E and G edit:
  - `ARG UV_VERSION=0.12.19` and `ARG PYTHON=3.12` at the top;
  - a `uv` stage from `ghcr.io/astral-sh/uv:${UV_VERSION}`;
  - a `builder` stage on `debian:trixie-slim` that installs CPython into `/python` with `uv python install ${PYTHON}` and syncs `/app` with `--frozen --no-dev`;
  - a runtime stage on `debian:trixie-slim`:
    - it copies `/python` and `/app` and runs as `app` (uid 1000);
    - it has `ENV OPENBLAS_NUM_THREADS=1` right after `USER app`, then `ENV LANG=C.UTF-8 DATA_DIR=/data PATH="/app/.venv/bin:$PATH"`, `VOLUME /data`, `EXPOSE 8000`, the comment and the unchanged CMD.
- C's `ENV MALLOC_TRIM_THRESHOLD_` edit anchors on `VOLUME /data` + `EXPOSE 8000`. G's `PYTHONWARNINGS` edit anchors right after the `ENV LANG=…` line. E pins the `OPENBLAS_NUM_THREADS=1` line. The D16 line sits between `USER app` and `ENV LANG=…`, which is outside both anchors.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_packaging.py`:

```python


CMD = ('CMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", '
       '"--no-access-log"]')


def _dockerfile():
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_pins_the_ci_uv_and_defaults_to_312_on_trixie():
    d = _dockerfile()
    uv = re.search(r"^ARG UV_VERSION=(\S+)$", d, re.M)
    assert uv and uv.group(1) == re.search(r'^\s+version: "(\S+)"', _ci_job("test"), re.M).group(1)
    assert re.search(r"^ARG PYTHON=3\.12$", d, re.M)
    froms = re.findall(r"^FROM (\S+)", d, re.M)
    assert froms[0] == "ghcr.io/astral-sh/uv:${UV_VERSION}"
    assert froms[-1] == "docker.io/library/debian:trixie-slim"
    assert "bookworm" not in d  # the bookworm uv tags stopped at 0.9.30
    assert re.search(r"^RUN uv python install \$\{PYTHON\}$", d, re.M)


def test_image_keeps_the_runtime_contract():
    d = _dockerfile()
    runtime = re.split(r"^FROM ", d, flags=re.M)[-1]  # the last stage is the image
    # the old base's `useradd -m app` got uid 1000; existing /data volumes are owned by it
    assert "RUN useradd -m --uid 1000 app && mkdir /data && chown app /data" in runtime
    # python:3.12-slim set LANG=C.UTF-8; debian:trixie-slim sets no locale
    assert re.search(r"^ENV LANG=C\.UTF-8 DATA_DIR=/data PATH=\"/app/\.venv/bin:\$PATH\"$", runtime, re.M)
    for line in ("COPY --from=builder /python /python", "WORKDIR /app", "USER app", "VOLUME /data",
                 "EXPOSE 8000"):
        assert re.search(f"^{re.escape(line)}$", runtime, re.M), line
    assert runtime.rstrip().endswith(CMD)
    # the image installs no dependency group: no pytest, no orjson
    syncs = re.findall(r"^RUN uv sync (.*)$", d, re.M)
    assert syncs and all("--frozen" in s and "--no-dev" in s and "--group" not in s for s in syncs)


def test_image_runs_blas_single_threaded():
    # spec D16: numpy's OpenBLAS otherwise spins one thread per core under search load
    runtime = re.split(r"^FROM ", _dockerfile(), flags=re.M)[-1]
    assert re.search(r"^ENV OPENBLAS_NUM_THREADS=1$", runtime, re.M)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_packaging.py -q`

Expected:

```
E       assert (None)
E       assert 'RUN useradd -m --uid 1000 app && mkdir /data && chown app /data' in 'ghcr.io/astral-sh/uv:python3.12-bookworm-slim\n\nWORKDIR /app\nENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy\n\nCOPY py...ing\nCMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]\n'
FAILED tests/test_packaging.py::test_dockerfile_pins_the_ci_uv_and_defaults_to_312_on_trixie
FAILED tests/test_packaging.py::test_image_keeps_the_runtime_contract - asser...
FAILED tests/test_packaging.py::test_image_runs_blas_single_threaded
3 failed, 7 passed
```

- The first two failures were replayed (`2 failed, 7 passed` without the third test).
- The third failure (not executed in the sandbox) prints an `assert None` from `re.search`.

- [ ] **Step 3: Minimal implementation**

Replace the whole of `Dockerfile`. The old text is:

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd -m app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000

# no per-request access log: it costs a stdout write through the container log
# pipe on every query; set --access-log if you need request tracing
CMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
```

The new text is below. It is the sandbox's final Dockerfile with two lines added after `USER app`: the D16 comment and `ENV OPENBLAS_NUM_THREADS=1` (not executed in the sandbox).

```dockerfile
# One parameterized image (spec D2): uv installs the interpreter named by PYTHON into
# /python; the runtime stage is plain debian:trixie-slim with /python and /app copied in.
# UV_VERSION is the same pin as .github/workflows/tests.yml.
ARG UV_VERSION=0.12.19
ARG PYTHON=3.12

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM docker.io/library/debian:trixie-slim AS builder
COPY --from=uv /uv /uvx /bin/
ARG PYTHON
ENV UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON=${PYTHON} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
RUN uv python install ${PYTHON}
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev

FROM docker.io/library/debian:trixie-slim
COPY --from=builder /python /python
COPY --from=builder /app /app
WORKDIR /app
RUN useradd -m --uid 1000 app && mkdir /data && chown app /data
USER app
# one OpenBLAS thread (spec D16): numpy's scipy-openblas otherwise starts one busy-spinning thread per core under search load
ENV OPENBLAS_NUM_THREADS=1
ENV LANG=C.UTF-8 DATA_DIR=/data PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000

# no per-request access log: it costs a stdout write through the container log
# pipe on every query; set --access-log if you need request tracing
CMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
```

Notes:
- The venv's `python` is a symlink into `/python`, so the runtime stage copies `/python` to the same path.
- `/python` holds only the interpreter that uv installed; `UV_PYTHON_PREFERENCE=only-managed` keeps uv from picking up any system Python in the builder.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_packaging.py -q`
Expected: `10 passed`. The sandbox replayed `9 passed` without the D16 test.

Run: `uv run pytest -q`
Expected:
- `171 passed, 1 warning` for A's tests alone, and `188 passed, 1 warning` with Plan B's 17.
- The sandbox, without Tasks 4–5 and the D16 test, replayed `148 passed, 1 warning`.

- [ ] **Step 5: Build the image and read its interpreter, SQLite, BLAS setting and uid**

Run: `podman build -t raggio-a .`

Expected: the build exits 0 and ends with `Successfully tagged localhost/raggio-a:latest`. The runtime stage has 11 steps: the D16 `ENV` is step 7 of 11 and the CMD is `[3/3] STEP 11/11`. The sandbox built the same file without the D16 lines (10 runtime steps, `v5 build exit 0`).

Run (not executed in the sandbox):

```bash
podman run --rm raggio-a python -c "import os, sqlite3, sys; print(sys.version.split()[0], sqlite3.sqlite_version, os.environ['OPENBLAS_NUM_THREADS'], os.getuid(), os.environ['LANG'])"
```

Expected: `3.12.14 3.53.1 1 1000 C.UTF-8`.
- 3.12.14 is the 3.12 build that `uv python install 3.12` gave with uv 0.12.19 in DGX probe p4 (gn100, aarch64, 2026-09-28).
- SQLite 3.53.1 is what spec D15 records for uv 0.12.19's PBS interpreters (p4). p2's scaling figures were measured on 3.50.4 and were not re-measured on 3.53.1.
- p4 read both on aarch64 only; this build is for the dev box's architecture. If the image reports another Python or SQLite version, record it in the PR 2 description. Task 10's `image.json` records both too.

- [ ] **Step 6: Check that a volume written by the old image works with the new one (not executed in the sandbox)**

The DGX `bench-tv` volume and users' `/data` volumes were written by the old image as uid 1000. Task 10 reuses `bench-tv`.

Run, before the commit, so that `HEAD:Dockerfile` is still the old file:

```bash
tmp=$(mktemp -d)
git show HEAD:Dockerfile > "$tmp/Dockerfile.old"
podman build -t raggio-a-old -f "$tmp/Dockerfile.old" .
podman volume create raggio-a-compat
MSYS_NO_PATHCONV=1 podman run -d --name raggio-a-compat -p 18100:8000 -v raggio-a-compat:/data -e ROOT_API_KEY=bench raggio-a-old
uv run python D:/DEV/_scratch/plan-sandbox/plan-A-tools/vol_compat.py write 18100
podman rm -f raggio-a-compat
MSYS_NO_PATHCONV=1 podman run -d --name raggio-a-compat -p 18100:8000 -v raggio-a-compat:/data -e ROOT_API_KEY=bench raggio-a
uv run python D:/DEV/_scratch/plan-sandbox/plan-A-tools/vol_compat.py read 18100
podman rm -f raggio-a-compat
podman volume rm raggio-a-compat
podman rmi raggio-a-old
rm -r "$tmp"
```

In Git Bash, `MSYS_NO_PATHCONV=1` keeps `raggio-a-compat:/data` from being rewritten as a Windows path list (on Linux it is a no-op). The `podman build -f "$tmp/…"` line needs the default conversion, so the variable is set only on the two `-v` lines.

Expected:
- The write phase prints a line starting `write ok`.
- The read phase prints a line starting `read ok` with `'chunks': 2100` and `'pending_jobs': 0`, and exits 0.
- The read phase checks the IVF collection that the old image wrote: a vector hit on `c7`, text hits, and a 100-document append plus a re-index.

If the read phase fails with a permission error on `/data`, compare uids with `podman run --rm raggio-a-old id -u` and `podman run --rm raggio-a id -u`, which should both print `1000`. Stop and report; do not commit.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile tests/test_packaging.py
git commit -m "$(cat <<'EOF'
build: PBS CPython 3.12 on debian trixie, one OpenBLAS thread (D2, D15, D16)

The uv binary comes from ghcr.io/astral-sh/uv:0.12.19 (the pin CI uses);
the builder installs CPython 3.12 into /python with uv python install and
syncs /app; the runtime is debian:trixie-slim with /python and /app copied
in. uid 1000, LANG=C.UTF-8 and the CMD are unchanged from the old base, so
existing /data volumes keep working. ENV OPENBLAS_NUM_THREADS=1 stops
numpy's OpenBLAS from spinning a thread per core under search load.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

Push `packaging-foundation` and open PR 2 against `main`. Its description starts with the Task 10 results, which the DGX run adds before the merge.

---

### Task 10: DGX new reference baseline on PR 2 (not executed in the sandbox)

Spec §2, order 2: "The DGX step records a **new reference baseline** for the PBS interpreter, SQLite 3.53.1 and single-threaded BLAS (D15, D16). The IVF build time and the flat vector rows must not regress beyond the band. Hybrid rows and IVF concurrent/filtered rows are expected to move and are reported."

This task measures two images in back-to-back sessions with the same runbook: `main` (with PR 1 and Plan B merged) as session `a-main`, then PR 2's head as session `a-ref`. It then compares them. `a-ref` becomes the reference that Plans C–G compare against.

Both sessions start from a flat index, so each one measures the IVF build at 4 GiB (raggio-ivf run0). A session that starts on IVF measures no build: session.sh's IVF-first order only detaches. A small helper, `~/raggio-bench/detach.sh`, therefore drops the IVF index before each session. It uses the same 8 GiB transient container and the same `DELETE /collections/bench/index` that session.sh's `detach_index` uses.

Spec §3.1 A1 decides the build gate: "The IVF build-time gate builds **twice per arm** and compares medians, against the band. There is no single-build tolerance and no rerun rule."
- Both sessions therefore run with `IVF_BUILDS=2` (Task 4). After its measured runs, session.sh detaches the index once more in the 8 GiB container, then runs `build2 raggio-ivf`, which builds it again from flat at 4 GiB.
- The measured runs keep the session's one IVF ↔ flat transition: the extra detach and build come after all of them. compare.py counts only build2's build time.
- Each side's build row then has two values (run0's and build2's), a median and a band. Gate 1 (Step 12) is that row's verdict.
- The two extra steps add about 13 minutes per session: about 5 for the detach (285 s in the research baseline) and about 8 for build2 (a 213 s build, then the bench's query phases, which compare.py ignores).

This is a DGX task. Nothing in it was run in the sandbox, and no command here has contacted gn100 yet. It makes no repo change and has no test cycle. Every command runs from the workstation repo root in Git Bash, with the ssh form written out in full.

**Files:**
- No repo file changes.
- On gn100:
  - Create `~/raggio-bench/detach.sh`, `~/raggio-bench/detach-a-main.log` and `~/raggio-bench/detach-a-ref.log`.
  - Create `~/raggio-bench/a-main/` and `~/raggio-bench/a-ref/` (session outputs), with their `.log` files.
  - Replace `~/raggio` twice, by the two deploys. unpack.sh keeps the previous checkout's results, logs and fingerprints in `~/raggio-bench/pre-deploy-<UTC time>/`.
  - No uv change: gn100's `~/.local/bin/uv` is 0.12.5, which meets PR 2's `>=0.12` floor (Step 3).
- On the workstation: create `D:/DEV/_scratch/dgx/a-main/` and `D:/DEV/_scratch/dgx/a-ref/` (copies, Step 13).

**Interfaces:**
- Consumes:
  - Task 6's adopted fingerprint `~/raggio-bench/state/fingerprint-raggio-2549619.json`;
  - the `bench-tv` volume in whatever index state Task 6 left;
  - `main` after PR 1 and Plan B merged, and PR 2's head (Tasks 7–9 on `packaging-foundation`, rebased on that `main`).
- Produces:
  - `~/raggio-bench/a-ref/`, the reference session for later plans: PBS CPython 3.12, SQLite 3.53.1, `OPENBLAS_NUM_THREADS=1`, `FLAT_RUNS=3`, `IVF_BUILDS=2`, host-warm, 4 GiB, podman's default swap;
  - `~/raggio-bench/a-ref/compare-a-main.txt`, which goes in PR 2's description;
  - a `bench-tv` volume left with an IVF index (a flat-first session ends IVF).

- [ ] **Step 1: Ask the user for a run window**

Ask, and wait for an explicit yes:

> Task 10 needs gn100 for about 2.5 hours:
> - deploy `main` (~5 min), drop the IVF index if present (~10 min), and run one session of `main`'s image (about 53 min, capped at 65 min);
> - deploy PR 2 (~10 min: the first trixie build downloads CPython), drop the IVF index again (~10 min), and run one session of PR 2's image (about 53 min, capped at 65 min).
>
> Each session builds the IVF index twice, as spec §3.1 A1 requires: once in its IVF warm-up run, and once more after its measured runs, after a second detach in the 8 GiB container. That adds about 13 minutes per session.
>
> Each deploy replaces `~/raggio`. Its `bench/results-*`, `*.log` and `fingerprint-*.json` files are copied to `~/raggio-bench/pre-deploy-<UTC time>/` first. `bench-wv`, its volume and `~/turborag` are not touched: turborag is only read.
>
> PR 2's setup needs uv 0.12 or later. gn100 has uv 0.12.5 (checked 2026-09-28), so no uv update is planned.
>
> May I use gn100 for up to 180 minutes from now?

If the answer names a shorter cap, below 150 minutes, say that both sessions will not fit. Offer to run `a-main` in this window and the rest (from Step 8) in a second one.

- [ ] **Step 2: Read-only pre-checks**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
uptime; free -m | head -2; df -h ~ | tail -1
podman ps -a --format "{{.Names}} {{.Status}} {{.Image}}"
pgrep -af "[s]ession\.sh|[b]ench/bench\.py|[d]etach\.sh|[d]gx-baseline" || echo "no bench running"
ls ~/raggio-bench ~/raggio-bench/state
cat ~/raggio/.deployed-sha
~/.local/bin/uv --version'
```

Expected:
- `no bench running`;
- `a-equiv` (Task 6) in `~/raggio-bench`, and no `a-main`, `a-ref` or `detach.sh`;
- `fingerprint-raggio-2549619.json` in the state directory;
- a last line starting `uv 0.12.5`, the version measured on gn100 on 2026-09-28 at 20:55.

Stop and ask the user if any of these holds:
- a bench process is running;
- `bench-tv` is running an image other than a `localhost/raggio` tag;
- `a-main` or `a-ref` already exists. session.sh refuses an existing label, and the user decides whether to move the old one into `~/raggio-bench/a-attempt-1/`; never delete it.
- the fingerprint is missing. Step 7's launch line then adopts the volume (`ADOPT=1`), as Task 6 did; say so to the user before launching.

- [ ] **Step 3: uv on gn100 meets PR 2's floor**

PR 2's `pyproject.toml` has `[tool.uv] required-version = ">=0.12"`, and its setup.sh runs `uv sync --frozen --group bench`.

On 2026-09-28 at 20:55, gn100's `~/.local/bin/uv` was 0.12.5, installed by the standalone installer (`~/.config/uv/uv-receipt.json` is present). 0.12.5 meets the floor, so no update is planned. If Step 2 printed `uv 0.12.5`, or any version of 0.12 or later, skip this step.

Only if Step 2 printed a version below 0.12 (someone changed uv since that check), ask:

> `~/.local/bin/uv` on gn100 is `<version>`; PR 2 requires uv 0.12 or later. May I run `~/.local/bin/uv self update 0.12.19` there? It replaces only that binary.

After a yes:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '~/.local/bin/uv self update 0.12.19 && ~/.local/bin/uv --version'
```

Expected: the last line starts `uv 0.12.19`. The standalone installer's receipt is what lets `self update` replace the binary. If it refuses anyway, stop and report its output to the user.

- [ ] **Step 4: Deploy `main`**

```bash
git switch main
git pull --ff-only
git status --short
git log --oneline -3
bash bench/dgx/deploy.sh
```

Expected:
- `git status --short` prints nothing.
- `git log` shows Plan B's merge on top of PR 1's.
- deploy.sh prints `deploying <main sha> to gn100`, then unpack's `kept <n> file(s) …` line and setup.sh's `+ …` trace.
- On `main`, `pyproject.toml` has no `bench` group yet, so setup.sh runs the plain `uv sync --frozen`.
- The last line is `<UTC time> deployed <main sha> as localhost/raggio:<main sha>`.

- [ ] **Step 5: Install the detach helper**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'mkdir -p ~/raggio-bench && cat > ~/raggio-bench/detach.sh' <<'EOF'
#!/usr/bin/env bash
# Leave the bench-tv volume with a flat index, so the next session.sh session starts flat
# and measures the IVF build (plan A, Task 10). Same 8 GiB transient container and
# DELETE /collections/bench/index as session.sh's detach_index; a flat volume is left as is.
# Usage: setsid -f timeout --kill-after=60 20m bash ~/raggio-bench/detach.sh <image> \
#          > ~/raggio-bench/detach-<label>.log 2>&1 < /dev/null
set -euo pipefail
IMAGE="${1:?usage: detach.sh <image>}"
URL=http://localhost:18000/collections/bench
KEY="x-api-key: bench"
ps -o pgid= -p $$ | tr -d ' ' > ~/raggio-bench/detach.pgid
field() {  # pending | index
  curl -sf --max-time 1800 -H "$KEY" "$URL" | "$HOME/raggio/.venv/bin/python" -c 'import json, sys
info = json.load(sys.stdin)
print(info["pending_jobs"] if sys.argv[1] == "pending" else (info.get("index") or {}).get("type"))' "$1"
}
wait_pending() { until [ "$(field pending)" = 0 ]; do sleep 15; done; }
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

podman stop -t 60 --ignore bench-tv
podman rm -f --ignore bench-tv
podman run -d --name bench-tv --memory 8g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench "$IMAGE"
until curl -sf -o /dev/null --max-time 2 http://localhost:18000/healthz; do sleep 0.5; done
wait_pending
before=$(field index)
log "index before: $before"
start=$SECONDS
if [ "$before" = ivf ]; then
  curl -sf --max-time 900 -X DELETE -H "$KEY" "$URL/index"
  echo
  wait_pending
fi
after=$(field index)
podman stop -t 60 bench-tv
log "index after: $after, detach took $((SECONDS - start)) s"
[ "$after" = flat ]
EOF
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'bash -n ~/raggio-bench/detach.sh && echo syntax ok'
```

Expected: `syntax ok`.

The only container it stops or removes is `bench-tv`. Its waits have no bounds of their own: the `timeout` in the launch line caps the whole script at 20 minutes.

- [ ] **Step 6: Leave the volume flat before `a-main`**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
img=localhost/raggio:$(cat ~/raggio/.deployed-sha)
setsid -f timeout --kill-after=60 20m bash ~/raggio-bench/detach.sh "$img" > ~/raggio-bench/detach-a-main.log 2>&1 < /dev/null
sleep 5; cat ~/raggio-bench/detach-a-main.log'
```

Poll every 2 minutes with `ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tail -n 3 ~/raggio-bench/detach-a-main.log'`.

It is done when the last line is `<UTC time> index after: flat, detach took <s> s`:
- If Task 6's session ended flat, the log has `index before: flat` and a detach time near 0.
- If it ended IVF, the log has `index before: ivf` and a detach time near 285 s, the research baseline's value at 8 GiB.

Anything else is a failure:
- any other last line;
- the script no longer running (`pgrep -af "[d]etach\.sh"` prints nothing) without that line.

On failure, stop, and report the log to the user. To stop the script early: `kill -TERM -- -"$(cat ~/raggio-bench/detach.pgid)"` in the same ssh form, then `podman stop -t 60 --ignore bench-tv`.

- [ ] **Step 7: Session `a-main`**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
adopt=1; [ -e ~/raggio-bench/state/fingerprint-raggio-2549619.json ] && adopt=0
setsid -f timeout --kill-after=60 65m env FLAT_RUNS=3 IVF_BUILDS=2 ADOPT=$adopt \
  bash ~/raggio/bench/dgx/session.sh a-main > ~/raggio-bench/a-main.log 2>&1 < /dev/null
sleep 5; head -n 3 ~/raggio-bench/a-main.log'
```

Expected first line: `<UTC time> session a-main: image localhost/raggio:<main sha>, bench checkout <main sha>`.

Poll every 5 minutes with `ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'tail -n 4 ~/raggio-bench/a-main.log'`. The session is done when the last line is `<UTC time> session a-main done`, about 53 minutes in. The log shows:
- `index at start: flat`;
- raggio run0–run3;
- raggio-ivf run0, whose build is measured at 4 GiB, about 213 s in the research baseline;
- raggio-ivf run1–run3;
- `IVF detach at 8g took <s> s`, the second detach (`IVF_BUILDS=2`), about 285 s in the research baseline;
- `build2 raggio-ivf`, whose build is measured at 4 GiB again.

A line starting `bench aborted:` or `pending_jobs=… giving up` ends the session early, and so does `IVF_BUILDS=2 needs a flat start`, which means Step 6 did not leave the volume flat. Read the log, and ask the user before relaunching; a relaunch needs a new label and another Step 6. To stop it early: `kill -TERM -- -"$(cat ~/raggio-bench/a-main/session.pgid)"`, run in the same ssh form. Never use a `pkill -f` pattern.

- [ ] **Step 8: Deploy PR 2's head**

```bash
git switch packaging-foundation
git status --short
git merge-base --is-ancestor main HEAD && echo "on top of main"
bash bench/dgx/deploy.sh
```

Expected:
- `git status --short` prints nothing, and the next line prints `on top of main`. If it doesn't, rebase PR 2 on `main` first and re-run Tasks 7–9's suite step.
- deploy.sh prints `deploying <PR 2 sha> to gn100` and the `kept …` line.
- setup.sh's trace shows `uv --version` printing `uv 0.12.5` (or `uv 0.12.19` in the unplanned case that Step 3 updated it), then `uv sync --frozen --group bench`.
- The `podman build` pulls `ghcr.io/astral-sh/uv:0.12.19` and `debian:trixie-slim` for arm64, and runs `uv python install 3.12`.
- The last line is `<UTC time> deployed <PR 2 sha> as localhost/raggio:<PR 2 sha>`.

If `uv sync` fails with `Required uv version`, Step 3 was skipped wrongly. Go back to it.

- [ ] **Step 9: Leave the volume flat before `a-ref`**

`a-main` ended on IVF. Run Step 6's launch and polling, with `detach-a-ref.log` in place of `detach-a-main.log`:

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
img=localhost/raggio:$(cat ~/raggio/.deployed-sha)
setsid -f timeout --kill-after=60 20m bash ~/raggio-bench/detach.sh "$img" > ~/raggio-bench/detach-a-ref.log 2>&1 < /dev/null
sleep 5; cat ~/raggio-bench/detach-a-ref.log'
```

Expected final line: `<UTC time> index after: flat, detach took <s> s`, after `index before: ivf`. This detach runs PR 2's image on the volume that the old image wrote. It is the first DGX test of Task 9 Step 6's compatibility.

- [ ] **Step 10: Session `a-ref`**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
setsid -f timeout --kill-after=60 65m env FLAT_RUNS=3 IVF_BUILDS=2 \
  bash ~/raggio/bench/dgx/session.sh a-ref > ~/raggio-bench/a-ref.log 2>&1 < /dev/null
sleep 5; head -n 3 ~/raggio-bench/a-ref.log'
```

Expected first line: `<UTC time> session a-ref: image localhost/raggio:<PR 2 sha>, bench checkout <PR 2 sha>`. Poll as in Step 7, with `a-ref`. The expected log shape is the same: `index at start: flat`, raggio run0–run3, raggio-ivf run0 with the build, run1–run3, the second `IVF detach at 8g took <s> s`, `build2 raggio-ivf` with the second build, then `session a-ref done`, about 53 minutes in. `a-main` already recorded the fingerprint, so there is no `ADOPT`.

- [ ] **Step 11: Check both images' labels**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
cd ~/raggio-bench
for s in a-main a-ref; do echo "$s"; cat "$s/image.json" "$s/regime.json"
  for r in run0 build2; do grep -o "\"index_build_s\": [0-9.]*" "$s/$r-raggio-ivf.json" || echo "no IVF build measured in $r"; done
done
podman image ls --format "{{.Tag}} {{.Size}}" localhost/raggio'
```

Expected:
- `a-main`: `{"python": "3.12.<n>", "sqlite_version": "3.40.1", "turbovec": "1.0.0", "openblas_num_threads": "unset"}`.
- `a-ref`: `{"python": "3.12.14", "sqlite_version": "3.53.1", "turbovec": "1.0.0", "openblas_num_threads": "1"}`, the versions p4 read from uv 0.12.19's PBS 3.12 on gn100.
- Both `regime.json` files: `{"page_cache": "host-warm", "first_start": "host-warm", "memory": "4g", "memory_swap": "host-default", "detach_memory": "8g", "concurrency": "8"}`.
- Each session then prints `"index_build_s": <seconds>` twice: the build that raggio-ivf run0 measured at 4 GiB, then build2's (each about 213 s in the research baseline). bench.py writes them to the runs' JSON, not to `facts.txt`.
- `no IVF build measured in run0` means the session did not start flat. `no IVF build measured in build2` means the second build did not run or did not build. Either way, stop and report: Gate 1 needs both builds of both sessions (spec §3.1 A1).
- The last lines are one `<tag> <size>` pair per `localhost/raggio` image. The `<main sha>` and `<PR 2 sha>` lines are the two images' sizes, which Step 13 reports.

If `a-ref` has no `image.json`, or its `openblas_num_threads` is not `"1"`, the image under test is not Task 9's. Stop and report. If `sqlite_version` is not `3.53.1` or `python` is not `3.12.14`, continue, and record the versions in PR 2's description; spec D15 records 3.53.1 for uv 0.12.19's PBS interpreters (p4).

- [ ] **Step 12: Compare, and apply the gates**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 '
cd ~/raggio && .venv/bin/python bench/dgx/compare.py ~/raggio-bench/a-main ~/raggio-bench/a-ref \
  | tee ~/raggio-bench/a-ref/compare-a-main.txt'
```

Expected shape:
- `A/B: baseline … vs candidate …`, then the two identical-regime `Regime (…)` lines, apart from the image labels.
- `The arms differ in sqlite_version, openblas_num_threads: a re-baseline across them, not a same-regime A/B (spec §6).`, with `python, ` first when the two 3.12 patch versions differ. This line is expected: it marks the D15 re-baseline that the spec calls for.
- The 7-cell table.
  - `raggio-ivf` `IVF index build (s)` has two values per side (run0's and build2's builds): each side's median and max − min band, and the verdict `better`, `within band` or `worse`.
  - `Cold start to first query (s)` is `not claimable`.
- No `Reported, not claimed` note: with `FLAT_RUNS=3` both sides have 3 counted runs per engine.
- Then the `better beyond the noise band: …` and `worse beyond the noise band: …` lines, which list `(engine, key)` pairs.

**Gate 1 (IVF build, D16 and spec §3.1 A1):** Gate 1 fails if and only if `('raggio-ivf', 'index_build_s')` is in the `worse beyond the noise band` list.
- Each side's value is the median of its two builds. The band is the wider of the two sides' max − min, and never below 1 s, as for every row.
- There is no single-build tolerance and no rerun rule (spec §3.1 A1). `better` and `within band` pass.
- A build row whose verdict is `no band` or `-` lacks a build on some side; Step 11 should have caught it. That is a missing measurement, not a gate result: do not merge PR 2, and report Step 11's output to the user.

**Gate 2 (vector rows, spec §2 order 2 and D16):** none of these pairs may be in the `worse` list:
- `('raggio', k)` for k in `lat_p50`, `lat_p95`, `lat_p99`, `qps_serial`, `qps_concurrent`, `lat_c_p95`, `lat_filtered_p50`, `lat_filtered_p95`, `recall_at_10`;
- `('raggio-ivf', k)` for k in `lat_p50`, `lat_p95`, `lat_p99`, `qps_serial`, `recall_at_10`: D16's "ingest-free rows" beyond the ones order 2 says are expected to move.

These rows are reported, not gated:
- every `hybrid_*` row, including `hybrid_first10_max_ms`, `hybrid_p99_after10`, `hybrid_c_p99` and `hybrid_cpu_ms_per_q_c`;
- `cpu_ms_per_q_c` of both engines (spec §3.1 G5: CPU per query is reported, not gated);
- the `raggio-ivf` rows `qps_concurrent`, `lat_c_p95`, `lat_filtered_p50` and `lat_filtered_p95`, which the spec expects to move (D16's p3 probe saw them improve);
- the memory and disk rows. A `worse` verdict on one of them is named to the user in the PR 2 description. It does not block by itself.

**The p4 expectation for the hybrid rows (D15, D2).** DGX probe p4 (`docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`, gn100, 2026-09-28, code at 19a8a2e) measured the base migration alone. One arm was the Debian image of 19a8a2e (CPython 3.12.12, SQLite 3.40.1). The other was a probe image of D2's shape (PBS CPython 3.12.14, SQLite 3.53.1). Both ran with `OPENBLAS_NUM_THREADS=1` at `--memory 4g` without `--memory-swap`, on the IVF data, 3 runs per arm. The medians:
- c=8: 12.7 → 17.9 hybrid QPS, p99 6,731 → 3,981 ms, server CPU per query 556.5 → 343.0 ms;
- c=16: 12.1 → 14.8 hybrid QPS, p99 8.9 → 1.9 s, server CPU per query 872.3 → 365.1 ms.

This is an expectation, not a target and not a gate:
- It was measured on the probe image with p4's own driver on p2's 500-query set, not on Task 9's image with bench.py.
- `hybrid_qps_concurrent`, `hybrid_c_p99` and `hybrid_cpu_ms_per_q_c` run at `CONCURRENCY=8`, the default, so p4's c=8 medians above are their counterparts. `a-ref`'s raggio-ivf QPS is expected above `a-main`'s, and its p99 and CPU per query below. p4's c=8 hybrid phase was 300 requests; bench.py's is 500.
- p4's c=16 figures above have no counterpart here: this task runs no `CONCURRENCY=16` session. bench.py's `hybrid_p99` is a serial row.
- `a-main` runs with BLAS unset, so this A/B also carries D16, which p4's two arms did not.
- p4 measured hybrid on the IVF data only. It sets no expectation for the `raggio` (flat) hybrid rows.

If both gates pass, `a-ref` is the new reference baseline, and PR 2 may merge after review.

If Gate 2 fails, do not merge PR 2:
1. Read both `facts.txt` files for host differences: load average, cached pages, other containers.
2. Report the table to the user. A repeat needs another detach and session under new labels (`a-ref2`), in a window the user grants.

If Gate 1 fails, do not merge PR 2. Report all four build times (run0 and build2 of each session), both medians and bands, and each session's `facts.txt` lines around raggio-ivf run0 and build2, to the user. D16's fallback applies, and only now that the gate has failed (spec §3.1 A1): keep BLAS multi-threaded for the build and limit it on the search path only, with threadpoolctl, still owned by A. That is a follow-up change the user approves before it is planned; it is not part of this plan.

- [ ] **Step 13: Stop the container, keep the results, and record them**

```bash
ssh -F "$SSH_CONFIG" -o BatchMode=yes gn100 'podman stop -t 60 --ignore bench-tv; podman ps -a --format "{{.Names}} {{.Status}}"'
scp -r -F "$SSH_CONFIG" -o BatchMode=yes gn100:raggio-bench/a-main gn100:raggio-bench/a-ref /d/DEV/_scratch/dgx/
```

As in Task 6, the scp target is written `/d/…`, not `D:/…`.

Stopping `bench-tv` frees its 4 GiB. `bench-wv` is left as it was.

Put `compare-a-main.txt` in PR 2's description under a `DGX reference baseline (D15, D16)` heading, with:
- both sessions' `image.json` and regime lines, and both image ids;
- all four build times (run0 and build2 of `a-main` and of `a-ref`), both medians and bands, and Gate 1's verdict;
- the detach times: Steps 6 and 9, and each session's second `IVF detach at 8g took <s> s` before build2;
- the first-hybrid-pass values;
- the raggio-ivf concurrent and filtered rows, next to the p3 probe's figures quoted in D16 (serial 4 vs 8 ms, filtered 32 vs 70 ms, and F's server concurrency of about 325 vs 203 QPS, with the setting vs without). p3 used its own setup, so these are context, not targets.
- the raggio-ivf hybrid rows, next to p4's figures from Step 12 (c=8: 12.7 → 17.9 QPS, p99 6,731 → 3,981 ms, CPU per query 556.5 → 343.0 ms). They were measured on the probe image, so they are context, not targets.
- both sessions' `CPU per query under concurrency` per flat run, with the p4 state each one matches (c=8 fast 50.8–56.8 ms, slow 115.9–117.6 ms).
- both images' sizes: the `<main sha>` and `<PR 2 sha>` lines of Step 11's `podman image ls`. p4 reported 408 MB for the Debian image of 19a8a2e and 317.4 MB for its PBS 3.12 probe image, which, like Task 9's image, has no `raggio_native` and no orjson. This is a reported expectation, not a gate.

State that later plans compare against `~/raggio-bench/a-ref` (PBS CPython 3.12, SQLite `<version>`, `OPENBLAS_NUM_THREADS=1`), not against baseline3 or the research baseline.

---

## Spec §6 coverage

Every item of the DGX benchmark protocol, and where this plan meets it:

| §6 item | Where | How |
|---|---|---|
| Access, and the run window with a time cap | Tasks 6 and 10, Step 1 | The first step asks for a window. Every ssh/scp uses the exact `-F … -o BatchMode=yes gn100` form. |
| Deploy: `git archive`, corpus link, ground truth, `uv sync --frozen --group bench`, `podman build -t localhost/raggio:<sha>` | Task 3 (`deploy.sh`, `unpack.sh`, `setup.sh`), Task 7 (`--group bench`) | unpack.sh keeps the old checkout's results before the wipe. `~/turborag` is only read. |
| Container: `bench-tv`, 4 GiB, port 18000, `ROOT_API_KEY=bench`; `podman rm -f` names only `bench-tv`; a fresh container per run | Task 3 (`fresh_container`, `test_guard_rails`), Task 10 (`detach.sh`) | `CONTAINER=bench-tv` is the only name any script stops or removes. |
| Bench invocation: `--limit 2549619`, no `--reingest` | Task 2 (preflight aborts on a wrong limit), Task 3 (`bench_run`, and a guard rail against `2549119` and `--reingest`) | |
| Order in a session: facts, first start, pending wait, warm-up run0, grouped runs with one IVF ↔ flat transition, the 8 GiB detach, the build measured at 4 GiB | Task 3 (`session.sh`), Task 4 (`IVF_BUILDS=2`) | Task 10 adds a detach before each session, so both sessions start flat and measure the build. With `IVF_BUILDS=2`, each then detaches at 8 GiB and builds once more at 4 GiB after its measured runs, which keep the one transition (spec §3.1 A1). |
| Noise band: flat run1 vs run2, IVF ≥ 3 runs with median and max − min, concurrent QPS ≥ 3 runs, a fresh container per arm, a win only beyond the band | Task 3 (`compare.py`), Task 4 (`FLAT_RUNS`, the not-claimed note, `BUILD_FILE`) | Tasks 6 and 10 run `FLAT_RUNS=3`. Task 10 also runs `IVF_BUILDS=2`, so its build row has two values per side and Gate 1 compares medians against the band (spec §3.1 A1). |
| The concurrent phase repeats the same 500 queries (warm caches) | Task 5 (the report note) | |
| `OPENBLAS_NUM_THREADS`: both arms the same, every row records it | Task 4 (`image.json`, the arms-differ line), Task 9 (the D16 `ENV`) | Task 10's A/B crosses it on purpose, and compare.py says so. |
| Pinning with `os.sched_setaffinity` when a single-thread latency needs attributing | Not in this plan | A's steps attribute no single-thread latency. See the spec gaps. |
| Regime labelling: page cache, memory cap, `sqlite_version`, the same regime on both arms | Task 4 (`regime.json`, `image.json`, `SAME_REGIME`) | Task 6 hand-labels baseline3's first start as `mostly-cold`. |
| The first hybrid pass after a load, reported on its own | Task 5 (`hybrid_first10_max_ms`, `hybrid_p99_after10`) | |
| Spec §3.1 G5 and §4.2: the concurrent hybrid p99 and CPU per query rows, with the CPU rows absent, never 0, where `cpu.stat` is not readable | Task 5 (`hybrid_c_p99`, `cpu_ms_per_q_c`, `hybrid_cpu_ms_per_q_c`, `--cpu-container`, the absent-row log line, compare.py's `-` cells), Task 6 Step 8 (the rows are present on gn100), Task 10 Step 12 | A reports them and gates none: baseline3 has no counterpart, and G owns the p99 guard. |
| Spec §3.1 G7 and §4.2: the `CONCURRENCY` session knob, default 8 | Task 5 (`lib.sh`, `bench_run`, the regime's `concurrency` label and `REGIME_KEYS`) | Tasks 6 and 10 run at the default. G's c=16 sessions set `CONCURRENCY=16`, and compare.py flags arms that ran at different concurrency. |
| Memory gate: `--memory 4g --memory-swap 4g` | Task 4 (`MEMORY_SWAP=4g`) | A makes no memory claim, so neither DGX task uses it. |
| Cold start claimed only true-cold | Task 3 (`cold_start_s` is `not claimable`), Task 4 (`FIRST_START=true-cold` label) | The fadvise eviction (D12) belongs to Plan C. |
| Tunables chosen on seed 7, published on seed 42 | Not applicable | A tunes nothing. |
| Log and background discipline: `setsid -f`, a log file, polling, no `pkill -f`, the user's work left alone | Task 3 (`session.pgid`), Tasks 6 and 10 (launch and poll steps), Task 10 (`detach.pgid`) | `bench-wv` and `~/turborag` are never modified. |

## Cross-plan notes

These are places where another plan describes Plan A differently from what this plan builds. This plan keeps its own behaviour; the other plans' owners reconcile them.

- **Plan E (`2026-09-28-native-bm25.md`):** no mismatch found. Its DGX task (Interfaces, line 3289) consumes only `deploy.sh`, `~/raggio/.deployed-sha` and Task 10's `~/raggio-bench/detach.sh <image>` with the last log line `index after: flat, detach took <s> s`; all three match. It runs its own arms, not `session.sh` or `compare.py`. `detach.sh` exists only on gn100 (Task 10 Step 5), so E's pre-check (line 3399, `detach.sh ok`) is what catches its absence.
- **Plan G (`2026-09-28-free-threaded-python.md`):** reconciled in the cross-plan review. G used to assume that A's `session.sh` holds `~/raggio-bench/active-session`; A has no such lock. G's `g-ensure-ivf.sh`, pre-flights, poll and stop recipe now find a running `session.sh` with `pgrep -f` on its command line (`bash <path>/bench/dgx/session.sh <label>`), and treat only G's own `g-active` as a lock file. A keeps its behaviour: `session.sh` writes no lock file, so any change to that command line must be reflected in G.
  - Spec §3.1 G5, G7, G8 and G9: Plan G reads what Task 5 builds. Its Task 10 Interfaces, its "Cross-plan assumptions" list and its synthetic `regime.json` carry the keys `hybrid_c_p99`, `cpu_ms_per_q_c` and `hybrid_cpu_ms_per_q_c` with the labels "Hybrid p99 under concurrency (ms)", "CPU per query under concurrency (ms)" and "Hybrid CPU per query under concurrency (ms)". The `Regime (baseline): …` line format there includes `concurrency=…` between `memory_swap` and `python`, and the synthetic `regime.json` sets `concurrency` to 16 for `-c16` sessions and 8 otherwise. G's `g-abab.sh` sets `CONCURRENCY=16` for its c=16 sessions and 8 for the others, and `g-session.sh` hands it to `session.sh`. G gates on "Hybrid p99 under concurrency (ms)" (G5): a hybrid QPS win needs it not `worse` in either cross table. A real c=8 session compared with a c=16 one prints `The arms differ in concurrency: …`; G's pair tables pair c=8 with c=8 and c=16 with c=16, so they print none.
- **Plan C (`2026-09-28-cold-start-and-loop-hygiene.md`):** reconciled with spec §3.1 G5 and G7. Task 5 adds 11 tests, so A ends at 171 and every A count from Task 5 on is 9 higher. C's lines 102 and 174 expect 188 tests after Plans A and B (46 + 125 + 17), and line 102's "after Task 9" total is 252, citing "A's 162+9 = 171". C's relative-count rule (`N0`) is unchanged. C also carries B1's 404 wording: a search that races B's `delete_collection` gets `collection '<name>' not found` (`CollectionDeletedError`), not a 500.
- **Plan D (`2026-09-28-ingest-path.md`):** reconciled. D's "Suite counts" bullets, its Phase D1 pre-step and its Self-Review state N0 = 253 (252 without C's Task 12 test), from A's 171, B's 17 and C's 65. The example totals after each `N0 + k` are 261, 270, 275, 277, 280 and 281 "when N0 is 253". D's relative rule is unchanged. D's use of `compare.load`, `values`, `summary`, `verdict`, `RESOLUTION` and `RUN_FILE` is unchanged: `load` and `values` differ only for a session with a `build<N>` file, and only in `raggio-ivf`'s one-shot rows. D reads none of Task 5's new keys.
- **`IVF_BUILDS` (spec §3.1 A1), all plans:** an optional `session.sh` knob with default 1. No other plan's `session.sh` launch line, `compare.py` call or cross-plan name changes. Without it, `session.sh` runs exactly the commands it ran before the knob existed.
- **Plan F:** no mismatch found. Its `planF-cpu.py` records `cpu_ms_per_query` from the same cgroup `cpu.stat`, in its own files; bench.py's `cpu_ms_per_q_c` and `hybrid_cpu_ms_per_q_c` are different keys, and neither side reads the other's.
