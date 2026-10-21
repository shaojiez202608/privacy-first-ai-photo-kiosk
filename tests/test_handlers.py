"""Lambda handlers: routing, body limits, no 5xx, authorizer."""

import base64
import hashlib
import json

import pytest
from conftest import consent

from handlers import api, authorizer, workers


@pytest.fixture
def wired(local, monkeypatch):
    monkeypatch.setattr(api, "_service", local.service)
    monkeypatch.setattr(workers, "_service", local.service)
    return local


def ev(route, body=None, sid="", secret="", b64=False):
    raw = json.dumps(body) if body is not None else None
    if raw and b64:
        raw = base64.b64encode(raw.encode()).decode()
    return {"routeKey": route, "headers": {"X-Session-Secret": secret}, "pathParameters": {"id": sid},
            "body": raw, "isBase64Encoded": b64}


def test_routes(wired, b64):
    n = json.loads(api.handler(ev("GET /notice"), None)["body"])
    r = api.handler(ev("POST /sessions", {"consent": "accepted", "notice_version": n["notice_version"],
                                          "notice_sha256": n["notice_sha256"]}, b64=True), None)
    assert r["statusCode"] == 201 and r["headers"]["Cache-Control"] == "no-store"
    s = json.loads(r["body"])
    sid, sec = s["session_id"], s["session_secret"]
    r = api.handler(ev("POST /sessions/{id}/photo", {"image_base64": b64("blocks.png"), "style": "pixel"}, sid, sec), None)
    assert r["statusCode"] == 202
    assert json.loads(api.handler(ev("GET /sessions/{id}", sid=sid, secret=sec), None)["body"])["status"] == "ready"
    assert api.handler(ev("DELETE /sessions/{id}", sid=sid, secret=sec), None)["statusCode"] == 200
    assert api.handler(ev("PUT /x"), None)["statusCode"] == 404


def test_bad_json_and_oversize(wired):
    e = ev("POST /sessions")
    e["body"] = "{not json"
    assert api.handler(e, None)["statusCode"] == 400
    e["body"] = "x" * 10000
    assert api.handler(e, None)["statusCode"] == 413


def test_unexpected_error_is_not_5xx(wired, monkeypatch, capsys):
    def boom(*a):
        raise KeyError("secret detail")

    monkeypatch.setattr(wired.service, "notice", boom)
    r = api.handler(ev("GET /notice"), None)
    assert r["statusCode"] == 424
    out = capsys.readouterr().out
    assert "secret detail" not in out and "KeyError" in out


def test_workers_validate_ref(wired):
    assert workers.process({"session_ref": "../etc"}, None) == {"status": "invalid"}
    assert workers.expire({}, None) == {"status": "invalid"}


def test_expire_raises_for_workflow_retry_when_incomplete(wired, monkeypatch):
    monkeypatch.setattr(wired.service, "expire", lambda ref: {"status": "incomplete"})
    with pytest.raises(RuntimeError):
        workers.expire({"session_ref": "a" * 32}, None)


def test_expire_ok(wired):
    _, body = consent(wired.service)
    from kiosk import ids
    assert workers.expire({"session_ref": ids.session_ref(body["session_id"])}, None)["status"] == "deleted"


def test_authorizer_check():
    d = hashlib.sha256(b"T" * 48).digest()
    assert authorizer.check("T" * 48, d)
    assert not authorizer.check("T" * 47 + "U", d)
    assert not authorizer.check(None, d) and not authorizer.check("short", d) and not authorizer.check("x" * 300, d)


def test_authorizer_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(authorizer, "_digest", None)
    monkeypatch.delenv("KIOSK_TOKEN_SECRET_ARN", raising=False)
    assert authorizer.handler({"headers": {"x-kiosk-token": "T" * 48}}, None) == {"isAuthorized": False}
    assert "kiosk_auth_denied" in capsys.readouterr().out
