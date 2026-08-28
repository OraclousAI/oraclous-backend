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

# The ceiling on one grounding message (#685). Bounded by the run page, not by this module: every
# failed member is joined into ONE 2000-char string there, so ~six failed members still fit and the
# live payload ("the web-search credential has no remaining quota") is 47 characters. Leaves room
# for the ``grounding: `` prefix the orchestrator adds within a 300-character whole.
_MESSAGE_CAP = 280


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


def _split_tool_steps(
    tool_steps: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(succeeded, failed)`` TOOL steps of the member's trace, in order. An llm turn is neither.

    Split on ``status`` alone. A step's ``tool_call_id`` says whether a CLAIM can resolve against
    it (``_ok_tool_call_ids``); it says nothing about whether the call ran, and it is nullable in
    the stored trace — so keying the two questions off the same set would let an ``ok`` step
    carrying no id be reported as a failure and have its RESULT payload excerpted.
    """
    steps = [s for s in (tool_steps or []) if isinstance(s, dict) and s.get("kind") == "tool"]
    succeeded = [s for s in steps if s.get("status") == "ok"]
    return succeeded, [s for s in steps if s.get("status") != "ok"]


def _no_successful_call_message(errored: list[dict[str, Any]]) -> str:
    """Case A (#685): calls were made and every one failed — say so, and name the last failure.

    The member did not fabricate anything; it truthfully reported that it reached nothing, so the
    old accusation points the operator at the grounding rules instead of at the real cause (live
    run ``eb08c17d``: four searches, every one a spent-quota status from the search provider).

    Every step handed here is a NON-ok one (``_split_tool_steps``), so the excerpt can only ever
    be a failed call's ``detail`` — the connector's own diagnostic, already redacted and capped
    where the trace is built. An ``ok`` step's ``detail`` is the opposite object: the tool's RESULT
    payload (retrieved text, customer rows), which must never reach the run page (§11).

    Bounded at ``_MESSAGE_CAP``: every failed member of a run shares ONE 2000-char run-page string
    (``team_run_service`` joins them), so a generous per-member sentence silently cuts later
    members off the page entirely.
    """
    last = errored[-1]  # the LAST failure, by trace order — the most recent attempt is the cause
    count = f"{len(errored)} calls, all errored" if len(errored) > 1 else "1 call, errored"
    name = last.get("name") or "<unnamed tool>"
    message = f"no tool call succeeded ({count}) — last failure from {name!r}"
    detail = last.get("detail")
    if isinstance(detail, str) and detail.strip():
        message = f"{message}: {' '.join(detail.split())}"
    return message[:_MESSAGE_CAP]


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
        # #685: three different conditions used to share one accusing sentence. They have three
        # different owners — fix the credential/connector, fix the member's prompt or tool list,
        # or distrust the member — so the operator has to be able to tell them apart. All three
        # still FAIL the member: with no successful call there is nothing to ground (fail-closed).
        succeeded, failed = _split_tool_steps(tool_steps)
        if succeeded:
            return ["no driving_signals: the member made claims nothing backs"]
        if failed:
            return [_no_successful_call_message(failed)]
        return ["no tool call was made: the member answered without calling any of its tools"]
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
