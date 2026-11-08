FROM localhost/py314-eval-tests:312
USER 0
WORKDIR /project
COPY . /project
ENV UV_PROJECT_ENVIRONMENT=/app/.venv OPENBLAS_NUM_THREADS=1
RUN uv sync --frozen --group bench --extra native --python /app/.venv/bin/python
ENTRYPOINT ["/app/.venv/bin/python"]
