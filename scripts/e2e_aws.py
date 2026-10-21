"""End-to-end verification against a deployed stack (D1-D5 plus denial paths and log checks).

    uv run python scripts/e2e_aws.py --params config/params.json [--skip-expiry] [--report out/e2e_report.json]

Uses the operator's own credentials only to read the kiosk token, to inspect storage, the session
table, the workflow and the logs, and to verify receipt MACs. Every request to the kiosk goes
through the public API like a kiosk would. Prints PASS/FAIL/INFO lines; exits 1 on any FAIL. The
report masks 12-digit numbers and contains no session ids, secrets or image data.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]
from gen_samples import build  # noqa: E402
from guards import mask_text  # noqa: E402

from kiosk import ids  # noqa: E402
from kiosk.imaging import has_metadata  # noqa: E402

RESULTS: list[dict] = []


def record(kind: str, name: str, ok: bool | None, detail: str = "") -> None:
    tag = "INFO" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append({"result": tag, "check": name, "detail": mask_text(detail)})
    print(f"  [{tag}] {kind} {name}" + (f" -- {mask_text(detail)}" if detail else ""), flush=True)


def http(method: str, url: str, headers: dict | None = None, body: dict | bytes | None = None,
         timeout: int = 40) -> tuple[int, bytes, dict]:
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    h = dict(headers or {})
    if isinstance(body, dict):
        h["Content-Type"] = "application/json"
    if not url.startswith("https://"):
        raise ValueError("only https URLs")
    req = urllib.request.Request(url, data=data, method=method, headers=h)  # noqa: S310 - https only
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https URLs from the stack
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class Kiosk:
    def __init__(self, api: str, token: str) -> None:
        self.api, self.token = api.rstrip("/"), token

    def call(self, method: str, path: str, body=None, secret: str | None = None, token: str | None = None):
        h = {"X-Kiosk-Token": self.token if token is None else token}
        if secret:
            h["X-Session-Secret"] = secret
        if token == "":
            h.pop("X-Kiosk-Token")
        status, raw, _ = http(method, self.api + path, h, body)
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {"raw": raw[:80].decode(errors="replace")}

    def start(self, notice: dict) -> tuple[str, str, dict]:
        s, b = self.call("POST", "/sessions", {"consent": "accepted", "notice_version": notice["notice_version"],
                                              "notice_sha256": notice["notice_sha256"]})
        if s != 201:
            raise SystemExit(f"could not start a session: {s} {b}")
        return b["session_id"], b["session_secret"], b

    def wait(self, sid: str, sec: str, timeout: int = 150) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            s, b = self.call("GET", f"/sessions/{sid}", secret=sec)
            if s != 200 or b.get("status") != "processing":
                return {"http": s, **b}
            time.sleep(2)
        return {"http": 0, "status": "timeout"}


def outputs(cfn, stack: str) -> dict:
    o = cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    return {x["OutputKey"]: x["OutputValue"] for x in o}


def versions(s3, bucket: str, prefix: str) -> int:
    n = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
        n += len(page.get("Versions", [])) + len(page.get("DeleteMarkers", []))
    return n


def audit_events(logs, group: str, since_ms: int, needle: str, wait_s: int = 90) -> list[dict]:
    end = time.time() + wait_s
    while True:
        evs = []
        for page in logs.get_paginator("filter_log_events").paginate(
                logGroupName=group, startTime=since_ms, filterPattern=f'"{needle}"'):
            for e in page.get("events", []):
                msg = _decode(e["message"])
                if isinstance(msg, dict):
                    evs.append(msg)
        if evs or time.time() > end:
            return evs
        time.sleep(5)


def _decode(raw: str):
    """Audit lines are JSON; with Lambda's JSON log format they may be wrapped in a record whose
    "message" holds the original line."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    inner = msg.get("message") if isinstance(msg, dict) else None
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            inner = None
    return inner if isinstance(inner, dict) and inner.get("audit") else msg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", default="config/params.json")
    ap.add_argument("--report", default="out/e2e_report.json")
    ap.add_argument("--skip-expiry", action="store_true")
    ap.add_argument("--only-d1", action="store_true", help="normal flow only (used for the model variant)")
    args = ap.parse_args()
    params = json.loads(Path(args.params).read_text())
    d, sess_p = params["deploy"], params["session"]
    region, stack = d["region"], d["stack_name"]
    b = boto3.session.Session(region_name=region)
    out = outputs(b.client("cloudformation"), stack)
    s3, ddb, sfn, logs, kms = (b.client(x) for x in ("s3", "dynamodb", "stepfunctions", "logs", "kms"))
    bucket, table, group = out["ImageBucketName"], out["SessionTableName"], out["AuditLogGroup"]
    token = b.client("secretsmanager").get_secret_value(SecretId=out["KioskTokenSecretArn"])["SecretString"]
    k = Kiosk(out["ApiUrl"], token)
    samples = {n: base64.b64encode(v[0]).decode() for n, v in build().items()}
    t0 = int(time.time() * 1000)
    seen_secrets: list[str] = []
    print(f"== e2e against {stack} in {region} (model={params['image_model']['provider']}, "
          f"moderation={params['moderation']['provider']}, retention={sess_p['retention_seconds']}s, "
          f"link={sess_p['share_link_ttl_seconds']}s)")

    print("== access control")
    record("AUTH", "no kiosk token -> 401", k.call("GET", "/notice", token="")[0] == 401)
    record("AUTH", "wrong kiosk token -> 403", k.call("GET", "/notice", token="x" * 48)[0] == 403)
    s, notice = k.call("GET", "/notice")
    record("AUTH", "valid kiosk token -> notice", s == 200 and "notice_sha256" in notice)
    if args.only_d1:
        return run_d1(k, notice, samples, s3, bucket, region, sess_p, seen_secrets, b, args, quick=True)
    status, _, _ = http("GET", f"https://{bucket}.s3.{region}.amazonaws.com/s/")
    record("AUTH", "anonymous bucket read refused", status == 403, f"HTTP {status}")

    print("== D2 decline")
    before = versions(s3, bucket, "s/")
    s, body = k.call("POST", "/sessions", {"consent": "declined", "notice_version": notice["notice_version"],
                                           "notice_sha256": notice["notice_sha256"]})
    record("D2", "declined -> 200, no session", s == 200 and "session_id" not in body)
    record("D2", "no object written", versions(s3, bucket, "s/") == before)
    s, _ = k.call("POST", "/sessions", {"consent": "accepted", "notice_version": "old", "notice_sha256": "0" * 64})
    record("D2", "consent to a stale notice refused (409)", s == 409)

    sid, sec, share = run_d1(k, notice, samples, s3, bucket, region, sess_p, seen_secrets, b, args)
    ref = ids.session_ref(sid)

    print("== denial paths")
    record("DENY", "wrong session secret -> 404", k.call("GET", f"/sessions/{sid}", secret="y" * 43)[0] == 404)
    try:
        s3.get_object(Bucket=bucket, Key=f"s/{ref}/result.png")
        record("DENY", "operator GetObject denied by bucket policy", False, "operator could read the image")
    except ClientError as e:
        record("DENY", "operator GetObject denied by bucket policy", e.response["Error"]["Code"] == "AccessDenied")
    tampered = re.sub(r"X-Amz-Signature=[0-9a-f]{4}", "X-Amz-Signature=0000", share["url"])
    record("DENY", "tampered presigned URL refused", http("GET", tampered)[0] == 403)
    s, _ = k.call("POST", f"/sessions/{sid}/photo", {"image_base64": samples["blocks.png"], "style": "comic"}, sec)
    record("DENY", "second photo in one session refused (409)", s == 409)

    print("== D3 delete on request")
    s, body = k.call("DELETE", f"/sessions/{sid}", secret=sec)
    r = body.get("receipt", {})
    record("D3", "delete -> 200 with receipt", s == 200 and r.get("remaining_versions") == 0,
           f"versions_deleted={r.get('versions_deleted')} markers={r.get('delete_markers_deleted')}")
    mac = kms.verify_mac(KeyId=out["ReceiptKeyArn"], MacAlgorithm="HMAC_SHA_256",
                         Message=json.dumps({x: y for x, y in r.items() if x != "mac"}, sort_keys=True,
                                            separators=(",", ":")).encode(), Mac=base64.b64decode(r["mac"]))
    record("D3", "receipt MAC verifies with the regional KMS key", mac["MacValid"])
    record("D3", "no object version or delete marker under the session prefix", versions(s3, bucket, f"s/{ref}/") == 0)
    record("D3", "session record deleted", "Item" not in ddb.get_item(TableName=table, Key={"session_ref": {"S": ref}},
                                                                       ConsistentRead=True))
    ex = sfn.describe_execution(executionArn=out["StateMachineArn"].replace(":stateMachine:", ":execution:")
                                + f":exp-{ref}")["status"]
    record("D3", "expiry workflow stopped", ex == "ABORTED", ex)
    record("D3", "share link stops working after deletion", http("GET", share["url"])[0] in (403, 404))
    evs = audit_events(logs, group, t0, r.get("receipt_id", "-"))
    record("D3", "audit log has session_deleted with the receipt id",
           any(e.get("event") == "session_deleted" for e in evs))

    print("== D5 content filter and embedded instructions")
    for sample in ("threat-text.png", "instruction.png", "unsafe-marker.png"):
        sid5, sec5, _ = k.start(notice)
        seen_secrets += [sid5, sec5]
        s, _ = k.call("POST", f"/sessions/{sid5}/photo", {"image_base64": samples[sample], "style": "comic"}, sec5)
        st = k.wait(sid5, sec5)
        blocked = st.get("status") == "rejected"
        record("D5", f"{sample}: outcome measured", None, f"status={st.get('status')} reason={st.get('reason_code')}")
        if blocked:
            record("D5", f"{sample}: rejected and no share link", "share" not in st)
        record("D5", f"{sample}: input deleted (no version under input.png)",
               versions(s3, bucket, f"s/{ids.session_ref(sid5)}/input.png") == 0)
        k.call("DELETE", f"/sessions/{sid5}", secret=sec5)

    print("== input validation")
    sidv, secv, _ = k.start(notice)
    seen_secrets += [sidv, secv]
    for sample, want in (("tiny.png", 422), ("not-an-image.png", 422), ("animated.gif", 422)):
        s, _ = k.call("POST", f"/sessions/{sidv}/photo", {"image_base64": samples[sample], "style": "comic"}, secv)
        record("VAL", f"{sample} -> {want}", s == want, f"HTTP {s}")
    s, _ = k.call("POST", f"/sessions/{sidv}/photo", {"image_base64": samples["blocks.png"], "style": "free text"}, secv)
    record("VAL", "style outside the enum -> 400", s == 400)
    s, _ = k.call("POST", f"/sessions/{sidv}/photo", {"image_base64": samples["large-noise.png"], "style": "comic"}, secv)
    record("VAL", "oversized upload refused (no 5xx)", s in (400, 413), f"HTTP {s}")
    k.call("DELETE", f"/sessions/{sidv}", secret=secv)

    if not args.skip_expiry:
        print("== D4 expiry (wait for the retention period)")
        sid4, sec4, _ = k.start(notice)
        seen_secrets += [sid4, sec4]
        ref4 = ids.session_ref(sid4)
        k.call("POST", f"/sessions/{sid4}/photo", {"image_base64": samples["blocks.png"], "style": "pixel"}, sec4)
        k.wait(sid4, sec4)
        exec_arn = out["StateMachineArn"].replace(":stateMachine:", ":execution:") + f":exp-{ref4}"
        deadline = time.time() + sess_p["retention_seconds"] + 240
        status = "RUNNING"
        while time.time() < deadline and status == "RUNNING":
            time.sleep(10)
            status = sfn.describe_execution(executionArn=exec_arn)["status"]
        record("D4", "expiry workflow succeeded after the retention period", status == "SUCCEEDED", status)
        record("D4", "no object version left", versions(s3, bucket, f"s/{ref4}/") == 0)
        record("D4", "session record deleted", "Item" not in ddb.get_item(
            TableName=table, Key={"session_ref": {"S": ref4}}, ConsistentRead=True))
        evs = audit_events(logs, group, t0, ref4)
        record("D4", "audit log has session_expired", any(e.get("event") == "session_expired" for e in evs))

    print("== logs")
    canary_stream = f"e2e-canary-{int(time.time())}"
    logs.create_log_stream(logGroupName=group, logStreamName=canary_stream)
    logs.put_log_events(logGroupName=group, logStreamName=canary_stream, logEvents=[{
        "timestamp": int(time.time() * 1000),
        "message": json.dumps({"audit": True, "event": "log_canary",
                               "note": "synthetic canary kiosk.canary@example.com from 192.0.2.44"})}])
    time.sleep(5)
    got = logs.get_log_events(logGroupName=group, logStreamName=canary_stream)["events"]
    text = got[0]["message"] if got else ""
    record("LOG", "data protection policy masks e-mail and IP in a canary event",
           bool(text) and "kiosk.canary@example.com" not in text and "192.0.2.44" not in text, text[:120])
    logs.delete_log_stream(logGroupName=group, logStreamName=canary_stream)
    all_text = ""
    for page in logs.get_paginator("filter_log_events").paginate(logGroupName=group, startTime=t0):
        all_text += "\n".join(e["message"] for e in page.get("events", []))
    leaked = [x for x in seen_secrets if x in all_text]
    record("LOG", "no session id or secret in the audit log", not leaked, f"{len(leaked)} found")
    record("LOG", "no image data in the audit log", samples["landscape.png"][:60] not in all_text
           and not re.search(r"[A-Za-z0-9+/]{200,}", all_text))
    record("LOG", "no e-mail or IPv4 address in the audit log",
           not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+|\b(?:\d{1,3}\.){3}\d{1,3}\b", all_text))
    acc = ""
    for page in logs.get_paginator("filter_log_events").paginate(logGroupName=out["AccessLogGroup"], startTime=t0):
        acc += "\n".join(e["message"] for e in page.get("events", []))
    record("LOG", "API access log has route templates only (no ids, no IP)",
           bool(acc) and not any(x in acc for x in seen_secrets) and not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", acc))
    finish(args)


def run_d1(k, notice, samples, s3, bucket, region, sess_p, seen_secrets, b, args, quick=False):
    print("== D1 normal flow")
    sid, sec, created = k.start(notice)
    seen_secrets += [sid, sec]
    ref = ids.session_ref(sid)
    record("D1", "consent receipt returned", created["consent_receipt"]["decision"] == "accepted")
    s, _ = k.call("POST", f"/sessions/{sid}/photo", {"image_base64": samples["landscape-gps.jpg"], "style": "comic"}, sec)
    record("D1", "photo accepted (202, asynchronous)", s == 202)
    t = time.time()
    st = k.wait(sid, sec)
    record("D1", "result ready", st.get("status") == "ready",
           f"status={st.get('status')} reason={st.get('reason_code')} after {time.time() - t:.0f}s")
    if st.get("status") != "ready":
        if quick:
            finish(args)
        return sid, sec, {"url": "https://example.com/"}
    share = st["share"]
    record("D1", "share link points to the regional S3 endpoint with SigV4",
           share["url"].startswith(f"https://{bucket}.s3.{region}.amazonaws.com/") and "X-Amz-Algorithm=AWS4-HMAC-SHA256"
           in share["url"])
    record("D1", "link lifetime <= configured", share["expires_in"] <= sess_p["share_link_ttl_seconds"])
    record("D1", "input deleted right after processing", versions(s3, bucket, f"s/{ref}/input.png") == 0)
    status, data, hdr = http("GET", share["url"])
    from PIL import Image
    ok = status == 200 and Image.open(io.BytesIO(data)).format == "PNG"
    record("D1", "download works", ok, f"HTTP {status}")
    if ok:
        record("D1", "downloaded result carries no EXIF/GPS metadata", not has_metadata(data))
    record("D1", "QR code returned", share["qr_svg"].startswith("data:image/svg+xml"))
    if not quick:
        wait = share["expires_in"] + 5
        print(f"  ... waiting {wait}s for the link to expire")
        time.sleep(wait)
        status, _, _ = http("GET", share["url"])
        record("D1", "link refused after expiry", status == 403, f"HTTP {status}")
    else:
        k.call("DELETE", f"/sessions/{sid}", secret=sec)
        record("D1", "session deleted after the model check", versions(s3, bucket, f"s/{ref}/") == 0)
        finish(args)
    return sid, sec, share


def finish(args) -> None:
    fails = [r for r in RESULTS if r["result"] == "FAIL"]
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(mask_text(json.dumps({"results": RESULTS, "failed": len(fails)}, indent=2)))
    print(f"== {len([r for r in RESULTS if r['result'] == 'PASS'])} passed, {len(fails)} failed, "
          f"{len([r for r in RESULTS if r['result'] == 'INFO'])} info -> {args.report}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
