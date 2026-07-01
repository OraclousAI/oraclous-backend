"""#604 — the pure closed-loop verdict-consumption decision (domain/verdict_consumption.py).

Deterministic, no I/O. Exercises the fail-closed precedence: STORE_ONLY | RE_TASK | ESCALATE.
"""

from __future__ import annotations

import pytest
from oraclous_execution_engine_service.domain import verdict_consumption as vc

pytestmark = pytest.mark.unit

_KW = dict(  # the common decide_action bounds (a first settle: count 0, no prior score/fingerprint)
    run_state="SUCCEEDED",
    re_dispatch_count=0,
    last_verdict_score=None,
    last_verdict_fingerprint=None,
    max_re_dispatches=3,
    livelock_epsilon=0.02,
)


def _decide(verdict, **over) -> str:  # noqa: ANN001
    return vc.decide_action(verdict, **{**_KW, **over})


# ── verdict_passed / verdict_score ────────────────────────────────────────────────────────────────
def test_verdict_passed_reads_both_pass_and_passed_keys() -> None:
    assert vc.verdict_passed({"pass": True}) is True
    assert vc.verdict_passed({"passed": True}) is True
    assert vc.verdict_passed({"pass": False}) is False
    assert vc.verdict_passed({}) is False and vc.verdict_passed(None) is False


def test_verdict_score_prose_and_battery_fraction() -> None:
    assert vc.verdict_score({"score": 0.7}) == pytest.approx(0.7)
    assert vc.verdict_score({"check_verdicts": [{"passed": True}, {"passed": False}]}) == 0.5
    assert vc.verdict_score({}) is None


# ── the store-only branch ─────────────────────────────────────────────────────────────────────────
def test_no_verdict_is_store_only() -> None:
    assert _decide(None) == vc.STORE_ONLY  # no gate declared → nothing to consume


def test_a_passing_verdict_is_store_only() -> None:
    assert _decide({"pass": True, "recommended_action": "accept"}) == vc.STORE_ONLY
    assert _decide({"passed": True, "recommended_action": "pass"}) == vc.STORE_ONLY


def test_a_grader_outage_verdict_is_store_only_never_escalates() -> None:
    # a transient grader blip (recommended_action hardcoded escalate_human) must NOT branch the run
    out = {"pass": False, "recommended_action": "escalate_human", "grader_unavailable": True}
    assert _decide(out) == vc.STORE_ONLY


# ── the escalate branch (HITL) ────────────────────────────────────────────────────────────────────
def test_escalate_human_and_reject_escalate() -> None:
    assert _decide({"pass": False, "recommended_action": "escalate_human"}) == vc.ESCALATE
    # reject is a HITL-class verdict — escalates, NEVER an autonomous re-dispatch (ADR-037 Dec 4)
    assert _decide({"pass": False, "recommended_action": "reject"}) == vc.ESCALATE


def test_a_critical_floor_failure_escalates_regardless_of_action() -> None:
    v = {"pass": False, "recommended_action": "revise", "blocking_severity": "CRITICAL"}
    assert _decide(v) == vc.ESCALATE  # CRITICAL is HITL-class even with a re_task action


def test_pool_exhausted_is_store_only_not_a_604_concern() -> None:
    # review F3: COST_BUDGET is #585's GOVERNED terminal (its own escalation surface), NOT a #604
    # re-dispatch/escalate trigger — there is no pool to re-dispatch into, and escalating would
    # regress the shipped #585 COST_BUDGET-terminal contract. Checked FIRST, so it is authoritative
    # even for a (structurally-impossible) below-threshold dict verdict — never into an empty pool.
    v = {"pass": False, "recommended_action": "revise"}
    assert _decide(v, run_state="COST_BUDGET") == vc.STORE_ONLY
    assert _decide(None, run_state="COST_BUDGET") == vc.STORE_ONLY  # the real (no-verdict) case


def test_an_unmapped_action_escalates_fail_closed() -> None:
    assert _decide({"pass": False, "recommended_action": "wat"}) == vc.ESCALATE
    assert _decide({"pass": False}) == vc.ESCALATE  # no action at all → fail-closed


# ── the re_task branch ────────────────────────────────────────────────────────────────────────────
def test_revise_retry_block_re_task() -> None:
    for action in ("revise", "retry", "block"):
        assert _decide({"pass": False, "recommended_action": action}) == vc.RE_TASK


# ── the bounds (a closed loop MUST terminate) ─────────────────────────────────────────────────────
def test_max_re_dispatches_ceiling_escalates() -> None:
    v = {"pass": False, "recommended_action": "revise"}
    assert _decide(v, re_dispatch_count=2) == vc.RE_TASK  # below the ceiling
    assert _decide(v, re_dispatch_count=3) == vc.ESCALATE  # at the ceiling → escalate


def test_livelock_same_fingerprint_no_improvement_escalates() -> None:
    v = {"pass": False, "recommended_action": "revise", "score": 0.5, "failures": [{"check": "x"}]}
    fp = vc.verdict_fingerprint(v)
    # the SAME below-threshold shape recurring with no score gain → livelock → escalate
    assert _decide(v, last_verdict_fingerprint=fp, last_verdict_score=0.5) == vc.ESCALATE
    # a MEANINGFUL score gain over the prior → not a livelock → keep re-tasking
    assert _decide(v, last_verdict_fingerprint=fp, last_verdict_score=0.40) == vc.RE_TASK


def test_a_different_fingerprint_is_not_a_livelock() -> None:
    v = {"pass": False, "recommended_action": "revise", "score": 0.5, "failures": [{"check": "y"}]}
    assert (
        _decide(v, last_verdict_fingerprint="something-else", last_verdict_score=0.5) == vc.RE_TASK
    )


def test_livelock_sub_epsilon_score_drift_with_same_shape_still_escalates() -> None:
    # review VC-1: the fingerprint is the failure SHAPE (dims), NOT the score — so a stuck run whose
    # score jitters SUB-EPSILON (0.50 → 0.51, below the 0.02 epsilon) while the SAME dims keep
    # failing is still a livelock. If the score were folded into the fingerprint, the key would
    # differ every round and the guard would be unreachable (the run would burn the whole ceiling).
    # Uses the real BATTERY OHMCheckVerdict shape (keyed ``name``) — the deployed e2e's path.
    prior = {
        "passed": False,
        "recommended_action": "block",
        "score": 0.50,
        "failures": [{"name": "gate"}],
    }
    now = {
        "passed": False,
        "recommended_action": "block",
        "score": 0.51,
        "failures": [{"name": "gate"}],
    }
    assert vc.verdict_fingerprint(now) == vc.verdict_fingerprint(
        prior
    )  # same shape, score excluded
    # 0.51 is only +0.01 over 0.50 (< the 0.02 epsilon) → no meaningful gain → livelock → escalate
    assert (
        _decide(
            now, last_verdict_fingerprint=vc.verdict_fingerprint(prior), last_verdict_score=0.50
        )
        == vc.ESCALATE
    )


def test_fingerprint_distinguishes_failing_shape_under_both_verdict_schemas() -> None:
    # review VC-1 residual: the failing-check id lives under DIFFERENT keys — a battery
    # OHMCheckVerdict keys it ``name``, a prose Verdict failure keys it ``dimension``. The key must
    # read both, or every battery verdict collapses to one empty-dims key and a new battery failure
    # shape is falsely escalated as a livelock (the exact e2e path).
    # battery shape (``name``): different failing checks → different keys; score is excluded.
    assert vc.verdict_fingerprint({"score": 0.3, "failures": [{"name": "must-contain"}]}) == (
        vc.verdict_fingerprint({"score": 0.9, "failures": [{"name": "must-contain"}]})
    )
    assert vc.verdict_fingerprint({"failures": [{"name": "must-contain"}]}) != (
        vc.verdict_fingerprint({"failures": [{"name": "min-length"}]})
    )
    # neither battery key is empty-collapsed
    assert vc.verdict_fingerprint(
        {"failures": [{"name": "must-contain"}]}
    ) != vc.verdict_fingerprint({})
    # prose shape (``dimension``) via check_verdicts, failing-only
    prose_x = {"check_verdicts": [{"dimension": "accuracy", "passed": False}]}
    prose_y = {"check_verdicts": [{"dimension": "coverage", "passed": False}]}
    assert vc.verdict_fingerprint(prose_x) != vc.verdict_fingerprint(prose_y)


def test_next_loop_state_bumps_count_and_records_the_basis() -> None:
    v = {"pass": False, "recommended_action": "revise", "score": 0.5}
    st = vc.next_loop_state(v, 1)
    assert st["re_dispatch_count"] == 2
    assert st["last_verdict_score"] == pytest.approx(0.5)
    assert st["last_verdict_fingerprint"] == vc.verdict_fingerprint(v)
