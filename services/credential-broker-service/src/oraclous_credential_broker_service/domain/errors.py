"""OAuth runtime-token error codes (ORAA-4 §21 domain layer).

Lifted from the legacy ``constants.py`` OAUTH_ERROR_CODES — the stable codes the broker returns in
its structured error response so callers can branch (re-consent, retry, etc.).
"""

from __future__ import annotations

from enum import StrEnum


class OAuthErrorCode(StrEnum):
    TOKEN_NOT_FOUND = "oauth_token_not_found"  # noqa: S105 — error code, not a secret
    TOKEN_EXPIRED = "oauth_token_expired"  # noqa: S105 — error code, not a secret
    INSUFFICIENT_SCOPES = "oauth_insufficient_scopes"
    REFRESH_FAILED = "oauth_refresh_failed"
    PROVIDER_ERROR = "oauth_provider_error"
    INVALID_PROVIDER = "oauth_invalid_provider"
    RATE_LIMITED = "oauth_rate_limited"
