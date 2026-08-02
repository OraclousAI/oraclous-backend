"""Unit: org slug resolution never hands the insert a slug it knows is taken (#676). No DB.

``OrgService._resolve_slug`` walks a bounded ladder — ``base``, then ``base[:60]-2 … base[:60]-51``
— and, on exhaustion, returns ``base[:55]-50``. For any base under 55 chars (every real org name)
that fallback is byte-identical to the ``n=50`` rung the ladder already tested and found taken, so
the insert violates ``ix_organisations_slug_unique`` and the caller gets a raw 500. The comment at
the fallback calls this "extremely unlikely"; it is certain, on the 52nd org sharing a first name.

Registration derives the org name from the user's first name (``default_org_name``), so the user
never chose it — the resolution must keep succeeding rather than start refusing. These tests pin
behaviour through the public ``create_org`` seam, not the private ladder, and assert properties
(unique, valid, within ``String(63)``) rather than an exact suffix, so the fix is free to escape the
numeric ladder however it likes.
"""

from __future__ import annotations

import re
from typing import cast

import pytest
from oraclous_auth_service.domain.organisations import slugify
from oraclous_auth_service.repositories.org_member_repository import OrgMemberRepository
from oraclous_auth_service.repositories.organisation_repository import OrganisationRepository
from oraclous_auth_service.repositories.user_repository import UserRepository
from oraclous_auth_service.services.org_service import OrgService
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.unit

_SLUG_MAX = 63
_SLUG_CHARSET = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_OWNER = "00000000-0000-0000-0000-0000000006c4"

# The name a registering user named "File-Native" gets, and the slug it derives — the exact pair
# from the #676 report (``Key (slug)=(file-native-s-second-mind-50) already exists``).
_NAME = "File-Native's Second Mind"


class _StubOrg:
    """What the repository hands back — the service only reads ``id`` and ``slug``."""

    def __init__(self, *, id: str, name: str, slug: str) -> None:
        self.id = id
        self.name = name
        self.slug = slug


class _FakeOrgRepo:
    """An in-memory ``organisations`` table that enforces the unique slug index, like Postgres.

    ``taken`` seeds slugs that already exist without going through ``create``; ``created`` records
    what the service actually inserted.
    """

    def __init__(self, *, taken: set[str] | None = None) -> None:
        self.taken: set[str] = set(taken or ())
        self.created: list[str] = []
        self.create_calls = 0

    async def slug_exists(self, slug: str) -> bool:
        return slug in self.taken

    async def create(
        self, *, id: str | None = None, name: str, slug: str, owner_user_id: str
    ) -> _StubOrg:
        self.create_calls += 1
        if slug in self.taken:
            # what ``ix_organisations_slug_unique`` raises through asyncpg -> SQLAlchemy
            raise IntegrityError(
                "INSERT INTO organisations",
                {},
                Exception(
                    'duplicate key value violates unique constraint "ix_organisations_slug_unique"'
                ),
            )
        self.taken.add(slug)
        self.created.append(slug)
        return _StubOrg(id=id or f"org-{len(self.created)}", name=name, slug=slug)


class _FakeMemberRepo:
    def __init__(self) -> None:
        self.added: list[tuple[str, str, str]] = []

    async def add(self, *, organisation_id: str, user_id: str, role: str) -> None:
        self.added.append((organisation_id, user_id, role))


def _service(repo: _FakeOrgRepo) -> OrgService:
    return OrgService(
        organisations=cast(OrganisationRepository, repo),
        members=cast(OrgMemberRepository, _FakeMemberRepo()),
        users=cast(UserRepository, object()),
    )


def _assert_valid_slug(slug: str) -> None:
    assert _SLUG_CHARSET.match(slug), f"{slug!r} is not a valid slug"
    assert len(slug) <= _SLUG_MAX, f"{slug!r} is {len(slug)} chars, over the String(63) column"


# --- the reported failure ------------------------------------------------------------------------


async def test_the_fifty_second_org_sharing_a_name_still_registers() -> None:
    """The #676 reproduction. 51 orgs fill ``base`` plus the whole numeric ladder; the 52nd must
    still get a slug rather than a ``UniqueViolationError`` the caller renders as a 500."""
    repo = _FakeOrgRepo()
    service = _service(repo)

    for _ in range(52):
        await service.create_org(name=_NAME, owner_user_id=_OWNER)

    assert len(repo.created) == 52
    assert len(set(repo.created)) == 52, "every org needs its own slug"
    for slug in repo.created:
        _assert_valid_slug(slug)


async def test_resolution_never_returns_a_slug_it_was_told_is_taken() -> None:
    """The defect itself, isolated. With every numeric rung taken, the exhaustion path must escape
    the ladder — today it returns ``base[:55]-50``, a rung it already tested and rejected."""
    base = slugify(_NAME)
    repo = _FakeOrgRepo(taken={base} | {f"{base[:60]}-{n}" for n in range(2, 200)})
    service = _service(repo)

    org = await service.create_org(name=_NAME, owner_user_id=_OWNER)

    assert org.slug not in {base} | {f"{base[:60]}-{n}" for n in range(2, 200)}
    _assert_valid_slug(org.slug)


async def test_a_collision_raised_at_insert_is_retried_not_propagated() -> None:
    """``slug_exists`` then ``create`` is check-then-act, so two concurrent registrations can both
    clear the check. The repository docstring already calls the unique index "the race backstop" —
    the backstop has to be caught. A registering user must never see the raw IntegrityError."""
    base = slugify(_NAME)
    repo = _FakeOrgRepo()
    # ``base`` is free at check time and taken by the time the insert lands — the concurrent race.
    original_slug_exists = repo.slug_exists

    async def _racing_slug_exists(slug: str) -> bool:
        answer = await original_slug_exists(slug)
        if slug == base and not answer:
            repo.taken.add(base)  # a parallel registration wins between the check and the insert
        return answer

    repo.slug_exists = _racing_slug_exists  # type: ignore[method-assign]
    service = _service(repo)

    org = await service.create_org(name=_NAME, owner_user_id=_OWNER)

    assert org.slug != base, "the losing side must move to a free slug"
    assert repo.create_calls >= 2, "the insert has to be retried, not abandoned"
    _assert_valid_slug(org.slug)


async def test_a_long_name_stays_within_the_slug_column_on_the_exhaustion_path() -> None:
    """``slug`` is ``String(63)``, and ``slugify`` already spends all 63 on a long name. The escape
    from the ladder must still fit — a 64-char slug is a different 500 in the same place.

    Both truncations are seeded, because the current fallback shortens ``base[:60]`` to
    ``base[:55]`` and so accidentally escapes for a *long* name while colliding for a short one.
    """
    long_name = "M" * 200
    base = slugify(long_name)
    taken = (
        {base}
        | {f"{base[:60]}-{n}" for n in range(2, 200)}
        | {f"{base[:55]}-{n}" for n in range(2, 200)}
    )
    repo = _FakeOrgRepo(taken=taken)
    service = _service(repo)

    org = await service.create_org(name=long_name, owner_user_id=_OWNER)

    assert org.slug not in taken
    _assert_valid_slug(org.slug)


# --- green pins: what must not change ------------------------------------------------------------


async def test_the_first_org_for_a_name_still_gets_the_plain_slug() -> None:
    repo = _FakeOrgRepo()
    org = await _service(repo).create_org(name="Acme Corp", owner_user_id=_OWNER)
    assert org.slug == "acme-corp"


async def test_the_second_org_for_a_name_still_gets_the_dash_two_suffix() -> None:
    repo = _FakeOrgRepo(taken={"acme-corp"})
    org = await _service(repo).create_org(name="Acme Corp", owner_user_id=_OWNER)
    assert org.slug == "acme-corp-2"


async def test_the_owner_membership_is_still_created() -> None:
    """The slug work must not disturb ``create_org``'s other half: the creator becomes owner."""
    repo = _FakeOrgRepo()
    members = _FakeMemberRepo()
    service = OrgService(
        organisations=cast(OrganisationRepository, repo),
        members=cast(OrgMemberRepository, members),
        users=cast(UserRepository, object()),
    )

    org = await service.create_org(name="Acme Corp", owner_user_id=_OWNER)

    assert members.added == [(org.id, _OWNER, "owner")]
