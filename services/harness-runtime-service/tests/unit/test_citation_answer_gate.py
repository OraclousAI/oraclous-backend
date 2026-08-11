"""#743 criterion 7 — the answer-time citation gate, in platform code (§CITE rev3).

Two rules, both blocking, both evaluated in platform code at the end of a run. Neither depends on
anything a third-party tool chooses to send, because the platform mints the id itself:

* **Rule 1 — an asserted fact carries no ``citation_id``.** Kills
  ``(source: partner-agreement.md)``: model prose is not a citation.
* **Rule 2 — a cited ``citation_id`` is not in the set the platform served to that run.** Kills
  the invented ``source_tool_call_id=call_...``, and every hallucinated source with it.

**The checker is platform code at the run boundary, not an OHM member** (§CITE rev2 decision 2, and
`solution-architect` on #743). A gate implemented as a model instruction is a gate that can be
talked out of, which is precisely the failure this Contract closes. So it is a pure function over
(the draft, the run's served set) and it is unit-testable with no loop and no model.

Both #734 failures are pinned below as tests, because they are the evidence the Contract was
written from.

**rev1 rules 3 and 4 are NOT tested here.** rev3 moved precision to connect time (#744): a tool that
cannot name its documents is refused before it is ever used, so by answer time the only thing that
can still be missing is a version, and that degrades a citation rather than failing an answer.

Two things this file decides, flagged for the Tests Review gate because §CITE states the rules but
not their mechanism:

* **How a citation appears in a draft.** §CITE fixes ``citation_id`` as ``cit_`` + 32 hex
  characters, which is self-delimiting, so the gate reads the ids occurring in the draft text. §CITE
  does not prescribe a wrapper syntax and these tests do not invent one.
* **A run that served nothing cannot fail rule 1.** A reasoning-only member has no source to cite,
  and rule 1 exists to catch prose standing in for a citation the member *did* have. Without that
  limit the gate would fail every tool-less member — the same trap §CITE-QUAL calls out with its
  Limit 1. If the reviewer reads it otherwise, this is the test to change.

``oraclous_harness_runtime_service.domain.citation_gate`` does not exist yet, so it is imported
function-locally and these tests hard-fail RED until the ``[impl]`` lands
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


def _check(answer: str, served: set[str]) -> Any:
    from oraclous_harness_runtime_service.domain.citation_gate import check_answer_citations

    return check_answer_citations(answer, served)


def _rules(result: Any) -> list[int]:
    return sorted(v.rule for v in result.violations)


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


async def test_the_poc_invented_receipt_id_fails() -> None:
    # #734, verbatim: the model emitted a source_tool_call_id the platform never issued. It is not
    # a citation_id at all, so the answer cites nothing and fails on rule 1 rather than rule 2.
    result = _check(
        "The partner agreement sets a 30-day notice. source_tool_call_id=call_8f3a2b",
        {_SERVED_A},
    )
    assert result.passed is False
    assert _rules(result) == [1]


# --- rule 1: an asserted fact carrying no citation_id ---------------------------------------


async def test_prose_sourcing_is_not_a_citation() -> None:
    # #734, the other half: `(source: partner-agreement.md)` is a bare filename in model prose. It
    # resolves to nothing, no gate can check it, and the member HAD a real id it could have used.
    result = _check("The notice period is 30 days (source: partner-agreement.md).", {_SERVED_A})
    assert result.passed is False
    assert _rules(result) == [1]


async def test_a_run_that_served_nothing_cannot_fail_rule_1() -> None:
    # A reasoning-only member has no source to cite. Rule 1 catches prose standing in for a
    # citation the member was actually handed — see the note in this module's docstring.
    result = _check("I already knew the answer: 30 days.", set())
    assert result.passed is True
    assert list(result.violations) == []


async def test_an_empty_served_set_still_rejects_an_invented_id() -> None:
    # Serving nothing does not license citing something. This is the run shape a member reaches by
    # answering without ever retrieving, and it is where a fabricated id is most likely.
    result = _check(f"The notice period is 30 days [{_FORGED}].", set())
    assert result.passed is False
    assert _rules(result) == [2]
    assert [v.citation_id for v in result.violations] == [_FORGED]
