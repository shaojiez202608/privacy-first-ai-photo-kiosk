# Security

## Reporting a vulnerability

If you discover a potential security issue in this project, notify AWS Security via the
[AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/) or
directly via e-mail to aws-security@amazon.com. Please do **not** create a public GitHub issue.

> This is a prototype for demonstration and learning, not for production. Nothing in this
> repository constitutes legal advice, and it makes no statement that any law or regulation is
> satisfied. Assess it with your own security and legal teams.

## What gets deployed

One stack per region (see [docs/architecture.md](docs/architecture.md)): an HTTP API with a Lambda
authorizer, four arm64 Lambda functions, a Step Functions Standard workflow, a versioned private
S3 bucket for session images, a DynamoDB table for consent and session state, two KMS keys (data,
receipt HMAC), a Secrets Manager secret (kiosk device token), a Bedrock guardrail, four CloudWatch
log groups, and optionally a CloudFront distribution with a private S3 origin for the kiosk page.

## How data is handled

- Images are re-encoded (metadata dropped), stored only under `s/<session_ref>/`, and deleted with
  every version and delete marker: the input right after processing, the result on request or at
  the end of the retention period. Lifecycle rules and DynamoDB TTL are a backstop only.
- The session table holds consent evidence and state, never image content. The session id is not
  stored; the secret only as a SHA-256 hash.
- Audit events are allowlisted; the log groups additionally carry a data protection policy.
- Details: [docs/compliance/](docs/compliance/).

## Controls and where they are implemented

| Control | Implementation |
|---|---|
| Kiosk device authentication on every route | `src/handlers/authorizer.py`, authorizer in `infra/kiosk_infra/stack.py` |
| Unguessable session ids, hashed secrets, same 404 for unknown and wrong | `src/kiosk/ids.py`, `KioskService._authorize` in `src/kiosk/service.py` |
| Consent bound to the notice hash; decline creates nothing | `KioskService.create_session` |
| Upload validation and metadata stripping | `src/kiosk/imaging.py` |
| Fixed server-side prompts (no text from images or requests) | `src/kiosk/styles.py` |
| Image content filters on input and output, fail closed | `GuardrailModeration` in `src/kiosk/aws.py`, guardrail in the stack |
| Model output validation (NaN/Infinity, size, base64, finish reasons) | `parse_model_response` in `src/kiosk/aws.py` |
| Deletion of all versions, receipts signed with KMS HMAC | `S3ObjectStore._purge`, `KioskService.purge`, `KmsMacSigner` |
| Short SigV4 presigned links, capped by the bucket (`s3:signatureAge`) | `regional_s3_client`, bucket policy in the stack |
| Only the API/processor roles may read or write images | `DenyImageAccessExceptKioskFunctions` bucket policy |
| Audit allowlist, no IP/path in access logs, data protection policy | `src/kiosk/audit.py`, stage access log format, `data_protection_policy` in the stack |
| Region pinning, cross-region inference only with opt-in | `infra/kiosk_infra/params.py`, `scripts/region_check.py` |
| Least-privilege IAM, cdk-nag with written reasons | stack, [SECURITY-NOTES.md](SECURITY-NOTES.md) |

## Known caveats

See [SECURITY-NOTES.md](SECURITY-NOTES.md) and [THREAT_MODEL.md](THREAT_MODEL.md). In short: the
kiosk token is a shared device secret; presigned links are bearer links until they expire;
CloudFront is a global service (static page only); content filters can produce false positives
and false negatives.

## Production hardening

See the checklist in [docs/handover.md](docs/handover.md#production-hardening-checklist).

## Cleanup

`make destroy` then `make verify-cleanup` (see [docs/cleanup.md](docs/cleanup.md)).
