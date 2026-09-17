"""#1111 item 4 (engine half) — the engine captures and persists a member's attempt count.

Decision 4 (posted on #1111): ``attempts = 1 + the in-run recovery retries the member spent``,
reported by the harness on the execution result it hands back (T-harness,
``test_member_attempt_count.py``). This file pins the ENGINE's half, mirroring #1108's own
``on_member_failure``/``member_error_codes`` shape (``test_member_failure_capture.py``) exactly:

* ``make_harness_dispatch``'s ``dispatch`` closure gains ``on_member_attempts(role, attempts)``, a
  sibling of ``on_member_failure``, fired in the SAME fail-closed branch (a non-SUCCEEDED/PARTIAL
  member) with a validated ``int >= 1`` — never a missing, non-int, or non-positive value. Scoped to
  a failed member only, same as ``on_member_failure``: a run's free-text failure summary is the only
  consumer today (T-summary, below), and it only ever discusses failed members.
* ``TeamRunService``'s settle persists the collected ``{role: count}`` map onto the row via
  ``transition(...)``'s ``member_attempt_counts`` kwarg.
* A re-drive clears a stale entry for a role it re-executes (the same #1108 ruling 2c shape).
* The ORM model (``models/team_run.py``) gains the ``member_attempt_counts`` JSONB column.

RED until the [impl] lands: ``make_harness_dispatch`` has no ``on_member_attempts`` parameter (a
``TypeError`` at the call site, marked ``# type: ignore[call-arg]`` — the new seam this slice adds),
the settle ``transition(...)`` call never carries ``member_attempt_counts`` (a ``KeyError`` on the
fake repo's recorded kwargs), and ``EngineTeamRun`` has no such column (a ``KeyError`` on
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


# ── make_harness_dispatch: on_member_attempts fires only for a genuine, valid count ──────────────


async def test_dispatch_reports_the_attempt_count_on_a_failed_member() -> None:
    harness = _StubHarness(
        {
            "id": str(uuid.uuid4()),
            "status": "FAILED",
            "output": None,
            "error_type": "tool_quota_exhausted",
            "attempts": 3,
        }
    )
    calls: list[tuple[str, int]] = []
    dispatch = make_harness_dispatch(
        harness,
        {},
        on_member_attempts=lambda role, count: calls.append((role, count)),  # type: ignore[call-arg]
    )
    with pytest.raises(HarnessClientError):
        await dispatch(_member("reader"), [], None)
    assert calls == [("reader", 3)]


@pytest.mark.parametrize(
    "bad_attempts",
    [None, "3", 0, -1, 1.5],
    ids=["absent", "string", "zero", "negative", "float"],
)
async def test_dispatch_drops_a_missing_or_invalid_attempt_count(bad_attempts: Any) -> None:
    result: dict[str, Any] = {"id": str(uuid.uuid4()), "status": "FAILED", "output": None}
    if bad_attempts is not None:
        result["attempts"] = bad_attempts
    calls: list[tuple[str, int]] = []
    dispatch = make_harness_dispatch(
        _StubHarness(result),
        {},
        on_member_attempts=lambda role, count: calls.append((role, count)),  # type: ignore[call-arg]
    )
    with pytest.raises(HarnessClientError):
        await dispatch(_member(), [], None)
    assert calls == []


async def test_dispatch_never_reports_a_succeeded_member() -> None:
    calls: list[tuple[str, int]] = []
    result = {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ok", "attempts": 2}
    dispatch = make_harness_dispatch(
        _StubHarness(result),
        {},
        on_member_attempts=lambda role, count: calls.append((role, count)),  # type: ignore[call-arg]
    )
    await dispatch(_member(), [], None)
    assert calls == []


# ── TeamRunService.drive: the collected attempt counts are persisted at settle ───────────────────


class _NoopProvenance:
    """#826: ``provenance`` is a non-optional TeamRunService kwarg; unrelated here, so a no-op
    stand-in is enough."""

    async def emit(self, record: Any) -> None:
        return None


class FakeTeamRunRepo:
    """In-memory mirror of TeamRunRepository, additionally recording every ``transition(...)``
    call's kwargs — what this file asserts ``member_attempt_counts`` lands in."""

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
    """Dispatches per role; a role named in ``failures`` returns FAILED carrying its scripted
    attempt count, every other role succeeds."""

    def __init__(self, failures: dict[str, int] | None = None) -> None:
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
                "error_type": "tool_quota_exhausted",
                "attempts": self._failures[role],
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


async def test_settle_persists_the_attempt_count_for_a_failed_member() -> None:
    repo = FakeTeamRunRepo()
    await _run(
        _svc(repo, _RoleFailureHarness({"a": 3})),
        _principal(),
        manifest=_team([_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert _settle_transition(repo)["member_attempt_counts"] == {"a": 3}


async def test_settle_records_an_empty_map_when_every_member_succeeds() -> None:
    repo = FakeTeamRunRepo()
    await _run(
        _svc(repo, _RoleFailureHarness({})),
        _principal(),
        manifest=_team([_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert _settle_transition(repo)["member_attempt_counts"] == {}


async def test_a_redrive_clears_a_stale_attempt_count_before_the_role_reexecutes() -> None:
    # #1108 ruling 2c's shape, applied to attempts: 'b' failed on a prior drive with a stale count
    # and this drive re-runs 'b' (already re-queued by rerun(), modelled here by state="QUEUED") and
    # it now succeeds — the stale count must not survive onto the settled row.
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
        member_attempt_counts={"b": 4},  # type: ignore[call-arg]
    )
    repo.rows[row.id] = row

    result = await _svc(repo, _RoleFailureHarness({})).drive(row.id, _principal())

    assert result.member_attempt_counts == {}


# ── the ORM model carries the column ───────────────────────────────────────────────────────────


def test_engine_team_run_model_declares_a_member_attempt_counts_column() -> None:
    # #1111 decision 4: JSONB role -> attempt count, default {} — the same shape #1108's
    # member_error_codes column has (migration 0030); this one is migration 0031.
    column = EngineTeamRun.__table__.columns["member_attempt_counts"]
    assert isinstance(column.type, JSONB)
    assert column.nullable is False
