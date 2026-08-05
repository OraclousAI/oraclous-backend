"""E2E: an MCP import failure tells the console WHICH failure it was (#715).

Drives the DEPLOYED stack through the application-gateway (`:8006`) with a real registration and a
real JWT. The console branches on the ORA-56 ``code``, never on ``message``
(``interface-contracts.md`` §3), so what matters here is that the envelope's ``code`` differs by
cause — an MCP server refusing authentication is not the Oraclous platform being unavailable.

Three causes are reachable without anyone's private token, so all three run keyless:

1. a real hosted MCP server that refuses an unauthenticated request (``api.githubcopilot.com``);
2. a host that is routable but has nothing listening;
3. a ``credential_id`` that does not resolve for this fresh org.

The fourth DoD case — a hosted server plus a WORKING credential still imports — needs a real GitHub
token, so it is proven by hand against the local stack and pasted into the PR rather than run here
(Rule 8: a real credential is asked of the human, never faked).

Case 1 needs outbound internet from the registry container. When it is unavailable the request comes
back as an unreachable host instead of a refusal, which would make the test assert nothing useful —
so that one case is skipped on a non-refusal answer, while cases 2 and 3 (both purely local) always
run. A skip of a single case is visible in the report; it never turns a failure green.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = pytest.mark.e2e

# A REAL hosted MCP server that answers promptly and refuses an unauthenticated request — the exact
# reproduction in #715. Not a stub: the point is that a live refusal is reported as a refusal.
_HOSTED_MCP = "https://api.githubcopilot.com/mcp/"
# Routable, public, and nothing listens on this port — an unreachable host, not a refusal.
_DEAD_HOST = "http://93.184.216.34:9/mcp"


def _import(c: httpx.Client, url: str, *, credential_id: str | None = None) -> httpx.Response:
    body: dict = {"server_url": url, "label": "e2e-mcp"}
    if credential_id is not None:
        body["credential_id"] = credential_id
    return c.post("/api/v1/tools/import-mcp", json=body)


def _envelope(resp: httpx.Response) -> dict:
    """The ORA-56 error envelope the console reads — the inner object under ``error``."""
    body = resp.json()
    err = body.get("error") if isinstance(body, dict) else None
    assert isinstance(err, dict), f"no ORA-56 envelope in {resp.status_code}: {resp.text}"
    assert err.get("code"), f"no code in {resp.text}"
    assert err.get("requestId"), f"no requestId in {resp.text}"
    return err


def test_an_mcp_auth_refusal_is_not_reported_as_the_platform_being_unavailable(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register("MCP Import E2E")
    c = gateway_client(user["token"])

    refused = _import(c, _HOSTED_MCP)
    if refused.status_code == 502:
        pytest.skip("no outbound internet from the stack — the hosted server never answered")
    body = _envelope(refused)
    assert body["code"] == "CREDENTIALS_REQUIRED", body
    assert refused.status_code == 409, refused.text
    # the token the console deep-links its credential-onboarding from
    assert body["needs_credential"]["provider"] == "mcp", body


def test_an_unreachable_host_and_an_auth_refusal_carry_different_codes(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """#715 in one test. Both used to be 502 SERVICE_UNAVAILABLE, whose curated message is 'The
    service is temporarily unavailable' — a sentence about Oraclous, untrue for a server that
    answered promptly and refused."""
    user = register("MCP Import E2E")
    c = gateway_client(user["token"])

    unreachable = _envelope(_import(c, _DEAD_HOST))
    assert unreachable["code"] in ("SERVICE_UNAVAILABLE", "GATEWAY_TIMEOUT"), unreachable

    refused = _import(c, _HOSTED_MCP)
    if refused.status_code == 502:
        pytest.skip("no outbound internet from the stack — the hosted server never answered")
    assert _envelope(refused)["code"] != unreachable["code"]


def test_a_credential_that_does_not_resolve_is_reported_as_our_failure_not_the_servers(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """A credential id belonging to nobody is a problem with the request, so it must not look like
    the MCP server misbehaving. Fully local — the import fails before any egress."""
    user = register("MCP Import E2E")
    c = gateway_client(user["token"])

    resp = _import(c, _HOSTED_MCP, credential_id="00000000-0000-0000-0000-0000000000ff")
    body = _envelope(resp)
    assert resp.status_code == 422, resp.text
    assert body["code"] == "VALIDATION_FAILED", body
    fields = {d["field"]: d["issue"] for d in body.get("details", [])}
    assert fields.get("credential_id") == "CREDENTIAL_UNRESOLVABLE", body


def test_no_upstream_detail_reaches_the_caller(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The richer signal must not widen the leak surface — the envelope carries a coarse code, never
    the MCP server's own words or any internal host."""
    user = register("MCP Import E2E")
    c = gateway_client(user["token"])

    text = _import(c, _HOSTED_MCP).text
    for leak in ("githubcopilot", "Bad credentials", ".internal", "capability-registry"):
        assert leak not in text, f"{leak!r} leaked: {text}"
