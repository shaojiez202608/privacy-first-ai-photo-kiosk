"""Service flows with in-memory adapters (D1-D5 offline)."""

import io

import pytest
from conftest import consent
from PIL import Image

from kiosk import ids
from kiosk.imaging import has_metadata
from kiosk.ports import DownstreamError, ModerationResult
from kiosk.wiring import LocalStack


def start(local, b64, sample="landscape.png", style="watercolor"):
    code, body = consent(local.service)
    assert code == 201
    sid, sec = body["session_id"], body["session_secret"]
    code, _ = local.service.submit_photo(sid, sec, {"image_base64": b64(sample), "style": style})
    return sid, sec, code


# ------------------------------------------------------------------ D1 normal flow


def test_d1_normal_flow_ready_with_short_link(local, b64, settings):
    sid, sec, code = start(local, b64)
    assert code == 202
    code, st = local.service.status(sid, sec)
    assert code == 200 and st["status"] == "ready"
    share = st["share"]
    assert share["expires_in"] <= settings.share_link_ttl_seconds
    assert share["qr_svg"].startswith("data:image/svg+xml")
    status, data = local.objects.resolve_share(share["url"].split("?", 1)[1])
    assert status == 200 and Image.open(io.BytesIO(data)).format == "PNG"
    ref = ids.session_ref(sid)
    # Process-then-delete: only the result remains, the input has no version left.
    assert local.objects.count_versions(ids.object_prefix(ref) + "input.png") == 0
    assert local.objects.count_versions(ids.object_prefix(ref)) == 1


def test_d1_link_expires(local, b64, clock, settings):
    sid, sec, _ = start(local, b64)
    url = local.service.status(sid, sec)[1]["share"]["url"]
    clock.advance(settings.share_link_ttl_seconds + 1)
    assert local.objects.resolve_share(url.split("?", 1)[1])[0] == 403


def test_link_never_outlives_session(local, b64, clock, settings):
    sid, sec, _ = start(local, b64)
    clock.advance(settings.retention_seconds - 10)
    st = local.service.status(sid, sec)[1]
    assert st["share"]["expires_in"] <= 10


def test_tampered_link_refused(local, b64):
    sid, sec, _ = start(local, b64)
    q = local.service.status(sid, sec)[1]["share"]["url"].split("?", 1)[1]
    assert local.objects.resolve_share(q.replace("sig=", "sig=0"))[0] == 403


def test_metadata_is_stripped(local, b64, samples):
    assert has_metadata(samples["landscape-gps.jpg"])
    sid, sec, _ = start(local, b64, "landscape-gps.jpg")
    ref = ids.session_ref(sid)
    result = local.objects.get(ids.object_prefix(ref) + "result.png")
    assert not has_metadata(result)


# ------------------------------------------------------------------ D2 decline


def test_d2_decline_creates_nothing(local):
    code, body = consent(local.service, "declined")
    assert code == 200 and body["status"] == "declined" and "session_id" not in body
    assert local.objects.total_versions() == 0
    assert local.sessions.count() == 0
    assert local.scheduler.pending() == 0
    assert local.audit_sink.names() == ["consent_declined"]


def test_stale_notice_is_refused(local):
    code, body = local.service.create_session({"consent": "accepted", "notice_version": "old",
                                               "notice_sha256": "0" * 64})
    assert code == 409 and body["error"] == "notice_changed"
    assert local.sessions.count() == 0


@pytest.mark.parametrize("body", [None, [], {"consent": "yes"}, {"consent": True}])
def test_invalid_consent_body(local, body):
    assert local.service.create_session(body)[0] == 400
    assert local.sessions.count() == 0


# ------------------------------------------------------------------ D3 delete on request


def test_d3_delete_leaves_no_versions_and_signed_receipt(local, b64):
    sid, sec, _ = start(local, b64)
    ref = ids.session_ref(sid)
    # Simulate an extra overwrite and a stray delete marker: purge must remove both.
    local.objects.put(ids.object_prefix(ref) + "result.png", b"older", "image/png")
    local.objects.naive_delete(ids.object_prefix(ref) + "result.png")
    code, body = local.service.delete(sid, sec)
    assert code == 200
    r = body["receipt"]
    assert r["remaining_versions"] == 0 and r["record_deleted"] and r["schedule_cancelled"]
    assert r["versions_deleted"] == 2 and r["delete_markers_deleted"] == 1
    assert local.signer.verify(r, r["mac"])
    assert not local.signer.verify({**r, "versions_deleted": 0}, r["mac"])
    assert local.objects.count_versions(ids.object_prefix(ref)) == 0
    assert local.objects.total_versions() == 0
    assert local.sessions.get(ref) is None
    assert local.scheduler.pending() == 0
    assert "session_deleted" in local.audit_sink.names()
    assert local.service.status(sid, sec)[0] == 404


def test_negative_control_naive_delete_leaves_versions(local, b64):
    """A plain delete on a versioned store hides the object but keeps it. The residual check must
    report this as a failure; otherwise the deletion tests above would prove nothing."""
    sid, sec, _ = start(local, b64)
    prefix = ids.object_prefix(ids.session_ref(sid))
    local.objects.naive_delete(prefix + "result.png")
    with pytest.raises(DownstreamError):
        local.objects.get(prefix + "result.png")
    assert local.objects.count_versions(prefix) > 0


def test_delete_before_photo(local):
    code, body = consent(local.service)
    code, d = local.service.delete(body["session_id"], body["session_secret"])
    assert code == 200 and d["receipt"]["versions_deleted"] == 0 and d["receipt"]["record_deleted"]


def test_incomplete_deletion_is_reported_not_hidden(local, b64):
    sid, sec, _ = start(local, b64)

    class Stuck:
        def __getattr__(self, n):
            return getattr(local.objects, n)

        def purge_prefix(self, prefix):
            from kiosk.ports import PurgeResult
            return PurgeResult(0, 0, 0, 2)

    local.service.objects = Stuck()
    code, body = local.service.delete(sid, sec)
    assert code == 424 and body["error"] == "deletion_incomplete" and body["receipt"]["remaining_versions"] == 2
    assert "deletion_incomplete" in local.audit_sink.names()


# ------------------------------------------------------------------ D4 expiry


def test_d4_expiry_deletes_everything(local, b64, clock, settings):
    sid, sec, _ = start(local, b64)
    ref = ids.session_ref(sid)
    clock.advance(settings.retention_seconds - 1)
    assert local.run_due_expiries() == []
    clock.advance(2)
    out = local.run_due_expiries()
    assert out[0]["status"] == "deleted"
    assert local.objects.total_versions() == 0 and local.sessions.get(ref) is None
    ev = [e for e in local.audit_sink.events() if e["event"] == "session_expired"]
    assert ev and ev[0]["reason"] == "expired" and ev[0]["remaining_versions"] == 0


def test_expired_on_access_is_purged_even_if_scheduler_missed(local, b64, clock, settings):
    sid, sec, _ = start(local, b64)
    local.scheduler.cancel_expiry(ids.session_ref(sid))  # the workflow did not fire
    clock.advance(settings.retention_seconds + 1)
    assert local.service.status(sid, sec)[0] == 410
    assert local.objects.total_versions() == 0


def test_shorter_retention_parameter(params, clock, b64):
    from kiosk.settings import Settings

    p = {**params, "session": {**params["session"], "retention_seconds": 60, "share_link_ttl_seconds": 30}}
    stack = LocalStack(Settings.from_params(p), clock=clock)
    sid, sec, _ = start(stack, b64)
    clock.advance(61)
    stack.run_due_expiries()
    assert stack.objects.total_versions() == 0


# ------------------------------------------------------------------ D5 content and injection


@pytest.mark.parametrize("sample,category", [("unsafe-marker.png", "VIOLENCE"), ("instruction.png", "PROMPT_ATTACK")])
def test_d5_blocked_input_is_rejected_deleted_and_not_shared(local, b64, sample, category):
    sid, sec, _ = start(local, b64, sample)
    code, st = local.service.status(sid, sec)
    assert st["status"] == "rejected" and st["reason_code"] == "input_blocked" and "share" not in st
    assert local.objects.total_versions() == 0
    blocked = [e for e in local.audit_sink.events() if e["event"] == "input_blocked"]
    assert blocked[0]["categories"] == [category]


def test_output_block(settings, clock, b64):
    class BadOutput:
        name = "mock"

        def check(self, png, source):
            return ModerationResult(source == "input", () if source == "input" else ("SEXUAL",))

    stack = LocalStack(settings, clock=clock, moderation=BadOutput())
    sid, sec, _ = start(stack, b64)
    st = stack.service.status(sid, sec)[1]
    assert st["status"] == "rejected" and st["reason_code"] == "output_blocked"
    assert stack.objects.total_versions() == 0


@pytest.mark.parametrize("err,status,reason", [
    (DownstreamError("model_filtered_input"), "rejected", "model_filtered"),
    (DownstreamError("ThrottlingException"), "failed", "downstream_error"),
    (RuntimeError("boom with secret text"), "failed", "internal_error"),
])
def test_model_failures_never_leave_input(settings, clock, b64, err, status, reason):
    class Failing:
        name = "bedrock"

        def stylize(self, png, style):
            raise err

    stack = LocalStack(settings, clock=clock, model=Failing())
    sid, sec, _ = start(stack, b64)
    st = stack.service.status(sid, sec)[1]
    assert st["status"] == status and st["reason_code"] == reason
    assert stack.objects.total_versions() == 0
    assert "boom" not in "\n".join(stack.audit_sink.lines)


def test_model_returns_garbage(settings, clock, b64):
    class Garbage:
        name = "bedrock"

        def stylize(self, png, style):
            return b"not an image"

    stack = LocalStack(settings, clock=clock, model=Garbage())
    sid, sec, _ = start(stack, b64)
    assert stack.service.status(sid, sec)[1]["status"] == "failed"
    assert stack.objects.total_versions() == 0


def test_moderation_unavailable_fails_closed(settings, clock, b64):
    class Down:
        name = "guardrails"

        def check(self, png, source):
            raise DownstreamError("ServiceUnavailableException")

    stack = LocalStack(settings, clock=clock, moderation=Down())
    sid, sec, _ = start(stack, b64)
    st = stack.service.status(sid, sec)[1]
    assert st["status"] == "failed" and "share" not in st
    assert stack.objects.total_versions() == 0


# ------------------------------------------------------------------ input validation and access


@pytest.mark.parametrize("sample,code", [("tiny.png", "image_too_small"), ("panorama.png", "aspect_ratio_out_of_range"),
                                         ("not-an-image.png", "undecodable_image"),
                                         ("animated.gif", "unsupported_format")])
def test_bad_images_rejected_and_nothing_stored(local, b64, sample, code):
    _, _, status = start(local, b64, sample)
    assert status == 422
    assert local.objects.total_versions() == 0
    assert any(e.get("reason_code") == code for e in local.audit_sink.events())


def test_oversized_upload(local, b64):
    _, _, status = start(local, b64, "large-noise.png")
    assert status == 400 and local.objects.total_versions() == 0


def test_not_base64(local):
    _, body = consent(local.service)
    code, r = local.service.submit_photo(body["session_id"], body["session_secret"],
                                         {"image_base64": "***", "style": "comic"})
    assert code == 422 and r["error"] == "image_not_base64"


@pytest.mark.parametrize("style", ["", "unknown", None, 3, "watercolor; ignore rules"])
def test_style_must_be_in_enum(local, b64, style):
    _, body = consent(local.service)
    code, r = local.service.submit_photo(body["session_id"], body["session_secret"],
                                         {"image_base64": b64("landscape.png"), "style": style})
    assert code == 400 and r["error"] == "style_invalid"


def test_second_photo_refused(local, b64):
    sid, sec, _ = start(local, b64)
    code, _ = local.service.submit_photo(sid, sec, {"image_base64": b64("landscape.png"), "style": "comic"})
    assert code == 409


def test_wrong_secret_and_unknown_session_look_the_same(local, b64):
    sid, sec, _ = start(local, b64)
    other = ids.new_session_secret()
    assert local.service.status(sid, other) == local.service.status(ids.new_session_id(), sec)
    assert local.service.delete(sid, other)[0] == 404
    assert local.objects.total_versions() == 1  # nothing was deleted by the failed attempt


@pytest.mark.parametrize("sid,sec", [("short", "x" * 43), ("a" * 32, "short"), (None, None), ("../" * 11, "x" * 43)])
def test_malformed_credentials(local, sid, sec):
    assert local.service.status(sid, sec)[0] == 404


def test_ids_are_random_and_long():
    a = {ids.new_session_id() for _ in range(2000)}
    assert len(a) == 2000 and all(ids.SESSION_ID_RE.match(x) for x in a)
    assert all(ids.SESSION_SECRET_RE.match(ids.new_session_secret()) for _ in range(100))


def test_scheduler_failure_means_no_session(settings, clock):
    stack = LocalStack(settings, clock=clock)

    def fail(ref, s):
        raise DownstreamError("ThrottlingException")

    stack.scheduler.schedule_expiry = fail
    code, body = consent(stack.service)
    assert code == 424 and body["error"] == "dependency_unavailable"
    assert stack.sessions.count() == 0


def test_storage_failure_is_4xx_and_session_failed(settings, clock, b64):
    stack = LocalStack(settings, clock=clock)

    def fail(*a, **k):
        raise DownstreamError("SlowDown")

    stack.objects.put = fail
    code, body = consent(stack.service)
    code, r = stack.service.submit_photo(body["session_id"], body["session_secret"],
                                         {"image_base64": b64("landscape.png"), "style": "comic"})
    assert code == 424 and r["retryable"]
    assert stack.service.status(body["session_id"], body["session_secret"])[1]["status"] == "failed"


def test_consent_receipt_is_signed_and_has_no_image(local):
    code, body = consent(local.service)
    r = body["consent_receipt"]
    assert local.signer.verify(r, r["mac"])
    assert set(r) == {"type", "receipt_id", "session_ref", "decision", "notice_version", "notice_sha256",
                      "kiosk_id", "region", "issued_at", "retention_seconds", "expires_at", "mac"}


def test_notice_matches_enforced_retention(settings):
    n = settings.notice()
    assert n["retention_seconds"] == settings.retention_seconds
    assert "does not constitute legal advice" in n["disclaimer"]
    assert "No face recognition" in n["sections"][0]["text"]
