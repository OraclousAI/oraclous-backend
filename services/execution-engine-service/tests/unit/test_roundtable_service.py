"""RoundtableService — create/drive/respond turn coordination (fakes, no I/O)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_execution_engine_service.models.roundtable import EngineRoundtable
from oraclous_execution_engine_service.services.harness_client import HarnessClientError
from oraclous_execution_engine_service.services.roundtable_service import (
    RoundtableError,
    RoundtableService,
)
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_AGENT = {"role": "analyst", "kind": "agent", "manifest": {"ohm_version": "1.0"}}
_HUMAN = {"role": "reviewer", "kind": "human", "prompt": "approve?"}


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineRoundtable] = {}

    async def create(self, **kw: object) -> EngineRoundtable:
        row = EngineRoundtable(id=uuid.uuid4(), current_turn=0, state="QUEUED", transcript=[], **kw)
        self.rows[row.id] = row
        return row

    async def get(self, rt_id: uuid.UUID, org: uuid.UUID) -> EngineRoundtable | None:
        row = self.rows.get(rt_id)
        return row if row and row.organisation_id == org else None

    async def update(self, rt_id: uuid.UUID, org: uuid.UUID, **fields: object) -> EngineRoundtable:
        row = self.rows[rt_id]
        for k, v in fields.items():
            setattr(row, k, v)
        return row


class _FakeHarness:
    def __init__(self, *, status: str = "SUCCEEDED") -> None:
        self.calls: list[str] = []
        self._status = status

    async def execute(self, *, input_text, manifest_inline=None, manifest_ref=None) -> dict:  # noqa: ANN001
        self.calls.append(input_text)
        return {"status": self._status, "output": f"agent-{len(self.calls)}", "error_message": "x"}


class _FakeProv:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def emit(self, record) -> None:  # noqa: ANN001
        self.events.append(record.action)


def _request_svc(harness=None):  # noqa: ANN001, ANN202
    repo, prov, calls = _FakeRepo(), _FakeProv(), []
    svc = RoundtableService(
        roundtables=repo,
        provenance=prov,
        harness=harness,
        enqueue=lambda r, o, u: calls.append(r),
    )
    return svc, repo, prov, calls


# ── create ────────────────────────────────────────────────────────────────────────────────────────
async def test_create_records_and_enqueues() -> None:
    svc, repo, prov, calls = _request_svc()
    rt = await svc.create(_principal(), topic="t", actors=[_AGENT], max_rounds=1)
    assert rt.state == "QUEUED" and calls == [rt.id]
    assert "engine.roundtable.create" in prov.events


async def test_create_no_actors_raises() -> None:
    svc, *_ = _request_svc()
    with pytest.raises(RoundtableError):
        await svc.create(_principal(), topic="t", actors=[], max_rounds=1)


async def test_create_agent_without_manifest_raises() -> None:
    svc, *_ = _request_svc()
    with pytest.raises(RoundtableError):
        await svc.create(
            _principal(), topic="t", actors=[{"role": "a", "kind": "agent"}], max_rounds=1
        )


# ── drive ─────────────────────────────────────────────────────────────────────────────────────────
async def test_drive_all_agent_completes() -> None:
    harness = _FakeHarness()
    svc, repo, prov, _ = _request_svc(harness)
    rt = await repo.create(
        organisation_id=_ORG, user_id=_USER, topic="t", actors=[_AGENT], max_rounds=2
    )
    out = await svc.drive(rt.id, _principal())
    assert out.state == "SUCCEEDED"
    assert len(out.transcript) == 2 and out.final_output == "agent-2"  # 2 rounds × 1 actor
    assert len(harness.calls) == 2
    assert "engine.roundtable.complete" in prov.events


async def test_drive_pauses_at_human_turn() -> None:
    harness = _FakeHarness()
    svc, repo, _, _ = _request_svc(harness)
    rt = await repo.create(
        organisation_id=_ORG, user_id=_USER, topic="t", actors=[_AGENT, _HUMAN], max_rounds=1
    )
    out = await svc.drive(rt.id, _principal())
    assert out.state == "ESCALATED" and out.current_turn == 1  # ran the agent, paused at the human
    assert len(out.transcript) == 1 and out.transcript[0]["kind"] == "agent"


async def test_drive_agent_failure_fails_the_roundtable() -> None:
    svc, repo, _, _ = _request_svc(_FakeHarness(status="FAILED"))
    rt = await repo.create(
        organisation_id=_ORG, user_id=_USER, topic="t", actors=[_AGENT], max_rounds=1
    )
    out = await svc.drive(rt.id, _principal())
    assert out.state == "FAILED" and out.error_message


async def test_drive_harness_unreachable_fails() -> None:
    class _Down:
        async def execute(self, **_kw):  # noqa: ANN003, ANN202
            raise HarnessClientError("down")

    svc, repo, _, _ = _request_svc(_Down())
    rt = await repo.create(
        organisation_id=_ORG, user_id=_USER, topic="t", actors=[_AGENT], max_rounds=1
    )
    out = await svc.drive(rt.id, _principal())
    assert out.state == "FAILED"


# ── respond ─────────────────────────────────────────────────────────────────────────────────────
async def test_respond_appends_human_turn_and_reenqueues() -> None:
    harness = _FakeHarness()
    svc, repo, prov, calls = _request_svc(harness)
    rt = await repo.create(
        organisation_id=_ORG, user_id=_USER, topic="t", actors=[_AGENT, _HUMAN], max_rounds=1
    )
    await svc.drive(rt.id, _principal())  # → ESCALATED at the human turn (turn 1)
    out = await svc.respond(rt.id, _principal(), "looks good")
    assert out.state == "QUEUED" and out.current_turn == 2  # advanced past the human turn
    assert out.transcript[-1] == {
        "turn": 1,
        "role": "reviewer",
        "kind": "human",
        "output": "looks good",
    }
    assert rt.id in calls  # re-enqueued to continue driving


async def test_respond_when_not_escalated_raises() -> None:
    svc, repo, _, _ = _request_svc(_FakeHarness())
    rt = await repo.create(
        organisation_id=_ORG, user_id=_USER, topic="t", actors=[_HUMAN], max_rounds=1
    )  # state QUEUED, not ESCALATED
    with pytest.raises(RoundtableError):
        await svc.respond(rt.id, _principal(), "x")


async def test_no_org_scope_raises() -> None:
    svc, *_ = _request_svc()
    with pytest.raises(RoundtableError):
        await svc.create(_principal(org=None), topic="t", actors=[_AGENT], max_rounds=1)
