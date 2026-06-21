"""Invitation token domain (domain layer, threat T-INVITE).

A high-entropy (256-bit) URL-safe token is generated once and returned to the inviter; only its
SHA-256 hash + a lookup prefix are stored (never the raw token). Acceptance recomputes the hash and
compares in constant time. SHA-256 (not bcrypt) is correct here: the token is random + high-entropy,
not a low-entropy password, so a fast hash is safe and avoids bcrypt's 72-byte limit.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from enum import StrEnum

_TOKEN_BYTES = 32  # 256-bit
_PREFIX_LEN = 12


class InvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


def generate_invitation_token() -> tuple[str, str, str]:
    """Return ``(raw_token, prefix, token_hash)``. Only prefix + hash are persisted."""
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    return raw, raw[:_PREFIX_LEN], hash_token(raw)


def token_prefix(raw: str) -> str:
    return raw[:_PREFIX_LEN]


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_matches(raw: str, stored_hash: str) -> bool:
    """Constant-time comparison of a presented token against the stored hash."""
    return hmac.compare_digest(hash_token(raw), stored_hash)


def is_expired(expires_at: datetime | None) -> bool:
    return expires_at is not None and expires_at < datetime.now(UTC)
