# Retention and deletion (template for compliance review)

> **Template, not a conclusion.** Not legal advice; no statement that any law or regulation is
> satisfied. Retention periods are business and legal decisions for your team.

## Principle

Deletion is **explicit**. The application deletes data itself at defined moments. S3 lifecycle
rules and DynamoDB TTL are kept only as a backstop for the case where an explicit deletion could
not run, because neither is immediate:

- S3 lifecycle actions run asynchronously; there can be a delay between the expiration date and
  removal, and expiration is expressed in whole days
  ([S3 docs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-expire-general-considerations.html)).
- DynamoDB TTL deletes expired items "within a few days" and expired items can still be read until
  then ([DynamoDB docs](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/howitworks-ttl.html)).
  The service treats a session as expired on access regardless of TTL.

## Retention parameters

| Parameter | Sandbox value | Meaning |
|---|---|---|
| `session.retention_seconds` | 300 | Session lifetime from consent. The notice text is generated from this value. Allowed 60-86400. |
| `session.share_link_ttl_seconds` | 120 | Upper bound of a download link; never longer than the remaining session. The bucket refuses signatures older than this (`s3:signatureAge`). |
| `session.ttl_grace_seconds` | 3600 | Added to the expiry for the DynamoDB TTL backstop. |
| `deploy.log_retention_days` | 7 (handover 30) | Retention of all log groups. |
| Lifecycle backstop | 1 day | Current and noncurrent versions under `s/`, expired delete markers, incomplete uploads. |
| KMS pending window | 7 days | Waiting period when the stack is destroyed. |

`<Record the values your team chose and why.>`

## Deletion events

| Trigger | What is deleted | How it is verified |
|---|---|---|
| Processing ends (any outcome) | input image, every version | `input_deleted` audit event with counts; e2e check `versions(s/<ref>/input.png) == 0` |
| Guest presses Delete | all object versions and delete markers under `s/<ref>/`, unfinished uploads, the session record; the expiry workflow is stopped | signed deletion receipt (`remaining_versions` must be 0); `session_deleted` audit event; tests `test_d3_*`, `test_service_on_moto_*`; e2e D3 |
| Retention period ends | same as above | workflow purge; `session_expired` audit event; tests `test_d4_*`; e2e D4 |
| Session used after expiry (workflow missed) | same as above | `expired_on_access` reason; test `test_expired_on_access_*` |
| Consent declined | nothing exists | `consent_declined` event only; test `test_d2_*`; e2e D2 |
| Stack destroyed | buckets (all versions), table, log groups, secrets; KMS keys scheduled for deletion | `make verify-cleanup` |

If a deletion cannot complete, the API returns `424 deletion_incomplete` with the receipt showing
`remaining_versions > 0`, writes a `deletion_incomplete` audit event, and the workflow retries.
Nothing is reported as deleted unless a listing after the delete found no version left.

## Copies outside the primary stores

| Possible copy | Status in this prototype |
|---|---|
| S3 object versions and delete markers | removed explicitly (see above) |
| S3 replication, backups, Object Lock | not configured |
| DynamoDB point-in-time recovery / backups | disabled on purpose (PITR would keep deleted records restorable) |
| CloudWatch Logs | no image data; allowlisted fields; retention as configured |
| Step Functions execution history | holds `session_ref` and wait seconds only |
| Bedrock / Guardrails | this project stores nothing there; for service-side handling see [Bedrock data protection](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html) |
| Guest phone | the downloaded file is outside the system once downloaded |
| Kiosk browser | session values in page memory only; the page holds the result image while shown |

## Deletion receipt format

```json
{"type": "deletion", "receipt_id": "<uuid>", "session_ref": "<32 hex>", "reason": "user_request",
 "versions_deleted": 1, "delete_markers_deleted": 0, "uploads_aborted": 0, "remaining_versions": 0,
 "record_deleted": true, "schedule_cancelled": true, "region": "<REGION>",
 "deleted_at": "2026-10-21T02:17:40Z", "mac": "<base64 HMAC-SHA-256 from the region's KMS key>"}
```

Operators verify a receipt with `kms:VerifyMac` on the canonical JSON (sorted keys, no spaces,
without `mac`).
