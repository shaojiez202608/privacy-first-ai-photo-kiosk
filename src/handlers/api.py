"""API Gateway HTTP API (payload format 2.0) -> KioskService.

The kiosk token has already been checked by the Lambda authorizer. Session calls also need the
session id (path) and the session secret (``X-Session-Secret`` header). Responses are never cached.
Unexpected errors return 424 with an error code; the exception message is not logged.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from kiosk.audit import AuditLogger
from kiosk.wiring import aws_service

_service = None
_HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _svc():
    global _service
    if _service is None:
        _service = aws_service()
    return _service


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": _HEADERS, "body": json.dumps(body)}


def _body(event: dict[str, Any], limit: int) -> Any:
    raw = event.get("body") or ""
    if len(raw) > limit:
        raise OverflowError
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw) if raw else {}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    svc = _svc()
    route = event.get("routeKey", "")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    sid = (event.get("pathParameters") or {}).get("id", "")
    secret = headers.get("x-session-secret", "")
    limit = svc.s.max_upload_bytes * 2 + 4096
    try:
        if route == "GET /notice":
            return _resp(*svc.notice())
        if route == "POST /sessions":
            return _resp(*svc.create_session(_body(event, 8192)))
        if route == "POST /sessions/{id}/photo":
            return _resp(*svc.submit_photo(sid, secret, _body(event, limit)))
        if route == "GET /sessions/{id}":
            return _resp(*svc.status(sid, secret))
        if route == "DELETE /sessions/{id}":
            return _resp(*svc.delete(sid, secret))
        return _resp(404, {"error": "route_not_found"})
    except OverflowError:
        svc.audit.emit("request_rejected", stage="request", reason_code="body_too_large")
        return _resp(413, {"error": "body_too_large"})
    except (ValueError, UnicodeDecodeError):
        svc.audit.emit("request_rejected", stage="request", reason_code="body_not_json")
        return _resp(400, {"error": "body_not_json"})
    except Exception as exc:  # noqa: BLE001 - never a 5xx, never the message
        AuditLogger(region=svc.s.region).emit("request_rejected", stage="request", reason_code="internal_error",
                                              error_code=type(exc).__name__)
        return _resp(424, {"error": "dependency_unavailable", "retryable": True})
