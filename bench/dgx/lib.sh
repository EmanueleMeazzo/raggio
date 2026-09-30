# Shared settings for the DGX runbook (spec §6). Sourced by setup.sh and session.sh.
# shellcheck shell=bash
set -euo pipefail

LIMIT=2549619         # corpus size; the volume holds 2,549,119 chunks (LIMIT minus 500 held-out queries)
CONTAINER=bench-tv    # the only container this runbook ever removes; bench-wv is never touched
VOLUME=bench-tv       # reused across sessions and images; never removed
PORT=18000
MEMORY=4g             # every measured run
MEMORY_SWAP="${MEMORY_SWAP:-}"           # empty: podman's default swap; 4g = the swapless memory gate (spec §6, D13)
FIRST_START="${FIRST_START:-host-warm}"  # host-warm, or true-cold after a reboot or an fadvise eviction (D12)
DETACH_MEMORY="${DETACH_MEMORY:-$MEMORY}"  # IVF -> flat detach; 8g only for an image from before plan C (ADR 0001, C7)
FLAT_RUNS="${FLAT_RUNS:-2}"  # measured flat runs after run0; 3 when a concurrent-QPS row is claimed (spec §6)
CONCURRENCY="${CONCURRENCY:-8}"  # bench.py --concurrency of every run; G's c=16 sessions set 16 (spec §3.1 G7)
IVF_BUILDS="${IVF_BUILDS:-1}"  # IVF builds of a flat-start session; 2 = one more after the measured runs (spec §3.1 A1)
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
