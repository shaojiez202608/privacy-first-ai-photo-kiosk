"""Interfaces between the service and its adapters (AWS in ``kiosk.aws``, in-memory in ``kiosk.local``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class DownstreamError(Exception):
    """A dependency failed. ``code`` is a short, loggable name (AWS error code or exception class)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PurgeResult:
    versions_deleted: int
    delete_markers_deleted: int
    uploads_aborted: int
    remaining_versions: int


@dataclass(frozen=True)
class ModerationResult:
    allowed: bool
    categories: tuple[str, ...] = ()


@dataclass
class SessionRecord:
    session_ref: str
    secret_hash: str
    consent_receipt_id: str
    notice_version: str
    notice_sha256: str
    consented_at: int
    expires_at: int
    retention_seconds: int
    status: str = "consented"
    style: str | None = None
    reason_code: str | None = None
    result_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class SessionStore(Protocol):
    def put_new(self, record: SessionRecord, ttl: int) -> None: ...
    def get(self, ref: str) -> SessionRecord | None: ...
    def update(self, ref: str, *, expect_status: str | None = None, **fields: Any) -> bool: ...
    def delete(self, ref: str) -> bool: ...


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def purge_prefix(self, prefix: str) -> PurgeResult: ...
    def purge_key(self, key: str) -> PurgeResult: ...
    def count_versions(self, prefix: str) -> int: ...
    def presign_get(self, key: str, expires_in: int) -> str: ...


class Scheduler(Protocol):
    def schedule_expiry(self, ref: str, seconds: int) -> None: ...
    def cancel_expiry(self, ref: str) -> bool: ...


class Moderation(Protocol):
    name: str

    def check(self, png: bytes, source: str) -> ModerationResult: ...


class ImageModel(Protocol):
    name: str

    def stylize(self, png: bytes, style: str) -> bytes: ...


class Signer(Protocol):
    def sign(self, payload: dict[str, Any]) -> str: ...
    def verify(self, payload: dict[str, Any], mac: str) -> bool: ...


class Invoker(Protocol):
    def process_async(self, ref: str) -> None: ...
