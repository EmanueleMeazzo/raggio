"""bench/bench.py wiring of the reuse decision: the preflight runs before the
ground-truth pass, fingerprints are keyed by data set and --limit, and the flags the
DGX runbook passes exist with the documented defaults."""
import asyncio

import httpx
import numpy as np
import pytest

pytest.importorskip("orjson", reason="bench.py needs the bench dependency group")

import sys
sys.path.insert(0, "bench")
import bench
from bench_reuse import BenchAbort, write_fingerprint

LIMIT, QUERIES = 2549619, 500


class ReachedGroundTruth(Exception):
    pass


@pytest.fixture
def corpus(monkeypatch):
    """A fake corpus of the arXiv shape; ground truth raises so a test can see the
    preflight let the run through."""
    def load_corpus(limit, n_queries, seed):
        return None, [], [], np.arange(limit - n_queries), None, np.zeros((n_queries, 4), np.float32)

    def ground_truth(*a, **kw):
        raise ReachedGroundTruth

    monkeypatch.setattr(bench, "DIM", 4)
    monkeypatch.setattr(bench, "load_corpus", load_corpus)
    monkeypatch.setattr(bench, "ground_truth", ground_truth)


def fake_engine(monkeypatch, handler):
    """Route every httpx.AsyncClient bench.py opens to `handler`; returns the request log."""
    seen, real = [], httpx.AsyncClient

    def record(request):
        seen.append(f"{request.method} {request.url.path}")
        return handler(request)

    monkeypatch.setattr(bench.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(record), **kw))
    return seen


def holding(chunks, index="flat"):
    return lambda request: httpx.Response(
        200, json={"chunks": chunks, "pending_jobs": 0, "index": {"type": index}})


def run(*argv):
    return asyncio.run(bench.main(list(argv)))


def test_parser_defaults():
    args = bench.build_parser().parse_args([])
    assert args.fingerprint_dir == "bench"
    assert args.state_timeout == 900
    assert not args.adopt and not args.reingest


def test_flat_and_ivf_share_one_fingerprint_per_limit(tmp_path):
    args = bench.build_parser().parse_args(["--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path)])
    assert bench.fp_file("raggio", args) == tmp_path / f"fingerprint-raggio-{LIMIT}.json"
    assert bench.fp_file("raggio-ivf", args) == bench.fp_file("raggio", args)
    assert bench.fp_file("weaviate", args) == tmp_path / f"fingerprint-weaviate-{LIMIT}.json"


def test_ivf_engine_builds_through_its_own_step():
    assert bench.ENGINES["raggio-ivf"]["ingest"] is bench.ingest_raggio
    assert bench.ENGINES["raggio-ivf"]["build_index"] is bench.build_ivf_index
    assert bench.ENGINES["raggio"]["build_index"] is None


def test_wrong_limit_aborts_before_ground_truth(monkeypatch, corpus, tmp_path):
    # the DGX volume holds 2,549,119 chunks; --limit 2549119 expects 2,548,619
    seen = fake_engine(monkeypatch, holding(2549119, "ivf"))
    with pytest.raises(BenchAbort, match="--limit 2549119"):
        run("--engine", "raggio-ivf", "--limit", "2549119", "--fingerprint-dir", str(tmp_path))
    assert all(s.startswith("GET ") for s in seen)  # no DELETE, no ingest


def test_unreachable_engine_aborts_before_ground_truth(monkeypatch, corpus, tmp_path):
    def down(request):
        raise httpx.ReadTimeout("timed out", request=request)
    seen = fake_engine(monkeypatch, down)
    with pytest.raises(BenchAbort, match="unreachable"):
        run("--engine", "raggio", "--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path))
    assert seen == ["GET /collections/bench"]


def test_legacy_unkeyed_fingerprint_is_not_trusted(monkeypatch, corpus, tmp_path):
    # bench/fingerprint-raggio.json (email corpus, or a copy from another checkout)
    # must not vouch for the arXiv volume
    args = bench.build_parser().parse_args(["--limit", str(LIMIT)])
    write_fingerprint(tmp_path / "fingerprint-raggio.json", bench.fingerprint(args))
    fake_engine(monkeypatch, holding(LIMIT - QUERIES))
    with pytest.raises(BenchAbort, match="--adopt"):
        run("--engine", "raggio", "--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path))


def test_matching_fingerprint_passes_preflight(monkeypatch, corpus, tmp_path):
    args = bench.build_parser().parse_args(["--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path)])
    write_fingerprint(bench.fp_file("raggio", args), bench.fingerprint(args))
    seen = fake_engine(monkeypatch, holding(LIMIT - QUERIES))
    with pytest.raises(ReachedGroundTruth):
        run("--engine", "raggio-ivf", "--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path))
    assert not any(s.startswith("DELETE") for s in seen)


def test_adopt_passes_preflight_and_records_nothing_yet(monkeypatch, corpus, tmp_path):
    fake_engine(monkeypatch, holding(LIMIT - QUERIES))
    with pytest.raises(ReachedGroundTruth):
        run("--engine", "raggio-ivf", "--limit", str(LIMIT), "--fingerprint-dir", str(tmp_path), "--adopt")
    assert list(tmp_path.iterdir()) == []  # written by the engine's own step, not the preflight
