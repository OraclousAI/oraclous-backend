"""Cross-organisation isolation for the ADR-009 usage-aggregation primitive
(story C3).

AC4 of the brief: "Tests on the 0d harness prove an org A admin cannot
aggregate org B." This pins the seam contract — the aggregator's read is
implicitly scoped to the ambient organisation-context (0f), and the
ReBAC gate is checked against the **bound** organisation's resource only. So:

* Even with both organisations' events sharing a single store, an aggregate
  under org A's context returns only org A's totals — never any of org B's
  events. (The store double already org-scopes by ``organisation_id`` the same
  way the C1 isolation test's double does, so a leak here would have to come
  from the aggregator asking the wrong scope.)
* An org-A admin who happens to also belong to org B but is **not** an org-B
  admin cannot aggregate B by re-pointing their bound context — the ReBAC gate
  is checked against the bound org's resource, so binding a non-admin context
  is a fail-closed denial.

Mirrors the pattern in ``test_usage_event_isolation.py`` (the C1 cross-org
isolation suite): an in-memory store double that filters by ``organisation_id``,
two distinct org contexts, and an assertion that no event with the *other*
organisation's id appears in the local aggregate.

Per the TDD-window guardrail, the not-yet-built
``oraclous_substrate.aggregation`` seam is imported function-locally so
collection stays clean. RED until backend-implementer creates it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from oraclous_governance import (
    OrganisationContext,
    PrincipalType,
    use_organisation_context,
)
from oraclous_substrate.rebac import AccessDecisionClient, AccessRequest
from oraclous_substrate.usage import UsageEvent, UsageEventStream

pytestmark = [pytest.mark.unit, pytest.mark.organization_isolation]


class _RecordingStore:
    """Organisation-scoped in-memory store double (same shape as C1)."""

    def __init__(self) -> None:
        self.writes: list[UsageEvent] = []

    async def write(self, event: UsageEvent) -> None:
        self.writes.append(event)

    async def read(self, organisation_id: uuid.UUID) -> list[UsageEvent]:
        return [e for e in self.writes if str(e.organisation_id) == str(organisation_id)]


class _AdminOnlyResolver:
    """Resolves the org-admin relation only when the request names an
    organisation in the ``admin_of`` set — every other request denies.

    Records every check so the test can assert which orgs were checked.
    """

    def __init__(self, *, admin_of: set[str]) -> None:
        self._admin_of = admin_of
        self.calls: list[AccessRequest] = []

    async def resolve(self, request: AccessRequest) -> bool | None:
        self.calls.append(request)
        return str(request.organisation_id) in self._admin_of


def _context(organisation_id: uuid.UUID, principal_id: uuid.UUID) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=organisation_id,
        principal_id=principal_id,
        principal_type=PrincipalType.USER,
    )


async def test_aggregator_returns_only_the_bound_organisations_events() -> None:
    """Two organisations share a single store. Under org A's admin context the
    aggregator's totals reflect A's events only — never B's."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    stream = UsageEventStream(store)
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    admin_a = uuid.uuid4()
    admin_b = uuid.uuid4()
    resolver = _AdminOnlyResolver(admin_of={str(org_a), str(org_b)})
    access = AccessDecisionClient(resolver=resolver)
    aggregator = UsageAggregator(store=store, access=access)

    # Org A emits two token events; org B emits one storage event.
    with use_organisation_context(_context(org_a, admin_a)):
        await stream.emit(
            action_type="model.tokens",
            quantity=100,
            unit="tokens",
            dimensions={"model": "claude-x"},
        )
        await stream.emit(
            action_type="model.tokens",
            quantity=50,
            unit="tokens",
            dimensions={"model": "claude-x"},
        )
    with use_organisation_context(_context(org_b, admin_b)):
        await stream.emit(
            action_type="storage.write",
            quantity=9999,
            unit="bytes",
            dimensions={"workspace_id": "wb"},
        )

    now = datetime.now(UTC)
    with use_organisation_context(_context(org_a, admin_a)):
        a_result = await aggregator.aggregate(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

    # Org A's totals reflect only org A's emits.
    assert a_result.total_events == 2
    assert dict(a_result.totals_by_unit) == {"tokens": 150}
    assert str(a_result.organisation_id) == str(org_a)

    # None of org B's distinctive quantities leaks across.
    assert 9999 not in a_result.totals_by_unit.values()
    assert "bytes" not in a_result.totals_by_unit
    assert "storage.write" not in a_result.totals_by_action_type


async def test_org_admin_cannot_aggregate_a_different_org_by_rebinding_context() -> None:
    """An org-A admin who happens to also belong to org B (without admin rights
    there) cannot aggregate org B by binding org B's context — the ReBAC gate
    is checked against the *bound* organisation's resource, so a non-admin
    binding fails closed."""
    from oraclous_substrate.aggregation import (
        UsageAggregationDenied,
        UsageAggregator,
    )

    store = _RecordingStore()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    # The principal is admin of org A only — never of org B.
    principal = uuid.uuid4()
    resolver = _AdminOnlyResolver(admin_of={str(org_a)})
    access = AccessDecisionClient(resolver=resolver)
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    # Re-binding the context to org B does not make the principal an admin of B.
    with use_organisation_context(_context(org_b, principal)):
        with pytest.raises(UsageAggregationDenied):
            await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))

    # The denied attempt did check ReBAC against org B's resource (not org A's).
    assert any(str(c.organisation_id) == str(org_b) for c in resolver.calls), (
        "the ReBAC gate must be evaluated against the bound organisation, "
        "not whichever organisation the principal happens to be admin of"
    )
    assert not any(str(c.organisation_id) == str(org_a) for c in resolver.calls), (
        "rebinding to org B must not implicitly reuse org A's admin grant"
    )


async def test_concurrent_two_org_aggregates_do_not_cross_contaminate() -> None:
    """Two interleaved aggregate calls under two organisations produce two
    isolated results — each one names its own organisation, sums its own
    events, and excludes the other organisation's emits entirely."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    stream = UsageEventStream(store)
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    admin_a, admin_b = uuid.uuid4(), uuid.uuid4()
    resolver = _AdminOnlyResolver(admin_of={str(org_a), str(org_b)})
    access = AccessDecisionClient(resolver=resolver)
    aggregator = UsageAggregator(store=store, access=access)

    # Emit a distinctive pattern per org so a leak is unmistakable.
    with use_organisation_context(_context(org_a, admin_a)):
        await stream.emit(
            action_type="model.tokens",
            quantity=11,
            unit="tokens",
            dimensions={"side": "a"},
        )
    with use_organisation_context(_context(org_b, admin_b)):
        await stream.emit(
            action_type="capability.invocation",
            quantity=1,
            unit="count",
            dimensions={"side": "b"},
        )
        await stream.emit(
            action_type="capability.invocation",
            quantity=1,
            unit="count",
            dimensions={"side": "b"},
        )

    now = datetime.now(UTC)
    with use_organisation_context(_context(org_a, admin_a)):
        a_result = await aggregator.aggregate(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )
    with use_organisation_context(_context(org_b, admin_b)):
        b_result = await aggregator.aggregate(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

    assert str(a_result.organisation_id) == str(org_a)
    assert str(b_result.organisation_id) == str(org_b)
    # A sees only its tokens; never the invocation count from B.
    assert dict(a_result.totals_by_unit) == {"tokens": 11}
    assert "count" not in a_result.totals_by_unit
    # B sees only its invocations; never the token total from A.
    assert dict(b_result.totals_by_unit) == {"count": 2}
    assert "tokens" not in b_result.totals_by_unit
