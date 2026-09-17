"""#1072 (T2) — ``HarnessExecutionService`` cancellation: the service-level half of the design (a
Postgres lease + a poll watcher; see the #1072 design doc). ``HarnessExecutionService.execute()``
runs the tool-use loop as a cancellable task while a lease row (inserted before the loop starts)
lets ``cancel()`` — called from another request, possibly another replica's request that hit THIS
replica's lease row via the DB — flag the run for cancellation. A watcher on the OWNING replica
polls the flag and cancels the loop task, which interrupts an in-flight LLM call; the service then
persists whatever the loop had already booked into the shared ``LoopProgress`` (#1072 T1,
``oraclous_harness_runtime_service.domain.loop.progress.LoopProgress``) as a ``CANCELLED`` terminal
row, emits its usual step + closure provenance, and skips the post-run memory hook (the run did not
complete).

**Not-yet-built seams**, pinned by this file (each imported function-locally, never at module
level, per ``.claude/rules/tests-seam-imports.md`` — the surrounding module/class already exists,
so most of the seams below surface as ``TypeError``/``AttributeError`` at the call site rather than
``ModuleNotFoundError``; the two genuinely new modules give ``ModuleNotFoundError``):

* ``oraclous_harness_runtime_service.repositories.execution_lease_repository`` (new module) —
  ``ExecutionLeaseRepository`` (the real Postgres-backed repo; T4 pins it + its RLS isolation) and
  ``DuplicateExecutionId`` (raised by ``create()`` on a PK conflict — "a duplicate id returns 409
  (global primary key)", design doc). This file uses only the SHAPE (an
  ``ExecutionLeaseRepository``-like fake: ``create(execution_id, organisation_id)``,
  ``request_cancel(execution_id, organisation_id) -> bool``, ``is_cancel_requested(execution_id,
  organisation_id) -> bool``, ``release(execution_id, organisation_id)``) and imports the real
  ``DuplicateExecutionId`` only where a test asserts the service actually raises it. Every method
  is org-scoped — the lease table carries forced RLS, so an unbound or wrong org sees zero rows
  under the real ``oraclous_app`` role; the watcher and ``release`` must present the execution's
  OWN org (known from the run's principal), never call org-blind.
* ``HarnessExecutionService.__init__`` gains ``leases: ExecutionLeaseRepository`` (alongside the
  existing ``executions``/``checkpoints`` repos), ``cancel_poll_seconds: float`` and
  ``cancel_wait_seconds: float`` (the ``HARNESS_CANCEL_POLL_SECONDS`` /
  ``HARNESS_CANCEL_WAIT_SECONDS`` settings, injectable so tests never sleep multiple real seconds).
  Today's constructor rejects all three — ``TypeError``.
* ``HarnessExecutionService.execute()`` gains ``execution_id: uuid.UUID | None = None`` (the engine
  mints and supplies it so it can cancel before any response arrives; the service still mints one
  itself when absent). Today: ``TypeError``. The route-level counterpart
  ``oraclous_harness_runtime_service.schema.harness_schemas.ExecuteHarnessRequest.execution_id`` is
  T3's seam, not exercised here — this file drives the service method directly.
* ``HarnessExecutionService.cancel(self, *, execution_id: uuid.UUID, organisation_id: uuid.UUID) ->
  HarnessExecution | CancelPending`` (new method; today: ``AttributeError``). Terminal row for
  ``(id, org)`` → returns the row untouched (the lease is never consulted). Otherwise a lease for
  ``(id, org)`` → sets the cancel flag and waits up to ``cancel_wait_seconds`` for the terminal
  row to land, returning it if it does. Unknown id, or a lease that belongs to a DIFFERENT org →
  ``CancelError`` (see below), identical in both cases — the flag is never set across orgs.
* ``CancelError`` (new exception in this module, alongside the existing ``ResumeError`` it mirrors)
  — ``status_code`` defaults to 404 (both of ``cancel()``'s error paths are not-found).
* ``CancelPending`` (new small marker in this module) — ``execution_id: uuid.UUID`` and
  ``status: str = "CANCEL_REQUESTED"``, returned when the wait expires before the terminal row lands
  (the route maps this to 202; T3).
* ``oraclous_harness_runtime_service.models.enums.HarnessStatus.CANCELLED`` — a new member on the
  EXISTING ``HarnessStatus`` enum (module-level import of ``HarnessStatus`` itself is safe; only
  ``.CANCELLED`` is missing today — ``AttributeError``).

The cancellation scenarios drive ``execute()`` with ``run_tool_use_loop`` monkeypatched to a fake
that writes into the shared ``progress`` object then hangs (mirrors
``test_fetched_urls_service_threading.py``'s ``_stub_loop`` technique of stubbing the name inside
``harness_execution_service`` module, and T1's "book usage before a long in-flight call, then get
cancelled" shape) — never a full fake LLM/loop run, since T1 already owns loop-internals coverage.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_harness_runtime_service.models.enums import HarnessStatus
from oraclous_harness_runtime_service.services.memory_client import (
    MemoryWriter,
    drain_pending_writes,
)
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()

# How long the fake loop sleeps once it has booked its progress — many times longer than any test
# here will wait before it must have been cancelled out from under it.
_HANG_SECONDS = 60.0

_FAST_POLL_SECONDS = 0.01
_SHORT_WAIT_SECONDS = 0.02
_LONG_WAIT_SECONDS = 2.0


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _manifest() -> dict[str, Any]:
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
            "name": "Cancel Demo",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": [{"ref": "core/echo@1.0.0", "binding": "echo"}],
        "prompts": [{"role": "primary", "source": "inline", "body": "You are helpful."}],
        "runtime": {"entrypoint": "echo"},
    }


_DESCRIPTOR = {
    "id": "cap-1",
    "metadata": {"name": "Echo"},
    "spec": {"capabilities": [{"name": "run", "description": "Echo back", "parameters": {}}]},
}


class _FakeRegistry:
    async def resolve_capability(self, ref: str, *, explicit_id: str | None = None) -> dict:
        return {"id": "cap-1", "name": "Echo", "descriptor": _DESCRIPTOR}

    async def list_instances(self) -> list[dict]:
        return []

    async def create_instance(self, *, capability_id: str, name: str, configuration: dict) -> dict:
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(self, instance_id: uuid.UUID, mappings: dict) -> dict:
        return {}

    async def execute(self, instance_id: uuid.UUID, input_data: dict) -> dict:
        return {"status": "SUCCESS", "output_data": {}}


class _FakeExecutions:
    """Org-scoped in-memory execution rows, seedable directly via ``.rows`` for the terminal-row
    cancel scenarios that never run a loop at all."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, SimpleNamespace] = {}
        self.created: dict[str, Any] | None = None
        self.create_calls = 0

    async def create(self, **fields: Any) -> SimpleNamespace:
        self.create_calls += 1
        self.created = fields
        row = SimpleNamespace(id=fields["execution_id"], **fields)
        self.rows[row.id] = row
        return row

    async def get(
        self, execution_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> SimpleNamespace | None:
        row = self.rows.get(execution_id)
        return row if row is not None and row.organisation_id == organisation_id else None

    async def update_run(
        self, execution_id: uuid.UUID, organisation_id: uuid.UUID, **fields: Any
    ) -> SimpleNamespace:
        prior = self.rows.get(execution_id)
        merged = SimpleNamespace(**{**(vars(prior) if prior else {}), **fields, "id": execution_id})
        self.rows[execution_id] = merged
        return merged


class _FakeLeases:
    """``ExecutionLeaseRepository``-shaped fake (design doc: the Postgres lease row that carries a
    cancel request across replicas). Every method is org-scoped, mirroring the real table's forced
    RLS: ``is_cancel_requested`` and ``release`` take the CALLER's org and see/affect nothing when
    it does not match the row's own org — an unbound or wrong org sees zero rows under the real
    ``oraclous_app`` role, so the watcher and ``release`` must present the execution's OWN org
    (known from the run's principal), never call org-blind. ``request_cancel`` was already
    org-scoped — a wrong-org caller gets ``False`` and the flag is left untouched, never leaking
    whether the id exists at all."""

    def __init__(self) -> None:
        self._owner: dict[uuid.UUID, uuid.UUID] = {}
        self._cancel_requested: set[uuid.UUID] = set()
        self.released: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.request_cancel_calls: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.is_cancel_requested_calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def create(self, execution_id: uuid.UUID, organisation_id: uuid.UUID) -> None:
        from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
            DuplicateExecutionId,
        )

        if execution_id in self._owner:
            raise DuplicateExecutionId(execution_id)
        self._owner[execution_id] = organisation_id

    async def request_cancel(self, execution_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        self.request_cancel_calls.append((execution_id, organisation_id))
        if self._owner.get(execution_id) != organisation_id:
            return False
        self._cancel_requested.add(execution_id)
        return True

    async def is_cancel_requested(
        self, execution_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> bool:
        self.is_cancel_requested_calls.append((execution_id, organisation_id))
        if self._owner.get(execution_id) != organisation_id:
            return False  # wrong/unbound org: zero rows visible under RLS
        return execution_id in self._cancel_requested

    async def release(self, execution_id: uuid.UUID, organisation_id: uuid.UUID) -> None:
        self.released.append((execution_id, organisation_id))
        if self._owner.get(execution_id) != organisation_id:
            return  # wrong/unbound org: DELETE affects zero rows under RLS
        self._owner.pop(execution_id, None)
        self._cancel_requested.discard(execution_id)


class _FakeProv:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    async def emit(self, record: Any) -> None:
        self.events.append((record.action, record.resource, record.outcome))


def _capture_writer(seen: list[dict]) -> MemoryWriter:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({"body": json.loads(req.content.decode())})
        return httpx.Response(201, json={"memory_id": "m1", "importance_score": 0.4})

    return MemoryWriter(
        base_url="http://knowledge-graph-service:8000",
        headers={"X-Internal-Key": "k", "X-Organisation-Id": str(_ORG)},
        timeout=2.0,
        transport=httpx.MockTransport(handler),
    )


def _service(
    executions: Any = None,
    *,
    leases: Any = None,
    provenance: Any = None,
    memory: MemoryWriter | None = None,
    cancel_poll_seconds: float = _FAST_POLL_SECONDS,
    cancel_wait_seconds: float = _LONG_WAIT_SECONDS,
) -> Any:
    from oraclous_harness_runtime_service.services.harness_execution_service import (
        HarnessExecutionService,
    )

    return HarnessExecutionService(
        registry=_FakeRegistry(),
        broker=None,
        executions=executions if executions is not None else _FakeExecutions(),
        assignments=None,
        checkpoints=None,
        provenance=provenance if provenance is not None else _FakeProv(),
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
        memory=memory,
        # RED today: __init__ takes none of these three — TypeError.
        leases=leases if leases is not None else _FakeLeases(),
        cancel_poll_seconds=cancel_poll_seconds,
        cancel_wait_seconds=cancel_wait_seconds,
    )


def _stub_fast_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loop that answers immediately — SUCCEEDED, no cancellation involved."""

    async def fake_run_tool_use_loop(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            status=HarnessStatus.SUCCEEDED,
            error_type=None,
            error_message=None,
            checkpoint=None,
            output="done",
            steps=[],
            total_tokens=0,
            iterations=1,
            input_tokens=0,
            output_tokens=0,
            protocol_shape="fake",
            served_citation_ids=[],
            fetched_urls=[],
        )

    import oraclous_harness_runtime_service.services.harness_execution_service as svc_mod

    monkeypatch.setattr(svc_mod, "run_tool_use_loop", fake_run_tool_use_loop)


def _stub_hanging_loop(
    monkeypatch: pytest.MonkeyPatch, started: asyncio.Event, *, steps: list[Any] | None = None
) -> None:
    """A loop that books its usage into the shared ``progress`` (T1's ``LoopProgress``) the moment
    it is called, signals ``started``, then hangs far longer than any test here waits — standing in
    for an in-flight LLM call the service's watcher must cancel out from under it. Mirrors T1's
    ``test_loop_cancellation_progress.py`` fake shape, one level up (the SERVICE call site, not the
    loop internals — T1 already owns those)."""

    async def fake_run_tool_use_loop(*, progress: Any = None, **kwargs: Any) -> SimpleNamespace:
        if progress is not None:
            progress.total_tokens = 100
            progress.prompt_tokens = 60
            progress.completion_tokens = 40
            if steps:
                progress.steps.extend(steps)
        started.set()
        await asyncio.sleep(_HANG_SECONDS)
        raise AssertionError(
            "the hanging loop ran to completion instead of being cancelled by the watcher"
        )

    import oraclous_harness_runtime_service.services.harness_execution_service as svc_mod

    monkeypatch.setattr(svc_mod, "run_tool_use_loop", fake_run_tool_use_loop)


# ----------------------------------------------------------------- execute(): execution_id --------


async def test_execute_uses_caller_supplied_execution_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_fast_loop(monkeypatch)
    execs = _FakeExecutions()
    leases = _FakeLeases()
    svc = _service(execs, leases=leases)
    execution_id = uuid.uuid4()

    row = await svc.execute(
        manifest_inline=_manifest(),
        manifest_ref=None,
        user_input="go",
        principal=_principal(),
        execution_id=execution_id,
    )  # TypeError today: execute() accepts no execution_id kwarg

    assert row.id == execution_id
    assert execs.created is not None
    assert execs.created["execution_id"] == execution_id
    # the lease's whole lifecycle (design doc: inserted before the loop, deleted after the terminal
    # row persists) applies to EVERY terminal outcome, not only a cancelled one — released with the
    # RUN's own org, never org-blind (the delete is a no-op under RLS otherwise).
    assert leases.released == [(execution_id, _ORG)]


async def test_duplicate_execution_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_fast_loop(monkeypatch)
    execution_id = uuid.uuid4()
    leases = _FakeLeases()
    await leases.create(execution_id, _ORG)  # an id already claimed by an in-flight dispatch
    svc = _service(_FakeExecutions(), leases=leases)

    from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
        DuplicateExecutionId,
    )

    with pytest.raises(DuplicateExecutionId):
        await svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )


async def test_execute_with_id_of_existing_terminal_row_rejected_before_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay of an ``execution_id`` whose LEASE was already released (an earlier run finished
    and the lease's normal end-of-run cleanup fired) but whose TERMINAL ``executions`` row still
    exists must be rejected as a duplicate BEFORE the loop runs — never after paying for a whole
    loop run and only then hitting the executions table's PK conflict at the very end (PR #1084
    round-2 review, non-blocking item 3: that late failure means spend with no row and a 500)."""
    loop_calls = 0

    async def fake_run_tool_use_loop(**kwargs: Any) -> SimpleNamespace:
        nonlocal loop_calls
        loop_calls += 1
        return SimpleNamespace(
            status=HarnessStatus.SUCCEEDED,
            error_type=None,
            error_message=None,
            checkpoint=None,
            output="done",
            steps=[],
            total_tokens=0,
            iterations=1,
            input_tokens=0,
            output_tokens=0,
            protocol_shape="fake",
            served_citation_ids=[],
            fetched_urls=[],
        )

    import oraclous_harness_runtime_service.services.harness_execution_service as svc_mod

    monkeypatch.setattr(svc_mod, "run_tool_use_loop", fake_run_tool_use_loop)

    execution_id = uuid.uuid4()
    execs = _FakeExecutions()
    execs.rows[execution_id] = SimpleNamespace(
        id=execution_id,
        execution_id=execution_id,
        organisation_id=_ORG,
        status=HarnessStatus.SUCCEEDED.value,
        total_tokens=5,
        input_tokens=3,
        output_tokens=2,
    )
    leases = _FakeLeases()  # no lease row — already released once the earlier run completed
    svc = _service(execs, leases=leases)

    from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
        DuplicateExecutionId,
    )

    with pytest.raises(DuplicateExecutionId):
        await svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )

    assert loop_calls == 0  # rejected up front — the loop must never have been entered


# ------------------------------------------------------- execute() + cancel(): the in-flight run --


async def test_cancel_flag_stops_in_flight_loop_and_persists_cancelled_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    _stub_hanging_loop(monkeypatch, started)
    execs = _FakeExecutions()
    leases = _FakeLeases()
    execution_id = uuid.uuid4()
    svc = _service(
        execs,
        leases=leases,
        cancel_poll_seconds=_FAST_POLL_SECONDS,
        cancel_wait_seconds=_LONG_WAIT_SECONDS,
    )

    exec_task = asyncio.create_task(
        svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)

    cancel_result = await svc.cancel(execution_id=execution_id, organisation_id=_ORG)
    finished_row = await asyncio.wait_for(exec_task, timeout=5.0)

    # the run's OWN return value: a CANCELLED terminal booked from whatever the loop had already
    # spent before the watcher cut it off — never nothing, never a crash.
    assert finished_row.status == HarnessStatus.CANCELLED.value
    assert finished_row.total_tokens == 100
    # the org spend endpoint (execution_repository.spend_by_model, /v1/harnesses/spend) sums
    # input_tokens + output_tokens, NOT total_tokens — an implementation that only books
    # total_tokens from LoopProgress would leave both at 0 and silently undercount org spend.
    assert finished_row.input_tokens == 60
    assert finished_row.output_tokens == 40
    assert cancel_result.status == HarnessStatus.CANCELLED.value  # cancel() saw the SAME terminal
    assert execs.create_calls == 1  # exactly one terminal row — no separate "started" placeholder


async def test_cancelled_run_emits_step_and_closure_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oraclous_harness_runtime_service.domain.loop.tool_use import LoopStep
    from oraclous_harness_runtime_service.models.enums import StepKind

    started = asyncio.Event()
    booked_step = LoopStep(index=0, kind=StepKind.LLM, name="primary", status="tool_calls")
    _stub_hanging_loop(monkeypatch, started, steps=[booked_step])
    execs = _FakeExecutions()
    leases = _FakeLeases()
    prov = _FakeProv()
    execution_id = uuid.uuid4()
    svc = _service(execs, leases=leases, provenance=prov)

    exec_task = asyncio.create_task(
        svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await svc.cancel(execution_id=execution_id, organisation_id=_ORG)
    await asyncio.wait_for(exec_task, timeout=5.0)

    resource = f"harness_execution:{execution_id}"
    assert ("llm.complete", resource, "primary:tool_calls") in prov.events  # the booked step
    assert ("harness.execute", resource, HarnessStatus.CANCELLED.value) in prov.events  # closure


async def test_cancelled_run_skips_memory_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    _stub_hanging_loop(monkeypatch, started)
    execs = _FakeExecutions()
    leases = _FakeLeases()
    seen: list[dict] = []
    execution_id = uuid.uuid4()
    svc = _service(execs, leases=leases, memory=_capture_writer(seen))

    exec_task = asyncio.create_task(
        svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await svc.cancel(execution_id=execution_id, organisation_id=_ORG)
    row = await asyncio.wait_for(exec_task, timeout=5.0)
    await drain_pending_writes()

    assert row.status == HarnessStatus.CANCELLED.value
    assert seen == []  # a run that did not complete writes no episodic memory


async def test_lease_released_after_terminal_row(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    _stub_hanging_loop(monkeypatch, started)
    execs = _FakeExecutions()
    leases = _FakeLeases()
    execution_id = uuid.uuid4()
    svc = _service(execs, leases=leases)

    exec_task = asyncio.create_task(
        svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await svc.cancel(execution_id=execution_id, organisation_id=_ORG)
    await asyncio.wait_for(exec_task, timeout=5.0)

    assert leases.released == [(execution_id, _ORG)]
    assert execution_id not in leases._owner  # the row itself is gone once the terminal row lands
    # the watcher itself polled with the RUN's own org — org-blind would see zero rows under RLS.
    assert leases.is_cancel_requested_calls
    assert all(org == _ORG for _eid, org in leases.is_cancel_requested_calls)


async def test_lease_released_when_loop_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The "released on every terminal outcome" guarantee (``test_execute_uses_caller_supplied_
    execution_id``) must hold when the loop raises an ordinary exception too, not only on success
    or cancellation — an unreleased lease on a crashed run would wedge every future ``cancel()``
    against a lease whose run no longer exists."""

    async def fake_run_tool_use_loop(**kwargs: Any) -> SimpleNamespace:
        raise RuntimeError("boom")

    import oraclous_harness_runtime_service.services.harness_execution_service as svc_mod

    monkeypatch.setattr(svc_mod, "run_tool_use_loop", fake_run_tool_use_loop)

    execs = _FakeExecutions()
    leases = _FakeLeases()
    execution_id = uuid.uuid4()
    svc = _service(execs, leases=leases)

    with pytest.raises(RuntimeError):
        await svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )

    assert leases.released == [(execution_id, _ORG)]


# ------------------------------------------------------------- cancel(): the non-loop scenarios --


async def test_cancel_unknown_id_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service(_FakeExecutions(), leases=_FakeLeases())

    from oraclous_harness_runtime_service.services.harness_execution_service import CancelError

    with pytest.raises(CancelError) as exc:
        await svc.cancel(execution_id=uuid.uuid4(), organisation_id=_ORG)
    assert exc.value.status_code == 404


async def test_cancel_other_org_not_found_and_flag_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = uuid.uuid4()
    leases = _FakeLeases()
    await leases.create(execution_id, _ORG)  # an in-flight run owned by org A
    svc = _service(_FakeExecutions(), leases=leases)
    other_org = uuid.uuid4()

    from oraclous_harness_runtime_service.services.harness_execution_service import CancelError

    with pytest.raises(CancelError) as wrong_org_exc:
        await svc.cancel(execution_id=execution_id, organisation_id=other_org)
    with pytest.raises(CancelError) as unknown_id_exc:
        await svc.cancel(execution_id=uuid.uuid4(), organisation_id=_ORG)

    # indistinguishable from an unknown id: same status code, same message — a caller from the
    # wrong org must learn nothing about whether the id exists at all.
    assert wrong_org_exc.value.status_code == unknown_id_exc.value.status_code == 404
    assert str(wrong_org_exc.value) == str(unknown_id_exc.value)
    # never set across orgs — checked with the TRUE owning org so a False here cannot be an
    # artifact of asking the fake with the wrong org itself.
    assert await leases.is_cancel_requested(execution_id, _ORG) is False


async def test_cancel_terminal_returns_row_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_id = uuid.uuid4()
    execs = _FakeExecutions()
    execs.rows[execution_id] = SimpleNamespace(
        id=execution_id, organisation_id=_ORG, status="SUCCEEDED", total_tokens=42
    )
    leases = _FakeLeases()  # no lease row — already released once the run completed
    svc = _service(execs, leases=leases)

    result = await svc.cancel(execution_id=execution_id, organisation_id=_ORG)

    assert result.status == "SUCCEEDED"
    assert result.total_tokens == 42  # idempotent: the true (already-settled) spend, untouched
    assert leases.request_cancel_calls == []  # a finished run's cancel never even looks at a lease


async def test_cancel_inflight_sets_flag_and_returns_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    _stub_hanging_loop(monkeypatch, started)
    execs = _FakeExecutions()
    leases = _FakeLeases()
    execution_id = uuid.uuid4()
    svc = _service(execs, leases=leases)

    exec_task = asyncio.create_task(
        svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)

    result = await svc.cancel(execution_id=execution_id, organisation_id=_ORG)
    await asyncio.wait_for(exec_task, timeout=5.0)

    assert leases.request_cancel_calls == [(execution_id, _ORG)]  # the flag WAS set on this lease
    # cancel() itself returned the terminal row (not a pending marker) — the wait caught it in time
    assert result.id == execution_id
    assert result.status == HarnessStatus.CANCELLED.value


class _NeverNoticingLeases(_FakeLeases):
    """``is_cancel_requested`` blocks on ``unblock`` (never set by the test until AFTER the
    ``CancelPending`` assertion below) instead of ever returning a value. A watcher that awaits
    this call is provably stuck mid-check for as long as the test holds ``unblock`` closed — it
    can never observe the flag and so can never cancel the hanging loop, no matter how the real
    implementation schedules its poll (check-then-sleep, sleep-then-check, any interval). This
    makes the expiry a hard gate, not a race against relative sleep durations."""

    def __init__(self, unblock: asyncio.Event) -> None:
        super().__init__()
        self._unblock = unblock

    async def is_cancel_requested(
        self, execution_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> bool:
        await self._unblock.wait()
        return await super().is_cancel_requested(execution_id, organisation_id)


async def test_cancel_wait_expiry_returns_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    unblock_watcher = asyncio.Event()
    _stub_hanging_loop(monkeypatch, started)
    execs = _FakeExecutions()
    leases = _NeverNoticingLeases(unblock_watcher)
    execution_id = uuid.uuid4()
    svc = _service(execs, leases=leases, cancel_wait_seconds=_SHORT_WAIT_SECONDS)

    exec_task = asyncio.create_task(
        svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5.0)

        from oraclous_harness_runtime_service.services.harness_execution_service import (
            CancelPending,
        )

        result = await svc.cancel(execution_id=execution_id, organisation_id=_ORG)

        assert isinstance(result, CancelPending)
        assert result.execution_id == execution_id
        assert result.status == "CANCEL_REQUESTED"
        assert execs.create_calls == 0  # the wait gave up before any terminal row could land
    finally:
        unblock_watcher.set()  # release any watcher stuck mid-check before tearing the task down
        exec_task.cancel()
        with contextlib.suppress(BaseException):
            await exec_task


# ------------------------------------------------- execute(): the OUTER task's own cancellation ---


async def test_outer_cancellation_propagates_and_writes_no_cancelled_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """code-reviewer B1 (PR #1094): the WATCHER cancelling ``loop_task`` (a run cancel) and the
    HANDLER's own task being cancelled (a forced shutdown, or a future ``asyncio.timeout()``/
    TaskGroup around ``execute()``) both surface as the SAME ``except asyncio.CancelledError``
    around ``await loop_task``. They must not be treated alike: only the watcher's cancel may turn
    into a CANCELLED terminal row + closing provenance. The handler's own cancellation must
    propagate as ``asyncio.CancelledError`` — no ``executions.create`` call at all (never a
    CANCELLED row, never any row) — while the lease is still released, so a duplicate id is never
    left permanently blocked by a run that was cut off mid-flight.

    Deterministic via ``started`` (sequences past "the loop has been entered") and
    ``asyncio.wait_for`` on the task's own completion — no sleeps."""
    started = asyncio.Event()
    _stub_hanging_loop(monkeypatch, started)
    execs = _FakeExecutions()
    leases = _FakeLeases()
    execution_id = uuid.uuid4()
    # RED today: HarnessExecutionService.__init__ takes no `leases` kwarg — TypeError.
    svc = _service(execs, leases=leases)

    exec_task = asyncio.create_task(
        svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            execution_id=execution_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)

    exec_task.cancel()  # the OUTER task's own cancellation — never the watcher

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(exec_task, timeout=5.0)

    assert execs.create_calls == 0  # no CANCELLED row, no row at all
    assert leases.released == [(execution_id, _ORG)]  # released despite the propagated cancel
