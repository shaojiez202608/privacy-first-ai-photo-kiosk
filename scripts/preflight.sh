#!/usr/bin/env bash
# Read-only preflight. Changes nothing. Usage: scripts/preflight.sh [params-file]
# Shows the target account (last 4 digits only) and region and asks you to confirm them.
# Non-interactive runs confirm with CONFIRM_ACCOUNT=<last 4 digits>.
set -uo pipefail
cd "$(dirname "$0")/.."
PARAMS="${1:-config/params.json}"
fail=0
ok() { echo "  ok    $*"; }
bad() { echo "  FAIL  $*"; fail=1; }
info() { echo "  info  $*"; }

echo "== parameter file"
[ -f "$PARAMS" ] || { bad "$PARAMS missing (run: make init)"; exit 1; }
read -r STACK REGION PROVIDER MODEL MOD < <(python3 -c '
import json, sys
p = json.load(open(sys.argv[1]))
print(p["deploy"]["stack_name"], p["deploy"]["region"], p["image_model"]["provider"],
      p["image_model"]["model_id"] or "-", p["moderation"]["provider"])' "$PARAMS")
ok "$PARAMS (stack $STACK, region $REGION, model provider $PROVIDER $MODEL, moderation $MOD)"
if .venv/bin/python -c 'import json,sys; sys.path.insert(0,"infra"); from kiosk_infra.params import validate; validate(json.load(open(sys.argv[1])))' "$PARAMS" 2>/tmp/kiosk-preflight.err; then
  ok "parameter validation"
else
  bad "parameter validation:"; sed 's/^/        /' /tmp/kiosk-preflight.err | tail -12
fi

echo "== tools"
for t in uv node npx aws python3 rsync; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t"; else bad "$t not found"; fi
done
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  info "docker is running (not needed: the Lambda bundle is built without containers)"
else
  info "docker not running or not installed (not needed: the Lambda bundle is built without containers)"
fi
[ -f build/bundle.current ] && ok "Lambda bundle $(cat build/bundle.current)" || info "Lambda bundle not built yet (make bundle)"

echo "== credentials and target"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || { bad "no AWS credentials"; exit 1; }
LAST4="${ACCOUNT: -4}"
info "account ********${LAST4}, region ${REGION}"
if [ "${CONFIRM_ACCOUNT:-}" != "$LAST4" ]; then
  if [ -t 0 ]; then
    read -r -p "  Type the last 4 digits of the account to confirm this is the intended non-production account: " ans
    [ "$ans" = "$LAST4" ] || { bad "not confirmed; stop if this is not your intended account"; exit 1; }
  else
    bad "set CONFIRM_ACCOUNT=<last 4 digits> to confirm the target account"; exit 1
  fi
fi
ok "target account confirmed"

echo "== services in ${REGION}"
aws cloudformation describe-stacks --stack-name CDKToolkit --region "$REGION" --query 'Stacks[0].StackStatus' --output text >/dev/null 2>&1 \
  && ok "CDK bootstrap stack present (not modified by this project)" || bad "CDKToolkit missing in $REGION"
aws cloudformation describe-type --type RESOURCE --type-name AWS::Bedrock::Guardrail --region "$REGION" >/dev/null 2>&1 \
  && ok "AWS::Bedrock::Guardrail resource type" || { [ "$MOD" = guardrails ] && bad "AWS::Bedrock::Guardrail not available in $REGION"; }
if [ "$PROVIDER" = bedrock ]; then
  case "$MODEL" in
    us.*|eu.*|apac.*|jp.*|au.*|ca.*|us-gov.*)
      if aws bedrock get-inference-profile --inference-profile-identifier "$MODEL" --region "$REGION" >/dev/null 2>&1; then
        ok "inference profile $MODEL; destination regions:"
        aws bedrock get-inference-profile --inference-profile-identifier "$MODEL" --region "$REGION" \
          --query 'models[].modelArn' --output text | tr '\t' '\n' | sed -E 's#arn:aws[a-z-]*:bedrock:([a-z0-9-]+)::.*#        \1#'
      else bad "inference profile $MODEL not available in $REGION"; fi ;;
    *)
      TYPES=$(aws bedrock get-foundation-model --model-identifier "$MODEL" --region "$REGION" --query 'modelDetails.inferenceTypesSupported' --output text 2>/dev/null)
      case "$TYPES" in *ON_DEMAND*) ok "model $MODEL on-demand in $REGION" ;; *) bad "model $MODEL not on-demand in $REGION (got: ${TYPES:-none})" ;; esac ;;
  esac
  AV=$(aws bedrock get-foundation-model-availability --model-id "${MODEL#*.stability.}" --region "$REGION" --query agreementAvailability.status --output text 2>/dev/null || true)
  info "model agreement status: ${AV:-unknown} (this project never creates agreements or changes account-level settings)"
fi
info "quotas: check Lambda concurrency and Bedrock image-model quotas in Service Quotas for the expected volume"
exit $fail
