# Handover

For a team that takes this prototype into its own non-production account. Not legal advice; the
project makes no statement that any law or regulation is satisfied.

## What you receive
- The stack, parameter templates and scripts described in the [README](../README.md).
- Compliance review templates in [docs/compliance/](compliance/) to fill in with your legal team.
- Evidence of what was tested: [VERIFICATION.md](../VERIFICATION.md), [DEPLOYMENT-LOG.md](../DEPLOYMENT-LOG.md).

## Handover parameter file
Start from `config/params.handover.example.json`:
- `removal_policy=retain`: the image bucket, session table, log groups and KMS keys survive
  `cdk destroy`. This protects data from accidental deletion in a shared account; it also means
  deletion must be done deliberately (`make destroy` asks before deleting retained data). Choose
  `destroy` only for disposable accounts.
- `us-west-2` with the in-region Bedrock model `stability.sd3-5-large-v1:0` (no cross-region call).
  Check availability again with `make preflight` before deploying.
- Log retention 30 days. Choose the value with your legal team.

## Operating notes
- Kiosk token: `make kiosk-token` prints it for the page's setup screen. Rotate by updating the
  secret value; the authorizer caches it per execution environment (cold start reloads it).
- Notice text: generated in `src/kiosk/settings.py` from the parameters; change
  `session.notice_version` whenever the text changes (consents to old versions are refused).
- Audit events: `/<prefix>/audit`, JSON lines with `"audit": true`. Event names and fields are listed
  in `src/kiosk/audit.py`.
- Verify a receipt: `kms verify-mac` with the receipt key on the canonical JSON (see
  [retention-and-deletion.md](compliance/retention-and-deletion.md)).

## Production hardening checklist
1. Replace the shared kiosk token with per-device credentials (mTLS on a custom domain, or IAM per
   device), and add rotation for whatever secret remains.
2. Put AWS WAF in front (REST API, or CloudFront + WAF before the API, after reviewing the data
   flow); add rate-based rules and bot control as needed.
3. Custom domains with TLS policies for the page and the API; CloudFront geo restriction and WAF
   for the page, or serve the page from the kiosk (`frontend.enabled=false`).
4. Lock down who can change the image bucket policy, KMS key policies and log data protection
   policies (SCPs, permission boundaries); alert on changes (CloudTrail, EventBridge).
5. Decide whether CloudTrail S3 data events are needed for object-level audit; they record caller
   IP addresses — weigh this against data minimisation.
6. Alarms: `deletion_incomplete` events, workflow failures, `LogEventsWithFindings` from the data
   protection policy (should be zero), Lambda errors, throttling.
7. Tune content filter strengths with consented test material; decide on a human review path.
8. Legal review of the notice, retention, log retention, and the cross-border list for each country.
9. Load test the flow and set Lambda reserved concurrency and API throttling from the results;
   check Bedrock quotas in Service Quotas.
10. Run the CI ASH job and the deploy-verify workflow (OIDC, approval environment) before each release.
11. Backup policy: confirm that no backup, replication or PITR is added for the image bucket and
    session table without updating the deletion design.
