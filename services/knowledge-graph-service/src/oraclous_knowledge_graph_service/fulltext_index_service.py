"""KGS deprecation shim: fulltext_index_service — HTTP proxy to KRS (ADR-014 Option B, ORAA-56).

This module is a backwards-compatibility shim.  Callers must migrate to
oraclous_knowledge_retriever_service.fulltext_index_service.

Note: ``**kwargs`` are accepted for signature compatibility with KRS but are not forwarded to KRS.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_transport: httpx.AsyncBaseTransport | None = None


class FulltextIndexService:
    async def build(self, graph_id: str, **kwargs: Any) -> dict:
        logger.warning(
            "oraclous_knowledge_graph_service.fulltext_index_service is deprecated; "
            "migrate to oraclous_knowledge_retriever_service"
        )
        krs_base_url = os.environ.get("KRS_BASE_URL", "http://krs-service:8006")
        async with httpx.AsyncClient(transport=_transport or httpx.AsyncHTTPTransport()) as client:
            resp = await client.post(
                f"{krs_base_url}/fulltext/build",
                json={"graph_id": graph_id},
            )
            resp.raise_for_status()
            return resp.json()
