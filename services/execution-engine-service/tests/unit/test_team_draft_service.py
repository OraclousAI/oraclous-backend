"""TeamDraftService (#635) — unit, fake repositories, no DB.

Pins the service contract of the draft loop: the shared-validator verdict embedded on every
write, the born-bounded list clamp, the from-run peel (each ineligible shape a curated 422,
NOTHING persisted), refine's preserve-the-rest apply vs blocked-op untouched, and refine-nl's
draft → peel → apply path including the 202-pending budget. The op-drafter run is faked at the
``TeamRunService`` seam (create/get) — the REAL LLM path is the gateway e2e's job.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_draft_service import (
    PendingOpDraft,
    RefineOutcome,
    TeamDraftService,
)
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
        self.team_run_id = kw.get("team_run_id")  # #638: set only on a from-run draft
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

    async def get_by_team_run(
        self, organisation_id: uuid.UUID, team_run_id: uuid.UUID
    ) -> _Row | None:
        for row in self.rows.values():
            if row.organisation_id == organisation_id and row.team_run_id == team_run_id:
                return row
        return None

    async def create_from_run(self, **kw: Any) -> tuple[_Row, bool]:
        # #638: idempotent — return the existing (org, team_run_id) draft if one exists, else insert
        existing = await self.get_by_team_run(kw["organisation_id"], kw["team_run_id"])
        if existing is not None:
            return existing, False
        row = _Row(**kw)
        self.rows[row.id] = row
        return row, True

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
    def __init__(
        self,
        state: str,
        results: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.state = state
        self.results = results or {}
        # the collect token's identity check reads the run's manifest name
        self.manifest = manifest or {"metadata": {"name": "refine-op-drafter"}}


class _FakeTeamRuns:
    """The TeamRunService seam refine-nl/from-run consume: create (submit) + get (poll/read)."""

    def __init__(self) -> None:
        self.runs: dict[uuid.UUID, _RunRow] = {}
        self.created: list[dict[str, Any]] = []

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
        refine_nl_poll_seconds=0.2,
        refine_nl_poll_interval_seconds=0.01,
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
    row, verdict, created = await svc.create_from_run(
        _principal(), team_run_id=run.id, name="from-prose"
    )
    assert created is True  # #638: a first from-run mints the draft (→ the route 201s)
    assert verdict.would_block is False
    assert row.name == "from-prose" and row.id in repo.rows
    assert row.team_run_id == run.id  # #638: the source run is stamped for idempotency
    manifest = row.manifest
    assert {m["role"] for m in manifest["members"]} == {"researcher", "writer"}
    # per-member reasoning-only sub-harnesses were synthesized server-side (the e2e's old
    # client-side step) — so the draft is GO-able as stored
    assert set(row.sub_harnesses) == {"researcher", "writer"}
    assert row.sub_harnesses["researcher"]["metadata"]["kind"] == "agent"


async def test_from_run_is_idempotent_per_org_and_run() -> None:
    # #638: a second from-run for the SAME run returns the SAME draft (created=False → the route
    # 200s), never a duplicate — a reload / second tab on ?compile=<runId> is safe.
    svc, repo, team_runs = _service()
    compiled = '{"members": [{"role": "researcher", "kind": "agent", "subgoal": "research"}]}'
    run = team_runs.seed("SUCCEEDED", _reviewer_results(compiled))
    first, _v1, c1 = await svc.create_from_run(_principal(), team_run_id=run.id)
    second, _v2, c2 = await svc.create_from_run(_principal(), team_run_id=run.id)
    assert c1 is True and c2 is False  # first mints, second returns the existing
    assert second.id == first.id  # ONE draft, same id
    assert len(repo.rows) == 1


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


# ── refine: typed op, preserve-the-rest vs blocked-untouched ─────────────────


async def test_refine_applies_a_valid_op_and_bumps_version() -> None:
    svc, _repo, _ = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    outcome = await svc.refine(
        row.id,
        _principal(),
        edit_op={"op": "add_member", "role": "fact-checker", "depends_on": ["a"]},
    )
    assert isinstance(outcome, RefineOutcome)
    assert outcome.applied is True and outcome.verdict.would_block is False
    assert outcome.row.version == 2
    roles = [m["role"] for m in outcome.row.manifest["members"]]
    assert roles == ["a", "fact-checker"]  # preserve-the-rest: 'a' untouched, in place
    assert "fact-checker" in outcome.row.sub_harnesses  # synthesized for the new member


async def test_refine_blocked_op_leaves_the_draft_untouched() -> None:
    svc, _repo, _ = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    outcome = await svc.refine(
        row.id,
        _principal(),
        edit_op={"op": "add_member", "role": "rogue", "tools": ["not-surveyed-anywhere"]},
    )
    assert outcome.applied is False and outcome.verdict.would_block is True
    assert outcome.row.version == 1  # untouched
    assert [m["role"] for m in outcome.row.manifest["members"]] == ["a"]


async def test_refine_dry_run_validates_without_persisting() -> None:
    svc, _repo, _ = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    outcome = await svc.refine(
        row.id,
        _principal(),
        edit_op={"op": "add_member", "role": "b", "depends_on": ["a"]},
        dry_run=True,
    )
    assert outcome.applied is False and outcome.verdict.would_block is False
    assert outcome.row.version == 1  # previewed, not persisted


async def test_refine_malformed_op_is_a_422() -> None:
    svc, _repo, _ = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    with pytest.raises(TeamRunError) as exc:
        await svc.refine(row.id, _principal(), edit_op={"op": "explode_the_team"})
    assert exc.value.status_code == 422


# ── refine-nl: draft (op-drafter run) → peel → same apply path ───────────────


async def test_refine_nl_drafts_an_op_through_the_team_run_path_and_applies_it() -> None:
    svc, _repo, team_runs = _service()
    row, _ = await svc.create(
        _principal(),
        name="d",
        manifest=_team([_member("a"), _member("b", ["a"])]),
        sub_harnesses={},
    )
    # remove b's dep so the drafted add_depends_on op applies cleanly
    outcome = await svc.refine_nl(
        row.id,
        _principal(),
        instruction="make b depend on a",
        models=[
            {
                "role": "primary",
                "binding": "openrouter/x",
                "protocol_shape": "openai-compatible",
                "config": {"credential_id": "c1"},
            }
        ],
    )
    assert isinstance(outcome, RefineOutcome)
    # the op-drafter was submitted through the SAME create path, models bound on doc + sub
    submitted = team_runs.created[0]
    assert submitted["manifest"]["models"][0]["config"]["credential_id"] == "c1"
    assert submitted["sub_harnesses"]["op-drafter"]["models"][0]["binding"] == "openrouter/x"
    # the drafter's subgoal carries the CURRENT manifest + the surveyed catalog + the edit ask
    subgoal = submitted["manifest"]["members"][0]["subgoal"]
    assert "CURRENT TEAM MANIFEST" in subgoal and "make b depend on a" in subgoal
    assert outcome.op == {"op": "add_depends_on", "role": "b", "depends_on": "a"}
    assert outcome.op_drafter_run_id is not None


async def test_refine_nl_missing_models_is_a_422() -> None:
    svc, _repo, _ = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    with pytest.raises(TeamRunError) as exc:
        await svc.refine_nl(row.id, _principal(), instruction="add a member", models=[])
    assert exc.value.status_code == 422


async def test_refine_nl_returns_pending_when_the_drafter_outruns_the_budget() -> None:
    svc, _repo, team_runs = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    still_running = team_runs.seed("RUNNING")
    outcome = await svc.refine_nl(row.id, _principal(), op_drafter_run_id=still_running.id)
    assert isinstance(outcome, PendingOpDraft)
    assert outcome.op_drafter_run_id == still_running.id


async def test_refine_nl_collects_a_settled_drafter_run_by_id() -> None:
    svc, _repo, team_runs = _service()
    row, _ = await svc.create(
        _principal(),
        name="d",
        manifest=_team([_member("a"), _member("b")]),
        sub_harnesses={},
    )
    settled = team_runs.seed(
        "SUCCEEDED",
        {"op-drafter": {"output": '{"op": "add_depends_on", "role": "b", "depends_on": "a"}'}},
    )
    outcome = await svc.refine_nl(row.id, _principal(), op_drafter_run_id=settled.id, dry_run=True)
    assert isinstance(outcome, RefineOutcome)
    assert outcome.applied is False  # dry_run previews
    assert outcome.verdict.would_block is False
    assert outcome.row.version == 1


async def test_refine_nl_failed_drafter_run_is_a_422() -> None:
    svc, _repo, team_runs = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    failed = team_runs.seed("FAILED")
    with pytest.raises(TeamRunError) as exc:
        await svc.refine_nl(row.id, _principal(), op_drafter_run_id=failed.id)
    assert exc.value.status_code == 422


async def test_refine_nl_collect_rejects_a_non_op_drafter_run() -> None:
    # fail-closed on run identity: an arbitrary same-org run id (e.g. a compiler run) is a
    # curated 422 on the FIRST read — never a burned poll budget + a misleading peel error.
    svc, _repo, team_runs = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a")]), sub_harnesses={}
    )
    imposter = team_runs.seed("RUNNING", manifest={"metadata": {"name": "harness-compiler"}})
    with pytest.raises(TeamRunError) as exc:
        await svc.refine_nl(row.id, _principal(), op_drafter_run_id=imposter.id)
    assert exc.value.status_code == 422
    assert exc.value.error_type == "not_an_op_drafter_run"


async def test_refine_nl_applies_to_the_current_document_not_a_pre_poll_snapshot() -> None:
    # a concurrent PUT landing while the drafter runs must NOT be clobbered by a stale apply
    svc, repo, team_runs = _service()
    row, _ = await svc.create(
        _principal(), name="d", manifest=_team([_member("a"), _member("b")]), sub_harnesses={}
    )
    settled = team_runs.seed(
        "SUCCEEDED",
        {"op-drafter": {"output": '{"op": "add_depends_on", "role": "b", "depends_on": "a"}'}},
    )
    # simulate the concurrent edit: rename via PUT before the collect call applies the op
    await svc.replace(
        row.id,
        _principal(),
        name="renamed-mid-poll",
        manifest=_team([_member("a"), _member("b")]),
        sub_harnesses={},
    )
    outcome = await svc.refine_nl(row.id, _principal(), op_drafter_run_id=settled.id)
    assert isinstance(outcome, RefineOutcome) and outcome.applied is True
    assert outcome.row.name == "renamed-mid-poll"  # the mid-poll edit survives
    assert outcome.row.version == 3  # v1 create → v2 PUT → v3 refine apply
