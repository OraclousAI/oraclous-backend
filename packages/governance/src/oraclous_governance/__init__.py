"""oraclous-governance — request-scoped organisation-context kernel (Layer 1, ADR-006)."""

from __future__ import annotations

from oraclous_governance.context import (
    MembershipResolver,
    OrganisationContext,
    OrganisationResolutionError,
    Principal,
    PrincipalType,
    resolve_organisation_context,
)
from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    current_organisation_context,
    use_organisation_context,
)

__all__ = [
    "MembershipResolver",
    "MissingOrganisationContextError",
    "OrganisationContext",
    "OrganisationResolutionError",
    "Principal",
    "PrincipalType",
    "current_organisation_context",
    "resolve_organisation_context",
    "use_organisation_context",
]
