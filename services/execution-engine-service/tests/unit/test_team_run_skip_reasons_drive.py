"""#1119/#1154 — the drive persists WHY a member was skipped, mirroring #1108/#1111.

The new team-run graph read (``GET /v1/engine/team-runs/{id}/graph``, #1154) shows a per-node
``skip_reason`` + ``reason_role``. ``packages/ohm/orchestrate.py``'s ``run_team`` (pinned separately
by ``packages/ohm/tests/test_orchestrate_skip_reasons.py``, commit 1 of this PR) gains
``TeamRunResult.member_skip_reasons: dict[str, dict[str, str]]`` — role -> {"code", "role"} — and a
synchronous, best-effort ``on_skip(role, code, tested_role)`` hook fired before that role's own
checkpoint. This file pins the ENGINE half, exactly the shape ``test_member_failure_capture.py``
(#1108) and ``test_member_attempt_counts.py`` (#1111) already proved for their own per-member maps:

* ``TeamRunService.drive`` seeds ``member_skip_reasons`` from the row, carried forward only for
  roles this drive does NOT re-execute (the ``seeded``/``completed`` set) — a re-driven role starts
  clean, same as ``member_error_codes``/``member_attempt_counts``.
* A new ``_record_skip(role, code, tested_role)`` closure, passed as ``on_skip`` into
  ``run_team_hybrid``, writes ``member_skip_reasons[role] = {"code": code, "role": tested_role}``.
* ``_checkpoint`` persists ``member_skip_reasons`` onto the row via ``checkpoint(...)``, PRUNED to
  roles whose LIVE status is currently ``"skipped"`` at that instant (mirrors how ``member_timings``
  is pruned a few lines below it in ``team_run_service.py``).
* The mid-drive-death ``except Exception`` path (the G-C "never strand RUNNING" handler) persists
  the live skip-reasons map onto the FAILED ``transition(...)`` call, pruned to roles
  ``_backfill_unreached`` still reports ``"skipped"`` afterward — never a role it rewrote to
  ``"failed"``. (``_backfill_unreached`` only ever rewrites a ``"running"``/missing entry to
  ``"failed"``; an already-``"skipped"`` entry passes through untouched — see
  ``test_backfill_unreached_keeps_each_settled_members_own_terminal_status`` in
  ``test_team_run_checkpoint.py`` — so this premise holds and the test below drives exactly that.)
* The normal settle path writes ``result.member_skip_reasons`` (the orchestrator's own final map)
  onto the SUCCEEDED/FAILED/... ``transition(...)`` call WHOLESALE, no pruning needed.

RED until the [impl] lands: ``EngineTeamRun`` has no ``member_skip_reasons`` column (a ``TypeError``
constructing a row with that kwarg — the same RED shape ``test_member_failure_capture.py`` used
before #1108's column landed), and neither ``checkpoint(...)`` nor the settle/failure
``transition(...)`` calls carry the key yet (a ``KeyError`` on the fake repo's recorded kwargs) —
never a skip, never a module-level collection error. Nothing here needs a function-local seam
import: ``TeamRunService``, ``team_run.py``'s ``run_team_hybrid``, ``TeamRunRepository`` and
``EngineTeamRun`` all already exist and are safely importable at module level; the missing pieces
are a column and some kwargs, not a whole module.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class _NoopProvenance:
    """#826: ``provenance`` is a non-optional ``TeamRunService`` kwarg; unrelated here, so a no-op
    stand-in is enough (mirrors the sibling #1108/#1111 test files)."""

    async def emit(self, record: Any) -> None:
        return None


class FakeTeamRunRepo:
    """In-memory mirror of ``TeamRunRepository``, recording both every ``transition(...)`` call's
    kwargs (what the settle/failure paths are asserted against) and every ``checkpoint(...)``
    call's kwargs (what the mid-drive durability write is asserted against) — the union of the two
    sibling fakes in ``test_member_failure_capture.py`` and ``test_team_run_checkpoint.py``."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineTeamRun] = {}
        self.transitions: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []

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
        app_id: uuid.UUID | None = None,  # #932: tracks the real repo's signature
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
        self.checkpoints.append(dict(fields))
        return True


class _OutputHarness:
    """Dispatches per role (parsed off ``manifest_ref``); each role in ``outputs`` SUCCEEDS with
    the given ``output`` value, every other role succeeds with a bland placeholder."""

    def __init__(self, outputs: dict[str, Any] | None = None) -> None:
        self._outputs = outputs or {}

    def _role_of(self, manifest_ref: str | None) -> str:
        return manifest_ref.split("/")[-1].split("@")[0] if manifest_ref else "?"

    async def execute(self, **kw: Any) -> dict[str, Any]:
        role = self._role_of(kw.get("manifest_ref"))
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": self._outputs.get(role, f"{role}-out"),
            "total_tokens": 100,
        }


def _svc(repo: FakeTeamRunRepo, harness: Any) -> TeamRunService:
    return TeamRunService(
        team_runs=repo,
        provenance=_NoopProvenance(),
        harness=harness,
        enqueue=lambda _r, _o, _u: None,
    )


def _agent(
    role: str, deps: list[str] | None = None, *, run_if: dict[str, Any] | None = None
) -> dict[str, Any]:
    member: dict[str, Any] = {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "depends_on": deps or [],
    }
    if run_if is not None:
        member["run_if"] = run_if
    return member


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
    # the settle (or FAILED-abort) transition is the one carrying member_status; the earlier
    # QUEUED->RUNNING claim does not.
    return next(t for t in repo.transitions if "member_status" in t)


# ── (1) the normal settle path writes the orchestrator's map wholesale ───────────────────────────


async def test_settle_persists_skip_reasons() -> None:
    # "target" depends on "researcher", whose output field never satisfies the condition -> a
    # genuine run_if skip, not an injected predicate. The condition reads results["researcher"]
    # ["output"] (make_harness_dispatch wraps every harness answer as {"output": ..., ...}).
    repo = FakeTeamRunRepo()
    _condition = {"from_role": "researcher", "field": "output", "op": "eq", "value": "tradeable"}
    team = _team(
        [
            _agent("researcher"),
            _agent("target", ["researcher"], run_if=_condition),
        ]
    )
    harness = _OutputHarness({"researcher": "flat"})

    row = await _run(
        _svc(repo, harness), _principal(), manifest=team, sub_harnesses={}, gate_decisions={}
    )

    assert row.member_status["target"] == "skipped"
    assert _settle_transition(repo)["member_skip_reasons"] == {
        "target": {"code": "condition_false", "role": "researcher"}
    }


async def test_settle_records_an_empty_map_when_nothing_is_skipped() -> None:
    repo = FakeTeamRunRepo()
    team = _team([_agent("researcher"), _agent("target", ["researcher"])])
    harness = _OutputHarness({"researcher": "tradeable"})

    row = await _run(
        _svc(repo, harness), _principal(), manifest=team, sub_harnesses={}, gate_decisions={}
    )

    assert row.member_status["target"] == "succeeded"
    assert _settle_transition(repo)["member_skip_reasons"] == {}


# ── (2) a mid-drive checkpoint carries the reason for the role that IS skipped at that instant ───


async def test_checkpoint_writes_reason_with_skipped_status() -> None:
    repo = FakeTeamRunRepo()
    _condition = {"from_role": "researcher", "field": "output", "op": "eq", "value": "tradeable"}
    team = _team(
        [
            _agent("researcher"),
            _agent("target", ["researcher"], run_if=_condition),
        ]
    )
    harness = _OutputHarness({"researcher": "flat"})

    await _run(
        _svc(repo, harness), _principal(), manifest=team, sub_harnesses={}, gate_decisions={}
    )

    skip_checkpoints = [
        c for c in repo.checkpoints if (c.get("member_status") or {}).get("target") == "skipped"
    ]
    assert skip_checkpoints, "no checkpoint recorded 'target' as skipped"
    assert skip_checkpoints[0]["member_skip_reasons"] == {
        "target": {"code": "condition_false", "role": "researcher"}
    }


# ── (3) a re-drive clears a stale reason for a role it re-executes ───────────────────────────────


async def test_rerun_replaces_reasons_from_the_new_drive() -> None:
    # "b" was skipped on a prior drive and carries a stale reason naming "a"; this drive re-runs
    # "b" (modelled directly as state=QUEUED, the same shortcut
    # test_a_redrive_clears_a_stale_error_code_before_the_role_reexecutes uses for
    # member_error_codes) and it now succeeds because "a"'s output satisfies the condition this
    # time — the stale reason must not survive onto the settled row.
    repo = FakeTeamRunRepo()
    manifest = _team(
        [
            _agent("a"),
            _agent(
                "b",
                ["a"],
                run_if={"from_role": "a", "field": "output", "op": "eq", "value": "tradeable"},
            ),
        ]
    )
    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
        state="QUEUED",
        results={},
        paused_at=[],
        member_status={"a": "succeeded", "b": "skipped"},
        member_skip_reasons={"b": {"code": "condition_false", "role": "a"}},  # type: ignore[call-arg]
    )
    repo.rows[row.id] = row

    result = await _svc(repo, _OutputHarness({"a": "tradeable"})).drive(row.id, _principal())

    assert result.member_status["b"] == "succeeded"
    assert result.member_skip_reasons == {}


# ── (4) a mid-drive death prunes the live map to roles still 'skipped' after the backfill ────────


def _kill_after_checkpoint_with_skip(
    monkeypatch: Any,
    exc: BaseException,
    *,
    results: dict[str, Any],
    member_status: dict[str, str],
    skip_calls: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Stand in for the worker being killed mid-drive, the same technique
    ``test_team_run_checkpoint.py``'s ``_kill_after_one_checkpoint`` uses: replace
    ``run_team_hybrid`` outright, since the two real kills this covers (Celery's
    ``SoftTimeLimitExceeded`` / a SIGKILL) arrive from OUTSIDE the DAG, not from a member failing.

    Additionally invokes ``on_skip`` (synchronously, per commit 1's pin) for each entry in
    ``skip_calls`` BEFORE the checkpoint hook, mirroring the real orchestrator's own ordering
    (``on_skip`` fires before that role's checkpoint — see
    ``packages/ohm/tests/test_orchestrate_skip_reasons.py::test_on_skip_fires_before_the_members_checkpoint``).
    """
    from oraclous_execution_engine_service.services import team_run_service as trs

    seen: dict[str, Any] = {"wired_checkpoint": False, "wired_skip": False}

    async def _killed(*args: Any, **kw: Any) -> Any:
        skip_hook = kw.get("on_skip")
        seen["wired_skip"] = skip_hook is not None
        if skip_hook is not None:
            for role, code, tested_role in skip_calls:
                skip_hook(role, code, tested_role)
        checkpoint_hook = kw.get("on_checkpoint")
        seen["wired_checkpoint"] = checkpoint_hook is not None
        if checkpoint_hook is not None:
            await checkpoint_hook(results, member_status)
        raise exc

    monkeypatch.setattr(trs, "run_team_hybrid", _killed)
    return seen


async def test_failed_drive_keeps_only_skipped_roles_reasons(monkeypatch: Any) -> None:
    # "researcher" settled; "target" is genuinely skipped (run_if false, reason recorded via
    # on_skip); "other" is still "running" when the kill lands and _backfill_unreached rewrites it
    # to "failed" — per test_backfill_unreached_keeps_each_settled_members_own_terminal_status
    # (test_team_run_checkpoint.py), backfill NEVER turns an already-"skipped" entry into "failed",
    # so this premise holds exactly as stated in the plan: the FAILED transition's
    # member_skip_reasons must carry "target" only, never "other".
    repo = FakeTeamRunRepo()
    manifest = _team([_agent("researcher"), _agent("target", ["researcher"]), _agent("other")])
    seen = _kill_after_checkpoint_with_skip(
        monkeypatch,
        SoftTimeLimitExceeded(),
        results={"researcher": {"output": "flat"}},
        member_status={"researcher": "succeeded", "target": "skipped", "other": "running"},
        skip_calls=[("target", "condition_false", "researcher")],
    )

    row = await _run(
        _svc(repo, _OutputHarness()),
        _principal(),
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
    )

    assert seen["wired_checkpoint"], "the drive did not wire a checkpoint hook"
    assert row.state == "FAILED"
    assert row.member_status == {
        "researcher": "succeeded",
        "target": "skipped",
        "other": "failed",
    }
    assert _settle_transition(repo)["member_skip_reasons"] == {
        "target": {"code": "condition_false", "role": "researcher"}
    }


# ── the ORM model carries the column ──────────────────────────────────────────────────────────────


def test_engine_team_run_model_declares_a_member_skip_reasons_column() -> None:
    # role -> {"code", "role"}, JSONB, default {} — the same shape member_error_codes (migration
    # 0030) and member_attempt_counts (migration 0031) already carry.
    from sqlalchemy.dialects.postgresql import JSONB

    column = EngineTeamRun.__table__.columns["member_skip_reasons"]
    assert isinstance(column.type, JSONB)
    assert column.nullable is False
