"""Asynchronous processor (invoked with InvocationType=Event) and expiry purge (invoked by the
Step Functions workflow after the Wait state). Both only receive a session_ref, never image data."""

from __future__ import annotations

from typing import Any

from kiosk import ids
from kiosk.wiring import aws_service

_service = None


def _svc():
    global _service
    if _service is None:
        _service = aws_service()
    return _service


def process(event: dict[str, Any], context: Any) -> dict[str, Any]:
    ref = event.get("session_ref") if isinstance(event, dict) else None
    if not isinstance(ref, str) or not ids.SESSION_REF_RE.match(ref):
        return {"status": "invalid"}
    return _svc().process(ref)


def expire(event: dict[str, Any], context: Any) -> dict[str, Any]:
    ref = event.get("session_ref") if isinstance(event, dict) else None
    if not isinstance(ref, str) or not ids.SESSION_REF_RE.match(ref):
        return {"status": "invalid"}
    result = _svc().expire(ref)
    if result["status"] != "deleted":
        # Let the workflow's Retry policy run again; the lifecycle rule remains the backstop.
        raise RuntimeError("deletion incomplete")
    return result
