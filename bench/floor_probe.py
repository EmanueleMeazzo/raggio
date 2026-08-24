# Path B/C probe: (C) served per-request overhead floor — vector search against a
# ~100-row collection is pure HTTP + pydantic parse + dispatch, the scan is free;
# (B) stdlib json vs orjson serialization of a real 10-hit search response.
# Usage: uv run python bench/floor_probe.py   (bench-tv container must be up)
import json
import statistics as st
import time

import httpx
import numpy as np

TR = "http://localhost:18000"
HDRS = {"x-api-key": "bench", "content-type": "application/json"}
rng = np.random.default_rng(3)
q = (rng.standard_normal(1536)).astype(np.float32)
q /= np.linalg.norm(q)
qvec = [round(float(x), 5) for x in q]

with httpx.Client(headers=HDRS, timeout=30) as c:
    # ---- C: overhead floor on a tiny collection ----
    c.delete(f"{TR}/collections/floor")
    r = c.post(f"{TR}/collections", json={"name": "floor", "dim": 1536, "bit_width": 4})
    r.raise_for_status()
    docs = [{"doc_id": f"d{i}", "chunks": [{
        "id": f"c{i}", "text": "tiny", "position": 0,
        "vector": [round(float(x), 5) for x in v / np.linalg.norm(v)],
    }]} for i, v in enumerate(rng.standard_normal((100, 1536)).astype(np.float32))]
    job = c.post(f"{TR}/collections/floor/documents", json={"documents": docs}).json()["job_id"]
    while c.get(f"{TR}/collections/floor/jobs/{job}").json()["status"] not in ("done", "error"):
        time.sleep(0.2)

    body = {"query": {"vector": qvec}, "mode": "vector", "k": 10}
    for _ in range(20):
        c.post(f"{TR}/collections/floor/search", json=body).raise_for_status()
    lat = []
    for _ in range(200):
        t0 = time.perf_counter()
        c.post(f"{TR}/collections/floor/search", json=body)
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    print(f"floor (100 rows): p50={lat[100]:.2f}ms p90={lat[180]:.2f}ms  "
          f"<- pure HTTP+parse+dispatch, scan is ~0")
    c.delete(f"{TR}/collections/floor")

    # ---- B: serialization cost of a real response from the 553k collection ----
    resp = c.post(f"{TR}/collections/bench/search", json=body).json()
    payload_kb = len(json.dumps(resp)) / 1024
    n = 1000
    t0 = time.perf_counter()
    for _ in range(n):
        json.dumps(resp)
    std_ms = (time.perf_counter() - t0) / n * 1000
    import orjson
    t0 = time.perf_counter()
    for _ in range(n):
        orjson.dumps(resp)
    orj_ms = (time.perf_counter() - t0) / n * 1000
    print(f"real response {payload_kb:.1f}KB: json.dumps={std_ms:.3f}ms orjson={orj_ms:.3f}ms "
          f"(saves {std_ms - orj_ms:.3f}ms/query)")
    # request-side reference: pydantic must validate the 1536-float vector each call
    t0 = time.perf_counter()
    for _ in range(n):
        json.dumps(body)
    print(f"request body json.dumps={((time.perf_counter() - t0) / n * 1000):.3f}ms (client-side reference)")
