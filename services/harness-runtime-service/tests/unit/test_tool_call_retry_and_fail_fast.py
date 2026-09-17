"""#1111 items 2/3 — the tool-use loop retries a transient tool failure and fails fast on a
curated non-transient one, instead of always feeding every tool error back to the model as prose.

Decision 2 (posted on #1111): tool errors are classified from the registry's curated token — a
transient one (429/5xx/timeout/connection-reset — pinned onto ``RegistryError.transient`` by
2dd6daed's ``test_tool_dispatch_curated_token.py``) is retried, inside the loop, with the SAME
backoff helper and the SAME bound the loop already uses for a transient LLM-call error
(``tool_use._LLM_MAX_RETRIES`` — ADR-042 #551) — before the model ever sees an error. A
non-transient curated failure (``PROVIDER_QUOTA_EXHAUSTED`` / ``PROVIDER_AUTH_FAILED``) fails the
member at once: no retry, no further model turn. Decision 3 assigns the member-facing tokens
``tool_quota_exhausted`` / ``tool_credential_rejected`` (parallel to #1108's
``llm_credential_rejected``) for those two cases.

Today, ~:1826-1834 of ``tool_use.py`` catches ANY tool-dispatch exception, feeds its
``str(exc)`` straight back to the model, and moves on — no classification, no retry, no fail-fast.
This file pins the loop's OWN behaviour end to end (``run_tool_use_loop``, a scripted LLM + an
in-process ``dispatch`` stub, no registry/network — same shape as ``test_tool_use_loop.py``'s
ADR-042 retry tests). It never pins a private helper name, only what a caller of the loop can
observe: how many times ``dispatch`` and ``llm.complete`` were called, and the run's final
status/error_type/error_message. RED until the [impl] lands.

Out of scope here (already pinned elsewhere, not duplicated):
* whether ``RegistryError`` carries ``error_code``/``transient`` at all — 2dd6daed's
  ``test_tool_dispatch_curated_token.py``, against ``HarnessExecutionService``'s dispatch closure.
* the LLM-side ``llm_credential_rejected`` classification (401/403) — already merged on ``main``
  (#1108, commit f5962f4f, ``test_tool_use_loop.py``'s
  ``test_credential_rejected_status_is_classified_as_llm_credential_rejected``); this file never
  re-tests it.
"""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from oraclous_harness_runtime_service.domain.loop import tool_use
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind
from oraclous_harness_runtime_service.services.registry_client import RegistryError

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]


async def _no_sleep(_seconds: float) -> None:
    """A no-op stand-in for asyncio.sleep, matching test_tool_use_loop.py's own retry tests —
    the tool-retry bound is exercised for real, with no wall-clock cost."""


def _env() -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
    )


_SPEC = ToolSpec(
    name="web__search",
    description="search the web",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": []},
    binding="web",
    operation="search",
)


class _ScriptedLLM:
    """Calls the tool once, then answers with whatever the loop fed back — same shape as
    ``test_tool_use_loop.py``'s ``_ScriptedLLM``. ``calls`` counts every model turn, so a caller
    can tell an internal dispatch retry from an actual extra turn."""

    protocol_shape = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        self.calls += 1
        observed = [m for m in messages if m.get("role") == "tool"]
        if not observed and tools:
            return LLMResponse(text="", tool_calls=[ToolCall("c1", tools[0].name, {})])
        last = observed[-1]["content"] if observed else "none"
        return LLMResponse(text=f"observed: {last}")


def _registry_error(*, error_code: str, transient: bool, message: str) -> Exception:
    """The exact exception shape 2dd6daed pins ``dispatch()`` to raise: a ``RegistryError``
    carrying the registry's curated ``error_code`` and its ``transient`` classification. Built by
    setting the attributes directly (rather than through the constructor) so this file's tests
    exercise the LOOP's retry/fail-fast behaviour without also depending on
    ``RegistryError.__init__`` accepting a ``transient`` keyword — a separate, already-pinned
    concern (2dd6daed) that lands in the same [impl]."""
    err = RegistryError(message)
    err.error_code = error_code  # type: ignore[attr-defined]
    err.transient = transient  # type: ignore[attr-defined]
    return err


class _FlakyDispatch:
    """Raises a curated TRANSIENT tool error ``fail_n`` times, then succeeds — models a
    rate-limited search connector clearing on retry."""

    def __init__(self, fail_n: int, *, error_code: str = "PROVIDER_RATE_LIMITED") -> None:
        self.calls = 0
        self._fail_n = fail_n
        self._error_code = error_code

    async def __call__(self, spec: ToolSpec, args: dict) -> dict:
        self.calls += 1
        if self.calls <= self._fail_n:
            raise _registry_error(
                error_code=self._error_code, transient=True, message="the tool call failed"
            )
        return {"results": ["ok"]}


# ── a transient tool failure is retried inside the loop, before the model ever sees it ──────────


async def test_transient_tool_failure_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_use, "_async_sleep", _no_sleep)
    # clears exactly at the existing LLM retry bound — the SAME bound this decision reuses.
    dispatch = _FlakyDispatch(fail_n=tool_use._LLM_MAX_RETRIES)
    llm = _ScriptedLLM()

    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=dispatch,
        policy=_env(),
    )

    assert result.status is HarnessStatus.SUCCEEDED
    assert dispatch.calls == tool_use._LLM_MAX_RETRIES + 1  # the initial try + bounded retries
    # the model was called exactly twice: once to issue the call, once to receive its result — the
    # retries happened entirely inside the loop, never as extra model turns.
    assert llm.calls == 2
    assert "ok" in (result.output or "")


async def test_a_recovered_tool_call_is_fed_back_as_an_ordinary_result_not_an_error() -> None:
    dispatch = _FlakyDispatch(fail_n=1)
    llm = _ScriptedLLM()

    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=dispatch,
        policy=_env(),
    )

    assert result.status is HarnessStatus.SUCCEEDED
    assert "error" not in (result.output or "").lower()
    assert any(s.status == "ok" for s in result.steps if s.kind is StepKind.TOOL)
    assert not any(s.status == "error" for s in result.steps if s.kind is StepKind.TOOL)


async def test_transient_tool_retries_are_bounded_then_todays_behaviour_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_use, "_async_sleep", _no_sleep)
    dispatch = _FlakyDispatch(fail_n=999)  # never clears
    llm = _ScriptedLLM()

    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=dispatch,
        policy=_env(),
    )

    # the bound is exhausted, but a tool failure never hard-fails the run by itself — today's
    # behaviour (fed back to the model, which can still answer) continues past the bound.
    assert result.status is HarnessStatus.SUCCEEDED
    assert dispatch.calls == tool_use._LLM_MAX_RETRIES + 1
    assert llm.calls == 2
    assert "observed" in (result.output or "")


# ── a curated, non-transient tool failure fails the member at once ──────────────────────────────


class _AlwaysFailingDispatch:
    def __init__(self, *, error_code: str, message: str) -> None:
        self.calls = 0
        self._error_code = error_code
        self._message = message

    async def __call__(self, spec: ToolSpec, args: dict) -> dict:
        self.calls += 1
        raise _registry_error(error_code=self._error_code, transient=False, message=self._message)


async def test_a_spent_quota_fails_the_member_on_the_first_refused_call() -> None:
    dispatch = _AlwaysFailingDispatch(
        error_code="PROVIDER_QUOTA_EXHAUSTED",
        message="customer-account-987 has exceeded its monthly search quota",
    )
    llm = _ScriptedLLM()

    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=dispatch,
        policy=_env(),
    )

    assert result.status is HarnessStatus.FAILED
    assert dispatch.calls == 1  # no retry
    assert llm.calls == 1  # no further model turn — the model was never asked again
    assert result.error_type == "tool_quota_exhausted"
    assert result.error_message is not None
    assert "quota" in result.error_message.lower()
    assert "web.search" in result.error_message  # names the failing tool (binding.operation)
    # the raw provider/customer text is never echoed into the member-facing message.
    assert "customer-account-987" not in result.error_message


async def test_a_rejected_tool_credential_fails_the_member_on_the_first_refused_call() -> None:
    dispatch = _AlwaysFailingDispatch(
        error_code="PROVIDER_AUTH_FAILED",
        message="401 from vendor: api key sk-live-abcdef123456 is invalid",
    )
    llm = _ScriptedLLM()

    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=dispatch,
        policy=_env(),
    )

    assert result.status is HarnessStatus.FAILED
    assert dispatch.calls == 1
    assert llm.calls == 1
    assert result.error_type == "tool_credential_rejected"
    assert result.error_message is not None
    assert "credential" in result.error_message.lower()
    assert "web.search" in result.error_message  # names the tool whose credential was rejected
    assert "sk-live-abcdef123456" not in result.error_message


@pytest.mark.parametrize("error_code", ["PROVIDER_QUOTA_EXHAUSTED", "PROVIDER_AUTH_FAILED"])
async def test_a_curated_non_transient_failure_is_never_retried(error_code: str) -> None:
    # regression guard distinguishing the two branches of decision 2: only a TRANSIENT curated
    # token is retried — a non-transient one (however it is spelled) is fail-fast from the start.
    dispatch = _AlwaysFailingDispatch(error_code=error_code, message="refused")
    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=dispatch,
        policy=_env(),
    )
    assert dispatch.calls == 1
    assert result.status is HarnessStatus.FAILED


# ── an ordinary, non-curated tool error keeps today's behaviour exactly ─────────────────────────


async def test_an_uncurated_tool_error_is_still_fed_back_with_no_fail_fast() -> None:
    class _BoomDispatch:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, spec: ToolSpec, args: dict) -> dict:
            self.calls += 1
            raise RuntimeError("connection refused")  # no error_code, no transient attribute

    dispatch = _BoomDispatch()
    llm = _ScriptedLLM()

    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=dispatch,
        policy=_env(),
    )

    # unchanged from today: no retry (a plain error is not transient), no fail-fast (the model
    # still gets a turn and can answer having observed the failure).
    assert result.status is HarnessStatus.SUCCEEDED
    assert dispatch.calls == 1
    assert llm.calls == 2
    assert "connection refused" in (result.output or "")
