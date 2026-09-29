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
    return re.sub(r"\\n\s*", " ", text).splitlines()


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
