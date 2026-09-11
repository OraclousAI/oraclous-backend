"""#834 DESIGN §C site 4 — a member failed under the new ``outcome_critical`` rule MUST be
re-runnable, or a failed compile becomes unrecoverable.

The member itself stays recorded ``member_status == "partial"`` (DESIGN §A.1 / mirrored by
``packages/ohm/tests/test_orchestrate_outcome_critical.py``) even though it is now the reason the
RUN is ``"failed"`` — so the three places that decide "is a member re-runnable" by checking
``status in ("failed", "blocked")`` (``_faulted_roles``, ``rerun()``'s 409 check, and
``_completed_for_resume``'s resume-seed) all silently miss it today: a run stuck this way would
404-loop through ``/rerun`` forever (409 ``nothing_to_rerun``) even though its state is FAILED, or
would re-seed the SAME empty output back in on every drive.

Modelled on ``test_team_run_service.py`` (read it first) — same ``FakeTeamRunRepo`` /
``EngineTeamRun`` construction style. RED until the [impl] teaches these three call sites to also
recognise a "partial but critical-and-empty" member as faulted/re-runnable/not-yet-resumed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
    load_team_manifest,
)
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class FakeTeamRunRepo:
    """In-memory mirror of TeamRunRepository's create/get/transition (CAS) semantics — verbatim
    from test_team_run_service.py, so this file's fixtures stay comparable to that one's."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineTeamRun] = {}

    async def create(self, **kw: Any) -> EngineTeamRun:
        raise NotImplementedError("this file drives rerun()/_faulted_roles() on pre-seeded rows")

    async def get(self, team_run_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineTeamRun | None:
        row = self.rows.get(team_run_id)
        return row if row is not None and row.organisation_id == organisation_id else None

    async def transition(
        self,
        team_run_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        new_state: str,
        allowed_from: frozenset[str],
        **fields: Any,
    ) -> tuple[EngineTeamRun | None, bool]:
        row = self.rows.get(team_run_id)
        if row is None or row.organisation_id != organisation_id or row.state not in allowed_from:
            return row, False
        row.state = new_state
        for key, value in fields.items():
            setattr(row, key, value)
        return row, True


def _svc(repo: FakeTeamRunRepo) -> TeamRunService:
    return TeamRunService(
        team_runs=repo,
        harness=None,
        enqueue=lambda rid, org, user: None,
        evaluate=None,
    )


def _reviewer_manifest() -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "compiler",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:compiler/reviewer@1",
                "outcome_critical": True,
                "outputs_schema": {"required": ["members"]},
            }
        ],
        "runtime": {"entrypoint": "reviewer"},
    }


def _failed_critical_partial_row() -> EngineTeamRun:
    """A run the new #834 rule has already failed: the reviewer settled PARTIAL with its declared
    `members` key present but EMPTY — recorded status stays "partial", the run state is FAILED."""
    return EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=_reviewer_manifest(),
        sub_harnesses={},
        gate_decisions={},
        state="FAILED",
        results={"reviewer": {"status": "PARTIAL", "members": []}},
        paused_at=[],
        member_status={"reviewer": "partial"},
    )


# ── _faulted_roles must recognise a critical-and-empty "partial" member as faulted ─────────────
#
# ``_faulted_roles`` ALWAYS includes the team's SINK role(s) regardless of status (#604: so a
# SUCCEEDED-but-below-threshold run still regenerates). A single-member manifest makes "reviewer"
# a sink trivially, which would make these two tests pass/fail for the WRONG reason (sink
# membership, not the #834 rule). These fixtures add a downstream "publisher" so "reviewer" is NOT
# the sink — the only way "reviewer" can appear in ``faulted`` is the new rule itself.


def _reviewer_with_downstream_manifest() -> dict[str, Any]:
    manifest = _reviewer_manifest()
    manifest["runtime"] = {"entrypoint": "reviewer"}
    manifest["members"].append(
        {
            "role": "publisher",
            "kind": "agent",
            "manifest_ref": "org:compiler/publisher@1",
            "depends_on": ["reviewer"],
        }
    )
    return manifest


async def test_faulted_roles_includes_a_critical_member_whose_partial_output_is_empty() -> None:
    repo = FakeTeamRunRepo()
    svc = _svc(repo)
    manifest = _reviewer_with_downstream_manifest()
    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
        state="FAILED",
        results={"reviewer": {"status": "PARTIAL", "members": []}, "publisher": None},
        paused_at=[],
        member_status={"reviewer": "partial", "publisher": "blocked"},
    )
    team = load_team_manifest(row.manifest)
    faulted = svc._faulted_roles(row, team)
    assert "reviewer" in faulted  # NOT via sink membership — "publisher" is the sink here


async def test_faulted_roles_excludes_a_non_critical_partial_member_unchanged() -> None:
    # regression guard: an ORDINARY #587 partial member (not outcome_critical) must NOT become
    # faulted merely because it degraded — today's behaviour is unchanged. Downstream still runs
    # (a non-critical partial is not blocking), so "publisher" succeeds normally.
    repo = FakeTeamRunRepo()
    svc = _svc(repo)
    manifest = _reviewer_with_downstream_manifest()
    manifest["members"][0]["outcome_critical"] = False
    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
        state="FAILED",  # some OTHER member failed elsewhere; irrelevant to this assertion
        results={
            "reviewer": {"status": "PARTIAL", "members": []},
            "publisher": {"output": "done"},
        },
        paused_at=[],
        member_status={"reviewer": "partial", "publisher": "succeeded"},
    )
    team = load_team_manifest(row.manifest)
    faulted = svc._faulted_roles(row, team)
    assert "reviewer" not in faulted


# ── rerun() must not 409 nothing_to_rerun when the only faulted member is critical+empty ───────


async def test_rerun_does_not_409_when_the_only_faulted_member_is_critical_and_empty() -> None:
    repo = FakeTeamRunRepo()
    svc = _svc(repo)
    row = _failed_critical_partial_row()
    repo.rows[row.id] = row
    result = await svc.rerun(row.id, _principal())  # must NOT raise TeamRunError(409)
    assert result.state == "QUEUED"


async def test_rerun_still_409s_when_truly_nothing_is_re_runnable() -> None:
    # regression guard: a FAILED row with an ordinary clean member_status (nothing failed, nothing
    # critical-and-empty) is still a genuine 409 — this rule must not make rerun() permissive.
    repo = FakeTeamRunRepo()
    svc = _svc(repo)
    manifest = _reviewer_manifest()
    manifest["members"][0]["outcome_critical"] = False
    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
        state="FAILED",
        results={},
        paused_at=[],
        member_status={},
    )
    repo.rows[row.id] = row
    with pytest.raises(TeamRunError) as ei:
        await svc.rerun(row.id, _principal())
    assert ei.value.status_code == 409
    assert ei.value.error_type == "nothing_to_rerun"


# ── _completed_for_resume must NOT seed a critical member whose declared output was empty ──────


def test_completed_for_resume_excludes_a_critical_member_with_an_empty_declared_output() -> None:
    # contrast with test_team_run_service.py's own
    # test_completed_for_resume_seeds_partial_members_so_they_are_not_redispatched, where an
    # ORDINARY partial member IS seeded — a critical member whose empty output is WHY the run
    # failed must be excluded, or a re-run would re-seed the same empty output forever and the
    # member would never actually re-dispatch.
    row = _failed_critical_partial_row()
    seeded = TeamRunService._completed_for_resume(row)
    assert "reviewer" not in seeded


def test_completed_for_resume_still_seeds_an_ordinary_partial_member() -> None:
    # regression guard for the existing #587 behaviour this rule must not disturb.
    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest={},
        sub_harnesses={},
        gate_decisions={},
        state="FAILED",
        results={"a": {"output": "ok"}, "b": {"output": "best-effort"}, "c": None},
        member_status={"a": "succeeded", "b": "partial", "c": "failed"},
        paused_at=[],
    )
    seeded = TeamRunService._completed_for_resume(row)
    assert set(seeded) == {"a", "b"}
