"""Closed-loop verdict-consumption decision (domain layer; #604, ADR-048 decision 5).

The E4 evaluator produces a Verdict the engine stores on the settled team-run row; this module
is the PURE, deterministic decision that consumes it — given the stored verdict + the run's
terminal state + its cross-re-dispatch loop state, it returns one branch: STORE_ONLY | RE_TASK |
ESCALATE.
No I/O, no DB.

The value-set MAPPING (CTO-confirmed on #604; the ADR-048 §5 table names ``re_task/re_route/
escalate_human`` but the SHIPPED verdict enums are ``{accept,revise,retry,escalate_human,reject}`` /
``{pass,block,escalate_human}`` — the ADR table is what's stale) is applied as a **fail-closed
precedence**:

1. a pool-exhausted terminal (#585 ``COST_BUDGET``) → STORE_ONLY (#604 NEVER re-handles pool
   exhaustion — there is no pool to re-dispatch into, and ``COST_BUDGET`` is already #585's GOVERNED
   terminal / escalation surface; converting it would regress the shipped #585 contract, and a
   ``COST_BUDGET`` run carries no graded verdict anyway). Checked FIRST so it is authoritative even
   for the (structurally-impossible) dict-verdict case — a defensive guard against re-dispatching
   into an empty pool.
2. no gate / unparseable verdict → STORE_ONLY (nothing to consume — the common no-criteria run).
3. the gate PASSED → STORE_ONLY (terminal; unchanged behaviour).
4. a CRITICAL floor failure (battery ``blocking_severity``) → ESCALATE (ADR-037 line 116, regardless
   of the recommended_action).
5. ``recommended_action`` ∈ {escalate_human, reject} → ESCALATE (HITL — ``reject`` is a human-class
   verdict; autonomously re-dispatching it would violate accepted ADR-037 Decision 4).
6. ``recommended_action`` ∈ {revise, retry, block} → RE_TASK, UNLESS a bound trips → ESCALATE:
   * LIVELOCK — the same below-threshold SHAPE recurs with no score improvement across
     re-dispatches (the anti-repeat guard, lifted to the verdict layer);
   * the ``MAX_RE_DISPATCHES`` ceiling is reached.
7. anything else below-threshold (an unmapped/ambiguous action) → ESCALATE (fail-closed default).

``re_route`` is NOT a live branch: no shipped verdict value maps to it and there is no run-level
routing seam (the only routing is the within-run ADR-043 loop, off-limits per #552/#553) — building
it would be an unreachable dead path (CTO-confirmed). It is a deferred opt-in.
"""

from __future__ import annotations

import json
from typing import Any

STORE_ONLY = "store_only"
RE_TASK = "re_task"
ESCALATE = "escalate"

# shipped verdict values → branch (single-rubric RecommendedAction + battery recommended_action)
_RE_TASK_ACTIONS = frozenset({"revise", "retry", "block"})
_ESCALATE_ACTIONS = frozenset({"escalate_human", "reject"})

#: the run terminal state that means the #585 run-level pool was exhausted mid-drive
_POOL_EXHAUSTED_STATE = "COST_BUDGET"


def verdict_passed(verdict: Any) -> bool:  # noqa: ANN401
    """True iff the gate verdict is a clean pass. Handles BOTH stored keys — a prose Verdict dumps
    the alias ``pass`` OR the field name ``passed`` (eval/types.py), the battery + the fail-closed
    verdict use ``pass``/``passed`` — so a mismatch never fail-opens a below-threshold run."""
    if not isinstance(verdict, dict):
        return False
    for key in ("pass", "passed"):
        if key in verdict:
            return bool(verdict[key])
    return False


def verdict_score(verdict: Any) -> float | None:  # noqa: ANN401
    """A 0–1 attainment: a prose Verdict's ``score`` or a battery's passed-fraction over its checks.
    ``None`` when absent/unparseable (fail-closed — treated as no improvement)."""
    if not isinstance(verdict, dict):
        return None
    score = verdict.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return max(0.0, min(1.0, float(score)))
    checks = verdict.get("check_verdicts")
    if isinstance(checks, list) and checks:
        passed = sum(1 for c in checks if isinstance(c, dict) and c.get("passed"))
        return passed / len(checks)
    return None


def verdict_fingerprint(verdict: Any) -> str:  # noqa: ANN401
    """The livelock recurrence key — the below-threshold SHAPE (which dimensions/checks failed),
    NOT the score. The score is deliberately EXCLUDED: it is the IMPROVEMENT measure, compared
    separately by ``_improved`` against ``livelock_epsilon``. Folding the score into the key (as a
    rounded bucket) would let a sub-epsilon score jitter (0.50→0.51, below the 0.02 epsilon) mint a
    FRESH key every re-dispatch — the fingerprint-equality guard would never match and the epsilon
    check would be unreachable, so a stuck run would burn the whole re-dispatch budget instead of
    escalating (review VC-1). The SAME failing shape recurring with no meaningful score gain is a
    stuck loop, whatever the score jitter."""
    # Read the failing-check id under EITHER schema's key: a battery ``OHMCheckVerdict`` keys it
    # ``name``, a prose ``Verdict`` failure keys it ``dimension`` (``check`` kept as a defensive
    # fallback). Missing ``name`` would collapse every battery verdict to one empty-dims key and
    # falsely flag a genuinely-new battery failure shape as a livelock (review VC-1 residual).
    dims: list[str] = []
    if isinstance(verdict, dict):
        failures = verdict.get("failures")
        if isinstance(failures, list):  # battery OHMCheckVerdict blocking subset, or prose failures
            dims += [
                str(f.get("check") or f.get("dimension") or f.get("name") or "")
                for f in failures
                if isinstance(f, dict)
            ]
        checks = verdict.get("check_verdicts")
        if isinstance(checks, list):
            dims += [
                str(c.get("check") or c.get("dimension") or c.get("name") or "")
                for c in checks
                if isinstance(c, dict) and not c.get("passed")
            ]
    return json.dumps({"dims": sorted(set(dims))}, sort_keys=True)


def _improved(score: float | None, last_score: float | None, epsilon: float) -> bool:
    """True if this run's score is a MEANINGFUL gain over the prior re-dispatch (fail-closed: an
    absent score on either side counts as NO improvement, so an oscillation escalates)."""
    if score is None or last_score is None:
        return False
    return score > last_score + epsilon


def decide_action(
    verdict: Any,  # noqa: ANN401
    *,
    run_state: str,
    re_dispatch_count: int,
    last_verdict_score: float | None,
    last_verdict_fingerprint: str | None,
    max_re_dispatches: int,
    livelock_epsilon: float,
) -> str:
    """Return the branch — STORE_ONLY | RE_TASK | ESCALATE — for a settled run (fail-closed
    precedence; see the module docstring). ``re_dispatch_count``/``last_verdict_*`` are this run's
    persisted cross-re-dispatch loop state (0/None on the first settle)."""
    if run_state == _POOL_EXHAUSTED_STATE:
        # #585 governs pool exhaustion — COST_BUDGET is its terminal, not a #604 re-dispatch trigger
        # (no pool to re-dispatch into; escalating would regress the shipped #585 contract).
        # Checked FIRST so it is authoritative even for a (structurally-impossible) dict verdict — a
        # defensive guard against re_task-ing into an empty pool (review F3).
        return STORE_ONLY
    if not isinstance(verdict, dict):
        return STORE_ONLY  # no gate declared / unparseable → nothing to consume (run stays as-is)
    if verdict.get("grader_unavailable"):
        return (
            STORE_ONLY  # a grader OUTAGE is not a real grade — never branch (run stays SUCCEEDED)
        )
    if verdict_passed(verdict):
        return STORE_ONLY
    if verdict.get("blocking_severity") == "CRITICAL":
        return ESCALATE  # a CRITICAL floor failure is HITL-class regardless of recommended_action
    action = verdict.get("recommended_action")
    if action in _ESCALATE_ACTIONS:
        return ESCALATE
    if action in _RE_TASK_ACTIONS:
        # a re_task candidate — but the bounds make the loop terminate (fail-closed → escalate)
        if re_dispatch_count >= max_re_dispatches:
            return ESCALATE
        if (
            last_verdict_fingerprint is not None
            and verdict_fingerprint(verdict) == last_verdict_fingerprint
        ):
            if not _improved(verdict_score(verdict), last_verdict_score, livelock_epsilon):
                return ESCALATE  # livelock — same below-threshold shape, no score gain
        return RE_TASK
    return ESCALATE  # unmapped/ambiguous below-threshold action → fail-closed


def next_loop_state(verdict: Any, prior_count: int) -> dict[str, Any]:  # noqa: ANN401
    """The cross-re-dispatch loop state to persist when a run is RE_TASK re-dispatched: the bumped
    count + this verdict's score + fingerprint (the livelock basis for the next settle)."""
    return {
        "re_dispatch_count": prior_count + 1,
        "last_verdict_score": verdict_score(verdict),
        "last_verdict_fingerprint": verdict_fingerprint(verdict),
    }
