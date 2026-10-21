# Third parties and cross-border data flows (template for compliance review)

> **Template, not a conclusion.** Not legal advice; no statement that any law or regulation is
> satisfied. Whether a flow counts as a transfer, and what it requires, is for counsel to assess.

## Services that process session data

All are AWS services in the deployment region (`deploy.region`) unless stated otherwise.

| Service | Data it receives | Region | Notes |
|---|---|---|---|
| Amazon API Gateway (HTTP API) | requests, including the uploaded image (TLS) | `<REGION>` | access log without IP or path |
| AWS Lambda | requests, images while processing | `<REGION>` | arm64 functions |
| Amazon S3 | images (input, result) | `<REGION>` | SSE-KMS; regional endpoint used for links |
| Amazon DynamoDB | session record (no image) | `<REGION>` | no PITR |
| AWS Step Functions | session_ref, wait seconds | `<REGION>` | |
| AWS KMS | key operations (no image) | `<REGION>` | one key set per region |
| AWS Secrets Manager | kiosk device token | `<REGION>` | |
| Amazon CloudWatch Logs | audit events (no image) | `<REGION>` | data protection policy |
| Amazon Bedrock Guardrails (ApplyGuardrail) | input and output images | `<REGION>` | image filters accepted only in regions where they are generally available |
| Amazon Bedrock image model (optional) | input image, fixed prompt | `<REGION>`, or the profile's geography (below) | model from a third-party provider, run by Bedrock; per the [Bedrock data protection page](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html) model providers have no access to customer prompts and completions |
| Amazon CloudFront (optional) | static page assets only; no images, no session data | global edge network | `frontend.enabled=false` removes it |

AWS Organizations [AI services opt-out policies](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_ai-opt-out.html)
cover services that may use and store content for service improvement, possibly in another region.
On 2026-10-21 the [list of covered services](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_ai-opt-out_all.html#ai-opt-out-all-list)
included Amazon CloudWatch (used here for audit logs, which carry no image or personal data by
design) and Amazon Rekognition and Amazon Textract (deliberately not used for moderation or text
detection). Amazon Bedrock was not on that list. Setting an opt-out policy is an organization-level
decision outside this project (OPEN-QUESTIONS.md). No face detection or recognition service is used.

## Cross-region calls

**Default: none.** With the sandbox and handover parameter files, every regional resource and every
call that carries session data stays in `deploy.region`. `make synth-regions` produces
`cdk.out-regions/<region>/region-check.json` listing resource types (regional or global) and the
cross-region call list.

| Call | Enabled by | Data | Possible regions | Evidence |
|---|---|---|---|---|
| `bedrock:InvokeModel` through a geographic cross-Region inference profile (for example `us.stability.stable-image-control-structure-v1:0`) | `image_model.allow_cross_region_inference=true` (synth fails without it); global profiles are always refused | input image (re-encoded) and a fixed prompt | regions listed in the profile (`make preflight` prints them; for the US profile on 2026-10-21: us-east-1, us-east-2, us-west-2) | CloudTrail `additionalEventData.inferenceRegion` in the source region ([docs](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)) |

Per the Bedrock documentation, geographic cross-Region inference routes within the geography of the
profile and data stays on the AWS network, encrypted in transit; CloudTrail logs the request in the
source region. Whether that is acceptable for `<COUNTRY>` is a legal decision.

Global services in the path that carry no session data: IAM (roles and policies) and, if enabled,
CloudFront (page assets).

## Moving to another country or region

1. Copy the parameter file, set `deploy.region`, `deploy.stack_name`, `session.notice_version` and the notice texts.
2. Check the model and filters there: `make preflight PARAMS=<file>` (model on-demand availability,
   guardrail resource type) and the region table in `infra/kiosk_infra/params.py`.
3. `make synth-regions` style check for the new region; review `region-check.json`.
4. Re-run this review with the new region and `<COUNTRY>`.

`<List any further processors your deployment adds.>`
