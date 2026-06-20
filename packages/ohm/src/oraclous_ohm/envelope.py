"""The typed hand-off envelope — the inter-member medium (ADR-035 §3).

The structured successor to the round-table's flattened, 4000-char-truncated context string
(``roundtable_service._render_context``): a member→member payload, validated against the *producing*
member's ``outputs_schema`` at the hand-off boundary (fail-closed — a bad payload is an error, not a
silent truncation). It carries **data only, never capability**: receiving an envelope does not widen
the receiver's ``tools[]`` ceiling (ADR-032 §1) — there is deliberately no capability field here.
Lives in ``packages/ohm`` beside the schema it references, per ADR-035 §3. Pure; I/O-free.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.errors import OHMHandoffError
from oraclous_ohm.manifest import OHMMember


class HandoffEnvelope(BaseModel):
    """A typed member→member hand-off (ADR-035 §3). Data only — no capability is ever carried."""

    model_config = ConfigDict(extra="ignore")

    from_role: str  # the producing member
    to_role: str  # the consuming member (a depends_on edge)
    objective_slice: str = ""  # the specific sub-goal this hand-off addresses
    payload: dict[str, Any] = Field(
        default_factory=dict
    )  # validated vs the producer's outputs_schema
    provenance_ref: str | None = None  # the sub-run that produced it (one provenance stream)
    cursor: str | None = None  # optional continuation token for streamed/paginated work


def validate_payload(payload: dict[str, Any], outputs_schema: dict[str, Any]) -> list[str]:
    """Return validation errors of ``payload`` against ``outputs_schema`` (empty list = valid).

    Lenient when no schema is declared (an empty ``outputs_schema`` imposes no contract). When the
    schema declares ``required`` keys, every one must be present — fail-closed on a missing key.
    """
    if not outputs_schema:
        return []
    errors: list[str] = []
    required = outputs_schema.get("required", [])
    if isinstance(required, list):
        errors.extend(f"missing required output {key!r}" for key in required if key not in payload)
    return errors


def build_handoff(
    from_member: OHMMember,
    to_member: OHMMember,
    payload: dict[str, Any],
    *,
    objective_slice: str = "",
    provenance_ref: str | None = None,
    cursor: str | None = None,
) -> HandoffEnvelope:
    """Build a ``HandoffEnvelope``, validating ``payload`` vs the producer's ``outputs_schema``.

    Fail-closed: a payload that violates the producer's declared ``outputs_schema`` raises
    ``OHMHandoffError`` rather than threading a malformed (or silently truncated) hand-off.
    """
    errors = validate_payload(payload, from_member.outputs_schema)
    if errors:
        raise OHMHandoffError(
            f"hand-off {from_member.role!r}->{to_member.role!r} invalid: {'; '.join(errors)}"
        )
    return HandoffEnvelope(
        from_role=from_member.role,
        to_role=to_member.role,
        objective_slice=objective_slice,
        payload=payload,
        provenance_ref=provenance_ref,
        cursor=cursor,
    )
