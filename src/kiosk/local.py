"""In-memory adapters for local mode and tests. No AWS, no network, no Docker.

``MemoryObjectStore`` imitates a *versioned* bucket: a plain delete only adds a delete marker and
keeps every earlier version, exactly like S3 with versioning enabled. That is what makes the
deletion tests meaningful: a naive "delete the key" leaves residual versions, and the tests show that
``purge_prefix`` leaves none.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from PIL import Image, ImageFilter, ImageOps

from .ports import DownstreamError, ModerationResult, PurgeResult, SessionRecord

# ---------------------------------------------------------------- sessions


class MemorySessionStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[SessionRecord, int]] = {}
        self._lock = threading.Lock()

    def put_new(self, record: SessionRecord, ttl: int) -> None:
        with self._lock:
            if record.session_ref in self._items:
                raise DownstreamError("ConditionalCheckFailedException")
            self._items[record.session_ref] = (record, ttl)

    def get(self, ref: str) -> SessionRecord | None:
        with self._lock:
            item = self._items.get(ref)
            return None if item is None else SessionRecord(**{**item[0].__dict__})

    def update(self, ref: str, *, expect_status: str | None = None, **fields: Any) -> bool:
        with self._lock:
            item = self._items.get(ref)
            if item is None or (expect_status is not None and item[0].status != expect_status):
                return False
            for k, v in fields.items():
                setattr(item[0], k, v)
            return True

    def delete(self, ref: str) -> bool:
        with self._lock:
            return self._items.pop(ref, None) is not None

    def count(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------- objects


class MemoryObjectStore:
    """Versioned object store. ``versions[key]`` is a list of dicts: version_id, data, marker."""

    def __init__(self, share_base_url: str = "http://127.0.0.1:8080/share",
                 clock: Callable[[], float] = time.time) -> None:
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self._share_key = secrets.token_bytes(32)
        self._base = share_base_url
        self._clock = clock
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        with self._lock:
            self.versions.setdefault(key, []).append(
                {"version_id": secrets.token_hex(8), "data": bytes(data), "marker": False, "type": content_type})

    def get(self, key: str) -> bytes:
        with self._lock:
            vs = self.versions.get(key)
            if not vs or vs[-1]["marker"]:
                raise DownstreamError("NoSuchKey")
            return vs[-1]["data"]

    def naive_delete(self, key: str) -> None:
        """What a plain DeleteObject does on a versioned bucket: add a delete marker, keep the data."""
        with self._lock:
            self.versions.setdefault(key, []).append(
                {"version_id": secrets.token_hex(8), "data": b"", "marker": True, "type": ""})

    def purge_prefix(self, prefix: str) -> PurgeResult:
        with self._lock:
            keys = [k for k in self.versions if k.startswith(prefix)]
            versions = sum(1 for k in keys for v in self.versions[k] if not v["marker"])
            markers = sum(1 for k in keys for v in self.versions[k] if v["marker"])
            for k in keys:
                del self.versions[k]
            remaining = sum(len(v) for k, v in self.versions.items() if k.startswith(prefix))
        return PurgeResult(versions, markers, 0, remaining)

    def purge_key(self, key: str) -> PurgeResult:
        with self._lock:
            vs = self.versions.pop(key, [])
        return PurgeResult(sum(1 for v in vs if not v["marker"]), sum(1 for v in vs if v["marker"]), 0, 0)

    def count_versions(self, prefix: str) -> int:
        with self._lock:
            return sum(len(v) for k, v in self.versions.items() if k.startswith(prefix))

    def total_versions(self) -> int:
        with self._lock:
            return sum(len(v) for v in self.versions.values())

    # Local stand-in for a presigned URL: the same properties (bearer, bound to one key, expiry
    # checked at request time) with an HMAC instead of SigV4.
    def presign_get(self, key: str, expires_in: int) -> str:
        exp = int(self._clock()) + int(expires_in)
        sig = self._sig(key, exp)
        return f"{self._base}?{urllib.parse.urlencode({'k': key, 'exp': exp, 'sig': sig})}"

    def resolve_share(self, query: str) -> tuple[int, bytes | None]:
        q = urllib.parse.parse_qs(query)
        try:
            key, exp, sig = q["k"][0], int(q["exp"][0]), q["sig"][0]
        except (KeyError, ValueError, IndexError):
            return 400, None
        if not hmac.compare_digest(self._sig(key, exp), sig):
            return 403, None
        if self._clock() > exp:
            return 403, None
        try:
            return 200, self.get(key)
        except DownstreamError:
            return 404, None

    def _sig(self, key: str, exp: int) -> str:
        return hmac.new(self._share_key, f"{key}|{exp}".encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- scheduler


class LocalScheduler:
    """Records due times; ``due()`` returns the sessions whose wait has elapsed (the local server
    polls it, the way the Step Functions Wait state would fire)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._due: dict[str, float] = {}
        self._clock = clock
        self._lock = threading.Lock()

    def schedule_expiry(self, ref: str, seconds: int) -> None:
        with self._lock:
            self._due[ref] = self._clock() + seconds

    def cancel_expiry(self, ref: str) -> bool:
        with self._lock:
            return self._due.pop(ref, None) is not None

    def due(self) -> list[str]:
        now = self._clock()
        with self._lock:
            refs = [r for r, t in self._due.items() if t <= now]
            for r in refs:
                del self._due[r]
            return refs

    def pending(self) -> int:
        return len(self._due)


# ---------------------------------------------------------------- moderation (test double)

# Test markers drawn by scripts/gen_samples.py and the kiosk page. The mock is NOT a classifier: it
# recognises these two colour patches only, so that the reject path can be demonstrated offline.
MARKERS = {
    (255, 0, 255): "VIOLENCE",  # "unsafe content" test sample
    (0, 255, 255): "PROMPT_ATTACK",  # "embedded instruction" test sample
}
MARKER_BOX = (2, 2, 14, 14)


def find_marker(png: bytes) -> str | None:
    with Image.open(io.BytesIO(png)) as img:
        rgb = img.convert("RGB")
        if min(rgb.size) < 16:
            return None
        patch = rgb.crop(MARKER_BOX).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    for colour, category in MARKERS.items():
        if all(abs(a - b) <= 24 for a, b in zip(patch, colour, strict=True)):
            return category
    return None


class MockModeration:
    name = "mock"

    def check(self, png: bytes, source: str) -> ModerationResult:
        cat = find_marker(png)
        return ModerationResult(allowed=cat is None, categories=(cat,) if cat else ())


class NoModeration:
    name = "none"

    def check(self, png: bytes, source: str) -> ModerationResult:
        return ModerationResult(allowed=True)


# ---------------------------------------------------------------- image "model"


class TransformModel:
    """Deterministic Pillow transformation per style. Used in local mode and as the cloud provider
    when no in-region Bedrock image model is configured. It is not a generative model."""

    name = "transform"

    def stylize(self, png: bytes, style: str) -> bytes:
        with Image.open(io.BytesIO(png)) as src:
            img = src.convert("RGB")
        if style == "watercolor":
            out = ImageOps.posterize(img.filter(ImageFilter.GaussianBlur(3)), 4)
            out = Image.blend(out, Image.new("RGB", out.size, (250, 245, 235)), 0.25)
        elif style == "comic":
            edges = ImageOps.invert(img.convert("L").filter(ImageFilter.FIND_EDGES)).point(lambda p: 0 if p < 200 else 255)
            out = Image.composite(ImageOps.posterize(img, 3), Image.new("RGB", img.size, (0, 0, 0)), edges)
        elif style == "pixel":
            w, h = img.size
            small = img.resize((max(1, w // 16), max(1, h // 16)), Image.Resampling.BILINEAR)
            out = ImageOps.posterize(small, 3).resize((w, h), Image.Resampling.NEAREST)
        else:
            raise DownstreamError("UnknownStyle")
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()


# ---------------------------------------------------------------- receipts


class LocalHmacSigner:
    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or secrets.token_bytes(32)

    def sign(self, payload: dict[str, Any]) -> str:
        return base64.b64encode(hmac.new(self._key, canonical(payload), hashlib.sha256).digest()).decode()

    def verify(self, payload: dict[str, Any], mac: str) -> bool:
        return hmac.compare_digest(self.sign(payload), mac)


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps({k: v for k, v in payload.items() if k != "mac"}, sort_keys=True,
                      separators=(",", ":")).encode()


class InlineInvoker:
    """Runs processing in the calling thread (tests) or in a background thread (local server)."""

    def __init__(self, background: bool = False) -> None:
        self.target: Callable[[str], Any] | None = None
        self._background = background

    def process_async(self, ref: str) -> None:
        if self.target is None:
            raise DownstreamError("InvokerNotBound")
        if self._background:
            threading.Thread(target=self.target, args=(ref,), daemon=True).start()
        else:
            self.target(ref)
