"""ADR-043 #552 conductor hardening (self-review + CTO fast-follow) — fail-fast manifest validation
+ the ADR-042 seed-status fix. RED until the [impl] lands.

Three holes the deployed conductor shipped with: (1) a declared loop convergence threshold with NO
``success_criteria`` would silently skip the evaluator grade (a false-positive convergence) — the
manifest must reject the combo fail-fast; (2) a non-positive ``max_rounds`` / budget is accepted;
(3) a seeded loop member that converges immediately on resume loses its ADR-042 ``member_status``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import OHMLoop, OHMMember
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _team_doc(*, convergence: str | None = None, success_criteria: str = "", **termination: Any):
    term: dict[str, Any] = {}
    if convergence is not None:
        term["convergence"] = convergence
    term.update(termination)
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "t",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [
            {"role": "writer", "kind": "agent", "manifest_ref": "o:x/writer@1"},
            {"role": "critic", "kind": "agent", "manifest_ref": "o:x/critic@1"},
        ],
        "orchestration": {"success_criteria": success_criteria, "termination": term},
        "runtime": {"entrypoint": "writer"},
    }


def test_convergence_without_success_criteria_is_rejected_at_load() -> None:
    # a declared threshold has nothing to grade against → reject fail-fast (else the done-check
    # silently skips the grade and converges on coverage+artifacts alone — a false positive).
    with pytest.raises(OHMError):
        load_ohm(_team_doc(convergence="evaluator>=0.8", success_criteria=""))


def test_convergence_with_success_criteria_loads() -> None:
    m = load_ohm(_team_doc(convergence="evaluator>=0.8", success_criteria="an accurate draft"))
    assert m.orchestration is not None
    assert m.orchestration.termination.convergence == "evaluator>=0.8"


def test_absent_convergence_needs_no_criteria() -> None:
    # no threshold declared → success_criteria is optional (coverage+artifacts are the floor)
    m = load_ohm(_team_doc(convergence=None, success_criteria=""))
    assert m.orchestration is not None


@pytest.mark.parametrize("bad", [0, -1])
def test_max_rounds_must_be_positive(bad: int) -> None:
    with pytest.raises(OHMError):
        load_ohm(_team_doc(success_criteria="x", max_rounds=bad))


@pytest.mark.parametrize("bad", [0, -1])
def test_max_wall_seconds_must_be_positive(bad: int) -> None:
    with pytest.raises(OHMError):
        load_ohm(_team_doc(success_criteria="x", max_wall_seconds=bad))


def test_budget_must_be_positive() -> None:
    doc = _team_doc(success_criteria="x")
    doc["budget"] = {"max_tokens_total": -100}
    with pytest.raises(OHMError):
        load_ohm(doc)


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


def _by(*roles: str) -> dict[str, OHMMember]:
    return {r: OHMMember(role=r, kind="agent", manifest_ref=f"o:x/{r}@1") for r in roles}


async def test_seeded_members_keep_member_status_on_immediate_convergence() -> None:
    # ADR-042: a loop resumed with seeded results that converges immediately (the coordinator sees
    # every member already produced → returns []) must still report the seeded members' terminal
    # status — else the persisted member_status is incomplete + the run looks like nobody ran.
    from oraclous_ohm.orchestrate import run_loop_seam

    async def dispatch(member: OHMMember, envs: Any, item: Any) -> dict:  # must NOT be called
        raise AssertionError("a seeded+converged loop must not re-dispatch")

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return []  # everything already produced (seeded) → the coordinator is done

    async def done_check(results: dict[str, Any]) -> bool:
        return True  # the coded check confirms (the seeded outputs satisfy it)

    res = await run_loop_seam(
        loop=_loop("a", "b"),
        by_role=_by("a", "b"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=5,
        seed_results={"a": {"out": "a"}, "b": {"out": "b"}},
    )
    assert res.status == "converged"
    assert res.member_status.get("a") == "succeeded"  # seeded members recorded, not dropped
    assert res.member_status.get("b") == "succeeded"
