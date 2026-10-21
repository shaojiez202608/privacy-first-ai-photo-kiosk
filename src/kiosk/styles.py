"""The fixed set of styles. Prompts are server-side constants.

No text from the request, from the image or from any model output is ever put into a prompt. The
kiosk only sends a style key, which must be one of the keys below. This is the structural control
against instructions embedded in an image (THREAT_MODEL.md, T4): there is no channel through which
image content could become an instruction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Style:
    key: str
    label: str
    prompt: str
    style_preset: str  # Stability style_preset enum value (Control Structure)


STYLES: dict[str, Style] = {
    s.key: s
    for s in (
        Style("watercolor", "Watercolor", "soft watercolor painting, pastel colors, paper texture", "fantasy-art"),
        Style("comic", "Comic", "bold comic book illustration, clean ink outlines, flat colors", "comic-book"),
        Style("pixel", "Pixel art", "retro pixel art, limited color palette, crisp pixels", "pixel-art"),
    )
}

NEGATIVE_PROMPT = "text, watermark, logo, signature"


def style_or_none(key: object) -> Style | None:
    return STYLES.get(key) if isinstance(key, str) else None
