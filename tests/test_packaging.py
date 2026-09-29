"""Packaging (spec D2, D5): orjson is a bench-only dependency, uv >= 0.12 is required to
touch the lock, the lock matches pyproject, and every documented way to run an
orjson-importing bench script asks for the bench group."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
LOCK = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def _name(req):
    return re.match(r"[A-Za-z0-9._-]+", req).group(0).lower()


def test_orjson_is_a_bench_only_dependency():
    groups = PYPROJECT["dependency-groups"]
    assert "orjson" in [_name(r) for r in groups["bench"]]
    assert "orjson" not in [_name(r) for r in PYPROJECT["project"]["dependencies"]]
    assert "orjson" not in [_name(r) for r in groups["dev"]]
    # runtime JSON stays on the stdlib (ADR 0001:31)
    assert [p for p in (ROOT / "src").rglob("*.py") if "orjson" in p.read_text(encoding="utf-8")] == []


def test_uv_floor_is_declared():
    assert PYPROJECT["tool"]["uv"]["required-version"] == ">=0.12"


def test_lock_matches_the_dependency_groups():
    (raggio,) = [p for p in LOCK["package"] if p["name"] == "raggio"]
    locked = {g: sorted((d["name"], d.get("specifier", "")) for d in deps)
              for g, deps in raggio["metadata"]["requires-dev"].items()}
    declared = {g: sorted((_name(r), r[len(_name(r)):]) for r in reqs)
                for g, reqs in PYPROJECT["dependency-groups"].items()}
    assert locked == declared
    assert LOCK["revision"] >= 3  # a uv >= 0.12 re-lock writes revision 3; the old lock is 2


def _shell_lines(text):
    """Lines with backslash continuations joined, so a wrapped command reads as one."""
    return re.sub(r"\\\n\s*", " ", text).splitlines()


def test_orjson_scripts_are_documented_with_the_bench_group():
    # a plain `uv sync` no longer installs orjson: a command without --group bench dies
    # with ModuleNotFoundError before it measures anything
    scripts = [p.name for p in (ROOT / "bench").glob("*.py") if "import orjson" in p.read_text(encoding="utf-8")]
    assert "bench.py" in scripts
    sources = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "bench").glob("*.py"))]
    missing = [f"{p.relative_to(ROOT).as_posix()}: {line.strip()}"
               for p in sources for line in _shell_lines(p.read_text(encoding="utf-8"))
               if "uv run" in line and any(f"bench/{s}" in line for s in scripts)
               and "--group bench" not in line]
    assert missing == []


def test_develop_instructions_and_the_dgx_setup_sync_the_bench_group():
    # without the group, the bench-harness tests skip and the DGX venv can't run bench.py
    for doc in ("README.md", "docs/getting-started.md"):
        assert "uv sync --group bench" in (ROOT / doc).read_text(encoding="utf-8").splitlines()
    setup = (ROOT / "bench/dgx/setup.sh").read_text(encoding="utf-8")
    assert 'run "$HOME/.local/bin/uv" sync --frozen --group bench' in setup.splitlines()


def _ci():
    return (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")


def _ci_job(name):
    """One job's lines of tests.yml: the jobs later plans add (native, 3.14t) carry their
    own steps and pins."""
    lines = _ci().splitlines()
    start = lines.index(f"  {name}:")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^ {0,2}\S", lines[i])), len(lines))
    return "\n".join(lines[start:end])


def _floor():
    return tuple(int(x) for x in PYPROJECT["tool"]["uv"]["required-version"].removeprefix(">=").split("."))


def test_ci_pins_uv_above_the_floor_and_python_312():
    job = _ci_job("test")
    assert re.search(r"^    runs-on: ubuntu-latest$", job, re.M)
    # third-party action pinned by commit, tag in the comment
    assert re.search(r"^\s+- uses: astral-sh/setup-uv@[0-9a-f]{40} # v\d+\.\d+\.\d+$", job, re.M)
    uv = re.search(r'^\s+version: "(\d+\.\d+\.\d+)"', job, re.M).group(1)
    assert tuple(int(x) for x in uv.split(".")) >= _floor()
    assert re.search(r'^\s+python-version: "3\.12"$', job, re.M)


def test_ci_checks_the_lock_and_runs_the_suite_with_the_bench_group():
    job = _ci_job("test")
    steps = [ln.strip().removeprefix("- run: ") for ln in job.splitlines() if ln.strip().startswith("- run: ")]
    # without --group bench the bench-harness tests would skip instead of run
    assert steps == ["uv lock --check", "uv sync --frozen --group bench", "uv run --no-sync pytest -q"]
    ci = _ci()
    assert re.search(r"^  pull_request:", ci, re.M) and re.search(r"^  push:\n    branches: \[main\]", ci, re.M)


CMD = ('CMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", '
       '"--no-access-log"]')


def _dockerfile():
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_pins_the_ci_uv_and_defaults_to_312_on_trixie():
    d = _dockerfile()
    uv = re.search(r"^ARG UV_VERSION=(\S+)$", d, re.M)
    assert uv and uv.group(1) == re.search(r'^\s+version: "(\S+)"', _ci_job("test"), re.M).group(1)
    assert re.search(r"^ARG PYTHON=3\.12$", d, re.M)
    froms = re.findall(r"^FROM (\S+)", d, re.M)
    assert froms[0] == "ghcr.io/astral-sh/uv:${UV_VERSION}"
    assert froms[-1] == "docker.io/library/debian:trixie-slim"
    assert "bookworm" not in d  # the bookworm uv tags stopped at 0.9.30
    assert re.search(r"^RUN uv python install \$\{PYTHON\}$", d, re.M)


def test_image_keeps_the_runtime_contract():
    d = _dockerfile()
    runtime = re.split(r"^FROM ", d, flags=re.M)[-1]  # the last stage is the image
    # the old base's `useradd -m app` got uid 1000; existing /data volumes are owned by it
    assert "RUN useradd -m --uid 1000 app && mkdir /data && chown app /data" in runtime
    # python:3.12-slim set LANG=C.UTF-8; debian:trixie-slim sets no locale
    assert re.search(r"^ENV LANG=C\.UTF-8 DATA_DIR=/data PATH=\"/app/\.venv/bin:\$PATH\"$", runtime, re.M)
    for line in ("COPY --from=builder /python /python", "WORKDIR /app", "USER app", "VOLUME /data",
                 "EXPOSE 8000"):
        assert re.search(f"^{re.escape(line)}$", runtime, re.M), line
    assert runtime.rstrip().endswith(CMD)
    # the image installs no dependency group: no pytest, no orjson
    syncs = re.findall(r"^RUN uv sync (.*)$", d, re.M)
    assert syncs and all("--frozen" in s and "--no-dev" in s and "--group" not in s for s in syncs)


def test_image_runs_blas_single_threaded():
    # spec D16: numpy's OpenBLAS otherwise spins one thread per core under search load
    runtime = re.split(r"^FROM ", _dockerfile(), flags=re.M)[-1]
    assert re.search(r"^ENV OPENBLAS_NUM_THREADS=1$", runtime, re.M)
