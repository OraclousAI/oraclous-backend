"""#580 — a retrieval that finds NO DATA degrades the member (flagged PARTIAL), it does not crash.

The knowledge-retriever connector flags an empty result ``data_absent`` (data-absence, not error).
The tool-use loop swaps in a clear "no data, proceeding" note — so the model stops looping on the
empty result — and, when the member completes, finishes as a flagged ``HarnessStatus.PARTIAL``
(reason ``empty_retrieval``) via #587's ``_degrade``: never a silent SUCCEEDED (ADR-021
never-silently), non-cascading (a partial member doesn't fail the team). A genuinely-BROKEN tool (a
raised error) is UNCHANGED — fed back to the model as today, never mistaken for data-absence.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

_SPEC = ToolSpec(
    name="kr__search",
    description="search the knowledge graph",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="knowledge-retriever",
    operation="search",
)


async def _empty_dispatch(spec: ToolSpec, args: dict) -> dict:
    return {"hits": [], "data_absent": True}  # the connector's data-absence signal


async def _nonempty_dispatch(spec: ToolSpec, args: dict) -> dict:
    return {"hits": [{"id": "n1"}]}


async def _broken_dispatch(spec: ToolSpec, args: dict) -> dict:
    raise RuntimeError("retriever exploded")  # broken-system, NOT data-absence


class _RetrieveThenAnswer:
    """Turn 1: call the tool. Turn 2+: answer (no tool call)."""

    protocol_shape = "fake"

    def __init__(self) -> None:
        self._calls = 0
        self.seen_tool_content: list[str] = []

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self._calls += 1
        for m in messages:
            if m.get("role") == "tool":
                self.seen_tool_content.append(str(m.get("content", "")))
        if self._calls == 1:
            return LLMResponse(text="searching", tool_calls=[ToolCall("c1", tools[0].name, {})])
        return LLMResponse(text="here is my best-effort answer", tool_calls=[])


class _AlwaysRetrieve:
    """Never converges — calls the tool every turn (forces the iteration cap)."""

    protocol_shape = "fake"

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        return LLMResponse(text="still searching", tool_calls=[ToolCall("c1", tools[0].name, {})])


def _env(on_exhaustion: str | None = None, **caps: Any) -> PolicyEnvelope:
    kw: dict[str, Any] = {
        "max_iterations": caps.get("max_iterations", 6),
        "max_tool_calls": caps.get("max_tool_calls"),
        "max_wall_time_seconds": caps.get("max_wall"),
        "max_tokens": caps.get("max_tokens"),
    }
    if on_exhaustion is not None:
        kw["on_exhaustion"] = on_exhaustion
    return PolicyEnvelope(**kw)


async def _run(llm: Any, dispatch: Any, **caps: Any) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=dispatch,
        policy=_env(**caps),
    )


async def test_empty_retrieval_completes_partial_not_succeeded() -> None:
    result = await _run(_RetrieveThenAnswer(), _empty_dispatch)
    assert (
        result.status is HarnessStatus.PARTIAL
    )  # data-absence → degrade, never a silent SUCCEEDED
    assert result.error_type == "empty_retrieval"
    assert result.checkpoint is None  # degrade FINISHES (not a resumable pause)
    assert result.output == "here is my best-effort answer"  # the member proceeded with what it had


async def test_empty_retrieval_feeds_the_model_a_proceed_note() -> None:
    # the model sees a clear "no data, proceed" note — NOT a raw empty result it would loop on.
    llm = _RetrieveThenAnswer()
    await _run(llm, _empty_dispatch)
    assert any("No data was found" in c for c in llm.seen_tool_content)
    assert all(
        "data_absent" not in c for c in llm.seen_tool_content
    )  # the private flag is stripped


async def test_nonempty_retrieval_succeeds_unchanged() -> None:
    result = await _run(_RetrieveThenAnswer(), _nonempty_dispatch)
    assert result.status is HarnessStatus.SUCCEEDED  # real data → normal success, no degrade
    assert result.error_type is None


async def test_empty_retrieval_churn_degrades_not_escalates() -> None:
    # the model ignores the note + keeps retrying empty → iteration cap. With the DEFAULT policy
    # (escalate) #580 STILL degrades — the churn was data-absence, never a hard fail (#440).
    result = await _run(_AlwaysRetrieve(), _empty_dispatch, max_iterations=3)
    assert result.status is HarnessStatus.PARTIAL
    assert result.error_type == "empty_retrieval"


async def test_broken_retrieval_is_not_a_degrade() -> None:
    # a RAISED tool error is broken-system, not data-absence — fed back as today (the model recovers
    # and answers → SUCCEEDED); the member is NEVER flagged empty_retrieval for a real failure.
    result = await _run(_RetrieveThenAnswer(), _broken_dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.error_type is None
