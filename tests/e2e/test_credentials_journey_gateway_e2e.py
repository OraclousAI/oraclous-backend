"""Credentials journey END-TO-END through the API GATEWAY — NO fakes.

A real user, through the gateway, stores a secret credential, sees their connected provider, and
reads the metadata back — and the **secret is never echoed** (it is KMS-sealed by the broker).
A credential is org-isolated: another user cannot read it. Real credential-broker + KMS envelope,
nothing mocked.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.security]


def _store(c: httpx.Client, user_id: str, secret: str) -> httpx.Response:
    return c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": "my key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": secret},
        },
    )


def test_a_user_stores_a_credential_and_the_secret_is_never_echoed(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register("Cred Owner")
    c = gateway_client(user["token"])
    secret = "sk-super-secret-" + uuid.uuid4().hex

    store = _store(c, user["user_id"], secret)
    assert store.status_code == 201, store.text
    cid = store.json()["id"]
    assert secret not in store.text  # the store response never echoes the secret

    # the connected provider shows up for the user
    assert "openrouter" in c.get("/credentials/providers").json()["providers"]

    # the metadata reads back, but the KMS-sealed secret is never returned
    got = c.get(f"/credentials/{cid}")
    assert got.status_code == 200, got.text
    assert secret not in got.text


def test_a_credential_is_org_isolated_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    owner = register("Cred A")
    cid = _store(gateway_client(owner["token"]), owner["user_id"], "sk-" + uuid.uuid4().hex).json()[
        "id"
    ]
    other = gateway_client(register("Cred B")["token"])
    assert other.get(f"/credentials/{cid}").status_code in (
        403,
        404,
    )  # B cannot read A's credential
