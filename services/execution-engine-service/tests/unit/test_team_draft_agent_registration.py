"""Saving a compiled team files its agents, and gives them graph tools (#694 + #695).

Both issues meet in ``TeamDraftService._synthesize_subs``, which is why they ship together: #694
decides WHICH capability ref each generated member gets, #695 decides WHERE the generated manifest
is stored and what ``manifest_ref`` points at. Registering agents that still declared file tools
would put the wrong thing in the library permanently, so #694's correction lands first.

What is broken today, both on run ``fe548aac`` (real org, 14 members, a graph bound):

* every member resolved to ``core/write@1`` and wrote into ``/tmp/oraclous-agent-sandbox/<org>/``;
* every member's ``manifest_ref`` was ``org:compiled/<role>@1``, which resolves to nothing, and
  the generated manifests shipped inline in ``engine_team_drafts.sub_harnesses``.

The registry id is the reference (ADR-050, Proposed — amends ADR-031 §124). The console builder
already keys an agent by its registry id, and a compiled member and a console-built agent are the
same object; a second reference form would re-split the object #695 exists to merge.

RED until the [impl] lands.
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

# The SEEDED refs. Written down once in
# ``packages/ohm/tests/test_compiled_graph_substrate.py::_SEEDED_GRAPH_REFS``, which is the arbiter
# if these two suites ever disagree again — that disagreement is what #694 is about.
_GRAPH_INGEST = "core/graph-ingest@1.0.0"
_RETRIEVER = "core/knowledge-retriever@1.0.0"


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _member(role: str, deps: list[str] | None = None, tools: list[str] | None = None) -> dict:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:compiled/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
        "tools": tools or [],
    }


def _team(members: list[dict]) -> dict[str, Any]:
    return {
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
        self.writes = 0

    async def create(self, **kw: Any) -> _Row:
        self.writes += 1
        row = _Row(**kw)
        self.rows[row.id] = row
        return row

    async def get(self, draft_id: uuid.UUID, organisation_id: uuid.UUID) -> _Row | None:
        row = self.rows.get(draft_id)
        return row if row is not None and row.organisation_id == organisation_id else None

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
        self.writes += 1
        row = _Row(**kw)
        self.rows[row.id] = row
        return row, True

    async def replace(self, draft_id: uuid.UUID, org: uuid.UUID, **kw: Any) -> _Row | None:
        row = await self.get(draft_id, org)
        if row is None:
            return None
        self.writes += 1
        row.name, row.manifest, row.sub_harnesses = kw["name"], kw["manifest"], kw["sub_harnesses"]
        row.version += 1
        return row

    async def update_documents(self, draft_id: uuid.UUID, org: uuid.UUID, **kw: Any) -> _Row | None:
        row = await self.get(draft_id, org)
        if row is None:
            return None
        self.writes += 1
        row.manifest, row.sub_harnesses = kw["manifest"], kw["sub_harnesses"]
        row.version += 1
        return row

    async def delete(self, draft_id: uuid.UUID, org: uuid.UUID) -> bool:
        return self.rows.pop(draft_id, None) is not None


class _RunRow:
    def __init__(self, state: str, results: dict[str, Any] | None = None) -> None:
        self.id = uuid.uuid4()
        self.state = state
        self.results = results or {}
        self.manifest = {"metadata": {"name": "compiler-team"}}


class _FakeTeamRuns:
    def __init__(self) -> None:
        self.runs: dict[uuid.UUID, _RunRow] = {}

    def seed(self, state: str, results: dict[str, Any] | None = None) -> _RunRow:
        row = _RunRow(state, results)
        self.runs[row.id] = row
        return row

    async def get(self, run_id: uuid.UUID, principal: Principal) -> _RunRow:
        row = self.runs.get(run_id)
        if row is None:
            raise TeamRunError("team run not found", 404)
        return row


class _FakeRegistry:
    """The registry seam: the catalog read the service already uses, plus the new agent filing."""

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.filed: dict[uuid.UUID, dict[str, Any]] = {}
        self.upserts: list[uuid.UUID] = []
        self._fail_on = fail_on

    async def list_capability_rows(self) -> list[dict[str, str]]:
        return [
            {"name": n, "description": ""}
            for n in ("web-research", "graph-ingest", "knowledge-retriever", "bash")
        ]

    async def list_capabilities(self) -> list[str]:
        return [r["name"] for r in await self.list_capability_rows()]

    async def upsert_harness(
        self, descriptor: dict[str, Any], *, descriptor_id: uuid.UUID
    ) -> uuid.UUID:
        self.upserts.append(descriptor_id)
        if self._fail_on is not None and len(self.upserts) > self._fail_on:
            from oraclous_execution_engine_service.services.registry_client import (
                RegistryClientError,
            )

            raise RegistryClientError("registry unreachable: ConnectError")
        self.filed[descriptor_id] = descriptor
        return descriptor_id


def _service(
    registry: _FakeRegistry | None = None,
) -> tuple[TeamDraftService, _FakeDraftRepo, _FakeTeamRuns, _FakeRegistry]:
    repo, runs = _FakeDraftRepo(), _FakeTeamRuns()
    registry = registry if registry is not None else _FakeRegistry()
    svc = TeamDraftService(
        drafts=repo,  # type: ignore[arg-type] — duck-typed seams in unit tests
        team_runs=runs,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
    )
    return svc, repo, runs, registry


def _reviewer_results(compiled: dict[str, Any]) -> dict[str, Any]:
    return {"reviewer": {"output": json.dumps(compiled), "status": "SUCCEEDED"}}


def _compiled(tools: list[str] | None = None) -> dict[str, Any]:
    return {
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:compiled/researcher@1",
                "subgoal": "gather evidence",
                "tools": ["web-research"],
                "depends_on": [],
            },
            {
                "role": "editor",
                "kind": "agent",
                "manifest_ref": "org:compiled/editor@1",
                "subgoal": "write the assessment",
                "tools": tools if tools is not None else ["graph-ingest"],
                "depends_on": ["researcher"],
            },
        ]
    }


def _refs(row: _Row) -> dict[str, str]:
    return {m["role"]: m["manifest_ref"] for m in row.manifest["members"]}


# ── #694: the generated members get GRAPH capabilities ────────────────────────


async def test_a_generated_member_is_granted_the_graph_capability_not_the_file_one() -> None:
    """The heart of #694 at the service seam. ``graph-ingest`` must resolve to the real seeded ref,
    not a provisional ``core/graph-ingest@1``, so the registry resolves it."""
    svc, _repo, runs, registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _verdict, _created = await svc.create_from_run(_principal(), team_run_id=run.id)
    editor_id = uuid.UUID(_refs(row)["editor"])
    caps = {c["binding"]: c["ref"] for c in registry.filed[editor_id]["capabilities"]}
    assert caps == {"graph-ingest": _GRAPH_INGEST}


async def test_a_freshly_compiled_team_declaring_a_file_tool_is_refused() -> None:
    """Run ``fe548aac``'s exact shape: a member declaring the lower-cased ``write``.

    A NEW compile does not get healed, it gets rejected. Under the graph substrate the drafter is
    not shown ``write`` at all, so a draft that names it anyway is a defect in the compile, and
    ``create_from_run`` already refuses a blocked team with a 422 and persists nothing.

    Healing belongs to the drafts that predate this slice, and it happens on the paths that SAVE a
    blocked draft rather than refusing it — see the ``replace`` test below.
    """
    svc, repo, runs, registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled(tools=["write", "read"])))
    with pytest.raises(TeamRunError) as exc:
        await svc.create_from_run(_principal(), team_run_id=run.id)
    assert exc.value.status_code == 422
    assert "F-SUBSTRATE-FILE" in str(exc.value)
    assert repo.rows == {} and registry.filed == {}


async def test_replacing_a_legacy_draft_heals_its_file_refs_onto_the_graph() -> None:
    """Amendment 2: an old draft is repaired by being re-written, not by a migration and not by a
    rewrite at dispatch. ``replace`` re-synthesizes, so the stored manifest describes what it
    actually uses at all times.

    ``replace`` reports a blocked verdict but still persists — a draft may be SAVED blocked, and
    the user refines until the verdict is green. That is what makes it the healing seam and
    ``create_from_run`` (which refuses) not.

    Note the id rule this pins alongside the refs: the agent id is taken from the ``manifest_ref``
    on the manifest BEING WRITTEN when that parses as a UUID, and minted fresh when it does not.
    Here the incoming manifest carries the legacy ``org:compiled/editor@1``, so a fresh id is
    correct; ``test_replacing_a_draft_reuses_the_stored_ids`` writes a manifest already carrying
    UUIDs and pins the reuse side of the same rule.
    """
    svc, repo, runs, registry = _service()
    seeded = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _v, _c = await svc.create_from_run(_principal(), team_run_id=seeded.id)
    legacy = _team([_member("editor", tools=["write", "read"])])
    replaced, verdict = await svc.replace(
        row.id, _principal(), name="legacy", manifest=legacy, sub_harnesses={}
    )
    assert verdict.would_block is True  # honest about the file tools it still declares
    assert replaced.id in repo.rows  # ...and saved anyway, so the user can refine it
    editor_id = uuid.UUID(_refs(replaced)["editor"])
    caps = {c["binding"]: c["ref"] for c in registry.filed[editor_id]["capabilities"]}
    assert caps == {"write": _GRAPH_INGEST, "read": _RETRIEVER}
    # the BINDING is preserved (ADR-032, the ceiling is binding-based) — only the ref moves
    assert "core/write@1" not in caps.values() and "core/read@1" not in caps.values()


# ── #695: R1 — saving a team files its agents ─────────────────────────────────


async def test_saving_a_compiled_run_files_one_agent_per_member() -> None:
    svc, _repo, runs, registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _verdict, created = await svc.create_from_run(_principal(), team_run_id=run.id)
    assert created is True
    assert len(registry.filed) == 2
    assert {d["metadata"]["name"] for d in registry.filed.values()} == {"researcher", "editor"}
    assert all(d["metadata"]["kind"] == "agent" for d in registry.filed.values())


async def test_each_member_now_points_at_the_id_the_registry_returned() -> None:
    """ADR-050: ``manifest_ref`` is the registry id. ``org:compiled/<role>@1`` resolved to nothing;
    ``get_capability`` interpolates the ref straight into a path, so a ref carrying ``/`` produced
    a different URL rather than a lookup."""
    svc, _repo, runs, registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _verdict, _created = await svc.create_from_run(_principal(), team_run_id=run.id)
    for role, ref in _refs(row).items():
        assert uuid.UUID(ref) in registry.filed, role
        assert "org:compiled/" not in ref


async def test_the_draft_stops_carrying_the_generated_manifests_inline() -> None:
    """ADR-050 D3 — one source of truth. Keeping the inline copy AND a resolvable ref would mean
    two copies of every agent, and editing the filed one would silently not affect the team, which
    makes the reuse #695 exists for cosmetic. The run record keeps its own snapshot instead
    (see ``test_team_run_manifest_ref_snapshot.py``)."""
    svc, _repo, runs, _registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _verdict, _created = await svc.create_from_run(_principal(), team_run_id=run.id)
    assert row.sub_harnesses == {}


# ── #695: R2 — no duplicates ──────────────────────────────────────────────────


async def test_a_second_save_of_the_same_run_files_no_duplicate() -> None:
    """A reload or a second tab on ``?compile=<runId>``. Idempotent per ``(org, team_run_id)``."""
    svc, _repo, runs, registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    first, _v, created_first = await svc.create_from_run(_principal(), team_run_id=run.id)
    second, _v2, created_second = await svc.create_from_run(_principal(), team_run_id=run.id)
    assert created_first is True and created_second is False
    assert second.id == first.id
    assert len(registry.filed) == 2


async def test_refining_a_draft_refreshes_the_existing_agents_and_files_only_the_new_one() -> None:
    """R2's other half. The id comes from the STORED member's ``manifest_ref``, so an edit updates
    the agents the user already has rather than minting a second set beside them."""
    svc, _repo, runs, registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _v, _c = await svc.create_from_run(_principal(), team_run_id=run.id)
    before = dict(_refs(row))
    outcome = await svc.refine(
        row.id,
        _principal(),
        edit_op={
            "op": "add_member",
            "role": "fact-checker",
            "kind": "agent",
            "tools": ["web-research"],
            "depends_on": ["editor"],
            "subgoal": "verify the claims",
        },
    )
    assert outcome.applied is True, outcome.verdict.blocking
    after = _refs(outcome.row)
    assert {r: after[r] for r in before} == before  # the two originals keep their ids
    assert len(registry.filed) == 3  # exactly one NEW agent, never four


async def test_replacing_a_draft_reuses_the_stored_ids() -> None:
    svc, _repo, runs, registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _v, _c = await svc.create_from_run(_principal(), team_run_id=run.id)
    before = dict(_refs(row))
    replaced, _verdict = await svc.replace(
        row.id, _principal(), name="renamed", manifest=row.manifest, sub_harnesses={}
    )
    assert _refs(replaced) == before
    assert len(registry.filed) == 2
    assert registry.upserts.count(uuid.UUID(before["editor"])) == 2  # created, then refreshed


async def test_creating_a_draft_directly_also_builds_and_files_its_members() -> None:
    """Amendment 2 names four entry points, and ``create`` is one of them. It takes a
    caller-supplied ``sub_harnesses``, and ``_synthesize_subs`` fills only the roles the caller left
    out — so a caller's own sub-harness still wins, and an omitted member is no longer bodiless."""
    svc, repo, _runs, registry = _service()
    row, verdict = await svc.create(
        _principal(),
        name="hand-authored",
        manifest=_team([_member("researcher", tools=["web-research"])]),
        sub_harnesses={},
    )
    assert verdict.would_block is False, verdict.blocking
    assert row.id in repo.rows
    assert len(registry.filed) == 1
    assert uuid.UUID(_refs(row)["researcher"]) in registry.filed


async def test_a_non_agent_member_is_never_filed_as_an_agent() -> None:
    """A ``kind: human`` member is a gate, not an agent. It has no sub-harness to build and nothing
    to put in the library — filing one would place a person on ``/app/agents``."""
    svc, _repo, _runs, registry = _service()
    manifest = _team(
        [
            _member("researcher", tools=["web-research"]),
            {
                "role": "approver",
                "kind": "human",
                "human_role": "reviewer",
                "subgoal": "approve the assessment",
                "depends_on": ["researcher"],
                "tools": [],
            },
        ]
    )
    row, _verdict = await svc.create(
        _principal(), name="gated", manifest=manifest, sub_harnesses={}
    )
    assert len(registry.filed) == 1
    assert {d["metadata"]["name"] for d in registry.filed.values()} == {"researcher"}
    approver = next(m for m in row.manifest["members"] if m["role"] == "approver")
    assert approver.get("manifest_ref") in (None, "")


async def test_a_draft_from_another_org_is_absent_not_refiled() -> None:
    """ADR-006 fail-closed tenancy. A cross-org draft id is a 404, and the miss must not become a
    registration in the CALLER's org — that would copy one tenant's agent into another's library."""
    svc, _repo, runs, registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _v, _c = await svc.create_from_run(_principal(), team_run_id=run.id)
    filed_before = dict(registry.filed)
    stranger = Principal(
        principal_id=uuid.uuid4(), principal_type=PrincipalType.USER, organisation_id=uuid.uuid4()
    )
    with pytest.raises(TeamRunError) as exc:
        await svc.replace(row.id, stranger, name="stolen", manifest=row.manifest, sub_harnesses={})
    assert exc.value.status_code == 404
    assert registry.filed == filed_before


# ── #695: R3 — fail closed ────────────────────────────────────────────────────


async def test_a_registry_failure_fails_the_save_and_persists_nothing() -> None:
    """A half-registered draft is worse than a draft that was not saved: the team would point at
    one filed agent and one dangling ref, and the failure would only surface at the next run."""
    svc, repo, runs, _registry = _service(_FakeRegistry(fail_on=1))
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    with pytest.raises(TeamRunError):
        await svc.create_from_run(_principal(), team_run_id=run.id)
    assert repo.rows == {}
    assert repo.writes == 0


async def test_the_registry_failure_is_a_curated_error_not_a_500() -> None:
    svc, _repo, runs, _registry = _service(_FakeRegistry(fail_on=0))
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    with pytest.raises(TeamRunError) as exc:
        await svc.create_from_run(_principal(), team_run_id=run.id)
    assert exc.value.status_code in (502, 503)
    assert "registry" in str(exc.value).lower()


# ── #695: R4 — an abandoned compile files nothing ─────────────────────────────


async def test_a_run_that_is_never_saved_files_no_agent() -> None:
    """Registration happens at draft persistence, not at every compile. ``from-run`` IS the
    explicit save; a compile the user abandons never becomes a draft. The reporting org's two
    drafts alone would otherwise have produced 18 agents."""
    svc, repo, runs, registry = _service()
    abandoned = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    saved = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    # only the second run is saved; both compiled the same two members
    await svc.create_from_run(_principal(), team_run_id=saved.id)
    assert len(repo.rows) == 1
    assert len(registry.filed) == 2, "the abandoned compile must not have contributed four"
    # and reading the saved draft back registers nothing further
    stored = next(iter(repo.rows.values()))
    before = dict(registry.filed)
    await svc.get(stored.id, _principal())
    assert registry.filed == before
    assert await repo.get_by_team_run(_ORG, abandoned.id) is None


async def test_a_failed_compile_files_nothing() -> None:
    svc, repo, runs, registry = _service()
    run = runs.seed("FAILED", _reviewer_results(_compiled()))
    with pytest.raises(TeamRunError):
        await svc.create_from_run(_principal(), team_run_id=run.id)
    assert registry.filed == {}
    assert repo.rows == {}


async def test_a_blocked_compile_files_nothing() -> None:
    """The capability-absence gate rejects the team, so nothing reaches the library."""
    svc, repo, runs, registry = _service()
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled(tools=["definitely-not-a-real-tool"])))
    with pytest.raises(TeamRunError):
        await svc.create_from_run(_principal(), team_run_id=run.id)
    assert registry.filed == {}
    assert repo.rows == {}


# ── back-compat: an unwired registry ──────────────────────────────────────────


async def test_without_a_wired_registry_the_draft_keeps_its_inline_manifests() -> None:
    """``registry`` is optional on this service (it degrades seed-only on a registry outage for the
    catalog). With none wired there is nowhere to file an agent, so the old inline shape is kept
    rather than a draft being written with refs that resolve to nothing."""
    repo, runs = _FakeDraftRepo(), _FakeTeamRuns()
    svc = TeamDraftService(drafts=repo, team_runs=runs)  # type: ignore[arg-type]
    run = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _verdict, _created = await svc.create_from_run(_principal(), team_run_id=run.id)
    assert set(row.sub_harnesses) == {"researcher", "editor"}
    assert row.sub_harnesses["editor"]["metadata"]["kind"] == "agent"


# ── #694 B2: a file ref arriving via sub_harnesses[], not via members[].tools ──────────────────
#
# Added by `backend-implementer` on the [impl] PR, at `qa-engineer`'s request (Q2/Q3) after
# `code-reviewer` reproduced the hole (B2). The suite covered a file tool declared in
# ``members[].tools``; nothing covered one arriving underneath, in a caller-supplied descriptor's
# own ``capabilities[].ref``. Both gates read the declared tools, and the ceiling check is
# binding-based, so a tmp-sandbox ref hiding under a clean binding passed everything and was FILED.


def _sub_with_ref(role: str, ref: str, binding: str) -> dict[str, Any]:
    """A caller-supplied sub-harness whose BINDING is clean and whose REF is not."""
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": role,
            "kind": "agent",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": [{"ref": ref, "binding": binding}],
        "prompts": [{"role": "primary", "source": "inline", "body": f"You are the {role}."}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }


async def test_creating_a_draft_never_files_a_tmp_sandbox_ref_hidden_under_a_clean_binding() -> (
    None
):
    """The member declares ``graph-ingest`` and every gate agrees, because every gate reads the
    DECLARED tools. The descriptor underneath carries ``core/write@1``, which is the per-org tmp
    directory run ``fe548aac`` wrote 10 KB into. Filed, that survives the run rather than dying
    with it — the exact thing #694's correction lands first to prevent."""
    svc, _repo, _runs, registry = _service()
    row, verdict = await svc.create(
        _principal(),
        name="clean-looking",
        manifest=_team([_member("editor", tools=["graph-ingest"])]),
        sub_harnesses={"editor": _sub_with_ref("editor", "core/write@1", "graph-ingest")},
    )
    assert verdict.would_block is False, verdict.blocking  # the declared tools really are clean
    filed = registry.filed[uuid.UUID(_refs(row)["editor"])]
    assert [c["ref"] for c in filed["capabilities"]] == [_GRAPH_INGEST]
    # the BINDING is untouched, so the member's ceiling (ADR-032) still matches
    assert [c["binding"] for c in filed["capabilities"]] == ["graph-ingest"]


async def test_replacing_a_draft_never_files_a_tmp_sandbox_ref_hidden_under_a_clean_binding() -> (
    None
):
    """``replace`` takes ``sub_harnesses`` straight off the request body too."""
    svc, _repo, runs, registry = _service()
    seeded = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _v, _c = await svc.create_from_run(_principal(), team_run_id=seeded.id)
    replaced, _verdict = await svc.replace(
        row.id,
        _principal(),
        name="clean-looking",
        manifest=_team([_member("editor", tools=["graph-ingest"])]),
        sub_harnesses={"editor": _sub_with_ref("editor", "core/edit@1", "graph-ingest")},
    )
    filed = registry.filed[uuid.UUID(_refs(replaced)["editor"])]
    assert [c["ref"] for c in filed["capabilities"]] == [_GRAPH_INGEST]


async def test_a_forwarded_pre_695_draft_is_healed_rather_than_filed_verbatim() -> None:
    """Q3 — the same gap from the non-adversarial side, and the likelier way in.

    A console holding a draft saved before this slice forwards the ``sub_harnesses`` it already
    has. ``_synthesize_subs`` keeps a supplied entry verbatim, so without a correction at filing
    time Amendment 2 heals nothing at all on the path that actually carries the legacy documents —
    only on the one that passes ``{}``."""
    svc, _repo, runs, registry = _service()
    seeded = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    row, _v, _c = await svc.create_from_run(_principal(), team_run_id=seeded.id)
    legacy_subs = {
        "editor": _sub_with_ref("editor", "core/write@1", "write"),
        "researcher": _sub_with_ref("researcher", "core/read@1", "read"),
    }
    replaced, _verdict = await svc.replace(
        row.id,
        _principal(),
        name="forwarded",
        manifest=_team([_member("editor", tools=["write"]), _member("researcher", tools=["read"])]),
        sub_harnesses=legacy_subs,
    )
    filed = {
        role: {c["binding"]: c["ref"] for c in registry.filed[uuid.UUID(ref)]["capabilities"]}
        for role, ref in _refs(replaced).items()
    }
    assert filed == {"editor": {"write": _GRAPH_INGEST}, "researcher": {"read": _RETRIEVER}}


@pytest.mark.security
async def test_a_draft_cannot_name_an_agent_id_its_stored_draft_does_not_reference() -> None:
    """``manifest_ref`` arrives on the REQUEST BODY, and filing PUTs an existing row in place.

    Cross-org was already closed (the registry read and write are both org-scoped under RLS). This
    is the same-org case: naming a colleague's agent id in a draft you are creating must not
    silently overwrite that agent's descriptor. An unknown id mints a fresh agent instead — a spare
    row, never a clobbered one."""
    svc, _repo, runs, registry = _service()
    seeded = runs.seed("SUCCEEDED", _reviewer_results(_compiled()))
    victim_row, _v, _c = await svc.create_from_run(_principal(), team_run_id=seeded.id)
    victim_id = uuid.UUID(_refs(victim_row)["editor"])
    victim_descriptor = dict(registry.filed[victim_id])

    hostile = _team([_member("editor", tools=["graph-ingest"])])
    hostile["members"][0]["manifest_ref"] = str(victim_id)  # someone else's agent, same org
    row, _verdict = await svc.create(
        _principal(), name="hostile", manifest=hostile, sub_harnesses={}
    )
    assert uuid.UUID(_refs(row)["editor"]) != victim_id
    assert registry.filed[victim_id] == victim_descriptor  # untouched
