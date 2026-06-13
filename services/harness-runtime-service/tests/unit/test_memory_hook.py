"""Unit: the post-run memory hook (#332 / ADR-027 §5) — flag-gating + the ZERO-RISK contract.

The two MANDATORY assertions:
  * flag OFF → ZERO memory calls (the writer is never even constructed);
  * flag ON + KGS UNREACHABLE → the run STILL COMPLETES successfully (fire-and-forget: the write
    fails detached, swallowed + logged, and nothing reaches the run path).

Plus: the episodic payload shape (agent, task, status, tool usage, session = execution id), the
graph-context selection (exactly one manifest ``config.graph_id`` → sent; ambiguous → omitted →
KGS org-default), the DENIED-resume procedural feedback memory, and the writer's own swallow-all.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_harness_runtime_service.core.config import get_settings
from oraclous_harness_runtime_service.core.dependencies import get_memory_writer
from oraclous_harness_runtime_service.domain.ohm.signatures import TrustStore
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_harness_runtime_service.services.memory_client import (
    MemoryWriter,
    _pending,
    drain_pending_writes,
)

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _manifest(graph_id: str | None = None, extra_cap: dict | None = None) -> dict[str, Any]:
    cap: dict[str, Any] = {"ref": "core/echo@1.0.0", "binding": "echo"}
    if graph_id:
        cap["config"] = {"graph_id": graph_id}
    capabilities = [cap]
    if extra_cap:
        capabilities.append(extra_cap)
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
            "name": "Memory Demo",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": capabilities,
        "prompts": [{"role": "primary", "source": "inline", "body": "You are helpful."}],
        "runtime": {"entrypoint": "echo"},
    }


_DESCRIPTOR = {
    "id": "cap-1",
    "metadata": {"name": "Echo"},
    "spec": {
        "capabilities": [{"name": "run", "description": "Echo back", "parameters": {}}],
    },
}


class _FakeRegistry:
    def __init__(self) -> None:
        self.executed: list[dict] = []

    async def list_tools(self) -> list[dict]:
        return [{"id": "cap-1", "name": "Echo", "descriptor": _DESCRIPTOR}]

    async def resolve_capability(self, ref: str, *, explicit_id: str | None = None) -> dict:
        return {"id": "cap-1", "name": "Echo", "descriptor": _DESCRIPTOR}

    async def list_instances(self) -> list[dict]:
        return []

    async def create_instance(self, *, capability_id: str, name: str, configuration: dict) -> dict:
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(self, instance_id: uuid.UUID, mappings: dict) -> dict:
        return {}

    async def execute(self, instance_id: uuid.UUID, input_data: dict) -> dict:
        self.executed.append(input_data)
        return {"status": "SUCCESS", "output_data": {"echo": "ok"}}


class _FakeExecutions:
    async def create(self, **fields: Any) -> SimpleNamespace:
        return SimpleNamespace(id=fields["execution_id"], **fields)


class _FakeProv:
    async def emit(self, record: Any) -> None:
        pass


def _service(memory: MemoryWriter | None) -> HarnessExecutionService:
    return HarnessExecutionService(
        registry=_FakeRegistry(),
        broker=None,
        executions=_FakeExecutions(),
        assignments=None,
        checkpoints=None,
        provenance=_FakeProv(),
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
        memory=memory,
    )


def _capture_writer(seen: list[dict]) -> MemoryWriter:
    def handler(req: httpx.Request) -> httpx.Response:
        import json

        seen.append(
            {
                "path": req.url.path,
                "body": json.loads(req.content.decode()),
                "internal_key": req.headers.get("X-Internal-Key"),
            }
        )
        return httpx.Response(201, json={"memory_id": "m1", "importance_score": 0.4})

    return MemoryWriter(
        base_url="http://knowledge-graph-service:8000",
        headers={"X-Internal-Key": "k", "X-Organisation-Id": str(_ORG)},
        timeout=2.0,
        transport=httpx.MockTransport(handler),
    )


# ------------------------------------------------------------- flag gating


def test_flag_off_in_code_builds_no_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """HARNESS_MEMORY_WRITES defaults FALSE in code → get_memory_writer returns None → the
    execution service holds no writer → zero memory calls by construction."""
    monkeypatch.delenv("HARNESS_MEMORY_WRITES", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().memory_writes is False
        assert get_memory_writer(_principal()) is None
    finally:
        get_settings.cache_clear()


def test_flag_on_builds_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_MEMORY_WRITES", "true")
    get_settings.cache_clear()
    try:
        writer = get_memory_writer(_principal())
        assert isinstance(writer, MemoryWriter)
    finally:
        get_settings.cache_clear()


async def test_flag_off_run_completes_with_zero_memory_calls() -> None:
    """The OFF path through execute(): no writer, run completes, nothing scheduled."""
    svc = _service(memory=None)
    row = await svc.execute(
        manifest_inline=_manifest(), manifest_ref=None, user_input="go", principal=_principal()
    )
    await drain_pending_writes()  # nothing must be pending
    assert row.status == "SUCCEEDED"


# ------------------------------------------------------------- the zero-risk test (MANDATORY)


async def test_flag_on_kgs_unreachable_run_still_completes() -> None:
    """THE zero-risk test: the writer points at a dead endpoint (connection refused) with the
    flag on — the run MUST still complete successfully, and draining the detached writes must
    surface no exception (they are swallowed + logged)."""
    writer = MemoryWriter(
        base_url="http://127.0.0.1:9",  # nothing listens here — refused/unroutable
        headers={"X-Internal-Key": "k"},
        timeout=0.2,
    )
    svc = _service(memory=writer)
    row = await svc.execute(
        manifest_inline=_manifest(), manifest_ref=None, user_input="go", principal=_principal()
    )
    assert row.status == "SUCCEEDED"  # the run completed BEFORE the write could even fail
    await drain_pending_writes()  # the failed write surfaces nothing — swallowed by contract


async def test_writer_swallows_a_raising_transport() -> None:
    """Even a transport that explodes synchronously inside httpx is contained."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise RuntimeError("kaboom")

    writer = MemoryWriter(
        base_url="http://kgs",
        headers={},
        transport=httpx.MockTransport(handler),
    )
    writer.schedule_run_outcome(
        harness_id="h",
        harness_name="X",
        status="SUCCEEDED",
        user_input="i",
        output="o",
        tool_names=[],
        execution_id=uuid.uuid4(),
        graph_id=None,
    )
    await drain_pending_writes()  # no raise


def test_schedule_without_a_running_loop_is_swallowed() -> None:
    """Scheduling outside an event loop (no run path would do this, but belt-and-braces) logs
    and returns instead of raising."""
    writer = MemoryWriter(base_url="http://kgs", headers={})
    writer.schedule_run_outcome(
        harness_id="h",
        harness_name="X",
        status="FAILED",
        user_input="i",
        output=None,
        tool_names=[],
        execution_id=uuid.uuid4(),
        graph_id=None,
    )  # no running loop → no raise


# ----------------------------------------------------- bounds (whole-write deadline + pending cap)


async def test_whole_write_deadline_bounds_a_trickling_responder() -> None:
    """A responder that never returns (a byte-trickling/hung KGS) must not keep the detached write
    alive indefinitely — the OVERALL per-write deadline (httpx timeout × factor) cancels it. Proven
    READABLY: draining is bounded well under the would-be hang and surfaces nothing (swallowed)."""
    hang = asyncio.Event()  # never set → the handler would await forever without the deadline

    async def hung_handler(req: httpx.Request) -> httpx.Response:
        await hang.wait()
        return httpx.Response(201, json={})

    # timeout=0.1 → whole-write deadline floors at 1.0s; the write self-cancels by then.
    writer = MemoryWriter(
        base_url="http://kgs",
        headers={},
        timeout=0.1,
        transport=httpx.MockTransport(hung_handler),
    )
    writer.schedule_run_outcome(
        harness_id="h",
        harness_name="X",
        status="SUCCEEDED",
        user_input="i",
        output="o",
        tool_names=[],
        execution_id=uuid.uuid4(),
        graph_id=None,
    )
    # The write must finish (by hitting its own deadline) well within this bound — a regression that
    # dropped the whole-write deadline would hang past it and raise TimeoutError here (readable).
    await asyncio.wait_for(drain_pending_writes(), timeout=5.0)


async def test_pending_set_is_bounded_when_saturated() -> None:
    """The in-flight set is capped: once _MAX_IN_FLIGHT writes are parked, a further schedule is
    SKIPPED (logged), never queued — a stuck KGS can never make _pending grow without limit."""
    from oraclous_harness_runtime_service.services import memory_client

    release = asyncio.Event()

    async def parked_handler(req: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(201, json={})

    writer = MemoryWriter(
        base_url="http://kgs",
        headers={},
        timeout=30.0,
        transport=httpx.MockTransport(parked_handler),
    )

    def _schedule_one() -> None:
        writer.schedule_run_outcome(
            harness_id="h",
            harness_name="X",
            status="SUCCEEDED",
            user_input="i",
            output="o",
            tool_names=[],
            execution_id=uuid.uuid4(),
            graph_id=None,
        )

    try:
        for _ in range(memory_client._MAX_IN_FLIGHT + 25):
            _schedule_one()
        await asyncio.sleep(0)  # let the parked tasks register
        assert len(_pending) <= memory_client._MAX_IN_FLIGHT  # the cap held; extras were skipped
    finally:
        release.set()
        await drain_pending_writes()


# ------------------------------------------------------------- payload shape


async def test_episodic_payload_shape_and_internal_path() -> None:
    seen: list[dict] = []
    svc = _service(memory=_capture_writer(seen))
    row = await svc.execute(
        manifest_inline=_manifest(),
        manifest_ref=None,
        user_input="summarise the quarterly report",
        principal=_principal(),
    )
    await drain_pending_writes()

    assert row.status == "SUCCEEDED"
    assert len(seen) == 1  # ONE episodic memory per completed run, no procedural without feedback
    call = seen[0]
    assert call["path"] == "/internal/v1/memories"
    assert call["internal_key"] == "k"
    body = call["body"]
    assert body["type"] == "episodic"
    assert body["source"] == "agent"
    assert body["event_type"] == "harness_run"
    assert body["agent_id"] == "01976e3a-7c9b-7b00-9c45-1234567890ab"
    assert body["session_id"] == str(row.id)
    assert "Memory Demo" in body["content"] and "SUCCEEDED" in body["content"]
    assert "summarise the quarterly report" in body["content"]
    assert "echo.run" in body["content"]  # key tool usage (the loop's TOOL step name)
    assert "graph_id" not in body  # no manifest graph context → KGS org-default


async def test_graph_context_sent_when_manifest_binds_exactly_one() -> None:
    seen: list[dict] = []
    svc = _service(memory=_capture_writer(seen))
    graph_id = str(uuid.uuid4())
    await svc.execute(
        manifest_inline=_manifest(graph_id=graph_id),
        manifest_ref=None,
        user_input="go",
        principal=_principal(),
    )
    await drain_pending_writes()
    assert seen[0]["body"]["graph_id"] == graph_id


async def test_ambiguous_graph_context_is_omitted() -> None:
    seen: list[dict] = []
    svc = _service(memory=_capture_writer(seen))
    second = {
        "ref": "core/echo@1.0.0",
        "binding": "echo2",
        "config": {"graph_id": str(uuid.uuid4())},
    }
    await svc.execute(
        manifest_inline=_manifest(graph_id=str(uuid.uuid4()), extra_cap=second),
        manifest_ref=None,
        user_input="go",
        principal=_principal(),
    )
    await drain_pending_writes()
    assert "graph_id" not in seen[0]["body"]  # two distinct contexts → ambiguous → org-default


# ------------------------------------------------------------- human feedback (resume DENIED)


async def test_resume_denied_writes_episodic_and_procedural_feedback() -> None:
    class _Execs:
        def __init__(self, row: SimpleNamespace) -> None:
            self._row = row

        async def get(self, execution_id: Any, organisation_id: Any) -> SimpleNamespace | None:
            return self._row if organisation_id == _ORG else None

        async def update_run(self, execution_id: Any, organisation_id: Any, **f: Any) -> Any:
            return SimpleNamespace(id=execution_id, **f)

    class _Ckpts:
        async def get_latest_pending(self, execution_id: Any, org: Any) -> SimpleNamespace:
            return SimpleNamespace(id=uuid.uuid4(), manifest_doc={})

        async def set_decision(self, cid: Any, org: Any, status: str) -> SimpleNamespace:
            return SimpleNamespace(id=cid, status=status)

        async def revert_to_pending(self, cid: Any, org: Any) -> None:
            pass

    row = SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        status="ESCALATED",
        error_type="hitl_required",
        output="partial",
        iterations=1,
        total_tokens=5,
        steps=[],
        input="dangerous thing",
        harness_name="Memory Demo",
        harness_id="01976e3a-7c9b-7b00-9c45-1234567890ab",
    )
    seen: list[dict] = []
    svc = HarnessExecutionService(
        registry=None,
        broker=None,
        executions=_Execs(row),
        assignments=None,
        checkpoints=_Ckpts(),
        provenance=_FakeProv(),
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
        memory=_capture_writer(seen),
    )
    out = await svc.resume(
        execution_id=row.id,
        principal=_principal(),
        decision="DENIED",
        decision_reason="never touch production data",
    )
    await drain_pending_writes()

    assert out.status == "FAILED"
    kinds = {c["body"]["type"] for c in seen}
    assert kinds == {"episodic", "procedural"}  # run outcome + the explicit human feedback
    procedural = next(c["body"] for c in seen if c["body"]["type"] == "procedural")
    assert procedural["source"] == "user_feedback"
    assert procedural["category"] == "feedback"
    assert "never touch production data" in procedural["content"]
    episodic = next(c["body"] for c in seen if c["body"]["type"] == "episodic")
    assert "FAILED" in episodic["content"]


# ------------------------------------------------------------- non-blocking proof


async def test_run_returns_before_a_slow_write_finishes() -> None:
    """Fire-and-forget proof, READABLY: a write that would take far longer than the run must not
    delay execute(). Asserted two ways so a regression FAILS rather than HANGS the suite —

      * ``execute()`` is bounded by ``asyncio.wait_for`` and must return well within it (a blocking
        regression raises TimeoutError → a readable failure, not a hung worker);
      * at the moment ``execute()`` returns, the write task is still in flight (parked in the
        handler) — proving the write genuinely OUTLIVED the return rather than completing inside it.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(req: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()  # park the write until the test lets go
        return httpx.Response(201, json={})

    writer = MemoryWriter(
        base_url="http://kgs",
        headers={},
        timeout=30.0,  # generous per-phase + whole-write deadline so the parked write does NOT trip
        transport=httpx.MockTransport(slow_handler),
    )
    svc = _service(memory=writer)
    try:
        # A non-blocking run returns near-instantly; 5s is orders of magnitude of headroom. A
        # regression that AWAITS the write would block on release (never set) → TimeoutError here.
        row = await asyncio.wait_for(
            svc.execute(
                manifest_inline=_manifest(),
                manifest_ref=None,
                user_input="go",
                principal=_principal(),
            ),
            timeout=5.0,
        )
        assert row.status == "SUCCEEDED"  # returned while the write is parked
        # Prove the write OUTLIVED the return: it has started (the handler ran) but is still parked
        # (in flight), not completed inside execute().
        await asyncio.wait_for(started.wait(), timeout=5.0)
        assert any(not t.done() for t in _pending), (
            "the run returned but no memory write is in flight — the write did not outlive the run"
        )
    finally:
        release.set()  # let the parked write complete even if an assertion failed
        await drain_pending_writes()
