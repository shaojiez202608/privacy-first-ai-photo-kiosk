# Standards review

Review against the shared prototype standard (review checklist A–F). Status: **met**, **changed**
(found during the work and fixed), **n/a** (with reason). Evidence is `file` or `file:section`.
Not legal advice. State at tag `v0.1.0`.

## A. Positioning and redaction

| # | Item | Status | Evidence |
|---|---|---|---|
| A1 | README first screen: demo/learning only, user responsible, assess with security and legal teams, link to hardening checklist | met | `README.md` (first block), `README.zh-CN.md` |
| A2 | No customer names, people, domains, 12-digit numbers, personal profile names, template leftovers | met | `tests/test_docs.py::test_no_forbidden_strings_or_account_ids` (all tracked text files); final `git log --all -p` check in VERIFICATION.md |
| A3 | Placeholders `<UPPER_SNAKE>`, `example.com`, `111122223333`; screenshots have provenance; data synthetic | met | `docs/compliance/*` placeholders; no screenshots in the repo; `sample-data/README.md`, `scripts/gen_samples.py` |

## B. Standard repository files

| # | Item | Status | Evidence |
|---|---|---|---|
| B1 | LICENSE (MIT-0 + Amazon copyright), SECURITY (full), CoC, CONTRIBUTING (tests, commits, no account ids, licensing, AI rule) | met | `LICENSE`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`; `test_required_files` |
| B2 | THIRD-PARTY-LICENSES (two tables, no relicensing, from metadata); CHANGELOG + VERSION | met | `THIRD-PARTY-LICENSES`, `CHANGELOG.md`, `VERSION`, tag `v0.1.0` |
| B3 | Issue/PR templates, AGENTS.md + CLAUDE.md, Makefile, .gitignore (.dockerignore n/a: no Docker build) | met | `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md`, `AGENTS.md`, `CLAUDE.md`, `Makefile` |
| B4 | DECISIONS, OPEN-QUESTIONS, SECURITY-NOTES, THREAT_MODEL (DRAFT), VERIFICATION, DEPLOYMENT-LOG | met | files at the root; `test_threat_model_is_draft_and_covers_required_threats` |

## C. Configuration and deployment

| # | Item | Status | Evidence |
|---|---|---|---|
| C1 | Parameter templates, input validation, removal policy as parameter, uniform tags | met | `config/*.example.json`, `infra/kiosk_infra/params.py`, `tests/test_params.py` |
| C2 | Preflight (account last 4 digits, region, tools, services), destructive-change guard, e2e checks | met | `scripts/preflight.sh`, `scripts/deploy.sh` (`ALLOW_DESTRUCTIVE`), `scripts/e2e_aws.py` |
| C3 | Verification record with date, region, configuration, commit, failures, limits | met | `VERIFICATION.md`, `DEPLOYMENT-LOG.md` |

## D. Security and CI

| # | Item | Status | Evidence |
|---|---|---|---|
| D1 | cdk-nag clean, every acknowledgement with a reason; trust policies with SourceAccount + SourceArn where documented; resource policies on sensitive resources | met | `tests/test_infra.py::test_nag_clean_and_region_pinned`, `SECURITY-NOTES.md` (acknowledgements, trust policies), bucket and key policies |
| D2 | CI: lint, tests, synth, secret scan; actions pinned by SHA (looked up with `git ls-remote`), minimal permissions; Dependabot grouped with cooldown; gitleaks without unexplained exceptions | met | `.github/workflows/ci.yml`, `.github/dependabot.yml`, `.gitleaks.toml`; zizmor: no findings |
| D3 | No account-level settings changed | met | DECISIONS (HTTP API chosen to avoid the account-level API Gateway role); DEPLOYMENT-LOG |
| D4 | Dependencies: lock file, cooldown, osv-scanner, High+ fixed | changed | osv-scanner found High/Critical in pillow 12.3.0 and certifi 2026.7.22; upgraded with a documented per-package cooldown exception (`pyproject.toml`); bundle built from `uv.lock` |

## E. Cleanup

| # | Item | Status | Evidence |
|---|---|---|---|
| E1 | Cleanup script with confirmations; residual check that fails on leftovers; README lists what is not removed automatically | met | `scripts/cleanup.sh`, `scripts/verify_cleanup.py`, README "Clean up" |
| E2 | All used regions checked; clear conclusion | met | DEPLOYMENT-LOG (us-east-1 only deployed region); cycle 1 found and fixed one leftover log group |

## F. Documentation and commits

| # | Item | Status | Evidence |
|---|---|---|---|
| F1 | Fact check with links and dates; cost only as dimensions; README section order | met | `docs/architecture.md#fact-check`, README "Prerequisites and cost", `test_readme_section_order` |
| F2 | Conventional Commits with verification in the body; not pushed | met | `git log`; no remote configured |

## Project-specific requirements

| Item | Status | Evidence |
|---|---|---|
| No face recognition, no identity matching; synthetic images only (Pillow) | met | `src/`, `scripts/gen_samples.py`, `web/app.js` canvas samples |
| "Not legal advice" in every document; no "satisfies law X" statements | met | `tests/test_docs.py` (with negative control) |
| Explicit deletion including all versions and delete markers; lifecycle/TTL only backstop; tests and cloud check | met | `S3ObjectStore._purge`, tests D3/D4 + moto, e2e D3/D4 |
| Unguessable session ids; short presigned links; expiry tested in the cloud | met | `src/kiosk/ids.py`; e2e "link refused after expiry" |
| Region parameter; two regions synthesised; cross-region call list (default none) | met | `make synth-regions`, `scripts/region_check.py`, `docs/compliance/third-parties-and-cross-border.md` |
| Bedrock model ids and Guardrails image support checked with docs and `list-foundation-models` | met | `docs/architecture.md` fact check 1–10 |
| CloudWatch Logs data protection (scope checked), no image or personal data in logs (tests) | met | stack, `tests/test_audit_and_logs.py`, e2e canary |
| Per-region KMS keys; pending-deletion state recorded | met | stack; DEPLOYMENT-LOG |
| Compliance templates (data flow mermaid, inventory, retention/deletion, third parties/cross-border) | met | `docs/compliance/` |
| THREAT_MODEL.md | met | `THREAT_MODEL.md` |

Summary: checklist A–F 18 items: 17 met, 1 changed (D4), 0 not applicable (inside B3 only the
`.dockerignore` part does not apply, because nothing is built with Docker). Project-specific: 10 met.
