"""Unit tests — KRS schema client circuit breaker (ORAA-57).

Pins the expected behaviour of ``SchemaClient``, the component in
knowledge-retriever-service that fetches OHM-schema envelopes from the
knowledge-graph-service internal API and applies a read-through cache with
circuit-breaker semantics.

Circuit-breaker contract:
  1. Successful KGS response → return schema dict, populate cache.
  2. KGS timeout or connection error, cached schema exists → return cached copy.
  3. KGS timeout or connection error, no cache → raise SchemaUnavailable.
  4. KGS returns 404 → raise SchemaNotFound (never served from cache).
  5. A recovered KGS response refreshes the cache entry.

Import target (function-local, ORA-48):
  oraclous_knowledge_retriever_service.schema_client.SchemaClient
  oraclous_knowledge_retriever_service.schema_client.SchemaUnavailable
  oraclous_knowledge_retriever_service.schema_client.SchemaNotFound

RED until SchemaClient is implemented.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_CLIENT_MODULE = "oraclous_knowledge_retriever_service.schema_client"

GRAPH_ID = str(uuid.uuid4())
KGS_BASE_URL = "http://knowledge-graph-service:8000"

_SCHEMA_ENVELOPE = {
    "graph_id": GRAPH_ID,
    "schema_version": "v1",
    "nodes": {
        "Company": {
            "label": "Company",
            "properties": {"name": "string"},
            "sample_count": 3,
            "indexes": [],
        }
    },
    "relationships": {},
    "constraints": [],
    "indexes": [],
    "last_updated": "2026-06-04T00:00:00+00:00",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(cache: dict | None = None):
    """Instantiate a SchemaClient with optional pre-populated cache."""
    from oraclous_knowledge_retriever_service.schema_client import SchemaClient  # ORA-48

    return SchemaClient(kgs_base_url=KGS_BASE_URL, cache=cache if cache is not None else {})


def _ok_response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = body
    return resp


def _not_found_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 404
    resp.json.return_value = {"detail": "Graph schema not found"}
    return resp


# ---------------------------------------------------------------------------
# 1. Successful fetch
# ---------------------------------------------------------------------------


class TestSchemaClientSuccessfulFetch:
    """Happy path: KGS responds with 200 → schema is returned and cached."""

    async def test_successful_fetch_returns_schema_dict(self) -> None:
        """get_schema returns the envelope dict on a 200 response from KGS."""
        client = _make_client()
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=_ok_response(_SCHEMA_ENVELOPE)))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.get_schema(GRAPH_ID)

        assert result["graph_id"] == GRAPH_ID
        assert "nodes" in result

    async def test_successful_fetch_populates_cache(self) -> None:
        """After a successful KGS call, the schema is stored in the cache."""
        cache: dict = {}
        client = _make_client(cache=cache)
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=_ok_response(_SCHEMA_ENVELOPE)))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            await client.get_schema(GRAPH_ID)

        assert GRAPH_ID in cache, "cache must be populated after a successful KGS fetch"

    async def test_kgs_url_includes_graph_id(self) -> None:
        """SchemaClient must request /internal/v1/schema/{graph_id} on KGS."""
        mock_http_client = MagicMock()
        mock_get = AsyncMock(return_value=_ok_response(_SCHEMA_ENVELOPE))
        mock_http_client.get = mock_get

        client = _make_client()
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_http_client
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            await client.get_schema(GRAPH_ID)

        call_url = mock_get.call_args.args[0] if mock_get.call_args.args else ""
        assert GRAPH_ID in call_url, f"KGS request URL must include graph_id; got {call_url!r}"
        assert "internal/v1/schema" in call_url, (
            f"KGS request must target /internal/v1/schema; got {call_url!r}"
        )


# ---------------------------------------------------------------------------
# 2. Circuit breaker — fallback to cache on failure
# ---------------------------------------------------------------------------


class TestSchemaClientCircuitBreakerCacheHit:
    """When KGS is unreachable, a pre-cached schema is returned (circuit open)."""

    async def test_timeout_with_cached_schema_returns_cached(self) -> None:
        """TimeoutException from KGS → return cached schema without re-raising."""
        import httpx

        cache = {GRAPH_ID: dict(_SCHEMA_ENVELOPE)}
        client = _make_client(cache=cache)
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.TimeoutException = httpx.TimeoutException
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(
                    get=AsyncMock(side_effect=httpx.TimeoutException("connect timeout"))
                )
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.get_schema(GRAPH_ID)

        assert result == cache[GRAPH_ID], "circuit breaker must return the cached schema on timeout"

    async def test_connection_error_with_cached_schema_returns_cached(self) -> None:
        """ConnectError from KGS → return cached schema without re-raising."""
        import httpx

        cache = {GRAPH_ID: dict(_SCHEMA_ENVELOPE)}
        client = _make_client(cache=cache)
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.ConnectError = httpx.ConnectError
            mock_httpx.TimeoutException = httpx.TimeoutException
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(
                    get=AsyncMock(side_effect=httpx.ConnectError("connection refused"))
                )
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.get_schema(GRAPH_ID)

        assert result == cache[GRAPH_ID]

    async def test_cached_schema_returned_has_correct_graph_id(self) -> None:
        """Cached schema returned by the circuit breaker must match the requested graph_id."""
        import httpx

        cache = {GRAPH_ID: dict(_SCHEMA_ENVELOPE)}
        client = _make_client(cache=cache)
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.TimeoutException = httpx.TimeoutException
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(side_effect=httpx.TimeoutException("timeout")))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.get_schema(GRAPH_ID)

        assert result["graph_id"] == GRAPH_ID


# ---------------------------------------------------------------------------
# 3. Circuit breaker — no cache, failure → SchemaUnavailable
# ---------------------------------------------------------------------------


class TestSchemaClientCircuitBreakerCacheMiss:
    """When KGS fails and no cache entry exists, SchemaUnavailable is raised."""

    async def test_timeout_with_no_cache_raises_schema_unavailable(self) -> None:
        """TimeoutException + empty cache → SchemaUnavailable raised."""
        import httpx
        from oraclous_knowledge_retriever_service.schema_client import (  # ORA-48
            SchemaUnavailable,
        )

        client = _make_client(cache={})
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.TimeoutException = httpx.TimeoutException
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(side_effect=httpx.TimeoutException("timeout")))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(SchemaUnavailable):
                await client.get_schema(GRAPH_ID)

    async def test_connection_error_with_no_cache_raises_schema_unavailable(self) -> None:
        """ConnectError + empty cache → SchemaUnavailable raised."""
        import httpx
        from oraclous_knowledge_retriever_service.schema_client import (  # ORA-48
            SchemaUnavailable,
        )

        client = _make_client(cache={})
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.ConnectError = httpx.ConnectError
            mock_httpx.TimeoutException = httpx.TimeoutException
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(side_effect=httpx.ConnectError("refused")))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(SchemaUnavailable):
                await client.get_schema(GRAPH_ID)

    async def test_schema_unavailable_message_identifies_graph_id(self) -> None:
        """SchemaUnavailable exception message must include the requested graph_id."""
        import httpx
        from oraclous_knowledge_retriever_service.schema_client import (  # ORA-48
            SchemaUnavailable,
        )

        client = _make_client(cache={})
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.TimeoutException = httpx.TimeoutException
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(side_effect=httpx.TimeoutException("timeout")))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(SchemaUnavailable) as exc_info:
                await client.get_schema(GRAPH_ID)

        assert GRAPH_ID in str(exc_info.value), (
            "SchemaUnavailable message must include the graph_id for diagnostics"
        )


# ---------------------------------------------------------------------------
# 4. 404 from KGS — always propagated, never served from cache
# ---------------------------------------------------------------------------


class TestSchemaClientNotFoundPropagation:
    """404 from KGS is always propagated as SchemaNotFound — even if a cached
    entry exists. A 404 indicates the graph was deleted; stale cache must not mask it.
    """

    async def test_kgs_404_raises_schema_not_found(self) -> None:
        """HTTP 404 from KGS → SchemaNotFound raised."""
        from oraclous_knowledge_retriever_service.schema_client import (  # ORA-48
            SchemaNotFound,
        )

        client = _make_client(cache={})
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=_not_found_response()))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(SchemaNotFound):
                await client.get_schema(GRAPH_ID)

    async def test_kgs_404_not_masked_by_stale_cache(self) -> None:
        """404 from KGS → SchemaNotFound even when a cached entry is present."""
        from oraclous_knowledge_retriever_service.schema_client import (  # ORA-48
            SchemaNotFound,
        )

        cache = {GRAPH_ID: dict(_SCHEMA_ENVELOPE)}
        client = _make_client(cache=cache)
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=_not_found_response()))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(SchemaNotFound):
                await client.get_schema(GRAPH_ID)

    async def test_schema_not_found_identifies_graph_id(self) -> None:
        """SchemaNotFound exception message must include the graph_id."""
        from oraclous_knowledge_retriever_service.schema_client import (  # ORA-48
            SchemaNotFound,
        )

        client = _make_client(cache={})
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=_not_found_response()))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(SchemaNotFound) as exc_info:
                await client.get_schema(GRAPH_ID)

        assert GRAPH_ID in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. Cache refresh after recovery
# ---------------------------------------------------------------------------


class TestSchemaClientCacheRefresh:
    """A successful KGS response after a failure refreshes the stale cache entry."""

    async def test_successful_fetch_after_cache_hit_updates_cache(self) -> None:
        """When KGS recovers, the fresh schema replaces the stale cache entry."""
        stale_schema = dict(_SCHEMA_ENVELOPE)
        fresh_schema = {**_SCHEMA_ENVELOPE, "schema_version": "v2"}

        cache = {GRAPH_ID: stale_schema}
        client = _make_client(cache=cache)
        with patch(f"{_CLIENT_MODULE}.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=_ok_response(fresh_schema)))
            )
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.get_schema(GRAPH_ID)

        assert result["schema_version"] == "v2", (
            "cache must be refreshed with the latest KGS response"
        )
        assert cache[GRAPH_ID]["schema_version"] == "v2"
