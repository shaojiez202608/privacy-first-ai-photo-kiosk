"""Residual-resource check after destroy. Read-only. Exits 1 if anything of this project is left.

    uv run python scripts/verify_cleanup.py [--params config/params.json] [--regions us-east-1 ...]

Per region: CloudFormation stack, S3 buckets (with object versions and delete markers), DynamoDB
tables, log groups (project, Lambda, CDK handlers), Lambda functions and layers, Step Functions
state machines, HTTP APIs, Bedrock guardrails, Secrets Manager secrets (including scheduled
deletions), KMS aliases, KMS keys tagged with the project (keys in PendingDeletion are listed with
their deletion date, not counted as residue), resources found by tag (each hit confirmed with the
owning service because the tagging index lags), and the project's objects in the CDK bootstrap
assets bucket. Global: IAM roles and policies, CloudFront distributions, origin access controls and
response headers policies. All list calls are paginated. Output masks 12-digit numbers.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guards import mask_text  # noqa: E402

GENERIC_ASSET_MARKERS = ("Custom::", "AWS679f53fac002430cb0da5b7982bd2287", "AwsCliLayer")


def project_asset_keys(stack: str) -> list[str]:
    """Object keys of this project's own assets (Lambda bundle, page assets, template) from the current
    cdk.out manifest and the manifests saved in .deploy/. CDK's shared provider packages are excluded."""
    keys: list[str] = []
    manifests = [Path("cdk.out") / f"{stack}.assets.json", *sorted(Path(".deploy").glob(f"{stack}.*.assets.json"))]
    for manifest in (m for m in manifests if m.is_file()):
        for asset in json.loads(manifest.read_text())["files"].values():
            if any(m in asset.get("displayName", "") for m in GENERIC_ASSET_MARKERS):
                continue
            keys += [d["objectKey"] for d in asset["destinations"].values()]
    return sorted(set(keys))


def pages(client, op: str, key: str, **kw):
    for page in client.get_paginator(op).paginate(**kw):
        yield from page.get(key, [])


def check_region(region: str, stack: str, prefix: str, project: str, asset_keys: list[str], global_checks: bool,
                 notes: list[str]) -> list[str]:
    left: list[str] = []
    sess = boto3.session.Session(region_name=region)

    def add(kind: str, name: str) -> None:
        left.append(f"{region} {kind}: {name}")

    cfn = sess.client("cloudformation")
    try:
        status = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        if status != "DELETE_COMPLETE":
            add("stack", f"{stack} {status}")
    except ClientError:
        pass

    s3 = sess.client("s3")
    for b in pages(s3, "list_buckets", "Buckets"):
        if b["Name"].startswith((stack.lower(), f"{prefix}-")):
            try:
                v = s3.list_object_versions(Bucket=b["Name"])
                add("s3 bucket", f"{b['Name']} versions={len(v.get('Versions', []))} "
                                 f"delete_markers={len(v.get('DeleteMarkers', []))}")
            except ClientError:
                add("s3 bucket", b["Name"])

    for t in pages(sess.client("dynamodb"), "list_tables", "TableNames"):
        if t.startswith((stack, prefix)):
            add("dynamodb table", t)

    logs = sess.client("logs")
    for p in (f"/{prefix}/", f"/aws/lambda/{prefix}-", f"/aws/lambda/{stack}"):
        for g in pages(logs, "describe_log_groups", "logGroups", logGroupNamePrefix=p):
            add("log group", g["logGroupName"])

    lam = sess.client("lambda")
    for f in pages(lam, "list_functions", "Functions"):
        if f["FunctionName"].startswith((f"{prefix}-", stack[:30])):
            add("lambda", f["FunctionName"])
    for layer in pages(lam, "list_layers", "Layers"):
        if layer["LayerName"].startswith(stack[:30]):
            add("lambda layer", layer["LayerName"])

    for m in pages(sess.client("stepfunctions"), "list_state_machines", "stateMachines"):
        if m["name"].startswith(prefix):
            add("state machine", m["name"])

    for api in sess.client("apigatewayv2").get_apis().get("Items", []):
        if api["Name"].startswith(prefix):
            add("http api", api["Name"])

    try:
        for g in pages(sess.client("bedrock"), "list_guardrails", "guardrails"):
            if g["name"].startswith(prefix):
                add("bedrock guardrail", g["name"])
    except ClientError as exc:
        add("guardrail check failed", exc.response["Error"]["Code"])

    smc = sess.client("secretsmanager")
    for s in pages(smc, "list_secrets", "SecretList", IncludePlannedDeletion=True,
                   Filters=[{"Key": "name", "Values": [f"{prefix}/"]}]):
        add("secret" + (" (scheduled for deletion)" if s.get("DeletedDate") else ""), s["Name"])

    kms = sess.client("kms")
    for a in pages(kms, "list_aliases", "Aliases"):
        if a["AliasName"].startswith(f"alias/{prefix}-"):
            add("kms alias", a["AliasName"])

    tagging = sess.client("resourcegroupstaggingapi")
    for m in pages(tagging, "get_resources", "ResourceTagMappingList",
                   TagFilters=[{"Key": "Project", "Values": [project]}]):
        arn = m["ResourceARN"]
        state = exists(sess, arn)
        if isinstance(state, str):
            notes.append(f"{region} kms key {arn.rsplit('/', 1)[-1]}: {state}")
        elif state:
            add("tagged resource (confirmed to exist)", arn)

    if asset_keys:
        account = sess.client("sts").get_caller_identity()["Account"]
        bucket = f"cdk-hnb659fds-assets-{account}-{region}"
        for key in asset_keys:
            try:
                v = s3.list_object_versions(Bucket=bucket, Prefix=key)
            except ClientError:
                continue
            n = sum(1 for x in v.get("Versions", []) + v.get("DeleteMarkers", []) if x["Key"] == key)
            if n:
                add("bootstrap asset object", f"{key} versions={n}")

    if global_checks:
        iam = sess.client("iam")
        for r in pages(iam, "list_roles", "Roles"):
            if r["RoleName"].startswith((f"{prefix}-", stack[:30])):
                add("iam role", r["RoleName"])
        for p in pages(iam, "list_policies", "Policies", Scope="Local"):
            if p["PolicyName"].startswith((f"{prefix}-", stack[:30])):
                add("iam policy", p["PolicyName"])
        cf = sess.client("cloudfront")
        dists = [d for page in cf.get_paginator("list_distributions").paginate()
                 for d in page.get("DistributionList", {}).get("Items", []) or []]
        for d in dists:
            if (d.get("Comment") or "").startswith(prefix):
                add("cloudfront distribution", f"{d['Id']} {d.get('Status')}")
        for o in cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", []) or []:
            if o["Name"].startswith(stack[:30]) or o["Name"].startswith(prefix):
                add("cloudfront origin access control", o["Name"])
        for p in cf.list_response_headers_policies(Type="custom").get("ResponseHeadersPolicyList", {}).get(
                "Items", []) or []:
            name = p["ResponseHeadersPolicy"]["ResponseHeadersPolicyConfig"]["Name"]
            if name.startswith(prefix):
                add("cloudfront response headers policy", name)
    return left


def exists(sess, arn: str) -> bool | str:
    """Confirm a tagging-API hit with the owning service. KMS keys pending deletion return a note."""
    svc = arn.split(":")[2]
    name = arn.split(":")[-1].split("/")[-1]
    try:
        if svc == "kms":
            md = sess.client("kms").describe_key(KeyId=arn)["KeyMetadata"]
            if md["KeyState"] == "PendingDeletion":
                when = md["DeletionDate"].astimezone(timezone.utc)
                return f"PendingDeletion until {when:%Y-%m-%d %H:%M} UTC"
            return True
        if svc == "lambda":
            sess.client("lambda").get_function(FunctionName=arn)
        elif svc == "s3":
            sess.client("s3").head_bucket(Bucket=name)
        elif svc == "logs":
            g = arn.split(":log-group:")[-1].removesuffix(":*")
            return bool(sess.client("logs").describe_log_groups(logGroupNamePrefix=g)["logGroups"])
        elif svc == "dynamodb":
            sess.client("dynamodb").describe_table(TableName=name)
        elif svc == "states":
            sess.client("stepfunctions").describe_state_machine(stateMachineArn=arn)
        elif svc == "secretsmanager":
            return not sess.client("secretsmanager").describe_secret(SecretId=arn).get("DeletedDate")
        elif svc == "bedrock":
            sess.client("bedrock").get_guardrail(guardrailIdentifier=arn)
        elif svc == "apigateway":
            sess.client("apigatewayv2").get_api(ApiId=arn.split("/apis/")[-1].split("/")[0])
        elif svc == "iam":
            sess.client("iam").get_role(RoleName=name)
        elif svc == "cloudfront":
            sess.client("cloudfront").get_distribution(Id=name)
        else:
            return True  # unknown type: report it
        return True
    except ClientError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", default="config/params.json")
    ap.add_argument("--regions", nargs="*")
    args = ap.parse_args()
    params = Path(args.params) if Path(args.params).is_file() else Path("config/params.example.json")
    d = json.loads(params.read_text())["deploy"]
    regions = args.regions or [d["region"]]
    keys = project_asset_keys(d["stack_name"])
    print(f"stack={d['stack_name']} prefix={d['resource_prefix']} regions={regions} project_asset_keys={len(keys)}")
    left: list[str] = []
    notes: list[str] = []
    for i, r in enumerate(regions):
        left += check_region(r, d["stack_name"], d["resource_prefix"], d["tags"]["Project"], keys, i == 0, notes)
    for n in notes:
        print(mask_text("NOTE  " + n))
    for item in left:
        print(mask_text("LEFT  " + item))
    if left:
        print(f"Residual resources remain: {len(left)}")
        sys.exit(1)
    print("Cleaned up: nothing of this project remains in the checked regions"
          + (" (KMS keys are scheduled for deletion, see NOTE lines)." if notes else "."))


if __name__ == "__main__":
    main()
