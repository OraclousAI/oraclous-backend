"""MCP Streamable-HTTP client protocol (domain layer, #541 — protocol rev 2025-03-26).

The registry is an MCP *client*. A real hosted MCP server (e.g. GitHub's remote MCP) is NOT the
stateless single-POST JSON-RPC subset the executor first shipped — it requires the full
**Streamable HTTP** flow: an ``initialize`` handshake, a session id echoed on every subsequent
request (``Mcp-Session-Id``), and responses that may be **SSE-framed**
(``text/event-stream``: ``event:``/``data:`` lines) rather than a single JSON body. This module is
that flow, reused by the invoke executor (``mcp.py``) and the import discovery seam.

Two security controls are threaded through EVERY request (shared with the rest of the registry):

* **SSRF egress + #492 IP-pinning** — the caller resolves+vets the URL once via ``egress_allowed``
  + passes the pinned IP; each request DIALS that IP (Host + TLS SNI kept as the name), closing the
  DNS-rebinding TOCTOU. The SSE/streaming connection uses the vetted IP, never a re-resolution.
* **Broker-held Bearer** — an optional credential (resolved by the broker, never stored) is sent as
  a Bearer on the handshake AND the calls, so a hosted server that 401s discovery is reachable.

The raw MCP/transport error is NEVER surfaced (a generic ``McpProtocolError`` + a coarse code).
Stdio/local-subprocess transport is out of scope (parked/local; ADR-038 — cloud-first).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from oraclous_capability_registry_service.domain.egress import pinned_request

PROTOCOL_VERSION = "2025-03-26"
# advertise BOTH so a server may answer with a single JSON body or an SSE stream (spec: the client
# MUST accept both on a Streamable-HTTP POST).
_ACCEPT = "application/json, text/event-stream"
_CLIENT_INFO = {"name": "oraclous-registry", "version": "1.0"}
_SESSION_HEADER = "mcp-session-id"
# a hostile/broken MCP server must not OOM the importer with an unbounded body (review M3); a real
# tools/list is kilobytes. Reject anything past this cap fail-closed, before parsing.
_MAX_BODY_BYTES = 4 * 1024 * 1024


class McpProtocolError(Exception):
    """The MCP server could not be handshaken / called — generic; no raw upstream detail leaks.
    ``code`` is the coarse error_type; ``rpc_code`` is the (safe, numeric) JSON-RPC error code when
    the failure was a tool error, surfaced in metadata — never the raw upstream message."""

    def __init__(self, message: str, *, code: str, rpc_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.rpc_code = rpc_code


def _guard_body_size(resp: httpx.Response) -> None:
    """Fail-closed if the server's declared or actual body exceeds the cap — an oversized body is a
    hostile/broken server, never a real MCP message (review M3)."""
    declared = resp.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_BODY_BYTES:
                raise McpProtocolError("the MCP server body is too large", code="MCP_BAD_RESPONSE")
        except ValueError:  # a non-numeric content-length is itself malformed → fail-closed
            raise McpProtocolError(
                "the MCP server sent a malformed content-length", code="MCP_BAD_RESPONSE"
            ) from None
    if len(resp.content) > _MAX_BODY_BYTES:  # backstop for a chunked/undeclared body
        raise McpProtocolError("the MCP server body is too large", code="MCP_BAD_RESPONSE")


def _parse_message(resp: httpx.Response) -> dict[str, Any]:
    """Parse a Streamable-HTTP response body into the JSON-RPC message — a single JSON object OR the
    first SSE ``data:`` frame that carries a JSON-RPC ``result``/``error``. Fail-closed on anything
    that is not a JSON-RPC object (a hostile server can send any bytes)."""
    _guard_body_size(resp)
    ctype = resp.headers.get("content-type", "").lower()
    if "text/event-stream" in ctype:
        for raw in resp.text.splitlines():
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[len("data:") :].strip())
            except ValueError:
                continue
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return msg
        raise McpProtocolError("no JSON-RPC message in the SSE stream", code="MCP_BAD_RESPONSE")
    try:
        body = resp.json()
    except ValueError as exc:
        raise McpProtocolError(
            "the MCP server returned a non-JSON body", code="MCP_BAD_RESPONSE"
        ) from exc
    if not isinstance(body, dict):
        raise McpProtocolError("the MCP server returned a malformed body", code="MCP_BAD_RESPONSE")
    return body


def _require_ok(resp: httpx.Response) -> None:
    if resp.status_code not in (200, 202):  # 202 = an accepted notification (no body)
        raise McpProtocolError(f"the MCP server returned {resp.status_code}", code="MCP_HTTP_ERROR")


class McpSession:
    """One initialized Streamable-HTTP session against a single (pinned) MCP server URL."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        server_url: str,
        pinned_ip: str,
        bearer: str | None = None,
    ) -> None:
        self._client = client
        self._url = server_url
        self._pinned_ip = pinned_ip
        self._bearer = bearer
        self._session_id: str | None = None
        self._id = 0

    def _headers(self, *, with_session: bool) -> dict[str, str]:
        _target, host_headers, _ext = pinned_request(self._url, self._pinned_ip)
        headers = {"content-type": "application/json", "accept": _ACCEPT, **host_headers}
        if self._bearer:
            headers["authorization"] = f"Bearer {self._bearer}"
        if with_session and self._session_id:
            headers[_SESSION_HEADER] = self._session_id
        return headers

    async def _post(self, payload: dict[str, Any], *, with_session: bool) -> httpx.Response:
        target, _h, extensions = pinned_request(self._url, self._pinned_ip)
        try:
            return await self._client.post(
                target,
                json=payload,
                headers=self._headers(with_session=with_session),
                extensions=extensions,
            )
        except httpx.HTTPError as exc:
            raise McpProtocolError(
                "the MCP server could not be reached", code="MCP_UNREACHABLE"
            ) from exc

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def initialize(self) -> None:
        """The handshake: initialize → capture ``Mcp-Session-Id`` → notifications/initialized.
        A sessionless server (no header) still works — the session id is simply not echoed."""
        resp = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
            },
            with_session=False,
        )
        _require_ok(resp)
        msg = _parse_message(resp)
        if msg.get("error"):
            raise McpProtocolError("the MCP server rejected initialize", code="MCP_HANDSHAKE_ERROR")
        self._session_id = resp.headers.get(_SESSION_HEADER)
        # notifications/initialized — a JSON-RPC NOTIFICATION (no id, no response body expected).
        notif = await self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, with_session=True
        )
        _require_ok(notif)

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue a request WITHIN the initialized session; return the JSON-RPC ``result`` object.
        Raises on a JSON-RPC error or a non-object result (no raw upstream message ever leaks)."""
        resp = await self._post(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params},
            with_session=True,
        )
        _require_ok(resp)
        msg = _parse_message(resp)
        if msg.get("error"):
            err = msg.get("error")
            rpc_code = err.get("code") if isinstance(err, dict) else None
            raise McpProtocolError(
                "the MCP tool returned an error",
                code="MCP_TOOL_ERROR",
                rpc_code=rpc_code if isinstance(rpc_code, int) else None,
            )
        result = msg.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError(
                "the MCP server returned a malformed result", code="MCP_BAD_RESPONSE"
            )
        return result
