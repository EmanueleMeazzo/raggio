"""Isolated dataset, HTTP server and offline diagnostics for sibling reevaluation."""
import argparse
import asyncio
from collections import Counter
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import sys
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))
import numpy as np
import raggio.app as app_module
import raggio.store as store
from raggio.config import Settings
from _sibling_reference import BASELINE_REVISION, BASELINE_STORE_SHA256, METHODS

KEY = "sibling-evaluation"
COLLECTION = "fixture"


def select_arm(arm):
    if arm == "baseline":
        for name, method in METHODS.items():
            setattr(store.Collection, name, method)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def settings(path):
    s = Settings()
    s.data_dir, s.root_api_key = str(path), KEY
    return s


def cases(query_sets):
    definitions = [
        ("doc-vector-k5-ranked", "indoc", "vector", "chunks", 5, True, "ranked"),
        ("doc-vector-k10-ranked", "indoc", "vector", "chunks", 10, True, "ranked"),
        ("global-vector-k10-ranked", "summary", "vector", "chunks", 10, False, "ranked"),
        ("global-hybrid-k10-ranked", "summary", "hybrid", "chunks", 10, False, "ranked"),
        ("ranked-summary-control", "summary", "vector", "summaries", 10, False, "ranked"),
        ("sofia-summary", "summary", "hybrid", "summaries", 10, False, None),
        ("sofia-doc-vector", "indoc", "vector", "chunks", 5, True, None),
        ("sofia-doc-hybrid", "indoc", "hybrid", "chunks", 5, True, None),
        ("all-siblings-control", "indoc", "hybrid", "chunks", 10, True, "all"),
        ("summary-only-control", "summary", "vector", "chunks", 10, False, "summary"),
    ]
    out = {}
    for name, query_set, mode, scope, k, by_doc, expansion in definitions:
        bodies = []
        for q in query_sets[query_set]:
            body = {"query": {"vector": q["vector"]}, "mode": mode, "scope": scope, "k": k}
            if mode == "hybrid":
                body["query"]["text"] = q["text"]
            if by_doc:
                body["filter"] = {"doc_id": str(q["doc"])}
            if expansion:
                body["expand"] = {"summary": True}
                if expansion == "ranked":
                    body["expand"]["siblings_topk"] = 3
                elif expansion == "all":
                    body["expand"]["siblings_all"] = True
            bodies.append(body)
        if not bodies:
            raise ValueError(f"no queries for {name}")
        out[name] = bodies
    return out


async def prepare(args):
    target = Path(args.output)
    if (target / "catalog.db").exists():
        raise ValueError("refusing to replace an existing dataset")
    manager = store.CollectionManager(settings(target), lambda cfg: None)
    if args.source == "wiki":
        corpus = Path(args.corpus)
        metadata = json.loads((corpus / "meta.json").read_text())
        if not metadata.get("embedded"):
            raise ValueError("Wikipedia corpus is not embedded")
        vectors = np.load(corpus / "vecs.npy", mmap_mode="r")
        dim = vectors.shape[1]
    else:
        dim = args.dim
    await manager.create_collection(COLLECTION, dim, 4, None, None, None, fts_keys=["title"])
    col = await manager.touch(COLLECTION)
    selected, batch = set(), []
    query_sets = {"summary": [], "indoc": []}
    try:
        if args.source == "wiki":
            current = None
            with (corpus / "records.jsonl").open("rb") as stream:
                for row, line in enumerate(stream):
                    record = json.loads(line)
                    doc_id = str(record["doc"])
                    if current is None or current["doc_id"] != doc_id:
                        if current is not None:
                            batch.append(current)
                        if len(selected) >= args.docs:
                            break
                        selected.add(doc_id)
                        current = {"doc_id": doc_id, "chunks": []}
                    value = {"text": record["text"], "vector": vectors[row].tolist(),
                             "metadata": {"title": record["title"], "doc": doc_id}}
                    if record["kind"] == "summary":
                        current["summary"] = value
                    else:
                        current["chunks"].append({**value, "id": str(record["id"]),
                                                  "position": len(current["chunks"])})
                    if len(batch) >= 100:
                        await col._process_job({"documents": batch})
                        batch = []
                else:
                    if current is not None:
                        batch.append(current)
            if batch:
                await col._process_job({"documents": batch})
            query_vectors = np.load(corpus / "qvecs.npy", mmap_mode="r")
            with (corpus / "queries.jsonl").open("rb") as stream:
                for row, line in enumerate(stream):
                    q = json.loads(line)
                    kind = q["set"]
                    if str(q["doc"]) in selected and len(query_sets[kind]) < 16:
                        query_sets[kind].append({"doc": str(q["doc"]), "text": q["text"],
                                                "vector": np.round(query_vectors[row], 5).tolist()})
        else:
            rng = np.random.default_rng(821)
            centers = rng.standard_normal((args.docs, dim), dtype=np.float32)
            query_centers = store._normalize(centers)
            for d in range(args.docs):
                doc_id = f"d{d}"
                selected.add(doc_id)
                vecs = store._normalize(centers[d] + .35 * rng.standard_normal((20, dim), dtype=np.float32))
                text = f"scientific retrieval topic{d} café Σ漢字 " * 12
                batch.append({"doc_id": doc_id,
                    "summary": {"text": f"Summary topic{d}", "vector": query_centers[d].tolist(),
                                "metadata": {"title": f"topic{d}"}},
                    "chunks": [{"id": f"d{d}c{j}", "position": j, "text": text,
                                "vector": v.tolist(), "metadata": {"title": f"topic{d}"}}
                               for j, v in enumerate(vecs)]})
                if d < 16:
                    query_sets["summary"].append({"doc": doc_id, "text": f"topic{d} scientific retrieval",
                                                  "vector": np.round(query_centers[d], 5).tolist()})
                    query_sets["indoc"].append({"doc": doc_id, "text": f"topic{d} scientific retrieval",
                                                "vector": np.round(vecs[0], 5).tolist()})
                if len(batch) >= 100:
                    await col._process_job({"documents": batch})
                    batch = []
            if batch:
                await col._process_job({"documents": batch})
        stats = col.stats()
    finally:
        await manager.shutdown()
    requests = cases(query_sets)
    manifest = {"source": args.source, "docs": len(selected), "dim": dim, "stats": stats,
                "seed": 821, "fts_keys": ["title"], "cases": requests}
    manifest["queries_sha256"] = hashlib.sha256(json.dumps(requests, sort_keys=True).encode()).hexdigest()
    write_json(target.parent / (target.name + "-queries.json"), manifest)
    print(json.dumps({k: v for k, v in manifest.items() if k != "cases"}), flush=True)


def facts(arm):
    return {"arm": arm, "baseline_revision": BASELINE_REVISION,
            "baseline_store_sha256": BASELINE_STORE_SHA256,
            "candidate_store_sha256": hashlib.sha256(Path(store.__file__).read_text(encoding="utf-8").encode()).hexdigest(),
            "executed_methods_sha256": {name: hashlib.sha256(inspect.getsource(getattr(store.Collection, name)).encode()).hexdigest()
                                         for name in METHODS},
            "python": sys.version, "platform": platform.platform(), "sqlite": store.sqlite3.sqlite_version,
            "versions": {name: importlib.metadata.version(name) for name in
                         ("raggio", "turbovec", "numpy", "httpx", "fastapi", "raggio-native")},
            "native_bm25": store._native is not None, "openblas_threads": os.environ.get("OPENBLAS_NUM_THREADS")}


def profile(args):
    from fastapi.testclient import TestClient
    captured = []
    trace = []
    active = [False]
    connect = store.connect_db
    manager_type = app_module.CollectionManager

    def traced_connect(*a, **kw):
        db = connect(*a, **kw)
        db.set_trace_callback(lambda sql: trace.append(sql) if active[0] else None)
        return db

    def capture_manager(*a, **kw):
        manager = manager_type(*a, **kw)
        captured.append(manager)
        return manager

    manifest = json.loads(Path(args.queries).read_text())
    output = {"facts": facts(args.arm), "cases": {}}
    with patch.object(store, "connect_db", traced_connect), patch.object(app_module, "CollectionManager", capture_manager):
        with TestClient(app_module.create_app(settings(args.data_dir))) as client:
            headers = {"x-api-key": KEY}
            for name, bodies in manifest["cases"].items():
                body = bodies[0]
                for _ in range(3):
                    client.post(f"/collections/{COLLECTION}/search", headers=headers, json=body).raise_for_status()
                trace.clear()
                active[0] = True
                response = client.post(f"/collections/{COLLECTION}/search", headers=headers, json=body)
                response.raise_for_status()
                active[0] = False
                counts = Counter("vector_blobs" if "FROM VECS WHERE ID IN" in sql.upper() else
                                 "document_membership" if "SELECT ID FROM RECORDS WHERE DOC_ID" in sql.upper() else
                                 "other_select" for sql in trace if sql.lstrip().upper().startswith("SELECT"))
                tracemalloc.start()
                client.post(f"/collections/{COLLECTION}/search", headers=headers, json=body).raise_for_status()
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                hits = response.json()["hits"]
                output["cases"][name] = {"sql": dict(counts), "python_peak_bytes": peak,
                    "response_sha256": hashlib.sha256(response.content).hexdigest(),
                    "hits": len(hits), "distinct_documents": len({h["doc_id"] for h in hits})}
    write_json(args.output, output)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--source", choices=("synthetic", "wiki"), required=True)
    prep.add_argument("--output", required=True)
    prep.add_argument("--corpus")
    prep.add_argument("--docs", type=int, default=500)
    prep.add_argument("--dim", type=int, default=1024)
    for mode in ("serve", "profile"):
        command = sub.add_parser(mode)
        command.add_argument("--arm", choices=("baseline", "candidate"), required=True)
        command.add_argument("--data-dir", required=True)
        command.add_argument("--output", required=True)
        if mode == "serve":
            command.add_argument("--port", type=int, default=8000)
        else:
            command.add_argument("--queries", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        asyncio.run(prepare(args))
    else:
        select_arm(args.arm)
        if args.command == "profile":
            profile(args)
        else:
            import uvicorn
            if store._native is None:
                raise RuntimeError("native scorer required")
            write_json(args.output, facts(args.arm))
            uvicorn.run(app_module.create_app(settings(args.data_dir)), host="0.0.0.0", port=args.port,
                        log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
