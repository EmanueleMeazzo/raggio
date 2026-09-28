# DGX probe p2 — hybrid stages, native BM25 prototype on aarch64 (2026-09-28)

This probe ran on gn100 from 05:55 to 08:36 CEST. The baseline image is Debian python 3.12.12 with libsqlite3 3.40.1 and glibc 2.36.

Raw evidence is in `D:/DEV/_scratch/dgx-probes/p2-hybrid-native/` and mirrored at `~/probe/p2-hybrid-native/`:

- `logs/e2e-summary-all.txt`: the 15-config matrix;
- `logs/e2e-ratios.txt`;
- `out/*.json`;
- the native prototype source;
- the wheel, at `build/wheels/raggio_native-0.1.0-cp312-cp312-linux_aarch64.whl`.

**The prototype is not Plan E's crate.** It uses the `unicode-normalization` and `unicode-general-category` crates (Unicode 16/17) in place of `build.rs`/`gen_unicode.py`. It was built on bookworm, not trixie. The e2e images patch `raggio.store` with probe knobs:

- `NATIVE_BM25`;
- `PRUNE_TOKEN_FIX`;
- `SQLITE_MEMSTATUS`.

## Key numbers

### Hybrid text-leg stages

Setup: baseline image, 4g, data-ivf copy, 300 seed-7 two-stage queries. Times are p50 / p99 in ms.

| Stage | true-cold | warm |
|---|---|---|
| prune/df | 13.2 / 317.9 | 0 |
| s1a ranked OR (FTS5) | 30.0 / 224.8 | 29.6 / 44.5 (max 634) |
| s1b AND | 3.3 / 30.3 | 3.2 / 29.9 |
| candidate fetch | 1.3 / 3.3 | 1.3 / 19.1 |
| **stage-2 Python** | 31.3 / 48.0 | 31.5 / 45.9 |
| whole text leg | 82.6 / 587.3 | 67.7 / 112.8 (max 682) |

Stage 2 is 38–46 % of the leg. It is not the 85 % that brief 1 assumed. s1a is the floor once stage 2 goes native.

### Native prototype build on aarch64

- **Toolchain:** `rust:1-slim-bookworm`, rustc 1.98.1, PyO3 0.29.2, maturin 1.15.0.
- **Wheel:** `uv build` with `--compatibility off` gives `cp312-cp312-linux_aarch64`.
- **The .so:** 595 kB. NEEDED is only `libgcc_s.so.1` and `libc.so.6`, with symbols from GLIBC_2.17 to 2.34.
- **Build time:** wheel about 10 s, image rebuild about 14 s.

### Parity against Python `_bm25_rescore`

- Scores and ids: **0 id and 0 bit mismatches** on 120 synthetic, 300 real (seed 7) and 494 bench hybrid queries.
- Tokenizer:
  - 0 divergences on 200k random strings.
  - 5,033 code points diverge, but none is assigned in UCD 15.0.0.
  - Golden cases: 31/31 identical on the DGX interpreter.
- Devanagari: `'हिन्दी'` tokenizes to `['ह','नद']` in both Python and native.

### Speed and scaling

**Native stage 2** runs at p50 1.5 ms and p99 2.5 ms, **21×** faster than Python (31.5 ms). The whole text leg, warm:

- native: p50 38.7 ms, p99 86.5 ms;
- Python: p50 68.9 ms, p99 114.0 ms.

**Thread scaling of the text leg, host-warm, 4g.** Values are q/s at T=1/2/4/8.

| Config | q/s |
|---|---|
| Python, Debian SQLite (memstatus on) | 14.4 / 22.6 / 21.5 / 16.3 |
| native, memstatus on | 26.1 / 34.4 / 18.1 / 16.6 |
| SQL only, memstatus on | 27.9 / 36.5 / 18.4 / 16.8 |
| Python, memstatus **off** | 15.1 / 23.9 / 25.3 / 19.8 |
| native, memstatus **off** | 27.9 / 49.5 / 82.4 / **100.1** |
| SQL only, PBS CPython 3.12.12 + bundled SQLite 3.50.4 | 28.6 / 47.6 / 45.4 / 39.4 |

- The SQL-only memstatus-on run shows sys time of 88.8 s and 4.1M voluntary context switches.
- Memstatus was switched off with a ctypes hack (`sqlite3_shutdown` + `config(9,0)` + `initialize`).
- On PBS memstatus cannot be switched off: `_sqlite3` is built in, and `sqlite3_config` is not exported.
- The JSON-aggregate SQL variant gave **no gain** (0/300 mismatches, same collapse).

### Tail attribution (hybrid p99 ≈ 555 ms)

**Cause.** 24 of the 500 bench titles contain underscore tokens. `_prune_common`'s `re.findall(r'\w+')` keeps `'_2'`, `'_x'` and `'_'`:

- their df is 0, so they are always kept;
- FTS5 unicode61 turns them into `'2'` and similar, with df around 2.5M;
- the ranked OR stage 1a then matches 347k–738k rows, against a budget of 50,982 postings.

These queries take 400–665 ms in s1a. The worst is i=153: 737,927 matches in 624.5 ms, against a median of 30–34k matches in 28.5 ms. Fetch and stage 2 are not involved.

**`fix_sim`:** the prune step uses `[^\W_]+`, the stage-2 tokenizer, with `_fold`.

| Metric | Before | After |
|---|---|---|
| s1a p99 / max | 496–501 / 664 ms | 43.7 / 62 ms |
| native text-leg warm p99 | 500.5 ms | 68.8 ms |
| Python text-leg warm p99 | 534.6 ms | 104.6 ms |
| OR queries over budget | 15 | 2 |
| text-leg p50 | — | unchanged |
| hit@10 (probe) | 0.994 | 0.994 |

The fix changes the kept token set of 24 queries:

- top-10 is identical on 98.2 % of queries;
- top-100 is identical on 96.2 %;
- top-100 overlap is 98.98 %.

**A second, smaller tail source:** the first ~10 queries after a cold load take 470–980 ms. Only a warm-up fixes it.

### End-to-end matrix

Setup for every row:

- a fresh 4g server on a copy of data-ivf, true-cold after fadvise eviction;
- the first GET (cold load) took 147–161 s in every config;
- 500 seed-42 queries per phase.

Phases:

- **serial1:** bench-equivalent;
- **concurrent:** c=8;
- **serial2 / serial3:** warm repeats.

Each row gives p50 / p95 / p99 in ms. "fix" means the prune-tokenizer fix.

**IVF (nlist 256 / nprobe 16):**

| Config | serial1 | conc QPS | serial3 |
|---|---|---|---|
| A base | 139.7 / 275.7 / 657.1 | 13.2 | 88.8 / 139.8 / 546.7 |
| A2 knob off | 142.1 / 274.3 / 652.9 | 13.2 | 95.2 / 139.6 / 540.9 |
| B native | 107.8 / 241.8 / 616.9 | 13.5 (p99 7.1 s) | 76.0 / 110.7 / 523.8 |
| C native, memstatus off | 102.8 / 238.4 / 591.6 | 49.7 | 66.8 / 105.7 / 504.0 |
| G native + fix | 107.7 / 169.3 / 510.5 | 18.0 | 72.7 / 102.1 / **120.8** |
| F native, memstatus off + fix | 102.2 / 173.0 / 519.1 | **54.2** (p99 258 ms) | 66.7 / 97.5 / 107.1 |
| D Python + fix | 140.4 / 210.9 / 532.7 | 13.6 | 95.2 / 132.0 / 151.8 |
| H PBS native | 96.1 / 216.0 / 538.4 | 33.9 | 67.7 / 105.7 / 458.0 |
| I PBS Python | 134.7 / 245.9 / 565.9 | 16.1 | 93.3 / 131.8 / 476.7 |
| J PBS native + fix | 95.7 / 157.4 / 417.8 | 41.7 (p99 375 ms) | 64.6 / 94.3 / 111.6 |

**FLAT (Plan E's gate path):** data detached at 8g in 285.2 s.

| Config | serial1 | conc QPS | serial3 |
|---|---|---|---|
| K base | 133.4 / 278.2 / 637.9 | 13.35 | 90.7 / 142.6 / 542.3 |
| N knob 0 | 132.7 / 280.1 / 645.5 | 13.3 | 91.9 / 142.7 / 540.7 |
| L native | 105.0 / 239.6 / 622.0 | 13.15 (p99 6.9 s) | 66.4 / 114.2 / 520.0 |
| M native, memstatus off + fix | 97.0 / 169.4 / 525.8 | 48.6 (p99 282 ms) | 71.8 / 103.4 / 116.1 |
| O PBS native | 95.3 / 215.1 / 543.8 | 28.7 | 71.4 / 110.8 / 445.5 |

**Ratios for Plan E alone, against the mean of the base and knob-off arms:**

| Path | p50 (serial1 / serial2 / serial3) | Other | Between-server band |
|---|---|---|---|
| flat | 0.79 / 0.71 / 0.73 | p95 0.80–0.87, p99 0.96–0.97, conc QPS 0.99× | p50 0.6–1.3 %, p95 ≤ 5.5 % |
| IVF | 0.76 / 0.78 / 0.83 | — | p50 1.7–7.0 % |
| PBS, native vs Python | 0.71–0.73 | conc QPS 2.1× | — |

**Correctness across the matrix:**

- text_hit@10 is 0.984 in all 15 configs.
- Hybrid hits are 500/500 identical to base for every native and knob-off arm, and 495/500 for the prune-fix arms.

**Side observations:**

- Vector concurrent QPS is bimodal (120–225) regardless of config.
- The first filtered query does a full scan (max 47–54 s).
- Flat vector serial p50 varies from 22.3 to 27.4 ms between servers.

## Conclusions

1. **Estimated bench hybrid after Plan E (flat, host-warm):**
   - p50 about 63–70 ms (0.71–0.79×);
   - p95 about 128–141 ms;
   - p99 about 530–545 ms, essentially unchanged;
   - concurrent QPS about 1.0×, with concurrent p99 roughly doubled.
   - IVF p50 about 65–75 ms.
2. **The prune-tokenizer fix is the only p99 lever.** Warm p99 goes from about 541 to about 117 ms, and p95 to about 115–135 ms. If a pass starts FTS-true-cold, the first ~10 queries keep p99 at about 420–525 ms.
3. **Concurrency is limited by SQLite, not only the GIL.**
   - With memstatus on, concurrent hybrid is capped by SQLite's global memstatus malloc mutex (Debian 3.40.1).
   - PBS SQLite 3.50.4 (spec D2's interpreter) gives native 2.1× over Python.
   - Memstatus off gives 3.6–4.1× (48–54 QPS).
4. **Plan E's p50 gate is realistic:** an effect of −21 % to −29 %, against a band of ≤ 2.8 %. The brief's "p50 ≤ 0.75×" is borderline on the true-cold first pass and on IVF.
5. **Brief targets:**
   - "Concurrent QPS ≥ 1.5×" fails on Debian SQLite 3.40.1 and passes on PBS.
   - "stage-2 ≤ 5 ms and ≥ 10×" is met (1.5 ms, 21×).
6. **The JSON-aggregate SQL follow-up is falsified:** it showed no gain on the DGX.
7. **aarch64 Dockerfile gotchas for Plan E:**
   1. `rust:*-slim` images have **no python3**. Build with the uv-installed `/python` (PYO3_PYTHON, `gen_unicode.py`). Whether maturin exports PYO3_PYTHON to `build.rs` was **not verified** on aarch64.
   2. `--compatibility off` gives a non-manylinux `cp312` wheel. **The builder's Debian release must be ≤ the runtime's** (trixie → trixie is fine). Plan G's 3.14/3.14t images need their own build.
   3. The builder needs network access to PyPI and crates.io, or a vendored/`--locked` setup.
   4. The default OCI format of podman **ignores `SHELL`**, so use `set -o pipefail` inside `RUN`.
   5. PBS SQLite is statically linked, so memstatus cannot be changed at runtime.
8. **D10 parity holds** on the DGX interpreter (UCD 15.0.0). The real tokenizer inconsistency is the prune step's `\w+`, which disagrees with both stage 2 and FTS5 unicode61.

## Plan impacts (as proposed by the probe)

- **E:**
  - Add the prune-tokenizer fix as its **own task and its own A/B arm**, before the native arm, with an underscore-title regression test (the i=153 pattern). Gate: text_hit@10 unchanged, and the OR match count within budget.
  - Correct the cost model (stage 2 is 31.5 ms / 38–46 %).
  - Drop the JSON-aggregate follow-up.
  - Concurrent QPS is not a gate on Debian SQLite.
  - Use the Dockerfile gotchas above.
  - A/B protocol: same page-cache regime per arm, compare warm passes, report pass 1 separately, and keep a knob-off arm on a separate server.
- **A / D2:** moving from Debian SQLite 3.40.1 to PBS SQLite 3.50.4 is itself a performance change:
  - IVF concurrent hybrid 13.2 → 16.1 QPS with Python;
  - warm p99 about 541 → 458–477 ms.
  - So A's "equivalence re-baseline" will not be equivalent on hybrid concurrent/p99 after the base migration. Record a new reference baseline after D2.
- **F:**
  - Fan-out threads that hydrate or filter through SQLite share the memstatus mutex. Measure on post-D2 SQLite, and record `sqlite_version`.
  - Vector concurrent QPS is bimodal, so take the band from ≥ 3 runs.
- **G:**
  - Removing the GIL will not make the text leg scale while SQLite takes a global mutex per malloc.
  - G's verdict needs this understood: on PBS memstatus is fixed on (plateau about 39–48 q/s SQL-only).
  - `raggio_native` must be rebuilt per interpreter.
- **C:**
  - The first GET after a fadvise true-cold eviction takes 147–161 s in this probe.
  - The first filtered query does a 47–54 s full scan.
  - The first ~10 hybrid queries after a load are slow, which a warm-up could hide.
- **B:** concurrent c=8 p99 of 3–7 s comes from the SQLite mutex convoy. Any latency check of B's locks must use the same SQLite build.
- **Spec §6:**
  - The noise band must include **between-server** variance (IVF identical code 6–7 %, flat ≤ 1.3 %).
  - Every row states its regime (true-cold vs host-warm) and cap. Pages cached by dead cgroups escape the 4g cap.
