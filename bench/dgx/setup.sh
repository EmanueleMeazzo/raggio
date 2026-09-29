#!/usr/bin/env bash
# DGX side of a deploy (deploy.sh runs it): corpus symlink, cached ground truth, host
# venv for bench.py, and the image under test tagged with the deployed SHA.
# Usage: bash ~/raggio/bench/dgx/setup.sh <sha>
source "$(dirname "$0")/lib.sh"
SHA="${1:?usage: setup.sh <git sha>}"

cd "$RAGGIO_DIR"
echo "$SHA" > .deployed-sha
run ln -sfn "$HOME/turborag/bench/corpus" bench/corpus
run cp "$HOME/turborag/bench/gt-$LIMIT-42-d1024.npz" bench/
run "$HOME/.local/bin/uv" --version
run "$HOME/.local/bin/uv" sync --frozen --group bench
run podman build -t "localhost/raggio:$SHA" .
mkdir -p "$STATE"
log "deployed $SHA as localhost/raggio:$SHA"
