"""Broker connect-sink HTTP client (ORAA-4 §21 services layer — internal integration).

Implements the ``ConnectSink`` port against the credential-broker's internal G1 bridge
(``POST /internal/oauth-connect``, X-Internal-Key gated). The auth-service connect flow exchanges
the provider code and hands the resulting token here; the broker persists it as a resolvable
oauth credential. CI exercises the connect flow with a fake sink (no live broker); this real client
is used in the running stack.
"""

from __future__ import annotations

import httpx

_TIMEOUT = httpx.Timeout(10.0)


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
        """Land the token as a broker credential; return the credential id. Raises on a non-2xx."""
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
        return str(resp.json()["credential_id"])
