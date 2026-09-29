# One parameterized image (spec D2): uv installs the interpreter named by PYTHON into
# /python; the runtime stage is plain debian:trixie-slim with /python and /app copied in.
# UV_VERSION is the same pin as .github/workflows/tests.yml.
ARG UV_VERSION=0.12.20
ARG PYTHON=3.12

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM docker.io/library/debian:trixie-slim AS builder
COPY --from=uv /uv /uvx /bin/
ARG PYTHON
ENV UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON=${PYTHON} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
RUN uv python install ${PYTHON}
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev

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

# no per-request access log: it costs a stdout write through the container log
# pipe on every query; set --access-log if you need request tracing
CMD ["uvicorn", "raggio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
