"""The answer-time citation gate (domain layer) — Contract #735 §CITE **rev4**, issues #743 + #782.

Two rules, both blocking, both evaluated **in platform code** at the run boundary:

* **Rule 1 — the answer NAMES A SOURCE IN PROSE while carrying no ``citation_id`` for it.** Kills
  ``(source: partner-agreement.md)`` and the invented ``source_tool_call_id=call_...``: the member
  pointed at a source, so a citation was available to it, and it wrote prose instead.
* **Rule 2 — a cited ``citation_id`` is not in the set the platform served to that run.** Kills
  every hallucinated source.

Otherwise it PASSES. Neither rule depends on anything a third-party tool chooses to send, because
the platform mints the id itself.

**An answer that cites nothing at all is not a violation.** rev4 deleted rev2's rule 1 ("an asserted
fact carries no ``citation_id``") outright, and its shipped approximation — *the run served sources
and the answer cited none* — with it. Separating an asserted fact from reasoning is a judgment call,
and this Contract forbids a model-implemented gate. The model is not a source of truth, so anything
it says without a citation is its own reasoning, and reasoning needs no source. The case this
protects is the one the gate must never punish: a member that retrieves, reads what came back, and
honestly reports it has no answer. That is the product working.

**This is code, not an OHM member.** UC-E1 draws ``citation-checker`` as a team member, and a
harness member may still review citation *quality* as an ordinary reviewer. The guarantee, though,
is a gate at the run boundary: a gate implemented as a model instruction is a gate that can be
talked out of, which is precisely the failure this Contract closes. So this is a pure function over
(the draft, the run's served set) — no loop, no model, no I/O. **What a violation DOES is loop
behaviour** and lives in ``domain/loop/tool_use.py``: the draft goes back to the member with a
correction, bounded by the run's iteration budget (§CITE rev4 Limit 1).

**rev1's rules 3 and 4 are not here.** rev3 moved precision to connect time (§CITE-QUAL, #744): a
tool that cannot name its documents is refused before it is ever used, so by answer time the only
thing that can still be missing is a version, and a missing version degrades a citation rather than
failing an answer.

Two properties rule 1 needs, or it catches what it was never aimed at:

* **The detection is attribution MARKERS, never filenames.** "I edited ``partner-agreement.md`` for
  you" names a file and asserts nothing; a filename-shaped pattern would block it, and would block
  every member that mentions a file it touched. The marker list is ``source:`` / ``sources:``, and
  changing it is the Contract's business, never the implementer's.
* **``source_tool_call_id`` is NOT a marker** (#788, ruled 2026-08-12 — rev4 listed it, and that is
  superseded). #642 already verifies that token against the member's real trace, so watching it here
  too laid a prose guess over a working proof. See the note on ``_ATTRIBUTION_MARKER`` below.
* **The window is THE LINE.** A marker fires unless a ``cit_`` id sits on the same line. rev4's own
  wording does the work: an answer "names a source in prose while carrying no ``citation_id`` **for
  it**" is a per-marker check, and a whole-answer window cannot express "for it" — it would let a
  member cite one real source and prose-source a second claim in the same draft.

**rev4 Limit 2, on the record: rule 1's detection will misfire.** A bulleted source list fires it
falsely, because the ``Sources:`` line carries no id of its own::

    Sources:
    - [cit_9f2a…]

That costs the member one iteration and a correction naming the remedy, never a refused answer,
which is the deal Limit 2 explicitly strikes. **It is not licence to widen the window.** If the
deployed stack shows it misfiring often enough to matter, that is a ``Contract`` issue for
``solution-architect``.

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
#
# #780 item 2: matched case-INSENSITIVELY. Ids are minted lowercase, so an uppercase retype is a
# formatting slip by the model rather than a fabrication — but a lowercase-only pattern does not
# merely mis-handle it, it fails to EXTRACT the token at all, so an uppercase forgery slips past
# both rules. The flag covers the trailing guard too, or a mixed-case run yields a truncated match.
_CITATION_ID = re.compile(r"\bcit_[0-9a-f]{32}(?![0-9a-f])", re.IGNORECASE)

# rule 1's attribution markers. A marker is the member pointing at a source; a filename is not.
#
# `source_tool_call_id` is deliberately NOT here (#788, ruled 2026-08-12). §CITE rev4 listed it,
# because the #734 PoC used it as a stand-in for a citation the platform never issued. But that
# token is already VERIFIED, properly, by #642: `validate_grounding` resolves every declared
# `source_tool_call_id` against an `ok` tool step in the member's own trace and fails closed on a
# missing, null, unresolvable, or errored id. Watching it here as well put a prose heuristic on top
# of a real verifier — and the heuristic won, because #642's own grounding block (which the platform
# ORDERS every tool-declaring member to emit) has no `cit_` id on its line. A live member was
# corrected four times and killed at `citation_unresolved` for obeying that order.
_ATTRIBUTION_MARKER = re.compile(r"\bsources?:", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CitationViolation:
    """One failed rule. ``citation_id`` names the offending id for rule 2, and is None for rule 1,
    where the defect is prose standing in for an id rather than a particular id being wrong."""

    rule: int
    citation_id: str | None = None


@dataclass(frozen=True, slots=True)
class CitationCheckResult:
    passed: bool
    violations: list[CitationViolation] = field(default_factory=list)


def cited_citation_ids(answer: str) -> list[str]:
    """Every ``citation_id`` occurring in the draft, in first-seen order, deduplicated.

    Citing one source twice in an answer is one citation, so a duplicate never produces a second
    violation and never inflates the report the console shows the user. Deduplication is
    case-insensitive for the same reason the match is, but each id is returned AS THE MODEL WROTE
    IT — a correction that names the id has to name the one the member can find in its own draft.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in _CITATION_ID.finditer(answer):
        citation_id = match.group(0)
        if citation_id.lower() not in seen:
            seen.add(citation_id.lower())
            out.append(citation_id)
    return out


def _names_a_source_without_citing(answer: str) -> bool:
    """Rule 1: does any LINE carry an attribution marker with no ``citation_id`` alongside it?

    Per-line, per this module's docstring — one real citation elsewhere in the draft does not buy
    amnesty for a claim that points at a source with no id.
    """
    return any(
        _ATTRIBUTION_MARKER.search(line) and not _CITATION_ID.search(line)
        for line in answer.splitlines()
    )


def check_answer_citations(answer: str, served: Collection[str]) -> CitationCheckResult:
    """Run both §CITE rules over one draft and the run's served set.

    ``served`` is what the platform actually handed this run — on a resumed run the PERSISTED UNION
    of every segment, not the current segment alone, or a post-pause answer citing a pre-pause
    source would be failed by bookkeeping.

    Rule 1 yields at most ONE violation: it is a single verdict about the draft ("the member pointed
    at a source and wrote prose"), not a count of how many times it did so. EVERY unserved id, by
    contrast, is reported — the console has to tell the user which sources were invented, and
    stopping at the first would understate the problem on exactly the worst answers.
    """
    served_set = {citation_id.lower() for citation_id in served}
    violations: list[CitationViolation] = []
    if _names_a_source_without_citing(answer):
        violations.append(CitationViolation(rule=1))
    violations.extend(
        CitationViolation(rule=2, citation_id=citation_id)
        for citation_id in cited_citation_ids(answer)
        if citation_id.lower() not in served_set
    )
    return CitationCheckResult(passed=not violations, violations=violations)
