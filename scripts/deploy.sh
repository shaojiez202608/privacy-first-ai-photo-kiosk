#!/usr/bin/env bash
# Guarded deploy: preflight -> bundle -> cdk diff (refuses deletions/replacements unless
# ALLOW_DESTRUCTIVE=1) -> cdk deploy. Output is masked (12-digit numbers).
# Usage: scripts/deploy.sh [params-file]
set -euo pipefail
cd "$(dirname "$0")/.."
PARAMS="${1:-config/params.json}"
mask() { sed -E 's/[0-9]{12}/111122223333/g'; }
CDK=(npx -y aws-cdk@2)
./scripts/preflight.sh "$PARAMS"
./scripts/build_bundle.sh
STACK=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["deploy"]["stack_name"])' "$PARAMS")
REGION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["deploy"]["region"])' "$PARAMS")
echo "== cdk diff"
DIFF=$("${CDK[@]}" diff -c "params=$PARAMS" 2>&1 || true)
echo "$DIFF" | mask | grep -vE '^\s*$' | tail -60
if echo "$DIFF" | grep -qE '^\[-\]|requires replacement|may be replaced|\(replace\)'; then
  if [ "${ALLOW_DESTRUCTIVE:-}" != "1" ]; then
    echo "The change set deletes or replaces resources. Review the diff above and re-run with ALLOW_DESTRUCTIVE=1." >&2
    exit 2
  fi
  echo "ALLOW_DESTRUCTIVE=1: continuing with deletions/replacements."
fi
mkdir -p out
echo "== cdk deploy ($STACK in $REGION)"
"${CDK[@]}" deploy -c "params=$PARAMS" --require-approval never --outputs-file "out/${STACK}.outputs.json" 2>&1 | mask
# Keep the asset manifest of what was uploaded: later synths (diff, destroy) overwrite cdk.out, and
# cleanup must delete exactly the bootstrap objects of this project.
mkdir -p .deploy && cp "cdk.out/${STACK}.assets.json" ".deploy/${STACK}.$(date -u +%Y%m%dT%H%M%SZ).assets.json"
