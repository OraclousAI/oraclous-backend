"""Unit: the MCP import preserves WHY it failed (#715).

Every import failure currently arrives at the caller as the same ``McpImportError`` with a constant
message, so nothing downstream can tell an MCP server's auth refusal from an unreachable host from a
credential we could not resolve. The distinction already exists inside ``McpProtocolError`` (a
coarse ``code``, and the upstream status formatted into the message) and is thrown away twice: once
when ``_list_tools`` re-raises, and again when the route flattens everything into one 502.

These tests pin the information-preservation half — the failure reason survives from the protocol
layer to the service boundary as ATTRIBUTES, and the four causes become distinct exception types.
The HTTP mapping built on top of them is pinned in the sibling
``test_mcp_import_failure_codes.py``.

The leak posture does not move: a coarse code plus the numeric upstream status is the whole of the
new signal. No raw upstream message or body may ride along (``McpProtocolError``'s own contract).

The not-yet-built exception types are imported FUNCTION-LOCALLY on purpose (see
``.claude/rules/tests-seam-imports.md``): a module-level import would abort collection for the whole
suite until the ``[impl]`` lands, instead of failing only these tests.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from oraclous_capability_registry_service.domain.connectors.mcp_protocol import (
    McpProtocolError,
    McpSession,
)
from oraclous_capability_registry_service.services.credential_client import (
    CredentialResolutionError,
    ResolvedCredential,
)
from oraclous_capability_registry_service.services.mcp_import_service import (
    McpImportError,
    McpImportService,
)

pytestmark = pytest.mark.unit

_PUB = "https://93.184.216.34/mcp"  # a literal PUBLIC ip → egress allowed without a DNS lookup
# a fake upstream body carrying every shape that must not cross the boundary
_SECRET = "SECRET-internal-host-db.corp.internal-token-abc123"  # noqa: S105


class _FakeCaps:
    def __init__(self) -> None:
        self.created: list = []

    async def create(self, *, organisation_id, kind, descriptor, status="active"):  # noqa: ANN001, ANN202
        row = SimpleNamespace(id=uuid.uuid4(), descriptor=descriptor, status=status)
        self.created.append(row)
        return row


class _Broker:
    """A broker that either resolves to a payload or raises, so both credential failures are
    reachable from the service boundary without a real credential-broker."""

    def __init__(self, *, payload: dict | None = None, raise_exc: Exception | None = None) -> None:
        self._payload = payload if payload is not None else {"api_key": "tok"}
        self._raise = raise_exc

    async def resolve(self, *, organisation_id, user_id, requirement, credential_id=None):  # noqa: ANN001, ANN202
        if self._raise is not None:
            raise self._raise
        return ResolvedCredential(credential_type="api_key", payload=self._payload)

    async def aclose(self) -> None:  # pragma: no cover
        return None


def _svc(handler, caps: _FakeCaps | None = None, broker: _Broker | None = None):  # noqa: ANN001, ANN202
    return McpImportService(
        capabilities=caps or _FakeCaps(),
        broker=broker,
        transport=httpx.MockTransport(handler),
    )


async def _import(handler, caps: _FakeCaps | None = None, **kw):  # noqa: ANN001, ANN202
    """Drive one import and return whatever it raised, so each test can assert on the type."""
    return await _svc(handler, caps=caps, broker=kw.pop("broker", None)).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="srv", **kw
    )


def _status(code: int, *, text: str = "") -> object:
    return lambda _r: httpx.Response(code, text=text)


def _raises(exc: Exception) -> object:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# ── the protocol layer: the upstream status becomes a real attribute ──────────────────────────────
#
# ``_require_ok`` already knows the status; today it only formats it into the message, where nothing
# can read it back out without parsing prose.


async def test_an_http_failure_carries_the_upstream_status_as_an_attribute() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_status(401))) as client:
        session = McpSession(client, server_url=_PUB, pinned_ip="93.184.216.34")
        with pytest.raises(McpProtocolError) as exc:
            await session.initialize()
    assert exc.value.code == "MCP_HTTP_ERROR"
    assert exc.value.status == 401


async def test_an_unreachable_server_reports_no_upstream_status() -> None:
    """Nothing answered, so there is no status to report — None, never a guessed one."""
    transport = httpx.MockTransport(_raises(httpx.ConnectError("boom")))
    async with httpx.AsyncClient(transport=transport) as client:
        session = McpSession(client, server_url=_PUB, pinned_ip="93.184.216.34")
        with pytest.raises(McpProtocolError) as exc:
            await session.initialize()
    assert exc.value.code == "MCP_UNREACHABLE"
    assert exc.value.status is None


async def test_a_timeout_is_coded_apart_from_an_unreachable_host() -> None:
    """A server that is up but slow and a host that is not there are different problems for the
    admin: one is worth retrying, the other needs the address fixed."""
    transport = httpx.MockTransport(_raises(httpx.ConnectTimeout("slow")))
    async with httpx.AsyncClient(transport=transport) as client:
        session = McpSession(client, server_url=_PUB, pinned_ip="93.184.216.34")
        with pytest.raises(McpProtocolError) as exc:
            await session.initialize()
    assert exc.value.code == "MCP_TIMEOUT"


# ── the service boundary: the reason survives the re-raise ────────────────────────────────────────


async def test_the_import_error_keeps_the_protocol_code_and_status() -> None:
    with pytest.raises(McpImportError) as exc:
        await _import(_status(500))
    assert exc.value.code == "MCP_HTTP_ERROR"
    assert exc.value.status == 500


@pytest.mark.parametrize("refusal", [401, 403])
async def test_a_server_that_refuses_authentication_is_its_own_error_type(refusal: int) -> None:
    """The reproduction case: ``https://api.githubcopilot.com/mcp/`` answers promptly and refuses an
    unauthenticated request. That is not the platform being unavailable."""
    from oraclous_capability_registry_service.services.mcp_import_service import (
        McpServerRefusedAuth,
    )

    with pytest.raises(McpServerRefusedAuth) as exc:
        await _import(_status(refusal))
    assert exc.value.status == refusal


async def test_an_unreachable_host_is_not_reported_as_an_auth_refusal() -> None:
    """The whole point of #715: these two must not collapse into one signal."""
    from oraclous_capability_registry_service.services.mcp_import_service import (
        McpServerRefusedAuth,
    )

    with pytest.raises(McpImportError) as exc:
        await _import(_raises(httpx.ConnectError("boom")))
    assert not isinstance(exc.value, McpServerRefusedAuth)
    assert exc.value.code == "MCP_UNREACHABLE"


async def test_a_timeout_is_its_own_error_type_at_the_service_boundary() -> None:
    from oraclous_capability_registry_service.services.mcp_import_service import McpServerTimeout

    with pytest.raises(McpServerTimeout):
        await _import(_raises(httpx.ReadTimeout("slow")))


async def test_a_server_error_stays_a_plain_import_error() -> None:
    """A 500 from the MCP server is the server being broken — not an auth problem, not ours."""
    from oraclous_capability_registry_service.services.mcp_import_service import (
        McpCredentialError,
        McpServerRefusedAuth,
        McpServerTimeout,
    )

    with pytest.raises(McpImportError) as exc:
        await _import(_status(500))
    assert not isinstance(exc.value, (McpServerRefusedAuth, McpServerTimeout, McpCredentialError))


# ── the credential failures are OURS, not the server's ────────────────────────────────────────────


async def test_an_unresolvable_credential_is_its_own_error_type() -> None:
    from oraclous_capability_registry_service.services.mcp_import_service import (
        McpCredentialUnresolvable,
        McpServerRefusedAuth,
    )

    broker = _Broker(raise_exc=CredentialResolutionError("no", error_code="not_found"))
    with pytest.raises(McpCredentialUnresolvable) as exc:
        await _import(_status(200), broker=broker, user_id=uuid.uuid4(), credential_id="cred-x")
    assert not isinstance(exc.value, McpServerRefusedAuth)  # not the MCP server's fault


async def test_a_credential_that_is_not_an_api_key_is_its_own_error_type() -> None:
    from oraclous_capability_registry_service.services.mcp_import_service import (
        McpCredentialNotApiKey,
        McpCredentialUnresolvable,
    )

    broker = _Broker(payload={"oauth_token": "t"})  # resolved fine, wrong shape for a Bearer
    with pytest.raises(McpCredentialNotApiKey) as exc:
        await _import(_status(200), broker=broker, user_id=uuid.uuid4(), credential_id="cred-x")
    assert not isinstance(exc.value, McpCredentialUnresolvable)  # separable from each other


async def test_a_missing_broker_is_still_a_credential_failure() -> None:
    """An auth'd import with no configured broker cannot be the MCP server's fault either."""
    from oraclous_capability_registry_service.services.mcp_import_service import McpCredentialError

    with pytest.raises(McpCredentialError):
        await _import(_status(200), user_id=uuid.uuid4(), credential_id="cred-x")


@pytest.mark.parametrize(
    "broker",
    [
        _Broker(raise_exc=CredentialResolutionError("no", error_code="not_found")),
        _Broker(payload={"oauth_token": "t"}),
    ],
)
async def test_a_credential_failure_still_creates_nothing(broker: _Broker) -> None:
    """Fail-closed (invariant 3.5) survives the new typing: a credential we cannot use never
    degrades into an anonymous import of the server."""
    caps = _FakeCaps()
    tools = lambda _r: httpx.Response(200, json={"result": {"tools": [{"name": "t"}]}})  # noqa: E731
    with pytest.raises(McpImportError):
        await _import(tools, caps=caps, broker=broker, user_id=uuid.uuid4(), credential_id="c")
    assert caps.created == []


# ── the leak posture is unchanged ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("upstream_status", [401, 403, 500])
async def test_no_upstream_body_rides_along_with_the_new_signal(upstream_status: int) -> None:
    """A coarse code plus the numeric status is the whole of the new signal. The server's body is
    untrusted and may name internal hosts — it must reach neither the message nor any attribute."""
    with pytest.raises(McpImportError) as exc:
        await _import(_status(upstream_status, text=_SECRET))
    surfaced = " ".join(
        str(v) for v in (str(exc.value), exc.value.code, exc.value.status, exc.value.args)
    )
    assert "SECRET" not in surfaced
    assert "corp.internal" not in surfaced
    assert "abc123" not in surfaced
