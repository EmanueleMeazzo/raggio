# ADR 0006 — Free-threaded CPython 3.14t: an experimental image, evaluated

Status: experimental (evaluation only) · Date: 2026-10-02 · Spec D2, D6, D11, D15, D16; item 4

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
barely beat serial (8.6 QPS) on 20 cores. p4 measured free threading on the DGX at 19a8a2e,
before plan F: 3.14t gave +14–31 % concurrent IVF QPS and, at c=16 only, +51 % hybrid QPS
(22.4 against 14.8), at 77–79 % more CPU per query and a p99 of 7.6 s against 1.9–2.0 s. At
c=8 there was no hybrid gain.

The GIL is not the only lock on the hybrid path (spec D15). Every SQLite call takes
SQLite's global memstatus malloc mutex. On the DGX, SQL-only text search on Debian's SQLite
3.40.1 went 27.9 / 36.5 / 18.4 / 16.8 q/s at 1 / 2 / 4 / 8 threads; on SQLite 3.50.4, in
the uv-managed CPython that probe p2 used, 28.6 / 47.6 / 45.4 / 39.4 q/s, a plateau at about
39–48 q/s whatever the GIL does (`docs/superpowers/research/2026-09-28-dgx-probe-p2-hybrid-native.md`).
p4's interpreters (uv 0.12.19: 3.12.14, 3.14.7t) bundle SQLite 3.53.1 (probe p4,
`docs/superpowers/research/2026-09-28-dgx-probe-p4-freethreaded.md`); G's images (uv 0.12.22:
3.12.15, 3.14.8t) report theirs at Task 11 Step 7. p4 ran on uv 0.12.19's 3.12.14 and
3.14.7t; G's images run uv 0.12.22's 3.12.15 and 3.14.8t. p2's figures were not re-measured
on 3.53.1. Memstatus cannot be switched off from Python
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
| **`PYTHON=3.14t` build arg on the one Dockerfile** (`podman build --build-arg PYTHON=3.14t`); no `Dockerfile.ft` | Spec D2. The builder is already the Rust image (ADR 0004), so the turbovec sdist builds there with no extra stage; 3.14t needs no other build arg, because uv falls back to the sdist on its own (probe p4). `PYTHON=3.14 UV_NO_BINARY_PACKAGE=turbovec` builds the optional C1 control from the same file: 3.14 with the GIL, turbovec built locally from the same sdist with maturin, as an abi3 wheel (`cp39-abi3-linux_aarch64` on the DGX; the sdist enables `abi3-py39`), while 3.14t builds `cp314-cp314t`. So C1 is not the same turbovec build: it separates the 3.12 → 3.14 interpreter effect and a local build from PyPI's manylinux wheel (maturin 1.14.1; a local sdist build resolves maturin 1.15.0) |
| **The image stays experimental.** The default stays `PYTHON=3.12` until turbovec publishes cp314t wheels; raggio never forks turbovec | Spec D6. A cp314t wheel request is one of the upstream drafts (ADR 0005). If turbovec's sdist does not build for cp314t, only the builder stage's build dependencies and flags may change; a fix that needs turbovec's own source is drafted upstream for the maintainer to post, and the evaluation parks (spec §3.1 G2) |
| **Build-time check** (`docker/check_image.py`, the last builder step): the interpreter is the flavour `PYTHON` names; every compiled module in the venv imports with the GIL still off; uvicorn's own loading of the app and one pass over the request path (ingest, get, list, vector/text/hybrid/filtered search, IVF attach and search, detach, delete) keep it off; `/healthz` agrees; `bm25` is `native`, from a `raggio_native` that carries the interpreter's own extension suffix (never abi3, spec §4.2) | CPython re-enables the GIL, with only a RuntimeWarning, when it imports an extension that has not declared free-threading support. The check makes that warning an error and also reads the GIL state after every import |
| **Runtime guard**: the image sets `PYTHONWARNINGS=error:The global interpreter lock (GIL) has been enabled:RuntimeWarning` | A lazy import that would re-enable the GIL fails loudly instead of silently serving with it. On a GIL build it never fires. `PYTHON_GIL=0` / `-X gil=0` hide exactly this and are refused by the check |
| **`/healthz` reports `"gil_enabled"`** (additive, spec §4.1) | What a deployment actually runs with, visible without a shell |
| **`GC_FREEZE` env var**, `0` (default) or `1`: after `resume_pending`, collect and `gc.freeze()` the startup heap, including the collections a replayed job loaded; unfreeze after shutdown | The free-threaded collector is non-generational and stop-the-world: every collection walks the whole heap. Frozen objects are skipped (+38% on the laptop's rescore bench). A frozen collection that is later evicted or deleted is still freed, by reference counting (tested); only a reference cycle created before the freeze waits for the unfreeze. A deployment knob, off until the DGX says otherwise |
| **meta.db connections without a statement cache on free-threaded builds** (`SQLITE_CACHED_STATEMENTS` in `store.py`: 0 there, sqlite3's own 128 otherwise) | With biased reference counting, a statement another thread cached is freed only when that thread next runs Python, so `stop()`'s `close()` left a zombie connection holding `meta.db`, `-wal` and `-shm` open (on Windows, deleting the collection failed). Reproduced on 3.14.7t (Windows); not on 3.14.8t, whose gh-157838 merges a detached thread's biased reference counts. Kept for 3.14.7t and older builds; Linux not observed. Preparing each statement again costs about 10 µs per statement on 3.14t (laptop: a 10-id hydrate 18–20 → 28–30 µs, an FTS query 151–157 → 160–176 µs), well under 1% of a search; the DGX A/B includes it. The writer connection loses its cache too, so D2's batched finishes re-prepare their statements; the ABAB measures search only. The catalog connection keeps the default: it closes only at shutdown, and nothing deletes catalog.db while the server runs |
| **Non-blocking CI job** on 3.14t, x86_64 and aarch64, with the extension required and the warning filter set; then the image check and the concurrency stress tests at 30 s | Spec D11. `continue-on-error` until 3.14t is the default image |
| **Unicode drift is measured, not fixed**: `bench/unicode_drift_probe.py` | 3.12 has Unicode 15.0.0 and 3.14 has 16.0.0. The tokenizer runs on `unicodedata` at query time, and `raggio_native` builds its tables from the interpreter it is compiled for, so each image is self-consistent but the two arms differ. On x86_64: 5,812 code points newly assigned, 1 changed category without token drift (U+1171E, Mn → Mc), 5,053 tokenize differently, all newly assigned. The probe counts the bench corpus rows that contain one, so a quality difference between arms can be attributed |

## Decision rule

The DGX A/B (spec §6, §7) runs `PYTHON=3.12` (A) and `PYTHON=3.14t` (B), built from the same
commit, as four sessions on one boot in the order A1 B1 A2 B2, each at 8 concurrent
clients (`CONCURRENCY=8`) and each followed, on the same image, by a c=16 session (spec §3.1
G7; the chain is A1 A1c16 B1 B1c16 A2 A2c16 B2 B2c16). Each session starts from the
IVF index, so every session runs the same order (IVF runs, the 4 GiB detach, flat runs), and
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
never gated. The IVF CPU-per-query rows are not comparable with p4's: plan F's pool doubled
IVF CPU per query (5.19 → 10.09 ms at c=8). If every B run reads high-CPU and every A run
low (or the reverse), the report shows the per-run CPU table and says the difference may be
the interpreter's, not the bimodal state.

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
default. So is building a custom SQLite (spec D15). Re-run on 3.15t once
python-build-standalone ships 3.15.0 and turbovec has a cp315t build path.

## Consequences

- `podman build --build-arg PYTHON=3.14t` compiles turbovec and `raggio_native` in the
  builder (about 2 minutes for the whole image on a 3-CPU x86_64 podman machine); the
  runtime stage copies no toolchain. On the DGX (aarch64), probe p4's 3.14t image, without
  `raggio_native`, built in 28.7 s [28.2..29.3] over 3 `--no-cache` builds with pre-pulled
  bases and no uv cache; the dependency step, turbovec's compile, took 17–19 s of that.
  The 3.14 GIL image built in 10.6 s. The images are 323.6 MB (3.14t) and 320.9 MB (3.14).
  `raggio_native`'s cp314t compile time on the DGX was not measured.
- p4's interpreters (uv 0.12.19: 3.12.14, 3.14.7t) bundle SQLite 3.53.1; G's images
  (uv 0.12.22: 3.12.15, 3.14.8t) report theirs at Task 11 Step 7.
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
