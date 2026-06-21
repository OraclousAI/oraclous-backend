"""The closed error-code taxonomy and its server-side policy.

Mirrors ``packages/errors/contract/error-code-taxonomy.json`` and the curated
message of each ``packages/errors/contract/samples/<CODE>.json``. The contract
JSON is the source of truth; ``tests/contract/test_error_emitter.py`` asserts this
module never drifts from it. The values are duplicated in Python (not loaded at
runtime) so the installed wheel stays self-contained — the ``contract/`` directory
is intentionally not shipped in the wheel.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    """The 14-value closed taxonomy. The frontend branches only on this code."""

    VALIDATION_FAILED = "VALIDATION_FAILED"
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    CREDENTIALS_REQUIRED = "CREDENTIALS_REQUIRED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"


@dataclass(frozen=True)
class CodePolicy:
    """Server-side defaults for a code: the guidance HTTP status, the default
    ``retryable`` flag, and the curated generic message. The message never reflects
    request content (Interface Contracts §3)."""

    http_status: int
    retryable_default: bool
    default_message: str


CODE_POLICY: dict[ErrorCode, CodePolicy] = {
    ErrorCode.VALIDATION_FAILED: CodePolicy(400, False, "One or more fields are invalid."),
    ErrorCode.MALFORMED_REQUEST: CodePolicy(400, False, "The request body could not be parsed."),
    ErrorCode.UNAUTHENTICATED: CodePolicy(401, False, "Authentication is required."),
    ErrorCode.UNAUTHORIZED: CodePolicy(
        403, False, "You do not have permission to perform this action."
    ),
    ErrorCode.NOT_FOUND: CodePolicy(404, False, "The requested resource was not found."),
    ErrorCode.METHOD_NOT_ALLOWED: CodePolicy(
        405, False, "That method is not allowed on this resource."
    ),
    ErrorCode.CONFLICT: CodePolicy(
        409, False, "The request conflicts with the current state of the resource."
    ),
    ErrorCode.CREDENTIALS_REQUIRED: CodePolicy(
        409, False, "A required credential is missing or needs authorization."
    ),
    ErrorCode.PAYLOAD_TOO_LARGE: CodePolicy(413, False, "The request payload is too large."),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: CodePolicy(
        415, False, "The request content type is not supported."
    ),
    ErrorCode.RATE_LIMITED: CodePolicy(429, True, "Too many requests. Please retry later."),
    ErrorCode.INTERNAL_ERROR: CodePolicy(500, False, "An unexpected error occurred."),
    ErrorCode.SERVICE_UNAVAILABLE: CodePolicy(503, True, "The service is temporarily unavailable."),
    ErrorCode.GATEWAY_TIMEOUT: CodePolicy(504, True, "The upstream service timed out."),
}


def http_status_for(code: ErrorCode) -> int:
    """The guidance HTTP status for a code (taxonomy default)."""
    return CODE_POLICY[code].http_status


def default_retryable(code: ErrorCode) -> bool:
    """The default ``retryable`` flag for a code (server may override per-call)."""
    return CODE_POLICY[code].retryable_default


def default_message(code: ErrorCode) -> str:
    """The curated generic message for a code (never reflects request content)."""
    return CODE_POLICY[code].default_message


# Upstream HTTP error status -> canonical code, for normalising a proxied
# upstream's error response into the envelope. 400 maps to MALFORMED_REQUEST,
# never VALIDATION_FAILED — the gateway cannot synthesise the field-level
# ``details`` that an opaque upstream body would require. 502 has no code in the
# closed enum, so an unreachable upstream becomes SERVICE_UNAVAILABLE (retryable),
# consistent with 503.
_UPSTREAM_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.MALFORMED_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.UNAUTHORIZED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.SERVICE_UNAVAILABLE,
    503: ErrorCode.SERVICE_UNAVAILABLE,
    504: ErrorCode.GATEWAY_TIMEOUT,
}


def status_to_code(status: int) -> ErrorCode:
    """Map a proxied upstream's HTTP error status to a canonical code.

    Never returns VALIDATION_FAILED (that code requires field-level ``details`` the
    gateway cannot reconstruct from an opaque upstream body). Unmapped 4xx fall
    back to MALFORMED_REQUEST; unmapped 5xx fall back to SERVICE_UNAVAILABLE.
    """
    mapped = _UPSTREAM_STATUS_TO_CODE.get(status)
    if mapped is not None:
        return mapped
    if 400 <= status < 500:
        return ErrorCode.MALFORMED_REQUEST
    return ErrorCode.SERVICE_UNAVAILABLE
