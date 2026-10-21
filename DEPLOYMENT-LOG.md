# Deployment log

Account ids are replaced with `111122223333`; no credentials appear here. Not legal advice.
Disposable sandbox account, IAM user credentials from the shell environment. Region **us-east-1**
only (no other region was deployed; us-west-2 was synthesised only). The CDKToolkit stack was used
as is and not modified; no account-level setting was changed.

## Cycle 1 — 2026-10-21, commit `642b4e6`

Step 1.4 also carried the logging change that was committed afterwards as `48c0aec`.

Parameter file: `config/params.example.json` with `session.retention_seconds=180` and
`session.share_link_ttl_seconds=60` (shortened for D4 and link expiry).

| Step | Time (UTC) | Command | Result |
|---|---|---|---|
| 1.1 preflight | 02:07 | `CONFIRM_ACCOUNT=<last4> scripts/preflight.sh config/params.json` | all ok (CDKToolkit present, guardrail resource type available) |
| 1.2 deploy | 02:07–02:14 | `scripts/deploy.sh config/params.json` | `proto-kiosk-sandbox` CREATE_COMPLETE |
| 1.3 e2e | 02:14–02:20 | `scripts/e2e_aws.py --params config/params.json` | 48 passed, 0 failed, 3 info |
| 1.4 update to Bedrock (cross-Region profile) | 02:22–02:25 | `scripts/deploy.sh config/params.cris.json` (provider bedrock, `us.stability.stable-image-control-structure-v1:0`, `allow_cross_region_inference=true`) | UPDATE_COMPLETE; diff had property changes only (guard did not trigger) |
| 1.5 e2e model check | 02:25 | `scripts/e2e_aws.py --params config/params.cris.json --only-d1` | 13 passed; CloudTrail `InvokeModel` `inferenceRegion=us-east-1` |
| 1.6 destroy | 02:30–02:35 | `CONFIRM_STACK=proto-kiosk-sandbox CONFIRM_DATA=yes scripts/cleanup.sh config/params.cris.json` | stack deleted; auto-delete handler log group deleted; 6 project asset objects deleted from the bootstrap bucket (CDK shared packages kept) |
| 1.7 residual check | 02:36 | `scripts/verify_cleanup.sh --params config/params.cris.json` | **1 residue**: log group `/proto-kiosk/cdk-handlers` |
| 1.8 manual fix | 02:37 | `aws logs delete-log-group --log-group-name /proto-kiosk/cdk-handlers` | deleted; re-check: nothing left |

Resources created (stack `proto-kiosk-sandbox`): HTTP API `proto-kiosk-api` (5 routes, authorizer
`proto-kiosk-kiosk-token`), Lambda `proto-kiosk-api`, `-processor`, `-expiry`, `-authorizer` (arm64,
python3.14) and the CDK BucketDeployment/auto-delete handlers, state machine
`proto-kiosk-session-expiry`, S3 image bucket and site bucket, DynamoDB session table, KMS keys
`alias/proto-kiosk-data` and `alias/proto-kiosk-receipts`, secret `proto-kiosk/kiosk-token`,
guardrail `proto-kiosk-image-guardrail` + version, log groups `/proto-kiosk/audit`, `/api-access`,
`/workflow`, `/cdk-handlers`, CloudFront distribution with OAC and response headers policy, IAM roles.

Findings from this cycle (fixed in later commits):
- `/proto-kiosk/cdk-handlers` was recreated by the BucketDeployment handler after CloudFormation
  had deleted it (no retention) → explicit dependency (`01e134e`).
- `cleanup.sh` used a variable named `GROUPS` (bash builtin) → renamed (`1a73fb6`).
- botocore INFO lines in the audit log group → library loggers set to WARNING (`48c0aec`).
- osv-scanner (run after the cycle) flagged pillow 12.3.0 / certifi 2026.7.22 → upgraded; the
  bundle is now built from `uv.lock`.

KMS keys after cycle 1: both `PendingDeletion`, deletion dates 2026-10-28 02:31 and 02:34 UTC.

Conclusion cycle 1: 已清理，无残留 (after the manual deletion of one log group, recorded above;
KMS keys are in PendingDeletion).

## Cycle 2 — final commit

See the section added after the final run below.
