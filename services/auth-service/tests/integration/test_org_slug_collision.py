"""Integration: registrations sharing a first name never 500 on the slug index (#676).

Real Postgres (testcontainers), so ``ix_organisations_slug_unique`` is live and the failure mode is
the reported one rather than a fake's imitation. The unit suite pins the resolution logic; this pins
the observable HTTP contract: signup keeps returning 201.

Registration derives the org name from the user's first name (``default_org_name``), so the user
never picked it. The 52nd "File-Native" must still get an account — a 4xx here would be a worse bug
than the 500 it replaced.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from oraclous_auth_service.repositories.organisation_repository import OrganisationRepository

pytestmark = pytest.mark.integration

# One org fills ``base``; the numeric ladder ``base-2 … base-51`` fills fifty more. The 52nd
# registration is the one that exhausts resolution and, before the fix, 500s.
_LADDER_CAPACITY = 51
_SHARED_FIRST_NAME = "File-Native"


async def _register(client: AsyncClient, email: str, full_name: str) -> tuple[int, dict]:
    r = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "GoodPass1", "full_name": full_name},
    )
    return r.status_code, (r.json() if r.content else {})


async def _slug_of(client: AsyncClient, access_token: str) -> str:
    orgs = await client.get("/v1/orgs", headers={"Authorization": f"Bearer {access_token}"})
    assert orgs.status_code == 200, orgs.text
    return str(orgs.json()[0]["slug"])


async def test_every_registration_sharing_a_first_name_succeeds(client: AsyncClient) -> None:
    """The #676 reproduction, through the real index. Before the fix the 52nd returns 500
    ``INTERNAL_ERROR`` on ``duplicate key value violates unique constraint``."""
    slugs: list[str] = []

    for i in range(_LADDER_CAPACITY + 1):
        status, body = await _register(client, f"user{i}@ex.com", _SHARED_FIRST_NAME)
        assert status == 201, f"registration {i} failed: {status} {body}"
        slugs.append(await _slug_of(client, body["access_token"]))

    assert len(slugs) == _LADDER_CAPACITY + 1
    assert len(set(slugs)) == len(slugs), "two orgs share a slug"
    assert all(len(s) <= 63 for s in slugs), "a slug overflowed the String(63) column"


async def test_signup_never_answers_4xx_because_a_name_is_taken(client: AsyncClient) -> None:
    """A derived org name is not a user choice, so exhausting the ladder must not turn into a
    'name taken' rejection. Guards the fix against the wrong remedy."""
    for i in range(_LADDER_CAPACITY + 1):
        status, body = await _register(client, f"dup{i}@ex.com", _SHARED_FIRST_NAME)
        assert status == 201, f"registration {i} was refused: {status} {body}"


async def test_a_slug_taken_between_the_check_and_the_insert_still_registers(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concurrent race, forced deterministically against the real index.

    ``slug_exists`` then ``create`` is check-then-act, so a parallel registration can take the slug
    in between; the repository docstring already calls the unique index "the race backstop". This
    test makes the losing side's view deterministic: ``slug_exists`` answers "free" once for a slug
    that is in fact held, so the insert reaches Postgres and the index rejects it.

    It lives at the integration tier on purpose. Recovering here needs a SAVEPOINT
    (``session.begin_nested()``) around the insert, because ``core/database.py`` gives one session
    per request and a failed ``flush`` leaves it unusable until rollback — which would discard the
    whole request. The plain ``except IntegrityError: retry`` that satisfies the unit suite's
    in-memory repository raises ``PendingRollbackError`` against a real session, so only this test
    can tell the two implementations apart.
    """
    status_first, first = await _register(client, "winner@ex.com", _SHARED_FIRST_NAME)
    assert status_first == 201, first
    held = await _slug_of(client, first["access_token"])

    real_slug_exists = OrganisationRepository.slug_exists
    lied = False

    async def _free_once(self: OrganisationRepository, slug: str) -> bool:
        """Answer "free" the first time ``held`` is checked, then tell the truth — the losing side
        of a race clears the check, and by the time its insert lands the winner holds the slug."""
        nonlocal lied
        if slug == held and not lied:
            lied = True
            return False
        return await real_slug_exists(self, slug)

    monkeypatch.setattr(OrganisationRepository, "slug_exists", _free_once)

    status_second, second = await _register(client, "loser@ex.com", _SHARED_FIRST_NAME)

    assert lied, (
        "the lie was never spent: resolution no longer asks before inserting, so this test models "
        "nothing. Rewrite it to force the collision the new way — do not just delete it."
    )
    assert status_second == 201, f"the losing side of the race was not recovered: {second}"
    slug_second = await _slug_of(client, second["access_token"])
    assert slug_second != held, "the losing side must move to a free slug"
    assert len(slug_second) <= 63, "a slug overflowed the String(63) column"


async def test_the_first_two_registrations_keep_their_familiar_slugs(client: AsyncClient) -> None:
    """Green pin. The fix changes only the exhaustion path; the everyday shape is untouched."""
    _, first = await _register(client, "first@ex.com", _SHARED_FIRST_NAME)
    _, second = await _register(client, "second@ex.com", _SHARED_FIRST_NAME)

    assert await _slug_of(client, first["access_token"]) == "file-native-s-second-mind"
    assert await _slug_of(client, second["access_token"]) == "file-native-s-second-mind-2"
