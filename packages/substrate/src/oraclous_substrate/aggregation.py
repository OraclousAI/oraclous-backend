"""Substrate usage-aggregation primitive (Layer 1, ADR-009 / story C3).

A query primitive over the C1 usage-event stream (``oraclous_substrate.usage``)
that returns per-organisation aggregates — total event count, totals by unit,
totals by action_type — over a configurable closed time window. ReBAC-gated to
an org-admin relation on the bound organisation; fail-closed on absent /
ambiguous / errored authorisation and on a missing organisation context.

Layer-1 substrate primitive — no HTTP surface and no service router wires it.
The R6 application gateway is what will eventually expose an authenticated
HTTP endpoint over the platform. Until then the primitive is callable only
from substrate-internal paths (none today) and the R0.5 "no HTTP route"
acceptance criterion is held by both this module's lack of any HTTP-framework
import and by the test that scans ``services/`` for references.

Identity is sourced from the ambient organisation-context: the
public method exposes no caller-supplied ``organisation_id`` parameter, so
there is no body-supplied channel to smuggle a tenant scope through (T1-M1).

Fail-closed sequencing — the ReBAC check is evaluated *before* any event read,
so a denied caller never touches the events and cannot learn the
organisation's usage shape through the exception path (T2 / info-leak):

  1. ``current_organisation_context()`` — propagates ``MissingOrganisationContextError``
     when no context is bound; never defaulted.
  2. ``AccessDecisionClient.check`` — DENY on absent / ambiguous (``None``) /
     errored resolution (the seam's own contract, ADR-004 / T1-M2). Any
     non-allow becomes ``UsageAggregationDenied`` here.
  3. ``UsageEventStore.read`` — scoped to the bound organisation only.
  4. In-process windowing + grouping.

ADR-009 invariant carried forward from C1/C2 (rulings 10133/10167): the
substrate surfaces only the raw cost-driving signal vocabulary
(``tokens`` / ``count`` / ``bytes``). Priced and rated units (USD, credits)
are downstream rater concerns and never appear in the metering path; the
aggregator faithfully carries through whatever units C2 emitted, so the
guarantee holds compositionally with C2's own emit-side rejection.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from oraclous_governance import current_organisation_context

from oraclous_substrate.rebac import AccessDecisionClient, AccessRequest
from oraclous_substrate.usage import UsageEventStore

# Canonical ReBAC relation a principal must hold on its organisation to
# aggregate usage events. Single, fixed value pinned by the suite — the
# aggregator never freelances per-call relation names (T2 audit-pinnability).
ORG_ADMIN_RELATION = "organisation_admin"


class UsageAggregationDenied(Exception):
    """Raised when a usage-aggregation request fails the ReBAC org-admin gate.

    The message is intentionally opaque — never includes per-action totals,
    per-event quantities, or any event-shape signal. A denied caller must not
    be able to infer the organisation's usage from the denial path itself
    (T2 / information leak).
    """


@dataclass(frozen=True, slots=True)
class UsageAggregate:
    """Per-organisation usage totals over a closed time window [start, end].

    Frozen so a returned aggregate cannot be silently re-attributed or its
    totals mutated after the fact. ``totals_by_unit`` and
    ``totals_by_action_type`` are immutable mappings (``MappingProxyType``) for
    the same reason.
    """

    organisation_id: uuid.UUID
    window_start: datetime
    window_end: datetime
    total_events: int
    totals_by_unit: Mapping[str, float]
    totals_by_action_type: Mapping[str, float]


class UsageAggregator:
    """ReBAC-gated aggregation query over the C1 usage-event stream."""

    def __init__(self, *, store: UsageEventStore, access: AccessDecisionClient) -> None:
        self._store = store
        self._access = access

    async def aggregate(self, *, start: datetime, end: datetime) -> UsageAggregate:
        """Aggregate the current organisation's usage events over [start, end].

        The bound organisation-context provides both the ``organisation_id`` and
        the ``principal`` checked against the org-admin relation — neither is a
        caller argument. An inverted window (``end < start``) raises
        ``ValueError`` rather than returning an empty aggregate, so a query bug
        is loud instead of silent.
        """
        if end < start:
            raise ValueError("usage aggregation: end must not be before start")

        # T1-M1 fail-closed: a missing organisation-context propagates
        # ``MissingOrganisationContextError`` from the governance kernel
        # before any authorisation or store traffic happens.
        context = current_organisation_context()
        organisation_id = context.organisation_id

        decision = await self._access.check(
            AccessRequest(
                organisation_id=str(organisation_id),
                subject=str(context.principal_id),
                # The resource names this organisation; the canonical form is
                # ``organisation:<uuid>`` so callers / auditors can tell that
                # the gate is keyed on the org-as-resource and not on a generic
                # global handle.
                resource=f"organisation:{organisation_id}",
                relation=ORG_ADMIN_RELATION,
            )
        )
        if not decision.allowed:
            # Sequenced before the store read so a denied call never touches
            # the events. The message carries no totals (T2 info-leak).
            raise UsageAggregationDenied("usage aggregation denied")

        events = await self._store.read(organisation_id)

        totals_by_unit: dict[str, float] = defaultdict(float)
        totals_by_action_type: dict[str, float] = defaultdict(float)
        in_window = 0
        for event in events:
            if event.timestamp < start or event.timestamp > end:
                continue
            in_window += 1
            totals_by_unit[event.unit] += float(event.quantity)
            totals_by_action_type[event.action_type] += float(event.quantity)

        return UsageAggregate(
            organisation_id=organisation_id,
            window_start=start,
            window_end=end,
            total_events=in_window,
            # Freeze the result mappings so a downstream caller cannot mutate
            # an aggregate after it is returned.
            totals_by_unit=MappingProxyType(dict(totals_by_unit)),
            totals_by_action_type=MappingProxyType(dict(totals_by_action_type)),
        )
