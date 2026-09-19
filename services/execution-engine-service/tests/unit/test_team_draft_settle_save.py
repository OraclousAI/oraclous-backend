"""#1169 — a build run that settles SUCCEEDED is saved as a team by the ENGINE, at settle time.

Pinned seam names: ``TeamRunService.__init__(..., team_draft_saver=<CompiledTeamSaver>,
team_draft_save_timeout=<float>)``, mirroring the held ``team_drafts``/``schedules``
collaborators and the ``artifact_save_timeout`` posture (resolved once at the wiring boundary from
``ENGINE_TEAM_DRAFT_SAVE_TIMEOUT_SECONDS``, threaded down explicitly). ``CompiledTeamSaver`` is a
``Protocol`` in ``team_run_service`` whose one method is
``save_from_run(*, run_row, org, user_id, name)`` (async).

Why: the console only saves a compiled team (``POST /v1/engine/team-drafts/from-run``) while its own
tab is still open, so a build that succeeds on a retry, or after the tab closed, was never saved.
``_drive``'s SUCCEEDED tail now calls the saver AFTER ``_consume_verdict`` (so a RE_TASK back to
QUEUED or an ESCALATE to PAUSED never saves), only for a compile run (``is_compile_run``), best
effort: a raising or hanging saver never raises out of ``_drive`` and never flips the run.

Pinned observable behaviour, not exact wiring: the saver is called with the run row, the run's OWN
organisation as ``org`` (tenancy), the run's ``user_id`` and a ``name`` string. The name (case 7):
the compile manifest's own name is always the generic ``harness-compiler`` (the predicate requires
it), which is no title for a saved team, so the settle-time name is the deterministic fallback
``compiled-`` plus the first 8 hex chars of the run id.

RED until the [impl] lands: every test builds ``TeamRunService`` with the new kwargs, so each fails
with a ``TypeError`` on the unknown ``team_draft_saver`` (never a skip, never a collection error).
No not-yet-built module is imported here, so there is no module-level seam import to hoist.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_FAST = 0.05  # an injected, very small save timeout


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class _RecordingProvenance:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, record: Any) -> None:
        self.events.append(record)


class _FakeTeamRunRepo:
    """In-memory mirror of ``TeamRunRepository`` (create/get/transition/checkpoint)."""

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

    async def checkpoint(
        self, team_run_id: uuid.UUID, organisation_id: uuid.UUID, **fields: Any
    ) -> bool:
        row = self.rows.get(team_run_id)
        if row is None or row.organisation_id != organisation_id or row.state != "RUNNING":
            return False
        for key, value in fields.items():
            setattr(row, key, value)
        return True


class _Harness:
    """Every member SUCCEEDS, except the roles named in ``failing`` (a FAILED execution)."""

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self._failing = failing

    async def execute(self, **kw: Any) -> dict[str, Any]:
        ref = kw.get("manifest_ref") or ""
        role = ref.split("/")[-1].split("@")[0]
        if role in self._failing:
            return {
                "id": str(uuid.uuid4()),
                "status": "FAILED",
                "output": None,
                "error_message": "x",
            }
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": f"{role}-out",
            "total_tokens": 100,
        }


class _Evaluate:
    """A scripted judge: every check grades below threshold (so a gated run is verdict-consumed)."""

    async def evaluate(self, **kw: Any) -> dict[str, Any]:
        return {"pass": False, "score": 0.2, "recommended_action": "escalate_human"}

    async def aclose(self) -> None:
        return None


class _Saver:
    """A stand-in ``CompiledTeamSaver``: records every ``save_from_run`` call."""

    def __init__(self, *, raises: Exception | None = None, hangs: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises
        self._hangs = hangs

    async def save_from_run(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self._raises is not None:
            raise self._raises
        if self._hangs:
            await asyncio.Event().wait()
        return None


def _compile_manifest() -> dict[str, Any]:
    """A compile-run-shaped team (the ``is_compile_run`` predicate: the compiler's name plus its
    reviewer member), small enough to drive with a fake harness."""
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "harness-compiler",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [
            {
                "role": "drafter",
                "kind": "agent",
                "manifest_ref": "org:compiler/drafter@1",
                "subgoal": "draft",
                "depends_on": [],
            },
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:compiler/reviewer@1",
                "subgoal": "review",
                "depends_on": ["drafter"],
            },
        ],
        "runtime": {"entrypoint": "drafter"},
    }


def _ordinary_manifest() -> dict[str, Any]:
    doc = _compile_manifest()
    doc["metadata"]["name"] = "support-triage"
    doc["members"][1]["role"] = "closer"
    doc["members"][1]["manifest_ref"] = "org:support/closer@1"
    doc["runtime"]["entrypoint"] = "drafter"
    return doc


def _gated(manifest: dict[str, Any], severity: str) -> dict[str, Any]:
    """Gate the run through a scored battery a failing judge turns into a verdict action:
    MAJOR -> re_task (back to QUEUED), CRITICAL -> escalate (PAUSED)."""
    manifest["orchestration"] = {"success_criteria": "battery:gate"}
    manifest["batteries"] = {
        "gate": {
            "name": "gate",
            "floor": "and",
            "checks": [
                {
                    "name": "correct",
                    "kind": "evaluator",
                    "rubric": "the result is correct",
                    "severity": severity,
                }
            ],
        }
    }
    return manifest


def _build(
    repo: _FakeTeamRunRepo,
    provenance: _RecordingProvenance,
    *,
    harness: Any = None,
    evaluate: Any = None,
    **service_kwargs: Any,
) -> TeamRunService:
    return TeamRunService(
        team_runs=repo,  # type: ignore[arg-type]
        provenance=provenance,  # type: ignore[arg-type]
        harness=harness or _Harness(),
        enqueue=lambda *_a: None,
        evaluate=evaluate,
        **service_kwargs,
    )


async def _drive(svc: TeamRunService, manifest: dict[str, Any]) -> EngineTeamRun:
    row = await svc.create(_principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    return await svc.drive(row.id, _principal())


# ── (1) a compile run that settles SUCCEEDED is saved, once, under its own org ────────────────


async def test_a_succeeded_compile_run_is_saved_once_under_its_own_org() -> None:
    repo, prov, saver = _FakeTeamRunRepo(), _RecordingProvenance(), _Saver()
    svc = _build(repo, prov, team_draft_saver=saver, team_draft_save_timeout=5.0)

    row = await _drive(svc, _compile_manifest())

    assert row.state == "SUCCEEDED"
    assert len(saver.calls) == 1
    call = saver.calls[0]
    assert call["org"] == _ORG  # tenancy: the run's own organisation
    assert call["run_row"].id == row.id
    assert call["user_id"] == _USER


# ── (2) only a run that is STILL SUCCEEDED after verdict-consumption saves ────────────────────


async def test_a_failed_compile_run_is_not_saved() -> None:
    repo, prov, saver = _FakeTeamRunRepo(), _RecordingProvenance(), _Saver()
    svc = _build(
        repo,
        prov,
        harness=_Harness(failing=frozenset({"reviewer"})),
        team_draft_saver=saver,
        team_draft_save_timeout=5.0,
    )

    row = await _drive(svc, _compile_manifest())

    assert row.state == "FAILED"
    assert saver.calls == []


async def test_a_compile_run_escalated_to_paused_is_not_saved() -> None:
    repo, prov, saver = _FakeTeamRunRepo(), _RecordingProvenance(), _Saver()
    svc = _build(
        repo, prov, evaluate=_Evaluate(), team_draft_saver=saver, team_draft_save_timeout=5.0
    )

    row = await _drive(svc, _gated(_compile_manifest(), "CRITICAL"))

    assert row.state == "PAUSED"  # ESCALATE: a human decides, nothing is saved yet
    assert saver.calls == []


async def test_a_compile_run_re_tasked_back_to_queued_is_not_saved() -> None:
    repo, prov, saver = _FakeTeamRunRepo(), _RecordingProvenance(), _Saver()
    svc = _build(
        repo, prov, evaluate=_Evaluate(), team_draft_saver=saver, team_draft_save_timeout=5.0
    )

    row = await _drive(svc, _gated(_compile_manifest(), "MAJOR"))

    assert row.state == "QUEUED"  # RE_TASK: the run is not settled yet
    assert saver.calls == []


# ── (3) an ordinary user team is never saved as a draft ───────────────────────────────────────


async def test_an_ordinary_team_that_succeeds_is_not_saved() -> None:
    repo, prov, saver = _FakeTeamRunRepo(), _RecordingProvenance(), _Saver()
    svc = _build(repo, prov, team_draft_saver=saver, team_draft_save_timeout=5.0)

    row = await _drive(svc, _ordinary_manifest())

    assert row.state == "SUCCEEDED"
    assert saver.calls == []


# ── (4) a raising saver never fails the run, never raises out of _drive ───────────────────────


async def test_a_raising_saver_leaves_the_run_succeeded_and_the_finish_emitted() -> None:
    repo, prov, saver = _FakeTeamRunRepo(), _RecordingProvenance(), _Saver(raises=RuntimeError("x"))
    svc = _build(repo, prov, team_draft_saver=saver, team_draft_save_timeout=5.0)

    row = await _drive(svc, _compile_manifest())  # must not raise

    assert len(saver.calls) == 1  # it was attempted
    assert row.state == "SUCCEEDED"
    assert repo.rows[row.id].state == "SUCCEEDED"
    finishes = [e for e in prov.events if e.action == "engine.team_run.finish"]
    assert [f.outcome for f in finishes] == ["SUCCEEDED"]


# ── (5) a hanging saver is bounded by the timeout ─────────────────────────────────────────────


async def test_a_hanging_saver_times_out_without_hanging_or_flipping_the_run() -> None:
    repo, prov, saver = _FakeTeamRunRepo(), _RecordingProvenance(), _Saver(hangs=True)
    svc = _build(repo, prov, team_draft_saver=saver, team_draft_save_timeout=_FAST)

    # the outer guard turns a design that never bounds the save into a failure, not a stuck suite
    row = await asyncio.wait_for(_drive(svc, _compile_manifest()), timeout=10)

    assert len(saver.calls) == 1
    assert row.state == "SUCCEEDED"
    assert repo.rows[row.id].state == "SUCCEEDED"


# ── (6) no saver attached: today's behaviour, unchanged ───────────────────────────────────────


async def test_no_saver_attached_leaves_a_compile_run_as_it_is_today() -> None:
    repo, prov = _FakeTeamRunRepo(), _RecordingProvenance()
    svc = _build(repo, prov, team_draft_saver=None)  # the collaborator is optional

    row = await _drive(svc, _compile_manifest())

    assert row.state == "SUCCEEDED"
    finishes = [e for e in prov.events if e.action == "engine.team_run.finish"]
    assert [f.outcome for f in finishes] == ["SUCCEEDED"]


# ── (7) the settle-time draft name ────────────────────────────────────────────────────────────


async def test_the_settle_time_draft_name_is_compiled_plus_eight_hex_of_the_run_id() -> None:
    repo, prov, saver = _FakeTeamRunRepo(), _RecordingProvenance(), _Saver()
    svc = _build(repo, prov, team_draft_saver=saver, team_draft_save_timeout=5.0)

    row = await _drive(svc, _compile_manifest())

    assert len(saver.calls) == 1
    name = saver.calls[0]["name"]
    assert re.fullmatch(r"compiled-[0-9a-f]{8}", name)
    assert name == f"compiled-{row.id.hex[:8]}"
