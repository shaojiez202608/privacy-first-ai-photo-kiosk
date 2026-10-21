# Contributing

Thank you for your interest in this prototype. Bug reports, fixes and improvements are welcome.

## Before you open a pull request
- Every change needs tests. Run `make lint test synth synth-regions demo-offline` and, for changes
  to scripts or CI, `make scan`. The PR template asks which checks you ran and what was not tested.
- Changes that touch data handling must keep the deletion tests passing and update
  `docs/compliance/` (data inventory, retention and deletion, third parties and cross-border).
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit subjects and explain
  what changed, why, and how you verified it in the body.
- Do not commit AWS account ids (use `111122223333` in examples), credentials, real parameter files,
  screenshots of real environments, or any image of a person. Sample data must be synthetic.
- If you use an AI assistant, you must be able to explain every line it wrote. See `AGENTS.md`.

## Reporting security issues
See [SECURITY.md](SECURITY.md). Do not open public issues for security problems.

## Licensing
By submitting a contribution you confirm that you have the right to do so and that it is
provided under the [MIT-0](LICENSE) license of this repository.

---
This document does not constitute legal advice.
