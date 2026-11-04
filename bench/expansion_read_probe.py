"""Explore batched expansion reads without changing production code.

uv run --group bench --extra native python bench/expansion_read_probe.py --pairs 5 --out results.json
The prototype replaces only the expansion loop inside Collection.search's existing
read lock. Ranked siblings still call the production implementation. Fixture setup,
SQL tracing and allocation tracing are outside the paired endpoint timings.
"""
import argparse
import asyncio
from collections import Counter, defaultdict
import gc
import hashlib
import inspect
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import textwrap
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import orjson
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import raggio.store as store
import raggio.app as app_module
from raggio.app import create_app
from raggio.config import Settings


def materialize(row):
    # Fresh mutable metadata for every occurrence, just like _hydrate. Text is immutable.
    return {"id": row[1], "doc_id": row[2], "type": row[3], "position": row[4],
            "text": row[5], "metadata": json.loads(row[6])}


async def batch_expansion(col, hits, qvec, qtext, expand):
    if not hits or not (expand.summary or (expand.siblings_all and not expand.siblings_topk)):
        for hit in hits:
            await col._expand(hit, qvec, qtext, expand)
        return
    all_siblings = expand.siblings_all and not expand.siblings_topk
    docs = list(dict.fromkeys(hit["doc_id"] for hit in hits
                            if all_siblings or hit["type"] != "summary"))
    siblings, summaries = defaultdict(list), {}
    if docs:
        types = "type IN ('chunk','summary')" if all_siblings else "type='summary'"
        with col._reading() as db:
            rows = list(store._rows_by_id(db,
                "SELECT id, external_id, doc_id, type, position, text, metadata"
                " FROM records WHERE doc_id IN ({}) AND " + types, docs))
        for row in rows:
            if row[3] == "summary":
                summaries.setdefault(row[2], row)
            else:
                siblings[row[2]].append(row)
        for group in siblings.values():
            group.sort(key=lambda row: (row[4] is not None, row[4] or 0, row[0]))
        del rows
    ranked_only = SimpleNamespace(siblings_topk=expand.siblings_topk,
                                  siblings_all=False, summary=False)
    for hit in hits:
        if expand.siblings_topk:
            await col._expand(hit, qvec, qtext, ranked_only)
        ext = hit.get("expansion", {})
        if all_siblings:
            ext["siblings"] = [materialize(row) for row in siblings[hit["doc_id"]]
                               if hit["type"] != "chunk" or row[1] != hit["id"]]
        if expand.summary and hit["type"] != "summary":
            row = summaries.get(hit["doc_id"])
            ext["summary"] = materialize(row) if row else None
        if ext:
            hit["expansion"] = ext


def candidate_search(expansion_function=batch_expansion):
    """Keep the production search and lock verbatim; replace one guarded loop."""
    source = textwrap.dedent(inspect.getsource(store.Collection.search))
    before = "for hit in hits:\n                await self._expand(hit, qvec, qtext, expand)"
    assert source.count(before) == 1, "Search changed; review the prototype hook before running"
    source = source.replace(before, "await _probe_batch_expansion(self, hits, qvec, qtext, expand)")
    namespace = {**vars(store), "_probe_batch_expansion": expansion_function}
    exec(compile(source, __file__, "exec"), namespace)
    return namespace["search"]


async def prepare(settings, rows, dim):
    manager = store.CollectionManager(settings)
    rng = np.random.default_rng(821)
    centers = rng.standard_normal(((rows + 19) // 20, dim), dtype=np.float32)
    try:
        for name in ("clustered", "dispersed"):
            await manager.create_collection(name, dim, 4, None, None, None)
            col = await manager.touch(name)
            vectors = rng.standard_normal((rows, dim), dtype=np.float32)
            if name == "clustered":
                vectors = centers[np.arange(rows) // 20] + 0.35 * vectors
            vectors = store._normalize(vectors)
            docs = []
            for start in range(0, rows, 20):
                d = start // 20
                docs.append({"doc_id": f"d{d}", "summary": {
                    "vector": store._normalize(centers[d:d+1])[0],
                    "text": f"Summary topic{d}", "metadata": {"nested": {"tags": ["Σ", d]}},
                }, "chunks": [{"id": f"c{i}", "position": i-start, "vector": vectors[i],
                    "text": f"scientific retrieval topic{d} café Σ漢字 " * 12,
                    "metadata": {"source": name, "nested": {"tags": ["Σ", i]}, "group": d % 10}}
                    for i in range(start, min(start+20, rows))]})
            await col._process_job({"documents": docs})
    finally:
        await manager.shutdown()
    return store._normalize(centers)


def post(client, name, body):
    response = client.post(f"/collections/{name}/search", content=body,
        headers={"x-api-key": "benchmark", "content-type": "application/json"})
    assert response.status_code == 200, response.text
    return response


async def store_result(col, body):
    b = json.loads(body)
    q = b["query"]
    vec = store._normalize(np.array([q["vector"]], dtype=np.float32)) if "vector" in q else None
    e = b.get("expand")
    expand = SimpleNamespace(siblings_topk=e.get("siblings_topk"), siblings_all=e.get("siblings_all", False),
                             summary=e.get("summary", False)) if e else None
    return await col.search(b["mode"], vec, q.get("text"), b["k"], b.get("scope", "chunks"),
                            b.get("filter"), expand)


async def diagnostics(col, body, factory):
    statements = []
    enter = store._ReadConn.__enter__
    def traced(rc):
        db = enter(rc)
        db.set_trace_callback(statements.append)
        return db
    with patch.object(store.Collection, "search", factory):
        try:
            with patch.object(store._ReadConn, "__enter__", traced):
                hits = await store_result(col, body)
        finally:
            for rc in col._read_conns:
                rc.db.set_trace_callback(None)
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        counts = Counter()
        for sql in selects:
            sql = " ".join(sql.lower().split())
            category = ("hydration" if sql.startswith("select id, external_id") else
                        "vector_blobs" if sql.startswith("select id, vec") else
                        "document_membership" if sql.startswith("select id from records where doc_id") else "other")
            counts[category] += 1
        gc.collect()
        tracemalloc.start()
        result = await store_result(col, body)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert result == hits
    return {"select_count": len(selects), "select_categories": dict(counts),
            "store_python_peak_bytes": peak, "hits": len(hits),
            "distinct_documents": len({h["doc_id"] for h in hits})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dim", type=int, default=3072)
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--baseline-revision")
    parser.add_argument("--vector-reuse", action="store_true", help="Also compare the bounded sibling-vector prototype")
    args = parser.parse_args()
    if args.rows < 400 or args.dim < 8 or args.dim % 8 or min(args.pairs, args.iterations) < 1:
        parser.error("rows >= 400, dim divisible by 8, positive pairs/iterations required")
    baseline = store.Collection.search
    factories = {"baseline": baseline, "prototype": candidate_search()}
    vector_methods = {name: store.Collection._fp16_vecs for name in factories}
    if args.vector_reuse:
        from expansion_vector_probe import cached_fp16_vecs, vector_expansion
        factories["vectors"] = candidate_search(vector_expansion)
        vector_methods["vectors"] = cached_fp16_vecs
    top = {"siblings_topk": 3, "summary": True}
    all_rows = {"siblings_all": True, "summary": True}
    cases = {
        "clustered-k5-ranked": ("clustered", {"mode": "vector", "k": 5, "expand": top}),
        "clustered-k10-ranked": ("clustered", {"mode": "vector", "k": 10, "expand": top}),
        "clustered-k10-summary": ("clustered", {"mode": "vector", "k": 10, "expand": {"summary": True}}),
        "clustered-k10-all": ("clustered", {"mode": "vector", "k": 10, "expand": all_rows}),
        "clustered-k100-all": ("clustered", {"mode": "vector", "k": 100, "expand": all_rows}),
        "dispersed-k10-ranked": ("dispersed", {"mode": "vector", "k": 10, "expand": top}),
        "dispersed-k10-all": ("dispersed", {"mode": "vector", "k": 10, "expand": all_rows}),
        "summary-hits-all": ("clustered", {"mode": "vector", "scope": "summaries", "k": 10, "expand": all_rows}),
        "text-k10-ranked": ("clustered", {"mode": "text", "k": 10, "expand": top}),
        "hybrid-k10-ranked": ("clustered", {"mode": "hybrid", "k": 10, "expand": top}),
        "clustered-k10-control": ("clustered", {"mode": "vector", "k": 10}),
    }
    runs, profiles = {}, {}
    with tempfile.TemporaryDirectory(prefix="raggio-expansion-") as directory:
        settings = Settings()
        settings.data_dir, settings.root_api_key = directory, "benchmark"
        centers = asyncio.run(prepare(settings, args.rows, args.dim))
        managers = []
        def capture_manager(*a, **kw):
            manager = store.CollectionManager(*a, **kw)
            managers.append(manager)
            return manager
        with patch.object(app_module, "CollectionManager", capture_manager), TestClient(create_app(settings)) as client:
            for label, (collection, case) in cases.items():
                bodies = []
                for i in range(args.iterations):
                    query = {"text": f"topic{i % 20}"} if case["mode"] != "vector" else {}
                    if case["mode"] != "text":
                        query["vector"] = np.round(centers[i % len(centers)], 5)
                    bodies.append(orjson.dumps({**case, "query": query}, option=orjson.OPT_SERIALIZE_NUMPY))
                for name, factory in factories.items():
                    with patch.object(store.Collection, "search", factory), patch.object(store.Collection, "_fp16_vecs", vector_methods[name]):
                        for raw in bodies[:3]:
                            post(client, collection, raw)
                rounds = []
                for pair in range(args.pairs):
                    samples = {name: [] for name in factories}
                    order = tuple(factories) if pair % 2 == 0 else tuple(factories)[::-1]
                    for i, raw in enumerate(bodies):
                        outputs = []
                        for name in (order if i % 2 == 0 else order[::-1]):
                            with patch.object(store.Collection, "search", factories[name]), patch.object(store.Collection, "_fp16_vecs", vector_methods[name]):
                                wall, cpu = time.perf_counter(), time.process_time()
                                response = post(client, collection, raw)
                                samples[name].append(((time.perf_counter()-wall)*1000, (time.process_time()-cpu)*1000))
                            outputs.append(response.content)
                        assert all(output == outputs[0] for output in outputs), (label, pair, i)
                    row = {name: {"endpoint_p50_ms": statistics.median(v[0] for v in values),
                                  "endpoint_p95_ms": float(np.percentile([v[0] for v in values], 95)),
                                  "cpu_per_request_ms": statistics.mean(v[1] for v in values)}
                           for name, values in samples.items()}
                    rounds.append(row)
                runs[label] = rounds
                async def profile():
                    col = await managers[-1].touch(collection)
                    profiles = {}
                    for name, factory in factories.items():
                        with patch.object(store.Collection, "_fp16_vecs", vector_methods[name]):
                            profiles[name] = await diagnostics(col, bodies[0], factory)
                    return profiles
                profiles[label] = client.portal.call(profile)
                print(json.dumps({"case": label, "last_round": rounds[-1], "profiles": profiles[label]}), flush=True)
    medians = {label: {name: {key: statistics.median(row[name][key] for row in rows)
                             for key in rows[0][name]} for name in factories} for label, rows in runs.items()}
    result = {"baseline_revision": args.baseline_revision,
              "store_source_sha256": hashlib.sha256(Path(store.__file__).read_bytes()).hexdigest(),
              "python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
              "versions": {name: importlib.metadata.version(name) for name in ("fastapi", "pydantic", "numpy", "turbovec")},
              "openblas_threads": os.environ.get("OPENBLAS_NUM_THREADS"),
              "rows_per_collection": args.rows, "dim": args.dim, "pairs": args.pairs, "iterations": args.iterations,
              "seed": 821, "transport": "ASGI TestClient", "same_response_bytes": True,
              "medians": medians, "runs": runs, "profiles": profiles}
    if args.out:
        args.out.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(medians, indent=2))


if __name__ == "__main__":
    main()
