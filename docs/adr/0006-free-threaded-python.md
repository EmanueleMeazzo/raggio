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
3.12.15, 3.14.8t) both bundle SQLite 3.53.1, as Verification records. p2's figures were not re-measured
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
  (uv 0.12.22: 3.12.15, 3.14.8t) both bundle SQLite 3.53.1, as Verification records.
- The 3.14t CI job needs Rust on the runner and takes longer than the other jobs; it cannot
  fail the workflow.
- `GC_FREEZE=1` keeps the startup heap alive for the process lifetime (it would be anyway:
  modules, settings, the manager).

## Verification

DGX ABAB on gn100 (spec §6): the sessions g-a1-py312, g-b1-py314t, g-a2-py312, g-b2-py314t, in this
order, each started from the IVF index by `g-ensure-ivf.sh` and measured by
`bench/dgx/session.sh` with `FLAT_RUNS=3` in a fresh 4 GiB container per run,
`--limit 2549619`, seed 42. Every image runs `OPENBLAS_NUM_THREADS=1` (spec D16); each
table's Regime lines give both arms' `sqlite_version` (D15). A and B differ in `python`
by design, and in `sqlite_version` when their interpreters bundle different SQLite builds.
The `(c=16)` tables are the same four pairs from the `-c16` sessions (`CONCURRENCY=16`, spec
§3.1 G7): reported, and not read by the decision.
On one boot:
boot_id `60d0317d-10f5-4b78-a46a-966e4f6048c3` before the first session,
boot_id `60d0317d-10f5-4b78-a46a-966e4f6048c3` after the last.

### Images

- A: `localhost/raggio:e3f558e`: Python 3.12.15, GIL enabled, turbovec 1.0.0 (cp39-abi3-manylinux_2_28_aarch64), raggio_native 0.1.0 (raggio_native.cpython-312-aarch64-linux-gnu.so), SQLite 3.53.1, OPENBLAS_NUM_THREADS 1, Unicode 15.0.0, raggio_native Unicode 15.0.0
- B: `localhost/raggio:e3f558e-py314t`: Python 3.14.8t, GIL disabled, turbovec 1.0.0 (cp314-cp314t-linux_aarch64), raggio_native 0.1.0 (raggio_native.cpython-314t-aarch64-linux-gnu.so), SQLite 3.53.1, OPENBLAS_NUM_THREADS 1, Unicode 16.0.0, raggio_native Unicode 16.0.0

### GIL and logs

| Session | Arm | /healthz answers | gil_enabled false | gil_enabled true | container logs | Traceback or GIL lines |
|---|---|---|---|---|---|---|
| g-a1-py312 | A | 12 | 0 | 12 | 9 | 0 |
| g-a1-py312-c16 | A | 14 | 0 | 14 | 11 | 0 |
| g-b1-py314t | B | 12 | 12 | 0 | 10 | 0 |
| g-b1-py314t-c16 | B | 14 | 14 | 0 | 11 | 0 |
| g-a2-py312 | A | 13 | 0 | 13 | 10 | 0 |
| g-a2-py312-c16 | A | 14 | 0 | 14 | 11 | 0 |
| g-b2-py314t | B | 12 | 12 | 0 | 9 | 0 |
| g-b2-py314t-c16 | B | 13 | 13 | 0 | 11 | 0 |

### A1 → B1

A/B: baseline g-a1-py312 vs candidate g-b1-py314t
Regime (baseline): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=8, python=3.12.15, sqlite_version=3.53.1, openblas_num_threads=1
Regime (candidate): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=8, python=3.14.8, sqlite_version=3.53.1, openblas_num_threads=1
The arms differ in python: a re-baseline across them, not a same-regime A/B (spec §6).

| Engine | Metric | base median | base band | cand median | cand band | verdict |
|---|---|---|---|---|---|---|
| raggio | Memory after ingest (MB) | 1501 | 1 | 1522 | 0 | worse |
| raggio | Memory under query load (MB) | 1505 | 1 | 1530 | 1 | worse |
| raggio | Disk footprint (MB) | 16463 | 0 | 16463 | 0 | within band |
| raggio | Search p50 (ms) | 18.5 | 0.0 | 18.8 | 0.1 | worse |
| raggio | Search p95 (ms) | 19.7 | 0.2 | 20.0 | 0.2 | worse |
| raggio | Search p99 (ms) | 20.3 | 0.2 | 20.5 | 0.4 | within band |
| raggio | QPS serial | 54 | 0 | 53 | 0 | within band |
| raggio | QPS concurrent | 127 | 36 | 124 | 1 | within band |
| raggio | p95 under concurrency (ms) | 67.5 | 3.4 | 69.4 | 7.2 | within band |
| raggio | CPU per query under concurrency (ms) | 118.0 | 32.4 | 118.5 | 1.5 | within band |
| raggio | Filtered p50 (ms) | 26.0 | 2.0 | 24.1 | 3.8 | within band |
| raggio | Filtered p95 (ms) | 30.9 | 5.1 | 29.1 | 6.8 | within band |
| raggio | Recall@10 vs exact | 1.000 | 0.000 | 1.000 | 0.000 | within band |
| raggio | Hybrid p50 (ms) | 49.2 | 0.8 | 47.6 | 1.9 | within band |
| raggio | Hybrid p95 (ms) | 74.6 | 1.5 | 73.7 | 2.0 | within band |
| raggio | Hybrid p99 (ms) | 100.2 | 14.4 | 106.7 | 11.8 | within band |
| raggio | Hybrid first 10 queries, slowest (ms) | 127.0 | 0.7 | 125.9 | 5.5 | within band |
| raggio | Hybrid p99 without the first 10 (ms) | 91.2 | 1.1 | 88.5 | 2.6 | better |
| raggio | Hybrid QPS serial | 20.1 | 0.2 | 20.5 | 0.8 | within band |
| raggio | Hybrid QPS concurrent | 36.2 | 2.0 | 40.4 | 0.2 | better |
| raggio | Hybrid p99 under concurrency (ms) | 465.5 | 49.4 | 356.5 | 7.5 | better |
| raggio | Hybrid CPU per query under concurrency (ms) | 336.0 | 13.5 | 314.3 | 2.9 | better |
| raggio | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio | Cold start to first query (s) | 2.1 | 0.5 | 2.5 | 0.1 | not claimable |
| raggio-ivf | Memory after ingest (MB) | 1505 | 1 | 1529 | 1 | worse |
| raggio-ivf | Memory under query load (MB) | 1512 | 3 | 1546 | 2 | worse |
| raggio-ivf | Disk footprint (MB) | 16751 | 0 | 16751 | 0 | within band |
| raggio-ivf | Search p50 (ms) | 6.4 | 0.4 | 6.4 | 0.1 | within band |
| raggio-ivf | Search p95 (ms) | 7.7 | 0.8 | 7.7 | 0.4 | within band |
| raggio-ivf | Search p99 (ms) | 8.5 | 1.0 | 9.0 | 1.0 | within band |
| raggio-ivf | QPS serial | 153 | 9 | 152 | 3 | within band |
| raggio-ivf | QPS concurrent | 330 | 15 | 412 | 38 | better |
| raggio-ivf | p95 under concurrency (ms) | 31.3 | 0.8 | 25.5 | 2.3 | better |
| raggio-ivf | CPU per query under concurrency (ms) | 10.1 | 0.6 | 11.8 | 0.7 | worse |
| raggio-ivf | Filtered p50 (ms) | 6.0 | 0.1 | 6.2 | 0.7 | within band |
| raggio-ivf | Filtered p95 (ms) | 7.1 | 0.4 | 8.0 | 0.9 | within band |
| raggio-ivf | Recall@10 vs exact | 0.995 | 0.000 | 0.995 | 0.000 | within band |
| raggio-ivf | Hybrid p50 (ms) | 47.0 | 1.1 | 45.8 | 0.1 | better |
| raggio-ivf | Hybrid p95 (ms) | 73.1 | 2.0 | 72.1 | 0.3 | within band |
| raggio-ivf | Hybrid p99 (ms) | 100.9 | 11.5 | 107.5 | 9.6 | within band |
| raggio-ivf | Hybrid first 10 queries, slowest (ms) | 124.8 | 2.1 | 123.7 | 2.0 | within band |
| raggio-ivf | Hybrid p99 without the first 10 (ms) | 89.4 | 3.6 | 87.9 | 4.8 | within band |
| raggio-ivf | Hybrid QPS serial | 21.0 | 0.4 | 21.5 | 0.2 | better |
| raggio-ivf | Hybrid QPS concurrent | 37.3 | 1.2 | 42.7 | 1.6 | better |
| raggio-ivf | Hybrid p99 under concurrency (ms) | 441.1 | 57.5 | 335.5 | 20.4 | better |
| raggio-ivf | Hybrid CPU per query under concurrency (ms) | 207.0 | 7.2 | 185.2 | 6.7 | better |
| raggio-ivf | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio-ivf | Cold start to first query (s) | 2.5 | 0.5 | 3.1 | 0.1 | not claimable |

Beyond the noise band: better [('raggio', 'hybrid_p99_after10'), ('raggio', 'hybrid_qps_concurrent'), ('raggio', 'hybrid_c_p99'), ('raggio', 'hybrid_cpu_ms_per_q_c'), ('raggio-ivf', 'qps_concurrent'), ('raggio-ivf', 'lat_c_p95'), ('raggio-ivf', 'hybrid_p50'), ('raggio-ivf', 'hybrid_qps_serial'), ('raggio-ivf', 'hybrid_qps_concurrent'), ('raggio-ivf', 'hybrid_c_p99'), ('raggio-ivf', 'hybrid_cpu_ms_per_q_c')], worse [('raggio', 'mem_after_ingest_mb'), ('raggio', 'mem_under_load_mb'), ('raggio', 'lat_p50'), ('raggio', 'lat_p95'), ('raggio-ivf', 'mem_after_ingest_mb'), ('raggio-ivf', 'mem_under_load_mb'), ('raggio-ivf', 'cpu_ms_per_q_c')].

```json
{"cpu_ms_per_q_c": {"baseline": [118.724744, 86.333948, 117.961848], "candidate": [119.37495200000001, 118.549658, 117.922498]}, "cpu_missing": []}
```

### A2 → B2

A/B: baseline g-a2-py312 vs candidate g-b2-py314t
Regime (baseline): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=8, python=3.12.15, sqlite_version=3.53.1, openblas_num_threads=1
Regime (candidate): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=8, python=3.14.8, sqlite_version=3.53.1, openblas_num_threads=1
The arms differ in python: a re-baseline across them, not a same-regime A/B (spec §6).

| Engine | Metric | base median | base band | cand median | cand band | verdict |
|---|---|---|---|---|---|---|
| raggio | Memory after ingest (MB) | 1501 | 1 | 1522 | 1 | worse |
| raggio | Memory under query load (MB) | 1505 | 1 | 1530 | 4 | worse |
| raggio | Disk footprint (MB) | 16463 | 0 | 16463 | 0 | within band |
| raggio | Search p50 (ms) | 18.5 | 0.1 | 18.8 | 0.2 | worse |
| raggio | Search p95 (ms) | 19.6 | 0.2 | 19.9 | 0.2 | worse |
| raggio | Search p99 (ms) | 20.2 | 0.1 | 20.4 | 0.3 | within band |
| raggio | QPS serial | 54 | 0 | 53 | 1 | within band |
| raggio | QPS concurrent | 151 | 46 | 126 | 2 | within band |
| raggio | p95 under concurrency (ms) | 67.2 | 5.7 | 69.6 | 7.3 | within band |
| raggio | CPU per query under concurrency (ms) | 95.0 | 38.0 | 118.5 | 0.9 | within band |
| raggio | Filtered p50 (ms) | 26.5 | 4.4 | 25.7 | 3.8 | within band |
| raggio | Filtered p95 (ms) | 32.0 | 6.3 | 28.8 | 5.3 | within band |
| raggio | Recall@10 vs exact | 1.000 | 0.000 | 1.000 | 0.000 | within band |
| raggio | Hybrid p50 (ms) | 49.4 | 1.0 | 48.4 | 0.5 | within band |
| raggio | Hybrid p95 (ms) | 74.9 | 0.2 | 74.3 | 0.8 | within band |
| raggio | Hybrid p99 (ms) | 99.9 | 11.1 | 106.8 | 9.9 | within band |
| raggio | Hybrid first 10 queries, slowest (ms) | 128.1 | 1.7 | 127.4 | 0.9 | within band |
| raggio | Hybrid p99 without the first 10 (ms) | 90.9 | 0.2 | 89.6 | 3.0 | within band |
| raggio | Hybrid QPS serial | 20.0 | 0.3 | 20.2 | 0.3 | within band |
| raggio | Hybrid QPS concurrent | 36.3 | 3.1 | 40.1 | 0.4 | better |
| raggio | Hybrid p99 under concurrency (ms) | 433.8 | 48.1 | 351.7 | 9.4 | better |
| raggio | Hybrid CPU per query under concurrency (ms) | 334.3 | 17.9 | 316.7 | 2.9 | within band |
| raggio | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio | Cold start to first query (s) | 2.1 | 0.5 | 2.5 | 0.1 | not claimable |
| raggio-ivf | Memory after ingest (MB) | 1505 | 0 | 1527 | 1 | worse |
| raggio-ivf | Memory under query load (MB) | 1513 | 1 | 1545 | 2 | worse |
| raggio-ivf | Disk footprint (MB) | 16751 | 0 | 16751 | 0 | within band |
| raggio-ivf | Search p50 (ms) | 6.0 | 0.2 | 6.4 | 0.2 | worse |
| raggio-ivf | Search p95 (ms) | 7.2 | 0.4 | 7.7 | 0.2 | worse |
| raggio-ivf | Search p99 (ms) | 7.7 | 0.7 | 8.2 | 0.5 | within band |
| raggio-ivf | QPS serial | 164 | 7 | 152 | 4 | worse |
| raggio-ivf | QPS concurrent | 335 | 7 | 413 | 32 | better |
| raggio-ivf | p95 under concurrency (ms) | 29.8 | 1.4 | 25.2 | 1.5 | better |
| raggio-ivf | CPU per query under concurrency (ms) | 10.1 | 0.2 | 10.9 | 0.4 | worse |
| raggio-ivf | Filtered p50 (ms) | 5.9 | 0.3 | 6.0 | 0.8 | within band |
| raggio-ivf | Filtered p95 (ms) | 7.0 | 0.8 | 7.7 | 0.9 | within band |
| raggio-ivf | Recall@10 vs exact | 0.994 | 0.000 | 0.994 | 0.000 | within band |
| raggio-ivf | Hybrid p50 (ms) | 45.6 | 1.1 | 45.7 | 0.5 | within band |
| raggio-ivf | Hybrid p95 (ms) | 71.9 | 0.7 | 71.1 | 1.0 | within band |
| raggio-ivf | Hybrid p99 (ms) | 97.0 | 11.4 | 95.8 | 1.6 | within band |
| raggio-ivf | Hybrid first 10 queries, slowest (ms) | 123.9 | 1.7 | 123.7 | 3.1 | within band |
| raggio-ivf | Hybrid p99 without the first 10 (ms) | 89.2 | 2.2 | 88.0 | 0.5 | within band |
| raggio-ivf | Hybrid QPS serial | 21.6 | 0.5 | 21.5 | 0.2 | within band |
| raggio-ivf | Hybrid QPS concurrent | 37.3 | 1.1 | 42.1 | 0.4 | better |
| raggio-ivf | Hybrid p99 under concurrency (ms) | 413.3 | 25.8 | 345.6 | 12.6 | better |
| raggio-ivf | Hybrid CPU per query under concurrency (ms) | 207.2 | 6.7 | 188.1 | 1.9 | better |
| raggio-ivf | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio-ivf | Cold start to first query (s) | 2.5 | 0.3 | 3.0 | 0.1 | not claimable |

Beyond the noise band: better [('raggio', 'hybrid_qps_concurrent'), ('raggio', 'hybrid_c_p99'), ('raggio-ivf', 'qps_concurrent'), ('raggio-ivf', 'lat_c_p95'), ('raggio-ivf', 'hybrid_qps_concurrent'), ('raggio-ivf', 'hybrid_c_p99'), ('raggio-ivf', 'hybrid_cpu_ms_per_q_c')], worse [('raggio', 'mem_after_ingest_mb'), ('raggio', 'mem_under_load_mb'), ('raggio', 'lat_p50'), ('raggio', 'lat_p95'), ('raggio-ivf', 'mem_after_ingest_mb'), ('raggio-ivf', 'mem_under_load_mb'), ('raggio-ivf', 'lat_p50'), ('raggio-ivf', 'lat_p95'), ('raggio-ivf', 'qps_serial'), ('raggio-ivf', 'cpu_ms_per_q_c')].

```json
{"cpu_ms_per_q_c": {"baseline": [79.297788, 95.047596, 117.252404], "candidate": [118.480404, 118.081616, 119.030166]}, "cpu_missing": []}
```

### A1 → A2

A/B: baseline g-a1-py312 vs candidate g-a2-py312
Regime (baseline): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=8, python=3.12.15, sqlite_version=3.53.1, openblas_num_threads=1
Regime (candidate): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=8, python=3.12.15, sqlite_version=3.53.1, openblas_num_threads=1

| Engine | Metric | base median | base band | cand median | cand band | verdict |
|---|---|---|---|---|---|---|
| raggio | Memory after ingest (MB) | 1501 | 1 | 1501 | 1 | within band |
| raggio | Memory under query load (MB) | 1505 | 1 | 1505 | 1 | within band |
| raggio | Disk footprint (MB) | 16463 | 0 | 16463 | 0 | within band |
| raggio | Search p50 (ms) | 18.5 | 0.0 | 18.5 | 0.1 | within band |
| raggio | Search p95 (ms) | 19.7 | 0.2 | 19.6 | 0.2 | within band |
| raggio | Search p99 (ms) | 20.3 | 0.2 | 20.2 | 0.1 | within band |
| raggio | QPS serial | 54 | 0 | 54 | 0 | within band |
| raggio | QPS concurrent | 127 | 36 | 151 | 46 | within band |
| raggio | p95 under concurrency (ms) | 67.5 | 3.4 | 67.2 | 5.7 | within band |
| raggio | CPU per query under concurrency (ms) | 118.0 | 32.4 | 95.0 | 38.0 | within band |
| raggio | Filtered p50 (ms) | 26.0 | 2.0 | 26.5 | 4.4 | within band |
| raggio | Filtered p95 (ms) | 30.9 | 5.1 | 32.0 | 6.3 | within band |
| raggio | Recall@10 vs exact | 1.000 | 0.000 | 1.000 | 0.000 | within band |
| raggio | Hybrid p50 (ms) | 49.2 | 0.8 | 49.4 | 1.0 | within band |
| raggio | Hybrid p95 (ms) | 74.6 | 1.5 | 74.9 | 0.2 | within band |
| raggio | Hybrid p99 (ms) | 100.2 | 14.4 | 99.9 | 11.1 | within band |
| raggio | Hybrid first 10 queries, slowest (ms) | 127.0 | 0.7 | 128.1 | 1.7 | within band |
| raggio | Hybrid p99 without the first 10 (ms) | 91.2 | 1.1 | 90.9 | 0.2 | within band |
| raggio | Hybrid QPS serial | 20.1 | 0.2 | 20.0 | 0.3 | within band |
| raggio | Hybrid QPS concurrent | 36.2 | 2.0 | 36.3 | 3.1 | within band |
| raggio | Hybrid p99 under concurrency (ms) | 465.5 | 49.4 | 433.8 | 48.1 | within band |
| raggio | Hybrid CPU per query under concurrency (ms) | 336.0 | 13.5 | 334.3 | 17.9 | within band |
| raggio | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio | Cold start to first query (s) | 2.1 | 0.5 | 2.1 | 0.5 | not claimable |
| raggio-ivf | Memory after ingest (MB) | 1505 | 1 | 1505 | 0 | within band |
| raggio-ivf | Memory under query load (MB) | 1512 | 3 | 1513 | 1 | within band |
| raggio-ivf | Disk footprint (MB) | 16751 | 0 | 16751 | 0 | within band |
| raggio-ivf | Search p50 (ms) | 6.4 | 0.4 | 6.0 | 0.2 | within band |
| raggio-ivf | Search p95 (ms) | 7.7 | 0.8 | 7.2 | 0.4 | within band |
| raggio-ivf | Search p99 (ms) | 8.5 | 1.0 | 7.7 | 0.7 | within band |
| raggio-ivf | QPS serial | 153 | 9 | 164 | 7 | better |
| raggio-ivf | QPS concurrent | 330 | 15 | 335 | 7 | within band |
| raggio-ivf | p95 under concurrency (ms) | 31.3 | 0.8 | 29.8 | 1.4 | better |
| raggio-ivf | CPU per query under concurrency (ms) | 10.1 | 0.6 | 10.1 | 0.2 | within band |
| raggio-ivf | Filtered p50 (ms) | 6.0 | 0.1 | 5.9 | 0.3 | within band |
| raggio-ivf | Filtered p95 (ms) | 7.1 | 0.4 | 7.0 | 0.8 | within band |
| raggio-ivf | Recall@10 vs exact | 0.995 | 0.000 | 0.994 | 0.000 | worse |
| raggio-ivf | Hybrid p50 (ms) | 47.0 | 1.1 | 45.6 | 1.1 | better |
| raggio-ivf | Hybrid p95 (ms) | 73.1 | 2.0 | 71.9 | 0.7 | within band |
| raggio-ivf | Hybrid p99 (ms) | 100.9 | 11.5 | 97.0 | 11.4 | within band |
| raggio-ivf | Hybrid first 10 queries, slowest (ms) | 124.8 | 2.1 | 123.9 | 1.7 | within band |
| raggio-ivf | Hybrid p99 without the first 10 (ms) | 89.4 | 3.6 | 89.2 | 2.2 | within band |
| raggio-ivf | Hybrid QPS serial | 21.0 | 0.4 | 21.6 | 0.5 | better |
| raggio-ivf | Hybrid QPS concurrent | 37.3 | 1.2 | 37.3 | 1.1 | within band |
| raggio-ivf | Hybrid p99 under concurrency (ms) | 441.1 | 57.5 | 413.3 | 25.8 | within band |
| raggio-ivf | Hybrid CPU per query under concurrency (ms) | 207.0 | 7.2 | 207.2 | 6.7 | within band |
| raggio-ivf | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio-ivf | Cold start to first query (s) | 2.5 | 0.5 | 2.5 | 0.3 | not claimable |

Beyond the noise band: better [('raggio-ivf', 'qps_serial'), ('raggio-ivf', 'lat_c_p95'), ('raggio-ivf', 'hybrid_p50'), ('raggio-ivf', 'hybrid_qps_serial')], worse [('raggio-ivf', 'recall_at_10')].

```json
{"cpu_ms_per_q_c": {"baseline": [118.724744, 86.333948, 117.961848], "candidate": [79.297788, 95.047596, 117.252404]}, "cpu_missing": []}
```

### B1 → B2

A/B: baseline g-b1-py314t vs candidate g-b2-py314t
Regime (baseline): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=8, python=3.14.8, sqlite_version=3.53.1, openblas_num_threads=1
Regime (candidate): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=8, python=3.14.8, sqlite_version=3.53.1, openblas_num_threads=1

| Engine | Metric | base median | base band | cand median | cand band | verdict |
|---|---|---|---|---|---|---|
| raggio | Memory after ingest (MB) | 1522 | 0 | 1522 | 1 | within band |
| raggio | Memory under query load (MB) | 1530 | 1 | 1530 | 4 | within band |
| raggio | Disk footprint (MB) | 16463 | 0 | 16463 | 0 | within band |
| raggio | Search p50 (ms) | 18.8 | 0.1 | 18.8 | 0.2 | within band |
| raggio | Search p95 (ms) | 20.0 | 0.2 | 19.9 | 0.2 | within band |
| raggio | Search p99 (ms) | 20.5 | 0.4 | 20.4 | 0.3 | within band |
| raggio | QPS serial | 53 | 0 | 53 | 1 | within band |
| raggio | QPS concurrent | 124 | 1 | 126 | 2 | better |
| raggio | p95 under concurrency (ms) | 69.4 | 7.2 | 69.6 | 7.3 | within band |
| raggio | CPU per query under concurrency (ms) | 118.5 | 1.5 | 118.5 | 0.9 | within band |
| raggio | Filtered p50 (ms) | 24.1 | 3.8 | 25.7 | 3.8 | within band |
| raggio | Filtered p95 (ms) | 29.1 | 6.8 | 28.8 | 5.3 | within band |
| raggio | Recall@10 vs exact | 1.000 | 0.000 | 1.000 | 0.000 | within band |
| raggio | Hybrid p50 (ms) | 47.6 | 1.9 | 48.4 | 0.5 | within band |
| raggio | Hybrid p95 (ms) | 73.7 | 2.0 | 74.3 | 0.8 | within band |
| raggio | Hybrid p99 (ms) | 106.7 | 11.8 | 106.8 | 9.9 | within band |
| raggio | Hybrid first 10 queries, slowest (ms) | 125.9 | 5.5 | 127.4 | 0.9 | within band |
| raggio | Hybrid p99 without the first 10 (ms) | 88.5 | 2.6 | 89.6 | 3.0 | within band |
| raggio | Hybrid QPS serial | 20.5 | 0.8 | 20.2 | 0.3 | within band |
| raggio | Hybrid QPS concurrent | 40.4 | 0.2 | 40.1 | 0.4 | within band |
| raggio | Hybrid p99 under concurrency (ms) | 356.5 | 7.5 | 351.7 | 9.4 | within band |
| raggio | Hybrid CPU per query under concurrency (ms) | 314.3 | 2.9 | 316.7 | 2.9 | within band |
| raggio | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio | Cold start to first query (s) | 2.5 | 0.1 | 2.5 | 0.1 | not claimable |
| raggio-ivf | Memory after ingest (MB) | 1529 | 1 | 1527 | 1 | better |
| raggio-ivf | Memory under query load (MB) | 1546 | 2 | 1545 | 2 | within band |
| raggio-ivf | Disk footprint (MB) | 16751 | 0 | 16751 | 0 | within band |
| raggio-ivf | Search p50 (ms) | 6.4 | 0.1 | 6.4 | 0.2 | within band |
| raggio-ivf | Search p95 (ms) | 7.7 | 0.4 | 7.7 | 0.2 | within band |
| raggio-ivf | Search p99 (ms) | 9.0 | 1.0 | 8.2 | 0.5 | within band |
| raggio-ivf | QPS serial | 152 | 3 | 152 | 4 | within band |
| raggio-ivf | QPS concurrent | 412 | 38 | 413 | 32 | within band |
| raggio-ivf | p95 under concurrency (ms) | 25.5 | 2.3 | 25.2 | 1.5 | within band |
| raggio-ivf | CPU per query under concurrency (ms) | 11.8 | 0.7 | 10.9 | 0.4 | better |
| raggio-ivf | Filtered p50 (ms) | 6.2 | 0.7 | 6.0 | 0.8 | within band |
| raggio-ivf | Filtered p95 (ms) | 8.0 | 0.9 | 7.7 | 0.9 | within band |
| raggio-ivf | Recall@10 vs exact | 0.995 | 0.000 | 0.994 | 0.000 | worse |
| raggio-ivf | Hybrid p50 (ms) | 45.8 | 0.1 | 45.7 | 0.5 | within band |
| raggio-ivf | Hybrid p95 (ms) | 72.1 | 0.3 | 71.1 | 1.0 | within band |
| raggio-ivf | Hybrid p99 (ms) | 107.5 | 9.6 | 95.8 | 1.6 | better |
| raggio-ivf | Hybrid first 10 queries, slowest (ms) | 123.7 | 2.0 | 123.7 | 3.1 | within band |
| raggio-ivf | Hybrid p99 without the first 10 (ms) | 87.9 | 4.8 | 88.0 | 0.5 | within band |
| raggio-ivf | Hybrid QPS serial | 21.5 | 0.2 | 21.5 | 0.2 | within band |
| raggio-ivf | Hybrid QPS concurrent | 42.7 | 1.6 | 42.1 | 0.4 | within band |
| raggio-ivf | Hybrid p99 under concurrency (ms) | 335.5 | 20.4 | 345.6 | 12.6 | within band |
| raggio-ivf | Hybrid CPU per query under concurrency (ms) | 185.2 | 6.7 | 188.1 | 1.9 | within band |
| raggio-ivf | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio-ivf | Cold start to first query (s) | 3.1 | 0.1 | 3.0 | 0.1 | not claimable |

Beyond the noise band: better [('raggio', 'qps_concurrent'), ('raggio-ivf', 'mem_after_ingest_mb'), ('raggio-ivf', 'cpu_ms_per_q_c'), ('raggio-ivf', 'hybrid_p99')], worse [('raggio-ivf', 'recall_at_10')].

```json
{"cpu_ms_per_q_c": {"baseline": [119.37495200000001, 118.549658, 117.922498], "candidate": [118.480404, 118.081616, 119.030166]}, "cpu_missing": []}
```

### A1 → B1 (c=16)

A/B: baseline g-a1-py312-c16 vs candidate g-b1-py314t-c16
Regime (baseline): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=16, python=3.12.15, sqlite_version=3.53.1, openblas_num_threads=1
Regime (candidate): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=16, python=3.14.8, sqlite_version=3.53.1, openblas_num_threads=1
The arms differ in python: a re-baseline across them, not a same-regime A/B (spec §6).

| Engine | Metric | base median | base band | cand median | cand band | verdict |
|---|---|---|---|---|---|---|
| raggio | Memory after ingest (MB) | 1501 | 1 | 1521 | 1 | worse |
| raggio | Memory under query load (MB) | 1505 | 1 | 1531 | 6 | worse |
| raggio | Disk footprint (MB) | 16463 | 0 | 16463 | 0 | within band |
| raggio | Search p50 (ms) | 18.5 | 0.2 | 18.7 | 0.4 | within band |
| raggio | Search p95 (ms) | 19.7 | 0.1 | 19.8 | 0.3 | within band |
| raggio | Search p99 (ms) | 20.3 | 0.1 | 20.5 | 0.3 | within band |
| raggio | QPS serial | 54 | 1 | 53 | 1 | within band |
| raggio | QPS concurrent | 212 | 63 | 210 | 86 | within band |
| raggio | p95 under concurrency (ms) | 102.0 | 7.9 | 101.0 | 15.8 | within band |
| raggio | CPU per query under concurrency (ms) | 62.5 | 22.9 | 65.2 | 25.4 | within band |
| raggio | Filtered p50 (ms) | 25.3 | 4.7 | 25.1 | 2.5 | within band |
| raggio | Filtered p95 (ms) | 27.0 | 6.6 | 28.0 | 8.3 | within band |
| raggio | Recall@10 vs exact | 1.000 | 0.000 | 1.000 | 0.000 | within band |
| raggio | Hybrid p50 (ms) | 49.7 | 1.9 | 49.5 | 0.8 | within band |
| raggio | Hybrid p95 (ms) | 75.0 | 1.3 | 74.8 | 0.4 | within band |
| raggio | Hybrid p99 (ms) | 108.3 | 7.9 | 98.7 | 14.8 | within band |
| raggio | Hybrid first 10 queries, slowest (ms) | 126.4 | 0.2 | 127.0 | 0.6 | worse |
| raggio | Hybrid p99 without the first 10 (ms) | 90.4 | 0.4 | 90.6 | 0.9 | within band |
| raggio | Hybrid QPS serial | 20.0 | 0.5 | 19.9 | 0.3 | within band |
| raggio | Hybrid QPS concurrent | 34.3 | 1.8 | 36.5 | 0.7 | better |
| raggio | Hybrid p99 under concurrency (ms) | 988.7 | 141.6 | 794.9 | 31.3 | better |
| raggio | Hybrid CPU per query under concurrency (ms) | 483.5 | 19.7 | 454.5 | 6.9 | better |
| raggio | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio | Cold start to first query (s) | 2.1 | 0.0 | 2.5 | 0.0 | not claimable |
| raggio-ivf | Memory after ingest (MB) | 1507 | 2 | 1528 | 1 | worse |
| raggio-ivf | Memory under query load (MB) | 1516 | 4 | 1548 | 3 | worse |
| raggio-ivf | Disk footprint (MB) | 16751 | 0 | 16751 | 0 | within band |
| raggio-ivf | Search p50 (ms) | 6.1 | 0.3 | 6.1 | 0.2 | within band |
| raggio-ivf | Search p95 (ms) | 7.4 | 0.4 | 7.3 | 0.2 | within band |
| raggio-ivf | Search p99 (ms) | 8.0 | 0.3 | 7.9 | 0.5 | within band |
| raggio-ivf | QPS serial | 161 | 6 | 161 | 5 | within band |
| raggio-ivf | QPS concurrent | 371 | 12 | 469 | 31 | better |
| raggio-ivf | p95 under concurrency (ms) | 67.5 | 4.4 | 55.0 | 15.0 | within band |
| raggio-ivf | CPU per query under concurrency (ms) | 10.2 | 0.7 | 10.9 | 0.2 | within band |
| raggio-ivf | Filtered p50 (ms) | 6.1 | 0.2 | 6.1 | 0.3 | within band |
| raggio-ivf | Filtered p95 (ms) | 7.3 | 0.4 | 7.1 | 0.8 | within band |
| raggio-ivf | Recall@10 vs exact | 0.995 | 0.000 | 0.992 | 0.000 | worse |
| raggio-ivf | Hybrid p50 (ms) | 46.2 | 1.3 | 45.9 | 0.9 | within band |
| raggio-ivf | Hybrid p95 (ms) | 71.8 | 1.9 | 71.7 | 1.2 | within band |
| raggio-ivf | Hybrid p99 (ms) | 107.3 | 13.7 | 99.1 | 6.9 | within band |
| raggio-ivf | Hybrid first 10 queries, slowest (ms) | 124.2 | 2.0 | 124.9 | 2.8 | within band |
| raggio-ivf | Hybrid p99 without the first 10 (ms) | 89.1 | 1.5 | 89.4 | 0.7 | within band |
| raggio-ivf | Hybrid QPS serial | 21.3 | 0.4 | 21.3 | 0.4 | within band |
| raggio-ivf | Hybrid QPS concurrent | 37.4 | 0.9 | 39.6 | 1.8 | better |
| raggio-ivf | Hybrid p99 under concurrency (ms) | 897.5 | 58.9 | 743.3 | 28.3 | better |
| raggio-ivf | Hybrid CPU per query under concurrency (ms) | 394.4 | 10.9 | 370.3 | 16.2 | better |
| raggio-ivf | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio-ivf | Cold start to first query (s) | 2.6 | 0.0 | 3.0 | 0.1 | not claimable |

Beyond the noise band: better [('raggio', 'hybrid_qps_concurrent'), ('raggio', 'hybrid_c_p99'), ('raggio', 'hybrid_cpu_ms_per_q_c'), ('raggio-ivf', 'qps_concurrent'), ('raggio-ivf', 'hybrid_qps_concurrent'), ('raggio-ivf', 'hybrid_c_p99'), ('raggio-ivf', 'hybrid_cpu_ms_per_q_c')], worse [('raggio', 'mem_after_ingest_mb'), ('raggio', 'mem_under_load_mb'), ('raggio', 'hybrid_first10_max_ms'), ('raggio-ivf', 'mem_after_ingest_mb'), ('raggio-ivf', 'mem_under_load_mb'), ('raggio-ivf', 'recall_at_10')].

```json
{"cpu_ms_per_q_c": {"baseline": [71.137064, 62.535318, 48.257682], "candidate": [41.723188, 67.09540799999999, 65.182928]}, "cpu_missing": []}
```

### A2 → B2 (c=16)

A/B: baseline g-a2-py312-c16 vs candidate g-b2-py314t-c16
Regime (baseline): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=16, python=3.12.15, sqlite_version=3.53.1, openblas_num_threads=1
Regime (candidate): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=16, python=3.14.8, sqlite_version=3.53.1, openblas_num_threads=1
The arms differ in python: a re-baseline across them, not a same-regime A/B (spec §6).

| Engine | Metric | base median | base band | cand median | cand band | verdict |
|---|---|---|---|---|---|---|
| raggio | Memory after ingest (MB) | 1501 | 1 | 1522 | 1 | worse |
| raggio | Memory under query load (MB) | 1506 | 1 | 1530 | 3 | worse |
| raggio | Disk footprint (MB) | 16463 | 0 | 16463 | 0 | within band |
| raggio | Search p50 (ms) | 18.5 | 0.4 | 18.6 | 0.4 | within band |
| raggio | Search p95 (ms) | 19.8 | 0.7 | 19.8 | 0.4 | within band |
| raggio | Search p99 (ms) | 20.2 | 0.5 | 20.6 | 0.3 | within band |
| raggio | QPS serial | 54 | 1 | 53 | 1 | within band |
| raggio | QPS concurrent | 203 | 9 | 205 | 25 | within band |
| raggio | p95 under concurrency (ms) | 99.2 | 4.7 | 96.8 | 17.3 | within band |
| raggio | CPU per query under concurrency (ms) | 70.0 | 6.5 | 67.1 | 10.7 | within band |
| raggio | Filtered p50 (ms) | 23.3 | 2.7 | 26.9 | 1.6 | worse |
| raggio | Filtered p95 (ms) | 31.5 | 6.0 | 28.6 | 7.3 | within band |
| raggio | Recall@10 vs exact | 1.000 | 0.000 | 1.000 | 0.000 | within band |
| raggio | Hybrid p50 (ms) | 48.8 | 1.4 | 48.8 | 0.9 | within band |
| raggio | Hybrid p95 (ms) | 74.0 | 0.5 | 74.5 | 0.3 | within band |
| raggio | Hybrid p99 (ms) | 108.2 | 12.8 | 108.0 | 10.4 | within band |
| raggio | Hybrid first 10 queries, slowest (ms) | 126.6 | 1.3 | 126.1 | 3.3 | within band |
| raggio | Hybrid p99 without the first 10 (ms) | 90.2 | 0.8 | 89.9 | 1.8 | within band |
| raggio | Hybrid QPS serial | 20.2 | 0.5 | 20.2 | 0.3 | within band |
| raggio | Hybrid QPS concurrent | 34.3 | 1.0 | 36.2 | 0.5 | better |
| raggio | Hybrid p99 under concurrency (ms) | 982.6 | 13.9 | 804.3 | 8.7 | better |
| raggio | Hybrid CPU per query under concurrency (ms) | 480.4 | 10.9 | 457.8 | 6.6 | better |
| raggio | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio | Cold start to first query (s) | 2.1 | 0.5 | 2.5 | 0.1 | not claimable |
| raggio-ivf | Memory after ingest (MB) | 1505 | 1 | 1528 | 0 | worse |
| raggio-ivf | Memory under query load (MB) | 1515 | 2 | 1547 | 4 | worse |
| raggio-ivf | Disk footprint (MB) | 16751 | 0 | 16751 | 0 | within band |
| raggio-ivf | Search p50 (ms) | 6.3 | 0.6 | 6.1 | 1.1 | within band |
| raggio-ivf | Search p95 (ms) | 7.5 | 0.6 | 7.0 | 1.1 | within band |
| raggio-ivf | Search p99 (ms) | 8.0 | 0.7 | 7.5 | 1.2 | within band |
| raggio-ivf | QPS serial | 157 | 13 | 164 | 26 | within band |
| raggio-ivf | QPS concurrent | 375 | 8 | 482 | 32 | better |
| raggio-ivf | p95 under concurrency (ms) | 63.5 | 7.0 | 59.2 | 7.2 | within band |
| raggio-ivf | CPU per query under concurrency (ms) | 10.1 | 0.5 | 10.9 | 0.2 | worse |
| raggio-ivf | Filtered p50 (ms) | 6.0 | 0.4 | 6.6 | 1.2 | within band |
| raggio-ivf | Filtered p95 (ms) | 6.8 | 0.3 | 8.1 | 0.5 | worse |
| raggio-ivf | Recall@10 vs exact | 0.992 | 0.000 | 0.993 | 0.000 | within band |
| raggio-ivf | Hybrid p50 (ms) | 46.6 | 0.9 | 45.6 | 2.0 | within band |
| raggio-ivf | Hybrid p95 (ms) | 72.3 | 1.1 | 71.4 | 2.0 | within band |
| raggio-ivf | Hybrid p99 (ms) | 97.5 | 2.3 | 97.6 | 3.5 | within band |
| raggio-ivf | Hybrid first 10 queries, slowest (ms) | 126.0 | 3.2 | 124.4 | 2.4 | within band |
| raggio-ivf | Hybrid p99 without the first 10 (ms) | 89.3 | 1.7 | 87.0 | 2.8 | within band |
| raggio-ivf | Hybrid QPS serial | 21.2 | 0.5 | 21.5 | 0.7 | within band |
| raggio-ivf | Hybrid QPS concurrent | 37.9 | 1.5 | 39.0 | 0.1 | within band |
| raggio-ivf | Hybrid p99 under concurrency (ms) | 925.7 | 68.9 | 748.1 | 8.4 | better |
| raggio-ivf | Hybrid CPU per query under concurrency (ms) | 385.8 | 19.0 | 377.1 | 4.0 | within band |
| raggio-ivf | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio-ivf | Cold start to first query (s) | 2.6 | 0.3 | 3.0 | 0.1 | not claimable |

Beyond the noise band: better [('raggio', 'hybrid_qps_concurrent'), ('raggio', 'hybrid_c_p99'), ('raggio', 'hybrid_cpu_ms_per_q_c'), ('raggio-ivf', 'qps_concurrent'), ('raggio-ivf', 'hybrid_c_p99')], worse [('raggio', 'mem_after_ingest_mb'), ('raggio', 'mem_under_load_mb'), ('raggio', 'lat_filtered_p50'), ('raggio-ivf', 'mem_after_ingest_mb'), ('raggio-ivf', 'mem_under_load_mb'), ('raggio-ivf', 'cpu_ms_per_q_c'), ('raggio-ivf', 'lat_filtered_p95')].

```json
{"cpu_ms_per_q_c": {"baseline": [66.151464, 72.65985, 69.95770399999999], "candidate": [67.107076, 60.483230000000006, 71.182586]}, "cpu_missing": []}
```

### A1 → A2 (c=16)

A/B: baseline g-a1-py312-c16 vs candidate g-a2-py312-c16
Regime (baseline): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=16, python=3.12.15, sqlite_version=3.53.1, openblas_num_threads=1
Regime (candidate): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=16, python=3.12.15, sqlite_version=3.53.1, openblas_num_threads=1

| Engine | Metric | base median | base band | cand median | cand band | verdict |
|---|---|---|---|---|---|---|
| raggio | Memory after ingest (MB) | 1501 | 1 | 1501 | 1 | within band |
| raggio | Memory under query load (MB) | 1505 | 1 | 1506 | 1 | within band |
| raggio | Disk footprint (MB) | 16463 | 0 | 16463 | 0 | within band |
| raggio | Search p50 (ms) | 18.5 | 0.2 | 18.5 | 0.4 | within band |
| raggio | Search p95 (ms) | 19.7 | 0.1 | 19.8 | 0.7 | within band |
| raggio | Search p99 (ms) | 20.3 | 0.1 | 20.2 | 0.5 | within band |
| raggio | QPS serial | 54 | 1 | 54 | 1 | within band |
| raggio | QPS concurrent | 212 | 63 | 203 | 9 | within band |
| raggio | p95 under concurrency (ms) | 102.0 | 7.9 | 99.2 | 4.7 | within band |
| raggio | CPU per query under concurrency (ms) | 62.5 | 22.9 | 70.0 | 6.5 | within band |
| raggio | Filtered p50 (ms) | 25.3 | 4.7 | 23.3 | 2.7 | within band |
| raggio | Filtered p95 (ms) | 27.0 | 6.6 | 31.5 | 6.0 | within band |
| raggio | Recall@10 vs exact | 1.000 | 0.000 | 1.000 | 0.000 | within band |
| raggio | Hybrid p50 (ms) | 49.7 | 1.9 | 48.8 | 1.4 | within band |
| raggio | Hybrid p95 (ms) | 75.0 | 1.3 | 74.0 | 0.5 | within band |
| raggio | Hybrid p99 (ms) | 108.3 | 7.9 | 108.2 | 12.8 | within band |
| raggio | Hybrid first 10 queries, slowest (ms) | 126.4 | 0.2 | 126.6 | 1.3 | within band |
| raggio | Hybrid p99 without the first 10 (ms) | 90.4 | 0.4 | 90.2 | 0.8 | within band |
| raggio | Hybrid QPS serial | 20.0 | 0.5 | 20.2 | 0.5 | within band |
| raggio | Hybrid QPS concurrent | 34.3 | 1.8 | 34.3 | 1.0 | within band |
| raggio | Hybrid p99 under concurrency (ms) | 988.7 | 141.6 | 982.6 | 13.9 | within band |
| raggio | Hybrid CPU per query under concurrency (ms) | 483.5 | 19.7 | 480.4 | 10.9 | within band |
| raggio | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio | Cold start to first query (s) | 2.1 | 0.0 | 2.1 | 0.5 | not claimable |
| raggio-ivf | Memory after ingest (MB) | 1507 | 2 | 1505 | 1 | within band |
| raggio-ivf | Memory under query load (MB) | 1516 | 4 | 1515 | 2 | within band |
| raggio-ivf | Disk footprint (MB) | 16751 | 0 | 16751 | 0 | within band |
| raggio-ivf | Search p50 (ms) | 6.1 | 0.3 | 6.3 | 0.6 | within band |
| raggio-ivf | Search p95 (ms) | 7.4 | 0.4 | 7.5 | 0.6 | within band |
| raggio-ivf | Search p99 (ms) | 8.0 | 0.3 | 8.0 | 0.7 | within band |
| raggio-ivf | QPS serial | 161 | 6 | 157 | 13 | within band |
| raggio-ivf | QPS concurrent | 371 | 12 | 375 | 8 | within band |
| raggio-ivf | p95 under concurrency (ms) | 67.5 | 4.4 | 63.5 | 7.0 | within band |
| raggio-ivf | CPU per query under concurrency (ms) | 10.2 | 0.7 | 10.1 | 0.5 | within band |
| raggio-ivf | Filtered p50 (ms) | 6.1 | 0.2 | 6.0 | 0.4 | within band |
| raggio-ivf | Filtered p95 (ms) | 7.3 | 0.4 | 6.8 | 0.3 | better |
| raggio-ivf | Recall@10 vs exact | 0.995 | 0.000 | 0.992 | 0.000 | worse |
| raggio-ivf | Hybrid p50 (ms) | 46.2 | 1.3 | 46.6 | 0.9 | within band |
| raggio-ivf | Hybrid p95 (ms) | 71.8 | 1.9 | 72.3 | 1.1 | within band |
| raggio-ivf | Hybrid p99 (ms) | 107.3 | 13.7 | 97.5 | 2.3 | within band |
| raggio-ivf | Hybrid first 10 queries, slowest (ms) | 124.2 | 2.0 | 126.0 | 3.2 | within band |
| raggio-ivf | Hybrid p99 without the first 10 (ms) | 89.1 | 1.5 | 89.3 | 1.7 | within band |
| raggio-ivf | Hybrid QPS serial | 21.3 | 0.4 | 21.2 | 0.5 | within band |
| raggio-ivf | Hybrid QPS concurrent | 37.4 | 0.9 | 37.9 | 1.5 | within band |
| raggio-ivf | Hybrid p99 under concurrency (ms) | 897.5 | 58.9 | 925.7 | 68.9 | within band |
| raggio-ivf | Hybrid CPU per query under concurrency (ms) | 394.4 | 10.9 | 385.8 | 19.0 | within band |
| raggio-ivf | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio-ivf | Cold start to first query (s) | 2.6 | 0.0 | 2.6 | 0.3 | not claimable |

Beyond the noise band: better [('raggio-ivf', 'lat_filtered_p95')], worse [('raggio-ivf', 'recall_at_10')].

```json
{"cpu_ms_per_q_c": {"baseline": [71.137064, 62.535318, 48.257682], "candidate": [66.151464, 72.65985, 69.95770399999999]}, "cpu_missing": []}
```

### B1 → B2 (c=16)

A/B: baseline g-b1-py314t-c16 vs candidate g-b2-py314t-c16
Regime (baseline): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=16, python=3.14.8, sqlite_version=3.53.1, openblas_num_threads=1
Regime (candidate): page_cache=host-warm, first_start=host-warm, memory=4g, memory_swap=host-default, concurrency=16, python=3.14.8, sqlite_version=3.53.1, openblas_num_threads=1

| Engine | Metric | base median | base band | cand median | cand band | verdict |
|---|---|---|---|---|---|---|
| raggio | Memory after ingest (MB) | 1521 | 1 | 1522 | 1 | within band |
| raggio | Memory under query load (MB) | 1531 | 6 | 1530 | 3 | within band |
| raggio | Disk footprint (MB) | 16463 | 0 | 16463 | 0 | within band |
| raggio | Search p50 (ms) | 18.7 | 0.4 | 18.6 | 0.4 | within band |
| raggio | Search p95 (ms) | 19.8 | 0.3 | 19.8 | 0.4 | within band |
| raggio | Search p99 (ms) | 20.5 | 0.3 | 20.6 | 0.3 | within band |
| raggio | QPS serial | 53 | 1 | 53 | 1 | within band |
| raggio | QPS concurrent | 210 | 86 | 205 | 25 | within band |
| raggio | p95 under concurrency (ms) | 101.0 | 15.8 | 96.8 | 17.3 | within band |
| raggio | CPU per query under concurrency (ms) | 65.2 | 25.4 | 67.1 | 10.7 | within band |
| raggio | Filtered p50 (ms) | 25.1 | 2.5 | 26.9 | 1.6 | within band |
| raggio | Filtered p95 (ms) | 28.0 | 8.3 | 28.6 | 7.3 | within band |
| raggio | Recall@10 vs exact | 1.000 | 0.000 | 1.000 | 0.000 | within band |
| raggio | Hybrid p50 (ms) | 49.5 | 0.8 | 48.8 | 0.9 | within band |
| raggio | Hybrid p95 (ms) | 74.8 | 0.4 | 74.5 | 0.3 | within band |
| raggio | Hybrid p99 (ms) | 98.7 | 14.8 | 108.0 | 10.4 | within band |
| raggio | Hybrid first 10 queries, slowest (ms) | 127.0 | 0.6 | 126.1 | 3.3 | within band |
| raggio | Hybrid p99 without the first 10 (ms) | 90.6 | 0.9 | 89.9 | 1.8 | within band |
| raggio | Hybrid QPS serial | 19.9 | 0.3 | 20.2 | 0.3 | better |
| raggio | Hybrid QPS concurrent | 36.5 | 0.7 | 36.2 | 0.5 | within band |
| raggio | Hybrid p99 under concurrency (ms) | 794.9 | 31.3 | 804.3 | 8.7 | within band |
| raggio | Hybrid CPU per query under concurrency (ms) | 454.5 | 6.9 | 457.8 | 6.6 | within band |
| raggio | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio | Cold start to first query (s) | 2.5 | 0.0 | 2.5 | 0.1 | not claimable |
| raggio-ivf | Memory after ingest (MB) | 1528 | 1 | 1528 | 0 | within band |
| raggio-ivf | Memory under query load (MB) | 1548 | 3 | 1547 | 4 | within band |
| raggio-ivf | Disk footprint (MB) | 16751 | 0 | 16751 | 0 | within band |
| raggio-ivf | Search p50 (ms) | 6.1 | 0.2 | 6.1 | 1.1 | within band |
| raggio-ivf | Search p95 (ms) | 7.3 | 0.2 | 7.0 | 1.1 | within band |
| raggio-ivf | Search p99 (ms) | 7.9 | 0.5 | 7.5 | 1.2 | within band |
| raggio-ivf | QPS serial | 161 | 5 | 164 | 26 | within band |
| raggio-ivf | QPS concurrent | 469 | 31 | 482 | 32 | within band |
| raggio-ivf | p95 under concurrency (ms) | 55.0 | 15.0 | 59.2 | 7.2 | within band |
| raggio-ivf | CPU per query under concurrency (ms) | 10.9 | 0.2 | 10.9 | 0.2 | within band |
| raggio-ivf | Filtered p50 (ms) | 6.1 | 0.3 | 6.6 | 1.2 | within band |
| raggio-ivf | Filtered p95 (ms) | 7.1 | 0.8 | 8.1 | 0.5 | worse |
| raggio-ivf | Recall@10 vs exact | 0.992 | 0.000 | 0.993 | 0.000 | within band |
| raggio-ivf | Hybrid p50 (ms) | 45.9 | 0.9 | 45.6 | 2.0 | within band |
| raggio-ivf | Hybrid p95 (ms) | 71.7 | 1.2 | 71.4 | 2.0 | within band |
| raggio-ivf | Hybrid p99 (ms) | 99.1 | 6.9 | 97.6 | 3.5 | within band |
| raggio-ivf | Hybrid first 10 queries, slowest (ms) | 124.9 | 2.8 | 124.4 | 2.4 | within band |
| raggio-ivf | Hybrid p99 without the first 10 (ms) | 89.4 | 0.7 | 87.0 | 2.8 | within band |
| raggio-ivf | Hybrid QPS serial | 21.3 | 0.4 | 21.5 | 0.7 | within band |
| raggio-ivf | Hybrid QPS concurrent | 39.6 | 1.8 | 39.0 | 0.1 | within band |
| raggio-ivf | Hybrid p99 under concurrency (ms) | 743.3 | 28.3 | 748.1 | 8.4 | within band |
| raggio-ivf | Hybrid CPU per query under concurrency (ms) | 370.3 | 16.2 | 377.1 | 4.0 | within band |
| raggio-ivf | Hybrid text-hit@10 | 0.984 | 0.000 | 0.984 | 0.000 | within band |
| raggio-ivf | Cold start to first query (s) | 3.0 | 0.1 | 3.0 | 0.1 | not claimable |

Beyond the noise band: better [('raggio', 'hybrid_qps_serial')], worse [('raggio-ivf', 'lat_filtered_p95')].

```json
{"cpu_ms_per_q_c": {"baseline": [41.723188, 67.09540799999999, 65.182928], "candidate": [67.107076, 60.483230000000006, 71.182586]}, "cpu_missing": []}
```

### IVF fan-out

`bench/ivf_fanout_probe.py fanout` in each image, on the same IVF index, `bench-tv` stopped:

#### A

```text
loaded bench: 2549119 rows, nlist 256, nprobe 16, dim 1024, 4-bit, 1.1s
LISTS largest 23552 rows, 72% of the 32,768-row pooled-path cliff
nq=8 nprobe=16 k=50: 128 plain calls/batch, 53.3 grouped calls/batch; audience mean 2.40, histogram (size 1..) [933, 449, 278, 194, 104, 93, 55, 27]
  T    plain r1/r2 ms   grouped r1/r2 ms  plain x  grp x  verdict
  1    30.24/   30.23    38.19/   41.20     0.94   0.76  plain  (plain==serial: True; grouped same top-k 40/40)
  2    17.44/   17.44    25.00/   21.31     1.63   0.75  plain  (plain==serial: True; grouped same top-k 40/40)
  4    16.44/   12.68    13.23/   12.69     1.95   1.12  plain  (plain==serial: True; grouped same top-k 40/40)
  8     7.56/    8.45     8.45/    8.31     3.55   0.96  plain  (plain==serial: True; grouped same top-k 40/40)
 12     7.71/    7.56     7.11/    7.06     3.72   1.08  GROUPED WINS  (plain==serial: True; grouped same top-k 40/40)
 16     7.06/    7.70     7.01/    6.74     3.84   1.07  GROUPED WINS  (plain==serial: True; grouped same top-k 40/40)
 20     7.08/    7.16     7.21/    7.07     3.99   1.00  plain  (plain==serial: True; grouped same top-k 40/40)
 32     7.18/    7.43     6.68/    7.03     3.89   1.06  GROUPED WINS  (plain==serial: True; grouped same top-k 40/40)
serial r1/r2 28.39/29.95 ms; chosen pool size 8 (default here 12); GUARD grouped vs plain at 8: OK
```

#### B

```text
loaded bench: 2549119 rows, nlist 256, nprobe 16, dim 1024, 4-bit, 1.0s
LISTS largest 23552 rows, 72% of the 32,768-row pooled-path cliff
nq=8 nprobe=16 k=50: 128 plain calls/batch, 53.3 grouped calls/batch; audience mean 2.40, histogram (size 1..) [933, 449, 278, 194, 104, 93, 55, 27]
  T    plain r1/r2 ms   grouped r1/r2 ms  plain x  grp x  verdict
  1    28.08/   29.52    56.36/   51.10     1.03   0.54  plain  (plain==serial: True; grouped same top-k 40/40)
  2    16.57/   16.51    25.43/   23.26     1.79   0.68  plain  (plain==serial: True; grouped same top-k 40/40)
  4     9.96/   10.42    13.58/   13.70     2.91   0.75  plain  (plain==serial: True; grouped same top-k 40/40)
  8     7.52/    7.26     8.31/    8.50     4.01   0.88  plain  (plain==serial: True; grouped same top-k 40/40)
 12     6.99/    7.21     7.35/    7.44     4.17   0.96  plain  (plain==serial: True; grouped same top-k 40/40)
 16     6.90/    6.87     7.26/    6.92     4.31   0.97  plain  (plain==serial: True; grouped same top-k 40/40)
 20     6.86/    6.69     7.36/    7.47     4.38   0.91  plain  (plain==serial: True; grouped same top-k 40/40)
 32     7.21/    7.12     8.29/    8.24     4.14   0.87  plain  (plain==serial: True; grouped same top-k 40/40)
serial r1/r2 29.65/28.51 ms; chosen pool size 16 (default here 12); GUARD grouped vs plain at 16: OK
```

### Unicode drift

`bench/unicode_drift_probe.py` dumps of both images: 15.0.0 -> 16.0.0: 5812 newly assigned, 1 changed, 5053 with token drift.
Scan of the bench corpus:

```json
{"rows": 2549619, "rows_with_drift": 0, "rows_with_token_drift": 0, "first_rows_with_drift": []}
```

### Upstream wheels

https://pypi.org/pypi/turbovec/json on 2026-10-02: the latest release and its cp314t wheels.

```json
{"turbovec": "1.0.0", "cp314t_wheels": []}
```

### Decision

**Hold**: B won beyond the noise band on `Hybrid QPS concurrent` in both pairs, but turbovec 1.0.0 ships no cp314t wheel, and an own turbovec build never becomes the default (spec D6). The build arg and the CI job stay; re-run this ABAB when turbovec releases a cp314t wheel.

Not won:

- QPS concurrent: A2 → B2: A2 0 high/3 low, B2 1 high/2 low (high-CPU: >= 1.5x the pair's lowest CPU per query). The flat runs' fast/slow state must split evenly on both sides of a pair; until it does, the pair is neither a win nor a miss (spec §3.1 G5)

SQLite (spec D15): A runs SQLite 3.53.1, B runs SQLite 3.53.1. The hybrid rows run under SQLite's global memstatus mutex whatever the GIL does: a hybrid row that does not scale is SQLite-bound, not a free-threading failure, and credits a win only on one SQLite build.

Costs (flat rows `worse` beyond the noise band in both pairs; they do not gate): Memory after ingest (MB), Memory under query load (MB), Search p50 (ms), Search p95 (ms).

Noise (spec §6, D16): both arms run `OPENBLAS_NUM_THREADS=1`; rootless podman cannot pin cores, so a serial-latency cost is not attributed to free threading unless both arms are re-measured pinned to the X925 cores with `os.sched_setaffinity`.
