"""Builder for the canonical gateway error envelope.

Produces the exact ``{"error": {...}}`` shape defined by
``packages/errors/contract/error-envelope.schema.json`` as a plain ``dict`` — no
web-framework or pydantic dependency, so any service can serialise it however it
likes. ``tests/contract/test_error_emitter.py`` validates the output against the
JSON Schema and the forbidden-substring scanner.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from oraclous_errors.codes import CODE_POLICY, ErrorCode

_ISSUE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9_.\[\]-]+$")  # field name/path charset — never @/:/space
# A credential requirement_id / provider is a machine token: alnum-led, then alnum/_/./-, capped.
# The charset forbids '/', ':', '@', and whitespace by construction, so the token can never carry a
# URL, an internal host, a path, or a secret value into a user-facing body (leak-safe at the seam).
_REQ_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")


@dataclass(frozen=True)
class FieldError:
    """One ``details`` entry for a VALIDATION_FAILED envelope.

    ``field`` is the offending field name or path, never its value; ``issue`` is an
    uppercase machine token (``^[A-Z][A-Z0-9_]*$``), never a reflected raw value.
    """

    field: str
    issue: str


@dataclass(frozen=True)
class NeedsCredential:
    """The ``needs_credential`` token on a CREDENTIALS_REQUIRED envelope.

    Names WHICH credential a caller must onboard to proceed — ``requirement_id`` (the requirement
    type, e.g. ``api_key``) and ``provider`` (e.g. ``web_search``). Both are leak-safe machine
    tokens by construction (``_REQ_RE``/``_PROVIDER_RE``): never a value, a credential id, a URL, or
    a secret. The frontend renders an onboarding prompt off this pair; it carries no ``login_url``
    (a real URL would risk surfacing an internal host — that stays service-internal).
    """

    requirement_id: str
    provider: str


def build_envelope(
    code: ErrorCode,
    *,
    request_id: str,
    message: str | None = None,
    retryable: bool | None = None,
    details: Sequence[FieldError] | None = None,
    needs_credential: NeedsCredential | None = None,
) -> dict[str, Any]:
    """Build a contract-conformant error envelope.

    ``message`` and ``retryable`` default to the code's curated policy. ``details``
    is required for VALIDATION_FAILED and forbidden for every other code; the optional
    ``needs_credential`` token is permitted only for CREDENTIALS_REQUIRED (both mirror
    the schema's if/then/else), so a misuse raises ``ValueError`` at the call site
    rather than emitting a non-conformant body.
    """
    policy = CODE_POLICY[code]
    inner: dict[str, Any] = {
        "code": code.value,
        "message": policy.default_message if message is None else message,
        "requestId": request_id,
        "retryable": policy.retryable_default if retryable is None else retryable,
    }
    if code is ErrorCode.CREDENTIALS_REQUIRED:
        if needs_credential is not None:
            # Fail-closed at the seam: a non-conformant requirement_id/provider (e.g. a value that
            # slipped past an extractor) raises here rather than being relayed in the body (§3 rule
            # 8). The charset guarantees no URL / host / secret can ride through.
            if not _REQ_RE.match(needs_credential.requirement_id):
                raise ValueError("needs_credential.requirement_id must be a capped machine token")
            if not _PROVIDER_RE.match(needs_credential.provider):
                raise ValueError("needs_credential.provider must be a capped machine token")
            inner["needs_credential"] = {
                "requirement_id": needs_credential.requirement_id,
                "provider": needs_credential.provider,
            }
    elif needs_credential is not None:
        raise ValueError(
            f"'needs_credential' is only valid for CREDENTIALS_REQUIRED, not {code.value}"
        )
    if code is ErrorCode.VALIDATION_FAILED:
        if not details:
            raise ValueError("VALIDATION_FAILED requires a non-empty 'details' list")
        # Fail-closed at the seam: a non-conformant field/issue (e.g. a reflected value that slipped
        # past an extractor) raises here rather than being relayed in the error body (§3 rule 8).
        for d in details:
            if not d.field or not _FIELD_RE.match(d.field):
                raise ValueError("detail.field must be a field name/path, never a value")
            if not _ISSUE_RE.match(d.issue):
                raise ValueError("detail.issue must be a machine token (^[A-Z][A-Z0-9_]*$)")
        inner["details"] = [{"field": d.field, "issue": d.issue} for d in details]
    elif details:
        raise ValueError(f"'details' is only valid for VALIDATION_FAILED, not {code.value}")
    return {"error": inner}
