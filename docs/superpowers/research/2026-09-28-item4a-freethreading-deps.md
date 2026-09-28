# item4a-freethreading-deps

## Summary
Item 4a (free-threaded CPython 3.14t dependency readiness) found one real runtime blocker and one dev-tooling blocker. Everything else is ready at the versions already in uv.lock.
- **Runtime blocker: turbovec 1.0.0.** It ships only cp39-abi3 wheels, and abi3 wheels cannot load on free-threaded Python, so uv builds it from the sdist (needs Rust 1.89 or newer). Once built, it declares itself GIL-free (PyO3 0.29 default). Verified by import on Windows and Linux x86-64.
- **Other runtime extensions are ready.** numpy 2.5.2, pydantic-core 2.46.4, httptools 0.8.0, uvloop 0.22.1, pyyaml 6.0.3, watchfiles 1.2.0 and websockets 17.0.1 all have cp314t wheels for manylinux aarch64 and x86_64. All of them keep the GIL off when imported (checked empirically on x86-64 only). The only Windows gaps are watchfiles 1.2.0 (fixed in 1.3.0) and uvloop (no Windows builds at all).
- **Dev blocker: orjson 3.12.0.** It refuses to build on free-threaded Python ("orjson v3.12.0 does not support free-threaded Python"), so a plain `uv sync --frozen` fails on 3.14t. orjson is only used in bench/ and no environment marker can exclude free-threaded builds (PEP 780 is still a Draft), so it should move to a separate `bench` group.
- **Tooling.**
  - uv.lock needs no change: the same lock synced on 3.14t and 3.14 and stayed byte-identical.
  - Local uv 0.5.24 must be upgraded; uv 0.9.0 or newer works, 0.12.19 is recommended.
  - The current base image tag (uv python3.12-bookworm-slim) is stale; it contains uv 0.9.30.
  - No free-threaded uv image exists, so the Dockerfile should install Python with `uv python install`. A candidate multi-stage Dockerfile builds and runs (324 MB, down from 395 MB).
- **GIL rule verified.** A test extension that does not declare GIL safety re-enables the GIL with a RuntimeWarning. `PYTHON_GIL=0` or `-X gil=0` suppresses that. The warning filter "error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" turns it into a hard failure without touching numpy's own RuntimeWarnings.
- **Python 3.14 compatibility of src/ is clean.** 46/46 tests pass on 3.14 and 3.14t. The only new noise is 3.13+ "unclosed database" ResourceWarnings from tests that never call stop().
- **Local numbers (Windows x86-64 laptop, 6 cores, WSL2 podman, one noisy run each):** concurrent hybrid search goes from 63-109 QPS with the GIL to 585-606 QPS free-threaded. With the GIL, concurrent hybrid is slower than serial, which looks like GIL contention. Vector search is within noise. The DGX results show the same pattern: hybrid concurrent 13.8 vs serial 8.6 QPS on 20 cores.
- **Recommendation:** one parameterized Dockerfile (PYTHON=3.14t for free-threaded, PYTHON=3.14 as the GIL control), keep uvloop/httptools, add a guard test, and run the ABAB DGX A/B protocol in the brief before making 3.14t the default.

## Brief
# Item 4a: Free-threaded CPython 3.14t dependency readiness for raggio

Labels used below:
- **LOCAL-WIN**: native Windows 11 x86-64 laptop (Xeon E-2226G, 6 cores).
- **LOCAL-WSL**: podman on WSL2 on the same laptop (6 visible cores, server and client on the same host).
- None of the numbers here come from the DGX.
- Scratch artifacts are in `D:\DEV\_scratch\probe-item4a\`. Nothing inside `D:\DEV\raggio` was modified.

---

## 1. Findings at a glance

1. **Runtime blocker: turbovec only.**
   - turbovec 1.0.0 publishes only `cp39-abi3` wheels plus an sdist (`uv.lock:958`).
   - abi3 wheels cannot load on free-threaded builds. The Python extensions howto says the limited API and stable ABI are not supported there, and PyO3 ignores `abi3` when targeting a free-threaded interpreter.
   - So uv builds turbovec from the sdist, which needs Rust 1.89 or newer (`turbovec-python/Cargo.toml` rust-version) and a C linker.
   - The built module keeps the GIL off. turbovec uses PyO3 0.29 with a bare `#[pymodule]` (`D:\DEV\_scratch\turbovec\turbovec-python\src\lib.rs:2151`), and PyO3 0.28+ modules default to `gil_used = false`.
   - Empirical results:
     - LOCAL-WIN produced `_turbovec.cp314t-win_amd64.pyd` (maturin 1.15.0). Importing it leaves `sys._is_gil_enabled()` False.
     - The LOCAL-WSL Linux x86_64 image confirms the same.
2. **Every other runtime extension has cp314t wheels at the locked version** for manylinux aarch64 and x86_64, and keeps the GIL disabled on import (empirical on x86-64).
3. **Dev-group blocker: orjson.**
   - orjson 3.12.0 (dev group, `pyproject.toml:15-18`) has no cp314t wheels.
   - Its sdist build aborts with `orjson v3.12.0 does not support free-threaded Python` (LOCAL-WIN, uv 0.12.19).
   - Result: `uv sync --frozen` (dev group is the default) fails on 3.14t.
   - `src/` and `tests/` never import orjson; only `bench/bench.py:22` and `bench/floor_probe.py:53` do.
   - PEP 508 has no free-threading marker (PEP 780 "ABI features as environment markers" is Draft), so a marker cannot exclude it.
4. **Lock**: no change needed.
   - `uv sync --frozen --python 3.14t` and `--python 3.14` both work with the existing uv.lock, which stays byte-identical.
   - The lock is universal over markers, not over wheel tags; turbovec simply falls back to its sdist.
5. **uv**:
   - Local uv 0.5.24 only knows the 3.14.0a4 download, so it must be upgraded.
   - Minimum sensible version is 0.9.0 (3.14 stable and default; free-threaded 3.14 allowed without special opt-in). Recommended: 0.12.19 (2026-09-24), which ships 3.14.0-3.14.7 plus +freethreaded and 3.15.0rc2.
   - `Dockerfile:1` uses `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`. That tag is frozen: bookworm tags stopped at 0.9.30, and `uv --version` inside the image says 0.9.30. Current tags are trixie only.
   - No `*3.14t*` uv images exist (checked all 7994 tags), so Python must come from `uv python install`.
6. **GIL re-enable rule: verified** with the docs and a negative-control extension (section 3).
7. **uvloop and httptools**: keep both. Both declare free-threading support at the locked versions.
8. **Python 3.14 compatibility**: clean (section 6).
9. **Local served A/B (LOCAL-WSL)**: hybrid concurrent search goes from 63-109 QPS with the GIL to 585-606 QPS free-threaded; vector search is within noise (section 7).
10. **Client-side free-threading race in httpcore 1.0.9.**
    - It occurs when one sync `httpx.Client` is shared across threads: `httpcore/_sync/http11.py:276` reads `self._expire_at` twice, check then use.
    - It reproduced as `TypeError: '>' not supported between instances of 'float' and 'NoneType'` (runbig_3.log) in my in-process test client.
    - Upstream fix PR https://github.com/encode/httpcore/pull/1090 (opened 2026-06-24) is still unmerged.
    - It does not affect raggio: the server uses `httpx.AsyncClient` on the event-loop thread (`src/raggio/embeddings.py:20`), and so does bench.py (`bench/bench.py:195,211,220,309,360`).
    - Rule for any free-threaded test client: one `httpx.Client` per thread.

---

## 2. Dependency table (from `D:\DEV\raggio\uv.lock`; PyPI JSON survey in `probe-item4a\pypi_wheels.jsonl`)

Column notes:
- **cp314t wheels** refers to the locked version, in the order lin-aarch64 / lin-x86_64 / win_amd64.
- **Declares GIL-free**:
  - "yes (emp)" means that in a fresh subprocess on 3.14.7t, importing the module left `sys._is_gil_enabled()` False with no RuntimeWarning (LOCAL-WIN and/or LOCAL-WSL Linux x86_64).
  - aarch64 was not tested empirically: podman has no qemu here. The same source compiles the same `Py_mod_gil` slot on every platform.

| Package | Locked (latest) | Kind | cp314t wheels | Declares GIL-free | First free-threaded release | Action |
|---|---|---|---|---|---|---|
| annotated-doc | 0.0.5 | pure | n/a | n/a | - | none |
| annotated-types | 0.8.0 | pure | n/a | n/a | - | none |
| anyio | 4.14.2 (4.15.1) | pure | n/a | classifier "Free Threading :: 2 - Beta" | - | none |
| certifi | 2026.7.22 | pure | n/a | n/a | - | none |
| click | 8.4.2 (8.5.0) | pure | n/a | n/a | - | none |
| colorama | 0.4.6 | pure, win32 only | n/a | n/a | - | none |
| fastapi | 0.141.1 | pure | n/a | n/a | - | none |
| h11 | 0.16.0 | pure | n/a | n/a | - | none |
| httpcore | 1.0.9 | pure | n/a | n/a | - | Sync-pool race (PR #1090); server unaffected (AsyncClient) |
| httptools | 0.8.0 | Cython ext | Y / Y / Y | yes (emp): parser, url_parser | cp314t since 0.8.0 ("Mark cython module as free-threading compatible (#139)") | none |
| httpx | 0.28.1 | pure | n/a | n/a | - | none |
| idna | 3.19 (3.20) | pure | n/a | n/a | - | none |
| numpy | 2.5.2 (2.5.3) | C ext | Y / Y / Y | yes (emp) | cp313t 2.1.0 (2024-08-18), cp314t 2.3.2; 2.5.2 has no cp313t | none; caveats in section 4 |
| pydantic | 2.13.4 (2.13.5) | pure | n/a | n/a | tracker: 2.11.0 | none |
| pydantic-core | 2.46.4 (2.49.0) | PyO3 ext | Y / Y / Y | yes (emp) | cp313t 2.29.0 (PR #1628, 2025-02-07), cp314t 2.35.0 (2025-06-11); 2.41+ dropped cp313t | none |
| python-dotenv | 1.2.3 | pure | n/a | n/a | - | none |
| pyyaml | 6.0.3 | C ext (`_yaml`) | Y / Y / Y | yes (emp): yaml._yaml | cp314t since 6.0.3 | none |
| starlette | 1.6.0 (1.7.0) | pure | n/a | n/a | - | none |
| **turbovec** | 1.0.0 | PyO3 0.29 ext, `abi3-py39` | **N / N / N** (abi3 only + sdist) | yes (emp, built from sdist): PyO3 0.28+ default | none published | **BLOCKER**: build from sdist (Rust 1.89+) or get upstream cp314t wheels |
| typing-extensions | 4.16.0 | pure | n/a | n/a | - | none |
| typing-inspection | 0.4.4 | pure | n/a | n/a | - | none |
| uvicorn | 0.52.4 (0.54.0) | pure | n/a | n/a | - | none |
| uvloop | 0.22.1 | Cython ext | Y / Y / - (no Windows builds; excluded on win32 by the lock marker) | yes (emp, Linux x86_64): uvloop, uvloop.loop | 0.22.0 "Add free-threading support (#693)"; cp314t from 0.22.x | none |
| watchfiles | 1.2.0 (1.3.0) | PyO3 ext | Y / Y / **N** (1.3.0: Y) | yes (emp; the Windows copy was built from the sdist) | cp313t/cp314t since 1.1.0 | Optional bump to >=1.3.0 so Windows 3.14t does not need Rust |
| websockets | 17.0.1 (17.1) | C speedups | Y / Y / Y | yes (emp): websockets.speedups | cp314t since 16.0 | none |
| orjson (dev) | 3.12.0 | Rust ext | N / N / N | **sdist build refuses on free-threaded** | none | **Dev blocker**: move to a `bench` group |
| pytest (dev) | 9.1.1 | pure | n/a | n/a | - | none |
| markupsafe (docs) | lock has cp314t | C ext | Y | not probed | - | docs group only |

Other sources:
- **py-free-threading tracker** (https://py-free-threading.github.io/tracking/, updated 2026-09-25): numpy 2.1.0, pydantic 2.11.0, pydantic-core 2.29.0, uvloop 0.22.1, httptools 0.8.0, PyYAML 6.0.3, Cython 3.1.0, PyO3 0.23, maturin 1.7.5, uv 0.4.24. uvicorn, fastapi, starlette, httpx, anyio, websockets and watchfiles are not listed.
- **Stdlib extensions** probed with no GIL re-enable: sqlite3, unicodedata, hashlib.
- **Full serving import path in the free-threaded image (LOCAL-WSL).** I ran `uvicorn.main` plus `Config('raggio.app:create_app', factory=True).load()`. It loads HttpToolsProtocol, WebSocketsSansIOProtocol, the uvloop factory, and `uvicorn.supervisors`. Result: `Py_GIL_DISABLED 1`, `gil_enabled False`.
- **watchfiles is imported at every uvicorn start, even without `--reload`.** The try-import is at `uvicorn/supervisors/__init__.py:11-14`. So it must be GIL-free or uninstalled; it is GIL-free.

---

## 3. GIL re-enable rule (verified)

**Docs:**
- https://docs.python.org/3.14/howto/free-threading-python.html
  - Importing an extension that does not declare support re-enables the GIL and emits a RuntimeWarning.
  - Check the state with `sys._is_gil_enabled()`.
  - The single-thread overhead is stated as roughly 1-8% in 3.14.
- https://docs.python.org/3.14/howto/free-threading-extensions.html
  - Covers `Py_mod_gil` / `Py_MOD_GIL_NOT_USED` and `PyUnstable_Module_SetGIL`.
  - The limited API and stable ABI are not supported on free-threaded builds.
- https://docs.python.org/3.14/using/cmdline.html (`PYTHON_GIL`, `-X gil`)
  - `PYTHON_GIL=0` or `-X gil=0` forces the GIL off.
  - `-X gil` takes precedence over the environment variable.
- PyO3 (via Context7, pyo3.rs main, free-threading guide):
  - "Since PyO3 0.28, modules are assumed to be thread-safe by default."
  - `abi3` is ignored for free-threaded targets.
  - abi3t (PEP 803) requires 3.15+.

**Negative control** (`probe-item4a\gilctl\`): a PyO3 crate with `#[pymodule(gil_used = true)]`, built as `gilctl.cp314t-win_amd64.pyd`.
- Default import prints: `RuntimeWarning: The global interpreter lock (GIL) has been enabled to load module 'gilctl.gilctl', which has not declared that it can run safely without the GIL. To override this behavior and keep the GIL disabled (at your own risk), run with PYTHON_GIL=0 or -Xgil=0.` After that, `sys._is_gil_enabled()` is True.
- With `PYTHON_GIL=0` or `-X gil=0`: no warning, and `_is_gil_enabled()` stays False.
- With `-W "error:The global interpreter lock (GIL) has been enabled:RuntimeWarning"`: the import fails and the process exits 1. numpy's unrelated RuntimeWarnings (for example divide-by-zero) stay plain warnings.
- **Recommendation:**
  - Ship the free-threaded image with `PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning"`.
  - Never set `PYTHON_GIL=0` in production: it would silently run an undeclared extension without a GIL.

---

## 4. Event loop and HTTP parser cost, uvicorn[standard], pydantic-core, numpy

**What uvicorn[standard] pulls in** (`uv.lock`, uvicorn optional-dependencies):
- httptools, python-dotenv, pyyaml
- uvloop (marker: not PyPy, not cygwin, not win32)
- watchfiles, websockets
- colorama (win32)

**Free-threading status of those packages:**
- All of them have free-threaded wheels at the locked versions.
- watchfiles is only functionally needed for `--reload`, but it is still imported on every start (section 2).
- websockets is imported because the default `--ws auto` loads it; raggio has no websocket routes (a grep of `src/` found none). Adding `--ws none` to the CMD would skip it, but that is not needed for free-threading.
- uvicorn 0.52+ also offers an alpha `--http zttp` (zttp 0.0.34 has cp314t wheels and keeps the GIL off). Not recommended: it is alpha.

**Published comparisons:**
- uvicorn publishes no official httptools-vs-h11 benchmark.
- A Markaicode article claims 50.2k vs 15.1k req/s on hello-world; the source is dubious (UNVERIFIED).
- The uvloop README's "2-4x faster than asyncio" dates from 2016 and is marketing.
- The only relevant third-party analysis found (sephirothx/sketchy PR #1031) says the gain comes mostly from uvloop. It also notes that httptools accepts unbounded headers while h11 caps them at 16 KiB.
- For raggio, search cost dominates the roughly 0.7 ms HTTP floor (the `/healthz` round trip).

**Local measurement:** out-of-process, LOCAL-WSL, single runs (section 7).
- Vector-search QPS varies 545-976 across configurations with no consistent loop/parser ordering, so it is noise-dominated.
- Hybrid search is dominated by the GIL effect, not by the parser.

**Conclusion:**
- Keep `uvicorn[standard]` with loop/http `auto` (uvloop + httptools). Nothing needs dropping for free-threading.
- Add `--loop asyncio --http h11` only as an optional DGX A/B arm.

**pydantic-core timeline:**
- First free-threaded wheels: cp313t in 2.29.0 (2025-02-07, PR https://github.com/pydantic/pydantic-core/pull/1628, "Support free-threaded Python 3.13t").
- cp314t since 2.35.0 (2025-06-11).
- 2.41+ dropped cp313t, and numpy 2.5.2 has no cp313t wheels either. So **3.13t is not a viable target**; 3.14t is.

**numpy caveats** (https://numpy.org/doc/stable/reference/thread_safety.html):
- Free-threading support has been "experimental" since 2.1.
- Object arrays are not protected; do not mutate a shared array from several threads.
- `np.random.Generator` is not thread-safe. raggio's `self._cal_rng` (`src/raggio/store.py:612`) is shared, but it is only used by the single worker in `_feed_calibration` (`store.py:790`, `store.py:805`). Hand this to item 4b.

---

## 5. Tooling (uv, venv, lock, Docker)

**uv versions with free-threaded milestones** (changelogs `changelogs/0.4.x.md` and `0.9.x.md` at github.com/astral-sh/uv):

| uv version | Change |
|---|---|
| 0.4.16 | "Allow requesting free-threaded Python interpreters" |
| 0.4.21 | "Add support for managed installations of free-threaded Python" |
| 0.4.28 | `+freethreaded` request suffix |
| 0.9.0 | "Python 3.14 is now the default stable version"; "Allow use of free-threaded variants in Python 3.14+ without explicit opt-in" (PR #16142). The GIL build is still preferred: `uv python install 3.14` installs the GIL build. |
| 0.9.8 | "Allow Python requests to include `+gil`" (a discovery specifier only: `uv python install 3.14+gil` fails with "No download found", verified) |
| 0.9.26 | Clearer error message for abi3 wheels on free-threaded Python |
| 0.12.19 | Latest (2026-09-24); verified locally at `probe-item4a\uvbin\uv.exe`, global uv left untouched |

**venv:**
- A 3.14t venv has a different site-packages path (`lib/python3.14t/...`) and different wheel tags.
- To keep a 3.12 or 3.14 dev venv alongside it, use a separate environment: `UV_PROJECT_ENVIRONMENT=.venv-ft`. Add `.venv*` to `.gitignore` (which currently has only `.venv/`) and `.dockerignore`.

**Lock and markers:**
- The lock is universal over markers; no lock changes are needed.
- No PEP 508 marker selects free-threaded builds (PEP 780 is Draft: https://peps.python.org/pep-0780/).
- `tool.uv.required-environments` works on markers only and cannot require cp314t tags.

**Docker:**
- No free-threaded uv images exist.
- Use the distroless `ghcr.io/astral-sh/uv:0.12.19` image for `/uv` and `/uvx`, `UV_PYTHON_INSTALL_DIR=/python`, `uv python install 3.14t`, and copy `/python` plus `/app` into `debian:trixie-slim`. This follows the uv Docker guide's multi-stage pattern via Context7: "Using uv in Docker" and "Managed Python in Docker"; the Python install directory must be copied along with the venv.

**The runtime changes** when moving off `python3.12-bookworm-slim` (checked in the images):
- Python 3.12.12 with Debian SQLite 3.40.1 becomes python-build-standalone 3.14.7 with bundled SQLite 3.53.1 (FTS5, `threadsafety` 3).
- This SQLite change confounds comparisons against historical DGX numbers. Compare free-threaded against the 3.14 GIL control built from the same Dockerfile.

**Sizes (LOCAL-WSL x86_64):**

| Image | Size | Notes |
|---|---|---|
| orig312 (current Dockerfile) | 395 MB | |
| gil (PYTHON=3.14) | 322 MB | abi3 turbovec wheel |
| ft (PYTHON=3.14t) | 324 MB | turbovec built from sdist; the builder's dependency sync took 46.7 s including the Rust build, on a 6-core laptop |

**turbovec sdist details** (downloaded and verified: sha256 matches the lock):
- Contains `Cargo.lock` and a `[profile.release]` with `lto = true`, `codegen-units = 1`, `opt-level = 3`.
- Does **not** contain `.cargo/config.toml`.
  - Consequence: x86_64 sdist builds lose the x86-64-v2 baseline for non-kernel code. The SIMD kernels are still selected at runtime.
  - aarch64 is unaffected; that config applies only to x86_64.
- No BLAS dependency, even though the release workflow installs openblas.
- The published aarch64 wheel comes from maturin-action with `--release --locked` and no aarch64 target-cpu override (`D:\DEV\_scratch\turbovec\.github\workflows\release-pypi.yml:57-63`). So a native DGX sdist build should match it apart from the compiler version.

**Upstream path to cp314t wheels:**
- Add a free-threaded interpreter to maturin-action (for example `args: --release --locked --out dist -i python3.14t`) in the Linux and Windows jobs.
- No such upstream issue exists yet. Issue #163 mentions "future free-threaded builds" falling back to the sdist.
- The maintainer would have to file this; agents did not post anything.

---

## 6. Python 3.14 compatibility grep of src/

**asyncio:**
- No `get_event_loop`, `set_event_loop_policy` / policies, `asyncio.iscoroutinefunction`, or `asyncio.run` in `src/`.
- Uses `asyncio.Condition` (`store.py:221`), `create_task` (`app.py:89`), `to_thread` (`app.py:225`) and `gather`. All fine on 3.14.
- uvicorn 0.52.4 uses `loop_factory`, not the deprecated policies.
- The tests use `asyncio.run` (fine).

**sqlite3:**
- Only `?` placeholders (3.14 deprecates named placeholders supplied as a sequence; not applicable).
- `check_same_thread=` is passed as a keyword (`store.py:274`).
- Legacy transaction control with explicit `.commit()`; the 3.14 default is unchanged.
- `executescript("BEGIN;...COMMIT;")` at `store.py:343-348` is fine.
- On 3.13+, sqlite3 emits `ResourceWarning: unclosed database` for connections that are never closed.
  - About 53 per test run: 25 from `store.py:274` (`open_meta_db`) and 28 from `store.py:1070` (per-thread `_rdb`).
  - They come from tests that never call `stop()`; there are 0 on 3.12.
  - Production closes connections in `stop()` (`store.py:664-667`).

**pathlib:** no deprecated APIs used.

**datetime:** `datetime.now(timezone.utc)` (`store.py:82`); no `utcnow` or `utcfromtimestamp`.

**unicodedata:** 3.14 moves to Unicode 16.0. `_fold` can only differ for newly assigned code points. Low risk, UNVERIFIED on real corpora.

**Test results (LOCAL-WIN):**
- 46 passed on 3.14.7t (about 10 s; GIL still disabled at session end) and on 3.14.7.
- With `-W error::DeprecationWarning`, and the Starlette testclient/httpx deprecation ignored (it is present on 3.12 too): still 46 passed on both.
- So raggio and its dependencies emit no 3.14 DeprecationWarnings.

---

## 7. Local served measurements (LOCAL-WSL, NOT DGX)

**Out-of-process setup:**
- A server container and a separate client container share one podman pod, so they talk over localhost.
- The client is always a free-threaded process with one `httpx.Client` per thread.
- 20,000 random 256-dim vectors, each with a 12-word text. Concurrency 16, 1,200 concurrent requests per mode. One run per row.
- Scripts: `probe-item4a\serve_probe.py`, `client_probe.py`, `oop_run.sh`; results in `oop_results.log`.
- Loop/http = `auto` means uvloop + httptools.

| Server image | Loop/http | GIL after traffic | Ingest vec/s | healthz ms | Vector p50 ms | Vector QPS x16 | Hybrid p50 ms | Hybrid QPS x16 |
|---|---|---|---|---|---|---|---|---|
| orig312 (3.12.12, SQLite 3.40.1) | auto | on | 1260 | 0.71 | 3.40 | 678 | 4.23 | 63 |
| gil (3.14.7 PBS, SQLite 3.53.1) | auto | on | 1303 | 0.75 | 2.44 | 844 | 3.75 | 109 |
| gil | asyncio/h11 | on | 1114 | 1.02 | 2.64 | 578 | 4.31 | 68 |
| ft (3.14.7t) | auto | **off** | 1117 | 0.71 | 3.52 | 545 | 4.48 | **585** |
| ft | asyncio/h11 | **off** | 1381 | 0.80 | 3.37 | 976 | 3.82 | **606** |

**Reading:**
- With the GIL, concurrent hybrid QPS (63-109) is *below* the serial rate implied by its own p50 (about 1/3.75 ms, roughly 267/s). That signature suggests GIL contention between the event loop and the to_thread workers.
- Free-threaded removes it: about 5.4x over the gil/auto row on 6 cores.
- Vector-search figures are noise-dominated. The earlier in-process free-threaded h11 runs at N=3000 ranged 788-1110 QPS.
- Serial p50 is 0.1-1.1 ms higher on free-threaded, consistent with the documented overhead but inside the noise.

**Earlier in-process runs** (the client shares the server's interpreter, so biased; `served_results_podman.txt`) point the same way:
- Hybrid x16: 99 (orig312 auto), 131 (gil auto), 443 (ft auto), 551 (ft h11).

**DGX context** (existing data, not new; `D:\DEV\raggio\bench\results-arxiv.md:16-26`, 552k x 1024 dims, concurrency 8, 20 cores):

| Mode | Serial QPS | Concurrent QPS | Scaling |
|---|---|---|---|
| raggio flat vector | 42 | 129 | about 3x |
| raggio flat hybrid | 8.6 | 13.8 | about 1.6x |

- That hybrid scaling is the headroom free-threading targets.
- ADR `docs/adr/0001-performance-optimization-decisions.md:49` already names "free-threaded Python" as the remaining lever. Line 18 assumes "SQLite releases the GIL", which the GIL rows above show is not enough.

---

## 8. Design options

| Option | Description | Verdict |
|---|---|---|
| **A (recommended)** | One parameterized multi-stage Dockerfile. `ARG PYTHON` selects 3.14t or 3.14. uv-managed Python goes into `/python`, turbovec is built from the sdist in a `rust:1-slim-trixie` builder, and the runtime is `debian:trixie-slim` with the fail-fast `PYTHONWARNINGS`. The build fails if the interpreter flavour is wrong or any serving import re-enables the GIL. Built and run on LOCAL-WSL x86_64: `probe-item4a\ctx\Dockerfile.ft`. | **Recommended** |
| B | Wait for upstream turbovec cp314t wheels, then drop Rust from the builder. | Pursue in parallel (user files the request); do not block on it. |
| C | Separate `Dockerfile.ft` next to the current Dockerfile. | Rejected: two files drift, and the control image would not share the Python build or SQLite. |
| D | Official `python` or uv `python3.14-*` images. | Rejected: no free-threaded variants exist in uv's tags. Docker Hub free-threaded variants are UNVERIFIED and would not match. |
| E | Target 3.13t. | Rejected: pydantic-core 2.41+ and numpy 2.5.2 have no cp313t wheels. |
| F | `PYTHON_GIL=0` escape hatch. | Rejected as a default: it hides undeclared extensions. |

**Candidate Dockerfile** (option A, adds a build-time assertion and an optional strict-control ARG to the tested `ctx/Dockerfile.ft`):

```dockerfile
ARG UV_VERSION=0.12.19
# 3.14t = free-threaded (GIL off); 3.14 = GIL control. Same python-build-standalone build and SQLite either way.
ARG PYTHON=3.14t

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM docker.io/library/rust:1-slim-trixie AS builder
COPY --from=uv /uv /uvx /bin/
ARG PYTHON
# optional strict control: --build-arg UV_NO_BINARY_PACKAGE=turbovec compiles turbovec from sdist on the GIL image too
ARG UV_NO_BINARY_PACKAGE
ENV UV_PYTHON_INSTALL_DIR=/python UV_PYTHON_PREFERENCE=only-managed UV_PYTHON=${PYTHON} \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN uv python install ${PYTHON}
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev
# fail the build if the interpreter flavour is wrong or any serving import re-enables the GIL
RUN want=$(case "$PYTHON" in *t) echo 1;; *) echo 0;; esac) && \
    /app/.venv/bin/python -W "error:The global interpreter lock (GIL) has been enabled:RuntimeWarning" -c "import sys, sysconfig, raggio.app, uvicorn.supervisors, uvicorn.loops.uvloop, uvicorn.protocols.http.httptools_impl, uvicorn.protocols.http.h11_impl, uvicorn.protocols.websockets.websockets_sansio_impl; ft = sysconfig.get_config_var('Py_GIL_DISABLED') or 0; assert ft == $want, (ft, '$PYTHON'); assert sys._is_gil_enabled() == (not ft)"

FROM docker.io/library/debian:trixie-slim
COPY --from=builder /python /python
COPY --from=builder /app /app
WORKDIR /app
RUN useradd -m app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data PATH="/app/.venv/bin:$PATH" \
    PYTHONWARNINGS="error:The global interpreter lock (GIL) has been enabled:RuntimeWarning"
VOLUME /data
EXPOSE 8000
# no per-request access log (ADR 0001)
CMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
```

Verification notes:
- Verified on LOCAL-WSL: an `ARG` declared without a default is **not** exported to `RUN` unless passed. So `UV_NO_BINARY_PACKAGE` is unset by default and `turbovec` when passed.
- The runtime image no longer contains `uv`; nothing in the bench uses uv inside the container.
- The assertion `RUN` step itself is new and has not been built yet. The import list and the `-W` filter were each checked separately.

**Minimal viable dependency configuration:**
- **3.14t image**: the existing `pyproject.toml` and `uv.lock` unchanged, plus `--no-dev` (as today), plus a Rust toolchain in the builder for turbovec.
- **3.14 GIL control**: identical, with `--build-arg PYTHON=3.14`. It uses the abi3 turbovec wheel, or the sdist with `--build-arg UV_NO_BINARY_PACKAGE=turbovec`.
- **Local 3.14t dev and tests**: needs orjson moved out of `dev` (or `--no-dev` plus a separately installed pytest), and a Rust toolchain on Windows (turbovec, and watchfiles 1.2.0 unless bumped to 1.3.0).

---

## 9. Upgrade steps (exact commands)

**Local (Windows, Git Bash).** The existing uv lives at `~/.local/bin/uv` (standalone-installer layout).

```bash
uv self update 0.12.19            # was 0.5.24; if uv was installed another way, use that installer
uv --version                      # expect: uv 0.12.19
uv python install 3.14t 3.14      # cpython-3.14.7+freethreaded and cpython-3.14.7
# before the orjson split (plain `uv sync` fails on 3.14t because of orjson):
UV_PROJECT_ENVIRONMENT=.venv-ft uv sync --frozen --python 3.14t --no-dev
uv pip install --python .venv-ft pytest
.venv-ft/Scripts/python -m pytest -q        # expect 46 passed (plus the new free-threading tests)
# after moving orjson to a `bench` group:
UV_PROJECT_ENVIRONMENT=.venv-ft uv run --frozen --python 3.14t pytest -q
uv run --frozen --python 3.14 pytest -q                  # GIL control
uv run --frozen --python 3.12 pytest -q                  # current baseline
```

**Lock:**
- No change for the interpreter switch; `uv lock --check` must stay clean.
- Only the orjson group move (task T2) regenerates it: `uv lock`, then `git diff uv.lock` should show only group membership.

**Dockerfile:** replace it with the option A file.
- `podman build --build-arg PYTHON=3.14t -t raggio:ft .`
- `podman build --build-arg PYTHON=3.14 -t raggio:gil .`

---

## 10. Proposed ordered task list (TDD)

**T1. uv floor (tooling)**
- Files:
  - Optionally `pyproject.toml`: `[tool.uv] required-version = ">=0.9.0"`.
  - The README / `docs/benchmark*.md` prerequisites line.
- Test: `uv lock --check` and `uv sync --frozen` succeed with 0.12.19.
- Acceptance: `uv --version` is 0.9.0 or newer on the dev laptop and the DGX, and uv.lock is unchanged. With `required-version`, an older uv refuses to run with a clear message. Whether that setting touches uv.lock is UNVERIFIED; confirm `git diff --exit-code uv.lock`.

**T2. Move orjson to a `bench` dependency group**
- Files:
  - `pyproject.toml:14-21`: `dev = ["pytest>=8"]`, `bench = ["orjson>=3.12.0"]`.
  - `uv.lock` (via `uv lock`).
  - The bench.py docstring at `bench/bench.py:1-5`.
  - The commands in `docs/benchmark.md:98-100` and `docs/benchmark-arxiv.md:241-246` become `uv run --group bench python bench/bench.py ...`.
  - `bench/floor_probe.py` usage note.
- Red: `UV_PROJECT_ENVIRONMENT=.venv-ft uv sync --frozen --python 3.14t` fails with "orjson v3.12.0 does not support free-threaded Python".
- Green: the same command succeeds.
- Acceptance: `uv lock --check` is clean. `uv run --group bench --python 3.12 python -c "import orjson"` works. The docs CI (`uv sync --only-group docs`) is unaffected.

**T3. Free-threading guard tests**
- Files: new `tests/test_freethreading.py`.
- Design:
  - `pytestmark = skipif(not sysconfig.get_config_var("Py_GIL_DISABLED"))`.
  - Parametrize over the modules: `raggio.app`, `raggio.store`, `uvicorn.supervisors`, `uvicorn.protocols.http.httptools_impl`, `uvicorn.protocols.http.h11_impl`, `uvicorn.protocols.websockets.websockets_sansio_impl`, `uvicorn.protocols.websockets.websockets_impl`, `numpy`, `pydantic_core`, `yaml`, `turbovec`, plus `uvloop` and `uvicorn.loops.uvloop` when `sys.platform != "win32"`.
  - Each case runs `subprocess.run([sys.executable, "-W", "error:The global interpreter lock (GIL) has been enabled:RuntimeWarning", "-c", f"import {m}, sys; sys.exit(sys._is_gil_enabled())"], env=env)`, where `env` is scrubbed of `PYTHON_GIL` and `PYTHONWARNINGS`. Assert returncode 0 and no GIL warning in stderr.
  - A second test proves the harness can fail: the same probe with `-X gil=1` must return 1 (this is the red step).
  - Optional `tests/conftest.py` `pytest_sessionfinish` hook: on free-threaded builds, report failure if `sys._is_gil_enabled()` became True during the run.
- Acceptance:
  - 3.14t: all pass.
  - 3.12 and 3.14: skipped.
  - Removing any module's GIL-free declaration, or injecting the `gilctl` negative control from `probe-item4a\gilctl`, turns the suite red.
- Note: `.dockerignore` excludes `tests/`, so the image check lives in the Dockerfile (T4).

**T4. Parameterized Dockerfile (option A)**
- Files: `Dockerfile`; `.dockerignore` (add `.venv*`); `.gitignore` (add `.venv*/`); build commands in `README.md:46-47`, `docs/benchmark.md:90`, `docs/benchmark-arxiv.md:226`.
- Tests: shell acceptance only.
  - `podman build --build-arg PYTHON=3.14t -t raggio:ft .` passes, including the assertion step.
  - `podman build --build-arg PYTHON=3.14 -t raggio:gil .` passes.
  - `podman run --rm raggio:ft python -c "import sys,sysconfig;print(sysconfig.get_config_var('Py_GIL_DISABLED'),sys._is_gil_enabled())"` prints `1 False`. The gil image prints `0 True`.
  - `podman run -d -p 8000:8000 -e ROOT_API_KEY=k raggio:ft`, then `curl -s localhost:8000/healthz` returns 200.
  - Image size is no larger than the current one (LOCAL-WSL: 324 MB vs 395 MB).
- The default of `ARG PYTHON` is a user decision (see decisions).

**T5. ResourceWarning cleanup on 3.13+ (optional, independent)**
- Files: the fixtures in `tests/test_api.py`, `tests/test_store_optimizations.py`, `tests/test_ivf_index.py` that create stores or collections, or TestClients without lifespan exit, and never call `stop()`.
- Test: `uv run --python 3.14 pytest -q -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning`.
- Acceptance: 46 passed and zero "unclosed database" warnings on 3.14 and 3.14t, with no production code change.

**T6. Optional dependency bumps**
- `watchfiles>=1.3.0` gives Windows cp314t wheels, so Windows 3.14t no longer needs Rust for it.
- Do not bump other packages in the same change, to keep the A/B clean.
- Acceptance: `uv lock` diff is limited to watchfiles, and T3 stays green.

**T7. DGX A/B (human executor, section 11), then ADR**
- Files: `docs/adr/0004-free-threaded-runtime.md` and `bench/results-ft-ab-*.md`.
- Acceptance:
  - The thresholds agreed in the user decisions are met.
  - `mkdocs build --strict` passes if the docs nav changes.

**T8. (Conditional on T7) Flip the Dockerfile default to `PYTHON=3.14t`** and update the README.

**Hand-off to item 4b** (free-threading thread-safety audit, not this item):
- Dict caches mutated from worker threads: `store.py:581`, `store.py:585`, and the eviction `self._allow_cache.pop(next(iter(self._allow_cache)), None)` at `store.py:1188`. Iterating a dict while another thread mutates it can raise under free-threading.
- `_cal_rng` (`store.py:612`); `db_lock` discipline (`store.py:616`); per-thread read connections via `threading.local` (`store.py:593`, `store.py:1065-1075`).
- The `asyncio.to_thread` default pool size, min(32, cpu+4), which is 24 on the DGX.

---

## 11. DGX measurement protocol (for a human executor; agents have no SSH)

```bash
# 0. host facts (record in the report)
uname -m; nproc; podman --version; uv --version; git -C <raggio-checkout> rev-parse --short HEAD
cd <raggio-checkout>

# 1. build both images natively on aarch64 from the same commit
podman build --build-arg PYTHON=3.14t -t raggio:ft .
podman build --build-arg PYTHON=3.14  -t raggio:gil .
# optional strict control (turbovec compiled from sdist, like the ft image):
podman build --build-arg PYTHON=3.14 --build-arg UV_NO_BINARY_PACKAGE=turbovec -t raggio:gil-src .

# 2. aarch64 empirical GIL check: closes the only unverified gap in the dependency table
for t in ft gil; do podman run --rm -e ROOT_API_KEY=x -e DATA_DIR=/tmp raggio:$t python -c "import sys,sysconfig,raggio.app,uvicorn.supervisors,uvicorn.loops.uvloop,uvicorn.protocols.http.httptools_impl,uvicorn.protocols.websockets.websockets_sansio_impl,turbovec,numpy,pydantic_core,yaml,uvloop,websockets.speedups,watchfiles; print('$t', sys.version.split()[0], sysconfig.get_config_var('Py_GIL_DISABLED'), sys._is_gil_enabled())"; done
# expected: "ft 3.14.7 1 False" and "gil 3.14.7 0 True"

# 3. A/B in ABAB order, fresh volume and fresh checkpoints per run (bench.py merges bench/results-partial.json)
for run in 1 2; do for t in gil ft; do
  rm -f bench/results-partial.json bench/fingerprint-raggio.json bench/fingerprint-raggio-ivf.json
  podman rm -f bench-tv; podman volume rm -f bench-tv
  podman run -d --name bench-tv --memory 4g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench raggio:$t
  PYTHONUNBUFFERED=1 uv run --group bench --python 3.12 python bench/bench.py --engine raggio --reingest \
    --limit <N from docs/benchmark-arxiv.md> --concurrency 8 --out bench/results-ft-ab-$t-$run.md \
    --caps-note "raggio:$t capped at 4 GiB (PYTHON=$([ $t = ft ] && echo 3.14t || echo 3.14))"
  PYTHONUNBUFFERED=1 uv run --group bench --python 3.12 python bench/bench.py --engine raggio-ivf \
    --limit <N> --concurrency 8 --out bench/results-ft-ab-$t-$run.md \
    --caps-note "raggio:$t capped at 4 GiB"
done; done
# (before T2 lands, drop "--group bench": orjson is still in the dev group)

# 4. optional arms, same loop body with a changed image or CMD:
#    - concurrency sweep: --concurrency 16 and 20 for raggio:ft and raggio:gil
#    - parser/loop arm: podman run ... raggio:ft uvicorn raggio.app:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log --loop asyncio --http h11
#    - strict control: raggio:gil-src in place of raggio:gil

# 5. after each run: confirm the ft server kept the GIL off (a warning would have been fatal) and check memory
podman logs bench-tv 2>&1 | grep -i -E "global interpreter lock|Traceback" || echo clean
podman stats bench-tv --no-stream
```

**Report:** for each metric row of bench.py, give the ft/gil ratio as the median of the two runs. In particular: QPS concurrent, p95 under concurrency, hybrid QPS concurrent, serial p50/p95, filtered p50, ingest vec/s, IVF build time, memory after ingest and under load, cold start, recall@10, hybrid text-hit@10.

**Suggested acceptance** (user decision):
- Hybrid QPS concurrent at least 1.5x (ft vs gil).
- Vector QPS concurrent at least 1.0x.
- Serial p50 regression at most 10% (docs: 1-8% single-thread overhead).
- recall@10 and hybrid text-hit identical within 0.002.
- Memory under load at most 1.25x and under the 4 GiB cap (PEP 779 cites about 15-20% higher memory for free-threaded builds).
- Zero errors, no GIL warning in the logs.

Do **not** compare against the historical 3.12 results: the SQLite version (3.40.1 vs 3.53.1) and the Python build both change.

---

## 12. Risks

- **Correctness under true parallelism (item 4b):** the dict caches (`store.py:581/585/1188`), the shared RNG (`store.py:612`), and anything that relied on the GIL implicitly. The hybrid-path races only appear under free-threading. The A/B must check recall and hit-rate, not just speed.
- **Unverified on aarch64:** the aarch64 GIL declarations (step 2 of the DGX protocol closes this) and the turbovec sdist build on aarch64 (Rust image is multi-arch; not run here).
- **turbovec sdist build cost:**
  - The builder needs Rust and the build is LTO with codegen-units=1: 46.7 s on the laptop for the dependency sync including the turbovec build; the build is cached per layer.
  - The x86_64 sdist builds lack the x86-64-v2 baseline (no `.cargo/config.toml` in the sdist). Irrelevant on the DGX.
- **Documented free-threading costs:** 1-8% single-thread overhead (3.14 docs) and about 15-20% more memory (PEP 779). The 4 GiB cap should still hold, but measure.
- **numpy:** free-threading support is "experimental" per its docs; never share mutable arrays across threads.
- **httpcore 1.0.9 sync-pool race:** affects only shared sync clients. Any future threaded test client must use one Client per thread.
- **Fail-fast side effect:** the `PYTHONWARNINGS` filter turns a lazily imported undeclared extension into a 500 on that request. That is intended; the build-time assertion catches it earlier for the known import set.
- **Stale base tag:** keeping the current `python3.12-bookworm-slim` tag means uv stays frozen at 0.9.30. Pin the `UV_VERSION` build-arg and bump it deliberately.
- **Windows dev on 3.14t needs Rust:** for turbovec, and for watchfiles 1.2.0 unless bumped.
- **uvicorn zttp** is alpha; do not adopt.


## User decisions
- Target interpreter: 3.14t rather than 3.13t. Recommended: 3.14t. 3.13t is not viable because pydantic-core 2.41+ and numpy 2.5.2 publish no cp313t wheels.
- turbovec supply: build from the sdist in a Rust builder stage now, or wait for upstream cp314t wheels. Recommended: build from the sdist now. In parallel, you (not an agent) could file a turbovec issue or PR adding a free-threaded interpreter (for example `-i python3.14t`) to the maturin-action jobs in release-pypi.yml.
- orjson: move it from the `dev` group to a new `bench` group, which changes pyproject.toml, uv.lock and the bench docs commands, or keep it and always use `--no-dev` plus a separately installed pytest on 3.14t. Recommended: move it to a `bench` group.
- Default for `ARG PYTHON` in the new Dockerfile, before and after the DGX A/B. Recommended: land the parameterized Dockerfile with `PYTHON=3.14` (GIL) as the interim default, which already moves production from 3.12 with SQLite 3.40.1 to 3.14.7 with SQLite 3.53.1. Flip to `3.14t` only after the A/B meets the thresholds. Alternative: keep 3.12 until then.
- Keep uvicorn[standard] with loop/http `auto` (uvloop + httptools). Recommended: keep. Both are free-threading-ready, and dropping them gave no consistent local win. Run the asyncio/h11 arm on the DGX only as an optional side measurement.
- Dependency bumps. Recommended: only watchfiles>=1.3.0 (Windows cp314t wheels), and only if Windows 3.14t development matters. Defer uvicorn 0.54.0, starlette 1.7.0, numpy 2.5.3, pydantic-core 2.49.0 and the rest to a separate change so the A/B stays clean.
- DGX A/B acceptance thresholds. Recommended: hybrid QPS concurrent at least 1.5x; vector QPS concurrent at least 1.0x; serial p50 regression at most 10%; recall@10 and hybrid text-hit within 0.002; memory under load at most 1.25x and within the 4 GiB cap; no errors or GIL warnings.
- Guard mechanism. Recommended: use the PYTHONWARNINGS error filter plus the build-time assertion, and never set PYTHON_GIL=0. Optionally also expose the GIL state in logs at startup. Changing the /healthz response shape would be an API change and is not recommended.
- Whether to add `[tool.uv] required-version = ">=0.9.0"` to pyproject.toml. Recommended: yes. It is cheap and prevents the 0.5.24-style mismatch.

## Unverified
- GIL-free declarations on linux aarch64 were not tested empirically: no qemu or arm64 emulation was available locally. The wheels exist and are built from the same sources that were verified on x86-64. Step 2 of the DGX protocol closes this gap.
- The turbovec 1.0.0 sdist build on aarch64 (DGX) with rust:1-slim-trixie was not run. Only x86_64 Linux and Windows builds were verified.
- All performance numbers come from a Windows x86-64 laptop (Xeon E-2226G, 6 cores; native, and podman on WSL2 with client and server on the same host), one noisy run per configuration. No DGX numbers were produced.
- Third-party httptools-vs-h11 and uvloop-vs-asyncio figures (Markaicode 50.2k vs 15.1k req/s; the uvloop README's 2-4x) come from unreliable or old sources.
- The interpretation that GIL-build hybrid concurrent QPS falls below its serial rate because of GIL contention (event loop vs to_thread workers) is inferred from the measurements, not profiled.
- `uv self update 0.12.19` working on the user's global uv 0.5.24 is assumed from its ~/.local/bin location (standalone installer). The actual install method was not confirmed.
- Whether `[tool.uv] required-version` changes uv.lock contents was not tested.
- The Unicode 16.0 change in 3.14 was assumed to have no practical effect on raggio's `_fold` text normalization; this was not tested on real corpora.
- The candidate Dockerfile's build-time assertion RUN step has not been built as written. Its import list and warning filter were verified separately, and the rest of the file was built and run as ctx/Dockerfile.ft.
- The free-threaded memory overhead on raggio's actual workload was not measured locally. PEP 779 cites about 15-20% on pyperformance.
- Whether Docker Hub's official python images offer free-threaded variants was not checked. The uv GHCR tags were checked: there are none.
