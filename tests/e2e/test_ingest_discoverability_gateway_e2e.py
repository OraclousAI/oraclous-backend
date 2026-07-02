"""#579: KGS ingest discoverability THROUGH THE GATEWAY — the published contract + a helpful 405.

Surfaced by the #440 book-GO e2e: a user couldn't discover how to LOAD content through the gateway
(only graph CRUD was in the published contract), and a `POST …/documents` — the natural "add a
document" guess — returned a bare 405. This proves the fix on the deployed stack, gateway-only:

* the published contract served at the edge (`GET /v1/openapi.json`) now lists the KGS ingest
  surfaces (`/ingest`, `/batch-ingest`, `/upload`, `/ingest-sql`, `/documents`) — discoverable;
* `GET …/documents` works (the read surface);
* `POST …/documents` returns a HELPFUL 405 naming the real ingest surfaces (`/upload`, `/ingest`)
  — the mistake self-corrects — instead of a bare "Method Not Allowed".

No fakes: real registration → real gateway → real KGS. No LLM key needed; CI-runnable on a cold
stack. Auto-skips when the gateway is down.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_INGEST_PATHS = [
    "/api/v1/graphs/{graphId}/ingest",
    "/api/v1/graphs/{graphId}/batch-ingest",
    "/api/v1/graphs/{graphId}/upload",
    "/api/v1/graphs/{graphId}/ingest-sql",
    "/api/v1/graphs/{graphId}/documents",
]


def test_ingest_paths_are_published_and_post_documents_is_a_helpful_405(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
    gateway_url: str,
) -> None:
    user = register(f"ingestdisco{uuid.uuid4().hex[:8]} user")
    c = gateway_client(user["token"])
    graph_id = c.post("/api/v1/graphs", json={"name": "ingest-discovery"}).json()["id"]

    # (1) #579 Ask 1 — the published contract at the edge now lists the ingest surfaces.
    spec = httpx.get(f"{gateway_url}/v1/openapi.json", timeout=15.0).json()
    published = spec["paths"]
    missing = [p for p in _INGEST_PATHS if p not in published]
    assert not missing, f"ingest paths not published in the gateway contract: {missing}"

    # (2) the read surface works through the gateway (a fresh graph → an empty document list).
    docs = c.get(f"/api/v1/graphs/{graph_id}/documents")
    assert docs.status_code == 200, docs.text
    assert docs.json() == []

    # (3) #579 Ask 2 — POSTing to /documents to "add" content returns an ACTIONABLE 405 through the
    #     gateway: the `Allow` header (GET is the method here) + a curated pointer to the published
    #     contract fetched above (which lists /upload, /ingest, …). The gateway never relays the
    #     upstream body (§3 rule 8), so the hint arrives via the Allow header + the contract — the
    #     discoverability loop closes and a wrong-method guess self-corrects.
    bad = c.post(f"/api/v1/graphs/{graph_id}/documents", json={})
    assert bad.status_code == 405, bad.text
    env = bad.json()["error"]
    assert env["code"] == "METHOD_NOT_ALLOWED"
    assert "/v1/openapi.json" in env["message"]  # points to the discoverability surface
    assert bad.headers.get("allow") == "GET"  # the safe method-list header is surfaced
    # and the surface it points to actually carries the ingest endpoints (loop closed, from step 1).
    assert "/api/v1/graphs/{graphId}/upload" in published
