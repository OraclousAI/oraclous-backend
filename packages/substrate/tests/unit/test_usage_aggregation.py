"""Failing tests for the substrate usage-aggregation primitive (ORA-23, story C3).

C1 (ORA-21) built the append-only usage-event stream
(``oraclous_substrate.usage.UsageEventStream``). C2 (ORA-22) wired emission into
the metered actions. C3 sits one layer up: a substrate-level **query primitive**
that returns per-organisation aggregates over a configurable time range, gated
by a ReBAC org-admin relation. Lift-tag is **Greenfield** — no legacy aggregation
to lift from.

These tests pin the ADR-009 / threat-catalogue contracts the brief tags:

* **AC1 — correct per-organisation totals over a time range**, with reads
  org-scoped (T1-M1). The aggregator takes ``organisation_id`` only from the
  ambient organisation-context (0f / ORA-14), never from a caller argument, so
  there is no body-supplied channel to smuggle a tenant scope through.
* **AC2 — ReBAC-gated to an org-admin relation** (T2 / ADR-004); a non-admin is
  denied, and "denied" means *fail-closed*: an absent relation, an ambiguous
  resolution, and a resolver error all deny.
* **AC3 — no HTTP route is added** — the substrate primitive stays at Layer 1;
  the HTTP endpoint is deferred to the R6 application gateway (ADR-009; the
  R0.5 release page's "Scope → Out of scope" line).
* **AC4 — cross-organisation isolation** is pinned in
  ``tests/organization_isolation/test_usage_aggregation_isolation.py``.

ADR-009 rules-of-engagement (settled by ORA-22 rulings 10133/10167 and pinned by
C2's suite) carry forward here: the aggregator returns the **raw** signal
(tokens, count, bytes) and never a priced/rated unit (no USD, no credits).
Billing is the downstream consumer.

Per the ORA-48 TDD-window guardrail, intra-repo seams that do not yet exist on
the tree are imported **function-locally** so module-level collection does not
abort. The already-built seams (``oraclous_governance``,
``oraclous_substrate.usage``, ``oraclous_substrate.rebac``) are imported at
module level.

RED until backend-implementer creates ``oraclous_substrate.aggregation``.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from oraclous_governance import (
    MissingOrganisationContextError,
    OrganisationContext,
    PrincipalType,
    use_organisation_context,
)
from oraclous_substrate.rebac import AccessDecisionClient, AccessRequest
from oraclous_substrate.usage import UsageEvent, UsageEventStream

pytestmark = [pytest.mark.unit]


# --------------------------------------------------------------------------- #
# Test doubles — minimal in-memory implementations of the injected dependencies.
# --------------------------------------------------------------------------- #


class _RecordingStore:
    """In-memory ``UsageEventStore`` that org-scopes reads (same shape as the
    one used by C1 and C2 tests). The aggregator must rely on the store to scope
    by ``organisation_id`` rather than scanning everything in-process."""

    def __init__(self) -> None:
        self.writes: list[UsageEvent] = []

    async def write(self, event: UsageEvent) -> None:
        self.writes.append(event)

    async def read(self, organisation_id: uuid.UUID) -> list[UsageEvent]:
        return [e for e in self.writes if str(e.organisation_id) == str(organisation_id)]


class _Resolver:
    """ReBAC relation resolver double — records every check and returns a
    canned result (``True`` / ``False`` / ``None``) or raises.

    Mirrors the resolver used by 0g's ``test_rebac_client.py`` so the aggregator
    composes over the same fail-closed seam.
    """

    def __init__(
        self,
        *,
        result: bool | None = True,
        raises: Exception | None = None,
    ) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[AccessRequest] = []

    async def resolve(self, request: AccessRequest) -> bool | None:
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        return self._result


def _context(organisation_id: uuid.UUID | None = None) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=organisation_id or uuid.uuid4(),
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


async def _emit_event(
    stream: UsageEventStream,
    *,
    action_type: str,
    quantity: float,
    unit: str,
    dimensions: dict | None = None,
) -> None:
    await stream.emit(
        action_type=action_type,
        quantity=quantity,
        unit=unit,
        dimensions=dimensions or {"label": action_type},
    )


# --------------------------------------------------------------------------- #
# AC1 — correct per-organisation totals over a configurable time range
# --------------------------------------------------------------------------- #


@pytest.mark.audit
async def test_aggregate_returns_total_event_count_for_window() -> None:
    """The aggregate's event count equals the number of events the current
    organisation emitted within the window."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    stream = UsageEventStream(store)
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    ctx = _context()
    with use_organisation_context(ctx):
        await _emit_event(stream, action_type="model.tokens", quantity=120, unit="tokens")
        await _emit_event(stream, action_type="capability.invocation", quantity=1, unit="count")
        await _emit_event(stream, action_type="storage.write", quantity=2048, unit="bytes")
        result = await aggregator.aggregate(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

    assert result.total_events == 3


@pytest.mark.audit
async def test_aggregate_groups_totals_by_unit() -> None:
    """The aggregate sums quantity grouped by unit — the raw metering vocabulary
    (tokens / count / bytes) per ADR-009 §4; never a priced/rated unit.
    """
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    stream = UsageEventStream(store)
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with use_organisation_context(_context()):
        # Two token-emitting events, two count-emitting, one bytes-emitting.
        await _emit_event(stream, action_type="model.tokens", quantity=100, unit="tokens")
        await _emit_event(stream, action_type="model.tokens", quantity=50, unit="tokens")
        await _emit_event(stream, action_type="capability.invocation", quantity=1, unit="count")
        await _emit_event(stream, action_type="substrate.traversal", quantity=2, unit="count")
        await _emit_event(stream, action_type="storage.write", quantity=4096, unit="bytes")
        result = await aggregator.aggregate(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

    assert dict(result.totals_by_unit) == {"tokens": 150, "count": 3, "bytes": 4096}


@pytest.mark.audit
async def test_aggregate_groups_totals_by_action_type() -> None:
    """The aggregate also breaks down totals by ``action_type``, the per-
    metered-action decomposition the brief names (token/invocation/storage/
    traversal)."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    stream = UsageEventStream(store)
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with use_organisation_context(_context()):
        await _emit_event(stream, action_type="model.tokens", quantity=100, unit="tokens")
        await _emit_event(stream, action_type="model.tokens", quantity=50, unit="tokens")
        await _emit_event(stream, action_type="capability.invocation", quantity=1, unit="count")
        await _emit_event(stream, action_type="storage.write", quantity=2048, unit="bytes")
        result = await aggregator.aggregate(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

    assert dict(result.totals_by_action_type) == {
        "model.tokens": 150,
        "capability.invocation": 1,
        "storage.write": 2048,
    }


@pytest.mark.audit
async def test_aggregate_excludes_events_strictly_before_window_start() -> None:
    """Events emitted before the requested ``start`` are not in the totals."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    stream = UsageEventStream(store)
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    ctx = _context()
    with use_organisation_context(ctx):
        await _emit_event(stream, action_type="model.tokens", quantity=999, unit="tokens")
        # Open the window strictly *after* what's been emitted so far.
        start = datetime.now(UTC) + timedelta(minutes=10)
        end = start + timedelta(hours=1)
        result = await aggregator.aggregate(start=start, end=end)

    assert result.total_events == 0
    assert dict(result.totals_by_unit) == {}


@pytest.mark.audit
async def test_aggregate_excludes_events_strictly_after_window_end() -> None:
    """Events emitted after the requested ``end`` are not in the totals."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    stream = UsageEventStream(store)
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    ctx = _context()
    # Open and close the window in the past so today's emits land after it.
    start = datetime.now(UTC) - timedelta(hours=2)
    end = datetime.now(UTC) - timedelta(hours=1)
    with use_organisation_context(ctx):
        await _emit_event(stream, action_type="storage.write", quantity=1024, unit="bytes")
        result = await aggregator.aggregate(start=start, end=end)

    assert result.total_events == 0
    assert dict(result.totals_by_unit) == {}


@pytest.mark.audit
async def test_aggregate_with_no_events_returns_empty_totals() -> None:
    """Aggregation of an empty stream returns zero/empty totals rather than
    raising — an empty window is a legitimate result, not a failure."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with use_organisation_context(_context()):
        result = await aggregator.aggregate(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

    assert result.total_events == 0
    assert dict(result.totals_by_unit) == {}
    assert dict(result.totals_by_action_type) == {}


@pytest.mark.audit
async def test_aggregate_window_bounds_are_reported_on_the_result() -> None:
    """The returned aggregate carries the window it was computed against, so a
    caller can never confuse two windows' results."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    start = datetime(2026, 5, 1, tzinfo=UTC)
    end = datetime(2026, 5, 31, tzinfo=UTC)
    with use_organisation_context(_context()):
        result = await aggregator.aggregate(start=start, end=end)

    assert result.window_start == start
    assert result.window_end == end


@pytest.mark.audit
async def test_aggregate_rejects_inverted_window() -> None:
    """An ``end`` strictly before ``start`` is a programming error, not an
    empty window — refuse fail-closed rather than silently return zero, which
    would mask a query bug."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    start = datetime(2026, 5, 31, tzinfo=UTC)
    end = datetime(2026, 5, 1, tzinfo=UTC)
    with use_organisation_context(_context()), pytest.raises(ValueError):
        await aggregator.aggregate(start=start, end=end)


# --------------------------------------------------------------------------- #
# AC1 (continued) — identity from the bound context, never a caller argument
# (T1-M1; mirrors C1's emit-side guarantee on the read side)
# --------------------------------------------------------------------------- #


@pytest.mark.audit
async def test_aggregate_takes_no_caller_supplied_organisation_id() -> None:
    """Structural guard: ``aggregate`` exposes no parameter through which a
    caller could request another organisation's aggregate. The org scope is
    taken from the ambient organisation-context only (T1-M1)."""
    from oraclous_substrate.aggregation import UsageAggregator

    params = list(inspect.signature(UsageAggregator.aggregate).parameters)
    assert "organisation_id" not in params
    assert "org_id" not in params
    # And confirm the legitimate kwargs are there.
    assert {"start", "end"}.issubset(set(params))


@pytest.mark.security
async def test_aggregate_without_bound_context_fails_closed() -> None:
    """T1-M1 fail-closed: with no organisation-context bound, aggregation must
    halt rather than default or read an attacker-influenced value."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with pytest.raises(MissingOrganisationContextError):
        await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))


@pytest.mark.audit
async def test_aggregate_result_carries_bound_organisation_id() -> None:
    """The returned aggregate names the organisation it was computed for, so a
    log/audit record cannot be silently re-attributed."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    org = uuid.uuid4()
    now = datetime.now(UTC)
    with use_organisation_context(_context(org)):
        result = await aggregator.aggregate(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

    assert str(result.organisation_id) == str(org)


# --------------------------------------------------------------------------- #
# AC2 — ReBAC-gated to an org-admin relation, fail-closed on deny
# --------------------------------------------------------------------------- #


@pytest.mark.security
@pytest.mark.rebac
async def test_aggregate_calls_rebac_check_against_org_resource() -> None:
    """The aggregator gates each call on a ReBAC check whose ``resource`` names
    the **current organisation** and whose ``subject``/``organisation_id`` come
    from the bound context — never from a caller argument."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    resolver = _Resolver(result=True)
    access = AccessDecisionClient(resolver=resolver)
    aggregator = UsageAggregator(store=store, access=access)

    ctx = _context()
    now = datetime.now(UTC)
    with use_organisation_context(ctx):
        await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))

    assert len(resolver.calls) >= 1
    request = resolver.calls[0]
    assert str(request.organisation_id) == str(ctx.organisation_id)
    assert str(request.subject) == str(ctx.principal_id)
    # The resource names the org being aggregated — not a generic global handle.
    assert str(ctx.organisation_id) in request.resource
    # A non-empty relation is checked; the exact spelling is the implementer's
    # choice but a single canonical relation must be used (see consistency test).
    assert request.relation
    assert isinstance(request.relation, str)


@pytest.mark.security
@pytest.mark.rebac
async def test_aggregate_uses_a_single_canonical_admin_relation() -> None:
    """Across distinct contexts the aggregator checks the **same** relation —
    so the org-admin gate is a fixed contract a security review can pin, not a
    per-call freelance string."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    resolver = _Resolver(result=True)
    access = AccessDecisionClient(resolver=resolver)
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with use_organisation_context(_context()):
        await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))
    with use_organisation_context(_context()):
        await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))

    relations = {call.relation for call in resolver.calls}
    assert len(relations) == 1, f"aggregator must use one canonical relation; saw {relations}"


@pytest.mark.security
@pytest.mark.rebac
async def test_aggregate_denied_when_relation_absent() -> None:
    """AC2: a non-admin principal cannot aggregate — the ReBAC seam returns a
    DENY decision and the aggregator refuses rather than returning data."""
    from oraclous_substrate.aggregation import (
        UsageAggregationDenied,
        UsageAggregator,
    )

    store = _RecordingStore()
    access = AccessDecisionClient(resolver=_Resolver(result=False))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with use_organisation_context(_context()), pytest.raises(UsageAggregationDenied):
        await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))


@pytest.mark.security
@pytest.mark.rebac
async def test_aggregate_denied_on_ambiguous_resolution() -> None:
    """T1-M2 fail-closed: an ambiguous/indeterminate ReBAC resolution denies."""
    from oraclous_substrate.aggregation import (
        UsageAggregationDenied,
        UsageAggregator,
    )

    store = _RecordingStore()
    access = AccessDecisionClient(resolver=_Resolver(result=None))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with use_organisation_context(_context()), pytest.raises(UsageAggregationDenied):
        await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))


@pytest.mark.security
@pytest.mark.rebac
async def test_aggregate_denied_on_resolver_error() -> None:
    """A ReBAC resolver error denies — never propagates as a fail-open allow.

    The aggregator composes over the substrate ``AccessDecisionClient``, whose
    own contract already turns a resolver error into a typed DENY; this asserts
    the aggregator honours that DENY rather than swallowing it / treating it as
    "we couldn't check, so let it through".
    """
    from oraclous_substrate.aggregation import (
        UsageAggregationDenied,
        UsageAggregator,
    )

    store = _RecordingStore()
    access = AccessDecisionClient(resolver=_Resolver(raises=RuntimeError("store down")))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with use_organisation_context(_context()), pytest.raises(UsageAggregationDenied):
        await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))


@pytest.mark.security
@pytest.mark.rebac
async def test_aggregate_denial_does_not_leak_event_totals() -> None:
    """A denied aggregate raises *before* any totals reach the caller — the
    exception must not carry per-action-type sums (which would let a non-admin
    learn the org's usage shape via an error message)."""
    from oraclous_substrate.aggregation import (
        UsageAggregationDenied,
        UsageAggregator,
    )

    store = _RecordingStore()
    stream = UsageEventStream(store)
    access = AccessDecisionClient(resolver=_Resolver(result=False))
    aggregator = UsageAggregator(store=store, access=access)

    ctx = _context()
    now = datetime.now(UTC)
    with use_organisation_context(ctx):
        # Pre-populate the org's stream with some real activity.
        await _emit_event(stream, action_type="model.tokens", quantity=12345, unit="tokens")
        await _emit_event(stream, action_type="storage.write", quantity=98765, unit="bytes")
        with pytest.raises(UsageAggregationDenied) as excinfo:
            await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))

    message = str(excinfo.value)
    assert "12345" not in message
    assert "98765" not in message


@pytest.mark.security
@pytest.mark.rebac
async def test_aggregate_denial_skips_the_store_read() -> None:
    """Fail-closed sequencing: the ReBAC check runs *before* the event read, so
    a denied aggregate never touches the store. Otherwise a side-effecting
    store could leak through an exception path."""
    from oraclous_substrate.aggregation import (
        UsageAggregationDenied,
        UsageAggregator,
    )

    class _CountingStore(_RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.read_calls: list[uuid.UUID] = []

        async def read(self, organisation_id: uuid.UUID) -> list[UsageEvent]:
            self.read_calls.append(organisation_id)
            return await super().read(organisation_id)

    store = _CountingStore()
    access = AccessDecisionClient(resolver=_Resolver(result=False))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with use_organisation_context(_context()), pytest.raises(UsageAggregationDenied):
        await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))

    assert store.read_calls == [], "store must not be read when access is denied"


# --------------------------------------------------------------------------- #
# AC2 (continued) — only the current org's events feed the aggregate
# (single-process pin; the harness-tier cross-org isolation lives in
# tests/organization_isolation/test_usage_aggregation_isolation.py)
# --------------------------------------------------------------------------- #


@pytest.mark.security
@pytest.mark.organization_isolation
async def test_aggregate_reads_only_the_bound_organisation_from_the_store() -> None:
    """The aggregator asks the store for the bound organisation's events only
    — never every organisation's events to filter in-process — so even a
    store-side leak is bounded by the ask."""
    from oraclous_substrate.aggregation import UsageAggregator

    asked_for: list[uuid.UUID] = []

    class _ScopedStore(_RecordingStore):
        async def read(self, organisation_id: uuid.UUID) -> list[UsageEvent]:
            asked_for.append(organisation_id)
            return await super().read(organisation_id)

    store = _ScopedStore()
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    org_a = uuid.uuid4()
    now = datetime.now(UTC)
    with use_organisation_context(_context(org_a)):
        await aggregator.aggregate(start=now - timedelta(hours=1), end=now + timedelta(hours=1))

    assert asked_for == [org_a], (
        "aggregator must scope the store read to the bound org, not pull cross-org"
    )


# --------------------------------------------------------------------------- #
# ADR-009 — the aggregator stays in the raw cost-driving vocabulary
# (no priced/rated unit; carried forward from C2 rulings 10133/10167)
# --------------------------------------------------------------------------- #


@pytest.mark.security
@pytest.mark.operator_separation
async def test_aggregate_does_not_emit_priced_or_rated_totals() -> None:
    """ADR-009: the substrate emits/aggregates the raw cost-driving signal only.
    USD and *credits* are both rated/priced and live in the downstream rater —
    the aggregate's ``totals_by_unit`` keys must not include either."""
    from oraclous_substrate.aggregation import UsageAggregator

    store = _RecordingStore()
    stream = UsageEventStream(store)
    access = AccessDecisionClient(resolver=_Resolver(result=True))
    aggregator = UsageAggregator(store=store, access=access)

    now = datetime.now(UTC)
    with use_organisation_context(_context()):
        await _emit_event(stream, action_type="model.tokens", quantity=100, unit="tokens")
        await _emit_event(stream, action_type="capability.invocation", quantity=1, unit="count")
        result = await aggregator.aggregate(
            start=now - timedelta(hours=1), end=now + timedelta(hours=1)
        )

    rated_units = {"usd", "credits", "credit", "cost", "price"}
    seen_units = {u.lower() for u in result.totals_by_unit}
    assert seen_units.isdisjoint(rated_units), (
        f"aggregate must not surface priced/rated units; saw {seen_units & rated_units}"
    )


# --------------------------------------------------------------------------- #
# AC3 — no HTTP route is added (the endpoint is R6 / application gateway)
# --------------------------------------------------------------------------- #


@pytest.mark.security
def test_aggregation_module_does_not_depend_on_an_http_framework() -> None:
    """The Layer-1 aggregation primitive must not import any HTTP framework —
    that would couple it to the R6 gateway boundary that owns transport. The
    endpoint is deferred to the application gateway service (ADR-009; R0.5
    Scope → Out of scope)."""
    import oraclous_substrate.aggregation as aggregation

    source = inspect.getsource(aggregation)
    forbidden = ["fastapi", "starlette", "flask"]
    for needle in forbidden:
        assert needle not in source.lower(), (
            f"oraclous_substrate.aggregation must not depend on {needle!r} — "
            "the HTTP endpoint is R6, not R0.5 (ADR-009)"
        )


@pytest.mark.security
def test_aggregation_module_lives_in_layer_1_substrate() -> None:
    """The aggregator is a substrate primitive (Layer 1), not a service module
    (Layer 4). A misplaced module would let a service silently couple to it."""
    import oraclous_substrate.aggregation as aggregation

    assert aggregation.__name__.startswith("oraclous_substrate."), (
        "the aggregator must live under the substrate package (Layer 1)"
    )


@pytest.mark.security
def test_no_service_router_wires_the_aggregator() -> None:
    """No file under ``services/*/src`` references ``UsageAggregator`` — the
    R6 gateway endpoint is out of scope for R0.5, so wiring it up at any
    service today would be a process violation against ADR-009 §scope.

    Preconditioned on the aggregator module existing: that keeps this test RED
    before ``[impl]`` lands (so it cannot pass vacuously), and a permanent
    guardrail after — flagging any future PR that wires it into a service
    router before R6's gateway story.
    """
    from pathlib import Path

    # RED-before-impl precondition: importing here fails with ModuleNotFoundError
    # until the [impl] PR lands ``oraclous_substrate.aggregation``.
    from oraclous_substrate.aggregation import UsageAggregator  # noqa: F401

    repo_root = Path(__file__).resolve().parents[4]
    services_root = repo_root / "services"
    assert services_root.is_dir(), f"services root not found at {services_root}"

    offenders: list[str] = []
    for src_file in services_root.rglob("*.py"):
        text = src_file.read_text(encoding="utf-8")
        if "UsageAggregator" in text:
            offenders.append(str(src_file.relative_to(repo_root)))
    assert offenders == [], (
        "no service may wire the aggregator until R6 (the HTTP endpoint is "
        f"deferred to the application gateway); found references in: {offenders}"
    )
