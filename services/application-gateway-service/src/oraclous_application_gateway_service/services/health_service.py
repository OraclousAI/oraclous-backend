"""Upstream health aggregation (services layer).

Fans out to every fronted upstream's ``/health`` concurrently (bounded by the small fixed fan-out +
a short per-check timeout) and rolls the results up: an upstream is ``ok`` (200), ``degraded``
(other status), or ``down`` (unreachable/timeout); ``overall`` is ``ok`` only if all are ``ok``.
The endpoint itself always answers 200 — the body reflects the substrate state.
"""

from __future__ import annotations

import asyncio
import time

from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
from oraclous_application_gateway_service.schema.health import (
    UpstreamHealth,
    UpstreamsHealthResponse,
)


class HealthService:
    def __init__(
        self, *, upstream_client: UpstreamClient, targets: dict[str, str], timeout: float = 2.0
    ) -> None:
        self._client = upstream_client
        self._targets = targets
        self._timeout = timeout

    async def _check(self, name: str, base_url: str) -> UpstreamHealth:
        started = time.monotonic()
        code = await self._client.health_check(base_url, timeout_s=self._timeout)
        latency_ms = int((time.monotonic() - started) * 1000)
        if code == 200:
            status = "ok"
        elif code is None:
            status = "down"
        else:
            status = "degraded"
        return UpstreamHealth(name=name, status=status, latency_ms=latency_ms)

    async def check_all(self) -> UpstreamsHealthResponse:
        results = await asyncio.gather(
            *(self._check(name, base) for name, base in self._targets.items())
        )
        ordered = sorted(results, key=lambda r: r.name)
        overall = "ok" if all(r.status == "ok" for r in ordered) else "degraded"
        return UpstreamsHealthResponse(overall=overall, upstreams=ordered)
