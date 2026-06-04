"""Password hashing + strength policy (ORAA-4 §21 domain layer, threat T-PWD).

bcrypt cost=12 (pinned). The raw password is never logged or persisted — only the bcrypt hash.
bcrypt silently truncates input beyond 72 bytes, which would let a long password be matched by any
72-byte prefix; we reject over-length input explicitly rather than truncate.
"""

from __future__ import annotations

import bcrypt

_BCRYPT_ROUNDS = 12
_MAX_BYTES = 72  # bcrypt's hard input limit
_MIN_LEN = 8


class PasswordPolicyError(ValueError):
    """The proposed password fails the strength policy."""


def validate_password_strength(password: str) -> None:
    """Raise :class:`PasswordPolicyError` if ``password`` is too weak. Returns None on success."""
    if len(password) < _MIN_LEN:
        raise PasswordPolicyError(f"password must be at least {_MIN_LEN} characters")
    if len(password.encode("utf-8")) > _MAX_BYTES:
        raise PasswordPolicyError("password must be at most 72 bytes")
    if password.lower() == password or password.upper() == password:
        # require mixed case as a minimal complexity floor
        raise PasswordPolicyError("password must contain both upper and lower case letters")
    if not any(c.isdigit() for c in password):
        raise PasswordPolicyError("password must contain at least one digit")


def hash_password(password: str) -> str:
    """Return the bcrypt hash of ``password`` (cost=12). Caller validates strength first."""
    if len(password.encode("utf-8")) > _MAX_BYTES:
        raise PasswordPolicyError("password must be at most 72 bytes")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode(
        "utf-8"
    )


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-time check of ``password`` against a stored hash. False if no hash (OAuth-only)."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False
