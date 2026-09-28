# Shared settings for the DGX runbook (spec §6). Sourced by setup.sh and session.sh.
# shellcheck shell=bash
set -euo pipefail

LIMIT=2549619         # corpus size; the volume holds 2,549,119 chunks (LIMIT minus 500 held-out queries)
CONTAINER=bench-tv    # the only container this runbook ever removes; bench-wv is never touched
VOLUME=bench-tv       # reused across sessions and images; never removed
PORT=18000
MEMORY=4g             # every measured run
DETACH_MEMORY=8g      # IVF -> flat detach: refused at 4 GiB until plan C releases build memory
FLAT_RUNS=2           # measured runs after the warm-up run0: flat band = run1 vs run2
IVF_RUNS=3            # IVF: median of >= 3 runs, band = max - min
API_KEY=bench
RAGGIO_DIR="${RAGGIO_DIR:-$HOME/raggio}"          # deploy.sh wipes and re-creates this
BENCH_HOME="${BENCH_HOME:-$HOME/raggio-bench}"   # survives deploys: fingerprints + session outputs
STATE="$BENCH_HOME/state"
PENDING_WAIT_S=3600
CAPS="raggio container capped at 4 GiB (4-bit quantized flat index; IVF column adds the optional index)."
HOST_NOTE="NVIDIA DGX Spark - GB10 Grace, 20 aarch64 cores (10x Cortex-X925 + 10x Cortex-A725), 122 GB unified LPDDR5x, native Linux, rootless podman"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

# print a command, then run it unless DRY_RUN=1
run() {
  printf '+ %s\n' "$*"
  if [ "${DRY_RUN:-0}" != 1 ]; then "$@"; fi
}
