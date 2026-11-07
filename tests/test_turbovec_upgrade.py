"""turbovec 1.1 (ADR 0017): a DATA_DIR written under turbovec 1.0.0 opens with the same results, its
first sync rewrites the index as the v8 container (which 1.0.0 cannot read), and raggio's id enumeration
(`search(probe, k=len(index))`, behind _reconcile_ghosts and the IVF shard intersections) still returns
every id on an index past the staged-search threshold."""
import json
import shutil
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from turbovec import IdMapIndex

from raggio.app import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "datadir-turbovec-1.0.0"
KEY = {"x-api-key": "root-key"}
WAIT_SECONDS = 360


def magic(data_dir: Path, coll: str) -> bytes:
    return (data_dir / "collections" / coll / "index.tvim").read_bytes()[:4]


@pytest.fixture
def legacy_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    shutil.copytree(FIXTURE, data, ignore=shutil.ignore_patterns("expected.json"))
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(data))
    return data


def fixture_queries():
    """The fixture generator's own query list (tests/ is not a package: load it by path)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_turbovec_v7_datadir",
                                                  Path(__file__).parent / "fixtures" / "make_turbovec_v7_datadir.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    return gen.queries()


def search_all(client, coll, expected):
    return {name: [h["id"] for h in client.post(f"/collections/{coll}/search", json=body, headers=KEY).json()["hits"]]
            for name, body in fixture_queries() if name in expected}


def test_a_turbovec_1_0_data_dir_opens_with_the_same_results_and_its_first_stop_writes_v8(legacy_dir):
    """Opening alone is enough to convert: stop() always syncs the index (its flush), and a sync into
    a v7 file rewrites it as v8, which turbovec 1.0.0 cannot read (ADR 0017: back up before upgrading)."""
    exp = json.loads((FIXTURE / "expected.json").read_text())
    coll = exp["collection"]
    assert exp["turbovec"] == "1.0.0" and magic(legacy_dir, coll) == b"TV7\0"
    with TestClient(create_app()) as c:
        assert search_all(c, coll, exp["results"]) == exp["results"]
    assert magic(legacy_dir, coll) == b"TV8\0"
    with TestClient(create_app()) as c:
        assert search_all(c, coll, exp["results"]) == exp["results"]


def test_the_first_sync_rewrites_the_index_as_v8_and_it_reloads(legacy_dir):
    exp = json.loads((FIXTURE / "expected.json").read_text())
    coll = exp["collection"]
    far = np.zeros(32, np.float32)
    far[31] = 1.0  # a new document; its vector is not among any recorded query's neighbours
    doc = {"doc_id": "added", "chunks": [{"id": "added#0", "text": "a new chunk", "vector": far.tolist()}]}
    with TestClient(create_app()) as c:
        jid = c.post(f"/collections/{coll}/documents", json={"documents": [doc]}, headers=KEY).json()["job_id"]
        deadline = time.time() + WAIT_SECONDS
        while c.get(f"/collections/{coll}/jobs/{jid}", headers=KEY).json()["status"] != "done":
            assert time.time() < deadline, "ingest job did not finish"
            time.sleep(0.05)
        before = search_all(c, coll, exp["results"])
    assert magic(legacy_dir, coll) == b"TV8\0"
    with TestClient(create_app()) as c:
        assert search_all(c, coll, exp["results"]) == before
        info = c.get(f"/collections/{coll}", headers=KEY).json()
        assert info["chunks"] == 2 * 120 + 1 and info["summaries"] == 120
        hit = c.post(f"/collections/{coll}/search", json={"query": {"vector": far.tolist()}, "k": 1},
                     headers=KEY).json()["hits"]
        assert hit[0]["id"] == "added#0"


@pytest.mark.parametrize("allow_fraction", [None, 3])
def test_enumeration_returns_every_id_past_the_staged_threshold(allow_fraction):
    """40,000 vectors, dim 64 (a multiple of 32): turbovec 1.1 stages the search on capable CPUs (arm64,
    x86 with AVX-512 VBMI + VNNI) and scans the whole index elsewhere; both must enumerate every id."""
    rng = np.random.default_rng(5)
    n, dim = 40_000, 64
    vecs = rng.standard_normal((n, dim)).astype(np.float32)
    vecs[:, :4] += 2.0  # a shared direction, as many open-source embedding models have
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    ids = rng.choice(10 * n, size=n, replace=False).astype(np.uint64)
    idx = IdMapIndex(dim=dim, bit_width=4)
    idx.add_with_ids(vecs, ids)
    probe = np.zeros((1, dim), np.float32)
    probe[0, 0] = 1.0
    if allow_fraction is None:
        got = idx.search(probe, k=len(idx))[1][0]
        assert len(got) == n and set(got.tolist()) == set(ids.tolist())
    else:
        allow = np.sort(rng.choice(ids, size=n // allow_fraction, replace=False))
        got = idx.search(probe, k=len(allow), allowlist=allow)[1][0]
        assert set(got.tolist()) == set(allow.tolist())
