"""Session identifiers.

* ``session_id``: 192 random bits (``secrets.token_urlsafe(24)``), sent in the URL path. Not stored.
* ``session_secret``: 256 random bits, sent in the ``X-Session-Secret`` header. Only its SHA-256 is stored.
* ``session_ref``: SHA-256 of the session id (first 32 hex characters). Used as the database key,
  the object-key prefix, the Step Functions execution name and in audit events. It is derived from a
  random value, so it is not linked to a person, and it cannot be turned back into the session id.

Knowing a ``session_ref`` (for example from the audit log) does not let anyone act on a session: every
call needs the session id *and* the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
SESSION_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
SESSION_REF_RE = re.compile(r"^[0-9a-f]{32}$")


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def new_session_secret() -> str:
    return secrets.token_urlsafe(32)


def session_ref(session_id: str) -> str:
    return hashlib.sha256(b"kiosk-session-ref:" + session_id.encode()).hexdigest()[:32]


def secret_hash(secret: str) -> str:
    return hashlib.sha256(b"kiosk-session-secret:" + secret.encode()).hexdigest()


def secret_matches(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(secret_hash(secret), stored_hash)


def object_prefix(ref: str) -> str:
    """Every object of a session lives under this prefix (and nothing else does)."""
    if not SESSION_REF_RE.match(ref):
        raise ValueError("invalid session_ref")
    return f"s/{ref}/"
