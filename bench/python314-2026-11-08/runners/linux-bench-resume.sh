#!/usr/bin/env bash
set -euo pipefail
B="$HOME/raggio-bench/python314-reevaluation-20261107"
trap 'podman stop -t 30 py314-eval-server >/dev/null 2>&1 || true; podman rm py314-eval-server >/dev/null 2>&1 || true' EXIT
for index in flat ivf; do
  for round in 1 2 3; do
    case "$round" in 1) order='312 314 314t';; 2) order='314 314t 312';; 3) order='314t 312 314';; esac
    for arm in $order; do
      echo "BENCH $index round=$round arm=$arm $(date -u +%FT%TZ)"
      podman run -d --name py314-eval-server --memory 4g --memory-swap 4g -p 28140:8000 -v "$B/data-current-$index:/data" -e ROOT_API_KEY=bench "localhost/py314-eval-runtime:$arm" >/dev/null
      for n in $(seq 1 120); do if curl -sf http://127.0.0.1:28140/healthz >/dev/null; then break; fi; sleep 1; done
      curl -sf --max-time 600 -H 'x-api-key: bench' http://127.0.0.1:28140/collections/bench > "$B/results/start-$index-$round-$arm.json"
      pid=$(podman inspect --format '{{.State.Pid}}' py314-eval-server)
      cgroup=$(awk -F: '$1==0 {print $3}' "/proc/$pid/cgroup")
      podman run --rm --network host --name py314-eval-client -v "$B:/eval" -v "$B/results:/results" -v /sys/fs/cgroup:/cgroup:ro localhost/py314-eval-tests:312 /app/.venv/bin/python /eval/arxiv-bench.py --cgroup "/cgroup$cgroup" --arm "$arm" --round "$round" --index "$index" --out "/results/arxiv-$index-$round-$arm.json" > "$B/results/arxiv-$index-$round-$arm.log" 2>&1
      podman logs py314-eval-server > "$B/results/server-$index-$round-$arm.log" 2>&1
      if grep -E 'Traceback|global interpreter lock.*enabled' "$B/results/server-$index-$round-$arm.log"; then exit 1; fi
      podman stop -t 30 py314-eval-server >/dev/null
      podman rm py314-eval-server >/dev/null
      tail -2 "$B/results/arxiv-$index-$round-$arm.log"
    done
  done
done
date -u +%FT%TZ > "$B/bench.done"
echo "BENCH DONE $(date -u +%FT%TZ)"
