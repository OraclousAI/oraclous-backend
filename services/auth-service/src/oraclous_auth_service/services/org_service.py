"""Organisation use-cases (services layer).

Create an org (creator becomes its owner member), list a user's orgs, read/patch a single org with
membership + role authorisation, and resolve the active organisation for a token. Cross-org access
is masked as 404 (a non-member cannot tell a foreign org exists, T-ENUM); a member lacking the
required role gets 403 (T-PRIV). Slugs are unique: a candidate is resolved before the insert and a
candidate the index still rejects is retried, never surfaced (#676).
"""

from __future__ import annotations

import secrets

from oraclous_auth_service.core.db_errors import ConstraintViolation
from oraclous_auth_service.domain.organisations import (
    MemberView,
    OrgRole,
    can_manage,
    role_rank,
    slugify,
)
from oraclous_auth_service.models.organisation_model import Organisation
from oraclous_auth_service.models.user_model import User
from oraclous_auth_service.repositories.org_member_repository import OrgMemberRepository
from oraclous_auth_service.repositories.organisation_repository import OrganisationRepository
from oraclous_auth_service.repositories.user_repository import UserRepository

_SLUG_MAX_TRIES = 50
_SLUG_MAX_LEN = 63  # organisations.slug is String(63)
_SLUG_LADDER_STEM = 60  # leaves room for "-51"
_SLUG_RANDOM_BYTES = 4  # 8 hex chars
_SLUG_RANDOM_TRIES = 8
_INSERT_MAX_TRIES = 5


def _truncate(base: str, keep: int) -> str:
    """Cut ``base`` to ``keep`` chars without leaving a trailing hyphen, which a suffix would
    double into an invalid slug."""
    return base[:keep].rstrip("-") or "org"


def _random_slug(base: str) -> str:
    """``base`` with a random suffix, trimmed so the whole slug still fits the column."""
    suffix = secrets.token_hex(_SLUG_RANDOM_BYTES)
    return f"{_truncate(base, _SLUG_MAX_LEN - len(suffix) - 1)}-{suffix}"


def _slug_candidates(base: str) -> list[str]:
    """Candidate slugs in preference order: the plain base, then ``base-2 … base-51``, then random
    suffixes.

    ``base`` is an already-slugified input (the caller passes ``slugify(name)`` — #731's
    ``organisations.slugify``, itself composed around the shared ``basic_slug`` primitive). This
    is a UNIQUENESS LADDER over that base, not a text normaliser, so it is out of #731's scope: it
    never calls ``.lower()`` or substitutes non-alphanumerics — it only appends a numeric or random
    suffix and re-truncates to fit the column.

    The numeric ladder keeps everyday slugs short and familiar. The random tail is the escape for a
    name whose ladder is full: before #676 that case fell back onto ``base[:55]-50``, which for any
    base under 55 chars — every real org name — is the rung the ladder had just found taken, so the
    52nd org sharing a first name always failed the unique index. Every candidate fits
    ``String(63)``.
    """
    return [
        base,
        *(f"{_truncate(base, _SLUG_LADDER_STEM)}-{n}" for n in range(2, _SLUG_MAX_TRIES + 2)),
        *(_random_slug(base) for _ in range(_SLUG_RANDOM_TRIES)),
    ]


class OrgNotFoundError(Exception):
    """Org does not exist or the caller is not a member — maps to HTTP 404 (enumeration mask)."""


class OrgForbiddenError(Exception):
    """Caller is a member but lacks the required role — maps to HTTP 403."""


class OrgService:
    def __init__(
        self,
        *,
        organisations: OrganisationRepository,
        members: OrgMemberRepository,
        users: UserRepository,
    ) -> None:
        self._orgs = organisations
        self._members = members
        self._users = users

    async def role_for(self, *, org_id: str, user_id: str) -> str | None:
        """The member's role in an org (owner/admin/member), or None if not a member. Used at token
        issuance to stamp the ``org_role`` claim (R7-SEC S2)."""
        return await self._members.role_for(organisation_id=org_id, user_id=user_id)

    async def _resolve_slug(self, name: str, *, rejected: frozenset[str] = frozenset()) -> str:
        """The first candidate no other org holds.

        ``rejected`` carries candidates whose insert the unique index already refused during this
        call — a read cannot see a row a concurrent transaction has not committed, so asking again
        would return the same losing answer.
        """
        base = slugify(name)
        for candidate in _slug_candidates(base):
            if candidate not in rejected and not await self._orgs.slug_exists(candidate):
                return candidate
        # Every candidate read as taken. Offer a fresh random one and let the insert arbitrate.
        return _random_slug(base)

    async def create_org(
        self, *, name: str, owner_user_id: str, org_id: str | None = None
    ) -> Organisation:
        """Create an organisation and make ``owner_user_id`` its owner member.

        Resolution asks which slugs are free, but that read-then-insert can lose a race to a
        parallel registration, so a candidate the unique index refuses is retried against a fresh
        one rather than surfacing as a 500 (#676). Registration derives the org name from the
        user's first name, so refusing here would lock out the second user sharing a first name —
        resolution has to keep succeeding. The repository scopes the failed insert to a SAVEPOINT,
        which is what leaves the request's session usable across the retry.
        """
        rejected: set[str] = set()
        attempt = 0
        while True:
            attempt += 1
            slug = await self._resolve_slug(name, rejected=frozenset(rejected))
            try:
                org = await self._orgs.create(
                    id=org_id, name=name, slug=slug, owner_user_id=owner_user_id
                )
            except ConstraintViolation:
                if attempt >= _INSERT_MAX_TRIES:
                    raise  # not a slug race any more — a real constraint fault, let it surface
                rejected.add(slug)
                continue
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

    async def list_members(self, *, org_id: str, user_id: str) -> list[MemberView]:
        """The org's member roster (any member may view). 404-masked if caller is not a member."""
        caller_role = await self._members.role_for(organisation_id=org_id, user_id=user_id)
        if caller_role is None:
            raise OrgNotFoundError("organisation not found")
        members = await self._members.list_members(org_id)
        emails = {
            u.id: u.email for u in await self._users.list_by_ids([m.user_id for m in members])
        }
        return [
            MemberView(
                user_id=m.user_id, email=emails.get(m.user_id), role=m.org_role, since=m.since
            )
            for m in members
        ]

    async def change_member_role(
        self, *, org_id: str, actor_user_id: str, target_user_id: str, role: str
    ) -> MemberView:
        """Change a member's role (admin+; actor must outrank target — owner stays immutable)."""
        await self._authorise_member_management(
            org_id=org_id, actor_user_id=actor_user_id, target_user_id=target_user_id
        )
        updated = await self._members.update_role(
            organisation_id=org_id, user_id=target_user_id, role=role
        )
        if updated is None:  # pragma: no cover — the guard already proved target is a member
            raise OrgNotFoundError("member not found")
        user = await self._users.get_by_id(target_user_id)
        return MemberView(
            user_id=updated.user_id,
            email=user.email if user is not None else None,
            role=updated.org_role,
            since=updated.since,
        )

    async def remove_member(self, *, org_id: str, actor_user_id: str, target_user_id: str) -> None:
        """Remove a member (admin+; actor must outrank target — the owner cannot be removed)."""
        await self._authorise_member_management(
            org_id=org_id, actor_user_id=actor_user_id, target_user_id=target_user_id
        )
        await self._members.remove(organisation_id=org_id, user_id=target_user_id)

    async def _authorise_member_management(
        self, *, org_id: str, actor_user_id: str, target_user_id: str
    ) -> None:
        """Shared guard for role-change/removal. 404 if caller isn't a member or target isn't found;
        403 if the caller lacks admin, or does not strictly outrank the target (protects the owner,
        equal-rank peers, and self)."""
        actor_role = await self._members.role_for(organisation_id=org_id, user_id=actor_user_id)
        if actor_role is None:
            raise OrgNotFoundError("organisation not found")
        if not can_manage(actor_role, min_role=OrgRole.ADMIN):
            raise OrgForbiddenError("requires admin or owner role")
        target_role = await self._members.role_for(organisation_id=org_id, user_id=target_user_id)
        if target_role is None:
            raise OrgNotFoundError("member not found")
        if role_rank(actor_role) <= role_rank(target_role):
            raise OrgForbiddenError("cannot manage a member of equal or higher role")

    async def resolve_active_org(self, *, user: User, requested_org_id: str | None) -> str:
        """The org to embed in the issued token: a validated X-Organisation-Id, else the default."""
        if not requested_org_id:
            return user.default_organisation_id
        role = await self._members.role_for(organisation_id=requested_org_id, user_id=user.id)
        if role is None:
            raise OrgNotFoundError("selected organisation is not one you belong to")
        return requested_org_id
