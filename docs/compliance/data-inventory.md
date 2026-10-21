# Data inventory (template for compliance review)

> **Template, not a conclusion.** Not legal advice; it makes no statement that any law or
> regulation is satisfied. Classification of each item (for example, whether it is personal data)
> is for your legal team to decide. Fill in `<...>`.

Facts below are for commit `v0.1.0` with the sandbox parameters (`config/params.example.json`).

| Item | Content | Store | Encryption | Created | Deleted by | Backstop | Readable by |
|---|---|---|---|---|---|---|---|
| Input image | Uploaded picture, decoded and re-encoded as PNG (EXIF/GPS/text chunks dropped) | S3 `s/<ref>/input.png` (versioned bucket) | SSE-KMS, data key of the region; TLS only | photo submitted | processor, right after processing, every version (`purge_key`) | lifecycle: expire after 1 day, noncurrent after 1 day | API and processor roles only (bucket policy) |
| Result image | Stylized PNG | S3 `s/<ref>/result.png` | same | processing ready | delete on request, or expiry workflow at end of retention (all versions and delete markers) | same lifecycle rule | API/processor roles; guest via presigned link |
| Session record | session_ref, SHA-256 of the secret, consent receipt id, notice version and hash, consent time, expiry, status, style key, reason code, result key | DynamoDB | KMS data key | consent accepted | delete on request / expiry (item deleted) | TTL (`expires_at` + `ttl_grace_seconds`) | API, processor, expiry roles |
| Consent receipt | type, receipt id, session_ref, decision, notice version/hash, kiosk id, region, times, retention, HMAC | returned to the kiosk; same fields in the audit event | HMAC via KMS key | consent accepted | not stored server-side beyond the audit event | - | kiosk (response) |
| Deletion receipt | receipt id, session_ref, reason, counts deleted/remaining, record deleted, schedule cancelled, region, time, HMAC | returned to the kiosk; audit event | HMAC via KMS key | deletion | not stored server-side beyond the audit event | - | kiosk (response) |
| Audit events | allowlisted fields only (`src/kiosk/audit.py`): event, ids derived from random values, enums, counts | CloudWatch Logs `/<prefix>/audit` | KMS data key; data protection policy (audit + mask) | each event | log retention (`deploy.log_retention_days`, sandbox 7 days) | - | operators with log read access; masked fields need `logs:Unmask` |
| API access log | request id, time, route template, status, latency, authorizer error, integration status (no IP, no path, no user agent) | CloudWatch Logs `/<prefix>/api-access` | same | each request | log retention | - | operators |
| Workflow log | execution events without input/output data (`includeExecutionData=false`) | CloudWatch Logs `/<prefix>/workflow` | same | each execution | log retention | - | operators |
| Workflow execution history | execution name `exp-<ref>`, input `{session_ref, wait_seconds}` | Step Functions | service-managed | consent accepted | kept by the service for its execution history retention period after the execution closes ([Step Functions quotas](https://docs.aws.amazon.com/step-functions/latest/dg/service-quotas.html#service-limits-state-machine-executions)); contains no image or personal data | - | operators |
| Kiosk device token | random secret | Secrets Manager | KMS data key | deploy | destroy | - | authorizer role; operators |
| Session id / secret | random values | kiosk page memory only | TLS in transit | consent accepted | page restart | - | the kiosk page |
| Presigned link | bearer URL to one result object | kiosk screen, QR code, guest phone | TLS | each status call when ready | expires (≤ `share_link_ttl_seconds`, ≤ session end; bucket refuses older signatures) | - | whoever holds the link |

Not collected: names, contact details, account ids of guests, device identifiers, IP addresses (no
S3 server access logs, no CloudFront viewer logs, no IP in the API access log), face templates or
any biometric template.

`<Add any data your deployment adds: printed receipts, payment, kiosk telemetry, ...>`
