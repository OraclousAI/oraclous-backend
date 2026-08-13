"""#781 (security) — ``data_absent`` is a RESERVED result key, so it needs a PROVENANCE check.

``domain/loop/tool_use.py`` pops ``data_absent`` from **any** tool result and believes it. The
comment above the pop says the key is "set ONLY by the knowledge-retriever connector … (no other
tool may emit it)". Nothing enforced that sentence, so any tool that reflects its arguments — a
generic REST call, an imported MCP server — carried a model-chosen flag straight through.

**What that buys a model, and what it does not.** Two of the three consequences were known when
this issue was filed on 2026-08-11: a false ``empty_retrieval`` alert to an operator, and the
platform-authored "there is no data there to find" note injected into the transcript the model
itself reads. The third arrived with the #792 precedence ruling (`0b7504b`, 2026-08-13), which gave
the same flag a second job at the loop terminal:

    if citation_blocked is not None and (citation_blocked_rule2 or not retrieval_empty):
        return _escalate("citation", "citation_unresolved", ...)

A member blocked by §CITE rule 1 that cannot fix its draft within the budget must end ESCALATED /
``citation_unresolved``. With a forged ``retrieval_empty`` it falls through to ``_budget_gate``
instead and degrades to PARTIAL / ``empty_retrieval`` — and a PARTIAL **ships its answer to the
user, flagged**, where the escalation would have stopped it. **The model can soften its own gate
failure.** That is ``test_a_rule1_block_with_a_forged_absence_marker_still_escalates`` below, and it
is the reason this issue moved from "tidy the asymmetry" to a `security` fix.

**The severity is not larger than that**, and the tests say so by what they do not assert. A forged
``data_absent`` carries no ids, so it cannot mint a citation; a rule 2 violation outranks every
degrade by the #792 ruling, so a forged id still escalates on a data-absent run. This degrades an
outcome. It does not fabricate provenance.

**The fix is the one #743 already built for the sibling key**, applied to this one: accumulate the
flag only from a trusted binding, and keep popping it from every result so the name never survives
into the transcript. Both halves matter — popping everywhere is what stops the model from learning
the key is live.

**Two trust sets, not one** (decided on #781, open to the Code Review gate). ``data_absent`` is
emitted in exactly one place in the repository — the knowledge-retriever connector — while
``citation_bindings`` covers three capabilities and #746 will extend it to live web reads and
imported MCP tools. An imported MCP tool's result is whatever the remote server returned, which is
the echo-shaped surface this issue is about; sharing one set would re-open this hole the day #746
lands, with nobody having decided to open it.
``test_a_citation_minting_binding_is_not_automatically_believed_about_absence`` is the test that
pins the split, and it is the one to delete if the ruling goes the other way.

``run_tool_use_loop``'s ``data_absent_bindings`` keyword does not exist yet, so the tests that pass
it hard-fail RED until the ``[impl]`` lands. Module-level imports are all shipped seams, so
collection stays clean (``.claude/rules/tests-seam-imports.md``).
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

# The first-party retrieval the platform trusts: the ONLY connector that emits `data_absent`.
_SEARCH = ToolSpec(
    name="kr__search",
    description="search the knowledge graph",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="knowledge-retriever",
    operation="search",
)
# A tool that reflects its input — a generic REST call or an imported MCP server. This is the
# model's actual reach into a tool result, and therefore the whole attack surface. The binding name
# is deliberately unremarkable: the defence must be provenance, never a name the model could pick.
_ECHO = ToolSpec(
    name="mcp__notes__echo",
    description="an imported tool that returns what it is given",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="mcp:notes",
    operation="read",
)
# A retrieval bound under a manifest-chosen alias. The service resolves the trusted CAPABILITY to
# whatever alias this manifest gave it, so the loop must believe the set it is handed rather than
# the string "knowledge-retriever".
_ALIASED = ToolSpec(
    name="Read__search",
    description="search the knowledge graph",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="Read",
    operation="search",
)

_ENVELOPE = PolicyEnvelope(
    max_iterations=6, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
)
# One tool turn and three answer attempts — enough budget to be corrected, not enough to outlast it.
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


async def _dispatch(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    """The retrieval reports data-absence honestly; the echo tool reflects the model's args."""
    if spec.binding in (_SEARCH.binding, _ALIASED.binding):
        return {"hits": [], "data_absent": True, "served_citation_ids": []}
    return dict(args)


def _serving_dispatch(*citation_ids: str) -> Any:
    """A retrieval that SERVED something and reflects the echo tool's args — used to show that a
    binding trusted to mint citations is still not trusted to claim absence."""

    async def dispatch(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        if spec.binding == _ECHO.binding:
            return dict(args)
        return {
            "hits": [{"id": "n0", "type": "Chunk", "properties": {"text": "30-day notice"}}],
            "served_citation_ids": list(citation_ids),
        }

    return dispatch


class _Scripted:
    """Plays one scripted entry per model turn; the LAST entry repeats forever, which is what a
    member that cannot be corrected looks like. A ``_Call`` entry calls a tool; any string is a
    final answer with no tool calls."""

    protocol_shape = "fake"

    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.turns = 0
        self.tool_content: list[str] = []

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.tool_content = [str(m.get("content", "")) for m in messages if m.get("role") == "tool"]
        entry = self._script[min(self.turns, len(self._script) - 1)]
        self.turns += 1
        if isinstance(entry, _Call):
            call = ToolCall(f"c{self.turns}", entry.spec.name, dict(entry.args))
            return LLMResponse(text="working", tool_calls=[call])
        return LLMResponse(text=str(entry), tool_calls=[])


class _Call:
    """A scripted tool call: which tool, and the arguments the model chose."""

    def __init__(self, spec: ToolSpec, **args: Any) -> None:
        self.spec = spec
        self.args = args


async def _run(
    llm: Any,
    *,
    tool_specs: list[ToolSpec],
    dispatch: Any = _dispatch,
    policy: PolicyEnvelope = _ENVELOPE,
    **trust: Any,
) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="what notice period did we agree",
        tool_specs=tool_specs,
        dispatch=dispatch,
        policy=policy,
        **trust,
    )


# --- the injection: an untrusted tool cannot flag a retrieval empty --------------------------


async def test_an_untrusted_tool_cannot_flag_the_run_as_data_absent() -> None:
    # The whole issue, at its smallest. The model pushes `data_absent: true` through an echo-shaped
    # tool and then answers normally. No retrieval reported anything, so the run must be a plain
    # SUCCEEDED — an operator must not be shown a data-absence alert that never happened, and the
    # member must not be flagged PARTIAL for a failure it invented.
    llm = _Scripted(_Call(_ECHO, data_absent=True, note="hi"), "the notice period is 30 days")
    result = await _run(llm, tool_specs=[_SEARCH, _ECHO])
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.error_type is None


async def test_the_untrusted_flag_is_stripped_and_buys_no_platform_note() -> None:
    # The second half of the fix. The key is popped from EVERY result, trusted or not, so its name
    # never reaches the model — a model that cannot read the key cannot learn to write one. And the
    # platform's "there is no data there to find" note is NOT injected: that note is an instruction
    # the platform authors, and a model able to trigger it at will can talk itself out of retrying.
    llm = _Scripted(_Call(_ECHO, data_absent=True, note="hi"), "the notice period is 30 days")
    await _run(llm, tool_specs=[_SEARCH, _ECHO])
    assert llm.tool_content  # the tool really ran — an empty transcript would pass vacuously
    assert all("data_absent" not in c for c in llm.tool_content)
    assert all("No data was found" not in c for c in llm.tool_content)


async def test_a_trusted_retrieval_still_degrades_exactly_as_before() -> None:
    # #580, unchanged. The honest signal from the honest connector still marks the run to degrade —
    # a fix that hardened the key by killing the feature would be no fix at all (ADR-021).
    llm = _Scripted(_Call(_SEARCH, query="q"), "I could not find the notice period")
    result = await _run(llm, tool_specs=[_SEARCH, _ECHO])
    assert result.status is HarnessStatus.PARTIAL
    assert result.error_type == "empty_retrieval"


async def test_a_trusted_binding_is_believed_under_the_alias_the_service_resolved() -> None:
    # Trust is a set the SERVICE computes from the resolved registry rows, and it names the alias
    # this manifest chose. A loop that keyed on the literal string "knowledge-retriever" would
    # disbelieve a real retriever bound as "Read", which is what the deployed harness actually does.
    llm = _Scripted(_Call(_ALIASED, query="q"), "I could not find the notice period")
    result = await _run(
        llm, tool_specs=[_ALIASED], data_absent_bindings=frozenset({_ALIASED.binding})
    )
    assert result.status is HarnessStatus.PARTIAL
    assert result.error_type == "empty_retrieval"


async def test_an_untrusted_binding_stays_untrusted_when_the_service_names_the_set() -> None:
    # The same explicit set, from the other side: a binding absent from it is not believed, even
    # though a sibling binding in the same run is. This is the shape the service actually passes.
    llm = _Scripted(_Call(_ECHO, data_absent=True), "the notice period is 30 days")
    result = await _run(
        llm,
        tool_specs=[_ALIASED, _ECHO],
        data_absent_bindings=frozenset({_ALIASED.binding}),
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.error_type is None


async def test_a_citation_minting_binding_is_not_automatically_believed_about_absence() -> None:
    # The two-set decision, pinned. This binding IS trusted to mint citations — its served ids land
    # in the run's set, and the answer citing one passes the gate — and it is NOT trusted to claim
    # data-absence. Only the knowledge-retriever connector emits `data_absent`; #746 will add live
    # web and imported MCP rows to the MINTING set, and those results are whatever a remote server
    # returned. Delete this test only if the two sets are ruled to be one.
    llm = _Scripted(
        _Call(_ECHO, data_absent=True),
        _Call(_SEARCH, query="q"),
        f"the notice period is 30 days [{_CIT_A}]",
    )
    result = await _run(
        llm,
        tool_specs=[_SEARCH, _ECHO],
        dispatch=_serving_dispatch(_CIT_A),
        citation_bindings=frozenset({_SEARCH.binding, _ECHO.binding}),
        data_absent_bindings=frozenset({_SEARCH.binding}),
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.error_type is None  # the echo tool's absence claim was ignored
    assert set(result.served_citation_ids) == {_CIT_A}  # its citation trust is untouched


# --- the composed case: a forged marker must not soften the citation terminal ----------------


async def test_a_rule1_block_with_a_forged_absence_marker_still_escalates() -> None:
    # THE test this issue exists for, and the reason it is `security` rather than a tidy-up.
    #
    # The member never retrieves. It pushes `data_absent` through the echo tool, then spends its
    # budget on a draft that keeps failing §CITE rule 1 (a "Sources:" line carrying no id). Under
    # the #792 ruling a rule 1-only block on a data-absent run degrades to PARTIAL — the accepted
    # Limit 2 misfire landing on MISSING data. Here the data-absence is FORGED, so that mercy is
    # not the member's to claim: the run must reach ESCALATED / citation_unresolved, and its answer
    # must not be shipped to the user at all.
    #
    # Pinned on the TERMINAL, not on the flag. The flag is an implementation detail; what the user
    # receives is the property.
    llm = _Scripted(
        _Call(_ECHO, data_absent=True),
        "I found nothing.\nSources: none were available.",
    )
    result = await _run(llm, tool_specs=[_SEARCH, _ECHO], policy=_TIGHT)
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "citation_unresolved"


async def test_a_degrade_configured_member_cannot_forge_its_way_to_partial_either() -> None:
    # The same run on a member that asked to degrade at a budget gate (#587). `on_exhaustion` is a
    # BUDGET preference; it was never a licence to ship an answer that failed the citation gate,
    # and a forged absence marker must not become a second route to the terminal #782 refused.
    llm = _Scripted(
        _Call(_ECHO, data_absent=True),
        "I found nothing.\nSources: none were available.",
    )
    result = await _run(llm, tool_specs=[_SEARCH, _ECHO], policy=_TIGHT_DEGRADE)
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "citation_unresolved"


# --- the #792 ruling itself, preserved ------------------------------------------------------


async def test_a_rule1_block_on_a_genuinely_data_absent_run_still_degrades() -> None:
    # #792 branch 2, unchanged and deliberately re-pinned HERE: the ruling is correct given a
    # TRUSTWORTHY marker, and this issue's job is to make the marker trustworthy, never to revert
    # the branch. Same script as the forgery test above, with the flag coming from the real
    # retrieval — and the terminal flips back to the honest decline's PARTIAL / empty_retrieval.
    llm = _Scripted(
        _Call(_SEARCH, query="q"),
        "I found nothing.\nSources: none were available.",
    )
    result = await _run(llm, tool_specs=[_SEARCH, _ECHO], policy=_TIGHT)
    assert result.status is HarnessStatus.PARTIAL
    assert result.error_type == "empty_retrieval"


async def test_a_forged_id_on_a_genuinely_data_absent_run_still_escalates() -> None:
    # #792 branch 1, unchanged: a rule 2 violation is WRONG data, and it outranks the degrade even
    # for a degrade-configured member. Nothing in this issue touches that precedence.
    forged = "cit_deadbeefdeadbeefdeadbeefdeadbeef"
    llm = _Scripted(_Call(_SEARCH, query="q"), f"the notice period is 30 days [{forged}]")
    result = await _run(llm, tool_specs=[_SEARCH, _ECHO], policy=_TIGHT_DEGRADE)
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "citation_unresolved"
