"""#730 — ``deliverable_format`` through the draft store (knowledge PR 101, §DELIV).

Two service-layer conventions are already established and this feature follows BOTH, at different
entry points (confirmed by reading ``test_team_draft_service.py`` before writing this file, per the
issue's own instruction):

  * ``create``/``replace`` PERSIST a blocked draft regardless — "a draft may be SAVED blocked — the
    loop iterates until the strip is green" (see ``test_create_with_an_unsurveyed_tool_still_
    persists_but_blocks``). A reserved/unknown ``deliverable_format`` follows this SAME convention:
    the draft is stored, ``would_block`` is True, the blocking list names the reason.
  * ``create_from_run`` REFUSES a blocked compile outright — a curated 422, nothing persisted (see
    ``test_from_run_ineligible_shapes_are_curated_422s`` / the existing ``compiled_team_blocked``
    branch, which already fires on ANY ``would_block`` gate — this feature needs no new branch
    there, only a new reason for the SAME gate to fire on).

RED-by-design until the ``[impl]`` lands: neither ``validate_draft`` nor ``assemble_and_report``
knows about ``deliverable_format`` yet, so every "must block" / "must survive the peel" assertion
below fails against today's code.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_draft_service import TeamDraftService
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _member(
    role: str, deps: list[str] | None = None, deliverable_format: str | None = None
) -> dict:
    member: dict[str, Any] = {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
        "tools": [],
        "outputs_schema": {"required": ["summary"]},  # #697
    }
    if deliverable_format is not None:
        member["deliverable_format"] = deliverable_format
    return member


def _team(members: list[dict], *, deliverable_format: str | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "draft-team",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }
    if deliverable_format is not None:
        doc["deliverable_format"] = deliverable_format
    return doc


class _Row:
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

    async def get_by_team_run(self, organisation_id: uuid.UUID, team_run_id: uuid.UUID) -> None:
        return None

    async def create_from_run(self, **kw: Any) -> tuple[_Row, bool]:
        row = _Row(**kw)
        self.rows[row.id] = row
        return row, True

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


class _RunRow:
    def __init__(self, output: str) -> None:
        self.id = uuid.uuid4()
        self.state = "SUCCEEDED"
        self.results = {"reviewer": {"output": output, "status": "SUCCEEDED"}}
        self.manifest = {"metadata": {"name": "harness-compiler"}}


class _FakeTeamRuns:
    def __init__(self, output: str) -> None:
        self.row = _RunRow(output)

    async def get(self, run_id: uuid.UUID, principal: Principal) -> _RunRow:
        if run_id != self.row.id:
            raise TeamRunError("team run not found", 404)
        return self.row


def _svc(repo: _FakeDraftRepo, team_runs: _FakeTeamRuns | None = None) -> TeamDraftService:
    # the fakes are duck-typed seams in unit tests, never the real repository/service types
    return TeamDraftService(
        drafts=repo,  # type: ignore[arg-type]
        team_runs=team_runs,  # type: ignore[arg-type]
    )


# ── create()/replace(): persists regardless — the shared verdict names the reason ───────────


async def test_create_with_a_supported_team_level_format_persists_and_reads_back() -> None:
    repo = _FakeDraftRepo()
    row, verdict = await _svc(repo).create(
        _principal(),
        name="d",
        manifest=_team([_member("researcher")], deliverable_format="markdown"),
        sub_harnesses={},
    )
    assert verdict.would_block is False
    assert row.manifest["deliverable_format"] == "markdown"
    assert row.id in repo.rows


async def test_create_with_a_reserved_team_level_format_still_persists_but_blocks() -> None:
    repo = _FakeDraftRepo()
    row, verdict = await _svc(repo).create(
        _principal(),
        name="d",
        manifest=_team([_member("researcher")], deliverable_format="pdf"),
        sub_harnesses={},
    )
    assert verdict.would_block is True
    assert any("F-DELIVERABLE-FORMAT-RESERVED" in b for b in verdict.blocking)
    # persisted regardless — drafts are drafts (matches the F-CAPABILITY-MISSING convention)
    assert row.id in repo.rows


async def test_create_with_a_reserved_member_level_format_still_persists_but_blocks() -> None:
    repo = _FakeDraftRepo()
    row, verdict = await _svc(repo).create(
        _principal(),
        name="d",
        manifest=_team([_member("researcher", deliverable_format="docx")]),
        sub_harnesses={},
    )
    assert verdict.would_block is True
    assert any("F-DELIVERABLE-FORMAT-RESERVED" in b for b in verdict.blocking)
    assert row.id in repo.rows


async def test_create_with_an_unknown_format_blocks_with_a_different_code_than_reserved() -> None:
    repo = _FakeDraftRepo()
    _row, verdict = await _svc(repo).create(
        _principal(),
        name="d",
        manifest=_team([_member("researcher")], deliverable_format="banana"),
        sub_harnesses={},
    )
    assert verdict.would_block is True
    assert any("F-DELIVERABLE-FORMAT-UNKNOWN" in b for b in verdict.blocking)
    assert not any("F-DELIVERABLE-FORMAT-RESERVED" in b for b in verdict.blocking)


async def test_team_and_member_level_formats_are_independent_through_create() -> None:
    repo = _FakeDraftRepo()
    row, verdict = await _svc(repo).create(
        _principal(),
        name="d",
        manifest=_team(
            [_member("researcher", deliverable_format="text")], deliverable_format="markdown"
        ),
        sub_harnesses={},
    )
    assert verdict.would_block is False
    assert row.manifest["deliverable_format"] == "markdown"
    assert row.manifest["members"][0]["deliverable_format"] == "text"


async def test_adding_a_member_via_replace_does_not_change_the_teams_declared_format() -> None:
    repo = _FakeDraftRepo()
    svc = _svc(repo)
    created, _v = await svc.create(
        _principal(),
        name="d",
        manifest=_team([_member("researcher")], deliverable_format="markdown"),
        sub_harnesses={},
    )
    replaced, verdict = await svc.replace(
        created.id,
        _principal(),
        name="d",
        manifest=_team(
            [_member("researcher"), _member("writer", ["researcher"])],
            deliverable_format="markdown",
        ),
        sub_harnesses={},
    )
    assert verdict.would_block is False
    assert replaced.manifest["deliverable_format"] == "markdown"  # unchanged by the added member
    assert len(replaced.manifest["members"]) == 2


# ── create_from_run(): refused outright, nothing persisted ───────────────────────────────────


def _compiled(
    *, team_format: str | None = None, member_format: str | None = None
) -> dict[str, Any]:
    reviewer: dict[str, Any] = {
        "role": "Reviewer",
        "kind": "agent",
        "manifest_ref": "org:compiled/reviewer@1",
        "subgoal": "review",
        "tools": [],
        "outputs_schema": {"required": ["summary"]},
    }
    if member_format is not None:
        reviewer["deliverable_format"] = member_format
    doc: dict[str, Any] = {"members": [reviewer]}
    if team_format is not None:
        doc["deliverable_format"] = team_format
    return doc


async def _from_run(compiled: dict[str, Any]) -> tuple[TeamDraftService, _FakeDraftRepo, Any]:
    repo = _FakeDraftRepo()
    runs = _FakeTeamRuns(json.dumps(compiled))
    svc = _svc(repo, runs)
    outcome = await svc.create_from_run(_principal(), team_run_id=runs.row.id)
    return svc, repo, outcome


async def test_from_run_carries_a_supported_team_level_format_to_the_stored_draft() -> None:
    _svc_, repo, (row, verdict, created) = await _from_run(_compiled(team_format="markdown"))
    assert created is True
    assert verdict.would_block is False
    assert row.manifest.get("deliverable_format") == "markdown"
    assert row.id in repo.rows


async def test_from_run_without_a_deliverable_format_still_drafts() -> None:
    """Back-compat: every draft compiled before #730 has no deliverable_format and must still
    peel, validate, and store."""
    _svc_, repo, (row, verdict, created) = await _from_run(_compiled())
    assert created is True
    assert verdict.would_block is False
    assert row.manifest.get("deliverable_format") is None


async def test_from_run_refuses_a_reserved_team_level_format_and_persists_nothing() -> None:
    repo = _FakeDraftRepo()
    runs = _FakeTeamRuns(json.dumps(_compiled(team_format="pdf")))
    svc = _svc(repo, runs)
    with pytest.raises(TeamRunError) as exc:
        await svc.create_from_run(_principal(), team_run_id=runs.row.id)
    assert exc.value.status_code == 422
    assert exc.value.error_type == "compiled_team_blocked"
    assert not repo.rows  # NOTHING persisted


async def test_from_run_refuses_a_reserved_member_level_format_and_persists_nothing() -> None:
    repo = _FakeDraftRepo()
    runs = _FakeTeamRuns(json.dumps(_compiled(member_format="html")))
    svc = _svc(repo, runs)
    with pytest.raises(TeamRunError) as exc:
        await svc.create_from_run(_principal(), team_run_id=runs.row.id)
    assert exc.value.status_code == 422
    assert not repo.rows


async def test_from_run_refuses_an_unknown_format_distinctly_from_a_reserved_one() -> None:
    repo = _FakeDraftRepo()
    runs = _FakeTeamRuns(json.dumps(_compiled(team_format="banana")))
    svc = _svc(repo, runs)
    with pytest.raises(TeamRunError) as exc:
        await svc.create_from_run(_principal(), team_run_id=runs.row.id)
    assert exc.value.status_code == 422
    # the curated message rolls up the gate's blocking reasons — the UNKNOWN code must be in it,
    # never the RESERVED one (a different value entirely)
    assert "F-DELIVERABLE-FORMAT-UNKNOWN" in str(exc.value)
    assert "F-DELIVERABLE-FORMAT-RESERVED" not in str(exc.value)
    assert not repo.rows
