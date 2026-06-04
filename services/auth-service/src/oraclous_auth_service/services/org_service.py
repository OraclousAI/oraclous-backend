"""Organisation use-cases (ORAA-4 §21 services layer).

Create an org (creator becomes its owner member), list a user's orgs, read/patch a single org with
membership + role authorisation, and resolve the active organisation for a token. Cross-org access
is masked as 404 (a non-member cannot tell a foreign org exists, T-ENUM); a member lacking the
required role gets 403 (T-PRIV). Slugs are unique (suffixed before insert; the index backstops).
"""

from __future__ import annotations

from oraclous_auth_service.domain.organisations import OrgRole, can_manage, slugify
from oraclous_auth_service.models.organisation_model import Organisation
from oraclous_auth_service.models.user_model import User
from oraclous_auth_service.repositories.org_member_repository import OrgMemberRepository
from oraclous_auth_service.repositories.organisation_repository import OrganisationRepository

_SLUG_MAX_TRIES = 50


class OrgNotFoundError(Exception):
    """Org does not exist or the caller is not a member — maps to HTTP 404 (enumeration mask)."""


class OrgForbiddenError(Exception):
    """Caller is a member but lacks the required role — maps to HTTP 403."""


class OrgService:
    def __init__(
        self, *, organisations: OrganisationRepository, members: OrgMemberRepository
    ) -> None:
        self._orgs = organisations
        self._members = members

    async def _resolve_slug(self, name: str) -> str:
        base = slugify(name)
        if not await self._orgs.slug_exists(base):
            return base
        for n in range(2, _SLUG_MAX_TRIES + 2):
            candidate = f"{base[:60]}-{n}"
            if not await self._orgs.slug_exists(candidate):
                return candidate
        # Extremely unlikely; let the unique index backstop a final attempt.
        return f"{base[:55]}-{_SLUG_MAX_TRIES}"

    async def create_org(
        self, *, name: str, owner_user_id: str, org_id: str | None = None
    ) -> Organisation:
        """Create an organisation and make ``owner_user_id`` its owner member."""
        slug = await self._resolve_slug(name)
        org = await self._orgs.create(id=org_id, name=name, slug=slug, owner_user_id=owner_user_id)
        await self._members.add(
            organisation_id=org.id, user_id=owner_user_id, role=OrgRole.OWNER.value
        )
        return org

    async def list_for_user(self, *, user_id: str) -> list[Organisation]:
        org_ids = await self._members.organisation_ids_for(user_id)
        return await self._orgs.get_many(org_ids)

    async def get_org(self, *, org_id: str, user_id: str) -> Organisation:
        role = await self._members.role_for(organisation_id=org_id, user_id=user_id)
        if role is None:
            raise OrgNotFoundError("organisation not found")
        org = await self._orgs.get_by_id(org_id)
        if org is None:
            raise OrgNotFoundError("organisation not found")
        return org

    async def update_org(
        self,
        *,
        org_id: str,
        user_id: str,
        name: str | None = None,
        description: str | None = None,
        logo_url: str | None = None,
    ) -> Organisation:
        role = await self._members.role_for(organisation_id=org_id, user_id=user_id)
        if role is None:
            raise OrgNotFoundError("organisation not found")
        if not can_manage(role, min_role=OrgRole.ADMIN):
            raise OrgForbiddenError("requires admin or owner role")
        org = await self._orgs.update(org_id, name=name, description=description, logo_url=logo_url)
        if org is None:
            raise OrgNotFoundError("organisation not found")
        return org

    async def resolve_active_org(self, *, user: User, requested_org_id: str | None) -> str:
        """The org to embed in the issued token: a validated X-Organisation-Id, else the default."""
        if not requested_org_id:
            return user.default_organisation_id
        role = await self._members.role_for(organisation_id=requested_org_id, user_id=user.id)
        if role is None:
            raise OrgNotFoundError("selected organisation is not one you belong to")
        return requested_org_id
