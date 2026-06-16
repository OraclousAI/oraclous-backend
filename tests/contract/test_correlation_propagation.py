"""Contract: the end-to-end correlation id propagates gateway → upstream (WP-6, ORAA-4 A5).

Two halves:

* the gateway's ``forward_request_headers`` includes EXACTLY ONE ``x-request-id`` (the gateway's
  server-minted id) and DROPS any client-supplied copy (anti-forge — mirroring how the trust
  headers are handled);
* the shared ``oraclous_telemetry`` correlation middleware + structured-logging config make an
  upstream's log line carry that same id (the structured-log assertion from the WP-6 spec).

Marked ``contract`` (cross-service interface shape) + ``security`` (a client must never be able to
forge the correlation handle).
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_telemetry import (
    CorrelationIdMiddleware,
    bind_organisation_id,
    get_request_id,
    reset_organisation_id,
)

pytestmark = [pytest.mark.contract, pytest.mark.security]


def _request_id_values(headers: list[tuple[bytes, bytes]]) -> list[bytes]:
    return [value for key, value in headers if key.decode("latin-1").lower() == "x-request-id"]


# --- gateway → upstream header set -----------------------------------------


def test_forward_includes_exactly_one_request_id() -> None:
    from oraclous_application_gateway_service.services.proxy_service import forward_request_headers

    out = forward_request_headers(
        [(b"accept", b"application/json")],
        None,
        internal_key="k",
        request_id="req_abc123",
    )
    values = _request_id_values(out)
    assert values == [b"req_abc123"], values


def test_forward_drops_client_supplied_request_id() -> None:
    from oraclous_application_gateway_service.services.proxy_service import forward_request_headers

    out = forward_request_headers(
        [(b"x-request-id", b"req_CLIENT_FORGED"), (b"accept", b"application/json")],
        None,
        internal_key="k",
        request_id="req_gateway_minted",
    )
    values = _request_id_values(out)
    # Exactly one — and it is the gateway's, never the client's forged copy.
    assert values == [b"req_gateway_minted"], values


def test_forward_drops_client_request_id_on_authenticated_path() -> None:
    from oraclous_application_gateway_service.services.proxy_service import forward_request_headers

    principal = Principal(
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
        organisation_id=uuid.uuid4(),
    )
    out = forward_request_headers(
        [(b"x-request-id", b"req_CLIENT_FORGED")],
        principal,
        internal_key="k",
        request_id="req_gateway_minted",
    )
    assert _request_id_values(out) == [b"req_gateway_minted"]


def test_forward_omits_request_id_when_none() -> None:
    # Backward-compatible: no request_id given → no x-request-id forwarded.
    from oraclous_application_gateway_service.services.proxy_service import forward_request_headers

    out = forward_request_headers([(b"accept", b"application/json")], None, internal_key="k")
    assert _request_id_values(out) == []


# --- the upstream's structured log carries the same id ---------------------


def test_upstream_log_line_carries_forwarded_request_id() -> None:
    """Simulate the upstream: the correlation middleware binds the forwarded id, and a log line
    emitted while handling the request carries it through the shared structured-logging config.

    We attach the shared JSON formatter + correlation filter to a capture handler (rather than rely
    on caplog, which does not see a ``dictConfig``-reconfigured root) and assert the emitted line.
    """
    from oraclous_telemetry.logging_config import CorrelationFilter, JsonFormatter

    forwarded_id = "req_gateway_minted_xyz"
    emitted: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationFilter())
    logger = logging.getLogger("upstream.handler.test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    captured: dict[str, str] = {}

    async def app(scope, receive, send):  # minimal ASGI app standing in for an upstream
        logger.info("handling request")  # the bound id must reach this line
        captured["bound"] = get_request_id()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = CorrelationIdMiddleware(app)
    scope = {"type": "http", "headers": [(b"x-request-id", forwarded_id.encode())]}
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    import asyncio

    try:
        asyncio.run(middleware(scope, receive, send))
    finally:
        logger.removeHandler(handler)

    # The id was bound for the request...
    assert captured["bound"] == forwarded_id
    # ...the structured log line carries the same id...
    assert emitted, "expected the upstream handler log line"
    assert json.loads(emitted[0])["request_id"] == forwarded_id
    # ...and the response re-emits exactly one x-request-id with that value.
    start = next(m for m in sent if m["type"] == "http.response.start")
    rid_headers = [v for k, v in start["headers"] if k.lower() == b"x-request-id"]
    assert rid_headers == [forwarded_id.encode()]
    # ...and the id is reset after the request (no leak into the next pooled request).
    assert get_request_id() == ""


def test_json_formatter_emits_request_and_org_ids() -> None:
    """The JSON formatter serialises the bound request_id + organisation_id onto the record."""
    from oraclous_telemetry.logging_config import CorrelationFilter, JsonFormatter

    org_token = bind_organisation_id("org-123")
    try:
        record = logging.LogRecord(
            name="svc",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        CorrelationFilter().filter(record)
        line = JsonFormatter().format(record)
        payload = json.loads(line)
    finally:
        reset_organisation_id(org_token)

    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["organisation_id"] == "org-123"
    # request_id was not bound here, so it is omitted (not an empty string).
    assert "request_id" not in payload
