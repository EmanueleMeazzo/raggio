# ADR 0004 — Native stage-2 BM25 scorer and tokenizer (`raggio_native`)

Status: accepted · Date: 2026-10-01

## Context

ADR 0003 made stage 2 of the text leg a Python loop: full-query BM25 plus the SDM-lite
bigram bonus over the FTS5 candidates (up to ~1,500 per query). Its consequences priced
that at "~tens of ms at 2.5M rows". Measured since on the DGX (2.5M arXiv records, flat
index, warm page cache; `docs/superpowers/research/2026-09-28-dgx-probe-p2-hybrid-native.md`):

- The text leg's p50 is 67.7 ms: the ranked-OR stage 1a 29.6 ms, the AND stage 1b
  3.2 ms, the candidate fetch 1.3 ms, and the Python stage 2 31.5 ms, 38–46% of the leg.
  Stage 1a is SQLite's, and it is the floor any scorer leaves.
- Stage 2 holds the GIL throughout, so the vector leg that `asyncio.gather` runs beside
  it waits. ADR 0001's "SQLite releases the GIL; legs overlap fully" (its line 18)
  predates the Python stage 2 and no longer holds while stage 2 runs in Python.
- A Rust/PyO3 scorer returned the same ids and bit-identical scores in 1.5 ms p50
  (2.5 ms p99), 21× the Python scorer, with the GIL released. The text leg fell from
  68.9 to 38.7 ms p50, and flat hybrid p50 to 0.71–0.79× over three runs.
- The hybrid p99 (~541 ms warm) was not stage 2's. 24 of the 500 bench titles hold
  underscore tokens (`MgB$_2$`), which `_prune_common`'s `\w+` kept with df 0 while FTS5
  indexes them as near-universal terms, so the ranked OR scanned 347k–738k rows against
  a ~51k budget.

## Decisions

| Decision | Rationale / evidence |
|---|---|
| **The prune tokenizes like stage 2 and FTS5** (spec D14). `_prune_common` keeps `_fold_tokens(query)` tokens (`[^\W_]+` after the fold) and keys the df cache by the term FTS5 matches (`_fold` of each token); trigram collections, where `_` and diacritics are significant substrings, keep `\w+`. The prune's second caller, the text-mode sibling expansion (`Collection._expand`), changes with it. `bench/text_probe.py` (ADR 0003's evidence script) replicates the pre-D14 prune and is left untouched. Landed and measured on its own, before the native scorer | The three token paths now agree on `_`. It is a **ranking change**: both MATCH strings now carry folded tokens, so compatibility forms in a query (ligatures, superscripts, math alphanumerics) match their ASCII spelling. The converse is a loss: FTS5's unicode61 tokenizer indexes these forms as written, so a query term such as `CO₂`, `m²` or `ℝ³` now asks for `co2`, `m2` or `r3` and no longer matches a document that holds the original form. A query with other matching terms still finds that document through them. The final review found this loss after p2's simulation, whose bench queries are ASCII; it is accepted here, and matching both forms is a follow-up. p2's simulation kept hit@10 at 0.994 and top-10 identical on 98.2% of bench queries, and moved the warm text-leg p99 from 534.6 to 104.6 ms. It is this change's only p99 lever |
| **A Rust + PyO3 0.29 + maturin extension**, import name `raggio_native`, distribution `raggio-native`, crate in `native/`. It exports `fold_tokens(text)` and `bm25_topn(qtoks, idf, rids, texts, avgdl, n, k1, b, sdm_weight)`, and runs the whole candidate loop inside `py.detach`, with the GIL released | The only option with maintained Unicode machinery, GIL release and a proven bit-exact prototype (options below). turbovec builds with the same toolchain |
| **Bit parity, not a tolerance.** `bm25_topn` returns the ids and the exact float bits of `store._bm25_topn`, the pure-Python reference, in `(-score, rid)` order | CPython 3.12's `sum()` is Neumaier-compensated. The crate's `PySum` replays it term by term, in the tf dict's first-occurrence order; every expression keeps Python's operation order; Rust never contracts to FMA. A candidate with no query token and no query bigram scores the int `0`, as in Python. Pinned by the goldens, the edge cases and 40 seeded random scenarios, each with an exact tie, in `tests/test_native_bm25.py` |
| **Tokenizer parity with the build interpreter's Unicode data.** `native/gen_unicode.py`, run by `build.rs` with the interpreter PyO3 builds for, generates the tables from that interpreter's `unicodedata`: the word class `[^\W_]`, the `lower()` + NFKD + drop-combining fold of every code point, and the Final_Sigma context classes. The crate exports `UNIDATA_VERSION`; `store._load_native` refuses an extension built for other Unicode data (RuntimeWarning, Python scorer) | Spec D10: exact parity, bit-identical by `repr`, including mathematical alphanumerics folding to uppercase ASCII, Hangul to jamo and Indic spacing marks splitting words: Devanagari `'हिन्दी'` is `['ह', 'नद']` in both, because Python's `re` does not count spacing vowel signs as word characters and the fold drops the virama. Rust's own tables are Unicode 16/17 against Python 3.12's 15.0, which would diverge on ~5,000 unassigned code points. Every code point is tested against `_fold_tokens` |
| **Query-side work stays in Python**: `_fold_tokens(query)`, `_prune_common`, the df lookups, and the IDF from one read of `N = sum(indexed_counts.values())` | Query text arrives as JSON and can hold lone surrogates, which PyO3's UTF-8 extraction rejects; candidate texts come from SQLite and cannot. IDF needs the shared df cache anyway, and the query side is microseconds |
| **Optional, with Python as the reference and the fallback.** The `native` extra (`uv sync --extra native`) resolves `raggio-native` from `native/` (`[tool.uv.sources]`). Without the extension raggio runs the Python scorer, with the same results | Development and CI without Rust keep working; the Python code stays the specification |
| **`NATIVE_BM25`**, a deployment knob in `raggio.config.Settings`, unprefixed like the others: `auto` (the default; empty means `auto`) runs native when it is importable, `0` forces Python. `GET /healthz` reports `"bm25": "native"` or `"python"` | Rollback without a rebuild, and a visible answer to which scorer an instance runs. The D14 prune tokenizer is not behind this knob: `0` restores the Python scorer, not the pre-D14 prune |
| **Built from source for the exact interpreter**: no abi3, `#[pymodule(gil_used = false)]` | abi3 cannot load on free-threaded builds; building for the image's own `/python` keeps a 3.14t image possible |
| **Image**: the Dockerfile's builder stage is `rust:${RUST_VERSION}-slim-trixie` with `ARG RUST_VERSION` pinned. It builds for uv's `/python`, linked as `python3` and named in `PYO3_PYTHON`. Both `uv sync` calls add `--extra native`, cargo builds `--locked`, `CARGO_TARGET_DIR` stays outside `/app`, and the runtime stage is still plain `debian:trixie-slim` without a toolchain | Spec D2 and §4.1. The slim Rust image has no `python3`, and the extension and its Unicode tables must be built by the interpreter the runtime runs. The wheel is not manylinux-audited, so the builder runs the runtime's Debian release. `native/` is copied before the dependency-only sync, so the extension's layer caches apart from `src/` |
| **CI**: a `native` job on `ubuntu-latest` and `ubuntu-24.04-arm` installs the pinned Rust, builds the extra and runs the suite with `REQUIRE_NATIVE=1`, which turns a missing extension into a failure instead of skipped parity tests. The `test` job keeps running without the extension | Spec D11. aarch64 is the DGX's architecture: bit parity there is checked by CI and by `bench/bm25_probe.py` |
| **First-party native code is in scope** | ADR 0001's standing constraint that turbovec is an external dependency ([its line 48](0001-performance-optimization-decisions.md)) is superseded for first-party native code: `native/` is raggio's own crate, built with the image. The turbovec stance itself is unchanged here; ADR 0005 revisits it |

## Options considered

| Option | Verdict |
|---|---|
| (a) Rust + PyO3 + maturin | **Adopted** |
| (b) C extension | Rejected: no Unicode tables without ICU or utf8proc, each with its own Unicode version; more unsafe code |
| (c) Cython | Rejected: the work is `str` processing that needs the GIL; a nogil core needs its own tables, as in (b) |
| (d) numpy | Rejected: most of stage 2 is tokenizing the candidate texts, and numpy cannot tokenize |
| (e) Tokens precomputed at ingest | Rejected for now: gigabytes of extra storage or a change to FTS5 position semantics, plus a migration and ingest cost |
| (f) LRU of folded tokens | Rejected: ~12 KB of Python objects per record, a low hit rate on real traffic, and it would flatter the benchmark's repeated concurrent queries |
| (g) Pure-Python ASCII fast path | Not part of this change: 2.5–3.5x on ASCII corpora but still GIL-bound, and it would make the reference less plainly the reference |
| (h) One aggregate row per SQL statement in `_text_ids`, instead of one cursor step per row | Rejected: measured on the DGX, it returned the same candidates and gained nothing. What caps concurrent text legs there is SQLite's memstatus mutex, not the per-row GIL handoff |

## Consequences

- Stage 2 costs 1.5 ms p50 instead of 31.5 ms on the DGX, with the GIL released. The
  scorer changes no record, order or score. Flat hybrid p50 is expected at
  0.71–0.79×; the text leg's floor is now stage 1a (~30 ms), which is SQLite's.
- The hybrid p99 is the prune fix's. The first ~10 queries after a load still take
  470–980 ms whatever the scorer; only a warm-up hides them.
- Concurrent hybrid does not scale with the GIL released on Debian's SQLite 3.40.1: its
  global memstatus mutex serializes every SQLite allocation. p2 measured 0.99× concurrent
  QPS there, with a native concurrent p99 of 6.9 s; on PBS's SQLite 3.50.4 native gave
  2.1× the Python scorer's concurrent QPS; that was not re-measured on SQLite 3.53.1,
  which uv 0.12.19's PBS interpreters bundle. Concurrent QPS and p99 are reported,
  not gated, and every DGX row records `sqlite3.sqlite_version` (spec D15).
- Two implementations must stay in lockstep: a change to `_bm25_topn` or `_fold_tokens`
  changes `native/src/lib.rs` in the same commit, or the parity tests fail. So would a
  future change to CPython's `sum()`.
- An interpreter upgrade that changes `unicodedata.unidata_version` needs the extension
  rebuilt. The image always rebuilds it; a stale local build is refused, never trusted.
- `uv sync` is exact: a sync without `--extra native` uninstalls the extension. Tests
  then skip the native cases, or fail under `REQUIRE_NATIVE=1`.
- The image build pulls the Rust image and compiles the crate (~25 s), and needs network
  access to PyPI and crates.io. The runtime image carries no toolchain.
- FTS5's `unicode61` tokenizer (stage 1, Unicode 6.1 tables) still differs from the
  stage-2 tokenizer on scripts newer than Unicode 6.1. That predates this decision and is
  unchanged by it (spec D10).

## Results — DGX A/B

Measured on gn100 on 2026-10-01, in one window, following spec §6. Every arm is a fresh `bench-tv` container on the same volume and flat index, with a 4 GiB cap and podman's default swap. All arms are host-warm: a discarded warm-up arm ran first, and no cold-start claim is made (spec D12). Each arm sends bench.py's hybrid queries (500, `--limit 2549619`, seed 42) through `bench/bm25_probe.py passes`: three serial passes, then one at concurrency 8. Pass 1 is the first after the load and is reported apart; the gates compare the warm passes 2 and 3. e-prune and e-python run the same scorer on two servers. The band is their gap, or any compared arm's pass-2 vs pass-3 spread if that is wider.

### Arms

| Arm | What it runs | Image | `bm25` in `/healthz` | Python | turbovec | `NATIVE_BM25` | `/healthz` after start | first GET after start | Regime |
|---|---|---|---|---|---|---|---|---|---|
| e-base | `main` at the merge base: the `\w+` prune, Python stage 2 | `localhost/raggio:49c09f2` | absent | 3.12.14 | 1.0.0 | unset | 320 ms | 2608 ms | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-prune | Task 1's commit: the D14 prune, Python stage 2 | `localhost/raggio:be2f37c` | absent | 3.12.14 | 1.0.0 | unset | 851 ms | 3171 ms | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-native | the branch head: the D14 prune, native stage 2 | `localhost/raggio:f95b3b8` | native | 3.12.14 | 1.0.0 | unset | 845 ms | 3153 ms | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-python | the branch head with `ENV NATIVE_BM25=0`: the D14 prune, Python stage 2 | `localhost/raggio:f95b3b8-py` | python | 3.12.14 | 1.0.0 | 0 | 842 ms | 3144 ms | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |

### Hybrid passes

Latencies in ms. "First 10 max" is the slowest of the pass's first 10 queries. Text-hit is bench's hybrid text-hit@10.

| Arm | Pass | p50 | p95 | p99 | First 10 max | q/s | Text-hit@10 | Regime |
|---|---|---|---|---|---|---|---|---|
| e-base | 1 (first after the load) | 79.209 | 119.883 | 489.455 | 447.286 | 11.4 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-base | 2 (warm) | 76.994 | 110.649 | 482.868 | 439.196 | 11.8 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-base | 3 (warm) | 77.573 | 108.365 | 489.26 | 407.524 | 11.8 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-prune | 1 (first after the load) | 79.19 | 113.598 | 145.116 | 145.985 | 12.5 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-prune | 2 (warm) | 77.718 | 98.687 | 119.862 | 120.695 | 13.0 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-prune | 3 (warm) | 76.973 | 97.702 | 115.94 | 118.476 | 13.1 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-native | 1 (first after the load) | 49.421 | 74.685 | 96.751 | 125.592 | 20.0 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-native | 2 (warm) | 46.552 | 66.763 | 84.839 | 84.858 | 21.5 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-native | 3 (warm) | 45.906 | 64.687 | 80.409 | 80.409 | 22.0 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-python | 1 (first after the load) | 79.506 | 106.761 | 138.081 | 141.725 | 12.6 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-python | 2 (warm) | 76.887 | 97.947 | 116.64 | 119.792 | 13.1 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-python | 3 (warm) | 76.931 | 97.923 | 116.398 | 121.033 | 13.2 | 0.984 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |

### Prune tokenizer (D14): e-prune vs e-base

| Metric (warm mean, ms) | Candidate | Reference | Ratio | Band | Verdict | Regime |
|---|---|---|---|---|---|---|
| Hybrid p99 (gate) | 117.9 (e-prune) | 486.1 (e-base) | 0.24× | ±6.4 (1.3%) | better | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| Hybrid p95 (reported) | 98.2 (e-prune) | 109.5 (e-base) | 0.90× | ±2.3 (2.1%) | better | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| Hybrid p50 (reported) | 77.3 (e-prune) | 77.3 (e-base) | 1.00× | ±0.7 (1.0%) | within band | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |

Gate: warm p99 better beyond the band. Text-hit@10 over every pass of every arm: [0.984]. Hybrid top-10s changed by the prune: 5 of 500 (1.0%). p2 measured 5 of 500 hybrid top-10s, and 1.8% of the text leg's. The ranked OR, counted by the probe with the D14 prune: 24 underscore queries, the largest matching 48760 rows against a budget of 50982; 2 of 500 queries over the budget.

### Native scorer: e-native vs e-prune and e-python

| Metric (warm mean, ms) | Candidate | Reference | Ratio | Band | Verdict | Regime |
|---|---|---|---|---|---|---|
| Hybrid p50 (gate) | 46.2 (e-native) | 77.1 (e-prune + e-python) | 0.60× | ±0.7 (1.0%) | better | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| Hybrid p95 (reported) | 65.7 (e-native) | 98.1 (e-prune + e-python) | 0.67× | ±2.1 (2.1%) | better | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| Hybrid p99 (reported) | 82.6 (e-native) | 117.2 (e-prune + e-python) | 0.70× | ±4.4 (3.8%) | better | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |

Gate: warm flat hybrid p50 better beyond the band; p2 expects 0.71–0.79×. Hybrid top-10s changed: e-native vs e-python 0, and e-python vs e-prune 0, so `NATIVE_BM25=0` gives the prune arm's rankings back. Parity by `repr` on the same query texts is in the probe below.

### Concurrent (reported, not gated)

Spec D15: SQLite's memstatus mutex caps concurrent hybrid, so concurrent QPS and p99 are reported, not gated. No expectation is declared for SQLite 3.53.1: p2 measured 0.99× on Debian's 3.40.1 and 2.1× on PBS's 3.50.4.

| Arm | q/s | p50 (ms) | p99 (ms) | SQLite | Regime |
|---|---|---|---|---|---|
| e-base | 17.7 | 442.631 | 1047.152 | 3.53.1 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-prune | 17.5 | 459.336 | 795.416 | 3.53.1 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-native | 36.7 | 217.048 | 440.907 | 3.53.1 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |
| e-python | 17.7 | 445.904 | 804.444 | 3.53.1 | host-warm, 4g, swap host-default, SQLite 3.53.1, OPENBLAS_NUM_THREADS=1 |

e-native / e-python at concurrency 8: 2.07× the q/s, 0.55× the p99.

### Measurements

```json
{
 "queries": 500,
 "arms": {
  "e-base": {
   "queries": 500,
   "passes": [
    {
     "p50": 79.209,
     "p95": 119.883,
     "p99": 489.455,
     "first10_max": 447.286,
     "qps": 11.4,
     "text_hit": 0.984
    },
    {
     "p50": 76.994,
     "p95": 110.649,
     "p99": 482.868,
     "first10_max": 439.196,
     "qps": 11.8,
     "text_hit": 0.984
    },
    {
     "p50": 77.573,
     "p95": 108.365,
     "p99": 489.26,
     "first10_max": 407.524,
     "qps": 11.8,
     "text_hit": 0.984
    }
   ],
   "concurrent": {
    "qps": 17.7,
    "p50": 442.631,
    "p99": 1047.152
   },
   "python": "3.12.14",
   "sqlite_version": "3.53.1",
   "turbovec": "1.0.0",
   "openblas_num_threads": "1",
   "native_bm25": "unset",
   "page_cache": "host-warm",
   "memory": "4g",
   "memory_swap": "host-default",
   "bm25": "absent",
   "image": "localhost/raggio:49c09f2"
  },
  "e-prune": {
   "queries": 500,
   "passes": [
    {
     "p50": 79.19,
     "p95": 113.598,
     "p99": 145.116,
     "first10_max": 145.985,
     "qps": 12.5,
     "text_hit": 0.984
    },
    {
     "p50": 77.718,
     "p95": 98.687,
     "p99": 119.862,
     "first10_max": 120.695,
     "qps": 13.0,
     "text_hit": 0.984
    },
    {
     "p50": 76.973,
     "p95": 97.702,
     "p99": 115.94,
     "first10_max": 118.476,
     "qps": 13.1,
     "text_hit": 0.984
    }
   ],
   "concurrent": {
    "qps": 17.5,
    "p50": 459.336,
    "p99": 795.416
   },
   "python": "3.12.14",
   "sqlite_version": "3.53.1",
   "turbovec": "1.0.0",
   "openblas_num_threads": "1",
   "native_bm25": "unset",
   "page_cache": "host-warm",
   "memory": "4g",
   "memory_swap": "host-default",
   "bm25": "absent",
   "image": "localhost/raggio:be2f37c"
  },
  "e-native": {
   "queries": 500,
   "passes": [
    {
     "p50": 49.421,
     "p95": 74.685,
     "p99": 96.751,
     "first10_max": 125.592,
     "qps": 20.0,
     "text_hit": 0.984
    },
    {
     "p50": 46.552,
     "p95": 66.763,
     "p99": 84.839,
     "first10_max": 84.858,
     "qps": 21.5,
     "text_hit": 0.984
    },
    {
     "p50": 45.906,
     "p95": 64.687,
     "p99": 80.409,
     "first10_max": 80.409,
     "qps": 22.0,
     "text_hit": 0.984
    }
   ],
   "concurrent": {
    "qps": 36.7,
    "p50": 217.048,
    "p99": 440.907
   },
   "python": "3.12.14",
   "sqlite_version": "3.53.1",
   "turbovec": "1.0.0",
   "openblas_num_threads": "1",
   "native_bm25": "unset",
   "page_cache": "host-warm",
   "memory": "4g",
   "memory_swap": "host-default",
   "bm25": "native",
   "image": "localhost/raggio:f95b3b8"
  },
  "e-python": {
   "queries": 500,
   "passes": [
    {
     "p50": 79.506,
     "p95": 106.761,
     "p99": 138.081,
     "first10_max": 141.725,
     "qps": 12.6,
     "text_hit": 0.984
    },
    {
     "p50": 76.887,
     "p95": 97.947,
     "p99": 116.64,
     "first10_max": 119.792,
     "qps": 13.1,
     "text_hit": 0.984
    },
    {
     "p50": 76.931,
     "p95": 97.923,
     "p99": 116.398,
     "first10_max": 121.033,
     "qps": 13.2,
     "text_hit": 0.984
    }
   ],
   "concurrent": {
    "qps": 17.7,
    "p50": 445.904,
    "p99": 804.444
   },
   "python": "3.12.14",
   "sqlite_version": "3.53.1",
   "turbovec": "1.0.0",
   "openblas_num_threads": "1",
   "native_bm25": "0",
   "page_cache": "host-warm",
   "memory": "4g",
   "memory_swap": "host-default",
   "bm25": "python",
   "image": "localhost/raggio:f95b3b8-py"
  }
 },
 "top10_changed": {
  "e-prune vs e-base": 5,
  "e-python vs e-prune": 0,
  "e-native vs e-python": 0
 }
}
```

### Probe

`bench/bm25_probe.py parity` in `localhost/raggio:f95b3b8`, over the bench collection's `meta.db` with `bench-tv` stopped, on the same query texts:

```text
parity: 500 queries (494 two-stage): id-order mismatches 0, score bit-mismatches 0, max relative diff 0
avgdl: native 158.41015625 python 158.41015625
stage 2 p50/p99 ms: native [1.646, 2.676] python [31.117, 46.776]
text leg p50/p99 ms: native [36.212, 71.686] python [65.878, 103.783]
text leg q/s by threads: native {'1': 27.4, '2': 45.7, '4': 45.6, '8': 42.8} python {'1': 15.1, '2': 23.0, '4': 27.1, '8': 23.0}
ranked OR: budget 50982 rows, queries over it 2, underscore queries' matches [28195, 44742, 24329, 45409, 38181, 48760, 37670, 35882, 22435, 24650, 38989, 34856, 46550, 42322, 12017, 22696, 44445, 40444, 27214, 35765, 27070, 32746, 42145, 35019]
PASS
```

```json
{"queries": 500, "avgdl_native": 158.41015625, "avgdl_python": 158.41015625, "two_stage": 494, "id_mismatches": 0, "score_bit_mismatches": 0, "max_rel_diff": 0.0, "stage2_ms_native": [1.646, 2.676], "text_leg_ms_native": [36.212, 71.686], "stage2_ms_python": [31.117, 46.776], "text_leg_ms_python": [65.878, 103.783], "text_leg_qps_native": {"1": 27.4, "2": 45.7, "4": 45.6, "8": 42.8}, "text_leg_qps_python": {"1": 15.1, "2": 23.0, "4": 27.1, "8": 23.0}, "or_budget": 50982, "or_over_budget": 2, "or_matches_underscore": [28195, 44742, 24329, 45409, 38181, 48760, 37670, 35882, 22435, 24650, 38989, 34856, 46550, 42322, 12017, 22696, 44445, 40444, 27214, 35765, 27070, 32746, 42145, 35019]}
```
