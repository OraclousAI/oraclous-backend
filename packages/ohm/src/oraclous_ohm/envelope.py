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
    source_layer: str | None = None  # the hand-off's truth tier (additive, #514) — clamped to the
    # non-canonical floor at the dispatch boundary; a member can't self-assign a canonical tier.


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


def _ok_tool_call_ids(tool_steps: list[dict[str, Any]] | None) -> set[str]:
    """The tool_call_ids of the member's OWN steps that actually ran and succeeded (#641)."""
    ids: set[str] = set()
    for step in tool_steps or []:
        if not isinstance(step, dict):
            continue
        if step.get("kind") != "tool" or step.get("status") != "ok":
            continue
        call_id = step.get("tool_call_id")
        if isinstance(call_id, str) and call_id:
            ids.add(call_id)
    return ids


def grounding_counts(
    driving_signals: list[dict[str, Any]] | None, tool_steps: list[dict[str, Any]] | None
) -> tuple[int, int]:
    """``(grounded, total)`` claims for a tool-declaring member (#642).

    ``total`` is at least 1 even with no claims at all: a member that declared tools and cited
    nothing has exactly one unbacked obligation, so it scores 0/1 rather than a vacuous 1/1.
    """
    ok_ids = _ok_tool_call_ids(tool_steps)
    signals = [s for s in (driving_signals or []) if isinstance(s, dict)]
    grounded = sum(1 for s in signals if s.get("source_tool_call_id") in ok_ids)
    return grounded, max(1, len(signals))


def validate_grounding(
    driving_signals: list[dict[str, Any]] | None, tool_steps: list[dict[str, Any]] | None
) -> list[str]:
    """Return the grounding errors of a member's claims (empty list = every claim has a receipt).

    A claim is grounded only when its ``source_tool_call_id`` resolves to a ``status == "ok"`` tool
    step in the member's OWN trace (#641). Fail-closed on every weaker shape: no claims at all, a
    missing/null source id, an id that resolves to nothing, and an id that points at a call that
    ERRORED (run ``1fe1bcb5``'s collector cited an ``unknown_tool`` failure as its evidence).
    """
    signals = [s for s in (driving_signals or []) if isinstance(s, dict)]
    if not signals:
        return ["no driving_signals: the member made claims nothing backs"]
    ok_ids = _ok_tool_call_ids(tool_steps)
    errors: list[str] = []
    for signal in signals:
        name = signal.get("signal") or "<unnamed>"
        call_id = signal.get("source_tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            errors.append(f"claim {name!r} carries no source_tool_call_id")
        elif call_id not in ok_ids:
            errors.append(f"claim {name!r} cites {call_id!r}, which is no ok tool call of its own")
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
