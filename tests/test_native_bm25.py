"""Plan E: the prune tokenizer (spec D14), and raggio_native (native/, ADR 0004) against
the pure-Python reference in raggio.store.

The extension is optional: without it (a plain `uv sync`) the native tests skip and the
Python-reference tests still run. The CI native job sets REQUIRE_NATIVE=1, which turns a
missing extension into a collection error instead of skips.
"""

import asyncio
import json
import math
import os
import random
import re
import sys
import types
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from raggio import store
from raggio.app import create_app
from raggio.config import Settings
from raggio.store import Collection, CollectionConfig, CollectionManager

try:
    import raggio_native as native
except ImportError:
    if os.environ.get("REQUIRE_NATIVE") == "1":
        raise
    native = None

needs_native = pytest.mark.skipif(
    native is None, reason="raggio_native is not built (uv sync --extra native)"
)

# ---- prune tokenizer (D14): the ranked OR sees the tokens FTS5 indexed ------------------

# the underscore-title pattern of the bench corpus (p2): the old `\w+` prune kept '_2',
# '_x' and '_' with df 0, FTS5 unicode61 read them as '2', 'x' and nothing, and the ranked
# OR matched 347k-738k rows against a 51k budget
TITLE = "Superconductivity in MgB$_2$ and Sr$_x$Ba$_{1-x}$ films _ a DFT study"


def _prune_collection(path, texts, tokenizer="unicode61"):
    col = Collection(CollectionConfig("t", 8, 4, None, None, None, tokenizer=tokenizer),
                     Path(path), lambda: None)
    rng = random.Random(0)
    asyncio.run(col._process_job({"documents": [
        {"doc_id": f"d{i}", "chunks": [
            {"id": cid, "text": text, "vector": [rng.uniform(-1, 1) for _ in range(8)]}]}
        for i, (cid, text) in enumerate(texts.items())
    ]}))
    col._df_cache_churn = 0  # the ingest counted as churn; start from a clean cache
    return col


def test_prune_splits_underscore_titles_like_stage_2_and_fts5(tmp_path, monkeypatch):
    texts = {"title": TITLE}
    texts.update({f"f{i}": f"filler{i} 2 x 1 a in and" for i in range(40)})
    col = _prune_collection(tmp_path, texts)
    monkeypatch.setattr(store, "FTS_SCAN_BUDGET_MIN_ROWS", 10)
    kept, toks = col._prune_common(TITLE)
    assert toks == store._fold_tokens(TITLE)[:100]  # the stage-2 tokens
    assert not any("_" in t for t in kept + toks)
    db = col._rdb()
    vocab = {r[0] for r in db.execute("SELECT term FROM records_fts_v")}
    assert not any("_" in t for t in vocab)  # FTS5 unicode61 splits on '_' as well
    assert set(kept) <= vocab  # no df-0 phantom survives: every kept token is indexed
    assert kept == ["superconductivity", "mgb", "sr", "ba", "films", "dft", "study"]
    budget = max(store.FTS_SCAN_BUDGET_MIN_ROWS,
                 int(store.FTS_SCAN_BUDGET * sum(col.indexed_counts.values())))
    matched = db.execute("SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH ?",
                         (store._or_query(kept),)).fetchone()[0]
    assert matched <= budget
    hits = asyncio.run(col.search("text", None, TITLE, 3, "chunks", None, None))
    assert hits[0]["id"] == "title"


def test_prune_of_separators_only_is_empty(tmp_path):
    col = _prune_collection(tmp_path, {"c1": "a b"})
    assert col._prune_common("_ __ ___ $_{}$") == ([], [])


def test_prune_keys_df_by_the_fts5_term(tmp_path, monkeypatch):
    # FTS5 lowercases the query's terms, but _fold_tokens leaves math alphanumerics as
    # ASCII capitals ('𝐀' folds to 'A'): keyed as is, 'AB' has df 0 and survives the
    # prune, and the ranked OR matches every 'ab' row. The key is _fold of the token.
    texts = {f"c{i}": f"ab filler{i}" for i in range(40)}
    texts["r"] = "x 1 rare"
    col = _prune_collection(tmp_path, texts)
    monkeypatch.setattr(store, "FTS_SCAN_BUDGET_MIN_ROWS", 10)
    col._df_cache.clear()
    kept, toks = col._prune_common("x_1 𝐀𝐁 rare")
    assert toks == ["x", "1", "AB", "rare"]  # the stage-2 tokens
    assert set(col._df_cache) == {"x", "1", "ab", "rare"}
    assert kept == ["x", "1", "rare"]
    matched = col._rdb().execute("SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH ?",
                                 (store._or_query(kept),)).fetchone()[0]
    assert matched == 1


def test_trigram_prune_keeps_word_tokens(tmp_path):
    # trigram matches substrings, where '_' and diacritics are significant: the D14
    # split applies to unicode61 collections only
    col = _prune_collection(tmp_path, {"c1": "get_user_id café"}, tokenizer="trigram")
    assert col._prune_common("get_user_id café") == (
        ["get_user_id", "café"], ["get_user_id", "café"])


# ---- tokenizer: native fold_tokens against the reference --------------------------------

GOLDEN = [
    ("Café_Bar x²", ["cafe", "bar", "x2"]),  # '_' separates, NFKD folds compatibility forms
    ("İstanbul", ["istanbul"]),  # lower() gives i + U+0307; the combining dot is dropped
    ("ﬁnite ﬂow", ["finite", "flow"]),
    ("Ⅷ ½ µm Å K", ["viii", "1", "2", "μm", "a", "k"]),
    # Final_Sigma: a word-final capital sigma lowers to ς, also across the case-ignorable
    # U+0345; a lone Σ has no cased letter before it and stays σ
    ("ΣΟΦΙΑΣ σοφίας ΑΣ. ΑΣ\u0345 Σ", ["σοφιας", "σοφιας", "ας", "ας", "σ"]),
    ("ΑΣ'Β ΑΣ'", ["ασ", "β", "ας"]),  # the apostrophe is case-ignorable: Β after it is cased
    ("Ἀθῆναι ὈΔΥΣΣΕΥΣ", ["αθηναι", "οδυσσευς"]),
    ("Straße", ["straße"]),  # lower() keeps ß (casefold would not)
    ("𝐀𝐁𝟏", ["AB1"]),  # NFKD runs after lower(): math bold folds to UPPERCASE ASCII
    # spacing vowel signs (Mc, combining class 0) survive the fold but are not
    # alphanumeric, so they split words; the virama (combining class 9) is dropped
    ("ह\u093fन\u094dद\u0940", ["ह", "नद"]),  # "Hindi"
    ("한국어 量子力学", ["한국어", "量子力学"]),  # Hangul -> jamo
    ("a\u0301b", ["ab"]),
    ("x_1 naïve_bayes 42", ["x", "1", "naive", "bayes", "42"]),
    ("٣٤ ² ①", ["٣٤", "2", "1"]),
    ("ǅungla ﬀ", ["dzungla", "ff"]),
    ("\u00a0\u200d\t\n", []),
    ("", []),
]


@pytest.mark.parametrize(("text", "tokens"), GOLDEN)
def test_python_fold_tokens_goldens(text, tokens):
    assert store._fold_tokens(text) == tokens  # the reference the native tokenizer copies


@needs_native
@pytest.mark.parametrize(("text", "tokens"), GOLDEN)
def test_native_fold_tokens_goldens(text, tokens):
    assert native.fold_tokens(text) == tokens


@needs_native
def test_unicode_data_matches_the_interpreter():
    # the tables are generated at build time from the build interpreter's unicodedata;
    # store refuses the extension when they disagree with the running interpreter's
    assert native.UNIDATA_VERSION == unicodedata.unidata_version


def _first_divergence(chars, make):
    for c in chars:
        s = make(c)
        if native.fold_tokens(s) != store._fold_tokens(s):
            return f"U+{ord(c):04X} {unicodedata.name(c, '?')}: {s!r}"
    return None


@needs_native
def test_fold_tokens_matches_python_on_every_code_point():
    # every code point alone and in the four Final_Sigma positions: after a space
    # (Cased lookup), between a cased letter and Σ (Case_Ignorable, backward scan),
    # between Σ and a cased letter (Case_Ignorable, forward scan), before a space
    def make(c):
        return f"{c} {c}Σ a{c}Σ aΣ{c}b aΣ{c} "

    chars = [chr(cp) for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF]
    for i in range(0, len(chars), 2048):
        chunk = chars[i : i + 2048]
        text = "".join(make(c) for c in chunk)
        if native.fold_tokens(text) != store._fold_tokens(text):
            pytest.fail(_first_divergence(chunk, make) or f"divergence in chunk {i}")


POOLS = [
    [chr(c) for c in range(0x20, 0x7F)],
    list("àáâãäåçèéêëìíîïñòóôõöùúûüýÿœæßøłđħıĳŀŉſǅǈǋǲΣσςΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΤΥΦΧΨΩάέήίόύώΐΰϊϋ"),
    [chr(c) for c in range(0x300, 0x370)],  # combining diacritics
    [chr(c) for c in range(0x0900, 0x097F)] + [chr(c) for c in range(0x0E00, 0x0E7F)],
    [chr(c) for c in range(0xAC00, 0xAC00 + 500)] + [chr(c) for c in range(0x4E00, 0x4E00 + 500)],
    list("ﬁﬂﬀﬃﬄ²³¹½¼¾ⅠⅡⅢⅣⅧⓐⒶ①⑴µÅK𝐀𝐚𝟏‐‑–—_'’.,;:!?/\\\t\n ")
    + ["\u00ad", "\u200b", "\u200c", "\u200d", "\u2060", "\u0345"],  # SHY, ZW*, WJ, U+0345
]


@needs_native
def test_fold_tokens_matches_python_on_mixed_scripts():
    rng = random.Random(1)
    for _ in range(20_000):
        s = "".join(rng.choice(rng.choice(POOLS)) for _ in range(rng.randint(0, 40)))
        assert native.fold_tokens(s) == store._fold_tokens(s), ascii(s)


# ---- stage-2 scorer: the Python reference ----------------------------------------------

N_DOCS = 1000
DF = {"alpha": 400, "beta": 30, "gamma": 999}  # gamma's IDF clamps to 1e-6
CAND = [(10, "alpha beta gamma"), (11, "Beta alpha"), (12, "alpha alpha beta"),
        (13, None), (14, ""), (15, "delta")]
QUERY = "Alpha beta alpha gamma"
# literal output of Collection._bm25_rescore before the native scorer existed: the
# Python path (and NATIVE_BM25=0) stays bit-for-bit what raggio shipped
PINNED = ([11, 12, 10, 13], [4.630506582919665, 4.082961455183432, 3.9300947468477836, 0.0])


def _idf(qtoks):
    return {t: max(math.log((N_DOCS - DF.get(t, 0) + 0.5) / (DF.get(t, 0) + 0.5)), 1e-6)
            for t in dict.fromkeys(qtoks)}


def _collection(path, **kw):
    Path(path).mkdir(parents=True, exist_ok=True)
    return Collection(CollectionConfig("t", 8, 4, None, None, None), Path(path),
                      lambda: None, **kw)


def _pinned_collection(path, monkeypatch, **kw):
    col = _collection(path, **kw)
    col.indexed_counts = {"chunk": N_DOCS}
    monkeypatch.setattr(col, "_df", lambda t: DF.get(t, 0))
    monkeypatch.setattr(col, "_avgdl", lambda: 2.5)
    return col


def _topn_args(qtext, cand, n, avgdl=2.5):
    qtoks = store._fold_tokens(qtext)[:100]
    return (qtoks, _idf(qtoks), [r for r, _ in cand], [t for _, t in cand], avgdl, n,
            store.BM25_K1, store.BM25_B, store.SDM_WEIGHT)


def test_python_scorer_is_pinned(tmp_path, monkeypatch):
    col = _pinned_collection(tmp_path, monkeypatch)
    assert col._bm25_rescore(QUERY, CAND, 4) == PINNED
    assert store._bm25_topn(*_topn_args(QUERY, CAND, 4)) == PINNED


def test_python_scorer_edge_cases():
    # sum() of nothing is int 0: a candidate without any query token, for a query
    # without bigrams, scores int 0, and the API serializes it as 0, not 0.0
    ids, scores = store._bm25_topn(["alpha"], {"alpha": 1.0}, [1, 2], ["alpha", "beta"],
                                   1.0, 10, 1.2, 0.75, 0.2)
    assert ids == [1, 2] and [type(s) for s in scores] == [float, int]
    assert store._bm25_topn([], {}, [1], ["alpha"], 1.0, 10, 1.2, 0.75, 0.2) == ([], [])
    assert store._bm25_topn(["alpha"], {"alpha": 1.0}, [], [], 0.0, 10, 1.2, 0.75, 0.2) == ([], [])
    assert store._bm25_topn(*_topn_args(QUERY, CAND, 0)) == ([], [])
    with pytest.raises(ZeroDivisionError):  # an all-punctuation avgdl sample
        store._bm25_topn(*_topn_args(QUERY, CAND, 4, avgdl=0.0))


# ---- stage-2 scorer: native against the reference --------------------------------------

WORDS = ["alpha", "beta", "gamma", "delta", "Epsilon", "naïve", "Schrödinger", "ΣΟΦΙΑΣ",
         "σοφίας", "ﬁnite", "x_1", "量子力学", "ह\u093fन\u094dद\u0940", "Gauß", "İstanbul", "O(n log n)",
         "k-means", r"$\alpha$", "the", "of"]


def _scenario(seed):
    rng = random.Random(seed)
    texts = [" ".join(rng.choices(WORDS, k=rng.randint(0, 60))) for _ in range(300)]
    texts[3], texts[4], texts[7] = None, "", texts[8]  # missing, empty, an exact tie
    rids = rng.sample(range(1, 10**7), len(texts))
    qtoks = store._fold_tokens(" ".join(rng.choices(WORDS, k=rng.randint(1, 12))))
    idf = {t: rng.choice([1e-6, rng.uniform(0.01, 12.0)]) for t in dict.fromkeys(qtoks)}
    return (qtoks, idf, rids, texts, rng.uniform(5.0, 60.0), rng.choice([1, 10, 100, 1000]),
            store.BM25_K1, store.BM25_B, rng.choice([0.0, store.SDM_WEIGHT, 1.0]))


def _same(got, ref):
    assert got[0] == ref[0]  # the same ids in the same (-score, rid) order
    assert list(map(repr, got[1])) == list(map(repr, ref[1]))  # bit-identical, same types


@needs_native
@pytest.mark.parametrize("seed", range(40))
def test_native_scorer_matches_reference(seed):
    args = _scenario(seed)
    _same(native.bm25_topn(*args), store._bm25_topn(*args))


@needs_native
def test_native_scorer_ignores_candidate_order():
    qtoks, idf, rids, texts, *rest = _scenario(99)
    pairs = list(zip(rids, texts))
    random.Random(5).shuffle(pairs)
    shuffled = native.bm25_topn(qtoks, idf, [r for r, _ in pairs], [t for _, t in pairs], *rest)
    _same(shuffled, native.bm25_topn(qtoks, idf, rids, texts, *rest))


@needs_native
def test_native_scorer_edge_cases():
    _same(native.bm25_topn(*_topn_args(QUERY, CAND, 4)), PINNED)
    int_zero = (["alpha"], {"alpha": 1.0}, [1, 2], ["alpha", "beta"], 1.0, 10, 1.2, 0.75, 0.2)
    _same(native.bm25_topn(*int_zero), store._bm25_topn(*int_zero))
    assert native.bm25_topn([], {}, [1], ["alpha"], 1.0, 10, 1.2, 0.75, 0.2) == ([], [])
    assert native.bm25_topn(["alpha"], {"alpha": 1.0}, [], [], 0.0, 10, 1.2, 0.75, 0.2) == ([], [])
    assert native.bm25_topn(*_topn_args(QUERY, CAND, 0)) == ([], [])
    with pytest.raises(ZeroDivisionError):
        native.bm25_topn(*_topn_args(QUERY, CAND, 4, avgdl=0.0))
    # misuse the reference would silently absorb (zip truncates) is an error here
    with pytest.raises(ValueError, match="length"):
        native.bm25_topn(["alpha"], {"alpha": 1.0}, [1, 2], ["alpha"], 1.0, 10, 1.2, 0.75, 0.2)
    with pytest.raises(ValueError, match="idf"):
        native.bm25_topn(["alpha", "beta"], {"alpha": 1.0}, [1], ["alpha"], 1.0, 10, 1.2, 0.75, 0.2)
    with pytest.raises(ValueError, match="idf"):
        native.bm25_topn(["alpha"], {"alpha": 1.0, "beta": 1.0}, [1], ["alpha"], 1.0, 10, 1.2, 0.75, 0.2)


@needs_native
def test_native_scorer_is_thread_safe():
    # gil_used = false, and scoring runs detached from the interpreter: threads running
    # at once must each get exactly the single-threaded answer
    scenarios = [_scenario(seed) for seed in range(16)]
    expected = [store._bm25_topn(*a) for a in scenarios]
    with ThreadPoolExecutor(8) as pool:
        for _ in range(4):
            for got, ref in zip(pool.map(lambda a: native.bm25_topn(*a), scenarios), expected):
                _same(got, ref)


# ---- store dispatch ---------------------------------------------------------------------


@needs_native
def test_collection_scores_natively_unless_disabled(tmp_path, monkeypatch):
    real, calls = native.bm25_topn, []
    monkeypatch.setattr(native, "bm25_topn", lambda *a: calls.append(a) or real(*a))
    on = _pinned_collection(tmp_path / "on", monkeypatch)
    assert on._scorer() is native
    _same(on._bm25_rescore(QUERY, CAND, 4), PINNED)
    assert len(calls) == 1
    off = _pinned_collection(tmp_path / "off", monkeypatch, native_bm25=False)
    assert off._scorer() is None
    _same(off._bm25_rescore(QUERY, CAND, 4), PINNED)
    assert len(calls) == 1  # NATIVE_BM25=0: the Python reference scored


def test_collection_falls_back_to_python_without_the_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_native", None)
    col = _pinned_collection(tmp_path, monkeypatch)
    assert col._scorer() is None
    _same(col._bm25_rescore(QUERY, CAND, 4), PINNED)


def test_extension_is_optional_and_unicode_checked(monkeypatch):
    monkeypatch.setitem(sys.modules, "raggio_native", None)  # makes the import fail
    assert store._load_native() is None
    stale = types.ModuleType("raggio_native")
    stale.UNIDATA_VERSION = "1.1.0"  # built by an interpreter with other Unicode data
    monkeypatch.setitem(sys.modules, "raggio_native", stale)
    with pytest.warns(RuntimeWarning, match="Unicode 1.1.0"):
        assert store._load_native() is None


@needs_native
def test_extension_loads_when_unicode_matches():
    assert store._load_native() is native
    assert store._native is native


@needs_native
def test_avgdl_tokenizes_natively_with_the_same_result(tmp_path, monkeypatch):
    col = _collection(tmp_path)
    docs = [{"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "text": text,
                                            "vector": [float(i + 1)] * 8}]}
            for i, text in enumerate(t for _, t in CAND if t)]
    asyncio.run(col._process_job({"documents": docs}))
    real, calls = native.fold_tokens, []
    monkeypatch.setattr(native, "fold_tokens", lambda t: calls.append(t) or real(t))
    fast = col._avgdl()
    assert calls
    col._avgdl_cache, col._native_bm25 = None, False
    assert col._avgdl() == fast


# ---- NATIVE_BM25 knob and /healthz ------------------------------------------------------


def test_native_bm25_setting(monkeypatch):
    monkeypatch.delenv("NATIVE_BM25", raising=False)
    assert Settings().native_bm25 == "auto"
    monkeypatch.setenv("NATIVE_BM25", "0")
    assert Settings().native_bm25 == "0"
    monkeypatch.setenv("NATIVE_BM25", "off")  # a typo must not silently pick a scorer
    with pytest.raises(ValueError, match="NATIVE_BM25"):
        Settings()


@pytest.mark.parametrize(("knob", "enabled"), [("auto", True), ("0", False)])
def test_manager_hands_the_knob_to_collections(tmp_path, monkeypatch, knob, enabled):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NATIVE_BM25", knob)
    manager = CollectionManager(Settings())

    async def load():
        await manager.create_collection("c", 8, 4, None, None, None)
        col = await manager.touch("c")
        await manager.shutdown()
        return col

    assert asyncio.run(load())._native_bm25 is enabled


def _healthz(tmp_path, monkeypatch, knob):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NATIVE_BM25", knob)
    with TestClient(create_app(embedder_factory=lambda cfg: None)) as c:
        return c.get("/healthz").json()


def test_healthz_reports_python_when_disabled(tmp_path, monkeypatch):
    assert _healthz(tmp_path, monkeypatch, "0")["bm25"] == "python"


def test_healthz_reports_python_without_the_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_native", None)
    assert _healthz(tmp_path, monkeypatch, "auto")["bm25"] == "python"


@needs_native
def test_healthz_reports_native(tmp_path, monkeypatch):
    body = _healthz(tmp_path, monkeypatch, "auto")
    assert body["bm25"] == "native" and body["status"] == "ok"


# ---- packaging: the image and CI build the extension ------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def _dockerfile():
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_image_builds_the_extension_in_a_rust_builder_stage():
    d = _dockerfile()
    assert re.search(r"^ARG RUST_VERSION=\d+\.\d+\.\d+$", d, re.M)  # pinned, like UV_VERSION
    stages = re.split(r"^FROM ", d, flags=re.M)[1:]
    (builder,) = [s for s in stages if s.split("\n", 1)[0].endswith(" AS builder")]
    runtime = stages[-1]
    # the wheel is not manylinux-audited: the builder runs the runtime's Debian release
    assert builder.startswith("docker.io/library/rust:${RUST_VERSION}-slim-trixie AS builder")
    assert runtime.startswith("docker.io/library/debian:trixie-slim\n")
    # rust:*-slim has no python3: PyO3 and build.rs (gen_unicode.py) must run uv's /python,
    # the interpreter the runtime copies, or the Unicode tables are some other interpreter's
    install = builder.index("RUN uv python install ${PYTHON}\n")
    link = re.search(r"^RUN bash -c 'set -euo pipefail; .*uv python find .*/usr/local/bin/python3.*'$",
                     builder, re.M)
    pyo3 = builder.index("ENV PYO3_PYTHON=/usr/local/bin/python3\n")
    assert link and install < link.start() < pyo3
    # podman's OCI format ignores SHELL: a RUN with a pipe sets pipefail itself
    for run in re.findall(r"^RUN .*$", d, re.M):
        assert not re.search(r"(?<!\|)\|(?!\|)", run) or "set -euo pipefail" in run, run
    # the crate is copied before the dependency-only sync: its build layer caches apart from src/
    assert pyo3 < builder.index("COPY native ./native") < builder.index(
        "RUN uv sync --frozen --no-install-project")
    syncs = re.findall(r"^RUN uv sync (.*)$", d, re.M)
    assert len(syncs) == 2 and all("--extra native" in s for s in syncs)
    # maturin builds --locked against the committed lock: the image compiles these crates only
    native_cfg = (ROOT / "native" / "pyproject.toml").read_text(encoding="utf-8")
    assert "locked = true" in native_cfg.splitlines() and (ROOT / "native" / "Cargo.lock").is_file()
    # cargo's build tree stays out of /app, which the runtime stage copies whole
    assert re.search(r"CARGO_TARGET_DIR=/tmp/\S+", builder)
    assert "native/target" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    # the runtime stage: no toolchain, and A's (D16) and C's (D13) ENV lines still there
    assert "cargo" not in runtime and "rust" not in runtime
    code = "\n".join(ln for ln in runtime.splitlines() if not ln.lstrip().startswith("#"))
    assert "OPENBLAS_NUM_THREADS=1" in code and "MALLOC_TRIM_THRESHOLD_=134217728" in code


def test_ci_builds_the_extension_and_requires_it():
    ci = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "\n  native:\n" in ci
    job = re.split(r"\n  (?=\S)", ci.split("\n  native:\n", 1)[1], 1)[0]
    # x86_64 and the DGX's aarch64, with the image's toolchain
    assert "runner: [ubuntu-latest, ubuntu-24.04-arm]" in job
    rust = re.search(r"^ARG RUST_VERSION=(\S+)$", _dockerfile(), re.M).group(1)
    assert f"rustup toolchain install {rust} --profile minimal" in job
    assert "run: uv sync --frozen --group bench --extra native" in job
    # REQUIRE_NATIVE=1: a missing extension fails the job instead of skipping the parity tests
    assert re.search(r'run: uv run --no-sync pytest -q\n\s+env:\n\s+REQUIRE_NATIVE: "1"\n', job)


# ---- bench/bm25_probe.py: the DGX parity and speed probe --------------------------------

PROBE_QUERIES = ["alpha beta gamma", "Epsilon naïve the of", "ΣΟΦΙΑΣ σοφίας x_1 the",
                 "k-means O(n log n) of", "the of alpha", "zzz", ""]


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "bench"))
    import bm25_probe

    return bm25_probe


def _probe_collection(path, monkeypatch):
    # a 120-doc corpus: a zero budget floor makes the pruner drop tokens, so stage 2 runs
    monkeypatch.setattr(store, "FTS_SCAN_BUDGET_MIN_ROWS", 0)
    col = _collection(path)
    rng = random.Random(3)
    docs = [{"doc_id": f"d{i}", "chunks": [{"id": f"c{i}", "vector": [float(i % 7 + 1)] * 8,
                                            "text": " ".join(rng.choices(WORDS, k=rng.randint(1, 30)))}]}
            for i in range(120)]
    asyncio.run(col._process_job({"documents": docs}))
    return col


def test_probe_text_leg_is_the_collections_text_leg(tmp_path, monkeypatch, probe):
    col = _probe_collection(tmp_path, monkeypatch)
    assert sum(probe.is_two_stage(col, q) for q in PROBE_QUERIES) >= 3  # stage 2 runs
    for enabled in (False, True) if native else (False,):
        col._native_bm25 = enabled
        leg = probe.TextLeg(tmp_path / "meta.db", enabled)
        assert leg.cfg.tokenizer == "unicode61"
        for q in PROBE_QUERIES:
            assert leg._prune_common(q) == col._prune_common(q)
            _same(leg._text_ids(q, probe.N, "chunks", None), col._text_ids(q, probe.N, "chunks", None))
    # the tokenizer comes from the collection's FTS schema: Task 1's prune branches on it
    (tmp_path / "tri").mkdir()
    _prune_collection(tmp_path / "tri", {"c1": "get_user_id café"}, tokenizer="trigram")
    assert probe.TextLeg(tmp_path / "tri" / "meta.db", False).cfg.tokenizer == "trigram"


@needs_native
def test_probe_parity_passes_and_catches_a_one_ulp_drift(tmp_path, monkeypatch, probe, capsys):
    col = _probe_collection(tmp_path, monkeypatch)
    # "ΣΟΦΙΑΣ σοφίας x_1 the" is the one underscore query: its ranked OR, counted directly
    kept, _ = col._prune_common(PROBE_QUERIES[2])
    under = col._rdb().execute("SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH ?",
                               (store._or_query(kept),)).fetchone()[0]
    asyncio.run(col.stop())  # like bench-tv, stopped before the probe opens its meta.db
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps(PROBE_QUERIES * 3), encoding="utf-8")
    argv = ["parity", str(tmp_path / "meta.db"), str(queries), "--threads", "1,2"]
    assert probe.main(argv) == 0
    res = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert res["id_mismatches"] == res["score_bit_mismatches"] == 0 and res["max_rel_diff"] == 0
    assert res["two_stage"] >= 9 and set(res["text_leg_qps_native"]) == {"1", "2"}
    assert res["or_budget"] == 2  # max(FTS_SCAN_BUDGET_MIN_ROWS = 0, int(0.02 * 120 chunks))
    assert res["or_matches_underscore"] == [under] * 3 and under > 0
    assert res["or_over_budget"] > 0  # the rarest real token survives even a 2-row budget
    real = native.bm25_topn

    def drifted(*args):
        ids, scores = real(*args)
        return ids, [math.nextafter(s, math.inf) for s in scores]

    monkeypatch.setattr(native, "bm25_topn", drifted)
    assert probe.main(argv) == 1
    res = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert res["score_bit_mismatches"] > 0 and 0 < res["max_rel_diff"] < 1e-15


def test_probe_queries_are_the_bench_hybrid_queries(tmp_path, monkeypatch, probe):
    pytest.importorskip("orjson", reason="bench.py needs the bench dependency group")
    src = (ROOT / "bench" / "bench.py").read_text(encoding="utf-8")
    for line in (  # the derivation bm25_probe.bench_queries repeats
        "hrng = np.random.default_rng(args.seed)",
        "hybrid_rows = hrng.choice(ingest_rows, size=len(queries), replace=False)",
        "hybrid_texts = [TITLES[r] if TITLES is not None else path_tokens(paths[r]) for r in hybrid_rows]",
    ):
        assert line in src
    import bench

    def load_corpus(limit, n_queries, seed):
        bench.TITLES = [f"title {i}" for i in range(limit)]
        paths = [f"p/{i}.md" for i in range(limit)]
        return None, paths, [], np.arange(n_queries, limit), None, np.zeros((n_queries, 4))

    monkeypatch.setattr(bench, "TITLES", None)
    monkeypatch.setattr(bench, "load_corpus", load_corpus)
    out = tmp_path / "q.json"
    assert probe.main(["queries", str(out), "--limit", "50", "--queries", "5", "--seed", "42"]) == 0
    rows = np.random.default_rng(42).choice(np.arange(5, 50), size=5, replace=False)
    assert json.loads(out.read_text(encoding="utf-8")) == [f"title {r}" for r in rows]


def test_probe_passes_measures_warm_passes_apart_from_the_first(tmp_path, monkeypatch, probe):
    pytest.importorskip("orjson", reason="bench.py needs the bench dependency group")
    import bench

    def load_corpus(limit, n_queries, seed):
        bench.TITLES = [f"title {i}" for i in range(limit)]
        paths = [f"p/{i}.md" for i in range(limit)]
        return None, paths, [], np.arange(n_queries, limit), None, np.zeros((n_queries, 4))

    calls = []

    async def run_queries(engine, queries, concurrency, headers, filt=None, k=10, texts=None):
        calls.append((engine, concurrency, headers, list(texts)))
        n = len(calls)
        # pass 1 is the first after a load: its first queries are slow (spec §6)
        lat = [10.0 * n + i % 7 + (500.0 if n == 1 and i < 10 else 0.0) for i in range(len(queries))]
        # every query finds the doc its title came from, except query 0
        hits = [[f"r{t.split()[1]}"] if i else ["r0"] for i, t in enumerate(texts)]
        return lat, 2.0 * n, hits

    monkeypatch.setattr(bench, "TITLES", None)
    monkeypatch.setattr(bench, "load_corpus", load_corpus)
    monkeypatch.setattr(bench, "run_queries", run_queries)
    out = tmp_path / "passes.json"
    assert probe.main(["passes", str(out), "--limit", "100", "--queries", "20", "--seed", "42",
                       "--passes", "3", "--concurrency", "8"]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    rows = np.random.default_rng(42).choice(np.arange(20, 100), size=20, replace=False)
    assert [c[:3] for c in calls] == [("raggio", 1, bench.TR_HDRS)] * 3 + [("raggio", 8, bench.TR_HDRS)]
    assert all(c[3] == [f"title {r}" for r in rows] for c in calls)  # bench.py's hybrid texts
    first, *warm = res["passes"]
    assert res["queries"] == 20 and len(warm) == 2
    assert first["first10_max"] > 500 > max(p["first10_max"] for p in warm)
    assert [p["qps"] for p in res["passes"]] == [10.0, 5.0, 3.3]  # 20 queries / wall
    assert all(p["text_hit"] == 19 / 20 for p in res["passes"])
    assert res["top10"] == [["r0"]] + [[f"r{r}"] for r in rows[1:]]
    assert set(res["concurrent"]) == {"qps", "p50", "p99"} and res["concurrent"]["qps"] == 2.5
