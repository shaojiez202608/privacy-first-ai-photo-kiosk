"""Runtime settings and the privacy notice shown before consent.

The same ``Settings`` object is built from the parameter file (local mode, tests) or from Lambda
environment variables (cloud). The notice text is generated from the settings, so the retention
period the user agrees to is always the one the system enforces.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass

from .imaging import ImageLimits


@dataclass(frozen=True)
class Settings:
    region: str
    kiosk_id: str
    notice_version: str
    retention_seconds: int
    share_link_ttl_seconds: int
    ttl_grace_seconds: int
    max_upload_bytes: int
    max_side: int
    min_side: int
    max_aspect: float
    max_pixels: int
    model_provider: str  # transform | bedrock
    moderation_provider: str  # mock | guardrails | none
    operator_contact: str

    @property
    def limits(self) -> ImageLimits:
        return ImageLimits(self.max_upload_bytes, self.max_side, self.min_side, self.max_aspect, self.max_pixels)

    def notice(self) -> dict:
        minutes, seconds = divmod(self.retention_seconds, 60)
        retention = f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
        sections = [
            ("Purpose", "Your photo is used only to create one stylized picture for you at this kiosk. "
                        "No face recognition or identity matching is performed."),
            ("What is stored", "The photo you submit is kept only while it is being processed and is deleted "
                               "as soon as processing ends. The stylized result is kept until the session ends."),
            ("Retention", f"The session ends {retention} after you agree. All images of the session are then "
                          "deleted automatically, including stored copies and versions."),
            ("Delete now", "You can delete everything at any time with the Delete button. You receive a "
                           "deletion receipt."),
            ("Sharing", f"The download link and QR code are valid for at most "
                        f"{self.share_link_ttl_seconds} s and never longer than the session."),
            ("Records", "We keep a record that you agreed (time, notice version, a random session reference). "
                        "The record contains no image and no personal details."),
            ("Contact", self.operator_contact),
        ]
        text = "\n".join(f"{k}: {v}" for k, v in sections)
        return {
            "notice_version": self.notice_version,
            "notice_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "retention_seconds": self.retention_seconds,
            "share_link_ttl_seconds": self.share_link_ttl_seconds,
            "sections": [{"title": k, "text": v} for k, v in sections],
            "disclaimer": "Prototype for demonstration. This notice is a template and does not constitute "
                          "legal advice; its suitability for any jurisdiction must be assessed by counsel.",
        }

    @classmethod
    def from_params(cls, params: dict, region: str | None = None) -> Settings:
        s, d = params["session"], params["deploy"]
        return cls(
            region=region or d.get("region", "us-east-1"),
            kiosk_id=s["kiosk_id"],
            notice_version=s["notice_version"],
            retention_seconds=s["retention_seconds"],
            share_link_ttl_seconds=s["share_link_ttl_seconds"],
            ttl_grace_seconds=s["ttl_grace_seconds"],
            max_upload_bytes=s["max_upload_bytes"],
            max_side=s["max_side"],
            min_side=s["min_side"],
            max_aspect=s["max_aspect"],
            max_pixels=s["max_pixels"],
            model_provider=params["image_model"]["provider"],
            moderation_provider=params["moderation"]["provider"],
            operator_contact=s["operator_contact"],
        )

    def to_env(self) -> dict[str, str]:
        return {"KIOSK_SETTINGS": json.dumps(asdict(self), sort_keys=True)}

    @classmethod
    def from_env(cls) -> Settings:
        data = json.loads(os.environ["KIOSK_SETTINGS"])
        data["region"] = os.environ.get("AWS_REGION", data["region"])
        return cls(**data)
