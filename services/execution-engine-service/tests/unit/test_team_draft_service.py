"""TeamDraftService (#635) — unit, fake repositories, no DB.

Pins the store's service contract (the shared-validator verdict on every write, the
born-bounded list clamp, version semantics, the fail-closed org/404 edges) and the from-run
peel — each ineligible shape a curated 422, NOTHING persisted.
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
        self.created_at = None
        self.updated_at = None


class _FakeDraftRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _Row] = {}
        self.list_calls: list[dict[str, int]] = []

    async def create(self, **kw: Any) -> _Row:
        row = _Row(**kw)
        self.rows[row.id] = row
        return row

    async def get(self, draft_id: uuid.UUID, organisation_id: uuid.UUID) -> _Row | None:
        row = self.rows.get(draft_id)
        if row is None or row.organisation_id != organisation_id:
            return None
        return row

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        self.list_calls.append({"limit": limit, "offset": offset})
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
    def __init__(self, state: str, results: dict[str, Any] | None = None) -> None:
        self.id = uuid.uuid4()
        self.state = state
        self.results = results or {}


class _FakeTeamRuns:
    """The TeamRunService seam refine-nl/from-run consume: create (submit) + get (poll/read)."""

    def __init__(self) -> None:
        self.runs: dict[uuid.UUID, _RunRow] = {}
        self.created: list[dict[str, Any]] = []

    def seed(self, state: str, results: dict[str, Any] | None = None) -> _RunRow:
        row = _RunRow(state, results)
        self.runs[row.id] = row
        return row

    async def create(self, principal: Principal, **kw: Any) -> _RunRow:
        self.created.append(kw)
        row = _RunRow(
            "SUCCEEDED",
            {
                "op-drafter": {
                    "output": '{"op": "add_depends_on", "role": "b", "depends_on": "a"}',
                    "status": "SUCCEEDED",
                }
            },
        )
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
        drafts=repo,  # type: ignore[arg-type] — the seam is duck-typed in unit tests
        team_runs=team_runs,  # type: ignore[arg-type]
    )
    return svc, repo, team_runs


# ── the store: verdict on every write, clamp, 403 ────────────────────────────


async def test_create_embeds_the_shared_validator_verdict() -> None:
    svc, _repo, _ = _service()
    row, verdict = await svc.create(
        _principal(),
        name="d",
        manifest=_team([_member("a"), _member("b", ["a"])]),
        sub_harnesses={},
    )
    assert row.version == 1
    assert verdict.would_block is False and verdict.blocking == []
    # the RENDERED dry-run report rides along (the shared validator's wire shape)
    assert isinstance(verdict.report, str) and verdict.report != ""


async def test_create_with_an_unsurveyed_tool_still_persists_but_blocks() -> None:
    # a draft may be SAVED blocked — the loop iterates until the strip is green; the verdict says so
    svc, repo, _ = _service()
    row, verdict = await svc.create(
        _principal(),
        name="d",
        manifest=_team([_member("a", tools=["definitely-not-a-real-tool"])]),
        sub_harnesses={},
    )
    assert verdict.would_block is True
    assert any("F-CAPABILITY-MISSING" in b for b in verdict.blocking)
    assert row.id in repo.rows  # persisted regardless — drafts are drafts


async def test_create_rejects_a_non_team_manifest_as_422() -> None:
    svc, repo, _ = _service()
    doc = _team([_member("a")])
    doc["metadata"]["kind"] = "agent"
    with pytest.raises(TeamRunError) as exc:
        await svc.create(_principal(), name="d", manifest=doc, sub_harnesses={})
    assert exc.value.status_code == 422
    assert not repo.rows  # nothing persisted


async def test_no_org_principal_is_a_403() -> None:
    svc, _repo, _ = _service()
    with pytest.raises(TeamRunError) as exc:
        await svc.create(
            _principal(org=None), name="d", manifest=_team([_member("a")]), sub_harnesses={}
        )
    assert exc.value.status_code == 403


async def test_list_clamps_limit_and_offset_server_side() -> None:
    svc, repo, _ = _service()
    await svc.list_for_org(_principal(), limit=9999, offset=-5)
    await svc.list_for_org(_principal(), limit=0, offset=3)
    assert repo.list_calls == [
        {"limit": 200, "offset": 0},  # clamped to the hard max / floor
        {"limit": 1, "offset": 3},
    ]


async def test_replace_bumps_version_and_revalidates() -> None:
    svc, _repo, _ = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    updated, verdict = await svc.replace(
        row.id,
        _principal(),
        name="d2",
        manifest=_team([_member("a"), _member("b", ["a"])]),
        sub_harnesses={},
    )
    assert updated.version == 2 and updated.name == "d2"
    assert verdict.would_block is False


async def test_get_replace_delete_404_on_a_missing_draft() -> None:
    svc, _repo, _ = _service()
    missing = uuid.uuid4()
    for coro in (
        svc.get(missing, _principal()),
        svc.replace(
            missing, _principal(), name="x", manifest=_team([_member("a")]), sub_harnesses={}
        ),
        svc.delete(missing, _principal()),
    ):
        with pytest.raises(TeamRunError) as exc:
            await coro
        assert exc.value.status_code == 404


# ── from-run: the peel, each failure a curated 422 (nothing persisted) ───────


def _reviewer_results(payload: str) -> dict[str, Any]:
    return {"reviewer": {"output": payload, "status": "SUCCEEDED"}}


async def test_from_run_peels_validates_and_persists_with_sub_harnesses() -> None:
    svc, repo, team_runs = _service()
    compiled = (
        'Here is the team:\n{"members": ['
        '{"role": "researcher", "kind": "agent", "subgoal": "research"},'
        '{"role": "writer", "kind": "agent", "subgoal": "write", "depends_on": ["researcher"]}'
        "]}"
    )
    run = team_runs.seed("SUCCEEDED", _reviewer_results(compiled))
    row, verdict = await svc.create_from_run(_principal(), team_run_id=run.id, name="from-prose")
    assert verdict.would_block is False
    assert row.name == "from-prose" and row.id in repo.rows
    manifest = row.manifest
    assert {m["role"] for m in manifest["members"]} == {"researcher", "writer"}
    # per-member reasoning-only sub-harnesses were synthesized server-side (the e2e's old
    # client-side step) — so the draft is GO-able as stored
    assert set(row.sub_harnesses) == {"researcher", "writer"}
    assert row.sub_harnesses["researcher"]["metadata"]["kind"] == "agent"


@pytest.mark.parametrize(
    ("state", "results"),
    [
        ("FAILED", {}),  # not SUCCEEDED
        ("SUCCEEDED", {}),  # no reviewer output at all
        ("SUCCEEDED", _reviewer_results("no json here")),  # nothing to peel
        ("SUCCEEDED", _reviewer_results('{"members": []}')),  # empty members
        ("SUCCEEDED", _reviewer_results('{"members": [{"kind": "agent"}]}')),  # invalid member
    ],
)
async def test_from_run_ineligible_shapes_are_curated_422s(
    state: str, results: dict[str, Any]
) -> None:
    svc, repo, team_runs = _service()
    run = team_runs.seed(state, results)
    with pytest.raises(TeamRunError) as exc:
        await svc.create_from_run(_principal(), team_run_id=run.id)
    assert exc.value.status_code == 422
    assert not repo.rows  # NOTHING persisted on any ineligible shape
