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


async def test_the_first_two_registrations_keep_their_familiar_slugs(client: AsyncClient) -> None:
    """Green pin. The fix changes only the exhaustion path; the everyday shape is untouched."""
    _, first = await _register(client, "first@ex.com", _SHARED_FIRST_NAME)
    _, second = await _register(client, "second@ex.com", _SHARED_FIRST_NAME)

    assert await _slug_of(client, first["access_token"]) == "file-native-s-second-mind"
    assert await _slug_of(client, second["access_token"]) == "file-native-s-second-mind-2"
