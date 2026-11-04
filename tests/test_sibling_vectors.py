"""Score parity, resource bounds and lifecycle checks for per-search sibling reuse."""
import asyncio
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import raggio.store as store

WAIT = 30
EXPAND = SimpleNamespace(siblings_topk=3, siblings_all=False, summary=True)


async def reference_expansion(col, hits, qvec, qtext, expand):
    for hit in hits:
        await col._expand(hit, qvec, qtext, expand)


async def fixture_col(path, dim=32, per_doc=20, bit_width=4, tokenizer="unicode61"):
    col = store.Collection(store.CollectionConfig("t", dim, bit_width, None, None, None,
                                                  tokenizer=tokenizer), Path(path), lambda: None)
    rng = np.random.default_rng(853)
    vectors = store._normalize(rng.standard_normal((2 * per_doc + 2, dim), dtype=np.float32))
    vectors[1:4] = vectors[0]  # exact ties, including the summary
    await col._process_job({"documents": [
        {"doc_id": f"d{d}", "summary": {"text": "planet summary", "vector": vectors[d * (per_doc + 1)]},
         "chunks": [{"id": f"d{d}c{i}", "position": i % 4, "text": "planet " * (i % 3 + 1),
                     "vector": vectors[d * (per_doc + 1) + i + 1],
                     "metadata": {"nested": ["Σ", {"flag": True}]}} for i in range(per_doc)]}
        for d in range(2)]})
    return col, vectors[:1]


async def results(col, qvec, mode="vector", scope="chunks", expand=EXPAND, k=10, filt=None, **kw):
    return await col.search(mode, qvec if mode != "text" else None,
                            "planet" if mode != "vector" else None, k, scope,
                            {"doc_id": "d0"} if filt is None else filt, expand, **kw)


async def assert_reference(col, qvec, monkeypatch, **kw):
    actual = await results(col, qvec, **kw)
    with monkeypatch.context() as ref:
        ref.setattr(store.Collection, "_expand_hits", reference_expansion)
        expected = await results(col, qvec, **kw)
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    return actual


@pytest.mark.parametrize("dim,per_doc,bit_width", [
    (8, 19, 4), (24, 20, 4), (40, 20, 4), (32, 50, 4), (32, 51, 4), (32, 200, 2), (32, 201, 2),
    (1024, 20, 4), (3072, 20, 4), (32, 512, 4), (32, 513, 4),
])
@pytest.mark.parametrize("ivf", [False, True])
def test_sibling_reuse_exact_score_and_order_parity(tmp_path, monkeypatch, dim, per_doc, bit_width, ivf):
    async def run():
        col, qvec = await fixture_col(tmp_path, dim, per_doc, bit_width)
        try:
            if ivf:
                monkeypatch.setattr(store, "IVF_MIN_ROWS", 1)
                await col._process_job({"op": "attach_index", "nlist": 2, "nprobe": 1})
            sibling_k = 300 if per_doc >= 512 else 3
            expand = SimpleNamespace(siblings_topk=sibling_k, siblings_all=True, summary=True)
            for mode, scope, fusion, alpha in (("vector", "chunks", "rrf", .5),
                    ("hybrid", "both", "rrf", .5), ("hybrid", "chunks", "relative", .25),
                    ("hybrid", "chunks", "relative", 0), ("text", "chunks", "rrf", .5)):
                await assert_reference(col, qvec, monkeypatch, mode=mode, scope=scope,
                                       expand=expand, fusion=fusion, alpha=alpha)
            # A larger repeated-document set crosses the 512-id blob-fetch boundary.
            if per_doc >= 512:
                await assert_reference(col, qvec, monkeypatch, expand=expand, k=40,
                                       filt={"doc_id": ["d0", "d1"]})
        finally:
            await col.stop()
    asyncio.run(run())


@pytest.mark.parametrize("tokenizer", ["unicode61", "trigram"])
@pytest.mark.parametrize("legacy", [False, True])
def test_sibling_reuse_legacy_unindexed_and_expansion_modes(tmp_path, monkeypatch, tokenizer, legacy):
    async def run():
        col, qvec = await fixture_col(tmp_path, tokenizer=tokenizer)
        try:
            if legacy:
                col.db.execute("DELETE FROM vecs WHERE id=(SELECT id FROM records WHERE external_id='d0c2')")
                col.db.execute("UPDATE vecs SET vec=x'00' WHERE id=(SELECT id FROM records WHERE external_id='d0c3')")
                rid = col.db.execute("SELECT id FROM records WHERE external_id='d0c4'").fetchone()[0]
                col.db.execute("UPDATE records SET indexed=0, position=NULL WHERE id=?", (rid,))
                col.index.remove(rid)
                col.db.execute("DELETE FROM records WHERE external_id='d0'")
                col.index.remove(1)
                col.db.commit()
                col._invalidate_allow()
                col.indexed_counts = dict(col.db.execute("SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type"))
            for mode in ("vector", "text", "hybrid"):
                for expand in (None, EXPAND,
                        SimpleNamespace(siblings_topk=None, siblings_all=True, summary=True),
                        SimpleNamespace(siblings_topk=None, siblings_all=False, summary=True)):
                    await assert_reference(col, qvec, monkeypatch, mode=mode, scope="both", expand=expand)
            await assert_reference(col, qvec, monkeypatch, filt={"doc_id": "missing"})
        finally:
            await col.stop()
    asyncio.run(run())


def test_preparation_is_bounded_readonly_and_prefers_repeated_documents(tmp_path, monkeypatch):
    async def run():
        col, qvec = await fixture_col(tmp_path)
        try:
            hits = [{"doc_id": "d1"}] * 2 + [{"doc_id": "d0"}] * 5
            monkeypatch.setattr(store, "SIBLING_VECTOR_CACHE_BYTES", 20 * col.cfg.dim * 4)
            cache = await asyncio.to_thread(col._prepare_sibling_vectors, hits, 3)
            assert set(cache) == {"d0"}
            assert sum(row.nbytes for group in cache.values() for row in group.values()) == 20 * col.cfg.dim * 4
            assert all(not row.flags.writeable for row in cache["d0"].values())
            await assert_reference(col, qvec, monkeypatch)
            monkeypatch.setattr(store, "SIBLING_VECTOR_CACHE_ROWS", 19)
            assert await asyncio.to_thread(col._prepare_sibling_vectors, hits, 3) == {}
            monkeypatch.setattr(store, "SIBLING_VECTOR_CACHE_BYTES", 0)
            await assert_reference(col, qvec, monkeypatch)
            assert col._search_allowed(qvec, 3, np.empty(0, dtype=np.uint64), vectors={}) == ([], [])
        finally:
            await col.stop()
    asyncio.run(run())


def test_reuse_fetches_sibling_vectors_once_and_metadata_remains_independent(tmp_path, monkeypatch):
    async def run():
        col, qvec = await fixture_col(tmp_path)
        try:
            calls = []
            original = col._fp16_vecs
            def counted(ids):
                calls.append(tuple(ids))
                return original(ids)
            monkeypatch.setattr(col, "_fp16_vecs", counted)
            hits = await results(col, qvec)
            assert len(calls) == 2  # primary ranking and one sibling preparation
            appearances = {}
            for hit in hits:
                for sibling in hit["expansion"]["siblings"]:
                    appearances.setdefault(sibling["id"], []).append(sibling)
            copies = next(rows for rows in appearances.values() if len(rows) > 1)
            copies[0]["metadata"]["nested"][1]["flag"] = False
            assert copies[1]["metadata"]["nested"][1]["flag"] is True
            await col.patch_metadata("d0", {"revision": 2}, True)
            fresh = await assert_reference(col, qvec, monkeypatch)
            assert all(h["metadata"]["revision"] == 2 for h in fresh)
            await col._process_job({"documents": [{"doc_id": "d0", "chunks": [
                {"id": "d0c0", "vector": -qvec[0], "text": "planet changed"}]}]})
            await assert_reference(col, qvec, monkeypatch)
        finally:
            await col.stop()
    asyncio.run(run())


def test_large_document_is_rejected_without_fetching_its_vectors(tmp_path, monkeypatch):
    async def run():
        col, _ = await fixture_col(tmp_path, per_doc=51)
        try:
            def unexpected(ids):
                raise AssertionError("large document must not be decoded for reuse")
            monkeypatch.setattr(col, "_fp16_vecs", unexpected)
            assert await asyncio.to_thread(col._prepare_sibling_vectors, [{"doc_id": "d0"}] * 2, 3) == {}
            plan = col.db.execute("EXPLAIN QUERY PLAN SELECT id FROM records WHERE doc_id=?"
                " AND type='chunk' AND indexed=1 ORDER BY id LIMIT ?", ("d0", 51)).fetchall()
            detail = " ".join(row[3] for row in plan)
            assert "COVERING INDEX idx_records_doc_type" in detail and "TEMP B-TREE" not in detail
        finally:
            await col.stop()
    asyncio.run(run())


def test_preparation_runs_off_loop_and_holds_read_lock_against_writes(tmp_path, monkeypatch):
    async def run():
        col, qvec = await fixture_col(tmp_path)
        started, release = threading.Event(), threading.Event()
        owner, original = threading.get_ident(), col._prepare_sibling_vectors
        def blocked(hits, n):
            assert threading.get_ident() != owner
            started.set()
            assert release.wait(WAIT)
            return original(hits, n)
        monkeypatch.setattr(col, "_prepare_sibling_vectors", blocked)
        try:
            query = asyncio.create_task(results(col, qvec))
            assert await asyncio.to_thread(started.wait, WAIT)
            writer = asyncio.create_task(col.patch_metadata("d0", {"revision": 1}, True))
            await asyncio.sleep(0)  # the event loop remains able to schedule the writer
            assert not writer.done()
            release.set()
            before = await asyncio.wait_for(query, WAIT)
            assert all("revision" not in hit["metadata"] for hit in before)
            await asyncio.wait_for(writer, WAIT)
            after = await results(col, qvec)
            assert all(hit["metadata"]["revision"] == 1 for hit in after)
        finally:
            release.set()
            await col.stop()
    asyncio.run(run())


@pytest.mark.parametrize("stop", [False, True])
def test_cancelled_preparation_is_discarded_after_write_or_stop(tmp_path, monkeypatch, stop):
    async def run():
        col, qvec = await fixture_col(tmp_path)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = col._fp16_vecs
        preparing = False
        outcome = []
        prepare = col._prepare_sibling_vectors
        def prepare_spy(hits, n):
            nonlocal preparing
            preparing = True
            try:
                cache = prepare(hits, n)
                outcome.append(cache)
                return cache
            except RuntimeError as exc:
                outcome.append(str(exc))
                raise
            finally:
                finished.set()
        def blocked(ids):
            if preparing:
                started.set()
                assert release.wait(WAIT)
            return original(ids)
        monkeypatch.setattr(col, "_prepare_sibling_vectors", prepare_spy)
        monkeypatch.setattr(col, "_fp16_vecs", blocked)
        try:
            query = asyncio.create_task(results(col, qvec))
            assert await asyncio.to_thread(started.wait, WAIT)
            query.cancel()
            with pytest.raises(asyncio.CancelledError):
                await query
            if stop:
                await asyncio.wait_for(col.stop(), WAIT)
            else:
                await asyncio.wait_for(col.patch_metadata("d0", {"revision": 1}, True), WAIT)
            release.set()
            assert await asyncio.to_thread(finished.wait, WAIT)
            if stop:
                assert "closed" in outcome[0]
            else:
                assert outcome == [{}]
        finally:
            release.set()
            await col.stop()
    asyncio.run(run())
