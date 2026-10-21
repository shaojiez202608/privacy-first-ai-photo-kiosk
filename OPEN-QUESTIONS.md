# Open questions

Technical and business questions the prototype could not answer. None blocks the prototype.

## Business and product
1. **Which AI function?** Stylization is the example. Background replacement, avatar generation or
   others change the model, the prompts, and possibly what is sent to the model.
2. **Kiosk form factor and network.** Browser kiosk with a stable connection is assumed. Offline
   operation, a native app or a captive network change authentication and the page hosting
   (`frontend.enabled`).
3. **Countries and regions.** Which countries, and must processing stay in one region or may it stay
   within a geography (cross-Region inference profiles)? Determines `deploy.region` and
   `image_model.allow_cross_region_inference`.
4. **Requirements from the legal review.** Notice wording, retention period
   (`session.retention_seconds`), log retention, whether consent evidence must be kept longer than
   the audit log retention, minimum age handling, accessibility of the notice.
5. **Deletion after leaving the kiosk.** Today a guest deletes at the kiosk or waits for expiry.
   Should the share page or a printed code allow deletion later? That needs a second, safe channel
   for the session secret.
6. **Should the result be deleted after the first download?** Not possible with presigned links
   alone; would need a download proxy.
7. **Languages** of the notice and UI.

## Technical
8. **Device identity.** Per-kiosk credentials (mTLS on a custom domain, IAM per device, or a
   provisioning flow) instead of one shared device token.
9. **WAF.** HTTP APIs do not integrate with AWS WAF. If WAF is required: REST API (needs the
   account-level API Gateway CloudWatch role, owned by the account administrator) or CloudFront +
   WAF in front of the API (then API traffic passes the global edge; review data flow).
10. **Content filter tuning.** One harmless synthetic test card was blocked as HATE at HIGH
    strength. The filter strength per category, and whether a human review path is needed, should
    be decided with real (consented, non-production) test material.
11. **AI services opt-out policy.** Amazon CloudWatch is on the AWS Organizations AI services opt-out
    list. Logs here carry no image or personal data; whether to set an organisation-wide opt-out is
    for the account owner.
12. **Model availability per target region.** In us-east-1 image models were only reachable through
    `us.` profiles on 2026-10-21; re-check `make preflight` for each new region.
13. **Maintainer handle.** CODEOWNERS uses `@shaojiez202608`; confirm before publishing.

---
This document does not constitute legal advice.
