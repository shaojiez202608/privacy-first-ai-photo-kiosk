#!/usr/bin/env bash
# Destroy the stack and remove what CloudFormation leaves behind. Asks twice: once for the stack,
# once more before deleting any data (retained buckets, tables, log groups) or bootstrap asset
# objects. A failing step prints a warning and the script continues; run verify-cleanup afterwards.
# Non-interactive: CONFIRM_STACK=<stack name> and CONFIRM_DATA=yes.
# Usage: scripts/cleanup.sh [params-file]
set -uo pipefail
cd "$(dirname "$0")/.."
PARAMS="${1:-config/params.json}"
mask() { sed -E 's/[0-9]{12}/111122223333/g'; }
warn() { echo "WARNING: $* (continuing)" >&2; }
read -r STACK PREFIX REGION REMOVAL < <(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))["deploy"]
print(d["stack_name"], d["resource_prefix"], d["region"], d["removal_policy"])' "$PARAMS")
ask() {  # ask <prompt> <expected> <env value>
  [ "${3:-}" = "$2" ] && return 0
  [ -t 0 ] || { echo "Not confirmed (set the matching CONFIRM_* variable)."; return 1; }
  read -r -p "$1 " a; [ "$a" = "$2" ]
}
ask "Type the stack name ($STACK) to destroy it in $REGION:" "$STACK" "${CONFIRM_STACK:-}" || exit 1

# Save the asset manifest before destroy re-synthesises cdk.out.
[ -f "cdk.out/${STACK}.assets.json" ] && mkdir -p .deploy && cp "cdk.out/${STACK}.assets.json" ".deploy/${STACK}.predestroy.assets.json"

echo "== cdk destroy"
npx -y aws-cdk@2 destroy -c "params=$PARAMS" --force 2>&1 | mask || warn "cdk destroy failed"

echo "== log groups created by services (CDK custom-resource handlers, Lambda)"
for p in "/aws/lambda/${STACK}" "/aws/lambda/${PREFIX}-"; do
  for g in $(aws logs describe-log-groups --region "$REGION" --log-group-name-prefix "$p" --query 'logGroups[].logGroupName' --output text); do
    aws logs delete-log-group --region "$REGION" --log-group-name "$g" && echo "deleted $g" || warn "could not delete $g"
  done
done

echo "== secrets scheduled for deletion (the name stays reserved during the recovery window)"
for s in $(aws secretsmanager list-secrets --region "$REGION" --include-planned-deletion \
            --filters Key=name,Values="${PREFIX}/" --query 'SecretList[].ARN' --output text); do
  aws secretsmanager delete-secret --region "$REGION" --secret-id "$s" --force-delete-without-recovery >/dev/null \
    && echo "deleted secret $(echo "$s" | mask)" || warn "could not delete a secret"
done

LOWER=$(echo "$STACK" | tr 'A-Z' 'a-z')
BUCKETS=$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '$LOWER')].Name" --output text)
TABLES=$(aws dynamodb list-tables --region "$REGION" --query "TableNames[?starts_with(@, '$STACK')]" --output text)
GROUPS=$(aws logs describe-log-groups --region "$REGION" --log-group-name-prefix "/${PREFIX}/" --query 'logGroups[].logGroupName' --output text)
KEYS=$(uv run python -c 'import sys; sys.path.insert(0, "scripts"); from verify_cleanup import project_asset_keys; print(" ".join(project_asset_keys(sys.argv[1])))' "$STACK")
if [ -n "$BUCKETS$TABLES$GROUPS$KEYS" ]; then
  echo "== data left behind (removal_policy=$REMOVAL) and bootstrap objects"
  [ -n "$BUCKETS" ] && echo "buckets: $BUCKETS" | mask
  [ -n "$TABLES" ] && echo "tables: $TABLES"
  [ -n "$GROUPS" ] && echo "log groups: $GROUPS"
  [ -n "$KEYS" ] && echo "this project's objects in the CDK bootstrap assets bucket: $(echo $KEYS | wc -w) (CDK generic provider packages are kept)"
  if ask "Delete these (all object versions included)? Type yes:" "yes" "${CONFIRM_DATA:-}"; then
    for b in $BUCKETS; do
      uv run python - "$b" "$REGION" <<'PY' || warn "could not empty a bucket"
import sys, boto3
boto3.resource("s3", region_name=sys.argv[2]).Bucket(sys.argv[1]).object_versions.delete()
PY
      aws s3api delete-bucket --bucket "$b" --region "$REGION" && echo "deleted bucket" || warn "could not delete a bucket"
    done
    for t in $TABLES; do aws dynamodb delete-table --region "$REGION" --table-name "$t" >/dev/null && echo "deleted table $t" || warn "could not delete $t"; done
    for g in $GROUPS; do aws logs delete-log-group --region "$REGION" --log-group-name "$g" && echo "deleted $g" || warn "could not delete $g"; done
    ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
    ASSETS="cdk-hnb659fds-assets-${ACCOUNT}-${REGION}"
    for k in $KEYS; do  # the bootstrap bucket is versioned: every version and delete marker of exactly this key
      for v in $(aws s3api list-object-versions --bucket "$ASSETS" --prefix "$k" \
                  --query "[Versions[?Key=='$k'].VersionId, DeleteMarkers[?Key=='$k'].VersionId][]" --output text); do
        [ "$v" = "None" ] && continue
        aws s3api delete-object --bucket "$ASSETS" --key "$k" --version-id "$v" >/dev/null \
          && echo "deleted asset $k ($v)" || warn "could not delete asset $k"
      done
    done
  else
    echo "Kept data and asset objects."
  fi
fi
echo "KMS keys are not deleted immediately: CloudFormation schedules them for deletion (pending window). verify-cleanup lists their state."
echo "Now run: make verify-cleanup PARAMS=$PARAMS"
