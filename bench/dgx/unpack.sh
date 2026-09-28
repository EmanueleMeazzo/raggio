# DGX side of deploy.sh (spec §6 "Deploy"): stdin is `git archive HEAD`. Results, logs and
# fingerprints a previous checkout left in ~/raggio/bench are copied to
# ~/raggio-bench/pre-deploy-<UTC time>/ first; then ~/raggio is replaced by the archive.
# deploy.sh sends this text as the ssh command, so it must not refer to its own path.
set -euo pipefail
if [ -d ~/raggio/bench ]; then
  keep=~/raggio-bench/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p "$keep"
  find ~/raggio/bench -maxdepth 1 -type f \
    \( -name 'results-*' -o -name '*.log' -o -name 'fingerprint-*.json' \) -exec cp -p {} "$keep"/ \;
  rmdir "$keep" 2>/dev/null || echo "kept $(ls "$keep" | wc -l) file(s) of the previous checkout in $keep"
fi
rm -rf ~/raggio && mkdir ~/raggio && tar -x -C ~/raggio
