"""``outcome_blockers`` derivation (domain layer; #834 ruling §B).

The per-member reason map the engine already computes and then discards (``TeamRunResult.
member_errors``, role -> reason) is collapsed into ONE free-text ``error_message`` at settle and
the structured, per-role answer is never persisted. This module is the ONE place that re-derives a
caller-visible, structured answer to "which member lost the run's deliverable, and what was it" —
read-side, off the same ``results``/``member_status``/manifest snapshot every settled run already
carries, never a second write path.

Pure; accepts a raw manifest dict (the stored snapshot both ``TeamRunOut`` and
``TeamRunRepository`` rows carry) so it needs no live ``OHMManifest`` re-validation. Mirrors
``answer_roles.sink_roles``'s posture: fail-closed to ``[]`` on anything that does not look like a
valid member list, never raises.

Sourcing (DESIGN §B.2, no step-trace parsing):
  * ``code``            <- the harness's own ``error_type`` on the member's stored result; the
                           platform fallback when the harness gave none (an older harness
                           response, or the emptiness rule fired with nothing recorded).
  * ``message``          <- the harness's own ``error_message``, capped at the existing 300-char
                           per-detail cap (``team_run_service._FAILURE_SUMMARY_MAX_DETAIL_CHARS``,
                           duplicated here as a plain int per that constant's own docstring:
                           reaching into another module's private name to stay in step would be
                           worse than the small duplication).
  * ``capability_lost``  <- named from the member's OWN declaration: the declared
                           ``outputs_schema.required`` key(s) it did not deliver.

A member surfaces here iff it is recorded ``"succeeded"`` OR ``"partial"`` (#834 criterion 5: the
emptiness condition decides regardless of which terminal status the member arrived on; the member
itself is never relabelled), is declared ``outcome_critical``, and at least one of its declared
``outputs_schema.required`` keys is missing or empty in its stored result — the SAME emptiness
rule ``packages/ohm/orchestrate.py`` uses to fail the run (ruling §A.1, widened by criterion 5),
applied read-side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: mirrors team_run_service._FAILURE_SUMMARY_MAX_DETAIL_CHARS — see the module docstring above for
#: why this is a plain duplicated int rather than a cross-layer import of a private constant.
_MESSAGE_CAP = 300
_FALLBACK_CODE = "outcome_critical_empty_output"
_FALLBACK_MESSAGE = "the member completed without delivering its declared output"


@dataclass(frozen=True)
class OutcomeBlocker:
    """One outcome-critical member that did not deliver, and why (#834). The pure domain shape;
    ``schema.engine_schemas.MemberOutcomeBlock`` is its API-facing (pydantic) twin."""

    role: str
    code: str
    message: str
    capability_lost: str


def _is_empty(value: Any) -> bool:
    """#834 ruling §A.1: empty means ``None``, ``""``, a whitespace-only string, ``[]``, or
    ``{}``. A present, non-empty value means delivered."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def derive_outcome_blockers(
    *,
    results: dict[str, Any] | None,
    member_status: dict[str, str] | None,
    manifest: dict[str, Any] | None,
) -> list[OutcomeBlocker]:
    """The run's ``outcome_blockers`` — empty on a clean run, never raising on a malformed one."""
    if not isinstance(manifest, dict):
        return []
    members = manifest.get("members")
    if not isinstance(members, list):
        return []
    results = results or {}
    member_status = member_status or {}

    blockers: list[OutcomeBlocker] = []
    for raw in members:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        if not isinstance(role, str) or not role:
            continue
        # #834 criterion 5: the emptiness condition decides regardless of which terminal status
        # the member arrived on — "succeeded" as much as "partial" (the original #749 shape: a
        # reviewer answering {"members": []} with no degrade at all still settles "succeeded").
        if member_status.get(role) not in ("succeeded", "partial") or not raw.get(
            "outcome_critical"
        ):
            continue
        outputs_schema = raw.get("outputs_schema")
        required = outputs_schema.get("required") if isinstance(outputs_schema, dict) else None
        if not isinstance(required, list) or not required:
            continue
        out = results.get(role)
        payload = out if isinstance(out, dict) else {}
        lost = [key for key in required if isinstance(key, str) and _is_empty(payload.get(key))]
        if not lost:
            continue
        error_type = payload.get("error_type")
        error_message = payload.get("error_message")
        code = error_type if isinstance(error_type, str) and error_type.strip() else _FALLBACK_CODE
        message = (
            error_message
            if isinstance(error_message, str) and error_message.strip()
            else _FALLBACK_MESSAGE
        )
        if len(message) > _MESSAGE_CAP:
            message = message[: _MESSAGE_CAP - 1] + "…"
        blockers.append(
            OutcomeBlocker(
                role=role,
                code=code,
                message=message,
                capability_lost=", ".join(lost),
            )
        )
    return blockers
