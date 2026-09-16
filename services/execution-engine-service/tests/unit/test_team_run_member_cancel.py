"""#1072 — the engine cancels a member's harness execution on timeout instead of leaving it to run
orphaned, and folds whatever spend the cancel confirms (or, failing confirmation, the member's own
resolved cap) into the team-pooled budget.

Design (backend-implementer, #1072): ``make_harness_dispatch``'s ``dispatch`` closure mints a fresh
``execution_id`` per member call and sends it to ``harness.execute``, so it can cancel before any
response arrives. On ``HarnessTimeout`` it calls ``harness.cancel(execution_id, ...)``: a confirmed
(200-shaped) result folds its ``total_tokens`` into ``on_cost`` and records the child via
``on_child`` (mirroring the existing ``result.get("id")`` convention the success path already
uses); an unconfirmed cancel (``None`` for a 202, or a raised ``HarnessRejected``/
``HarnessClientError``) fails closed and charges the member's own resolved cap
(``resolve_member_caps``) instead — CLAUDE.md §3.5, an unmeasured spend counts as spent, never as
headroom. Either way the #1067 "timed out: ..." message is still the one raised.

RED until the [impl] lands: today ``dispatch`` sends no ``execution_id`` at all and its
``except HarnessTimeout`` handler raises straight away with no cancel call — every assertion below
fails on a missing kwarg / an empty call list, never a skip or a collection error. All the imports
below are pre-existing seams (``make_harness_dispatch``, ``run_team_harness``, the ``Harness*``
errors) — only their BEHAVIOUR is new, so they are imported at module level like every other direct
test of this factory (e.g. ``test_team_per_member_cap.py``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClientError,
    HarnessRejected,
    HarnessTimeout,
)
from oraclous_execution_engine_service.services.team_run import (
    make_harness_dispatch,
    run_team_harness,
)
from oraclous_ohm.manifest import OHMBudget, OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("13579bdf-2468-4680-9753-13579bdf2468")


def _member(role: str = "a", *, max_tokens: int | None = None) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", max_tokens=max_tokens)


def _team(members: list[OHMMember], budget: OHMBudget | None = None) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        budget=budget,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


class _TimeoutThenCancelHarness:
    """Every ``execute()`` call times out. ``cancel()`` is scripted per call, FIFO, via
    ``cancel_results``: a ``dict`` → the harness CONFIRMED the cancel (the same shape
    ``HarnessClient.cancel``'s 200 response returns — carrying ``id``/``status``/``total_tokens``);
    ``None`` → a 202 (still winding down, ``HarnessClient.cancel`` returns ``None``); an
    ``Exception`` instance → raised as-is (``HarnessRejected`` for a 404, ``HarnessClientError`` for
    a transport failure). Records every ``execute``/``cancel`` call's kwargs for inspection."""

    def __init__(self, cancel_results: list[Any] | None = None) -> None:
        self.execute_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self._cancel_results = list(cancel_results) if cancel_results is not None else [None]

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.execute_calls.append(kwargs)
        raise HarnessTimeout("harness call timed out: exceeded its wall-clock time limit")

    async def cancel(self, execution_id: uuid.UUID, **kwargs: Any) -> dict[str, Any] | None:
        self.cancel_calls.append({"execution_id": execution_id, **kwargs})
        idx = len(self.cancel_calls) - 1
        outcome = self._cancel_results[idx] if idx < len(self._cancel_results) else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _RoleAwareOrphanSpendHarness:
    """Member ``a`` always times out and its cancel CONFIRMS an orphan spend; any other role
    (only ``b`` in the pooled-ceiling test below) would succeed — used to prove it is never
    dispatched at all."""

    def __init__(self, orphan_tokens: int) -> None:
        self.execute_calls: list[dict[str, Any]] = []
        self._orphan_tokens = orphan_tokens

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.execute_calls.append(kwargs)
        if kwargs.get("manifest_ref") == "org:x/a@1":
            raise HarnessTimeout("harness call timed out: exceeded its wall-clock time limit")
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran"}

    async def cancel(self, execution_id: uuid.UUID, **kwargs: Any) -> dict[str, Any] | None:
        return {"id": str(execution_id), "status": "CANCELLED", "total_tokens": self._orphan_tokens}


async def test_timeout_cancels_dispatched_execution_id() -> None:
    """On a timeout, dispatch cancels the SAME execution_id it minted and sent to execute() —
    exactly once, never a fresh/different id and never zero calls."""
    harness = _TimeoutThenCancelHarness(
        cancel_results=[{"id": "x", "status": "CANCELLED", "total_tokens": 0}]
    )
    dispatch = make_harness_dispatch(harness, {})
    with pytest.raises(HarnessClientError):
        await dispatch(_member(max_tokens=1_000), [], None)
    assert len(harness.execute_calls) == 1
    assert len(harness.cancel_calls) == 1
    dispatched_id = harness.execute_calls[0].get("execution_id")
    assert dispatched_id is not None
    assert harness.cancel_calls[0]["execution_id"] == dispatched_id


async def test_cancelled_spend_folds_into_on_cost_and_on_child() -> None:
    """A confirmed cancel (200-shaped, carrying total_tokens=321) folds that spend into on_cost and
    records the child via on_child (the cancel result's own "id", mirroring the existing
    result.get("id") convention on the success path) — the #1067 timeout message is still raised,
    a confirmed spend never swallows the failure."""
    costs: list[int] = []
    children: list[tuple[str, str]] = []
    harness = _TimeoutThenCancelHarness(
        cancel_results=[{"id": "cancelled-child-id", "status": "CANCELLED", "total_tokens": 321}]
    )
    dispatch = make_harness_dispatch(
        harness,
        {},
        on_cost=costs.append,
        on_child=lambda cid, role: children.append((cid, role)),
    )
    with pytest.raises(HarnessClientError) as exc_info:
        await dispatch(_member(role="writer", max_tokens=1_000), [], None)
    assert "timed out:" in str(exc_info.value)
    assert costs == [321]
    assert children == [("cancelled-child-id", "writer")]


@pytest.mark.parametrize(
    "cancel_outcome",
    [
        None,
        HarnessRejected(404, "not found"),
        HarnessClientError("connection refused"),
    ],
    ids=["202_pending", "404_rejected", "transport_error"],
)
async def test_unconfirmed_cancel_charges_member_cap(cancel_outcome: Any) -> None:
    """When the cancel itself cannot CONFIRM the spend (202 → None, or a raised
    HarnessRejected/HarnessClientError), the dispatch fails closed and charges the member's own
    resolved cap (resolve_member_caps) to the pool rather than the unmeasurable true spend
    (CLAUDE.md §3.5 — an ambiguous tally is spent, never headroom). The #1067 timeout message is
    still raised."""
    costs: list[int] = []
    harness = _TimeoutThenCancelHarness(cancel_results=[cancel_outcome])
    dispatch = make_harness_dispatch(harness, {}, on_cost=costs.append)
    with pytest.raises(HarnessClientError) as exc_info:
        await dispatch(_member(max_tokens=5_000), [], None)
    assert "timed out:" in str(exc_info.value)
    assert costs == [5_000]


async def test_unconfirmed_cancel_without_member_cap_charges_nothing() -> None:
    """Owner ruling (#1072, fail-closed default): an unconfirmed cancel (here a 202 -> None) still
    fails closed when there IS a resolved cap to protect (the case above). But when the member has
    NO resolved cap at all — no member-level max_tokens override AND no team budget block to fall
    back to, so resolve_member_caps(member, None) yields (None, None) — there is no pool to
    protect, so the dispatch charges nothing: on_cost is never called (not even on_cost(None),
    which would crash any real caller expecting an int), and the #1067 timeout message is still
    raised unchanged."""
    costs: list[int] = []
    harness = _TimeoutThenCancelHarness(cancel_results=[None])  # 202: cancel never confirms
    dispatch = make_harness_dispatch(harness, {}, on_cost=costs.append)
    with pytest.raises(HarnessClientError) as exc_info:
        await dispatch(_member(max_tokens=None), [], None)  # no override, no team budget → no pool
    assert "timed out:" in str(exc_info.value)
    assert len(harness.cancel_calls) == 1  # the timeout still attempts a cancel, cap or no cap
    assert costs == []  # nothing to protect => nothing charged, and on_cost(None) never happens


async def test_fresh_execution_id_per_dispatch() -> None:
    """Each dispatch mints its OWN execution_id — two dispatches of the same member never reuse an
    id, so a cancel sent for the first can never race a second dispatch's in-flight execution."""
    harness = _TimeoutThenCancelHarness(cancel_results=[None, None])
    dispatch = make_harness_dispatch(harness, {})
    member = _member(max_tokens=1_000)
    with pytest.raises(HarnessClientError):
        await dispatch(member, [], None)
    with pytest.raises(HarnessClientError):
        await dispatch(member, [], None)
    assert len(harness.execute_calls) == 2
    first_id = harness.execute_calls[0].get("execution_id")
    second_id = harness.execute_calls[1].get("execution_id")
    assert first_id is not None
    assert second_id is not None
    assert first_id != second_id


async def test_pooled_ceiling_halts_next_member_after_orphan_spend() -> None:
    """ADR-031 §D3 + #1072: member "a" times out; its cancel CONFIRMS a spend that alone exceeds
    the team's pooled ceiling. That confirmed spend is charged to the pool BEFORE "b" is ever
    admitted, in the NEXT stage — "b" depends on "c" (a SIBLING of "a" that SUCCEEDS), never on
    "a" itself, so the only thing that can stop "b" from dispatching is the pooled ceiling, never
    ADR-042's separate "blocked by a failed upstream" path. "b"'s harness.execute is never called
    at all."""
    harness = _RoleAwareOrphanSpendHarness(orphan_tokens=1_500)
    team = _team(
        [
            OHMMember(role="a", kind="agent", manifest_ref="org:x/a@1"),
            OHMMember(role="c", kind="agent", manifest_ref="org:x/c@1"),
            OHMMember(role="b", kind="agent", manifest_ref="org:x/b@1", depends_on=["c"]),
        ],
        budget=OHMBudget(max_tokens_total=1_000),
    )
    result = await run_team_harness(team, harness)
    dispatched_refs = {c.get("manifest_ref") for c in harness.execute_calls}
    assert dispatched_refs == {"org:x/a@1", "org:x/c@1"}  # "b" never reached the harness at all
    assert result.member_status["c"] == "succeeded"  # "b"'s dependency delivered cleanly
    assert result.member_status["b"] == "budget_skipped"  # the POOL stopped it, not a block
