"""oraclous-errors — the shared error taxonomy and envelope emitter.

The contract data (schema, taxonomy, forbidden-substrings, samples) lives under
``packages/errors/contract/`` and is the cross-repo source of truth. This package
is the Python emitter every backend service uses to produce the canonical error
envelope without re-declaring the shape.
"""

from __future__ import annotations

from oraclous_errors.codes import (
    CODE_POLICY,
    CodePolicy,
    ErrorCode,
    default_message,
    default_retryable,
    http_status_for,
    status_to_code,
)
from oraclous_errors.envelope import FieldError, NeedsCredential, build_envelope
from oraclous_errors.request_id import REQUEST_ID_PATTERN, new_request_id

__all__ = [
    "CODE_POLICY",
    "REQUEST_ID_PATTERN",
    "CodePolicy",
    "ErrorCode",
    "FieldError",
    "NeedsCredential",
    "build_envelope",
    "default_message",
    "default_retryable",
    "http_status_for",
    "new_request_id",
    "status_to_code",
]
