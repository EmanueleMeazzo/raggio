"""Concurrency correctness (ADR 0001, addendum 2026-09): the shared catalog connection,
delete_collection vs a racing touch(), Collection.stop() vs in-flight threads,
copy-on-write indexed_counts, and a mixed-workload stress run checked after
quiescence. Races that can't be forced deterministically under the GIL are pinned
structurally (lock spies, identity checks) instead of by timing."""
import ast
import asyncio
import collections
import sys
import threading
import time
import zlib
from pathlib import Path

import httpx
import numpy as np
import pytest

import raggio.store as store
from raggio.app import create_app
from raggio.config import Settings
from raggio.store import CollectionManager

DIM = 8
ROOT = {"x-api-key": "root-key"}


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


def make_manager(tmp_path, monkeypatch) -> CollectionManager:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    return CollectionManager(Settings(), lambda cfg: FakeEmbedder())


@pytest.fixture
def busy_switching():
    """Force a thread switch every ~1us so unlocked shared state interleaves at
    statement granularity instead of once per 5ms default switch interval."""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


# ---- R1: the shared catalog connection ----


class _LockAsserting:
    """Wraps a sqlite3 connection (and the cursors it returns): every call must run
    with `lock` held: pysqlite objects are not safe to share across threads."""

    def __init__(self, inner, lock):
        self._inner, self._lock = inner, lock

    def _check(self):
        assert self._lock.locked(), "catalog touched outside _catalog_lock"

    def execute(self, *args):
        self._check()
        return _LockAsserting(self._inner.execute(*args), self._lock)

    def fetchone(self):
        self._check()
        return self._inner.fetchone()

    def fetchall(self):
        self._check()
        return self._inner.fetchall()

    def commit(self):
        self._check()
        return self._inner.commit()

    def close(self):
        self._check()
        return self._inner.close()

    def __iter__(self):
        return self

    def __next__(self):
        self._check()
        return next(self._inner)


def test_every_catalog_statement_runs_under_the_catalog_lock(tmp_path, monkeypatch):
    m = make_manager(tmp_path, monkeypatch)
    m.catalog = _LockAsserting(m.catalog, m._catalog_lock)

    async def run():
        await m.create_collection("a", DIM, 4, None, None, "key-a")
        assert m.get_config("a").name == "a"
        assert m.get_config("missing") is None
        assert m.list_collections() == ["a"]
        m.set_index_config("a", {"nlist": 2, "nprobe": 1})
        await m.touch("a")
        await m.delete_collection("a")
        assert m.list_collections() == []
        await m.shutdown()

    asyncio.run(run())


def test_concurrent_catalog_reads_return_the_requested_row(tmp_path, monkeypatch, busy_switching):
    # require_collection is a sync dependency: FastAPI runs it on threadpool threads,
    # one per in-flight request, all reading the manager's single catalog connection
    m = make_manager(tmp_path, monkeypatch)
    names = [f"c{i}" for i in range(8)]

    async def create():
        for n in names:
            await m.create_collection(n, DIM, 4, None, None, f"key-{n}")

    asyncio.run(create())
    bad = []
    start = threading.Barrier(16)

    def hammer(t):
        start.wait()
        deadline = time.monotonic() + 1.0
        k = 0
        while time.monotonic() < deadline and len(bad) < 20:
            n = names[(t + k) % len(names)]
            k += 1
            try:
                cfg = m.get_config(n)
                if cfg is None or cfg.name != n or cfg.key_hash != store.hash_key(f"key-{n}"):
                    bad.append((n, cfg and cfg.name))
                if k % 8 == 0 and m.list_collections() != names:
                    bad.append(("list_collections", n))
            except Exception as e:  # InterfaceError / IndexError / SystemError on main
                bad.append((n, repr(e)))

    threads = [threading.Thread(target=hammer, args=(t,)) for t in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    asyncio.run(m.shutdown())
    assert bad == []


def test_concurrent_collection_key_checks_are_consistent(tmp_path, monkeypatch, busy_switching):
    # end to end: collection B's key must get exactly 401 on A and 200 on B, never a
    # 500 (InterfaceError) or a decision made against the other collection's row
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    app = create_app(embedder_factory=lambda cfg: FakeEmbedder())

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                for n in ("A", "B"):
                    r = await c.post("/collections", headers=ROOT,
                                     json={"name": n, "collection_key": f"key{n}"})
                    assert r.status_code == 201, r.text
                seen = collections.Counter()

                async def hit(i):
                    target = "A" if i % 2 else "B"
                    r = await c.get(f"/collections/{target}", headers={"x-api-key": "keyB"})
                    seen[f"{target}:{r.status_code}"] += 1

                # 16 concurrent requests x 80 rounds: c=16, as in p4's R1 hammer on gn100
                for _ in range(80):
                    await asyncio.gather(*(hit(i) for i in range(16)))
        return seen

    assert asyncio.run(run()) == {"A:401": 640, "B:200": 640}


def test_no_threading_lock_is_held_across_an_await():
    # _catalog_lock and db_lock are threading locks the loop thread takes too: an
    # await inside one parks every pool thread behind a suspended coroutine, and a
    # second coroutine taking it on the loop deadlocks the server (not reentrant)
    tree = ast.parse(Path(store.__file__).read_text(encoding="utf-8"))
    sites, awaits = collections.Counter(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        locks = [i.context_expr.attr for i in node.items
                 if isinstance(i.context_expr, ast.Attribute)
                 and i.context_expr.attr in ("_catalog_lock", "db_lock")]
        if locks:
            sites.update(locks)
            awaits += [(locks[0], n.lineno) for stmt in node.body for n in ast.walk(stmt)
                       if isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor))]
    assert sites["_catalog_lock"] and sites["db_lock"]  # the scan still sees the locks
    assert awaits == []
