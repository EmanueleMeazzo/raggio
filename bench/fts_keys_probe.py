"""Local fts_keys cost/compatibility probe; Sofia runs deployment acceptance separately.

Fresh subprocesses compare a supplied base checkout, current default, and current
fts_keys. Collection search timings include asyncio handoffs and hydration, without
HTTP. Admission uses in-process TestClient with workers paused, including client cost.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def worker(args):
    sys.path.insert(0, args.source)
    import numpy as np
    from fastapi.testclient import TestClient
    import raggio.app as api
    import raggio.store as store
    from raggio.config import Settings

    keys = ["title", "source", "tags"] if args.arm == "keys" else None
    rng = np.random.default_rng(1105)
    vocab = np.array([f"word{i}" for i in range(4096)])
    docs = []
    for d in range(args.docs):
        vectors = store._normalize(rng.standard_normal((7, args.dim), dtype=np.float32))
        meta = {"title": f"Report quasar{d} - Annex GW1.16 café Σ",
                "source": f"https://sharepoint.example.test/sites/legal/Reports/{d}/document.pdf",
                "tags": "legal, scientific, chunk-3, chunks-12", "group": d % 8}
        def record(i):
            return {"text": f"topic{d} scientific retrieval " + " ".join(rng.choice(vocab, args.words)),
                    "vector": vectors[i], "metadata": meta}
        docs.append({"doc_id": f"d{d}", "summary": record(0), "chunks": [
            {"id": f"c{d}-{i}", "position": i, **record(i + 1)} for i in range(6)]})

    async def run(directory):
        cfg = store.CollectionConfig("probe", args.dim, 4, None, None, None)
        if args.arm != "base":
            cfg.fts_keys = keys
        col = store.Collection(cfg, directory, lambda: None)
        try:
            begin, cpu = time.perf_counter(), time.process_time()
            for start in range(0, len(docs), 50):
                await col._process_job({"documents": docs[start:start + 50]})
            ingestion = {"records_per_s": len(docs) * 7 / (time.perf_counter() - begin),
                         "cpu_ms_per_record": (time.process_time() - cpu) * 1000 / (len(docs) * 7)}
            col.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            sizes = {"meta_db_bytes": (directory / "meta.db").stat().st_size,
                     "fts_bytes": col.db.execute("SELECT SUM(pgsize) FROM dbstat"
                        " WHERE name GLOB 'records_fts_*'").fetchone()[0]}
            shapes = [(mode, scoped) for mode in ("text", "hybrid") for scoped in (False, True)]
            digest = hashlib.sha256()
            results = {}
            for mode, scoped in shapes:
                name = mode + ("_document" if scoped else "_summaries")
                async def search(i):
                    d = (i * 37) % args.docs
                    text = f"topic{d} scientific retrieval"
                    vector = np.array([docs[d]["summary"]["vector"]], dtype=np.float32) if mode == "hybrid" else None
                    return await col.search(mode, vector, text, 5 if scoped else 10,
                        "chunks" if scoped else "summaries", {"doc_id": f"d{d}"} if scoped else None, None)
                for i in range(10):
                    await search(i)
                values = []
                cpu_values = []
                for i in range(args.queries):
                    start, cpu = time.perf_counter(), time.process_time()
                    hits = await search(i)
                    cpu_values.append((time.process_time() - cpu) * 1000)
                    values.append((time.perf_counter() - start) * 1000)
                    assert hits
                    digest.update(json.dumps(hits, sort_keys=True).encode())
                results[name] = {"p50_ms": statistics.median(values),
                                 "p95_ms": float(np.percentile(values, 95)),
                                 "cpu_ms_per_query": statistics.mean(cpu_values)}
            title_hits = 0
            for d in range(20):
                hits = await col.search("text", None, f"quasar{d}", 3, "summaries", None, None)
                title_hits += any(h["doc_id"] == f"d{d}" for h in hits)
            control = {}
            if args.paired_control:
                # Hold data, connection/cache state, vector path and event loop fixed;
                # interleave only the default BM25 methods this feature changed.
                spec = importlib.util.spec_from_file_location("raggio._fts_reference",
                    Path(args.base_source) / "raggio/store.py")
                reference = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = reference
                spec.loader.exec_module(reference)
                check_dir = directory.parent / "old-schema"
                check_dir.mkdir()
                old_db = reference.open_meta_db(check_dir / "meta.db")
                schema_sql = "SELECT name, sql FROM sqlite_master ORDER BY name"
                assert old_db.execute(schema_sql).fetchall() == col.db.execute(schema_sql).fetchall()
                old_db.close()
                methods = {"base": (reference.Collection._text_ids.__get__(col),
                                    reference.Collection._avgdl.__get__(col)),
                           "default": (col._text_ids, col._avgdl)}
                for mode, scoped in shapes:
                    values = {arm: [] for arm in methods}
                    cpu_values = {arm: [] for arm in methods}
                    for i in range(args.queries):
                        d = (i * 37) % args.docs
                        vector = np.array([docs[d]["summary"]["vector"]], dtype=np.float32) if mode == "hybrid" else None
                        hits_by_arm = {}
                        for arm in (list(methods) if i % 2 else list(methods)[::-1]):
                            col._text_ids, col._avgdl = methods[arm]
                            begin, cpu = time.perf_counter(), time.process_time()
                            hits_by_arm[arm] = await col.search(mode, vector,
                                f"topic{d} scientific retrieval", 5 if scoped else 10,
                                "chunks" if scoped else "summaries",
                                {"doc_id": f"d{d}"} if scoped else None, None)
                            values[arm].append((time.perf_counter() - begin) * 1000)
                            cpu_values[arm].append((time.process_time() - cpu) * 1000)
                        assert hits_by_arm["base"] == hits_by_arm["default"]
                    control[mode + ("_document" if scoped else "_summaries")] = {
                        arm: {"p50_ms": statistics.median(values[arm]),
                              "p95_ms": float(np.percentile(values[arm], 95)),
                              "cpu_ms": statistics.mean(cpu_values[arm])} for arm in methods}
                col._text_ids, col._avgdl = methods["default"]
            col.db.execute("INSERT INTO records_fts(records_fts, rank) VALUES('integrity-check',1)")
            col.db.commit()
            return {"ingest": ingestion, "size": sizes, "search": results,
                    "body_query_digest": digest.hexdigest(), "synthetic_title_top3": title_hits,
                    "paired_default_control": control}
        finally:
            await col.stop()

    with tempfile.TemporaryDirectory(prefix="raggio-fts-cost-") as tmp:
        directory = Path(tmp) / "collection"
        directory.mkdir()
        result = asyncio.run(run(directory))
        settings = Settings()
        settings.data_dir, settings.root_api_key = str(Path(tmp) / "admission"), "probe"
        headers = {"x-api-key": "probe", "content-type": "application/json"}
        # Reuse the same vector values and metadata; HTTP ingress expects JSON lists.
        import orjson
        raw = orjson.dumps({"documents": docs[:25]}, option=orjson.OPT_SERIALIZE_NUMPY)
        with patch.object(store.Collection, "start_worker", lambda self: None), TestClient(api.create_app(settings)) as c:
            create = {"name": "probe", "dim": args.dim}
            if args.arm != "base":
                create["fts_keys"] = keys
            c.post("/collections", headers=headers, json=create).raise_for_status()
            values = []
            for i in range(18):
                begin, cpu = time.perf_counter(), time.process_time()
                response = c.post("/collections/probe/documents", headers=headers, content=raw)
                elapsed = [(time.perf_counter() - begin) * 1000, (time.process_time() - cpu) * 1000]
                assert response.status_code == 202, response.text
                if i >= 3:
                    values.append(elapsed)
            result["admission"] = {"p50_ms": statistics.median(v[0] for v in values),
                                   "p95_ms": float(np.percentile([v[0] for v in values], 95)),
                                   "cpu_ms": statistics.mean(v[1] for v in values), "records": 175}
    result.update(arm=args.arm, max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  store_lf_sha256=hashlib.sha256(Path(store.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
                  api_lf_sha256=hashlib.sha256(Path(api.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
                  python=platform.python_version(), sqlite=store.sqlite3.sqlite_version,
                  native=store._native is not None)
    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-source", default="/baseline-src")
    p.add_argument("--source", default="/project/src")
    p.add_argument("--docs", type=int, default=1200)
    p.add_argument("--dim", type=int, default=256)
    p.add_argument("--words", type=int, default=80)
    p.add_argument("--queries", type=int, default=100)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--arm", choices=["base", "default", "keys"])
    p.add_argument("--paired-control", action="store_true")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.arm:
        if args.paired_control and args.arm != "default":
            p.error("paired-control requires the default arm")
        worker(args)
        return
    runs = []
    for rep in range(args.rounds):
        arms = ["base", "default", "keys"]
        if rep % 2:
            arms.reverse()
        run = {}
        for arm in arms:
            cmd = [sys.executable, __file__, "--arm", arm, "--source",
                   args.base_source if arm == "base" else args.source,
                   "--docs", str(args.docs), "--dim", str(args.dim), "--words", str(args.words),
                   "--queries", str(args.queries)]
            child = subprocess.run(cmd, capture_output=True, text=True)
            if child.returncode:
                raise RuntimeError(child.stderr)
            run[arm] = json.loads(child.stdout.splitlines()[-1])
            print(f"round {rep + 1} {arm}: ingest {run[arm]['ingest']['records_per_s']:.0f} records/s", flush=True)
        assert run["base"]["body_query_digest"] == run["default"]["body_query_digest"]
        assert run["keys"]["synthetic_title_top3"] == 20
        runs.append(run)
    result = {"protocol": "fresh subprocesses, reversed round order, seeded identical generated corpus; local filesystem",
              "base_revision": "a197726311296d77fd12d932be7a594a6c46c08a",
              "search_transport": "Collection.search with hydration and asyncio handoffs, no HTTP",
              "admission_transport": "in-process ASGI TestClient, client CPU included, workers paused",
              "docs": args.docs, "records": args.docs * 7, "dim": args.dim, "body_words": args.words,
              "queries_per_shape": args.queries, "rounds": args.rounds,
              "openblas_num_threads": os.environ.get("OPENBLAS_NUM_THREADS"), "runs": runs}
    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
