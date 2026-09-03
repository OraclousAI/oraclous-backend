"""#907 — the literal reproduction: a refine/compile run executed by the scripted fake LLM.

#907's second symptom: ``FakeLLMClient.complete`` (harness-runtime's ``domain/llm/fake.py:61``)
answers "No tools were available; nothing to do." when it observed no tool result. That prose is not
a typed edit op, so ``TeamDraftService._peel_json`` raised ``TeamRunError(..., 422,
error_type="op_drafter_unparseable")`` — a caller reading that token has no way to tell "the model
misbehaved" from "the model never really ran". This file pins the typed distinction: when the
member's own result says ``simulated: True``, the 422 must say so too (``op_drafter_simulated`` for
refine-nl, ``reviewer_output_simulated`` for draft-from-run) — and the existing unparseable token
must still fire, unchanged, when the flag is absent (no regression).

Fixtures mirror ``test_team_draft_service.py``'s ``_FakeTeamRuns``/``_service`` seam exactly (the
op-drafter run is faked at the ``TeamRunService`` boundary; the real LLM path is the gateway e2e's
job) so this file stays a pure addition — no shared fixture is touched or renamed.

RED until the [impl] lands: today's ``_peel_json`` has no notion of ``simulated`` at all, so both
new-token assertions fail (the error_type is still "op_drafter_unparseable"/"reviewer_output_
unparseable"), never a skip.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_draft_service import TeamDraftService
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()

# #907's fake LLM answer, verbatim (domain/llm/fake.py:61) — not a typed edit op, and (for the
# reviewer leg) not a compiled team either.
_FAKE_NOTHING_TO_DO = "No tools were available; nothing to do."


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


def _member(role: str, deps: list[str] | None = None, tools: list[str] | None = None) -> dict:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
        "tools": tools or [],
        "outputs_schema": {"required": ["summary"]},
    }


def _team(members: list[dict], org: uuid.UUID = _ORG) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "draft-team",
            "owner_organization_id": str(org),
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }


class _Row:
    """A draft row shaped like the ORM object (attribute access is all the service uses)."""

    def __init__(self, **kw: Any) -> None:
        self.id = kw.get("id", uuid.uuid4())
        self.organisation_id = kw["organisation_id"]
        self.user_id = kw.get("user_id", _USER)
        self.name = kw["name"]
        self.manifest = kw["manifest"]
        self.sub_harnesses = kw.get("sub_harnesses", {})
        self.version = kw.get("version", 1)
        self.team_run_id = kw.get("team_run_id")
        self.created_at = None
        self.updated_at = None


class _FakeDraftRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _Row] = {}

    async def create(self, **kw: Any) -> _Row:
        row = _Row(**kw)
        self.rows[row.id] = row
        return row

    async def get(self, draft_id: uuid.UUID, organisation_id: uuid.UUID) -> _Row | None:
        row = self.rows.get(draft_id)
        if row is None or row.organisation_id != organisation_id:
            return None
        return row

    async def get_by_team_run(
        self, organisation_id: uuid.UUID, team_run_id: uuid.UUID
    ) -> _Row | None:
        for row in self.rows.values():
            if row.organisation_id == organisation_id and row.team_run_id == team_run_id:
                return row
        return None

    async def create_from_run(self, **kw: Any) -> tuple[_Row, bool]:
        existing = await self.get_by_team_run(kw["organisation_id"], kw["team_run_id"])
        if existing is not None:
            return existing, False
        row = _Row(**kw)
        self.rows[row.id] = row
        return row, True

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        return [], 0

    async def replace(
        self, draft_id: uuid.UUID, organisation_id: uuid.UUID, **kw: Any
    ) -> _Row | None:
        row = await self.get(draft_id, organisation_id)
        if row is None:
            return None
        row.name = kw["name"]
        row.manifest = kw["manifest"]
        row.sub_harnesses = kw["sub_harnesses"]
        row.version += 1
        return row

    async def update_documents(
        self, draft_id: uuid.UUID, organisation_id: uuid.UUID, **kw: Any
    ) -> _Row | None:
        row = await self.get(draft_id, organisation_id)
        if row is None:
            return None
        row.manifest = kw["manifest"]
        row.sub_harnesses = kw["sub_harnesses"]
        row.version += 1
        return row

    async def delete(self, draft_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        row = await self.get(draft_id, organisation_id)
        if row is None:
            return False
        del self.rows[draft_id]
        return True


class _RunRow:
    def __init__(
        self,
        state: str,
        results: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.state = state
        self.results = results or {}
        self.manifest = manifest or {"metadata": {"name": "refine-op-drafter"}}


class _FakeTeamRuns:
    def __init__(self) -> None:
        self.runs: dict[uuid.UUID, _RunRow] = {}

    def seed(
        self,
        state: str,
        results: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> _RunRow:
        row = _RunRow(state, results, manifest)
        self.runs[row.id] = row
        return row

    async def create(self, principal: Principal, **kw: Any) -> _RunRow:
        row = _RunRow("SUCCEEDED", {})
        self.runs[row.id] = row
        return row

    async def get(self, run_id: uuid.UUID, principal: Principal) -> _RunRow:
        row = self.runs.get(run_id)
        if row is None:
            raise TeamRunError("team run not found", 404)
        return row


def _service(
    repo: _FakeDraftRepo | None = None, team_runs: _FakeTeamRuns | None = None
) -> tuple[TeamDraftService, _FakeDraftRepo, _FakeTeamRuns]:
    repo = repo or _FakeDraftRepo()
    team_runs = team_runs or _FakeTeamRuns()
    svc = TeamDraftService(
        drafts=repo,  # type: ignore[arg-type]
        team_runs=team_runs,  # type: ignore[arg-type]
        refine_nl_poll_seconds=0.2,
        refine_nl_poll_interval_seconds=0.01,
    )
    return svc, repo, team_runs


def _reviewer_results(payload: str, *, simulated: bool | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"output": payload, "status": "SUCCEEDED"}
    if simulated is not None:
        result["simulated"] = simulated
    return {"reviewer": result}


# ── refine-nl (the op-drafter leg): op_drafter_simulated ─────────────────────────────────────────


async def test_refine_nl_on_a_simulated_fake_nothing_to_do_answer_is_op_drafter_simulated() -> None:
    # The literal #907 reproduction: the op-drafter's own result says the LLM that ran it was the
    # scripted stand-in — the 422 must say so too, not the generic "unparseable" token.
    svc, _repo, team_runs = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    settled = team_runs.seed(
        "SUCCEEDED",
        {"op-drafter": {"output": _FAKE_NOTHING_TO_DO, "status": "SUCCEEDED", "simulated": True}},
    )
    with pytest.raises(TeamRunError) as exc:
        await svc.refine_nl(row.id, _principal(), op_drafter_run_id=settled.id)
    assert exc.value.status_code == 422
    assert exc.value.error_type == "op_drafter_simulated"


async def test_refine_nl_on_the_same_answer_without_the_flag_is_still_op_drafter_unparseable() -> (
    None
):
    # No regression: an unparseable answer from a REAL model (simulated absent/False) keeps the
    # existing token — this is #907's second bug reproduced from before the fix, and it must still
    # be reported the way it always was.
    svc, _repo, team_runs = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    settled = team_runs.seed(
        "SUCCEEDED",
        {"op-drafter": {"output": _FAKE_NOTHING_TO_DO, "status": "SUCCEEDED"}},
    )
    with pytest.raises(TeamRunError) as exc:
        await svc.refine_nl(row.id, _principal(), op_drafter_run_id=settled.id)
    assert exc.value.status_code == 422
    assert exc.value.error_type == "op_drafter_unparseable"


async def test_refine_nl_on_an_explicitly_real_unparseable_answer_is_op_drafter_unparseable() -> (
    None
):
    svc, _repo, team_runs = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    settled = team_runs.seed(
        "SUCCEEDED",
        {
            "op-drafter": {
                "output": _FAKE_NOTHING_TO_DO,
                "status": "SUCCEEDED",
                "simulated": False,
            }
        },
    )
    with pytest.raises(TeamRunError) as exc:
        await svc.refine_nl(row.id, _principal(), op_drafter_run_id=settled.id)
    assert exc.value.error_type == "op_drafter_unparseable"


# ── draft-from-run (the reviewer leg): reviewer_output_simulated ─────────────────────────────────


async def test_create_from_run_on_a_simulated_fake_nothing_to_do_answer_is_reviewer_output_simulated() -> (  # noqa: E501
    None
):
    svc, repo, team_runs = _service()
    run = team_runs.seed("SUCCEEDED", _reviewer_results(_FAKE_NOTHING_TO_DO, simulated=True))
    with pytest.raises(TeamRunError) as exc:
        await svc.create_from_run(_principal(), team_run_id=run.id)
    assert exc.value.status_code == 422
    assert exc.value.error_type == "reviewer_output_simulated"
    assert not repo.rows  # nothing persisted on a simulated-run 422, same as any ineligible shape


async def test_create_from_run_on_the_same_answer_without_the_flag_is_still_reviewer_output_unparseable() -> (  # noqa: E501
    None
):
    svc, repo, team_runs = _service()
    run = team_runs.seed("SUCCEEDED", _reviewer_results(_FAKE_NOTHING_TO_DO))
    with pytest.raises(TeamRunError) as exc:
        await svc.create_from_run(_principal(), team_run_id=run.id)
    assert exc.value.status_code == 422
    assert exc.value.error_type == "reviewer_output_unparseable"
    assert not repo.rows
