"""KGS deprecation shim: query_cache_service — HTTP proxy to KRS (ADR-014 Option B, ORAA-56).

This module is a backwards-compatibility shim.  Callers must migrate to
oraclous_knowledge_retriever_service.query_cache_service.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_transport: httpx.AsyncBaseTransport | None = None


class QueryCacheService:
    async def get(self, key: str, **kwargs: Any) -> dict:
        logger.warning(
            "oraclous_knowledge_graph_service.query_cache_service is deprecated; "
            "migrate to oraclous_knowledge_retriever_service"
        )
        krs_base_url = os.environ.get("KRS_BASE_URL", "http://krs-service:8006")
        async with httpx.AsyncClient(transport=_transport or httpx.AsyncHTTPTransport()) as client:
            resp = await client.get(f"{krs_base_url}/cache", params={"key": key})
            return resp.json()
