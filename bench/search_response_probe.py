"""Paired search response encoding benchmark, through the real retrieval endpoint.

Run: uv run --group bench python bench/search_response_probe.py --pairs 5 --out results.json
Uses generated vectors/text and temporary local storage. Both arms use the same
collection, native search, SQLite reads, ranking and response bytes. The default
arm returns a dict for FastAPI to encode; direct uses the production fast path.
ASGI TestClient timings include client work and exclude external embeddings/network.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import gc
import importlib.metadata
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

import numpy as np
import orjson
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import raggio.app as app_module
from raggio.config import Settings
from raggio.store import CollectionManager, _normalize


async def prepare(settings, rows, dim):
    """Build a fixture outside all timers, using the ordinary storage/index path."""
    manager = CollectionManager(settings)
    try:
        await manager.create_collection("bench", dim, 4, None, None, None)
        col = await manager.touch("bench")
        vectors = _normalize(np.random.default_rng(719).standard_normal((rows, dim), dtype=np.float32))
        documents = []
        for start in range(0, rows, 20):
            documents.append({"doc_id": f"d{start // 20}", "summary": {
                "vector": vectors[start], "text": "Scientific retrieval summary",
                "metadata": {"source": "probe", "group": start // 20 % 10},
            }, "chunks": [{"id": f"c{i}", "position": i - start, "vector": vectors[i],
                           "text": f"scientific retrieval topic{i % 101} café Σ漢字 " * 10,
                           "metadata": {"source": "probe", "group": start // 20 % 10, "year": 2026,
                                        "tags": ["retrieval", "Σ"], "nested": {"flag": True, "sequence": i}}}
                          for i in range(start, min(start + 20, rows))]})
        await col._process_job({"documents": documents})
    finally:
        await manager.shutdown()


def default_response(hits):
    return {"hits": hits}


def post(client, raw):
    response = client.post("/collections/bench/search", content=raw, headers={
        "x-api-key": "benchmark", "content-type": "application/json",
    })
    assert response.status_code == 200, response.text
    return response


def encode_probe(data, iterations):
    operations = {"default": lambda: JSONResponse(jsonable_encoder(data)),
                  "direct": lambda: app_module._search_response(data["hits"])}
    assert operations["default"]().body == operations["direct"]().body
    samples = {name: [] for name in operations}
    for i in range(iterations):
        for name in (tuple(operations) if i % 2 == 0 else tuple(operations)[::-1]):
            start = time.perf_counter()
            operations[name]()
            samples[name].append((time.perf_counter() - start) * 1000)
    peaks = {}
    # Allocation tracing is separate: it distorts timings. The existing hits are
    # outside this trace; this measures temporary encoding allocations, not RSS.
    for name, operation in operations.items():
        gc.collect()
        tracemalloc.start()
        response = operation()
        _, peaks[name] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del response
    return {name: {"encode_p50_ms": statistics.median(samples[name]),
                   "encoding_python_peak_bytes": peaks[name]} for name in operations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--dim", type=int, default=1024)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.rows < 20 or args.dim < 8 or args.dim % 8 or args.pairs < 1 or args.iterations < 1:
        parser.error("rows >= 20, positive pairs/iterations, and a dimension divisible by 8 are required")
    qvecs = _normalize(np.random.default_rng(743).standard_normal((args.iterations, args.dim), dtype=np.float32))
    cases = {
        "vector-k10": {"mode": "vector", "k": 10},
        "text-k10": {"mode": "text", "k": 10},
        "hybrid-k10": {"mode": "hybrid", "k": 10},
        "vector-k100": {"mode": "vector", "k": 100},
        "vector-k1000": {"mode": "vector", "k": 1000},
        "vector-expanded": {"mode": "vector", "k": 10, "expand": {"siblings_topk": 3, "summary": True}},
        "text-expanded": {"mode": "text", "k": 10, "expand": {"siblings_topk": 3, "summary": True}},
        "vector-doc-filter": {"mode": "vector", "k": 10, "filter": {"doc_id": "d0"}},
    }
    factories = {"default": default_response, "direct": app_module._search_response}
    runs, components = {}, {}
    with tempfile.TemporaryDirectory(prefix="raggio-search-response-") as directory:
        settings = Settings()
        settings.data_dir, settings.root_api_key = directory, "benchmark"
        asyncio.run(prepare(settings, args.rows, args.dim))
        with TestClient(app_module.create_app(settings)) as client, ThreadPoolExecutor(max_workers=8) as pool:
            for label, case in cases.items():
                bodies = []
                for vec in qvecs:
                    query = {}
                    if case["mode"] != "vector":
                        query["text"] = "scientific retrieval topic7"
                    if case["mode"] != "text":
                        query["vector"] = np.round(vec, 5)
                    bodies.append(orjson.dumps({**case, "query": query}, option=orjson.OPT_SERIALIZE_NUMPY))
                for factory in factories.values():
                    with patch.object(app_module, "_search_response", factory):
                        for i in range(3):
                            post(client, bodies[i % len(bodies)])
                case_runs = []
                for pair in range(args.pairs):
                    samples = {name: [] for name in factories}
                    order = tuple(factories) if pair % 2 == 0 else tuple(factories)[::-1]
                    for i, raw in enumerate(bodies):
                        outputs = []
                        for name in (order if i % 2 == 0 else order[::-1]):
                            # Same patch machinery outside the clocks for both arms.
                            with patch.object(app_module, "_search_response", factories[name]):
                                wall, cpu = time.perf_counter(), time.process_time()
                                response = post(client, raw)
                                samples[name].append(((time.perf_counter() - wall) * 1000,
                                                      (time.process_time() - cpu) * 1000))
                            outputs.append(response.content)
                        assert outputs[0] == outputs[1], label
                    row = {name: {"search_p50_ms": statistics.median(x[0] for x in values),
                                  "search_p95_ms": float(np.percentile([x[0] for x in values], 95)),
                                  "cpu_per_search_ms": statistics.mean(x[1] for x in values)}
                           for name, values in samples.items()}
                    case_runs.append(row)
                    print(json.dumps({"case": label, "pair": pair, **row}), flush=True)
                runs[label] = case_runs
                response = post(client, bodies[0])
                components[label] = {"response_bytes": len(response.content),
                                     "arms": encode_probe(response.json(), args.iterations)}
            concurrent = []
            bodies = [orjson.dumps({"mode": "vector", "query": {"vector": np.round(vec, 5)}, "k": 10},
                                   option=orjson.OPT_SERIALIZE_NUMPY) for vec in qvecs]
            # Start the client threads and warm the concurrent scan path outside
            # the clocks. Longer samples avoid measuring just a short startup burst.
            for factory in factories.values():
                with patch.object(app_module, "_search_response", factory):
                    list(pool.map(lambda raw: post(client, raw), bodies))
            for pair in range(args.pairs):
                row, outputs = {}, []
                for name in (tuple(factories) if pair % 2 == 0 else tuple(factories)[::-1]):
                    with patch.object(app_module, "_search_response", factories[name]):
                        wall, cpu = time.perf_counter(), time.process_time()
                        responses = []
                        for _ in range(5):
                            responses.extend(pool.map(lambda raw: post(client, raw), bodies))
                        wall_ms, cpu_ms = (time.perf_counter() - wall) * 1000, (time.process_time() - cpu) * 1000
                    outputs.append([r.content for r in responses])
                    row[name] = {"qps": len(responses) * 1000 / wall_ms, "cpu_per_search_ms": cpu_ms / len(responses)}
                assert outputs[0] == outputs[1], "concurrent"
                concurrent.append(row)
            runs["vector-concurrency8"] = concurrent
    medians = {case: {name: {key: statistics.median(row[name][key] for row in values)
                            for key in values[0][name]} for name in factories}
               for case, values in runs.items()}
    result = {"python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
              "versions": {name: importlib.metadata.version(name) for name in ("fastapi", "pydantic", "numpy", "turbovec")},
              "chunk_rows": args.rows, "summary_rows": (args.rows + 19) // 20, "dim": args.dim,
              "pairs": args.pairs, "iterations": args.iterations, "seed_data": 719, "seed_queries": 743,
              "concurrent_requests_per_arm_per_pair": 5 * args.iterations, "concurrent_warmup_requests_per_arm": args.iterations,
              "transport": "in-process ASGI TestClient", "same_response_bytes": True,
              "medians": medians, "runs": runs, "encoding_components": components}
    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"medians": medians, "encoding_components": components}, indent=2))


if __name__ == "__main__":
    main()
