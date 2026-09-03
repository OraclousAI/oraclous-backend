"""#899 — an unknown tool name must tell the model what the right name would have been.

The loop already fails closed on a name it does not recognise: the call is never dispatched and the
model is handed ``{"error": "unknown_tool", "detail": <the wrong name>}``. That verdict is not
actionable. It gives the model back its own mistake and nothing else, so the only move left is to
guess again — which is #692/#693 one layer along, where a member told "409" repeated the failing
call until its budget ran out.

These tests pin the hint, not the wording. What must hold is that a near miss comes back with the
real name, that a name with no near miss comes back with a BOUNDED sample of what does exist, and
that neither addition weakens the fail-closed rule or escapes redaction.

Pure unit: a scripted LLM + an in-memory dispatch, no registry and no network.
"""

from __future__ import annotations

import json

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]


def _env(*, redact: tuple[str, ...] = ()) -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
        gated_bindings=frozenset(),
        tool_ceiling=frozenset(),
        redact_patterns=redact,
    )


def _spec(name: str) -> ToolSpec:
    binding, _, operation = name.partition("__")
    return ToolSpec(
        name=name,
        description=f"the {operation} operation",
        parameters={"type": "object", "properties": {}, "required": []},
        binding=binding,
        operation=operation or name,
    )


class _CallsThenAnswers:
    """Calls ``wrong_name`` once, then answers — so the loop's reply to the bad call is observable
    in the transcript the second turn receives."""

    protocol_shape = "fake"

    def __init__(self, wrong_name: str) -> None:
        self._wrong = wrong_name
        self.observed: list[str] = []

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        replies = [m for m in messages if m.get("role") == "tool"]
        if not replies:
            return LLMResponse(text="", tool_calls=[ToolCall("c1", self._wrong, {})])
        self.observed = [str(m.get("content") or "") for m in replies]
        return LLMResponse(text="done")


async def _ok_dispatch(spec: ToolSpec, args: dict) -> dict:  # noqa: ANN401, ARG001
    return {"rows": []}


def _unknown_payload(content: str) -> dict:
    """The loop appends a receipt line after the JSON body; peel it back off."""
    return json.loads(content.split("\n[receipt:")[0])


async def test_a_near_miss_is_told_the_real_name() -> None:
    # one transposed character: the shape a weak model actually produces, and the whole reason
    # this issue exists.
    llm = _CallsThenAnswers("registry__list_tables")
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_spec("registry__list_tabels"), _spec("graph__ingest")],
        dispatch=_ok_dispatch,
        policy=_env(),
    )

    assert result.status is HarnessStatus.SUCCEEDED
    payload = _unknown_payload(llm.observed[0])
    assert payload["error"] == "unknown_tool"
    assert payload["detail"] == "registry__list_tables"  # the wrong name is still reported
    assert "registry__list_tabels" in payload["did_you_mean"]
    # the suggestion is a SHORTLIST, not the catalogue relabelled
    assert len(payload["did_you_mean"]) <= 3


async def test_a_name_with_no_near_miss_gets_the_real_names_instead() -> None:
    # nothing close to "teleport" exists. A shortlist of near misses would be empty, and an empty
    # hint is the verdict this issue is replacing — so fall back to naming what DOES exist.
    llm = _CallsThenAnswers("teleport")
    await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_spec("registry__list_tables"), _spec("graph__ingest")],
        dispatch=_ok_dispatch,
        policy=_env(),
    )

    payload = _unknown_payload(llm.observed[0])
    assert payload["error"] == "unknown_tool"
    assert "did_you_mean" not in payload  # never a suggestion the model cannot use
    assert set(payload["available_tools"]) == {"registry__list_tables", "graph__ingest"}


async def test_the_fallback_list_is_bounded() -> None:
    """A busy organisation offers tens of tools, and this reply is written into the transcript on
    every failing turn. An unbounded list would grow the prompt once per iteration, for a member
    that is already failing — so the fallback names a sample and stops."""
    from oraclous_harness_runtime_service.domain.loop.tool_use import _MAX_LISTED_TOOLS

    specs = [_spec(f"pack{i:03d}__run") for i in range(_MAX_LISTED_TOOLS + 25)]
    llm = _CallsThenAnswers("teleport")
    await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=specs,
        dispatch=_ok_dispatch,
        policy=_env(),
    )

    listed = _unknown_payload(llm.observed[0])["available_tools"]
    assert len(listed) == _MAX_LISTED_TOOLS
    assert set(listed) <= {s.name for s in specs}  # never a name that does not exist


async def test_an_unknown_tool_is_still_never_dispatched() -> None:
    """The hint is added ALONGSIDE the fail-closed rule, never in place of it. A suggestion the
    model did not ask for must not become a call the platform makes on its behalf."""
    dispatched: list[str] = []

    async def _recording_dispatch(spec: ToolSpec, args: dict) -> dict:  # noqa: ANN401, ARG001
        dispatched.append(spec.name)
        return {"rows": []}

    llm = _CallsThenAnswers("registry__list_tables")
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_spec("registry__list_tabels")],
        dispatch=_recording_dispatch,
        policy=_env(),
    )

    assert dispatched == []
    assert result.status is HarnessStatus.SUCCEEDED
    assert any(
        s.kind is StepKind.TOOL and s.status == "error" and "unknown_tool" in (s.detail or "")
        for s in result.steps
    )


async def test_the_hint_is_redacted_like_every_other_tool_reply() -> None:
    """A tool name can carry a tenant's own words (an imported server names its tools). The hint
    puts real names into a message that is written to the transcript AND to the step trace, so it
    goes through the same redaction as every other tool result — CLAUDE.md §11."""
    llm = _CallsThenAnswers("customer__secret_report")
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_spec("customer__secret_reports")],
        dispatch=_ok_dispatch,
        policy=_env(redact=(r"secret_reports?",)),
    )

    assert "secret_report" not in llm.observed[0]
    assert all("secret_report" not in (s.detail or "") for s in result.steps)
