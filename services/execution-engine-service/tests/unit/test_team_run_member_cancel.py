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

import asyncio
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
    (only ``c`` in the pooled-ceiling test below — a SIBLING of "a", not a dependent) would
    succeed — used to prove "a" is never allowed to gate "c" out of the harness, only "b" (which
    depends on "c") is.

    "a" and "c" have no dependency between them, so a correct scheduler dispatches both
    concurrently. Nothing here awaits anything by itself, so without an explicit yield point a
    single-threaded event loop would run whichever coroutine starts first straight through to
    completion — including "a" raising and its cancel charging the pool — before the other ever
    gets a turn; that would make "c" reach the harness only by accident of scheduling order, not
    because the implementation actually dispatches siblings concurrently. ``c_dispatched`` forces
    "a" to always wait until "c"'s ``execute()`` call has actually been recorded before "a" raises
    its timeout, regardless of which coroutine the loop happens to start first — if "c" already
    set the event, ``Event.wait()`` returns immediately with no suspension, so this never adds
    latency to the case the scheduler already gets right.
    """

    def __init__(self, orphan_tokens: int) -> None:
        self.execute_calls: list[dict[str, Any]] = []
        self._orphan_tokens = orphan_tokens
        self.c_dispatched = asyncio.Event()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.execute_calls.append(kwargs)
        if kwargs.get("manifest_ref") == "org:x/a@1":
            await asyncio.wait_for(self.c_dispatched.wait(), timeout=5.0)
            raise HarnessTimeout("harness call timed out: exceeded its wall-clock time limit")
        self.c_dispatched.set()  # "c" has reached the harness — safe now for "a" to raise
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran"}

    async def cancel(self, execution_id: uuid.UUID, **kwargs: Any) -> dict[str, Any] | None:
        return {"id": str(execution_id), "status": "CANCELLED", "total_tokens": self._orphan_tokens}


class _EarlyBookThenUnconfirmedCancelHarness:
    """One member succeeds and books its own ``total_tokens``; an independent second member times
    out and its cancel never CONFIRMS (202 -> ``None``) — used to prove what an unconfirmed cancel
    charges when the team has ONLY a pooled ``max_tokens_total`` and no resolved per-member cap
    (#1072 ruling, case 2:
    https://github.com/OraclousAI/oraclous-backend/issues/1072#issuecomment-5701622081). The
    scheduler may dispatch "a" and the timeout role concurrently (they are siblings, not
    dependents) — the timeout role's ``execute()`` blocks on ``booked_event`` until "a"'s own
    ``execute()`` has returned, so "a" always books its spend BEFORE the timeout role's cancel is
    charged. Without this gate a concurrent scheduler could charge the timeout role against the
    pool's FULL headroom (never having seen "a"'s booking yet), making the expected charged amount
    meaningless.
    """

    def __init__(self, *, booked_role: str, booked_tokens: int, timeout_role: str) -> None:
        self.execute_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self._booked_role = booked_role
        self._booked_tokens = booked_tokens
        self._timeout_role = timeout_role
        self.booked_event = asyncio.Event()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.execute_calls.append(kwargs)
        if kwargs.get("manifest_ref") == f"org:x/{self._timeout_role}@1":
            await asyncio.wait_for(self.booked_event.wait(), timeout=5.0)
            raise HarnessTimeout("harness call timed out: exceeded its wall-clock time limit")
        result = {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": "ran",
            "total_tokens": self._booked_tokens,
        }
        self.booked_event.set()  # "a" has booked — safe now for the timeout role to be charged
        return result

    async def cancel(self, execution_id: uuid.UUID, **kwargs: Any) -> dict[str, Any] | None:
        self.cancel_calls.append({"execution_id": execution_id, **kwargs})
        return None  # 202: still winding down, the cancel never confirms


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


async def test_unconfirmed_cancel_with_pool_only_charges_remaining_headroom() -> None:
    """Owner ruling (#1072, case 2:
    https://github.com/OraclousAI/oraclous-backend/issues/1072#issuecomment-5701622081): the
    earlier "no member cap means no pool" premise was false — a team with ONLY
    ``budget.max_tokens_total`` (no per-member cap, no member override) resolves every member's cap
    to ``(None, None)`` via ``resolve_member_caps``, yet the pool is still live. An unconfirmed
    cancel (here a 202 -> None) then charges the pool's REMAINING HEADROOM
    (``max_tokens_total`` minus what is already booked), exhausting the pool so no later stage is
    dispatched — the fail-closed direction, since the orphan's real spend cannot be measured. An
    independent, earlier member ("a") succeeds first and books its own tokens; "b" depends on "a"
    (which succeeded), never on the timed-out member, so only the pool — never a
    blocked-by-upstream-failure path — can be what stops it. "a" and "t" are siblings the
    scheduler may dispatch concurrently, so the two on_cost calls can land in either order — the
    ORDER is not asserted, only the two amounts. The 700 figure stays meaningful (not a race
    artifact) because the fake harness gates "t"'s execute() on an event "a" sets only once it has
    already booked (see ``_EarlyBookThenUnconfirmedCancelHarness``), so "t" is always charged
    AFTER "a"'s 300 has landed, regardless of which on_cost call the scheduler runs first."""
    costs: list[int] = []
    team = _team(
        [
            OHMMember(role="a", kind="agent", manifest_ref="org:x/a@1"),
            OHMMember(role="t", kind="agent", manifest_ref="org:x/t@1"),
            OHMMember(role="b", kind="agent", manifest_ref="org:x/b@1", depends_on=["a"]),
        ],
        budget=OHMBudget(max_tokens_total=1_000),
    )
    harness = _EarlyBookThenUnconfirmedCancelHarness(
        booked_role="a", booked_tokens=300, timeout_role="t"
    )
    result = await run_team_harness(team, harness, on_cost=costs.append)
    assert len(harness.cancel_calls) == 1  # "t"'s timeout attempted exactly one cancel
    # "a"'s real spend (300) and "t"'s charged headroom (1_000 - 300 = 700, exhausting the pool),
    # in either order — see the docstring for why 700 is still meaningful under concurrency.
    assert sorted(costs) == [300, 700]
    assert result.member_status["b"] == "budget_skipped"  # the pool stopped it, not a block


async def test_unconfirmed_cancel_never_charges_negative_when_pool_already_over_budget() -> None:
    """#1072 review finding C3 (PR #1094, qa-engineer): ``_Pool.remaining_tokens()`` must clamp to
    ``max(0, max_tokens - spent)`` so an unconfirmed cancel's fail-closed pool charge is NEVER
    negative — a negative charge would LOWER the pool's recorded spend below what is already
    booked, undoing the exhaustion the pool exists to enforce. Here "a" alone books MORE than the
    whole pooled ceiling (1_500 against a 1_000 ``max_tokens_total``), so by the time "t" times out
    the pool is already over budget; its unconfirmed cancel (202 -> None) must still charge exactly
    0, never a negative number that would claw the recorded spend back down. "b" depends on "a"
    only (never on "t"), so the pool — never a blocked-by-upstream-failure path — is what gates it,
    proving the pool really did stay exhausted rather than being clawed back under its ceiling."""
    costs: list[int] = []
    team = _team(
        [
            OHMMember(role="a", kind="agent", manifest_ref="org:x/a@1"),
            OHMMember(role="t", kind="agent", manifest_ref="org:x/t@1"),
            OHMMember(role="b", kind="agent", manifest_ref="org:x/b@1", depends_on=["a"]),
        ],
        budget=OHMBudget(max_tokens_total=1_000),
    )
    harness = _EarlyBookThenUnconfirmedCancelHarness(
        booked_role="a", booked_tokens=1_500, timeout_role="t"
    )
    result = await run_team_harness(team, harness, on_cost=costs.append)
    assert len(harness.cancel_calls) == 1  # "t"'s timeout attempted exactly one cancel
    # "a"'s real spend (1_500, already over the 1_000 ceiling) and "t"'s charge clamped to 0 — never
    # a negative number (which would read as -500, clawing the recorded spend back down to 1_000).
    assert sorted(costs) == [0, 1_500]
    assert sum(costs) == 1_500  # recorded pool spend never DECREASES from what "a" already booked
    assert result.member_status["b"] == "budget_skipped"  # the pool stayed exhausted, not clawed


async def test_unconfirmed_cancel_without_any_token_ceiling_charges_nothing() -> None:
    """Owner ruling (#1072, case 3:
    https://github.com/OraclousAI/oraclous-backend/issues/1072#issuecomment-5701622081): when the
    member has NO resolved cap at all AND the team has no pooled ``max_tokens_total`` either, there
    is no ceiling to protect, so an unconfirmed cancel (here a 202 -> None) charges nothing:
    on_cost is never called (not even on_cost(None), which would crash any real caller expecting an
    int), and the #1067 timeout message is still raised unchanged."""
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
