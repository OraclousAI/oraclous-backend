"""#597 (ADR-047 §7) — Layer-1: deterministic plan guardrails for the compiler eval-set.

Run on EVERY generated manifest, cheap, no model call. This is **the importer's validator, reused**
(ADR-047 §1 "one validator, two on-ramps") — ``validate_draft`` lowers the drafted JSON to members,
diffs each member's ``tools[]`` against the surveyed catalog (ADR-032 capability-absence), and runs
the same ``assemble_and_report`` dry-run the importer uses. On top of that shared verdict this Layer
adds the two checks the assembler deliberately does NOT enforce:

* **acyclicity** — ``assemble_team`` DEMOTES a ``depends_on`` cycle to a coordinator-loop seam
  (Tarjan SCC) rather than blocking it, so a genuine plan defect (an unintended cycle) slips the
  shared validator. Layer-1 runs ``topological_stages`` explicitly so a cycle / duplicate role /
  unknown ``depends_on`` is a hard BLOCK (ADR-047 §7: "the DAG is acyclic/runnable").
* **per-agent-cap ≤ team-pool** — the runtime SILENTLY clamps a member cap above the pool
  (``resolve_member_caps`` ``min(cap, total)``, ADR-031); the eval-set is STRICTER — a plan that
  DECLARES a member cap above the pool is a malformed plan (the drafter is told to keep each
  per-member cap ≤ the pool), so Layer-1 BLOCKS it rather than relying on the silent clamp.

Fail-closed throughout: a draft that cannot be parsed BLOCKS with a gap report, never a hallucinated
GO. The returned ``GuardrailReport`` is a thin structured pass/fail + a ``render()`` that prints the
importer's ``GO: BLOCKED`` surface — the Layer-1 harness the eval-set runner calls per generation.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from oraclous_ohm.capabilities import assert_subharness_within_ceiling
from oraclous_ohm.compiler.validate import validate_draft
from oraclous_ohm.dag import topological_stages
from oraclous_ohm.errors import OHMCapabilityError, OHMDagError
from oraclous_ohm.import_ import assemble_and_report
from oraclous_ohm.manifest import OHMBudget, OHMMember, OHMOrchestration
from oraclous_ohm.parse import load_ohm
from pydantic import BaseModel, ConfigDict, Field


class GuardrailCheck(BaseModel):
    """One deterministic guardrail and its disposition (a label — never customer text)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: str


class GuardrailReport(BaseModel):
    """The Layer-1 verdict over one generated manifest. ``would_block`` mirrors the importer's
    ``ImportReport.would_block`` (any blocking reason), so the eval-set gates on the same fact-based
    boolean the compiler's reviewer does."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    would_block: bool
    blocking: list[str] = Field(default_factory=list)
    checks: list[GuardrailCheck] = Field(default_factory=list)

    def render(self) -> str:
        """The importer's ``GO`` surface (ADR-047 §1): a ``GO:`` decision line + a ``BLOCK`` line
        per blocking reason — so a Layer-1 BLOCK reads identically to a filesystem-import BLOCK."""
        lines = [f"Plan guardrails: {len(self.checks)} checks"]
        for c in self.checks:
            lines.append(f"  {'ok  ' if c.passed else 'FAIL'} {c.name}: {c.detail}")
        lines.append("  GO: BLOCKED" if self.would_block else "  GO: ready")
        lines.extend(f"    BLOCK {b}" for b in self.blocking)
        return "\n".join(lines)


_ORG_NS = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _to_data(draft: str | dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a draft to a dict — peel the JSON team out of a member's prose output (#599), pass
    a dict through. Returns ``None`` when no JSON team manifest is present (blocked upstream)."""
    if isinstance(draft, dict):
        return draft
    match = re.search(r"\{.*\}", draft, re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_members(raw_members: list[Any]) -> tuple[list[OHMMember], str | None]:
    """Parse members fail-closed. A member that fails schema (e.g. a human without ``human_role``)
    returns a blocking reason rather than crashing — the disposition ``validate_draft`` gives."""
    members: list[OHMMember] = []
    for raw in raw_members:
        try:
            members.append(OHMMember.model_validate(raw))
        except Exception:  # noqa: BLE001 — a malformed member is a plan defect, fail-closed
            return [], "a draft member failed schema validation"
    return members, None


def _parse_budget(raw_budget: Any) -> OHMBudget | None:
    if not isinstance(raw_budget, dict):
        return None
    try:
        return OHMBudget.model_validate(raw_budget)
    except Exception:  # noqa: BLE001 — a malformed budget BLOCKS (the caller flags it, never skips)
        return None


def _parse_orchestration(raw_orch: Any) -> tuple[OHMOrchestration | None, list[str]]:
    """Parse a draft orchestration fail-closed, mirroring ``validate_draft`` so BOTH the catalog and
    no-catalog on-ramps validate it identically (a malformed orchestration BLOCKS, never slips)."""
    if not isinstance(raw_orch, dict):
        return None, []
    try:
        return OHMOrchestration.model_validate(raw_orch), []
    except Exception:  # noqa: BLE001 — a malformed orchestration is a plan defect, fail-closed
        return None, ["F-DRAFT-INVALID: the draft orchestration failed schema validation"]


def _cap_within_pool(members: list[OHMMember], budget: OHMBudget | None) -> list[str]:
    """STRICTER than the runtime clamp (ADR-031): a member DECLARING a cap above the team pool is
    a malformed plan. (The runtime would silently ``min(cap, total)`` — the eval-set blocks it.)
    Checks BOTH each member's own cap AND the budget-level per-member DEFAULT against the pool — a
    default above the pool grants every member-without-its-own-cap more than the whole team."""
    if budget is None:
        return []
    out: list[str] = []
    # the team-wide per-member DEFAULT must itself sit within the pool (else every defaulted member
    # would resolve above it — the runtime clamps each, but the plan is still malformed).
    if (
        budget.max_tokens_total is not None
        and budget.max_tokens_per_member is not None
        and budget.max_tokens_per_member > budget.max_tokens_total
    ):
        out.append(
            f"F-CAP-OVER-POOL: budget.max_tokens_per_member {budget.max_tokens_per_member} "
            f"exceeds the team pool {budget.max_tokens_total}"
        )
    if (
        budget.max_tool_calls_total is not None
        and budget.max_tool_calls_per_member is not None
        and budget.max_tool_calls_per_member > budget.max_tool_calls_total
    ):
        out.append(
            f"F-CAP-OVER-POOL: budget.max_tool_calls_per_member {budget.max_tool_calls_per_member} "
            f"exceeds the team pool {budget.max_tool_calls_total}"
        )
    for m in members:
        if (
            budget.max_tokens_total is not None
            and m.max_tokens is not None
            and m.max_tokens > budget.max_tokens_total
        ):
            out.append(
                f"F-CAP-OVER-POOL: member {m.role!r} max_tokens {m.max_tokens} "
                f"exceeds the team pool {budget.max_tokens_total}"
            )
        if (
            budget.max_tool_calls_total is not None
            and m.max_tool_calls is not None
            and m.max_tool_calls > budget.max_tool_calls_total
        ):
            out.append(
                f"F-CAP-OVER-POOL: member {m.role!r} max_tool_calls {m.max_tool_calls} "
                f"exceeds the team pool {budget.max_tool_calls_total}"
            )
    return out


def _subharness_ceilings(
    members: list[OHMMember], sub_harnesses: dict[str, dict[str, Any]]
) -> list[str]:
    """ADR-032 cross-member ceiling: a member's sub-harness can only NARROW its ``tools[]`` ceiling,
    never widen it. A sub declaring a capability the member never declared is a violation."""
    by_role = {m.role: m for m in members}
    out: list[str] = []
    for role, sub_doc in sub_harnesses.items():
        member = by_role.get(role)
        if member is None:
            continue
        try:
            sub = load_ohm(sub_doc)
        except Exception:  # noqa: BLE001 — a malformed sub-harness blocks, fail-closed
            out.append(f"F-SUBHARNESS-INVALID: member {role!r} sub-harness failed to load")
            continue
        try:
            assert_subharness_within_ceiling(member, sub)
        except OHMCapabilityError as exc:
            out.append(f"F-CEILING-EXCEEDED: member {role!r} sub-harness widens its ceiling: {exc}")
    return out


def run_plan_guardrails(
    draft: str | dict[str, Any],
    *,
    owner_organization_id: uuid.UUID = _ORG_NS,
    catalog: Any = None,
    sub_harnesses: dict[str, dict[str, Any]] | None = None,
) -> GuardrailReport:
    """Layer-1 (ADR-047 §7): run the deterministic plan guardrails over one generated manifest.

    ``draft`` is the drafted Team Harness (a dict, or a member's prose output with embedded JSON).
    ``catalog`` is the surveyed capability catalog (names/refs/dicts); when given, every member tool
    must resolve to it (ADR-032 capability-absence); when ``None``, the capability diff is skipped
    and only the shared assembly dry-run runs. Returns a fail-closed pass/fail + the gap report.
    """
    checks: list[GuardrailCheck] = []
    blocking: list[str] = []

    data = _to_data(draft)
    if data is None or not isinstance(data.get("members"), list):
        checks.append(
            GuardrailCheck(name="schema", passed=False, detail="not an OHM team manifest")
        )
        return GuardrailReport(
            passed=False,
            would_block=True,
            blocking=["F-DRAFT-INVALID: the draft is not an OHM team manifest with members[]"],
            checks=checks,
        )

    members, parse_err = _parse_members(data["members"])
    if parse_err is not None:
        checks.append(GuardrailCheck(name="schema", passed=False, detail=parse_err))
        # cannot run the structural checks without parsed members — fail closed here.
        return GuardrailReport(
            passed=False,
            would_block=True,
            blocking=[f"F-DRAFT-INVALID: {parse_err}"],
            checks=checks,
        )
    checks.append(
        GuardrailCheck(name="schema", passed=True, detail=f"{len(members)} members parsed")
    )

    # acyclic/runnable DAG — the check the assembler demotes-to-a-loop rather than enforcing.
    try:
        stages = topological_stages(members)
        checks.append(
            GuardrailCheck(name="dag_acyclic", passed=True, detail=f"{len(stages)} stages")
        )
    except OHMDagError as exc:
        checks.append(GuardrailCheck(name="dag_acyclic", passed=False, detail=str(exc)))
        blocking.append(f"F-DAG-INVALID: {exc}")

    # capability-absence + assembly — the shared validator (one validator, two on-ramps). Both
    # branches validate the orchestration identically (the no-catalog branch used to drop it). The
    # whole call is wrapped fail-closed: a validator crash BLOCKS, never a hallucinated GO (the
    # "fail-closed throughout" contract).
    try:
        if catalog is not None:
            shared = validate_draft(
                data, catalog, owner_organization_id=owner_organization_id, name="eval-plan"
            )
            shared_block, shared_reasons = shared["would_block"], list(shared["blocking"])
        else:
            orchestration, orch_reasons = _parse_orchestration(data.get("orchestration"))
            result = assemble_and_report(
                "eval-plan",
                members,
                owner_organization_id=owner_organization_id,
                shape="compiled",
                orchestration=orchestration,
            )
            shared_block = result.report.would_block or bool(orch_reasons)
            shared_reasons = list(result.report.blocking) + orch_reasons
    except Exception as exc:  # noqa: BLE001 — fail-closed: a validator crash BLOCKS, never a GO
        shared_block = True
        shared_reasons = [f"F-VALIDATOR-ERROR: the shared validator failed ({type(exc).__name__})"]
    checks.append(
        GuardrailCheck(
            name="capability_absence",
            passed=not shared_block,
            detail="ok" if not shared_block else "; ".join(shared_reasons),
        )
    )
    if shared_block:
        blocking.extend(shared_reasons)

    # per-agent-cap ≤ team-pool — STRICTER than the runtime clamp. A present-but-INVALID budget must
    # BLOCK (never silently disable this stricter check and report GO: ready) — distinguish it from
    # a genuinely absent budget.
    raw_budget = data.get("budget")
    budget = _parse_budget(raw_budget)
    cap_reasons = _cap_within_pool(members, budget)
    if raw_budget is not None and budget is None:
        cap_reasons.append("F-DRAFT-INVALID: the draft budget failed schema validation")
    checks.append(
        GuardrailCheck(
            name="cap_within_pool",
            passed=not cap_reasons,
            detail="ok" if not cap_reasons else "; ".join(cap_reasons),
        )
    )
    blocking.extend(cap_reasons)

    # cross-member sub-harness ceiling (ADR-032) — only when sub-harnesses are supplied.
    if sub_harnesses:
        ceiling_reasons = _subharness_ceilings(members, sub_harnesses)
        checks.append(
            GuardrailCheck(
                name="subharness_ceiling",
                passed=not ceiling_reasons,
                detail="ok" if not ceiling_reasons else "; ".join(ceiling_reasons),
            )
        )
        blocking.extend(ceiling_reasons)

    return GuardrailReport(
        passed=not blocking,
        would_block=bool(blocking),
        blocking=blocking,
        checks=checks,
    )
