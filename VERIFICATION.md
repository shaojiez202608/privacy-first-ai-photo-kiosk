# Verification

What was actually run, with results. Not legal advice. Account ids masked.

**Final verification commit: `4918f58`** (clean tree). Later commits change documentation only.
Date: 2026-10-21. Machine: macOS arm64, Python 3.13 venv (uv 0.10.10), Node 24, CDK CLI 2.x (npx).

## Local (no AWS)

| Check | Command | Result |
|---|---|---|
| Lint | `make lint` | ruff: all checks passed |
| Tests | `make test` | **293 passed**, 0 failed (unit, moto, Stubber, in-process synth + cdk-nag, docs rules) |
| Offline demo D1–D6 | `uv run python scripts/demo_local.py` | 21/21 checks passed, D6 templates for 2 regions |
| Synth + cdk-nag | `make synth` (sandbox, handover, cris examples) | no unacknowledged findings |
| D6 two regions | `make synth-regions` | us-east-1 and us-west-2 synthesised; region check passed; cross-region calls: none |
| Synth without credentials | `cdk synth` with AWS credentials unset | succeeded (CI condition) |
| Fresh clone | clone → `make init samples` → lint, test, demo, synth, synth-regions | all passed (found and fixed: the parameter templates had not been committed) |
| Secret scan, history | `gitleaks git .` | no leaks found |
| Secret canary | `scripts/secret_canary.sh` | generated fake key detected (scanner works) |
| Working tree | `detect-secrets scan` | 0 findings |
| Workflows | `uvx zizmor .github/workflows` | no findings |
| Dependencies | `osv-scanner scan --lockfile uv.lock` | no issues (after upgrading pillow to 13.0.0 and certifi to 2026.10.19; before: High/Critical advisories) |
| ASH | — | **not run locally**: no Docker on this machine. The CI `ash` job (copied from a workflow already run on GitHub) is where ASH runs; CI has **not been run on GitHub** for this repository (not pushed) |

Negative controls that must fail: plain delete on a versioned store leaves versions
(`test_negative_control_naive_delete_leaves_versions`); a default us-east-1 S3 client presigns with
SigV2 (`test_default_client_would_presign_sigv2_negative_control`); compliance-claim patterns fire
on claim examples (`test_claim_patterns_catch_examples`).

## Cloud (us-east-1, sandbox account)

Parameters: `config/params.example.json` with `retention_seconds=180`, `share_link_ttl_seconds=60`;
model variant: the same plus `us.stability.stable-image-control-structure-v1:0` with
`allow_cross_region_inference=true` (equivalent to `params.cris-bedrock.example.json`).

### Cycle 2 (final, commit `4918f58`)
| Scenario | Result |
|---|---|
| Access control: no token 401, wrong token 403, anonymous bucket read 403 | pass |
| D1 consent, async processing, ready, regional SigV4 link, link ≤ TTL, input deleted, download OK, no EXIF/GPS in result, QR, link refused after expiry (HTTP 403) | pass |
| D2 decline → no session, no object; stale notice 409 | pass |
| Denial: wrong secret 404; operator GetObject denied by bucket policy; tampered link 403; second photo 409 | pass |
| D3 delete → receipt `remaining_versions=0`; KMS VerifyMac valid; no version under prefix; record deleted; workflow ABORTED; link dead; `session_deleted` audit event | pass |
| D4 expiry workflow SUCCEEDED after retention; no version; record deleted; `session_expired` event | pass |
| D5 (Guardrails, HIGH): threat text → blocked VIOLENCE; embedded instruction → blocked PROMPT_ATTACK; "unsafe marker" test card → blocked HATE (**false positive**: harmless text); all inputs deleted, no share link | pass (outcome recorded) |
| Validation: tiny / not an image / GIF → 422; style outside enum → 400; oversized → 413 | pass |
| Logs: data protection masks e-mail and IP in a canary event; no session id/secret, image data, e-mail or IPv4 in the audit log; access log has route templates only | pass |
| D6 in the cloud: 60 stack resources, no physical id naming another region; global types only IAM and CloudFront; nothing tagged in us-west-2 | pass |
| **Total e2e** | **48 passed, 0 failed, 3 info** |
| Bedrock model (cross-Region profile, opt-in): D1 with the real model ready in 14 s, input deleted, no metadata, QR, session deleted | 13 passed, 0 failed; CloudTrail `InvokeModel` `inferenceRegion=us-east-1` |
| Destroy + residual check (us-east-1, us-west-2) | **已清理，无残留**; 4 KMS keys PendingDeletion (until 2026-10-28) |

Cycle 1 (commit `642b4e6`) had the same results and one leftover log group, fixed in `01e134e`
(see DEPLOYMENT-LOG.md).

## What these results do not show
- No load test: one session at a time; no latency or throughput figures are claimed.
- Content filters were tested with five synthetic images only; filter quality is not measured.
- The in-region Bedrock model of the handover file (us-west-2, `stability.sd3-5-large-v1:0`) was
  synthesised but **not deployed**; its request shape is covered only by Stubber tests.
- The `us.` profile can route to us-east-2 or us-west-2; only us-east-1 processing was observed.
- CloudWatch Logs data protection was checked with one e-mail and one IP canary only.
- CI and ASH have not run on GitHub; Dependabot not exercised.
- The kiosk page was exercised over HTTP by scripts and in local mode, not with a real kiosk or a
  camera; the browser UI was not tested with automated browser tests.
