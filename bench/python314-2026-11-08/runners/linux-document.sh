#!/usr/bin/env bash
set -euo pipefail
B="$HOME/raggio-bench/python314-reevaluation-20261107"
while [ ! -f "$B/extra.done" ]; do sleep 15; done
echo "DOC START $(date -u +%FT%TZ)"
mkdir -p "$B/data-document"
podman run --rm -e PYTHONPATH=/project/src -v "$B/source:/project:ro" -v "$B:/eval" -v "$B/data-document:/data" localhost/py314-eval-tests:312 /app/.venv/bin/python /eval/document-bench.py --prepare
podman unshare chown -R 1000:1000 "$B/data-document"
trap 'podman stop -t 30 py314-eval-doc-server >/dev/null 2>&1 || true; podman rm py314-eval-doc-server >/dev/null 2>&1 || true' EXIT
for round in 1 2 3; do
  case "$round" in 1) order='312 314 314t';; 2) order='314 314t 312';; 3) order='314t 312 314';; esac
  for arm in $order; do
    echo "DOC $round $arm $(date -u +%FT%TZ)"
    podman run -d --name py314-eval-doc-server --memory 4g --memory-swap 4g -p 28140:8000 -v "$B/data-document:/data" -e ROOT_API_KEY=bench "localhost/py314-eval-runtime:$arm" >/dev/null
    for n in $(seq 1 120); do if curl -sf http://127.0.0.1:28140/healthz >/dev/null; then break; fi; sleep 1; done
    pid=$(podman inspect --format '{{.State.Pid}}' py314-eval-doc-server)
    group=$(awk -F: '$1==0 {print $3}' "/proc/$pid/cgroup")
    podman run --rm --network host -v "$B:/eval" -v "$B/results:/results" -v /sys/fs/cgroup:/cgroup:ro localhost/py314-eval-tests:312 /app/.venv/bin/python /eval/document-bench.py --cgroup "/cgroup$group" --arm "$arm" --round "$round" --out "/results/document-$round-$arm.json" > "$B/results/document-$round-$arm.log" 2>&1
    podman logs py314-eval-doc-server > "$B/results/doc-server-$round-$arm.log" 2>&1
    if grep -E 'Traceback|global interpreter lock.*enabled' "$B/results/doc-server-$round-$arm.log"; then exit 1; fi
    podman stop -t 30 py314-eval-doc-server >/dev/null
    podman rm py314-eval-doc-server >/dev/null
  done
done
date -u +%FT%TZ > "$B/document.done"
echo "DOC DONE $(date -u +%FT%TZ)"
