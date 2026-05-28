"""Failing tests for the request-scoped organisation-context kernel (ORA-14, story 0f).

Scope: ``packages/governance`` — the organisation-context *object* and the
resolution that obtains ``organisation_id`` from the authenticated principal.
Propagation across async boundaries is pinned separately in
``test_context_propagation.py``.

Shape reference: Contract ORA-3 (ratified Option B, 28 May 2026) — the substrate
resolves ``organisation_id`` from organisation membership; the principal carries
identity only (``principal_id`` from the JWT ``sub``, ``principal_type`` from the
token). Active-organisation selection for multi-org principals is a *validated*
selection checked against membership, fail-closed — never trusted from the
request body. Legacy behavioural reference: ``auth-service`` service-account
token claims (``principal_type``) and ``knowledge-graph-builder``
``org_member_service`` (``BELONGS_TO`` membership; a principal may belong to
several organisations).

Invariants pinned: ADR-006 (organisation_id sourced from authenticated context,
never the body) and the fail-closed default (Structured Threat Catalogue T1-M1).

RED until backend-implementer creates ``oraclous_governance.context``.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from oraclous_governance.context import (
    MembershipResolver,
    OrganisationContext,
    OrganisationResolutionError,
    Principal,
    PrincipalType,
    resolve_organisation_context,
)

pytestmark = [pytest.mark.unit]


class _Membership:
    """Test double for the membership store.

    The real store lives in the substrate (ORA-15 / Epic A2); here it is injected
    via the ``MembershipResolver`` seam. ``organisations_for`` returns the
    organisations the principal BELONGS_TO.
    """

    def __init__(self, *orgs: uuid.UUID) -> None:
        self._orgs = list(orgs)
        self.calls: list[Principal] = []

    async def organisations_for(self, principal: Principal) -> list[uuid.UUID]:
        self.calls.append(principal)
        return list(self._orgs)


def _principal(principal_type: PrincipalType = PrincipalType.USER) -> Principal:
    return Principal(principal_id=uuid.uuid4(), principal_type=principal_type)


# ── the object ───────────────────────────────────────────────────────────────


def test_membership_resolver_is_the_injected_seam() -> None:
    """The membership store is reached through a named typed protocol, so the
    real substrate store can be swapped for a double here (ORA-15 / A2)."""
    assert hasattr(MembershipResolver, "organisations_for")


def test_context_carries_organisation_principal_and_type() -> None:
    """AC#1: the context carries organisation_id, principal id, principal type."""
    org = uuid.uuid4()
    pid = uuid.uuid4()
    ctx = OrganisationContext(
        organisation_id=org, principal_id=pid, principal_type=PrincipalType.USER
    )
    assert ctx.organisation_id == org
    assert ctx.principal_id == pid
    assert ctx.principal_type is PrincipalType.USER


def test_context_is_immutable() -> None:
    """A resolved context cannot be mutated to swap organisation after the fact —
    this closes an org-swap path (ADR-006)."""
    ctx = OrganisationContext(
        organisation_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.organisation_id = uuid.uuid4()  # type: ignore[misc]


# ── resolution: single-org principal ─────────────────────────────────────────


async def test_single_org_principal_resolves_implicitly() -> None:
    """A principal in exactly one organisation resolves to it with no selection,
    carrying its own identity through to the context."""
    org = uuid.uuid4()
    principal = _principal()
    ctx = await resolve_organisation_context(principal, resolver=_Membership(org))
    assert ctx.organisation_id == org
    assert ctx.principal_id == principal.principal_id
    assert ctx.principal_type is principal.principal_type


async def test_resolution_sources_org_only_from_membership() -> None:
    """ADR-006: organisation_id comes from the resolved membership, not from any
    caller-supplied value — membership is actually consulted on every resolve."""
    org = uuid.uuid4()
    resolver = _Membership(org)
    ctx = await resolve_organisation_context(_principal(), resolver=resolver)
    assert ctx.organisation_id == org
    assert len(resolver.calls) == 1


async def test_service_account_principal_type_preserved() -> None:
    """A service-account principal resolves a context tagged as such (legacy SA
    tokens carry ``principal_type=service_account``)."""
    org = uuid.uuid4()
    principal = _principal(PrincipalType.SERVICE_ACCOUNT)
    ctx = await resolve_organisation_context(principal, resolver=_Membership(org))
    assert ctx.principal_type is PrincipalType.SERVICE_ACCOUNT


# ── resolution: fail-closed ──────────────────────────────────────────────────


@pytest.mark.organization_isolation
async def test_no_membership_fails_closed() -> None:
    """T1-M1: a principal with no organisation membership is denied — never
    defaulted to some organisation."""
    with pytest.raises(OrganisationResolutionError):
        await resolve_organisation_context(_principal(), resolver=_Membership())


@pytest.mark.organization_isolation
async def test_multi_org_principal_without_selection_fails_closed() -> None:
    """An ambiguous (multi-org) principal with no active-org selection is denied —
    never silently assigned one of the organisations."""
    resolver = _Membership(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(OrganisationResolutionError):
        await resolve_organisation_context(_principal(), resolver=resolver)


# ── resolution: active-org selection (validated X-Organisation-Id) ────────────


async def test_multi_org_principal_with_member_selection_resolves() -> None:
    """A multi-org principal selecting an organisation it belongs to resolves to
    that organisation."""
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    resolver = _Membership(org_a, org_b)
    ctx = await resolve_organisation_context(
        _principal(), resolver=resolver, requested_organisation_id=org_b
    )
    assert ctx.organisation_id == org_b


@pytest.mark.organization_isolation
async def test_selection_of_non_member_org_fails_closed() -> None:
    """The active-org selection (the validated ``X-Organisation-Id`` header) is
    checked against membership: selecting an organisation the principal does NOT
    belong to is denied.

    This is the proof that organisation_id can never be smuggled in from the
    client — even an explicit selection must name a real membership
    (ADR-006, T1-M1).
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    outsider_org = uuid.uuid4()
    resolver = _Membership(org_a, org_b)
    with pytest.raises(OrganisationResolutionError):
        await resolve_organisation_context(
            _principal(), resolver=resolver, requested_organisation_id=outsider_org
        )


@pytest.mark.organization_isolation
async def test_single_org_principal_selecting_other_org_fails_closed() -> None:
    """Even a single-org principal cannot select a different organisation than the
    one it belongs to."""
    home = uuid.uuid4()
    other = uuid.uuid4()
    resolver = _Membership(home)
    with pytest.raises(OrganisationResolutionError):
        await resolve_organisation_context(
            _principal(), resolver=resolver, requested_organisation_id=other
        )
