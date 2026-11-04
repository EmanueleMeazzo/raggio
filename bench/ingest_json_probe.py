"""Paired ingest parsing or vector conversion and durable admission benchmark.

Run: uv run --group bench python bench/ingest_json_probe.py --out results.json
Vector conversion: uv run --group bench python bench/ingest_json_probe.py --comparison conversion --out results.json
Uses generated data and temporary local databases. Workers are deliberately paused
to isolate admission (parse, validate, journal, 202); this is not completed ingest
throughput or a network benchmark. Both arms use the same API and SQLite journal.
Parsing compares stdlib/native with model_dump in both arms; conversion compares
model_dump/vector reuse with native parsing in both arms.
"""
import argparse
from contextlib import ExitStack
import importlib.metadata
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

import numpy as np
import orjson
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import raggio.app as app_module
from raggio.config import Settings
from raggio.store import Collection, _encode_payload


def payload(rows, dim):
    rng = np.random.default_rng(719)
    vectors = rng.standard_normal((rows, dim)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = np.round(vectors, 5)
    return orjson.dumps({"documents": [{"doc_id": f"d{i}", "chunks": [{
        "id": f"c{i}", "text": "retrieval data scientific benchmark " * 15,
        "position": 0, "vector": vectors[i], "metadata": {"source": "probe", "sequence": i},
    }]} for i in range(rows)]}, option=orjson.OPT_SERIALIZE_NUMPY)


def measure_ms(operation, iterations):
    for _ in range(5):
        operation()
    start = time.perf_counter()
    for _ in range(iterations):
        operation()
    return (time.perf_counter() - start) * 1000 / iterations


def parse_ms(raw, native, iterations):
    if native:
        return measure_ms(lambda: app_module.IngestIn.model_validate_json(raw), iterations)
    return measure_ms(lambda: app_module.IngestIn.model_validate(json.loads(raw)), iterations)


def copy_payload(body):
    return body.model_dump()


def client(stack, directory, native, dim):
    settings = Settings()
    settings.data_dir = str(directory)
    settings.root_api_key = "benchmark"
    if native:
        app = app_module.create_app(settings)
    else:
        with patch.object(app_module, "_IngestJSONRoute", APIRoute):
            app = app_module.create_app(settings)
    c = stack.enter_context(TestClient(app))
    response = c.post("/collections", json={"name": "bench", "dim": dim}, headers={"x-api-key": "benchmark"})
    response.raise_for_status()
    return c


def admit(c, raw, converter):
    # Apply the same patch machinery outside the timers for both synchronous arms.
    with patch.object(app_module, "_ingest_payload", converter):
        wall, cpu = time.perf_counter(), time.process_time()
        response = c.post("/collections/bench/documents", content=raw, headers={
            "x-api-key": "benchmark", "content-type": "application/json",
        })
        cpu_ms = (time.process_time() - cpu) * 1000
        wall_ms = (time.perf_counter() - wall) * 1000
    assert response.status_code == 202, response.text
    return wall_ms, cpu_ms, response.json()["job_id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=250)
    parser.add_argument("--dim", type=int, default=1024)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--comparison", choices=("parsing", "conversion"), default="parsing")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.rows < 1 or args.dim < 8 or args.dim % 8 or args.pairs < 1 or args.iterations < 1:
        parser.error("positive rows/pairs/iterations and a positive dimension divisible by 8 are required")
    raw = payload(args.rows, args.dim)
    body = app_module.IngestIn.model_validate_json(raw)
    assert (body.model_dump()
            == app_module.IngestIn.model_validate(json.loads(raw)).model_dump())
    assert _encode_payload(app_module._ingest_payload(body)) == _encode_payload(body.model_dump())
    names = ("stdlib", "native") if args.comparison == "parsing" else ("copy", "reuse")
    converters = {name: app_module._ingest_payload if name == "reuse" else copy_payload for name in names}
    runs = []
    for pair in range(args.pairs):
        results = {}
        # Pausing the worker keeps its index/FTS work out of both admission arms.
        with tempfile.TemporaryDirectory(prefix="raggio-ingest-probe-") as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(Collection, "start_worker", lambda self: None))
            clients = {name: client(stack, Path(tmp) / name,
                                    args.comparison == "conversion" or name == "native", args.dim)
                       for name in names}
            for name, c in clients.items():
                for _ in range(3):
                    admit(c, raw, converters[name])
            samples = {name: [] for name in clients}
            order = names if pair % 2 == 0 else names[::-1]
            for i in range(args.iterations):
                for name in (order if i % 2 == 0 else order[::-1]):
                    samples[name].append(admit(clients[name], raw, converters[name]))
            for name, values in samples.items():
                last = clients[name].get(f"/collections/bench/jobs/{values[-1][2]}", headers={"x-api-key": "benchmark"})
                assert last.json()["status"] == "pending", last.text
                results[name] = {
                    "admission_p50_ms": statistics.median(x[0] for x in values),
                    "admission_p95_ms": float(np.percentile([x[0] for x in values], 95)),
                    "cpu_per_admission_ms": statistics.mean(x[1] for x in values),
                }
        for name in order:
            if args.comparison == "parsing":
                results[name]["parse_validate_ms"] = parse_ms(raw, name == "native", args.iterations)
            else:
                converter = converters[name]
                results[name]["model_conversion_ms"] = measure_ms(lambda: converter(body), args.iterations)
                results[name]["conversion_encode_ms"] = measure_ms(
                    lambda: _encode_payload(converter(body)), args.iterations)
        runs.append(results)
        print(json.dumps({"pair": pair, **results}), flush=True)
    medians = {name: {key: statistics.median(run[name][key] for run in runs)
                     for key in runs[0][name]} for name in names}
    result = {
        "python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
        "versions": {name: importlib.metadata.version(name) for name in ("fastapi", "pydantic", "numpy", "turbovec")},
        "rows": args.rows, "dim": args.dim, "payload_bytes": len(raw), "pairs": args.pairs,
        "iterations": args.iterations, "workers": "paused", "transport": "in-process ASGI TestClient",
        "comparison": args.comparison,
        "medians": medians, "runs": runs,
    }
    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
