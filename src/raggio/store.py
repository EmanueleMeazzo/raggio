import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from turbovec import IdMapIndex

from .config import Settings
from .embeddings import Embedder

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

# Optional ScaNN-style IVF index, attached/removed per collection via the index API.
# Measured (bench/ivf_probe.py, ADR 0002): at ~550k rows every recall-preserving cell
# is slower or barely faster than the flat scan (~0.4ms fixed cost per probed shard),
# so it stays opt-in; at 2.2M rows it wins 3.5-6.8x. nprobe=16 keeps recall@10 >=0.95
# on the real corpus. Fixed RAM cost is ~0.5-1 MB per shard (bench/shard_mem_probe.py).
IVF_DEFAULT_NPROBE = 16
IVF_MIN_ROWS = 1024  # k-means needs a training corpus; below this, attach is refused
IVF_TRAIN_SAMPLE = 65_536
IVF_BUILD_BLOCK = 16_384  # rows per streamed rebuild block (~100 MB f32 at 1536-d)


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


def _filter_sql(scope: str, filt: dict | None) -> tuple[str, list]:
    """Build WHERE clause for records: type scope + metadata equality / range filters."""
    clauses, params = ["indexed = 1"], []
    if scope == "chunks":
        clauses.append("type = 'chunk'")
    elif scope == "summaries":
        clauses.append("type = 'summary'")
    for key, val in (filt or {}).items():
        path = "$." + key
        if isinstance(val, dict):
            for op, bound in val.items():
                if op not in RANGE_OPS:
                    raise ValueError(f"unsupported filter operator: {op}")
                clauses.append(f"json_extract(metadata, ?) {RANGE_OPS[op]} ?")
                params.extend([path, bound])
        else:
            clauses.append("json_extract(metadata, ?) = ?")
            params.extend([path, val])
    return " AND ".join(clauses), params


def _rows_by_id(db, sql: str, ids: list[int]):
    """Point-fetch by id list: run `sql` (one {} slot for the qmarks) in chunks of
    512 ids, comfortably under SQLite's bound-parameter limit."""
    for s in range(0, len(ids), 512):
        chunk = ids[s : s + 512]
        yield from db.execute(sql.format(",".join("?" * len(chunk))), chunk)


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
        CREATE INDEX IF NOT EXISTS idx_records_doc ON records(doc_id);
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY,
            payload TEXT,
            status TEXT,
            error TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    _ensure_fts(db, tokenizer)
    # doc-frequency lookups for query-token pruning; references records_fts by name so
    # it survives an _ensure_fts rebuild
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts_v USING fts5vocab(records_fts, 'row')")
    return db


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
    # ~8k rows per shard, power of two: smaller shards pay more in per-shard fixed
    # search cost (~0.4ms each) than they save in rows scanned
    return int(np.clip(2 ** round(np.log2(max(n, 1) / 8192)), 16, 1024))


class _IvfIndex:
    """ScaNN-style coarse partitioning: k-means centroids route each vector to one
    IdMapIndex shard; a query scans only the nprobe closest shards, trading a little
    recall (tunable per query) for skipping most of the corpus. Duck-types the slice
    of IdMapIndex that Collection uses, so the resident index is either kind."""

    def __init__(self, centroids: np.ndarray, shards: list, nprobe: int) -> None:
        self.centroids = centroids  # (nlist, dim) f32, unit norm, static after train
        self.shards = shards
        self.nprobe = nprobe
        # per-shard id arrays for allowlist intersection (turbovec rejects allowlist
        # ids an index doesn't hold): built lazily via a full-k probe, dropped for any
        # shard a write touches. ~8 bytes/row when filtered queries occur, else nothing.
        self._id_cache: list = [None] * len(shards)

    @property
    def nlist(self) -> int:
        return len(self.shards)

    @staticmethod
    def _assign(mat: np.ndarray, C: np.ndarray) -> np.ndarray:
        return np.concatenate(
            [(mat[s : s + 8192] @ C.T).argmax(1) for s in range(0, len(mat), 8192)]
        )

    @classmethod
    def train(cls, sample: np.ndarray, nlist: int, dim: int, bit_width: int, nprobe: int):
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
        return cls(np.ascontiguousarray(C), shards, nprobe)

    @classmethod
    def load(cls, directory: Path, dim: int, bit_width: int, nprobe: int):
        C = np.load(directory / "centroids.npy")
        shards = []
        for j in range(len(C)):
            p = directory / f"shard-{j:04d}.tvim"
            shards.append(
                IdMapIndex.load(str(p)) if p.exists() else IdMapIndex(dim=dim, bit_width=bit_width)
            )
        return cls(C, shards, nprobe)

    def sync(self, directory) -> None:
        d = Path(directory)
        d.mkdir(exist_ok=True)
        cpath = d / "centroids.npy"
        if not cpath.exists():  # static after train; shard syncs are incremental
            np.save(cpath, self.centroids)
        for j, sh in enumerate(self.shards):
            sh.sync(str(d / f"shard-{j:04d}.tvim"))

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

    def add_with_ids(self, mat: np.ndarray, ids: np.ndarray) -> None:
        asg = self._assign(mat, self.centroids)
        for j in np.unique(asg):
            m = asg == j
            self.shards[j].add_with_ids(np.ascontiguousarray(mat[m]), ids[m])
            self._id_cache[j] = None

    def remove(self, rid: int) -> None:
        # ponytail: O(nlist) contains scan (~us each) beats maintaining an id->shard map
        for j, sh in enumerate(self.shards):
            if sh.contains(rid):
                sh.remove(rid)
                self._id_cache[j] = None
                return

    def _shard_ids(self, j: int) -> np.ndarray:
        ids = self._id_cache[j]
        if ids is None:
            sh = self.shards[j]
            if len(sh):
                probe = np.zeros((1, self.centroids.shape[1]), dtype=np.float32)
                probe[0, 0] = 1.0
                ids = np.sort(sh.search(probe, k=len(sh))[1][0])  # sorted: intersections
                # use searchsorted instead of a per-call re-sort
            else:
                ids = np.empty(0, np.uint64)
            self._id_cache[j] = ids  # racing readers compute the same array; last wins
        return ids

    def _intersect(self, allow: np.ndarray, j: int) -> np.ndarray:
        """allow ∩ shard j's ids (both uint64; shard side pre-sorted)."""
        sids = self._shard_ids(j)
        if not len(sids):
            return sids
        pos = np.minimum(np.searchsorted(sids, allow), len(sids) - 1)
        return allow[sids[pos] == allow]

    def search(self, queries: np.ndarray, k: int, allowlist=None, nprobe: int | None = None):
        """Merged top-k over probed shards. Mirrors IdMapIndex.search: single-query
        results are trimmed exactly; a batch is rectangular, short rows padded with
        id 0 / -inf score (record ids start at 1, so padding never hydrates)."""
        if allowlist is not None and len(allowlist) <= 128:
            # tiny allowlists (metadata filters, sibling expansion) must not lose
            # rows to unprobed shards: probe exactly the shards that own them
            owners = [
                j for j in range(self.nlist)
                if len(self.shards[j]) and len(self._intersect(allowlist, j))
            ]
            probes = [owners] * len(queries)
        else:
            npb = min(nprobe or self.nprobe, self.nlist)
            sims = queries @ self.centroids.T
            probes = [
                np.argpartition(-sims[qi], npb - 1)[:npb] for qi in range(len(queries))
            ]
        out = []
        for qi, probe in enumerate(probes):
            q = np.ascontiguousarray(queries[qi : qi + 1])
            parts_s, parts_i = [], []
            for j in probe:
                sh = self.shards[j]
                if not len(sh):
                    continue
                allow = allowlist
                if allow is not None:  # per-shard slice: turbovec rejects foreign ids
                    allow = self._intersect(allowlist, j)
                    if not len(allow):
                        continue
                s, i = sh.search(q, k=min(k, len(sh)), allowlist=allow)
                parts_s.append(s[0])
                parts_i.append(i[0])
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


class Collection:
    """A resident collection: turbovec index + sqlite metadata + ingest worker."""

    def __init__(
        self, cfg: CollectionConfig, directory: Path, embedder_factory, set_index_config=None
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
        # the catalog decides which representation is live; a stale sibling on disk
        # (crashed attach/detach) is ignored and rebuilt by the replayed job
        if cfg.index_config and (self.ivf_dir / "centroids.npy").exists():
            self.index = _IvfIndex.load(
                self.ivf_dir, cfg.dim, cfg.bit_width,
                cfg.index_config.get("nprobe", IVF_DEFAULT_NPROBE),
            )
        elif self.index_path.exists():
            self.index = IdMapIndex.load(str(self.index_path))
        else:
            self.index = IdMapIndex(dim=cfg.dim, bit_width=cfg.bit_width)
        self.index.prepare()  # warm search caches at load, not on the first query
        # per-type indexed-row counts, kept in step by _upsert_rows/_delete_doc_rows:
        # lets search skip the allowlist (and its full-table id fetch) when nothing
        # would be excluded — the allowlist path costs ~15x a plain scan at 500k rows
        self.indexed_counts: dict[str, int] = dict(
            self.db.execute("SELECT type, COUNT(*) FROM records WHERE indexed=1 GROUP BY type")
        )
        self.lock = _RWLock()  # searches share; ingest/delete/sync are exclusive
        self._scan_queue: list = []  # (qvec, n, future) waiting for a batched scan
        self._scan_task: asyncio.Task | None = None
        self._allow_cache: dict[str, np.ndarray] = {}  # (scope, filter) -> allowlist ids
        # folded token -> doc frequency: a fts5vocab df lookup walks the term's whole
        # doclist (~15-30ms for near-universal tokens), and exactly those hot tokens
        # recur in every query — cached, pruning costs ~0 after warmup
        self._df_cache: dict[str, int] = {}
        self._df_cache_churn = 0  # rows written since the df cache was last (re)built
        # mean folded-token doc length for Python BM25; GIL-atomic swap, no lock —
        # concurrent recomputes land on the same value. Invalidated with the df cache.
        self._avgdl_cache: float | None = None
        # search-path reads use one connection per thread: concurrent readers on the
        # shared self.db raise SQLITE_MISUSE (pysqlite connections aren't concurrency-
        # safe), and WAL makes independent read connections cheap and non-blocking
        self._read_local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._closed = False  # set by stop(); makes stale searches fail closed instead
        # of resurrecting connections on a dead collection (leaks the handle and, on
        # Windows, keeps the deleted collection dir undeletable)
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
        live = {r[0] for r in self.db.execute("SELECT id FROM records WHERE indexed=1")}
        ghosts = [int(i) for i in all_ids if int(i) not in live]
        for g in ghosts:
            self.index.remove(g)
        if ghosts:
            self._sync_index()

    # ---- worker / ingest queue ----

    def start_worker(self) -> None:
        self._worker = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        async with self.lock.write():
            await asyncio.to_thread(self._sync_index)
            self._closed = True
        for c in self._read_conns:
            c.close()
        self.db.close()
        if self._embedder is not None:
            await self._embedder.aclose()

    async def enqueue(self, payload: dict) -> int:
        # journaling a bulky payload is real I/O: run the transaction in a thread and
        # only touch the (non-thread-safe) wake event back on the loop
        job_id = await asyncio.to_thread(self._enqueue_row, json.dumps(payload))
        self._wake.set()
        return job_id

    def _enqueue_row(self, payload_json: str) -> int:
        with self.db_lock:
            # explicit id, not lastrowid: MAX+1 is race-free under db_lock
            job_id = self.db.execute("SELECT COALESCE(MAX(id),0)+1 FROM jobs").fetchone()[0]
            self.db.execute(
                "INSERT INTO jobs(id, payload, status, created_at, updated_at)"
                " VALUES (?, ?, 'pending', ?, ?)",
                (job_id, payload_json, _now(), _now()),
            )
            self.db.commit()
        return job_id

    def pending_jobs(self) -> int:
        return self._rdb().execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
        ).fetchone()[0]

    def _claim_next(self) -> tuple | None:
        # 'processing' included so jobs interrupted by a crash are replayed on boot
        with self.db_lock:
            row = self.db.execute(
                "SELECT id, payload FROM jobs WHERE status IN ('pending','processing') ORDER BY id LIMIT 1"
            ).fetchone()
            if row is not None:
                self.db.execute(
                    "UPDATE jobs SET status='processing', updated_at=? WHERE id=?", (_now(), row[0])
                )
                self.db.commit()
            return row

    def _finish_job(self, job_id: int, status: str, error: str | None) -> None:
        with self.db_lock:
            # payload cleared on success so vector-heavy jobs don't accumulate on disk;
            # kept on error for diagnosis
            self.db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=?,"
                " payload=CASE WHEN ?='done' THEN NULL ELSE payload END WHERE id=?",
                (status, error, _now(), status, job_id),
            )
            self.db.commit()
            # return the cleared payload's pages to the OS between jobs. fetchall() is
            # load-bearing: the pragma frees pages per STEP, and pysqlite's execute()
            # steps once — without exhausting the cursor it frees a single page
            self.db.execute("PRAGMA incremental_vacuum").fetchall()

    async def _run_worker(self) -> None:
        while True:
            # clear BEFORE claiming: an enqueue landing during the claim either becomes
            # visible to the claim itself or re-sets the event, so no wakeup is lost
            self._wake.clear()
            row = await asyncio.to_thread(self._claim_next)
            if row is None:
                await self._wake.wait()
                continue
            job_id, payload = row
            try:
                await self._process_job(json.loads(payload))
                status, error = "done", None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status, error = "error", str(e)
            await asyncio.to_thread(self._finish_job, job_id, status, error)

    async def _process_job(self, payload: dict) -> None:
        op = payload.get("op")
        if op == "attach_index":
            await self._attach_index(payload)
            return
        if op == "detach_index":
            await self._detach_index()
            return
        # flatten documents into records: (external_id, doc_id, type, position, text, metadata, vector)
        rows = []
        for d in payload["documents"]:
            if d.get("summary"):
                s = d["summary"]
                rows.append([d["doc_id"], d["doc_id"], "summary", None, s.get("text"), s.get("metadata"), s.get("vector")])
            for i, c in enumerate(d.get("chunks") or []):
                pos = c.get("position") if c.get("position") is not None else i
                rows.append([c["id"], d["doc_id"], "chunk", pos, c.get("text"), c.get("metadata"), c.get("vector")])
        if not rows:
            return
        # last occurrence wins: a duplicate external_id within one payload would
        # otherwise leave the first copy's id in the vector index (the in-batch
        # replace can't remove it — it isn't added until after the row loop)
        rows = list({r[0]: r for r in rows}.values())
        need = [i for i, r in enumerate(rows) if r[6] is None]
        for start in range(0, len(need), 64):
            batch = need[start : start + 64]
            vecs = await self.embedder.embed([rows[i][4] for i in batch])
            for i, v in zip(batch, vecs):
                rows[i][6] = v
        mat = _normalize(np.array([r[6] for r in rows], dtype=np.float32))

        async with self.lock.write():
            # off the event loop: the FTS triggers tokenize every row (expensive with
            # trigram). Batch atomicity relies on job replay + idempotent upserts, not
            # on one transaction, so an interleaved commit (e.g. enqueue) is harmless.
            ids, fresh = await asyncio.to_thread(self._upsert_rows, rows, mat)
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

    def _upsert_rows(self, rows: list, mat: np.ndarray) -> tuple[list[int], list[bool]]:
        # fp16 originals retained on disk (half the f32 size, negligible loss vs the
        # 4-bit codes): the only way to rebuild the index representation later, since
        # turbovec can't enumerate or reconstruct vectors
        vecs16 = mat.astype(np.float16)
        ids, fresh = [], []
        with self.db_lock:
            # explicit ids, not lastrowid: records are only inserted here, in the single worker
            next_id = self.db.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0] + 1
            for n, (ext_id, doc_id, rtype, pos, text, meta, _) in enumerate(rows):
                old = self.db.execute(
                    "SELECT id, indexed, type FROM records WHERE external_id=?", (ext_id,)
                ).fetchone()
                fresh.append(old is None)
                if old:  # upsert: replace record; makes crash-replay of a job idempotent
                    if old[1]:
                        self.index.remove(old[0])
                        self.indexed_counts[old[2]] -= 1
                    self.db.execute("DELETE FROM records WHERE id=?", (old[0],))
                    self.db.execute("DELETE FROM vecs WHERE id=?", (old[0],))
                self.db.execute(
                    "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
                    " VALUES (?,?,?,?,?,?,?,1)",
                    (next_id, ext_id, doc_id, rtype, pos, text, json.dumps(meta or {}), ),
                )
                self.db.execute(
                    "INSERT INTO vecs(id, vec) VALUES (?,?)", (next_id, vecs16[n].tobytes())
                )
                self.indexed_counts[rtype] = self.indexed_counts.get(rtype, 0) + 1
                ids.append(next_id)
                next_id += 1
            self.db.commit()
        self._allow_cache.clear()
        self._df_cache_churn += len(rows)
        return ids, fresh

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
        rows = self._rdb().execute(
            "SELECT v.vec FROM vecs v JOIN records r ON r.id=v.id WHERE r.indexed=1"
            " ORDER BY RANDOM() LIMIT ?", (k,),
        ).fetchall()
        if not rows:
            return np.empty((0, self.cfg.dim), np.float32)
        mat = np.frombuffer(b"".join(r[0] for r in rows), dtype=np.float16)
        return mat.reshape(len(rows), -1).astype(np.float32)

    async def _backfill_vecs(self) -> None:
        """Records ingested before vector retention lack the fp16 original a rebuild
        needs. Re-embed from text where possible (assumes the collection's configured
        embedding model produced the stored vectors); otherwise fail the job with a
        count so the caller knows to re-ingest."""
        rows = await asyncio.to_thread(
            lambda: self._rdb().execute(
                "SELECT r.id, r.text FROM records r LEFT JOIN vecs v ON v.id=r.id"
                " WHERE r.indexed=1 AND v.id IS NULL"
            ).fetchall()
        )
        if not rows:
            return
        no_text = sum(1 for _, t in rows if not t)
        if no_text:
            raise ValueError(
                f"{no_text} records have neither a retained vector nor text;"
                " re-ingest them before attaching an index"
            )
        for s in range(0, len(rows), 64):
            batch = rows[s : s + 64]
            vecs = await self.embedder.embed([t for _, t in batch])
            vecs16 = _normalize(np.array(vecs, dtype=np.float32)).astype(np.float16)

            def write():
                with self.db_lock:
                    for (rid, _), v in zip(batch, vecs16):
                        self.db.execute(
                            "INSERT OR REPLACE INTO vecs(id, vec) VALUES (?,?)", (rid, v.tobytes())
                        )
                    self.db.commit()

            await asyncio.to_thread(write)

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
        await self._backfill_vecs()

        def build():
            n = self._rdb().execute("SELECT COUNT(*) FROM records WHERE indexed=1").fetchone()[0]
            if n < IVF_MIN_ROWS:
                raise ValueError(f"index needs at least {IVF_MIN_ROWS} indexed records, have {n}")
            nlist = nlist_req or _ivf_auto_nlist(n)
            if n < 8 * nlist:
                raise ValueError(f"nlist={nlist} too large for {n} records (need >=8 rows per shard)")
            _require_headroom(
                n * self.cfg.dim * self.cfg.bit_width // 8  # second copy of the codes
                + min(n, IVF_TRAIN_SAMPLE) * self.cfg.dim * 4  # f32 k-means sample
                + nlist * (1 << 20),  # per-shard fixed overhead
                "index build",
            )
            ivf = _IvfIndex.train(
                self._vec_sample(IVF_TRAIN_SAMPLE), nlist, self.cfg.dim, self.cfg.bit_width,
                int(nprobe_req or IVF_DEFAULT_NPROBE),
            )
            seen = []
            for ids, mat in self._iter_vec_blocks():
                ivf.add_with_ids(mat, ids)
                seen.append(ids)
            return ivf, set(map(int, np.concatenate(seen)))

        ivf, seen = await asyncio.to_thread(build)
        tmp = self.dir / "ivf.tmp"
        async with self.lock.write():

            def swap():
                live = {r[0] for r in self._rdb().execute("SELECT id FROM records WHERE indexed=1")}
                for gone in seen - live:  # deleted while the build streamed
                    ivf.remove(gone)
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

    async def _detach_index(self) -> None:
        """Rebuild the flat index from retained vectors and drop the shards."""
        if not isinstance(self.index, _IvfIndex):
            if self.cfg.index_config:  # crash replay landed past the swap: finish bookkeeping
                self._save_index_config(None)
            if self.ivf_dir.exists():
                await asyncio.to_thread(shutil.rmtree, self.ivf_dir, True)
            return
        missing = await asyncio.to_thread(
            lambda: self._rdb().execute(
                "SELECT COUNT(*) FROM records r LEFT JOIN vecs v ON v.id=r.id"
                " WHERE r.indexed=1 AND v.id IS NULL"
            ).fetchone()[0]
        )
        if missing:
            raise ValueError(f"{missing} records lack a retained vector; re-ingest them first")

        def build():
            _require_headroom(
                len(self.index) * self.cfg.dim * self.cfg.bit_width // 8, "index removal"
            )
            flat = IdMapIndex(dim=self.cfg.dim, bit_width=self.cfg.bit_width)
            sample = self._vec_sample(CAL_SAMPLE)
            if len(sample) >= 64:
                flat.calibrate(sample)  # calibrate-early holds for rebuilds too
            seen = []
            for ids, mat in self._iter_vec_blocks():
                flat.add_with_ids(mat, ids)
                seen.append(ids)
            return flat, set(map(int, np.concatenate(seen))) if seen else set()

        flat, seen = await asyncio.to_thread(build)
        tmp = self.dir / "index.tvim.tmp"
        async with self.lock.write():

            def swap():
                live = {r[0] for r in self._rdb().execute("SELECT id FROM records WHERE indexed=1")}
                for gone in seen - live:
                    flat.remove(gone)
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
            self._read_local.db = db
            with self.db_lock:
                self._read_conns.append(db)
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
                where, params = _filter_sql(scope, filt)
                ids = [r[0] for r in self._rdb().execute(f"SELECT id FROM records WHERE {where}", params)]
                allow = np.array(ids, dtype=np.uint64)
                try:  # ponytail: tiny FIFO; LRU if filters vary widely. Concurrent
                    # searches race the eviction — losing the race is fine, crashing isn't.
                    if len(self._allow_cache) >= 8:
                        self._allow_cache.pop(next(iter(self._allow_cache)), None)
                except (StopIteration, RuntimeError):  # emptied / resized mid-iteration
                    pass
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
        kept < all signals _text_ids to restore full-query ranking in stage 2."""
        toks = re.findall(r"\w+", qtext)[:100]
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
        for t in toks:
            key = _fold(t)
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
        return [t for t in toks if _fold(t) in kept], toks

    def _avgdl(self) -> float:
        """Mean folded-token doc length from a ~256-doc sample (random id probes — a
        full ORDER BY RANDOM() materializes the whole table). Only enters the BM25
        length normalization, so sampling error barely moves ranking."""
        cached = self._avgdl_cache
        if cached is not None:
            return cached
        db = self._rdb()
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
                    dls.append(len(_fold_tokens(row[0])))
        avgdl = (sum(dls) / len(dls)) if dls else 1.0
        self._avgdl_cache = avgdl
        return avgdl

    def _bm25_rescore(
        self, qtext: str, cand: list[tuple[int, str | None]], n: int
    ) -> tuple[list[int], list[float]]:
        """Full-query BM25 over a candidate set, in Python. Reproduces FTS5's bm25()
        (k1/b, ln((N-df+0.5)/(df+0.5)) IDF with the 1e-6 clamp) plus a small SDM-lite
        ordered-bigram proximity term. df comes from the shared df cache; df-0 tokens
        (typos) get the clamp floor, never full weight."""
        qtoks = _fold_tokens(qtext)[:100]
        if not qtoks or not cand:
            return [], []
        total = sum(self.indexed_counts.values())
        idf = {}
        for t in dict.fromkeys(qtoks):
            df = self._df(t)
            idf[t] = max(math.log((total - df + 0.5) / (df + 0.5)), 1e-6)
        pairs = {(a, b) for a, b in zip(qtoks, qtoks[1:]) if a != b}
        avgdl = self._avgdl()
        scored = []
        for rid, text in cand:
            toks = _fold_tokens(text or "")
            dl = len(toks) or 1
            norm = BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl)
            tf: dict[str, int] = {}
            for t in toks:
                if t in idf:
                    tf[t] = tf.get(t, 0) + 1
            s = sum(idf[t] * f * (BM25_K1 + 1) / (f + norm) for t, f in tf.items())
            if pairs:
                tf2: dict[tuple, int] = {}
                for pr in zip(toks, toks[1:]):
                    if pr in pairs:
                        tf2[pr] = tf2.get(pr, 0) + 1
                s += SDM_WEIGHT * sum(
                    (idf[a] + idf[b]) / 2 * f * (BM25_K1 + 1) / (f + norm)
                    for (a, b), f in tf2.items()
                )
            scored.append((s, rid))
        scored.sort(key=lambda x: (-x[0], x[1]))
        top = scored[:n]
        return [rid for _, rid in top], [s for s, _ in top]

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
            rows = self.db.execute(
                "SELECT id, indexed, type FROM records WHERE doc_id=?", (doc_id,)
            ).fetchall()
            for rid, indexed, rtype in rows:
                if indexed:
                    self.index.remove(rid)
                    self.indexed_counts[rtype] -= 1
            self.db.execute(
                "DELETE FROM vecs WHERE id IN (SELECT id FROM records WHERE doc_id=?)", (doc_id,)
            )
            self.db.execute("DELETE FROM records WHERE doc_id=?", (doc_id,))
            self.db.commit()
        self._allow_cache.clear()
        self._df_cache_churn += len(rows)
        return len(rows)

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
        # attach/detach jobs update index_config from worker threads while the loop
        # creates/deletes rows: serialize write transactions so they never interleave
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

    def get_config(self, name: str) -> CollectionConfig | None:
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
        return [r[0] for r in self.catalog.execute("SELECT name FROM collections ORDER BY name")]

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
                c = Collection(
                    cfg, self._dir(name), lambda: self.embedder_factory(cfg),
                    lambda ic, name=name: self.set_index_config(name, ic),
                )
                c.start_worker()
                self.resident[name] = c
            c.last_used = time.monotonic()
            return c

    async def _evict(self, name: str) -> None:
        c = self.resident.pop(name, None)
        if c:
            await c.stop()

    async def delete_collection(self, name: str) -> None:
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
        self.catalog.close()
