"""Identity seam (ORAA-4 §21 core layer) — pluggable principal verification.

R3.5-P1-S1 runs in `dev` mode: a fixed bearer resolves to a fixed dev principal, and a
`StaticMembershipResolver` maps that principal to the single dev organisation. This is the ONE
seam where single-tenant lives — the write path still flows through
`oraclous_substrate.access.enforced_organisation_id()` (fail-closed). Flip `KGS_AUTH_MODE=jwt`
once the identity/org service (R3.5-P3) exists; `verify_token` keeps the same signature so the
swap is local. The three swap points are: `verify_token`, the dev org binder
(`core/dependencies.bind_org_context`), and this `StaticMembershipResolver`.
"""

from __future__ import annotations

import uuid

from oraclous_governance import MembershipResolver, Principal, PrincipalType

from oraclous_knowledge_graph_service.core.config import get_settings


class AuthError(Exception):
    """Authentication failed. Maps to HTTP 401."""


async def verify_token(token: str) -> Principal:
    """Resolve a bearer token to an authenticated Principal. Patchable module-level name."""
    settings = get_settings()
    if settings.auth_mode == "dev":
        if token != settings.dev_bearer:
            raise AuthError("invalid dev bearer token")
        return Principal(
            principal_id=uuid.UUID(settings.dev_user_id),
            principal_type=PrincipalType.USER,
        )
    # jwt mode is wired when the identity/org service (R3.5-P3) lands; keep dev mode until then.
    raise AuthError("KGS_AUTH_MODE=jwt requires the identity service; set KGS_AUTH_MODE=dev")


class StaticMembershipResolver(MembershipResolver):
    """Dev resolver: every principal belongs to the single configured dev organisation.

    Drop-in for the real membership resolver (identity/org service) later — same Protocol.
    """

    def __init__(self, organisation_id: uuid.UUID) -> None:
        self._organisation_id = organisation_id

    async def organisations_for(self, principal: Principal) -> list[uuid.UUID]:
        return [self._organisation_id]
