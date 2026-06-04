"""KGS deprecation shim: similarity_service — HTTP proxy to KRS (ADR-014 Option B, ORAA-56).

This module is a backwards-compatibility shim.  Callers must migrate to
oraclous_knowledge_retriever_service.similarity_service.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_transport: httpx.AsyncBaseTransport | None = None


class SimilarityService:
    async def compute(self, node_id: str, **kwargs: Any) -> dict:
        logger.warning(
            "oraclous_knowledge_graph_service.similarity_service is deprecated; "
            "migrate to oraclous_knowledge_retriever_service"
        )
        krs_base_url = os.environ.get("KRS_BASE_URL", "http://krs-service:8006")
        async with httpx.AsyncClient(transport=_transport or httpx.AsyncHTTPTransport()) as client:
            resp = await client.post(
                f"{krs_base_url}/similarity/compute",
                json={"node_id": node_id},
            )
            return resp.json()
