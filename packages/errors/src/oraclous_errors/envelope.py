"""Builder for the ORA-37 gateway error envelope.

Produces the exact ``{"error": {...}}`` shape defined by
``packages/errors/contract/error-envelope.schema.json`` as a plain ``dict`` — no
web-framework or pydantic dependency, so any service can serialise it however it
likes. ``tests/contract/test_error_emitter.py`` validates the output against the
JSON Schema and the forbidden-substring scanner.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from oraclous_errors.codes import CODE_POLICY, ErrorCode


@dataclass(frozen=True)
class FieldError:
    """One ``details`` entry for a VALIDATION_FAILED envelope.

    ``field`` is the offending field name or path, never its value; ``issue`` is an
    uppercase machine token (``^[A-Z][A-Z0-9_]*$``), never a reflected raw value.
    """

    field: str
    issue: str


def build_envelope(
    code: ErrorCode,
    *,
    request_id: str,
    message: str | None = None,
    retryable: bool | None = None,
    details: Sequence[FieldError] | None = None,
) -> dict[str, Any]:
    """Build a contract-conformant error envelope.

    ``message`` and ``retryable`` default to the code's curated policy. ``details``
    is required for VALIDATION_FAILED and forbidden for every other code (mirrors
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
    if code is ErrorCode.VALIDATION_FAILED:
        if not details:
            raise ValueError("VALIDATION_FAILED requires a non-empty 'details' list")
        inner["details"] = [{"field": d.field, "issue": d.issue} for d in details]
    elif details:
        raise ValueError(f"'details' is only valid for VALIDATION_FAILED, not {code.value}")
    return {"error": inner}
