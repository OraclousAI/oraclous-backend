"""#826 — background adopted-tool runs emit nothing.

``_run_adopted_tool_async`` (tasks/run_tasks.py:256-322) dispatches an ADOPTED_TOOL_RUN schedule
fire straight to the capability-registry's ``execute`` over HTTP and constructs no collector at
all — unlike ``_run_async`` (:56-86) and ``_fire_schedules_async`` (:163-195), which both build a
``PostgresProvenanceSink`` + ``ProvenanceCollector`` before driving. Per the 24 August 2026
solution-architect ruling on #826, the engine's own dispatch of a capability invocation must emit
through the collector too.

Unit-tested like ``test_adopted_tool_redelivery.py``: fakes for JobRepository/RegistryClient,
monkeypatched into the worker module — plus fakes for ``PostgresProvenanceSink`` /
``ProvenanceCollector``, both of which the module ALREADY imports at module level (they exist
today; only the call site inside ``_run_adopted_tool_async`` is missing), so patching them is a
plain monkeypatch, not a not-yet-built-seam import.

RED until the dispatch wires a collector: every test below asserts on ``created`` (the collectors
the patched factory actually built) and finds it empty.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_execution_engine_service.tasks import run_tasks

pytestmark = pytest.mark.unit

_RUN, _INST, _ORG, _USER = (str(uuid.uuid4()) for _ in range(4))


def _expected_hash(payload: Any) -> str | None:
    """The REAL substrate helper (24 August ruling §3), function-local — not yet built."""
    from oraclous_substrate.provenance import hash_payload  # seam import: not-yet-built

    return hash_payload(payload)


def _row(*, execution_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(execution_id=execution_id, dispatched_at=None)


class _FakeJobs:
    def __init__(self, row: SimpleNamespace) -> None:
        self.row = row
        self.closed = False

    async def get_adopted_run(self, run_id: uuid.UUID, org_id: uuid.UUID) -> SimpleNamespace:
        return self.row

    async def claim_adopted_dispatch(
        self, run_id: uuid.UUID, org_id: uuid.UUID, *, now: datetime, lease_seconds: int
    ) -> bool:
        r = self.row
        stale_before = now - timedelta(seconds=lease_seconds)
        if r.execution_id is None and (r.dispatched_at is None or r.dispatched_at < stale_before):
            r.dispatched_at = now
            return True
        return False

    async def set_adopted_execution_id(
        self, run_id: uuid.UUID, org_id: uuid.UUID, execution_id: uuid.UUID
    ) -> None:
        self.row.execution_id = execution_id

    async def close(self) -> None:
        self.closed = True


class _FakeRegistry:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls = 0
        self.closed = False
        self._raises = raises

    async def execute(self, instance_id: uuid.UUID, input_data: dict) -> dict:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return {"id": str(uuid.uuid4()), "status": "SUCCESS"}

    async def aclose(self) -> None:
        self.closed = True


class _FakeSink:
    """Stands in for PostgresProvenanceSink — never opens a real connection."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeCollector:
    def __init__(self, sink: Any) -> None:
        self.sink = sink
        self.events: list[Any] = []

    async def emit(self, record: Any) -> None:
        self.events.append(record)


def _wire(
    monkeypatch: pytest.MonkeyPatch, jobs: _FakeJobs, registry: _FakeRegistry
) -> list[_FakeCollector]:
    created: list[_FakeCollector] = []

    def _collector_factory(sink: Any) -> _FakeCollector:
        c = _FakeCollector(sink)
        created.append(c)
        return c

    monkeypatch.setattr(run_tasks, "JobRepository", lambda *a, **k: jobs)
    monkeypatch.setattr(run_tasks, "RegistryClient", lambda *a, **k: registry)
    monkeypatch.setattr(run_tasks, "PostgresProvenanceSink", lambda *a, **k: _FakeSink())
    monkeypatch.setattr(run_tasks, "ProvenanceCollector", _collector_factory)
    monkeypatch.setattr(run_tasks, "build_downstream_headers", lambda principal, settings: {})
    return created


async def test_a_successful_dispatch_emits_capability_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, registry = _FakeJobs(_row()), _FakeRegistry()
    created = _wire(monkeypatch, jobs, registry)
    payload = {"channel": "email"}

    await run_tasks._run_adopted_tool_async(_RUN, _INST, payload, _ORG, _USER)

    assert created, "no ProvenanceCollector was ever constructed — the dispatch emits nothing"
    events = created[0].events
    assert len(events) == 1, events
    assert events[0].action == "capability.invoke"
    assert events[0].resource == f"tool_instance:{_INST}"
    assert events[0].outcome == "SUCCESS"
    assert events[0].input_hash == _expected_hash(payload)


async def test_a_failed_dispatch_still_emits_with_a_failure_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = _FakeJobs(_row())
    registry = _FakeRegistry(raises=RuntimeError("registry unreachable"))
    created = _wire(monkeypatch, jobs, registry)

    with pytest.raises(RuntimeError):
        await run_tasks._run_adopted_tool_async(_RUN, _INST, {}, _ORG, _USER)

    assert created, "no ProvenanceCollector was ever constructed on the failure path"
    events = created[0].events
    assert len(events) == 1, events
    assert events[0].action == "capability.invoke"
    assert events[0].resource == f"tool_instance:{_INST}"
    assert events[0].outcome == "FAILED"
