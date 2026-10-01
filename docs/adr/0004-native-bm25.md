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
