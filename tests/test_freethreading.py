"""Free-threaded CPython 3.14t (ADR 0006, spec D6): the GIL state /healthz reports, the
GC_FREEZE knob, meta.db connections that really close on a free-threaded build, the
build-time image check and the GIL guard (the guard skips on GIL builds), the Dockerfile's
3.14t path, the non-blocking 3.14t CI job, the Unicode drift probe and the docs."""
import importlib.machinery
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import time
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


def _collect_until_dead(ref):
    # gc.collect() returns 0 without collecting while another thread's collection is
    # still running, so one call can leave a dead cycle alive: retry, bounded. A frozen
    # cycle is never collected, however often this runs
    import gc

    for _ in range(100):
        gc.collect()
        if ref() is None:
            break
        time.sleep(0.01)


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
        _collect_until_dead(alive)
        assert alive() is None  # a cycle allocated after the freeze is still collected
    assert gc.get_freeze_count() == 0  # unfrozen on shutdown


def test_gc_freeze_still_frees_a_collection_loaded_before_the_freeze(make_app, monkeypatch):
    # the freeze runs after resume_pending, so a collection a replayed job loaded is part
    # of the frozen heap. Deleting it must still free it and its index: reference counting
    # does that, and the collector, which skips frozen objects, is never needed
    import gc
    import weakref

    from raggio.store import CollectionManager

    root = {"x-api-key": "root-key"}
    unit = (np.ones(DIM) / np.sqrt(DIM)).tolist()
    with TestClient(make_app()) as client:
        assert client.post("/collections", headers=root, json={"name": "kb", "dim": DIM}).status_code == 201
        job = client.post("/collections/kb/documents", headers=root, json={"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": unit}]}]}).json()["job_id"]
        deadline = time.monotonic() + 30
        while client.get(f"/collections/kb/jobs/{job}", headers=root).json()["status"] != "done":
            assert time.monotonic() < deadline, "the ingest job did not finish"
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
        _collect_until_dead(loaded[0])
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


# ---- meta.db connections close for real on a free-threaded build ----


def test_stop_really_closes_meta_db_after_reads_on_many_threads(tmp_path, monkeypatch):
    # every thread that served a read opened its own connection, and stop() closes them
    # all from one other thread. On a free-threaded build a statement cached by its
    # owning thread is only freed when that thread next runs Python (biased reference
    # counting), so each cached statement kept its closed connection alive as a zombie:
    # meta.db-wal/-shm stayed on disk and, on Windows, delete could not remove the dir
    import asyncio

    from raggio.config import Settings
    from raggio.store import CollectionManager

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_DIM", str(DIM))
    m = CollectionManager(Settings(), lambda cfg: FakeEmbedder())
    unit = np.ones(DIM) / np.sqrt(DIM)

    async def run():
        await m.create_collection("x", DIM, 4, None, None, None)
        c = await m.touch("x")
        await c.enqueue({"documents": [
            {"doc_id": "d", "chunks": [{"id": "c", "text": "t", "vector": unit.tolist()}]}]})
        deadline = time.monotonic() + 30
        while c.pending_jobs():
            assert time.monotonic() < deadline, "the ingest job did not drain"
            await asyncio.sleep(0.01)
        # the default executor's threads stay alive and idle across shutdown
        await asyncio.gather(*(asyncio.to_thread(c.list_records, "both", None, None, 10, 0)
                               for _ in range(8)))
        await asyncio.gather(*(asyncio.to_thread(c.get_document, "d") for _ in range(8)))
        await m.shutdown()
        return sorted(p.name for p in m._dir("x").glob("meta.db*"))

    assert asyncio.run(run()) == ["meta.db"]


# ---- the build-time image check ----

CHECK_IMAGE = ROOT / "docker" / "check_image.py"
THIS_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}{'t' if FT else ''}"


def load_check_image():
    spec = importlib.util.spec_from_file_location("check_image", CHECK_IMAGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child(args, env=None):
    """A fresh interpreter without the caller's warning filter or GIL override."""
    clean = {k: v for k, v in os.environ.items() if k not in ("PYTHONWARNINGS", "PYTHON_GIL")}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=ROOT,
                          env={**clean, **(env or {})}, timeout=300)


def test_check_image_passes_on_this_interpreter():
    native = ["--require-native"] if os.environ.get("REQUIRE_NATIVE") == "1" else []
    r = child([str(CHECK_IMAGE), "--python", THIS_PYTHON, *native])
    assert r.returncode == 0, r.stderr
    assert f"GIL {'disabled' if FT else 'enabled'}," in r.stdout
    assert "extension modules imported" in r.stdout


@pytest.mark.parametrize("python", [
    f"3.{sys.version_info.minor}{'' if FT else 't'}",  # the other flavour
    f"3.{sys.version_info.minor - 1}{'t' if FT else ''}",  # another minor
])
def test_check_image_rejects_an_interpreter_the_build_arg_did_not_ask_for(python):
    r = child([str(CHECK_IMAGE), "--python", python])
    assert r.returncode == 1 and f"check_image: FAILED: PYTHON={python}, but" in r.stderr


def test_check_image_refuses_python_gil_0():
    r = child([str(CHECK_IMAGE), "--python", THIS_PYTHON], env={"PYTHON_GIL": "0"})
    assert r.returncode == 1 and "PYTHON_GIL=0 / -X gil=0 hides" in r.stderr


@needs_ft
def test_check_image_fails_when_the_gil_is_on():
    # the guard's red run: the same interpreter with the GIL forced on
    r = child(["-X", "gil=1", str(CHECK_IMAGE), "--python", THIS_PYTHON])
    assert r.returncode == 1 and "the GIL is enabled before any import" in r.stderr


def test_check_image_finds_every_compiled_module_it_can_import(tmp_path):
    check_image = load_check_image()
    suffixes = (".cpython-314t-x86_64-linux-gnu.so", ".so")
    for rel in ("numpy/_core/_multiarray_umath.cpython-314t-x86_64-linux-gnu.so",
                "raggio_native/raggio_native.cpython-314t-x86_64-linux-gnu.so",
                "yaml/_yaml.so",
                "numpy.libs/libscipy_openblas64_-6bb31eeb.so",  # a vendored lib, not a module
                "numpy/_core/multiarray.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"")
    assert check_image.extension_modules(tmp_path, suffixes) == [
        "numpy._core._multiarray_umath", "raggio_native.raggio_native", "yaml._yaml"]
    # spec §4.2: raggio_native is rebuilt per interpreter; an abi3 or bare .so build is refused
    assert check_image.built_for_this_interpreter(
        "raggio_native/raggio_native.cpython-314t-x86_64-linux-gnu.so", suffixes)
    assert not check_image.built_for_this_interpreter("raggio_native/raggio_native.abi3.so", suffixes)
    assert not check_image.built_for_this_interpreter("raggio_native/raggio_native.so", suffixes)


@pytest.fixture
def gil_needing_extension(tmp_path):
    """An extension module that declares no free-threading support, so importing it
    really re-enables the GIL, under the name _testmultiphase_nonmodule. Where CPython
    ships its _testmultiphase test extension (Windows), that is copied under the name of
    the one module in it that declares none. Where it ships none (uv's Linux
    interpreters have no lib-dynload), a one-function single-phase module with no
    Py_mod_gil slot is compiled instead. Returns the code that puts it on sys.path."""
    spec = importlib.util.find_spec("_testmultiphase")
    if spec is not None:
        suffix = next(s for s in importlib.machinery.EXTENSION_SUFFIXES if spec.origin.endswith(s))
        shutil.copy(spec.origin, tmp_path / f"_testmultiphase_nonmodule{suffix}")
    elif sys.platform.startswith("linux"):
        _compile_gil_needing_extension(tmp_path)
    else:
        pytest.skip("this CPython build ships no _testmultiphase test extension")
    return f"import sys; sys.path.insert(0, {str(tmp_path)!r}); "


def _compile_gil_needing_extension(tmp_path):
    def unavailable(why):
        # REQUIRE_NATIVE=1 (CI's free-threaded step, the DGX runs) turns the skip into a failure
        (pytest.fail if os.environ.get("REQUIRE_NATIVE") == "1" else pytest.skip)(why)

    include = sysconfig.get_path("include")
    if not (Path(include) / "Python.h").is_file():
        unavailable(f"no Python.h in {include}, so no GIL-needing test extension can be built")
    cc = shlex.split(sysconfig.get_config_var("CC") or "cc")
    if not shutil.which(cc[0]):
        cc = [shutil.which("cc")] if shutil.which("cc") else []
    if not cc:
        unavailable("no C compiler, so no GIL-needing test extension can be built")
    src = tmp_path / "_testmultiphase_nonmodule.c"
    src.write_text(
        "#include <Python.h>\n"
        "static struct PyModuleDef def = "
        '{PyModuleDef_HEAD_INIT, "_testmultiphase_nonmodule", NULL, -1, NULL};\n'
        "PyMODINIT_FUNC PyInit__testmultiphase_nonmodule(void) { return PyModule_Create(&def); }\n",
        encoding="utf-8",
    )
    out = tmp_path / f"_testmultiphase_nonmodule{importlib.machinery.EXTENSION_SUFFIXES[0]}"
    r = subprocess.run([*cc, "-shared", "-fPIC", "-I", include, str(src), "-o", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.fail(f"compiling the GIL-needing test extension failed (rc {r.returncode}):\n"
                    + r.stderr[-2000:])


@needs_ft
def test_the_image_warning_filter_fails_a_real_gil_re_enable(gil_needing_extension):
    probe = gil_needing_extension + "import _testmultiphase_nonmodule; print(sys._is_gil_enabled())"
    unguarded = child(["-c", probe])
    assert unguarded.returncode == 0 and unguarded.stdout.strip() == "True"  # the GIL came back
    # the string the Dockerfile's runtime ENV and the CI job put in PYTHONWARNINGS
    guarded = child(["-c", probe], env={"PYTHONWARNINGS": load_check_image().GIL_WARNING_FILTER})
    assert guarded.returncode == 1
    assert "RuntimeWarning: The global interpreter lock (GIL) has been enabled" in guarded.stderr


@needs_ft
def test_check_image_names_the_module_that_enabled_the_gil(gil_needing_extension):
    # even with the warning swallowed, the GIL state check after each import catches it
    probe = gil_needing_extension + (
        f"sys.path.insert(0, {str(CHECK_IMAGE.parent)!r}); import check_image, warnings; "
        "warnings.simplefilter('ignore'); check_image.import_all(['_testmultiphase_nonmodule'])")
    r = child(["-c", probe])
    assert r.returncode == 1
    assert "CheckFailed: importing _testmultiphase_nonmodule enabled the GIL" in r.stderr


# ---- the unified Dockerfile's 3.14t path (spec D2) ----

TURBOVEC_MIN_RUST = (1, 89)  # turbovec 1.0.0's rust-version: 3.14t builds it from its sdist


def _stages():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = re.split(r"^FROM ", dockerfile, flags=re.M)[1:]
    (builder,) = [s for s in stages if s.split("\n", 1)[0].endswith(" AS builder")]
    return dockerfile, builder, stages[-1]


def test_the_builder_checks_the_venv_it_built():
    _, builder, _ = _stages()
    sync = builder.index("RUN uv sync --frozen --no-dev --extra native")
    copy = builder.index("COPY docker/check_image.py /tmp/check_image.py")
    run = builder.index('RUN /app/.venv/bin/python /tmp/check_image.py --python "${PYTHON}" '
                        "--require-native")
    assert sync < copy < run  # the full venv exists, and the script stays out of /app


def test_the_runtime_image_fails_loudly_on_a_gil_re_enable():
    dockerfile, _, runtime = _stages()
    check_image = load_check_image()
    assert f'ENV PYTHONWARNINGS="{check_image.GIL_WARNING_FILTER}"' in runtime.splitlines()
    # nothing but comments may mention the override that would hide a re-enable
    code = [ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "PYTHON_GIL" in ln or "gil=" in ln.lower()]
    # the ABAB arms differ in the interpreter only (spec §6): every PYTHON gets A's single
    # OpenBLAS thread (D16), set once and by nothing else, and C's trim threshold (D13)
    assert [ln for ln in code if "OPENBLAS_NUM_THREADS" in ln] == ["ENV OPENBLAS_NUM_THREADS=1"]
    assert "ENV OPENBLAS_NUM_THREADS=1" in runtime.splitlines()
    assert "ENV MALLOC_TRIM_THRESHOLD_=134217728" in runtime.splitlines()


def test_the_builder_can_build_turbovec_from_its_sdist():
    dockerfile, builder, _ = _stages()
    rust = re.search(r"^ARG RUST_VERSION=(\d+)\.(\d+)\.\d+$", dockerfile, re.M)
    assert (int(rust[1]), int(rust[2])) >= TURBOVEC_MIN_RUST
    # --build-arg UV_NO_BINARY_PACKAGE=turbovec: a local sdist build on a GIL interpreter too
    # (C1), still abi3 (the sdist enables abi3-py39), so not 3.14t's cp314t build; unset, uv
    # installs PyPI's abi3 wheel as before, and 3.14t falls back to the sdist on its own
    assert re.search(r"^ARG UV_NO_BINARY_PACKAGE$", builder, re.M)
    assert builder.index("ARG UV_NO_BINARY_PACKAGE") < builder.index("RUN uv sync --frozen")
    # raggio_native is rebuilt for each image's own interpreter, never abi3 (spec §4.2):
    # PyO3 builds for the /python that uv installed for PYTHON (Plan E's builder)
    assert 'uv python find "${PYTHON}"' in builder
    assert "ENV PYO3_PYTHON=/usr/local/bin/python3" in builder.splitlines()
    syncs = re.findall(r"^RUN uv sync (.*)$", builder, re.M)
    assert len(syncs) == 2 and all("--extra native" in s for s in syncs)


# ---- the non-blocking 3.14t CI job (spec D11) ----


def _jobs():
    import yaml  # uvicorn[standard] installs PyYAML

    ci = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    return ci, yaml.safe_load(ci)["jobs"]


def _runs(job):
    return {s["run"].strip(): s.get("env", {}) for s in job["steps"] if "run" in s}


def test_ci_runs_the_suite_on_314t_without_blocking_the_workflow():
    ci, jobs = _jobs()
    ft = jobs["freethreaded"]
    assert ft["continue-on-error"] is True  # experimental until 3.14t is the default
    assert ft["timeout-minutes"] <= 30
    # x86_64 and aarch64 (the DGX), the same runners as the native job
    assert ft["runs-on"] == "${{ matrix.runner }}"
    assert ft["strategy"]["matrix"]["runner"] == jobs["native"]["strategy"]["matrix"]["runner"]
    # the same uv action, pin and uv version as the blocking job; only the interpreter differs
    (setup,) = [s for s in ft["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    (base,) = [s for s in jobs["test"]["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert setup["uses"] == base["uses"]
    assert setup["with"] == {**base["with"], "python-version": "3.14t"}
    # turbovec builds from its sdist and raggio-native from native/: the image's Rust pin
    rust = [s["run"] for s in jobs["native"]["steps"] if s.get("name", "").startswith("Install Rust")]
    assert rust and rust[0] in [s.get("run") for s in ft["steps"]]


def test_ci_314t_job_turns_a_gil_re_enable_into_a_failure():
    ci, jobs = _jobs()
    ft = jobs["freethreaded"]
    assert ft["env"]["PYTHONWARNINGS"] == load_check_image().GIL_WARNING_FILTER
    runs = _runs(ft)
    # no --group bench: orjson has no free-threaded wheel
    assert "uv sync --frozen --extra native" in runs
    assert runs["uv run --no-sync pytest -q"] == {"REQUIRE_NATIVE": "1"}
    assert "uv run --no-sync python docker/check_image.py --python 3.14t --require-native" in runs
    stress = runs['uv run --no-sync pytest -q tests/test_concurrency.py -k "mixed_workload or stop_under_load"']
    assert float(stress["RAGGIO_STRESS_SECONDS"]) >= 30
    assert "PYTHON_GIL" not in ci and "gil=0" not in ci.lower()


# ---- Unicode drift between the arms (3.12: Unicode 15.0.0, 3.14t: 16.0.0) ----


def load_drift_probe():
    path = ROOT / "bench" / "unicode_drift_probe.py"
    spec = importlib.util.spec_from_file_location("unicode_drift_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_drift_probe_dump_records_what_the_tokenizer_does_per_code_point():
    probe = load_drift_probe()
    dump = probe.dump(range(0x41, 0x42))  # "A"
    dump.update(probe.dump([0x5F, 0xE9, 0x301, 0xFB01, 0x378]))
    assert dump["65"] == ["Lu", "a"]
    assert dump["95"] == ["Pc", " "]  # '_' separates tokens
    assert dump["233"] == ["Ll", "e"]  # NFKD folds the accent away
    assert dump["769"] == ["Mn", ""]  # a combining mark alone vanishes
    assert dump["64257"] == ["Ll", "fi"]
    assert "888" not in dump  # U+0378 is unassigned and no word character: not recorded


def test_drift_probe_diff_separates_new_code_points_from_changed_ones():
    probe = load_drift_probe()
    a = {"unidata": "15.0.0", "cp": {"65": ["Lu", "a"], "95": ["Pc", " "], "300": ["Lo", "x"]}}
    b = {"unidata": "16.0.0", "cp": {"65": ["Lu", "a"], "95": ["Pc", " "], "300": ["Lo", " "],
                                     "7000": ["Lo", "z"], "7001": ["So", " "]}}
    assert probe.diff(a, b) == {
        "from": "15.0.0", "to": "16.0.0",
        "newly_assigned": [7000, 7001],  # unassigned (Cn) in the first dump
        "changed": [300],  # assigned in both, recorded differently
        "token_drift": [300, 7000],  # the ones whose tokens differ
    }


def test_drift_probe_scan_counts_corpus_rows_with_a_drifted_code_point(tmp_path):
    import json

    probe = load_drift_probe()
    drift = {"from": "15.0.0", "to": "16.0.0", "newly_assigned": [0x1E5D0, 0x2B740],
             "changed": [], "token_drift": [0x1E5D0]}
    rows = [{"title": "plain", "text": "nothing new"},
            {"title": "Ol Onal \U0001E5D0", "text": "a new word character"},
            {"title": "t", "text": "a new symbol \U0002B740"},
            {"title": "beyond the limit \U0001E5D0", "text": ""}]
    corpus = tmp_path / "abstracts.jsonl"
    corpus.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert probe.scan(drift, corpus, limit=3) == {
        "rows": 3, "rows_with_drift": 2, "rows_with_token_drift": 1,
        "first_rows_with_drift": [1, 2]}


# ---- docs: ADR 0006, the knob, the /healthz example, the build arg ----


def test_docs_record_the_evaluation_and_document_the_knob(make_app):
    import json

    adr = (ROOT / "docs/adr/0006-free-threaded-python.md").read_text(encoding="utf-8")
    for needle in ("PYTHON=3.14t", "GC_FREEZE", "gil_enabled", "cp314t", "## Decision rule",
                   "**Continue**", "**Hold**", "**Park**", "## Verification",
                   "bench/unicode_drift_probe.py", "docker/check_image.py",
                   "sqlite_version", "OPENBLAS_NUM_THREADS=1"):  # spec D15, D16
        assert needle in adr, needle
    # committed text names no local path, ssh detail, user or host address (G-R16)
    for private in ("D:/", "C:/", "/home/", "/Users/", "/root/",
                    "ssh_config", ".ssh/", "@gn100"):
        assert private not in adr, private
    assert re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", adr) is None
    for path in ("README.md", "docs/getting-started.md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "| `GC_FREEZE` |" in text, path
        assert "podman build --build-arg PYTHON=3.14t -t raggio:py314t ." in text, path
    # the /healthz example in the API reference shows every key the endpoint returns
    api = (ROOT / "docs/api.md").read_text(encoding="utf-8")
    example = re.search(r"`GET /healthz`.*?\*\*200\*\* `(\{.*?\})`", api, re.S).group(1)
    with TestClient(make_app()) as client:
        assert set(json.loads(example)) == set(client.get("/healthz").json())
