"""#993 — a declared output key holds the member's answer DIRECTLY as a string or a list of
strings, never a nested object (Contract ruling, owner 2026-09-10, made in the issue's own terms).

Live evidence, app ``c3462066-bcf3-4756-8bdf-cbc9d21873d0`` ("Daily AI News Digest"): run
``57eb8029`` shipped ``{"linked_summary": {"summary": [...], "artifact_refs": []}}``; run
``b9c54e61`` shipped ``{"linked_summary": [...], "artifact_refs": []}`` for the SAME team. The
console can read one shape, never both — so the platform now guarantees one, on the way out:

1. A wrapper of the vacuous shape ``{"summary": v}`` / ``{"summary": v, "artifact_refs": []}``
   (every OTHER key empty-list/None) is UNWRAPPED to ``v`` — silently, no correction turn spent.
2. A bare string or list of strings ships unchanged.
3. Anything else (a nested object carrying REAL extra data, a number, a list of non-strings) earns
   ONE bounded correction turn; if the model repeats the offence, it ships JSON-serialised as a
   string, so the declared key ALWAYS reads as text — never dropped, never nested.
4. ``artifact_refs`` is exempt (a bookkeeping list, never text) and always stays as declared.

RED until the [impl] adds this shape guarantee to ``run_tool_use_loop`` (``tool_use.py``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = pytest.mark.unit

_GATE_NAME = "declared_output_shape"


def _envelope(*, declared_output_keys: tuple[str, ...] = ()) -> PolicyEnvelope:
    # a function, never a module-level constant: `declared_output_keys` does not exist on
    # `PolicyEnvelope` until the [impl] lands (the #853 `test_json_repair_gate.py` convention).
    return PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
        declared_output_keys=declared_output_keys,
    )


class _Scripted:
    """One scripted final answer per model turn — no tool calls, ever (a tool-less `linker`)."""

    protocol_shape = "fake"

    def __init__(self, *script: str) -> None:
        self._script = list(script)
        self.turns = 0

    async def complete(self, *, messages: Any, system: str, tools: list[Any]) -> LLMResponse:
        entry = self._script[min(self.turns, len(self._script) - 1)]
        self.turns += 1
        return LLMResponse(text=entry, tool_calls=[])


async def _dispatch(_spec: Any, _args: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("a tool-less member never dispatches")


async def _run(llm: Any, *, policy: PolicyEnvelope) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="write your linked summary",
        tool_specs=[],
        dispatch=_dispatch,
        policy=policy,
    )


def _shape_gate_steps(result: Any) -> list[Any]:
    return [s for s in result.steps if s.name == _GATE_NAME]


def _corrections(result: Any) -> list[Any]:
    return [s for s in _shape_gate_steps(result) if s.status == "declared_output_correction"]


async def test_a_vacuous_summary_wrapper_unwraps_to_the_bare_list() -> None:
    answer = json.dumps(
        {"linked_summary": {"summary": ["line one", "line two"], "artifact_refs": []}}
    )
    llm = _Scripted(answer)
    result = await _run(
        llm, policy=_envelope(declared_output_keys=("linked_summary", "artifact_refs"))
    )
    assert result.status is HarnessStatus.SUCCEEDED
    shipped = json.loads(result.output or "")
    assert shipped["linked_summary"] == ["line one", "line two"]
    assert _shape_gate_steps(result) == []  # the unwrap is silent — no correction turn spent


async def test_a_wrapper_with_empty_artifact_refs_beside_it_also_unwraps() -> None:
    answer = json.dumps({"summary": {"summary": "the whole answer", "artifact_refs": []}})
    llm = _Scripted(answer)
    result = await _run(llm, policy=_envelope(declared_output_keys=("summary",)))
    assert result.status is HarnessStatus.SUCCEEDED
    assert json.loads(result.output or "")["summary"] == "the whole answer"
    assert _shape_gate_steps(result) == []


async def test_a_bare_list_ships_unchanged() -> None:
    answer = json.dumps({"linked_summary": ["a", "b"], "artifact_refs": []})
    llm = _Scripted(answer)
    result = await _run(
        llm, policy=_envelope(declared_output_keys=("linked_summary", "artifact_refs"))
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert json.loads(result.output or "") == {"linked_summary": ["a", "b"], "artifact_refs": []}
    assert _shape_gate_steps(result) == []


async def test_a_bare_string_ships_unchanged() -> None:
    answer = json.dumps({"summary": "the whole answer, plainly"})
    llm = _Scripted(answer)
    result = await _run(llm, policy=_envelope(declared_output_keys=("summary",)))
    assert result.status is HarnessStatus.SUCCEEDED
    assert json.loads(result.output or "") == {"summary": "the whole answer, plainly"}
    assert _shape_gate_steps(result) == []


async def test_artifact_refs_is_exempt_and_stays_top_level() -> None:
    answer = json.dumps({"summary": "text", "artifact_refs": ["doc:1", "doc:2"]})
    llm = _Scripted(answer)
    result = await _run(llm, policy=_envelope(declared_output_keys=("summary", "artifact_refs")))
    assert result.status is HarnessStatus.SUCCEEDED
    shipped = json.loads(result.output or "")
    # a list of REFS, not strings — never coerced to text, never dropped
    assert shipped["artifact_refs"] == ["doc:1", "doc:2"]


async def test_a_genuinely_nested_answer_gets_one_correction_then_ships_as_text() -> None:
    # Real extra data beside `summary` (`confidence`) — not the vacuous {}/[]/None wrapper — so it
    # cannot be silently unwrapped without losing data. The model repeats the offence verbatim, so
    # past the one-turn budget it ships JSON-serialised as a string rather than being dropped.
    bad = json.dumps({"linked_summary": {"summary": "x", "confidence": 0.9}})
    llm = _Scripted(bad, bad)
    result = await _run(llm, policy=_envelope(declared_output_keys=("linked_summary",)))
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_corrections(result)) == 1  # exactly one correction turn spent, never a second
    shipped = json.loads(result.output or "")
    assert isinstance(shipped["linked_summary"], str)
    # the key reads as text, and the original data survives, readable inside it
    assert json.loads(shipped["linked_summary"]) == {"summary": "x", "confidence": 0.9}


async def test_the_correction_fixes_it_and_the_run_ships_clean() -> None:
    bad = json.dumps({"linked_summary": {"summary": "x", "confidence": 0.9}})
    fixed = json.dumps({"linked_summary": "x"})
    llm = _Scripted(bad, fixed)
    result = await _run(llm, policy=_envelope(declared_output_keys=("linked_summary",)))
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_corrections(result)) == 1
    assert json.loads(result.output or "") == {"linked_summary": "x"}


async def test_a_member_with_no_declared_keys_is_never_checked() -> None:
    # Back-compat: a pre-#993 member (or one that declared nothing) is byte-for-byte unchanged.
    answer = json.dumps({"linked_summary": {"summary": "x", "confidence": 0.9}})
    llm = _Scripted(answer)
    result = await _run(llm, policy=_envelope())
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == answer
    assert _shape_gate_steps(result) == []
