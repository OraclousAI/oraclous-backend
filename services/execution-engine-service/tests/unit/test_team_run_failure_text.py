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
        failed=[f"member-{i}" for i in range(200)],
        blocked=[f"blocked-{i}" for i in range(200)],
        member_errors={f"member-{i}": _loop_error("RegistryError", "x" * 400) for i in range(200)},
    )
    assert len(text) <= 2000


def test_the_cap_never_cuts_mid_word_leaving_a_dangling_fragment() -> None:
    text = summarise_failed_run(
        failed=[f"member-{i}" for i in range(200)],
        blocked=[],
        member_errors={f"member-{i}": _loop_error("RegistryError", "x" * 400) for i in range(200)},
    )
    assert text.endswith((".", "…"))


def test_a_failure_with_no_recorded_detail_still_names_the_member() -> None:
    text = summarise_failed_run(failed=["ghost"], blocked=[], member_errors={})
    assert "ghost" in text


def test_truncation_keeps_the_counts_and_the_rerunnable_statement() -> None:
    # T3 says the counts and the re-runnable statement STAY. A cap that cut the front of the text
    # to fit the reasons would satisfy the length assertion and drop both.
    text = summarise_failed_run(
        failed=[f"member-{i}" for i in range(200)],
        blocked=[f"blocked-{i}" for i in range(200)],
        member_errors={f"member-{i}": _loop_error("RegistryError", "x" * 400) for i in range(200)},
    )
    assert "200 of its members failed" in text
    assert "re-run" in text.lower()
    assert "member-0" in text


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


def test_the_unrecognised_json_fragment_itself_is_still_dropped() -> None:
    text = summarise_failed_run(
        failed=["analyst"],
        blocked=[],
        member_errors={"analyst": 'grounding: claim not supported: {"n": 1}'},
    )
    assert '{"n": 1}' not in text


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
