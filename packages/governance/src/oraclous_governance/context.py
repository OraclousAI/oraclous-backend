"""Organisation-context object + resolution (Layer 1, ADR-006).

A resolved ``OrganisationContext`` carries the ``organisation_id`` that scopes
every substrate read/write, together with the authenticated principal's identity.
The ``organisation_id`` is sourced from the authenticated context only — either an
auth-issued organisation claim on the principal (R1 agent tokens, ORA-31) or a
membership lookup through the injected ``MembershipResolver`` — never from a
request body. The only client-influenced channel is a *validated* active-org
selection (the ``X-Organisation-Id`` header), and even that must name a real
membership.

Fail-closed (Structured Threat Catalogue T1-M1): no membership, an ambiguous
multi-org principal with no selection, and a selection naming a non-member
organisation all raise rather than defaulting to some organisation.

Shape reference: Contract ORA-3 (ratified Option B, 28 May 2026). Legacy
behavioural reference: ``auth-service`` service-account token claims
(``principal_type``) and ``knowledge-graph-builder`` ``org_member_service``
(``BELONGS_TO`` membership).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PrincipalType(StrEnum):
    """The kind of authenticated principal a context is resolved for."""

    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated principal: identity only.

    ``organisation_id`` is the optional auth-issued organisation claim (R1 agent
    tokens, ORA-31); ``None`` models the R0.5 identity-only principal whose
    organisation must be resolved against membership. ``org_role`` is the member's
    role in ``organisation_id`` (owner/admin/member), auth-issued as the ``org_role``
    JWT claim (R7-SEC S2); ``None`` for non-member principals (agent/service-account
    / a token minted before the claim existed) — those never satisfy an admin gate.
    """

    principal_id: uuid.UUID
    principal_type: PrincipalType
    organisation_id: uuid.UUID | None = None
    org_role: str | None = None


# --- Org-role rank for the application authz floor (R7-SEC S2) ---
# The wire value is a string (owner/admin/member). Canonical rank lives HERE so every service checks
# admin-vs-member the same way (owner ≥ admin ≥ member). Fail-closed: an unknown/missing role ranks
# below member, and an unknown minimum is unreachable.
_ORG_ROLE_RANK = {"owner": 3, "admin": 2, "member": 1}


def org_role_at_least(role: str | None, *, minimum: str) -> bool:
    """True iff ``role`` ranks at least ``minimum`` (owner ≥ admin ≥ member), fail-closed."""
    return _ORG_ROLE_RANK.get(role or "", 0) >= _ORG_ROLE_RANK.get(minimum, 99)


@dataclass(frozen=True, slots=True)
class OrganisationContext:
    """The resolved, immutable organisation scope for a request.

    Frozen so a resolved context cannot be mutated to swap organisation after the
    fact (closes an org-swap path; ADR-006).
    """

    organisation_id: uuid.UUID
    principal_id: uuid.UUID
    principal_type: PrincipalType


class MembershipResolver(Protocol):
    """The seam onto the organisation-membership store.

    The concrete store lives in the substrate (ORA-15 / Epic A2); it is injected
    here so the resolution logic stays free of storage concerns. Returns the
    organisations the principal ``BELONGS_TO`` (a principal may belong to several).
    """

    async def organisations_for(self, principal: Principal) -> list[uuid.UUID]: ...


class OrganisationResolutionError(Exception):
    """Raised when an organisation scope cannot be resolved fail-closed."""


async def resolve_organisation_context(
    principal: Principal,
    *,
    resolver: MembershipResolver,
    requested_organisation_id: uuid.UUID | None = None,
) -> OrganisationContext:
    """Resolve the organisation context for ``principal``, fail-closed.

    An auth-issued organisation claim on the principal is preferred and skips the
    membership lookup entirely (a signed token is authenticated context, not a
    request-body value). Otherwise the organisation is resolved from membership,
    honouring a validated active-org selection when present.
    """
    if principal.organisation_id is not None:
        organisation_id = principal.organisation_id
    else:
        memberships = await resolver.organisations_for(principal)
        organisation_id = _resolve_from_membership(memberships, requested_organisation_id)
    return OrganisationContext(
        organisation_id=organisation_id,
        principal_id=principal.principal_id,
        principal_type=principal.principal_type,
    )


def _resolve_from_membership(
    memberships: list[uuid.UUID],
    requested_organisation_id: uuid.UUID | None,
) -> uuid.UUID:
    available = set(memberships)
    if requested_organisation_id is not None:
        # The validated X-Organisation-Id selection is the only client-influenced
        # channel, and it must name a real membership (ADR-006, T1-M1).
        if requested_organisation_id not in available:
            raise OrganisationResolutionError(
                "selected organisation is not one the principal belongs to"
            )
        return requested_organisation_id
    if not available:
        raise OrganisationResolutionError("principal belongs to no organisation")
    if len(available) > 1:
        raise OrganisationResolutionError(
            "principal belongs to multiple organisations; an active-organisation "
            "selection is required"
        )
    (organisation_id,) = available
    return organisation_id
