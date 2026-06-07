"""TaskService — the task board + complete (drives the harness, flips the engine job) (fakes)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_execution_engine_service.models.enums import EngineJobState as S
from oraclous_execution_engine_service.models.job import EngineJob
from oraclous_execution_engine_service.services.harness_client import HarnessClientError
from oraclous_execution_engine_service.services.task_service import TaskError, TaskService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineJob] = {}
        self.transition_applies = True  # set False to simulate a concurrent move (CAS no-op)

    def add(self, *, state: str, assignment_id: uuid.UUID | None) -> EngineJob:
        row = EngineJob(
            id=uuid.uuid4(),
            organisation_id=_ORG,
            user_id=_USER,
            state=state,
            assignment_id=assignment_id,
            input_text="go",
            progress=0,
            retry_count=0,
            max_retries=0,
        )
        self.rows[row.id] = row
        return row

    async def get(self, job_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineJob | None:
        row = self.rows.get(job_id)
        return row if row and row.organisation_id == organisation_id else None

    async def list_for_org(self, organisation_id: uuid.UUID, *, state=None, limit: int = 50):  # noqa: ANN001, ANN202
        return [
            r
            for r in self.rows.values()
            if r.organisation_id == organisation_id and (state is None or r.state == state)
        ]

    async def transition(self, job_id, organisation_id, *, new_state, allowed_from, **fields):  # noqa: ANN001, ANN002, ANN003, ANN202
        row = self.rows[job_id]
        if not self.transition_applies or row.state not in allowed_from:
            return row, False
        row.state = new_state
        for k, v in fields.items():
            setattr(row, k, v)
        return row, True


class _FakeHarness:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple[uuid.UUID, str]] = []
        self._raises = raises

    async def complete_assignment(self, assignment_id: uuid.UUID, output: str) -> dict:
        if self._raises:
            raise HarnessClientError("assignment already completed")
        self.calls.append((assignment_id, output))
        return {"status": "COMPLETED"}


class _FakeProv:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def emit(self, record) -> None:  # noqa: ANN001
        self.events.append((record.action, record.outcome))


def _svc(harness: _FakeHarness) -> tuple[TaskService, _FakeRepo, _FakeProv]:
    repo, prov = _FakeRepo(), _FakeProv()
    return TaskService(jobs=repo, harness=harness, provenance=prov), repo, prov  # type: ignore[arg-type]


async def test_list_tasks_returns_only_escalated() -> None:
    svc, repo, _ = _svc(_FakeHarness())
    repo.add(state=S.ESCALATED.value, assignment_id=uuid.uuid4())
    repo.add(state=S.SUCCEEDED.value, assignment_id=None)
    tasks = await svc.list_tasks(_principal())
    assert len(tasks) == 1 and tasks[0].state == S.ESCALATED.value


async def test_complete_drives_harness_and_flips_job() -> None:
    harness = _FakeHarness()
    svc, repo, prov = _svc(harness)
    aid = uuid.uuid4()
    job = repo.add(state=S.ESCALATED.value, assignment_id=aid)
    out = await svc.complete(job.id, _principal(), output="approved")
    assert harness.calls == [(aid, "approved")]
    assert out.state == S.SUCCEEDED.value and out.output == "approved"
    assert ("engine.task.complete", "SUCCEEDED") in prov.events


async def test_complete_not_found_raises() -> None:
    svc, _, _ = _svc(_FakeHarness())
    with pytest.raises(TaskError):
        await svc.complete(uuid.uuid4(), _principal(), output="x")


async def test_complete_non_escalated_job_raises() -> None:
    svc, repo, _ = _svc(_FakeHarness())
    job = repo.add(state=S.RUNNING.value, assignment_id=None)
    with pytest.raises(TaskError):
        await svc.complete(job.id, _principal(), output="x")


async def test_complete_harness_rejection_raises() -> None:
    harness = _FakeHarness(raises=True)
    svc, repo, _ = _svc(harness)
    job = repo.add(state=S.ESCALATED.value, assignment_id=uuid.uuid4())
    with pytest.raises(TaskError):
        await svc.complete(job.id, _principal(), output="x")
    assert repo.rows[job.id].state == S.ESCALATED.value  # not flipped if the harness rejected


async def test_complete_cas_noop_raises_without_false_success() -> None:
    # the harness completed, but our engine job moved under us (concurrent cancel) → the CAS no-ops.
    # We must surface the split, not emit a fake SUCCEEDED.
    harness = _FakeHarness()
    svc, repo, prov = _svc(harness)
    repo.transition_applies = False
    job = repo.add(state=S.ESCALATED.value, assignment_id=uuid.uuid4())
    with pytest.raises(TaskError):
        await svc.complete(job.id, _principal(), output="x")
    assert harness.calls  # the harness WAS driven (the genuine split)
    assert ("engine.task.complete", "SUCCEEDED") not in prov.events  # no false success provenance
