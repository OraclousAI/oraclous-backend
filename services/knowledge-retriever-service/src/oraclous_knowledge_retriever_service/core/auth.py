"""Identity seam (ORAA-4 §21 core layer) — pluggable principal verification (KRS read side).

Mirrors the KGS dev-auth seam: a fixed bearer resolves to a fixed dev principal, and a
`StaticMembershipResolver` maps it to the single dev organisation (the same org KGS writes to, so
KRS reads that org's graph). Flip `KRS_AUTH_MODE=jwt` once the identity service (R3.5-P3) exists.
"""

from __future__ import annotations

import uuid

from oraclous_governance import MembershipResolver, Principal, PrincipalType

from oraclous_knowledge_retriever_service.core.config import get_settings


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
    raise AuthError("KRS_AUTH_MODE=jwt requires the identity service; set KRS_AUTH_MODE=dev")


class StaticMembershipResolver(MembershipResolver):
    """Dev resolver: every principal belongs to the single configured dev organisation."""

    def __init__(self, organisation_id: uuid.UUID) -> None:
        self._organisation_id = organisation_id

    async def organisations_for(self, principal: Principal) -> list[uuid.UUID]:
        return [self._organisation_id]
