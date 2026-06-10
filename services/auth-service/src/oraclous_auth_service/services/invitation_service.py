"""Invitation use-cases (ORAA-4 §21 services layer, threat T-INVITE / T-PRIV).

Create (admin+ only; supersedes any prior pending for the same email), list, revoke, public peek,
and accept → membership. Every accept/peek failure mode (unknown / expired / revoked / replayed /
hash-mismatch) collapses to one generic ``InvitationInvalidError`` (400) so the token is no
enumeration oracle. The raw token is returned exactly once at creation; only its hash is stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oraclous_auth_service.domain.invitations import (
    generate_invitation_token,
    is_expired,
    token_matches,
    token_prefix,
)
from oraclous_auth_service.domain.organisations import OrgRole, can_manage
from oraclous_auth_service.models.invitation_model import OrgInvitation
from oraclous_auth_service.repositories.audit_repository import AuditRepository
from oraclous_auth_service.repositories.invitation_repository import InvitationRepository
from oraclous_auth_service.repositories.org_member_repository import OrgMemberRepository
from oraclous_auth_service.repositories.organisation_repository import OrganisationRepository
from oraclous_auth_service.repositories.user_repository import normalize_email
from oraclous_auth_service.services.org_service import OrgForbiddenError, OrgNotFoundError

_DEFAULT_TTL_DAYS = 7
_INVITABLE_ROLES = {OrgRole.MEMBER.value, OrgRole.ADMIN.value}


class InvitationInvalidError(Exception):
    """Token unknown / expired / revoked / replayed — maps to a generic HTTP 400 (no oracle)."""


class InvitationRoleError(Exception):
    """An invitation role outside {member, admin} was requested — maps to HTTP 422."""


class InvitationService:
    def __init__(
        self,
        *,
        invitations: InvitationRepository,
        members: OrgMemberRepository,
        organisations: OrganisationRepository,
        audit: AuditRepository | None = None,
    ) -> None:
        self._inv = invitations
        self._members = members
        self._orgs = organisations
        self._audit = audit

    async def _require_admin(self, *, org_id: str, user_id: str) -> None:
        role = await self._members.role_for(organisation_id=org_id, user_id=user_id)
        if role is None:
            raise OrgNotFoundError("organisation not found")
        if not can_manage(role, min_role=OrgRole.ADMIN):
            raise OrgForbiddenError("requires admin or owner role")

    async def create_invitation(
        self,
        *,
        org_id: str,
        inviter_user_id: str,
        email: str,
        role: str = OrgRole.MEMBER.value,
        subgraph_grants: dict | None = None,
        ttl_days: int = _DEFAULT_TTL_DAYS,
    ) -> tuple[OrgInvitation, str]:
        await self._require_admin(org_id=org_id, user_id=inviter_user_id)
        if role not in _INVITABLE_ROLES:
            raise InvitationRoleError("invitation role must be 'member' or 'admin'")
        normalized = normalize_email(email)
        # At most one pending invitation per (org, email): supersede the prior one.
        await self._inv.supersede_pending(organisation_id=org_id, email=normalized)
        raw, prefix, token_hash = generate_invitation_token()
        inv = await self._inv.create(
            organisation_id=org_id,
            email=normalized,
            org_role=role,
            token_hash=token_hash,
            token_prefix=prefix,
            invited_by_user_id=inviter_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=ttl_days),
            subgraph_grants=subgraph_grants,
        )
        return inv, raw

    async def list_invitations(self, *, org_id: str, user_id: str) -> list[OrgInvitation]:
        await self._require_admin(org_id=org_id, user_id=user_id)
        return await self._inv.list_for_org(org_id)

    async def revoke_invitation(self, *, org_id: str, invitation_id: str, user_id: str) -> None:
        await self._require_admin(org_id=org_id, user_id=user_id)
        if not await self._inv.revoke(invitation_id=invitation_id, organisation_id=org_id):
            raise InvitationInvalidError("invitation not found or not pending")

    async def _resolve_pending(self, raw_token: str) -> OrgInvitation:
        """Find the live pending invitation for a raw token, or raise the generic error."""
        candidates = await self._inv.by_prefix_pending(token_prefix(raw_token))
        for inv in candidates:
            if token_matches(raw_token, inv.token_hash) and not is_expired(inv.expires_at):
                return inv
        raise InvitationInvalidError("invitation is invalid or has expired")

    async def peek(self, *, raw_token: str) -> dict:
        inv = await self._resolve_pending(raw_token)
        org = await self._orgs.get_by_id(inv.organisation_id)
        return {
            "organisation_id": inv.organisation_id,
            "organisation_name": org.name if org is not None else None,
            "email": inv.email,
            "role": inv.org_role,
            "status": inv.status,
            "subgraph_grants": inv.subgraph_grants,
        }

    async def accept(self, *, raw_token: str, accepter_user_id: str) -> dict:
        inv = await self._resolve_pending(raw_token)
        # Idempotent: if already a member, don't duplicate the edge — just settle the invitation.
        existing = await self._members.role_for(
            organisation_id=inv.organisation_id, user_id=accepter_user_id
        )
        if existing is None:
            await self._members.add(
                organisation_id=inv.organisation_id,
                user_id=accepter_user_id,
                role=inv.org_role,
            )
        await self._inv.mark_accepted(
            invitation_id=inv.id,
            accepted_by_user_id=accepter_user_id,
            accepted_at=datetime.now(UTC),
        )
        if self._audit is not None:
            await self._audit.record(
                event="invitation.accept",
                actor_type="user",
                actor_id=accepter_user_id,
                organisation_id=inv.organisation_id,
                target=inv.id,
            )
        return {"organisation_id": inv.organisation_id, "role": inv.org_role}
