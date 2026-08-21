"""#853 — a member declared ``requires_valid_json`` gets ONE bounded repair turn on a malformed
``graph-ingest`` document, instead of losing the whole run to it.

The live failure: a five-member run's synthesising member wrote a 5,395-character decision brief
with one bracket missing; `JSON.parse` failed at character 2,540 and every section was lost, after
twenty to thirty minutes of real tool work by every member (run `e3a6af87-eefb-4b6e-8c1a-
d59639af9e35`, artifact `c15bc7c8-ccc1-4d40-baee-3f956c626d94`). Ruled 2026-08-21:

1. The declaration is `PolicyEnvelope.requires_valid_json` (threaded from `OHMMember.
   requires_valid_json` via `build_envelope` — see `test_requires_valid_json_envelope.py`).
2. The check runs in the loop, before the malformed call is ever dispatched to the (fire-and-forget,
   async) `graph-ingest` connector — catching it here is both the right layer and the only layer
   early enough to matter (the KGS ingest worker parses later, possibly after the run has settled).
3. The repair is granted ON TOP of the member's own tool-call budget, never charged to it — a member
   that already spent its whole budget on real work still gets the one repair call.

RED until the [impl] adds the pre-dispatch JSON check + one-time correction to
``run_tool_use_loop``'s ``_run_tool_calls`` (``tool_use.py``).
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

_JSON_REPAIR_STATUS = "json_repair"

_INGEST = ToolSpec(
    name="graph_ingest__ingest",
    description="ingest content into the knowledge graph",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="graph-ingest",
    operation="ingest",
)

# The issue's own defect, reproduced in miniature: a claims list closed, the next section object
# never closed. `json.loads` fails at char 20 here the same way it failed at char 2,540 there.
_BROKEN = '{"sections": [{"a": 1}], {"b": 2}]}'
_FIXED = '{"sections": [{"a": 1}, {"b": 2}]}'


def _envelope(*, requires_valid_json: bool = True, max_tool_calls: int | None = None) -> Any:
    # a function, never a module-level constant: `requires_valid_json` does not exist on
    # `PolicyEnvelope` until the [impl] lands, so a module-level construction would raise
    # `TypeError` at COLLECTION time and abort the whole test run, not just this file's tests.
    return PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=max_tool_calls,
        max_wall_time_seconds=None,
        max_tokens=None,
        requires_valid_json=requires_valid_json,
    )


class _Scripted:
    """One scripted entry per model turn. ``("ingest", content)`` calls graph-ingest with that
    content (``source_type: "json"`` unless a third element overrides it); any other value is a
    final answer with no tool calls."""

    protocol_shape = "fake"

    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.turns = 0
        # every role="user"/"tool" message content seen across the WHOLE run so far, in order —
        # accumulates turn over turn (the loop's own `messages` list already carries prior turns,
        # so this simply mirrors it); never grab `[-1]` off this, since the last entry by the run's
        # end is whatever the LATEST tool call returned, not necessarily a correction (#853 review).
        self.user_messages_seen: list[str] = []

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.user_messages_seen = [
            str(m.get("content", "")) for m in messages if m.get("role") in ("user", "tool")
        ]
        entry = self._script[min(self.turns, len(self._script) - 1)]
        self.turns += 1
        if isinstance(entry, tuple) and entry[0] == "ingest":
            content = entry[1]
            source_type = entry[2] if len(entry) > 2 else "json"
            call = ToolCall(
                f"c{self.turns}",
                _INGEST.name,
                {"graph_id": "g1", "content": content, "source_type": source_type},
            )
            return LLMResponse(text="ingesting", tool_calls=[call])
        return LLMResponse(text=entry, tool_calls=[])


def _tracked_dispatch() -> tuple[Any, list[dict[str, Any]]]:
    """A fake ``graph-ingest`` dispatch mirroring the real connector: fire-and-forget, always
    accepts whatever content it's handed (the KGS worker is what actually parses it, later)."""
    calls: list[dict[str, Any]] = []

    async def dispatch(_spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(args)
        return {"job_id": f"job{len(calls)}", "status": "queued"}

    return dispatch, calls


async def _run(llm: Any, dispatch: Any, *, policy: PolicyEnvelope | None = None) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="write the decision brief",
        tool_specs=[_INGEST],
        dispatch=dispatch,
        policy=policy if policy is not None else _envelope(),
    )


def _repairs(result: Any) -> list[Any]:
    return [s for s in result.steps if s.status == _JSON_REPAIR_STATUS]


# --- the issue's own scenario: one bracket wrong, one repair turn, the run survives -----------


async def test_a_malformed_document_gets_one_corrective_turn_and_then_succeeds() -> None:
    llm = _Scripted(("ingest", _BROKEN), ("ingest", _FIXED), "done")
    dispatch, calls = _tracked_dispatch()
    result = await _run(llm, dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_repairs(result)) == 1
    # the malformed attempt was never sent to the (async, fire-and-forget) connector at all
    assert len(calls) == 1
    assert calls[0]["content"] == _FIXED


async def test_the_correction_quotes_the_real_parser_error() -> None:
    llm = _Scripted(("ingest", _BROKEN), ("ingest", _FIXED), "done")
    dispatch, _calls = _tracked_dispatch()
    await _run(llm, dispatch)
    # the model must read the parser's OWN message (e.g. "Expecting ',' delimiter"), not a generic
    # "invalid JSON" — a vague message is what makes a one-shot repair unreliable. Scan the WHOLE
    # transcript rather than pinning a position: by the run's end the transcript's last tool
    # message is the corrected call's own dispatch result, not the correction that preceded it.
    corrections = [m for m in llm.user_messages_seen if "line 1 column" in m]
    assert len(corrections) == 1  # exactly one correction, never re-sent on the retry's own reply
    assert "char" in corrections[0]


# --- a second failure settles exactly as today — no loop, no silent retry storm ---------------


async def test_a_second_malformed_attempt_gets_no_second_repair() -> None:
    llm = _Scripted(("ingest", _BROKEN), ("ingest", _BROKEN), "done")
    dispatch, calls = _tracked_dispatch()
    result = await _run(llm, dispatch)
    assert len(_repairs(result)) == 1  # only the first attempt was corrected
    # the second malformed attempt fell through to the normal path — dispatched like any other call
    assert len(calls) == 1
    assert calls[0]["content"] == _BROKEN
    # "settles exactly as today" is pinned, not just implied: a member that never fixes its output
    # still finishes — today's ordinary tool-error handling, no loop, no silent retry storm.
    assert result.status is HarnessStatus.SUCCEEDED


# --- a member with no declaration is byte-for-byte unaffected ---------------------------------


async def test_a_member_without_the_declaration_is_never_checked() -> None:
    llm = _Scripted(("ingest", _BROKEN), "done")
    dispatch, calls = _tracked_dispatch()
    result = await _run(llm, dispatch, policy=_envelope(requires_valid_json=False))
    assert len(_repairs(result)) == 0
    assert len(calls) == 1  # the malformed content was dispatched exactly as it is today
    assert calls[0]["content"] == _BROKEN


async def test_a_non_json_source_type_is_never_checked() -> None:
    # A declared member still ingests markdown/plain-text content — "requires_valid_json" scopes
    # to a JSON document, not to every ingest call this member ever makes. Text that is not meant
    # to be JSON is not malformed JSON; checking it here would be a live regression on prose ingest.
    prose = "# Adoption Barriers\n\nCost is the biggest one, not { curly braces } that don't close."
    llm = _Scripted(("ingest", prose, "md"), "done")
    dispatch, calls = _tracked_dispatch()
    result = await _run(llm, dispatch)  # requires_valid_json=True (the default)
    assert len(_repairs(result)) == 0
    assert len(calls) == 1 and calls[0]["content"] == prose
    assert result.status is HarnessStatus.SUCCEEDED


# --- the repair is granted on top of the member's own budget, never charged to it -------------


async def test_a_member_at_its_tool_call_cap_still_gets_the_repair() -> None:
    # The scenario the issue actually describes: a member SPENDS its budget on real work first
    # (one successful ingest), THEN writes the broken brief. `max_tool_calls=1` means that first
    # call already exhausts the cap, so the tool-call budget gate (`tool_use.py:447`) fires on the
    # malformed call BEFORE the pre-dispatch JSON check ever runs — unless the repair is genuinely
    # granted on top of, not carved out of, the member's own budget.
    llm = _Scripted(("ingest", _FIXED), ("ingest", _BROKEN), ("ingest", _FIXED), "done")
    dispatch, calls = _tracked_dispatch()
    result = await _run(llm, dispatch, policy=_envelope(max_tool_calls=1))
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_repairs(result)) == 1
    # the pre-budget call, then the repaired one on the granted slot — the malformed attempt in
    # between was never dispatched at all.
    assert len(calls) == 2
    assert calls[0]["content"] == _FIXED
    assert calls[1]["content"] == _FIXED
