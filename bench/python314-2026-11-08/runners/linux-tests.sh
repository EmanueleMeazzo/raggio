#!/usr/bin/env bash
set -u
B="$HOME/raggio-bench/python314-reevaluation-20261107"
for arm in 312 314 314t; do
  echo "TEST $arm $(date -u +%FT%TZ)"
  timeout --kill-after=30 20m podman run --rm -w /project -e PYTHONPATH=/project/src --name "py314-eval-tests-$arm" -v "$B/source:/project:ro" -v "$B/results:/results" "localhost/py314-eval-tests:$arm" /app/.venv/bin/python -m pytest -q -ra -o cache_dir=/tmp/pytest-cache --durations=15 --junitxml="/results/linux-$arm.xml" > "$B/results/linux-$arm.log" 2>&1
  printf '%s suite rc=%s\n' "$arm" "$?" | tee -a "$B/results/test-summary-current.txt"
  tail -20 "$B/results/linux-$arm.log"
done
echo "SOAK 314t $(date -u +%FT%TZ)"
timeout --kill-after=30 10m podman run --rm -w /project -e PYTHONPATH=/project/src --name py314-eval-soak -e RAGGIO_STRESS_SECONDS=30 -v "$B/source:/project:ro" -v "$B/results:/results" localhost/py314-eval-tests:314t /app/.venv/bin/python -m pytest -q -ra -o cache_dir=/tmp/pytest-cache tests/test_concurrency.py -k 'mixed_workload or stop_under_load' --junitxml=/results/linux-314t-soak.xml > "$B/results/linux-314t-soak.log" 2>&1
printf '314t soak rc=%s\n' "$?" | tee -a "$B/results/test-summary-current.txt"
tail -10 "$B/results/linux-314t-soak.log"
date -u +%FT%TZ > "$B/tests-current.done"
