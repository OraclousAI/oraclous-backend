"""Unit: route-level HTTP mapping for a failed query embedding (#1109 ruling 3).

`RetrievalService._embed_query` already classifies a failed embed call into
``QueryEmbeddingCredentialRejected`` (401/403 — the credential itself is bad) versus a plain
``QueryEmbeddingUnavailable`` (the provider was unreachable). #1109 splits a THIRD case out of
that same call: a 429/quota EXHAUSTED credential is neither "bad key" nor "unreachable provider" —
the fix differs (wait/upgrade, not replace the key) — so the route must tell all three apart:

  * a REJECTED credential (401/403)          -> 422 ``MODEL_CREDENTIAL_REJECTED``
  * an EXHAUSTED credential (429/quota)       -> 422 ``MODEL_CREDENTIAL_REQUIRED`` (unchanged code)
  * anything else (provider unreachable etc.) -> 503

Pinned for the two single-graph read surfaces that embed a query — semantic and hybrid, both in
``search_routes.py`` (call sites ~84 and ~119) — the same failure reaching the caller identically no
matter which of the two embedded it. Today ``search_routes.py`` maps a rejected credential to
``MODEL_CREDENTIAL_REQUIRED`` (the pre-#1109 collapse), so most cases here are genuinely RED, not
just import-RED.

Federated search (``federated_routes.py``) is out of scope: its ``FederatedService._try_embed``
catches every embedding exception and degrades to ``meta.semantic_degraded`` by design (ADR-026
partial-result surface) rather than raising, so it never reaches this route-mapping decision.

The fakes below raise the classified domain exception directly so this file exercises route
MAPPING; the classification itself (which provider text becomes which exception) is pinned
separately in ``test_query_embed_failure.py`` and ``packages/embedding/tests/
test_credential_rejection.py``.

``QueryEmbeddingCredentialExhausted`` does not exist yet — every reference to it is FUNCTION-LOCAL
(`.claude/rules/tests-seam-imports.md`) so this module collects cleanly and hard-fails RED
(ImportError) until the `[impl]` lands.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
from oraclous_knowledge_retriever_service.core.dependencies import get_retrieval_service
from oraclous_knowledge_retriever_service.services.retrieval_service import (
    QueryEmbeddingCredentialRejected,
    QueryEmbeddingUnavailable,
)

pytestmark = pytest.mark.unit

_AUTH = {"Authorization": "Bearer dev-token"}
_GRAPH_ID = str(uuid.uuid4())
_SEARCH_BODY = {"query": "who wrote the first program?", "graph_id": _GRAPH_ID}

#: Shaped like a real provider error, naming an internal host (rule 8) — this text must never reach
#: the response body no matter which of the three refusals is raised.
_LEAKY_TEXT = "broker.internal:8004"


@pytest.fixture
def app():
    from oraclous_knowledge_retriever_service.app import create_app  # noqa: PLC0415

    return create_app()


@pytest.fixture
async def async_client(app):
    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


class _FakeRetrievalService:
    """Stands in for `RetrievalService` at the route boundary — raises exactly the exception the
    real `_embed_query` would have raised, for both single-graph search modalities."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def semantic(self, **_kw):
        raise self._error

    async def hybrid(self, **_kw):
        raise self._error


@pytest.fixture
def post_with_error(app, async_client) -> Callable[..., Awaitable]:
    """Override the retrieval-service dependency with a fake that raises `error` for ONE request,
    then clear the override so fixtures never leak between tests."""

    async def _post(path: str, body: dict, *, error: Exception):
        fake = _FakeRetrievalService(error)
        app.dependency_overrides[get_retrieval_service] = lambda: fake
        try:
            return await async_client.post(path, json=body, headers=_AUTH)
        finally:
            app.dependency_overrides.clear()

    return _post


def _exhausted(msg: str) -> Exception:
    from oraclous_knowledge_retriever_service.services.retrieval_service import (  # noqa: PLC0415
        QueryEmbeddingCredentialExhausted,
    )

    return QueryEmbeddingCredentialExhausted(msg)


# ── rejected (401/403) -> 422 MODEL_CREDENTIAL_REJECTED ─────────────────────────────────────────


async def test_semantic_rejected_credential_is_422_rejected(post_with_error) -> None:
    resp = await post_with_error(
        "/v1/search/semantic",
        _SEARCH_BODY,
        error=QueryEmbeddingCredentialRejected(f"provider said 401 ({_LEAKY_TEXT})"),
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "MODEL_CREDENTIAL_REJECTED"
    assert _LEAKY_TEXT not in resp.text


async def test_hybrid_rejected_credential_is_422_rejected(post_with_error) -> None:
    resp = await post_with_error(
        "/v1/search/hybrid",
        _SEARCH_BODY,
        error=QueryEmbeddingCredentialRejected(f"provider said 403 ({_LEAKY_TEXT})"),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "MODEL_CREDENTIAL_REJECTED"
    assert _LEAKY_TEXT not in resp.text


# ── exhausted (429/quota) -> 422 MODEL_CREDENTIAL_REQUIRED ──────────────────────────────────────


async def test_semantic_exhausted_credential_is_422_required(post_with_error) -> None:
    resp = await post_with_error(
        "/v1/search/semantic",
        _SEARCH_BODY,
        error=_exhausted(f"provider said 429 insufficient_quota ({_LEAKY_TEXT})"),
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "MODEL_CREDENTIAL_REQUIRED"
    assert _LEAKY_TEXT not in resp.text


async def test_hybrid_exhausted_credential_is_422_required(post_with_error) -> None:
    resp = await post_with_error(
        "/v1/search/hybrid",
        _SEARCH_BODY,
        error=_exhausted(f"provider said 429 insufficient_quota ({_LEAKY_TEXT})"),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "MODEL_CREDENTIAL_REQUIRED"
    assert _LEAKY_TEXT not in resp.text


# ── anything else (provider unreachable etc.) -> 503 ────────────────────────────────────────────


async def test_semantic_unavailable_is_503(post_with_error) -> None:
    resp = await post_with_error(
        "/v1/search/semantic",
        _SEARCH_BODY,
        error=QueryEmbeddingUnavailable(f"connection refused ({_LEAKY_TEXT})"),
    )
    assert resp.status_code == 503, resp.text
    assert _LEAKY_TEXT not in resp.text


async def test_hybrid_unavailable_is_503(post_with_error) -> None:
    resp = await post_with_error(
        "/v1/search/hybrid",
        _SEARCH_BODY,
        error=QueryEmbeddingUnavailable(f"connection refused ({_LEAKY_TEXT})"),
    )
    assert resp.status_code == 503, resp.text
    assert _LEAKY_TEXT not in resp.text
