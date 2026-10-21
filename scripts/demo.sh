#!/usr/bin/env bash
# One-command demo. Usage:
#   scripts/demo.sh --offline        D1-D6 in local mode (no AWS, no network); prints a summary
#   scripts/demo.sh --aws [PARAMS]   end-to-end against the deployed stack (scripts/e2e_aws.py)
#   scripts/demo.sh --reset          local reset: regenerate samples, clear local outputs
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:---offline}" in
  --offline)
    uv run python scripts/gen_samples.py >/dev/null
    if [ ! -d cdk.out-regions/us-west-2 ] && command -v npx >/dev/null 2>&1; then
      make -s synth-regions >/dev/null 2>&1 || echo "(D6 synth skipped: npx/cdk not available)"
    fi
    uv run python scripts/demo_local.py ;;
  --aws)
    uv run python scripts/e2e_aws.py --params "${2:-config/params.json}" --report out/e2e_report.json ;;
  --reset)
    uv run python scripts/gen_samples.py
    echo "local mode keeps all state in memory: stop and restart make local to reset it" ;;
  *) echo "usage: $0 --offline | --aws [PARAMS] | --reset" >&2; exit 2 ;;
esac
