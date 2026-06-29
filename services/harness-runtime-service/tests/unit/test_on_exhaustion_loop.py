"""#587 — the tool-use loop DEGRADES (PARTIAL) vs ESCALATES at each budget breach, by on_exhaustion.

Today every budget breach (tool_call / token / wall / iteration) ``_escalate``s — pause/fail for a
human. #587 adds a sibling ``_degrade``: under ``on_exhaustion: degrade`` the loop finishes with
its best-effort ``last_text`` as a flagged ``HarnessStatus.PARTIAL`` (typed reason, NO checkpoint —
it finishes here, not a resumable pause). The escalate arm is unchanged (default = back-compat).
This is the SINGLE degrade primitive #580 reuses.

RED until the [impl] adds HarnessStatus.PARTIAL + the ``_degrade`` helper + the per-site branch on
``policy.on_exhaustion`` + ``PolicyEnvelope.on_exhaustion``.
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
    name="pg__list_tables",
    description="list tables",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="pg",
    operation="list_tables",
)


async def _ok_dispatch(spec: ToolSpec, args: dict) -> dict:
    return {"tables": ["a", "b"]}


class _LoopingLLM:
    """Emits best-effort text + a tool call each turn — never converges (forces a budget breach with
    a non-empty last_text to degrade with)."""

    protocol_shape = "fake"

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        return LLMResponse(
            text="best-effort progress", tool_calls=[ToolCall("c1", tools[0].name, {})]
        )


class _TokenLLM:
    """Reports token usage; answers with text (the token check pre-empts convergence)."""

    protocol_shape = "fake"

    def __init__(self, tokens: int) -> None:
        self._tokens = tokens

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        return LLMResponse(text="best-effort progress", tool_calls=[], total_tokens=self._tokens)


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


# the 4 budget breach sites + their typed reason
_SITES = [
    (_LoopingLLM(), {"max_tool_calls": 1}, "tool_call_budget"),
    (_TokenLLM(150), {"max_tokens": 100}, "token_budget"),
    (_LoopingLLM(), {"max_iterations": 3}, "iteration_cap"),
    (_LoopingLLM(), {"max_wall": 0}, "wall_time"),
]


def test_harness_status_has_partial() -> None:
    # PARTIAL is a distinct terminal (degrade), not ESCALATED/FAILED/SUCCEEDED.
    assert HarnessStatus.PARTIAL.value == "PARTIAL"
    assert HarnessStatus.PARTIAL not in (
        HarnessStatus.ESCALATED,
        HarnessStatus.FAILED,
        HarnessStatus.SUCCEEDED,
    )


@pytest.mark.parametrize("llm, caps, reason", _SITES)
async def test_breach_site_degrades_to_partial(llm: Any, caps: dict, reason: str) -> None:
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env("degrade", **caps),
    )
    assert result.status is HarnessStatus.PARTIAL  # degrade → a flagged partial, not a crash/pause
    assert result.error_type == reason  # the typed breach reason is preserved
    assert result.checkpoint is None  # degrade FINISHES — never a resumable HITL checkpoint
    assert result.output == "best-effort progress"  # the best-effort last_text is surfaced


@pytest.mark.parametrize("llm, caps, reason", _SITES)
async def test_breach_site_escalates_by_default(llm: Any, caps: dict, reason: str) -> None:
    # the default (no on_exhaustion) escalates EXACTLY as today — back-compat for every manifest.
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(**caps),
    )
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == reason
