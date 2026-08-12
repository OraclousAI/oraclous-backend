"""#782 — the citation gate AT THE RUN BOUNDARY: what a violation does (§CITE rev4, Contract #735).

``test_citation_answer_gate.py`` pins the rule verdicts. This file pins the behaviour they drive,
and it is the half that actually closes the gap: #743 shipped ``check_answer_citations`` as a pure
function and nothing calls it, so today a fabricated citation is **detectable** but not **blocked**.
A test that only calls the pure function proves nothing about the run boundary.

**A blocked answer goes back to the member, not to the user** (rev4 decision 9). The member is the
only party that can fix the defect, and an error it cannot act on is one it can only retry blindly —
the #692/#693 failure, where a member was told "409" and simply repeated the failing call. So the
gate has to run INSIDE the loop, before the final answer is accepted, rather than after
``run_tool_use_loop`` returns.

**The mechanism is the completion contract's nudge (#543), twelve lines above where this goes** —
append the answer, append a correction, record a step, ``continue``. The gate sits AFTER the nudge
and BEFORE the ``retrieval_empty`` degrade check.

Three things this file decides rather than inherits, all flagged in the PR body for the Tests Review
gate:

* **The terminal outcome when the retry budget is spent.** Pinned in ``_UNRESOLVED_STATUS`` /
  ``_UNRESOLVED_ERROR_TYPE`` below — two constants, flip them together, nothing else in the file
  encodes the choice.
* **The retry bound.** Corrections are NOT one-shot like the nudge: each one consumes an iteration
  from the run's existing budget, per the brief's "retries are bounded" decision and §CITE Limit 1.
* **The seam that carries the persisted union into the loop** — ``prior_served_citation_ids``.

``run_tool_use_loop``'s new keyword does not exist yet, so every test that passes it hard-fails RED
until the ``[impl]`` lands. The module-level imports are all shipped seams, so collection is clean
(``.claude/rules/tests-seam-imports.md``).
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import LoopCheckpoint, run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.tool_dispatch]

_CIT_A = "cit_9f2a4c81b7d3e50a1c6f28934bd5e7a0"
_CIT_B = "cit_0b1d3f57a9c2e4680d8f1a3b5c7e9021"
_FORGED = "cit_deadbeefdeadbeefdeadbeefdeadbeef"

# --- THE OPEN DECISION, for be-test-reviewer to rule at the Tests Review gate ----------------
# What does a run report once the member has spent the retry budget without satisfying the gate?
#
#   Option 1 (briefed by product-planner, authored here): the run FAILS — ESCALATED, typed
#   `citation_unresolved`. A fabricated citation must not reach a user, which is the entire point of
#   the Contract. #580's degrade-to-PARTIAL exists for MISSING data; this is WRONG data, and
#   shipping it flagged still ships it.
#
#   Option 2: degrade to PARTIAL and ship the answer with the violation recorded.
#
# Flip these two constants together to switch the whole file to option 2. Nothing else encodes it.
_UNRESOLVED_STATUS = HarnessStatus.ESCALATED
_UNRESOLVED_ERROR_TYPE = "citation_unresolved"

# The correction's step in the trace (criterion 9). StepKind.GATE because §CITE calls this a gate,
# and StepKind.GATE is "a governance decision"; the nudge used StepKind.LLM because a nudge is a
# prompt, not a decision. Named here so the trace shape is one edit away if the reviewer disagrees.
_CORRECTION_KIND = StepKind.GATE
_CORRECTION_NAME = "citation"
_CORRECTION_STATUS = "citation_correction"

_SEARCH = ToolSpec(
    name="kr__search",
    description="search the knowledge graph",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="knowledge-retriever",
    operation="search",
)

_ENVELOPE = PolicyEnvelope(
    max_iterations=6, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
)
# A deliberately short budget: the member gets one retrieval turn and three answer attempts.
_TIGHT = PolicyEnvelope(
    max_iterations=4, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
)


class _Scripted:
    """Plays one scripted entry per model turn; the LAST entry repeats forever, which is what a
    member that cannot be corrected looks like. ``RETRIEVE`` calls the retrieval tool; any other
    string is a final answer with no tool calls."""

    protocol_shape = "fake"
    RETRIEVE = "\x00retrieve"

    def __init__(self, *script: str) -> None:
        self._script = list(script)
        self.turns = 0
        self.user_messages: list[str] = []

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.user_messages = [
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        ]
        entry = self._script[min(self.turns, len(self._script) - 1)]
        self.turns += 1
        if entry == self.RETRIEVE:
            call = ToolCall(f"c{self.turns}", _SEARCH.name, {"query": "q"})
            return LLMResponse(text="searching", tool_calls=[call])
        return LLMResponse(text=entry, tool_calls=[])


def _serving(*citation_ids: str) -> Any:
    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "hits": [{"id": "n0", "type": "Chunk", "properties": {"text": "30-day notice"}}],
            "served_citation_ids": list(citation_ids),
        }

    return dispatch


async def _never(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("no tool should be dispatched")


async def _run(
    llm: Any,
    dispatch: Any = None,
    *,
    policy: PolicyEnvelope = _ENVELOPE,
    specs: list[ToolSpec] | None = None,
    **kwargs: Any,
) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="what notice period did we agree",
        tool_specs=specs if specs is not None else [_SEARCH],
        dispatch=dispatch if dispatch is not None else _never,
        policy=policy,
        **kwargs,
    )


def _corrections(result: Any) -> list[Any]:
    return [s for s in result.steps if s.status == _CORRECTION_STATUS]


# --- criterion 1: a correct answer is accepted, untouched -----------------------------------


async def test_an_answer_citing_a_served_id_succeeds_with_no_correction() -> None:
    llm = _Scripted(_Scripted.RETRIEVE, f"The notice period is 30 days [{_CIT_A}].")
    result = await _run(llm, _serving(_CIT_A))
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == f"The notice period is 30 days [{_CIT_A}]."
    assert _corrections(result) == []
    assert llm.turns == 2  # the gate did not cost the member a turn


# --- criterion 2: the bug #743 shipped, at the run boundary ---------------------------------


async def test_an_answer_citing_nothing_succeeds_even_though_the_run_served_sources() -> None:
    # The run served two sources; the member cited neither and named neither. The model is not a
    # source of truth, so an uncited sentence is its own reasoning. This is the property the whole
    # rev4 revision exists to protect, and the one an over-eager gate destroys first.
    llm = _Scripted(_Scripted.RETRIEVE, "The two clauses do not conflict.")
    result = await _run(llm, _serving(_CIT_A, _CIT_B))
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == "The two clauses do not conflict."
    assert _corrections(result) == []


# --- criterion 3: an honest decline is never blocked ----------------------------------------


async def test_an_honest_decline_after_a_retrieval_that_returned_hits_succeeds() -> None:
    # A member that searches, reads what came back, and reports it has no answer is behaving
    # correctly. Refusing to hallucinate is the product working (rev4 decision 8).
    llm = _Scripted(_Scripted.RETRIEVE, "I don't have that information in the sources I was given.")
    result = await _run(llm, _serving(_CIT_A))
    assert result.status is HarnessStatus.SUCCEEDED
    assert _corrections(result) == []


# --- criterion 11: a run with no retrieval at all is never gated -----------------------------


async def test_a_reasoning_only_member_is_never_gated() -> None:
    # No tools, nothing served, a plain answer: there is nothing to check and the gate must not
    # insert itself. Without this the gate would add a spurious turn to every tool-less member.
    llm = _Scripted("A 30-day notice period is the market standard.")
    result = await _run(llm, specs=[])
    assert result.status is HarnessStatus.SUCCEEDED
    assert _corrections(result) == []
    assert llm.turns == 1


# --- criteria 4 + 7 + 9: rule 1 is corrected, and the member's second answer is the output ---


async def test_a_prose_marker_is_corrected_and_the_fixed_answer_is_the_output() -> None:
    good = f"The notice period is 30 days [{_CIT_A}]."
    llm = _Scripted(
        _Scripted.RETRIEVE,
        "The notice period is 30 days (source: partner-agreement.md).",
        good,
    )
    result = await _run(llm, _serving(_CIT_A))
    assert result.status is HarnessStatus.SUCCEEDED
    # criterion 7: the CORRECTED text is the run's output — the blocked draft must not survive.
    assert result.output == good
    assert llm.turns == 3  # the correction cost exactly one extra turn


async def test_the_correction_names_the_defect_and_the_remedy() -> None:
    # #692/#693: an error a member can act on beats a status it can only retry blindly. §CITE rev4
    # gives the exact string for rule 1; assert the load-bearing half of it rather than the whole
    # sentence, so the implementer may append detail without breaking the test.
    llm = _Scripted(
        _Scripted.RETRIEVE,
        "The notice period is 30 days (source: partner-agreement.md).",
        f"The notice period is 30 days [{_CIT_A}].",
    )
    await _run(llm, _serving(_CIT_A))
    correction = llm.user_messages[-1]
    assert "names a source in text but carries no citation" in correction
    assert "citation_id" in correction  # what to cite
    assert "remove the claim" in correction  # the other way out, for a member with no id to cite


async def test_each_correction_appends_a_step_to_the_trace() -> None:
    # criterion 9: the transcript has to show what happened, or a run that silently took an extra
    # turn is indistinguishable from a model that changed its mind.
    llm = _Scripted(
        _Scripted.RETRIEVE,
        "The notice period is 30 days (source: partner-agreement.md).",
        f"The notice period is 30 days [{_CIT_A}].",
    )
    result = await _run(llm, _serving(_CIT_A))
    steps = _corrections(result)
    assert len(steps) == 1
    assert steps[0].kind is _CORRECTION_KIND
    assert steps[0].name == _CORRECTION_NAME
    assert steps[0].detail  # never blank — the step exists to say WHICH rule blocked the answer


# --- criterion 6: rule 2 is corrected the same way, naming the offending id -------------------


async def test_an_unserved_id_is_corrected_and_the_message_names_it() -> None:
    good = f"The notice period is 30 days [{_CIT_A}]."
    llm = _Scripted(
        _Scripted.RETRIEVE,
        f"The notice period is 30 days [{_FORGED}].",
        good,
    )
    result = await _run(llm, _serving(_CIT_A))
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == good
    correction = llm.user_messages[-1]
    assert "never served to this run" in correction
    assert _FORGED in correction  # naming the id is what makes the error actionable


# --- criterion 8: the retry budget is finite, and what a spent budget reports -----------------


async def test_a_member_that_never_fixes_its_answer_exhausts_the_budget() -> None:
    # rev4 Limit 1: a member that cannot satisfy the gate must not loop. The run's existing
    # iteration budget is the bound, so each correction consumes an iteration — corrections are NOT
    # one-shot like the completion nudge, which would let one bad answer through on the second pass.
    llm = _Scripted(
        _Scripted.RETRIEVE, "The notice period is 30 days (source: partner-agreement.md)."
    )
    result = await _run(llm, _serving(_CIT_A), policy=_TIGHT)
    assert result.status is _UNRESOLVED_STATUS
    assert result.error_type == _UNRESOLVED_ERROR_TYPE
    # the budget was really spent, not short-circuited: one retrieval turn + three answer attempts.
    assert llm.turns == _TIGHT.max_iterations
    assert result.iterations == _TIGHT.max_iterations
    assert len(_corrections(result)) >= 2


async def test_an_exhausted_run_is_not_reported_as_a_plain_iteration_cap() -> None:
    # The typed reason is the point: "did not converge" tells an operator nothing, and #692 is the
    # record of what an untyped failure costs. A citation the member could not resolve has its own
    # name so the console can say what actually happened.
    llm = _Scripted(_Scripted.RETRIEVE, f"Notice is 30 days [{_FORGED}].")
    result = await _run(llm, _serving(_CIT_A), policy=_TIGHT)
    assert result.error_type != "iteration_cap"
    assert result.error_type == _UNRESOLVED_ERROR_TYPE


async def test_the_last_blocked_draft_is_still_carried_out_of_the_run() -> None:
    # Whatever the terminal status, the operator needs to SEE the answer that could not be cleared —
    # a blocked run with an empty output is unauditable.
    llm = _Scripted(
        _Scripted.RETRIEVE, "The notice period is 30 days (source: partner-agreement.md)."
    )
    result = await _run(llm, _serving(_CIT_A), policy=_TIGHT)
    assert result.status is _UNRESOLVED_STATUS
    assert result.output == "The notice period is 30 days (source: partner-agreement.md)."


# --- criterion 10: the gate reads the PERSISTED UNION, not this segment alone ------------------


def _paused(iteration: int = 1) -> LoopCheckpoint:
    """A checkpoint with nothing left to dispatch — the resume goes straight to the model, which is
    the shape that matters here: the post-pause segment served nothing of its own."""
    return LoopCheckpoint(
        messages=[{"role": "user", "content": "what notice period did we agree"}],
        pending_tool_calls=[],
        approved_tool_call_id="c0",
        iteration=iteration,
        tool_calls_made=1,
        tokens_used=10,
        redact_patterns=[],
    )


async def test_an_answer_citing_a_pre_pause_id_passes_after_a_resume() -> None:
    # The loop's own served set is a FRESH list on a HITL resume (the checkpoint carries the
    # transcript, not platform counters), so a gate reading `LoopResult.served_citation_ids` alone
    # would fail a post-pause answer citing a pre-pause source — a correct answer blocked by
    # bookkeeping. The persisted union is the truth; the loop has to be handed it.
    llm = _Scripted(f"The notice period is 30 days [{_CIT_A}].")
    result = await _run(llm, resume_state=_paused(), prior_served_citation_ids=[_CIT_A])
    assert result.status is HarnessStatus.SUCCEEDED
    assert _corrections(result) == []


async def test_the_resumed_loop_still_reports_only_its_own_segment() -> None:
    # The seam feeds the gate; it must NOT change what the loop returns. The repository unions the
    # segment into the persisted row (`update_run`), so a loop that returned the union instead would
    # move that merge to the wrong layer and silently double-count nothing — until someone reads
    # `LoopResult.served_citation_ids` expecting one segment and gets two.
    llm = _Scripted(f"The notice period is 30 days [{_CIT_A}].")
    result = await _run(llm, resume_state=_paused(), prior_served_citation_ids=[_CIT_A])
    assert list(result.served_citation_ids) == []


async def test_a_resumed_answer_citing_an_id_in_neither_segment_is_still_corrected() -> None:
    # The union widens the served set; it does not disable rule 2. Without this the seam could be
    # implemented as "skip the check on a resume" and every test above would still pass.
    llm = _Scripted(f"Notice is 30 days [{_FORGED}].", f"Notice is 30 days [{_CIT_A}].")
    result = await _run(llm, resume_state=_paused(), prior_served_citation_ids=[_CIT_A])
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_corrections(result)) == 1
    assert _FORGED in llm.user_messages[-1]
