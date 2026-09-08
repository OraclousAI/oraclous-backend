"""#961 — the website-address cleaner moves into the shared kernel, because THREE services now
have to agree on what one address means.

Until now exactly one service cleaned an address: the capability registry, at the last hop before
the search vendor (#951). One caller, one pass, so "what counts as the same site" never had to be
agreed with anyone.

#961's ruling 2 ends that. A person's list of sites now BINDS the run, and the binding is checked
in the harness runtime immediately before each search — a different service, comparing the sites
the model asked for against the sites the person named. That comparison is only meaningful if both
halves were cleaned the same way. The person pastes ``https://www.theverge.com/`` into a form; the
model calls the tool with ``theverge.com``; those are the same site, and a check that says
otherwise refuses a search that was perfectly correct.

Two copies of the rule is the failure this file exists to prevent. #946 shipped exactly that shape
— "two cleaning passes that disagreed with each other" — and it cost a review round to find. So the
rule lives once, in ``oraclous_ohm``, the kernel all three services already depend on
(``pyproject.toml``: "ohm is the shared kernel"), and the registry imports it rather than keeping
its own.

The FIXED-POINT property below is what makes a shared cleaner safe to apply more than once. It is
already relied upon inside the registry (the connector cleans to report what it searched, the
provider cleans again at the vendor hop); with a third caller in a third service it stops being an
internal convenience and becomes the contract.

The two services' side of the move — that each one uses THIS function and not a copy of it — is
asserted where those services' own tests live, because the kernel must not import a service to
check on it.

RED until ``oraclous_ohm.sites`` lands; the seam is imported function-locally
(`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("theverge.com", ["theverge.com"]),
        ("https://www.theverge.com/tech", ["theverge.com"]),
        ("WWW.BBC.CO.UK", ["bbc.co.uk"]),
        ("theverge.com, bbc.co.uk", ["theverge.com", "bbc.co.uk"]),
        (["https://theverge.com/", "www.theverge.com"], ["theverge.com"]),
    ],
)
def test_the_three_ways_a_person_writes_one_address_collapse_to_one(
    supplied: object, expected: list[str]
) -> None:
    """The behaviour the cross-service comparison rests on.

    A person filling in a form copies their address bar; a model calls a tool with a bare hostname.
    #961's check has to see those as the same site or it refuses correct searches, so the shapes
    that must collapse are pinned here rather than left implicit in the registry's own tests.
    """
    from oraclous_ohm.sites import normalise_sites

    assert normalise_sites(supplied) == expected


def test_cleaning_twice_equals_cleaning_once() -> None:
    """The fixed-point property, now a cross-service contract rather than an internal convenience.

    Three callers clean now: the engine when it reads the person's answer, the harness when it
    checks the model's call against it, the registry at the vendor hop. If a second pass changed
    anything, those three would each hold a DIFFERENT list and the check would compare the wrong
    pair. ``www.www.theverge.com`` is the case that actually broke it once: stripping one label per
    call reported ``www.theverge.com`` while sending ``theverge.com``.
    """
    from oraclous_ohm.sites import normalise_sites

    once = normalise_sites(["www.www.theverge.com", "https://www.bbc.co.uk/news"])
    assert normalise_sites(once) == once


def test_nothing_named_is_not_an_error() -> None:
    """The scope guard, at the bottom layer: no list means no restriction, never a refusal.

    Most searches name no sites at all and must stay exactly as they are (#961's scope note). This
    is where that begins — a blank box, an absent argument, and an empty list all mean the same
    thing, and none of them is a fault.
    """
    from oraclous_ohm.sites import normalise_sites

    assert normalise_sites(None) == []
    assert normalise_sites([]) == []
    assert normalise_sites("   ") == []


def test_a_publication_name_is_still_refused() -> None:
    """#951's ruling survives the move: a name is not an address, and the refusal says so.

    ``BBC News`` has no mechanical route to ``bbc.co.uk``; #951 deliberately built no name table and
    #963 exists because a model inventing one is exactly the harm. The message names the offending
    value so the person fixes THAT one rather than guessing which of their entries was wrong.
    """
    from oraclous_ohm.sites import InvalidSiteError, normalise_sites

    with pytest.raises(InvalidSiteError) as exc:
        normalise_sites(["BBC News"])
    assert "BBC News" in str(exc.value)
