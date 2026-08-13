"""#782 — coverage the PR #791 reviews found missing, plus the sticky-flag fix they required.

Three gaps, one behaviour fix, all from the Code Review / QA gates on the `[impl]` PR:

* **The empty-served-set correction had zero coverage at any level** (QA finding 1). It exists
  because of a live failure: a member burned all 25 iterations being told to "cite the
  ``citation_id`` you were given" on a run that had been given none (#788). The remedy has to be
  performable, and nothing exercised the branch that decides what that member reads.
* **A draft failing both rules at once was untested through the loop** (QA finding 2).
  ``_citation_correction``'s docstring claims both messages arrive together; only the pure function
  was covered, and only for two rule-2 violations.
* **The ``citation_blocked`` flag was sticky** (Code Review finding 2). A run where the gate fired
  once and which then failed to converge for an unrelated reason was reported
  ``citation_unresolved`` — "could not produce a citable answer" — when its last twelve turns were
  tool calls, and #587's ``on_exhaustion="degrade"`` was overridden for plain non-convergence.
  Fixed: the flag clears on any tool-call turn, so the terminal fires only when the run genuinely
  ENDED on a blocked answer. ``test_a_degrade_configured_member_still_escalates_on_an_unresolved_
  citation`` (the merged file) pins the ended-on-a-blocked-answer case; the two tests here pin the
  moved-past-it case.
* **The ids named in a correction are capped** (Code Review nit 3): a model that fabricates dozens
  of ids must not grow the correction prompt and the step detail without bound, once per iteration.

NOT here, deliberately: the precedence between ``citation_unresolved`` and ``empty_retrieval`` on a
data-absent run. That is escalated for a ruling (it is a Contract/ADR-021 question, not the
implementer's), and whichever way it lands gets pinned then.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.tool_dispatch]

_CIT_A = "cit_9f2a4c81b7d3e50a1c6f28934bd5e7a0"
_FORGED = "cit_deadbeefdeadbeefdeadbeefdeadbeef"

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
# One retrieval turn, one blocked answer, then only tool calls to the cap.
_TIGHT = PolicyEnvelope(
    max_iterations=4, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
)
_TIGHT_DEGRADE = PolicyEnvelope(
    max_iterations=4,
    max_tool_calls=None,
    max_wall_time_seconds=None,
    max_tokens=None,
    on_exhaustion="degrade",
)


class _Scripted:
    """One scripted entry per model turn; the last entry repeats forever. ``RETRIEVE`` calls the
    retrieval tool; any other string is a final answer with no tool calls."""

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


async def _run(llm: Any, dispatch: Any, *, policy: PolicyEnvelope = _ENVELOPE) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="what notice period did we agree",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=policy,
    )


def _corrections(result: Any) -> list[Any]:
    return [s for s in result.steps if s.status == _CORRECTION_STATUS]


# --- the #788 live failure: the remedy must be performable ----------------------------------


async def test_the_nothing_served_correction_offers_a_performable_remedy() -> None:
    # The run retrieved and was served NOTHING, then attributed in prose. Telling this member to
    # "cite the citation_id you were given" is an instruction it cannot follow — it was given none,
    # and a live member burned its whole budget discovering that (#788). The correction must name
    # the remedy that exists: remove the attribution.
    llm = _Scripted(
        _Scripted.RETRIEVE,
        "I found nothing.\nSources: none were available.",
        "I could not find the notice period in the available data.",
    )
    result = await _run(llm, _serving())
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_corrections(result)) == 1
    correction = llm.user_messages[-1]
    assert "No citations were served to this run" in correction
    assert "remove the source attribution" in correction
    # The unperformable instruction must NOT be the one this member reads.
    assert "Cite the `citation_id` you were given" not in correction


# --- both rules failing one draft arrive as one correction ----------------------------------


async def test_a_draft_failing_both_rules_gets_one_correction_carrying_both() -> None:
    # Line 1 fails rule 1 (a marker with no id on its line); line 2 fails rule 2 (a forged id).
    # Correcting only one would cost the member an extra iteration to discover the other — the
    # docstring's claim, previously untested through the loop.
    llm = _Scripted(
        _Scripted.RETRIEVE,
        f"Sources: partner-agreement.md\nThe notice period is 30 days [{_FORGED}].",
        f"The notice period is 30 days [{_CIT_A}].",
    )
    result = await _run(llm, _serving(_CIT_A))
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_corrections(result)) == 1
    correction = llm.user_messages[-1]
    assert "names a source in text but carries no citation" in correction  # rule 1
    assert _FORGED in correction  # rule 2, naming the offending id
    detail = _corrections(result)[0].detail
    assert "rule 1" in detail and "rule 2" in detail


# --- the sticky flag: a run that moved past its blocked draft -------------------------------


async def test_a_run_that_moved_past_a_blocked_draft_degrades_as_iteration_cap() -> None:
    # The gate fired once, the member then only called tools to the cap. That is plain
    # non-convergence, not an unresolved citation: the member never ended on a blocked answer, so
    # `citation_unresolved` would misreport what happened and override on_exhaustion="degrade"
    # (#587) for a failure that had nothing to do with citations.
    llm = _Scripted(_Scripted.RETRIEVE, "Sources: partner-agreement.md", _Scripted.RETRIEVE)
    result = await _run(llm, _serving(_CIT_A), policy=_TIGHT_DEGRADE)
    assert len(_corrections(result)) == 1  # the gate DID fire mid-run
    assert result.status is HarnessStatus.PARTIAL
    assert result.error_type == "iteration_cap"


async def test_a_run_that_moved_past_a_blocked_draft_escalates_as_iteration_cap() -> None:
    # Same run on an escalate-configured member: still an iteration cap, still not a citation
    # failure. The terminal's label must describe the run's end, not its history.
    llm = _Scripted(_Scripted.RETRIEVE, "Sources: partner-agreement.md", _Scripted.RETRIEVE)
    result = await _run(llm, _serving(_CIT_A), policy=_TIGHT)
    assert len(_corrections(result)) == 1
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "iteration_cap"


# --- the correction names at most five ids ---------------------------------------------------


async def test_a_correction_names_at_most_five_ids_and_counts_the_rest() -> None:
    # Seven distinct forged ids in one draft. The correction stays actionable (it names ids the
    # member can find in its own draft) without growing the prompt and the step detail without
    # bound — a model emitting dozens of fabrications would otherwise inflate every iteration.
    forged = [f"cit_{i:032x}" for i in range(1, 8)]
    draft = "The notice period is 30 days " + " ".join(f"[{f}]" for f in forged) + "."
    llm = _Scripted(_Scripted.RETRIEVE, draft, f"The notice period is 30 days [{_CIT_A}].")
    result = await _run(llm, _serving(_CIT_A))
    assert result.status is HarnessStatus.SUCCEEDED
    correction = llm.user_messages[-1]
    for named in forged[:5]:
        assert named in correction
    assert forged[5] not in correction and forged[6] not in correction
    assert "and 2 more" in correction
