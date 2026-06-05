"""Upstream HTTP client (ORAA-4 §21 repositories layer) — the gateway's only external substrate.

Opens a STREAMING request to an upstream over the shared ``httpx.AsyncClient`` (bounded timeouts)
and returns the still-open response for the caller to stream back + close. Connect/timeout failures
are mapped to gateway domain errors (→ 502 / 504) so the edge never hangs or leaks an httpx error.
"""

from __future__ import annotations

import httpx

from oraclous_application_gateway_service.domain.errors import (
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)


class UpstreamClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def open(
        self,
        *,
        method: str,
        url: str,
        headers: list[tuple[bytes, bytes]] | dict,
        params: str | None,
        content: bytes,
    ) -> httpx.Response:
        """Send the request with ``stream=True``; the caller must ``aclose()`` the response.

        ``params`` is the raw query string (forwarded verbatim). Raises ``UpstreamUnavailableError``
        on connect failure and ``UpstreamTimeoutError`` on timeout.
        """
        full_url = f"{url}?{params}" if params else url
        request = self._client.build_request(method, full_url, headers=headers, content=content)
        try:
            return await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:  # ConnectError, ReadError, etc. → upstream unreachable
            raise UpstreamUnavailableError(str(exc)) from exc
