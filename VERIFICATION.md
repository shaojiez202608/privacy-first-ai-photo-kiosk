# Verification

What was actually run, with results. Not legal advice. Account ids masked. Results for the final
commit are filled in by the final run (section "Final run").

## Local (no AWS)

Run on 2026-10-21, macOS arm64, Python 3.13 venv (uv), Node 24, CDK CLI 2.x via npx.

| Check | Command | Result |
|---|---|---|
| Lint | `make lint` | see final run |
| Tests | `make test` | see final run |
| Offline demo D1–D6 | `make demo-offline` | see final run |
| Synth + cdk-nag | `make synth`, `make synth-regions` | see final run |

## Cloud

Cycle 1 (commit `642b4e6`): see DEPLOYMENT-LOG.md.

## Final run

Pending.
