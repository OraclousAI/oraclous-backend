"""DI providers (ORAA-4 §21 core layer) — wiring only.

The shared upstream HTTP client is opened in ``core/lifespan`` and resolved per request from
``app.state``. Auth + proxy dependencies are layered on in later slices.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status


def get_http_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="gateway HTTP client unavailable",
        )
    return client


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
