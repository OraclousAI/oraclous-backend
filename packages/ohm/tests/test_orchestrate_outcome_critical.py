"""#834 ruling §A — an ``outcome_critical`` member's ``partial`` (degrade) fails the run only when
the member did not deliver what it declared. Modelled on ``test_orchestrate_partial_verdict.py``
(#587's own file) and must not contradict it: a PARTIAL dispatch is still recorded
``member_status == "partial"`` and downstream still runs — the ONLY thing this issue changes is
whether that ``partial`` also flips the team verdict to ``failed``.

The rule, exactly (DESIGN §A.1): a member marked ``outcome_critical`` that settles ``partial`` makes
the run FAILED when any of its declared ``outputs_schema.required`` keys is missing OR EMPTY
(``None``, ``""``, a whitespace-only string, ``[]``, ``{}``). A present, non-empty value means
delivered — the verdict is unchanged. A NON-critical member is untouched in every respect
(acceptance criterion 4 — this is a regression guard, not optional).

``test_regression_ruling1_critical_member_that_delivered_still_completes`` is the RULING-1
regression guard the design doc calls out by name and is NOT optional: it is what stops this issue
from re-litigating the ruling by failing a compile that really did produce a usable team.

**RATIFIED (orchestrator, on review of PR #1016): the member itself stays recorded
``member_status == "partial"`` — it is NEVER relabelled ``"failed"`` — even on a run whose overall
verdict is ``"failed"`` because of it.** Every test below asserts this explicitly rather than
leaving it implicit, so a reviewer does not have to re-derive it. Two independent reasons back
this, both already in the design: (1) DESIGN §C names ``_faulted_roles``, ``rerun()``'s
``nothing_to_rerun`` 409, and ``_completed_for_resume`` as SEPARATE sites that must independently
learn to treat a "partial but critical-and-empty" member as faulted/re-runnable — if the member
were simply relabelled ``"failed"`` instead, none of those three sites would need any change at
all (they already treat ``"failed"`` as re-runnable), so DESIGN would not have called them out.
(2) Relabelling would contradict #587's own pin that a PARTIAL dispatch always records
``"partial"`` (``test_orchestrate_partial_verdict.py``) — this issue narrows WHEN a ``partial``
also fails the RUN, it does not change what a PARTIAL dispatch is recorded as.

RED until the [impl] extends ``has_failure`` at ``orchestrate.py:724`` (and the same rule at the
``already_failed`` gate-precedence check, ``orchestrate.py:662``) to read the emptiness of a
critical member's declared output.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMBudget, OHMManifest, OHMMember, OHMMetadata, OHMRuntime
from oraclous_ohm.orchestrate import run_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(
    role: str,
    *,
    depends_on: list[str] | None = None,
    outcome_critical: bool = False,
    outputs_schema: dict[str, Any] | None = None,
) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=depends_on or [],
        outcome_critical=outcome_critical,
        outputs_schema=outputs_schema or {},
    )


def _human(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="human", human_role="author", depends_on=deps or [])


def _team(
    members: list[OHMMember],
    *,
    budget: OHMBudget | None = None,
) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        budget=budget,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _critical_reviewer(**kw: Any) -> OHMMember:
    return _m(
        "reviewer",
        outcome_critical=True,
        outputs_schema={"required": ["members"]},
        **kw,
    )


# ── RULING-1 regression guard — NOT OPTIONAL ────────────────────────────────────────────────────


async def test_regression_ruling1_critical_member_that_delivered_still_completes() -> None:
    """A critical member that degrades (partial) but genuinely delivered its declared, non-empty
    output must NOT fail the run — this is case (b) from ruling §A: the draft was clean and the
    reviewer benignly re-validated it past its cap (`_REVIEWER_OVERCHECK_SLACK`). Taken literally,
    "a partial on an outcome_critical member is failure" would also fail this — the ruling narrowed
    it to an EMPTY declared output specifically so this case does not fail."""

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"status": "PARTIAL", "members": ["a", "b"]}  # delivered — non-empty required key

    res = await run_team(_team([_critical_reviewer()]), dispatch)
    assert res.member_status["reviewer"] == "partial"
    assert res.status == "completed"  # delivered content → not a failure, despite the degrade


# ── a critical member's empty declared output fails the run ────────────────────────────────────


@pytest.mark.parametrize(
    "empty_value",
    [None, "", "   ", [], {}],
    ids=["none", "empty_str", "whitespace_str", "empty_list", "empty_dict"],
)
async def test_critical_member_partial_with_empty_declared_key_fails_run(empty_value: Any) -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"status": "PARTIAL", "members": empty_value}

    res = await run_team(_team([_critical_reviewer()]), dispatch)
    assert res.member_status["reviewer"] == "partial"  # still recorded partial, not re-labelled
    assert res.status == "failed"  # but an empty declared output on a critical member fails the run


async def test_critical_member_partial_with_missing_declared_key_fails_run() -> None:
    # Correction (be-test-reviewer, PR #1016 comment): this passes TODAY, before the [impl], via
    # the PRE-EXISTING #697 `validate_payload` presence check — not via this issue's new rule. That
    # check runs UNCONDITIONALLY at `orchestrate.py:574-587`, before the branch that splits
    # "succeeded" from "did its best" (partial), so a MISSING required key already fails a member
    # today regardless of outcome_critical or status. A green run here is therefore evidence the
    # #697 contract check still fires on a critical member exactly as it did before this issue —
    # NOT evidence of the new rule, which only starts to matter once the key is PRESENT-BUT-EMPTY
    # (§A.2: what the flag adds that #697 could not already see). Kept anyway as a guard that the
    # new code path does not accidentally weaken #697's existing, unconditional behaviour.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"status": "PARTIAL"}  # no "members" key at all

    res = await run_team(_team([_critical_reviewer()]), dispatch)
    assert res.status == "failed"


# ── acceptance criterion 4 — a non-critical member's degrade is untouched (regression guard) ────


async def test_non_critical_member_partial_with_empty_output_still_completes() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"status": "PARTIAL", "members": []}  # empty — but this member is NOT critical

    res = await run_team(
        _team([_m("reviewer", outputs_schema={"required": ["members"]})]), dispatch
    )
    assert res.member_status["reviewer"] == "partial"
    assert res.status == "completed"  # unchanged from today — partial stays outside failure


async def test_non_critical_member_with_no_outputs_schema_at_all_is_unaffected() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"status": "PARTIAL", "output": "best effort"}

    res = await run_team(_team([_m("a")]), dispatch)
    assert res.member_status["a"] == "partial"
    assert res.status == "completed"


# ── existing failed/blocked behaviour is unchanged by this rule ────────────────────────────────


async def test_a_genuine_failure_on_a_critical_member_still_fails_same_as_before() -> None:
    # a raised exception (not a degrade) is untouched — this rule only concerns the PARTIAL branch.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        raise RuntimeError("reviewer boom")

    res = await run_team(_team([_critical_reviewer()]), dispatch)
    assert res.member_status["reviewer"] == "failed"
    assert res.status == "failed"


async def test_a_blocked_downstream_of_a_critical_members_empty_partial_is_unchanged_shape() -> (
    None
):
    # today, a failed/blocked upstream blocks its downstream transitively (test_orchestrate.py).
    # the SAME shape must hold when the "failure" is this new empty-critical-output rule.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "reviewer":
            return {"status": "PARTIAL", "members": []}
        return {"out": member.role}

    res = await run_team(
        _team([_critical_reviewer(), _m("publisher", depends_on=["reviewer"])]), dispatch
    )
    assert res.status == "failed"
    assert "publisher" not in res.results  # downstream never dispatched


# ── orchestrate.py:725-727 — the budget-halt terminal is still outranked by a real failure ─────


async def test_critical_member_empty_output_failure_outranks_the_budget_halt() -> None:
    # mirrors test_pooled_budget.py::test_a_member_failure_outranks_the_budget_halt, but the
    # "failure" here is the NEW rule (a critical member's empty declared output), not a raise.
    spent = {"t": 0}

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "reviewer":
            return {"status": "PARTIAL", "members": []}  # fails in stage 1 (costs nothing)
        if member.role == "a":
            spent["t"] += 200  # exhausts the pool in stage 2
        return {"out": member.role}

    members = [
        _critical_reviewer(),  # stage 1 — the new rule fails this
        _m("seed"),  # stage 1 — a's clean upstream
        _m("a", depends_on=["seed"]),  # stage 2 — exhausts the pool
        _m("victim", depends_on=["a"]),  # stage 3 — budget_skipped
    ]
    res = await run_team(
        _team(members, budget=OHMBudget(max_tokens_total=150)),
        dispatch,
        cost_so_far=lambda: spent["t"],
    )
    assert res.status == "failed"  # the critical-output failure wins over the budget halt
    assert res.member_status["reviewer"] == "partial"
    assert res.member_status.get("victim") == "budget_skipped"  # the pool still halted downstream


# ── orchestrate.py:662 — the same rule holds on the gate-precedence (already_failed) path ──────


async def test_critical_member_empty_output_failure_outranks_a_pending_gate() -> None:
    # mirrors test_orchestrate.py::test_a_failed_member_in_a_parallel_branch_outranks_a_pending_gate
    # but the recorded "failure" is the new rule, in a branch parallel to the pending gate.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "reviewer":
            return {"status": "PARTIAL", "members": []}
        return {"out": member.role}

    team = _team(
        [_critical_reviewer(), _m("b"), _human("g", ["b"])]
    )  # g depends on b, not reviewer
    res = await run_team(team, dispatch, gate_decisions={})  # the gate is undecided
    assert res.status == "failed"  # NOT "paused" — the recorded failure outranks the pending gate
    assert res.member_status["reviewer"] == "partial"
    assert res.paused_at == []  # the run did not pause on the gate
