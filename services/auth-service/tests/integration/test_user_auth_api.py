"""Integration: the full user-identity flow against real Postgres (testcontainers, ORA-12 harness).

Drives the live FastAPI app (no mocks below the route) over httpx ASGITransport against a real
Postgres: register → login → refresh-rotation + reuse-detection → `/me` revocation re-check. Proves
the §22 "real endpoints vs real substrate" gate for Slice 1.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_register_login_refresh_me_flow(client: AsyncClient) -> None:
    # register (email is normalised to lowercase)
    r = await client.post(
        "/v1/auth/register", json={"email": "Alice@Ex.com", "password": "GoodPass1"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "alice@ex.com"
    access, refresh = body["access_token"], body["refresh_token"]
    assert access and refresh

    # duplicate registration → 409
    dup = await client.post(
        "/v1/auth/register", json={"email": "alice@ex.com", "password": "GoodPass1"}
    )
    assert dup.status_code == 409

    # /me with the access token carries a real organisation_id
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["principal_type"] == "user" and me.json()["organisation_id"]
    assert me.json()["org_role"] == "owner"  # the registrant owns their default org (R7-SEC S2)

    # login works; bad password and unknown email are the SAME generic 401 (no enumeration)
    assert (
        await client.post("/v1/auth/login", json={"email": "alice@ex.com", "password": "GoodPass1"})
    ).status_code == 200
    assert (
        await client.post(
            "/v1/auth/login", json={"email": "alice@ex.com", "password": "WrongPass1"}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/v1/auth/login", json={"email": "nobody@ex.com", "password": "GoodPass1"}
        )
    ).status_code == 401

    # refresh rotates to a NEW refresh token
    rotated = await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    assert rotated.status_code == 200
    new_refresh = rotated.json()["refresh_token"]
    assert new_refresh != refresh

    # reusing the OLD (rotated) refresh is detected → 401 AND kills the whole family
    assert (
        await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    ).status_code == 401
    # so even the freshly-rotated token is now revoked
    assert (
        await client.post("/v1/auth/refresh", json={"refresh_token": new_refresh})
    ).status_code == 401


async def test_me_rejects_missing_and_refresh_tokens(client: AsyncClient) -> None:
    assert (await client.get("/v1/auth/me")).status_code == 401
    reg = {"email": "bob@ex.com", "password": "GoodPass1"}
    r = await client.post("/v1/auth/register", json=reg)
    refresh = r.json()["refresh_token"]
    # a refresh token must not authorise a user route (type != access)
    bad = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert bad.status_code == 401


async def test_weak_password_rejected_at_register(client: AsyncClient) -> None:
    r = await client.post("/v1/auth/register", json={"email": "weak@ex.com", "password": "weak"})
    assert r.status_code == 422
