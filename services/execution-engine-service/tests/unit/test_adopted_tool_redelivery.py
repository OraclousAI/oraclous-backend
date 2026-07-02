"""#501-#1: the adopted-tool worker's exactly-once REDELIVERY guard.

Celery ``task_acks_late=True`` re-delivers ``engine.run_adopted_tool`` if a worker dies AFTER the
registry dispatch succeeded but BEFORE the ack. Unlike the harness path (a QUEUED→RUNNING CAS), the
adopted path has no other guard — so ``_run_adopted_tool_async`` reads the org-scoped run row and
short-circuits when ``execution_id`` is already stamped, and the tool never runs twice.

These are UNIT tests (fakes for the JobRepository + RegistryClient, monkeypatched into the worker
module); the true end-to-end exactly-once-under-crash is proven separately on the deployed stack.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from oraclous_execution_engine_service.tasks import run_tasks

pytestmark = pytest.mark.unit

_RUN, _INST, _ORG, _USER = (str(uuid.uuid4()) for _ in range(4))


class _FakeJobs:
    """A stateful adopted-run store: get returns the current row, set stamps it in place — so a
    re-invocation (a redelivery) observes the execution_id the first invocation wrote."""

    def __init__(self, row: SimpleNamespace) -> None:
        self.row = row
        self.closed = False

    async def get_adopted_run(self, run_id: uuid.UUID, org_id: uuid.UUID) -> SimpleNamespace:
        return self.row

    async def set_adopted_execution_id(
        self, run_id: uuid.UUID, org_id: uuid.UUID, execution_id: uuid.UUID
    ) -> None:
        self.row.execution_id = execution_id

    async def close(self) -> None:
        self.closed = True


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def execute(self, instance_id: uuid.UUID, input_data: dict) -> dict:
        self.calls += 1
        return {"id": str(uuid.uuid4()), "status": "SUCCESS"}

    async def aclose(self) -> None:
        self.closed = True


def _wire(monkeypatch: pytest.MonkeyPatch, jobs: _FakeJobs, registry: _FakeRegistry) -> None:
    monkeypatch.setattr(run_tasks, "JobRepository", lambda *a, **k: jobs)
    monkeypatch.setattr(run_tasks, "RegistryClient", lambda *a, **k: registry)
    # keep the identity-header build broker/settings-agnostic here (it is not under test)
    monkeypatch.setattr(run_tasks, "build_downstream_headers", lambda principal, settings: {})


async def test_worker_dispatches_and_stamps_on_a_fresh_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # a FRESH run: the row exists (created before enqueue) but is unstamped → the guard falls
    # through and the registry executes exactly once, then the returned execution_id is stamped.
    jobs = _FakeJobs(SimpleNamespace(execution_id=None))
    registry = _FakeRegistry()
    _wire(monkeypatch, jobs, registry)

    out = await run_tasks._run_adopted_tool_async(_RUN, _INST, {"channel": "email"}, _ORG, _USER)

    assert registry.calls == 1  # executed once
    assert out["execution_id"] is not None and not out.get("deduped")
    assert jobs.row.execution_id is not None  # stamped back onto the run row
    assert registry.closed and jobs.closed  # per-task clients disposed (ADR-012)


async def test_worker_redelivery_after_stamp_short_circuits_no_second_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # THE exactly-once gate: invoke the task, then invoke it AGAIN (a redelivery of the same task)
    # against the SAME store. The first stamps execution_id; the second sees it and short-circuits —
    # so registry.execute runs EXACTLY ONCE across the two deliveries (no duplicate draft).
    jobs = _FakeJobs(SimpleNamespace(execution_id=None))
    registry = _FakeRegistry()
    _wire(monkeypatch, jobs, registry)

    first = await run_tasks._run_adopted_tool_async(_RUN, _INST, {}, _ORG, _USER)
    second = await run_tasks._run_adopted_tool_async(_RUN, _INST, {}, _ORG, _USER)  # redelivery

    assert registry.calls == 1  # exactly ONE execute across the original + the redelivery
    assert not first.get("deduped")  # the first delivery ran the tool
    assert second["deduped"] is True  # the redelivery short-circuited
    assert second["execution_id"] == first["execution_id"]  # same stamped id returned


async def test_worker_short_circuits_a_prestamped_row_without_executing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a redelivery whose run already carries an execution_id (stamped by the crashed first worker):
    # the guard short-circuits BEFORE any registry call — the tool is never re-run.
    stamped = uuid.uuid4()
    jobs = _FakeJobs(SimpleNamespace(execution_id=stamped))
    registry = _FakeRegistry()
    _wire(monkeypatch, jobs, registry)

    out = await run_tasks._run_adopted_tool_async(_RUN, _INST, {}, _ORG, _USER)

    assert registry.calls == 0  # the tool was NOT executed
    assert out["deduped"] is True and out["execution_id"] == str(stamped)
