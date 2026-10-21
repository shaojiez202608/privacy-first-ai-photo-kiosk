# Decisions

Each entry: what was chosen, the alternatives, and why. When two options were close, the more
conservative one was chosen. Not legal advice.

## Technology choices

| Layer | Chosen | Alternatives | Why |
|---|---|---|---|
| IaC | AWS CDK (Python) with cdk-nag AwsSolutions | CDK TypeScript | one language for runtime, tests and infrastructure; synth runs in-process in tests |
| Kiosk page | static page on S3 (private) + CloudFront with OAC; optional (`frontend.enabled`) | local HTML only | OAC is the recommended way to reach a private S3 origin and supports SSE-KMS ([docs](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html)); the page carries no images (they go to the regional API and S3), so the global edge sees no session data. `frontend.enabled=false` for strict deployments, the page then comes from the kiosk |
| API | API Gateway **HTTP API** + Lambda (arm64) | REST API + Lambda; ALB + Lambda | REST API access logging needs the account-level API Gateway CloudWatch role, an account setting this project must not change (in the sandbox it belonged to another stack). HTTP API: access logs without it, Lambda authorizer, built-in CORS. Cost of the choice: no AWS WAF and no API keys/usage plans on HTTP APIs ([comparison](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html)); kept in the hardening list |
| Kiosk authentication | Lambda authorizer comparing a device token from Secrets Manager | API keys; IAM; Cognito | API keys must not be used for authorization ([docs](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-api-usage-plans.html)); IAM/Cognito would add per-device identity management that the scenario has not specified (OPEN-QUESTIONS) |
| Session and consent store | DynamoDB with TTL, consent evidence only | RDS | no images, key-value access, TTL as backstop. TTL deletes "within a few days", so the service deletes explicitly and treats expired sessions as gone on access |
| Image staging | S3, private, TLS, SSE-KMS, versioning on, lifecycle as backstop | no versioning | many organisations require versioning; with it on, the purge code is proven to remove every version and delete marker (moto tests, cloud e2e). With versioning off the same code works |
| Expiry orchestration | Step Functions Standard, one execution per session (Wait `SecondsPath` → purge Lambda with retry) | EventBridge Scheduler one-time schedules | the wait, the purge and its retries are one auditable execution per session that can be stopped on manual deletion; Scheduler invokes with 60-second precision and completed one-time schedules must be deleted separately ([docs](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html)). Billing dimensions differ (state transitions vs invocations); see pricing pages |
| AI processing | adapter with three providers: `transform` (deterministic Pillow transformation in the Lambda; local mock and sandbox default), Bedrock in-region on-demand model (`stability.sd3-5-large-v1:0`, us-west-2), Bedrock geographic inference profile (`us.stability.stable-image-control-structure-v1:0`) behind an explicit flag | always Bedrock | on 2026-10-21 `list-foundation-models` showed no image model with in-region ON_DEMAND inference in us-east-1 (only `us.` profiles routing to us-east-1/us-east-2/us-west-2). A cross-region call by default would contradict region pinning, so the sandbox default has no model call; the Bedrock path was tested in the cloud with the opt-in |
| Content safety | Bedrock Guardrails image content filters via ApplyGuardrail on input and output, fail closed; mock markers offline | Amazon Rekognition moderation; application rules only | official capability that covers images in the target regions; Rekognition is on the AI services opt-out list (content may be used for service improvement unless the organisation opts out). Preview regions are refused |
| Prompt-injection control | fixed server-side prompts selected by enum + Guardrails `PROMPT_ATTACK` | OCR of image text + text filters | no channel from image text to instructions exists; OCR (Textract) would add another processor that is on the opt-out list |
| Share | S3 presigned GET (SigV4, regional endpoint) + QR code (segno, server-side) | CloudFront signed URLs | the object stays in the region; lifetime enforced twice (signature expiry and bucket `s3:signatureAge`); presigned URLs from role credentials end no later than the credentials ([docs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)), which is shorter, not longer |
| QR code | generated on the server (segno, BSD-3-Clause) | a JavaScript library from a CDN | no third-party script on the kiosk page (CSP `script-src 'self'`) |
| Audit | CloudWatch Logs with allowlisted events and a data protection policy; CloudTrail data events optional | own audit store | native, encrypted with the region's key, retention set; the allowlist keeps personal data out and the data protection policy is a second layer |
| Encryption | per-region customer managed KMS key (data) + HMAC key (receipts) | AWS managed keys | region-local key material and an explicit key policy; destroy schedules deletion (7-day window in the sandbox) |
| Abuse protection | authorizer + stage throttling + body limits; WAF optional via REST API/CloudFront | WAF in the prototype | HTTP API has no WAF integration; throttling is best effort ([docs](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-api-usage-plans.html)) |
| Lambda runtime | Python 3.14, arm64 | Python 3.13 | newest supported Python runtime on 2026-10-21 (deprecation not before 2029); all runtimes support arm64 ([docs](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html)) |
| Packaging | wheels for `aarch64-manylinux_2_28` downloaded with `--only-binary`, no Docker | container image | no Docker on the build machine; Pillow publishes aarch64 wheels |

## Other decisions

1. **Retention default 300 s, range 60–86400 s.** "Shortest by default" taken as the shortest
   period that still leaves a guest time to scan the QR code and download; 60 s is the floor that
   keeps the flow usable. The sandbox run used 180 s to keep D4 short. Final value: OPEN-QUESTIONS.
2. **Input deleted right after processing**, result kept until the session ends. The input is
   stored only because processing is asynchronous (model latency vs the API timeout); a synchronous
   design without storing the input is a possible follow-up.
3. **Processing is asynchronous** (`InvocationType=Event`, no retries): a lost invocation leaves the
   session in `processing` until expiry, which still deletes the input.
4. **No S3 server access logs, no CloudFront viewer logs, no PITR** (see SECURITY-NOTES).
5. **Same 404** for unknown sessions and wrong secrets; **424** (not 5xx) for dependency failures.
6. **Receipts are HMAC-signed with KMS** rather than stored: they can be verified later without
   keeping anything about the session.
7. **Removal policy is a parameter**: sandbox `destroy` (buckets auto-emptied), handover `retain`
   (data and keys kept; see handover.md).
8. **Account id never in files**; the CDK environment comes from the shell.
9. **Unified decisions for this repository** (MIT-0 with the Amazon copyright line, SECURITY.md
   reporting through the AWS vulnerability page, `111122223333` placeholder, CODEOWNERS
   `@shaojiez202608`, git identity of the maintainer) follow the shared repository conventions.
10. **ASH is not run locally** (no Docker on the build machine); the CI `ash` job is the one
    copied from a workflow already proven on GitHub.
11. **Build directories are not deleted by scripts**: each bundle build goes to a new
    `build/bundle.XXXXXX` directory named in `build/bundle.current` (`rm -rf` is not used).
