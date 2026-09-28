#!/usr/bin/env bash
# One DGX measurement session of one image (spec §6 "Order in a session"):
#   1. host facts: uptime, free -m, Cached/Buffers from /proc/meminfo, podman ps -a,
#      image id/digest, bench SHA, python/sqlite/turbovec versions
#   2. first container start: time to /healthz and to the first GET /collections/bench
#   3. wait for pending_jobs == 0, then read the index state
#   4-5. both engine groups, the one matching the index state first, so the session makes
#      one IVF <-> flat transition. Per engine a discarded warm-up run0, then the measured
#      runs (flat run1-run2, IVF run1-run3; compare.py takes the median and max - min).
#      IVF first: the detach runs in a transient 8 GiB container (refused at 4 GiB until
#      plan C). Flat first: raggio-ivf run0 builds the index at 4 GiB and measures the build.
#   Every run starts from a fresh 4 GiB bench-tv container and waits for pending_jobs == 0.
# Outputs go to ~/raggio-bench/<label>/: facts.txt and <run>-<engine>.md/.json. Launch
# detached, capped at the agreed run window (timeout stops the harness and bench.py, not
# the container), and poll the log:
#   mkdir -p ~/raggio-bench && setsid -f timeout --kill-after=60 <cap, e.g. 60m> \
#     bash ~/raggio/bench/dgx/session.sh <label> > ~/raggio-bench/<label>.log 2>&1 < /dev/null
# To stop it early, signal its process group (no name pattern that could match the caller):
#   kill -TERM -- -"$(cat ~/raggio-bench/<label>/session.pgid)"
# IMAGE=<ref>: measure another image (default localhost/raggio:<deployed sha>).
# ADOPT=1: pass --adopt to the first run (first session on a volume with no fingerprint yet).
# DRY_RUN=1: print the commands without running them; DRY_INDEX=ivf|flat is the index
# state a dry run assumes (default ivf).
source "$(dirname "$0")/lib.sh"
LABEL="${1:?usage: session.sh <label>}"

cd "$RAGGIO_DIR"
SHA=$(cat .deployed-sha)
IMAGE="${IMAGE:-localhost/raggio:$SHA}"
if [ "${DRY_RUN:-0}" != 1 ] && ! podman image exists "$IMAGE"; then
  echo "image $IMAGE not found: deploy first, or set IMAGE" >&2
  exit 1
fi
OUT="$BENCH_HOME/$LABEL"
if [ -e "$OUT" ]; then
  echo "$OUT exists: pick a new label" >&2
  exit 1
fi
mkdir -p "$OUT" "$STATE"
if [ "${DRY_RUN:-0}" != 1 ]; then
  ps -o pgid= -p $$ | tr -d ' ' > "$OUT/session.pgid"
fi
URL="http://localhost:$PORT/collections/bench"
VERSIONS='import sys, sqlite3, importlib.metadata as m
print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version, "turbovec", m.version("turbovec"))'
FIELD='import json, sys
info = json.load(sys.stdin)
print(info["pending_jobs"] if sys.argv[1] == "pending" else (info.get("index") or {}).get("type"))'
EXTRA=()
if [ "${ADOPT:-0}" = 1 ]; then EXTRA=(--adopt); fi

ms_since() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }

# GET /collections/bench; the first one after a start loads the collection (minutes when cold)
info() { curl -sf --max-time 1800 -H "x-api-key: $API_KEY" "$URL"; }
field() { info | .venv/bin/python -c "$FIELD" "$1"; }

index_type() {  # prints ivf or flat
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "${DRY_INDEX:-ivf}"; return; fi
  field index
}

wait_started() {  # $1 = label, $2 = container start time in ns
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ wait for /healthz and the first GET /collections/bench"; return; fi
  local i
  for i in $(seq 1 1200); do
    if curl -sf -o /dev/null --max-time 2 "http://localhost:$PORT/healthz"; then break; fi
    if [ "$i" = 1200 ]; then log "$1: no /healthz after 600 s" >&2; exit 1; fi
    sleep 0.5
  done
  log "$1: /healthz after $(ms_since "$2") ms" | tee -a "$OUT/facts.txt"
  info > /dev/null
  log "$1: first GET /collections/bench answered after $(ms_since "$2") ms" | tee -a "$OUT/facts.txt"
}

wait_pending() {
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ wait for pending_jobs == 0"; return; fi
  local start=$SECONDS n
  while :; do
    n=$(field pending) || n=unknown
    if [ "$n" = 0 ]; then
      log "pending_jobs == 0 after $((SECONDS - start)) s" | tee -a "$OUT/facts.txt"
      return
    fi
    if [ $((SECONDS - start)) -ge "$PENDING_WAIT_S" ]; then
      log "pending_jobs=$n after ${PENDING_WAIT_S} s: giving up" >&2
      exit 1
    fi
    log "pending_jobs=$n, waiting"
    sleep 15
  done
}

fresh_container() {  # $1 = label for facts.txt, $2 = memory cap
  local t0
  run podman stop -t 60 --ignore "$CONTAINER"  # clean shutdown of the previous run's server
  run podman rm -f --ignore "$CONTAINER"
  t0=$(date +%s%N)
  run podman run -d --name "$CONTAINER" --memory "$2" -p "$PORT:8000" -v "$VOLUME:/data" \
    -e "ROOT_API_KEY=$API_KEY" "$IMAGE"
  wait_started "$1" "$t0"
  wait_pending
}

bench_run() {  # bench_run <run0|run1|...> <engine>
  local tag=$1 engine=$2
  fresh_container "$tag $engine" "$MEMORY"
  log "$tag $engine"
  run rm -f bench/results-partial.json
  run .venv/bin/python -u bench/bench.py --limit "$LIMIT" --caps-note "$CAPS" --host "$HOST_NOTE" \
    --engine "$engine" --fingerprint-dir "$STATE" --out "$OUT/$tag-$engine.md" "${EXTRA[@]}"
  EXTRA=()
  run mv bench/results-partial.json "$OUT/$tag-$engine.json"
}

engine_group() {  # engine_group <engine> <measured runs>: warm-up run0, then run1..runN
  local i
  for i in $(seq 0 "$2"); do bench_run "run$i" "$1"; done
}

detach_index() {  # the session's one IVF -> flat transition
  local start
  fresh_container "detach at $DETACH_MEMORY" "$DETACH_MEMORY"
  start=$SECONDS
  run curl -sf --max-time 900 -X DELETE -H "x-api-key: $API_KEY" "$URL/index"
  echo
  wait_pending
  if [ "${DRY_RUN:-0}" != 1 ] && [ "$(index_type)" != flat ]; then
    log "the detach did not leave a flat index: stopping" >&2
    exit 1
  fi
  log "IVF detach at $DETACH_MEMORY took $((SECONDS - start)) s" | tee -a "$OUT/facts.txt"
}

log "session $LABEL: image $IMAGE, bench checkout $SHA" | tee "$OUT/facts.txt"
{
  run uptime
  run free -m
  run grep -E '^(Cached|Buffers):' /proc/meminfo
  run podman ps -a
  run podman image inspect --format '{{.Id}} {{.Digest}}' "$IMAGE"
  run podman run --rm "$IMAGE" python -c "$VERSIONS"
} >> "$OUT/facts.txt" 2>&1 || true  # facts are informational: a missing one never stops the session

fresh_container "first start" "$MEMORY"
START_INDEX=$(index_type)
log "index at start: $START_INDEX" | tee -a "$OUT/facts.txt"
case "$START_INDEX" in
  ivf)
    engine_group raggio-ivf "$IVF_RUNS"
    detach_index
    engine_group raggio "$FLAT_RUNS"
    ;;
  flat)
    engine_group raggio "$FLAT_RUNS"
    engine_group raggio-ivf "$IVF_RUNS"  # run0 builds the index: index_build_s at 4 GiB
    ;;
  *)
    log "unexpected index state '$START_INDEX': stopping" >&2
    exit 1
    ;;
esac
run cp bench/bench.py "$OUT/"
log "session $LABEL done"
