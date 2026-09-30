"""Plan C: cold start and loop hygiene. The meta.db indexes and their one-time
migration, pinned query plans, the numpy ghost diff, off-loop collection
construction, rowid vector sampling, memory release, and the index-job pre-work
order. The checks are structural (EXPLAIN plans, SQL traces, thread identity), not
timings, so the suite stays deterministic on Windows and Linux."""
import asyncio
import ctypes
import logging
import sqlite3
import threading
import zlib
from pathlib import Path

import httpx
import numpy as np
import pytest

import raggio.store as store
from raggio.app import create_app
from raggio.config import Settings
from raggio.store import (
    Collection,
    CollectionConfig,
    CollectionManager,
    IdMapIndex,
    _IvfIndex,
    open_meta_db,
)

DIM = 8
ROOT = {"x-api-key": "root-key"}
LOG = "uvicorn.error"  # the logger plan C logs through (uvicorn's handler in the container)


class FakeEmbedder:
    """Deterministic: same text -> same unit vector."""

    async def embed(self, texts):
        out = []
        for t in texts:
            v = np.random.default_rng(zlib.crc32(t.encode())).standard_normal(DIM)
            out.append((v / np.linalg.norm(v)).tolist())
        return out

    async def aclose(self):
        pass


def make_collection(tmp_path, embedder=None) -> Collection:
    return Collection(
        CollectionConfig("t", DIM, 4, None, None, None), Path(tmp_path),
        lambda: embedder or FakeEmbedder(),
    )


def rowvec(i):
    v = np.random.default_rng(1000 + i).standard_normal(DIM)
    return (v / np.linalg.norm(v)).tolist()


def ingest(col, n, start=0, meta=None):
    docs = [
        {"doc_id": f"d{i}", "chunks": [
            {"id": f"c{i}", "text": f"chunk {i}", "vector": rowvec(i),
             "metadata": (meta or {}).get(i)},
        ]}
        for i in range(start, start + n)
    ]
    asyncio.run(col._process_job({"documents": docs}))


def plan(db, sql, params=()) -> str:
    """EXPLAIN QUERY PLAN details joined with ' | ' (rows are id, parent, notused, detail)."""
    return " | ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, list(params)))


class Recorder:
    """Proxy for a sqlite3 connection that records every execute() as (sql, params)."""

    def __init__(self, db, log):
        self._db, self.log = db, log

    def execute(self, sql, params=()):
        self.log.append((sql, list(params)))
        return self._db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._db, name)


def record_sql(col) -> list:
    """Trace every statement the collection runs on its read connections."""
    log, real = [], col._rdb
    col._rdb = lambda: Recorder(real(), log)
    return log


def make_manager(tmp_path, monkeypatch) -> CollectionManager:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    return CollectionManager(Settings(), lambda cfg: FakeEmbedder())


@pytest.fixture(autouse=True)
def small_ivf(monkeypatch):
    monkeypatch.setattr(store, "IVF_MIN_ROWS", 100)


@pytest.fixture(autouse=True)
def uvicorn_logs_reach_caplog(monkeypatch):
    # any test in the session that builds a uvicorn.Config applies uvicorn's
    # LOGGING_CONFIG, which sets propagate=False on "uvicorn"; caplog listens on the
    # root logger, so without this the caplog tests would depend on test order
    for name in ("uvicorn", LOG):
        monkeypatch.setattr(logging.getLogger(name), "propagate", True)


def index_names(db) -> set:
    # sql IS NULL filters out sqlite_autoindex_* (the external_id UNIQUE constraint)
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    )}


def make_legacy_db(path: Path, rows: int = 50) -> None:
    """A meta.db in the pre-plan-C layout: idx_records_doc, neither new index."""
    db = open_meta_db(path)
    db.execute("DROP INDEX IF EXISTS idx_records_doc_type")
    db.execute("DROP INDEX IF EXISTS idx_jobs_open")
    db.execute("CREATE INDEX IF NOT EXISTS idx_records_doc ON records(doc_id)")
    db.executemany(
        "INSERT INTO records(id, external_id, doc_id, type, position, text, metadata, indexed)"
        " VALUES (?,?,?,?,?,?,?,1)",
        [(i, f"c{i}", f"d{i % 7}", "chunk", i, f"chunk {i}", "{}") for i in range(1, rows + 1)],
    )
    db.commit()
    db.close()


# ---- Task 1: indexes and the one-time migration (D9) ----


def test_fresh_meta_db_has_the_covering_and_partial_indexes(tmp_path):
    db = open_meta_db(tmp_path / "meta.db")
    names = index_names(db)
    assert {"idx_records_doc_type", "idx_jobs_open"} <= names
    assert "idx_records_doc" not in names  # its (doc_id) prefix lives on in the covering index
    cols = [r[2] for r in db.execute("PRAGMA index_info(idx_records_doc_type)")]
    assert cols == ["doc_id", "type", "indexed"]
    partial = {r[1]: r[4] for r in db.execute("PRAGMA index_list(jobs)")}
    assert partial["idx_jobs_open"] == 1
    db.close()


def test_legacy_db_migrates_once_and_logs(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOG)
    path = tmp_path / "meta.db"
    make_legacy_db(path)
    db = open_meta_db(path)
    assert "one-time migration" in caplog.text
    assert "built idx_records_doc_type" in caplog.text
    assert "idx_records_doc" not in index_names(db)
    assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 50
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()
    caplog.clear()
    open_meta_db(path).close()  # already migrated: a silent no-op
    assert "idx_records_doc_type" not in caplog.text


def test_interrupted_migration_drops_the_old_index_on_reopen(tmp_path):
    path = tmp_path / "meta.db"
    make_legacy_db(path)
    db = sqlite3.connect(path)  # the process died after CREATE INDEX, before DROP INDEX
    db.execute("CREATE INDEX idx_records_doc_type ON records(doc_id, type, indexed)")
    db.close()
    db = open_meta_db(path)
    names = index_names(db)
    assert "idx_records_doc" not in names
    assert {"idx_records_doc_type", "idx_jobs_open"} <= names
    db.close()


def test_migration_heartbeat_logs_progress(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(store, "MIGRATION_LOG_EVERY_S", 0.0)
    monkeypatch.setattr(store, "MIGRATION_PROGRESS_OPS", 100)
    caplog.set_level(logging.INFO, logger=LOG)
    path = tmp_path / "meta.db"
    make_legacy_db(path, rows=2000)
    open_meta_db(path).close()
    assert "still building idx_records_doc_type" in caplog.text


def test_open_job_queries_use_the_partial_index(tmp_path):
    col = make_collection(tmp_path)
    with col.db_lock:
        col.db.executemany(
            "INSERT INTO jobs(id, payload, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            [(i, "{}", "done", "", "") for i in range(1, 51)]
            + [(51, "{}", "processing", "", ""), (52, "{}", "pending", "", ""),
               (53, "{}", "error", "", "")],
        )
        col.db.commit()
    db = col._rdb()
    # the literal predicate D's claim query, pending_jobs and resume_pending keep (§4.1)
    count = "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
    claim = ("SELECT id, payload FROM jobs WHERE status IN ('pending','processing')"
             " ORDER BY id LIMIT 1")
    assert "idx_jobs_open" in plan(db, count), plan(db, count)
    assert "idx_jobs_open" in plan(db, claim), plan(db, claim)
    assert col.pending_jobs() == 2
    assert col._claim_next()[0] == 51  # lowest open id, 'processing' replays first
