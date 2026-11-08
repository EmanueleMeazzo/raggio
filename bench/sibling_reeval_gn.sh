#!/usr/bin/env bash
# All resources belong to this isolated evaluation; existing services/data are untouched.
set -euo pipefail
TASK="${SIBLING_EVAL_HOME:?set SIBLING_EVAL_HOME}"
SHA="${SIBLING_EVAL_SHA:?set SIBLING_EVAL_SHA}"
case "$TASK" in "$HOME"/raggio-bench/sibling-reuse-reevaluation-*) ;; *) echo "unexpected evaluation directory" >&2; exit 1;; esac
IMAGE="localhost/raggio-sibling-reeval:${SHA:0:7}"
PREFIX="raggio-sibling-${SHA:0:7}"
SERVER=/project/bench/sibling_reeval_server.py
CLIENT=/project/bench/sibling_reeval_client.py
mkdir -p "$TASK/results" "$TASK/data"

case "${1:?setup|start|smoke|measure|stop|profile}" in
  setup)
    podman build --ignorefile "$TASK/source/bench/sibling_reeval.containerignore" -f "$TASK/source/bench/SiblingReevaluation.Dockerfile" -t "$IMAGE" "$TASK/source" > "$TASK/results/build.log" 2>&1
    podman image inspect "$IMAGE" > "$TASK/results/image.json"
    podman run --rm --network none "$IMAGE" -c 'from pathlib import Path; assert Path("/project/bench/sibling_reeval_server.py").is_file(); assert Path("/project/tests/test_sibling_vectors.py").is_file()'
    podman run --rm --network none --cpus 3 --memory 4g -v "$TASK/data:/data" "$IMAGE" "$SERVER" prepare --source synthetic --docs 500 --dim 1024 --output /data/synthetic > "$TASK/results/prepare-synthetic.log" 2>&1
    podman run --rm --network none --cpus 3 --memory 4g -v "$TASK/data:/data" -v "$HOME/raggio-bench/wiki/bench/corpus-wiki:/corpus:ro" "$IMAGE" "$SERVER" prepare --source wiki --docs 5000 --corpus /corpus --output /data/wiki > "$TASK/results/prepare-wiki.log" 2>&1
    ;;
  start)
    dataset="${2:?synthetic|wiki}"
    for arm in a b c; do
      name="$PREFIX-$arm"
      if podman container exists "$name"; then echo "container already exists: $name" >&2; exit 1; fi
      target="$TASK/data/$dataset-$arm"
      if [[ -e "$target" ]]; then echo "dataset copy already exists: $target" >&2; exit 1; fi
      cp --reflink=auto -a "$TASK/data/$dataset" "$target"
      variant=baseline; port=28312
      [[ "$arm" != b ]] || port=28313
      if [[ "$arm" == c ]]; then variant=candidate; port=28314; fi
      podman run -d --name "$name" --label "raggio.sibling_evaluation=$SHA" --cpus 3 --memory 4g -p "127.0.0.1:$port:8000" -v "$target:/data" -v "$TASK/results:/results" "$IMAGE" "$SERVER" serve --arm "$variant" --data-dir /data --output "/results/$dataset-$arm-facts.json"
    done
    python3 - "$TASK/results/$dataset-servers.json" "$PREFIX" <<'PY'
import json,subprocess,sys
prefix=sys.argv[2]
servers={arm:{'url':f'http://127.0.0.1:{28312+i}', 'pid':int(subprocess.check_output(['podman','inspect',prefix+'-'+arm,'--format','{{.State.Pid}}']))} for i,arm in enumerate(('a','b','c'))}
open(sys.argv[1],'w').write(json.dumps(servers))
PY
    ;;
  smoke)
    dataset="${2:?synthetic|wiki}"
    podman run --rm --network host --pid host -v "$TASK/data:/datasets:ro" -v "$TASK/results:/results" "$IMAGE" "$CLIENT" --servers "/results/$dataset-servers.json" --queries "/datasets/$dataset-queries.json" --output "/results/$dataset-smoke.json" --mode ab --pairs 1 --seconds .1 > "$TASK/results/$dataset-smoke.log" 2>&1
    ;;
  measure)
    dataset="${2:?synthetic|wiki}"
    podman run --rm --network host --pid host -v "$TASK/data:/datasets:ro" -v "$TASK/results:/results" "$IMAGE" "$CLIENT" --servers "/results/$dataset-servers.json" --queries "/datasets/$dataset-queries.json" --output "/results/$dataset-aa.json" --mode aa --cases doc-vector-k10-ranked,ranked-summary-control,sofia-summary > "$TASK/results/$dataset-aa.log" 2>&1
    podman run --rm --network host --pid host -v "$TASK/data:/datasets:ro" -v "$TASK/results:/results" "$IMAGE" "$CLIENT" --servers "/results/$dataset-servers.json" --queries "/datasets/$dataset-queries.json" --output "/results/$dataset-ab.json" --mode ab > "$TASK/results/$dataset-ab.log" 2>&1
    ;;
  stop)
    for arm in a b c; do
      name="$PREFIX-$arm"
      if podman container exists "$name"; then
        label=$(podman inspect "$name" --format '{{index .Config.Labels "raggio.sibling_evaluation"}}')
        [[ "$label" == "$SHA" ]] || { echo "container is not owned by this evaluation: $name" >&2; exit 1; }
        podman logs "$name" > "$TASK/results/$name-server.log" 2>&1
        podman stop -t 60 "$name"
        podman rm "$name"
      fi
    done
    ;;
  profile)
    dataset="${2:?synthetic|wiki}"
    for arm in baseline candidate; do
      target=a; [[ "$arm" != candidate ]] || target=c
      podman run --rm --network none --cpus 3 --memory 4g -v "$TASK/data/$dataset-$target:/data" -v "$TASK/data:/datasets:ro" -v "$TASK/results:/results" "$IMAGE" "$SERVER" profile --arm "$arm" --data-dir /data --queries "/datasets/$dataset-queries.json" --output "/results/$dataset-$arm-profile.json" > "$TASK/results/$dataset-$arm-profile.log" 2>&1
    done
    ;;
esac
