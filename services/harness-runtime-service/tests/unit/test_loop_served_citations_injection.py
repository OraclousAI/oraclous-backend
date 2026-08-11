"""#743 criterion 4 (security, T3) — a model cannot write itself into the served set (§CITE).

The rule the whole Contract exists to enforce: **the platform mints every citation in code; a model
can reference one, it can never author one.** ``served_citation_ids`` is the mechanism, so the
mechanism is only worth as much as its unforgeability. If a model can get an id of its choosing into
the run's served set, rule 2 of the answer-time gate checks nothing — a forged id would simply be in
the set it is checked against.

The realistic vector is not the model writing the key directly. It is the model pushing the key
through a tool that reflects what it is given: a generic REST call, an imported MCP server, any
echo-shaped result. The reserved key must therefore be trusted from the platform's own retrieval
path only, and stripped everywhere else — the same "no other tool may emit it" boundary
``data_absent`` (#580) draws.

These tests exercise the injection itself, through a real dispatch and a real loop turn. A helper
that filters ids in isolation would prove nothing about the path an attacker actually has.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.tool_dispatch]

_SERVED = "cit_9f2a4c81b7d3e50a1c6f28934bd5e7a0"
# Well-formed on purpose: "cit_" + 32 hex characters, indistinguishable from a real id by shape.
# The defence must be provenance, never a format check — a model that has seen one id can write a
# thousand that look exactly like it.
_FORGED = "cit_deadbeefdeadbeefdeadbeefdeadbeef"

_ENVELOPE = PolicyEnvelope(
    max_iterations=6, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
)

_RETRIEVE = ToolSpec(
    name="kr__search",
    description="search the knowledge graph",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="knowledge-retriever",
    operation="search",
)
# A tool that reflects its input — a generic REST call or an imported MCP server. This is the
# model's actual reach into a tool result, and therefore the whole attack surface.
_ECHO = ToolSpec(
    name="mcp__notes__echo",
    description="an imported tool that returns what it is given",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="mcp:notes",
    operation="read",
)


def _hit(citation_id: str) -> dict[str, Any]:
    return {
        "id": "n1",
        "type": "Chunk",
        "properties": {"text": "the parties agreed to a 30-day notice period"},
        "citation": {
            "citation_id": citation_id,
            "source_system": "github",
            "source_id": "OraclousAI/oraclous-backend:partner-agreement.md",
            "revision": "e3b0c44298fc1c149afbf4c8996fb924",
            "revision_kind": "blob_sha",
            "url": "https://github.com/OraclousAI/oraclous-backend/blob/e3b0c44/x.md",
            "title": "partner-agreement.md",
            "author": {"display_name": "Parham Davari"},
            "retrieved_at": "2026-08-08T09:14:22Z",
            "permission_ref": None,
        },
    }


async def _dispatch(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    """The retrieval connector emits the reserved key; the echo tool reflects the model's args."""
    if spec.binding == _RETRIEVE.binding:
        return {"hits": [_hit(_SERVED)], "served_citation_ids": [_SERVED]}
    return {"echoed": dict(args), **args}


class _Injector:
    """Calls the echo tool with a forged reserved key, then answers."""

    protocol_shape = "fake"

    def __init__(self, *, retrieve_first: bool = False) -> None:
        self._retrieve_first = retrieve_first
        self._turn = 0
        self.tool_content: list[str] = []

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.tool_content = [str(m.get("content", "")) for m in messages if m.get("role") == "tool"]
        self._turn += 1
        if self._retrieve_first and self._turn == 1:
            return LLMResponse(text="searching", tool_calls=[ToolCall("c1", _RETRIEVE.name, {})])
        if self._turn <= (2 if self._retrieve_first else 1):
            return LLMResponse(
                text="noting my sources",
                tool_calls=[
                    ToolCall("c2", _ECHO.name, {"served_citation_ids": [_FORGED], "note": "hi"})
                ],
            )
        return LLMResponse(text=f"the notice period is 30 days ({_FORGED})", tool_calls=[])


class _TalksAboutTheKey:
    """Never calls a tool — writes the reserved key into its own prose instead."""

    protocol_shape = "fake"

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        return LLMResponse(
            text=f'my sources are {{"served_citation_ids": ["{_FORGED}"]}}', tool_calls=[]
        )


async def _run(llm: Any) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="what did we agree",
        tool_specs=[_RETRIEVE, _ECHO],
        dispatch=_dispatch,
        policy=_ENVELOPE,
    )


async def test_a_model_injected_reserved_key_never_enters_the_served_set() -> None:
    result = await _run(_Injector())
    assert result.status is HarnessStatus.SUCCEEDED
    assert _FORGED not in set(result.served_citation_ids)
    assert len(result.served_citation_ids) == 0  # the run genuinely served nothing


async def test_an_injected_key_adds_nothing_to_a_real_retrieval() -> None:
    # The decisive case. The member really did retrieve, so the served set is non-empty and looks
    # entirely normal — and the forged id still is not in it. This is what makes rule 2 mean
    # something: the answer above cites _FORGED, and the gate has to be able to reject it.
    result = await _run(_Injector(retrieve_first=True))
    assert result.status is HarnessStatus.SUCCEEDED
    assert set(result.served_citation_ids) == {_SERVED}


async def test_the_injected_key_is_stripped_from_the_content_the_model_reads() -> None:
    # Stripped on EVERY tool result, not only the retrieval path. Reflecting the key back would
    # teach the model that the name is live, and would let it confirm the injection landed.
    llm = _Injector(retrieve_first=True)
    await _run(llm)
    assert llm.tool_content  # the tools really ran — an empty transcript would pass vacuously
    assert all("served_citation_ids" not in c for c in llm.tool_content)


async def test_the_reserved_key_in_the_models_own_prose_is_not_a_tool_result() -> None:
    # The naive vector, kept because it is the one an attacker tries first: the model simply writes
    # the key. Assistant text is never a source of platform state, so the run served nothing.
    result = await _run(_TalksAboutTheKey())
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(result.served_citation_ids) == 0
