"""#961 ruling 4 + #963 — the form's website box declares itself, and its example stops being a
guess.

Two issues, one mechanism, which is why they are built together.

**#961 ruling 4.** A generated app form can carry a box asking which websites a run may use. Until
now nothing said which box that was. The value reached the team as one labelled line of prose
inside a larger request (``fold`` → ``to_run_inputs``), so a member could read it, act on it, or
drop it — and #951's original report is a run that dropped it and still passed every check the run
has. Making the run OBEY the list means the run has to be able to FIND it, and guessing from the
box's label was offered to the owner and refused: a wrong guess either misses a restriction the
person meant or invents one they never wrote. So the box declares itself, and the declaration comes
from the model that invented the box — the only party that knows what it just wrote.

**#963.** The box shows a greyed-out example beside it. Eight live runs through the gateway with a
real model produced eight INVENTED addresses from a request that named publications rather than
addresses — including both ``bbc.com`` and ``bbc.co.uk`` from the same input, the two addresses
#951 established are mechanically indistinguishable. Two rounds of prompt wording did not stop it,
and a third was tried at code review and made things worse (the model copied the demonstration
field wholesale into forms that named no websites at all). The fix has to be mechanical, and it
could not be until ruling 4 said which box to check.

The check is deliberately narrow and is the only thing it can be: an address in the example must
appear in the person's OWN request text. It cannot tell a right address from a wrong one — nothing
can, which is #951's standing limit — so it only removes what nobody wrote down.

Ruled 2026-09-08: an example that MIXES a copied address with an invented one keeps the copied half
and loses the invented one. That mixed case is the worst form of the defect (a real address lends
its credibility to the guess beside it), and it is also the case where clearing everything would
throw away a value the person actually typed.

RED until the ``binds`` marker, the request-text check and ``run_site_restriction`` land; every
seam is imported function-locally (`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = [pytest.mark.unit]

ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

#: The request text behind every case below. It names one source by ADDRESS and one by NAME, which
#: is #963's worst case: the drafter copies what it can and invents the rest, into one value, with
#: nothing marking which half is which.
REQUEST = (
    "Write a short weekly roundup of consumer tech news. Only use BBC News and theverge.com as "
    "sources, cover the last seven days, and keep it to three bullet points."
)


def _team() -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "news-roundup",
            "owner_organization_id": str(ORG),
            "kind": "team",
        },
        "task_input": {"required": True, "key": "task", "description": "What to round up."},
        "members": [
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:news/writer@1",
                "subgoal": "write the roundup",
                "depends_on": [],
                "tools": [],
            }
        ],
        "runtime": {"entrypoint": "writer"},
    }


def _field(**overrides: Any) -> dict[str, Any]:
    field: dict[str, Any] = {
        "name": "Source addresses",
        "hint": "Which sites to draw from. Pasting a full link is fine.",
        "type": "short_text",
        "options": [],
        "example": "",
        "required": False,
        "binds": "sites",
    }
    field.update(overrides)
    return field


def _draft(*fields: dict[str, Any]) -> dict[str, Any]:
    return {"fields": list(fields)}


# ── ruling 4: the box says what it is ────────────────────────────────────────────────────────────


def test_a_website_box_declares_itself_and_the_declaration_survives_parsing() -> None:
    """The marker the model wrote is carried onto the parsed field, not dropped.

    Everything downstream — the example check below, and the run binding in the harness — keys on
    this one value. A parser that quietly ignored an unknown key would leave both halves of #961
    with nothing to act on and no error to say why.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft

    fields = parse_form_draft(_draft(_field()), request_text=REQUEST)

    assert [f.binds for f in fields] == ["sites"]


def test_an_ordinary_box_declares_nothing() -> None:
    """The scope guard, at the form layer.

    Most forms have no website box at all and must be byte-for-byte what they are today. A field
    that says nothing binds nothing — never a default, never an inference from its wording.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft

    fields = parse_form_draft(
        _draft(
            {
                "name": "Time frame",
                "hint": "How far back to look.",
                "type": "short_text",
                "options": [],
                "example": "the last seven days",
                "required": False,
            }
        ),
        request_text=REQUEST,
    )

    assert fields[0].binds == ""


def test_a_box_whose_wording_is_about_websites_but_declares_nothing_binds_nothing() -> None:
    """The refused alternative, pinned so nobody rebuilds it.

    Guessing from the label was offered to the owner and refused. This field is named and hinted
    exactly like a site restriction and still binds nothing, because it did not say so. A future
    change that "helpfully" recognises the wording fails here.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft

    fields = parse_form_draft(
        _draft(
            {
                "name": "News websites",
                "hint": "Website addresses to use for the roundup.",
                "type": "short_text",
                "options": [],
                "example": "",
                "required": False,
            }
        ),
        request_text=REQUEST,
    )

    assert fields[0].binds == ""


def test_a_marker_nobody_defined_is_refused_rather_than_ignored() -> None:
    """Fail-closed on a value the form contract does not know (CLAUDE.md §3.5).

    Ignoring it would be the worse half of both options: the person sees a box that looks like a
    restriction, and the run is bound by nothing. A curated refusal is what the endpoint already
    does with every other shape it cannot render.
    """
    from oraclous_execution_engine_service.domain.app_form import FormShapeError, parse_form_draft

    with pytest.raises(FormShapeError):
        parse_form_draft(_draft(_field(binds="whatever")), request_text=REQUEST)


def test_two_website_boxes_are_refused() -> None:
    """One restriction per form, or the run cannot say which list binds it.

    Two boxes both claiming the site restriction is not a form a person can fill in honestly — one
    of the two answers would silently lose. Refused where it is drafted, before anyone sees it.
    """
    from oraclous_execution_engine_service.domain.app_form import FormShapeError, parse_form_draft

    with pytest.raises(FormShapeError):
        parse_form_draft(
            _draft(_field(name="Sources"), _field(name="More sources")), request_text=REQUEST
        )


# ── #963: the example stops being a guess ────────────────────────────────────────────────────────


def test_an_invented_address_is_dropped_from_the_example() -> None:
    """The defect, in its own words: eight live runs, eight invented addresses.

    ``bbc.co.uk`` appears nowhere in the request — the request said "BBC News" — so it is a guess,
    and a guess shown as a placeholder is a value a person can accept without ever choosing it.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft

    fields = parse_form_draft(
        _draft(_field(example="bbc.co.uk, theverge.com")), request_text=REQUEST
    )

    assert "bbc.co.uk" not in fields[0].example


def test_the_half_the_person_actually_wrote_survives() -> None:
    """Ruled 2026-09-08: keep the copied half, drop the invented one.

    ``theverge.com`` is in the request character for character, so it is not a guess and there is no
    reason to take it away. Clearing the whole example was the alternative and it discards a value
    the person typed themselves.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft

    fields = parse_form_draft(
        _draft(_field(example="bbc.co.uk, theverge.com")), request_text=REQUEST
    )

    assert fields[0].example == "theverge.com"


def test_an_example_that_was_entirely_invented_becomes_empty() -> None:
    """An empty example is the RIGHT answer, not a missing one.

    This is the case the prompt has failed to produce in every live run since #953: a request that
    names publications only. Nothing in it was written down, so nothing survives, and the person
    types their own addresses into an empty box instead of accepting ours.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft

    fields = parse_form_draft(
        _draft(_field(example="theverge.com, bbc.com")),
        request_text="Round up consumer tech news from The Verge and BBC News.",
    )

    assert fields[0].example == ""


def test_a_pasted_link_in_the_request_still_counts_as_written_down() -> None:
    """A person who pasted a full link WROTE that address, whatever shape they wrote it in.

    #953's hint tells them pasting is fine, so a check that only recognised bare hostnames would
    delete the example of the person who took that advice — punishing the exact behaviour the form
    invites.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft

    fields = parse_form_draft(
        _draft(_field(example="theverge.com")),
        request_text="Round up tech news, only from https://www.theverge.com/tech please.",
    )

    assert fields[0].example == "theverge.com"


def test_only_the_website_box_has_its_example_checked() -> None:
    """The narrow scope ruling 4 bought.

    An ordinary field's example is prose lifted from the request and is none of this issue's
    business. Checking every example against the request text would delete honest examples that
    were summarised rather than copied, on every form the platform generates.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft

    fields = parse_form_draft(
        _draft(
            {
                "name": "Tone",
                "hint": "How it should read.",
                "type": "short_text",
                "options": [],
                "example": "brisk and factual",
                "required": False,
            }
        ),
        request_text=REQUEST,
    )

    assert fields[0].example == "brisk and factual"


def test_a_draft_with_no_request_text_keeps_the_example_it_was_given() -> None:
    """No request to check against is not evidence the example was invented.

    ``parse_form_draft`` is called from one place today, but it is a domain function with a public
    contract, and a caller that has no request text must not silently strip a field's example — an
    absent comparison proves nothing either way, and deleting on no evidence is its own defect.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft

    fields = parse_form_draft(_draft(_field(example="theverge.com")), request_text="")

    assert fields[0].example == "theverge.com"


# ── the person's answer becomes the run's binding ────────────────────────────────────────────────


def test_the_declared_box_gives_the_run_its_list_of_sites() -> None:
    """What the person typed into the declared box, cleaned into addresses the run is held to.

    This is the join between the two issues: ruling 4's marker is what lets the run path find the
    answer at all, and rulings 1 and 2 are what it then does with it.
    """
    from oraclous_execution_engine_service.domain.app_form import (
        parse_form_draft,
        run_site_restriction,
    )

    fields = parse_form_draft(_draft(_field()), request_text=REQUEST)
    sites = run_site_restriction(fields, {fields[0].id: "https://www.theverge.com/, bbc.co.uk"})

    assert sites == ["theverge.com", "bbc.co.uk"]


def test_a_form_with_no_website_box_binds_nothing() -> None:
    """The scope guard again, at the run boundary.

    An app whose form never asked about websites must produce no restriction at all — not an empty
    one that later reads as "restrict to nothing".
    """
    from oraclous_execution_engine_service.domain.app_form import (
        parse_form_draft,
        run_site_restriction,
    )

    fields = parse_form_draft(
        _draft(
            {
                "name": "Time frame",
                "hint": "How far back.",
                "type": "short_text",
                "options": [],
                "example": "",
                "required": False,
            }
        ),
        request_text=REQUEST,
    )

    assert run_site_restriction(fields, {fields[0].id: "the last seven days"}) == []


def test_a_website_box_left_blank_binds_nothing() -> None:
    """An optional box nobody filled in is not a restriction to nowhere.

    The field can exist and go unanswered — ``fold`` already drops a blank value from the request
    text, and the binding has to agree with it or the run is held to a list the person never gave.
    """
    from oraclous_execution_engine_service.domain.app_form import (
        parse_form_draft,
        run_site_restriction,
    )

    fields = parse_form_draft(_draft(_field()), request_text=REQUEST)

    assert run_site_restriction(fields, {fields[0].id: "   "}) == []
    assert run_site_restriction(fields, {}) == []


def test_the_answer_still_reaches_the_team_as_words_as_well() -> None:
    """The binding ADDS to the prose line; it does not replace it.

    The member still has to know which sites it may use in order to ask for them — a restriction
    enforced against a member that was never told is #697's mistake, where a contract was checked
    against a member nobody had asked. The fold is unchanged; the binding is the second half.
    """
    from oraclous_execution_engine_service.domain.app_form import parse_form_draft, to_run_inputs

    fields = parse_form_draft(_draft(_field()), request_text=REQUEST)
    inputs = to_run_inputs(_team(), fields, {fields[0].id: "theverge.com, bbc.co.uk"})

    assert "theverge.com" in inputs["task"]
    assert "Source addresses" in inputs["task"]


def test_a_bad_address_in_the_box_is_refused_where_the_person_can_still_fix_it() -> None:
    """A publication name typed into an address box is refused at the form, not at the vendor.

    The person is sitting in front of the form; refusing here costs one message and no tokens. The
    same value accepted here would either fail deep in a run they had already waited for, or — the
    #951 finding — be silently ignored by the search vendor and produce an unrestricted run that
    looks completely normal.
    """
    from oraclous_execution_engine_service.domain.app_form import (
        parse_form_draft,
        run_site_restriction,
    )
    from oraclous_ohm.sites import InvalidSiteError

    fields = parse_form_draft(_draft(_field()), request_text=REQUEST)

    with pytest.raises(InvalidSiteError):
        run_site_restriction(fields, {fields[0].id: "BBC News"})
