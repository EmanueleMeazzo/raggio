"""Compatibility checks for the exploration-only batched expansion prototype.

uv run --group bench --extra native python bench/expansion_parity_probe.py
No production method is changed outside each temporary patch context.
"""
import asyncio
import json
import argparse
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from expansion_read_probe import candidate_search, store
from raggio.config import Settings


async def check():
    cases = 0
    baseline, prototype = store.Collection.search, candidate_search(_expansion_function)
    with tempfile.TemporaryDirectory(prefix="raggio-expansion-parity-") as directory:
        settings = Settings()
        settings.data_dir, settings.root_api_key = directory, "benchmark"
        manager = store.CollectionManager(settings)
        try:
            await manager.create_collection("probe", 32, 4, None, None, None)
            col = await manager.touch("probe")
            rng = np.random.default_rng(829)
            vectors = store._normalize(rng.standard_normal((23, 32), dtype=np.float32))
            await col._process_job({"documents": [
                {"doc_id": "d0", "summary": {"text": "summary", "vector": vectors[0]},
                 "chunks": [{"id": f"c{i}", "text": "planet " * (i % 3 + 1), "position": i % 4,
                             "vector": vectors[i+1], "metadata": {"nested": ["Σ", {"flag": True}]}}
                            for i in range(20)]},
                {"doc_id": "d1", "chunks": [{"id": "alone", "text": "planet", "vector": vectors[21]}]},
                {"doc_id": "d2", "summary": {"text": "empty summary", "vector": vectors[22]}},
            ]})
            # Legacy missing/invalid vectors keep the original ranked-sibling fallback.
            # Exercise valid-vector cache paths before the legacy mutations.
            ranked = SimpleNamespace(siblings_topk=3, siblings_all=False, summary=True)
            for query in vectors[:5]:
                for mode in ("vector", "hybrid"):
                    for scope in ("chunks", "both"):
                        for k in (1, 5, 10, 23):
                            args = (mode, query[None, :], "planet" if mode == "hybrid" else None,
                                    k, scope, {"doc_id": "d0"}, ranked)
                            expected = await baseline(col, *args)
                            actual = await prototype(col, *args)
                            assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
                            cases += 1
            valid_args = ("vector", vectors[:1], None, 10, "chunks", {"doc_id": "d0"}, ranked)
            valid_concurrent = await asyncio.gather(*[factory(col, *valid_args) for factory in (baseline, prototype) for _ in range(5)])
            assert all(result == valid_concurrent[0] for result in valid_concurrent)
            await col.patch_metadata("d0", {"revision": 1}, True)
            fresh_valid = await prototype(col, *valid_args)
            assert fresh_valid == await baseline(col, *valid_args)
            assert all(hit["expansion"]["summary"]["metadata"]["revision"] == 1 for hit in fresh_valid)
            col.db.execute("DELETE FROM vecs WHERE id=(SELECT id FROM records WHERE external_id='c2')")
            col.db.execute("UPDATE vecs SET vec=x'00' WHERE id=(SELECT id FROM records WHERE external_id='c3')")
            # siblings_all includes unindexed chunks, and NULL positions sort first.
            row = col.db.execute("SELECT id FROM records WHERE external_id='c4'").fetchone()
            col.db.execute("UPDATE records SET indexed=0, position=NULL WHERE id=?", row)
            col.index.remove(row[0])
            col.db.commit()
            col._invalidate_allow()
            col.indexed_counts = dict(col.db.execute("SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type"))
            options = [None, SimpleNamespace(siblings_topk=None, siblings_all=False, summary=False),
                       SimpleNamespace(siblings_topk=None, siblings_all=False, summary=True),
                       SimpleNamespace(siblings_topk=None, siblings_all=True, summary=False),
                       SimpleNamespace(siblings_topk=None, siblings_all=True, summary=True),
                       SimpleNamespace(siblings_topk=3, siblings_all=False, summary=True),
                       SimpleNamespace(siblings_topk=3, siblings_all=True, summary=True)]
            for mode in ("vector", "text", "hybrid"):
                for scope in ("chunks", "summaries", "both"):
                    for filt in (None, {"doc_id": "d0"}, {"doc_id": ["d1", "d2"]}, {"doc_id": "missing"}):
                        for expand in options:
                            args = (mode, vectors[:1] if mode != "text" else None,
                                    "planet" if mode != "vector" else None, 30, scope, filt, expand)
                            expected = await baseline(col, *args)
                            actual = await prototype(col, *args)
                            assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False), (mode, scope, filt, expand)
                            cases += 1
            expanded = await prototype(col, "vector", vectors[:1], None, 10, "chunks", {"doc_id": "d0"}, options[4])
            a, b = expanded[:2]
            same = next(row for row in a["expansion"]["siblings"]
                        if row["id"] not in (a["id"], b["id"]))
            other = next(row for row in b["expansion"]["siblings"] if row["id"] == same["id"])
            same["metadata"]["nested"][1]["flag"] = False
            assert other["metadata"]["nested"][1]["flag"] is True
            args = ("vector", vectors[:1], None, 10, "chunks", {"doc_id": "d0"}, options[4])
            concurrent = await asyncio.gather(*[factory(col, *args) for factory in (baseline, prototype) for _ in range(5)])
            assert all(result == concurrent[0] for result in concurrent)
            await col.patch_metadata("d0", {"revision": 2}, True)
            updated = await prototype(col, *args)
            assert updated == await baseline(col, *args)
            assert all(hit["expansion"]["summary"]["metadata"]["revision"] == 2 for hit in updated)
            print(json.dumps({"parity_cases": cases, "independent_metadata": True,
                              "missing_invalid_vectors": True, "unindexed_null_position": True,
                              "concurrent_readers_per_state": 10, "fresh_after_metadata_write": True}))
        finally:
            await manager.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vector-reuse", action="store_true")
    args = parser.parse_args()
    if args.vector_reuse:
        from expansion_vector_probe import cached_fp16_vecs, vector_expansion
        _expansion_function = vector_expansion
        with patch.object(store.Collection, "_fp16_vecs", cached_fp16_vecs):
            asyncio.run(check())
    else:
        from expansion_read_probe import batch_expansion
        _expansion_function = batch_expansion
        asyncio.run(check())
