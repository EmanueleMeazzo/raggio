"""Reuse-or-ingest decisions for bench/bench.py.

At 2.5M rows a benchmark run reuses the vectors already in the engine volume. A wrong
"no data" answer DELETEs that collection and re-ingests it (~31 min), so everything here
fails loudly instead of guessing: an engine that doesn't answer, a chunk count that
doesn't match the run, or a fingerprint that doesn't match aborts before anything is
touched. Stdlib + httpx only, so the tests import it without the bench dependency group.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

# the first GET after a container start opens the collection on the event loop (128 s
# on the DGX from a cold page cache at 2.5M rows) and stats() runs there too; the old
# 30 s read that as "no data". 900 s is what the 2026-09-28 baseline ran with.
STATE_TIMEOUT_S = 900.0
PENDING_POLL_S = 5.0     # between pending_jobs polls while the job queue drains
PENDING_WAIT_S = 3600.0  # abort if the queue is still busy after this long


class BenchAbort(RuntimeError):
    """The harness can't safely choose between reuse and ingest; nothing was changed."""


@dataclass(frozen=True)
class EngineState:
    chunks: int | None     # None: the collection (Weaviate: class) does not exist
    ivf: bool = False      # raggio: an IVF index is attached
    pending_jobs: int = 0  # raggio: pending + processing jobs


def fingerprint_path(fp_dir, key: str, limit: int) -> Path:
    """One file per engine data set and corpus size: a 553k email run and a 2.5M arXiv
    run never read each other's fingerprint."""
    return Path(fp_dir) / f"fingerprint-{key}-{limit}.json"


def read_fingerprint(path) -> dict | None:
    """The saved fingerprint; None if none was written, {} if the file is unreadable
    (an empty dict never equals a real fingerprint, so it reads as a mismatch)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        fp = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return fp if isinstance(fp, dict) else {}


def write_fingerprint(path, fp: dict) -> None:
    """Write via a temp file + rename so a crash never leaves a truncated fingerprint."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(fp, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _unreachable(url: str, e: Exception) -> BenchAbort:
    return BenchAbort(f"{url} unreachable ({type(e).__name__}: {e}). Nothing was ingested or "
                      f"deleted. If the engine is up but slow to open the collection, raise "
                      f"--state-timeout (default {STATE_TIMEOUT_S:.0f}s).")


def _collection_names(r: httpx.Response) -> list | None:
    if r.status_code != 200:
        return None
    try:
        names = r.json()["collections"]
    except (ValueError, KeyError, TypeError):
        return None
    return names if isinstance(names, list) else None


async def raggio_state(client: httpx.AsyncClient, base: str, coll: str) -> EngineState:
    """What the raggio engine holds for `coll`. A 404 only counts as "absent" when the
    root collection list agrees; anything else that isn't a clean answer aborts."""
    url = f"{base}/collections/{coll}"
    try:
        r = await client.get(url)
        listing = await client.get(f"{base}/collections") if r.status_code == 404 else None
    except httpx.HTTPError as e:
        raise _unreachable(url, e) from e
    if listing is not None:
        names = _collection_names(listing)
        if names is not None and coll not in names:
            return EngineState(chunks=None)
        raise BenchAbort(f"GET {url} answered 404 but GET {base}/collections "
                         f"(HTTP {listing.status_code}) does not confirm '{coll}' is absent")
    if r.status_code != 200:
        raise BenchAbort(f"GET {url} -> HTTP {r.status_code}: {r.text[:200]}")
    try:
        info = r.json()
        return EngineState(chunks=int(info["chunks"]),
                           ivf=(info.get("index") or {}).get("type") == "ivf",
                           pending_jobs=int(info.get("pending_jobs", 0)))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise BenchAbort(f"GET {url}: unexpected body {r.text[:200]!r}") from e


async def weaviate_state(client: httpx.AsyncClient, base: str, wclass: str) -> EngineState:
    """Object count of `wclass`; None when the class does not exist."""
    url = f"{base}/v1/schema/{wclass}"
    agg = {"query": f"{{Aggregate {{{wclass} {{meta {{count}}}}}}}}"}
    try:
        r = await client.get(url)
        if r.status_code == 404:
            return EngineState(chunks=None)
        if r.status_code != 200:
            raise BenchAbort(f"GET {url} -> HTTP {r.status_code}: {r.text[:200]}")
        a = await client.post(f"{base}/v1/graphql", json=agg)
    except httpx.HTTPError as e:
        raise _unreachable(url, e) from e
    try:
        body = a.json()
    except ValueError as e:
        raise BenchAbort(f"weaviate aggregate: HTTP {a.status_code}, unexpected body {a.text[:200]!r}") from e
    if a.status_code != 200 or not isinstance(body, dict) or body.get("errors"):
        # weaviate answers HTTP 200 for graphql errors (e.g. a shard still loading)
        raise BenchAbort(f"weaviate aggregate failed: HTTP {a.status_code} {a.text[:300]}")
    try:
        return EngineState(chunks=int(body["data"]["Aggregate"][wclass][0]["meta"]["count"]))
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise BenchAbort(f"weaviate aggregate: unexpected body {a.text[:200]!r}") from e


async def wait_idle(client: httpx.AsyncClient, base: str, coll: str, *,
                    poll_s: float = PENDING_POLL_S, max_wait_s: float = PENDING_WAIT_S) -> EngineState:
    """raggio state once its job queue is empty: a half-applied ingest or index build
    must neither be mistaken for the corpus nor be measured."""
    t0 = time.monotonic()
    while True:
        st = await raggio_state(client, base, coll)
        if st.chunks is None or st.pending_jobs == 0:
            return st
        waited = time.monotonic() - t0
        if waited >= max_wait_s:
            raise BenchAbort(f"{coll}: {st.pending_jobs} jobs still pending after {waited:.0f}s")
        print(f"  {coll}: {st.pending_jobs} jobs pending, waiting ({waited:.0f}s)")
        await asyncio.sleep(poll_s)


def decide(name: str, state: EngineState, expected: int, saved_fp: dict | None,
           current_fp: dict, *, reingest: bool, adopt: bool, limit: int,
           fp_path="fingerprint") -> str:
    """'reuse', 'build-index' (raggio-ivf: keep the data, build the index) or 'ingest'
    (wipes the engine's copy). Raises BenchAbort when the engine holds data this run
    can't vouch for; only an empty engine or an explicit --reingest ever re-ingests.
    --adopt vouches for data that has no fingerprint on file, never against one."""
    if not state.chunks:  # absent or empty: nothing to lose
        return "ingest"
    counts_match = state.chunks == expected
    fp_ok = saved_fp == current_fp or (adopt and saved_fp is None)
    if reingest and name != "raggio-ivf":
        return "ingest"
    if reingest:  # raggio-ivf measures the index build: rebuild it on matching data only
        return "build-index" if counts_match and fp_ok else "ingest"
    if not counts_match:
        raise BenchAbort(
            f"[{name}] the engine holds {state.chunks:,} chunks but this run expects {expected:,} "
            f"(--limit {limit} minus the held-out queries). --limit is the corpus size, not the "
            f"ingested count, and a wrong value also changes the query set: fix --limit, or pass "
            f"--reingest to wipe and re-ingest (~31 min at 2.5M rows).")
    if not fp_ok and saved_fp is None:
        raise BenchAbort(
            f"[{name}] the chunk count matches but there is no fingerprint at {fp_path}. Pass "
            f"--adopt if the engine was ingested from this corpus, seed and query count (e.g. a "
            f"volume carried over from another checkout), or --reingest to rebuild it.")
    if not fp_ok:
        raise BenchAbort(
            f"[{name}] the chunk count matches but {fp_path} records another run (saved "
            f"{saved_fp}, this run {current_fp}), so the engine may hold other data. Pass "
            f"--reingest to rebuild it; if you know it holds this corpus, delete {fp_path} "
            f"and pass --adopt.")
    if name == "raggio-ivf" and not state.ivf:
        return "build-index"
    return "reuse"


async def prepare_engine(name: str, *, state, ingest, build_index=None, fp_path, expected: int,
                         fp_now: dict, reingest: bool, adopt: bool, limit: int) -> dict:
    """Decide, then reuse / ingest / build the index. `state()` -> EngineState,
    `ingest()` -> (seconds, chunk count), `build_index()` -> seconds. Returns the timings
    to merge into the results: ingest_s + ingest_vps, or index_build_s for raggio-ivf."""
    st = await state()
    saved = read_fingerprint(fp_path)
    action = decide(name, st, expected, saved, fp_now,
                    reingest=reingest, adopt=adopt, limit=limit, fp_path=fp_path)
    res = {}
    if action == "ingest":
        print(f"[{name}] ingest {expected} vectors...")
        elapsed, count = await ingest()
        if count != expected:
            raise BenchAbort(f"[{name}] ingested {count} chunks, expected {expected}")
        write_fingerprint(fp_path, fp_now)
        if build_index is None:
            res.update(ingest_s=elapsed, ingest_vps=expected / elapsed)
    else:
        if adopt and saved is None:
            write_fingerprint(fp_path, fp_now)
            print(f"[{name}] adopted the engine's {st.chunks} chunks as this corpus ({fp_path})")
        print(f"[{name}] reusing {expected} ingested chunks"
              + (", building the IVF index" if action == "build-index" else " (pass --reingest to rebuild)"))
    if action != "reuse" and build_index is not None:
        res["index_build_s"] = await build_index()
    return res
