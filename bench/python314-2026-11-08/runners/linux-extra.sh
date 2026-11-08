#!/usr/bin/env bash
set -euo pipefail
B="$HOME/raggio-bench/python314-reevaluation-20261107"
while [ ! -f "$B/bench.done" ]; do sleep 15; done
echo "EXTRA START $(date -u +%FT%TZ)"
for arm in 312 314 314t; do
  echo "HANDOFF $arm $(date -u +%FT%TZ)"
  podman run --rm -v "$B:/eval" -v "$B/results:/results" "localhost/py314-eval-tests:$arm" /app/.venv/bin/python /eval/handoff-bench.py --cpus 5,6,7,8 --out "/results/handoff-linux-$arm.json" > "$B/results/handoff-linux-$arm.log" 2>&1
  podman run --rm -w /project -e PYTHONPATH=/project/src -v "$B/source:/project:ro" -v "$B:/eval" -v "$B/results:/results" "localhost/py314-eval-tests:$arm" /app/.venv/bin/python /eval/handoff-bench.py --cpus 5,6,7,8 --pytest --out "/results/ingest-loaded-linux-$arm.json" > "$B/results/ingest-loaded-linux-$arm.log" 2>&1
done
for index in flat ivf; do
  for round in 1 2 3; do
    case "$round" in 1) order='312 314 314t';; 2) order='314 314t 312';; 3) order='314t 312 314';; esac
    for arm in $order; do
      echo "INGEST $index $round $arm $(date -u +%FT%TZ)"
      extra=()
      if [ "$index" = ivf ]; then extra=(--ivf 32); fi
      podman run --rm -e OPENBLAS_NUM_THREADS=1 -v "$B/source:/project:ro" -v "$B/results:/results" "localhost/py314-eval-tests:$arm" /app/.venv/bin/python /project/bench/ingest_probe.py --dim 512 --prefill 10000 --jobs 40 --job-rows 50 --reupsert-every 5 "${extra[@]}" --out "/results/ingest-$index-$round-$arm.json" > "$B/results/ingest-$index-$round-$arm.log" 2>&1
    done
  done
done
date -u +%FT%TZ > "$B/extra.done"
echo "EXTRA DONE $(date -u +%FT%TZ)"
