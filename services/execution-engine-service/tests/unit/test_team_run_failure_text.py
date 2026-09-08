"""Unit (#946 T3): a failed run's message reads as a sentence, not as an exception.

When a member fails, the loop's ``except`` handler records the failure as the JSON blob it fed back
to the model — ``{"error": "RegistryError", "detail": "..."}``. The team-run service used to
interpolate that blob verbatim into the run's ``error_message``, so the person who asked for a
digest of the week's AI news read ``RegistryError`` and a pair of braces on their screen.

Curate at that seam. Everything the text already carried stays: the counts, the statement that the
run is re-runnable, which members failed and which were blocked, the 2000-character cap, and the
rule that no upstream body is ever echoed. The raw per-member detail is untouched where it belongs
— on the run's step trace, which is what a debugging operator reads.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def summarise_failed_run(**kwargs: object) -> str:
    """Function-local import of the seam under test (.claude/rules/tests-seam-imports.md).

    Module-level it would abort collection for the whole run until the `[impl]` lands; here it
    hard-fails RED on this file's tests alone, which is the intended shape.
    """
    from oraclous_execution_engine_service.services.team_run_service import (
        summarise_failed_run as _impl,
    )

    return _impl(**kwargs)  # type: ignore[arg-type,no-any-return]


def _loop_error(name: str, detail: str) -> str:
    """Exactly what the tool-use loop records for a failed member."""
    return json.dumps({"error": name, "detail": detail})


# --- the text no longer reads as an exception ----------------------------------------------------


def test_it_carries_no_exception_class_name() -> None:
    text = summarise_failed_run(
        failed=["researcher"],
        blocked=[],
        member_errors={
            "researcher": _loop_error("RegistryError", "unknown search provider 'The Verge'")
        },
    )
    assert "RegistryError" not in text
    # An explicit set, not `"Error" not in text.replace("error", "")`. That expression removes
    # nothing a capital-E class name is made of, so it is identical to the plain check for every
    # realistic input — except that it can SYNTHESIZE one: "Eerrorrror" collapses to "Error". It
    # also misses every class name that does not end in "Error".
    for class_name in ("RegistryError", "RuntimeError", "TimeoutError", "Exception", "Traceback"):
        assert class_name not in text


def test_it_carries_no_json_punctuation() -> None:
    text = summarise_failed_run(
        failed=["researcher"],
        blocked=[],
        member_errors={
            "researcher": _loop_error("RegistryError", "unknown search provider 'The Verge'")
        },
    )
    for token in ('{"', '"}', '":', '", "'):
        assert token not in text


def test_the_useful_half_of_the_blob_survives() -> None:
    text = summarise_failed_run(
        failed=["researcher"],
        blocked=[],
        member_errors={
            "researcher": _loop_error("RegistryError", "unknown search provider 'The Verge'")
        },
    )
    assert "unknown search provider 'The Verge'" in text


def test_the_blob_is_unwrapped_where_production_actually_puts_it() -> None:
    """The real recorded shape, not the convenient one.

    By the time a member's failure reaches the curation, the orchestrator has wrapped it in its own
    prose: ``member 'a' harness did not succeed: FAILED — {"error": …}`` (`orchestrate.py` records
    `str(exc)`). An unwrapper that only matched a LEADING brace fires on none of these — the blob
    reaches the run page untouched while every isolated unit test stays green. That is exactly what
    happened, and it is why `test_team_run_service.py` also drives this through the real service.

    The wrapper prose goes with the class name: it names our own internals and helps nobody.
    """
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={
            "a": "member 'a' harness did not succeed: FAILED — "
            + _loop_error("RegistryError", "unknown search provider 'The Verge'")
        },
    )
    assert "RegistryError" not in text
    assert "harness did not succeed" not in text
    assert "unknown search provider 'The Verge'" in text


def test_a_sentence_containing_a_brace_is_not_mistaken_for_a_blob() -> None:
    recorded = "the template placeholder {name} was never filled in"
    text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": recorded})
    assert recorded in text


def test_a_plain_string_error_is_passed_through_unharmed() -> None:
    # not every recorded failure is the loop's JSON shape — a dispatch error is already a sentence
    text = summarise_failed_run(
        failed=["writer"],
        blocked=[],
        member_errors={"writer": "the web-search credential was rejected by the provider"},
    )
    assert "the web-search credential was rejected by the provider" in text


def test_a_blob_with_no_detail_still_says_something_useful() -> None:
    text = summarise_failed_run(
        failed=["writer"],
        blocked=[],
        member_errors={"writer": json.dumps({"error": "TimeoutError"})},
    )
    assert "writer" in text
    assert "TimeoutError" not in text
    assert text.strip()


# --- everything it carried before, it still carries -----------------------------------------------


def test_it_names_which_members_failed_and_which_were_blocked() -> None:
    text = summarise_failed_run(
        failed=["researcher", "editor"],
        blocked=["publisher"],
        member_errors={"researcher": _loop_error("RegistryError", "no key")},
    )
    for name in ("researcher", "editor", "publisher"):
        assert name in text


def test_it_still_reports_the_counts() -> None:
    text = summarise_failed_run(
        failed=["a", "b"],
        blocked=["c"],
        member_errors={},
    )
    # the rendered phrases, not bare digits — "2" alone is satisfied by a member named "member-2"
    assert "2 of its members failed" in text
    assert "1 could not start" in text


def test_it_still_says_the_run_can_be_rerun() -> None:
    text = summarise_failed_run(failed=["a"], blocked=[], member_errors={})
    assert "re-run" in text.lower()


def test_it_still_fits_the_existing_cap() -> None:
    text = summarise_failed_run(
        failed=[f"researcher-{i}" for i in range(200)],
        blocked=[f"blocked-{i}" for i in range(200)],
        member_errors={
            f"researcher-{i}": _loop_error("RegistryError", "x" * 400) for i in range(200)
        },
    )
    assert len(text) <= 2000


def test_the_cap_never_cuts_mid_word_leaving_a_dangling_fragment() -> None:
    """The role names are 13 characters and the blocked list is populated, both on purpose
    (qa-engineer, rounds 1 and 2). Without the long names the summary lands 11 characters short of
    the 2000-character whole; without the blocked list it lands short again, at 1907. Either way
    this test never reached the cut it is named for, and its assertion saw a full stop rather than
    the ellipsis it exists to check."""
    text = summarise_failed_run(
        failed=[f"researcher-{i}" for i in range(200)],
        blocked=[f"blocked-{i}" for i in range(200)],
        member_errors={
            f"researcher-{i}": _loop_error("RegistryError", "x" * 400) for i in range(200)
        },
    )
    assert len(text) == 2000  # the cut really fires
    assert text.endswith("…")


def test_a_failure_with_no_recorded_detail_still_names_the_member() -> None:
    text = summarise_failed_run(failed=["ghost"], blocked=[], member_errors={})
    assert "ghost" in text


def test_truncation_keeps_the_counts_and_the_rerunnable_statement() -> None:
    # T3 says the counts and the re-runnable statement STAY. A cap that cut the front of the text
    # to fit the reasons would satisfy the length assertion and drop both.
    text = summarise_failed_run(
        failed=[f"researcher-{i}" for i in range(200)],
        blocked=[f"blocked-{i}" for i in range(200)],
        member_errors={
            f"researcher-{i}": _loop_error("RegistryError", "x" * 400) for i in range(200)
        },
    )
    assert "200 of its members failed" in text
    assert "re-run" in text.lower()
    assert "researcher-0" in text


def test_the_curation_never_mutates_what_it_was_handed() -> None:
    # T3 criterion 4: the raw per-member detail stays available for debugging. The step trace is
    # written elsewhere and is not this function's to keep — but a curation that edited the mapping
    # in place would destroy the raw detail at its source, which is the one way this function could
    # break that criterion.
    recorded = _loop_error("RegistryError", "unknown search provider 'The Verge'")
    member_errors = {"researcher": recorded}
    summarise_failed_run(failed=["researcher"], blocked=[], member_errors=member_errors)
    assert member_errors == {"researcher": recorded}


# --- leak-safety is unchanged --------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.operator_separation
def test_an_upstream_body_is_not_reconstructed_by_the_curation() -> None:
    # the curation only ever removes; it must never add a field the recorded error did not carry
    detail = "the search provider returned 503"
    text = summarise_failed_run(
        failed=["researcher"],
        blocked=[],
        member_errors={
            "researcher": json.dumps(
                {"error": "SearchProviderError", "detail": detail, "body": "<html>secret</html>"}
            )
        },
    )
    assert "secret" not in text
    assert detail in text


# --- the shapes production actually records (#946 review round 2, C2/C3) --------------------------
#
# Four distinct shapes reach this function, from six sites in the orchestrator. The wrapped blob is
# covered above. These are the remaining two that were wrong.


def test_a_bare_exception_class_name_does_not_survive() -> None:
    """The original defect, by a route the other tests never took.

    The orchestrator records ``str(exc) or type(exc).__name__``, so an exception raised with no
    message — ``RegistryError()``, ``KeyError()``, a cancelled task — is recorded as nothing but its
    class name. There is no blob to unwrap, so it passed straight through onto the run page: exactly
    the text #946 was filed about, arriving by a different door.
    """
    text = summarise_failed_run(
        failed=["researcher"], blocked=[], member_errors={"researcher": "RegistryError"}
    )
    assert "RegistryError" not in text
    assert "researcher" in text  # the member is still named
    assert text.strip().endswith((".", "…"))


def test_a_class_name_is_replaced_by_something_a_person_can_read() -> None:
    text = summarise_failed_run(
        failed=["researcher"], blocked=[], member_errors={"researcher": "TimeoutError"}
    )
    # not simply deleted — a member that failed with no reason recorded should say so
    tail = text.split("It can be re-run.", 1)[-1]
    assert "researcher" in tail
    assert len(tail.split()) > 3


def test_a_real_sentence_that_merely_ends_in_error_is_not_mistaken_for_a_class_name() -> None:
    recorded = "the workspace could not be reached because of a network error"
    text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": recorded})
    assert recorded in text


def test_prose_carrying_an_unrecognised_json_fragment_keeps_its_prose() -> None:
    """C3. ``grounding:`` and output-contract errors quote the member's own material, which can be
    valid JSON. Dropping the whole reason because a brace parsed leaves the run page saying only
    that the member failed — strictly worse than the blob it was meant to remove.
    """
    text = summarise_failed_run(
        failed=["analyst"],
        blocked=[],
        member_errors={"analyst": 'grounding: claim not supported: {"n": 1}'},
    )
    assert "grounding: claim not supported" in text


def test_an_unrecognised_fragment_trails_the_reason_rather_than_replacing_it() -> None:
    """The security property is that the model cannot SUPPLY the reason, not that its words never
    appear (security audit, finding 2). A grounding error quotes the member on purpose, and the
    quote is evidence. What must never happen is the quote standing where the platform's own verdict
    should be — so the real reason leads, and anything the member wrote trails it."""
    text = summarise_failed_run(
        failed=["analyst"],
        blocked=[],
        member_errors={"analyst": 'grounding: claim not supported: {"n": 1}'},
    )
    reason = text.split("analyst stopped because ", 1)[1]
    assert reason.startswith("grounding: claim not supported")


def test_a_bare_unrecognised_json_object_still_shows_nothing() -> None:
    """Unchanged: a recorded value that is ONLY a blob has no prose to keep, and falling back to
    the raw text would put the blob straight back on the page."""
    text = summarise_failed_run(
        failed=["a"], blocked=[], member_errors={"a": '{"payload": {"rows": 12}}'}
    )
    assert "payload" not in text


def test_the_other_recorded_shapes_pass_through_intact() -> None:
    # the two orchestrator shapes that were already sentences, pinned so a later change to the
    # curation cannot quietly swallow them
    for recorded in (
        "grounding: claim 1 has no receipt; claim 2 has no receipt",
        "member 'a' declared an output contract it did not deliver: missing 'digest'",
        "loop did not converge: ESCALATED",
    ):
        text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": recorded})
        assert recorded in text


# --- the orchestrator's own wrapper prose is not a reason (#946 review round 3, N2) ---------------


def test_the_wrapper_prose_is_not_promoted_into_the_reason() -> None:
    """Keeping the prose around an unrecognised fragment (round 2, C3) had a side effect: when the
    prose IS the orchestrator's own wrapper, it became the reason.

    "member 'a' harness did not succeed: FAILED" names the member a second time and tells the reader
    nothing they did not get from "Failed: a." one sentence earlier. It is internal phrasing, which
    is exactly what this curation exists to remove.
    """
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={
            "a": "member 'a' harness did not succeed: FAILED — " + json.dumps({"error": "KeyError"})
        },
    )
    assert "harness did not succeed" not in text
    assert "FAILED" not in text
    assert "KeyError" not in text


def test_the_wrapper_with_no_blob_at_all_is_also_not_a_reason() -> None:
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={"a": "member 'a' harness did not succeed: ESCALATED"},
    )
    assert "harness did not succeed" not in text
    assert "ESCALATED" not in text


def test_real_prose_around_a_fragment_is_still_kept() -> None:
    # the C3 behaviour must survive the N2 fix — only the WRAPPER is recognised and dropped
    text = summarise_failed_run(
        failed=["analyst"],
        blocked=[],
        member_errors={"analyst": 'grounding: claim not supported: {"n": 1}'},
    )
    assert "grounding: claim not supported" in text


# --- the accepted loss in the class-name guard (#946 review round 3, N3) --------------------------


def test_a_one_word_capitalised_diagnostic_is_treated_as_a_class_name() -> None:
    """A recorded loss, pinned so it is a decision rather than a surprise.

    The guard matches ANY single capitalised token, not only names ending in 'Error'. That swallows
    real one-word diagnostics — 'Forbidden', 'Timeout', a Postgres code like 'P0001'. The bias is
    deliberate: #946 is about a class name reaching the page, so failing safe is the right
    direction, and requiring the 'Error' suffix would let a custom 'RegistryFault' straight through
    — failing open on precisely the thing the issue is about.
    """
    for value in ("Timeout", "Forbidden", "QuotaExceeded", "P0001", "StopIteration"):
        text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": value})
        assert value not in text
        assert "without reporting a reason" in text


def test_a_lowercase_one_word_diagnostic_is_kept() -> None:
    text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": "cancelled"})
    assert "cancelled" in text


# --- the wrapper is a PREFIX, not the whole value (#946 review round 4, R1) -----------------------
#
# The round-3 fix only fired when the orchestrator's wrapper was the entire recorded value, which in
# production it almost never is: a detail is appended whenever the harness reported one, and after
# this issue's own loop change that detail is the new terminal message. So the most likely route for
# the #946 scenario — a member that never adapts, escalating at the iteration cap — was still
# rendering the wrapper.


def test_the_wrapper_is_stripped_when_a_real_detail_follows_it() -> None:
    text = summarise_failed_run(
        failed=["researcher"],
        blocked=[],
        member_errors={
            "researcher": "member 'researcher' harness did not succeed: ESCALATED — tool-use loop "
            "did not converge — it kept re-sending a call that could not work (web-research.search)"
        },
    )
    assert "harness did not succeed" not in text
    assert "ESCALATED" not in text
    assert "it kept re-sending a call that could not work (web-research.search)" in text
    # the member is named once, by the "Failed:" line — not a second time inside its own reason
    assert text.count("researcher") == 2  # "Failed: researcher." + "researcher stopped because"


def test_the_simulated_model_warning_survives_the_strip() -> None:
    """#907 added this marker deliberately, so a reader knows the model was a stand-in. Swallowing
    it with the wrapper would lose a warning that changes how the result should be read."""
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={"a": "member 'a' harness did not succeed: FAILED — boom (simulated LLM)"},
    )
    assert "simulated LLM" in text
    assert "boom" in text
    assert "harness did not succeed" not in text


def test_the_simulated_marker_alone_is_still_worth_showing() -> None:
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={"a": "member 'a' harness did not succeed: FAILED (simulated LLM)"},
    )
    assert "simulated LLM" in text
    assert "harness did not succeed" not in text


def test_a_wrapper_with_nothing_after_it_still_says_no_reason_was_recorded() -> None:
    # every shape the round-3 fix already handled must keep its behaviour
    for recorded in (
        "member 'a' harness did not succeed: FAILED",
        "member 'a' harness did not succeed: ESCALATED",
        "member 'a' harness did not succeed:",
        "member 'the lead researcher' harness did not succeed: FAILED",
    ):
        text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": recorded})
        assert "harness did not succeed" not in text
        assert "without reporting a reason" in text


# --- #946 review round 5: the marker, the stray brace, and the cap -------------------------------
#
# Round 4 put the wrapper strip on a prefix, which is the shape production actually records. Three
# things the round-4 tests did not reach, because every one of them used an input with NO error blob
# in it — and the blob path is the one the #946 scenario itself takes.


def test_the_simulated_marker_survives_the_blob_path_too() -> None:
    """HIGH-1. ``team_run.py`` appends ``(simulated LLM)`` AFTER the detail, so on the blob path the
    marker sits past the blob's closing brace. ``raw_decode`` stops at that brace and the remainder
    is discarded, taking #907's warning with it — the exact shape a stand-in model produces whenever
    a member failed on a tool error, which is most of them.
    """
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={
            "a": "member 'a' harness did not succeed: FAILED — "
            + _loop_error("RegistryError", "unknown search provider")
            + " (simulated LLM)"
        },
    )
    assert "unknown search provider" in text
    assert "simulated LLM" in text
    assert "RegistryError" not in text


@pytest.mark.parametrize(
    "recorded",
    [
        # no brace anywhere
        "grounding: claim 1 has no receipt (simulated LLM)",
        # a brace that opens no valid JSON
        "the model wrote {oops and stopped (simulated LLM)",
        # prose around a JSON object with no `detail` key
        'grounding: claim not supported: {"n": 1} (simulated LLM)',
        # the wrapper and nothing else
        "member 'a' harness did not succeed: FAILED (simulated LLM)",
        # the wrapper, a real detail, no blob
        "member 'a' harness did not succeed: FAILED — boom (simulated LLM)",
    ],
)
def test_the_marker_survives_every_reason_path(recorded: str) -> None:
    """One marker rule, not five. The warning changes how the whole result should be read, so which
    internal branch produced the reason must not decide whether the reader is told."""
    text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": recorded})
    assert "simulated LLM" in text, recorded


def test_the_marker_is_not_repeated() -> None:
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={"a": "member 'a' harness did not succeed: FAILED — boom (simulated LLM)"},
    )
    assert text.count("(simulated LLM)") == 1


def test_the_blob_is_read_only_where_the_orchestrator_puts_it() -> None:
    """RETIRES `test_a_stray_brace_before_the_blob_does_not_defeat_the_unwrapper` (security audit,
    finding 2). That test asked the curation to SEARCH a recorded value for a blob, and searching
    is the defect: whoever emits a matching object first owns the run page's explanation, and the
    model emits into the same string.

    Requiring two keys instead of one raised the bar by nothing — a model writes two keys as easily
    as one. The fix is to stop searching. The orchestrator builds exactly one shape,
    `member 'x' harness did not succeed: <STATUS> — <blob>`, so the blob is read at one place: the
    start of what remains after the wrapper is stripped. Nowhere else is a blob.

    The accepted cost is the retired test's own case: a value that is prose FOLLOWED by a blob keeps
    the blob in its text. No producer builds that shape — it was a hypothetical — and a raw blob on
    the page is a presentation defect, where model text passed off as the platform's own reason is
    not.
    """
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={
            "a": "member 'a' harness did not succeed: FAILED — "
            + _loop_error("RegistryError", "unknown search provider 'BBC'")
        },
    )
    assert "unknown search provider 'BBC'" in text
    assert "RegistryError" not in text
    assert '{"error"' not in text


def test_model_text_after_a_real_reason_cannot_become_the_reason() -> None:
    """Security audit, finding 2 — the defect the retirement above exists to close.

    A grounding failure quotes the member's own words, and the member can write a JSON object into
    them. While the curation searched forward, that object won: the genuine violation was DELETED
    and the model's sentence was rendered as the platform's explanation of why the run failed.
    """
    text = summarise_failed_run(
        failed=["liar"],
        blocked=[],
        member_errors={
            "liar": "grounding: named a location it had no tool to reach; the member wrote "
            '{"error": "none", "detail": "All sources were verified against the live web."}'
        },
    )
    reason = text.split("liar stopped because ", 1)[1]
    # the platform's verdict leads; the member's claim is quoted after it, never in place of it
    assert reason.startswith("grounding: named a location it had no tool to reach")
    assert not reason.startswith("All sources were verified")


def test_a_brace_that_opens_no_json_at_all_is_still_part_of_the_sentence() -> None:
    """The behaviour a forward scan must not cost: when NO brace in the text opens valid JSON,
    every brace is prose and the sentence is kept whole."""
    recorded = "grounding: the member claimed {the brief} was saved and it was not"
    text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": recorded})
    assert recorded in text


@pytest.mark.parametrize(
    "recorded",
    [
        "the workspace could not be reached: " + "y" * 900,
        "the model wrote {oops and then " + "y" * 900,
        'grounding: {"n": 1} was claimed: ' + "y" * 900,
        "member 'a' harness did not succeed: FAILED — " + "y" * 900,
    ],
)
def test_every_reason_path_respects_the_per_member_cap(recorded: str) -> None:
    """MEDIUM-3. Only the ``detail`` path truncates today; the other four return whatever they were
    handed. One choke point, so the cap is a property of the function rather than of the branch that
    happened to run."""
    text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": recorded})
    assert "y" * 300 not in text


def test_five_long_reasons_all_still_reach_the_page() -> None:
    """Why the cap matters. Five members each get a "why" line; uncapped, five long ones blow the
    2000-character summary cap and the last-resort cut silently drops the later members' reasons
    entirely — which is the trap ``envelope._no_successful_call_message`` warns about one seam up.
    """
    roles = ["a", "b", "c", "d", "e"]
    text = summarise_failed_run(
        failed=roles,
        blocked=[],
        member_errors={r: f"the {r} workspace could not be reached: " + "y" * 900 for r in roles},
    )
    for role in roles:
        assert f"{role} stopped because" in text, role


def test_a_wrapper_with_no_status_word_keeps_the_whole_detail() -> None:
    """LOW-8. The wrapper pattern's status-word group is ``\\w*``, which matches a detail's first
    WORD just as happily when the status is missing. Unreachable through ``team_run.py`` today, and
    a silent word-eater is not a thing to leave armed in text a person reads.
    """
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={"a": "member 'a' harness did not succeed:  timeout after 30s"},
    )
    assert "timeout after 30s" in text


# --- the per-member limit must not undercut the grounding budget (#946 review round 5, MEDIUM-1) --
#
# `packages/ohm`'s grounding message is sized at 280 characters ON PURPOSE, so it fits inside a
# 300-character whole on the run page (#685), and several such errors are joined before they reach
# this function. Cutting a member's reason at 200 throws away the tail of a message that was
# deliberately built to fit — and the tail is the payload: the invented location the member named
# is appended LAST, after the rule that was broken.
#
# The result is the #946 symptom rebuilt at a different seam: the page says a rule was broken and
# refuses to say by what. It is not a space problem — the whole summary was using 326 of its 2000
# characters while throwing information away.

_TWO_GROUNDING_ERRORS = (
    "grounding: artifact_refs names 2 location(s), 0 of which any tool call returned; "
    "named a location it had no tool to reach and was never handed: "
    "Interrail_B.V./Identified_Risks/regulatory-exposure-2026.md"
)


def test_a_grounding_message_keeps_the_location_it_names() -> None:
    text = summarise_failed_run(
        failed=["analyst"], blocked=[], member_errors={"analyst": _TWO_GROUNDING_ERRORS}
    )
    # the whole recorded message survives — the tail is the actionable half
    assert "Interrail_B.V./Identified_Risks/regulatory-exposure-2026.md" in text
    assert "…" not in text


def test_the_per_member_limit_is_at_least_one_grounding_message() -> None:
    """A regression guard on the number itself. `packages/ohm` sizes ONE grounding message to fit a
    300-character whole; a per-member limit below that silently overrides another team's decision
    from a different file, where nobody looking at either one would see the conflict.

    Deliberately scoped to ONE message. Up to two are joined before they reach this seam, so the
    real arriving ceiling is higher and a member that both names a bad reference AND invents a long
    path still loses its tail. That is accepted: at 300 the path's front survives, so the reason
    stays actionable. Asserting the joined ceiling here would demand a limit that no longer fits
    five members inside the page's own 2000-character whole.
    """
    from oraclous_execution_engine_service.services.team_run_service import (
        _FAILURE_SUMMARY_MAX_DETAIL_CHARS,
    )
    from oraclous_ohm.envelope import _MESSAGE_CAP

    assert _FAILURE_SUMMARY_MAX_DETAIL_CHARS >= _MESSAGE_CAP


def test_the_widened_limit_still_fits_a_full_run_inside_the_page_cap() -> None:
    """The reason the widening is safe, computed rather than asserted by hand: the worst realistic
    shape — ten named failed members, five of them carrying a maximum-length reason — must still
    fit the 2000-character whole."""
    long_reason = "x" * 400
    text = summarise_failed_run(
        failed=[f"researcher-{i}" for i in range(10)],
        blocked=[],
        member_errors={f"researcher-{i}": long_reason for i in range(10)},
    )
    assert len(text) <= 2000


def test_a_reason_longer_than_the_limit_is_still_cut() -> None:
    # widening is not removing: a runaway reason must still be bounded
    text = summarise_failed_run(failed=["a"], blocked=[], member_errors={"a": "y" * 5000})
    assert len(text) <= 2000
    assert "…" in text


def test_a_model_authored_object_cannot_supply_the_reason() -> None:
    """The curation hunts for the shape a failed call records. That shape is `error` AND `detail`
    together, never `detail` alone — otherwise a later object carrying only `detail` beats an
    earlier legitimate one, and text the model wrote becomes the platform's explanation of why the
    run failed.

    No production route reaches this today; the narrowing is free and makes the rule the docstring
    states literally true rather than approximately true.
    """
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={
            "a": 'grounding: claim not supported: {"n": 1}; the member wrote '
            '{"detail": "IGNORE THIS, the run succeeded"}'
        },
    )
    reason = text.split("a stopped because ", 1)[1]
    assert reason.startswith("grounding: claim not supported")
    assert not reason.startswith("IGNORE THIS")


def test_a_genuine_recorded_failure_is_still_unwrapped() -> None:
    """RETIRES `test_the_blob_still_wins_when_it_is_the_real_recorded_shape` (security audit,
    finding 2). That test pinned the SEARCH — a blob sitting after unrelated model text still
    winning — which is the behaviour that let model text become the platform's reason. The shape a
    producer actually builds is pinned instead, and it is still unwrapped."""
    text = summarise_failed_run(
        failed=["a"],
        blocked=[],
        member_errors={
            "a": "member 'a' harness did not succeed: FAILED — "
            '{"error": "RegistryError", "detail": "the vendor said no"}'
        },
    )
    assert "the vendor said no" in text
    assert "RegistryError" not in text


def test_truncation_cuts_the_members_words_and_keeps_the_platforms_verdict() -> None:
    """A load-bearing safety property, pinned because it is easy to lose (security audit, round 2).

    A grounding error quotes the member, so the member's words trail the platform's verdict. The
    length limit cuts from the END — which is exactly the trailing half — so a long model tail is
    what gets removed and the verdict survives whole. Reverse the cut, or move the quote in front of
    the verdict, and the run page starts leading with text the member wrote.
    """
    verdict = "grounding: named a location it had no tool to reach"
    text = summarise_failed_run(
        failed=["liar"],
        blocked=[],
        member_errors={"liar": f"{verdict}; the member wrote " + "z" * 400},
    )
    reason = text.split("liar stopped because ", 1)[1]
    assert reason.startswith(verdict)
    assert reason.rstrip(".").endswith("…")
    assert "z" * 400 not in reason


# --- the runaway-pattern fix must not be revertible in silence (qa-engineer round 2, M5) ----------
#
# Reverting the bounded quantifier passed the entire suite — 2095 tests, zero failures. The pattern
# runs on the recorded member error, whose wrapper interpolates a MANIFEST-AUTHORED role name capped
# at 2000 characters, so any authenticated user could spend ~2.8 seconds of processor time per
# failed member, up to five per run, repeatably, on the run-completion path.
#
# Pinned two ways on purpose. The structural assertion is the reliable one — it cannot flake and it
# names the exact defect. The timing one is the honest one: it measures the property that actually
# matters, with enough headroom that only a genuine regression trips it.


def test_the_wrapper_pattern_carries_no_unbounded_lazy_quantifier() -> None:
    from oraclous_execution_engine_service.services.team_run_service import _ORCHESTRATOR_WRAPPER

    assert ".+?" not in _ORCHESTRATOR_WRAPPER.pattern
    assert ".*?" not in _ORCHESTRATOR_WRAPPER.pattern


def test_a_long_whitespace_run_does_not_stall_the_run_page() -> None:
    import time

    from oraclous_execution_engine_service.services.team_run_service import _ORCHESTRATOR_WRAPPER

    # the shape that made it quadratic: opens like the wrapper, never completes it
    hostile = "member " + " " * 2000 + "x"
    started = time.monotonic()
    _ORCHESTRATOR_WRAPPER.sub("", hostile, count=1)
    elapsed = time.monotonic() - started
    # measured at 0.00001s bounded against 2.8s lazy; 0.5s is a regression, not a slow machine
    assert elapsed < 0.5
