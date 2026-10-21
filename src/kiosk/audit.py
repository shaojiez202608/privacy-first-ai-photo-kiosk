"""Audit events with an allowlist schema.

Every audit event is one JSON line. Only known event names and known fields are written, and every
string value must match the field's pattern (short identifiers, enum values, counts). Anything else
is dropped and the *name* of the dropped field is recorded, never its value. This is what keeps
images, model output, free text, IP addresses, names and e-mail addresses out of the log, even if a
caller passes them by mistake. The CloudWatch Logs data protection policy on the log group is a
second, independent layer (see docs/architecture.md).
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

EVENTS = frozenset({
    "consent_accepted", "consent_declined", "consent_rejected_stale_notice",
    "photo_received", "photo_rejected", "processing_started", "input_blocked", "output_blocked",
    "model_filtered", "processing_failed", "processing_completed", "input_deleted",
    "share_link_issued", "session_deleted", "session_expired", "deletion_incomplete",
    "session_access_denied", "kiosk_auth_denied", "request_rejected", "log_canary",
})

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_UPPER = re.compile(r"^[A-Z][A-Z_]{0,31}$")
_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ERRNAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_KIOSK = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

STATUSES = frozenset({"consented", "processing", "ready", "rejected", "failed"})
REASONS = frozenset({"user_request", "expired", "processing_complete", "rejected", "failed",
                     "expired_on_access"})
STAGES = frozenset({"input", "output", "model", "storage", "consent", "auth", "request"})

# field -> validator (returns True if the value may be written)
FIELDS: dict[str, Callable[[Any], bool]] = {
    "session_ref": lambda v: isinstance(v, str) and bool(_HEX32.match(v)),
    "receipt_id": lambda v: isinstance(v, str) and bool(_UUID.match(v)),
    "notice_version": lambda v: isinstance(v, str) and bool(_VERSION.match(v)),
    "notice_sha256": lambda v: isinstance(v, str) and bool(_SHA.match(v)),
    "decision": lambda v: v in ("accepted", "declined"),
    "status": lambda v: v in STATUSES,
    "reason": lambda v: v in REASONS,
    "reason_code": lambda v: isinstance(v, str) and bool(_CODE.match(v)),
    "stage": lambda v: v in STAGES,
    "style": lambda v: isinstance(v, str) and bool(_CODE.match(v)),
    "categories": lambda v: isinstance(v, list) and len(v) <= 8
    and all(isinstance(x, str) and _UPPER.match(x) for x in v),
    "error_code": lambda v: isinstance(v, str) and bool(_ERRNAME.match(v)),
    "kiosk_id": lambda v: isinstance(v, str) and bool(_KIOSK.match(v)),
    "region": lambda v: isinstance(v, str) and (v == "local" or bool(_REGION.match(v))),
    "model_provider": lambda v: v in ("transform", "bedrock"),
    "moderation_provider": lambda v: v in ("mock", "guardrails", "none"),
    "versions_deleted": lambda v: _count(v),
    "delete_markers_deleted": lambda v: _count(v),
    "remaining_versions": lambda v: _count(v),
    "uploads_aborted": lambda v: _count(v),
    "record_deleted": lambda v: isinstance(v, bool),
    "schedule_cancelled": lambda v: isinstance(v, bool),
    "retention_seconds": lambda v: _count(v),
    "link_ttl_seconds": lambda v: _count(v),
    "image_width": lambda v: _count(v),
    "image_height": lambda v: _count(v),
    "duration_ms": lambda v: _count(v),
    "expires_at": lambda v: isinstance(v, str) and bool(_ISO.match(v)),
    "field_path": lambda v: isinstance(v, str) and bool(re.match(r"^[a-z_][a-z0-9_.\[\]]{0,63}$", v)),
    "validator": lambda v: isinstance(v, str) and bool(_CODE.match(v)),
}


def _count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**9


class AuditLogger:
    """Writes allowlisted audit events. ``sink`` receives one JSON string per event."""

    def __init__(self, sink: Callable[[str], None] | None = None, *, region: str | None = None,
                 kiosk_id: str | None = None, strict: bool = False,
                 clock: Callable[[], float] = time.time) -> None:
        self._sink = sink or (lambda line: print(line, file=sys.stdout, flush=True))
        self._base = {k: v for k, v in (("region", region), ("kiosk_id", kiosk_id)) if v}
        self._strict = strict
        self._clock = clock

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        if event not in EVENTS:
            if self._strict:
                raise ValueError(f"unknown audit event {event!r}")
            fields = {"reason_code": "unknown_event"}
            event = "request_rejected"
        record: dict[str, Any] = {
            "audit": True,
            "event": event,
            "event_id": str(uuid.uuid4()),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
        }
        dropped: list[str] = []
        for key, value in {**self._base, **fields}.items():
            check = FIELDS.get(key)
            if check is not None and check(value):
                record[key] = value
            else:
                dropped.append(key if re.match(r"^[a-z_][a-z0-9_]{0,40}$", key) else "invalid_field_name")
        if dropped:
            if self._strict:
                raise ValueError(f"audit fields rejected: {sorted(set(dropped))}")
            record["dropped_fields"] = sorted(set(dropped))
        self._sink(json.dumps(record, separators=(",", ":"), sort_keys=True))
        return record


class MemoryAuditSink:
    """Collects audit lines in memory (local mode and tests)."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.lines]

    def names(self) -> list[str]:
        return [e["event"] for e in self.events()]
