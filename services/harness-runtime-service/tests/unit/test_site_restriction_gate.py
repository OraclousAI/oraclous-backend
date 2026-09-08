"""#961 rulings 1, 2 and 3 — a person's list of websites binds the run, checked before each search.

Ruled 2026-09-08, all three:

1. **The list BINDS the run.** Not advice a member may drop. Leaving it advisory, and checking but
   only warning, were both offered and both refused. #951's original report is a run that ignored a
   person's list and still satisfied every gate the run has.
2. **Checked immediately BEFORE each search**, refusing a search call that leaves the restriction
   out, so a member cannot skip it even once. Checking the finished run afterwards was offered and
   refused: by then the unrestricted work is paid for and the person has already waited for it.
3. **A list that genuinely cannot be honoured FINISHES, marked incomplete**, with a plain sentence.
   Not a failure. #580's shape (a retrieval that found nothing degrades to a flagged PARTIAL) is
   reused deliberately, so no new failure mode is invented. Failing the run was offered and refused:
   the vendor's search is semantic, so a genuinely empty restricted result is rare, and throwing
   away a whole run over a rare data-absence is the wrong trade.

**What this can and cannot prove.** #951's limit stands and constrains the design: a wrong address
cannot be detected, only shown. Nothing tells ``bbc.com`` from ``bbc.co.uk`` — both resolve, both
return real pages. So the check below proves that SOME restriction was applied and that every site
in it was one the person named. It can never prove the person named the right ones.

**What passes the check** (ruled 2026-09-08): a NON-EMPTY SUBSET of the sites the person named.
Searching one named site at a time is a legitimate way to work through a list, so requiring every
site on every call would refuse honest searches. Naming a site the person did not is refused —
"only use these" is what they wrote. An absent or empty ``sites`` argument is refused, because that
is the unrestricted search this whole issue exists to stop.

**The scope guard is half this file.** A run that named no sites has no restriction, and every
search in it must behave byte-for-byte as it does today. Asserted, not assumed.

**Why the trusted-binding set.** A ``ToolSpec.binding`` is the manifest author's own alias, so a
manifest can name any imported tool ``web-research``. The gate therefore fires on a binding the
REGISTRY confirmed is the first-party web search, exactly as #780/#781 resolved the citation and
data-absence trust sets. Keying on the alias would let an author dodge the whole gate by renaming
their tool, and — read the other way — would fire the gate on an unrelated tool that happened to
share the name.

RED-by-design until the ``[impl]`` lands: ``PolicyEnvelope.required_sites`` and the loop's
``web_search_bindings`` keyword do not exist yet, so every seam is reached function-locally (§4.1).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

#: What the person named on the app's form. Two sites, so a subset is distinguishable from the lot.
NAMED = ("theverge.com", "bbc.co.uk")

#: The manifest author's alias for the first-party web search. Deliberately NOT "web-research": the
#: gate must key on what the registry confirmed, never on what the author called it.
SEARCH_BINDING = "research"

_SEARCH = ToolSpec(
    name="research__search",
    description="search the live web",
    parameters={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "sites": {"type": "array", "items": {"type": "string"}},
        },
    },
    binding=SEARCH_BINDING,
    operation="search",
)

_FETCH = ToolSpec(
    name="research__fetch",
    description="fetch a page",
    parameters={"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}},
    binding=SEARCH_BINDING,
    operation="fetch",
)


def _env(**overrides: Any) -> Any:
    from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope  # §4.1 seam

    kwargs: dict[str, Any] = {
        "max_iterations": 6,
        "max_tool_calls": None,
        "max_wall_time_seconds": None,
        "max_tokens": None,
    }
    kwargs.update(overrides)
    return PolicyEnvelope(**kwargs)


class _CallsThenAnswers:
    """Makes ONE tool call with the given arguments, then answers with what it observed."""

    protocol_shape = "fake"

    def __init__(self, *calls: tuple[str, dict[str, Any]]) -> None:
        self._calls = list(calls)
        self.turns = 0

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        self.turns += 1
        if self._calls:
            name, args = self._calls.pop(0)
            return LLMResponse(text="", tool_calls=[ToolCall(f"c{self.turns}", name, args)])
        observed = [m for m in messages if m.get("role") == "tool"]
        return LLMResponse(text=f"done: {observed[-1]['content'] if observed else 'nothing'}")


class _RecordingDispatch:
    """Records every call that actually reached a tool, and returns a fixed result."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result if result is not None else {"hits": [{"title": "T"}]}

    async def __call__(self, spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((spec.operation, dict(args)))
        return dict(self._result)


async def _run(
    llm: Any,
    dispatch: Any,
    *,
    required_sites: tuple[str, ...] = NAMED,
    trusted: frozenset[str] = frozenset({SEARCH_BINDING}),
    specs: list[ToolSpec] | None = None,
    max_iterations: int = 6,
) -> Any:
    from oraclous_harness_runtime_service.domain.loop.tool_use import (  # §4.1 seam
        run_tool_use_loop,
    )

    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="round up this week's tech news",
        tool_specs=specs if specs is not None else [_SEARCH],
        dispatch=dispatch,
        policy=_env(required_sites=required_sites, max_iterations=max_iterations),
        web_search_bindings=trusted,
    )


# ── ruling 2: an unrestricted search never reaches the vendor ────────────────────────────────────


async def test_a_search_that_omits_the_restriction_is_never_dispatched() -> None:
    """The whole point. Refused BEFORE the call, so the unrestricted work is never paid for and the
    person never waits for it — the reason a terminal check was offered and refused."""
    dispatch = _RecordingDispatch()
    await _run(_CallsThenAnswers((_SEARCH.name, {"query": "tech news"})), dispatch)

    assert dispatch.calls == []


async def test_the_refusal_tells_the_member_exactly_what_to_send() -> None:
    """A member told only "refused" can do nothing but retry blindly — the #692/#693 failure, where
    a member handed "409" repeated the identical call until its budget was gone. The correction
    names the sites, so the very next call can be right."""
    dispatch = _RecordingDispatch()
    result = await _run(_CallsThenAnswers((_SEARCH.name, {"query": "tech news"})), dispatch)

    refusal = "\n".join(s.detail or "" for s in result.steps)
    assert "theverge.com" in refusal
    assert "bbc.co.uk" in refusal


async def test_a_member_that_corrects_itself_gets_its_search() -> None:
    """The refusal is a correction, not a wall. A second call carrying the restriction runs."""
    dispatch = _RecordingDispatch()
    llm = _CallsThenAnswers(
        (_SEARCH.name, {"query": "tech news"}),
        (_SEARCH.name, {"query": "tech news", "sites": ["theverge.com", "bbc.co.uk"]}),
    )
    result = await _run(llm, dispatch)

    assert [args.get("sites") for _op, args in dispatch.calls] == [["theverge.com", "bbc.co.uk"]]
    assert result.status is HarnessStatus.SUCCEEDED


@pytest.mark.parametrize("empty", [[], "", None])
async def test_an_empty_restriction_argument_is_the_same_as_none(empty: Any) -> None:
    """An empty ``sites`` is what the connector already treats as "do not restrict" — so it is the
    unrestricted search under another name, and the gate has to see it that way too."""
    dispatch = _RecordingDispatch()
    await _run(_CallsThenAnswers((_SEARCH.name, {"query": "news", "sites": empty})), dispatch)

    assert dispatch.calls == []


# ── what counts as honouring the list ────────────────────────────────────────────────────────────


async def test_one_of_the_named_sites_is_enough_for_one_search() -> None:
    """Ruled 2026-09-08: a non-empty SUBSET passes.

    Working through a list one site at a time is a legitimate way to research, and requiring the
    whole list on every call would refuse it — turning a correct search into a refusal, which is
    the false-positive direction this gate must not have.
    """
    dispatch = _RecordingDispatch()
    await _run(
        _CallsThenAnswers((_SEARCH.name, {"query": "news", "sites": ["theverge.com"]})), dispatch
    )

    assert dispatch.calls and dispatch.calls[0][1]["sites"] == ["theverge.com"]


async def test_a_site_the_person_never_named_is_refused() -> None:
    """ "Only use these" is what the person wrote. A search that quietly widens the list past what
    they named is the same defect as ignoring it, one step smaller."""
    dispatch = _RecordingDispatch()
    result = await _run(
        _CallsThenAnswers((_SEARCH.name, {"query": "news", "sites": ["theverge.com", "cnn.com"]})),
        dispatch,
    )

    assert dispatch.calls == []
    assert "cnn.com" in "\n".join(s.detail or "" for s in result.steps)


async def test_a_pasted_link_from_the_model_still_counts_as_the_named_site() -> None:
    """The model may write the address in any of its shapes. ``https://www.theverge.com/tech`` is
    ``theverge.com``, and a check that said otherwise would refuse a correct search — which is why
    both halves are cleaned by the one shared kernel function (#946's two-passes defect)."""
    dispatch = _RecordingDispatch()
    await _run(
        _CallsThenAnswers(
            (_SEARCH.name, {"query": "news", "sites": ["https://www.theverge.com/tech"]})
        ),
        dispatch,
    )

    assert len(dispatch.calls) == 1


async def test_an_unreadable_site_argument_is_refused_rather_than_dispatched() -> None:
    """A model that puts a publication NAME in the argument has not honoured the restriction, and
    #951's live finding is why it cannot be let through: the vendor accepts junk with an ordinary
    200 and silently searches the whole web."""
    dispatch = _RecordingDispatch()
    await _run(
        _CallsThenAnswers((_SEARCH.name, {"query": "news", "sites": ["BBC News"]})), dispatch
    )

    assert dispatch.calls == []


# ── the scope guard: everything that is not a restricted search is untouched ─────────────────────


async def test_a_run_with_no_restriction_dispatches_an_unrestricted_search_untouched() -> None:
    """Most searches in the platform name no sites. They must be byte-for-byte what they are today
    — this is the one assertion that keeps the gate from breaking every ordinary run."""
    dispatch = _RecordingDispatch()
    result = await _run(
        _CallsThenAnswers((_SEARCH.name, {"query": "tech news"})), dispatch, required_sites=()
    )

    assert dispatch.calls == [("search", {"query": "tech news"})]
    assert result.status is HarnessStatus.SUCCEEDED


async def test_a_tool_that_is_not_a_search_is_never_checked() -> None:
    """The restriction is about searching. Fetching a page the run already found is a different
    operation, and gating it would refuse the member's own follow-up reads."""
    dispatch = _RecordingDispatch()
    await _run(
        _CallsThenAnswers((_FETCH.name, {"url": "https://theverge.com/a"})),
        dispatch,
        specs=[_SEARCH, _FETCH],
    )

    assert dispatch.calls == [("fetch", {"url": "https://theverge.com/a"})]


async def test_a_tool_the_registry_did_not_confirm_is_never_checked() -> None:
    """The trust set, from the other side. A binding the registry did not confirm as the
    first-party web search is not gated — the alias in the manifest is the author's free choice and
    proves nothing, so a search-shaped imported tool is simply outside this gate's reach.

    This is the #780/#781 posture, restated: trust what the registry resolved, never a name.
    """
    dispatch = _RecordingDispatch()
    await _run(_CallsThenAnswers((_SEARCH.name, {"query": "news"})), dispatch, trusted=frozenset())

    assert dispatch.calls == [("search", {"query": "news"})]


# ── ruling 3: a list that yields nothing finishes, marked incomplete ─────────────────────────────


async def test_a_restricted_search_that_found_nothing_finishes_flagged_not_failed() -> None:
    """Ruled: this is data-absence, not a fault. The member proceeds with what it has and the run
    settles PARTIAL — #580's shape exactly, so no new failure mode is invented."""
    dispatch = _RecordingDispatch(result={"hits": [], "sites_yielded_nothing": True})
    result = await _run(
        _CallsThenAnswers((_SEARCH.name, {"query": "news", "sites": ["theverge.com"]})), dispatch
    )

    assert result.status is HarnessStatus.PARTIAL


async def test_the_run_says_in_a_sentence_that_the_named_sites_had_nothing() -> None:
    """A person reading the run must learn WHY it is incomplete. "PARTIAL" alone is a status code;
    the sentence is what tells them their addresses may be wrong and they can fix them."""
    dispatch = _RecordingDispatch(result={"hits": [], "sites_yielded_nothing": True})
    result = await _run(
        _CallsThenAnswers((_SEARCH.name, {"query": "news", "sites": ["theverge.com"]})), dispatch
    )

    message = (result.error_message or "").lower()
    assert "site" in message
    assert "nothing" in message or "no results" in message


async def test_the_empty_flag_never_reaches_the_model() -> None:
    """A reserved result key is platform bookkeeping. Leaving it in the tool result teaches the
    model the key is live, and a model that can WRITE it can forge the flag — the #781 defect,
    where a forged ``data_absent`` bought a softer terminal on demand."""
    dispatch = _RecordingDispatch(result={"hits": [], "sites_yielded_nothing": True})
    result = await _run(
        _CallsThenAnswers((_SEARCH.name, {"query": "news", "sites": ["theverge.com"]})), dispatch
    )

    assert "sites_yielded_nothing" not in json.dumps([s.detail for s in result.steps])
    assert "sites_yielded_nothing" not in (result.output or "")


async def test_an_untrusted_tool_cannot_forge_the_empty_flag() -> None:
    """The #781 rule, applied to this key from the start rather than after an incident.

    A tool the registry did not confirm gets its flag STRIPPED but never BELIEVED, so no imported
    server can hand the platform a softer terminal by echoing a key name it guessed.
    """
    dispatch = _RecordingDispatch(result={"hits": [], "sites_yielded_nothing": True})
    result = await _run(
        _CallsThenAnswers((_SEARCH.name, {"query": "news"})),
        dispatch,
        required_sites=(),
        trusted=frozenset(),
    )

    assert result.status is HarnessStatus.SUCCEEDED


# ── the terminal when a member never complies ────────────────────────────────────────────────────


async def test_a_member_that_never_restricts_ends_with_a_terminal_that_says_so() -> None:
    """A member can refuse to comply until its budget is gone. That must not settle as an anonymous
    "did not converge" — #946's own lesson, where a run that failed for a nameable reason reported
    nothing a person could act on. The terminal names the restriction."""

    class _NeverComplies:
        protocol_shape = "fake"

        async def complete(self, *, messages, system, tools):  # noqa: ANN001, ANN202
            return LLMResponse(text="", tool_calls=[ToolCall("c", _SEARCH.name, {"query": "n"})])

    dispatch = _RecordingDispatch()
    result = await _run(_NeverComplies(), dispatch, max_iterations=3)

    assert dispatch.calls == []
    assert result.status is not HarnessStatus.SUCCEEDED
    assert "site" in (result.error_type or "").lower()


async def test_every_refusal_is_recorded_as_its_own_step() -> None:
    """The run's trace is where a person sees that the platform held the line. A refusal that left
    no step would make an enforced run and an unenforced one look identical afterwards."""
    dispatch = _RecordingDispatch()
    result = await _run(_CallsThenAnswers((_SEARCH.name, {"query": "news"})), dispatch)

    gates = [s for s in result.steps if s.kind is StepKind.GATE]
    assert gates and any("site" in s.name for s in gates)
