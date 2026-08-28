"""A run resolves its members' filed agents and keeps the result as its own record (#695, D3).

Once a draft stores references instead of inline manifests, something has to turn a reference back
into a manifest before the members can be dispatched. That happens ONCE, at run creation, and what
it produces is written onto the run row.

The reason it is a snapshot rather than a live read: an agent is editable in place and has no
version axis (ADR-050 D4). If a run resolved its members at dispatch, editing an agent would
rewrite the behaviour of runs that already happened. Holding the snapshot is what makes an
in-place edit safe — the edit changes the next run, never the last one.

The dispatch loop is deliberately UNCHANGED. ``team_run.py`` still looks each member up by role in
``sub_harnesses``, because the snapshot IS that dict.

RED until the [impl] adds the resolution step.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
)
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_EDITOR_ID = uuid.UUID("12341234-5678-5678-9abc-9abc9abc9abc")
_GRAPH_INGEST = "core/graph-ingest@1.0.0"


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _agent_manifest(role: str, caps: list[str], cap_id: uuid.UUID) -> dict[str, Any]:
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(cap_id),
            "name": role,
            "kind": "agent",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": [{"ref": r, "binding": r.split("/")[-1].split("@")[0]} for r in caps],
        "prompts": [{"role": "primary", "source": "inline", "body": f"You are the {role}."}],
        "runtime": {"entrypoint": "primary"},
        # an OHM actor's field is ``role`` (``OHMActor.role``, min_length=1) — ``build_subharness``
        # emits ``OHMActor(role="primary", …)``. Spelt ``name`` the fixture does not load, so
        # ``_enforce_member_ceilings`` rejected every manifest here as an invalid sub-harness before
        # any of these assertions could be reached (#694/#695 [impl] discovery).
        "actors": [{"role": "primary", "kind": "agent"}],
    }


def _team(manifest_ref: str, tools: list[str]) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "run-team",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [
            {
                "role": "editor",
                "kind": "agent",
                "manifest_ref": manifest_ref,
                "subgoal": "write the assessment",
                "tools": tools,
                "depends_on": [],
            }
        ],
        "runtime": {"entrypoint": "editor"},
    }


class _RunRow:
    def __init__(self, **kw: Any) -> None:
        self.id = uuid.uuid4()
        self.organisation_id = kw["organisation_id"]
        self.user_id = kw["user_id"]
        self.manifest = kw["manifest"]
        self.sub_harnesses = kw["sub_harnesses"]
        self.state = "QUEUED"
        self.results: dict[str, Any] = {}
        self.graph_id = kw.get("graph_id")
        self.workspace_root = kw.get("workspace_root")
        self.inputs = kw.get("inputs")
        self.seed_from_run_id = kw.get("seed_from_run_id")
        self.gate_decisions = kw.get("gate_decisions")


class _FakeRunRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _RunRow] = {}

    async def create(self, **kw: Any) -> _RunRow:
        row = _RunRow(**kw)
        self.rows[row.id] = row
        return row


class _FakeRegistry:
    """Resolves a filed agent by id, the way ``GET /api/v1/capabilities/{id}`` does."""

    def __init__(self, descriptors: dict[uuid.UUID, dict[str, Any]] | None = None) -> None:
        self.descriptors = descriptors or {}
        self.reads: list[uuid.UUID] = []

    async def get_capability(self, capability_id: uuid.UUID) -> dict[str, Any] | None:
        self.reads.append(capability_id)
        descriptor = self.descriptors.get(capability_id)
        return None if descriptor is None else {"id": str(capability_id), "descriptor": descriptor}


def _service(registry: _FakeRegistry | None = None) -> tuple[TeamRunService, _FakeRunRepo]:
    repo = _FakeRunRepo()
    svc = TeamRunService(
        team_runs=repo,  # type: ignore[arg-type] — duck-typed seam in unit tests
        enqueue=lambda _rid, _org, _user: None,
        registry=registry,  # type: ignore[call-arg] — the new seam this slice adds
    )
    return svc, repo


# ── R5: resolve the reference, keep the snapshot ──────────────────────────────


async def test_a_member_reference_is_resolved_and_snapshotted_onto_the_run() -> None:
    filed = _agent_manifest("editor", [_GRAPH_INGEST], _EDITOR_ID)
    registry = _FakeRegistry({_EDITOR_ID: filed})
    svc, repo = _service(registry)
    row = await svc.create(
        _principal(),
        manifest=_team(str(_EDITOR_ID), ["graph-ingest"]),
        sub_harnesses={},
        gate_decisions={},
    )
    stored = repo.rows[row.id].sub_harnesses
    assert set(stored) == {"editor"}
    assert stored["editor"]["metadata"]["name"] == "editor"
    assert [c["ref"] for c in stored["editor"]["capabilities"]] == [_GRAPH_INGEST]
    assert registry.reads == [_EDITOR_ID]


async def test_editing_the_agent_afterwards_leaves_the_run_record_alone() -> None:
    """The whole reason the snapshot exists. An agent has no version axis, so an in-place edit
    would otherwise rewrite what an old run is recorded as having executed."""
    filed = _agent_manifest("editor", [_GRAPH_INGEST], _EDITOR_ID)
    registry = _FakeRegistry({_EDITOR_ID: filed})
    svc, repo = _service(registry)
    row = await svc.create(
        _principal(),
        manifest=_team(str(_EDITOR_ID), ["graph-ingest"]),
        sub_harnesses={},
        gate_decisions={},
    )
    # a DEEP copy — comparing the stored dict against a reference to itself would pass under any
    # implementation, including one that resolves live at dispatch
    snapshot = copy.deepcopy(repo.rows[row.id].sub_harnesses["editor"])
    assert [c["ref"] for c in snapshot["capabilities"]] == [_GRAPH_INGEST]
    registry.descriptors[_EDITOR_ID] = _agent_manifest("editor", ["core/bash@1"], _EDITOR_ID)
    assert repo.rows[row.id].sub_harnesses["editor"] == snapshot
    assert registry.reads == [_EDITOR_ID]  # resolved ONCE, at creation — never re-read


async def test_an_unresolvable_reference_fails_the_run_at_creation() -> None:
    """Fail closed, and fail EARLY: a dangling reference discovered mid-drive would burn the
    upstream members' tokens before surfacing."""
    svc, _repo = _service(_FakeRegistry({}))
    with pytest.raises(TeamRunError) as exc:
        await svc.create(
            _principal(),
            manifest=_team(str(uuid.uuid4()), ["graph-ingest"]),
            sub_harnesses={},
            gate_decisions={},
        )
    assert exc.value.status_code == 422
    assert "editor" in str(exc.value)


# ── R6: a pre-existing draft still runs ───────────────────────────────────────


async def test_an_inline_sub_harness_wins_and_is_never_resolved() -> None:
    """Back-compat without a migration. A draft written before this slice carries inline manifests
    and an unresolvable ``org:compiled/<role>@1``; it must run exactly as it does today."""
    inline = _agent_manifest("editor", ["core/write@1"], uuid.uuid4())
    registry = _FakeRegistry({})
    svc, repo = _service(registry)
    row = await svc.create(
        _principal(),
        manifest=_team("org:compiled/editor@1", ["write"]),
        sub_harnesses={"editor": inline},
        gate_decisions={},
    )
    assert repo.rows[row.id].sub_harnesses == {"editor": inline}
    assert registry.reads == []  # the legacy ref is never dereferenced


async def test_a_legacy_reference_with_no_inline_manifest_is_a_clean_422() -> None:
    """``org:compiled/<role>@1`` was never resolvable. Without an inline manifest there is nothing
    to dispatch, and saying so beats a 500 halfway through the drive."""
    svc, _repo = _service(_FakeRegistry({}))
    with pytest.raises(TeamRunError) as exc:
        await svc.create(
            _principal(),
            manifest=_team("org:compiled/editor@1", ["write"]),
            sub_harnesses={},
            gate_decisions={},
        )
    assert exc.value.status_code == 422


# ── R7: the ceiling still holds for a filed agent ─────────────────────────────


@pytest.mark.security
async def test_a_filed_agent_edited_wider_than_the_member_is_rejected() -> None:
    """ADR-032. The agent is editable in place and the team that references it is not re-validated
    on that edit, so the run is where the two are reconciled. Without this, editing a filed agent
    would be a way to widen a member past what its team declared."""
    widened = _agent_manifest("editor", [_GRAPH_INGEST, "core/bash@1"], _EDITOR_ID)
    svc, _repo = _service(_FakeRegistry({_EDITOR_ID: widened}))
    with pytest.raises(TeamRunError) as exc:
        await svc.create(
            _principal(),
            manifest=_team(str(_EDITOR_ID), ["graph-ingest"]),  # the narrower ceiling
            sub_harnesses={},
            gate_decisions={},
        )
    assert exc.value.status_code == 422
    assert "bash" in str(exc.value) or "ceiling" in str(exc.value).lower()


@pytest.mark.security
async def test_a_filed_agent_within_the_member_ceiling_is_admitted() -> None:
    """The guard must not become a blanket refusal — a filed agent may legitimately narrow."""
    narrower = _agent_manifest("editor", [_GRAPH_INGEST], _EDITOR_ID)
    svc, repo = _service(_FakeRegistry({_EDITOR_ID: narrower}))
    row = await svc.create(
        _principal(),
        manifest=_team(str(_EDITOR_ID), ["graph-ingest", "web-research"]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert set(repo.rows[row.id].sub_harnesses) == {"editor"}


# ── #878 (ruled shape A): the engine binds the caller's model onto a resolved member ───────────
#
# Added by `backend-implementer` on the [impl] PR, after the ruling. ADR-050 D3 empties the draft's
# inline sub-harnesses, and the harness reads a model off the PER-MEMBER document — so a caller had
# nothing left to write one into and every member of a saved team failed
# `502: live LLM mode requires a model in the OHM`. The console's binder copies the SAME list into
# every role, and `test_team_draft_loop_gateway_e2e.py` step 6 does it line for line; this moves
# that copy behind the seam.

_MODELS = [
    {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": str(uuid.uuid4())},
    }
]


def _team_with_models(manifest_ref: str, tools: list[str]) -> dict[str, Any]:
    doc = _team(manifest_ref, tools)
    doc["models"] = _MODELS
    return doc


async def test_a_resolved_member_is_given_the_callers_model_binding() -> None:
    """The whole of #878. Without this the member document carries no model and the run 502s."""
    registry = _FakeRegistry({_EDITOR_ID: _agent_manifest("editor", [_GRAPH_INGEST], _EDITOR_ID)})
    svc, repo = _service(registry)
    row = await svc.create(
        _principal(),
        manifest=_team_with_models(str(_EDITOR_ID), ["graph-ingest"]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert repo.rows[row.id].sub_harnesses["editor"]["models"] == _MODELS


async def test_the_binding_does_not_disturb_the_rest_of_the_snapshot() -> None:
    """It adds one key. The resolved descriptor is otherwise the record of what executed."""
    filed = _agent_manifest("editor", [_GRAPH_INGEST], _EDITOR_ID)
    svc, repo = _service(_FakeRegistry({_EDITOR_ID: filed}))
    row = await svc.create(
        _principal(),
        manifest=_team_with_models(str(_EDITOR_ID), ["graph-ingest"]),
        sub_harnesses={},
        gate_decisions={},
    )
    stored = repo.rows[row.id].sub_harnesses["editor"]
    assert {k: v for k, v in stored.items() if k != "models"} == filed
    assert "models" not in filed  # the registry's copy is not mutated


async def test_an_inline_sub_harness_keeps_the_binding_the_caller_gave_it() -> None:
    """A caller that supplied the document bound it already, so the engine does not overwrite it —
    only a member the engine RESOLVED had nobody to bind it."""
    own = [{**_MODELS[0], "binding": "openrouter/anthropic/claude-haiku-4-5"}]
    inline = {**_agent_manifest("editor", [_GRAPH_INGEST], uuid.uuid4()), "models": own}
    svc, repo = _service(_FakeRegistry({}))
    row = await svc.create(
        _principal(),
        manifest=_team_with_models("org:compiled/editor@1", ["graph-ingest"]),
        sub_harnesses={"editor": inline},
        gate_decisions={},
    )
    assert repo.rows[row.id].sub_harnesses["editor"]["models"] == own


async def test_a_team_with_no_model_binding_resolves_exactly_as_before() -> None:
    """Default-OFF: a manifest carrying no ``models`` adds no key, so a team that bound its models
    some other way renders byte-for-byte as it did."""
    filed = _agent_manifest("editor", [_GRAPH_INGEST], _EDITOR_ID)
    svc, repo = _service(_FakeRegistry({_EDITOR_ID: filed}))
    row = await svc.create(
        _principal(),
        manifest=_team(str(_EDITOR_ID), ["graph-ingest"]),  # no models[]
        sub_harnesses={},
        gate_decisions={},
    )
    assert repo.rows[row.id].sub_harnesses["editor"] == filed


@pytest.mark.security
async def test_the_binding_does_not_widen_a_resolved_member_past_its_ceiling() -> None:
    """The ADR-032 re-check runs over the document that is actually stored, binding included."""
    widened = _agent_manifest("editor", [_GRAPH_INGEST, "core/bash@1"], _EDITOR_ID)
    svc, _repo = _service(_FakeRegistry({_EDITOR_ID: widened}))
    with pytest.raises(TeamRunError) as exc:
        await svc.create(
            _principal(),
            manifest=_team_with_models(str(_EDITOR_ID), ["graph-ingest"]),
            sub_harnesses={},
            gate_decisions={},
        )
    assert exc.value.status_code == 422


@pytest.mark.security
async def test_the_callers_binding_replaces_one_the_filed_agent_already_carried() -> None:
    """The ruling's recorded COST, pinned as a decision rather than left as prose (#878).

    An agent acquires a ``models`` key the ordinary way: it is filed from a draft whose
    sub-harnesses had a model bound into them client-side at GO. So a filed agent carrying its own
    binding is the normal case, not a contrived one, and the ruling says the caller's GO-time
    binding wins for every resolved member — which is what the console's unconditional overwrite
    already made true.

    Every other test here starts from an agent with NO ``models``, so they all exercise insertion.
    This is the replacement half, and it is the half a future reader could delete without reddening
    anything: silent, paid-for behaviour, changed by accident."""
    stale = [{**_MODELS[0], "config": {"credential_id": str(uuid.uuid4())}}]
    filed = {**_agent_manifest("editor", [_GRAPH_INGEST], _EDITOR_ID), "models": stale}
    svc, repo = _service(_FakeRegistry({_EDITOR_ID: filed}))
    row = await svc.create(
        _principal(),
        manifest=_team_with_models(str(_EDITOR_ID), ["graph-ingest"]),
        sub_harnesses={},
        gate_decisions={},
    )
    stored = repo.rows[row.id].sub_harnesses["editor"]["models"]
    assert stored == _MODELS
    # the stale credential is GONE, not merged beside the caller's — a run must never reach for a
    # credential its caller did not present
    assert stale[0]["config"]["credential_id"] not in str(stored)
