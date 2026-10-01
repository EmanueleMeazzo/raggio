# One parameterized image (spec D2): uv installs the interpreter named by PYTHON into
# /python; the runtime stage is plain debian:trixie-slim with /python and /app copied in.
# UV_VERSION is the same pin as .github/workflows/tests.yml.
# The builder stage is the Rust image: it compiles raggio-native (native/, ADR 0004) for
# the interpreter in /python, and needs network access to PyPI (maturin) and crates.io
# (native/Cargo.lock, built --locked). The wheel is not manylinux-audited, so the builder
# runs trixie like the runtime; the runtime stage copies no toolchain. RUST_VERSION is the
# same pin as the native job in .github/workflows/tests.yml.
ARG UV_VERSION=0.12.21
ARG PYTHON=3.12
ARG RUST_VERSION=1.98.1

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM docker.io/library/rust:${RUST_VERSION}-slim-trixie AS builder
COPY --from=uv /uv /uvx /bin/
ARG PYTHON
ENV UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON=${PYTHON} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    CARGO_TARGET_DIR=/tmp/cargo-target
RUN uv python install ${PYTHON}
# rust:*-slim has no python3: link /python's interpreter as python3 and name it in
# PYO3_PYTHON, so PyO3 and native/build.rs (gen_unicode.py) build for the interpreter the
# runtime stage copies. podman's OCI format ignores SHELL, so pipefail is set in the RUN.
RUN bash -c 'set -euo pipefail; py="$(uv python find "${PYTHON}")"; ln -s "$py" /usr/local/bin/python3; python3 -c "import sys; print(sys.prefix)" | grep -q "^/python/"'
ENV PYO3_PYTHON=/usr/local/bin/python3
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY native ./native
RUN uv sync --frozen --no-install-project --no-dev --extra native
COPY src ./src
RUN uv sync --frozen --no-dev --extra native

FROM docker.io/library/debian:trixie-slim
COPY --from=builder /python /python
COPY --from=builder /app /app
WORKDIR /app
RUN useradd -m --uid 1000 app && mkdir /data && chown app /data
USER app
# one OpenBLAS thread (spec D16): numpy's scipy-openblas otherwise starts one busy-spinning thread per core under search load
ENV OPENBLAS_NUM_THREADS=1
ENV LANG=C.UTF-8 DATA_DIR=/data PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000

# D13: an explicit trim threshold makes glibc return heap above 128 MiB to the OS at
# free() time and stops its dynamic mmap-threshold growth, so the multi-GB transient
# buffers of an index job are unmapped when freed. raggio also calls malloc_trim(0)
# after every index job. MALLOC_ARENA_MAX stays unset (spec D13).
ENV MALLOC_TRIM_THRESHOLD_=134217728

# no per-request access log: it costs a stdout write through the container log
# pipe on every query; set --access-log if you need request tracing
CMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
