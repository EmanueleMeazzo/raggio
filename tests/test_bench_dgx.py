"""The DGX runbook (bench/dgx/, spec §6): shell syntax and line endings, the guard rails
(only bench-tv is ever stopped or removed, --limit 2549619, no --reingest, state kept
outside the deploy-replaced checkout), dry runs of both session orders, the deploy's
unpack step, and the median / max - min noise band of compare.py."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, "bench/dgx")
import compare

DGX = Path("bench/dgx")
SCRIPTS = sorted(DGX.glob("*.sh"))
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")
BENCH_CMD = ".venv/bin/python -u bench/bench.py "
IMAGE = "localhost/raggio:abc1234"
STOP = "podman stop -t 60 --ignore bench-tv"
RM = "podman rm -f --ignore bench-tv"
STARTED = "wait for /healthz and the first GET /collections/bench"
IDLE = "wait for pending_jobs == 0"
DETACH = "curl -sf --max-time 900 -X DELETE -H x-api-key: bench http://localhost:18000/collections/bench/index"


def podman_run(memory, image=IMAGE):
    return (f"podman run -d --name bench-tv --memory {memory} -p 18000:8000 -v bench-tv:/data "
            f"-e ROOT_API_KEY=bench {image}")


def test_runbook_scripts_exist():
    assert [p.name for p in SCRIPTS] == ["deploy.sh", "lib.sh", "session.sh", "setup.sh", "unpack.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_use_lf_line_endings(script):
    # deploy ships `git archive HEAD`; a CRLF script dies on the DGX with $'\r': command not found
    assert b"\r" not in script.read_bytes()


def test_gitattributes_forces_lf_for_shell_scripts():
    # core.autocrlf=true on the workstation converts the archive too, unless the path says eol=lf
    lines = Path(".gitattributes").read_text().splitlines()
    assert "*.sh text eol=lf" in lines


@needs_bash
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script):
    subprocess.run([BASH, "-n", str(script)], check=True)


def test_guard_rails():
    text = {p.name: p.read_text(encoding="utf-8") for p in SCRIPTS}
    joined = "\n".join(text.values())
    assert "pkill" not in joined
    assert "volume rm" not in joined
    assert "--reingest" not in joined
    assert "2549119" not in joined  # the ingested count, never a --limit
    assert "LIMIT=2549619" in text["lib.sh"]
    assert "CONTAINER=bench-tv" in text["lib.sh"]
    rm_lines = stop_lines = 0
    for line in joined.splitlines():
        code = line.split("#")[0]
        if "podman rm" in code:
            rm_lines += 1
            assert code.split("podman rm", 1)[1].split() == ["-f", "--ignore", '"$CONTAINER"']
        if "podman stop" in code:
            stop_lines += 1
            assert code.split("podman stop", 1)[1].split() == ["-t", "60", "--ignore", '"$CONTAINER"']
    assert rm_lines and stop_lines  # the loop checked real lines
    # fingerprints and results live under BENCH_HOME, outside the ~/raggio that deploy replaces
    assert 'BENCH_HOME="${BENCH_HOME:-$HOME/raggio-bench}"' in text["lib.sh"]
    assert "rm -rf ~/raggio && mkdir ~/raggio && tar -x -C ~/raggio" in text["unpack.sh"]
    # the archive must carry the committed bytes, not autocrlf-converted ones
    assert 'git -c core.autocrlf=false archive HEAD | ssh_dgx' in text["deploy.sh"]
    # an early stop signals the session's process group, recorded here, never a pattern
    assert "ps -o pgid= -p $$ | tr -d ' ' > \"$OUT/session.pgid\"" in text["session.sh"]


def dry_session(tmp_path, *env_pairs):
    home, raggio = tmp_path / "bench-home", tmp_path / "raggio"
    raggio.mkdir()
    (raggio / ".deployed-sha").write_text("abc1234\n")
    env = {**os.environ, "DRY_RUN": "1", "BENCH_HOME": home.as_posix(), "RAGGIO_DIR": raggio.as_posix(),
           **dict(env_pairs)}
    out = subprocess.run([BASH, (DGX / "session.sh").resolve().as_posix(), "s1"], env=env,
                         capture_output=True, text=True, check=True).stdout
    return home, [ln[2:] for ln in out.splitlines() if ln.startswith("+ ")]


def bench_runs(cmds):
    """[(index, run tag, engine)] of the bench.py invocations."""
    return [(i, re.search(r"--out \S+/(run\d+)-", c).group(1), re.search(r"--engine (\S+)", c).group(1))
            for i, c in enumerate(cmds) if c.startswith(BENCH_CMD)]


@needs_bash
def test_session_starting_on_ivf_detaches_once_between_the_groups(tmp_path):
    _, cmds = dry_session(tmp_path, ("DRY_INDEX", "ivf"))
    runs = bench_runs(cmds)
    assert [(tag, engine) for _, tag, engine in runs] == [
        ("run0", "raggio-ivf"), ("run1", "raggio-ivf"), ("run2", "raggio-ivf"), ("run3", "raggio-ivf"),
        ("run0", "raggio"), ("run1", "raggio"), ("run2", "raggio")]
    # the one transition: after the IVF group, in a transient 8 GiB container, waited out
    (d,) = [i for i, c in enumerate(cmds) if "-X DELETE" in c]
    assert cmds[d] == DETACH
    assert runs[3][0] < d < runs[4][0]
    assert cmds[d - 5:d] == [STOP, RM, podman_run("8g"), STARTED, IDLE]
    assert cmds[d + 1] == IDLE
    assert [c for c in cmds if "--memory 8g" in c] == [podman_run("8g")]


@needs_bash
def test_session_starting_flat_builds_the_index_in_the_ivf_warm_up(tmp_path):
    _, cmds = dry_session(tmp_path, ("DRY_INDEX", "flat"))
    assert [(tag, engine) for _, tag, engine in bench_runs(cmds)] == [
        ("run0", "raggio"), ("run1", "raggio"), ("run2", "raggio"),
        ("run0", "raggio-ivf"), ("run1", "raggio-ivf"), ("run2", "raggio-ivf"), ("run3", "raggio-ivf")]
    # raggio-ivf run0 finds a flat index and builds it (bench.py's build-index path): no detach
    assert not [c for c in cmds if "-X DELETE" in c or "--memory 8g" in c]


@needs_bash
def test_every_run_starts_from_a_fresh_idle_4g_container(tmp_path):
    home, cmds = dry_session(tmp_path)
    out, state = (home / "s1").as_posix(), (home / "state").as_posix()
    assert cmds[:5] == [STOP, RM, podman_run("4g"), STARTED, IDLE]  # the first start
    for i, tag, engine in bench_runs(cmds):
        assert cmds[i - 6:i] == [STOP, RM, podman_run("4g"), STARTED, IDLE, "rm -f bench/results-partial.json"]
        assert cmds[i + 1] == f"mv bench/results-partial.json {out}/{tag}-{engine}.json"
        assert f"--limit 2549619 " in cmds[i] and f"--fingerprint-dir {state} " in cmds[i]
        assert cmds[i].endswith(f"--out {out}/{tag}-{engine}.md")  # no --adopt without ADOPT=1


@needs_bash
def test_session_records_the_host_facts_first(tmp_path):
    home, _ = dry_session(tmp_path)
    facts = (home / "s1" / "facts.txt").read_text().splitlines()
    assert facts[0].endswith(f"session s1: image {IMAGE}, bench checkout abc1234")
    for cmd in ["uptime", "free -m", "grep -E ^(Cached|Buffers): /proc/meminfo", "podman ps -a",
                "podman image inspect --format {{.Id}} {{.Digest}} " + IMAGE]:
        assert f"+ {cmd}" in facts
    assert any(f.startswith(f"+ podman run --rm {IMAGE} python -c import sys, sqlite3") for f in facts)


@needs_bash
def test_session_can_measure_another_image(tmp_path):
    _, cmds = dry_session(tmp_path, ("IMAGE", "localhost/raggio:baseline"))
    starts = [c for c in cmds if c.startswith("podman run -d ")]
    assert starts and all(c.endswith(" localhost/raggio:baseline") for c in starts)


@needs_bash
def test_session_adopt_applies_to_the_first_run_only(tmp_path):
    _, cmds = dry_session(tmp_path, ("ADOPT", "1"))
    runs = [c for c in cmds if c.startswith(BENCH_CMD)]
    assert ["--adopt" in r for r in runs] == [True] + [False] * 6


@needs_bash
def test_session_refuses_to_overwrite_a_label(tmp_path):
    home, raggio = tmp_path / "bench-home", tmp_path / "raggio"
    (home / "s1").mkdir(parents=True)
    raggio.mkdir()
    (raggio / ".deployed-sha").write_text("abc1234\n")
    env = {**os.environ, "DRY_RUN": "1", "BENCH_HOME": home.as_posix(), "RAGGIO_DIR": raggio.as_posix()}
    r = subprocess.run([BASH, (DGX / "session.sh").resolve().as_posix(), "s1"], env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "exists" in r.stderr


def archive(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack(home, files):
    env = {**os.environ, "HOME": home.as_posix()}
    subprocess.run([BASH, (DGX / "unpack.sh").resolve().as_posix()], input=archive(files), env=env,
                   check=True, capture_output=True)


@needs_bash
def test_unpack_keeps_the_previous_checkouts_results_then_replaces_it(tmp_path):
    bench = tmp_path / "raggio" / "bench"
    bench.mkdir(parents=True)
    for name in ["results-baseline3-run1-raggio.json", "baseline3.log", "fingerprint-raggio.json",
                 "gt-2549619-42-d1024.npz", "bench.py"]:
        (bench / name).write_text(name)
    unpack(tmp_path, {"README.md": b"new\n", "bench/dgx/lib.sh": b"# lib\n"})
    (kept,) = (tmp_path / "raggio-bench").glob("pre-deploy-*")
    assert sorted(p.name for p in kept.iterdir()) == [
        "baseline3.log", "fingerprint-raggio.json", "results-baseline3-run1-raggio.json"]
    assert sorted(p.relative_to(tmp_path / "raggio").as_posix()
                  for p in (tmp_path / "raggio").rglob("*") if p.is_file()) == ["README.md", "bench/dgx/lib.sh"]


@needs_bash
def test_unpack_on_a_fresh_host_keeps_nothing(tmp_path):
    unpack(tmp_path, {"README.md": b"new\n"})
    assert (tmp_path / "raggio" / "README.md").read_text() == "new\n"
    assert not (tmp_path / "raggio-bench").exists()


def write_session(d, runs):
    """runs: {"run1-raggio": {metric: value}} -> run1-raggio.json = {"raggio": {...}}"""
    d.mkdir(parents=True)
    for stem, metrics in runs.items():
        engine = stem.split("-", 1)[1]
        (d / f"{stem}.json").write_text(json.dumps({engine: metrics}))


def test_summary_is_the_median_and_max_minus_min():
    assert compare.summary([10.0, 12.0]) == (11.0, 2.0)
    mid, band = compare.summary([166.5, 214.3, 195.4])
    assert mid == 195.4 and band == pytest.approx(47.8)


def test_verdict_uses_the_noise_band_and_metric_direction():
    # latency: lower is better; baseline band 12 - 10 = 2 around a median of 11
    assert compare.verdict("lat_p50", [10.0, 12.0], [12.5, 12.5]) == "within band"
    assert compare.verdict("lat_p50", [10.0, 12.0], [13.5, 13.5]) == "worse"
    assert compare.verdict("lat_p50", [10.0, 12.0], [8.5, 8.5]) == "better"
    # throughput: higher is better
    assert compare.verdict("qps_concurrent", [100.0, 110.0], [90.0, 90.0]) == "worse"
    assert compare.verdict("qps_concurrent", [100.0, 110.0], [120.0, 120.0]) == "better"
    # identical runs: a zero band still accepts exact equality
    assert compare.verdict("recall_at_10", [0.9, 0.9], [0.9, 0.9]) == "within band"
    # a noisy candidate widens the band: median 12 is 2 worse, inside its own band of 2
    assert compare.verdict("lat_p50", [10.0, 10.0], [11.0, 13.0]) == "within band"
    # IVF: medians of three, the 16-24% warm spread is the band
    assert compare.verdict("qps_concurrent", [166.5, 195.4, 214.3], [150.0, 170.0, 200.0]) == "within band"
    # the band is never narrower than the table shows: disk is printed in whole MB
    assert compare.verdict("disk_mb", [16757.87, 16757.87], [16758.2, 16758.2]) == "within band"
    assert compare.verdict("disk_mb", [16757.87, 16757.87], [16760.0, 16760.0]) == "worse"


def test_verdict_needs_two_values_and_never_claims_the_warm_restart():
    assert compare.verdict("index_build_s", [213.0], [150.0]) == "no band"
    assert compare.verdict("cold_start_s", [4.3, 4.4], [30.0, 31.0]) == "not claimable"


def test_the_warm_up_counts_only_for_one_shot_metrics(tmp_path):
    write_session(tmp_path / "s", {
        "run0-raggio-ivf": {"index_build_s": 213.0, "lat_p50": 15.5},
        "run1-raggio-ivf": {"lat_p50": 11.2}, "run2-raggio-ivf": {"lat_p50": 11.1},
        "run3-raggio-ivf": {"lat_p50": 12.0}})
    runs = compare.load(tmp_path / "s")["raggio-ivf"]
    assert compare.values(runs, "lat_p50") == [11.2, 11.1, 12.0]
    assert compare.values(runs, "index_build_s") == [213.0]
    out = compare.render(tmp_path / "s")
    assert "| raggio-ivf | Search p50 (ms) | 11.2 / 11.1 / 12.0 | 11.2 | 0.9 |" in out
    assert "| raggio-ivf | IVF index build (s) | 213 | 213 | - |" in out


def test_compare_table_for_one_session_and_an_ab(tmp_path):
    base, cand = tmp_path / "main", tmp_path / "cand"
    write_session(base, {"run1-raggio": {"lat_p50": 23.0, "qps_concurrent": 129.0},
                         "run2-raggio": {"lat_p50": 24.0, "qps_concurrent": 131.0}})
    write_session(cand, {"run1-raggio": {"lat_p50": 30.0, "qps_concurrent": 130.0},
                         "run2-raggio": {"lat_p50": 30.0, "qps_concurrent": 130.0}})
    assert "| raggio | Search p50 (ms) | 23.0 / 24.0 | 23.5 | 1.0 |" in compare.render(base)
    ab = compare.render(base, cand)
    assert "| raggio | Search p50 (ms) | 23.5 | 1.0 | 30.0 | 0.0 | worse |" in ab
    assert "| raggio | QPS concurrent | 130 | 2 | 130 | 0 | within band |" in ab
    assert compare.beyond_band(base, cand) == {"better": [], "worse": [("raggio", "lat_p50")]}


def test_compare_marks_metrics_missing_from_a_session(tmp_path):
    # the IVF build is measured only by a flat-first session
    base, cand = tmp_path / "main", tmp_path / "cand"
    write_session(base, {"run0-raggio-ivf": {"index_build_s": 213.0},
                         "run1-raggio-ivf": {"lat_p50": 11.0}, "run2-raggio-ivf": {"lat_p50": 12.0}})
    write_session(cand, {"run1-raggio-ivf": {"lat_p50": 11.5}, "run2-raggio-ivf": {"lat_p50": 11.5}})
    ab = compare.render(base, cand)
    assert "| raggio-ivf | IVF index build (s) | 213 | - | - | - | - |" in ab
    assert "| raggio-ivf | Search p50 (ms) | 11.5 | 1.0 | 11.5 | 0.0 | within band |" in ab


def test_compare_refuses_a_directory_without_runs(tmp_path):
    with pytest.raises(SystemExit, match="no run<N>-<engine>.json"):
        compare.load(tmp_path)


def test_compare_covers_every_bench_metric():
    pytest.importorskip("orjson", reason="bench.py needs the bench dependency group")
    sys.path.insert(0, "bench")
    import bench
    assert [(key, label, fmt) for label, key, fmt in bench.ROWS] == \
        [(key, label, fmt) for key, label, fmt, _ in compare.METRICS]
