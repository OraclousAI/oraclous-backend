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

These are static assertions on the prompt, because a prompt is the whole deliverable here — no
code path branches on it. ``test_prompts.py`` (#750) is the precedent for testing a prompt at all,
but it matches plain substrings; this file goes further and asserts properties of the prompt's own
website rule, so the implementation is free to word that rule its own way and a later rewrite that
drops the substance still fails. That extra machinery is new here, so it is kept small and both of
its failure modes are guarded deliberately. Every guard below was raised at the Tests Review gate,
with a counterexample, and each is re-checked in the ``[impl]`` PR against the wording that shipped.

**A correct rule must not be rejected.** Three ways that nearly happened:

- The rule is extracted a BLOCK at a time, never a sentence at a time. A writer who puts one idea
  per sentence leaves sentences naming no website word at all ("Never ask for a publication's
  name."), and a sentence-level extractor drops exactly those — failing a prompt that satisfies
  every acceptance criterion, and quietly pressuring the implementer into one contorted run-on.
- A block absorbs the bulleted or indented lines under it. This prompt already writes its field
  contract as one bullet per line, so a rule in its own house style — a lead-in and a bullet per
  requirement — would otherwise be reduced to the lead-in alone.
- A refusal is any of the words a person actually reaches for, "avoid" and "don't" included. A test
  that recognises only "never" rejects a rule that plainly refuses.

**A wrong rule must not be accepted.** Where two words have to appear TOGETHER to mean anything
(a refusal, and the thing refused), they are required in the SAME sentence. Checked against the
joined rule instead, a prompt whose "name" is the field's own ``name`` key and whose "not" belongs
to an unrelated instruction passes while refusing nothing at all.
"""

from __future__ import annotations

import re

import pytest
from oraclous_ohm.compiler.prompts import APP_FORM_DRAFTER_PROMPT

pytestmark = pytest.mark.unit

#: A scheme-prefixed link. #951 proved the vendor silently ignores one, so the prompt must never
#: hold one out as the shape of an example value.
_SCHEME = re.compile(r"https?://", re.IGNORECASE)

#: A bare hostname: labels joined by dots, no scheme, no path, no spaces — the ONLY shape a search
#: can actually be restricted by. Every label needs at least two characters, so the sentence
#: abbreviations that share this shape ("e.g.", "i.e.", "U.S.") cannot satisfy a test that wants a
#: real address. Raised at the Tests Review gate.
_BARE_HOSTNAME = re.compile(r"\b[a-z0-9][a-z0-9-]+(?:\.[a-z0-9-]{2,})+\b", re.IGNORECASE)

#: The words that mark a passage as being about which websites to use. Matched on word boundaries:
#: a plain substring test for "site" also fires on "opposite" and "composite".
_WEBSITE_WORDS = ("website", "websites", "web site", "site", "sites", "hostname", "domain")
_WEBSITE_WORD = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _WEBSITE_WORDS) + r")\b", re.IGNORECASE
)

#: What it takes to REFUSE something rather than merely prefer it. Wide on purpose: "avoid" and
#: "don't" refuse a thing just as plainly as "never", and a test that accepts only some of the
#: words a person might reach for rejects a correct rule. Note "don't" is why a bare ``\bnot\b``
#: is not enough — the apostrophe means it never appears as a substring there.
_REFUSAL = re.compile(
    r"\b(?:never|not|n't|do not|don't|avoid|rather than|instead of|as opposed to|no longer)\b",
    re.IGNORECASE,
)

#: A line that continues the block above it rather than starting a new instruction: a bullet, or
#: an indented run-on. ``APP_FORM_DRAFTER_PROMPT`` already writes its field contract this way.
_CONTINUATION = re.compile(r"^\s*(?:[-*\u2022]|\s{2,}\S)")


def _blocks(prompt: str) -> list[str]:
    """The prompt's own instruction blocks. ``APP_FORM_DRAFTER_PROMPT`` is assembled as one string
    whose logical instructions each end in ``\\n``, so a line is the natural unit — except where an
    instruction runs on as a bulleted or indented list, which this prompt already does for the
    field contract. Such continuation lines are folded back into the line that introduced them.

    Without the fold, a rule written in this prompt's OWN house style — a lead-in line and a bullet
    per requirement — is reduced to its lead-in, and a substance-preserving reformat fails five of
    the six tests below. Raised at the Tests Review gate.
    """
    blocks: list[str] = []
    for raw in prompt.split("\n"):
        if not raw.strip():
            continue
        if blocks and _CONTINUATION.match(raw):
            blocks[-1] = f"{blocks[-1]} {raw.strip()}"
        else:
            blocks.append(raw.strip())
    return blocks


def _sentences(text: str) -> list[str]:
    """Split a block into sentences. Crude on purpose: it exists only to check that two words which
    must travel together actually do.

    Deliberately NOT split on a colon. A colon introduces rather than separates, so splitting there
    cuts a lead-in away from the clause that completes it — "Whether pasted or typed: a link to the
    address is accepted." would stop counting as one statement. Raised at the Tests Review gate.
    """
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", text) if s.strip()]


def _website_rule(prompt: str) -> str:
    """Every block of the prompt that talks about which websites to use, joined.

    Whole blocks, so a correctly worded rule survives however its author breaks it up — several
    sentences, or a lead-in and a list.
    """
    return " ".join(b for b in _blocks(prompt) if _WEBSITE_WORD.search(b))


def test_the_drafter_is_told_a_website_field_asks_for_an_address() -> None:
    """Acceptance criterion 1. Without this instruction the drafter invents "News Websites" and a
    person types "BBC News" — a value nothing downstream can honour without guessing."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT)
    assert rule, (
        "APP_FORM_DRAFTER_PROMPT says nothing about fields that name which websites to use, so "
        "the drafter is free to ask a person for a publication name"
    )
    assert re.search(r"\b(address|addresses|hostname|hostnames)\b", rule, re.IGNORECASE), (
        "the website rule never asks for an ADDRESS — a search can only be restricted by one, so "
        f"a field asking for anything else has to be guessed at. Rule was: {rule!r}"
    )


def test_the_drafter_is_told_a_publication_name_is_not_what_to_ask_for() -> None:
    """The rule has to REFUSE the name, not merely prefer the address. A drafter told only that
    addresses are good still writes "News Websites" and lets a person answer with a name.

    The refusal and the thing refused are required in the SAME sentence. Spread across a whole
    paragraph they prove nothing: "name" is also the field's own key, and "not" attaches to any
    instruction at all.
    """
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT)
    refusals = [
        s
        for s in _sentences(rule)
        if _REFUSAL.search(s) and re.search(r"\bnames?\b", s, re.IGNORECASE)
    ]
    assert refusals, (
        "no sentence of the website rule refuses a NAME — the drafter is told an address is "
        "wanted but never that a publication name is the wrong answer, so the guess this issue "
        f"exists to remove survives. Rule was: {rule!r}"
    )


def test_the_drafter_is_told_the_hint_accepts_a_pasted_full_link() -> None:
    """Acceptance criterion 2. A person's first instinct is to copy the address bar. #951 reduces
    a pasted link to its hostname in the connector, so accepting one costs nothing — but the
    person only knows that if the hint they are shown says so."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT)
    accepted = [
        s
        for s in _sentences(rule)
        if re.search(r"\b(link|links|url|urls|address bar)\b", s, re.IGNORECASE)
        and re.search(r"\b(paste|pasted|pasting|copy|copied|full|whole|entire)\b", s, re.IGNORECASE)
    ]
    assert accepted, (
        "no sentence of the website rule tells the drafter that a PASTED, whole link is accepted. "
        "A hint that only names an address reads as a refusal of what a person will actually do, "
        f"which is copy the address bar. Rule was: {rule!r}"
    )


def test_no_full_link_is_held_out_as_the_shape_of_an_example() -> None:
    """The #951 live finding, encoded. ``https://theverge.com/tech`` returns an ordinary 200 whose
    results are UNRESTRICTED — no error, no warning. An example in that shape teaches the drafter
    to write one into the field's ``example``, which is the value a person copies."""
    assert not _SCHEME.findall(APP_FORM_DRAFTER_PROMPT), (
        "APP_FORM_DRAFTER_PROMPT shows a scheme-prefixed link. The search vendor silently ignores "
        "one — a normal 200 with unrestricted results — so an example in that shape produces a "
        "run that looks fine and quietly dropped the restriction"
    )


def test_the_website_rule_carries_a_bare_address_example() -> None:
    """An example is what stops a model inventing a shape (the same reasoning as #951's connector
    description). A rule stated in the abstract leaves the drafter to decide what an address looks
    like, and "BBC News" is what it decides."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT)
    assert _BARE_HOSTNAME.findall(rule), (
        "the website rule states the requirement but shows no address, so the drafter has to "
        f"infer what one looks like. Rule was: {rule!r}"
    )


def test_the_rule_is_scoped_to_fields_about_which_websites_to_use() -> None:
    """Acceptance criterion 3. An unscoped rule bleeds: a drafter told "ask for addresses" starts
    proposing address fields for a brief about a company, which is a different defect in the same
    place.

    A hedge word is the most a static test can ask for here — whether the drafter actually leaves
    other fields alone is a live-run question, answered in the end-to-end proof. Named as a known
    gap at the Tests Review gate rather than papered over.
    """
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT)
    assert re.search(r"\b(only|other|others|unaffected|unchanged|else)\b", rule, re.IGNORECASE), (
        "the website rule never says which fields it does NOT cover, so it reads as advice about "
        f"every field. Rule was: {rule!r}"
    )


def test_the_rest_of_the_field_contract_is_untouched() -> None:
    """Acceptance criterion 3, from the other side: adding the website rule must not disturb what
    the drafter is asked for generally. Mechanical, so a prompt rewrite that quietly drops a key
    or a type fails here rather than at a person's screen. Green today — a guard, not scope."""
    for key in ("name", "hint", "type", "options", "example", "required"):
        assert f"'{key}'" in APP_FORM_DRAFTER_PROMPT, (
            f"APP_FORM_DRAFTER_PROMPT no longer names the {key!r} key of a field"
        )
    for field_type in ("short_text", "long_text", "choice"):
        assert field_type in APP_FORM_DRAFTER_PROMPT, (
            f"APP_FORM_DRAFTER_PROMPT no longer offers the {field_type!r} field type"
        )


# ── #961 ruling 4: the website box declares itself ──────────────────────────────────────────────
#
# Ruled 2026-09-08. A generated form's website box now carries a marker saying that is what it is,
# because the run has to FIND the restriction in order to be held to it, and guessing which box is
# about websites from its label was offered to the owner and refused.
#
# The marker is the model's own declaration. It is the only party that knows what it just wrote, so
# nothing downstream has to infer anything — which is the whole point of ruling 4, and also what
# finally makes #963 fixable: an example can be checked against the person's request only once
# something says which example to check.
#
# **The shape these tests guard against is a REPEAT of #953's own failure.** A third field was added
# to the output template at code review to demonstrate an empty example. It worked — and the model
# then copied that field, name and hint character for character, into a form for a request that
# named no websites at all. A demonstration of a website field teaches "always propose one", and the
# sentence forbidding that did not hold. So the marker must be taught as a RULE, not shown as a
# field the model can copy.


def test_the_drafter_is_told_to_mark_a_website_field_as_one() -> None:
    """Ruling 4. Without the marker the run cannot find the restriction, and #963 cannot be fixed
    mechanically at all — both halves of this work rest on this one instruction."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT)
    assert "binds" in rule, (
        "the website rule never asks the drafter to mark the field, so nothing downstream can tell "
        f"which box holds a site restriction. Rule was: {rule!r}"
    )
    assert "sites" in rule


def test_the_marker_is_scoped_to_the_website_field() -> None:
    """A marker on every field binds every field, which would make an app's time-frame box a site
    restriction. The instruction has to say it belongs to this one field and no other."""
    rule = _website_rule(APP_FORM_DRAFTER_PROMPT)
    marker_sentences = [s for s in _sentences(rule) if "binds" in s]
    assert marker_sentences, f"no sentence introduces the marker. Rule was: {rule!r}"
    assert any(
        re.search(r"\b(only|this field|that field|no other)\b", s, re.IGNORECASE)
        for s in marker_sentences
    ), (
        "the marker instruction never says it is confined to the website field, so it reads as "
        f"advice about every field. Sentences were: {marker_sentences!r}"
    )


def test_no_website_field_is_demonstrated_in_the_output_template() -> None:
    """The #953 code-review regression, pinned so nobody reintroduces it.

    The template is the last thing the model reads before it commits to an answer, so a field shown
    there is a field it emits — including for a request that named no websites, which breaks #953's
    third acceptance criterion and puts a canned box on every form the platform generates. The rule
    may be stated; the field may not be shown.
    """
    template = APP_FORM_DRAFTER_PROMPT[APP_FORM_DRAFTER_PROMPT.index('{"fields"') :]
    demonstrated = template[: template.index("]}") + 2]
    assert not _WEBSITE_WORD.search(demonstrated), (
        "the output template demonstrates a field about websites, which the drafter copies "
        f"wholesale into forms that asked for none. Template was: {demonstrated!r}"
    )


def test_the_marker_is_never_demonstrated_as_a_value_to_copy() -> None:
    """Same failure, one level down: a ``binds`` key sitting in the template is copied onto every
    field in the answer, which would bind an app to whatever a person typed in an unrelated box."""
    template = APP_FORM_DRAFTER_PROMPT[APP_FORM_DRAFTER_PROMPT.index('{"fields"') :]
    demonstrated = template[: template.index("]}") + 2]
    assert "binds" not in demonstrated
