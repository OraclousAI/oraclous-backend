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

**Reviewed and tightened, 2026-09-07 (#944 review, HIGH-2).** The correction described above is
gated on there being a REAL fetched set to measure against — not merely on the member HAVING tools.
A member whose every tool call errored (a spent search key, a rate-limited provider, a connector
outage — this repo has already hit a spent Tavily key on ``main``) has an EMPTY fetched set, and
under the plain ``tool_specs`` gate every one of its answers read as "every link invented", forever,
until the budget died. An empty fetched set now ships flagged on the FIRST attempt rather than
looping — there is nothing to measure the draft against, the same rationale criterion 1 already
applies to a tool-less member. And even with a real, non-empty fetched set, the correction is capped
at 2: past the cap the next attempt ships flagged rather than being sent back again, so a member
that keeps failing for its own reasons (an adversarial page instructing it to cite URLs it never
retrieved) cannot be walked through its whole iteration budget one correction at a time.
``test_a_member_that_never_stops_inventing_ships_flagged_after_the_correction_bound`` (formerly
"...degrades_rather_than_fails") and ``test_an_escalate_configured_member_still_only_degrades_on_
unverified_links`` were both rewritten for the bound; the degrade/PARTIAL terminal still exists —
it fires when the budget runs out mid-correction, before the bound is reached, never encoding "the
member never stops" as an unbounded loop.

``run_tool_use_loop``'s new keyword-free behaviour is additive, so every module-level import here is
a shipped seam and collection stays clean (``.claude/rules/tests-seam-imports.md``). The tests fail
RED on the ASSERTIONS until the ``[impl]`` lands, which is the intended shape for a change to an
existing function.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import LoopCheckpoint, run_tool_use_loop
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
# #944 review, HIGH-2: tight enough that the budget runs out DURING a correction — before the
# 2-correction bound is ever reached — so the degrade/PARTIAL terminal still has a scenario that
# exercises it now the bound exists. One search turn, one corrected attempt, then no turns left.
_TIGHTER = PolicyEnvelope(
    max_iterations=2, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
)
_TIGHTER_ESCALATE = PolicyEnvelope(
    max_iterations=2,
    max_tool_calls=None,
    max_wall_time_seconds=None,
    max_tokens=None,
    on_exhaustion="escalate",
)

# #975: two forged `cit_` ids for the citation-vs-link ordering tests below. Never served by any
# dispatch in this file unless a test's own dispatch explicitly serves one — citing either is then
# automatically a rule-2 violation, which is exactly the "fails the citation gate too" shape those
# tests need.
_CIT_ID_A = "cit_" + "a" * 32
_CIT_ID_B = "cit_" + "b" * 32


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


class _CapturingScripted(_Scripted):
    """Like ``_Scripted``, but also keeps the FULL message list of the LAST call — not just the
    user-role text ``_Scripted.user_messages`` extracts. Needed to inspect a ``tool``-role message's
    own content (the SOURCES block, its position relative to the receipt line), which is invisible
    to every existing helper in this file."""

    def __init__(self, *script: str) -> None:
        super().__init__(*script)
        self.last_messages: list[Any] = []

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.last_messages = list(messages)
        return await super().complete(messages=messages, system=system, tools=tools)


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
    # A call that failed is the check working, not provenance: _REAL must never be VERIFIED. But
    # #944 review (HIGH-2, 2026-09-07) changed the REMEDY when the fetched set is empty because
    # every call failed — there is no fetched set to measure a correction against, so the draft
    # ships flagged on the first attempt rather than being looped through a correction it
    # structurally cannot satisfy (the same rationale criterion 1 already applies to a tool-less
    # member).
    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"404 fetching {_REAL}")

    llm = _Scripted(_Scripted.READ, f"[Source]({_REAL})")
    result = await _run(llm, dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == f"[Source]({_REAL})"
    assert _steps(result, _CORRECTION_STATUS) == []
    assert result.unverified_links == [_REAL]  # flagged — never silently trusted


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


async def test_a_non_dict_tool_result_is_still_harvested() -> None:
    # A generic REST/imported-MCP tool can return a bare list, string, or scalar — `json.dumps` on
    # any of those is still a string the harvester reads the same way as a dict result. Nothing in
    # the harvest path may assume `result` is a mapping.
    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> Any:
        return [_REAL]

    llm = _Scripted(_Scripted.READ, f"[Source]({_REAL})")
    result = await _run(llm, dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.unverified_links == []
    assert _steps(result, _CORRECTION_STATUS) == []


async def test_a_url_past_the_persisted_truncation_boundary_still_counts_as_fetched() -> None:
    # The trace's persisted step `detail` is truncated at 500 characters (`_truncate`), but that
    # truncation must apply ONLY to what gets PERSISTED — the harvester reads the FULL tool result,
    # never the shortened copy, or a real citation buried in a long page would read as invented.
    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return {"text": "x" * 600 + f" {_REAL} " + "y" * 600}

    llm = _Scripted(_Scripted.READ, f"[Source]({_REAL})")
    result = await _run(llm, dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.unverified_links == []
    assert _steps(result, _CORRECTION_STATUS) == []


# --- criterion 5: the terminal when the member never converges -----------------------------------


async def test_a_member_that_never_stops_inventing_ships_flagged_after_the_bound() -> None:
    # #944 review, HIGH-2 (ruled 2026-09-07): the correction is bounded at 2, not unbounded. A
    # member whose every tool call errors, or that keeps failing for its own reasons (an
    # adversarial page instructing it to cite URLs it never retrieved), has no way to ever converge
    # — an unbounded loop would spend the WHOLE iteration budget correcting a defect the member
    # structurally cannot fix. Past the bound the next attempt SHIPS FLAGGED instead of being sent
    # back again. `_TIGHT` (4 iterations) is exactly search + 2 corrections + the shipped attempt.
    llm = _Scripted(_Scripted.SEARCH, f"[Source]({_FABRICATED})")
    result = await _run(llm, _returning(_REAL), policy=_TIGHT)
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == f"[Source]({_FABRICATED})"
    assert result.unverified_links == [_FABRICATED]  # flagged — never silently trusted
    assert len(_steps(result, _CORRECTION_STATUS)) == 2  # bounded, not one per iteration


async def test_a_budget_that_runs_out_mid_correction_still_degrades_rather_than_fails() -> None:
    # The degrade/PARTIAL terminal still exists — it fires when the budget runs out DURING a
    # correction, before the bound above is ever reached, not when "the member never stops"
    # (unbounded correction was itself the HIGH-2 defect; see the test above).
    llm = _Scripted(_Scripted.SEARCH, f"[Source]({_FABRICATED})")
    result = await _run(llm, _returning(_REAL), policy=_TIGHTER)
    assert result.status is _EXHAUSTED_STATUS
    assert result.error_type == _EXHAUSTED_ERROR_TYPE
    assert result.output is not None  # the last draft is carried out, flagged, never discarded
    assert result.unverified_links == [_FABRICATED]


async def test_an_escalate_configured_member_still_only_degrades_on_unverified_links() -> None:
    # Guards the ruling against a per-member on_exhaustion quietly restoring the rejected hard-fail.
    # #944 asks for the link to be FLAGGED, not for the answer to be refused. A budget too tight
    # to even reach the correction bound (see above) must still degrade, never escalate, on this
    # defect.
    llm = _Scripted(_Scripted.SEARCH, f"[Source]({_FABRICATED})")
    result = await _run(llm, _returning(_REAL), policy=_TIGHTER_ESCALATE)
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


# --- criterion 6: a mid-run HITL pause must not lose provenance already established ---------------
#
# #944 review, MEDIUM-3 (2026-09-07): `fetched_urls` used to start EMPTY on every resume, the same
# way `retrieval_empty` deliberately does. But a link the member genuinely fetched BEFORE a pause is
# real provenance whether or not the run was ever paused — starting fresh corrected an honest member
# for citing honestly, and published its honest links to the reader as fabricated. Modeled on how
# `test_citation_gate_loop.py` proves `prior_served_citation_ids` — but this needs no service-layer
# parameter and no persisted column: the tool-role message the member's pre-pause call produced is
# already sitting in the restored transcript.


def _paused_after_a_read(*, content: str) -> LoopCheckpoint:
    """A checkpoint whose transcript already carries one completed READ call, nothing left to
    dispatch — the resume goes straight to the model."""
    return LoopCheckpoint(
        messages=[
            {"role": "user", "content": "summarise this quarter's inference pricing"},
            {
                "role": "assistant",
                "content": "reading",
                "tool_calls": [{"id": "c1", "name": _READ.name, "args": {"url": _REAL}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": _READ.name,
                "content": f"{content}\n[receipt: source_tool_call_id=c1]",
            },
        ],
        pending_tool_calls=[],
        approved_tool_call_id="c1",
        iteration=1,
        tool_calls_made=1,
        tokens_used=10,
        redact_patterns=[],
    )


async def test_a_link_fetched_before_a_hitl_pause_still_counts_as_fetched_after_resume() -> None:
    checkpoint = _paused_after_a_read(content='{"text": "page body"}')
    llm = _Scripted(f"Prices fell. [Source]({_REAL})")
    result = await _run(llm, resume_state=checkpoint)
    assert result.status is HarnessStatus.SUCCEEDED
    assert _steps(result, _CORRECTION_STATUS) == []
    assert result.unverified_links == []


async def test_a_pre_pause_call_credits_its_url_named_argument_too() -> None:
    # Mirrors criterion 4's live-dispatch property (the READ's own `url` argument counts) across a
    # resume — the result of a read is page TEXT and need not repeat its own URL.
    checkpoint = _paused_after_a_read(content='{"text": "no url in the body at all"}')
    llm = _Scripted(f"Prices fell. [Source]({_REAL})")
    result = await _run(llm, resume_state=checkpoint)
    assert result.unverified_links == []


async def test_an_errored_pre_pause_call_is_not_credited_after_resume() -> None:
    # The resume-side re-derivation rejects a FAILED pre-pause call the same way live dispatch does
    # — crediting it would let a member legitimise any URL by having called a tool with it and
    # having the call fail, same as the live #944 rule this mirrors.
    checkpoint = _paused_after_a_read(content='{"error": "RuntimeError", "detail": "404"}')
    llm = _Scripted(f"[Source]({_REAL})")
    result = await _run(llm, resume_state=checkpoint)
    assert result.status is HarnessStatus.SUCCEEDED
    assert _steps(result, _CORRECTION_STATUS) == []  # HIGH-2: no fetched set → flag, never loop
    assert result.unverified_links == [_REAL]


# --- #944 review round 3: harvest-pipeline defects found reviewing round 2's own fixes ------------


async def test_an_all_caps_argument_name_still_counts_as_fetched() -> None:
    # MEDIUM-F: the argument-name splitter used to break a run of consecutive capitals into single
    # letters ("URL" -> "u", "r", "l"), matching no token. All-caps is the ordinary REST/MCP
    # convention for this word, so an honestly fetched URL passed as `URL=...` went uncredited and
    # cost the member a correction it did not deserve — always the SAFE direction, but a real bug.
    class _CapsArgLLM:
        protocol_shape = "fake"

        def __init__(self) -> None:
            self.turns = 0

        async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> Any:
            self.turns += 1
            if self.turns == 1:
                return LLMResponse(
                    text="reading", tool_calls=[ToolCall("c1", _READ.name, {"URL": _REAL})]
                )
            return LLMResponse(text=f"Prices fell. [Source]({_REAL})", tool_calls=[])

    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return {"text": "no url in the body at all"}

    result = await _run(_CapsArgLLM(), dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.unverified_links == []
    assert _steps(result, _CORRECTION_STATUS) == []


async def test_a_url_smuggled_through_a_non_url_named_argument_is_not_credited() -> None:
    # MEDIUM-E (coverage item K): `dispatch` receives the model's WHOLE args object with no schema
    # validation in the harness, so nothing stops a model from putting a URL under an argument name
    # that does not denote one — the review's own example, "a search whose QUERY happened to be the
    # fabricated URL". That call returning ok must not launder the URL into the set that judges the
    # member's own answer.
    class _QueryLaunderLLM:
        protocol_shape = "fake"

        def __init__(self) -> None:
            self.turns = 0

        async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> Any:
            self.turns += 1
            if self.turns == 1:
                return LLMResponse(
                    text="searching",
                    tool_calls=[ToolCall("c1", _SEARCH.name, {"query": _FABRICATED})],
                )
            return LLMResponse(text=f"[Source]({_FABRICATED})", tool_calls=[])

    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return {"results": []}  # the fabricated URL never appears in the RESULT either

    result = await _run(_QueryLaunderLLM(), dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    # HIGH-2: nothing was ever credited, so the draft ships flagged on the first attempt rather than
    # looping — the same remedy an all-tool-calls-errored member gets.
    assert _steps(result, _CORRECTION_STATUS) == []
    assert result.unverified_links == [_FABRICATED]


async def test_the_json_repair_corrections_own_prose_is_never_credited_after_resume() -> None:
    # LOW-H: the #853 JSON-repair correction is a `role: tool` message whose content is PROSE and
    # was never dispatched at all — the live path `continue`s before the harvest block, crediting
    # nothing. Before an explicit `status` marker existed, a resume's content-shape heuristic could
    # not recognise non-JSON prose as failed (only a specific JSON error shape counted as failed),
    # so the `url` argument of the never-dispatched call — never actually fetched — WAS credited,
    # but only after a resume.
    checkpoint = LoopCheckpoint(
        messages=[
            {"role": "user", "content": "summarise this quarter's inference pricing"},
            {
                "role": "assistant",
                "content": "reading",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": _READ.name,
                        "args": {"url": "https://never-fetched.example/report"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": _READ.name,
                "content": (
                    "Your last document was not valid JSON: bad token.\n"
                    "[receipt: source_tool_call_id=c1 status=error]"
                ),
            },
        ],
        pending_tool_calls=[],
        approved_tool_call_id="c1",
        iteration=1,
        tool_calls_made=1,
        tokens_used=10,
        redact_patterns=[],
    )
    llm = _Scripted("[Source](https://never-fetched.example/report)")
    result = await _run(llm, resume_state=checkpoint)
    assert result.unverified_links == ["https://never-fetched.example/report"]


async def test_an_ok_result_shaped_like_an_error_object_is_still_credited_after_resume() -> None:
    # LOW-I: `dispatch` decides ok/error by whether it raised, never by the shape of what it
    # returns — a connector's genuine ok payload can happen to look like `{"error": ...}` (a status
    # field literally named that). Before an explicit marker, the resume path GUESSED "failed" from
    # content shape alone and lost the credit — a false accusation costing the member a correction
    # it did not deserve. Same root cause as LOW-H, opposite direction.
    checkpoint = LoopCheckpoint(
        messages=[
            {"role": "user", "content": "summarise this quarter's inference pricing"},
            {
                "role": "assistant",
                "content": "reading",
                "tool_calls": [{"id": "c1", "name": _READ.name, "args": {"url": _REAL}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": _READ.name,
                "content": (
                    '{"error": "none", "detail": "queued"}\n'
                    "[receipt: source_tool_call_id=c1 status=ok]"
                ),
            },
        ],
        pending_tool_calls=[],
        approved_tool_call_id="c1",
        iteration=1,
        tool_calls_made=1,
        tokens_used=10,
        redact_patterns=[],
    )
    llm = _Scripted(f"[Source]({_REAL})")
    result = await _run(llm, resume_state=checkpoint)
    assert result.unverified_links == []


async def test_a_credential_bearing_pre_pause_argument_never_reaches_a_correction_message() -> None:
    # HIGH-D: the checkpoint's assistant `tool_calls` carry the model's RAW, unredacted arguments —
    # only `content`/`last_text` are stored redacted. A credential embedded in a pre-pause URL
    # argument must not enter `fetched_urls` in the clear, because that list is echoed verbatim into
    # a later correction message (`_link_correction`'s "you fetched: …" clause) whenever one fires.
    checkpoint = LoopCheckpoint(
        messages=[
            {"role": "user", "content": "summarise this quarter's inference pricing"},
            {
                "role": "assistant",
                "content": "reading",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": _READ.name,
                        "args": {"url": "https://api.example.com/x?api_key=SECRET123"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": _READ.name,
                "content": '{"text": "page body"}\n[receipt: source_tool_call_id=c1 status=ok]',
            },
        ],
        pending_tool_calls=[],
        approved_tool_call_id="c1",
        iteration=1,
        tool_calls_made=1,
        tokens_used=10,
        redact_patterns=[r"SECRET123"],
    )
    # Cites something the resumed fetched set does not contain at all, so every link is invented and
    # a correction fires — the path that echoes `fetched_urls` back to the model.
    llm = _Scripted(f"[Source]({_FABRICATED})")
    result = await _run(llm, resume_state=checkpoint)
    assert len(_steps(result, _CORRECTION_STATUS)) >= 1
    assert not any("SECRET123" in message for message in llm.user_messages)


# =================================================================================================
# #975 — cite-by-reference: LOOP behaviour (T2, new coverage).
#
# The registry (the loop's existing `fetched_urls` accumulator) becomes the numbered SOURCES list
# every member is shown, tools or not (ruling 5); `[Sn]` markers expand to the real URL; the #944
# raw-URL backstop stays, but its consequence hardens — a survivor is STRIPPED, never shipped
# intact (ruling 2); an unknown marker is a new offence, corrected the same bounded way a raw
# fabrication is (ruling 7).
#
# `run_tool_use_loop` does not accept `prior_fetched_urls`/`person_supplied_text` yet, and
# `LoopResult` has no `fetched_urls` attribute yet — so most of what follows is RED on
# `TypeError`/`AttributeError` today. Every test also carries an assertion that fails on BEHAVIOUR
# alone once both exist (T13): today's code ships a fabricated raw URL and a literal `[Sn]` intact,
# never expands a marker, and never gates a harvested URL through the registration rule.
# =================================================================================================


# --- a tool-less member is SHOWN its numbers before it can cite them (ruling 5) -------------------


async def test_a_tool_less_member_is_shown_a_numbered_sources_block_and_can_cite_it() -> None:
    # Ruling 5: the protocol applies to EVERY member, tools or not — the whole point of #975 is that
    # the `linker` role (no tools at all) gets a real citable number instead of nothing. It cannot
    # cite what it is never shown, so the block has to reach the FIRST (and, here, only) user turn.
    llm = _Scripted("Costs fell again [S1].")
    result = await _run(llm, specs=[], prior_fetched_urls=[_REAL])
    shown = "\n".join(llm.user_messages)
    assert f"[S1] {_REAL}" in shown  # the numbered SOURCES line, shown before it ever answers
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == f"Costs fell again [S1](<{_REAL}>)."


async def test_a_url_inside_person_supplied_text_is_a_citable_registry_seed() -> None:
    # Ruling 4: a URL the PERSON supplied (task text, intake answers) is a citable seed too, not
    # only a fetched one — a tool-less member can cite the address the person pasted into the task.
    llm = _Scripted("As requested, see [S1].")
    result = await _run(llm, specs=[], person_supplied_text=f"Please summarise {_REAL}")
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == f"As requested, see [S1](<{_REAL}>)."


# --- the raw-URL backstop hardens: stripped, never shipped intact (ruling 2) ----------------------


async def test_a_persistent_raw_fabricated_url_is_stripped_and_the_flag_step_is_recorded() -> None:
    # Ruling 2: #944's "ships flagged, never rewritten" hardens. Past the correction bound the raw
    # fabrication no longer reaches the reader at all — only the flag does.
    llm = _Scripted(_Scripted.SEARCH, f"Prices fell. [Source]({_FABRICATED})")
    result = await _run(llm, _returning(_REAL), policy=_TIGHT, prior_fetched_urls=[_ALSO_REAL])
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == "Prices fell. Source"  # stripped — #944 shipped this intact
    assert result.unverified_links == [_FABRICATED]
    flags = _steps(result, _FLAG_STATUS)
    assert len(flags) == 1
    assert flags[0].name == _GATE_NAME
    assert _FABRICATED in (flags[0].detail or "")


async def test_a_registry_matching_raw_url_survives_byte_identical() -> None:
    # The #944 backstop still credits an honestly-cited raw URL — cite-by-reference does not force
    # every citation through a marker. Written with tracking cruft the run's own fetch lacks
    # (canonical-equal, not byte-equal), so this also proves the surviving text is untouched.
    written = f"{_REAL}#pricing-table"
    llm = _Scripted(_Scripted.SEARCH, f"See {written} for detail.")
    result = await _run(llm, _returning(_REAL))
    assert result.output == f"See {written} for detail."
    assert result.unverified_links == []
    assert result.fetched_urls == [_REAL]


# --- an unknown marker is a NEW offence, corrected the same bounded way (ruling 7) ----------------


async def test_an_unknown_marker_triggers_a_correction_naming_it_then_is_stripped() -> None:
    # An unknown marker is a new offence class the raw-URL backstop never had to catch — it names
    # neither a real link nor "no link at all". Ruling 7 corrects it the same bounded way
    # (`_LINK_CORRECTION_MAX`), and it must never survive into what ships (the T3 invariant).
    llm = _Scripted(_Scripted.SEARCH, "Prices fell [S9].")
    result = await _run(llm, _returning(_REAL), policy=_TIGHT)
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_steps(result, _CORRECTION_STATUS)) == 2  # bounded at _LINK_CORRECTION_MAX
    correction = llm.user_messages[-1]
    assert "S9" in correction  # names the offending marker, not an anonymous "you cited wrongly"
    assert "[S9]" not in (result.output or "")


# --- a budget-exhausted degrade still ships through the same pass (A1/T3) -------------------------


async def test_a_budget_exhausted_degrade_ships_the_draft_stripped_and_marker_free() -> None:
    # A1/T3: `_degrade` carries the LAST DRAFT out as `.output`. That text goes through the same
    # expand+strip pass every other terminal does — a member that ran out of budget mid-correction
    # must never ship a raw fabrication, or a literal `[Sn]` naming nothing.
    llm = _Scripted(_Scripted.SEARCH, f"[Source]({_FABRICATED}) and [S9]")
    result = await _run(llm, _returning(_REAL), policy=_TIGHTER)
    assert result.status is _EXHAUSTED_STATUS
    assert _FABRICATED not in (result.output or "")
    assert "[S9]" not in (result.output or "")
    assert result.unverified_links == [_FABRICATED]


# --- `LoopResult.fetched_urls` is set on all FIVE return paths (A1) -------------------------------


async def test_fetched_urls_is_populated_on_a_succeeded_terminal() -> None:
    llm = _Scripted(_Scripted.SEARCH, "Prices fell, no links needed.")
    result = await _run(llm, _returning(_REAL, _ALSO_REAL))
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.fetched_urls == [_REAL, _ALSO_REAL]


async def test_fetched_urls_is_populated_on_a_degrade_terminal() -> None:
    llm = _Scripted(_Scripted.SEARCH, f"[Source]({_FABRICATED})")
    result = await _run(llm, _returning(_REAL), policy=_TIGHTER, prior_fetched_urls=[_ALSO_REAL])
    assert result.status is _EXHAUSTED_STATUS
    assert result.fetched_urls == [_ALSO_REAL, _REAL]  # seed first, then the harvest (seed order)


async def test_fetched_urls_is_populated_on_an_escalated_terminal() -> None:
    gated = PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
        gated_bindings=frozenset({"web-research"}),
    )
    llm = _Scripted(_Scripted.SEARCH)
    result = await _run(llm, _returning(_REAL), policy=gated, prior_fetched_urls=[_ALSO_REAL])
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "hitl_required"
    assert result.fetched_urls == [_ALSO_REAL]  # nothing dispatched yet — only the seed is in it


async def test_fetched_urls_is_populated_on_a_failed_terminal() -> None:
    class _BoomLLM:
        protocol_shape = "fake"

        async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> Any:
            raise RuntimeError("model endpoint down")

    result = await _run(_BoomLLM(), specs=[], prior_fetched_urls=[_REAL])
    assert result.status is HarnessStatus.FAILED
    assert result.fetched_urls == [_REAL]


async def test_fetched_urls_is_populated_on_a_budget_terminal() -> None:
    policy = PolicyEnvelope(
        max_iterations=6, max_tool_calls=0, max_wall_time_seconds=None, max_tokens=None
    )
    llm = _Scripted(_Scripted.SEARCH)
    result = await _run(llm, _returning(_REAL), policy=policy, prior_fetched_urls=[_ALSO_REAL])
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "tool_call_budget"
    assert result.fetched_urls == [_ALSO_REAL]


# --- a JSON answer stays parseable through the pass (T8) ------------------------------------------


async def test_a_json_answer_with_a_marker_stays_parseable_through_the_pass() -> None:
    # T8: a member with a declared output contract answers with JSON — the engine parses the
    # declared keys OUT OF THE REWRITTEN TEXT (team_run.py). Expansion must not break the encoding.
    import json

    llm = _Scripted(_Scripted.SEARCH, '{"summary": "Costs fell [S1]."}')
    result = await _run(llm, _returning(_REAL), prior_fetched_urls=[_ALSO_REAL])
    assert result.status is HarnessStatus.SUCCEEDED
    parsed = json.loads(result.output or "")
    assert parsed["summary"] == f"Costs fell [S1](<{_ALSO_REAL}>)."


# --- citation-then-link order; `cit_` ids survive the strip (T10) ---------------------------------


async def test_a_draft_failing_both_gates_is_corrected_citation_first() -> None:
    # #792's terminal precedence is older and unchanged by #975; this is the TURN-level ordering —
    # the citation gate runs and `continue`s before the link pass ever sees the draft, so one turn
    # failing both never spends two corrections at once.
    llm = _Scripted(
        _Scripted.SEARCH,
        f"See {_CIT_ID_A} and also [Source]({_FABRICATED}).",
        "Prices fell, no further detail.",
    )
    result = await _run(llm, _returning(_REAL))
    assert result.status is HarnessStatus.SUCCEEDED
    citation_steps = [s for s in result.steps if s.kind is StepKind.GATE and s.name == "citation"]
    assert len(citation_steps) == 1
    assert _steps(result, _CORRECTION_STATUS) == []  # the link pass never got a turn of its own
    assert result.output == "Prices fell, no further detail."
    assert result.fetched_urls == [_REAL]


async def test_cit_ids_in_the_shipped_answer_survive_the_strip() -> None:
    # The strip pass targets URLs, never `cit_` tokens — a served, valid citation sitting next to an
    # unverified raw URL in the SAME accepted draft must come out untouched.
    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return {"results": [{"url": _REAL}], "served_citation_ids": [_CIT_ID_B]}

    llm = _Scripted(_Scripted.SEARCH, f"See {_CIT_ID_B} and also [Source]({_FABRICATED}).")
    result = await _run(llm, dispatch, citation_bindings=frozenset({"web-research"}), policy=_TIGHT)
    assert result.status is HarnessStatus.SUCCEEDED
    assert _CIT_ID_B in (result.output or "")
    assert _FABRICATED not in (result.output or "")


# --- resume seeds `prior_fetched_urls` FIRST — no renumbering across a pause (A3) -----------------


async def test_resume_seeds_prior_fetched_urls_first_so_numbers_do_not_shift() -> None:
    # A3: the service passes the PERSISTED union as `prior_fetched_urls` on a resume, the same
    # pattern as `prior_served_citation_ids`. It must seed BEFORE the transcript's own re-derived
    # entries, or a number a member is shown after resume would point at a different URL than before
    # the pause.
    checkpoint = _paused_after_a_read(content='{"text": "page body"}')
    llm = _Scripted("Costs fell [S1] and [S2].")
    result = await _run(llm, resume_state=checkpoint, prior_fetched_urls=[_ALSO_REAL])
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == f"Costs fell [S1](<{_ALSO_REAL}>) and [S2](<{_REAL}>)."


# --- the registration gate: a refused entry never earns a number, but still gets flagged (S1) -----


async def test_a_gate_refused_harvested_url_is_never_registered_but_is_still_flagged_if_cited() -> (
    None
):
    # S1: the registration gate applies to every SOURCE, including a tool's own harvest — a
    # userinfo phishing shape or an over-length string must never earn a real [Sn] number just
    # because a tool happened to return it. The #944 raw-URL backstop still catches it when the
    # member cites it raw anyway.
    phishing = "https://arxiv.org@evil.example/paper"
    oversized = "https://example.org/" + ("a" * 2048)
    llm = _Scripted(_Scripted.SEARCH, f"[A]({phishing}) [B]({_REAL})")
    result = await _run(llm, _returning(phishing, oversized, _REAL), policy=_TIGHT)
    assert result.status is HarnessStatus.SUCCEEDED
    assert phishing not in result.fetched_urls
    assert oversized not in result.fetched_urls
    assert _REAL in result.fetched_urls
    assert result.unverified_links == [phishing]
    assert phishing not in (result.output or "")


# --- SOURCES lines sit before the receipt line (A6); the per-call cap holds (A7) ------------------


async def test_sources_lines_sit_before_the_receipt_line_in_the_tool_result_message() -> None:
    # A6: `_split_receipt` classifies anything AFTER the receipt as corruption — a SOURCES block
    # this run adds to a tool result has to land before it, never after.
    llm = _CapturingScripted(_Scripted.SEARCH, "no links here")
    result = await _run(llm, _returning(_REAL))
    assert result.status is HarnessStatus.SUCCEEDED
    tool_message = next(m for m in llm.last_messages if m.get("role") == "tool")
    content = tool_message["content"]
    assert f"[S1] {_REAL}" in content
    assert content.index(f"[S1] {_REAL}") < content.index("\n[receipt: ")


async def test_the_per_call_sources_cap_holds() -> None:
    # A7: uncapped SOURCES lines on one tool result would balloon every later turn's prompt in
    # proportion to a single page's harvest. Naming the cap as a module constant makes changing it a
    # decision, not a silent drift. Registration itself is NOT capped by this — only the displayed
    # lines are (uncapped entries stay verifiable via the raw-URL match).
    from oraclous_harness_runtime_service.domain.loop.tool_use import _SOURCES_PER_CALL_MAX

    assert _SOURCES_PER_CALL_MAX == 20  # small, named, and pinned here

    many_urls = [f"https://example.org/{i}" for i in range(_SOURCES_PER_CALL_MAX + 5)]
    llm = _CapturingScripted(_Scripted.SEARCH, "no links here")
    result = await _run(llm, _returning(*many_urls))
    assert result.status is HarnessStatus.SUCCEEDED
    tool_message = next(m for m in llm.last_messages if m.get("role") == "tool")
    lines = [ln for ln in tool_message["content"].splitlines() if ln.startswith("[S")]
    assert len(lines) == _SOURCES_PER_CALL_MAX  # first-registered win — the tail gets no line
    assert f"[S1] {many_urls[0]}" in tool_message["content"]
    assert result.fetched_urls[-1] == many_urls[-1]  # still REGISTERED, just never shown a line


# --- an empty registry never loops (ruling 3) -----------------------------------------------------


async def test_an_empty_registry_never_loops_first_pass_strips_and_flags() -> None:
    # Ruling 3 keeps #944's HIGH-2 fix: a correction needs a REAL registry to measure against. An
    # empty one ships flagged — and now stripped — on the very first attempt, never looped.
    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("404")

    llm = _Scripted(_Scripted.READ, f"See {_REAL} for detail.")
    result = await _run(llm, dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert _steps(result, _CORRECTION_STATUS) == []  # nothing to correct against — ships on pass 1
    assert result.unverified_links == [_REAL]
    assert _REAL not in (result.output or "")  # stripped, not merely flagged
    assert "See" in (result.output or "")
    assert result.fetched_urls == []
