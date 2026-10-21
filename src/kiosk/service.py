"""Kiosk session service: consent -> photo -> processing -> share -> deletion.

All entry points return ``(http_status, body)``. Dependency failures never become 5xx: they turn into
a 4xx with a short error code (API calls) or into a ``failed`` session status with a reason code
(asynchronous processing), and the input image is deleted in every outcome.

Deletion is explicit. ``purge`` removes every object version and delete marker under the session
prefix, deletes the session record, cancels the expiry workflow and returns a signed deletion
receipt. The bucket lifecycle rule and the DynamoDB TTL are only a backstop for the case where the
explicit deletion itself could not run.
"""

from __future__ import annotations

import base64
import binascii
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import ids
from .audit import AuditLogger
from .imaging import ImageRejected, sanitize
from .ports import (
    DownstreamError,
    ImageModel,
    Invoker,
    Moderation,
    ObjectStore,
    Scheduler,
    SessionRecord,
    SessionStore,
    Signer,
)
from .settings import Settings
from .styles import STYLES, style_or_none

Response = tuple[int, dict[str, Any]]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _err(status: int, code: str, **extra: Any) -> Response:
    return status, {"error": code, **extra}


class KioskService:
    def __init__(self, settings: Settings, *, sessions: SessionStore, objects: ObjectStore, scheduler: Scheduler,
                 moderation: Moderation, model: ImageModel, signer: Signer, invoker: Invoker, audit: AuditLogger,
                 clock: Callable[[], float] = time.time) -> None:
        self.s = settings
        self.sessions, self.objects, self.scheduler = sessions, objects, scheduler
        self.moderation, self.model, self.signer, self.invoker = moderation, model, signer, invoker
        self.audit, self.clock = audit, clock

    # ------------------------------------------------------------ notice / consent

    def notice(self) -> Response:
        n = self.s.notice()
        n["styles"] = [{"key": k, "label": v.label} for k, v in STYLES.items()]
        return 200, n

    def create_session(self, body: Any) -> Response:
        if not isinstance(body, dict):
            return self._reject("request", "body_not_object")
        decision = body.get("consent")
        notice = self.s.notice()
        if decision not in ("accepted", "declined"):
            return self._reject("consent", "consent_value_invalid", field_path="consent")
        if body.get("notice_version") != notice["notice_version"] or body.get("notice_sha256") != notice["notice_sha256"]:
            self.audit.emit("consent_rejected_stale_notice", notice_version=notice["notice_version"])
            return _err(409, "notice_changed", notice_version=notice["notice_version"])
        now = self.clock()
        receipt_id = str(uuid.uuid4())
        if decision == "declined":
            # Nothing is created: no session, no object, no record. Only the audit event.
            self.audit.emit("consent_declined", receipt_id=receipt_id, decision="declined",
                            notice_version=notice["notice_version"], notice_sha256=notice["notice_sha256"])
            return 200, {"status": "declined", "message": "No photo will be taken or processed."}

        sid, secret = ids.new_session_id(), ids.new_session_secret()
        ref = ids.session_ref(sid)
        expires = int(now) + self.s.retention_seconds
        record = SessionRecord(session_ref=ref, secret_hash=ids.secret_hash(secret), consent_receipt_id=receipt_id,
                               notice_version=notice["notice_version"], notice_sha256=notice["notice_sha256"],
                               consented_at=int(now), expires_at=expires, retention_seconds=self.s.retention_seconds)
        receipt = {"type": "consent", "receipt_id": receipt_id, "session_ref": ref, "decision": "accepted",
                   "notice_version": notice["notice_version"], "notice_sha256": notice["notice_sha256"],
                   "kiosk_id": self.s.kiosk_id, "region": self.s.region, "issued_at": _iso(now),
                   "retention_seconds": self.s.retention_seconds, "expires_at": _iso(expires)}
        try:
            receipt["mac"] = self.signer.sign(receipt)
            self.sessions.put_new(record, ttl=expires + self.s.ttl_grace_seconds)
        except DownstreamError as exc:
            return self._unavailable("consent", exc)
        try:
            self.scheduler.schedule_expiry(ref, self.s.retention_seconds)
        except DownstreamError as exc:
            # Without a scheduled expiry the session must not exist: undo and refuse.
            self.sessions.delete(ref)
            return self._unavailable("consent", exc)
        self.audit.emit("consent_accepted", session_ref=ref, receipt_id=receipt_id, decision="accepted",
                        notice_version=notice["notice_version"], notice_sha256=notice["notice_sha256"],
                        retention_seconds=self.s.retention_seconds, expires_at=_iso(expires))
        return 201, {"status": "consented", "session_id": sid, "session_secret": secret,
                     "expires_at": _iso(expires), "consent_receipt": receipt}

    # ------------------------------------------------------------ photo

    def submit_photo(self, session_id: str, secret: str, body: Any) -> Response:
        rec, resp = self._authorize(session_id, secret)
        if rec is None:
            return resp
        if not isinstance(body, dict):
            return self._reject("request", "body_not_object", session_ref=rec.session_ref)
        style = style_or_none(body.get("style"))
        if style is None:
            return self._reject("request", "style_invalid", session_ref=rec.session_ref, field_path="style")
        if rec.status != "consented":
            return self._reject("request", "photo_already_submitted", session_ref=rec.session_ref, status=409)
        raw_b64 = body.get("image_base64")
        if not isinstance(raw_b64, str) or len(raw_b64) > self.s.max_upload_bytes * 4 // 3 + 8:
            return self._reject("input", "image_too_large" if isinstance(raw_b64, str) else "image_missing",
                                session_ref=rec.session_ref, field_path="image_base64")
        try:
            raw = base64.b64decode(raw_b64, validate=True)
            clean = sanitize(raw, self.s.limits)
        except (binascii.Error, ValueError) as exc:
            code = exc.code if isinstance(exc, ImageRejected) else "image_not_base64"
            self.audit.emit("photo_rejected", session_ref=rec.session_ref, stage="input", reason_code=code)
            return _err(422, code)
        key = ids.object_prefix(rec.session_ref) + "input.png"
        try:
            if not self.sessions.update(rec.session_ref, expect_status="consented", status="processing",
                                        style=style.key):
                return self._reject("request", "photo_already_submitted", session_ref=rec.session_ref, status=409)
            self.objects.put(key, clean.png, "image/png")
            self.audit.emit("photo_received", session_ref=rec.session_ref, style=style.key,
                            image_width=clean.width, image_height=clean.height)
            self.invoker.process_async(rec.session_ref)
        except DownstreamError as exc:
            self._finish_failed(rec.session_ref, "storage", exc.code)
            return self._unavailable("storage", exc, session_ref=rec.session_ref)
        return 202, {"status": "processing"}

    def process(self, ref: str) -> dict[str, Any]:
        """Asynchronous processing. Always ends with the input deleted and a terminal status."""
        t0 = self.clock()
        rec = self.sessions.get(ref)
        if rec is None or rec.status != "processing" or rec.style not in STYLES:
            self._delete_input(ref)
            return {"status": "skipped"}
        self.audit.emit("processing_started", session_ref=ref, style=rec.style, model_provider=self.model.name,
                        moderation_provider=self.moderation.name)
        prefix = ids.object_prefix(ref)
        outcome: tuple[str, str | None] = ("failed", "internal_error")
        try:
            png = self.objects.get(prefix + "input.png")
            verdict = self.moderation.check(png, "input")
            if not verdict.allowed:
                self.audit.emit("input_blocked", session_ref=ref, stage="input", categories=list(verdict.categories))
                outcome = ("rejected", "input_blocked")
                return {"status": "rejected"}
            try:
                out_raw = self.model.stylize(png, rec.style)
            except DownstreamError as exc:
                if exc.code.startswith("model_filtered"):
                    self.audit.emit("model_filtered", session_ref=ref, stage="model", reason_code=exc.code)
                    outcome = ("rejected", "model_filtered")
                    return {"status": "rejected"}
                raise
            try:
                out = sanitize(out_raw, self.s.limits.__class__(
                    max_bytes=max(self.s.max_upload_bytes, len(out_raw)), max_side=self.s.max_side,
                    min_side=self.s.min_side, max_aspect=self.s.max_aspect, max_pixels=self.s.max_pixels))
            except ImageRejected as exc:
                raise DownstreamError("model_output_" + exc.code) from None
            verdict = self.moderation.check(out.png, "output")
            if not verdict.allowed:
                self.audit.emit("output_blocked", session_ref=ref, stage="output", categories=list(verdict.categories))
                outcome = ("rejected", "output_blocked")
                return {"status": "rejected"}
            self.objects.put(prefix + "result.png", out.png, "image/png")
            outcome = ("ready", None)
            return {"status": "ready"}
        except DownstreamError as exc:
            self.audit.emit("processing_failed", session_ref=ref, stage="model", error_code=_safe_code(exc.code))
            outcome = ("failed", "downstream_error")
            return {"status": "failed"}
        except Exception as exc:  # noqa: BLE001 - last-resort guard: never leave the input behind
            self.audit.emit("processing_failed", session_ref=ref, stage="model", error_code=type(exc).__name__)
            outcome = ("failed", "internal_error")
            return {"status": "failed"}
        finally:
            self._delete_input(ref)
            status, reason = outcome
            fields: dict[str, Any] = {"status": status}
            if reason:
                fields["reason_code"] = reason
            else:
                fields["result_key"] = prefix + "result.png"
            try:
                self.sessions.update(ref, expect_status="processing", **fields)
            except DownstreamError:
                pass
            if status == "ready":
                self.audit.emit("processing_completed", session_ref=ref, status="ready",
                                duration_ms=int((self.clock() - t0) * 1000))

    def _delete_input(self, ref: str) -> None:
        # Delete the input and all of its versions right after processing ("process, then delete").
        key = ids.object_prefix(ref) + "input.png"
        try:
            r = self._purge_key(key)
            self.audit.emit("input_deleted", session_ref=ref, reason="processing_complete",
                            versions_deleted=r[0], delete_markers_deleted=r[1], remaining_versions=r[2])
        except DownstreamError as exc:
            self.audit.emit("deletion_incomplete", session_ref=ref, stage="storage", error_code=_safe_code(exc.code))

    def _purge_key(self, key: str) -> tuple[int, int, int]:
        r = self.objects.purge_key(key)
        return r.versions_deleted, r.delete_markers_deleted, r.remaining_versions

    # ------------------------------------------------------------ status / share

    def status(self, session_id: str, secret: str) -> Response:
        rec, resp = self._authorize(session_id, secret)
        if rec is None:
            return resp
        body: dict[str, Any] = {"status": rec.status, "expires_at": _iso(rec.expires_at)}
        if rec.reason_code:
            body["reason_code"] = rec.reason_code
        if rec.status == "ready" and rec.result_key:
            remaining = rec.expires_at - int(self.clock())
            ttl = max(1, min(self.s.share_link_ttl_seconds, remaining))
            url = self.objects.presign_get(rec.result_key, ttl)
            body["share"] = {"url": url, "expires_in": ttl, "expires_at": _iso(self.clock() + ttl),
                             "qr_svg": _qr_svg(url)}
            self.audit.emit("share_link_issued", session_ref=rec.session_ref, link_ttl_seconds=ttl)
        return 200, body

    # ------------------------------------------------------------ deletion

    def delete(self, session_id: str, secret: str) -> Response:
        rec, resp = self._authorize(session_id, secret, purge_if_expired=False)
        if rec is None:
            return resp
        return self._purge_response(rec.session_ref, "user_request")

    def expire(self, ref: str) -> dict[str, Any]:
        """Called by the expiry workflow when the retention period has passed."""
        if not ids.SESSION_REF_RE.match(ref or ""):
            return {"status": "invalid"}
        code, body = self._purge_response(ref, "expired")
        return {"status": "deleted" if code == 200 else "incomplete", "receipt_id": body.get("receipt", {}).get(
            "receipt_id")}

    def _purge_response(self, ref: str, reason: str) -> Response:
        try:
            receipt = self.purge(ref, reason)
        except DownstreamError as exc:
            self.audit.emit("deletion_incomplete", session_ref=ref, reason=reason, stage="storage",
                            error_code=_safe_code(exc.code))
            return _err(424, "deletion_incomplete", retryable=True)
        if receipt["remaining_versions"]:
            return 424, {"error": "deletion_incomplete", "receipt": receipt, "retryable": True}
        return 200, {"status": "deleted", "receipt": receipt}

    def purge(self, ref: str, reason: str) -> dict[str, Any]:
        prefix = ids.object_prefix(ref)
        result = self.objects.purge_prefix(prefix)
        record_deleted = self.sessions.delete(ref)
        cancelled = False
        if reason != "expired":
            try:
                cancelled = self.scheduler.cancel_expiry(ref)
            except DownstreamError:
                cancelled = False
        receipt = {"type": "deletion", "receipt_id": str(uuid.uuid4()), "session_ref": ref, "reason": reason,
                   "versions_deleted": result.versions_deleted,
                   "delete_markers_deleted": result.delete_markers_deleted,
                   "uploads_aborted": result.uploads_aborted, "remaining_versions": result.remaining_versions,
                   "record_deleted": record_deleted, "schedule_cancelled": cancelled,
                   "region": self.s.region, "deleted_at": _iso(self.clock())}
        receipt["mac"] = self.signer.sign(receipt)
        event = "session_expired" if reason in ("expired", "expired_on_access") else "session_deleted"
        if result.remaining_versions:
            event = "deletion_incomplete"
        self.audit.emit(event, session_ref=ref, receipt_id=receipt["receipt_id"], reason=reason,
                        versions_deleted=result.versions_deleted,
                        delete_markers_deleted=result.delete_markers_deleted,
                        uploads_aborted=result.uploads_aborted, remaining_versions=result.remaining_versions,
                        record_deleted=record_deleted, schedule_cancelled=cancelled)
        return receipt

    # ------------------------------------------------------------ helpers

    def _authorize(self, session_id: Any, secret: Any, *, purge_if_expired: bool = True
                   ) -> tuple[SessionRecord | None, Response]:
        """Same 404 for an unknown session and for a wrong secret, so the API is no oracle."""
        not_found = _err(404, "session_not_found")
        if not isinstance(session_id, str) or not ids.SESSION_ID_RE.match(session_id) \
                or not isinstance(secret, str) or not ids.SESSION_SECRET_RE.match(secret):
            self.audit.emit("session_access_denied", reason_code="malformed_credentials")
            return None, not_found
        ref = ids.session_ref(session_id)
        try:
            rec = self.sessions.get(ref)
        except DownstreamError as exc:
            return None, self._unavailable("request", exc)
        if rec is None or not ids.secret_matches(secret, rec.secret_hash):
            self.audit.emit("session_access_denied", reason_code="unknown_session_or_secret")
            return None, not_found
        if self.clock() >= rec.expires_at:
            if purge_if_expired:
                self._purge_response(ref, "expired_on_access")
                return None, _err(410, "session_expired")
        return rec, (200, {})

    def _reject(self, stage: str, code: str, *, session_ref: str | None = None, field_path: str | None = None,
                status: int = 400) -> Response:
        fields: dict[str, Any] = {"stage": stage, "reason_code": code}
        if session_ref:
            fields["session_ref"] = session_ref
        if field_path:
            fields["field_path"] = field_path
        self.audit.emit("request_rejected", **fields)
        return _err(status, code)

    def _unavailable(self, stage: str, exc: DownstreamError, *, session_ref: str | None = None) -> Response:
        fields: dict[str, Any] = {"stage": stage, "error_code": _safe_code(exc.code)}
        if session_ref:
            fields["session_ref"] = session_ref
        self.audit.emit("request_rejected", reason_code="dependency_unavailable", **fields)
        return _err(424, "dependency_unavailable", retryable=True)

    def _finish_failed(self, ref: str, stage: str, code: str) -> None:
        self._delete_input(ref)
        try:
            self.sessions.update(ref, status="failed", reason_code="storage_error")
        except DownstreamError:
            pass


def _safe_code(code: str) -> str:
    """AWS error codes stay as they are (``ThrottlingException``); internal snake_case codes become
    CamelCase. Anything else collapses to ``UnknownError``."""
    if "_" in code:
        code = "".join(part[:1].upper() + part[1:] for part in code.split("_"))
    cleaned = "".join(ch for ch in code if ch.isalnum())[:64]
    return cleaned if cleaned and cleaned[0].isalpha() else "UnknownError"


def _qr_svg(url: str) -> str:
    import segno

    return segno.make(url, error="m").svg_data_uri(scale=4, border=2)
