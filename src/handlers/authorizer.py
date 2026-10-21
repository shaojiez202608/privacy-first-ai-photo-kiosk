"""HTTP API Lambda authorizer (simple responses). Accepts a request only if the ``X-Kiosk-Token``
header equals the kiosk token stored in Secrets Manager (constant-time comparison of SHA-256
digests). The token identifies a kiosk device, not a person; there are no user accounts."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from kiosk.audit import AuditLogger

_digest: bytes | None = None
_audit = AuditLogger(region=os.environ.get("AWS_REGION"))


def _expected() -> bytes:
    global _digest
    if _digest is None:
        import boto3

        value = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["KIOSK_TOKEN_SECRET_ARN"])
        _digest = hashlib.sha256(value["SecretString"].encode()).digest()
    return _digest


def check(token: Any, expected_digest: bytes) -> bool:
    if not isinstance(token, str) or not 20 <= len(token) <= 256:
        return False
    return hmac.compare_digest(hashlib.sha256(token.encode()).digest(), expected_digest)


def handler(event: dict[str, Any], context: Any) -> dict[str, bool]:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    try:
        ok = check(headers.get("x-kiosk-token"), _expected())
    except Exception as exc:  # noqa: BLE001 - fail closed
        _audit.emit("kiosk_auth_denied", stage="auth", error_code=type(exc).__name__)
        return {"isAuthorized": False}
    if not ok:
        _audit.emit("kiosk_auth_denied", stage="auth", reason_code="token_mismatch")
    return {"isAuthorized": ok}
