"""Free-threaded CPython 3.14t (ADR 0006, spec D6): the GIL state /healthz reports, the
GC_FREEZE knob, meta.db connections that really close on a free-threaded build, the
build-time image check and the GIL guard (the guard skips on GIL builds), the Dockerfile's
3.14t path, the non-blocking 3.14t CI job, the Unicode drift probe and the docs."""
import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import zlib
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from raggio.app import create_app

DIM = 8
ROOT = Path(__file__).resolve().parent.parent


def gil_enabled() -> bool:
    # sys._is_gil_enabled exists from 3.13 on; every older build runs with the GIL
    return getattr(sys, "_is_gil_enabled", lambda: True)()


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


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOT_API_KEY", "root-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return lambda: create_app(embedder_factory=lambda cfg: FakeEmbedder())


# ---- /healthz ----


def test_healthz_reports_whether_the_gil_is_enabled(make_app):
    with TestClient(make_app()) as client:
        body = client.get("/healthz").json()
    assert body["gil_enabled"] is gil_enabled()
    # additive: the keys other plans report stay
    assert body["status"] == "ok" and body["resident_collections"] == [] and "bm25" in body


def test_healthz_reads_the_gil_state_on_every_request(make_app, monkeypatch):
    # a lazily imported extension can re-enable the GIL after startup: /healthz must say
    # so from then on, not repeat what it saw first
    state = {"enabled": False}
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: state["enabled"], raising=False)
    with TestClient(make_app()) as client:
        assert client.get("/healthz").json()["gil_enabled"] is False
        state["enabled"] = True
        assert client.get("/healthz").json()["gil_enabled"] is True


# ---- GC_FREEZE ----


def test_gc_freeze_defaults_off_and_rejects_other_values(monkeypatch):
    from raggio.config import Settings

    monkeypatch.delenv("GC_FREEZE", raising=False)
    assert Settings().gc_freeze is False
    monkeypatch.setenv("GC_FREEZE", "1")
    assert Settings().gc_freeze is True
    for bad in ("true", "yes", "2"):
        monkeypatch.setenv("GC_FREEZE", bad)
        with pytest.raises(ValueError, match="GC_FREEZE"):
            Settings()


@pytest.mark.parametrize("knob, expected", [
    ("1", ["resume_pending", "collect", "freeze", "shutdown", "unfreeze"]),
    ("0", ["resume_pending", "shutdown"]),
    ("", ["resume_pending", "shutdown"]),
])
def test_gc_freeze_runs_after_resume_pending_and_unfreezes_after_shutdown(
    make_app, monkeypatch, knob, expected
):
    import gc

    from raggio.store import CollectionManager

    calls = []
    real_resume, real_shutdown = CollectionManager.resume_pending, CollectionManager.shutdown

    async def resume_pending(self):
        calls.append("resume_pending")
        await real_resume(self)

    async def shutdown(self):
        await real_shutdown(self)
        calls.append("shutdown")

    monkeypatch.setattr(CollectionManager, "resume_pending", resume_pending)
    monkeypatch.setattr(CollectionManager, "shutdown", shutdown)
    for name in ("collect", "freeze", "unfreeze"):
        monkeypatch.setattr(gc, name, lambda *a, _n=name, **k: calls.append(_n) or 0)
    monkeypatch.setenv("GC_FREEZE", knob)
    with TestClient(make_app()):
        pass
    assert calls == expected


def test_gc_freeze_freezes_the_startup_heap_but_still_collects_new_cycles(
    make_app, monkeypatch
):
    import gc
    import weakref

    class Node:
        pass

    monkeypatch.setenv("GC_FREEZE", "1")
    gc.unfreeze()
    with TestClient(make_app()) as client:
        assert gc.get_freeze_count() > 0
        assert client.get("/healthz").status_code == 200
        a, b = Node(), Node()
        a.other, b.other = b, a
        alive = weakref.ref(a)
        del a, b
        gc.collect()
        assert alive() is None  # a cycle allocated after the freeze is still collected
    assert gc.get_freeze_count() == 0  # unfrozen on shutdown


def test_gc_freeze_still_frees_a_collection_loaded_before_the_freeze(make_app, monkeypatch):
    # the freeze runs after resume_pending, so a collection a replayed job loaded is part
    # of the frozen heap. Deleting it must still free it and its index: reference counting
    # does that, and the collector, which skips frozen objects, is never needed
    import gc
    import time
    import weakref

    from raggio.store import CollectionManager

    root = {"x-api-key": "root-key"}
    unit = (np.ones(DIM) / np.sqrt(DIM)).tolist()
    with TestClient(make_app()) as client:
        assert client.post("/collections", headers=root, json={"name": "kb", "dim": DIM}).status_code == 201
        job = client.post("/collections/kb/documents", headers=root, json={"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": unit}]}]}).json()["job_id"]
        while client.get(f"/collections/kb/jobs/{job}", headers=root).json()["status"] != "done":
            time.sleep(0.01)
    loaded = []
    real_resume = CollectionManager.resume_pending

    async def resume_pending(self):
        await real_resume(self)
        loaded.append(weakref.ref(await self.touch("kb")))  # as a replayed job's load does

    monkeypatch.setattr(CollectionManager, "resume_pending", resume_pending)
    monkeypatch.setenv("GC_FREEZE", "1")
    gc.unfreeze()
    with TestClient(make_app()) as client:
        assert gc.get_freeze_count() > 0 and loaded[0]() is not None
        assert client.delete("/collections/kb", headers=root).status_code == 200
        gc.collect()
        assert loaded[0]() is None


# ---- the local free-threaded venv and the GIL guard ----


def test_the_314t_venv_stays_out_of_git_and_the_image_context():
    # UV_PROJECT_ENVIRONMENT=.venv-ft sits next to .venv in the checkout; podman build .
    # would otherwise send it along
    assert ".venv-ft/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".venv-ft" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


FT = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
needs_ft = pytest.mark.skipif(not FT, reason="the GIL guard needs a free-threaded build")


def test_the_suite_never_runs_with_the_gil_check_switched_off():
    # PYTHON_GIL=0 / -X gil=0 keep the GIL off even for an extension that needs it: the
    # free-threaded job would pass while hiding exactly what it exists to catch
    assert os.environ.get("PYTHON_GIL") != "0" and sys._xoptions.get("gil") != "0"


@needs_ft
def test_the_gil_is_still_disabled():
    # pytest imported every test module (raggio, raggio_native, turbovec, numpy, fastapi,
    # ...) before running any test, and the tests before this one ran on this interpreter
    assert sys._is_gil_enabled() is False
