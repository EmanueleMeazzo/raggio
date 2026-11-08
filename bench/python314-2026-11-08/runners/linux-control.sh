#!/usr/bin/env bash
set -euo pipefail
B="$HOME/raggio-bench/python314-reevaluation-20261107"
while [ ! -f "$B/document.done" ]; do sleep 15; done
echo "CONTROL START $(date -u +%FT%TZ)"
cd "$B/source"
podman build --build-arg PYTHON=3.14.10 --build-arg UV_VERSION=0.13.7 --build-arg UV_NO_BINARY_PACKAGE=turbovec -t localhost/py314-eval-runtime:314s . > "$B/results/build-runtime-314s.log" 2>&1
for round in 1 2 3; do
  case "$round" in 1) order='312 314 314t 314s';; 2) order='314s 314t 314 312';; 3) order='314t 312 314s 314';; esac
  for arm in $order; do
    echo "KERNEL $round $arm $(date -u +%FT%TZ)"
    podman run --rm --user 0 --entrypoint /app/.venv/bin/python --memory 4g --memory-swap 4g -v "$B:/eval" -v "$B/data-current-flat:/data:ro" -v "$B/results:/results" "localhost/py314-eval-runtime:$arm" /eval/kernel-bench.py --out "/results/kernel-$round-$arm.json" --arm "$arm" --round "$round" > "$B/results/kernel-$round-$arm.log" 2>&1
  done
done
trap 'podman stop -t 30 py314-eval-control >/dev/null 2>&1 || true; podman rm py314-eval-control >/dev/null 2>&1 || true' EXIT
for index in flat ivf; do
  for round in 1 2 3; do
    case "$round" in 1) order='314 314s 314t';; 2) order='314s 314t 314';; 3) order='314t 314 314s';; esac
    for arm in $order; do
    echo "CONTROL HTTP $index $round $arm $(date -u +%FT%TZ)"
    podman run -d --name py314-eval-control --memory 4g --memory-swap 4g -p 28140:8000 -v "$B/data-current-$index:/data" -e ROOT_API_KEY=bench "localhost/py314-eval-runtime:$arm" >/dev/null
    for n in $(seq 1 120); do if curl -sf http://127.0.0.1:28140/healthz >/dev/null; then break; fi; sleep 1; done
    curl -sf --max-time 600 -H 'x-api-key: bench' http://127.0.0.1:28140/collections/bench >/dev/null
    pid=$(podman inspect --format '{{.State.Pid}}' py314-eval-control)
    group=$(awk -F: '$1==0 {print $3}' "/proc/$pid/cgroup")
    podman run --rm --network host -v "$B:/eval" -v "$B/results:/results" -v /sys/fs/cgroup:/cgroup:ro localhost/py314-eval-tests:312 /app/.venv/bin/python /eval/arxiv-bench.py --limit-requests 100 --cgroup "/cgroup$group" --arm "$arm" --round "$round" --index "$index" --out "/results/control-$index-$round-$arm.json" > "$B/results/control-$index-$round-$arm.log" 2>&1
    podman logs py314-eval-control > "$B/results/control-server-$index-$round-$arm.log" 2>&1
    if grep -E 'Traceback|global interpreter lock.*enabled' "$B/results/control-server-$index-$round-$arm.log"; then exit 1; fi
    podman stop -t 30 py314-eval-control >/dev/null
    podman rm py314-eval-control >/dev/null
    done
  done
done
date -u +%FT%TZ > "$B/control.done"
echo "CONTROL DONE $(date -u +%FT%TZ)"
