"""AWS adapters against service models: moto (S3 versioning, DynamoDB, KMS, Step Functions) and
botocore Stubber (Bedrock runtime). The end-to-end test runs the real service code on moto and
checks that no object version or delete marker is left after deletion."""

import base64
import io
import json

import boto3
import pytest
from botocore.stub import ANY, Stubber
from conftest import consent
from moto import mock_aws
from PIL import Image

from kiosk import aws, ids
from kiosk.audit import AuditLogger, MemoryAuditSink
from kiosk.local import InlineInvoker, MockModeration, TransformModel
from kiosk.ports import DownstreamError, SessionRecord
from kiosk.service import KioskService

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",  # pragma: allowlist secret
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def aws_env():
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="kiosk-images")
        s3.put_bucket_versioning(Bucket="kiosk-images", VersioningConfiguration={"Status": "Enabled"})
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(TableName="sessions", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "session_ref", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "session_ref", "AttributeType": "S"}])
        kms = boto3.client("kms", region_name=REGION)
        key = kms.create_key(KeySpec="HMAC_256", KeyUsage="GENERATE_VERIFY_MAC")["KeyMetadata"]["Arn"]
        sfn = boto3.client("stepfunctions", region_name=REGION)
        iam = boto3.client("iam", region_name=REGION)
        role = iam.create_role(RoleName="sfn", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        sm = sfn.create_state_machine(name="proto-kiosk-session-expiry", roleArn=role, definition=json.dumps(
            {"StartAt": "W", "States": {"W": {"Type": "Wait", "SecondsPath": "$.wait_seconds", "End": True}}}))
        yield {"s3": s3, "ddb": ddb, "kms": kms, "key": key, "sfn": sfn, "sm": sm["stateMachineArn"]}


def _versions(s3, prefix=""):
    v = s3.list_object_versions(Bucket="kiosk-images", Prefix=prefix)
    return len(v.get("Versions", [])) + len(v.get("DeleteMarkers", []))


# ------------------------------------------------------------------ S3


def test_s3_purge_removes_versions_and_markers_only_in_prefix(aws_env):
    s3 = aws_env["s3"]
    store = aws.S3ObjectStore(s3, "kiosk-images")
    a, b = "s/" + "a" * 32 + "/", "s/" + "b" * 32 + "/"
    for i in range(3):
        store.put(a + "result.png", f"v{i}".encode(), "image/png")
    store.put(a + "input.png", b"in", "image/png")
    s3.delete_object(Bucket="kiosk-images", Key=a + "input.png")  # delete marker on top of a version
    store.put(b + "result.png", b"other", "image/png")
    assert _versions(s3, a) == 5  # 3 + 1 versions, 1 delete marker
    r = store.purge_prefix(a)
    assert (r.versions_deleted, r.delete_markers_deleted, r.remaining_versions) == (4, 1, 0)
    assert _versions(s3, a) == 0
    assert _versions(s3, b) == 1  # another session is untouched


def test_s3_purge_key_exact(aws_env):
    s3 = aws_env["s3"]
    store = aws.S3ObjectStore(s3, "kiosk-images")
    p = "s/" + "c" * 32 + "/"
    store.put(p + "input.png", b"1", "image/png")
    store.put(p + "input.png", b"2", "image/png")
    store.put(p + "input.png.keep", b"x", "image/png")
    r = store.purge_key(p + "input.png")
    assert r.versions_deleted == 2 and r.remaining_versions == 0
    assert _versions(s3, p) == 1


def test_s3_purge_paginates_and_batches(aws_env):
    s3 = aws_env["s3"]
    store = aws.S3ObjectStore(s3, "kiosk-images")
    p = "s/" + "d" * 32 + "/"
    for i in range(1105):  # more than one list page and more than one DeleteObjects batch
        s3.put_object(Bucket="kiosk-images", Key=f"{p}obj{i % 7}.png", Body=b"x")
    r = store.purge_prefix(p)
    assert r.versions_deleted == 1105 and r.remaining_versions == 0 and _versions(s3, p) == 0


def test_s3_purge_refuses_bucket_wide_prefix(aws_env):
    store = aws.S3ObjectStore(aws_env["s3"], "kiosk-images")
    for bad in ("", "s", "s/"):
        with pytest.raises(ValueError):
            store.purge_prefix(bad)


def test_s3_presign_is_sigv4_and_short(aws_env):
    store = aws.S3ObjectStore(aws.regional_s3_client(REGION), "kiosk-images")
    url = store.presign_get("s/x/result.png", 120)
    assert url.startswith(f"https://kiosk-images.s3.{REGION}.amazonaws.com/")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url and "X-Amz-Expires=120" in url


def test_default_client_would_presign_sigv2_negative_control(aws_env):
    """Why the regional client exists: a default client in us-east-1 presigns with SigV2, which the
    bucket policy (DenyNonSigV4) refuses and which ignores s3:signatureAge."""
    url = aws.S3ObjectStore(boto3.client("s3", region_name=REGION), "kiosk-images").presign_get("k", 60)
    assert "X-Amz-Algorithm" not in url


def test_s3_errors_become_codes(aws_env):
    store = aws.S3ObjectStore(aws_env["s3"], "missing-bucket")
    with pytest.raises(DownstreamError) as e:
        store.get("k")
    assert e.value.code == "NoSuchBucket"


# ------------------------------------------------------------------ DynamoDB, KMS, Step Functions


def _rec(ref="e" * 32):
    return SessionRecord(session_ref=ref, secret_hash="h" * 64, consent_receipt_id="r", notice_version="v1",
                         notice_sha256="0" * 64, consented_at=1, expires_at=2, retention_seconds=60)


def test_dynamo_store(aws_env):
    st = aws.DynamoSessionStore(aws_env["ddb"], "sessions")
    st.put_new(_rec(), ttl=99)
    with pytest.raises(DownstreamError):
        st.put_new(_rec(), ttl=99)  # no overwrite of an existing session
    assert st.get("e" * 32).status == "consented"
    assert st.update("e" * 32, expect_status="consented", status="processing", style="comic")
    assert not st.update("e" * 32, expect_status="consented", status="processing")  # state machine guard
    assert not st.update("f" * 32, status="ready")
    item = aws_env["ddb"].get_item(TableName="sessions", Key={"session_ref": {"S": "e" * 32}})["Item"]
    assert item["ttl"] == {"N": "99"}
    assert st.delete("e" * 32) and not st.delete("e" * 32)
    assert st.get("e" * 32) is None


def test_kms_mac_signer(aws_env):
    sig = aws.KmsMacSigner(aws_env["kms"], aws_env["key"])
    payload = {"type": "deletion", "receipt_id": "x", "remaining_versions": 0}
    mac = sig.sign(payload)
    assert sig.verify(payload, mac)
    assert not sig.verify({**payload, "remaining_versions": 1}, mac)


def test_step_functions_schedule_and_cancel(aws_env):
    sch = aws.StepFunctionsScheduler(aws_env["sfn"], aws_env["sm"])
    ref = "a1" * 16
    sch.schedule_expiry(ref, 60)
    ex = aws_env["sfn"].list_executions(stateMachineArn=aws_env["sm"])["executions"]
    assert ex[0]["name"] == f"exp-{ref}"
    assert json.loads(aws_env["sfn"].describe_execution(executionArn=ex[0]["executionArn"])["input"]) == {
        "session_ref": ref, "wait_seconds": 60}
    assert sch.cancel_expiry(ref)


# ------------------------------------------------------------------ Bedrock (Stubber)


USAGE = {k: 0 for k in ("topicPolicyUnits", "contentPolicyUnits", "wordPolicyUnits",
                        "sensitiveInformationPolicyUnits", "sensitiveInformationPolicyFreeUnits",
                        "contextualGroundingPolicyUnits")}


def _runtime():
    return boto3.client("bedrock-runtime", region_name=REGION)


def test_guardrail_allowed_and_blocked():
    rt = _runtime()
    mod = aws.GuardrailModeration(rt, "gid", "1")
    with Stubber(rt) as st:
        st.add_response("apply_guardrail", {"usage": USAGE, "action": "NONE", "outputs": [], "assessments": []},
                        {"guardrailIdentifier": "gid", "guardrailVersion": "1", "source": "INPUT", "content": ANY})
        st.add_response("apply_guardrail", {"usage": USAGE, "action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "Blocked"}],
                                            "assessments": [{"contentPolicy": {"filters": [
                                                {"type": "VIOLENCE", "confidence": "HIGH", "action": "BLOCKED"},
                                                {"type": "INSULTS", "confidence": "LOW", "action": "NONE"}]}}]},
                        {"guardrailIdentifier": "gid", "guardrailVersion": "1", "source": "OUTPUT", "content": ANY})
        assert mod.check(b"png", "input").allowed
        r = mod.check(b"png", "output")
        assert not r.allowed and r.categories == ("VIOLENCE",)


def test_guardrail_error_fails_closed():
    rt = _runtime()
    with Stubber(rt) as st:
        st.add_client_error("apply_guardrail", "ThrottlingException", "slow down: user content echo")
        with pytest.raises(DownstreamError) as e:
            aws.GuardrailModeration(rt, "gid", "1").check(b"png", "input")
    assert e.value.code == "ThrottlingException" and "echo" not in str(e.value)


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "#123456").save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize("family", ["control-structure", "sd35-image-to-image"])
def test_bedrock_model_request_shapes(family):
    m = aws.BedrockImageModel(_runtime(), "model", family, 0.7)
    body = m.request_body(_png(), "comic")
    assert body["output_format"] == "png" and "comic book" in body["prompt"]
    if family == "control-structure":
        assert body["control_strength"] == 0.7 and body["style_preset"] == "comic-book"
    else:
        assert body["mode"] == "image-to-image" and body["strength"] == 0.7


def test_bedrock_model_invoke_success():
    rt = _runtime()
    img = _png()
    with Stubber(rt) as st:
        st.add_response("invoke_model", {"body": io.BytesIO(json.dumps(
            {"seeds": [1], "finish_reasons": [None], "images": [base64.b64encode(img).decode()]}).encode()),
            "contentType": "application/json"}, {"modelId": "m", "contentType": "application/json",
                                                 "accept": "application/json", "body": ANY})
        assert aws.BedrockImageModel(rt, "m", "control-structure", 0.7).stylize(img, "pixel") == img


@pytest.mark.parametrize("raw,code", [
    (b'{"finish_reasons":["Filter reason: input image"]}', "model_filtered_input"),
    (b'{"finish_reasons":["Filter reason: output image"]}', "model_filtered_output"),
    (b'{"finish_reasons":["something new and free text"]}', "model_finish_reason_unknown"),
    (b'{"finish_reasons":[null],"images":[]}', "model_response_no_image"),
    (b'{"images":["@@@not base64"]}', "model_response_bad_base64"),
    (b'{"images":["AAAA"], "seeds":[NaN]}', "model_response_not_json"),
    (b'{"images":["AAAA"], "x": Infinity}', "model_response_not_json"),
    (b"[1,2,3]", "model_response_not_object"),
    (b"<html>", "model_response_not_json"),
])
def test_model_response_validation(raw, code):
    with pytest.raises(DownstreamError) as e:
        aws.parse_model_response(raw)
    assert e.value.code == code


def test_model_response_too_large():
    with pytest.raises(DownstreamError) as e:
        aws.parse_model_response(b" " * (aws.MAX_RESPONSE_BYTES + 1))
    assert e.value.code == "model_response_too_large"


# ------------------------------------------------------------------ service on moto


def test_service_on_moto_delete_and_expiry_leave_nothing(aws_env, settings, b64):
    sink = MemoryAuditSink()
    inv = InlineInvoker()
    svc = KioskService(settings, sessions=aws.DynamoSessionStore(aws_env["ddb"], "sessions"),
                       objects=aws.S3ObjectStore(aws.regional_s3_client(REGION), "kiosk-images"),
                       scheduler=aws.StepFunctionsScheduler(aws_env["sfn"], aws_env["sm"]),
                       moderation=MockModeration(), model=TransformModel(),
                       signer=aws.KmsMacSigner(aws_env["kms"], aws_env["key"]), invoker=inv,
                       audit=AuditLogger(sink, region=REGION, kiosk_id=settings.kiosk_id, strict=True))
    inv.target = svc.process
    s3 = aws_env["s3"]

    # manual deletion
    _, body = consent(svc)
    sid, sec = body["session_id"], body["session_secret"]
    assert svc.submit_photo(sid, sec, {"image_base64": b64("landscape.png"), "style": "comic"})[0] == 202
    st = svc.status(sid, sec)[1]
    assert st["status"] == "ready" and "X-Amz-Signature" in st["share"]["url"]
    prefix = ids.object_prefix(ids.session_ref(sid))
    assert _versions(s3, prefix + "input.png") == 0 and _versions(s3, prefix) == 1
    code, d = svc.delete(sid, sec)
    assert code == 200 and d["receipt"]["remaining_versions"] == 0
    assert svc.signer.verify(d["receipt"], d["receipt"]["mac"])
    assert _versions(s3, prefix) == 0
    assert aws_env["ddb"].scan(TableName="sessions")["Count"] == 0

    # expiry path (what the workflow's purge Lambda calls)
    _, body = consent(svc)
    sid2, sec2 = body["session_id"], body["session_secret"]
    svc.submit_photo(sid2, sec2, {"image_base64": b64("blocks.png"), "style": "pixel"})
    out = svc.expire(ids.session_ref(sid2))
    assert out["status"] == "deleted"
    assert _versions(s3) == 0
    names = [json.loads(x)["event"] for x in sink.lines]
    assert "session_deleted" in names and "session_expired" in names
