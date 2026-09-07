"""#944 — inline-link provenance AT THE RUN BOUNDARY: what a failed match does.

``test_link_provenance.py`` pins the verdicts. This file pins the behaviour they drive, and it is
the half that closes the gap: a pure function nothing calls makes a fabricated link **detectable**
but not **caught**. That is exactly the state ``check_answer_citations`` sat in between #743 and
#782, and it is worth not repeating.

**The consequence — RULED by the owner on #944 (2026-09-07): flag, and send back only when NOTHING
matched.** Three cases, and the split is the whole ruling:

* **Every link in the answer is unverified** (and there is at least one) — the draft goes BACK to
  the member with a correction, exactly as a citation violation does, bounded by the run's existing
  iteration budget. A member that fabricated all of its links has not merely slipped; it answered
  from training data while holding real fetched pages, and it can fix that.
* **Some verified, some not** — the answer is ACCEPTED and FLAGGED. It is real work with one bad
  link in it, and spending the budget to re-run the whole answer would throw away the good part.
* **Nothing unverified** — nothing happens, no step, no cost. Same posture as the citation gate.

**A spent budget DEGRADES, it never fails.** When the member cannot converge, the run finishes
``PARTIAL`` typed ``unverified_links`` with the last draft carried out, via the existing
``_degrade`` primitive (#587). This deliberately differs from the citation gate, which ESCALATES on
``citation_unresolved``. The two are not the same kind of defect and #944 says so in its own
acceptance criteria: a fabricated ``cit_`` id claims the PLATFORM served something it never served,
which is a lie about us; an unverified inline link is the model's own prose, and the criterion asks
for it to be *"flagged as unverified rather than silently trusted"* — flagged, not refused. Shipping
a flagged answer is the requested outcome, not a compromise.

**Where the fetched set comes from.** Every **ok** tool step contributes: the URLs in its result,
and the URLs in the arguments the model passed it. The arguments count only for an ok call, and for
a real reason — ``web-research.read(url=X)`` returning ok is the strongest evidence we will ever
have that X was really fetched, and it is the ordinary shape of a read. An **errored** call
contributes nothing at all, in either direction: a 404 on a composed URL is the check working.

**The limit, on the record.** A member holding a generic REST or imported MCP tool can call it with
a fabricated URL, and if that call returns ok the URL enters the fetched set — the model laundering
its own input through an echo-shaped tool. The same surface ``_SERVED_CITATION_IDS_KEY`` guards
against with a trusted-binding set. It is not guarded here because the laundering call has to
actually succeed against the fabricated URL, which is most of the way to the URL being real, and
because #746 has not yet settled which web/MCP bindings are trusted. Narrowing the harvest to that
set once it exists is a follow-up, not an implementer's choice.

``run_tool_use_loop``'s new keyword-free behaviour is additive, so every module-level import here is
a shipped seam and collection stays clean (``.claude/rules/tests-seam-imports.md``). The tests fail
RED on the ASSERTIONS until the ``[impl]`` lands, which is the intended shape for a change to an
existing function.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.tool_dispatch]

_REAL = "https://arstechnica.com/ai/2026/09/model-costs-fall-again/"
_ALSO_REAL = "https://www.theverge.com/2026/09/02/inference-prices"
# The fabricated citation from team run 8ef18ab0 (the `linker` role). Well-formed, real host,
# invented path — only provenance separates it from the two above.
_FABRICATED = "https://www.okta.com/blog/2023/10/okta-ai-token-costs"

# The trace record this check writes. StepKind.GATE because it is a governance decision, matching
# the citation gate's step; the two statuses are distinct because the read side must be able to tell
# "the member was corrected and then fixed it" from "the answer shipped carrying a bad link".
_GATE_NAME = "link_provenance"
_CORRECTION_STATUS = "link_correction"
_FLAG_STATUS = "unverified_links"

# The terminal when the member never converges. PARTIAL, never a hard failure — see the ruling in
# this module's docstring. The whole file reads these two, so the ruling lives in one place.
_EXHAUSTED_STATUS = HarnessStatus.PARTIAL
_EXHAUSTED_ERROR_TYPE = "unverified_links"

_SEARCH = ToolSpec(
    name="web_research__search",
    description="search the web",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="web-research",
    operation="search",
)
_READ = ToolSpec(
    name="web_research__read",
    description="read one page",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="web-research",
    operation="read",
)

_ENVELOPE = PolicyEnvelope(
    max_iterations=6, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
)
# One search turn plus three answer attempts — enough to be corrected twice and still not converge.
_TIGHT = PolicyEnvelope(
    max_iterations=4, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
)
# The same short budget on a member that asked to ESCALATE when a budget trips (#587). The default
# already escalates, so this is the configuration that would restore the rejected hard-fail option.
_TIGHT_ESCALATE = PolicyEnvelope(
    max_iterations=4,
    max_tool_calls=None,
    max_wall_time_seconds=None,
    max_tokens=None,
    on_exhaustion="escalate",
)


class _Scripted:
    """One scripted entry per model turn; the LAST entry repeats forever, which is what a member
    that cannot be corrected looks like. ``SEARCH``/``READ`` call a tool; anything else is a final
    answer with no tool calls."""

    protocol_shape = "fake"
    SEARCH = "\x00search"
    READ = "\x00read"

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
        if entry == self.SEARCH:
            return LLMResponse(
                text="searching",
                tool_calls=[ToolCall(f"c{self.turns}", _SEARCH.name, {"query": "token costs"})],
            )
        if entry == self.READ:
            return LLMResponse(
                text="reading",
                tool_calls=[ToolCall(f"c{self.turns}", _READ.name, {"url": _REAL})],
            )
        return LLMResponse(text=entry, tool_calls=[])


def _returning(*urls: str) -> Any:
    """A search that really returns these URLs in its result payload."""

    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return {"results": [{"title": "t", "url": u, "snippet": "…"} for u in urls]}

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
        user_input="summarise this quarter's inference pricing",
        tool_specs=specs if specs is not None else [_SEARCH, _READ],
        dispatch=dispatch if dispatch is not None else _never,
        policy=policy,
        **kwargs,
    )


def _steps(result: Any, status: str) -> list[Any]:
    return [s for s in result.steps if s.kind is StepKind.GATE and s.status == status]


# --- criterion 1: an honestly-cited answer is accepted, untouched, at no cost -----------------


async def test_an_answer_linking_only_fetched_urls_succeeds_with_no_step() -> None:
    llm = _Scripted(_Scripted.SEARCH, f"Prices fell. [Source]({_REAL})")
    result = await _run(llm, _returning(_REAL, _ALSO_REAL))
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == f"Prices fell. [Source]({_REAL})"
    assert _steps(result, _CORRECTION_STATUS) == []
    assert _steps(result, _FLAG_STATUS) == []
    assert llm.turns == 2  # the check did not cost the member a turn
    assert result.unverified_links == []


async def test_an_answer_with_no_links_at_all_succeeds_untouched() -> None:
    # The property that must survive every future change: a member that reasons without linking has
    # fabricated nothing, and punishing it would push every member toward inventing a link.
    llm = _Scripted(_Scripted.SEARCH, "The two figures do not contradict each other.")
    result = await _run(llm, _returning(_REAL))
    assert result.status is HarnessStatus.SUCCEEDED
    assert _steps(result, _CORRECTION_STATUS) == []
    assert result.unverified_links == []


async def test_a_member_with_no_tools_is_never_checked() -> None:
    # Nothing was fetched, so under a strict reading every link would be unverified. That is not
    # the intent: a reasoning-only member has no fetched set to be measured against, and inserting
    # a correction turn into every tool-less member is a cost with no signal behind it.
    llm = _Scripted(f"The standard reference is [here]({_REAL}).")
    result = await _run(llm, specs=[])
    assert result.status is HarnessStatus.SUCCEEDED
    assert _steps(result, _CORRECTION_STATUS) == []
    assert result.unverified_links == []


# --- criterion 2: every link invented → back to the member --------------------------------------


async def test_an_answer_whose_every_link_is_invented_goes_back_to_the_member() -> None:
    llm = _Scripted(
        _Scripted.SEARCH,
        f"Prices fell. [Source]({_FABRICATED})",
        f"Prices fell. [Source]({_REAL})",
    )
    result = await _run(llm, _returning(_REAL, _ALSO_REAL))
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == f"Prices fell. [Source]({_REAL})"
    assert len(_steps(result, _CORRECTION_STATUS)) == 1
    assert result.unverified_links == []


async def test_the_correction_names_the_offending_url_and_what_to_do() -> None:
    # The #692/#693 lesson: an error a member cannot act on is one it can only retry blindly. It
    # has to be told WHICH link failed and that it may only link pages it actually fetched.
    llm = _Scripted(
        _Scripted.SEARCH,
        f"Prices fell. [Source]({_FABRICATED})",
        f"Prices fell. [Source]({_REAL})",
    )
    await _run(llm, _returning(_REAL))
    correction = llm.user_messages[-1]
    assert _FABRICATED in correction
    assert _REAL in correction  # it is shown what it MAY link


async def test_each_correction_consumes_an_iteration_rather_than_being_one_shot() -> None:
    # Not the completion nudge's one-shot flag. A one-shot correction accepts whatever comes back
    # on attempt two, including a second fabrication, which is the loop this exists to close.
    llm = _Scripted(
        _Scripted.SEARCH,
        f"[Source]({_FABRICATED})",
        "[Source](https://www.okta.com/blog/2023/11/another-invention)",
        f"[Source]({_REAL})",
    )
    result = await _run(llm, _returning(_REAL))
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_steps(result, _CORRECTION_STATUS)) == 2


# --- criterion 3: some verified → accepted AND flagged, never sent back -------------------------


async def test_an_answer_with_one_bad_link_among_good_ones_is_accepted_and_flagged() -> None:
    # This is the ruled split. Real work with one bad link is not thrown away; it is shipped with
    # the bad link named, which is what the reader's screen needs to warn about.
    answer = f"[A]({_REAL}) and [B]({_FABRICATED}) and [C]({_ALSO_REAL})"
    llm = _Scripted(_Scripted.SEARCH, answer)
    result = await _run(llm, _returning(_REAL, _ALSO_REAL))
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == answer  # shipped intact, never rewritten
    assert _steps(result, _CORRECTION_STATUS) == []
    assert result.unverified_links == [_FABRICATED]
    assert llm.turns == 2  # the member was not asked to retry


async def test_the_flag_is_recorded_as_a_gate_step_naming_the_bad_link() -> None:
    # The flag has to survive into the persisted trace, because that is what the read side derives
    # from — there is no new database column for this (the #907 `simulated` posture).
    llm = _Scripted(_Scripted.SEARCH, f"[A]({_REAL}) and [B]({_FABRICATED})")
    result = await _run(llm, _returning(_REAL))
    flags = _steps(result, _FLAG_STATUS)
    assert len(flags) == 1
    assert flags[0].name == _GATE_NAME
    assert _FABRICATED in (flags[0].detail or "")


# --- criterion 4: where the fetched set comes from ----------------------------------------------


async def test_a_url_the_run_read_by_argument_counts_as_fetched() -> None:
    # web-research.read(url=X) returning ok is the strongest evidence we have that X was fetched.
    # The result of a read is page TEXT and need not repeat its own URL at all.
    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return {"text": "Inference prices fell 40% this quarter."}

    llm = _Scripted(_Scripted.READ, f"Prices fell 40%. [Source]({_REAL})")
    result = await _run(llm, dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.unverified_links == []
    assert _steps(result, _CORRECTION_STATUS) == []


async def test_a_url_from_an_ERRORED_call_does_not_count_as_fetched() -> None:
    # A call that failed is the check working, not provenance. Counting it would let a member
    # legitimise any URL by calling a tool with it and ignoring the error.
    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"404 fetching {_REAL}")

    llm = _Scripted(_Scripted.READ, f"[Source]({_REAL})", "I could not retrieve that page.")
    result = await _run(llm, dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_steps(result, _CORRECTION_STATUS)) == 1
    assert result.output == "I could not retrieve that page."


async def test_urls_fetched_across_several_turns_all_count() -> None:
    # The set accumulates over the whole run, like served_citation_ids. A member that searches,
    # then reads, then answers must not lose the first turn's provenance.
    seen: list[str] = []

    async def dispatch(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        seen.append(spec.operation)
        if spec.operation == "search":
            return {"results": [{"url": _ALSO_REAL}]}
        return {"text": "page body"}

    llm = _Scripted(_Scripted.SEARCH, _Scripted.READ, f"[A]({_ALSO_REAL}) and [B]({_REAL})")
    result = await _run(llm, dispatch)
    assert seen == ["search", "read"]
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.unverified_links == []


# --- criterion 5: the terminal when the member never converges -----------------------------------


async def test_a_member_that_never_stops_inventing_degrades_rather_than_fails() -> None:
    llm = _Scripted(_Scripted.SEARCH, f"[Source]({_FABRICATED})")
    result = await _run(llm, _returning(_REAL), policy=_TIGHT)
    assert result.status is _EXHAUSTED_STATUS
    assert result.error_type == _EXHAUSTED_ERROR_TYPE
    assert result.output is not None  # the last draft is carried out, flagged, never discarded
    assert result.unverified_links == [_FABRICATED]


async def test_an_escalate_configured_member_still_only_degrades_on_unverified_links() -> None:
    # Guards the ruling against a per-member on_exhaustion quietly restoring the rejected hard-fail.
    # #944 asks for the link to be FLAGGED, not for the answer to be refused.
    llm = _Scripted(_Scripted.SEARCH, f"[Source]({_FABRICATED})")
    result = await _run(llm, _returning(_REAL), policy=_TIGHT_ESCALATE)
    assert result.status is _EXHAUSTED_STATUS
    assert result.error_type == _EXHAUSTED_ERROR_TYPE


async def test_a_corrected_run_failing_later_for_another_reason_is_not_reported_as_links() -> None:
    # The citation gate had exactly this bug shape: a sticky flag reporting the wrong terminal for a
    # run that moved past the blocked draft and then ran out of budget for an unrelated reason.
    llm = _Scripted(
        _Scripted.SEARCH,
        f"[Source]({_FABRICATED})",  # blocked → corrected
        _Scripted.SEARCH,  # the member moves past it with a real tool call
        _Scripted.SEARCH,
        _Scripted.SEARCH,
    )
    result = await _run(llm, _returning(_REAL), policy=_TIGHT)
    assert result.error_type != _EXHAUSTED_ERROR_TYPE
