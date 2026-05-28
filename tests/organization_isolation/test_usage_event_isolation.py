"""Organisation isolation for the ADR-009 usage-event stream (ORA-21, story C1).

AC4: org A cannot read org B's usage events. This pins the *seam* contract —
the stream's read is implicitly scoped to the ambient organisation-context
(0f / ORA-14): there is no caller-supplied ``organisation_id`` argument, so a
principal can only ever read their own organisation's events. The data-layer
row-level-security backstop (T1-M3) is A1 / ORA-16's concern and is proven
separately against real Postgres in ``test_two_org_substrate.py``; this asserts
the API can never be *asked* for another organisation's data in the first place.

RED until backend-implementer creates ``oraclous_substrate.usage``.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from oraclous_governance import (
    MissingOrganisationContextError,
    OrganisationContext,
    PrincipalType,
    use_organisation_context,
)
from oraclous_substrate.usage import UsageEvent, UsageEventStream

pytestmark = [pytest.mark.unit, pytest.mark.organization_isolation]


class _RecordingStore:
    """Organisation-scoped in-memory store double (see test_usage_event_stream)."""

    def __init__(self) -> None:
        self.writes: list[UsageEvent] = []

    async def write(self, event: UsageEvent) -> None:
        self.writes.append(event)

    async def read(self, organisation_id: uuid.UUID) -> list[UsageEvent]:
        return [e for e in self.writes if str(e.organisation_id) == str(organisation_id)]


def _context(organisation_id: uuid.UUID) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=organisation_id,
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


async def _emit(stream: UsageEventStream, action_type: str) -> None:
    await stream.emit(
        action_type=action_type,
        quantity=1,
        unit="count",
        dimensions={"action": action_type},
    )


async def test_read_returns_only_the_current_organisations_events() -> None:
    """A read under org A's context never returns org B's usage events."""
    store = _RecordingStore()
    stream = UsageEventStream(store)
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()

    with use_organisation_context(_context(org_a)):
        await _emit(stream, "capability.invoke")
        await _emit(stream, "model.tokens")
    with use_organisation_context(_context(org_b)):
        await _emit(stream, "storage.write")

    with use_organisation_context(_context(org_a)):
        a_events = await stream.read()
    with use_organisation_context(_context(org_b)):
        b_events = await stream.read()

    assert len(a_events) == 2
    assert all(str(e.organisation_id) == str(org_a) for e in a_events)
    assert len(b_events) == 1
    assert all(str(e.organisation_id) == str(org_b) for e in b_events)
    # The defining isolation property: no cross-organisation leakage either way.
    assert not any(str(e.organisation_id) == str(org_b) for e in a_events)
    assert not any(str(e.organisation_id) == str(org_a) for e in b_events)


async def test_read_without_bound_context_fails_closed() -> None:
    """With no organisation context bound, a read halts rather than returning
    every organisation's events.
    """
    store = _RecordingStore()
    stream = UsageEventStream(store)
    with pytest.raises(MissingOrganisationContextError):
        await stream.read()


async def test_read_takes_no_caller_supplied_organisation() -> None:
    """Structural guard: read scopes to the ambient context, so it exposes no
    parameter through which a caller could request another organisation's data.
    """
    params = [
        name for name in inspect.signature(UsageEventStream.read).parameters if name != "self"
    ]
    assert params == []
