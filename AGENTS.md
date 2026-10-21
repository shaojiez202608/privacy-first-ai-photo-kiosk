# Notes for AI coding assistants

## Role
You maintain a prototype reference architecture for a privacy-first photo kiosk. The core claims
are: images exist only during a session, deletion removes every object version, logs hold no
personal data, consent is recorded without image content, and the stack is pinned to one region.
Every change must keep these claims true and tested.

## Common commands
- `make init` - install dependencies, create `config/params.json`
- `make lint test` - ruff and pytest (no AWS)
- `make synth synth-regions` - cdk synth + cdk-nag, two regions (no credentials)
- `make demo-offline` / `make local` - offline demo / local kiosk page
- `make preflight deploy verify destroy verify-cleanup` - AWS (sandbox account only)
- `make scan` - gitleaks (history + canary), detect-secrets, zizmor, osv-scanner

## Git
Conventional Commits (`feat(scope): ...`, `fix`, `docs`, `test`, `chore`, `ci`, `refactor`); the
body says what changed, why, and how it was verified. Never commit account ids, credentials,
`config/params.json`, generated images or images of people.

## Boundaries
- Always: add a test with every behaviour change; keep new audit fields in the allowlist with a
  validator; keep deletion covering every new object or record; mask 12-digit numbers in output.
- Ask first: adding any new data store, log field, third-party service or cross-region call;
  changing removal policies, retention defaults, IAM scopes or cdk-nag acknowledgements.
- Never: add face recognition or identity matching; log image bytes, model output text, session
  ids or secrets; change account-level settings; write statements that the project satisfies a
  specific law or regulation; push to a remote.

---
This document does not constitute legal advice.
