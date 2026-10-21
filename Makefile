# Single entry point. Targets used by CI (lint, test, synth, synth-regions, demo-offline, scan-ci) only
# check and never change AWS resources. Targets that call AWS say so.
PARAMS ?= config/params.json
PY := uv run python
CDK := npx -y aws-cdk@2

.PHONY: help init bundle samples preflight lint test synth synth-regions demo-offline local deploy verify destroy verify-cleanup scan scan-ci kiosk-token

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/'

init: ## Install dependencies; create config/params.json from the example if missing
	uv sync
	@[ -f config/params.json ] || { cp config/params.example.json config/params.json; echo "created config/params.json"; }

bundle: ## Build the linux/arm64 Lambda bundle (no Docker)
	./scripts/build_bundle.sh

samples: ## Generate the synthetic sample images (git-ignored)
	$(PY) scripts/gen_samples.py

preflight: init ## Read-only: confirm account (last 4 digits) and region, tools, service availability (AWS)
	./scripts/preflight.sh $(PARAMS)

lint: ## ruff (check only)
	uv run ruff check .

test: ## Unit and integration tests (in-memory, moto, Stubber; no AWS)
	uv run pytest

synth: bundle ## cdk synth + cdk-nag with the example parameters (no AWS credentials needed)
	$(CDK) synth -q -c params=config/params.example.json
	$(CDK) synth -q -c params=config/params.handover.example.json -o cdk.out-handover
	$(CDK) synth -q -c params=config/params.cris-bedrock.example.json -o cdk.out-cris

synth-regions: bundle ## D6: the same stack synthesised for us-east-1 and us-west-2, then the region check
	@for r in us-east-1 us-west-2; do \
	  $(CDK) synth -q -c params=config/params.example.json -c region=$$r -o cdk.out-regions/$$r || exit 1; \
	  $(PY) scripts/region_check.py --template cdk.out-regions/$$r/proto-kiosk-sandbox.template.json \
	    --params config/params.example.json --region $$r > cdk.out-regions/$$r/region-check.json || exit 1; \
	  echo "$$r: region check passed (cdk.out-regions/$$r/region-check.json)"; \
	done

demo-offline: samples ## D1-D6 in local mode with a summary (no AWS)
	./scripts/demo.sh --offline

local: ## Kiosk page and API on http://127.0.0.1:8080 with in-memory adapters (no AWS)
	$(PY) scripts/local_server.py --params config/params.example.json

deploy: init ## Preflight, refuse deletions/replacements unless ALLOW_DESTRUCTIVE=1, then deploy (AWS)
	./scripts/deploy.sh $(PARAMS)

verify: ## End-to-end check of the deployed stack, D1-D5, denial paths, logs (AWS)
	$(PY) scripts/e2e_aws.py --params $(PARAMS) --report out/e2e_report.json

kiosk-token: ## Print the kiosk device token for the browser setup screen (AWS; shown on screen only)
	@aws secretsmanager get-secret-value --region $$(python3 -c 'import json;print(json.load(open("$(PARAMS)"))["deploy"]["region"])') \
	  --secret-id $$(python3 -c 'import json;print(json.load(open("$(PARAMS)"))["deploy"]["resource_prefix"])')/kiosk-token \
	  --query SecretString --output text

destroy: ## Destroy the stack and remove leftovers, with confirmations (AWS)
	./scripts/cleanup.sh $(PARAMS)

verify-cleanup: ## Read-only residual check; exits non-zero if anything is left (AWS)
	./scripts/verify_cleanup.sh --params $(PARAMS)

scan-ci: ## Secret scan of the full history (gitleaks) and the working tree (detect-secrets)
	gitleaks git . --config .gitleaks.toml --no-banner --redact
	uvx detect-secrets scan --exclude-files '(\.venv|build|cdk\.out.*|out|uv\.lock|sample-data/images|\.pytest_cache|\.ruff_cache)/' \
	  | python3 -c 'import json,sys; r=json.load(sys.stdin)["results"]; print("detect-secrets findings:", len(r)); sys.exit(1 if r else 0)'

scan: scan-ci ## Local scans: canary check, workflow lint, dependency audit
	./scripts/secret_canary.sh
	uvx zizmor .github/workflows
	osv-scanner scan --lockfile uv.lock
