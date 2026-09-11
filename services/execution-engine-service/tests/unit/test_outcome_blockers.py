"""#834 ruling §B — the per-member reason map the engine already computes and then throws away
(``member_errors``, role -> reason) gets a real API surface: ``outcome_blockers`` on ``TeamRunOut``,
mirrored on ``TeamRunStatusOut``.

**The shape, ruled (DESIGN §B.1)**, following the ``simulated``/``unverified_links`` precedent
(#907/#944) exactly: a LIST of typed objects (never a bare map), each carrying ``role``, ``code``,
``message``, ``capability_lost``. EMPTY when clean and NEVER absent — a consumer reads it on every
run, and a missing key would make "nothing was lost" indistinguishable from "not checked" (the same
#944 argument). Mirrored onto ``TeamRunStatusOut`` per the #944 review's OPTIONAL-13 rule: the light
status and the full detail must never disagree about whether the run produced its deliverable.

**Where the values come from (DESIGN §B.2), no step-trace parsing:**
  * ``code``            <- the harness ``error_type``; the platform code when the emptiness rule
                           fired and the harness gave none.
  * ``message``          <- the harness ``error_message``, capped.
  * ``capability_lost``  <- named from the member's OWN declaration (which required keys it did not
                           deliver) — acceptance criterion 3.

This file also pins the companion lift this surface depends on: the engine drops a PARTIAL member's
``error_type``/``error_message`` today (``team_run.py:787`` only reads them when the status is
neither SUCCEEDED nor PARTIAL; the payload built at ``:802-829`` never lifts them). Lifting them
follows the #907 (``simulated``)/#944 (``unverified_links``) precedent exactly, including their
back-compat posture: absent on an older harness response, never a crash.

RED until the [impl] lands: neither the lift nor ``outcome_blockers``/``MemberOutcomeBlock`` exist
today. Modelled directly on ``test_member_unverified_links.py`` — read it first.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.schema.engine_schemas import TeamRunOut
from oraclous_execution_engine_service.services.team_run import run_team_harness
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, **over: Any) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", **over)


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _run_out(
    results: dict[str, Any],
    *,
    member_status: dict[str, str] | None = None,
    manifest: dict[str, Any] | None = None,
    state: str = "SUCCEEDED",
    error_message: str | None = None,
) -> TeamRunOut:
    return TeamRunOut(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        state=state,
        results=results,
        paused_at=[],
        error_message=error_message,
        created_at=None,
        member_status=member_status or {},
        manifest=manifest,
    )


# --- the engine lifts a PARTIAL member's error_type/error_message (the missing precondition) ---


async def test_a_partial_members_error_type_and_message_reach_its_stored_result() -> None:
    class _DegradingHarness:
        async def execute(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "id": str(uuid.uuid4()),
                "status": "PARTIAL",
                "output": "gave up",
                "error_type": "reviewer_degraded",
                "error_message": "the reviewer could not repair the draft within its budget",
            }

    res = await run_team_harness(_team([_m("reviewer")]), _DegradingHarness())
    stored = res.results["reviewer"]
    assert stored["error_type"] == "reviewer_degraded"  # today dropped for a PARTIAL status
    assert stored["error_message"] == "the reviewer could not repair the draft within its budget"


async def test_a_harness_response_predating_this_change_lifts_nothing_for_a_partial_member() -> (
    None
):
    # back-compat, the #907/#944 posture: a response with no error_type/error_message at all must
    # not crash the run.
    class _Old:
        async def execute(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": str(uuid.uuid4()), "status": "PARTIAL", "output": "gave up"}

    res = await run_team_harness(_team([_m("reviewer")]), _Old())
    stored = res.results["reviewer"]
    assert stored.get("error_type") is None
    assert stored.get("error_message") is None


# --- outcome_blockers: empty + present on a clean run -------------------------------------------


def test_outcome_blockers_is_empty_and_present_on_a_clean_run() -> None:
    out = _run_out({"reviewer": {"output": "ok", "status": "SUCCEEDED"}})
    dumped = out.model_dump()
    assert "outcome_blockers" in dumped  # never absent — #944's own argument, reused
    assert dumped["outcome_blockers"] == []


# --- outcome_blockers: one entry, naming role/code/message/capability_lost ----------------------


def test_outcome_blockers_names_the_member_and_what_it_lost() -> None:
    manifest = {
        "metadata": {"name": "compiler"},
        "members": [
            {
                "role": "reviewer",
                "kind": "agent",
                "outcome_critical": True,
                "outputs_schema": {"required": ["members"]},
            }
        ],
    }
    results = {
        "reviewer": {
            "status": "PARTIAL",
            "members": [],
            "error_type": "reviewer_degraded",
            "error_message": "the reviewer could not repair the draft within its budget",
        }
    }
    out = _run_out(
        results, member_status={"reviewer": "partial"}, manifest=manifest, state="FAILED"
    )
    blockers = out.outcome_blockers
    assert len(blockers) == 1
    block = blockers[0]
    assert block.role == "reviewer"
    assert block.code == "reviewer_degraded"  # lifted from the harness error_type
    assert "could not repair" in block.message
    assert "members" in block.capability_lost  # names the declared key it did not deliver


def test_outcome_blockers_falls_back_to_a_platform_code_when_the_harness_gave_none() -> None:
    # DESIGN §B.2: "the platform code when the emptiness rule is what fired and the harness gave
    # none" — a harness response predating this change (no error_type) still produces a NAMED code,
    # never a blank one.
    manifest = {
        "members": [
            {
                "role": "reviewer",
                "kind": "agent",
                "outcome_critical": True,
                "outputs_schema": {"required": ["members"]},
            }
        ]
    }
    results = {"reviewer": {"status": "PARTIAL", "members": []}}  # no error_type/error_message
    out = _run_out(
        results, member_status={"reviewer": "partial"}, manifest=manifest, state="FAILED"
    )
    blockers = out.outcome_blockers
    assert len(blockers) == 1
    assert blockers[0].code  # a non-empty, named code — never blank
    assert blockers[0].capability_lost


# --- outcome_blockers is independent of the pre-existing failed/blocked surface -----------------


def test_outcome_blockers_stays_empty_for_an_ordinary_failed_blocked_run() -> None:
    # regression guard: this is a NEW surface for the NEW rule — a classic member FAILURE (no
    # outcome_critical involved) must not populate it, and the existing error_message surface (its
    # meaning is untouched) is what a caller already reads for that case.
    manifest = {"members": [{"role": "a", "kind": "agent"}, {"role": "b", "kind": "agent"}]}
    out = _run_out(
        {"a": None, "b": None},
        member_status={"a": "failed", "b": "blocked"},
        manifest=manifest,
        state="FAILED",
        error_message=(
            "This run did not finish: 1 of its members failed and 1 could not start. "
            "It can be re-run."
        ),
    )
    assert out.outcome_blockers == []
    assert out.error_message is not None  # unchanged existing surface, unchanged meaning


# --- mirrored onto TeamRunStatusOut (the #944 OPTIONAL-13 rule) ---------------------------------


def test_status_out_carries_outcome_blockers() -> None:
    from oraclous_execution_engine_service.schema.engine_schemas import (
        MemberOutcomeBlock,
        TeamRunCost,
        TeamRunStatusOut,
    )

    block = MemberOutcomeBlock(
        role="reviewer",
        code="reviewer_degraded",
        message="the reviewer could not repair the draft within its budget",
        capability_lost="members",
    )
    out = TeamRunStatusOut(
        team_run_id=uuid.uuid4(),
        organisation_id=_ORG,
        healthy=False,
        state="FAILED",
        progress=80,
        last_run_at=None,
        last_outcome="FAILED",
        cost=TeamRunCost(tokens=100),
        outcome_blockers=[block],
    )
    dumped = out.model_dump()
    assert dumped["outcome_blockers"][0]["role"] == "reviewer"
    assert dumped["outcome_blockers"][0]["capability_lost"] == "members"


def test_status_out_outcome_blockers_defaults_to_empty_never_absent() -> None:
    from oraclous_execution_engine_service.schema.engine_schemas import (
        TeamRunCost,
        TeamRunStatusOut,
    )

    out = TeamRunStatusOut(
        team_run_id=uuid.uuid4(),
        organisation_id=_ORG,
        healthy=True,
        state="SUCCEEDED",
        progress=100,
        last_run_at=None,
        last_outcome="SUCCEEDED",
        cost=TeamRunCost(tokens=10),
    )
    dumped = out.model_dump()
    assert "outcome_blockers" in dumped
    assert dumped["outcome_blockers"] == []


# --- the light status surface must agree with the detail read (#944 OPTIONAL-13) ----------------


async def test_status_surface_agrees_with_detail_read_on_deliverable_loss() -> None:
    from oraclous_execution_engine_service.models.team_run import EngineTeamRun
    from oraclous_execution_engine_service.services.team_run_service import TeamRunService

    manifest = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "t",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:x/reviewer@1",
                "outcome_critical": True,
                "outputs_schema": {"required": ["members"]},
            }
        ],
        "runtime": {"entrypoint": "reviewer"},
    }
    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=uuid.uuid4(),
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
        state="FAILED",
        results={
            "reviewer": {
                "status": "PARTIAL",
                "members": [],
                "error_type": "reviewer_degraded",
                "error_message": "gave up",
            }
        },
        paused_at=[],
        member_status={"reviewer": "partial"},
    )

    class _Repo:
        async def get(self, team_run_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineTeamRun:
            return row

    svc = TeamRunService(team_runs=_Repo(), harness=None, enqueue=None, evaluate=None)
    from oraclous_governance import Principal, PrincipalType

    principal = Principal(
        principal_id=row.user_id, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    status = await svc.status(row.id, principal)
    detail = TeamRunOut.model_validate(row)

    assert bool(status.outcome_blockers) == bool(detail.outcome_blockers)  # never disagree
    assert [b.role for b in status.outcome_blockers] == [b.role for b in detail.outcome_blockers]
