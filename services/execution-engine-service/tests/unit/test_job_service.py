"""JobService — submit enqueues; the worker execute() runs + CAS-checkpoints; cancel (fakes)."""

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

    async def transition(
        self,
        job_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        new_state: str,
        allowed_from: frozenset[str],
        **fields: object,
    ) -> tuple[EngineJob | None, bool]:
        row = self.rows.get(job_id)
        if row is None or row.organisation_id != organisation_id:
            return None, False
        if row.state not in allowed_from:
            return row, False
        row.state = new_state
        for k, v in fields.items():
            setattr(row, k, v)
        return row, True


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


def _request_svc() -> tuple[JobService, _FakeRepo, list, _FakeProvenance]:
    repo, prov, calls = _FakeRepo(), _FakeProvenance(), []
    svc = JobService(jobs=repo, provenance=prov, enqueue=lambda j, o, u: calls.append((j, o, u)))  # type: ignore[arg-type]
    return svc, repo, calls, prov


def _worker_svc(repo: _FakeRepo, harness: _FakeHarness) -> tuple[JobService, _FakeProvenance]:
    prov = _FakeProvenance()
    return JobService(jobs=repo, provenance=prov, harness=harness), prov  # type: ignore[arg-type]


async def _queued_job(repo: _FakeRepo) -> EngineJob:
    return await repo.create(
        organisation_id=_ORG, user_id=_USER, input_text="go", manifest_inline={}
    )


# ── submit (request path) ─────────────────────────────────────────────────────────────────────────
async def test_submit_enqueues_and_returns_queued() -> None:
    svc, _, calls, prov = _request_svc()
    job = await svc.submit(principal=_principal(), input_text="go", manifest_inline={"x": 1})
    assert job.state == S.QUEUED.value
    assert calls == [(job.id, _ORG, _USER)]
    assert ("engine.job.submit", "QUEUED") in prov.events


async def test_submit_without_queue_raises() -> None:
    svc = JobService(jobs=_FakeRepo(), provenance=_FakeProvenance())  # type: ignore[arg-type]
    with pytest.raises(JobError):
        await svc.submit(principal=_principal(), input_text="go", manifest_inline={})


async def test_no_org_scope_raises() -> None:
    svc, _, _, _ = _request_svc()
    with pytest.raises(JobError):
        await svc.submit(principal=_principal(org=None), input_text="go", manifest_inline={})


# ── execute (worker path) ─────────────────────────────────────────────────────────────────────────
async def test_execute_succeeds() -> None:
    repo = _FakeRepo()
    job = await _queued_job(repo)
    hx = uuid.uuid4()
    svc, prov = _worker_svc(
        repo, _FakeHarness(result={"id": str(hx), "status": "SUCCEEDED", "output": "ok"})
    )
    out = await svc.execute(job.id, _principal())
    assert out.state == S.SUCCEEDED.value and out.harness_execution_id == hx
    assert out.output == "ok" and out.progress == 100
    assert ("engine.job.run", "SUCCEEDED") in prov.events


async def test_execute_harness_unreachable_marks_failed() -> None:
    repo = _FakeRepo()
    job = await _queued_job(repo)
    svc, _ = _worker_svc(repo, _FakeHarness(raises=True))
    out = await svc.execute(job.id, _principal())
    assert out.state == S.FAILED.value and out.error_type == "harness_unreachable"


async def test_execute_human_escalation_captures_assignment() -> None:
    repo = _FakeRepo()
    job = await _queued_job(repo)
    assignment_id = uuid.uuid4()
    result = {
        "id": str(uuid.uuid4()),
        "status": "ESCALATED",
        "error_type": "human_assignment",
        "steps": [{"kind": "gate", "status": "assigned", "detail": str(assignment_id)}],
    }
    svc, _ = _worker_svc(repo, _FakeHarness(result=result))
    out = await svc.execute(job.id, _principal())
    assert out.state == S.ESCALATED.value and out.assignment_id == assignment_id


async def test_execute_without_harness_raises() -> None:
    repo = _FakeRepo()
    job = await _queued_job(repo)
    svc = JobService(jobs=repo, provenance=_FakeProvenance())  # type: ignore[arg-type]
    with pytest.raises(JobError):
        await svc.execute(job.id, _principal())


# ── cancel + the cancel-races-worker guard ──────────────────────────────────────────────────────
async def test_cancel_queued_job() -> None:
    repo = _FakeRepo()
    job = await _queued_job(repo)
    svc, prov = _worker_svc(repo, _FakeHarness())
    out = await svc.cancel(job.id, _principal())
    assert out.state == S.CANCELLED.value
    assert ("engine.job.cancel", "CANCELLED") in prov.events


async def test_cancel_then_worker_run_is_a_noop() -> None:
    # cancel wins: once CANCELLED, the worker's QUEUED→RUNNING CAS does not apply → it leaves it be.
    repo = _FakeRepo()
    job = await _queued_job(repo)
    canceller, _ = _worker_svc(repo, _FakeHarness())
    await canceller.cancel(job.id, _principal())
    worker, _ = _worker_svc(repo, _FakeHarness(result={"status": "SUCCEEDED"}))
    out = await worker.execute(job.id, _principal())
    assert out.state == S.CANCELLED.value  # not overwritten by the run
