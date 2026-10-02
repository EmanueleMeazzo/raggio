"""Build-time image check (ADR 0006): the interpreter is the flavour PYTHON names, and on
a free-threaded build nothing raggio can load turns the GIL back on.

CPython 3.13t+ re-enables the GIL, with nothing but a RuntimeWarning, when it imports an
extension module that has not declared free-threading support. The filter below is
installed before any other import and makes that warning an error; the GIL state is also
checked after every import, in case a caller swallows the error. PYTHON_GIL=0 / -X gil=0
would hide exactly this and are refused.

The Dockerfile's builder stage runs it against /app/.venv; the free-threaded CI job and
tests/test_freethreading.py run it too:
    python docker/check_image.py --python 3.14t --require-native
"""
import re
import warnings

GIL_WARNING = "The global interpreter lock (GIL) has been enabled"
# the same filter as a -W / PYTHONWARNINGS value (that syntax escapes the message itself);
# the Dockerfile's runtime ENV and the CI job set exactly this string
GIL_WARNING_FILTER = f"error:{GIL_WARNING}:RuntimeWarning"
warnings.filterwarnings("error", message=re.escape(GIL_WARNING), category=RuntimeWarning)

import argparse  # noqa: E402
import importlib  # noqa: E402
import importlib.machinery  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import sys  # noqa: E402
import sysconfig  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import zlib  # noqa: E402
from pathlib import Path  # noqa: E402

DIM = 8
ROWS = 1100  # > IVF_MIN_ROWS (1024): the IVF attach and its shard fan-out run too
ROOT_KEY = "check-image-root-key"


class CheckFailed(Exception):
    pass


def free_threaded() -> bool:
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def gil_enabled() -> bool:
    return getattr(sys, "_is_gil_enabled", lambda: True)()


def check_flavour(python: str) -> None:
    """PYTHON=3.14t must give a free-threaded 3.14, PYTHON=3.12 a GIL 3.12."""
    want_ft = python.endswith("t")
    if free_threaded() != want_ft:
        kind = "free-threaded" if free_threaded() else "GIL"
        raise CheckFailed(f"PYTHON={python}, but this is a {kind} build ({platform.python_version()})")
    want = python.removesuffix("t").split(".")
    if platform.python_version().split(".")[: len(want)] != want:
        raise CheckFailed(f"PYTHON={python}, but this is Python {platform.python_version()}")
    if os.environ.get("PYTHON_GIL") == "0" or sys._xoptions.get("gil") == "0":
        raise CheckFailed("PYTHON_GIL=0 / -X gil=0 hides a GIL re-enable: unset it")
    if want_ft and gil_enabled():
        raise CheckFailed("the GIL is enabled before any import (PYTHON_GIL=1 or -X gil=1?)")


def extension_modules(site: Path, suffixes=tuple(importlib.machinery.EXTENSION_SUFFIXES)) -> list[str]:
    """Dotted names of the compiled extension modules installed under `site`. Files whose
    path is not an importable name (the vendored libs in numpy.libs/, ...) are skipped."""
    names = []
    for path in sorted(site.rglob("*")):
        suffix = next((s for s in suffixes if path.name.endswith(s)), None)
        if suffix is None or not path.is_file():
            continue
        parts = [*path.relative_to(site).parts[:-1], path.name[: -len(suffix)]]
        if all(p.isidentifier() for p in parts):
            names.append(".".join(parts))
    return names


def built_for_this_interpreter(path: str, suffixes=tuple(importlib.machinery.EXTENSION_SUFFIXES)) -> bool:
    """The file carries this interpreter's own tag, the first extension suffix
    (.cpython-314t-aarch64-linux-gnu.so, .cp312-win_amd64.pyd, ...): not .abi3.so, not a bare .so."""
    return str(path).endswith(suffixes[0])


def check_native() -> None:
    """spec §4.2: raggio_native is built for the image's own interpreter, without abi3. A
    build for another interpreter would not load on 3.14t, and raggio would fall back to the
    Python scorer without a word."""
    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    files = sorted(m.__file__ for name, m in list(sys.modules.items())
                   if name.split(".")[0] == "raggio_native"
                   and str(getattr(m, "__file__", None) or "").endswith(suffixes))
    if not files or not all(built_for_this_interpreter(f, suffixes) for f in files):
        raise CheckFailed(f"raggio_native is not built for this interpreter ({suffixes[0]}): {files}")


def import_all(names: list[str]) -> None:
    for name in names:
        importlib.import_module(name)
        if free_threaded() and gil_enabled():
            raise CheckFailed(f"importing {name} enabled the GIL")


def site_dirs() -> list[Path]:
    return sorted({Path(sysconfig.get_path("purelib")), Path(sysconfig.get_path("platlib"))})


class FakeEmbedder:
    async def embed(self, texts):
        import numpy as np

        out = []
        for t in texts:
            v = np.random.default_rng(zlib.crc32(t.encode())).standard_normal(DIM)
            out.append((v / np.linalg.norm(v)).tolist())
        return out

    async def aclose(self):
        pass


def _ok(response, status=200):
    if response.status_code != status:
        raise CheckFailed(f"{response.request.method} {response.request.url.path}: "
                          f"{response.status_code} {response.text}")
    return response.json()


def _wait(client, job_id, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = _ok(client.get(f"/collections/check/jobs/{job_id}"))
        if job["status"] == "done":
            return
        if job["status"] == "error":
            raise CheckFailed(f"job {job_id} failed: {job['error']}")
        time.sleep(0.05)
    raise CheckFailed(f"job {job_id} did not finish in {timeout:.0f}s")


def exercise(require_native: bool) -> dict:
    """uvicorn's own loading of the app, then one pass over the request path: create,
    ingest, get, list, vector/text/hybrid/filtered search, IVF attach, the same searches
    on the IVF index, detach, delete. Returns the final /healthz body."""
    import numpy as np
    import uvicorn
    from fastapi.testclient import TestClient

    from raggio.app import create_app

    config = uvicorn.Config("raggio.app:create_app", factory=True)
    config.load()  # the http/ws protocols and lifespan uvicorn would serve with
    config.get_loop_factory()  # and its event loop (uvloop where it is installed)
    import_all(sorted(m for m in sys.modules if m.startswith(("uvicorn", "uvloop"))))

    with TestClient(create_app(embedder_factory=lambda cfg: FakeEmbedder()),
                    headers={"x-api-key": ROOT_KEY}) as client:
        _ok(client.post("/collections", json={"name": "check", "dim": DIM}), 201)
        rng = np.random.default_rng(0)
        docs = [{"doc_id": f"d{i}", "chunks": [{
            "id": f"c{i}", "text": f"chunk {i} about {'solar wind' if i % 2 else 'deep sea'}",
            "vector": (v / np.linalg.norm(v)).tolist(),
            "metadata": {"group": "a" if i % 3 else "b", "n": i}}]}
            for i, v in enumerate(rng.standard_normal((ROWS, DIM)))]
        _wait(client, _ok(client.post("/collections/check/documents",
                                      json={"documents": docs}), 202)["job_id"])
        _ok(client.get("/collections/check/documents/d7"))
        _ok(client.get("/collections/check/documents",
                       params={"filter": '{"group": "b"}', "limit": 5}))
        queries = [
            {"query": {"text": "solar wind"}, "mode": "vector"},
            {"query": {"text": "solar wind"}, "mode": "text"},
            {"query": {"text": "deep sea"}, "mode": "hybrid"},
            {"query": {"text": "deep sea"}, "mode": "hybrid", "filter": {"group": "b"}},
            {"query": {"vector": docs[5]["chunks"][0]["vector"]}, "filter": {"n": {"gte": 3}}},
        ]

        def search_all():
            for q in queries:
                if not _ok(client.post("/collections/check/search", json={**q, "k": 5}))["hits"]:
                    raise CheckFailed(f"no hits for {q}")

        search_all()
        _wait(client, _ok(client.post("/collections/check/index",
                                      json={"nlist": 8, "nprobe": 4}), 202)["job_id"])
        if _ok(client.get("/collections/check"))["index"]["type"] != "ivf":
            raise CheckFailed("the IVF index did not attach")
        search_all()  # now through the IVF shard fan-out (IVF_SEARCH_THREADS)
        _wait(client, _ok(client.delete("/collections/check/index"), 202)["job_id"])
        if _ok(client.get("/collections/check"))["index"]["type"] != "flat":
            raise CheckFailed("the IVF index did not detach")
        _ok(client.delete("/collections/check"))
        health = _ok(client.get("/healthz"))
    if health["gil_enabled"] is not gil_enabled():
        raise CheckFailed(f"/healthz says gil_enabled={health['gil_enabled']}")
    if require_native and health["bm25"] != "native":
        raise CheckFailed(f"/healthz says bm25={health['bm25']!r}, the image needs 'native'")
    return health


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--python", required=True, help="the PYTHON build arg: 3.12, 3.14t, ...")
    parser.add_argument("--require-native", action="store_true",
                        help="fail unless the stage-2 BM25 scorer is raggio_native")
    args = parser.parse_args(argv)
    # ignore_cleanup_errors: on Windows the catalog.db of the app uvicorn.Config loaded is
    # still open (no lifespan ran for it)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
        os.environ.update(ROOT_API_KEY=ROOT_KEY, DATA_DIR=data_dir)
        try:
            check_flavour(args.python)
            names = [n for site in site_dirs() for n in extension_modules(site)]
            import_all(names)
            if args.require_native:
                import_all(["raggio_native"])
                check_native()
            health = exercise(args.require_native)
            if free_threaded() and gil_enabled():
                raise CheckFailed("the GIL was enabled while serving")
        except (CheckFailed, ImportError, RuntimeWarning) as e:
            print(f"check_image: FAILED: {e}", file=sys.stderr)
            return 1
    print(f"check_image: Python {platform.python_version()}"
          f" {'free-threaded' if free_threaded() else 'with the GIL'},"
          f" GIL {'enabled' if gil_enabled() else 'disabled'},"
          f" {len(names)} extension modules imported, bm25={health['bm25']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
