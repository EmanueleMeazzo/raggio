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
