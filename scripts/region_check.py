"""Region pinning check on a synthesized template (D6). Read-only, no AWS calls.

    uv run python scripts/region_check.py --template cdk.out/<stack>.template.json --params <file> [--region R]

Reports:
* every resource type and whether it is regional or global;
* every ARN or endpoint in the template that names another region or a region wildcard;
* the cross-region call list derived from the parameters (cross-Region inference profile).
Exits 1 if anything regional points outside the target region without being on the declared list.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from kiosk_infra.params import is_cross_region_profile  # noqa: E402

GLOBAL_TYPES = {
    "AWS::CloudFront::Distribution": "global edge service; serves the static kiosk page only (no images, no session data)",
    "AWS::CloudFront::OriginAccessControl": "CloudFront configuration object",
    "AWS::CloudFront::ResponseHeadersPolicy": "CloudFront configuration object",
    "AWS::IAM::Role": "IAM is a global service; roles hold no customer data",
    "AWS::IAM::Policy": "IAM is a global service; policies hold no customer data",
}
REGION_IN_STRING = re.compile(r"\b(?:[a-z]{2}(?:-gov)?-[a-z]+-\d)\b")


def _strings(node, path=""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


def check(template: dict, params: dict, region: str) -> dict:
    resources = template.get("Resources", {})
    types: dict[str, int] = {}
    for r in resources.values():
        types[r["Type"]] = types.get(r["Type"], 0) + 1
    findings = []
    for lid, r in resources.items():
        for path, s in _strings(r.get("Properties", {})):
            for reg in set(REGION_IN_STRING.findall(s)):
                if reg != region:
                    findings.append({"resource": lid, "path": path, "issue": f"names region {reg}"})
            if re.search(r"arn:[^:\"]*:[a-z0-9-]+:\*:", s):
                findings.append({"resource": lid, "path": path, "issue": "ARN with region wildcard",
                                 "bedrock_foundation_model": ":bedrock:*::foundation-model/" in s})
    m = params["image_model"]
    declared = []
    if m["provider"] == "bedrock" and is_cross_region_profile(m["model_id"]):
        declared.append({"call": "bedrock:InvokeModel", "via": m["model_id"],
                         "data": "input image (re-encoded, no metadata) and fixed style prompt",
                         "where": "any region of the profile's geography (list: aws bedrock get-inference-profile)",
                         "enabled_by": "image_model.allow_cross_region_inference=true"})
    # The only accepted exception: the foundation-model ARN behind a declared inference profile.
    unexplained = [f for f in findings if not (declared and f.get("bedrock_foundation_model"))]
    return {
        "target_region": region,
        "resource_types": {t: ("global: " + GLOBAL_TYPES[t]) if t in GLOBAL_TYPES else "regional" for t in sorted(types)},
        "cross_region_calls": declared or "none",
        "template_findings": findings,
        "unexplained_findings": unexplained,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--params", required=True)
    ap.add_argument("--region")
    args = ap.parse_args()
    params = json.loads(Path(args.params).read_text())
    region = args.region or params["deploy"]["region"]
    report = check(json.loads(Path(args.template).read_text()), params, region)
    print(json.dumps(report, indent=2))
    sys.exit(1 if report["unexplained_findings"] else 0)


if __name__ == "__main__":
    main()
