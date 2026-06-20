"""Shared fixtures for gateway e2e tests.

Every test in this package drives the DEPLOYED docker stack through the **application-gateway**
(`:8006`) with **real registration → real JWT** — no fakes, no mocks, no internal-function calls, no
DB-direct assertions (FUCK_CLAUDE_FUCK_PAPERCLIP.md / CLAUDE.md §9). The whole package auto-skips
when the gateway is unreachable (the `_require_gateway` autouse fixture), so unit CI stays green.

These are pytest fixtures (auto-discovered) rather than importable helpers on purpose: a test must
never `from tests.e2e.conftest import ...` (that import is not portable under collection — CLAUDE.md
§4.1). Take `register` / `gateway_client` / `gateway_url` as fixture arguments instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest

GATEWAY = "http://localhost:8006"  # the application-gateway — the ONLY external surface


def _gateway_up() -> bool:
    try:
        return httpx.get(f"{GATEWAY}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(autouse=True)
def _require_gateway() -> None:
    """Skip every e2e test when the deployed gateway is unreachable (keeps unit CI green)."""
    if not _gateway_up():
        pytest.skip("gateway :8006 not reachable")


@pytest.fixture
def gateway_url() -> str:
    return GATEWAY


@pytest.fixture
def register() -> Callable[..., dict]:
    """Factory: register a fresh user through the gateway → {token, org_id, user_id, email}."""

    def _register(full_name: str = "E2E User") -> dict:
        email = f"e2e-{uuid.uuid4().hex[:12]}@studio.test"
        reg = httpx.post(
            f"{GATEWAY}/v1/auth/register",
            json={"email": email, "password": "TestPass123", "full_name": full_name},
            timeout=15.0,
        )
        assert reg.status_code == 201, f"register failed: {reg.status_code} {reg.text}"
        token = reg.json()["access_token"]
        me = httpx.get(
            f"{GATEWAY}/v1/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=15.0
        ).json()
        return {
            "token": token,
            "org_id": me["organisation_id"],
            "user_id": me["id"],
            "email": email,
        }

    return _register


@pytest.fixture
def gateway_client() -> Iterator[Callable[[str], httpx.Client]]:
    """Factory for httpx clients bound to the gateway + a JWT; all are closed at teardown."""
    opened: list[httpx.Client] = []

    def _client(token: str) -> httpx.Client:
        c = httpx.Client(
            base_url=GATEWAY, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
        )
        opened.append(c)
        return c

    yield _client
    for c in opened:
        c.close()
