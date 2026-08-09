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

import os
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest

GATEWAY = "http://localhost:8006"  # the application-gateway — the ONLY external surface

#: The user's own model key, read from the env (scripts/e2e.sh exports it from deploy/.env.test).
#: Since #724 no service is handed a platform model key, so a stack running a REAL extractor
#: (``KGS_EXTRACTOR=openai``) needs the ORG to have designated one — otherwise every ingest that
#: reaches the extractor fails closed with ``model_credential_not_configured``. The compose DEFAULT
#: is ``KGS_EXTRACTOR=null``, which needs no credential, so CI and a key-free stack are unaffected.
_MODEL_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()


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


def _designate_org_model_credential(token: str, user_id: str) -> str | None:
    """Give a freshly registered org its own default model credential (#724), through the real API.

    The user pastes THEIR key via ``POST /credentials/`` and designates it the org default with
    ``PUT /credentials/{id}`` ``default_for="model"`` — the same two calls a real operator makes,
    nothing injected server-side. Returns the credential id, or None when no key is available.

    Needed because #724 removed the platform model key: on a stack running a real extractor an org
    with no designated credential fails every ingest closed. A key-free stack (the compose default
    ``KGS_EXTRACTOR=null``, which is what CI runs) never reaches a model, so this is a no-op there.
    """
    if not _MODEL_KEY:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    created = httpx.post(
        f"{GATEWAY}/credentials/",
        headers=headers,
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": "e2e org model key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _MODEL_KEY},
        },
        timeout=20.0,
    )
    assert created.status_code == 201, f"credential create failed: {created.text}"
    credential_id = created.json()["id"]
    designated = httpx.put(
        f"{GATEWAY}/credentials/{credential_id}",
        headers=headers,
        json={
            "id": credential_id,
            "user_id": user_id,
            "tool_id": str(uuid.uuid4()),
            "provider": "openrouter",
            "cred_type": "api_key",
            "default_for": "model",
        },
        timeout=20.0,
    )
    assert designated.status_code == 200, f"designate default failed: {designated.text}"
    return str(credential_id)


@pytest.fixture
def register() -> Callable[..., dict]:
    """Factory: register a fresh user through the gateway → {token, org_id, user_id, email}.

    ``with_model_credential=True`` also designates the caller's key as the org's default model
    credential (#724), which a test needs only when its ingest reaches a REAL extractor. It is
    opt-in rather than automatic because each registration would otherwise cost two extra gateway
    calls, and across the suite that trips the edge per-IP rate limiter (a 429 on register itself).
    ``model_credential_id`` is None when not requested, or when no key is available.
    """

    def _register(full_name: str = "E2E User", *, with_model_credential: bool = False) -> dict:
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
            "model_credential_id": (
                _designate_org_model_credential(token, me["id"]) if with_model_credential else None
            ),
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
