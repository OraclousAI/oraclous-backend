"""Unit: a coded registry failure reaches the member as a code, not a bare HTTP status (#692).

Team run `72ca031c-4226-41d7-8835-d70cf830b4ca`, member `Fetcher`, six identical failures:

    POST /internal/resolve-credential            → 404   (credential-broker)
    POST /api/v1/instances/40f596f9-.../execute  → 409   (capability-registry)
    tool step detail: {"error": "RegistryError", "detail": "POST /api/v1/instances/… → 409"}

The capability-registry already answers that 409 with a typed body — `ExecutionNotReadyError` puts
`error_code` next to the detail, and a deleted credential yields `credential_not_found`. The code is
lost one hop later: `RegistryClient._json` deliberately discards the upstream body (leak discipline,
ADR-042) and keeps only the method, path and status. So a missing credential and a genuine conflict
arrive at the member as the same bare `409`, and it cannot tell "reconnect this tool" from "retry".

`error_code` is a **registry-generated token from a closed vocabulary**, not customer content, so it
is the one field that can cross the leak boundary. These tests pin that: the code is carried, a hint
owned by the runtime makes it readable, and everything else in the body still stays out.

The step detail the model reads is `json.dumps({"error": type(exc).__name__, "detail": str(exc)})`
(`domain/loop/tool_use.py`), so pinning `str(exc)` pins #692 AC4.

RED until the #692 [impl] lands.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from oraclous_harness_runtime_service.services.registry_client import RegistryClient, RegistryError

pytestmark = pytest.mark.unit

_INSTANCE = uuid.UUID("40f596f9-4aed-4aed-9d7d-b830cb2e8b70")
_CUSTOMER_TEXT = "the reviewer said the manuscript's third act drags"


def _client(status_code: int, body: dict) -> RegistryClient:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(status_code, json=body)

    return RegistryClient("http://registry", headers={}, transport=httpx.MockTransport(handler))


async def _execute(status_code: int, body: dict) -> RegistryError:
    with pytest.raises(RegistryError) as ei:
        await _client(status_code, body).execute(_INSTANCE, {"operation": "read"})
    return ei.value


async def test_the_registry_error_code_is_carried_not_dropped() -> None:
    exc = await _execute(
        409, {"detail": "credential not found", "error_code": "credential_not_found"}
    )
    assert exc.error_code == "credential_not_found"


async def test_the_member_message_names_the_credential_not_only_the_status() -> None:
    """#692 AC4 — `409` is unactionable. The detail must say what to do about it."""
    exc = await _execute(
        409, {"detail": "credential not found", "error_code": "credential_not_found"}
    )
    message = str(exc).lower()
    assert "credential" in message
    assert "reconnect" in message
    assert "409" in message  # the status is still there for an operator reading a trace


async def test_a_genuine_conflict_is_distinguishable_from_a_missing_credential() -> None:
    """Both are 409 today. Two different codes must produce two different messages."""
    missing = await _execute(409, {"error_code": "credential_not_found"})
    pending = await _execute(409, {"error_code": "pending_approval"})
    assert missing.error_code != pending.error_code
    assert str(missing) != str(pending)


@pytest.mark.security
async def test_the_upstream_body_is_still_not_echoed() -> None:
    """Leak discipline holds: only the code crosses, never the free-text detail (it may quote the
    customer's own input or output — CLAUDE.md §11 / the ADR-042 leak class)."""
    exc = await _execute(409, {"detail": _CUSTOMER_TEXT, "error_code": "credential_not_found"})
    assert _CUSTOMER_TEXT not in str(exc)


@pytest.mark.security
@pytest.mark.parametrize(
    "hostile_code",
    [
        _CUSTOMER_TEXT,  # free text smuggled through the code field
        "x" * 300,  # unbounded length
        "code\nwith\nnewlines",
        {"nested": "object"},
        12345,
    ],
)
async def test_only_a_well_formed_code_token_crosses_the_boundary(hostile_code: object) -> None:
    """The registry is trusted, but the code is echoed into a model-visible string, so it is
    accepted only as a bounded `[a-z0-9_]` token — never as an arbitrary relay channel."""
    exc = await _execute(409, {"error_code": hostile_code})
    assert exc.error_code is None
    assert str(hostile_code)[:40] not in str(exc)


async def test_a_body_with_no_error_code_still_reports_the_status() -> None:
    exc = await _execute(503, {"detail": "upstream down"})
    assert exc.error_code is None
    assert "503" in str(exc)
    assert "/api/v1/instances" in str(exc)


async def test_a_non_json_error_body_does_not_break_the_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(502, text="<html>bad gateway</html>")

    client = RegistryClient("http://registry", headers={}, transport=httpx.MockTransport(handler))
    with pytest.raises(RegistryError) as ei:
        await client.execute(_INSTANCE, {"operation": "read"})
    assert ei.value.error_code is None
    assert "502" in str(ei.value)
    assert "html" not in str(ei.value).lower()
