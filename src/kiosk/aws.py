"""AWS adapters. Every adapter takes an injected boto3 client so tests can use moto or botocore Stubber.

Errors from AWS are turned into ``DownstreamError(code)`` where ``code`` is the AWS error code (or
the exception class name). Error *messages* are never propagated: they can echo request content.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from .ports import DownstreamError, ModerationResult, PurgeResult, SessionRecord
from .styles import NEGATIVE_PROMPT, STYLES


def _code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        return "".join(ch for ch in str(code) if ch.isalnum())[:64] or "ClientError"
    return type(exc).__name__


# ---------------------------------------------------------------- DynamoDB

_RECORD_FIELDS = ("secret_hash", "consent_receipt_id", "notice_version", "notice_sha256", "consented_at",
                  "expires_at", "retention_seconds", "status", "style", "reason_code", "result_key")


class DynamoSessionStore:
    def __init__(self, client: Any, table: str) -> None:
        self._c, self._t = client, table

    def put_new(self, record: SessionRecord, ttl: int) -> None:
        item = {"session_ref": {"S": record.session_ref}, "ttl": {"N": str(ttl)}}
        for f in _RECORD_FIELDS:
            v = getattr(record, f)
            if v is None:
                continue
            item[f] = {"N": str(v)} if isinstance(v, int) else {"S": str(v)}
        try:
            self._c.put_item(TableName=self._t, Item=item, ConditionExpression="attribute_not_exists(session_ref)")
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None

    def get(self, ref: str) -> SessionRecord | None:
        try:
            item = self._c.get_item(TableName=self._t, Key={"session_ref": {"S": ref}}, ConsistentRead=True).get("Item")
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None
        if not item:
            return None
        vals: dict[str, Any] = {}
        for f in _RECORD_FIELDS:
            if f in item:
                (kind, v), = item[f].items()
                vals[f] = int(v) if kind == "N" else v
        return SessionRecord(session_ref=ref, **vals)

    def update(self, ref: str, *, expect_status: str | None = None, **fields: Any) -> bool:
        names, values, sets = {"#pk": "session_ref"}, {}, []
        for i, (k, v) in enumerate(fields.items()):
            names[f"#f{i}"] = k
            values[f":v{i}"] = {"N": str(v)} if isinstance(v, int) and not isinstance(v, bool) else {"S": str(v)}
            sets.append(f"#f{i} = :v{i}")
        cond = "attribute_exists(#pk)"
        if expect_status is not None:
            names["#st"] = "status"
            values[":expect"] = {"S": expect_status}
            cond += " AND #st = :expect"
        try:
            self._c.update_item(TableName=self._t, Key={"session_ref": {"S": ref}},
                                UpdateExpression="SET " + ", ".join(sets), ConditionExpression=cond,
                                ExpressionAttributeNames=names, ExpressionAttributeValues=values)
            return True
        except ClientError as exc:
            if _code(exc) == "ConditionalCheckFailedException":
                return False
            raise DownstreamError(_code(exc)) from None
        except BotoCoreError as exc:
            raise DownstreamError(_code(exc)) from None

    def delete(self, ref: str) -> bool:
        try:
            r = self._c.delete_item(TableName=self._t, Key={"session_ref": {"S": ref}}, ReturnValues="ALL_OLD")
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None
        return bool(r.get("Attributes"))


# ---------------------------------------------------------------- S3


def regional_s3_client(region: str, config: Any = None) -> Any:
    """S3 client for the regional endpoint with SigV4. Presigned URLs then point at
    ``<bucket>.s3.<region>.amazonaws.com`` and carry X-Amz-Date, which the bucket policy's
    s3:signatureAge limit needs. (A default client in us-east-1 presigns with SigV2.)"""
    import boto3
    from botocore.config import Config

    cfg = Config(signature_version="s3v4", s3={"addressing_style": "virtual"}, region_name=region)
    return boto3.client("s3", config=config.merge(cfg) if config else cfg,
                        endpoint_url=f"https://s3.{region}.amazonaws.com")


class S3ObjectStore:
    """Objects are written with SSE-KMS. ``purge_prefix`` removes every object version and every
    delete marker under the prefix, aborts unfinished multipart uploads, then lists again and
    retries until nothing is left (or reports what is left)."""

    def __init__(self, client: Any, bucket: str, kms_key_id: str | None = None, *, attempts: int = 3) -> None:
        self._c, self._b, self._k, self._attempts = client, bucket, kms_key_id, attempts

    def put(self, key: str, data: bytes, content_type: str) -> None:
        extra: dict[str, Any] = {"ServerSideEncryption": "aws:kms"}
        if self._k:
            extra["SSEKMSKeyId"] = self._k
        try:
            self._c.put_object(Bucket=self._b, Key=key, Body=data, ContentType=content_type, **extra)
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None

    def get(self, key: str) -> bytes:
        try:
            return self._c.get_object(Bucket=self._b, Key=key)["Body"].read()
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None

    def _list(self, prefix: str, exact: str | None = None) -> tuple[list[dict], list[dict]]:
        versions: list[dict] = []
        markers: list[dict] = []

        def keep(key: str) -> bool:
            return key == exact if exact is not None else key.startswith(prefix)

        for page in self._c.get_paginator("list_object_versions").paginate(Bucket=self._b, Prefix=prefix):
            versions += [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", [])
                         if keep(v["Key"])]
            markers += [{"Key": m["Key"], "VersionId": m["VersionId"]} for m in page.get("DeleteMarkers", [])
                        if keep(m["Key"])]
        return versions, markers

    def purge_prefix(self, prefix: str) -> PurgeResult:
        if not prefix.endswith("/") or len(prefix) < 4:
            raise ValueError("purge prefix must be a session prefix ending in '/'")
        return self._purge(prefix, None)

    def purge_key(self, key: str) -> PurgeResult:
        """Every version and delete marker of exactly this key."""
        return self._purge(key, key)

    def _purge(self, prefix: str, exact: str | None) -> PurgeResult:
        n_versions = n_markers = n_uploads = 0
        try:
            for page in self._c.get_paginator("list_multipart_uploads").paginate(Bucket=self._b, Prefix=prefix):
                for up in page.get("Uploads", []):
                    if exact is not None and up["Key"] != exact:
                        continue
                    self._c.abort_multipart_upload(Bucket=self._b, Key=up["Key"], UploadId=up["UploadId"])
                    n_uploads += 1
            remaining = 0
            for _ in range(self._attempts):
                versions, markers = self._list(prefix, exact)
                if not versions and not markers:
                    remaining = 0
                    break
                marker_ids = {(m["Key"], m["VersionId"]) for m in markers}
                batch = versions + markers
                for i in range(0, len(batch), 1000):
                    r = self._c.delete_objects(Bucket=self._b, Delete={"Objects": batch[i:i + 1000], "Quiet": False})
                    for d in r.get("Deleted", []):
                        if (d.get("Key"), d.get("VersionId")) in marker_ids:
                            n_markers += 1
                        else:
                            n_versions += 1
                versions, markers = self._list(prefix, exact)
                remaining = len(versions) + len(markers)
                if remaining == 0:
                    break
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None
        return PurgeResult(n_versions, n_markers, n_uploads, remaining)

    def count_versions(self, prefix: str) -> int:
        try:
            v, m = self._list(prefix)
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None
        return len(v) + len(m)

    def presign_get(self, key: str, expires_in: int) -> str:
        return self._c.generate_presigned_url("get_object", Params={"Bucket": self._b, "Key": key,
                                                                    "ResponseContentDisposition":
                                                                        'attachment; filename="kiosk-photo.png"'},
                                              ExpiresIn=int(expires_in))


# ---------------------------------------------------------------- Step Functions


class StepFunctionsScheduler:
    """One Standard workflow execution per session: Wait (retention) -> purge Lambda."""

    def __init__(self, client: Any, state_machine_arn: str) -> None:
        self._c, self._arn = client, state_machine_arn

    def _execution_arn(self, ref: str) -> str:
        return self._arn.replace(":stateMachine:", ":execution:", 1) + f":exp-{ref}"

    def schedule_expiry(self, ref: str, seconds: int) -> None:
        try:
            self._c.start_execution(stateMachineArn=self._arn, name=f"exp-{ref}",
                                    input=json.dumps({"session_ref": ref, "wait_seconds": int(seconds)}))
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None

    def cancel_expiry(self, ref: str) -> bool:
        try:
            self._c.stop_execution(executionArn=self._execution_arn(ref), cause="session deleted on request")
            return True
        except ClientError as exc:
            if _code(exc) in ("ExecutionDoesNotExist", "ValidationException"):
                return False
            raise DownstreamError(_code(exc)) from None
        except BotoCoreError as exc:
            raise DownstreamError(_code(exc)) from None


# ---------------------------------------------------------------- Bedrock Guardrails

GUARDRAIL_CATEGORIES = frozenset({"HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT", "PROMPT_ATTACK"})


class GuardrailModeration:
    """ApplyGuardrail with one image content block. Fails closed: an API error raises
    DownstreamError and the service rejects the photo."""

    name = "guardrails"

    def __init__(self, client: Any, guardrail_id: str, version: str) -> None:
        self._c, self._id, self._v = client, guardrail_id, version

    def check(self, png: bytes, source: str) -> ModerationResult:
        try:
            r = self._c.apply_guardrail(guardrailIdentifier=self._id, guardrailVersion=self._v,
                                        source="INPUT" if source == "input" else "OUTPUT",
                                        content=[{"image": {"format": "png", "source": {"bytes": png}}}])
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None
        action = r.get("action")
        if action not in ("NONE", "GUARDRAIL_INTERVENED"):
            raise DownstreamError("UnexpectedGuardrailAction")
        cats: set[str] = set()
        for a in r.get("assessments", []) or []:
            for f in (a.get("contentPolicy") or {}).get("filters", []) or []:
                if f.get("action") == "BLOCKED":
                    t = f.get("type")
                    cats.add(t if t in GUARDRAIL_CATEGORIES else "OTHER")
        if action == "GUARDRAIL_INTERVENED" and not cats:
            cats.add("OTHER")
        return ModerationResult(allowed=action == "NONE", categories=tuple(sorted(cats)))


# ---------------------------------------------------------------- Bedrock image model


class ModelFiltered(DownstreamError):
    """The model itself filtered the request or its output (Stability finish_reasons)."""


FINISH_REASONS = {
    "Filter reason: prompt": "model_filtered_prompt",
    "Filter reason: output image": "model_filtered_output",
    "Filter reason: input image": "model_filtered_input",
    "Inference error": "model_inference_error",
}
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class BedrockImageModel:
    """Stability image models on Bedrock. ``family`` selects the request shape:

    * ``control-structure``: Stable Image Control Structure (keeps the structure of the input).
    * ``sd35-image-to-image``: Stable Diffusion 3.5 Large in ``image-to-image`` mode.
    """

    name = "bedrock"

    def __init__(self, client: Any, model_id: str, family: str, strength: float) -> None:
        if family not in ("control-structure", "sd35-image-to-image"):
            raise ValueError("unknown model family")
        self._c, self._id, self._family, self._strength = client, model_id, family, strength

    def request_body(self, png: bytes, style: str) -> dict[str, Any]:
        s = STYLES[style]
        img = base64.b64encode(png).decode()
        if self._family == "control-structure":
            return {"image": img, "prompt": s.prompt, "negative_prompt": NEGATIVE_PROMPT,
                    "control_strength": self._strength, "style_preset": s.style_preset, "output_format": "png"}
        return {"image": img, "prompt": s.prompt, "negative_prompt": NEGATIVE_PROMPT, "mode": "image-to-image",
                "strength": self._strength, "output_format": "png"}

    def stylize(self, png: bytes, style: str) -> bytes:
        try:
            r = self._c.invoke_model(modelId=self._id, contentType="application/json", accept="application/json",
                                     body=json.dumps(self.request_body(png, style)))
            raw = r["body"].read(MAX_RESPONSE_BYTES + 1)
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None
        return parse_model_response(raw)


def parse_model_response(raw: bytes) -> bytes:
    """Validate the model response. Free text from the model is never returned or logged; only a
    reason code from a fixed table leaves this function."""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DownstreamError("model_response_too_large")
    try:
        body = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise DownstreamError("model_response_not_json") from None
    if not isinstance(body, dict):
        raise DownstreamError("model_response_not_object")
    reasons = body.get("finish_reasons")
    if isinstance(reasons, list) and reasons and reasons[0] is not None:
        raise ModelFiltered(FINISH_REASONS.get(reasons[0], "model_finish_reason_unknown")
                            if isinstance(reasons[0], str) else "model_finish_reason_unknown")
    images = body.get("images")
    if not isinstance(images, list) or not images or not isinstance(images[0], str):
        raise DownstreamError("model_response_no_image")
    try:
        data = base64.b64decode(images[0], validate=True)
    except (binascii.Error, ValueError):
        raise DownstreamError("model_response_bad_base64") from None
    if not data:
        raise DownstreamError("model_response_no_image")
    return data


def _reject_constant(name: str) -> float:
    # json.loads accepts NaN/Infinity by default; a model response containing them is malformed.
    raise ValueError(name)



# ---------------------------------------------------------------- KMS HMAC receipts


class KmsMacSigner:
    def __init__(self, client: Any, key_id: str) -> None:
        self._c, self._k = client, key_id

    def sign(self, payload: dict[str, Any]) -> str:
        from .local import canonical

        try:
            r = self._c.generate_mac(KeyId=self._k, MacAlgorithm="HMAC_SHA_256", Message=canonical(payload))
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None
        return base64.b64encode(r["Mac"]).decode()

    def verify(self, payload: dict[str, Any], mac: str) -> bool:
        from .local import canonical

        try:
            r = self._c.verify_mac(KeyId=self._k, MacAlgorithm="HMAC_SHA_256", Message=canonical(payload),
                                   Mac=base64.b64decode(mac))
            return bool(r.get("MacValid"))
        except ClientError as exc:
            if _code(exc) == "KMSInvalidMacException":
                return False
            raise DownstreamError(_code(exc)) from None


# ---------------------------------------------------------------- Lambda async invoke


class LambdaInvoker:
    def __init__(self, client: Any, function_name: str) -> None:
        self._c, self._fn = client, function_name

    def process_async(self, ref: str) -> None:
        try:
            self._c.invoke(FunctionName=self._fn, InvocationType="Event",
                           Payload=json.dumps({"session_ref": ref}).encode())
        except (ClientError, BotoCoreError) as exc:
            raise DownstreamError(_code(exc)) from None
