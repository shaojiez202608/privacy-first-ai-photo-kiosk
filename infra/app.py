"""CDK app entry. All parameters come from one file (default config/params.json, created from
config/params.example.json by `make init`). The account comes from the environment
(CDK_DEFAULT_ACCOUNT), never from the file. Use `make deploy` so preflight and the
destructive-change guard run first.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kiosk_infra import params as params_check  # noqa: E402
from kiosk_infra.stack import KioskStack  # noqa: E402

app = cdk.App()
params_path = ROOT / (app.node.try_get_context("params") or "config/params.json")
if not params_path.is_file():
    sys.exit(f"{params_path} not found. Run `make init` or pass -c params=<file>.")
params = json.loads(params_path.read_text(encoding="utf-8"))
region_override = app.node.try_get_context("region")
if region_override:  # used by `make synth-regions` to prove the same stack synthesises elsewhere
    params["deploy"]["region"] = region_override
params_check.validate(params)  # fails synth before any construct is created
d = params["deploy"]
stack = KioskStack(app, d["stack_name"], params=params,
                   env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=d["region"]),
                   description="Prototype: privacy-first AI photo kiosk (consent, temporary processing, deletion)")
for k, v in d["tags"].items():
    cdk.Tags.of(app).add(k, v)
cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))
app.synth()
