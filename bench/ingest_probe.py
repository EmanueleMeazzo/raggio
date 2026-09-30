"""In-process ingest drain benchmark for the flat and IVF paths.

bench.py measures flat ingest end to end over HTTP (about 31 min for 2.55M rows) and
never measures IVF ingest: `--engine raggio-ivf --reingest` only rebuilds the index.
This probe drives one Collection in-process instead:

1. prefill --prefill rows through Collection._process_job in 2000-row payloads (a
   large prefill calibrates at CAL_THRESHOLD, like a real collection); with --ivf N,
   attach an IVF index of nlist N and run one index sync, so the full write that
   follows an attach is not timed;
2. start the worker and let --enqueuers tasks enqueue --jobs payloads of --job-rows
   rows each (IngestIn.model_dump() shape, vectors rounded to 5 decimals as in the
   bench) while the worker drains them. Every --reupsert-every-th job replaces
   existing rows (the removal path). The clock stops at pending_jobs() == 0;
3. report ingest_vps = jobs * job_rows / drain_s, the event-loop lag seen by a 5 ms
   ticker, the job statuses, the freelist and a fingerprint of records, vecs, counts
   and index size. The same arguments give the same fingerprint on every correct
   tree. IVF search results are not fingerprinted: two builds train differently.

It puts its own tree's src/ first on sys.path and uses only APIs that main had before
the binary job journal, so a copy dropped into an older checkout measures that
checkout's code (store_file in the result says which). The rows come from an uncapped
host process on a warm page cache: label them so, never as container rows. The timed
payloads are built before the clock starts: about 1.7 GB of Python floats at the
defaults.

Usage: uv run python bench/ingest_probe.py [--ivf 256] [--tmp DIR] [--out FILE]
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # this tree's raggio, not an installed copy
import raggio.store as store  # noqa: E402
from raggio.store import Collection, CollectionConfig  # noqa: E402

PREFILL_CHUNK = 2_000  # rows per prefill payload
WORDS = [f"w{i}" for i in range(30_000)]  # zipf-drawn vocabulary of the chunk texts
WORDS_PER_ROW = 250
TICK_S = 0.005  # period of the event-loop lag ticker
STALL_S = 0.020  # a tick later than this counts toward loop_stall_s
POLL_S = 0.01  # pending_jobs() poll period while the queue drains


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="In-process ingest drain benchmark (flat or IVF).")
    p.add_argument("--dim", type=int, default=1024)
    p.add_argument("--prefill", type=int, default=100_000, help="rows ingested before the clock starts")
    p.add_argument("--jobs", type=int, default=200, help="timed ingest jobs")
    p.add_argument("--job-rows", type=int, default=250, help="rows per timed job")
    p.add_argument("--reupsert-every", type=int, default=5,
                   help="every Nth job replaces existing rows (0 = never)")
    p.add_argument("--enqueuers", type=int, default=2, help="concurrent enqueue tasks")
    p.add_argument("--ivf", type=int, default=0, help="attach an IVF index of this nlist (0 = flat)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--tmp", default=None,
                   help="parent dir of the throwaway collection (default: the system temp"
                        " dir); put it on the disk under test")
    p.add_argument("--out", default=None, help="also write the result JSON to this file")
    return p


def make_payload(ids: range, dim: int, tag: str, rng: np.random.Generator) -> dict:
    """One bench-shaped ingest payload in IngestIn.model_dump() field order: per id a
    document arxiv/<id> with one chunk r<id>, a zipf text of WORDS_PER_ROW words and
    a float vector rounded to 5 decimals."""
    vecs = np.round(rng.standard_normal((len(ids), dim)) / np.sqrt(dim), 5)
    words = np.minimum(rng.zipf(1.2, size=(len(ids), WORDS_PER_ROW)), len(WORDS) - 1).tolist()
    return {"documents": [
        {"doc_id": f"arxiv/{i}", "summary": None, "chunks": [{
            "text": tag + " " + " ".join(WORDS[w] for w in ws),
            "vector": v.tolist(), "metadata": {"year": 2019 + i % 5},
            "id": f"r{i}", "position": None,
        }]}
        for i, v, ws in zip(ids, vecs, words)
    ]}


def make_jobs(args: argparse.Namespace) -> list[dict]:
    """The timed payloads. Job j adds the fresh rows prefill + j*job_rows onwards,
    except every reupsert_every-th job, which replaces prefill block m = j //
    reupsert_every. The blocks never overlap, so the final state does not depend on
    the order in which concurrent enqueuers land their jobs."""
    jobs = []
    for j in range(args.jobs):
        rng = np.random.default_rng([args.seed, 2, j])
        if args.reupsert_every and j % args.reupsert_every == 0:
            start, tag = j // args.reupsert_every * args.job_rows, "re"
        else:
            start, tag = args.prefill + j * args.job_rows, "new"
        jobs.append(make_payload(range(start, start + args.job_rows), args.dim, tag, rng))
    return jobs


async def timed_drain(col: Collection, payloads: list[dict], enqueuers: int) -> tuple[float, float, list[float]]:
    """Start the worker, enqueue the payloads from `enqueuers` tasks (round-robin
    split) and wait for an empty queue. Returns (drain_s, enqueue_s, lags): the time
    from the worker start to pending_jobs() == 0 and to the last enqueue returning,
    and how late each 5 ms tick of a loop ticker woke up, in seconds."""
    lags: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        while not done.is_set():
            t = time.perf_counter()
            await asyncio.sleep(TICK_S)
            # asyncio may wake a timer up to one clock resolution early: clamp at 0
            lags.append(max(0.0, time.perf_counter() - t - TICK_S))

    tick = asyncio.create_task(ticker())
    col.start_worker()
    t0 = time.perf_counter()
    enqueued: list[float] = []

    async def enqueuer(part: list[dict]) -> None:
        for payload in part:
            await col.enqueue(payload)
        enqueued.append(time.perf_counter() - t0)

    try:
        await asyncio.gather(*(enqueuer(payloads[i::enqueuers]) for i in range(enqueuers)))
        while col.pending_jobs():
            if col._worker.done():
                col._worker.result()  # re-raises whatever killed the worker
                raise RuntimeError(f"the worker exited with {col.pending_jobs()} jobs open")
            await asyncio.sleep(POLL_S)
        drain_s = time.perf_counter() - t0
    finally:
        done.set()
        await tick
    return drain_s, max(enqueued), lags


def final_state(col: Collection) -> dict:
    """Job statuses, freelist, leftover payload rows and the state fingerprint. Read on
    the write connection under db_lock: pending_jobs() can read 0 on a read connection
    while the last finish is still vacuuming in the worker's thread."""
    with col.db_lock:
        db = col.db
        jobs = dict(db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        freelist = db.execute("PRAGMA freelist_count").fetchone()[0]
        side_table = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='job_payloads'"
        ).fetchone()[0]
        payload_rows = (  # None in a tree from before the job_payloads table
            db.execute("SELECT COUNT(*) FROM job_payloads").fetchone()[0] if side_table else None
        )
        # keyed by external_id, never by rowid: rowids depend on the job order
        records = hashlib.sha256()
        for row in db.execute(
            "SELECT external_id, doc_id, type, position, text, metadata, indexed"
            " FROM records ORDER BY external_id"
        ):
            records.update(repr(row).encode())
        vecs = hashlib.sha256()
        for ext, blob in db.execute(
            "SELECT r.external_id, v.vec FROM records r JOIN vecs v ON v.id = r.id"
            " ORDER BY r.external_id"
        ):
            vecs.update(ext.encode() + b"\0" + blob)
        fingerprint = {
            "records": records.hexdigest()[:16],
            "vecs": vecs.hexdigest()[:16],
            "counts": dict(sorted(col.indexed_counts.items())),
            "n_index": len(col.index),
        }
    return {"jobs": jobs, "freelist": freelist, "payload_rows": payload_rows,
            "fingerprint": fingerprint}


async def run(args: argparse.Namespace, work: Path) -> dict:
    """Prefill, optionally attach IVF, then the timed drain; the result without args."""
    col = Collection(CollectionConfig("probe", args.dim, 4, None, None, None), work, lambda: None)
    try:
        col._cal_rng = np.random.default_rng([args.seed, 3])  # seeded calibration sample
        t = time.perf_counter()
        for s in range(0, args.prefill, PREFILL_CHUNK):
            ids = range(s, min(s + PREFILL_CHUNK, args.prefill))
            rng = np.random.default_rng([args.seed, 1, s])
            await col._process_job(make_payload(ids, args.dim, "pre", rng))
        if args.ivf:
            await col._process_job({"op": "attach_index", "nlist": args.ivf, "nprobe": None})
            if col.index_info()["type"] != "ivf":
                raise RuntimeError(f"attach_index nlist={args.ivf} left the index flat")
            # the first sync after an attach writes every shard in full: not timed
            await asyncio.to_thread(col._sync_index)
        prefill_s = time.perf_counter() - t
        payloads = make_jobs(args)
        drain_s, enqueue_s, lags = await timed_drain(col, payloads, args.enqueuers)
        state = await asyncio.to_thread(final_state, col)
        index = col.index_info()
    finally:
        await col.stop()
    rows = args.jobs * args.job_rows
    return {
        "ingest_vps": rows / drain_s,
        "drain_s": drain_s,
        "enqueue_s": enqueue_s,
        "prefill_s": prefill_s,
        "loop_stall_s": sum(x for x in lags if x > STALL_S),
        "loop_lag_max_ms": max(lags, default=0.0) * 1000,
        "rows": rows,
        "index": index,
        **state,
        "store_file": store.__file__,
        "sqlite_version": sqlite3.sqlite_version,
        "turbovec": _version("turbovec"),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "openblas_num_threads": os.environ.get("OPENBLAS_NUM_THREADS"),
        "regime": "host-warm, uncapped host process",
    }


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def main(argv: list[str] | None = None) -> dict:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name in ("dim", "jobs", "job_rows", "enqueuers"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be >= 1")
    for name in ("prefill", "reupsert_every", "ivf", "seed"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must be >= 0")
    if args.reupsert_every:
        blocks = -(-args.jobs // args.reupsert_every)  # re-upsert jobs 0, k, 2k, ...
        if blocks * args.job_rows > args.prefill:
            parser.error(f"{blocks} re-upsert jobs of {args.job_rows} rows need"
                         f" --prefill >= {blocks * args.job_rows}")
    work = Path(tempfile.mkdtemp(prefix="ingest-probe-", dir=args.tmp))
    try:
        result = asyncio.run(run(args, work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            print(f"warning: could not remove {work}", file=sys.stderr)
    result["args"] = dict(vars(args))
    print(json.dumps(result, sort_keys=True), flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    main()
