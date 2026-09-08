"""#953 — a generated app form must ask for a website's ADDRESS, never its name.

An app converted from a team run derives its own form: a team declares exactly one input, so a
model invents the field names and their hints (#938). For a news app it invents a field like
"News Websites", which asks a person for a publication NAME.

A web search can only be restricted by hostname. So something has to turn ``BBC News`` into
``bbc.co.uk``, and that is a guess which cannot be checked afterwards — ``bbc.com`` and
``bbc.co.uk`` both resolve and both return real pages, so a wrong guess and a right one are
indistinguishable. The owner ruled on 2026-09-07: ask for the address and the guess disappears.
There is deliberately no name-to-address table anywhere, and #951 built none.

Why the WORDING carries this much weight: #951's live probe against the search vendor
(2026-09-07) found a full URL is **silently ignored** — ``https://theverge.com/tech`` returns an
ordinary 200 whose results are completely unrestricted, with no error and no warning. A field that
invites the wrong kind of value therefore does not fail loudly. It produces a run that looks fine
and quietly dropped the restriction.

These are static assertions on the prompt, the same shape as ``test_prompts.py`` (#750), because a
prompt is the whole deliverable here — no code path branches on it. They are written against the
prompt's MEANING rather than its exact sentences: each one pulls out the prompt's own
website-related sentences and asserts a property of them, so the implementation is free to word
the rule its own way and a later rewrite that drops the substance still fails.
"""

from __future__ import annotations

import re

import pytest
from oraclous_ohm.compiler.prompts import APP_FORM_DRAFTER_PROMPT

pytestmark = pytest.mark.unit

#: A scheme-prefixed link. #951 proved the vendor silently ignores one, so the prompt must never
#: hold one out as the shape of an example value.
_SCHEME = re.compile(r"https?://", re.IGNORECASE)

#: A bare hostname: labels joined by dots, no scheme, no path, no spaces. This is the ONLY shape a
#: search can actually be restricted by.
_BARE_HOSTNAME = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+\b", re.IGNORECASE)

#: The words that mark a sentence as being about which websites to use.
_WEBSITE_WORDS = ("website", "web site", "site", "hostname", "domain")


def _sentences(prompt: str) -> list[str]:
    """Split the prompt into sentence-ish chunks. Deliberately crude: it only has to be good
    enough to isolate the website rule from the rest of the instructions."""
    return [s.strip() for s in re.split(r"(?<=[.:])\s+|\n", prompt) if s.strip()]


def _website_rule(prompt: str) -> str:
    """The prompt's own sentences about which websites to use, joined back together.

    Every assertion below reads THIS rather than the whole prompt, so a stray "site" elsewhere in
    the instructions cannot accidentally satisfy a test.
    """
    lowered_words = _WEBSITE_WORDS
    hits = [s for s in _sentences(prompt) if any(w in s.lower() for w in lowered_words)]
    return " ".join(hits)


def test_the_drafter_is_told_a_website_field_asks_for_an_address() -> None:
    """Acceptance criterion 1. Without this instruction the drafter invents "News Websites" and a
    person types "BBC News" — a value nothing downstream can honour without guessing."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT)
    assert rule, (
        "APP_FORM_DRAFTER_PROMPT says nothing about fields that name which websites to use, so "
        "the drafter is free to ask a person for a publication name"
    )
    assert "address" in rule.lower() or "hostname" in rule.lower(), (
        "the website rule never asks for an ADDRESS — a search can only be restricted by one, so "
        f"a field asking for anything else has to be guessed at. Rule was: {rule!r}"
    )


def test_the_drafter_is_told_a_publication_name_is_not_what_to_ask_for() -> None:
    """The rule has to REFUSE the name, not merely prefer the address. A drafter told only that
    addresses are good still writes "News Websites" and lets a person answer with a name."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT).lower()
    assert "name" in rule, (
        "the website rule never mentions a publication NAME, so nothing in it rules the name out "
        "— the guess this issue exists to remove survives"
    )
    assert re.search(r"\b(never|not|rather than|instead of|no[t]? its)\b", rule), (
        "the website rule mentions a name but never rules it out; it reads as a preference, and a "
        f"preference leaves the guess in place. Rule was: {rule!r}"
    )


def test_the_drafter_is_told_the_hint_accepts_a_pasted_full_link() -> None:
    """Acceptance criterion 2. A person's first instinct is to copy the address bar. #951 reduces
    a pasted link to its hostname in the connector, so accepting one costs nothing — but the
    person only knows that if the hint they are shown says so."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT).lower()
    assert re.search(r"\b(link|url|address bar)\b", rule), (
        "the website rule never tells the drafter that a pasted full link is accepted, so the "
        f"hint a person reads will not say it either. Rule was: {rule!r}"
    )
    assert re.search(r"\b(paste[d]?|copy|copied|full)\b", rule), (
        "the website rule mentions a link but never that a PASTED, whole one is fine — a hint "
        f"that only names an address reads as a refusal of what a person will actually do: {rule!r}"
    )


def test_no_full_link_is_held_out_as_the_shape_of_an_example() -> None:
    """The #951 live finding, encoded. ``https://theverge.com/tech`` returns an ordinary 200 whose
    results are UNRESTRICTED — no error, no warning. An example in that shape teaches the drafter
    to write one into the field's ``example``, which is the value a person copies."""
    leaks = _SCHEME.findall(APP_FORM_DRAFTER_PROMPT)
    assert not leaks, (
        "APP_FORM_DRAFTER_PROMPT shows a scheme-prefixed link. The search vendor silently ignores "
        "one — a normal 200 with unrestricted results — so an example in that shape produces a "
        "run that looks fine and quietly dropped the restriction"
    )


def test_the_website_rule_carries_a_bare_address_example() -> None:
    """An example is what stops a model inventing a shape (the same reasoning as #951's connector
    description). A rule stated in the abstract leaves the drafter to decide what an address looks
    like, and "BBC News" is what it decides."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT)
    examples = _BARE_HOSTNAME.findall(rule)
    assert examples, (
        "the website rule states the requirement but shows no address, so the drafter has to "
        f"infer what one looks like. Rule was: {rule!r}"
    )


def test_the_rule_is_scoped_to_fields_about_which_websites_to_use() -> None:
    """Acceptance criterion 3. An unscoped rule bleeds: a drafter told "ask for addresses" starts
    proposing address fields for a brief about a company, which is a different defect in the same
    place."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT).lower()
    assert re.search(r"\b(only|other|unaffected|unchanged|else)\b", rule), (
        "the website rule never says which fields it does NOT cover, so it reads as advice about "
        f"every field. Rule was: {rule!r}"
    )


def test_the_rest_of_the_field_contract_is_untouched() -> None:
    """Acceptance criterion 3, from the other side: adding the website rule must not disturb what
    the drafter is asked for generally. Mechanical, so a prompt rewrite that quietly drops a key
    or a type fails here rather than at a person's screen."""
    for key in ("name", "hint", "type", "options", "example", "required"):
        assert f"'{key}'" in APP_FORM_DRAFTER_PROMPT, (
            f"APP_FORM_DRAFTER_PROMPT no longer names the {key!r} key of a field"
        )
    for field_type in ("short_text", "long_text", "choice"):
        assert field_type in APP_FORM_DRAFTER_PROMPT, (
            f"APP_FORM_DRAFTER_PROMPT no longer offers the {field_type!r} field type"
        )
