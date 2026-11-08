#!/usr/bin/env bash
# Counterbalanced source selection in one fully drained HTTP process.
set -euo pipefail
TASK="${SIBLING_EVAL_HOME:?}"
SHA="${SIBLING_EVAL_SHA:?runtime image revision}"
DATASET="${2:?synthetic|wiki}"
SERVER_CPUS="${SIBLING_EVAL_SERVER_CPUS:-15,16,17}"
CLIENT_CPU="${SIBLING_EVAL_CLIENT_CPU:-19}"
IMAGE="localhost/raggio-sibling-reeval:${SHA:0:7}"
NAME="raggio-sibling-${SHA:0:7}-a"
case "$TASK" in "$HOME"/raggio-bench/sibling-reuse-reevaluation-*) ;; *) exit 1;; esac
client() {
  podman run --rm --network host --pid host -v "$TASK/data:/datasets:ro" -v "$TASK/results:/results" -v "$TASK/source/bench:/project/bench:ro" "$IMAGE" -c 'import os,runpy,sys; cpu=int(sys.argv.pop(1)); os.sched_setaffinity(0,{cpu}); sys.argv=["/project/bench/sibling_reeval_client.py",*sys.argv[1:]]; runpy.run_path(sys.argv[0],run_name="__main__")' "$CLIENT_CPU" --servers "/results/$DATASET-single-servers.json" --queries "/datasets/$DATASET-queries.json" "$@"
}
case "${1:?start|smoke|measure|stop|profile}" in
  start)
    ! podman container exists "$NAME"
    if [[ ! -e "$TASK/data/$DATASET-a" ]]; then cp --reflink=auto -a "$TASK/data/$DATASET" "$TASK/data/$DATASET-a"; fi
    podman run -d --name "$NAME" --label "raggio.sibling_evaluation=$SHA" --cpus 3 --memory 4g -p 127.0.0.1:28312:8000 -v "$TASK/data/$DATASET-a:/data" -v "$TASK/results:/results" -v "$TASK/source/bench:/project/bench:ro" "$IMAGE" /project/bench/sibling_reeval_server.py serve --arm baseline --data-dir /data --output "/results/$DATASET-single-facts.json"
    pid=$(podman inspect "$NAME" --format '{{.State.Pid}}')
    taskset -apc "$SERVER_CPUS" "$pid" > "$TASK/results/$DATASET-affinity-single.txt"
    python3 - "$TASK/results/$DATASET-single-servers.json" "$pid" "$SERVER_CPUS" <<'PY'
import json,sys
servers={arm:{'url':'http://127.0.0.1:28312','pid':int(sys.argv[2]),'variant':variant,'server_cpu_affinity':sys.argv[3]} for arm,variant in [('a','baseline'),('b','baseline'),('c','candidate')]}
open(sys.argv[1],'w').write(json.dumps(servers))
PY
    ;;
  smoke) client --mode ab --pairs 1 --seconds .1 --output "/results/$DATASET-single-smoke.json" > "$TASK/results/$DATASET-single-smoke.log" 2>&1;;
  measure)
    client --mode aa --cases doc-vector-k10-ranked,ranked-summary-control,sofia-summary --output "/results/$DATASET-aa-single.json" > "$TASK/results/$DATASET-aa-single.log" 2>&1
    client --mode ab --output "/results/$DATASET-ab-single.json" > "$TASK/results/$DATASET-ab-single.log" 2>&1
    ;;
  stop)
    label=$(podman inspect "$NAME" --format '{{index .Config.Labels "raggio.sibling_evaluation"}}')
    [[ "$label" == "$SHA" ]]
    podman logs "$NAME" > "$TASK/results/$DATASET-single-server.log" 2>&1
    podman stop -t 60 "$NAME"
    podman rm "$NAME"
    ;;
  profile)
    for arm in baseline candidate; do
      podman run --rm --network none --cpus 3 --memory 4g -v "$TASK/data/$DATASET-a:/data" -v "$TASK/data:/datasets:ro" -v "$TASK/results:/results" -v "$TASK/source/bench:/project/bench:ro" "$IMAGE" /project/bench/sibling_reeval_server.py profile --arm "$arm" --data-dir /data --queries "/datasets/$DATASET-queries.json" --output "/results/$DATASET-$arm-profile.json" > "$TASK/results/$DATASET-$arm-profile.log" 2>&1
    done
    ;;
esac
