# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-21

### Added
- Session service: consent against the exact notice hash, temporary processing (input deleted with
  all versions right after processing), share links bounded by the session, deletion on request and
  on expiry with HMAC-signed receipts, allowlisted audit events.
- Region-pinned CDK stack (HTTP API with a kiosk-token authorizer, arm64 Lambda, Step Functions
  expiry workflow, versioned private S3 bucket, DynamoDB with TTL, per-region KMS keys, Bedrock
  Guardrails image filters, CloudWatch Logs data protection, optional CloudFront page with OAC).
- Optional Bedrock image model (in-region on-demand model, or a geographic cross-Region inference
  profile behind an explicit opt-in).
- Local mode, offline demo (D1-D6), synthetic sample generator, compliance-review templates,
  threat model, ops scripts (preflight, guarded deploy, e2e, cleanup, residual check) and CI.

---
This document does not constitute legal advice.
