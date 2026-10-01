"""Plan E: the prune tokenizer (spec D14), and raggio_native (native/, ADR 0004) against
the pure-Python reference in raggio.store.

The extension is optional: without it (a plain `uv sync`) the native tests skip and the
Python-reference tests still run. The CI native job sets REQUIRE_NATIVE=1, which turns a
missing extension into a collection error instead of skips.
"""

import asyncio
import random
from pathlib import Path

import pytest

from raggio import store
from raggio.store import Collection, CollectionConfig

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
