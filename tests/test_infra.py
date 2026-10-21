"""Synthesis in-process (no AWS credentials needed): cdk-nag, privacy controls in the template, and
region pinning for two regions with the same code."""

import copy
import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from cdk_nag import AwsSolutionsChecks
from kiosk_infra.params import validate
from kiosk_infra.stack import KioskStack
from region_check import check as region_check

ROOT = Path(__file__).resolve().parents[1]


def synth(tmp, params, region=None):
    bundle = tmp / "bundle"
    (bundle / "kiosk").mkdir(parents=True, exist_ok=True)
    (bundle / "kiosk" / "service.py").write_text("")
    p = copy.deepcopy(params)
    if region:
        p["deploy"]["region"] = region
    validate(p)
    out = tmp / f"cdk.out-{p['deploy']['region']}"
    app = cdk.App(outdir=str(out))
    stack = KioskStack(app, p["deploy"]["stack_name"], params=p, bundle_dir=str(bundle),
                       env=cdk.Environment(region=p["deploy"]["region"]))
    for k, v in p["deploy"]["tags"].items():
        cdk.Tags.of(app).add(k, v)
    cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))
    app.synth()
    report = json.loads((out / "validation-report.json").read_text())
    nag = next(r for r in report["pluginReports"] if r["pluginName"] == "AwsSolutions")
    return p, Template.from_stack(stack), nag, json.loads((out / f"{p['deploy']['stack_name']}.template.json").read_text())


def load(name):
    return json.loads((ROOT / "config" / name).read_text())


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    return synth(tmp_path_factory.mktemp("sb"), load("params.example.json"))


CASES = [("params.example.json", None), ("params.example.json", "us-west-2"),
         ("params.handover.example.json", None), ("params.cris-bedrock.example.json", None)]


@pytest.mark.parametrize("name,region", CASES)
def test_nag_clean_and_region_pinned(tmp_path, name, region):
    p, _, nag, raw = synth(tmp_path, load(name), region)
    assert nag["conclusion"] == "success", nag.get("violations")
    for sv in nag.get("suppressedViolations", []):
        assert len(sv["reason"]) > 30
    rc = region_check(raw, p, p["deploy"]["region"])
    assert rc["unexplained_findings"] == []
    if p["image_model"]["allow_cross_region_inference"]:
        assert rc["cross_region_calls"] != "none"
    else:
        assert rc["cross_region_calls"] == "none"


def test_other_prefix_still_clean(tmp_path):
    p = load("params.example.json")
    p["deploy"].update(resource_prefix="acme-booth", stack_name="acme-booth-dev")
    p["session"]["share_link_ttl_seconds"] = 60
    _, t, nag, _ = synth(tmp_path, p)
    assert nag["conclusion"] == "success"
    t.has_resource_properties("AWS::Lambda::Function", {"FunctionName": "acme-booth-api"})


def test_lambdas_arm64_python314(sandbox):
    _, t, _, raw = sandbox
    own = {k: v for k, v in raw["Resources"].items() if v["Type"] == "AWS::Lambda::Function"
           and str(v["Properties"].get("FunctionName", "")).startswith("proto-kiosk-")}
    assert len(own) == 4
    for v in own.values():
        assert v["Properties"]["Architectures"] == ["arm64"] and v["Properties"]["Runtime"] == "python3.14"
        assert v["Properties"]["LoggingConfig"]["LogFormat"] == "JSON"


def test_image_bucket_controls(sandbox):
    _, t, _, raw = sandbox
    t.has_resource_properties("AWS::S3::Bucket", {
        "VersioningConfiguration": {"Status": "Enabled"},
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                           "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
        "BucketEncryption": {"ServerSideEncryptionConfiguration": [Match.object_like({
            "ServerSideEncryptionByDefault": Match.object_like({"SSEAlgorithm": "aws:kms"}), "BucketKeyEnabled": True})]},
        "LifecycleConfiguration": {"Rules": Match.array_with([Match.object_like({
            "Prefix": "s/", "ExpirationInDays": 1, "NoncurrentVersionExpiration": {"NoncurrentDays": 1}})])},
    })
    policies = json.dumps([v for v in raw["Resources"].values() if v["Type"] == "AWS::S3::BucketPolicy"])
    assert "DenyImageAccessExceptKioskFunctions" in policies
    assert '"s3:signatureAge": "120000"' in policies
    assert "DenyNonSigV4" in policies and "aws:SecureTransport" in policies


def test_signature_age_follows_parameter(tmp_path):
    p = load("params.example.json")
    p["session"]["share_link_ttl_seconds"] = 45
    raw = synth(tmp_path, p)[3]
    assert '"s3:signatureAge": "45000"' in json.dumps(raw)


def test_table_has_ttl_cmk_and_no_pitr(sandbox):
    _, t, _, _ = sandbox
    t.has_resource_properties("AWS::DynamoDB::Table", {
        "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
        "SSESpecification": Match.object_like({"SSEEnabled": True, "SSEType": "KMS"}),
        "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": False}})


def test_log_groups_retention_kms_and_data_protection(sandbox):
    p, t, _, raw = sandbox
    groups = [v for v in raw["Resources"].values() if v["Type"] == "AWS::Logs::LogGroup"]
    assert {g["Properties"]["LogGroupName"] for g in groups} == {
        "/proto-kiosk/audit", "/proto-kiosk/api-access", "/proto-kiosk/workflow", "/proto-kiosk/cdk-handlers"}
    for g in groups:
        pr = g["Properties"]
        assert pr["RetentionInDays"] == p["deploy"]["log_retention_days"] and "KmsKeyId" in pr
        ids = pr["DataProtectionPolicy"]["Statement"][0]["DataIdentifier"]
        assert "arn:aws:dataprotection::aws:data-identifier/EmailAddress" in ids
        assert pr["DataProtectionPolicy"]["Statement"][1]["Operation"] == {"Deidentify": {"MaskConfig": {}}}


def test_access_log_has_no_ip_or_path(sandbox):
    _, t, _, raw = sandbox
    stage = next(v for v in raw["Resources"].values() if v["Type"] == "AWS::ApiGatewayV2::Stage")
    fmt = stage["Properties"]["AccessLogSettings"]["Format"]
    for banned in ("sourceIp", "userAgent", "$context.path", "identity"):
        assert banned not in fmt
    assert stage["Properties"]["DefaultRouteSettings"]["ThrottlingRateLimit"] == 5


def test_every_route_has_authorizer(sandbox):
    _, _, _, raw = sandbox
    routes = [v for v in raw["Resources"].values() if v["Type"] == "AWS::ApiGatewayV2::Route"]
    assert len(routes) == 5
    assert all(r["Properties"]["AuthorizationType"] == "CUSTOM" for r in routes)
    api = next(v for v in raw["Resources"].values() if v["Type"] == "AWS::ApiGatewayV2::Api")
    origins = json.dumps(api["Properties"]["CorsConfiguration"]["AllowOrigins"])
    assert "*" not in origins.replace("\\", "")


def test_two_kms_keys_with_rotation_and_short_window(sandbox):
    _, t, _, _ = sandbox
    t.resource_count_is("AWS::KMS::Key", 2)
    t.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True, "PendingWindowInDays": 7})
    t.has_resource_properties("AWS::KMS::Key", {"KeySpec": "HMAC_256", "KeyUsage": "GENERATE_VERIFY_MAC"})


def test_guardrail_filters_images(sandbox):
    _, t, _, _ = sandbox
    t.has_resource_properties("AWS::Bedrock::Guardrail", {"ContentPolicyConfig": {"FiltersConfig": Match.array_with([
        Match.object_like({"Type": "VIOLENCE", "InputModalities": ["TEXT", "IMAGE"],
                           "OutputModalities": ["TEXT", "IMAGE"]}),
        Match.object_like({"Type": "PROMPT_ATTACK", "OutputStrength": "NONE"})])}})


def test_workflow_role_trust_has_source_conditions(sandbox):
    _, _, _, raw = sandbox
    roles = [v for v in raw["Resources"].values() if v["Type"] == "AWS::IAM::Role"]
    sfn = [r for r in roles if "states.amazonaws.com" in json.dumps(r["Properties"]["AssumeRolePolicyDocument"])]
    cond = sfn[0]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]
    assert "aws:SourceAccount" in cond["StringEquals"] and "aws:SourceArn" in cond["ArnLike"]


def test_no_model_permissions_without_bedrock(sandbox):
    _, _, _, raw = sandbox
    assert "bedrock:InvokeModel" not in json.dumps(raw)


def test_cris_permissions_are_bound_to_profile(tmp_path):
    raw = synth(tmp_path, load("params.cris-bedrock.example.json"))[3]
    text = json.dumps(raw)
    assert "bedrock:InferenceProfileArn" in text and "inference-profile/us.stability" in text


def test_handover_retains_data(tmp_path):
    _, t, _, raw = synth(tmp_path, load("params.handover.example.json"))
    bucket = [v for v in raw["Resources"].values() if v["Type"] == "AWS::S3::Bucket"
              and "LifecycleConfiguration" in v["Properties"]][0]
    assert bucket["DeletionPolicy"] == "Retain"
    assert "foundation-model/stability.sd3-5-large-v1:0" in json.dumps(raw)


def test_no_open_ingress_and_tags(sandbox):
    _, t, _, raw = sandbox
    assert "0.0.0.0/0" not in json.dumps(raw)
    t.has_resource_properties("AWS::DynamoDB::Table", {"Tags": Match.array_with([
        {"Key": "Project", "Value": "photo-kiosk"}, {"Key": "Teardown", "Value": "after-test"}])})


def test_lambda_env_has_no_secret_values(sandbox):
    _, _, _, raw = sandbox
    for v in raw["Resources"].values():
        if v["Type"] == "AWS::Lambda::Function":
            env = json.dumps(v["Properties"].get("Environment", {}))
            assert "SecretString" not in env and "password" not in env.lower()
