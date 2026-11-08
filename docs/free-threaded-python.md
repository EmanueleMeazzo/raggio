# Opt in to Python 3.14t

The `PYTHON` image-build argument selects the interpreter. To enable free threading:

```bash
podman build --build-arg PYTHON=3.14t -t raggio:py314t .
podman run -p 8000:8000 -v raggio-data:/data \
  -e ROOT_API_KEY=change-me raggio:py314t
```

Use `--build-arg PYTHON=3.14.10t` for the exact patch release tested in the
[2026-11-08 evaluation](python-3.14-reevaluation-2026-11-07.md). The default build
uses Python 3.12. When changing images, stop the previous process before starting
the alternative against the same data volume: one process owns each `DATA_DIR`.

## What improves, and what can cost more

At eight concurrent clients on the evaluated DGX Spark, document vector/hybrid
throughput improved **47–87%** over Python 3.12; large-corpus hybrid throughput
improved about **6–7%**, with better tails. The free-threaded build passed 818
applicable Linux tests and the extended concurrency soak.

Single-client expanded document search was **18% slower**. Higher concurrency did
not improve every route, and CPU-saturated blocking hand-offs were much slower:
an event ping-pong probe measured about **111 ms** versus **2 ms** on the GIL builds.
Keep CPU headroom, particularly when other CPU jobs share the host. These are
measurements on one machine and workload set; check your own request mix and
concurrency. The [full report](python-3.14-reevaluation-2026-11-07.md) includes the
repeat ranges, source-build control, memory accounting and limitations.

## Verify the running image

```bash
curl http://localhost:8000/healthz
```

Once startup has completed, the response includes:

```json
{
  "status": "ok",
  "resident_collections": [],
  "bm25": "native",
  "gil_enabled": false
}
```

`gil_enabled` is read on every request. The build checks the interpreter, compiled
extensions and request path. The runtime treats CPython's warning about an extension
re-enabling the GIL as an error. Avoid overriding `PYTHONWARNINGS`, and do not force
`PYTHON_GIL=0` or `-X gil=0`: those overrides can hide unsupported extensions and the
build check refuses them.

Free threading requires a different interpreter and compatible native extensions;
an environment variable cannot turn a standard Python 3.12 image into a free-threaded
one. The build flag installs the right interpreter and rebuilds the native scorer
for its ABI. turbovec currently compiles from its source distribution because it
has no cp314t wheel. The image's Rust builder handles that; no toolchain is needed
in the running container.

The opt-in remains experimental under [ADR 0006](adr/0006-free-threaded-python.md).
Its historical Hold reflected the missing upstream wheel despite measured gains;
the maintainer has authorized this explicit opt-in after the new evaluation. The
default image and the independent `GC_FREEZE` setting retain their existing defaults.
