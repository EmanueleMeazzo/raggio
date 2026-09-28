#!/usr/bin/env bash
# Ship the committed HEAD to the DGX and build its image (spec §6 "Deploy").
# Run on the workstation from the repo root:  bash bench/dgx/deploy.sh
# ~/raggio on the DGX is replaced (unpack.sh keeps a copy of its bench results and logs);
# bench state and session outputs live in ~/raggio-bench (lib.sh).
set -euo pipefail

SSH_CONFIG="${SSH_CONFIG:-C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config}"
DGX_HOST="${DGX_HOST:-gn100}"
ssh_dgx() { ssh -F "$SSH_CONFIG" -o BatchMode=yes "$DGX_HOST" "$@"; }

if ! git diff --quiet HEAD --; then
  echo "tracked files differ from HEAD; deploy ships HEAD only: commit or stash first" >&2
  exit 1
fi
SHA=$(git rev-parse --short=7 HEAD)
echo "deploying $SHA to $DGX_HOST"
git -c core.autocrlf=false archive HEAD | ssh_dgx "$(cat "$(dirname "$0")/unpack.sh")"
ssh_dgx "bash ~/raggio/bench/dgx/setup.sh $SHA"
