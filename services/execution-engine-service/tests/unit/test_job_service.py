"""JobService — submit enqueues; the worker execute() runs + CAS-checkpoints; cancel (fakes)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_execution_engine_service.models.enums import EngineJobState as S
from oraclous_execution_engine_service.models.job import EngineJob
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClientError,
    HarnessTimeout,
)
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
        kw.setdefault("max_retries", 0)  # the DB default applies at flush; a fake row needs it now
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

    async def list_stale_running(self, older_than: object, *, limit: int = 100) -> list[EngineJob]:
        return [r for r in self.rows.values() if r.state == S.RUNNING.value]


class _FakeHarness:
    def __init__(
        self, *, result: dict | None = None, raises: bool = False, timeout: bool = False
    ) -> None:
        self._result = result or {}
        self._raises = raises
        self._timeout = timeout

    async def execute(self, **_kw: object) -> dict:
        if self._timeout:
            raise HarnessTimeout("timed out")
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


def _worker_svc(
    repo: _FakeRepo, harness: _FakeHarness, *, enqueue: object | None = None
) -> tuple[JobService, _FakeProvenance]:
    prov = _FakeProvenance()
    return (
        JobService(jobs=repo, provenance=prov, harness=harness, enqueue=enqueue),  # type: ignore[arg-type]
        prov,
    )


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


async def test_enqueue_failure_fails_the_row_not_orphan_queued() -> None:
    repo = _FakeRepo()

    def boom(_j: object, _o: object, _u: object) -> None:
        raise RuntimeError("broker down")

    svc = JobService(jobs=repo, provenance=_FakeProvenance(), enqueue=boom)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await svc.submit(principal=_principal(), input_text="go", manifest_inline={})
    rows = list(repo.rows.values())
    assert len(rows) == 1
    assert rows[0].state == S.FAILED.value and rows[0].error_type == "enqueue_failed"


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


# ── S3: timeout + retry ─────────────────────────────────────────────────────────────────────────
async def test_execute_timeout_marks_timed_out() -> None:
    repo = _FakeRepo()
    job = await _queued_job(repo)
    svc, _ = _worker_svc(repo, _FakeHarness(timeout=True))
    out = await svc.execute(job.id, _principal())
    assert out.state == S.TIMED_OUT.value and out.error_type == "timeout"


async def test_failed_job_retries_when_under_cap() -> None:
    repo = _FakeRepo()
    job = await repo.create(
        organisation_id=_ORG, user_id=_USER, input_text="go", manifest_inline={}, max_retries=2
    )
    calls: list = []
    svc, prov = _worker_svc(
        repo, _FakeHarness(raises=True), enqueue=lambda j, o, u: calls.append(j)
    )
    out = await svc.execute(job.id, _principal())
    assert out.state == S.QUEUED.value and out.retry_count == 1  # re-queued, not terminal
    assert calls == [job.id]  # re-enqueued for another attempt
    assert any(a == "engine.job.retry" for a, _ in prov.events)


async def test_retry_exhausted_is_terminal() -> None:
    repo = _FakeRepo()
    job = await repo.create(
        organisation_id=_ORG, user_id=_USER, input_text="go", manifest_inline={}, max_retries=1
    )
    job.retry_count = 1  # already used the one retry
    calls: list = []
    svc, _ = _worker_svc(repo, _FakeHarness(raises=True), enqueue=lambda j, o, u: calls.append(j))
    out = await svc.execute(job.id, _principal())
    assert out.state == S.FAILED.value  # terminal, not re-queued
    assert calls == []


# ── S3: the RUNNING-stale reaper ─────────────────────────────────────────────────────────────────
async def test_reap_times_out_a_stale_running_job() -> None:
    import datetime as _dt

    repo = _FakeRepo()
    job = await _queued_job(repo)
    job.state = S.RUNNING.value  # stranded RUNNING (worker died after RUNNING)
    svc, _ = _worker_svc(repo, _FakeHarness(), enqueue=lambda j, o, u: None)
    reaped = await svc.reap_stale(older_than=_dt.datetime.now(_dt.UTC))
    assert reaped == 1 and repo.rows[job.id].state == S.TIMED_OUT.value
    assert repo.rows[job.id].error_type == "lease_expired"


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
