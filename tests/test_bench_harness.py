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


# ---- first hybrid pass (Task 5): spec §6 reports it separately ----------------------------

def test_first_hybrid_pass_is_reported_separately():
    lat = [900.0, 700.0, 600.0, 500.0, 480.0, 300.0, 200.0, 100.0, 50.0, 20.0] + [10.0] * 90
    assert bench.hybrid_first_pass(lat) == {"hybrid_first10_max_ms": 900.0, "hybrid_p99_after10": 10.0}
    assert bench.hybrid_first_pass(lat[:11]) == {}  # too short for a p99 of the rest
    keys = [key for _, key, _ in bench.ROWS]
    at = keys.index("hybrid_p99")
    assert keys[at + 1: at + 3] == ["hybrid_first10_max_ms", "hybrid_p99_after10"]


def test_report_says_the_concurrent_phase_repeats_the_queries():
    text = bench.report({"raggio": {"hybrid_first10_max_ms": 612.3}}, bench.build_parser().parse_args([]), 1000)
    assert "| Hybrid first 10 queries, slowest (ms) | 612.3 |" in text
    assert "| Hybrid p99 without the first 10 (ms) | — |" in text
    assert "The concurrent phases repeat the serial phases' queries (warm caches)." in text


# ---- spec §3.1 G5 rows (Task 5): the concurrent hybrid p99 and the server CPU per query ----

import subprocess

N = 100  # requests per phase in these tests


def cpu_stat(path, usage_usec):
    """A cgroup v2 cpu.stat as the kernel writes it."""
    path.write_text(f"usage_usec {usage_usec}\nuser_usec {usage_usec // 2}\n"
                    f"system_usec {usage_usec // 2}\nnr_periods 0\nnr_throttled 0\nthrottled_usec 0\n")


def test_cpu_stat_path_is_the_containers_cgroup(monkeypatch):
    seen = []

    def inspect(stdout, code=0):
        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")
        monkeypatch.setattr(bench.subprocess, "run", run)

    inspect("/user.slice/libpod-abc123.scope\n")
    assert bench.cpu_stat_path("bench-tv") == "/sys/fs/cgroup/user.slice/libpod-abc123.scope/cpu.stat"
    assert seen == [["podman", "inspect", "bench-tv", "--format", "{{.State.CgroupPath}}"]]
    inspect("", 125)  # no such container
    with pytest.raises(OSError, match="gave no cgroup path"):
        bench.cpu_stat_path("bench-tv")

    def no_podman(cmd, **kw):
        raise FileNotFoundError("podman")

    monkeypatch.setattr(bench.subprocess, "run", no_podman)
    with pytest.raises(OSError):
        bench.cpu_stat_path("bench-tv")


def test_read_usage_usec_reads_cpu_stat(tmp_path):
    stat = tmp_path / "cpu.stat"
    cpu_stat(stat, 123_456_789)
    assert bench.read_usage_usec(stat) == 123_456_789
    stat.write_text("user_usec 1\nsystem_usec 2\n")
    with pytest.raises(OSError, match="no usage_usec line"):
        bench.read_usage_usec(stat)
    with pytest.raises(OSError):  # a laptop: no such cgroup file
        bench.read_usage_usec(tmp_path / "missing" / "cpu.stat")


def test_cpu_rows_are_absent_never_zero(monkeypatch, tmp_path, capsys):
    stat = tmp_path / "cpu.stat"
    monkeypatch.setattr(bench, "cpu_stat_path", lambda container: str(stat))

    def phase(usec=0, delete=False):
        async def run():
            if delete:
                stat.unlink()
            elif usec:
                cpu_stat(stat, bench.read_usage_usec(stat) + usec)
            return [5.0] * N, 1.0, [["r0"]] * N
        return run

    def measure(container, run):
        out, cpu = asyncio.run(bench.cpu_phase("raggio", "cpu_ms_per_q_c", container, run))
        assert out[0] == [5.0] * N  # the phase's own result, untouched
        return cpu

    cpu_stat(stat, 1_000_000)
    assert measure("bench-tv", phase(usec=50_800 * N)) == {"cpu_ms_per_q_c": pytest.approx(50.8)}
    assert capsys.readouterr().out == ""
    assert measure(None, phase()) == {}  # no --cpu-container
    assert measure("bench-wv", phase()) == {}  # not the engine's container
    assert measure("bench-tv", phase(delete=True)) == {}  # cpu.stat went away mid-phase
    assert measure("bench-tv", phase()) == {}  # no cpu.stat at all
    stat.write_text("user_usec 1\n")
    assert measure("bench-tv", phase()) == {}  # no usage_usec line

    def no_cgroup(container):
        raise OSError(f"podman inspect {container} gave no cgroup path (exit 125)")

    monkeypatch.setattr(bench, "cpu_stat_path", no_cgroup)
    assert measure("bench-tv", phase()) == {}
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 6 and all(ln.startswith("[raggio] cpu_ms_per_q_c absent: ") for ln in lines)
    assert lines[0] == "[raggio] cpu_ms_per_q_c absent: no --cpu-container"
    assert lines[1] == "[raggio] cpu_ms_per_q_c absent: --cpu-container bench-wv is not raggio's container"
    assert lines[5] == "[raggio] cpu_ms_per_q_c absent: podman inspect bench-tv gave no cgroup path (exit 125)"


def test_bench_engine_measures_both_concurrent_phases(monkeypatch, tmp_path, capsys):
    stat, usage = tmp_path / "cpu.stat", [1_000_000]
    cpu_stat(stat, usage[0])

    async def run_queries(engine, queries, concurrency, headers, filt=None, k=10, texts=None):
        n = len(queries)
        if concurrency > 1:  # server CPU per request, p4's c=8 figures: 50.8 ms flat, 343.0 hybrid
            usage[0] += (343_000 if texts else 50_800) * n
            cpu_stat(stat, usage[0])
        lat = [10.0] * (n - 1) + [500.0] if texts and concurrency > 1 else [5.0] * n
        return lat, 1.0, [[f"r{i}"] for i in range(n)]

    async def nothing(*a, **kw):
        return {}

    for attr, fake in [("cpu_stat_path", lambda container: str(stat)), ("run_queries", run_queries),
                       ("prepare_engine", nothing), ("ensure_no_index", nothing), ("engine_state", nothing),
                       ("podman_mem", lambda container: 512.0), ("podman_disk", lambda volume: 100.0),
                       ("cold_start", lambda *a: 4.2)]:
        monkeypatch.setattr(bench, attr, fake)
    rows, paths = list(range(N)), [f"p{i}" for i in range(N)]

    def measure(*argv):
        args = bench.build_parser().parse_args(list(argv))
        return asyncio.run(bench.bench_engine("raggio", None, paths, [2000] * N, rows, [None] * N,
                                              [[i] for i in rows], ["text"] * N, paths, args))

    res = measure("--cpu-container", "bench-tv")
    assert res["cpu_ms_per_q_c"] == pytest.approx(50.8)
    assert res["hybrid_cpu_ms_per_q_c"] == pytest.approx(343.0)
    assert res["hybrid_c_p99"] == pytest.approx(495.1)  # the hybrid concurrent phase's own p99
    assert res["hybrid_p99"] == 5.0  # the serial row is unchanged
    assert "absent" not in capsys.readouterr().out
    res = measure()  # no --cpu-container: the CPU keys are absent, the p99 is not
    assert "cpu_ms_per_q_c" not in res and "hybrid_cpu_ms_per_q_c" not in res
    assert res["hybrid_c_p99"] == pytest.approx(495.1)
    out = capsys.readouterr().out
    assert "[raggio] cpu_ms_per_q_c absent: no --cpu-container" in out
    assert "[raggio] hybrid_cpu_ms_per_q_c absent: no --cpu-container" in out


def test_concurrent_rows_sit_next_to_their_siblings():
    keys = [key for _, key, _ in bench.ROWS]
    at = keys.index("lat_c_p95")
    assert keys[at + 1] == "cpu_ms_per_q_c"
    at = keys.index("hybrid_qps_concurrent")
    assert keys[at + 1: at + 3] == ["hybrid_c_p99", "hybrid_cpu_ms_per_q_c"]
    args = bench.build_parser().parse_args([])
    assert args.cpu_container is None and args.concurrency == 8
    text = bench.report({"raggio": {"hybrid_c_p99": 612.3}}, args, 1000)
    assert "| Hybrid p99 under concurrency (ms) | 612.3 |" in text
    # no engine has a CPU read: the rows are absent, never 0.0
    assert "| CPU per query under concurrency (ms) |" not in text
    assert "| Hybrid CPU per query under concurrency (ms) |" not in text
    text = bench.report({"raggio": {"cpu_ms_per_q_c": 50.8}, "weaviate": {}}, args, 1000)
    assert "| CPU per query under concurrency (ms) | 50.8 | — |" in text
    assert "| Hybrid CPU per query under concurrency (ms) |" not in text
