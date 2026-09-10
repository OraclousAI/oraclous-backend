"""#993/#994 → #996 ruling: the declared-key shape guarantee is UNWRAP ONLY.

Live evidence, app ``c3462066-bcf3-4756-8bdf-cbc9d21873d0`` ("Daily AI News Digest"): run
``57eb8029`` shipped ``{"linked_summary": {"summary": [...], "artifact_refs": []}}``; run
``b9c54e61`` shipped ``{"linked_summary": [...], "artifact_refs": []}`` for the SAME team. The
console can read one shape, never both — so the platform guarantees one, on the way out:

1. A wrapper of the vacuous shape ``{"summary": v}`` / ``{"summary": v, "artifact_refs": []}``
   (every OTHER key empty-list/None) is UNWRAPPED to ``v`` — silently, no correction turn spent.
2. A bare string or list of strings ships unchanged.
3. ``artifact_refs`` is exempt (a bookkeeping list, never text) and always stays as declared.

**#994 regression, superseded here.** #994 additionally spent ONE bounded correction turn on
anything still not a string/list-of-strings, then forced a JSON-string fallback past that budget.
Live run ``8c0d0bc7-c659-4379-a567-eebc8c28fa90``: the researcher's ``articles`` key is legitimately
a list of RECORDS, so the correction fired on every run; when the model answered the correction with
a top-level JSON array, ``_extract_answer_object`` returned ``{}`` (an array is not an object), the
forced fallback never ran (it only lives on the correction/budget path), and the loop shipped
SUCCEEDED with no ``articles`` key at all. Re-ruled 2026-09-10: the guarantee never touches a
non-wrapper shape at all — a list of records, a dict with real data, ships UNTOUCHED, no correction
turn, no extra LLM call. Structured keys are legitimate for downstream members; the console shows
"not plain text" for them. The pre-existing #697 rule (a missing required key fails the member) is
unchanged.

Tests superseded by this ruling and rewritten below (no longer pin the correction turn / JSON-string
fallback):
- ``test_a_genuinely_nested_answer_gets_one_correction_then_ships_as_text``
- ``test_the_correction_fixes_it_and_the_run_ships_clean``

RED until the [impl] removes the correction turn from ``run_tool_use_loop`` (``tool_use.py``).
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


async def test_a_genuinely_nested_answer_ships_untouched_no_correction_spent() -> None:
    # #994 regression, re-ruled: real extra data beside `summary` (`confidence`) — not the vacuous
    # {}/[]/None wrapper — is NOT the guarantee's business any more. It ships exactly as the member
    # wrote it: no correction turn, no second LLM call, no JSON-string coercion.
    answer = json.dumps({"linked_summary": {"summary": "x", "confidence": 0.9}})
    llm = _Scripted(answer)
    result = await _run(llm, policy=_envelope(declared_output_keys=("linked_summary",)))
    assert result.status is HarnessStatus.SUCCEEDED
    assert llm.turns == 1  # never re-prompted
    assert _shape_gate_steps(result) == []
    shipped = json.loads(result.output or "")
    assert shipped == {"linked_summary": {"summary": "x", "confidence": 0.9}}


async def test_a_list_of_records_ships_untouched_no_correction_spent() -> None:
    # The live regression itself (run 8c0d0bc7-c659-4379-a567-eebc8c28fa90, "Daily AI News Digest"):
    # a researcher's `articles` key legitimately holds a list of RECORDS, never text. Before #994 it
    # shipped fine; #994 spent a correction turn on it every run and the fix is to never do that.
    # Deliberately no `http(s)://` field: the link-provenance gate (#944/#975) strips a raw URL the
    # run never fetched, which would confound this test with a DIFFERENT gate's behaviour.
    articles = [
        {"title": "A", "id": "article-1"},
        {"title": "B", "id": "article-2"},
    ]
    answer = json.dumps({"articles": articles})
    llm = _Scripted(answer)
    result = await _run(llm, policy=_envelope(declared_output_keys=("articles",)))
    assert result.status is HarnessStatus.SUCCEEDED
    assert llm.turns == 1  # never re-prompted — the #994 bug required a second turn to trigger
    assert _shape_gate_steps(result) == []
    assert json.loads(result.output or "")["articles"] == articles


async def test_a_bare_top_level_json_array_answer_ships_as_is() -> None:
    # #994 regression ruling 4: `_extract_answer_object` returns `{}` for text that is not a JSON
    # OBJECT at all (a bare array has no `{`). That must never discard or blank a previously parsed
    # answer — the pre-#994 behaviour (no declared-key handling existed at all) shipped the raw text
    # untouched, and that is exactly what must still happen: never a silently key-less SUCCEEDED
    # manufactured by the guarantee itself.
    bare_array = json.dumps(["line one", "line two"])
    llm = _Scripted(bare_array)
    result = await _run(llm, policy=_envelope(declared_output_keys=("articles",)))
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == bare_array
    assert _shape_gate_steps(result) == []


async def test_a_member_with_no_declared_keys_is_never_checked() -> None:
    # Back-compat: a pre-#993 member (or one that declared nothing) is byte-for-byte unchanged.
    answer = json.dumps({"linked_summary": {"summary": "x", "confidence": 0.9}})
    llm = _Scripted(answer)
    result = await _run(llm, policy=_envelope())
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == answer
    assert _shape_gate_steps(result) == []
