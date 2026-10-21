# Demo script

All data in the demo is synthetic (shapes, colour blocks, text; no people). The demo does not
constitute legal advice; say so when presenting the compliance templates.

## Audience and length
- **10 minutes** (business and privacy stakeholders): D1, D2, D3, the data-flow template. Use the
  offline path or a deployed sandbox.
- **30 minutes** (architects, security, legal reviewers): all of D1–D6, the audit log, the threat
  model, the region check and the compliance templates.

## Preparation checklist
- [ ] `make init samples` (dependencies, synthetic images).
- [ ] Offline only: `make demo-offline` once (also synthesises the two regions for D6).
- [ ] Cloud: `make deploy` with a parameter file whose `session.retention_seconds` is short (for
      example 180) so D4 fits in the session; run `make verify` once as a warm-up (cold starts).
- [ ] `make kiosk-token` ready in a terminal (do not paste it into chat or slides).
- [ ] Tabs: kiosk page (`KioskUrl` or `http://127.0.0.1:8080`), the audit log group in CloudWatch
      Logs Insights, the image bucket "Show versions" view, `docs/compliance/data-flow.md`.

## One-command start and reset
- Offline start: `./scripts/demo.sh --offline` (prints a PASS/FAIL summary) or `make local`
  (interactive page with in-memory backends).
- Cloud run: `./scripts/demo.sh --aws config/params.json`.
- Reset: `./scripts/demo.sh --reset` (regenerates samples; local mode keeps state in memory, so
  restarting `make local` resets it). In the cloud, sessions delete themselves at expiry.

## Steps

| # | Operation | Expected | Talking points | If it fails |
|---|---|---|---|---|
| D1.1 | Open the page, enter the kiosk token | notice with purpose, retention, deletion, disclaimer | the retention in the text is generated from the enforced parameter | 401/403: wrong token; re-run `make kiosk-token` |
| D1.2 | "I agree" | sample pictures appear; session end time shown | consent is bound to the notice hash; a signed consent receipt is returned; no camera | 409 notice_changed: reload the page |
| D1.3 | Pick "Geometric landscape", style "Comic", "Create my picture" | "Working…", then the result and a QR code | async processing; input re-encoded (metadata dropped), filtered, stylized, filtered again; input deleted with all versions right away | stays in processing: check the processor log; session still expires |
| D1.4 | Scan the QR code with a phone | the PNG downloads | regional S3 endpoint, SigV4 presigned, lifetime ≤ link TTL and ≤ session | 403: link expired; refresh status to get a new one |
| D1.5 | Wait for the link time to pass, reload the link | 403 | the bucket also refuses old signatures (`s3:signatureAge`) | - |
| D2 | Start over, "No, thanks" | "No photo was taken" | nothing created: no session, no object, only a `consent_declined` audit event; show the bucket is unchanged | - |
| D3 | In a new session after D1.3, "Delete everything now" | deletion receipt with `remaining_versions: 0` | every version and delete marker removed, record deleted, workflow stopped; receipt HMAC verifiable with KMS; show `session_deleted` in the audit log and the empty prefix in "Show versions" | 424 deletion_incomplete: retry; explain the backstops |
| D4 | Create a session, process a picture, wait past the retention | page shows an error on refresh (410/404); bucket prefix empty | Step Functions Wait then purge; `session_expired` event; TTL/lifecycle are only backstops because they are not immediate | workflow still running: wait (retries) |
| D5 | Pick "Test: embedded instruction" (and "Test: unsafe-content marker") | "We could not use this picture" | prompts are fixed constants, image text cannot become an instruction; Guardrails blocked the instruction image as PROMPT_ATTACK in the sandbox; blocked input deleted, never shared; `input_blocked` event with category | if not blocked: the structural control still holds; record it as a filter false negative |
| D6 | `make synth-regions`; open `cdk.out-regions/us-west-2/region-check.json` | both regions synthesise; cross-region calls "none" | same code, one parameter; only CloudFront (page) and IAM are global; with the cris file the Bedrock profile appears in the list | synth error lists the parameter problem |
| Log | CloudWatch Logs Insights on `/<prefix>/audit`: `fields event, reason, categories | sort @timestamp desc` | events with refs, codes, counts only | allowlist schema; data protection policy masks e-mail/IP (e2e canary) | - |
| Docs | Open the four compliance templates | | templates for the legal review, not conclusions | - |

## Offline fallback
Network or account unavailable: `./scripts/demo.sh --offline` runs D1–D6 with in-memory adapters
(versioned object store simulation, local HMAC links, mock moderation that recognises two coloured
marker patches only) and prints the checks. `make local` shows the same page; links point to
`http://127.0.0.1:8080/share`.

## After the demo
`make destroy`, then `make verify-cleanup` (see [cleanup.md](cleanup.md)). Close the browser tab
(the page keeps the session secret in memory).
