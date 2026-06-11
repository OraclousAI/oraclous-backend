"""HarnessExecutionService.resume — the guards + the DENIED path (fakes; APPROVED is smoke-tested).

The APPROVED branch re-runs the loop (registry/materialise/LLM) and is covered end-to-end by the
smoke; here we pin the fail-closed guards and the DENIED termination, which touch only the execution
+ checkpoint repos.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_harness_runtime_service.domain.ohm.signatures import TrustStore
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
    ResumeError,
)

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


def _execution(*, status: str = "ESCALATED", error_type: str | None = "hitl_required"):  # noqa: ANN202
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        status=status,
        error_type=error_type,
        output="partial",
        iterations=2,
        total_tokens=10,
        steps=[{"index": 0, "kind": "gate", "name": "x", "status": "hitl_required", "detail": "d"}],
        input="go",
        harness_name="Demo",
    )


class _FakeExecutions:
    def __init__(self, row=None) -> None:  # noqa: ANN001
        self._row = row
        self.updated: dict | None = None

    async def get(self, execution_id, organisation_id):  # noqa: ANN001, ANN202
        return self._row if self._row and self._row.organisation_id == organisation_id else None

    async def update_run(self, execution_id, organisation_id, **fields):  # noqa: ANN001, ANN003, ANN202
        self.updated = fields
        return SimpleNamespace(id=execution_id, **fields)


class _FakeCheckpoints:
    def __init__(self, pending=True) -> None:  # noqa: ANN001
        self._pending = pending
        self.decided: str | None = None
        self.reverted = False

    async def get_latest_pending(self, execution_id, organisation_id):  # noqa: ANN001, ANN202
        if not self._pending:
            return None
        # manifest_doc={} is an invalid OHM → load_ohm raises (used by the compensation test).
        return SimpleNamespace(id=uuid.uuid4(), manifest_doc={})

    async def set_decision(self, checkpoint_id, organisation_id, new_status):  # noqa: ANN001, ANN202
        if self.decided is not None:  # already decided → CAS no-op
            return None
        self.decided = new_status
        return SimpleNamespace(id=checkpoint_id, status=new_status)

    async def revert_to_pending(self, checkpoint_id, organisation_id):  # noqa: ANN001, ANN202
        self.reverted = True


class _FakeProv:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def emit(self, record) -> None:  # noqa: ANN001
        self.events.append(record.action)


def _service(executions, checkpoints, prov):  # noqa: ANN001, ANN202
    return HarnessExecutionService(
        registry=None,
        broker=None,
        executions=executions,
        assignments=None,
        checkpoints=checkpoints,
        provenance=prov,
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
    )


async def test_resume_unknown_execution_404() -> None:
    svc = _service(_FakeExecutions(None), _FakeCheckpoints(), _FakeProv())
    with pytest.raises(ResumeError) as exc:
        await svc.resume(execution_id=uuid.uuid4(), principal=_principal(), decision="APPROVED")
    assert exc.value.status_code == 404


async def test_resume_non_hitl_execution_409() -> None:
    row = _execution(status="SUCCEEDED", error_type=None)
    svc = _service(_FakeExecutions(row), _FakeCheckpoints(), _FakeProv())
    with pytest.raises(ResumeError) as exc:
        await svc.resume(execution_id=row.id, principal=_principal(), decision="APPROVED")
    assert exc.value.status_code == 409


async def test_resume_no_pending_checkpoint_409() -> None:
    row = _execution()
    svc = _service(_FakeExecutions(row), _FakeCheckpoints(pending=False), _FakeProv())
    with pytest.raises(ResumeError) as exc:
        await svc.resume(execution_id=row.id, principal=_principal(), decision="APPROVED")
    assert exc.value.status_code == 409


async def test_resume_no_org_scope_401() -> None:
    svc = _service(_FakeExecutions(_execution()), _FakeCheckpoints(), _FakeProv())
    with pytest.raises(ResumeError) as exc:
        await svc.resume(
            execution_id=uuid.uuid4(), principal=_principal(org=None), decision="APPROVED"
        )
    assert exc.value.status_code == 401


async def test_resume_failure_after_cas_reverts_checkpoint_to_pending() -> None:
    # the approve claimed the checkpoint, then applying it failed (here: invalid manifest_doc →
    # load_ohm raises). The checkpoint must be un-claimed so the run is retryable, not stranded.
    row = _execution()
    execs, ckpts, prov = _FakeExecutions(row), _FakeCheckpoints(), _FakeProv()
    svc = _service(execs, ckpts, prov)
    with pytest.raises(Exception):  # noqa: B017, PT011 — load_ohm raises on the invalid doc
        await svc.resume(execution_id=row.id, principal=_principal(), decision="APPROVED")
    assert ckpts.decided == "APPROVED"  # it was claimed
    assert ckpts.reverted is True  # ...then un-claimed on failure (retryable)


async def test_resume_denied_terminates_failed_with_reason() -> None:
    row = _execution()
    execs, ckpts, prov = _FakeExecutions(row), _FakeCheckpoints(), _FakeProv()
    svc = _service(execs, ckpts, prov)
    out = await svc.resume(
        execution_id=row.id, principal=_principal(), decision="DENIED", decision_reason="nope"
    )
    assert out.status == "FAILED" and out.error_type == "human_rejected"
    assert out.error_message == "nope"
    assert ckpts.decided == "DENIED"
    assert "human.reject" in prov.events and "harness.resume" in prov.events
    # the denial appends a GATE step to the existing trace, never re-runs the loop.
    assert execs.updated["steps"][-1]["status"] == "denied"
