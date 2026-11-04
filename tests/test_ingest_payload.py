"""Vector references must preserve the serialized payload and durable handoff."""
import asyncio
import math
import random
import time

import numpy as np
import pytest

from raggio.app import IngestIn, RecordIn, _ingest_payload
from raggio.store import Collection, CollectionConfig, _encode_payload


@pytest.mark.parametrize("documents", [
    [{"doc_id": "empty"}],
    [{"doc_id": "text", "summary": {"text": "summary"}, "chunks": [{"id": "c", "text": "text"}]}],
    [{"doc_id": "vector", "summary": {"vector": [0.25] * 8}, "chunks": [{"id": "c", "vector": [0.5] * 8}]}],
    [{"doc_id": "mixed", "summary": {"text": "Σ café 漢字", "vector": [0.25] * 8}, "chunks": [
        {"id": "c", "position": "2", "text": "t", "vector": ["0.25", True, 0, -0.0, 0.5, 1.0, 2.0, 3.0],
         "metadata": {"vector": [1, 2], "nested": {"a": [1, None, {"b": True}]}, "large": 2**100}},
        {"id": "c", "text": "last duplicate wins", "vector": None},
    ]}, {"doc_id": "empty"}],
    [{"doc_id": "surrogates", "chunks": [{"id": "c", "text": "\ud800", "metadata": {"v": "\udc00"}}]}],
    [{"doc_id": "floats", "chunks": [{"id": "c", "vector": [
        -0.0, 1.0000000000000002, 1e-40, 0.1, math.inf, -math.inf, math.nan, 1.0,
    ]}]}],
])
def test_journal_bytes_match_model_dump(documents):
    body = IngestIn.model_validate({"documents": documents})
    expected = _encode_payload(body.model_dump())
    assert _encode_payload(_ingest_payload(body)) == expected


@pytest.mark.parametrize("seed", range(10))
def test_random_mixed_documents_keep_journal_bytes(seed):
    rng = random.Random(seed)

    def record():
        return {
            "text": "café Σ漢字" if rng.randrange(2) else None,
            "vector": [rng.uniform(-1, 1) for _ in range(8)] if rng.randrange(2) else None,
            "metadata": {"vector": [rng.randrange(100), None], "nested": {"flags": [True, False]}},
        }

    docs = [{"doc_id": f"d{i}", "summary": record() if rng.randrange(2) else None,
             "chunks": [{**record(), "id": f"c{j % 2}", "position": j if rng.randrange(2) else None}
                        for j in range(rng.randrange(5))]} for i in range(6)]
    body = IngestIn.model_validate({"documents": docs})
    assert _encode_payload(_ingest_payload(body)) == _encode_payload(body.model_dump())


def test_only_vectors_share_references_with_the_request():
    body = IngestIn.model_validate({"documents": [{"doc_id": "d", "summary": {
        "vector": [0.25] * 8, "metadata": {"nested": {"items": [1]}},
    }, "chunks": [{"id": "c", "vector": [0.5] * 8,
                   "metadata": {"vector": [2], "nested": {"items": [3]}}}]}]})
    payload = _ingest_payload(body)
    document = payload["documents"][0]
    source = body.documents[0]
    assert document["summary"]["vector"] is source.summary.vector
    assert document["chunks"][0]["vector"] is source.chunks[0].vector
    before = _encode_payload(body.model_dump())
    assert _encode_payload(payload) == before
    assert _encode_payload(body.model_dump()) == before  # encoding does not mutate the request
    document["summary"]["metadata"]["nested"]["items"].append(4)
    document["chunks"][0]["metadata"]["vector"].append(5)
    document["chunks"][0]["metadata"]["nested"]["items"].append(6)
    assert source.summary.metadata["nested"]["items"] == [1]
    assert source.chunks[0].metadata["vector"] == [2]
    assert source.chunks[0].metadata["nested"]["items"] == [3]


def test_summary_uses_its_declared_serialization_fields():
    class ExtendedRecord(RecordIn):
        extra: str = "not serialized as RecordIn"

    body = IngestIn.model_validate({"documents": [{"doc_id": "d", "summary": ExtendedRecord(vector=[0.25] * 8)}]})
    assert _encode_payload(_ingest_payload(body)) == _encode_payload(body.model_dump())


def test_enqueued_vectors_are_independent_before_worker_replay(tmp_path):
    async def run():
        cfg = CollectionConfig("t", 8, 4, None, None, None)
        body = IngestIn.model_validate({"documents": [{"doc_id": "d", "chunks": [{
            "id": "c", "text": "stored vector", "vector": [1., 2., 3., 4., 5., 6., 7., 8.],
            "metadata": {"source": "original"},
        }]}]})
        expected = _encode_payload(body.model_dump())
        original_vector = np.array(body.documents[0].chunks[0].vector, dtype=np.float32)
        first = Collection(cfg, tmp_path, lambda: None)
        try:
            job_id = await first.enqueue(_ingest_payload(body))
            body.documents[0].chunks[0].vector[:] = [8., 7., 6., 5., 4., 3., 2., 1.]
            body.documents[0].chunks[0].metadata["source"] = "changed"
            with first._reading() as db:
                saved = db.execute("SELECT data FROM job_payloads WHERE job_id=?", (job_id,)).fetchone()[0]
            assert saved == expected
        finally:
            await first.stop()

        recovered = Collection(cfg, tmp_path, lambda: None)
        try:
            recovered.start_worker()
            deadline = time.monotonic() + 60
            while recovered.pending_jobs() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            with recovered._reading() as db:
                assert db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "done"
                blob = db.execute("SELECT vec FROM vecs").fetchone()[0]
            expected_vec = (original_vector / np.linalg.norm(original_vector)).astype(np.float16)
            np.testing.assert_array_equal(np.frombuffer(blob, dtype=np.float16), expected_vec)
            assert recovered.get_document("d")["chunks"][0]["metadata"] == {"source": "original"}
        finally:
            await recovered.stop()

    asyncio.run(run())
