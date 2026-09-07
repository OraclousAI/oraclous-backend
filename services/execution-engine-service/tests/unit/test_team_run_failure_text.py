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
    assert "Error" not in text.replace("error", "")


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


# --- everything it carried before, it still carries ----------------------------------------------


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
    assert "2" in text and "1" in text


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


# --- leak-safety is unchanged ---------------------------------------------------------------------


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
