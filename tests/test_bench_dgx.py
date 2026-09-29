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
    assert re.search(r"^CONTAINER=bench-tv\s", text["lib.sh"], re.M)
    # no operator paths: the ssh config comes from SSH_CONFIG, the DGX home from $HOME
    assert not re.search(r"[A-Za-z]:/Users/|/home/", joined)
    for verb in ("podman kill", "podman container rm", "podman container kill", "podman container stop",
                 "prune", "podman rmi", "podman volume"):
        assert verb not in joined
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


# ---- regime labels (Task 4): spec §6, every row states its regime ------------------------

REGIME = {"page_cache": "host-warm", "first_start": "host-warm", "memory": "4g",
          "memory_swap": "host-default", "detach_memory": "8g", "concurrency": "8"}
BOOKWORM = {"python": "3.12.11", "sqlite_version": "3.40.1", "turbovec": "1.0.0",
            "openblas_num_threads": "unset"}
TRIXIE = {"python": "3.12.14", "sqlite_version": "3.53.1", "turbovec": "1.0.0",
          "openblas_num_threads": "1"}


def write_labels(d, regime=None, image=None):
    """regime.json and image.json as session.sh writes them."""
    if regime is not None:
        (d / "regime.json").write_text(json.dumps(regime))
    if image is not None:
        (d / "image.json").write_text(json.dumps(image))


def flat_session(d, lat):
    write_session(d, {"run1-raggio": {"lat_p50": lat[0]}, "run2-raggio": {"lat_p50": lat[1]}})


@needs_bash
def test_session_records_the_regime_labels(tmp_path):
    home, _ = dry_session(tmp_path)
    out = home / "s1"
    assert json.loads((out / "regime.json").read_text()) == REGIME
    assert json.loads((out / "image.json").read_text()) == {
        "python": "dry-run", "sqlite_version": "dry-run", "turbovec": "dry-run",
        "openblas_num_threads": "dry-run"}


@needs_bash
def test_swapless_gate_caps_swap_on_the_measured_runs_only(tmp_path):
    home, cmds = dry_session(tmp_path, ("MEMORY_SWAP", "4g"))
    starts = [c for c in cmds if c.startswith("podman run -d ")]
    # the first start and 4 IVF runs, the 8 GiB detach (podman's default swap), 3 flat runs
    assert [" --memory 4g --memory-swap 4g " in c for c in starts] == [True] * 5 + [False] + [True] * 3
    assert starts[5] == podman_run("8g")
    assert json.loads((home / "s1" / "regime.json").read_text())["memory_swap"] == "4g"
    runs = [c for c in cmds if c.startswith(BENCH_CMD)]
    assert len(runs) == 7 and all("Swap capped too: --memory-swap 4g." in r for r in runs)


@needs_bash
def test_first_start_regime_is_validated(tmp_path):
    home, raggio = tmp_path / "bench-home", tmp_path / "raggio"
    raggio.mkdir()
    (raggio / ".deployed-sha").write_text("abc1234\n")
    env = {**os.environ, "DRY_RUN": "1", "BENCH_HOME": home.as_posix(), "RAGGIO_DIR": raggio.as_posix()}
    session = [BASH, (DGX / "session.sh").resolve().as_posix()]
    r = subprocess.run([*session, "s1"], env={**env, "FIRST_START": "cold"}, capture_output=True, text=True)
    assert r.returncode != 0 and "use host-warm or true-cold" in r.stderr
    assert not (home / "s1").exists()
    subprocess.run([*session, "s2"], env={**env, "FIRST_START": "true-cold"}, capture_output=True, check=True)
    assert json.loads((home / "s2" / "regime.json").read_text())["first_start"] == "true-cold"


@needs_bash
def test_flat_runs_can_be_raised_for_a_concurrent_qps_claim(tmp_path):
    _, cmds = dry_session(tmp_path, ("FLAT_RUNS", "3"))
    assert [tag for _, tag, engine in bench_runs(cmds) if engine == "raggio"] == ["run0", "run1", "run2", "run3"]
    # a bad value refuses before the label directory exists, so it does not burn the label
    for var, bad in (("FLAT_RUNS", "abc"), ("FLAT_RUNS", "0"), ("FLAT_RUNS", "3 "), ("CONCURRENCY", "0")):
        sub = tmp_path / f"{var}-{len(bad)}{bad.strip()}"
        sub.mkdir()
        with pytest.raises(subprocess.CalledProcessError):
            dry_session(sub, (var, bad))
        assert not (sub / "bench-home" / "s1").exists()


@needs_bash
def test_deploy_refuses_without_an_ssh_config(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "SSH_CONFIG"}
    r = subprocess.run([BASH, (DGX / "deploy.sh").resolve().as_posix()], env=env, cwd=tmp_path,
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "set SSH_CONFIG" in r.stderr
    assert "deploying" not in r.stdout  # it stops before git or ssh


def test_image_facts_snippet_reports_sqlite_and_blas_threads():
    import sqlite3
    text = (DGX / "session.sh").read_text(encoding="utf-8")
    (snippet,) = re.findall(r"^IMAGE_FACTS='(.*?)'$", text, re.S | re.M)
    out = subprocess.run([sys.executable, "-c", snippet], env={**os.environ, "OPENBLAS_NUM_THREADS": "1"},
                         capture_output=True, text=True, check=True).stdout
    facts = json.loads(out)
    assert sorted(facts) == ["openblas_num_threads", "python", "sqlite_version", "turbovec"]
    assert facts["sqlite_version"] == sqlite3.sqlite_version
    assert facts["openblas_num_threads"] == "1"


def test_compare_prints_each_sides_regime(tmp_path):
    base, cand = tmp_path / "main", tmp_path / "cand"
    flat_session(base, (23.0, 24.0))
    flat_session(cand, (23.5, 23.5))
    write_labels(base, REGIME, BOOKWORM)
    write_labels(cand, REGIME, BOOKWORM)
    assert compare.render(base).splitlines()[1] == (
        "Regime (session): page_cache=host-warm, first_start=host-warm, memory=4g, "
        "memory_swap=host-default, concurrency=8, python=3.12.11, sqlite_version=3.40.1, "
        "openblas_num_threads=unset")
    ab = compare.render(base, cand).splitlines()
    assert ab[1].startswith("Regime (baseline): page_cache=host-warm, ")
    assert ab[2].startswith("Regime (candidate): page_cache=host-warm, ")
    assert not [ln for ln in ab if ln.startswith("The arms differ")]
    # a hand-assembled session without labels reads "unknown", never a guess
    (base / "regime.json").unlink()
    (base / "image.json").unlink()
    assert compare.regime(base) == {key: "unknown" for key in compare.REGIME_KEYS}


def test_compare_flags_arms_that_differ_in_regime(tmp_path):
    base, cand = tmp_path / "main", tmp_path / "cand"
    flat_session(base, (23.0, 24.0))
    flat_session(cand, (23.5, 23.5))
    write_labels(base, REGIME, BOOKWORM)
    write_labels(cand, REGIME, TRIXIE)
    assert ("The arms differ in python, sqlite_version, openblas_num_threads: a re-baseline across "
            "them, not a same-regime A/B (spec §6).") in compare.render(base, cand)
    write_labels(cand, {**REGIME, "memory_swap": "4g"}, BOOKWORM)  # swapless vs default swap
    assert "The arms differ in memory_swap: " in compare.render(base, cand)
    write_labels(cand, {**REGIME, "first_start": "true-cold"}, BOOKWORM)  # labels the first start only
    assert "The arms differ" not in compare.render(base, cand)
    assert compare.beyond_band(base, cand) == {"better": [], "worse": []}  # verdicts unchanged


def test_compare_reports_thin_concurrent_qps_as_not_claimed(tmp_path):
    two = {"run1-raggio": {"qps_concurrent": 129.0}, "run2-raggio": {"qps_concurrent": 131.0}}
    three = {**two, "run3-raggio": {"qps_concurrent": 200.0}}
    thin, full, cand = tmp_path / "thin", tmp_path / "full", tmp_path / "cand"
    write_session(thin, two)
    write_session(full, three)
    write_session(cand, three)
    note = "Reported, not claimed (spec §6 needs >= 3 runs per side; FLAT_RUNS=3): raggio QPS concurrent"
    assert compare.render(thin).splitlines()[-1] == note
    assert compare.render(thin, cand).splitlines()[-1] == note
    assert "Reported, not claimed" not in compare.render(full, cand)


# ---- the second IVF build (Task 4): spec §3.1 A1, the build gate builds twice per arm ----

@needs_bash
def test_a_second_ivf_build_runs_after_every_measured_run(tmp_path):
    home, cmds = dry_session(tmp_path, ("DRY_INDEX", "flat"), ("IVF_BUILDS", "2"))
    out = (home / "s1").as_posix()
    runs = [(i, *re.search(r"--out \S+/(\w+)-(raggio-ivf|raggio)\.md", c).groups())
            for i, c in enumerate(cmds) if c.startswith(BENCH_CMD)]
    assert [(tag, engine) for _, tag, engine in runs] == [
        ("run0", "raggio"), ("run1", "raggio"), ("run2", "raggio"),
        ("run0", "raggio-ivf"), ("run1", "raggio-ivf"), ("run2", "raggio-ivf"), ("run3", "raggio-ivf"),
        ("build2", "raggio-ivf")]
    # the measured runs keep the one transition (run0's build); the extra detach comes after them
    (d,) = [i for i, c in enumerate(cmds) if "-X DELETE" in c]
    assert runs[6][0] < d < runs[7][0]
    assert cmds[d - 5:d + 1] == [STOP, RM, podman_run("8g"), STARTED, IDLE, DETACH]
    assert [c for c in cmds if "--memory 8g" in c] == [podman_run("8g")]
    # build2 builds from flat in a fresh 4 GiB container, like any bench run
    b = runs[7][0]
    assert cmds[b - 6:b] == [STOP, RM, podman_run("4g"), STARTED, IDLE, "rm -f bench/results-partial.json"]
    assert "--engine raggio-ivf " in cmds[b] and cmds[b].endswith(f"--out {out}/build2-raggio-ivf.md")
    assert cmds[b + 1] == f"mv bench/results-partial.json {out}/build2-raggio-ivf.json"


@needs_bash
def test_ivf_builds_is_validated_and_needs_a_flat_start(tmp_path):
    home, raggio = tmp_path / "bench-home", tmp_path / "raggio"
    raggio.mkdir()
    (raggio / ".deployed-sha").write_text("abc1234\n")
    env = {**os.environ, "DRY_RUN": "1", "BENCH_HOME": home.as_posix(), "RAGGIO_DIR": raggio.as_posix()}
    session = [BASH, (DGX / "session.sh").resolve().as_posix()]
    r = subprocess.run([*session, "s1"], env={**env, "IVF_BUILDS": "3"}, capture_output=True, text=True)
    assert r.returncode != 0 and "IVF_BUILDS=3: use 1 or 2" in r.stderr
    assert not (home / "s1").exists()
    # an IVF start would need a second IVF <-> flat transition before the extra build
    r = subprocess.run([*session, "s2"], env={**env, "IVF_BUILDS": "2", "DRY_INDEX": "ivf"},
                       capture_output=True, text=True)
    assert r.returncode != 0 and "IVF_BUILDS=2 needs a flat start" in r.stderr
    assert BENCH_CMD not in r.stdout


def ivf_builds(d, first, second):
    """An IVF_BUILDS=2 session: run0 builds the index, build2 builds it again after the runs."""
    write_session(d, {
        "run0-raggio-ivf": {"index_build_s": first, "lat_p50": 15.5},
        "run1-raggio-ivf": {"lat_p50": 11.0}, "run2-raggio-ivf": {"lat_p50": 12.0},
        "build2-raggio-ivf": {"index_build_s": second, "lat_p50": 30.0}})


def test_the_second_ivf_build_counts_for_the_build_row_only(tmp_path):
    base, cand, cand2 = tmp_path / "main", tmp_path / "cand", tmp_path / "cand2"
    ivf_builds(base, 213.0, 221.0)
    ivf_builds(cand, 227.0, 233.0)
    ivf_builds(cand2, 215.0, 225.0)
    runs = compare.load(base)["raggio-ivf"]
    assert compare.values(runs, "index_build_s") == [213.0, 221.0]  # run0's build, then build2's
    assert compare.values(runs, "lat_p50") == [11.0, 12.0]  # build2's queries never count
    assert "| raggio-ivf | IVF index build (s) | 213 / 221 | 217 | 8 |" in compare.render(base)
    # medians against the band (spec §3.1 A1): 230 - 217 = 13 is beyond the wider band of 8
    assert "| raggio-ivf | IVF index build (s) | 217 | 8 | 230 | 6 | worse |" in compare.render(base, cand)
    assert compare.beyond_band(base, cand) == {"better": [], "worse": [("raggio-ivf", "index_build_s")]}
    # 220 - 217 = 3 is inside the candidate's band of 10: no single-build tolerance decides it
    assert "| raggio-ivf | IVF index build (s) | 217 | 8 | 220 | 10 | within band |" in compare.render(base, cand2)
    assert compare.beyond_band(base, cand2) == {"better": [], "worse": []}


# ---- spec §3.1 G7 knob and G5 rows (Task 5): CONCURRENCY in every run and in the regime ----

def test_lib_reads_the_concurrency_from_the_environment():
    lib = (DGX / "lib.sh").read_text(encoding="utf-8")
    session = (DGX / "session.sh").read_text(encoding="utf-8")
    assert 'CONCURRENCY="${CONCURRENCY:-8}"' in lib
    assert '--concurrency "$CONCURRENCY" --cpu-container "$CONTAINER"' in session
    assert '"concurrency": "%s"' in session


@needs_bash
def test_session_passes_the_concurrency_and_the_cpu_container(tmp_path):
    home, cmds = dry_session(tmp_path)
    runs = [c for c in cmds if c.startswith(BENCH_CMD)]
    assert len(runs) == 7 and all(" --concurrency 8 --cpu-container bench-tv " in r for r in runs)
    assert json.loads((home / "s1" / "regime.json").read_text())["concurrency"] == "8"
    c16 = tmp_path / "c16"  # a spec §3.1 G7 c=16 session
    c16.mkdir()
    home, cmds = dry_session(c16, ("CONCURRENCY", "16"))
    runs = [c for c in cmds if c.startswith(BENCH_CMD)]
    assert len(runs) == 7 and all(" --concurrency 16 --cpu-container bench-tv " in r for r in runs)
    assert json.loads((home / "s1" / "regime.json").read_text())["concurrency"] == "16"


def test_compare_flags_arms_that_ran_at_different_concurrency(tmp_path):
    base, cand = tmp_path / "main", tmp_path / "cand"
    flat_session(base, (23.0, 24.0))
    flat_session(cand, (23.5, 23.5))
    write_labels(base, REGIME, BOOKWORM)
    write_labels(cand, {**REGIME, "concurrency": "16"}, BOOKWORM)
    ab = compare.render(base, cand)
    assert ("The arms differ in concurrency: a re-baseline across them, not a same-regime A/B "
            "(spec §6).") in ab
    assert "memory_swap=host-default, concurrency=16, python=3.12.11" in ab.splitlines()[2]
    # a session recorded before the label existed reads "unknown", which differs too
    write_labels(cand, {k: v for k, v in REGIME.items() if k != "concurrency"}, BOOKWORM)
    assert "The arms differ in concurrency: " in compare.render(base, cand)


def test_compare_leaves_absent_cpu_rows_out_of_the_verdicts(tmp_path):
    base, cand, none = tmp_path / "main", tmp_path / "cand", tmp_path / "none"
    write_session(base, {"run1-raggio": {"lat_p50": 23.0, "hybrid_c_p99": 900.0},
                         "run2-raggio": {"lat_p50": 24.0, "hybrid_c_p99": 950.0}})
    write_session(cand, {
        "run1-raggio": {"lat_p50": 23.5, "hybrid_c_p99": 700.0, "cpu_ms_per_q_c": 50.8,
                        "hybrid_cpu_ms_per_q_c": 365.1},
        "run2-raggio": {"lat_p50": 23.5, "hybrid_c_p99": 720.0, "cpu_ms_per_q_c": 116.0,
                        "hybrid_cpu_ms_per_q_c": 361.9}})
    write_session(none, {"run1-raggio": {"lat_p50": 23.0}, "run2-raggio": {"lat_p50": 24.0}})
    ab = compare.render(base, cand)
    # one side lacks the CPU rows (a laptop, or a session before Task 5): "-", never 0, no verdict
    assert "| raggio | CPU per query under concurrency (ms) | - | - | 83.4 | 65.2 | - |" in ab
    assert "| raggio | Hybrid CPU per query under concurrency (ms) | - | - | 363.5 | 3.2 | - |" in ab
    assert "| raggio | Hybrid p99 under concurrency (ms) | 925.0 | 50.0 | 710.0 | 20.0 | better |" in ab
    assert compare.beyond_band(base, cand) == {"better": [("raggio", "hybrid_c_p99")], "worse": []}
    # neither side has them: no row at all
    assert "CPU per query" not in compare.render(base, none)
    assert "CPU per query" not in compare.render(none)
    # all three are lower-is-better: p4's fast state (50.8-56.8 ms) beats its slow one (115.9-117.6)
    assert [compare.HIGHER_IS_BETTER[k] for k in
            ("cpu_ms_per_q_c", "hybrid_c_p99", "hybrid_cpu_ms_per_q_c")] == [False] * 3
    assert compare.verdict("cpu_ms_per_q_c", [115.9, 117.6], [50.8, 56.8]) == "better"
