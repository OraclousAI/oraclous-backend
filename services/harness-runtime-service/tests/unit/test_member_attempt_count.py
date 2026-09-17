"""#1111 item 4 — the harness loop reports an "attempts" count on the execution result the
engine receives.

Decision 4 (posted on #1111): ``attempts = 1 + the in-run recovery retries the member spent``
(final-answer correction turns from ``test_declared_output_repair_gate.py``, plus the transient
model/tool retries already pinned by ``test_tool_use_loop.py``'s ADR-042 retry tests and
``test_tool_call_retry_and_fail_fast.py``). The harness reports it; the engine persists it (a
separate, later commit against ``team_run_service.py``/``EngineTeamRun``).

This file pins two things:

1. ``LoopResult`` gains an ``attempts`` field (default ``1``, the same "byte-for-byte unchanged for
   an untouched call site" convention every prior additive ``LoopResult`` field has used —
   ``served_citation_ids``, ``fetched_urls``). Four cases, each driven through
   ``run_tool_use_loop`` directly with a scripted LLM/dispatch double, exactly as
   ``test_declared_output_repair_gate.py`` and ``test_tool_call_retry_and_fail_fast.py`` do:
   a clean run (1), one final-answer correction turn (2), two transient tool retries then success
   (3), and a quota fail-fast on the very first tool call (1, no retry spent).
2. ``HarnessExecutionService.execute()`` persists ``result.attempts`` onto the created row —
   the same shape ``test_fetched_urls_service_threading.py`` pins for ``fetched_urls``, and the
   row this create() call builds is exactly what ``HarnessExecutionOut`` (``from_attributes=True``)
   turns into the JSON the engine's ``HarnessClient.execute()`` returns, i.e. the shape
   ``team_run.py``'s ``make_harness_dispatch`` reads ``result.get("error_type")`` from today.

RED until the [impl] adds ``LoopResult.attempts`` and threads it through
``HarnessExecutionService.execute()``'s ``self._executions.create(...)`` call — today neither
exists, so every case below fails with ``AttributeError`` (no ``.attempts`` on the returned
``LoopResult``) or a missing dict key (``execs.created`` never carries ``"attempts"``).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop import tool_use
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus
from oraclous_harness_runtime_service.services.registry_client import RegistryError

pytestmark = pytest.mark.unit


def _env(**overrides: Any) -> PolicyEnvelope:
    base: dict[str, Any] = dict(
        max_iterations=8, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
    )
    base.update(overrides)
    return PolicyEnvelope(**base)


async def _no_sleep(_seconds: float) -> None:
    """Matches test_tool_call_retry_and_fail_fast.py's own stand-in — the retry bound is exercised
    for real, with no wall-clock cost."""


# ── case 1: a clean run spends no recovery at all -------------------------------------------------


class _OneShotLLM:
    protocol_shape = "fake"

    async def complete(self, *, messages: Any, system: str, tools: list[Any]) -> LLMResponse:
        return LLMResponse(text="done", tool_calls=[])


async def _no_dispatch(_spec: Any, _args: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("a tool-less member never dispatches")


async def test_a_clean_run_reports_one_attempt() -> None:
    result = await run_tool_use_loop(
        llm=_OneShotLLM(),
        system="",
        user_input="go",
        tool_specs=[],
        dispatch=_no_dispatch,
        policy=_env(),
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.attempts == 1


# ── case 2: one final-answer correction turn spends exactly one recovery --------------------------
# Same broken/fixed pair test_declared_output_repair_gate.py uses for the correction gate itself.

_BROKEN_FINAL_ANSWER = (
    '{"posture": "steady", "headline": "Fed holds rates", "excerpt": "analysts said the outlook '
    'is "choppy":"uncertain" into next quarter"}'
)
_FIXED_FINAL_ANSWER = json.dumps(
    {
        "posture": "steady",
        "headline": "Fed holds rates",
        "excerpt": 'analysts said the outlook is "choppy":"uncertain" into next quarter',
    }
)


class _ScriptedAnswers:
    """One scripted final answer per model turn — no tool calls, a tool-less member."""

    protocol_shape = "fake"

    def __init__(self, *script: str) -> None:
        self._script = list(script)
        self.turns = 0

    async def complete(self, *, messages: Any, system: str, tools: list[Any]) -> LLMResponse:
        entry = self._script[min(self.turns, len(self._script) - 1)]
        self.turns += 1
        return LLMResponse(text=entry, tool_calls=[])


async def test_one_correction_turn_reports_two_attempts() -> None:
    llm = _ScriptedAnswers(_BROKEN_FINAL_ANSWER, _FIXED_FINAL_ANSWER)
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="write your assigned brief",
        tool_specs=[],
        dispatch=_no_dispatch,
        policy=_env(declared_output_keys=("posture", "headline")),
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert llm.turns == 2  # the correction earned exactly one more model turn
    assert result.attempts == 2


# ── case 3: two transient tool retries then success spends exactly two recoveries -----------------
# Same fixtures test_tool_call_retry_and_fail_fast.py uses for the retry-then-succeed behaviour.

_SPEC = ToolSpec(
    name="web__search",
    description="search the web",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": []},
    binding="web",
    operation="search",
)


class _ToolCallingLLM:
    """Calls the tool once, then answers with whatever the loop fed back."""

    protocol_shape = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: Any, system: str, tools: list[Any]) -> LLMResponse:
        self.calls += 1
        observed = [m for m in messages if m.get("role") == "tool"]
        if not observed and tools:
            return LLMResponse(text="", tool_calls=[ToolCall("c1", tools[0].name, {})])
        last = observed[-1]["content"] if observed else "none"
        return LLMResponse(text=f"observed: {last}")


def _registry_error(*, error_code: str, transient: bool, message: str) -> Exception:
    err = RegistryError(message)
    err.error_code = error_code  # type: ignore[attr-defined]
    err.transient = transient  # type: ignore[attr-defined]
    return err


class _FlakyDispatch:
    """Raises a curated TRANSIENT tool error ``fail_n`` times, then succeeds."""

    def __init__(self, fail_n: int) -> None:
        self.calls = 0
        self._fail_n = fail_n

    async def __call__(self, spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self._fail_n:
            raise _registry_error(
                error_code="PROVIDER_RATE_LIMITED", transient=True, message="the call failed"
            )
        return {"results": ["ok"]}


async def test_two_transient_tool_retries_then_success_reports_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_use, "_async_sleep", _no_sleep)
    dispatch = _FlakyDispatch(fail_n=2)  # well within the default _LLM_MAX_RETRIES bound (4)
    llm = _ToolCallingLLM()

    result = await run_tool_use_loop(
        llm=llm, system="", user_input="go", tool_specs=[_SPEC], dispatch=dispatch, policy=_env()
    )

    assert result.status is HarnessStatus.SUCCEEDED
    assert dispatch.calls == 3  # the initial try + 2 retries before it cleared
    assert result.attempts == 3


# ── case 4: a quota fail-fast spends no retry — the first refused call is the only attempt --------


class _AlwaysFailingDispatch:
    def __init__(self, *, error_code: str, message: str) -> None:
        self.calls = 0
        self._error_code = error_code
        self._message = message

    async def __call__(self, spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise _registry_error(error_code=self._error_code, transient=False, message=self._message)


async def test_a_quota_fail_fast_reports_one_attempt() -> None:
    dispatch = _AlwaysFailingDispatch(
        error_code="PROVIDER_QUOTA_EXHAUSTED", message="the monthly search quota is spent"
    )
    result = await run_tool_use_loop(
        llm=_ToolCallingLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=dispatch,
        policy=_env(),
    )

    assert result.status is HarnessStatus.FAILED
    assert dispatch.calls == 1  # no retry
    assert result.attempts == 1


# ── HarnessExecutionService.execute() persists result.attempts onto the created row ---------------
# Mirrors test_fetched_urls_service_threading.py's own shape for a new, additive LoopResult field:
# run_tool_use_loop is monkeypatched to a capturing stub (its own arithmetic is T1/T2/T3 above,
# not this section's concern) — this proves the SERVICE persists whatever attempts value the loop
# hands back, which is the exact row HarnessExecutionOut turns into the engine's response JSON.

_ORG = uuid.uuid4()
_USER = uuid.uuid4()

_DESCRIPTOR = {
    "id": "cap-1",
    "metadata": {"name": "Echo"},
    "spec": {"capabilities": [{"name": "run", "description": "Echo back", "parameters": {}}]},
}


def _principal() -> Any:
    from oraclous_governance import Principal, PrincipalType

    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _manifest() -> dict[str, Any]:
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
            "name": "Attempt Count Demo",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": [{"ref": "core/echo@1.0.0", "binding": "echo"}],
        "prompts": [{"role": "primary", "source": "inline", "body": "You are helpful."}],
        "runtime": {"entrypoint": "echo"},
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
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None

    async def create(self, **fields: Any) -> Any:
        from types import SimpleNamespace

        self.created = fields
        return SimpleNamespace(id=fields["execution_id"], **fields)


class _FakeProv:
    async def emit(self, record: Any) -> None:
        pass


def _service(executions: Any) -> Any:
    from oraclous_harness_runtime_service.services.harness_execution_service import (
        HarnessExecutionService,
    )
    from oraclous_ohm.signatures import TrustStore

    return HarnessExecutionService(
        registry=_FakeRegistry(),
        broker=None,
        executions=executions,
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
    )


def _stub_loop(monkeypatch: pytest.MonkeyPatch, *, attempts: int) -> None:
    from types import SimpleNamespace

    from oraclous_harness_runtime_service.models.enums import HarnessStatus as _Status

    async def fake_run_tool_use_loop(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            status=_Status.SUCCEEDED,
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
            attempts=attempts,
        )

    import oraclous_harness_runtime_service.services.harness_execution_service as svc_mod

    monkeypatch.setattr(svc_mod, "run_tool_use_loop", fake_run_tool_use_loop)


async def test_execute_persists_the_loops_attempt_count_onto_the_created_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loop(monkeypatch, attempts=3)
    execs = _FakeExecutions()
    svc = _service(execs)
    await svc.execute(
        manifest_inline=_manifest(), manifest_ref=None, user_input="go", principal=_principal()
    )
    assert execs.created is not None
    # today: KeyError — execute() never reads `.attempts` off the loop's result, so it is never
    # passed to `self._executions.create(...)`, which is what HarnessExecutionOut is built from.
    assert execs.created["attempts"] == 3
