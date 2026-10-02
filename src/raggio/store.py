import asyncio
import ctypes
import hashlib
import itertools
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import struct
import sys
import threading
import time
import unicodedata
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from turbovec import IdMapIndex

from .config import Settings, default_ivf_search_threads
from .embeddings import Embedder


def _load_native():
    """The optional Rust stage-2 scorer (native/, ADR 0004; `uv sync --extra native`), or
    None. Its Unicode tables come from the interpreter it was built for: built against
    other Unicode data it would tokenize differently, so it is refused, not trusted."""
    try:
        import raggio_native
    except ImportError:
        return None
    if raggio_native.UNIDATA_VERSION != unicodedata.unidata_version:
        warnings.warn(
            f"raggio_native was built for Unicode {raggio_native.UNIDATA_VERSION}, this"
            f" interpreter has {unicodedata.unidata_version}: using the Python BM25 scorer",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
    return raggio_native


_native = _load_native()

RANGE_OPS = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}

FTS_TOKENIZERS = {
    "unicode61": "unicode61 remove_diacritics 2",  # language-neutral exact tokens
    "trigram": "trigram",  # substring matching; tokens < 3 chars never match
}

RRF_K = 60  # standard reciprocal-rank-fusion constant

HYBRID_DEPTH = 100  # candidates fetched per hybrid leg before fusion (Weaviate fuses 100-deep)

# fp16 rescore (ADR 0003): the quantized scan over-fetches, then candidates are re-ranked
# against the retained fp16 originals (vecs table), removing quantization ranking error.
# Measured on the 2.55M arXiv bench: the 4-bit top-20 already contains the exact top-10
# (recall@10 1.000), at ~0.1-0.3 ms/query. 2-bit codes are noisier: wider, unmeasured floor.
RESCORE_MULT = 2
RESCORE_FLOOR = {2: 200, 4: 50}
RESCORE_CAP = 2000  # bounds per-query blob fetches at huge k

# two-stage BM25 (ADR 0003): FTS5 cannot rank a full query cheaply (no WAND — ORDER BY
# rank scores every row matching ANY term) and must never rank a rowid-restricted MATCH
# (bm25()'s IDF is recomputed per row probe, ~400x slower). So when the pruner dropped
# tokens, stage 1 collects candidates at bounded cost (pruned-OR ranked + AND-of-all-tokens
# unranked) and stage 2 scores the FULL query in Python — restoring the ranking quality
# pruning used to destroy (bench text-hit@5 0.73 -> 0.975 measured, Weaviate parity).
TEXT_OR_CAND = 500
# ponytail: AND candidates are rowid-ordered, not ranked — an AND set >> cap (queries of
# only-common tokens) samples arbitrarily; stage 1a's rarest-token guarantee covers that class
TEXT_AND_CAND = 1000
SDM_WEIGHT = 0.2  # ordered-bigram proximity term (SDM-lite, Metzler & Croft 2005)
BM25_K1, BM25_B = 1.2, 0.75  # FTS5's hardcoded parameters (fts5_aux.c) — kept for parity

# cap on total FTS postings scored per query, as a fraction of indexed rows: query
# tokens are kept rarest-first until the budget is spent, so near-universal tokens
# (IDF ~0, huge posting lists — one such token forces a full-corpus rank pass) are
# dropped while every selective, meaning-bearing token survives. The row floor keeps
# small corpora untouched: short posting lists are cheap to score anyway.
FTS_SCAN_BUDGET = 0.02
FTS_SCAN_BUDGET_MIN_ROWS = 1000

# TQ+ calibration: one shot when a collection first crosses CAL_THRESHOLD indexed
# vectors, from a reservoir sample witnessed since birth. Measured on the bench
# corpus (bench/cal_probe.py): +0.8pt recall@10; milestone refits and calibrating
# a large already-ingested index both LOSE recall, so it's calibrate-early-or-never.
CAL_THRESHOLD = 10_000
CAL_SAMPLE = 1024  # ~1024 representative rows is enough per turbovec docs

# meta.db vacuum policy after a job finishes (Collection._vacuum_after_finish): a full
# incremental_vacuum only once no job is open; while a backlog drains, a freelist of
# VACUUM_FREELIST_PAGES or more (64 MB at 4 KiB pages) is trimmed back to
# VACUUM_FREELIST_PAGES - VACUUM_CHUNK_PAGES, so one trim returns about the pages freed
# since the last trim plus 8 MB and never stalls enqueues on a whole-file vacuum
VACUUM_FREELIST_PAGES = 16_384
VACUUM_CHUNK_PAGES = 2_048

# Optional ScaNN-style IVF index, attached/removed per collection via the index API.
# Measured (bench/ivf_probe.py, ADR 0002): at ~550k rows every recall-preserving cell
# is slower or barely faster than the flat scan (each probed shard is a single-core scan
# of its bytes, ADR 0005), so it stays opt-in; at 2.2M rows it wins 3.5-6.8x.
# nprobe=16 keeps recall@10 >=0.95 on the real corpus. Fixed RAM cost is ~0.5-1 MB per
# shard (bench/shard_mem_probe.py).
IVF_DEFAULT_NPROBE = 16
IVF_MIN_ROWS = 1024  # k-means needs a training corpus; below this, attach is refused
IVF_TRAIN_SAMPLE = 65_536
IVF_BUILD_BLOCK = 16_384  # rows per streamed rebuild block (~100 MB f32 at 1536-d)

# one-time meta.db index migration (D9): heartbeat cadence of the progress log, and
# how many SQLite VM instructions run between progress-handler checks
MIGRATION_LOG_EVERY_S = 10.0
MIGRATION_PROGRESS_OPS = 1_000_000

# _vec_sample (k-means training / calibration sample): ids are MAX+1-allocated, so
# random rowids hit live rows at rate n/max_id. Point-read those while that rate is at
# least VEC_SAMPLE_MIN_DENSITY; mass deletes below it fall back to ORDER BY RANDOM()
VEC_SAMPLE_MIN_DENSITY = 0.2
VEC_SAMPLE_ROUNDS = 8

# attach headroom (D13): the summed transient terms under-reserve. p1 measured 2176-2186
# MiB of growth at 2.55M x 1024-d 4-bit (nlist 256) against 1757 MiB summed; x1.25
# covers it. The guard stays conservative; the swapless DGX run is the gate (§5.4)
ATTACH_NEED_FACTOR = 1.25

# uvicorn configures this logger with its stderr handler, so these lines reach
# `podman logs`; under pytest the records propagate to the root logger (caplog)
_log = logging.getLogger("uvicorn.error")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cgroup_mem_free() -> int | None:
    """Bytes left under the container memory cap, or None when uncapped/not Linux."""
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()  # cgroup v2
        if limit == "max":
            return None
        for ln in open("/proc/self/status"):
            if ln.startswith("VmRSS:"):
                return int(limit) - int(ln.split()[1]) * 1024
    except OSError:
        pass
    return None


def _require_headroom(need: int, what: str) -> None:
    """An index rebuild transiently holds a second copy of the codes (+ train sample).
    Refuse with a clean job error instead of letting the OOM killer take the container
    down — the job replays on boot, so an OOM here becomes a crash loop."""
    free = _cgroup_mem_free()
    if free is not None and free < need + 128 * 1024 * 1024:
        raise ValueError(
            f"{what} needs ~{(need + 128 * 1024 * 1024) >> 20} MB free memory,"
            f" container has ~{max(free, 0) >> 20} MB — raise the memory limit and retry"
        )


def _attach_need(n: int, dim: int, bit_width: int, nlist: int) -> int:
    """Bytes an IVF build adds on top of the loaded collection: a second copy of the
    codes, the f32 k-means sample, ~1 MB fixed per shard, scaled to the measured
    growth by ATTACH_NEED_FACTOR."""
    return int(
        (n * dim * bit_width // 8 + min(n, IVF_TRAIN_SAMPLE) * dim * 4 + nlist * (1 << 20))
        * ATTACH_NEED_FACTOR
    )


def _malloc_trim() -> bool:
    """Hand freed heap back to the OS after an index job (D13). A build frees GBs of
    transient buffers (the second code copy, the f32 sample, streamed blocks), and
    glibc keeps them in its arenas, so without this the container's RSS stays at the
    build's peak until restart. glibc-only: a no-op returning False on Windows, macOS
    and musl."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        trim = ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):
        return False
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return bool(trim(0))


def _retry_fs(fn) -> None:
    """Windows: freshly written files/dirs keep transient handles (AV scan, indexer,
    async deletes) that fail renames spuriously; retry briefly, like turbovec's
    _persist does for its own sync renames."""
    for attempt in range(10):
        try:
            fn()
            return
        except OSError:
            if attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    if (norms == 0).any():
        raise ValueError("zero vector cannot be normalized")
    return (mat / norms).astype(np.float32)


# Binary job journal row (job_payloads.data): _JOB_HEADER (magic, JSON byte length,
# vector count, dim; little-endian, so aarch64 and x86 read the same bytes), then the
# payload as UTF-8 JSON with each supplied vector replaced by its row index, then the
# vectors as little-endian float32. float32 is what the worker indexes anyway, so no
# precision is lost, and the vectors skip a JSON round trip that costs ~100 ms per
# 250x1024 job. Stdlib + numpy only: runtime JSON stays stdlib.
_JOB_MAGIC = b"RGJ\x01"
_JOB_HEADER = struct.Struct("<4sIII")


def _encode_payload(payload: dict) -> bytes:
    """Encode a job payload for job_payloads. A summary's or chunk's non-None `vector`
    becomes an int row index into the float32 block; None stays None ("embed this").
    Index-op payloads carry no vectors (n_vecs = dim = 0). Copies whatever it rewrites,
    so the caller's dict is never mutated. Raises ValueError unless the vectors form one
    rectangular float matrix (the route checks dims, so only direct callers hit that)."""
    vectors: list = []

    def take(rec: dict) -> dict:
        if rec.get("vector") is None:
            return rec
        vectors.append(rec["vector"])
        return {**rec, "vector": len(vectors) - 1}

    docs = payload.get("documents")
    if docs is not None:
        out = []
        for d in docs:
            d = dict(d)
            if d.get("summary"):
                d["summary"] = take(d["summary"])
            if d.get("chunks"):
                d["chunks"] = [take(c) for c in d["chunks"]]
            out.append(d)
        payload = {**payload, "documents": out}
    try:
        mat = np.asarray(vectors, dtype="<f4") if vectors else np.empty((0, 0), dtype="<f4")
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"job payload vectors are not one rectangular float matrix: {e}"
        ) from None
    if mat.ndim != 2:
        raise ValueError(
            f"job payload vectors are not one rectangular float matrix: shape {mat.shape}"
        )
    meta = json.dumps(payload).encode("utf-8")
    n, dim = mat.shape
    return _JOB_HEADER.pack(_JOB_MAGIC, len(meta), n, dim) + meta + mat.tobytes()


def _decode_payload(data: bytes) -> dict:
    """Inverse of _encode_payload. Each vector comes back as a row of one read-only
    little-endian float32 matrix (views into `data`, no copy). Anything malformed
    raises ValueError: short or overlong data, bad magic, bad UTF-8 or JSON, a non-dict
    top level, a malformed document list, or a vector index out of range."""
    size = _JOB_HEADER.size
    if len(data) < size:
        raise ValueError(f"job payload truncated: {len(data)} bytes, the header alone is {size}")
    magic, json_len, n, dim = _JOB_HEADER.unpack_from(data)
    if magic != _JOB_MAGIC:
        raise ValueError(f"job payload has bad magic {bytes(magic)!r}")
    end = size + json_len
    if len(data) != end + 4 * n * dim:
        raise ValueError(
            f"job payload is {len(data)} bytes but its header says {end + 4 * n * dim}"
            " (truncated or corrupt)"
        )
    payload = json.loads(bytes(data[size:end]).decode("utf-8"))  # both errors are ValueErrors
    if not isinstance(payload, dict):
        raise ValueError(f"job payload is a JSON {type(payload).__name__}, not an object")
    if n * dim:
        mat = np.frombuffer(data, dtype="<f4", count=n * dim, offset=end).reshape(n, dim)
    else:
        mat = np.empty((n, dim), dtype="<f4")
    mat.flags.writeable = False  # also for bytearray input: rows are shared views

    def row(i) -> np.ndarray:
        if type(i) is not int or not 0 <= i < n:
            raise ValueError(f"job payload vector index {i!r} is outside 0..{n - 1}")
        return mat[i]

    try:
        for d in payload.get("documents") or ():
            s = d.get("summary")
            if s and s.get("vector") is not None:
                s["vector"] = row(s["vector"])
            for c in d.get("chunks") or ():
                if c.get("vector") is not None:
                    c["vector"] = row(c["vector"])
    except (AttributeError, TypeError) as e:
        raise ValueError(f"job payload has a malformed document list: {e}") from None
    return payload


# the worker's claim: the lowest open job. 'processing' is included so jobs interrupted
# by a crash replay on boot. Keep the literal predicate byte for byte: it is what Plan
# C's partial index idx_jobs_open matches (spec 4.1), and C's plan test pins this text
_CLAIM_SQL = (
    "SELECT id, payload FROM jobs WHERE status IN ('pending','processing') ORDER BY id LIMIT 1"
)


def _payload_rows(payload: dict) -> list[list]:
    """Flatten an ingest payload's documents into record rows [external_id, doc_id,
    type, position, text, metadata, vector]; vector None means "embed this". Rows are
    lists because the worker fills in embedded vectors. Pure CPU: _process_job runs it
    in a worker thread."""
    rows = []
    for d in payload["documents"]:
        if d.get("summary"):
            s = d["summary"]
            rows.append([d["doc_id"], d["doc_id"], "summary", None, s.get("text"), s.get("metadata"), s.get("vector")])
        for i, c in enumerate(d.get("chunks") or []):
            pos = c.get("position") if c.get("position") is not None else i
            rows.append([c["id"], d["doc_id"], "chunk", pos, c.get("text"), c.get("metadata"), c.get("vector")])
    # last occurrence wins: _upsert_rows inserts a job's rows with one executemany, so
    # a duplicate external_id within one payload would violate UNIQUE and fail the job
    # (and the first copy would only have been replaced by the second anyway)
    return list({r[0]: r for r in rows}.values())


def _rows_matrix(rows: list[list]) -> np.ndarray:
    """The rows' vectors as one unit-norm float32 matrix (ValueError on a zero vector).
    np.array over nested float lists is ~13 ms per 250x1024 job: _process_job runs it
    in a worker thread."""
    return _normalize(np.array([r[6] for r in rows], dtype=np.float32))


def _filter_sql(scope: str, filt: dict | None) -> tuple[str, list]:
    """Build WHERE clause for records: type scope + metadata filters, all ANDed.
    Per key: scalar = equality; list = `in`; object = range ops / `in` / `contains`."""
    clauses, params = ["indexed = 1"], []
    if scope == "chunks":
        clauses.append("type = 'chunk'")
    elif scope == "summaries":
        clauses.append("type = 'summary'")
    for key, val in (filt or {}).items():
        path = "$." + key
        ops = val.items() if isinstance(val, dict) else [("in" if isinstance(val, list) else "eq", val)]
        for op, bound in ops:
            if op == "eq":
                clauses.append("json_extract(metadata, ?) = ?")
                params.extend([path, bound])
            elif op in RANGE_OPS:
                clauses.append(f"json_extract(metadata, ?) {RANGE_OPS[op]} ?")
                params.extend([path, bound])
            elif op == "in":
                if not isinstance(bound, list) or not bound or any(isinstance(b, (list, dict)) for b in bound):
                    raise ValueError(f"filter '{key}': 'in' needs a non-empty list of scalars")
                clauses.append(f"json_extract(metadata, ?) IN ({','.join('?' * len(bound))})")
                params.extend([path, *bound])
            elif op == "contains":  # substring; SQLite lower() folds ASCII only
                clauses.append("instr(lower(json_extract(metadata, ?)), lower(?)) > 0")
                params.extend([path, str(bound)])
            else:
                raise ValueError(f"unsupported filter operator: {op}")
    return " AND ".join(clauses), params


def _rows_by_id(db, sql: str, ids: list[int]):
    """Point-fetch by id list: run `sql` (one {} slot for the qmarks) in chunks of
    512 ids, comfortably under SQLite's bound-parameter limit."""
    for s in range(0, len(ids), 512):
        chunk = ids[s : s + 512]
        yield from db.execute(sql.format(",".join("?" * len(chunk))), chunk)


def _live_ids(db) -> np.ndarray:
    """Every indexed record id as a uint64 array. The planner answers this from the
    covering idx_records_doc_type (pinned in tests), never the text-heavy records
    pages. The order is the index's (doc_id, type, id), not id order;
    np.setdiff1d(assume_unique=True) needs no sort, and both sides must be uint64 (an
    int64/uint64 mix promotes to float64 and loses ids above 2**53)."""
    return np.fromiter(
        itertools.chain.from_iterable(db.execute("SELECT id FROM records WHERE indexed=1")),
        dtype=np.uint64,
    )


def _fold(token: str) -> str:
    """Approximate the unicode61 remove_diacritics tokenizer's folding, so vocab
    doc-frequency lookups hit the stored term ('café' -> 'cafe'). A miss is fail-open
    (df 0, token kept), so imperfect approximation costs latency, never results."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", token.lower()) if not unicodedata.combining(c)
    )


# unicode61 treats '_' as a separator, unlike \w — matters for Python-side tf counting
_TOKEN_RE = re.compile(r"[^\W_]+")


def _fold_tokens(text: str) -> list[str]:
    """Fold + tokenize a whole string: one _fold pass over the text, then split."""
    return _TOKEN_RE.findall(_fold(text))


def _bm25_topn(
    qtoks: list[str],
    idf: dict[str, float],
    rids: list[int],
    texts: list[str | None],
    avgdl: float,
    n: int,
    k1: float,
    b: float,
    sdm_weight: float,
) -> tuple[list[int], list[float]]:
    """Top-n (ids, scores) of the full-query BM25 + SDM-lite score over the candidates
    (rids[i], texts[i]), ordered by (-score, rid). idf maps each distinct query token.
    The pure-Python reference: raggio_native.bm25_topn returns the same ids and the
    bit-identical scores (ADR 0004)."""
    if not qtoks or not texts:
        return [], []
    pairs = {(x, y) for x, y in zip(qtoks, qtoks[1:]) if x != y}
    scored = []
    for rid, text in zip(rids, texts):
        toks = _fold_tokens(text or "")
        dl = len(toks) or 1
        norm = k1 * (1 - b + b * dl / avgdl)
        tf: dict[str, int] = {}
        for t in toks:
            if t in idf:
                tf[t] = tf.get(t, 0) + 1
        s = sum(idf[t] * f * (k1 + 1) / (f + norm) for t, f in tf.items())
        if pairs:
            tf2: dict[tuple, int] = {}
            for pr in zip(toks, toks[1:]):
                if pr in pairs:
                    tf2[pr] = tf2.get(pr, 0) + 1
            s += sdm_weight * sum(
                (idf[x] + idf[y]) / 2 * f * (k1 + 1) / (f + norm)
                for (x, y), f in tf2.items()
            )
        scored.append((s, rid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:n]
    return [rid for _, rid in top], [s for s, _ in top]


def _or_query(tokens: list[str]) -> str:
    """Quoted tokens OR'd into FTS5 MATCH syntax (any term qualifies; ranking elsewhere)."""
    return " OR ".join(f'"{t}"' for t in tokens)


def _and_query(tokens: list[str]) -> str:
    """Quoted tokens AND'd: docs containing every term. FTS5 evaluates this by doclist
    intersection with rowid seeks, so cost tracks the rarest term even when the others
    are near-universal — cheap, high-precision candidate generation."""
    return " AND ".join(f'"{t}"' for t in tokens)


def _rrf(k: int, *rankings: list[int]) -> tuple[list[int], list[float]]:
    """Reciprocal Rank Fusion: score(id) = sum over rankings of 1/(RRF_K + rank)."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, rid in enumerate(ranking, start=1):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
    return [rid for rid, _ in top], [s for _, s in top]


class _RWLock:
    """asyncio readers-writer lock: many readers or one writer, writer-preferring
    (new readers wait once a writer is queued, so steady query traffic can't
    starve the ingest worker)."""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writing = False
        self._writers_waiting = 0

    @asynccontextmanager
    async def read(self):
        async with self._cond:
            while self._writing or self._writers_waiting:
                await self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @asynccontextmanager
    async def write(self):
        async with self._cond:
            self._writers_waiting += 1
            try:
                while self._writing or self._readers:
                    await self._cond.wait()
            finally:
                self._writers_waiting -= 1
                # a writer cancelled while waiting must wake the readers its presence
                # blocked, or they wait forever once no other holder remains to notify
                self._cond.notify_all()
            self._writing = True
        try:
            yield
        finally:
            async with self._cond:
                self._writing = False
                self._cond.notify_all()


@dataclass
class CollectionConfig:
    name: str
    dim: int
    bit_width: int
    model: str | None
    base_url: str | None
    key_hash: str | None
    tokenizer: str = "unicode61"
    index_config: dict | None = None  # {"nlist": N, "nprobe": M} when an IVF index is attached


def open_meta_db(path: Path, tokenizer: str = "unicode61") -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    # job payloads are bulky and transient: without this the file keeps every page the
    # ingest backlog ever occupied (a full-corpus ingest ballooned meta.db to 7+ GB).
    # MUST run before journal_mode=WAL — that pragma initializes the db file, and
    # auto_vacuum is a silent no-op once the file exists. Existing dbs need a one-time
    # `PRAGMA auto_vacuum=INCREMENTAL; VACUUM;` to activate.
    db.execute("PRAGMA auto_vacuum=INCREMENTAL")
    db.execute("PRAGMA journal_mode=WAL")
    # two write connections per collection (event-loop jobs + worker records): let the
    # loser of a write-lock race wait instead of surfacing SQLITE_BUSY
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS records(
            id INTEGER PRIMARY KEY,
            external_id TEXT UNIQUE,
            doc_id TEXT,
            type TEXT CHECK(type IN ('chunk','summary')),
            position INTEGER,
            text TEXT,
            metadata TEXT,
            indexed INTEGER DEFAULT 0
        );
        -- fp16 originals, disk-only: the only way to rebuild the index representation
        -- (IVF attach/detach), since turbovec can't reconstruct vectors. A separate
        -- table, NOT a records column: 3KB/row inline blobs would drag ~GBs through
        -- every records full-table scan (measured: cold start 2.2s -> 26s at 552k)
        CREATE TABLE IF NOT EXISTS vecs(
            id INTEGER PRIMARY KEY,
            vec BLOB
        );
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY,
            payload TEXT,
            status TEXT,
            error TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        -- binary job payloads (_encode_payload), one row per open or failed job. A side
        -- table, NOT jobs.payload: the claim's status UPDATE would otherwise rewrite the
        -- payload's whole overflow chain (MBs per job). jobs.payload stays TEXT: journals
        -- written before the binary job journal keep their JSON there and still replay.
        CREATE TABLE IF NOT EXISTS job_payloads(
            job_id INTEGER PRIMARY KEY,
            data BLOB
        );
        """
    )
    _ensure_indexes(db, str(path))
    _ensure_fts(db, tokenizer)
    # doc-frequency lookups for query-token pruning; references records_fts by name so
    # it survives an _ensure_fts rebuild
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts_v USING fts5vocab(records_fts, 'row')")
    return db


def _ensure_indexes(db: sqlite3.Connection, label: str) -> None:
    """Secondary indexes, plus the one-time migration from the pre-2026-09 layout (D9).
    idx_records_doc_type covers every doc_id/type/indexed scan (cold start's per-type
    counts and live-id set, stats(), the per-document lookups) without reading the
    records table's text pages, and replaces idx_records_doc, whose (doc_id) prefix it
    contains. Build first, drop second: a kill in between leaves both, and the next
    open drops the old one. idx_jobs_open is partial, so the open-job lookups stop
    scanning the whole job journal. Runs inside Collection() construction, which is
    off the event loop (CollectionManager._construct)."""
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if "idx_records_doc_type" not in have:
        rows = db.execute("SELECT MAX(rowid) FROM records").fetchone()[0]
        t0 = last = time.monotonic()
        if rows:
            _log.info(
                "meta.db %s: one-time migration, building idx_records_doc_type over"
                " ~%d records (~20 s per 1M rows cold)", label, rows,
            )

            def beat() -> int:
                nonlocal last
                now = time.monotonic()
                if now - last >= MIGRATION_LOG_EVERY_S:
                    last = now
                    _log.info(
                        "meta.db %s: still building idx_records_doc_type (%.0f s)", label, now - t0
                    )
                return 0  # non-zero would abort the CREATE INDEX

            db.set_progress_handler(beat, MIGRATION_PROGRESS_OPS)
        try:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_doc_type ON records(doc_id, type, indexed)"
            )
        finally:
            db.set_progress_handler(None, 0)
        if rows:
            _log.info(
                "meta.db %s: built idx_records_doc_type in %.1f s", label, time.monotonic() - t0
            )
    if "idx_records_doc" in have:
        db.execute("DROP INDEX idx_records_doc")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_open ON jobs(id)"
        " WHERE status IN ('pending','processing')"
    )


def _ensure_fts(db: sqlite3.Connection, tokenizer: str) -> None:
    """Create (or repair) the BM25 index. Table, triggers and backfill run in ONE
    transaction: an interrupted backfill rolls the table back too, so the next open
    retries instead of silently serving an incomplete FTS index (or corrupting the
    external-content 'delete' command for rows it never indexed)."""
    tokenize = f"tokenize='{FTS_TOKENIZERS[tokenizer]}'"
    create = f"""
        CREATE VIRTUAL TABLE records_fts USING fts5(
            text, content='records', content_rowid='id', {tokenize}
        );
        CREATE TRIGGER records_fts_ai AFTER INSERT ON records BEGIN
            INSERT INTO records_fts(rowid, text) VALUES (new.id, new.text);
        END;
        CREATE TRIGGER records_fts_ad AFTER DELETE ON records BEGIN
            INSERT INTO records_fts(records_fts, rowid, text) VALUES('delete', old.id, old.text);
        END;
        INSERT INTO records_fts(records_fts) VALUES('rebuild');
    """
    row = db.execute("SELECT sql FROM sqlite_master WHERE name='records_fts'").fetchone()
    if row is None:
        db.executescript(f"BEGIN;{create}COMMIT;")
    elif tokenize not in row[0]:
        # table built with another tokenizer (e.g. collection dir survived a crashed
        # create or failed delete): drop and rebuild from records, atomically
        db.executescript(
            "BEGIN;"
            "DROP TRIGGER IF EXISTS records_fts_ai;"
            "DROP TRIGGER IF EXISTS records_fts_ad;"
            "DROP TABLE records_fts;"
            f"{create}COMMIT;"
        )


def _ivf_auto_nlist(n: int) -> int:
    # ~8k rows per shard, power of two (ADR 0002's measured sweet spot); below 32,768
    # rows turbovec scans a one-query search inline on the calling thread (ADR 0005)
    return int(np.clip(2 ** round(np.log2(max(n, 1) / 8192)), 16, 1024))


def _sorted_ids(ids) -> np.ndarray:
    """uint64, strictly increasing: the form _IvfIndex._intersect binary-searches.
    Cached filter allowlists arrive sorted (an O(n) check, no copy); unsorted or
    duplicated input (_expand's sibling lists) pays one np.unique."""
    a = np.asarray(ids, dtype=np.uint64)
    if len(a) > 1 and not bool((a[1:] > a[:-1]).all()):
        a = np.unique(a)
    return a


class _ShardPool:
    """Order-preserving map for _IvfIndex's per-(query, shard) searches, on threads
    owned by one Collection and shut down by its stop() (ADR 0005). Never the asyncio
    default executor: callers already run inside it (to_thread), so waiting on it
    from there can deadlock. turbovec releases the GIL for a whole search (py.detach)
    and runs nq=1 on shards under 32,768 rows inline on the calling thread, so these
    threads scan in parallel.

    The 32,768-row cliff (1,024 blocks, turbovec 1.0.0): from that size up, an nq=1
    search takes turbovec's pooled path instead, where every thread of this pool shares
    one rayon pool and the fan-out ceiling falls to about 2.45x (p3). The DGX corpus's
    largest list (nlist 256, 2.55M rows) is 30,443 rows, 93 % of the cliff, and the DGX
    run reports it (spec §7 F item 7). Never set RAYON_NUM_THREADS=1: it flattens the
    pooled path to about 1,440 calls/s from T=2 (spec §4.2)."""

    def __init__(self, threads: int, name: str = "") -> None:
        self.threads = max(1, int(threads))
        self._prefix = f"raggio-ivf-{name}" if name else "raggio-ivf"
        self._ex: ThreadPoolExecutor | None = None  # built on the first fanned-out call
        self._lock = threading.Lock()
        self._closed = False

    def map(self, fn, tasks: list) -> list:
        # 1 thread, 1 task, or closed: the plain loop on the calling thread (a pool of 1
        # measured 0.89x of serial). IVF_SEARCH_THREADS=1 is the pre-0005 path bit-for-bit
        # for distinct allowlists, which every caller passes (duplicate ids count once
        # toward the 128-id tiny cut).
        if self.threads <= 1 or len(tasks) <= 1 or self._closed:
            return [fn(t) for t in tasks]
        with self._lock:
            if not self._closed and self._ex is None:
                self._ex = ThreadPoolExecutor(self.threads, thread_name_prefix=self._prefix)
            ex = self._ex
        if ex is None:  # closed while we waited for the lock
            return [fn(t) for t in tasks]
        try:
            futs = [ex.submit(fn, t) for t in tasks]
        except RuntimeError:  # shut down between the check and submit (an orphaned search)
            return [fn(t) for t in tasks]
        return [f.result() for f in futs]

    def close(self) -> None:
        """Idempotent. Waits for running shard searches; later map() calls run serially."""
        with self._lock:
            self._closed = True
            ex, self._ex = self._ex, None
        if ex is not None:
            ex.shutdown(wait=True)


class _IvfIndex:
    """ScaNN-style coarse partitioning: k-means centroids route each vector to one
    IdMapIndex shard; a query scans only the nprobe closest shards, trading a little
    recall (tunable per query) for skipping most of the corpus. Duck-types the slice
    of IdMapIndex that Collection uses, so the resident index is either kind."""

    def __init__(
        self, centroids: np.ndarray, shards: list, nprobe: int, pool: _ShardPool | None = None
    ) -> None:
        self.centroids = centroids  # (nlist, dim) f32, unit norm, static after train
        self.shards = shards
        self.nprobe = nprobe
        self._pool = pool or _ShardPool(1)  # the owning Collection's; default: serial
        # per-shard id arrays for allowlist intersection (turbovec rejects allowlist
        # ids an index doesn't hold): built lazily via a full-k probe, dropped for any
        # shard a write touches. ~8 bytes/row when filtered queries occur, else nothing.
        self._id_cache: list = [None] * len(shards)
        # per-shard write generation: an id probe that started before a write to its
        # shard may not cache its (older) snapshot. Read lock-free, bumped under the lock.
        self._id_gen = [0] * len(shards)
        self._id_lock = threading.Lock()
        # shards whose file lags memory: sync() writes only these (plus any shard file
        # the target dir lacks). train() marks every shard, load() none.
        self._dirty: set[int] = set()

    @property
    def nlist(self) -> int:
        return len(self.shards)

    @staticmethod
    def _assign(mat: np.ndarray, C: np.ndarray) -> np.ndarray:
        return np.concatenate(
            [(mat[s : s + 8192] @ C.T).argmax(1) for s in range(0, len(mat), 8192)]
        )

    @classmethod
    def train(
        cls, sample: np.ndarray, nlist: int, dim: int, bit_width: int, nprobe: int,
        pool: _ShardPool | None = None,
    ):
        """k-means (8 Lloyd iterations, as measured in bench/ivf_probe.py) on a
        normalized sample. Every shard is calibrated from the sample BEFORE any row
        is added — the calibrate-early-or-never policy (see CAL_THRESHOLD) holds for
        rebuilds too, and a rebuild re-encodes from retained originals so this always
        applies cleanly."""
        rng = np.random.default_rng(0)
        C = sample[rng.choice(len(sample), nlist, replace=False)].copy()
        for _ in range(8):
            asg = cls._assign(sample, C)
            for j in range(nlist):
                m = asg == j
                if m.any():
                    C[j] = sample[m].mean(0)
            C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-12
        cal = np.ascontiguousarray(sample[:CAL_SAMPLE])
        shards = []
        for _ in range(nlist):
            sh = IdMapIndex(dim=dim, bit_width=bit_width)
            sh.calibrate(cal)
            shards.append(sh)
        ivf = cls(np.ascontiguousarray(C), shards, nprobe, pool)
        ivf._dirty.update(range(nlist))  # nothing on disk yet
        return ivf

    @classmethod
    def load(
        cls, directory: Path, dim: int, bit_width: int, nprobe: int,
        pool: _ShardPool | None = None,
    ):
        C = np.load(directory / "centroids.npy")
        shards = []
        for j in range(len(C)):
            p = directory / f"shard-{j:04d}.tvim"
            shards.append(
                IdMapIndex.load(str(p)) if p.exists() else IdMapIndex(dim=dim, bit_width=bit_width)
            )
        return cls(C, shards, nprobe, pool)

    @property
    def dirty_shards(self) -> frozenset[int]:
        """Shards with writes not yet synced to disk (empty: sync() is a no-op)."""
        with self._id_lock:
            return frozenset(self._dirty)

    def sync(self, directory) -> int:
        """Persist to `directory`: centroids once, then every dirty shard and every
        shard whose file the directory lacks (a fresh attach tmp dir gets them all).
        A shard's flag clears only after its own sync lands and no write raced it, so
        a failure part-way leaves the rest dirty for the next call. Returns the number
        of shard files written."""
        d = Path(directory)
        d.mkdir(exist_ok=True)
        cpath = d / "centroids.npy"
        if not cpath.exists():  # static after train; shard syncs are incremental
            np.save(cpath, self.centroids)
        written = 0
        for j, sh in enumerate(self.shards):
            path = d / f"shard-{j:04d}.tvim"
            if j not in self._dirty and path.exists():
                continue
            gen = self._id_gen[j]
            sh.sync(str(path))
            written += 1
            with self._id_lock:
                if gen == self._id_gen[j]:
                    self._dirty.discard(j)
        return written

    # ---- the IdMapIndex surface Collection uses ----

    def __len__(self) -> int:
        return sum(len(sh) for sh in self.shards)

    def contains(self, rid: int) -> bool:
        return any(sh.contains(rid) for sh in self.shards)

    def prepare(self) -> None:
        for sh in self.shards:
            if len(sh):
                sh.prepare()

    @property
    def calibration_state(self) -> str:
        return self.shards[0].calibration_state if self.shards else "uncalibrated"

    def _shard_written(self, j: int) -> None:
        """Every write to shard j calls this AFTER modifying the shard."""
        with self._id_lock:
            self._id_gen[j] += 1
            self._id_cache[j] = None
            self._dirty.add(j)

    def add_with_ids(self, mat: np.ndarray, ids: np.ndarray) -> None:
        asg = self._assign(mat, self.centroids)
        for j in np.unique(asg):
            m = asg == j
            self.shards[j].add_with_ids(np.ascontiguousarray(mat[m]), ids[m])
            self._shard_written(int(j))

    def remove(self, rid: int) -> None:
        # ponytail: O(nlist) contains scan (~us each) beats maintaining an id->shard map
        for j, sh in enumerate(self.shards):
            if sh.contains(rid):
                sh.remove(rid)
                self._shard_written(j)
                return

    def _shard_ids(self, j: int) -> np.ndarray:
        ids = self._id_cache[j]
        if ids is None:
            gen = self._id_gen[j]  # read BEFORE the probe snapshots the shard
            sh = self.shards[j]
            if len(sh):
                probe = np.zeros((1, self.centroids.shape[1]), dtype=np.float32)
                probe[0, 0] = 1.0
                ids = np.sort(sh.search(probe, k=len(sh))[1][0])  # sorted: intersections
                # use searchsorted instead of a per-call re-sort
            else:
                ids = np.empty(0, np.uint64)
            with self._id_lock:
                # a write since `gen` may postdate our snapshot (an orphaned search whose
                # task was cancelled holds no read lock): use it once, never cache it
                if gen == self._id_gen[j]:
                    self._id_cache[j] = ids
        return ids

    def _intersect(self, allow: np.ndarray, j: int) -> np.ndarray:
        """allow ∩ shard j's ids, sorted. Both sides are sorted and unique (allow via
        _sorted_ids), so binary-search the smaller side into the larger: a 255k-id
        filter against an 8k-row shard costs O(8k log 255k), not O(255k log 8k)
        (laptop, 16 probed shards of 2.55M rows: 94 ms -> 5 ms per query; DGX p3:
        29.8 -> 2.0 ms). search() calls this inside each pool task, so the pool's
        threads may run it concurrently: it only reads allow and the shard-id cache."""
        sids = self._shard_ids(j)
        if not len(sids) or not len(allow):
            return sids[:0]
        small, big = (allow, sids) if len(allow) <= len(sids) else (sids, allow)
        pos = np.minimum(np.searchsorted(big, small), len(big) - 1)
        return small[big[pos] == small]

    def search(self, queries: np.ndarray, k: int, allowlist=None, nprobe: int | None = None):
        """Merged top-k over probed shards. Mirrors IdMapIndex.search: single-query
        results are trimmed exactly; a batch is rectangular, short rows padded with
        id 0 / -inf score (record ids start at 1, so padding never hydrates).
        Every (query, shard) search is one pool task (ADR 0005); the merge consumes
        them in the serial loop's order, so any pool size returns the serial result
        bit-for-bit, tie order included. A filtered task cuts its own allowlist slice
        on the pool thread (spec §2 row 6; p3: 3.7 ms against 6.4-7.0 ms cut up front
        on the caller), so the intersections run in parallel too."""
        if allowlist is not None:  # sorted, unique: the form _intersect binary-searches
            allowlist = _sorted_ids(allowlist)
        tiny = allowlist is not None and len(allowlist) <= 128
        owned: dict[int, np.ndarray] = {}  # tiny allowlists only: shard j -> its slice
        if tiny:
            # tiny allowlists (metadata filters, sibling expansion) must not lose
            # rows to unprobed shards: probe exactly the shards that own them. The
            # owners are needed for the probe list itself, so this cut stays here.
            for j in range(self.nlist):
                if len(self.shards[j]):
                    a = self._intersect(allowlist, j)
                    if len(a):
                        owned[j] = a
            probes = [list(owned)] * len(queries)
        else:
            npb = min(nprobe or self.nprobe, self.nlist)
            sims = queries @ self.centroids.T
            probes = [
                np.argpartition(-sims[qi], npb - 1)[:npb] for qi in range(len(queries))
            ]
        tasks = []  # (qi, query row, shard index, shard, k), serial-loop order
        for qi, probe in enumerate(probes):
            q = np.ascontiguousarray(queries[qi : qi + 1])
            for j in probe:
                sh = self.shards[j]
                if len(sh):
                    tasks.append((qi, q, int(j), sh, min(k, len(sh))))

        def run(task):
            _, q, j, sh, kk = task
            allow = None
            if allowlist is not None:  # per-shard slice: turbovec rejects foreign ids
                allow = owned[j] if tiny else self._intersect(allowlist, j)
                if not len(allow):
                    return None  # turbovec rejects an empty allowlist; adds nothing
            s, i = sh.search(q, k=kk, allowlist=allow)
            return s[0], i[0]

        parts: list = [([], []) for _ in probes]
        for task, res in zip(tasks, self._pool.map(run, tasks)):
            if res is not None:
                parts[task[0]][0].append(res[0])
                parts[task[0]][1].append(res[1])
        out = []
        for parts_s, parts_i in parts:
            if parts_s:
                s, i = np.concatenate(parts_s), np.concatenate(parts_i)
                top = np.argsort(-s)[:k]
                out.append((s[top], i[top]))
            else:
                out.append((np.empty(0, np.float32), np.empty(0, np.uint64)))
        width = max(len(s) for s, _ in out)
        scores = np.full((len(out), width), -np.inf, np.float32)
        ids = np.zeros((len(out), width), np.uint64)
        for qi, (s, i) in enumerate(out):
            scores[qi, : len(s)], ids[qi, : len(i)] = s, i
        return scores, ids

    def all_ids(self) -> np.ndarray:
        """Every id in the index, via per-shard full-k probes (for ghost reconcile)."""
        parts = [self._shard_ids(j) for j in range(self.nlist) if len(self.shards[j])]
        return np.concatenate(parts) if parts else np.empty(0, np.uint64)


class CollectionDeletedError(Exception):
    """A request reached a collection that DELETE /collections/{name} removed while
    it waited; the app answers 404, as for a name that never existed (spec 3.1 B1)."""


class Collection:
    """A resident collection: turbovec index + sqlite metadata + ingest worker."""

    def __init__(
        self,
        cfg: CollectionConfig,
        directory: Path,
        embedder_factory,
        set_index_config=None,
        native_bm25: bool = True,
        ivf_search_threads: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.dir = directory
        self.index_path = directory / "index.tvim"
        self.ivf_dir = directory / "ivf"
        # persists cfg.index_config to the catalog (manager-provided); attach/detach
        # jobs call it from the worker
        self._set_index_config_cb = set_index_config or (lambda ic: None)
        self._embedder_factory = embedder_factory
        self._embedder: Embedder | None = None
        # THE write connection. Every write transaction runs wholly inside db_lock and
        # in a worker thread, never on the event loop. History of the alternatives:
        # sharing it loosely between the loop and threads let statements join each
        # other's in-flight transactions (a full-corpus ingest LOST a journaled job);
        # two independent write connections starved each other's busy handler under a
        # hot worker loop ("database is locked" past a 30s timeout).
        self.db = open_meta_db(directory / "meta.db", cfg.tokenizer)
        # IVF shard fan-out (ADR 0005): threads start on the first multi-shard search and
        # stop with the collection; flat collections never start any. 1 = serial loop.
        self._shard_pool = _ShardPool(ivf_search_threads or default_ivf_search_threads(), cfg.name)
        # the catalog decides which representation is live; a stale sibling on disk
        # (crashed attach/detach) is ignored and rebuilt by the replayed job
        if cfg.index_config and (self.ivf_dir / "centroids.npy").exists():
            self.index = _IvfIndex.load(
                self.ivf_dir, cfg.dim, cfg.bit_width,
                cfg.index_config.get("nprobe", IVF_DEFAULT_NPROBE), pool=self._shard_pool,
            )
        elif self.index_path.exists():
            self.index = IdMapIndex.load(str(self.index_path))
        else:
            self.index = IdMapIndex(dim=cfg.dim, bit_width=cfg.bit_width)
        self.index.prepare()  # warm search caches at load, not on the first query
        # per-type indexed-row counts, kept in step by _upsert_rows/_delete_doc_rows:
        # lets search skip the allowlist (and its full-table id fetch) when nothing
        # would be excluded — the allowlist path costs ~15x a plain scan at 500k rows.
        # Copy-on-write: writers (under db_lock) build a new dict and rebind it, never
        # mutate the published one, so a reader thread iterating it (sum(.values()))
        # can't hit "dictionary changed size during iteration" on free-threaded builds
        self.indexed_counts: dict[str, int] = dict(
            self.db.execute("SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type")
        )
        self.lock = _RWLock()  # searches share; ingest/delete/sync are exclusive
        self._scan_queue: list = []  # (qvec, n, future) waiting for a batched scan
        self._scan_task: asyncio.Task | None = None
        self._allow_cache: dict[str, np.ndarray] = {}  # (scope, filter) -> allowlist ids
        # bumped by every metadata/membership write; a filter scan that started before
        # a write may not cache its (older) snapshot. Read lock-free, bumped under the lock.
        self._allow_gen = 0
        self._allow_lock = threading.Lock()
        # folded token -> doc frequency: a fts5vocab df lookup walks the term's whole
        # doclist (~15-30ms for near-universal tokens), and exactly those hot tokens
        # recur in every query — cached, pruning costs ~0 after warmup
        self._df_cache: dict[str, int] = {}
        self._df_cache_churn = 0  # rows written since the df cache was last (re)built
        # mean folded-token doc length for Python BM25; GIL-atomic swap, no lock —
        # concurrent recomputes land on the same value. Invalidated with the df cache.
        self._avgdl_cache: float | None = None
        # stage 2 runs raggio_native when it is importable, unless NATIVE_BM25=0
        self._native_bm25 = native_bm25
        # search-path reads use one connection per thread: concurrent readers on the
        # shared self.db raise SQLITE_MISUSE (pysqlite connections aren't concurrency-
        # safe), and WAL makes independent read connections cheap and non-blocking
        self._read_local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._closed = False  # set by stop(); makes stale searches fail closed instead
        # of resurrecting connections on a dead collection (leaks the handle and, on
        # Windows, keeps the deleted collection dir undeletable)
        self.deleted = False  # set by delete_collection before stop(); a search that
        # then finds the collection closed raises CollectionDeletedError (404)
        self._reconcile_ghosts()
        # one-shot TQ+ calibration arming: any uncalibrated collection still below the
        # threshold participates. After an eviction/restart the reservoir only witnesses
        # vectors from this residency — a contiguous-window sample measured as good as a
        # uniform one (bench/cal_probe.py), and the alternative (disarm forever) forfeits
        # the recall gain for every collection whose first 10k vectors span two
        # residencies. Loading uncalibrated at/above the threshold stays uncalibrated:
        # late re-encoding measurably loses recall.
        self._cal_reservoir: list | None = (
            []
            if self.index.calibration_state == "uncalibrated" and len(self.index) < CAL_THRESHOLD
            else None
        )
        self._cal_seen = len(self.index)
        self._cal_rng = np.random.default_rng()
        # serializes whole write TRANSACTIONS on self.db (enqueue, job status, records
        # upserts/deletes, vacuum): one writer at a time, so transactions never
        # interleave and SQLite-level lock contention cannot occur in-process
        self.db_lock = threading.Lock()
        self.last_used = time.monotonic()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None

    @property
    def embedder(self) -> Embedder:
        # lazy so vector-only collections work without any embedding endpoint configured
        if self._embedder is None:
            if self._closed:  # stop() has run: a client built now would never be closed
                if self.deleted:
                    raise CollectionDeletedError(self.cfg.name)
                raise RuntimeError(f"collection '{self.cfg.name}' is closed")
            self._embedder = self._embedder_factory()
        return self._embedder

    def _reconcile_ghosts(self) -> None:
        """Evict index ids with no matching record: a crash between a db commit and
        index.sync (delete_document, or an upsert replay re-adding under fresh ids)
        leaves ids in the synced .tvim that nothing will ever remove. They eat top-k
        slots forever (hydration drops them), and MAX(id)+1 can collide with them.
        Always compares the id sets — counts can match while the sets differ (a
        crashed upsert replaces n rows with n fresh ids)."""
        if not len(self.index):
            return
        if isinstance(self.index, _IvfIndex):
            all_ids = self.index.all_ids()
        else:
            probe = np.zeros((1, self.cfg.dim), dtype=np.float32)
            probe[0, 0] = 1.0
            all_ids = self.index.search(probe, k=len(self.index))[1][0]  # every id
        # numpy set difference: a 2.55M-entry Python set plus a per-id membership loop
        # cost seconds on every cold start (brief 2b; the SQL scan is covering, Task 1)
        ghosts = np.setdiff1d(
            np.asarray(all_ids, dtype=np.uint64), _live_ids(self.db), assume_unique=True
        )
        for g in ghosts:  # evict only: a record the index lacks is never re-added (§4.3)
            self.index.remove(int(g))
        if len(ghosts):
            self._sync_index()

    # ---- worker / ingest queue ----

    def start_worker(self) -> None:
        self._worker = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        async with self.lock.write():
            # cancel the worker only once write-locked: it is then outside its write
            # section (upsert -> index add -> sync), so no orphaned to_thread body is
            # left committing rows the index never receives behind a released lock
            if self._worker:
                self._worker.cancel()
                try:
                    await self._worker
                except asyncio.CancelledError:
                    pass
            await asyncio.to_thread(self._sync_index)
            self._closed = True
            # write-locked: no search holding the read lock is mid-query. Orphaned
            # to_thread bodies of cancelled searches, orphaned index builds and the
            # unlocked reads (list_records, get_document, job status) can still be
            # (ADR 0001, concurrency addendum, Deferred row)
            await asyncio.to_thread(self._close_conns)
        if self._embedder is not None:
            await self._embedder.aclose()
        # after _closed: an orphaned search still running falls back to the serial loop
        await asyncio.to_thread(self._shard_pool.close)

    def _close_conns(self) -> None:
        # under db_lock: a write transaction that already holds db_lock on self.db (a
        # request's enqueue, or the worker's claim/finish orphaned by its cancellation)
        # commits before the close; one still waiting for db_lock finds the connection
        # closed and fails, and its job keeps its old status and replays on boot.
        # _rdb re-checks _closed here, so no read connection registers after this sweep
        with self.db_lock:
            conns, self._read_conns = self._read_conns, []
            for c in conns:
                c.close()
            self.db.close()

    async def enqueue(self, payload: dict) -> int:
        # encoding a bulky payload is real CPU and journaling it real I/O: both run in
        # a thread, and only the (non-thread-safe) wake event is touched on the loop
        job_id = await asyncio.to_thread(self._enqueue_row, payload)
        self._wake.set()
        return job_id

    def _enqueue_row(self, payload: dict) -> int:
        # encode BEFORE taking db_lock: the lock serializes every write on self.db, so
        # CPU work under it would stall the worker's claim/finish and other enqueues
        data = _encode_payload(payload)
        with self.db_lock:
            try:
                # explicit id, not lastrowid: MAX+1 is race-free under db_lock
                job_id = self.db.execute("SELECT COALESCE(MAX(id),0)+1 FROM jobs").fetchone()[0]
                # jobs.payload stays NULL: the payload lives in job_payloads, so the
                # claim's status UPDATE rewrites a small row, not an MB overflow chain
                self.db.execute(
                    "INSERT INTO jobs(id, payload, status, created_at, updated_at)"
                    " VALUES (?, NULL, 'pending', ?, ?)",
                    (job_id, _now(), _now()),
                )
                self.db.execute(
                    "INSERT INTO job_payloads(job_id, data) VALUES (?, ?)", (job_id, data)
                )
                self.db.commit()
            except BaseException:
                # one transaction: a jobs row without its payload would replay as an
                # error job, and a half-written txn would ride on the next commit
                self.db.rollback()
                raise
        return job_id

    def pending_jobs(self) -> int:
        return self._rdb().execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
        ).fetchone()[0]

    def _claim_next(self) -> tuple[int, dict | None, str | None] | None:
        """Claim the lowest open job. Returns (job_id, payload, None), or
        (job_id, None, "bad job payload: ...") when its payload cannot be decoded, or
        None when no job is open. The claim is one short transaction under db_lock;
        the decode runs after the lock is released, still in this worker thread, so
        neither the loop nor the other writers wait for it."""
        with self.db_lock:
            row = self.db.execute(_CLAIM_SQL).fetchone()
            if row is None:
                return None
            job_id, text = row
            blob = self.db.execute(
                "SELECT data FROM job_payloads WHERE job_id=?", (job_id,)
            ).fetchone()
            self.db.execute(
                "UPDATE jobs SET status='processing', updated_at=? WHERE id=?", (_now(), job_id)
            )
            self.db.commit()
        try:
            if blob is not None and blob[0] is not None:
                return job_id, _decode_payload(blob[0]), None
            if text is None:
                raise ValueError(
                    f"job {job_id} has no payload (neither jobs.payload nor job_payloads)"
                )
            payload = json.loads(text)  # a journal row written before the binary job journal
            if not isinstance(payload, dict):
                raise ValueError(f"payload is a JSON {type(payload).__name__}, not an object")
            return job_id, payload, None
        except Exception as e:
            # as when json.loads ran inside the worker's try: an undecodable row ends
            # as an 'error' job with its payload row kept, and the worker lives on
            return job_id, None, f"bad job payload: {e}"

    def _finish_job(self, job_id: int, status: str, error: str | None) -> None:
        with self.db_lock:
            # payload cleared on success so vector-heavy jobs don't accumulate on disk;
            # kept on error for diagnosis. The CASE clears a legacy TEXT payload; a
            # binary one is its job_payloads row, deleted in the same commit
            self.db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=?,"
                " payload=CASE WHEN ?='done' THEN NULL ELSE payload END WHERE id=?",
                (status, error, _now(), status, job_id),
            )
            if status == "done":
                self.db.execute("DELETE FROM job_payloads WHERE job_id=?", (job_id,))
            self.db.commit()
            # return freed pages to the OS: in full once the queue is empty, in bounded
            # trims while a backlog drains
            self._vacuum_after_finish()

    def _vacuum_after_finish(self) -> None:
        """Return freed meta.db pages to the OS after a job's finish commit (caller
        holds db_lock; no transaction is open). A full incremental_vacuum after every
        job cost 38-54 ms per job while a backlog drained, so it runs only once no job
        is open. Until then a freelist of VACUUM_FREELIST_PAGES or more is trimmed
        back to VACUUM_FREELIST_PAGES - VACUUM_CHUNK_PAGES: the file stays bounded
        however many pages one job freed, and no trim is a whole-file vacuum that
        holds enqueues behind db_lock."""
        # the literal predicate, byte for byte pending_jobs' query: idx_jobs_open
        # serves it, so counting open jobs never scans the journal
        open_jobs = self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
        ).fetchone()[0]
        # fetchall() is load-bearing on both vacuum pragmas: the pragma frees pages per
        # STEP, and pysqlite's execute() steps once -- without exhausting the cursor it
        # frees a single page
        if open_jobs == 0:
            self.db.execute("PRAGMA incremental_vacuum").fetchall()
            return
        free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
        if free >= VACUUM_FREELIST_PAGES:
            # a pragma argument cannot be a bound parameter; n is an int computed here
            n = int(free - VACUUM_FREELIST_PAGES + VACUUM_CHUNK_PAGES)
            self.db.execute(f"PRAGMA incremental_vacuum({n})").fetchall()

    async def _run_worker(self) -> None:
        while True:
            # clear BEFORE claiming: an enqueue landing during the claim either becomes
            # visible to the claim itself or re-sets the event, so no wakeup is lost
            self._wake.clear()
            row = await asyncio.to_thread(self._claim_next)
            if row is None:
                await self._wake.wait()
                continue
            # decoded in the claim's thread; an undecodable row comes back as
            # (job_id, None, reason) and finishes as 'error' like any failed job
            job_id, payload, bad = row
            try:
                if bad is not None:
                    raise ValueError(bad)
                await self._process_job(payload)
                status, error = "done", None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status, error = "error", str(e)
            await asyncio.to_thread(self._finish_job, job_id, status, error)

    async def _process_job(self, payload: dict) -> None:
        op = payload.get("op")
        if op in ("attach_index", "detach_index"):
            try:
                if op == "attach_index":
                    await self._attach_index(payload)
                else:
                    await self._detach_index()
            finally:
                await asyncio.to_thread(_malloc_trim)  # D13: failed builds free memory too
            return
        # flatten documents into records (external_id, doc_id, type, position, text,
        # metadata, vector) and build the matrix in worker threads: both are pure CPU
        # the event loop should not carry
        rows = await asyncio.to_thread(_payload_rows, payload)
        if not rows:
            return
        need = [i for i, r in enumerate(rows) if r[6] is None]
        for start in range(0, len(need), 64):
            batch = need[start : start + 64]
            vecs = await self.embedder.embed([rows[i][4] for i in batch])
            for i, v in zip(batch, vecs):
                rows[i][6] = v
        mat = await asyncio.to_thread(_rows_matrix, rows)

        async with self.lock.write():
            # off the event loop: the FTS triggers tokenize every row (expensive with
            # trigram). The upsert is one transaction that rolls back on any failure and
            # touches neither the index nor indexed_counts before it commits, so a
            # failed job leaves the collection as it was. The rows it replaced leave the
            # index only after that commit.
            ids, fresh, replaced = await asyncio.to_thread(self._upsert_rows, rows, mat)
            if replaced:
                await asyncio.to_thread(self._unindex, replaced)
            idarr = np.array(ids, dtype=np.uint64)
            # while the reservoir is armed, add in threshold-sized slices so even one
            # bulk job calibrates AT the threshold (calibrating after a large
            # uncalibrated ingest measurably loses recall); feed only rows new to the
            # index — re-upsert churn would skew the sample toward hot documents
            step = len(mat) if self._cal_reservoir is None else CAL_THRESHOLD
            for s in range(0, len(mat), step):
                m = mat[s : s + step]
                await asyncio.to_thread(self.index.add_with_ids, m, idarr[s : s + step])
                if self._cal_reservoir is None:
                    continue
                new = np.array(fresh[s : s + step], dtype=bool)
                sample = await asyncio.to_thread(self._feed_calibration, m[new])
                if sample is not None:
                    try:
                        # re-encodes the <=CAL_THRESHOLD rows added so far from their
                        # codes; everything after encodes fresh under the calibration
                        await asyncio.to_thread(self.index.calibrate, sample)
                        self._cal_reservoir = None
                    except Exception:
                        # best-effort: stay armed and retry on a later slice or job —
                        # calibration must never fail the ingest job (an 'error' job is
                        # terminal and would leave committed rows behind)
                        pass
            # ponytail: sync after every job; batch on an interval if write throughput matters
            await asyncio.to_thread(self._sync_index)

    def _feed_calibration(self, mat: np.ndarray) -> np.ndarray | None:
        """Reservoir-sample ingested vectors (Algorithm R); once the one-shot
        calibration threshold is crossed, return the sample and disarm forever."""
        if self._cal_reservoir is None:
            return None
        for row in mat:
            self._cal_seen += 1
            if len(self._cal_reservoir) < CAL_SAMPLE:
                self._cal_reservoir.append(row.copy())  # copy: a view would pin the whole job's matrix
            else:
                j = int(self._cal_rng.integers(self._cal_seen))
                if j < CAL_SAMPLE:
                    self._cal_reservoir[j] = row.copy()
        if len(self.index) >= CAL_THRESHOLD and len(self._cal_reservoir) >= CAL_SAMPLE:
            return np.vstack(self._cal_reservoir)  # caller disarms after calibrate succeeds
        return None

    def _invalidate_allow(self) -> None:
        """Every metadata/membership write calls this AFTER its commit."""
        with self._allow_lock:
            self._allow_gen += 1
            self._allow_cache.clear()

    def _upsert_rows(self, rows: list, mat: np.ndarray) -> tuple[list[int], list[bool], list[int]]:
        """Insert `rows` (_payload_rows' shape) with `mat` as their vectors, replacing
        any record with the same external_id: one transaction, rolled back on any
        failure. Returns (ids, fresh, replaced): the new record ids in row order, per
        row whether its external_id was new, and the replaced records' ids that are in
        the vector index. The index is not touched here: the caller (holding
        lock.write()) removes `replaced` with _unindex once this has committed, so a
        failed upsert is a pure database rollback."""
        # fp16 originals retained on disk (half the f32 size, negligible loss vs the
        # 4-bit codes): the only way to rebuild the index representation later, since
        # turbovec can't enumerate or reconstruct vectors
        vecs16 = mat.astype(np.float16)
        # row CPU before db_lock: the lock serializes every write on self.db
        blobs = [v.tobytes() for v in vecs16]
        metas = [json.dumps(r[5] or {}) for r in rows]
        with self.db_lock:
            try:
                # one IN query per 512 rows instead of a point query per row
                old = {
                    ext_id: (rid, indexed, rtype)
                    for ext_id, rid, indexed, rtype in _rows_by_id(
                        self.db,
                        "SELECT external_id, id, indexed, type FROM records WHERE external_id IN ({})",
                        [r[0] for r in rows],
                    )
                }
                # explicit ids, not lastrowid: records are only inserted here, in the single worker
                first = self.db.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0] + 1
                ids = list(range(first, first + len(rows)))
                # upsert = DELETE then INSERT in one transaction: external_id stays UNIQUE,
                # and replaying a job after a crash is idempotent
                gone = [(rid,) for rid, _, _ in old.values()]
                self.db.executemany("DELETE FROM records WHERE id=?", gone)
                self.db.executemany("DELETE FROM vecs WHERE id=?", gone)
                self.db.executemany(
                    "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
                    " VALUES (?,?,?,?,?,?,?,1)",
                    [(i, *r[:5], m) for i, r, m in zip(ids, rows, metas)],
                )
                # OR REPLACE: a vecs row already at a new id (past MAX(records.id)) is an
                # orphan with no record; a plain INSERT would fail this job and, as the
                # rollback leaves MAX(id) where it was, every later one
                self.db.executemany(
                    "INSERT OR REPLACE INTO vecs(id, vec) VALUES (?,?)", list(zip(ids, blobs))
                )
                self.db.commit()
            except BaseException:
                # the transaction is still open after a failed statement: without this,
                # the next commit on self.db (the worker's own _finish_job('error'))
                # would persist the half-applied upsert
                self.db.rollback()
                raise
            # published after the commit, still under db_lock: copy-on-write, one rebind
            # per job, none when the transaction rolled back
            counts = dict(self.indexed_counts)
            for _, indexed, rtype in old.values():
                if indexed:
                    counts[rtype] -= 1
            for r in rows:
                counts[r[2]] = counts.get(r[2], 0) + 1
            self.indexed_counts = counts  # one reference store: readers see old or new
        self._invalidate_allow()
        self._df_cache_churn += len(rows)
        fresh = [r[0] not in old for r in rows]
        replaced = [rid for rid, indexed, _ in old.values() if indexed]
        return ids, fresh, replaced

    def _unindex(self, rids: list[int]) -> None:
        """Remove records whose rows a committed transaction replaced or deleted from
        the vector index (the .tvim drops them at the next _sync_index). The caller
        holds lock.write(), so no search runs meanwhile; db_lock is not needed."""
        for rid in rids:
            self.index.remove(rid)

    # ---- optional IVF index (attach / detach) ----

    def _sync_index(self) -> None:
        if isinstance(self.index, _IvfIndex):
            self.index.sync(self.ivf_dir)
        else:
            self.index.sync(str(self.index_path))

    def _save_index_config(self, ic: dict | None) -> None:
        self._set_index_config_cb(ic)  # catalog first: it decides what load() trusts
        self.cfg.index_config = ic

    def index_info(self) -> dict:
        if isinstance(self.index, _IvfIndex):
            return {"type": "ivf", "nlist": self.index.nlist, "nprobe": self.index.nprobe}
        return {"type": "flat"}

    async def request_index(self, nlist: int | None, nprobe: int | None) -> int:
        """Validate cheaply on the loop, then queue the (idempotent, replayable) build."""
        if not (nlist is None and nprobe and isinstance(self.index, _IvfIndex)):  # nprobe-only retune
            n = sum(self.indexed_counts.values())
            if n < IVF_MIN_ROWS:
                raise ValueError(f"index needs at least {IVF_MIN_ROWS} indexed records, have {n}")
            if nlist is not None and n < 8 * nlist:
                raise ValueError(f"nlist={nlist} too large for {n} records (need >=8 rows per shard)")
        return await self.enqueue({"op": "attach_index", "nlist": nlist, "nprobe": nprobe})

    async def request_index_drop(self) -> int:
        if not isinstance(self.index, _IvfIndex):
            raise ValueError("collection has no index")
        return await self.enqueue({"op": "detach_index"})

    def _iter_vec_blocks(self):
        """Stream (ids, f32 matrix) of every indexed record from the retained fp16
        vectors, blockwise so a multi-GB collection never materializes at once."""
        db, last = self._rdb(), 0
        while True:
            rows = db.execute(
                "SELECT r.id, v.vec FROM records r JOIN vecs v ON v.id=r.id"
                " WHERE r.indexed=1 AND r.id>? ORDER BY r.id LIMIT ?",
                (last, IVF_BUILD_BLOCK),
            ).fetchall()
            if not rows:
                return
            last = rows[-1][0]
            mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16)
            yield (
                np.array([r[0] for r in rows], dtype=np.uint64),
                mat.reshape(len(rows), -1).astype(np.float32),
            )

    def _vec_sample(self, k: int) -> np.ndarray:
        """Up to k distinct, uniformly random retained vectors of indexed records.
        ORDER BY RANDOM() reads and sorts every vecs page (p1: 152 s cold at 2.55M rows),
        so while ids are dense, draw random rowids and point-read only those. Every
        live id is equally likely to be drawn, so the pick stays uniform. Too sparse,
        k above n/2, or too few hits after VEC_SAMPLE_ROUNDS falls back to the scan."""
        db = self._rdb()
        n = sum(self.indexed_counts.values())
        max_id = db.execute("SELECT MAX(id) FROM records").fetchone()[0] or 0
        if n and max_id and n / max_id >= VEC_SAMPLE_MIN_DENSITY and 2 * k <= n:
            rng = np.random.default_rng()
            got: dict[int, bytes] = {}
            tried = np.empty(0, dtype=np.int64)
            for _ in range(VEC_SAMPLE_ROUNDS):
                draws = rng.integers(1, max_id + 1, size=int((k - len(got)) * max_id / n * 1.3) + 64)
                cand = np.setdiff1d(draws, tried)
                tried = np.union1d(tried, cand)
                got.update(_rows_by_id(
                    db,
                    "SELECT v.id, v.vec FROM vecs v JOIN records r ON r.id=v.id"
                    " WHERE r.indexed=1 AND v.id IN ({})",
                    cand.tolist(),
                ))
                if len(got) >= k:
                    keys = list(got)
                    pick = rng.choice(len(keys), k, replace=False)
                    blob = b"".join(got[keys[i]] for i in pick)
                    mat = np.frombuffer(blob, dtype=np.float16)
                    return mat.reshape(k, self.cfg.dim).astype(np.float32)
        rows = db.execute(
            "SELECT v.vec FROM vecs v JOIN records r ON r.id=v.id WHERE r.indexed=1"
            " ORDER BY RANDOM() LIMIT ?", (k,),
        ).fetchall()
        if not rows:
            return np.empty((0, self.cfg.dim), np.float32)
        mat = np.frombuffer(b"".join(r[0] for r in rows), dtype=np.float16)
        return mat.reshape(len(rows), -1).astype(np.float32)

    async def _backfill_vecs(self, ids: np.ndarray | None = None, add=None) -> int:
        """Records ingested before vector retention lack the fp16 original a rebuild
        needs. Re-embed from text where possible (assumes the collection's configured
        embedding model produced the stored vectors); otherwise fail the job with a
        count so the caller knows to re-ingest.

        ids=None finds them with the records x vecs anti-join, a full records scan
        (p1: minutes cold at 2.55M rows) that attach only runs when too few retained
        vectors exist to train on. Otherwise `ids` are the rows the attach stream
        skipped, point-read by id. add(ids, f32 matrix) receives each written batch on
        a worker thread, so the caller never holds them all. Returns rows written."""
        if ids is None:
            rows = await asyncio.to_thread(
                lambda: self._rdb().execute(
                    "SELECT r.id, r.text FROM records r LEFT JOIN vecs v ON v.id=r.id"
                    " WHERE r.indexed=1 AND v.id IS NULL"
                ).fetchall()
            )
        else:
            rows = await asyncio.to_thread(
                lambda: list(_rows_by_id(
                    self._rdb(), "SELECT id, text FROM records WHERE indexed=1 AND id IN ({})",
                    ids.tolist(),
                ))
            )
        if not rows:
            return 0
        no_text = sum(1 for _, t in rows if not t)
        if no_text:
            raise ValueError(
                f"{no_text} records have neither a retained vector nor text;"
                " re-ingest them before attaching an index"
            )
        written = 0
        for s in range(0, len(rows), 64):
            batch = rows[s : s + 64]
            vecs = await self.embedder.embed([t for _, t in batch])
            vecs16 = _normalize(np.array(vecs, dtype=np.float32)).astype(np.float16)

            def write():
                # A DELETE can land while the batch is embedded; an orphan vecs row would
                # collide with the next ingest's MAX(id)+1, so write only live records.
                with self.db_lock:
                    n = 0
                    for (rid, _), v in zip(batch, vecs16):
                        n += self.db.execute(
                            "INSERT OR REPLACE INTO vecs(id, vec) SELECT ?, ?"
                            " WHERE EXISTS (SELECT 1 FROM records WHERE id=? AND indexed=1)",
                            (rid, v.tobytes(), rid),
                        ).rowcount
                    self.db.commit()
                    return n

            written += await asyncio.to_thread(write)
            if add is not None:
                await asyncio.to_thread(
                    add,
                    np.array([rid for rid, _ in batch], dtype=np.uint64),
                    vecs16.astype(np.float32),  # the fp16 round trip a stream would read
                )
        return written

    async def _attach_index(self, payload: dict) -> None:
        """Build IVF shards from retained vectors and swap them in. The build runs
        outside the lock — searches stay on the old index throughout; ingest can't
        interleave (this worker is the only adder), and deletes that land during the
        build are diffed out before the swap."""
        nlist_req, nprobe_req = payload.get("nlist"), payload.get("nprobe")
        if nlist_req is None and nprobe_req and isinstance(self.index, _IvfIndex):
            self.index.nprobe = int(nprobe_req)  # retune the default, no rebuild
            self._save_index_config({"nlist": self.index.nlist, "nprobe": self.index.nprobe})
            return
        t0 = time.monotonic()
        # guard first (D13): validate and refuse before any SQL, so a full container
        # fails in milliseconds instead of after minutes of cold pre-work. n comes from
        # the in-memory per-type counts, which equal COUNT(*) WHERE indexed=1
        n = sum(self.indexed_counts.values())
        if n < IVF_MIN_ROWS:
            raise ValueError(f"index needs at least {IVF_MIN_ROWS} indexed records, have {n}")
        nlist = nlist_req or _ivf_auto_nlist(n)
        if n < 8 * nlist:
            raise ValueError(f"nlist={nlist} too large for {n} records (need >=8 rows per shard)")
        _require_headroom(_attach_need(n, self.cfg.dim, self.cfg.bit_width, nlist), "index build")
        t1 = time.monotonic()
        # no records x vecs anti-join before the build (p1: 178 s cold with the COUNT):
        # rows without a retained vector are found from the stream below. A short sample
        # means retained vectors are scarce (ingested before retention): backfill them all
        # first, as before plan C, so k-means trains on a full sample
        sample = await asyncio.to_thread(self._vec_sample, IVF_TRAIN_SAMPLE)
        legacy = 0
        if len(sample) < min(n, IVF_TRAIN_SAMPLE):
            legacy = await self._backfill_vecs()
            sample = await asyncio.to_thread(self._vec_sample, IVF_TRAIN_SAMPLE)
        t2 = time.monotonic()

        def build():
            nonlocal sample
            tb = time.monotonic()
            ivf = _IvfIndex.train(
                sample, nlist, self.cfg.dim, self.cfg.bit_width,
                int(nprobe_req or IVF_DEFAULT_NPROBE), pool=self._shard_pool,
            )
            sample = None  # free the f32 sample before the stream, as before plan C
            seen = [np.empty(0, dtype=np.uint64)]
            for ids, mat in self._iter_vec_blocks():
                ivf.add_with_ids(mat, ids)
                seen.append(ids)
            seen = np.concatenate(seen)
            tl = time.monotonic()
            # the indexed rows the stream skipped are exactly those without a retained
            # vector (this worker is the only adder); the live ids come from the covering
            # index, never the records pages
            missing = np.setdiff1d(_live_ids(self._rdb()), seen, assume_unique=True)
            return ivf, seen, missing, tl - tb, time.monotonic() - tl

        ivf, seen, missing, build_s, live_s = await asyncio.to_thread(build)
        t3 = time.monotonic()
        wrote = 0
        if len(missing):
            parts = [seen]

            def add(ids, mat):  # each re-embedded batch goes straight into the new index
                ivf.add_with_ids(mat, ids)
                parts.append(ids)

            wrote = await self._backfill_vecs(missing, add)
            seen = np.concatenate(parts)  # the swap diff covers them too
        t4 = time.monotonic()
        tmp = self.dir / "ivf.tmp"
        async with self.lock.write():

            def swap():
                # deleted while the build streamed
                for gone in np.setdiff1d(seen, _live_ids(self._rdb()), assume_unique=True):
                    ivf.remove(int(gone))
                if tmp.exists():
                    shutil.rmtree(tmp)
                ivf.sync(tmp)

                def commit():  # on-disk commit point; catalog commit follows
                    if self.ivf_dir.exists():  # rebuild over an existing index
                        shutil.rmtree(self.ivf_dir)
                    tmp.rename(self.ivf_dir)

                _retry_fs(commit)
                ivf.prepare()

            await asyncio.to_thread(swap)
            self._save_index_config({"nlist": ivf.nlist, "nprobe": ivf.nprobe})
            self.index = ivf
            self._cal_reservoir = None  # shards were calibrated at train time
            self.index_path.unlink(missing_ok=True)  # flat file is stale from here on
        t5 = time.monotonic()
        _log.info(
            "attach_index %s: n=%d nlist=%d prework=%.2fs sample=%.2fs legacy_backfill=%d"
            " build=%.2fs live_scan=%.2fs backfill=%d rows %.2fs swap=%.2fs total=%.2fs",
            self.cfg.name, n, nlist, t1 - t0, t2 - t1, legacy, build_s, live_s,
            wrote, t4 - t3, t5 - t4, t5 - t0,
        )

    async def _detach_index(self) -> None:
        """Rebuild the flat index from retained vectors and drop the shards."""
        if not isinstance(self.index, _IvfIndex):
            if self.cfg.index_config:  # crash replay landed past the swap: finish bookkeeping
                self._save_index_config(None)
            if self.ivf_dir.exists():
                await asyncio.to_thread(shutil.rmtree, self.ivf_dir, True)
            return
        # guard first (D13): a refusal costs no SQL
        _require_headroom(
            len(self.index) * self.cfg.dim * self.cfg.bit_width // 8, "index removal"
        )

        def build():
            flat = IdMapIndex(dim=self.cfg.dim, bit_width=self.cfg.bit_width)
            sample = self._vec_sample(CAL_SAMPLE)
            if len(sample) >= 64:
                flat.calibrate(sample)  # calibrate-early holds for rebuilds too
            seen = [np.empty(0, dtype=np.uint64)]
            for ids, mat in self._iter_vec_blocks():
                flat.add_with_ids(mat, ids)
                seen.append(ids)
            seen = np.concatenate(seen)
            # rows the stream skipped have no retained vector. Every attach backfills
            # them all, so on an IVF collection this only trips if vecs rows were lost;
            # a stream before the refusal beats an anti-join on every detach
            missing = len(np.setdiff1d(_live_ids(self._rdb()), seen, assume_unique=True))
            if missing:
                raise ValueError(f"{missing} records lack a retained vector; re-ingest them first")
            return flat, seen

        flat, seen = await asyncio.to_thread(build)
        tmp = self.dir / "index.tvim.tmp"
        async with self.lock.write():

            def swap():
                for gone in np.setdiff1d(seen, _live_ids(self._rdb()), assume_unique=True):
                    flat.remove(int(gone))
                tmp.unlink(missing_ok=True)
                flat.sync(str(tmp))
                _retry_fs(lambda: os.replace(tmp, self.index_path))
                flat.prepare()

            await asyncio.to_thread(swap)
            self._save_index_config(None)
            self.index = flat
            await asyncio.to_thread(shutil.rmtree, self.ivf_dir, True)

    # ---- reads ----

    def _rdb(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError(f"collection '{self.cfg.name}' is closed")
        db = getattr(self._read_local, "db", None)
        if db is None:
            db = sqlite3.connect(self.dir / "meta.db", check_same_thread=False)
            db.execute("PRAGMA query_only=1")
            with self.db_lock:
                if self._closed:  # stop() swept the registry while this one opened
                    db.close()
                    raise RuntimeError(f"collection '{self.cfg.name}' is closed")
                self._read_conns.append(db)
            self._read_local.db = db
        return db

    def _hydrate(self, ids: list[int], scores: list[float] | None = None) -> list[dict]:
        if not ids:
            return []
        qmarks = ",".join("?" * len(ids))
        rows = {
            r[0]: r
            for r in self._rdb().execute(
                f"SELECT id, external_id, doc_id, type, position, text, metadata FROM records WHERE id IN ({qmarks})",
                ids,
            )
        }
        out = []
        for n, rid in enumerate(ids):
            r = rows.get(rid)
            if r is None:
                continue
            hit = {
                "id": r[1],
                "doc_id": r[2],
                "type": r[3],
                "position": r[4],
                "text": r[5],
                "metadata": json.loads(r[6]),
            }
            if scores is not None:
                hit["score"] = scores[n]
            out.append(hit)
        return out

    def _rescore_k(self, n: int) -> int:
        """Quantized over-fetch depth feeding the fp16 rescore (see RESCORE_MULT)."""
        floor = RESCORE_FLOOR.get(self.cfg.bit_width, RESCORE_FLOOR[4])
        return max(1, min(max(floor, RESCORE_MULT * n), RESCORE_CAP, len(self.index)))

    def _rescore_rows(
        self, queries: np.ndarray, rows: list[tuple[list[int], list[float], int]]
    ) -> list[tuple[list[int], list[float]]]:
        """Re-rank quantized candidates against the retained fp16 originals (exact
        cosine). rows: one (candidate_ids, quantized_scores, n) per query row. One
        union blob fetch + decode serves the whole batch, so the batched scan path
        pays a single round of point lookups. Ids without a valid blob (rows ingested
        before vector retention) keep their quantized score — merged, never dropped."""
        dim = self.cfg.dim
        union = list({rid for ids, _, _ in rows for rid in ids})
        blobs = {
            rid: blob
            for rid, blob in _rows_by_id(
                self._rdb(), "SELECT id, vec FROM vecs WHERE id IN ({})", union
            )
            if blob is not None and len(blob) == dim * 2
        }
        if blobs:
            mat = np.frombuffer(b"".join(blobs.values()), dtype=np.float16)
            mat = mat.reshape(len(blobs), dim).astype(np.float32)
            # fp16 round-trip drifts norms ~1e-3: renormalize for exact cosine
            mat /= np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12)
            rowof = {rid: j for j, rid in enumerate(blobs)}
        out = []
        for qi, (ids, scores, n) in enumerate(rows):
            merged = list(scores)
            have = [k for k, rid in enumerate(ids) if rid in blobs]
            if have:
                exact = mat[[rowof[ids[k]] for k in have]] @ queries[qi]
                for j, k in enumerate(have):
                    merged[k] = float(exact[j])
            order = sorted(range(len(ids)), key=lambda k: (-merged[k], ids[k]))[:n]
            out.append(([ids[k] for k in order], [merged[k] for k in order]))
        return out

    def _search_rescored(
        self, queries: np.ndarray, ns: list[int], **kw
    ) -> list[tuple[list[int], list[float]]]:
        """The one ranked-search entry point: over-fetch the quantized index to
        _rescore_k, drop -inf batch padding (short IVF rows, never real hits), and
        fp16-rescore — no caller can surface quantized scores by accident. ns: the
        final depth wanted per query row. Blocking; call from a thread."""
        scores, ids = self.index.search(queries, k=self._rescore_k(max(ns)), **kw)
        rows = []
        for row, n in enumerate(ns):
            r_ids, r_scores = [], []
            for i, s in zip(ids[row], scores[row]):
                if s > -np.inf:
                    r_ids.append(int(i))
                    r_scores.append(float(s))
            rows.append((r_ids, r_scores, n))
        return self._rescore_rows(queries, rows)

    async def _vector_ids(
        self, qvec: np.ndarray, n: int, scope: str, filt: dict | None,
        nprobe: int | None = None,
    ) -> tuple[list[int], list[float]]:
        """Top-n by cosine similarity. Caller must hold self.lock."""
        if len(self.index) == 0:
            return [], []
        other = {"chunks": "summary", "summaries": "chunk"}.get(scope)
        # allowlist only when it would actually exclude something: with no filter and
        # no rows of the other type, it's the whole index — and building it costs a
        # full-table id fetch plus a 15x slower masked scan
        if not filt and not (other and self.indexed_counts.get(other, 0)):
            return await self._scan_batched(qvec, n, nprobe)

        def run() -> tuple[list[int], list[float]]:
            key = f"{scope}|{json.dumps(filt, sort_keys=True)}"
            allow = self._allow_cache.get(key)
            if allow is None:
                gen = self._allow_gen  # read BEFORE the SELECT takes its snapshot
                where, params = _filter_sql(scope, filt)
                ids = [r[0] for r in self._rdb().execute(f"SELECT id FROM records WHERE {where}", params)]
                # sorted once per miss, whatever order the query plan returns: the IVF
                # path intersects by binary search (flat turbovec ignores the order)
                allow = np.sort(np.array(ids, dtype=np.uint64))
                with self._allow_lock:
                    # a write since `gen` may postdate our snapshot (an orphaned scan whose
                    # task was cancelled holds no read lock): use it once, never cache it
                    if gen == self._allow_gen:
                        if len(self._allow_cache) >= 8:  # ponytail: tiny FIFO; LRU if filters vary widely
                            self._allow_cache.pop(next(iter(self._allow_cache)))
                        self._allow_cache[key] = allow
            if len(allow) == 0:
                return [], []
            kw = {"nprobe": nprobe} if isinstance(self.index, _IvfIndex) else {}
            return self._search_rescored(qvec, [n], allowlist=allow, **kw)[0]

        return await asyncio.to_thread(run)

    async def _scan_batched(
        self, qvec: np.ndarray, n: int, nprobe: int | None = None
    ) -> tuple[list[int], list[float]]:
        """Full-index scans from concurrent requests share one kernel pass over the
        codes (~4x cheaper per query at nq>=8 than a pass each)."""
        fut = asyncio.get_running_loop().create_future()
        self._scan_queue.append((qvec, n, nprobe, fut))
        if self._scan_task is None or self._scan_task.done():
            self._scan_task = asyncio.create_task(self._drain_scans())
        return await fut

    async def _drain_scans(self) -> None:
        # runs lock-free: every waiter holds a read lock while awaiting its future, so
        # writers stay out. (If all waiters get cancelled mid-scan a writer could slip
        # in concurrently; the kernel's internal index lock serializes that case.)
        while self._scan_queue:
            batch, self._scan_queue = self._scan_queue, []
            try:  # any failure must reach every waiter — an unresolved future would
                # leave its caller holding a read lock forever
                mat = np.vstack([q for q, _, _, _ in batch])
                ns = [n for _, n, _, _ in batch]
                kw = {}
                if isinstance(self.index, _IvfIndex):
                    # one merged pass per batch: the widest nprobe wins (recall-safe)
                    kw["nprobe"] = max(p or self.index.nprobe for _, _, p, _ in batch)
                results = await asyncio.to_thread(self._search_rescored, mat, ns, **kw)
                for (_, _, _, fut), res in zip(batch, results):
                    if not fut.done():
                        fut.set_result(res)
            except Exception as e:
                for _, _, _, fut in batch:
                    if not fut.done():
                        fut.set_exception(e)

    def _df(self, key: str) -> int:
        """Doc frequency of a folded term, via the df cache (a fts5vocab lookup walks
        the term's whole doclist — ~ms for common terms, cached after warmup)."""
        df = self._df_cache.get(key)
        if df is None:
            row = self._rdb().execute("SELECT doc FROM records_fts_v WHERE term=?", (key,)).fetchone()
            df = row[0] if row else 0
            if len(self._df_cache) >= 65536:  # ponytail: Zipf head re-warms instantly
                self._df_cache.clear()
            self._df_cache[key] = df
        return df

    def _prune_common(self, qtext: str) -> tuple[list[str], list[str]]:
        """Return (kept, all) query tokens: kept rarest-first while their combined
        doc-frequency fits the FTS_SCAN_BUDGET, the rest dropped. The rarest token
        always survives, so a query of only-common words still matches. Unknown terms
        (df lookup misses, e.g. trigram tokenizer) cost nothing and are always kept.
        kept < all signals _text_ids to restore full-query ranking in stage 2.
        Tokens are stage 2's (_fold_tokens), which split on '_' as FTS5 unicode61 does:
        the old word regex kept '_2' with df 0, FTS5 read it as '2', and the ranked OR
        matched most of the corpus. Trigram matches substrings, where '_' and diacritics
        count, so it keeps the raw word tokens."""
        if self.cfg.tokenizer == "trigram":
            toks = re.findall(r"\w+", qtext)[:100]
        else:
            toks = _fold_tokens(qtext)[:100]
        # the df key is the term FTS5 matches: FTS5 lowercases the query's terms, and
        # _fold_tokens leaves the capitals NFKD makes from compatibility characters (math
        # alphanumerics, double-struck letters, the numero and trade mark signs, modifier
        # letters: bold A -> A), so fold again (A -> a)
        keys = [_fold(t) for t in toks]
        if not toks:
            return [], []
        total = sum(self.indexed_counts.values())
        budget = max(FTS_SCAN_BUDGET_MIN_ROWS, int(FTS_SCAN_BUDGET * total))
        # cached dfs are approximations (they only gate against the budget): refresh
        # after enough write churn rather than on every write, so the cache survives
        # mixed ingest+search workloads. Churn counts every insert/upsert/delete row,
        # so count-neutral rewrites still invalidate. avgdl rides the same event.
        if self._df_cache_churn > max(1000, total // 4):
            self._df_cache.clear()
            self._avgdl_cache = None
            self._df_cache_churn = 0
        dfs = {}
        for key in keys:
            if key not in dfs:
                dfs[key] = self._df(key)
        # df-0 tokens (typos, trigram tokenizer) cost nothing and are always kept, but
        # they must not satisfy the keep-guarantee: the rarest MATCHING token survives
        spent, kept, have_real = 0, set(), False
        for key in sorted(dfs, key=dfs.get):
            df = dfs[key]
            if df and have_real and spent + df > budget:
                break
            spent += df
            kept.add(key)
            have_real = have_real or df > 0
        return [t for t, key in zip(toks, keys) if key in kept], toks

    def _avgdl(self) -> float:
        """Mean folded-token doc length from a ~256-doc sample (random id probes — a
        full ORDER BY RANDOM() materializes the whole table). Only enters the BM25
        length normalization, so sampling error barely moves ranking."""
        cached = self._avgdl_cache
        if cached is not None:
            return cached
        db = self._rdb()
        nat = self._scorer()
        fold_tokens = nat.fold_tokens if nat else _fold_tokens
        maxid = db.execute("SELECT MAX(id) FROM records").fetchone()[0] or 0
        dls = []
        if maxid:
            for g in np.random.default_rng(0).integers(1, maxid + 1, size=256):
                row = db.execute(
                    "SELECT text FROM records WHERE id>=? AND indexed=1"
                    " AND text IS NOT NULL AND text!='' LIMIT 1",
                    (int(g),),
                ).fetchone()
                if row:
                    dls.append(len(fold_tokens(row[0])))
        avgdl = (sum(dls) / len(dls)) if dls else 1.0
        self._avgdl_cache = avgdl
        return avgdl

    def _scorer(self):
        """raggio_native when it is loaded and NATIVE_BM25 allows it, else None (the
        Python reference). Read per call, so tests can swap the module global."""
        return _native if self._native_bm25 else None

    def _bm25_rescore(
        self, qtext: str, cand: list[tuple[int, str | None]], n: int
    ) -> tuple[list[int], list[float]]:
        """Full-query BM25 over a candidate set. Reproduces FTS5's bm25() (k1/b,
        ln((N-df+0.5)/(df+0.5)) IDF with the 1e-6 clamp) plus a small SDM-lite
        ordered-bigram proximity term (_bm25_topn). df comes from the shared df cache;
        df-0 tokens (typos) get the clamp floor, never full weight."""
        qtoks = _fold_tokens(qtext)[:100]
        if not qtoks or not cand:
            return [], []
        total = sum(self.indexed_counts.values())
        idf = {}
        for t in dict.fromkeys(qtoks):
            df = self._df(t)
            idf[t] = max(math.log((total - df + 0.5) / (df + 0.5)), 1e-6)
        rids = [rid for rid, _ in cand]
        texts = [text for _, text in cand]
        nat = self._scorer()
        topn = nat.bm25_topn if nat else _bm25_topn
        return topn(qtoks, idf, rids, texts, self._avgdl(), n, BM25_K1, BM25_B, SDM_WEIGHT)

    def _text_ids(
        self, qtext: str, n: int, scope: str, filt: dict | None
    ) -> tuple[list[int], list[float]]:
        """Top-n by BM25. Score is positive BM25 points, higher = better. When the
        pruner dropped tokens, FTS5 only generates candidates and stage 2 restores
        full-query ranking (see the TEXT_* constants); otherwise the single FTS5
        query IS the full ranking."""
        kept, toks = self._prune_common(qtext)
        if not kept:
            return [], []
        two_stage = len(kept) < len(toks)
        match, limit = _or_query(kept), TEXT_OR_CAND if two_stage else n
        db = self._rdb()
        other = {"chunks": "summary", "summaries": "chunk"}.get(scope)
        plain = not filt and not (other and self.indexed_counts.get(other, 0))
        if not plain:
            where, params = _filter_sql(scope, filt)
        if plain:
            # nothing to exclude: skip the per-match join back to records (~40% of the
            # query cost). FTS rows mirror live records exactly (trigger-maintained),
            # and rank IS bm25 in fts5.
            rows = db.execute(
                "SELECT rowid, -rank FROM records_fts WHERE records_fts MATCH ?"
                " ORDER BY rank LIMIT ?",
                [match, limit],
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT r.id, -bm25(records_fts) FROM records_fts"
                " JOIN records r ON r.id = records_fts.rowid"
                f" WHERE records_fts MATCH ? AND {where}"
                " ORDER BY bm25(records_fts) LIMIT ?",
                [match, *params, limit],
            ).fetchall()
        if not two_stage:
            return [r[0] for r in rows], [r[1] for r in rows]
        # stage 1b: docs containing EVERY query token — unranked on purpose (rank on a
        # broad expression walks each phrase's whole posting list for IDF)
        m_and = _and_query(toks)
        if plain:
            and_rows = db.execute(
                "SELECT rowid FROM records_fts WHERE records_fts MATCH ? LIMIT ?",
                [m_and, TEXT_AND_CAND],
            ).fetchall()
        else:
            and_rows = db.execute(
                "SELECT r.id FROM records_fts JOIN records r ON r.id = records_fts.rowid"
                f" WHERE records_fts MATCH ? AND {where} LIMIT ?",
                [m_and, *params, TEXT_AND_CAND],
            ).fetchall()
        cand_ids = list(dict.fromkeys([r[0] for r in rows] + [r[0] for r in and_rows]))
        texts = dict(_rows_by_id(db, "SELECT id, text FROM records WHERE id IN ({})", cand_ids))
        return self._bm25_rescore(qtext, [(rid, texts.get(rid)) for rid in cand_ids], n)

    async def search(
        self,
        mode: str,
        qvec: np.ndarray | None,
        qtext: str | None,
        k: int,
        scope: str,
        filt: dict | None,
        expand,
        nprobe: int | None = None,
    ) -> list[dict]:
        n = max(HYBRID_DEPTH, k) if mode == "hybrid" else k  # per-leg depth so RRF sees the tail
        async with self.lock.read():
            if self._closed:  # queued for the read lock behind stop()'s write section
                if self.deleted:
                    raise CollectionDeletedError(self.cfg.name)
                raise RuntimeError(f"collection '{self.cfg.name}' is closed")
            if mode == "vector":
                ids, scores = await self._vector_ids(qvec, n, scope, filt, nprobe)
            elif mode == "text":
                ids, scores = await asyncio.to_thread(self._text_ids, qtext, n, scope, filt)
            else:  # hybrid: legs in parallel — sqlite releases the GIL, so they overlap
                (v_ids, _), (t_ids, _) = await asyncio.gather(
                    self._vector_ids(qvec, n, scope, filt, nprobe),
                    asyncio.to_thread(self._text_ids, qtext, n, scope, filt),
                )
                ids, scores = _rrf(k, v_ids, t_ids)
            hits = self._hydrate(ids, scores)
            if expand:
                for hit in hits:
                    await self._expand(hit, qvec, qtext, expand)
        return hits

    async def _expand(self, hit: dict, qvec: np.ndarray | None, qtext: str | None, expand) -> None:
        doc_id, ext = hit["doc_id"], {}
        self_clause = "AND external_id != ?" if hit["type"] == "chunk" else ""
        self_param = [hit["id"]] if hit["type"] == "chunk" else []
        if expand.siblings_topk and qvec is None:
            # text mode: rank siblings by BM25 (siblings matching no query term are
            # omitted; pruned matching is fine doc-scoped — the match set is tiny)
            match = _or_query(self._prune_common(qtext or "")[0])
            rows = await asyncio.to_thread(
                lambda: self._rdb().execute(
                    "SELECT r.id, -bm25(records_fts) FROM records_fts"
                    " JOIN records r ON r.id = records_fts.rowid"
                    f" WHERE records_fts MATCH ? AND doc_id=? AND type='chunk' AND indexed=1 {self_clause}"
                    " ORDER BY bm25(records_fts) LIMIT ?",
                    [match, doc_id, *self_param, expand.siblings_topk],
                ).fetchall()
            ) if match else []
            ext["siblings"] = self._hydrate([r[0] for r in rows], [r[1] for r in rows])
        elif expand.siblings_topk:
            sib = [
                r[0]
                for r in self._rdb().execute(
                    f"SELECT id FROM records WHERE doc_id=? AND type='chunk' AND indexed=1 {self_clause}",
                    [doc_id, *self_param],
                )
            ]
            if sib:
                allow = np.array(sib, dtype=np.uint64)
                rescored = await asyncio.to_thread(
                    self._search_rescored, qvec, [expand.siblings_topk], allowlist=allow
                )
                ext["siblings"] = self._hydrate(*rescored[0])
            else:
                ext["siblings"] = []
        elif expand.siblings_all:
            sib = [
                r[0]
                for r in self._rdb().execute(
                    f"SELECT id FROM records WHERE doc_id=? AND type='chunk' {self_clause}"
                    " ORDER BY position, id",
                    [doc_id, *self_param],
                )
            ]
            ext["siblings"] = self._hydrate(sib)
        if expand.summary and hit["type"] != "summary":
            row = self._rdb().execute(
                "SELECT id FROM records WHERE doc_id=? AND type='summary'", (doc_id,)
            ).fetchone()
            ext["summary"] = self._hydrate([row[0]])[0] if row else None
        if ext:
            hit["expansion"] = ext

    def get_document(self, doc_id: str) -> dict | None:
        # same read connection as _hydrate: mixing self.db here would see the ingest
        # worker's uncommitted rows and then hydrate them against the committed
        # snapshot, silently dropping chunks mid-upsert
        ids = [
            r[0]
            for r in self._rdb().execute(
                "SELECT id FROM records WHERE doc_id=? ORDER BY type DESC, position, id", (doc_id,)
            )
        ]
        if not ids:
            return None
        recs = self._hydrate(ids)
        return {
            "doc_id": doc_id,
            "summary": next((r for r in recs if r["type"] == "summary"), None),
            "chunks": [r for r in recs if r["type"] == "chunk"],
        }

    async def delete_document(self, doc_id: str) -> int:
        async with self.lock.write():
            # off the event loop: the bulk DELETE fires the FTS trigger per row
            deleted = await asyncio.to_thread(self._delete_doc_rows, doc_id)
            if deleted:
                await asyncio.to_thread(self._sync_index)
        return deleted

    def _delete_doc_rows(self, doc_id: str) -> int:
        with self.db_lock:
            try:
                rows = self.db.execute(
                    "SELECT id, indexed, type FROM records WHERE doc_id=?", (doc_id,)
                ).fetchall()
                self.db.execute(
                    "DELETE FROM vecs WHERE id IN (SELECT id FROM records WHERE doc_id=?)", (doc_id,)
                )
                self.db.execute("DELETE FROM records WHERE doc_id=?", (doc_id,))
                self.db.commit()
            except BaseException:
                self.db.rollback()  # as in _upsert_rows: the next commit must not persist half a delete
                raise
            # published after the commit, still under db_lock: copy-on-write, one rebind
            counts = dict(self.indexed_counts)
            for _, indexed, rtype in rows:
                if indexed:
                    counts[rtype] -= 1
            self.indexed_counts = counts
            # the rows are gone for good, so only now do their ids leave the index; still
            # under db_lock, as the next upsert reads MAX(id) there and would reuse them,
            # even when a cancelled caller has already released lock.write()
            self._unindex([rid for rid, indexed, _ in rows if indexed])
        self._invalidate_allow()
        self._df_cache_churn += len(rows)
        return len(rows)

    def list_records(
        self, scope: str, filt: dict | None, sort: str | None, limit: int, offset: int,
        include_vector: bool = False,
    ) -> dict:
        """Unranked filtered listing with total count — the no-query counterpart of
        search (browse, page, count per filter, export). Same scope/filter grammar."""
        where, params = _filter_sql(scope, filt)
        order, oparams = "id", []
        if sort:
            if not re.fullmatch(r"-?[\w.]+", sort):
                raise ValueError("sort must be a metadata key, optionally prefixed with '-' for descending")
            # ponytail: ORDER BY json_extract scans the filtered set (no expression index);
            # an expression index per hot sort key is the follow-up (spec §5.2), not C
            order = f"json_extract(metadata, ?) {'DESC' if sort[0] == '-' else 'ASC'}, id"
            oparams = ["$." + sort.lstrip("-")]
        db = self._rdb()
        total = db.execute(f"SELECT COUNT(*) FROM records WHERE {where}", params).fetchone()[0]
        ids = [
            r[0]
            for r in db.execute(
                # NOT INDEXED (D9): with idx_records_doc_type present the planner walks
                # the covering index in doc_id order and fetches each row by rowid (p1:
                # 0.91 -> 2.73 s warm at 2.55M; ANALYZE does not fix it). NOT INDEXED
                # keeps the rowid-order table scan and still allows rowid lookups, so
                # the default ORDER BY id page still stops after LIMIT rows.
                f"SELECT id FROM records NOT INDEXED WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, *oparams, limit, offset],
            )
        ]
        recs = self._hydrate(ids)
        if include_vector:  # decoded fp16 originals, keyed by external id (immune to a racing delete)
            blobs = dict(_rows_by_id(
                db, "SELECT r.external_id, v.vec FROM records r JOIN vecs v ON v.id = r.id WHERE r.id IN ({})", ids
            ))
            for rec in recs:
                blob = blobs.get(rec["id"])
                rec["vector"] = (
                    np.frombuffer(blob, dtype=np.float16).astype(np.float32).tolist()
                    if blob is not None and len(blob) == self.cfg.dim * 2 else None
                )
        return {"records": recs, "total": total}

    async def patch_metadata(self, doc_id: str, patch: dict, apply_to_chunks: bool) -> int:
        """RFC 7396 merge-patch the metadata of a document's records (null deletes a
        key). Metadata is neither embedded nor FTS-indexed, so this is one UPDATE."""
        async with self.lock.write():
            return await asyncio.to_thread(self._patch_rows, doc_id, patch, apply_to_chunks)

    def _patch_rows(self, doc_id: str, patch: dict, apply_to_chunks: bool) -> int:
        types = "" if apply_to_chunks else " AND type='summary'"
        with self.db_lock:
            cur = self.db.execute(
                f"UPDATE records SET metadata = json_patch(metadata, ?) WHERE doc_id=?{types}",
                (json.dumps(patch), doc_id),
            )
            self.db.commit()
        self._invalidate_allow()  # cached allowlists were evaluated over the old metadata
        return cur.rowcount

    def stats(self) -> dict:
        counts = dict(
            self._rdb().execute("SELECT type, COUNT(*) FROM records GROUP BY type").fetchall()
        )
        docs = self._rdb().execute("SELECT COUNT(DISTINCT doc_id) FROM records").fetchone()[0]
        return {
            "documents": docs,
            "chunks": counts.get("chunk", 0),
            "summaries": counts.get("summary", 0),
            "pending_jobs": self.pending_jobs(),
        }


class CollectionManager:
    def __init__(self, settings: Settings, embedder_factory=None) -> None:
        self.settings = settings
        self.embedder_factory = embedder_factory or self._default_embedder
        self.data_dir = Path(settings.data_dir)
        (self.data_dir / "collections").mkdir(parents=True, exist_ok=True)
        self.catalog = sqlite3.connect(self.data_dir / "catalog.db", check_same_thread=False)
        self.catalog.execute(
            "CREATE TABLE IF NOT EXISTS collections("
            "name TEXT PRIMARY KEY, dim INT, bit_width INT, model TEXT, base_url TEXT,"
            "key_hash TEXT, created_at TEXT, tokenizer TEXT DEFAULT 'unicode61',"
            "index_config TEXT)"
        )
        have = [r[1] for r in self.catalog.execute("PRAGMA table_info(collections)")]
        for col, ddl in (  # migrate catalogs created before hybrid search / the IVF index
            ("tokenizer", "tokenizer TEXT DEFAULT 'unicode61'"),
            ("index_config", "index_config TEXT"),
        ):
            if col not in have:
                self.catalog.execute(f"ALTER TABLE collections ADD COLUMN {ddl}")
                self.catalog.commit()
        # EVERY catalog statement (reads too) runs under this lock: require_collection
        # is a sync FastAPI dependency, so get_config runs on threadpool threads while
        # the loop creates/deletes rows and attach/detach jobs update index_config.
        # One pysqlite connection used concurrently returns another query's row or
        # raises InterfaceError (ADR 0001, addendum 2026-09)
        self._catalog_lock = threading.Lock()
        self.resident: dict[str, Collection] = {}
        self._load_lock = asyncio.Lock()

    def _default_embedder(self, cfg: CollectionConfig) -> Embedder:
        s = self.settings
        return Embedder(
            cfg.base_url or s.embedding_base_url,
            s.embedding_api_key,
            cfg.model or s.embedding_model,
        )

    def _dir(self, name: str) -> Path:
        return self.data_dir / "collections" / name

    def bm25_backend(self) -> str:
        """The stage-2 scorer collections run, as GET /healthz reports it."""
        if _native is not None and self.settings.native_bm25 == "auto":
            return "native"
        return "python"

    def get_config(self, name: str) -> CollectionConfig | None:
        with self._catalog_lock:
            row = self.catalog.execute(
                "SELECT name, dim, bit_width, model, base_url, key_hash, tokenizer, index_config"
                " FROM collections WHERE name=?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return CollectionConfig(
            *row[:6], row[6] or "unicode61", json.loads(row[7]) if row[7] else None
        )

    def set_index_config(self, name: str, ic: dict | None) -> None:
        with self._catalog_lock:
            self.catalog.execute(
                "UPDATE collections SET index_config=? WHERE name=?",
                (json.dumps(ic) if ic else None, name),
            )
            self.catalog.commit()

    def list_collections(self) -> list[str]:
        with self._catalog_lock:  # fetchall inside: the cursor is the shared state
            rows = self.catalog.execute("SELECT name FROM collections ORDER BY name").fetchall()
        return [r[0] for r in rows]

    async def create_collection(
        self,
        name: str,
        dim: int | None,
        bit_width: int,
        model: str | None,
        base_url: str | None,
        collection_key: str | None,
        tokenizer: str = "unicode61",
    ) -> CollectionConfig:
        if tokenizer not in FTS_TOKENIZERS:
            raise ValueError(f"tokenizer must be one of {sorted(FTS_TOKENIZERS)}")
        if self.get_config(name):
            raise ValueError(f"collection '{name}' already exists")
        if dim is None:
            dim = self.settings.embedding_dim
        if dim is None:  # probe the embedding endpoint for the dimension
            probe_cfg = CollectionConfig(name, 0, bit_width, model, base_url, None)
            embedder = self.embedder_factory(probe_cfg)
            try:
                dim = len((await embedder.embed(["dimension probe"]))[0])
            finally:
                await embedder.aclose()
        if dim <= 0 or dim % 8:  # turbovec constraint
            raise ValueError(f"dim must be a positive multiple of 8, got {dim}")
        cfg = CollectionConfig(name, dim, bit_width, model, base_url,
                               hash_key(collection_key) if collection_key else None, tokenizer)
        directory = self._dir(name)
        if directory.exists():  # leftover from a crashed create or failed delete: never resurrect
            await asyncio.to_thread(shutil.rmtree, directory)
        directory.mkdir(parents=True)
        open_meta_db(directory / "meta.db", tokenizer).close()
        with self._catalog_lock:
            self.catalog.execute(
                "INSERT INTO collections(name, dim, bit_width, model, base_url, key_hash,"
                " created_at, tokenizer) VALUES (?,?,?,?,?,?,?,?)",
                (name, dim, bit_width, model, base_url, cfg.key_hash, _now(), tokenizer),
            )
            self.catalog.commit()
        return cfg

    async def touch(self, name: str) -> Collection:
        """Return the resident collection, loading (and LRU-evicting) as needed."""
        c = self.resident.get(name)
        if c is not None:  # resident: never queue behind another collection's load
            c.last_used = time.monotonic()
            return c
        async with self._load_lock:
            c = self.resident.get(name)
            if c is None:
                cfg = self.get_config(name)
                if cfg is None:
                    raise KeyError(name)
                while len(self.resident) >= self.settings.max_resident_collections:
                    victims = sorted(
                        (v for v in self.resident.values() if v.pending_jobs() == 0),
                        key=lambda v: v.last_used,
                    )
                    if not victims:
                        break  # everyone is busy ingesting; allow going over budget
                    await self._evict(victims[0].cfg.name)
                c = await self._construct(name, cfg)
            c.last_used = time.monotonic()
            return c

    async def _construct(self, name: str, cfg: CollectionConfig) -> Collection:
        """Build a Collection on a worker thread (index load, the one-time meta.db
        migration, the reconcile scans: seconds to minutes cold), so the loop keeps
        serving /healthz and the resident collections. The caller holds _load_lock.
        If the awaiting request is cancelled mid-build, the build still finishes and
        is registered before the cancellation propagates. Releasing the lock early
        would let the next request build a second copy, and the orphan would keep open
        SQLite handles while its replayed jobs had no worker."""
        t0 = time.monotonic()
        build = asyncio.ensure_future(asyncio.to_thread(
            Collection, cfg, self._dir(name), lambda: self.embedder_factory(cfg),
            lambda ic, name=name: self.set_index_config(name, ic),
            native_bm25=self.settings.native_bm25 == "auto",
            ivf_search_threads=self.settings.ivf_search_threads,
        ))
        try:
            c = await asyncio.shield(build)
        except asyncio.CancelledError:
            while not build.done():
                try:
                    await asyncio.wait({build})
                except asyncio.CancelledError:
                    pass  # cancelled again: still must not orphan the build
            if build.cancelled() or build.exception() is not None:
                raise
            self._register(name, build.result())
            raise
        self._register(name, c)
        _log.info("collection %s loaded in %.2f s", name, time.monotonic() - t0)
        return c

    def _register(self, name: str, c: Collection) -> None:
        c.start_worker()  # on the loop: create_task needs the running loop
        self.resident[name] = c

    async def _evict(self, name: str) -> None:
        c = self.resident.pop(name, None)
        if c:
            await c.stop()

    async def delete_collection(self, name: str) -> None:
        # evict + catalog delete under _load_lock, like touch() and housekeeping's
        # evictions: stop() yields, and a touch() landing there would reload the
        # collection from the still-present row and files (a resident zombie on
        # deleted files that would also shadow a re-created collection of the same
        # name). Once the row is gone a racing touch() raises KeyError (404), so the
        # rmtree can run unlocked.
        async with self._load_lock:
            c = self.resident.get(name)
            if c is not None:  # its queued searches answer 404 once stop() closes it
                c.deleted = True
            await self._evict(name)
            with self._catalog_lock:
                self.catalog.execute("DELETE FROM collections WHERE name=?", (name,))
                self.catalog.commit()
        await asyncio.to_thread(shutil.rmtree, self._dir(name), True)

    async def resume_pending(self) -> None:
        """On boot, load any collection with unfinished jobs so its worker replays them."""
        for name in self.list_collections():
            db = sqlite3.connect(self._dir(name) / "meta.db")
            pending = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
            ).fetchone()[0]
            db.close()
            if pending:
                await self.touch(name)

    async def housekeeping(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff = time.monotonic() - self.settings.collection_idle_ttl
            async with self._load_lock:
                for name, c in list(self.resident.items()):
                    if c.last_used < cutoff and c.pending_jobs() == 0:
                        await self._evict(name)

    async def shutdown(self) -> None:
        for name in list(self.resident):
            await self._evict(name)
        with self._catalog_lock:  # a threadpool get_config may still be mid-query
            self.catalog.close()
