"""#1163: a team run records which team draft + version it was started from (unit; fake repos).

``TeamRunService.create`` takes ``team_draft_id``/``team_draft_version`` and enforces optimistic
concurrency against the draft store BEFORE any other create-time check (R3/R5): the client sends
the version it loaded, the server rejects a stale one with 409, and only a version match is
persisted. Both fields, or neither — one alone is a 422 (R4). A rerun keeps the pair untouched
(R8). RED until ``TeamRunService.__init__``/``create`` accept ``team_drafts``/the new keywords and
``core.dependencies.get_team_run_service`` wires a ``TeamDraftRepository`` in.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
)
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_OTHER_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class _NoopProvenance:
    """Mirrors ``test_team_run_service.py``'s stand-in — ``provenance`` is a non-optional
    TeamRunService kwarg; these tests exercise unrelated behaviour."""

    async def emit(self, record: Any) -> None:
        return None


class FakeTeamRunRepo:
    """In-memory mirror of TeamRunRepository's create/get/transition (CAS) semantics, copied from
    ``test_team_run_service.py`` (already widened for team_draft_id/version, #1163 T1)."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineTeamRun] = {}

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
        gate_decisions: dict[str, Any],
        workspace_root: str | None = None,
        graph_id: str | None = None,
        inputs: dict[str, Any] | None = None,
        seed_from_run_id: uuid.UUID | None = None,
        app_id: uuid.UUID | None = None,
        team_draft_id: uuid.UUID | None = None,
        team_draft_version: int | None = None,
    ) -> EngineTeamRun:
        row = EngineTeamRun(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            manifest=manifest,
            sub_harnesses=sub_harnesses,
            gate_decisions=gate_decisions,
            state="QUEUED",
            results={},
            paused_at=[],
            workspace_root=workspace_root,
            graph_id=graph_id,
            inputs=inputs,
            seed_from_run_id=seed_from_run_id,
        )
        # #1163: the model has no such columns yet (pre-impl); set as plain attributes so callers
        # that read them back see what they passed.
        if team_draft_id is not None:
            row.team_draft_id = team_draft_id
        if team_draft_version is not None:
            row.team_draft_version = team_draft_version
        self.rows[row.id] = row
        return row

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


def _team(members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "team",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }


def _agent(role: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
        "tools": [],
    }


class FakeDraftRepo:
    """In-memory stand-in for ``TeamDraftRepository.get`` — org-scoped, records every call."""

    def __init__(self, rows: dict[uuid.UUID, SimpleNamespace] | None = None) -> None:
        self.rows = rows or {}
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def get(self, draft_id: uuid.UUID, organisation_id: uuid.UUID) -> SimpleNamespace | None:
        self.calls.append((draft_id, organisation_id))
        row = self.rows.get(draft_id)
        if row is None or row.organisation_id != organisation_id:
            return None
        return row


def _draft(version: int, *, org: uuid.UUID = _ORG) -> tuple[uuid.UUID, FakeDraftRepo]:
    draft_id = uuid.uuid4()
    return draft_id, FakeDraftRepo(
        {draft_id: SimpleNamespace(id=draft_id, organisation_id=org, version=version)}
    )


def _svc(
    repo: FakeTeamRunRepo, drafts: FakeDraftRepo | None, *, enqueued: list[uuid.UUID] | None = None
) -> TeamRunService:
    kwargs: dict[str, Any] = {
        "team_runs": repo,
        "provenance": _NoopProvenance(),
        "enqueue": lambda rid, _org, _user: (enqueued if enqueued is not None else []).append(rid),
    }
    if drafts is not None:
        kwargs["team_drafts"] = drafts
    else:
        kwargs["team_drafts"] = None
    return TeamRunService(**kwargs)


def _manifest() -> dict[str, Any]:
    return _team([_agent("solo")])


async def test_matching_version_persists_and_enqueues() -> None:
    draft_id, drafts = _draft(2)
    repo = FakeTeamRunRepo()
    enqueued: list[uuid.UUID] = []
    svc = _svc(repo, drafts, enqueued=enqueued)

    row = await svc.create(
        _principal(),
        manifest=_manifest(),
        sub_harnesses={},
        gate_decisions={},
        team_draft_id=draft_id,
        team_draft_version=2,
    )

    assert row.team_draft_id == draft_id
    assert row.team_draft_version == 2
    assert enqueued == [row.id]


async def test_stale_lower_version_is_a_409_conflict() -> None:
    draft_id, drafts = _draft(3)
    repo = FakeTeamRunRepo()
    enqueued: list[uuid.UUID] = []
    svc = _svc(repo, drafts, enqueued=enqueued)

    with pytest.raises(TeamRunError) as exc_info:
        await svc.create(
            _principal(),
            manifest=_manifest(),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=draft_id,
            team_draft_version=2,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.error_type == "team_draft_version_conflict"
    assert repo.rows == {}
    assert enqueued == []


async def test_stale_higher_version_is_also_a_409_conflict() -> None:
    draft_id, drafts = _draft(3)
    repo = FakeTeamRunRepo()
    svc = _svc(repo, drafts)

    with pytest.raises(TeamRunError) as exc_info:
        await svc.create(
            _principal(),
            manifest=_manifest(),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=draft_id,
            team_draft_version=4,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.error_type == "team_draft_version_conflict"
    assert repo.rows == {}


async def test_a_foreign_org_draft_is_a_422_invalid_team_draft() -> None:
    draft_id, drafts = _draft(2, org=_OTHER_ORG)
    repo = FakeTeamRunRepo()
    svc = _svc(repo, drafts)

    with pytest.raises(TeamRunError) as exc_info:
        await svc.create(
            _principal(),
            manifest=_manifest(),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=draft_id,
            team_draft_version=2,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.error_type == "invalid_team_draft"
    assert exc_info.value.field == "team_draft_id"
    assert repo.rows == {}


async def test_an_unknown_draft_id_is_the_same_422() -> None:
    repo = FakeTeamRunRepo()
    drafts = FakeDraftRepo({})
    svc = _svc(repo, drafts)

    with pytest.raises(TeamRunError) as exc_info:
        await svc.create(
            _principal(),
            manifest=_manifest(),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=uuid.uuid4(),
            team_draft_version=1,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.error_type == "invalid_team_draft"
    assert exc_info.value.field == "team_draft_id"
    assert repo.rows == {}


async def test_id_without_version_is_422_ref_incomplete_naming_version() -> None:
    draft_id, drafts = _draft(1)
    repo = FakeTeamRunRepo()
    svc = _svc(repo, drafts)

    with pytest.raises(TeamRunError) as exc_info:
        await svc.create(
            _principal(),
            manifest=_manifest(),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=draft_id,
            team_draft_version=None,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.error_type == "team_draft_ref_incomplete"
    assert exc_info.value.field == "team_draft_version"
    assert repo.rows == {}


async def test_version_without_id_is_422_ref_incomplete_naming_id() -> None:
    repo = FakeTeamRunRepo()
    svc = _svc(repo, FakeDraftRepo({}))

    with pytest.raises(TeamRunError) as exc_info:
        await svc.create(
            _principal(),
            manifest=_manifest(),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=None,
            team_draft_version=1,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.error_type == "team_draft_ref_incomplete"
    assert exc_info.value.field == "team_draft_id"
    assert repo.rows == {}


async def test_the_precondition_is_checked_before_manifest_validation() -> None:
    # R5: a stale version AND an invalid manifest ({}) must give 409, not the manifest's 422 —
    # the precondition runs first, before load_team_manifest ever sees the body.
    draft_id, drafts = _draft(3)
    repo = FakeTeamRunRepo()
    svc = _svc(repo, drafts)

    with pytest.raises(TeamRunError) as exc_info:
        await svc.create(
            _principal(),
            manifest={},
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=draft_id,
            team_draft_version=2,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.error_type == "team_draft_version_conflict"


async def test_no_draft_fields_persists_none_and_never_calls_the_draft_store() -> None:
    repo = FakeTeamRunRepo()
    drafts = FakeDraftRepo({})
    svc = _svc(repo, drafts)

    row = await svc.create(_principal(), manifest=_manifest(), sub_harnesses={}, gate_decisions={})

    assert row.team_draft_id is None
    assert row.team_draft_version is None
    assert drafts.calls == []


async def test_no_draft_store_wired_is_a_503_and_persists_nothing() -> None:
    repo = FakeTeamRunRepo()
    svc = _svc(repo, None)

    with pytest.raises(TeamRunError) as exc_info:
        await svc.create(
            _principal(),
            manifest=_manifest(),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=uuid.uuid4(),
            team_draft_version=1,
        )

    assert exc_info.value.status_code == 503
    assert repo.rows == {}


async def test_get_team_run_service_wires_team_drafts_and_the_stale_check_runs() -> None:
    # Wiring (R3): the dependency-injected service must carry the draft store through, so a
    # request-path call rejects a stale version before anything is enqueued.
    from oraclous_execution_engine_service.core.dependencies import get_team_run_service

    draft_id, drafts = _draft(5)
    repo = FakeTeamRunRepo()
    svc = get_team_run_service(
        team_runs=repo, graphs=None, registry=None, provenance=_NoopProvenance(), team_drafts=drafts
    )

    with pytest.raises(TeamRunError) as exc_info:
        await svc.create(
            _principal(),
            manifest=_manifest(),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=draft_id,
            team_draft_version=4,
        )

    assert exc_info.value.status_code == 409
    assert repo.rows == {}


async def test_rerun_keeps_the_teams_id_and_version() -> None:
    draft_id, drafts = _draft(2)
    repo = FakeTeamRunRepo()
    svc = _svc(repo, drafts)
    row = await svc.create(
        _principal(),
        manifest=_manifest(),
        sub_harnesses={},
        gate_decisions={},
        team_draft_id=draft_id,
        team_draft_version=2,
    )
    row.state = "FAILED"
    row.member_status = {"solo": "failed"}

    rerun_row = await svc.rerun(row.id, _principal())

    assert rerun_row.team_draft_id == draft_id
    assert rerun_row.team_draft_version == 2
