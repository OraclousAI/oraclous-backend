"""#1108 ruling 2b/2c — the engine captures and persists a curated per-member failure token.

The harness-runtime half (ruling 2a, ``LLMClientError`` with a 401/403 status -> ``LoopResult.
error_type = "llm_credential_rejected"``) is pinned separately (T3, ``test_tool_use_loop.py``).
This file pins the ENGINE's half:

* ``make_harness_dispatch``'s ``dispatch`` closure gains ``on_member_failure(role, error_type)``,
  a sibling of ``on_child``/``on_cost``, fired in the fail-closed branch (a FAILED member) with a
  SANITIZED token (``^[a-z][a-z0-9_]{0,63}$``) — never a raw class name or free-text message. A
  token that fails the pattern, or a SUCCEEDED/PARTIAL member, never fires it.
* ``TeamRunService``'s settle persists the collected ``{role: token}`` map onto the row via
  ``transition(...)``'s ``member_error_codes`` kwarg.
* A re-drive clears a stale entry for a role it re-executes, so a role that fails once and then
  succeeds on re-run does not go on reporting the old rejection.
* The ORM model (``models/team_run.py``) gains the ``member_error_codes`` JSONB column.

RED until the [impl] lands: ``make_harness_dispatch`` has no ``on_member_failure`` parameter (a
``TypeError`` at the call site, marked ``# type: ignore[call-arg]`` — the new seam this slice
adds), the settle ``transition(...)`` call never carries ``member_error_codes`` (a ``KeyError`` on
the fake repo's recorded kwargs), and ``EngineTeamRun`` has no such column (a ``KeyError`` on
``__table__.columns``) — never a skip, never a module-level collection error.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.harness_client import HarnessClientError
from oraclous_execution_engine_service.services.team_run import make_harness_dispatch
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
from oraclous_governance import Principal, PrincipalType
from oraclous_ohm.manifest import OHMMember
from sqlalchemy.dialects.postgresql import JSONB

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


def _member(role: str = "a") -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", tools=[])


class _StubHarness:
    """Returns one scripted ``execute()`` result — never a real harness call."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        return self._result


# ── make_harness_dispatch: on_member_failure fires only for a genuine, curated token ─────────────


async def test_dispatch_reports_a_curated_token_on_a_failed_member() -> None:
    harness = _StubHarness(
        {
            "id": str(uuid.uuid4()),
            "status": "FAILED",
            "output": None,
            "error_type": "llm_credential_rejected",
        }
    )
    calls: list[tuple[str, str]] = []
    dispatch = make_harness_dispatch(
        harness,
        {},
        on_member_failure=lambda role, token: calls.append((role, token)),  # type: ignore[call-arg]
    )
    with pytest.raises(HarnessClientError):
        await dispatch(_member("reader"), [], None)
    assert calls == [("reader", "llm_credential_rejected")]  # fail-closed behaviour unchanged


@pytest.mark.parametrize(
    "bad_error_type",
    ["LLMClientError", "x" * 65, "bad token!", "", None],
    ids=["class-name-case", "too-long", "punctuation", "empty", "absent"],
)
async def test_dispatch_drops_a_non_curated_error_type(bad_error_type: str | None) -> None:
    result: dict[str, Any] = {"id": str(uuid.uuid4()), "status": "FAILED", "output": None}
    if bad_error_type is not None:
        result["error_type"] = bad_error_type
    calls: list[tuple[str, str]] = []
    dispatch = make_harness_dispatch(
        _StubHarness(result),
        {},
        on_member_failure=lambda role, token: calls.append((role, token)),  # type: ignore[call-arg]
    )
    with pytest.raises(HarnessClientError):
        await dispatch(_member(), [], None)
    assert calls == []


async def test_dispatch_never_reports_a_succeeded_member() -> None:
    calls: list[tuple[str, str]] = []
    dispatch = make_harness_dispatch(
        _StubHarness({"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ok"}),
        {},
        on_member_failure=lambda role, token: calls.append((role, token)),  # type: ignore[call-arg]
    )
    await dispatch(_member(), [], None)
    assert calls == []


# ── TeamRunService.drive: the collected tokens are persisted at settle ────────────────────────────


class _NoopProvenance:
    """#826: ``provenance`` is a non-optional TeamRunService kwarg; unrelated here, so a no-op
    stand-in is enough."""

    async def emit(self, record: Any) -> None:
        return None


class FakeTeamRunRepo:
    """In-memory mirror of TeamRunRepository, additionally recording every ``transition(...)``
    call's kwargs — what this file asserts ``member_error_codes`` lands in."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineTeamRun] = {}
        self.transitions: list[dict[str, Any]] = []

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
        self.transitions.append(dict(fields))
        return row, True

    async def checkpoint(
        self, team_run_id: uuid.UUID, organisation_id: uuid.UUID, **fields: Any
    ) -> bool:
        row = self.rows.get(team_run_id)
        if row is None or row.organisation_id != organisation_id or row.state != "RUNNING":
            return False
        for key, value in fields.items():
            setattr(row, key, value)
        return True


class _RoleFailureHarness:
    """Dispatches per role; a role named in ``failures`` returns FAILED with the given curated
    ``error_type`` token, every other role succeeds."""

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self._failures = failures or {}

    def _role_of(self, manifest_ref: str | None) -> str:
        return manifest_ref.split("/")[-1].split("@")[0] if manifest_ref else "?"

    async def execute(self, **kw: Any) -> dict[str, Any]:
        role = self._role_of(kw.get("manifest_ref"))
        if role in self._failures:
            return {
                "id": str(uuid.uuid4()),
                "status": "FAILED",
                "output": None,
                "error_type": self._failures[role],
            }
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": f"{role}-out"}


def _svc(repo: FakeTeamRunRepo, harness: Any) -> TeamRunService:
    return TeamRunService(
        team_runs=repo,
        provenance=_NoopProvenance(),
        harness=harness,
        enqueue=lambda _r, _o, _u: None,
    )


def _agent(role: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "depends_on": deps or [],
    }


def _team(members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "t",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }


async def _run(svc: TeamRunService, principal: Principal, **kw: Any) -> EngineTeamRun:
    row = await svc.create(principal, **kw)
    return await svc.drive(row.id, principal)


def _settle_transition(repo: FakeTeamRunRepo) -> dict[str, Any]:
    # the settle transition is the one carrying member_status; the earlier QUEUED->RUNNING claim
    # does not.
    return next(t for t in repo.transitions if "member_status" in t)


async def test_settle_persists_the_curated_token_for_a_failed_member() -> None:
    repo = FakeTeamRunRepo()
    await _run(
        _svc(repo, _RoleFailureHarness({"a": "llm_credential_rejected"})),
        _principal(),
        manifest=_team([_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert _settle_transition(repo)["member_error_codes"] == {"a": "llm_credential_rejected"}


async def test_settle_records_an_empty_map_when_every_member_succeeds() -> None:
    repo = FakeTeamRunRepo()
    await _run(
        _svc(repo, _RoleFailureHarness({})),
        _principal(),
        manifest=_team([_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert _settle_transition(repo)["member_error_codes"] == {}


async def test_settle_only_persists_the_role_that_actually_failed() -> None:
    repo = FakeTeamRunRepo()
    await _run(
        _svc(repo, _RoleFailureHarness({"b": "llm_credential_rejected"})),
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert _settle_transition(repo)["member_error_codes"] == {"b": "llm_credential_rejected"}


async def test_a_redrive_clears_a_stale_error_code_before_the_role_reexecutes() -> None:
    # #1108 ruling 2c: 'b' failed on a prior drive and carries a stale token; this drive re-runs
    # 'b' (already re-queued by rerun(), modelled here by state="QUEUED") and it now succeeds — the
    # stale token must not survive onto the settled row.
    repo = FakeTeamRunRepo()
    manifest = _team([_agent("a"), _agent("b", ["a"])])
    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
        state="QUEUED",
        results={"a": {"output": "a-out"}},
        paused_at=[],
        member_status={"a": "succeeded", "b": "failed"},
        member_error_codes={"b": "llm_credential_rejected"},  # type: ignore[call-arg]
    )
    repo.rows[row.id] = row

    result = await _svc(repo, _RoleFailureHarness({})).drive(row.id, _principal())

    assert result.member_error_codes == {}


# ── the ORM model carries the column ───────────────────────────────────────────────────────────


def test_engine_team_run_model_declares_a_member_error_codes_column() -> None:
    # #1108 ruling 2c: JSONB role -> curated token, default {} — the same shape as
    # child_execution_roles (#828, migration 0025); this one is migration 0030.
    column = EngineTeamRun.__table__.columns["member_error_codes"]
    assert isinstance(column.type, JSONB)
    assert column.nullable is False
