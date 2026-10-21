# Deployment guide

The commands are in the [README "Deploy" section](../README.md#deploy); this page explains the
prerequisites and every parameter. Not legal advice.

## Prerequisites
- Disposable non-production account, credentials in the environment, CDK bootstrap in the target
  region (`CDKToolkit` stack; this project never modifies it).
- uv, Node.js, AWS CLI v2, rsync. No Docker: the arm64 Lambda bundle is built from wheels.
- `make preflight` checks all of this read-only and asks you to confirm the last four digits of the
  account.

## Parameter file

One file holds every parameter (`config/params.json`, created from `config/params.example.json`;
git-ignored). It is validated at synth time; every problem is listed at once. The account id is
never in the file (the validator rejects 12-digit numbers).

| Key | Sandbox | Handover | Notes |
|---|---|---|---|
| `deploy.stack_name` | `proto-kiosk-sandbox` | `proto-kiosk-handover` | must start with the prefix |
| `deploy.resource_prefix` | `proto-kiosk` | same | names of functions, log groups, keys, secret |
| `deploy.region` | `us-east-1` | `us-west-2` | **all regional resources go here** |
| `deploy.removal_policy` | `destroy` | `retain` | retain keeps bucket, table, logs, keys on destroy |
| `deploy.log_retention_days` | 7 | 30 | CloudWatch allowed values |
| `deploy.image_bucket_versioning` | true | true | purge removes all versions either way |
| `deploy.lambda_memory_mb`, `processor_timeout_s` | 1024, 120 | same | |
| `deploy.api_throttle_rate`, `api_throttle_burst` | 5, 10 | same | stage default route throttling |
| `deploy.cors_extra_origins` | [] | [] | extra `https://` origins (needed if `frontend.enabled=false`) |
| `deploy.tags` | Project, Purpose, Teardown | same | applied to every resource |
| `frontend.enabled` | true | true | CloudFront + private site bucket |
| `frontend.geo_allowlist` | [] | [] | ISO country codes for CloudFront geo restriction |
| `session.kiosk_id` | `kiosk-demo-01` | | written to audit events |
| `session.notice_version` | `2026-10-v1` | | change when the notice text changes |
| `session.retention_seconds` | 300 | | 60-86400; notice text follows it |
| `session.share_link_ttl_seconds` | 120 | | ≤ retention; also the bucket's signature-age limit |
| `session.ttl_grace_seconds` | 3600 | | DynamoDB TTL backstop margin |
| `session.max_upload_bytes`, `max_side`, `min_side`, `max_aspect`, `max_pixels` | | | upload validation; keep within the guardrail and model limits on their pages |
| `session.operator_contact` | placeholder | | shown in the notice |
| `image_model.provider` | `transform` | `bedrock` | `transform` = deterministic Pillow transformation in Lambda |
| `image_model.model_id`, `family`, `strength` | - | `stability.sd3-5-large-v1:0`, `sd35-image-to-image`, 0.6 | only models with request code here are accepted |
| `image_model.allow_cross_region_inference` | false | false | must be true for `us.`/`eu.`… profiles; global profiles are refused |
| `moderation.provider` | `guardrails` | `guardrails` | `mock` (markers) or `none` (needs `allow_unmoderated`) |
| `moderation.filter_strength` | `HIGH` | `HIGH` | applied to all six categories |
| `logging.data_identifiers` | Address, EmailAddress, IpAddress, LatLong, Name | | CloudWatch Logs managed identifiers; add country ones as `Name-CC` |

## Deploying to another region
Copy the file, change `deploy.region` and `deploy.stack_name`, run `make preflight PARAMS=…`
(model and guardrail availability), `make synth`, then deploy. Nothing in the code changes.
`make synth-regions` shows the same stack for us-east-1 and us-west-2 and runs the region check.

## Guardrails behaviour
The stack creates the guardrail and a pinned version; the processor calls ApplyGuardrail for the
input and the output image and fails closed on errors.

## Removal policy
Sandbox `destroy`: buckets are emptied by a CDK custom resource and everything is deleted; KMS keys
are scheduled for deletion. Handover `retain`: data stays after `cdk destroy`; `make destroy` asks
before deleting it (see [handover.md](handover.md)).
