"""The answer-time citation gate (domain layer) — Contract #735 §CITE, issue #743.

Two rules, both blocking, both evaluated **in platform code** at the end of a run:

| # | Rule | Kills |
| --- | --- | --- |
| 1 | An asserted fact carries no ``citation_id``. | ``(source: partner-agreement.md)`` — prose is not a citation. |
| 2 | A cited ``citation_id`` is not in the set the platform served to that run. | The invented ``source_tool_call_id=call_...``, and every hallucinated source. |

Otherwise it PASSES. Neither rule depends on anything a third-party tool chooses to send, because
the platform mints the id itself.

**This is code, not an OHM member.** UC-E1 draws ``citation-checker`` as a team member, and a
harness member may still review citation *quality* as an ordinary reviewer. The guarantee, though,
is a gate at the run boundary: a gate implemented as a model instruction is a gate that can be
talked out of, which is precisely the failure this Contract closes. So this is a pure function over
(the draft, the run's served set) — no loop, no model, no I/O.

**rev1's rules 3 and 4 are not here.** rev3 moved precision to connect time (§CITE-QUAL, #744): a
tool that cannot name its documents is refused before it is ever used, so by answer time the only
thing that can still be missing is a version, and a missing version degrades a citation rather than
failing an answer.

Two limits the rules need, or they catch things they were never aimed at:

* **A run that served nothing cannot fail rule 1.** A reasoning-only member has no source to cite,
  and rule 1 exists to catch prose standing in for a citation the member actually WAS handed.
  Without the limit the gate fails every tool-less member on every run. §CITE-QUAL's Limit 1 is the
  same shape: never grade a thing that has nothing to cite. Serving nothing still does not license
  citing something — an invented id with an empty served set fails rule 2.
* **A draft that cites at least one id satisfies rule 1**, even when that id turns out to be
  invented. The two rules answer different questions: rule 1 asks whether the member cited at all,
  rule 2 asks whether what it cited was real. An invented id is a rule 2 failure, once.

§CITE fixes ``citation_id`` as ``cit_`` + 32 hex characters, which is self-delimiting, so the gate
reads the ids occurring in the draft. §CITE prescribes no wrapper syntax and this module invents
none: any wrapper chosen later (FE #194, asked on #735) still contains the id, so nothing here has
to change when that lands.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

# §CITE: `citation_id` = "cit_" + sha256(...)[:32] — lowercase hex, fixed width, self-delimiting.
# The trailing guard stops a longer hex run from yielding a truncated (and wrong) id; the leading
# `\b` stops a mid-word match. NOT a security control: a forged id is well-formed by construction,
# so only rule 2 — membership of the served set — can tell a real one from an invented one.
_CITATION_ID = re.compile(r"\bcit_[0-9a-f]{32}(?![0-9a-f])")


@dataclass(frozen=True, slots=True)
class CitationViolation:
    """One failed rule. ``citation_id`` names the offending id for rule 2, and is None for rule 1,
    where the defect is the ABSENCE of any id rather than a particular one."""

    rule: int
    citation_id: str | None = None


@dataclass(frozen=True, slots=True)
class CitationCheckResult:
    passed: bool
    violations: list[CitationViolation] = field(default_factory=list)


def cited_citation_ids(answer: str) -> list[str]:
    """Every ``citation_id`` occurring in the draft, in first-seen order, deduplicated.

    Citing one source twice in an answer is one citation, so a duplicate never produces a second
    violation and never inflates the report the console shows the user.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in _CITATION_ID.finditer(answer):
        citation_id = match.group(0)
        if citation_id not in seen:
            seen.add(citation_id)
            out.append(citation_id)
    return out


def check_answer_citations(answer: str, served: Collection[str]) -> CitationCheckResult:
    """Run both §CITE rules over one draft and the run's served set.

    ``served`` is ``LoopResult.served_citation_ids`` — what the platform actually handed this run.
    EVERY unserved id is reported, never only the first: the console has to tell the user which
    sources were invented, and stopping early would understate the problem on exactly the worst
    answers.
    """
    served_set = set(served)
    cited = cited_citation_ids(answer)
    violations: list[CitationViolation] = []
    if not cited and served_set:
        # Rule 1 — the member was handed sources and cited none of them. Gated on a non-empty served
        # set, per the limit in this module's docstring.
        violations.append(CitationViolation(rule=1))
    violations.extend(
        CitationViolation(rule=2, citation_id=citation_id)
        for citation_id in cited
        if citation_id not in served_set
    )
    return CitationCheckResult(passed=not violations, violations=violations)
