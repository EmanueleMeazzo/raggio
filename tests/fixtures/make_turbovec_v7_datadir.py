"""Write tests/fixtures/datadir-turbovec-1.0.0/: a raggio DATA_DIR whose index file turbovec 1.0.0
wrote (the v7 container, magic TV7\\0), and expected.json, the search results raggio gave on it.

Run once, in an environment with turbovec 1.0.0 (raggio 0.3.0's lock), from the repo root:
    uv run python tests/fixtures/make_turbovec_v7_datadir.py
tests/test_turbovec_upgrade.py loads the directory with the current turbovec and checks the results
are unchanged and that the first sync rewrites the file as v8 (ADR 0017).
"""
import importlib.metadata
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from raggio.app import create_app

OUT = Path(__file__).parent / "datadir-turbovec-1.0.0"
KEY = {"x-api-key": "root-key"}
COLL, DIM, N_DOCS = "legacy", 32, 120


def queries():
    """(name, body) of the searches whose results the fixture records."""
    rng = np.random.default_rng(11)
    out = []
    for i in range(5):
        q = rng.standard_normal(DIM).astype(np.float32)
        out.append((f"vector-{i}", {"query": {"vector": (q / np.linalg.norm(q)).round(6).tolist()}, "k": 5}))
    out.append(("hybrid", {"query": {"vector": out[0][1]["query"]["vector"], "text": "orchid harbour"},
                           "mode": "hybrid", "k": 5}))
    out.append(("one-document", {"query": {"vector": out[1][1]["query"]["vector"]}, "k": 3,
                                 "filter": {"doc_id": "d7"}}))
    out.append(("summaries", {"query": {"vector": out[2][1]["query"]["vector"]}, "k": 5, "scope": "summaries"}))
    return out


def documents():
    rng = np.random.default_rng(7)
    words = ["orchid", "harbour", "granite", "lantern", "meadow", "cobalt", "saffron", "tundra"]
    docs = []
    for d in range(N_DOCS):
        def rec(text):
            v = rng.standard_normal(DIM).astype(np.float32)
            return {"text": text, "vector": (v / np.linalg.norm(v)).round(6).tolist(), "metadata": {"n": d}}
        docs.append({"doc_id": f"d{d}", "summary": rec(f"summary of document {d} about {words[d % 8]}"),
                     "chunks": [{"id": f"d{d}#{c}", **rec(f"chunk {c} of document {d}: {words[(d + c) % 8]} "
                                                          f"{words[(d * 3 + c) % 8]}")} for c in range(2)]})
    return docs


def main():
    version = importlib.metadata.version("turbovec")
    if version != "1.0.0":
        sys.exit(f"turbovec {version}: the fixture must be written by 1.0.0")
    tmp = Path(tempfile.mkdtemp())
    import os
    os.environ["ROOT_API_KEY"], os.environ["DATA_DIR"] = "root-key", str(tmp)
    with TestClient(create_app()) as c:
        c.post("/collections", json={"name": COLL, "dim": DIM}, headers=KEY).raise_for_status()
        jid = c.post(f"/collections/{COLL}/documents", json={"documents": documents()}, headers=KEY).json()["job_id"]
        for _ in range(600):
            if c.get(f"/collections/{COLL}/jobs/{jid}", headers=KEY).json()["status"] == "done":
                break
            time.sleep(0.05)
        expected = {name: [h["id"] for h in c.post(f"/collections/{COLL}/search", json=body, headers=KEY).json()["hits"]]
                    for name, body in queries()}
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(tmp, OUT, ignore=shutil.ignore_patterns("raggio.lock", "*-wal", "*-shm"))
    magic = (OUT / "collections" / COLL / "index.tvim").read_bytes()[:4]
    assert magic == b"TV7\0", magic
    (OUT / "expected.json").write_text(json.dumps({"turbovec": version, "collection": COLL, "results": expected}, indent=1))
    print(f"wrote {OUT} ({sum(f.stat().st_size for f in OUT.rglob('*') if f.is_file()):,} bytes), magic {magic}")


if __name__ == "__main__":
    main()
