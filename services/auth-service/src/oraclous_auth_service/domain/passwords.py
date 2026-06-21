"""Password hashing + strength policy (domain layer, threat T-PWD).

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
    """The proposed password fails the strength policy.

    ``code`` is a stable machine token (e.g. ``too_short``) the gateway surfaces as the
    VALIDATION_FAILED ``issue`` so the console renders its own copy — never the raw message.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def validate_password_strength(password: str) -> None:
    """Raise :class:`PasswordPolicyError` if ``password`` is too weak. Returns None on success."""
    if len(password) < _MIN_LEN:
        raise PasswordPolicyError(
            f"password must be at least {_MIN_LEN} characters", code="too_short"
        )
    if len(password.encode("utf-8")) > _MAX_BYTES:
        raise PasswordPolicyError("password must be at most 72 bytes", code="too_long")
    if password.lower() == password or password.upper() == password:
        # require mixed case as a minimal complexity floor
        raise PasswordPolicyError(
            "password must contain both upper and lower case letters", code="missing_mixed_case"
        )
    if not any(c.isdigit() for c in password):
        raise PasswordPolicyError("password must contain at least one digit", code="missing_digit")


def hash_password(password: str) -> str:
    """Return the bcrypt hash of ``password`` (cost=12). Caller validates strength first."""
    if len(password.encode("utf-8")) > _MAX_BYTES:
        raise PasswordPolicyError("password must be at most 72 bytes", code="too_long")
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
