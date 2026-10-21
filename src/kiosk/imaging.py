"""Image validation and re-encoding.

Uploaded bytes are never stored as received. They are decoded, checked (format, size, dimensions,
aspect ratio), converted to RGB and written to a fresh PNG from the pixel data only. Re-encoding
drops every metadata block of the upload (EXIF, GPS position, camera serial numbers, ICC profiles,
text chunks), so none of it reaches storage, the model or the shared result.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

ALLOWED_FORMATS = frozenset({"PNG", "JPEG"})


class ImageRejected(ValueError):
    """The image failed validation. ``code`` is a short machine-readable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ImageLimits:
    max_bytes: int
    max_side: int  # longer side after downscaling
    min_side: int
    max_aspect: float  # long side / short side
    max_pixels: int  # decompression-bomb guard before decoding


@dataclass(frozen=True)
class CleanImage:
    png: bytes
    width: int
    height: int


def sanitize(data: bytes, limits: ImageLimits) -> CleanImage:
    if not isinstance(data, bytes | bytearray) or not data:
        raise ImageRejected("empty_image")
    if len(data) > limits.max_bytes:
        raise ImageRejected("image_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                fmt = probe.format
                w, h = probe.size
                if fmt not in ALLOWED_FORMATS:
                    raise ImageRejected("unsupported_format")
                if w * h > limits.max_pixels:
                    raise ImageRejected("too_many_pixels")
                probe.load()
                rgb = probe.convert("RGB")
    except ImageRejected:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombWarning,
            Image.DecompressionBombError):
        raise ImageRejected("undecodable_image") from None
    w, h = rgb.size
    if min(w, h) < limits.min_side:
        raise ImageRejected("image_too_small")
    if max(w, h) / min(w, h) > limits.max_aspect:
        raise ImageRejected("aspect_ratio_out_of_range")
    if max(w, h) > limits.max_side:
        scale = limits.max_side / max(w, h)
        rgb = rgb.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    return CleanImage(png=_encode_png(rgb), width=rgb.size[0], height=rgb.size[1])


def _encode_png(img: Image.Image) -> bytes:
    # A new image built from the pixel data alone carries no info/metadata from the source.
    fresh = Image.frombytes("RGB", img.size, img.tobytes())
    buf = io.BytesIO()
    fresh.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def has_metadata(data: bytes) -> bool:
    """True if a PNG/JPEG carries EXIF or textual metadata (used by tests and the e2e check)."""
    with Image.open(io.BytesIO(data)) as img:
        info_keys = {k for k in img.info if k not in ("dpi",)}
        return bool(info_keys) or bool(img.getexif())
