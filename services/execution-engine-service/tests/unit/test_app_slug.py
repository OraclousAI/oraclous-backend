"""An app's deep-link handle (#938).

A converted app needs a stable handle for the same reason the Validation Desk needed one: the
console should link to ``/app/apps/competitor-brief`` rather than carry a uuid nobody can read.
Nobody types it — it is generated from the name the person gave the app.

``uq_engine_apps_org_slug`` is unique per organisation WHERE the slug is not null
(``models/app.py:93-100``), which decides both ends of this module's behaviour: two apps in one
organisation can never share a handle, and an app whose name yields nothing usable is stored with a
null slug rather than failing the save.

The slug itself is not spelled here. ``basic_slug`` (``packages/ohm/src/oraclous_ohm/_slug.py:59``)
is the one plain-text primitive in the repo, and the ``check_slug_duplication`` guardrail (SLUG001)
fails CI on any function that lowercases and substitutes non-alphanumerics itself. What IS spelled
here is the cap, the null fallback, and the uniqueness ladder — the same shape auth-service already
uses for organisation handles.

RED until the helpers land in ``domain/apps.py``; the seam is imported function-locally
(`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]


def test_a_name_becomes_a_lowercase_hyphenated_handle() -> None:
    from oraclous_execution_engine_service.domain.apps import app_slug

    assert app_slug("Competitor Brief") == "competitor-brief"


def test_punctuation_and_repeated_spaces_collapse_to_single_hyphens() -> None:
    from oraclous_execution_engine_service.domain.apps import app_slug

    assert app_slug("Q3  Pricing — Deep Dive!") == "q3-pricing-deep-dive"


def test_a_very_long_name_is_capped_to_the_column_and_never_ends_in_a_hyphen() -> None:
    """The column is ``String(128)``. A naive truncation can land mid-hyphen and leave a trailing
    separator, which reads as a broken handle in a URL."""
    from oraclous_execution_engine_service.domain.apps import APP_SLUG_MAX, app_slug

    slug = app_slug("Competitor " * 40)

    assert slug is not None
    assert len(slug) <= APP_SLUG_MAX
    assert not slug.endswith("-")


def test_a_name_with_nothing_usable_yields_no_handle_at_all() -> None:
    """Null, not a made-up fallback. The unique index only covers non-null slugs, so an app with no
    handle is safe to store — and inventing one would mean the first such app claims a name the
    next one cannot have."""
    from oraclous_execution_engine_service.domain.apps import app_slug

    assert app_slug("!!! ???") is None
    assert app_slug("   ") is None


def test_the_first_candidate_is_the_plain_handle() -> None:
    """Everyday case: nothing else has this name, so the app gets the handle its name implies."""
    from oraclous_execution_engine_service.domain.apps import app_slug_candidates

    assert app_slug_candidates("competitor-brief")[0] == "competitor-brief"


def test_a_taken_handle_falls_to_a_numbered_one_before_anything_stranger() -> None:
    """The second Competitor Brief in an organisation becomes ``competitor-brief-2``. Keeping the
    ladder numeric keeps everyday handles short and guessable."""
    from oraclous_execution_engine_service.domain.apps import app_slug_candidates

    candidates = app_slug_candidates("competitor-brief")

    assert candidates[1] == "competitor-brief-2"
    assert candidates[2] == "competitor-brief-3"


def test_the_ladder_ends_in_random_handles_so_it_can_always_find_a_free_one() -> None:
    """A numeric ladder alone runs out: once every rung is taken the last candidate is one that was
    already refused. Auth-service hit exactly that with organisation handles (#676), so this ladder
    ends somewhere a collision is not the same value every time."""
    from oraclous_execution_engine_service.domain.apps import app_slug_candidates

    first = app_slug_candidates("competitor-brief")
    second = app_slug_candidates("competitor-brief")

    assert first[-1] != second[-1]


def test_every_candidate_fits_the_column_even_from_a_maximal_name() -> None:
    """The suffix is added to an already-capped base, so a long name plus a rung must not push the
    handle past what the column accepts — that would be a 500 on the save rather than a retry."""
    from oraclous_execution_engine_service.domain.apps import (
        APP_SLUG_MAX,
        app_slug,
        app_slug_candidates,
    )

    base = app_slug("Competitor " * 40)
    assert base is not None

    assert all(len(c) <= APP_SLUG_MAX for c in app_slug_candidates(base))


def test_the_candidates_are_all_distinct() -> None:
    """Each rung is one database round trip. A repeated candidate spends a query to be told the
    same thing twice."""
    from oraclous_execution_engine_service.domain.apps import app_slug_candidates

    candidates = app_slug_candidates("competitor-brief")

    assert len(set(candidates)) == len(candidates)
