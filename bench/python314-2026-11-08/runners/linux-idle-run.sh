#!/usr/bin/env bash
set -euo pipefail
B="$HOME/raggio-bench/python314-reevaluation-20261107"
cd "$B"
echo $$ > idle-run.pid
ps -o pgid= -p $$ | tr -d ' ' > idle-run.pgid
cleanup() {
  podman stop -t 15 --ignore py314-eval-server py314-eval-client py314-eval-doc-server py314-eval-control py314-eval-warmup >/dev/null 2>&1 || true
  podman rm --ignore py314-eval-server py314-eval-client py314-eval-doc-server py314-eval-control py314-eval-warmup >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'echo "IDLE RUN FAILED rc=$? line=$LINENO command=$BASH_COMMAND" >&2' ERR
facts() {
  date -u +%FT%TZ
  cat /proc/sys/kernel/random/boot_id
  uptime
  free -m
  podman ps --format '{{.Names}} {{.Image}} {{.Status}}'
  ps -eo pid,comm,pcpu --sort=-pcpu | head -20 || true
}
facts > results/idle-start-facts.txt
echo "IDLE RUN START $(date -u +%FT%TZ)"
echo "STAGE full-tests"
timeout --kill-after=30 30m bash linux-tests.sh
echo "STAGE post-reboot-warmup"
for index in flat ivf; do
  podman run -d --name py314-eval-server --memory 4g --memory-swap 4g -p 28140:8000 -v "$B/data-current-$index:/data" -e ROOT_API_KEY=bench localhost/py314-eval-runtime:312 >/dev/null
  for n in $(seq 1 120); do if curl -sf http://127.0.0.1:28140/healthz >/dev/null; then break; fi; sleep 1; done
  curl -sf --max-time 600 -H 'x-api-key: bench' http://127.0.0.1:28140/collections/bench > "results/boot-load-$index.json"
  pid=$(podman inspect --format '{{.State.Pid}}' py314-eval-server)
  group=$(awk -F: '$1==0 {print $3}' "/proc/$pid/cgroup")
  podman run --rm --network host --name py314-eval-warmup -v "$B:/eval" -v "$B/results:/results" -v /sys/fs/cgroup:/cgroup:ro localhost/py314-eval-tests:312 /app/.venv/bin/python /eval/arxiv-bench.py --prewarm --cgroup "/cgroup$group" --arm 312 --round 0 --index "$index" --out "/results/boot-warmup-$index.json" > "results/boot-warmup-$index.log" 2>&1
  podman stop -t 30 py314-eval-server >/dev/null
  podman rm py314-eval-server >/dev/null
done
echo "STAGE arxiv"
facts > results/arxiv-start-facts.txt
timeout --kill-after=60 90m bash linux-bench-resume.sh
echo "STAGE handoffs-and-ingest"
facts > results/extra-start-facts.txt
timeout --kill-after=60 30m bash linux-extra.sh
echo "STAGE document-retrieval"
facts > results/document-start-facts.txt
timeout --kill-after=60 30m bash linux-document.sh
echo "STAGE native-build-control"
facts > results/control-start-facts.txt
timeout --kill-after=60 40m bash linux-control.sh
facts > results/idle-end-facts.txt
date -u +%FT%TZ > idle-run.done
echo "IDLE RUN DONE $(date -u +%FT%TZ)"
