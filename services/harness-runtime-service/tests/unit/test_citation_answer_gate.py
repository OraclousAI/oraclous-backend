"""#782 — the answer-time citation gate's RULE VERDICTS at §CITE **rev4** (Contract #735).

Two rules, both blocking, both evaluated in platform code. Neither depends on anything a third-party
tool chooses to send, because the platform mints the id itself:

* **Rule 1 — the answer NAMES A SOURCE IN PROSE while carrying no ``citation_id`` for it.** Kills
  ``(source: partner-agreement.md)``.
* **Rule 2 — a cited ``citation_id`` is not in the set the platform served to that run.** Kills
  every hallucinated id.

**An answer that cites nothing at all is not a violation.** rev4 decision 8 deleted rev2's rule 1
("an asserted fact carries no ``citation_id``") outright: separating an asserted fact from reasoning
is a judgment call, and this Contract forbids a model-implemented gate. The model is not a source of
truth, so anything it says without a citation is its own reasoning, and reasoning needs no source.
The shipped approximation — *"the run served sources and the answer cited none"* — fails an honest
decline, which is the product working. That is the single most important property in this file.

**The checker is platform code at the run boundary, not an OHM member** (§CITE rev2 decision 2). A
gate implemented as a model instruction is a gate that can be talked out of, which is precisely the
failure this Contract closes. So the verdicts are a pure function over (the draft, the run's served
set), unit-testable with no loop and no model. **What a violation DOES is loop behaviour and is not
tested here** — see ``test_citation_gate_loop.py``.

**rev1 rules 3 and 4 are NOT tested here.** rev3 moved precision to connect time (#744): a tool that
cannot name its documents is refused before it is ever used, so by answer time the only thing that
can still be missing is a version, and that degrades a citation rather than failing an answer.

Rule 1's detection is pinned to the marker list as RULED on #788 (option B, 2026-08-12), and this
file does not widen it: ``source:`` and ``sources:`` only, firing when no ``cit_`` id sits
alongside the marker. ``source_tool_call_id`` — the brief's third marker — is REMOVED: #642's
``validate_grounding`` already verifies that token against an ``ok`` tool step in the member's own
trace, fail-closed, and a prose regex watching the same token put a guess on top of a proof (a live
member was killed at ``citation_unresolved`` for obeying ``GROUNDING_DIRECTIVE``). Extending or
re-widening the list is the Contract's business, not the implementer's — and not the test author's.

**What "alongside" means — RULED at the Tests Review gate: the window is THE LINE.** A marker fires
unless a ``cit_`` id appears on the same line. The brief did not name the window, but rev4's own
wording does the work: an answer "names a source in prose while carrying no ``citation_id`` **for
it**" is a per-marker check, and a whole-answer window cannot express "for it" — it lets a member
cite one real source and prose-source a second one in the same draft, which is half of the #734
failure surviving inside a partly-honest answer. The line is also the smallest window the Contract's
own passing example (``Source: [cit_9f2a…]``) sits inside.
``test_a_marker_on_its_own_line_fires_even_when_another_line_cites`` is where that ruling lives.

**The misfire this window accepts, put on the record at that gate.** A bulleted source list fires
rule 1 falsely, because the ``Sources:`` line carries no id of its own::

    Sources:
    - [cit_9f2a…]
    - [cit_0b1d…]

That is a plausible model output shape, not an exotic one, and it is ACCEPTABLE under rev4 Limit 2:
the cost is one iteration and a correction message naming the remedy, never a refused answer, which
is the deal Limit 2 explicitly strikes. **It is not licence to widen the window at implementation
time.** If the deployed-stack e2e shows this misfiring often enough to matter, that is a
``Contract`` issue for ``solution-architect``.

``check_answer_citations`` is imported function-locally, never at module level
(``.claude/rules/tests-seam-imports.md``).
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

_SERVED_A = "cit_9f2a4c81b7d3e50a1c6f28934bd5e7a0"
_SERVED_B = "cit_0b1d3f57a9c2e4680d8f1a3b5c7e9021"
# Well-formed and never served — the shape of a real id is not evidence of one.
_FORGED = "cit_deadbeefdeadbeefdeadbeefdeadbeef"
# #780 item 2: the same forgery in uppercase hex. The shipped `_CITATION_ID` is lowercase-only, so
# this token is not extracted at all and slips past BOTH rules.
_FORGED_UPPER = "cit_DEADBEEFDEADBEEFDEADBEEFDEADBEEF"
_SERVED_A_UPPER = "cit_" + _SERVED_A[4:].upper()


def _check(answer: str, served: set[str]) -> Any:
    from oraclous_harness_runtime_service.domain.citation_gate import check_answer_citations

    return check_answer_citations(answer, served)


def _rules(result: Any) -> list[int]:
    return sorted(v.rule for v in result.violations)


# --- the property rev4 exists to protect: silence is not a violation -------------------------


async def test_an_answer_citing_nothing_passes_even_when_the_run_served_sources() -> None:
    # THE regression test for the bug #743 shipped, and acceptance criterion 2. The run served two
    # sources; the member cited neither and named neither. Under rev2 that was rule 1; under rev4 it
    # is a member reasoning on its own account, which needs no source. RED against the shipped gate.
    result = _check("The two clauses do not conflict.", {_SERVED_A, _SERVED_B})
    assert result.passed is True
    assert list(result.violations) == []


async def test_an_honest_decline_after_a_retrieval_that_returned_hits_passes() -> None:
    # Criterion 3. A member that searches, reads what came back, and reports it has no answer is
    # behaving correctly — refusing to hallucinate is the product working, and a gate that punishes
    # it is worse than no gate (rev4 decision 8). WHY it found nothing is a retrieval problem with a
    # hundred causes; this gate does not pretend to diagnose it.
    result = _check("I don't have that information in the sources I was given.", {_SERVED_A})
    assert result.passed is True
    assert list(result.violations) == []


async def test_naming_a_file_without_an_attribution_marker_is_not_a_violation() -> None:
    # rev4 Limit 2: "I edited partner-agreement.md for you" names a file and asserts no fact. A
    # filename-shaped pattern would block it, and would block every member that mentions a file it
    # touched. The detection is attribution MARKERS, never filenames.
    result = _check("I edited partner-agreement.md for you and saved it.", {_SERVED_A})
    assert result.passed is True
    assert list(result.violations) == []


# --- rule 1: an attribution marker with no citation alongside it ----------------------------


async def test_prose_sourcing_is_not_a_citation() -> None:
    # #734, verbatim: `(source: partner-agreement.md)` is a bare filename in model prose. It
    # resolves to nothing and no gate can check it. Under rev4 the verdict is unchanged but the
    # REASON is new — the member pointed at a source and wrote prose instead of the id it was
    # handed, which is mechanical and diagnostic where "is this an asserted fact?" was neither.
    result = _check("The notice period is 30 days (source: partner-agreement.md).", {_SERVED_A})
    assert result.passed is False
    assert _rules(result) == [1]


async def test_prose_sourcing_fires_even_when_the_run_served_nothing() -> None:
    # rev4 detaches rule 1 from the served set entirely. The shipped gate PASSES this draft, because
    # it only fired rule 1 when the served set was non-empty — so a member that never retrieved
    # could point at a source freely. Pointing at a source the platform never issued is the defect,
    # whatever the run did or did not serve. RED against the shipped gate.
    result = _check("The notice period is 30 days (source: partner-agreement.md).", set())
    assert result.passed is False
    assert _rules(result) == [1]


async def test_a_receipt_id_in_prose_is_not_a_cite_layer_defect() -> None:
    # #734's other half, re-homed by the #788 ruling (option B). The invented receipt id is caught
    # at the #642 layer (`validate_grounding`, pinned in `packages/ohm/tests/test_grounding.py`),
    # which resolves it against the member's OWN trace and can tell whether the call actually ran —
    # something a prose regex never could. §CITE watching the same token was a guess overruling
    # that proof, and the guess killed a compliant live member. So this draft PASSES here.
    result = _check(
        "The partner agreement sets a 30-day notice. source_tool_call_id=call_8f3a2b",
        {_SERVED_A},
    )
    assert result.passed is True
    assert list(result.violations) == []


async def test_a_receipt_id_in_prose_passes_with_an_empty_served_set_too() -> None:
    # The served set changes nothing: the token is not a rule 1 marker at all under the #788
    # ruling, whatever the run served. (Green against the pre-rev4 gate as well — this pins that
    # rev4 does not regress it, not a RED-by-design behaviour change.)
    result = _check(
        "The partner agreement sets a 30-day notice. source_tool_call_id=call_8f3a2b", set()
    )
    assert result.passed is True
    assert list(result.violations) == []


async def test_the_plural_marker_fires_too() -> None:
    # The marker list is `source:` / `sources:` (#788 removed the brief's third marker).
    result = _check("Notice is 30 days (sources: partner-agreement.md, addendum.md).", {_SERVED_A})
    assert result.passed is False
    assert _rules(result) == [1]


async def test_the_marker_is_matched_case_insensitively() -> None:
    # A model capitalises the start of a line. Case is not a defence.
    result = _check("Notice is 30 days.\nSource: partner-agreement.md", {_SERVED_A})
    assert result.passed is False
    assert _rules(result) == [1]


async def test_a_marker_carrying_a_citation_passes() -> None:
    # Criterion 5. `Source: [cit_9f2a…]` is a CORRECT citation wearing a marker. §CITE prescribes no
    # wrapper syntax and this file invents none — the gate reads the bare id, so any wrapper chosen
    # later (FE #194) still contains it and nothing here has to change when that lands.
    result = _check(f"The notice period is 30 days. Source: [{_SERVED_A}]", {_SERVED_A})
    assert result.passed is True
    assert list(result.violations) == []


async def test_a_marker_on_its_own_line_fires_even_when_another_line_cites() -> None:
    # The "alongside" window, flagged in this module's docstring. One real citation does not buy
    # amnesty for a second claim that points at a source with no id — that is half of #734 surviving
    # a partly-honest answer. THIS is the test to change if the reviewer reads the window as the
    # whole answer rather than the line.
    result = _check(
        f"Notice is 30 days [{_SERVED_A}].\nThe term is one year (source: addendum.md).",
        {_SERVED_A},
    )
    assert result.passed is False
    assert _rules(result) == [1]


# --- rule 2: a cited id the platform never served -------------------------------------------


async def test_an_answer_citing_a_served_id_passes() -> None:
    result = _check(f"The notice period is 30 days [{_SERVED_A}].", {_SERVED_A, _SERVED_B})
    assert result.passed is True
    assert list(result.violations) == []


async def test_an_answer_citing_an_id_that_was_never_served_fails_rule_2() -> None:
    result = _check(f"The notice period is 30 days [{_FORGED}].", {_SERVED_A})
    assert result.passed is False
    assert _rules(result) == [2]
    assert [v.citation_id for v in result.violations] == [_FORGED]


async def test_a_forged_id_alongside_a_real_one_still_fails() -> None:
    # The dangerous shape: a mostly-honest answer with one invented source. Half-credit is not a
    # thing here — the run either cited only what it was served, or it did not.
    result = _check(
        f"Notice is 30 days [{_SERVED_A}] and the protocol version is 2 [{_FORGED}].",
        {_SERVED_A},
    )
    assert result.passed is False
    assert [v.citation_id for v in result.violations] == [_FORGED]


async def test_every_unserved_id_is_reported_not_just_the_first() -> None:
    # The console has to tell the user which sources were invented, so the gate reports all of
    # them. Stopping at the first would understate the problem on exactly the worst answers.
    other = "cit_11112222333344445555666677778888"
    result = _check(f"a [{_FORGED}] and b [{other}]", {_SERVED_A})
    assert result.passed is False
    assert sorted(v.citation_id for v in result.violations) == sorted([_FORGED, other])


async def test_an_empty_served_set_still_rejects_an_invented_id() -> None:
    # Serving nothing does not license citing something. This is the run shape a member reaches by
    # answering without ever retrieving, and it is where a fabricated id is most likely.
    result = _check(f"The notice period is 30 days [{_FORGED}].", set())
    assert result.passed is False
    assert _rules(result) == [2]
    assert [v.citation_id for v in result.violations] == [_FORGED]


async def test_a_run_that_served_nothing_is_not_gated_on_a_plain_answer() -> None:
    # Renamed from `test_a_run_that_served_nothing_cannot_fail_rule_1`: the verdict is unchanged but
    # the old name and comment encoded the DELETED rule's rationale ("rule 1 only fires when the
    # member was actually handed something"). Under rev4 the reason is simpler and stronger — the
    # member cited nothing and pointed at nothing, so there is nothing to check. Acceptance
    # criterion 11's pure-function half.
    result = _check("I already knew the answer: 30 days.", set())
    assert result.passed is True
    assert list(result.violations) == []


# --- #780 item 2, folded in (criterion 12): the id is matched case-insensitively -------------


async def test_an_uppercase_forgery_alongside_a_real_id_fails_rule_2() -> None:
    # The hole #780 recorded. `_CITATION_ID` is lowercase-hex only, so `cit_DEADBEEF…` is not
    # extracted at all: rule 2 never sees it, and rule 1 sees a draft that cites a real id. The
    # draft passes both rules today. Harmless while the gate is unwired; a hole the moment it is
    # not. RED against the shipped gate.
    result = _check(
        f"Notice is 30 days [{_SERVED_A}] and the term is one year [{_FORGED_UPPER}].",
        {_SERVED_A},
    )
    assert result.passed is False
    assert _rules(result) == [2]


async def test_a_served_id_retyped_in_uppercase_still_passes() -> None:
    # The other half of case-insensitivity, and the reason it is the right fix rather than a
    # tightening: ids are minted lowercase, so an uppercase retype is a formatting slip by the
    # model, not a fabrication. Lowercase before the membership test and both halves fall out.
    result = _check(f"The notice period is 30 days [{_SERVED_A_UPPER}].", {_SERVED_A})
    assert result.passed is True
    assert list(result.violations) == []


async def test_a_forgery_in_mixed_case_is_caught_too() -> None:
    mixed = "cit_DeadBeefDEADbeefdeadBEEFdeadbeef"
    result = _check(f"The notice period is 30 days [{mixed}].", {_SERVED_A})
    assert result.passed is False
    assert _rules(result) == [2]
