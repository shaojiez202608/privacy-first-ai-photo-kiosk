"""Output masking for scripts: 12-digit numbers (account ids) are replaced before anything is printed
or written to a report."""

from __future__ import annotations

import re

ACCOUNT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")


def mask_text(text: str) -> str:
    return ACCOUNT_RE.sub("111122223333", text)
