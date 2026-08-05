"""Unit: POST /api/v1/tools/import-mcp answers a DIFFERENT status per failure cause (#715).

``test_mcp_import_failure_signal`` pins that the service now knows WHY an import failed. This pins
what the route does with that knowledge, because the status the registry returns is what decides the
ORA-56 code the console finally sees: the gateway proxies ``/api/v1/tools`` and maps the upstream
status straight through ``status_to_code`` (``proxy_routes.py``), with two shape-driven exits: a 422
carrying FastAPI-shaped ``detail`` becomes ``VALIDATION_FAILED``, and a 409 carrying a top-level
``needs_credential`` becomes ``CREDENTIALS_REQUIRED``.

So the registry's job is to answer with the status AND the body shape those exits key on:

| cause                                 | status | body               | console code         |
| ------------------------------------- | ------ | ------------------ | -------------------- |
| the MCP server refused authentication | 409    | needs_credential   | CREDENTIALS_REQUIRED |
| the MCP host could not be reached     | 502    | -                  | SERVICE_UNAVAILABLE  |
| the MCP server timed out              | 504    | -                  | GATEWAY_TIMEOUT      |
| any other MCP protocol failure        | 502    | -                  | SERVICE_UNAVAILABLE  |
| the credential could not be resolved  | 422    | detail[].type      | VALIDATION_FAILED    |
| the credential is not an api_key      | 422    | detail[].type      | VALIDATION_FAILED    |
| the URL is not an allowed target      | 422    | unchanged          | unchanged            |

401 and 403 are deliberately NOT used for the MCP server's refusal: they map to ``UNAUTHENTICATED``
and ``UNAUTHORIZED``, which the console reads as the caller's own Oraclous session being bad. That
would trade one wrong message for another.

The exact codes are under Contract review on #720; these tests pin the proposed mapping so the
decision lands on something concrete. The route is exercised through the real app (real dependency
graph, real exception handlers) with the import service overridden — no network, no Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_capability_registry_service.services.credential_client import (
    CredentialResolutionError,
    ResolvedCredential,
)
from oraclous_capability_registry_service.services.mcp_import_service import McpImportService

pytestmark = pytest.mark.unit

_INTERNAL_KEY = "dev-internal-key"
_ORG = "00000000-0000-0000-0000-00000000aaaa"
_PRINCIPAL = "00000000-0000-0000-0000-0000000000c5"
_PLATFORM_ORG = "00000000-0000-0000-0000-0000000000a0"
_PUB = "https://93.184.216.34/mcp"  # a literal PUBLIC ip → egress allowed without a DNS lookup
_BLOCKED = "http://169.254.169.254/"  # link-local → the SSRF guard rejects it before any call
# a fake upstream body carrying every shape that must not cross the boundary
_SECRET = "SECRET-internal-host-db.corp.internal-token-abc123"  # noqa: S105


class _FakeCaps:
    def __init__(self) -> None:
        self.created: list = []

    async def create(self, *, organisation_id, kind, descriptor, status="active"):  # noqa: ANN001, ANN202
        row = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            kind=kind,
            descriptor=descriptor,
            status=status,
            name=descriptor["metadata"]["name"],
            content_hash=None,
            created_at=None,
            updated_at=None,
        )
        self.created.append(row)
        return row


class _Broker:
    def __init__(self, *, payload: dict | None = None, raise_exc: Exception | None = None) -> None:
        self._payload = payload if payload is not None else {"api_key": "tok"}
        self._raise = raise_exc

    async def resolve(self, *, organisation_id, user_id, requirement, credential_id=None):  # noqa: ANN001, ANN202
        if self._raise is not None:
            raise self._raise
        return ResolvedCredential(credential_type="api_key", payload=self._payload)

    async def aclose(self) -> None:  # pragma: no cover
        return None


ClientFactory = Callable[..., AsyncClient]


@pytest.fixture
async def make_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ClientFactory]:
    """Build a client over the REAL app with only the import service's transport + broker faked, so
    the route, its exception handlers and the response serialisation are all the shipped ones."""
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", _INTERNAL_KEY)
    monkeypatch.setenv("AUTH_MODE", "gateway")
    monkeypatch.setenv("PLATFORM_ORG_ID", _PLATFORM_ORG)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused/unused")
    from oraclous_capability_registry_service.app.factory import create_app
    from oraclous_capability_registry_service.core.config import get_settings
    from oraclous_capability_registry_service.core.dependencies import get_mcp_import_service

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    opened: list[AsyncClient] = []
    caps = _FakeCaps()

    def _factory(handler: Callable, *, broker: _Broker | None = None) -> AsyncClient:
        app.dependency_overrides[get_mcp_import_service] = lambda: McpImportService(
            capabilities=caps, broker=broker, transport=httpx.MockTransport(handler)
        )
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test")
        client.caps = caps  # type: ignore[attr-defined]  # so a test can assert nothing was created
        opened.append(client)
        return client

    yield _factory
    for c in opened:
        await c.aclose()
    get_settings.cache_clear()


def _auth() -> dict:
    return {
        "X-Internal-Key": _INTERNAL_KEY,
        "X-Principal-Id": _PRINCIPAL,
        "X-Principal-Type": "user",
        "X-Organisation-Id": _ORG,
        "X-Principal-Org-Role": "admin",
    }


def _status(code: int, *, text: str = "") -> Callable:
    return lambda _r: httpx.Response(code, text=text)


def _raises(exc: Exception) -> Callable:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _tools(*names: str) -> Callable:
    return lambda _r: httpx.Response(200, json={"result": {"tools": [{"name": n} for n in names]}})


async def _post(
    client: AsyncClient, *, url: str = _PUB, credential_id: str | None = None
) -> httpx.Response:
    body: dict = {"server_url": url, "label": "srv"}
    if credential_id is not None:
        body["credential_id"] = credential_id
    return await client.post("/api/v1/tools/import-mcp", json=body, headers=_auth())


# ── the MCP server's own failures ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("refusal", [401, 403])
async def test_a_server_that_refuses_authentication_answers_409_needing_a_credential(
    make_client: ClientFactory, refusal: int
) -> None:
    """The gateway turns a 409 carrying a top-level ``needs_credential`` into CREDENTIALS_REQUIRED —
    the code the console already deep-links from. It must be at the TOP level of the body, not
    nested under ``detail``, which is where ``extract_needs_credential`` reads it."""
    resp = await _post(make_client(_status(refusal)))
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["needs_credential"] == {"requirement_id": "api_key", "provider": "mcp"}


async def test_an_unreachable_host_answers_502(make_client: ClientFactory) -> None:
    resp = await _post(make_client(_raises(httpx.ConnectError("boom"))))
    assert resp.status_code == 502, resp.text


async def test_a_timeout_answers_504(make_client: ClientFactory) -> None:
    """Distinct from 502 because the two need different things from the admin: a timeout is worth
    retrying, an unreachable host needs the address corrected."""
    resp = await _post(make_client(_raises(httpx.ReadTimeout("slow"))))
    assert resp.status_code == 504, resp.text


async def test_a_broken_server_answers_502(make_client: ClientFactory) -> None:
    resp = await _post(make_client(_status(500)))
    assert resp.status_code == 502, resp.text


async def test_an_auth_refusal_and_an_unreachable_host_do_not_share_a_status(
    make_client: ClientFactory,
) -> None:
    """#715 in one assertion. The reproduction case is a hosted server that answered promptly and
    refused; today it is reported identically to a host that never answered at all."""
    refused = await _post(make_client(_status(401)))
    unreachable = await _post(make_client(_raises(httpx.ConnectError("boom"))))
    assert refused.status_code != unreachable.status_code


# ── our own failures: the caller's credential_id ──────────────────────────────────────────────────


async def test_an_unresolvable_credential_answers_422_naming_the_field(
    make_client: ClientFactory,
) -> None:
    """A credential the broker cannot resolve is a problem with the request, not with the MCP
    server. The FastAPI-shaped ``detail`` is what the gateway turns into VALIDATION_FAILED with a
    ``credential_id`` field error."""
    broker = _Broker(raise_exc=CredentialResolutionError("no", error_code="not_found"))
    resp = await _post(make_client(_tools("t"), broker=broker), credential_id="cred-x")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail, resp.text
    assert detail[0]["loc"][-1] == "credential_id"
    assert detail[0]["type"] == "credential_unresolvable"


async def test_a_non_api_key_credential_answers_422_with_its_own_reason(
    make_client: ClientFactory,
) -> None:
    """Separable from an unresolvable one — the admin picked a real credential of the wrong kind,
    which is a different fix from picking a credential that no longer exists."""
    broker = _Broker(payload={"oauth_token": "t"})
    resp = await _post(make_client(_tools("t"), broker=broker), credential_id="cred-x")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail[0]["loc"][-1] == "credential_id"
    assert detail[0]["type"] == "credential_not_api_key"


async def test_the_two_credential_failures_are_separable_from_the_servers_refusal(
    make_client: ClientFactory,
) -> None:
    """The second acceptance criterion: our failure never looks like the MCP server's."""
    broker = _Broker(raise_exc=CredentialResolutionError("no", error_code="not_found"))
    ours = await _post(make_client(_tools("t"), broker=broker), credential_id="cred-x")
    theirs = await _post(make_client(_status(401)))
    assert ours.status_code != theirs.status_code


async def test_a_credential_failure_creates_nothing(make_client: ClientFactory) -> None:
    """Fail-closed (invariant 3.5): the typed failure must not open an anonymous fallback path."""
    broker = _Broker(raise_exc=CredentialResolutionError("no", error_code="not_found"))
    client = make_client(_tools("t"), broker=broker)
    assert (await _post(client, credential_id="cred-x")).status_code == 422
    assert client.caps.created == []  # type: ignore[attr-defined]


# ── what must not change ──────────────────────────────────────────────────────────────────────────


async def test_a_blocked_url_still_answers_422(make_client: ClientFactory) -> None:
    """The SSRF guard's existing answer is already distinct and already correct — a regression
    guard, so the new mapping does not absorb it into the server-failure branch."""
    resp = await _post(make_client(_tools("t")), url=_BLOCKED)
    assert resp.status_code == 422, resp.text


async def test_a_successful_import_is_unchanged(make_client: ClientFactory) -> None:
    resp = await _post(make_client(_tools("do_a", "do_b")))
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["imported"]) == 2


async def test_a_successful_authd_import_is_unchanged(make_client: ClientFactory) -> None:
    """The FE#186 path that already works: a hosted server plus a good credential still imports."""
    resp = await _post(make_client(_tools("do_a"), broker=_Broker()), credential_id="cred-ok")
    assert resp.status_code == 201, resp.text
    assert resp.json()["imported"][0]["descriptor"]["spec"]["credential_id"] == "cred-ok"


@pytest.mark.parametrize("upstream_status", [401, 403, 500])
async def test_no_upstream_body_reaches_the_caller(
    make_client: ClientFactory, upstream_status: int
) -> None:
    """The richer signal must not widen the leak surface: a coarse cause is all that crosses."""
    resp = await _post(make_client(_status(upstream_status, text=_SECRET)))
    assert "SECRET" not in resp.text
    assert "corp.internal" not in resp.text
    assert "abc123" not in resp.text
