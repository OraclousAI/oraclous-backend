"""JobService spine — submit runs the harness + checkpoints terminal state (fakes, no I/O)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_execution_engine_service.models.enums import EngineJobState as S
from oraclous_execution_engine_service.models.job import EngineJob
from oraclous_execution_engine_service.services.harness_client import HarnessClientError
from oraclous_execution_engine_service.services.job_service import JobError, JobService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineJob] = {}

    async def create(self, **kw: object) -> EngineJob:
        row = EngineJob(id=uuid.uuid4(), state=S.QUEUED.value, progress=0, retry_count=0, **kw)
        self.rows[row.id] = row
        return row

    async def get(self, job_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineJob | None:
        row = self.rows.get(job_id)
        return row if row and row.organisation_id == organisation_id else None

    async def update(self, job_id: uuid.UUID, organisation_id: uuid.UUID, **fields: object):  # noqa: ANN201
        row = self.rows[job_id]
        for k, v in fields.items():
            setattr(row, k, v)
        return row


class _FakeHarness:
    def __init__(self, *, result: dict | None = None, raises: bool = False) -> None:
        self._result = result or {}
        self._raises = raises

    async def execute(self, **_kw: object) -> dict:
        if self._raises:
            raise HarnessClientError("unreachable")
        return self._result


class _FakeProvenance:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def emit(self, record) -> None:  # noqa: ANN001
        self.events.append((record.action, record.outcome))


def _svc(harness: _FakeHarness) -> tuple[JobService, _FakeRepo, _FakeProvenance]:
    repo, prov = _FakeRepo(), _FakeProvenance()
    return JobService(jobs=repo, harness=harness, provenance=prov), repo, prov  # type: ignore[arg-type]


async def test_submit_succeeds() -> None:
    hx_id = uuid.uuid4()
    svc, _, prov = _svc(
        _FakeHarness(result={"id": str(hx_id), "status": "SUCCEEDED", "output": "ok"})
    )
    job = await svc.submit(principal=_principal(), input_text="go", manifest_inline={"x": 1})
    assert job.state == S.SUCCEEDED.value
    assert job.harness_execution_id == hx_id
    assert job.output == "ok" and job.progress == 100
    assert ("engine.job.submit", "QUEUED") in prov.events
    assert ("engine.job.run", "SUCCEEDED") in prov.events


async def test_harness_unreachable_marks_failed() -> None:
    svc, _, _ = _svc(_FakeHarness(raises=True))
    job = await svc.submit(principal=_principal(), input_text="go", manifest_inline={})
    assert job.state == S.FAILED.value
    assert job.error_type == "harness_unreachable"


async def test_human_escalation_captures_assignment() -> None:
    assignment_id = uuid.uuid4()
    result = {
        "id": str(uuid.uuid4()),
        "status": "ESCALATED",
        "error_type": "human_assignment",
        "steps": [{"kind": "gate", "status": "assigned", "detail": str(assignment_id)}],
    }
    svc, _, _ = _svc(_FakeHarness(result=result))
    job = await svc.submit(principal=_principal(), input_text="go", manifest_inline={})
    assert job.state == S.ESCALATED.value
    assert job.assignment_id == assignment_id


async def test_no_org_scope_raises() -> None:
    svc, _, _ = _svc(_FakeHarness(result={"status": "SUCCEEDED"}))
    with pytest.raises(JobError):
        await svc.submit(principal=_principal(org=None), input_text="go", manifest_inline={})
