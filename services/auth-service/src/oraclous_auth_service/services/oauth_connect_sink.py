"""Broker connect-sink HTTP client (ORAA-4 §21 services layer — internal integration).

Implements the ``ConnectSink`` port against the credential-broker's internal G1 bridge
(``POST /internal/oauth-connect``, X-Internal-Key gated). The auth-service connect flow exchanges
the provider code and hands the resulting token here; the broker persists it as a resolvable
oauth credential. CI exercises the connect flow with a fake sink (no live broker); this real client
is used in the running stack.

A broker failure (non-2xx, unreachable, or timeout) is mapped to ``ConnectSinkError`` so the route
returns a deliberate 502 instead of leaking an unmapped 500 — mirroring ``oauth_provider_client``'s
discipline. The broker response body is never propagated to the caller (it is server-internal).
"""

from __future__ import annotations

import httpx

_TIMEOUT = httpx.Timeout(10.0)


class ConnectSinkError(Exception):
    """The broker connect bridge failed (rejected, unreachable, or timed out) — maps to HTTP 502.
    Carries no broker-side detail (server-internal), only a generic reason."""


class HttpxConnectSink:
    """POSTs a connected provider's token to the broker's ``/internal/oauth-connect`` bridge."""

    def __init__(self, *, broker_url: str, internal_key: str) -> None:
        self._url = broker_url.rstrip("/") + "/internal/oauth-connect"
        self._internal_key = internal_key

    async def oauth_connect(
        self,
        *,
        organisation_id: str,
        user_id: str,
        provider: str,
        name: str | None,
        token: dict,
    ) -> str:
        """Land the token as a broker credential; return the credential id. A broker failure
        (non-2xx / unreachable / timeout) is mapped to ConnectSinkError (no broker detail)."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    self._url,
                    headers={"X-Internal-Key": self._internal_key},
                    json={
                        "organisation_id": organisation_id,
                        "user_id": user_id,
                        "provider": provider,
                        "name": name,
                        "token": token,
                    },
                )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ConnectSinkError("credential broker rejected the connect") from exc
        except httpx.RequestError as exc:
            raise ConnectSinkError("credential broker unavailable") from exc
        return str(resp.json()["credential_id"])
