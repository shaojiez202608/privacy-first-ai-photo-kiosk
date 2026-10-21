"""The audit log contains no image data, no free text, no credentials and no personal data."""

import base64
import json
import re

import pytest
from conftest import consent

from kiosk.audit import EVENTS, AuditLogger, MemoryAuditSink

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
LONG_B64 = re.compile(r"[A-Za-z0-9+/=_-]{120,}")


def test_unknown_fields_and_bad_values_are_dropped():
    sink = MemoryAuditSink()
    log = AuditLogger(sink, region="us-east-1")
    rec = log.emit("photo_received", session_ref="a" * 32, style="comic", image=base64.b64encode(b"x" * 300).decode(),
                   email="person@example.com", ip="192.0.2.10", reason_code="Contains Free Text!",
                   categories=["VIOLENCE", "lowercase"], **{"weird key!": 1})
    line = sink.lines[0]
    assert rec["dropped_fields"] == ["categories", "email", "image", "invalid_field_name", "ip", "reason_code"]
    assert "example.com" not in line and "192.0.2.10" not in line and "Free Text" not in line
    assert rec["session_ref"] == "a" * 32 and rec["style"] == "comic"


def test_strict_mode_raises():
    log = AuditLogger(MemoryAuditSink(), strict=True)
    with pytest.raises(ValueError):
        log.emit("photo_received", style="free text here")
    with pytest.raises(ValueError):
        log.emit("not_an_event")


def test_unknown_event_is_recorded_as_rejected():
    sink = MemoryAuditSink()
    AuditLogger(sink).emit("hello")
    assert sink.events()[0]["event"] == "request_rejected"


def test_every_event_name_is_snake_case():
    assert all(re.match(r"^[a-z_]+$", e) for e in EVENTS)


def _all_flows(local, b64, clock, settings):
    secrets_seen = []
    for sample in ("landscape.png", "landscape-gps.jpg", "unsafe-marker.png", "instruction.png", "tiny.png"):
        _, body = consent(local.service)
        sid, sec = body["session_id"], body["session_secret"]
        secrets_seen += [sid, sec]
        local.service.submit_photo(sid, sec, {"image_base64": b64(sample), "style": "comic"})
        local.service.status(sid, sec)
        local.service.status(sid, "x" * 43)
    local.service.delete(sid, sec)
    consent(local.service, "declined")
    clock.advance(settings.retention_seconds + 1)
    local.run_due_expiries()
    return secrets_seen


def test_full_flows_leave_no_sensitive_data_in_audit(local, b64, clock, settings, samples):
    secrets_seen = _all_flows(local, b64, clock, settings)
    text = "\n".join(local.audit_sink.lines)
    for s in secrets_seen:
        assert s not in text  # neither session ids nor secrets
    for name in ("landscape.png", "unsafe-marker.png"):
        assert base64.b64encode(samples[name]).decode()[:60] not in text
    assert not EMAIL.search(text) and not IPV4.search(text) and not LONG_B64.search(text)
    for line in local.audit_sink.lines:
        rec = json.loads(line)
        assert rec["audit"] is True and rec["event"] in EVENTS and "dropped_fields" not in rec
        assert all(len(str(v)) <= 80 for k, v in rec.items() if k != "categories")


def test_style_prompts_never_logged(local, b64, clock, settings):
    from kiosk.styles import STYLES

    _all_flows(local, b64, clock, settings)
    text = "\n".join(local.audit_sink.lines)
    for s in STYLES.values():
        assert s.prompt not in text


def test_committed_audit_sample_is_schema_valid():
    from pathlib import Path

    from kiosk.audit import FIELDS
    path = Path(__file__).resolve().parents[1] / "sample-data" / "audit-log-sample.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) >= 8
    base = {"audit", "event", "event_id", "ts"}
    for line in lines:
        rec = json.loads(line)
        assert rec["event"] in EVENTS
        for k, v in rec.items():
            assert k in base or (k in FIELDS and FIELDS[k](v)), k
    assert not EMAIL.search("\n".join(lines)) and not IPV4.search("\n".join(lines))
