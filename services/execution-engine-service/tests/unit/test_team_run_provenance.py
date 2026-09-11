"""#826 — the flagship runtime emits nothing. Team-run lifecycle + judge provenance (§3.7).

Ruled by parhamdavari, 24 August 2026 (solution-architect) and 11 September 2026 (CTO), on #826:
``TeamRunService`` has no ``provenance`` field at all, unlike ``JobService``/``TaskService``/
``RoundtableService``/``ScheduleService`` — a team run appears in neither ``/v1/engine/activity``
nor ``/v1/engine/usage``. This pins four lifecycle emission points (claim, dispatch, member-settle,
terminal) plus the LLM-judge gate (``_grade_gate``), through the substrate ``ProvenanceCollector``.

RED until ``provenance`` is wired: today ``TeamRunService.__init__`` accepts no such kwarg at all,
so every test below either TypeErrors on construction or finds the fake collector never called.
``TeamRunService`` and ``ProvenanceRecord`` both exist today — what's missing is the wiring, which
surfaces as a runtime TypeError/AssertionError, not a collection-time ImportError. The one seam that
IS not-yet-built is ``hash_payload`` (the other worker's #826 slice, ``packages/substrate``); it is
imported function-locally, per ``.claude/rules/tests-seam-imports.md``, so a missing seam hard-fails
at runtime rather than aborting collection for the whole run.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


def _expected_hash(payload: Any) -> str | None:
    """The REAL substrate helper (24 August ruling §3), function-local — not yet built."""
    from oraclous_substrate.provenance import hash_payload  # seam import: not-yet-built

    return hash_payload(payload)


class FakeTeamRunRepo:
    """In-memory mirror of TeamRunRepository, including the #819 ``checkpoint`` write (copied from
    ``test_team_run_checkpoint.py`` — the same shape this suite's ``_on_dispatch``/``_checkpoint``
    hooks write through)."""

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


class ScriptedHarness:
    """Every member SUCCEEDS deterministically; a subclass can override ``execute`` to fail one."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, *, input_text: str, manifest_ref: str | None = None, **kw: Any) -> dict:
        role = (manifest_ref or input_text).split("/")[-1].split("@")[0]
        self.calls.append(role)
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": f"{role}-out",
            "total_tokens": 100,
        }


class _FakeProvenance:
    """Records every emitted record; ``raise_on`` scripts a sink failure for one action, so the
    fail-closed contract (job_service.py:99-112) can be pinned against a team-run emit too."""

    def __init__(self, *, raise_on: set[str] | None = None) -> None:
        self.events: list[Any] = []
        self._raise_on = raise_on or set()

    async def emit(self, record: Any) -> None:
        if record.action in self._raise_on:
            raise RuntimeError(f"provenance sink unavailable for {record.action}")
        self.events.append(record)


class FakeEvaluate:
    """A stand-in EvaluateClient (#477), copied from test_team_run_service.py's own fake."""

    def __init__(
        self, *, score: float = 0.8, passed: bool = True, raise_exc: Exception | None = None
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._score, self._passed, self._raise = score, passed, raise_exc

    async def evaluate(
        self,
        *,
        target_ref: str,
        target_output: str,
        success_criteria: str,
        target_kind: str = "run",
        pass_threshold: float = 0.7,
        judge_credential_id: str | None = None,
        judge_model: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"target_ref": target_ref})
        if self._raise is not None:
            raise self._raise
        return {
            "pass": self._passed,
            "score": self._score,
            "recommended_action": "accept" if self._passed else "escalate_human",
        }

    async def aclose(self) -> None:  # pragma: no cover - parity with the real client
        return None


def _agent(role: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
    }


def _team(members: list[dict[str, Any]], *, success_criteria: str | None = None) -> dict[str, Any]:
    team: dict[str, Any] = {
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
    if success_criteria is not None:
        team["orchestration"] = {"success_criteria": success_criteria}
    return team


def _svc(
    repo: FakeTeamRunRepo, harness: Any, *, provenance: Any, evaluate: Any = None
) -> tuple[TeamRunService, list[uuid.UUID]]:
    enqueued: list[uuid.UUID] = []
    svc = TeamRunService(
        team_runs=repo,
        harness=harness,
        evaluate=evaluate,
        provenance=provenance,  # not-yet-built kwarg — TypeErrors until #826 lands
        enqueue=lambda rid, _org, _user: enqueued.append(rid),
    )
    return svc, enqueued


async def _run(svc: TeamRunService, principal: Principal, **kwargs: Any) -> EngineTeamRun:
    row = await svc.create(principal, **kwargs)
    return await svc.drive(row.id, principal)


# ── the constructor gains a NON-optional provenance kwarg ───────────────────────────────────────


def test_team_run_service_requires_provenance_kwarg() -> None:
    """Unlike JobService/TaskService/RoundtableService/ScheduleService, TeamRunService takes no
    ``provenance`` at all today — this must become mandatory, not merely accepted."""
    with pytest.raises(TypeError):
        TeamRunService(team_runs=FakeTeamRunRepo(), harness=ScriptedHarness())  # type: ignore[call-arg]


# ── lifecycle point 1: run claimed QUEUED -> RUNNING (team_run_service.py:2098-2107) ─────────────


async def test_run_claimed_running_emits_start() -> None:
    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    svc, _ = _svc(repo, ScriptedHarness(), provenance=prov)
    row = await _run(
        svc, _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )

    starts = [e for e in prov.events if e.action == "engine.team_run.start"]
    assert len(starts) == 1, prov.events
    assert starts[0].resource == f"engine_team_run:{row.id}"
    assert starts[0].outcome == "RUNNING"


async def test_a_start_emit_failure_fails_the_row_and_reraises() -> None:
    """The 24 August ruling does not settle the emit-failure contract for team-run start; this pins
    the existing job_service.py:99-112 posture instead (noted in the test-author report): a
    provenance emit that fails must fail the row CLOSED — never leave it stuck RUNNING as a
    phantom claim with no audit trail — and propagate, rather than silently continue the drive."""
    repo = FakeTeamRunRepo()
    prov = _FakeProvenance(raise_on={"engine.team_run.start"})
    svc, _ = _svc(repo, ScriptedHarness(), provenance=prov)
    row = await svc.create(
        _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )

    with pytest.raises(RuntimeError):
        await svc.drive(row.id, _principal())

    assert repo.rows[row.id].state == "FAILED"  # never stuck RUNNING


async def test_a_failed_dispatch_audit_write_does_not_fail_a_healthy_run() -> None:
    """BLOCKING fix (review on #1027): ``_on_dispatch`` is a best-effort hook the orchestrator
    (``oraclous_ohm.orchestrate``) invokes inside ``contextlib.suppress(Exception)``. Before this
    fix, its dispatch emit went through the fail-closed ``_emit`` — so a transient sink error there
    CAS-ed the row RUNNING -> FAILED mid-drive, the orchestrator silently swallowed the re-raise and
    kept driving to a real success, and the later terminal ``transition(...)`` call became a no-op
    (CAS mismatch: the row was already FAILED, not RUNNING) — discarding the drive's real results.
    A healthy run must still reach SUCCEEDED with its real member output intact."""
    repo = FakeTeamRunRepo()
    prov = _FakeProvenance(raise_on={"engine.team_run.dispatch"})
    svc, _ = _svc(repo, ScriptedHarness(), provenance=prov)
    row = await _run(
        svc, _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )

    assert row.state == "SUCCEEDED"  # NOT "FAILED" — the crux of the bug
    assert row.results["a"]["output"] == "a-out"  # the real member output, not blanked

    settled = [e for e in prov.events if e.action == "engine.team_run.member"]
    assert len(settled) == 1, prov.events  # the settle checkpoint event still fired
    finishes = [e for e in prov.events if e.action == "engine.team_run.finish"]
    assert len(finishes) == 1 and finishes[0].outcome == "SUCCEEDED", prov.events


# ── lifecycle point 2: member dispatch admitted (_on_dispatch, :2143-2154) ───────────────────────


async def test_member_dispatch_emits_with_member_context() -> None:
    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    svc, _ = _svc(repo, ScriptedHarness(), provenance=prov)
    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    dispatches = [e for e in prov.events if e.action == "engine.team_run.dispatch"]
    # 24 August ruling §3: extra context concatenated into outcome as a string STOPS — the member
    # identity lives ONLY in context["member"], structured; outcome carries the status alone.
    assert {e.outcome for e in dispatches} == {"dispatched"}
    assert all(e.resource == f"engine_team_run:{row.id}" for e in dispatches)
    assert {e.context["member"] for e in dispatches} == {"a", "b"}, dispatches


# ── lifecycle point 3: member settled (_checkpoint, :2195-2235) ─────────────────────────────────


async def test_member_settled_emits_with_output_hash() -> None:
    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    svc, _ = _svc(repo, ScriptedHarness(), provenance=prov)
    row = await _run(
        svc, _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )

    settled = [e for e in prov.events if e.action == "engine.team_run.member"]
    assert len(settled) == 1, prov.events
    assert settled[0].resource == f"engine_team_run:{row.id}"
    # 24 August ruling §3, applied the same way as the dispatch event above: the member's identity
    # is NEVER recoverable by string-splitting outcome — it lives structured in context["member"].
    assert settled[0].outcome == "succeeded"
    assert settled[0].context["member"] == "a", settled[0].context
    assert settled[0].output_hash == _expected_hash(row.results["a"])


# ── lifecycle point 4: run terminal (:2379-2395 success, :2293-2306 fail-closed) ─────────────────


async def test_run_terminal_success_emits_finish() -> None:
    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    svc, _ = _svc(repo, ScriptedHarness(), provenance=prov)
    row = await _run(
        svc, _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )
    assert row.state == "SUCCEEDED"

    finishes = [e for e in prov.events if e.action == "engine.team_run.finish"]
    assert len(finishes) == 1, prov.events
    assert finishes[0].resource == f"engine_team_run:{row.id}"
    assert finishes[0].outcome == "SUCCEEDED"


async def test_run_terminal_failure_emits_finish() -> None:
    """A mid-drive exception (harness/decode/bug) fails the row at :2293-2306 — the finish emission
    must fire on this path too, not only the happy one."""

    class _Boom(ScriptedHarness):
        async def execute(self, **kw: Any) -> dict:
            raise RuntimeError("harness unreachable")

    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    svc, _ = _svc(repo, _Boom(), provenance=prov)
    row = await _run(
        svc, _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )
    assert row.state == "FAILED"

    finishes = [e for e in prov.events if e.action == "engine.team_run.finish"]
    assert len(finishes) == 1, prov.events
    assert finishes[0].resource == f"engine_team_run:{row.id}"
    assert finishes[0].outcome == "FAILED"


# ── the LLM-judge gate (_grade_gate, ~L1726-1805) ────────────────────────────────────────────────


async def test_grade_gate_emits_llm_judge_on_a_real_grade() -> None:
    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    evaluate = FakeEvaluate(score=0.9, passed=True)
    svc, _ = _svc(repo, ScriptedHarness(), provenance=prov, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is good")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert row.state == "SUCCEEDED" and row.verdict["pass"] is True

    judged = [e for e in prov.events if e.action == "llm.judge"]
    assert len(judged) == 1, prov.events
    assert judged[0].resource == f"engine_team_run:{row.id}"
    assert judged[0].outcome == "pass"  # carries the verdict, per the issue text


async def test_grade_gate_below_threshold_emits_fail() -> None:
    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    evaluate = FakeEvaluate(score=0.1, passed=False)
    svc, _ = _svc(repo, ScriptedHarness(), provenance=prov, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is good")
    await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})

    judged = [e for e in prov.events if e.action == "llm.judge"]
    assert len(judged) == 1, prov.events
    assert judged[0].outcome == "fail"


class _LoopHarness:
    """A tool-less loop harness: a coordinator call (``manifest_inline`` present, named
    "loop-coordinator") routes to every declared member not yet produced — tracked directly off
    this harness's own dispatch outcomes, never by parsing the coordinator's rendered prompt. A
    normal member dispatch (``manifest_ref`` set, no ``manifest_inline``) raises on a role's FIRST
    attempt if that role is in ``fail_first``; every attempt after succeeds."""

    def __init__(self, members: list[str], fail_first: set[str] | None = None) -> None:
        self._members = members
        self._fail_first = fail_first or set()
        self._attempts: dict[str, int] = dict.fromkeys(members, 0)
        self._produced: dict[str, bool] = dict.fromkeys(members, False)

    async def execute(
        self,
        *,
        input_text: str,
        manifest_ref: str | None = None,
        manifest_inline: dict[str, Any] | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        if (
            manifest_inline is not None
            and manifest_inline.get("metadata", {}).get("name") == "loop-coordinator"
        ):
            next_roles = [r for r in self._members if not self._produced[r]]
            return {
                "id": str(uuid.uuid4()),
                "status": "SUCCEEDED",
                "output": " ".join(next_roles) or "DONE",
                "total_tokens": 10,
            }
        role = (manifest_ref or input_text).split("/")[-1].split("@")[0]
        self._attempts[role] += 1
        if role in self._fail_first and self._attempts[role] == 1:
            raise RuntimeError("boom")
        self._produced[role] = True
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": f"{role}-out",
            "total_tokens": 100,
        }


def _loop_team(members: list[dict[str, Any]], loop_members: list[str]) -> dict[str, Any]:
    """A team whose declared members form ONE loop (ADR-043 #552) — the only topology in which a
    role can dispatch/settle more than once within a single ``drive()``."""
    team = _team(members)
    team["orchestration"] = {
        "loops": [{"members": loop_members, "routing": dict.fromkeys(loop_members, "keep trying")}],
        "termination": {"max_rounds": 5},
    }
    return team


# ── BLOCKING fix, review on #1027: settle-emit dedup keys on (role, status, output_hash) ────────


async def test_a_loop_members_genuine_re_settle_is_recorded_not_suppressed() -> None:
    """A role-only dedup (``emitted_members: set[str]``) would suppress role "a"'s SECOND settle
    (succeeded) because "a" was already recorded once (failed) on an earlier checkpoint — a loop's
    conductor can re-route a previously-failed member, so the same role genuinely re-settles within
    one drive. The fix keys on (role, status, output_hash): a new status/output is always recorded.
    """
    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    harness = _LoopHarness(members=["a"], fail_first={"a"})
    svc, _ = _svc(repo, harness, provenance=prov)
    row = await _run(
        svc,
        _principal(),
        manifest=_loop_team([_agent("a")], loop_members=["a"]),
        sub_harnesses={},
        gate_decisions={},
    )

    assert row.state == "SUCCEEDED", (row.state, row.results, row.member_status)
    assert row.results["a"]["output"] == "a-out"

    settled_a = [
        e
        for e in prov.events
        if e.action == "engine.team_run.member" and e.context["member"] == "a"
    ]
    assert len(settled_a) == 2, prov.events  # the genuine re-settle IS recorded, not suppressed
    assert {e.outcome for e in settled_a} == {"failed", "succeeded"}, settled_a


async def test_an_unchanged_repeat_settle_is_recorded_only_once() -> None:
    """The other half of the ruling: role "a" converges on round 1 and never changes again, but the
    checkpoint fires again on round 2 (the loop's snapshot is cumulative, so "a" carries forward
    unchanged) while role "b" transitions failed -> succeeded. "a"'s settle count must stay at 1
    across both rounds; "b" genuinely re-settles, so its count goes to 2."""
    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    harness = _LoopHarness(members=["a", "b"], fail_first={"b"})
    svc, _ = _svc(repo, harness, provenance=prov)
    row = await _run(
        svc,
        _principal(),
        manifest=_loop_team([_agent("a"), _agent("b")], loop_members=["a", "b"]),
        sub_harnesses={},
        gate_decisions={},
    )

    assert row.state == "SUCCEEDED", (row.state, row.results, row.member_status)

    def _settled(role: str) -> list[Any]:
        return [
            e
            for e in prov.events
            if e.action == "engine.team_run.member" and e.context["member"] == role
        ]

    assert len(_settled("a")) == 1, prov.events  # unchanged repeat stays suppressed
    assert {e.outcome for e in _settled("a")} == {"succeeded"}
    assert len(_settled("b")) == 2, prov.events  # genuine re-settle IS recorded
    assert {e.outcome for e in _settled("b")} == {"failed", "succeeded"}


async def test_grade_gate_fail_closed_path_also_emits_and_says_the_judge_failed() -> None:
    """:1786-1805 — the judge call itself errors and the run still SUCCEEDS (fail-closed). This
    path must ALSO emit, distinguishably from a genuine below-threshold ``fail`` verdict — a caller
    reading ``/v1/engine/activity`` needs to tell 'the judge ran and failed the run' apart from
    'the judge never ran'."""
    repo, prov = FakeTeamRunRepo(), _FakeProvenance()
    evaluate = FakeEvaluate(raise_exc=RuntimeError("judge unreachable"))
    svc, _ = _svc(repo, ScriptedHarness(), provenance=prov, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is good")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert row.state == "SUCCEEDED"  # the grader outage never strands the run (existing contract)

    judged = [e for e in prov.events if e.action == "llm.judge"]
    assert len(judged) == 1, prov.events
    assert judged[0].outcome == "grader_unavailable"  # never plain "fail" — the judge never ran
