# item1-native-bm25

## Summary
Stage 2 (the Python BM25 rescorer) is the biggest cost in raggio's hybrid path, and a Rust/PyO3 rescorer that releases the GIL removes about 95% of it with exactly the same output.
- **Windows box (Xeon E-2226G, Python 3.12):** shipped `_bm25_rescore` takes about 85 ms per 500 candidates and about 280 ms per 1500. Most of that is tokenization (`_fold`'s per-character generator plus the regex); scoring is only about 12 ms.
- **Prototype (`bm25_topn`):** 3.4–3.7 ms per 500 including glue, about 10 ms per 1500 (23–30x faster).
- **Exactness:** scores are bit-identical, not just close. It copies CPython 3.12's Neumaier `sum()` and its term order.
- **Parity checks, on Windows and in a Linux x86_64 container:** 0 of 120 queries differ in id order or score bits; 0 of 200k random mixed-script strings differ; 0 differences on any code point Python 3.12's Unicode 15.0 tables assign. The only differences are 5,033 code points that are unassigned in Unicode 15.0.
- **Full text leg on a local 100k-row copy of the real schema:** p50 100 ms → 18 ms (Windows) and 81 ms → 12 ms (Linux container). Stage 2 is 85–89% of the leg.
- **Concurrency:** Python stage 2 does not speed up with threads (1.08x at 8 threads); native does (2.2x on Windows, 3.6x for the scorer alone in Linux).
- **DGX (inferred, not measured):** stage 2 is probably 45–55 ms of the 104 ms hybrid p50. The vector and text legs appear to run one after the other rather than overlapping: the flat-vs-IVF hybrid p50 gap (12.6 ms) matches the vector-only gap (11.8 ms). The p99 is mostly the stage-1a FTS5 pass, as ADR 0003 says, though stage 2 adds roughly 150 ms on those 1500-candidate queries.
- **New risk:** in a Linux container, reading SQLite results row by row hands the GIL back and forth about 3,000 times per query. Throughput collapses with 4–8 threads (text leg with native scorer: 112 → 14 q/s). Folding each SELECT into one JSON-aggregate row fixes it (108–132 q/s at 8 threads). This did not happen on native Windows, and on the DGX it is unmeasured. It gates how much concurrency item 1 can deliver, and `stage_probe.py` measures it.
- **Recommendation:**
  - **Package:** a separate `raggio-native` maturin package in `native/`, pulled in by an optional `native` extra via a uv path source. Tested with the local uv 0.5.24, including rebuild on `.rs`/`Cargo`/`pyproject` changes.
  - **Docker:** a two-stage Dockerfile that builds with Rust in the first stage only. Tested on x86_64: builds in 86 s, final image 297 MB, a 567 KB `.so`.
  - **Runtime:** the existing Python code stays as the fallback and the test oracle; `/healthz` reports which scorer is active, and `RAGGIO_NATIVE=0` turns native off.
- **DGX checks:** `bench/stage_probe.py` (drafted in scratch) plus an A/B/A run of `bench.py --engine raggio`. Hybrid text-hit@10 must stay exactly 0.984.

## Brief
# Item 1: native, GIL-free BM25 stage-2 rescorer and tokenizer. Research brief

All numbers are from the **Windows box** (Intel Xeon E-2226G, 6C/12T, CPython 3.12.8, Windows 11) unless labelled **Linux container**. The Linux container ran on the same box: podman machine (WSL2 VM, 6 vCPU), image based on `python3.12-bookworm-slim` (CPython 3.12.12), `--memory 4g`. Other agents' containers were sharing that VM at load average ~2.5. **Nothing here was measured on the DGX**; all DGX figures are inferences and are marked as such.

Scratch artefacts are in `D:\DEV\_scratch\probe-item1\`. Nothing in `D:\DEV\raggio` was modified (`git status` is clean).

**Local side effects:**
- I started the podman machine, which was stopped. It is now also being used by other agents' containers (`raggio-probe:*`), so I left it running.
- I removed my own images and volume.
- A `--platform linux/arm64` probe pull briefly re-pointed the `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` tag to arm64. I re-pulled amd64 (tag now amd64, id 08337aba…) and deleted the arm64 image.

---

## 1. The text path as it ships today (evidence)

**Constants and token pipeline in `src/raggio/store.py`:**
- L32 `HYBRID_DEPTH = 100`.
- L48 `TEXT_OR_CAND = 500`, L51 `TEXT_AND_CAND = 1000`, L52 `SDM_WEIGHT = 0.2`, L53 `BM25_K1, BM25_B = 1.2, 0.75`.
- L60-61 `FTS_SCAN_BUDGET = 0.02`, `FTS_SCAN_BUDGET_MIN_ROWS = 1000`.
- L167-172 `_rows_by_id`: chunks of 512 ids, iterated **one cursor row at a time**.
- L175-181 `_fold`: `"".join(c for c in NFKD(token.lower()) if not unicodedata.combining(c))`.
- L185 `_TOKEN_RE = re.compile(r"[^\W_]+")`; L188 `_fold_tokens = _TOKEN_RE.findall(_fold(text))`.

**Collection methods in `store.py`:**
- **L1233 `_df`:** a df cache capped at 65,536 entries, backed by `records_fts_v` (fts5vocab).
- **L1245 `_prune_common`:** uses a *different* tokenizer, `re.findall(r"\w+")[:100]`, which keeps `_`, plus a per-token `_fold`. It keeps tokens rarest-first within the budget.
- **L1281 `_avgdl`:** 256 random-id probes through `_fold_tokens`; the result is cached.
- **L1304-1343 `_bm25_rescore`:**
  - qtoks = `_fold_tokens(qtext)[:100]`.
  - idf per `dict.fromkeys(qtoks)` = `max(ln((N-df+.5)/(df+.5)), 1e-6)`.
  - Ordered distinct pairs `a != b`.
  - Per candidate: `_fold_tokens(text or "")`, `dl = len or 1`, `norm = k1*(1-b+b*dl/avgdl)`.
  - Unigram `sum()` over a tf dict built in first-occurrence order, plus `SDM_WEIGHT * sum()` over the bigram tf dict.
  - Sort key `(-s, rid)`, take top n.
- **L1345-1397 `_text_ids`:**
  - Two-stage only when pruning dropped a token. At 2.55M rows the budget is 51k postings, so that is almost every title.
  - Stage 1a is ranked OR LIMIT 500; stage 1b is unranked AND LIMIT 1000.
  - `cand_ids = dict.fromkeys(or+and)`, then the text fetch, then `_bm25_rescore`.
- **L1399-1426 `search`:** hybrid is `asyncio.gather(self._vector_ids(...), asyncio.to_thread(self._text_ids, ...))` at L1417.

**Docs:**
- ADR 0001 L18 says "Hybrid legs run concurrently … SQLite releases the GIL; legs overlap fully". That was measured before ADR 0003 added a GIL-holding Python stage 2.
- ADR 0001 L49 names free-threaded Python and more CPUs as the remaining headroom.
- ADR 0003 describes stage 2 as "~tens of ms at 2.5M rows" and blames the p99 tail on stage-1a rank passes for near-universal tokens.

**Benchmark results (`bench/results-arxiv.md`, DGX):**

| Metric | raggio (flat) | raggio-ivf |
|---|---|---|
| Vector p50 | 23.0 ms | 11.2 ms |
| Hybrid p50 / p95 / p99 | 104.0 / 172.2 / 588.3 ms | 91.4 / 159.0 / 592.6 ms |
| Hybrid QPS, serial | 8.6 | — |
| Hybrid QPS, concurrent (c=8) | 13.8 | 13.3 |
| Vector-only QPS, concurrent | 129 | — |
| Hybrid text-hit@10 | 0.984 | 0.984 |

- Weaviate's hybrid concurrent QPS is 169.
- `bench.py` queries are the titles of sampled ingested docs, seed 42. The concurrent phase repeats the serial queries, so caches are warm.

**`bench/text_probe.py`:**
- It is a standalone script with its own `pybm25`: per-token fold, `\w+` tokens, no SDM. It does not import raggio, so it **cannot measure the native path**.
- Its "median 85 ms" is for the whole pipeline on the DGX.
- Locally its `pybm25` takes 114 ms per 500 candidates, against 84 ms for shipped `_bm25_rescore`.

**Existing tests (`tests/test_store_optimizations.py`):**
- L26-32: `_fold` and `_fold_tokens` examples.
- L349-367 `test_two_stage_restores_full_query_ranking`.
- L370-400 `test_python_bm25_matches_fts5_ranking`, which monkeypatches `store.SDM_WEIGHT = 0.0`. So native must receive the constants **as call arguments read at call time**.
- L403-414 `test_unpruned_query_skips_stage_two`, which monkeypatches `col._bm25_rescore`.

## 2. Where stage-2 time goes

### Shipped `_bm25_rescore` (Windows box, synthetic arXiv-like docs)
About 1,250 characters and 213 words per doc (`corpus.py`).

**Totals:**
- 500 candidates: 84–111 ms, about 190 µs per candidate.
- 1500 candidates: 262–301 ms.
- With 100% non-ASCII docs: 105 ms and 315 ms.

**Breakdown at 500 candidates:**
- `_fold`: 49 ms. The per-character generator dominates; `lower()` is 0.65 ms and NFKD 1.2 ms of that.
- `findall`: 28 ms.
- Scoring loop: 12 ms.

So tokenization is about 85% of stage 2. numpy cannot help, and Python has a floor of about 40 µs per doc just to create ~210 `str` objects.

**Best pure-Python alternative** (exact parity): `lower()` → `isascii()` → `re.findall('[a-z0-9]+')`, falling back to the shipped path for non-ASCII. It cuts tokenization 79 → 23 ms per 500 on all-ASCII docs and 82 → 31 ms at 10% non-ASCII, with no gain on non-ASCII docs. The best case is about 35 ms per 500, and it still holds the GIL.

### Candidate text fetch
Synthetic 400k-row db with raggio's `records` schema, warm OS cache:
- Reused connection (which is what `_rdb` does; it is thread-local): 5.9 ms per 500 (12 µs per row), 15.2 ms per 1500.
- Fresh connection: 15.8 ms and 23.8 ms.
- On the DGX, the 4 GiB cgroup (which includes page cache) against a 16.5 GB db may make this colder. Unverified.

### Native prototype `bm25_topn`, including Python glue

| Case | 500 candidates | 1500 candidates |
|---|---|---|
| Time | 3.4–3.7 ms | ~10 ms |
| Per candidate | 6.5–7 µs (ASCII); 9–9.6 µs (all non-ASCII) | — |
| Speedup vs shipped | 23–30x | — |

Switching from SipHash to FxHash (`rustc-hash` 2) was worth about 1.3x (the SipHash version ran 4.6 / 16.5 ms).

### Whole text leg, raggio's real code (`stage_probe.py`)
Run on `fts_100000.db`, 100k synthetic rows built with the real `open_meta_db` schema (FTS5 unicode61 plus fts5vocab). 300 seed-7 titles, all two-stage; candidates p50 500, max 1498. Warm df cache, times in ms:

| Stage | Windows p50 / p99 | Linux container p50 / p99 |
|---|---|---|
| prune (df) | 0.1 / 0.2 | 0.1 / 0.2 |
| stage 1a (OR ranked) | 4.5 / 20.3 | 3.2 / 18.8 |
| stage 1b (AND) | 5.7 / 31.7 | 3.7 / 15.6 |
| fetch | 4.3 / 10.7 | 1.8 / 5.6 |
| **stage 2, Python** | **84.9 / 258** | **71.4 / 239** |
| **stage 2, native** | **3.5 / 10.5** | **3.3 / 12.5** |
| text leg, Python | 100.3 / 306 | 80.6 / 267 |
| text leg, native | 18.3 / 61 | 12.3 / 39.7 |

- Stage 2's share of the Python text leg (median) is 85% on Windows and 89% in the Linux container.
- Target hit@10 is identical for Python and native (0.973 on this synthetic corpus).
- PARITY: 0 id-order mismatches and 0 score bit-mismatches, on both platforms.
- The FTS stages here are at 100k rows; at 2.55M they are larger, so stage 2's share on the DGX will be lower.

### Thread scaling of the text leg (df cache warm, 300 queries)

| Threads | Windows, Python | Windows, native | Linux, Python | Linux, native |
|---|---|---|---|---|
| T=1 | 10.0 q/s | 52.7 q/s | 12.2 q/s | 73.3 q/s |
| T=2 | 1.06x | 1.73x | 0.97x | 1.52x (112 q/s) |
| T=4 | 1.10x | 2.15x | 0.68x | 0.97x |
| T=8 | 1.08x | 2.23x (118 q/s) | 0.48x | **0.20x (14 q/s)** |

The scorer on its own, with no SQL, scales on both platforms:
- Windows: 203 → 593 calls/s at T=1 → 4.
- Linux container: 311 → 1133 q/s at T=1 → 8 (3.64x).

### New finding: GIL handoffs on row-by-row SQLite reads (Linux container)
CPython's `sqlite3` releases and retakes the GIL around every `sqlite3_step`. Stages 1a and 1b plus the fetch step through about 3,000 rows per query.

In `thread_diag.py` (Linux), fetch-only throughput goes 615 → 402 → 59 → 16 q/s at T = 1/2/4/8.

`thread_diag2.py` / `stage_probe.py` fold each SELECT into a single row: `SELECT json_group_array(rowid) FROM (… LIMIT …)` and `SELECT json_group_object(id, text) FROM records WHERE id IN (…)`, then `json.loads`. Results were identical (0 of 150 mismatches). SQL plus native stage 2, T = 1/4/8:

| Variant | Linux run 1 | Linux run 2 | Windows |
|---|---|---|---|
| Per-row | 69 / 56 / 24 q/s | 56 / 68 / 32 q/s | 50 / 87 / 93 q/s |
| JSON aggregate | 55 / 124 / 108 q/s | 54 / 94 / 132 q/s | 47 / 75 / 120 q/s |

The collapse did not appear on native Windows. The VM and CPU contention probably amplify it, and **on bare-metal DGX it is unmeasured**. The vector leg's per-row fp16 `vecs` fetch in `_search_rescored` has the same pattern. This is outside item 1's scope as assigned ("DB access stays in Python"), but it decides how much concurrency item 1 can deliver. `stage_probe.py` measures it on the DGX (§8).

### Stage-2 share on the DGX (inference)
**Estimate of stage 2's absolute cost:**
- On the DGX, text_probe's whole-pipeline median is 85 ms. Its `pybm25` alone takes 114 ms per 500 candidates on the Windows box.
- So DGX single-thread Python is about 1.5–1.9x faster than the Windows box, depending on what stages 1a/1b/fetch cost at 2.55M rows (assumed 10–25 ms).
- Shipped stage 2 on the DGX is then about 44–55 ms at p50 (500 candidates) and about 140–190 ms at 1500 candidates. Native would be about 2–3 ms and about 6–7 ms.

**Share of hybrid p50 (104 ms):** about 45–55 ms, **the largest single cost**. Vector-only p50 is 23 ms.

**Legs appear to run one after the other, not in parallel:** hybrid p50 falls by 12.6 ms from flat to IVF, almost exactly the vector-only gap (11.8 ms). If the legs overlapped, hybrid ≈ max(text, vector) and the gap would be about 0. Likely cause: the GIL-holding stage 2 makes the vector leg's many short GIL re-acquisitions (event loop, scan-drain task, numpy glue) wait up to the 5 ms switch interval.

**Concurrent QPS:** at c=8, hybrid gets only 1.6x over serial (13.8 vs 8.6 q/s). A GIL-held Python stage 2 of about 50–60 ms per query caps the text leg at about 17–20 q/s, consistent with 13.8 after other GIL work.

**p99 (588 ms):** mostly stage 1a, as ADR 0003 says. Stage 2 adds about 140–190 ms on these ≤1500-candidate queries, so native could remove roughly 100–200 ms of the p99.

**Projection (unverified):**
- Hybrid p50 104 → roughly 50–65 ms.
- p99 588 → roughly 400–480 ms.
- Hybrid concurrent QPS: a material rise, with the size bounded by the per-row GIL handoffs above and by vector-only concurrent throughput (129 q/s).

## 3. Exact semantics the native code must reproduce, and every divergence risk

**Fold order:**
- Whole-string `str.lower()`, which is the full mapping including the Final_Sigma context rule.
- Then NFKD, then drop every char with canonical combining class ≠ 0. There are 922 such chars: 896 Mn and 26 Mc.
- Marks with ccc = 0 (1,528 of them, e.g. Devanagari virama-less vowel signs, many Mc) are **kept**, and act as separators because they are not word chars.
- Doing the decomposition char by char is exact, because canonical reordering only moves ccc ≠ 0 marks and those are all dropped. Checked: 0 of 200k random strings differ.
- Rust: `text.to_lowercase()`, which implements Final_Sigma (checked in the context `a?bΣ ?`), then `unicode_normalization::char::decompose_compatible`, then filter on `canonical_combining_class == 0`. Plus an ASCII fast path using byte `to_ascii_lowercase`.

**Word chars:**
- `[^\W_]` in Python str regex is exactly `str.isalnum()`, which is exactly General_Category ∈ {Lu, Ll, Lt, Lm, Lo, Nd, Nl, No}. Verified exhaustively on 3.12 / UCD 15.0.
- It is **not** Rust `char::is_alphanumeric`: Alphabetic includes Other_Alphabetic marks such as Indic vowel signs, and circled letters.
- It is **not** the regex crate's `\w`, which adds M* and Pc per UTS#18.
- The prototype uses `unicode-general-category` 1.1.0 with an ASCII fast path.

**Edge cases verified to match:**
- `'ß'` stays `'ß'`: `lower()`, not `casefold()`.
- `'𝐀𝐁'` → `'AB'`, **uppercase**, because lower runs before NFKD.
- `'İ'` → `'i'`; `'ﬁ'` → `'fi'`; `'Ⅷ'` → `'viii'`; `'½'` → `['1','2']`; `'µ'` → `'μ'`.
- `'हिन्दी'` → `['ह','नद']`; Hangul syllables decompose to jamo.
- `'_'` is a separator. NBSP and ZWJ are separators.

**Scoring:**
- `dl = len or 1`; `norm = k1*(1-b+b*dl/avgdl)`.
- The per-doc unigram terms are summed **in first-occurrence order of the tf dict** with **CPython ≥ 3.12 `sum()`, which is Neumaier-compensated**. The prototype's `PySum` copies it:
  - add: `t=s+x; if |s|>=|x| {c+=(s-t)+x} else {c+=(x-t)+s}; s=t`
  - result: `s+c` if `c != 0 && c.is_finite()`, else `s`.
- The bigram term is added the same way, then `score += sdm_weight * s2`.
- Sort by `(-score, rid)`. Rids are unique because they come from `dict.fromkeys`.
- The result is **bit-identical**, not merely close.

**Divergence risks, each with a handling decision:**
1. **Unicode version skew:**
   - Python 3.12 uses UCD 15.0.0. Rust std 1.98 and `unicode-normalization` 0.1.25 use 17.0.0; `unicode-general-category` 1.1.0 uses 16.0.0.
   - Exhaustive check: 5,033 code points diverge on their own and 5,079 in context. **All are unassigned (Cn) in Python's UCD**, e.g. scripts added in Unicode 16/17. Zero assigned code points diverge. Same result on Windows and in the Linux container.
   - FTS5 `unicode61` itself uses Unicode 6.1 tables, so newly encoded scripts are already inconsistent between stage 1 and stage 2.
   - A Python upgrade shrinks the gap: 3.13 uses UCD 15.1 and 3.14 uses 16.0. The test compares against whichever Python is running.
2. **Lone surrogates:** Python treats them as separators, but PyO3 str → UTF-8 extraction fails on them. Query text arrives as JSON and can contain them, so **query tokenization stays in Python** (`_fold_tokens`). Candidate texts come from SQLite and cannot contain lone surrogates: Python's sqlite3 refuses to encode them on insert and fails to decode invalid UTF-8 on fetch.
3. **Empty score type:** a doc with no matching term gets int `0` in Python but `0.0` in native. This is only a JSON `0` vs `0.0` difference in text mode, and rare because candidates matched FTS. Accept it.
4. **`avgdl == 0`:** Python raises ZeroDivisionError, and native raises the same type. This is a pre-existing edge case when every sampled doc has zero tokens.
5. **`df > total`:** `math.log` domain error. This is pre-existing, and idf stays in Python.
6. **NaN is impossible:** idf ≥ 1e-6 and norm > 0. Even so, replace `partial_cmp().unwrap()` with `.unwrap_or(Equal)` so a panic can never happen. -0.0 cannot occur.
7. **Other arguments:** `n < 0` gives OverflowError in native, where Python would slice. n is always ≥ 1 because it is `max(HYBRID_DEPTH, k)`. `n = 0` and `n > len` are both handled.
8. **Missing idf:** native raises ValueError if the idf dict is missing a qtok. It cannot happen given how the caller builds idf.
9. **aarch64 floating point:** Rust never fuses into FMA implicitly. CPython's `sum` has no multiply, and each Python float op is a separate C call, so bit parity should hold on aarch64. **Unverified**; the DGX probe checks it.
10. **A future CPython `sum()` change** would break bit parity. The parity test catches it.
11. `_prune_common`'s `\w+` tokenizer and the 100-token cap are untouched; they stay in Python.

## 4. Options

| Option | Verdict | Why |
|---|---|---|
| **(a) Rust + PyO3 0.29 + maturin** | **Recommended; prototype works** | Maintained Unicode tables. `py.detach` releases the GIL. `#[pymodule(gil_used = false)]` supports free-threaded builds. Same toolchain as turbovec, which already uses `py.detach` (turbovec-python lib.rs L363/528/639). Parity is bit-exact and verified. |
| (b) C extension | Reject | No Unicode tables without ICU or utf8proc, each with its own Unicode version. Needs a compiler in the image anyway. More unsafe code. |
| (c) Cython | Reject | The work is Unicode string processing that needs the GIL (`str` methods); a nogil core would need its own tables, which is the same problem as (b). Also needs gcc in the image. |
| (d) numpy | Reject | Tokenization is 85% of the cost, and numpy cannot tokenize. |
| (e) Precompute tokens at ingest | Reject for now | Several extra GB, or a change to FTS5 position semantics. Needs a migration and adds ingest cost. fts5vocab `instance` walks whole posting lists. Native is already about 7 µs per candidate. |
| (f) LRU of folded tokens | Reject | About 12 KB of Python objects per record. Low hit rate on real traffic. It would flatter the benchmark, whose concurrent phase repeats queries. Must be invalidated on upsert, patch and delete. |
| (g) Pure-Python ASCII fast path in the fallback | Optional, separate | Exact, and 2.5–3.5x faster on ASCII corpora, but still holds the GIL. Keep it out of item 1 so the fallback stays the unchanged reference; coordinate with the other items. |

**PyO3 facts (0.29.2):**
- `Python::detach` is the new name for `allow_threads`; its closure must be `Ungil`/`Send`.
- Modules default to `gil_used = false` since 0.28; set it explicitly anyway.
- `PyBackedStr` can move into `detach`; confirmed by compiling.
- The **`extension-module` feature is deprecated**. Drop it and require `maturin >= 1.9.4` (current is 1.15.0). Per pyo3.rs/main: "The extension-module feature is deprecated and users should upgrade to maturin version 1.9.4 or higher". Checked: builds and imports fine without it.
- Use **no abi3**. The image builds from source for its exact interpreter, which keeps a cp314t free-threaded build possible; abi3 wheels are not loadable on free-threaded builds.

## 5. Recommended design

### Repository layout (new files)
```
native/
  Cargo.toml       # [package] raggio-native, edition 2021, rust-version 1.83, publish=false
                   # [lib] name="raggio_native", crate-type=["cdylib"]
                   # deps: pyo3 = "0.29.2" (NO extension-module feature),
                   #       unicode-normalization = "0.1.25", unicode-general-category = "1.1.0", rustc-hash = "2"
                   # [profile.release] lto = true, codegen-units = 1
  Cargo.lock       # committed (maturin locked = true)
  pyproject.toml   # see below
  src/lib.rs       # port of D:\DEV\_scratch\probe-item1\pkgtest\native\src\lib.rs (237 lines)
```

`native/pyproject.toml`, tested:
```toml
[build-system]
requires = ["maturin>=1.9.4,<2.0"]
build-backend = "maturin"

[project]
name = "raggio-native"
version = "0.1.0"
requires-python = ">=3.12"

[tool.maturin]
module-name = "raggio_native"
locked = true
strip = true

[tool.uv]
cache-keys = [{ file = "pyproject.toml" }, { file = "Cargo.toml" }, { file = "Cargo.lock" }, { file = "src/**/*.rs" }]
```
- `locked` and `strip` are documented `[tool.maturin]` keys and were accepted by maturin 1.15.
- Setting `cache-keys` replaces uv's defaults, so list `pyproject.toml` explicitly.
- Do not add a `readme` to `native/pyproject.toml`: `.dockerignore` excludes `*.md`.

**Why a top-level `raggio_native` and not `raggio._native`:** raggio is installed editable from `src/`, so an extension placed in `site-packages/raggio/` would never be found. A separate distribution also keeps pure-Python installs possible.

**Root `pyproject.toml` additions**; hatchling stays as the build backend:
```toml
[project.optional-dependencies]
native = ["raggio-native"]

[tool.uv.sources]
raggio-native = { path = "native" }
```

**`uv.lock` diff:** about 10 lines, verified. It adds `[package.optional-dependencies] native`, a `requires-dist` entry `{ name = "raggio-native", marker = "extra == 'native'", directory = "native" }`, and a `raggio-native 0.1.0` package with `source = { directory = "native" }`. uv locks it from static metadata without building (1.4 s).

**Caveat:** re-locking with the local uv 0.5.24 also rewrites the whole file: it drops `revision = 2`, all 715 `upload-time` fields and 4 graalpy wheels. Lock with the newer uv that produced `revision = 2`. uv 0.5.24 can still *read* the current lock (`uv sync --frozen` and `uv lock --check` both pass).

**Ignore files:**
- `.gitignore`: add `native/target/`. maturin creates `native/target` inside the source tree when uv builds it (observed).
- `.dockerignore`: add `native/target`.

### uv behaviour, verified with uv 0.5.24 on Windows (`pkgtest/`)
- `uv sync --frozen --extra native` builds the path dependency through maturin in about 21 s on first build. The local MSVC 14.44 and Windows SDK are found automatically, even though they are not on PATH.
- Re-running with no change: "Audited" in 0.2 s.
- After `touch native/src/lib.rs`, or an edit to `native/pyproject.toml`: rebuilds in about 13 s.
- **Plain `uv sync` (exact sync) *uninstalls* `raggio-native`.** Developers need `uv sync --extra native`. `uv run` is inexact and leaves it installed (verified).
- The alternative is a `native` dependency group listed in `default-groups` (supported since uv 0.4.27). But then every plain `uv sync` needs Rust.

### Python-facing API
Native:
- `raggio_native.bm25_topn(qtoks: list[str], idf: dict[str, float], rids: list[int], texts: list[str | None], avgdl: float, n: int, k1: float, b: float, sdm_weight: float) -> tuple[list[int], list[float]]`.
  - Validates that `len(rids) == len(texts)` (ValueError otherwise).
  - Returns `([], [])` on empty input; raises ZeroDivisionError if `avgdl == 0`.
  - All work runs inside `py.detach`.
- `raggio_native.fold_tokens(text: str) -> list[str]`: the same tokenizer `bm25_topn` uses. Exported for the parity tests only; production keeps using the Python `_fold_tokens` for query text because of lone surrogates.
- `raggio_native.unicode_versions() -> tuple[str, str, str]`: std, normalization and gencat Unicode versions.

**Inside `bm25_topn` (prototype):**
- A term index built in first-occurrence order of qtoks (`FxHashMap<&str, u32>`), and a dense nt×nt `is_pair` table (nt ≤ 100).
- Per doc: a tf array with first-occurrence `order`, an FxHashMap for bigram tf with `order2`, and dl.
- `PySum` for both the unigram and bigram sums.
- `select_nth_unstable_by(n)` then sort by `(-score, rid)`.

**Integration in `store.py`**; DB access, df, avgdl and idf all stay in Python:
```python
try:  # optional native stage-2 scorer (native/ -> raggio_native); pure Python otherwise
    if os.environ.get("RAGGIO_NATIVE", "1") == "0":
        raise ImportError("disabled by RAGGIO_NATIVE=0")
    import raggio_native as _native
except ImportError:
    _native = None
```
In `_bm25_rescore`, after the existing `qtoks` / empty guard / `idf` computation, call `avgdl = self._avgdl()` and then:
```python
if _native is not None:
    return _native.bm25_topn(qtoks, idf, [r for r, _ in cand], [t for _, t in cand],
                             avgdl, n, BM25_K1, BM25_B, SDM_WEIGHT)
```
Then the unchanged Python loop, with the `pairs` computation moved below. Read the constants at call time so the tests' `monkeypatch(store, "SDM_WEIGHT", 0.0)` still works.

`/healthz` gains `"text_scorer": "native" if store._native else "python"`, so the DGX run can confirm which scorer is active.

### Docker: two-stage build
Verified on x86_64 in local podman:
- Cold build 86 s.
- Final image 297 MB; the discarded build stage is 1.37 GB.
- `raggio_native.cpython-312-x86_64-linux-gnu.so` is 567 KB stripped.
- `import raggio_native, raggio.store` works as user `app`.
- Full `parity.py` passes inside the image.

```dockerfile
# ---- build stage: needs a C linker + Rust for the native BM25 extension ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ARG RUST_TOOLCHAIN=1.98.1
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain ${RUST_TOOLCHAIN}
ENV PATH="/root/.cargo/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY native ./native
RUN uv sync --frozen --no-install-project --no-dev --extra native
COPY src ./src
RUN uv sync --frozen --no-dev --extra native

# ---- runtime stage: no compiler, no rustup ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
RUN useradd -m app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
```
- The layer order keeps the rustup install and third-party dependencies cached; only a change under `native/` rebuilds the extension.
- The first `uv sync` runs fine with `native/` absent.
- The final stage does not carry uv's download cache (`/root/.cache/uv`), which the current single-stage image keeps in its layers. So the image should be smaller; the baseline was not measured.
- Debian's `rustc` is 1.63, too old, hence rustup.
- `--extra native` in the image means a Rust build failure fails the image build. At runtime the Python fallback plus `/healthz` plus `RAGGIO_NATIVE=0` are the safety valves.
- aarch64 (DGX) was **not built locally**: no arm64 emulation in the podman machine ("Exec format error"). rustup and bookworm gcc both support aarch64 Linux.
- Pre-built wheels (PyO3/maturin-action with manylinux_2_28 on x86_64 and on aarch64 via ubuntu-24.04-arm, like turbovec's `release-pypi.yml`) are only needed if `raggio-native` is ever published to PyPI. They are future work.

**Local Windows development:** rustup stable (≥ 1.83) plus the MSVC Build Tools, which are already installed. Then `uv sync --frozen --extra native`, and `uv run --frozen --extra native pytest -q`.

## 6. Ordered TDD task list

### Task 1: `bench/stage_probe.py` and the DGX baseline gate
**File:** copy `D:\DEV\_scratch\probe-item1\stage_probe.py` to `bench/stage_probe.py`.
- It binds `Collection._df`, `_prune_common`, `_avgdl`, `_bm25_rescore` and `_text_ids` onto a small `Probe` class with a thread-local read-only `_rdb`.
- It uses the seed-7 title sample stream from `text_probe.py`.
- It reports prune, 1a, 1b, fetch, stage 2 Python, stage 2 native, the leg totals, the stage-2 share, target hit@10, parity, text-leg thread scaling (Python and native), and the per-row vs JSON-aggregate thread test.
- Before integration it calls `raggio_native.bm25_topn` through the same glue; after integration it toggles the planned `store._native`.

**Tests:** none (it is a bench script, and `bench/` has no tests).

**Acceptance:**
- Runs locally against any `meta.db`; it already ran against `fts_100000.db`, built by the scratch `build_ftsdb.py`.
- Runs on the DGX against the current image (prints `native=NO`).
- Gate: continue if stage-2 Python p50 is at least about 25 ms and at least about 40% of the text leg (expected).

### Task 2: native crate skeleton with `fold_tokens`, plus packaging wiring
**Files:**
- `native/Cargo.toml`, `native/Cargo.lock`, `native/pyproject.toml`, `native/src/lib.rs` (`fold_chars`, `is_word`, `for_each_token`, `fold_tokens`, `unicode_versions`, and `#[pymodule(gil_used = false)] fn raggio_native`).
- Root `pyproject.toml` (extra plus source), `uv.lock`, `.gitignore`, `.dockerignore`.

**Tests first:** new `tests/test_native.py` starting with `nat = pytest.importorskip("raggio_native")`.
- `test_fold_tokens_examples` checks `nat.fold_tokens(s) == store._fold_tokens(s)` for: `"Café_Bar x²"`, `"İstanbul"`, `"ﬁnite ﬂow"`, `"Ⅷ ½ µm Å K"`, `"ΣΟΦΙΑΣ σοφίας ΑΣ."`, `"Straße"`, `"𝐀𝐁𝟏"`, `"हिन्दी 한국어 量子力学"`, `"a\u0301b"`, `"x_1 naïve_bayes"`, `"\u00a0\u200d\t\n"`, `""`.
- `test_unicode_versions_reported`.

**Commands:** `uv lock`, then `uv sync --frozen --extra native`, then `uv run --frozen --extra native pytest -q tests/test_native.py`.

**Acceptance:**
- The tests pass.
- Plain `uv run --frozen pytest -q` (no extra) passes, with the native tests skipped.

### Task 3: exhaustive Unicode parity tests
**File:** `tests/test_native.py`.
- `test_fold_tokens_parity_all_assigned_codepoints`: for every cp in `range(0x110000)`, excluding surrogates and code points with `unicodedata.category(chr(cp)) == "Cn"`, compare native vs Python on `chr(cp)` alone and in the context `f"a{c}bΣ {c}"`. That is about 290k code points, a few seconds.
- `test_fold_tokens_random_mixed_scripts`: seeded, about 20k strings from pools of ASCII, Latin accents and Greek including Σ, U+0300–036F, Devanagari and Thai, Hangul and CJK, and ligature/compatibility/dash/ZW characters. Copy the pools from scratch `parity.py`.

**Acceptance:** zero divergences on assigned code points, matching the scratch result.

### Task 4: `bm25_topn` in Rust
**File:** `native/src/lib.rs` (`PySum`, `bm25_topn`; use `partial_cmp(...).unwrap_or(Ordering::Equal)`).

**Tests first:** `tests/test_native.py`, using a fake self: `SimpleNamespace(indexed_counts={"chunk": N}, _df=..., _avgdl=lambda: 205.3)`, with the reference `Collection._bm25_rescore(fake, q, cand, n)`.
- `test_bm25_topn_bit_identical_random`: a seeded synthetic corpus of 1500 docs generated inside the test from a fixed word list plus accented words; 3 corpora × 40 queries. Assert `ids == ref_ids` and scores are bit-equal via `==` on floats.
- The candidate list includes `None`, `""` and an exact duplicate text so a tie is broken by rid.
- Also covered: single-token queries (no pairs), repeated query tokens, df = 0 tokens (the idf clamp), `n = 0`, `n > len(cand)`, `avgdl = 0` raising ZeroDivisionError in both paths, and a length mismatch raising ValueError.

**Acceptance:** 0 id-order mismatches and 0 bit mismatches, matching the scratch result of 0 of 120.

### Task 5: integrate into `store.py`
**File:** `src/raggio/store.py`: the `_native` import with the `RAGGIO_NATIVE` switch, and the dispatch in `_bm25_rescore` shown in §5.

**Tests first**, in `tests/test_store_optimizations.py`:
- A fixture:
  ```python
  @pytest.fixture(params=["python", "native"])
  def bm25_backend(request, monkeypatch):
      if request.param == "native":
          monkeypatch.setattr(store, "_native", pytest.importorskip("raggio_native"))
      else:
          monkeypatch.setattr(store, "_native", None)
  ```
- Parametrize `test_two_stage_restores_full_query_ranking` and `test_python_bm25_matches_fts5_ranking` with it.
- New `test_rescore_backends_identical`: a real Collection with about 50 ingested docs; for several queries, compare `_bm25_rescore` under both backends with exact equality.
- New `test_native_disabled_by_env`: a subprocess runs `python -c "import raggio.store as s; print(s._native)"` with `RAGGIO_NATIVE=0` and expects `None`.

**Command:** `uv run --frozen --extra native pytest -q`, and plain `uv run --frozen pytest -q`.

**Acceptance:** the whole suite passes in both environments.

### Task 6: `/healthz` reports the scorer
**Files:** `src/raggio/app.py`, `tests/test_api.py`.

**Test:** `text_scorer` is `"native"` when `store._native` is set, else `"python"`.

**Acceptance:** tests pass.

### Task 7: two-stage Dockerfile
**Files:** `Dockerfile` (as in §5), `.dockerignore`.

**Verification** (podman, locally or on the DGX):
- `podman build -t raggio .`
- `podman run --rm raggio python -c "import raggio_native, sys; print(raggio_native.unicode_versions())"`
- `curl -s localhost:18000/healthz` shows `"text_scorer":"native"`.

**Acceptance:** builds on aarch64 (DGX); the final image has no `cargo` or `rustc` (`podman run --rm raggio sh -c 'command -v cargo || echo none'`).

### Task 8: documentation
- New ADR `docs/adr/0004-native-bm25-stage2.md`: the decision, the options table, the bit-parity contract, the Unicode-skew policy, packaging, and the fallback and switch.
- Amend ADR 0001 L18 ("legs overlap fully" no longer holds while stage 2 holds the GIL) and ADR 0003's "~tens of ms" note.
- `docs/getting-started.md` L113-114 and `README.md` L187-188: Rust is optional; use `uv sync --extra native`; plain `uv sync` removes the extension.

### Task 9: DGX A/B/A measurement and results update (protocol in §8)
Update the hybrid rows and provenance note in `bench/results-arxiv.md`.

### Task 10 (conditional; recommend a separate item)
Only if the DGX probe shows per-row GIL handoffs hurting throughput: in `_text_ids`, fold the stage 1a/1b id lists and the `_rows_by_id` text fetch into single JSON-aggregate rows. The two-stage path's candidate order does not affect results, because stage 2 sorts by `(-score, rid)`. Keep per-row reads for the single-stage path (≤100 rows, order matters) and consider the vector leg's `vecs` fetch.

**Tests:** results identical to the current path for plain and filtered scopes.

## 7. Risks
- **Payoff:** the concurrency gain may be muted on the DGX by per-row GIL handoffs (§2). Measure first (Task 1), and handle it separately (Task 10).
- **Build dependencies:** the Docker build gains internet dependencies (rustup, crates.io) and 1–3 minutes of build time. Pin the toolchain with `RUST_TOOLCHAIN`; `Cargo.lock` is locked. aarch64 is not verified locally.
- **Unicode:** skew on code points unassigned in Python's UCD (accepted and test-enforced). A Python upgrade changes the oracle, and the tests re-validate.
- **Exact sync:** plain `uv sync` silently removes the extension, which is a developer-experience trap. `/healthz` and the docs mitigate it.
- **Lock churn** if re-locked with uv 0.5.24.
- **Bit-parity dependence** on CPython's `sum()` algorithm; the tests catch any change.
- **No test CI exists** (only `docs.yml`), so the parity tests run only where someone runs them.

## 8. DGX measurement protocol (run by the maintainer; agents have no DGX access)
**Step 0.** `git pull`, which brings `bench/stage_probe.py`.

**Baseline probe (current image):**
```
podman stop bench-tv
podman run --rm --memory 4g -v bench-tv:/data -v "$PWD/bench:/bench:ro" raggio \
  python /bench/stage_probe.py /data/collections/bench/meta.db --queries 300 --threads 1,2,4,8 | tee bench/stage-probe-before.log
podman start bench-tv
```
- Stop `bench-tv` so it does not share the cgroup.
- The volume is mounted read-write; the probe opens the db with `mode=ro` and `query_only`.
- `bench/` is dockerignored, so it is mounted rather than baked in.
- Record the pass-1 (cold) and pass-2 per-stage p50/p99, the stage-2 share, and the thread-scaling and per-row-vs-aggregate tables.

**After Tasks 2–7:**
1. `podman build -t raggio .`
2. Re-run the probe, teeing to `bench/stage-probe-after.log`.

**Required probe results:**
- `PARITY … id-order mismatches 0, score bit-mismatches 0` in both passes (this confirms aarch64 bit parity).
- hit@10 for native equals Python.
- Stage-2 native p50 ≤ 5 ms, and at least 10x faster than stage-2 Python.

**Bench A/B/A** (same image, same volume, the ingest reused through fingerprints):
```
podman rm -f bench-tv
podman run -d --name bench-tv --memory 4g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench -e RAGGIO_NATIVE=0 raggio   # A = python
curl -s localhost:18000/healthz            # expect "text_scorer":"python"
rm -f bench/results-partial.json
nohup env PYTHONUNBUFFERED=1 uv run python bench/bench.py --engine raggio --limit <same N as results-arxiv.md> \
  --out bench/results-arxiv-A-python.md --caps-note "raggio 4 GiB" > bench/ab-A.log 2>&1
```
- **B:** repeat without `-e RAGGIO_NATIVE=0`, confirming `"text_scorer":"native"`, with `--out bench/results-arxiv-B-native.md`.
- **A′:** repeat A to check for page-cache and thermal drift.
- Optionally repeat with `--engine raggio-ivf`.
- The `bench.py` client on the host needs no Rust: plain `uv run` does not install the extra.

**Acceptance criteria:**
- Hybrid text-hit@10 = **0.984 exactly** in A and B. Rankings are bit-identical, so any change is a bug.
- Hybrid p50: B ≤ 0.75 × A (projected about 50–65 ms).
- Hybrid p95: B < A. Hybrid p99: B ≤ A (projected −100 to −200 ms).
- Hybrid QPS concurrent: B ≥ 1.5 × A (from 13.8).
- Vector-only, filtered, and ingest-free rows within ±10% of A.
- A′ within ±10% of A.
- If B's concurrent QPS gain is small and the probe's per-row test collapses at 4–8 threads while JSON-aggregate scales, open Task 10.

## 9. Scratch artefacts (for the implementer)
All in `D:\DEV\_scratch\probe-item1\`:
- `pkgtest\native\src\lib.rs`: the recommended crate. Also `pkgtest\native\Cargo.toml`, `pkgtest\native\pyproject.toml`, `pkgtest\pyproject.toml`, `pkgtest\uv.lock` (written by 0.5.24) and `pkgtest\Dockerfile`.
- `native\`: the first prototype, laid out as `raggio_native._native`.
- `stage_probe.py`, `build_ftsdb.py`.
- Parity and timing scripts: `parity.py`, `perf_native.py`, `probe_stage2.py`, `probe_pyvariants.py`, `probe_fetch.py`, `semantics.py`, `calib.py`, `glue.py`, `thread_diag.py`, `thread_diag2.py`, `thread_scale.py`.
- Supporting files: `corpus.py` and the local dbs `fts_100000.db` and `fetch_400000.db`.

## User decisions
- Packaging: separate `raggio-native` maturin distribution in native/, pulled in by an optional extra `native` plus a `[tool.uv.sources]` path dependency. Recommended. Alternatives: a `native` dependency group listed in `default-groups`, so every plain `uv sync` builds it and every contributor needs Rust; or converting raggio itself to a maturin backend, which drops pure-Python installs and conflicts with the editable src/ layout.
- Docker policy: build the native extension as required in the image, so a Rust build failure fails the build, while keeping the runtime Python fallback, `/healthz` `text_scorer` and the `RAGGIO_NATIVE=0` switch. Recommended. The alternative is a best-effort build that falls back silently.
- Unicode-version skew: accept divergence only on code points unassigned in the running Python's UCD, enforced by the exhaustive test. Recommended; FTS5 unicode61 is on Unicode 6.1 anyway. The alternative is generating exact tables from Python's unicodedata into the crate.
- Re-lock uv.lock with the newer uv that produced `revision = 2`. Recommended. Re-locking with the local uv 0.5.24 rewrites about 720 lines (drops revision and upload-time fields, and the graalpy wheels).
- Scope of the GIL-handoff fix (JSON-aggregate SQL for stage-1 ids and the text fetch, and possibly the vector leg's vecs fetch): make it a separate follow-up gated by the DGX stage_probe result. Recommended, rather than folding it into item 1.
- Pure-Python ASCII fast path in the fallback: leave it out of item 1 so the fallback stays the unchanged reference oracle. Recommended; revisit if Python-only deployments matter.
- Add a test CI workflow running pytest with and without `--extra native` on ubuntu x86_64 and ubuntu-24.04-arm. Recommended, since only docs.yml exists today; this is the maintainer's call.
- DGX acceptance thresholds: hybrid text-hit@10 exactly 0.984; hybrid p50 at least 25% lower; p95 lower and p99 not worse; hybrid concurrent QPS at least 1.5x; other rows within ±10%; A′ within ±10% of A. Recommended as proposed.
- Documentation: a new ADR 0004 plus amendments to ADR 0001 L18 ('legs overlap fully') and ADR 0003's stage-2 cost note. Recommended.

## Unverified
- DGX single-thread Python speed relative to the Windows Xeon E-2226G, estimated at 1.5-1.9x, and therefore stage-2 absolute cost on the DGX, estimated at 44-55 ms p50 for 500 candidates and 140-190 ms for 1500.
- Stage 2's share of DGX hybrid p50 (104 ms) and p99 (588 ms), and the projected post-native hybrid p50 (about 50-65 ms), p99 (about 400-480 ms) and concurrent QPS gain.
- The inference that the hybrid legs are effectively serialized on the DGX (flat-vs-IVF hybrid p50 gap of 12.6 ms against a vector-only gap of 11.8 ms), and that GIL-held stage 2 is the cause.
- How badly per-row sqlite3 cursor iteration collapses throughput on bare-metal DGX Linux. It was seen only in a WSL2 podman VM shared with other agents' workloads (load about 2.5 on 6 vCPU), not on native Windows.
- Bit-exact native vs Python parity on aarch64. Verified on Windows x86_64 and in the Linux x86_64 container; expected to hold because Rust does no implicit FMA and CPython's sum() has no multiply.
- The Docker build on linux/aarch64 (rustup toolchain, bookworm gcc). Only x86_64 was built locally; no arm64 emulation was available.
- Cold page-cache cost of the candidate text fetch under the DGX container's 4 GiB cgroup cap against the 16.5 GB meta.db.
- Which uv version wrote the current uv.lock with `revision = 2`, and the current single-stage image's size (the claim that the two-stage image is smaller rests on the uv cache no longer being baked in).
- That SQLite's JSON1 aggregates are available in the DGX image's libsqlite3. Worked in the local bookworm-based image (Debian 3.40.x); only relevant to the conditional Task 10.
