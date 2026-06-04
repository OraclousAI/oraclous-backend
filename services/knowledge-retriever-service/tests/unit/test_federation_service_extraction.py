"""Federation service extraction tests (ORAA-59).

Acceptance criterion 1: ``oraclous_knowledge_retriever_service.federation_service``
(FederationService) lives *only* in KRS after R3-KRS-5 is implemented.

Pins the public contract of FederationService so the extraction cannot silently
drop or rename the methods the retriever endpoints depend on.  Behavioural tests
use async fakes; no live Neo4j is required for this module.

All imports of the not-yet-built seam are function-local per ORA-48 / TST001:
collection succeeds, each test fails RED at runtime with ``ModuleNotFoundError``
until the ``[impl]`` PR lands the module in KRS.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit

# ── Fixed UUIDs for deterministic test output ──────────────────────────────

_USER_A = "user-alpha-0001"
_GRAPH_1 = "graph-aaaa-0001"
_GRAPH_2 = "graph-bbbb-0002"

# ── Async Neo4j driver fake ────────────────────────────────────────────────


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def data(self) -> dict[str, Any]:
        return dict(self._data)


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return list(self._rows)

    async def single(self) -> _FakeRecord | None:
        return _FakeRecord(self._rows[0]) if self._rows else None


class _FakeSession:
    """Minimal async session that records Cypher calls and returns preset rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, params: dict[str, Any] | None = None) -> _FakeResult:
        self.queries.append((query, params or {}))
        return _FakeResult(self._rows)

    async def execute_write(self, fn) -> None:
        await fn(self)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass


class _FakeAsyncDriver:
    """Records sessions opened and returns a configured fake session."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.sessions: list[_FakeSession] = []

    def session(self, **_kwargs: Any) -> _FakeSession:
        s = _FakeSession(self._rows)
        self.sessions.append(s)
        return s


# ── Module importability ───────────────────────────────────────────────────


class TestFederationServiceModuleImport:
    """``oraclous_knowledge_retriever_service.federation_service`` is importable."""

    def test_federation_service_module_is_importable(self) -> None:
        """The federation_service module must be importable from the KRS package."""
        from oraclous_knowledge_retriever_service import (  # ORA-48
            federation_service,
        )

        assert federation_service is not None

    def test_federation_service_exposes_federation_service_class(self) -> None:
        """``federation_service`` module must expose a ``FederationService`` class."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationService,
        )

        assert callable(FederationService)

    def test_federation_service_exposes_federation_error(self) -> None:
        """``federation_service`` module must expose a ``FederationError`` exception."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationError,
        )

        assert issubclass(FederationError, Exception)

    def test_federation_error_carries_status_code(self) -> None:
        """``FederationError`` must carry a ``status_code`` attribute."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationError,
        )

        err = FederationError("boom", status_code=403)
        assert err.status_code == 403

    def test_federation_error_defaults_to_400(self) -> None:
        """``FederationError`` default status_code must be 400."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationError,
        )

        err = FederationError("bad request")
        assert err.status_code == 400


# ── Public API surface ─────────────────────────────────────────────────────


class TestFederationServicePublicAPI:
    """FederationService must expose the full read-side public API."""

    _REQUIRED_METHODS = [
        "federated_query",
        "federated_vector_search",
        "find_same_as_candidates",
        "resolve_entity",
        "find_federation_candidates",
    ]

    @pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
    def test_method_is_present(self, method_name: str) -> None:
        """FederationService must expose ``{method_name}``."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationService,
        )

        assert hasattr(FederationService, method_name), (
            f"FederationService.{method_name} is missing from KRS"
        )

    def test_constructor_accepts_async_driver(self) -> None:
        """FederationService must accept an async Neo4j driver at construction."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationService,
        )

        driver = _FakeAsyncDriver()
        svc = FederationService(driver)
        assert svc is not None

    def test_constructor_accepts_database_param(self) -> None:
        """FederationService must accept an optional ``neo4j_database`` parameter."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationService,
        )

        driver = _FakeAsyncDriver()
        svc = FederationService(driver, neo4j_database="my-db")
        assert svc is not None


# ── Validation / fail-closed behaviour ────────────────────────────────────


class TestFederationServiceValidation:
    """Permission gate: fail-closed on non-owned / non-federatable graphs."""

    @pytest.mark.asyncio
    async def test_federated_query_raises_on_too_many_graphs(self) -> None:
        """federated_query must raise FederationError when graph_ids exceeds MAX_GRAPH_IDS."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            MAX_GRAPH_IDS,
            FederationError,
            FederationService,
        )

        driver = _FakeAsyncDriver(rows=[])
        svc = FederationService(driver)
        too_many = [f"g-{i}" for i in range(MAX_GRAPH_IDS + 1)]

        with pytest.raises(FederationError) as exc_info:
            await svc.federated_query(_USER_A, too_many, "search")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_federated_query_raises_403_for_wrong_owner(self) -> None:
        """federated_query must raise FederationError(403) when a graph belongs to another user."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationError,
            FederationService,
        )

        # Row returns a different user_id than the caller
        rows = [
            {
                "graph_id": _GRAPH_1,
                "user_id": "other-user-999",
                "name": "Graph 1",
                "federatable": True,
            }
        ]
        driver = _FakeAsyncDriver(rows=rows)
        svc = FederationService(driver)

        with pytest.raises(FederationError) as exc_info:
            await svc.federated_query(_USER_A, [_GRAPH_1], "search")

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_federated_query_raises_400_for_non_federatable_graph(self) -> None:
        """federated_query must raise FederationError(400) when a graph is not federatable."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationError,
            FederationService,
        )

        rows = [
            {
                "graph_id": _GRAPH_1,
                "user_id": _USER_A,
                "name": "Graph 1",
                "federatable": False,
            }
        ]
        driver = _FakeAsyncDriver(rows=rows)
        svc = FederationService(driver)

        with pytest.raises(FederationError) as exc_info:
            await svc.federated_query(_USER_A, [_GRAPH_1], "search")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_federated_query_raises_400_for_missing_graph(self) -> None:
        """federated_query must raise FederationError(400) when a graph_id is not found."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationError,
            FederationService,
        )

        # DB returns no rows → requested graph_id is not found
        driver = _FakeAsyncDriver(rows=[])
        svc = FederationService(driver)

        with pytest.raises(FederationError) as exc_info:
            await svc.federated_query(_USER_A, [_GRAPH_1], "search")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_vector_search_raises_403_for_wrong_owner(self) -> None:
        """federated_vector_search must also apply the ownership gate."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationError,
            FederationService,
        )

        rows = [
            {
                "graph_id": _GRAPH_1,
                "user_id": "not-the-caller",
                "name": "Stolen Graph",
                "federatable": True,
            }
        ]
        driver = _FakeAsyncDriver(rows=rows)
        svc = FederationService(driver)

        with pytest.raises(FederationError) as exc_info:
            await svc.federated_vector_search(_USER_A, [_GRAPH_1], "query text")

        assert exc_info.value.status_code == 403


# ── Schema constants ───────────────────────────────────────────────────────


class TestFederationSchemaConstants:
    """Guard-rail constants must be importable and within safe bounds."""

    def test_max_graph_ids_is_positive_integer(self) -> None:
        """MAX_GRAPH_IDS must be a positive integer."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            MAX_GRAPH_IDS,
        )

        assert isinstance(MAX_GRAPH_IDS, int)
        assert MAX_GRAPH_IDS > 0

    def test_max_total_results_is_positive_integer(self) -> None:
        """MAX_TOTAL_RESULTS must be a positive integer."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            MAX_TOTAL_RESULTS,
        )

        assert isinstance(MAX_TOTAL_RESULTS, int)
        assert MAX_TOTAL_RESULTS > 0


# ── SAME_AS deduplication ─────────────────────────────────────────────────


class TestFederationSameAsDeduplication:
    """federated_query with deduplicate_entities=True must surface CrossGraphLink entries."""

    @pytest.mark.asyncio
    async def test_deduplication_returns_cross_graph_link_for_same_name_type(self) -> None:
        """Two entities with identical (name, type) across graphs must produce a CrossGraphLink."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederatedQueryOptions,
            FederationService,
        )

        # Validation rows — both graphs are owned by user_a and federatable
        validation_rows = [
            {"graph_id": _GRAPH_1, "user_id": _USER_A, "name": "G1", "federatable": True},
            {"graph_id": _GRAPH_2, "user_id": _USER_A, "name": "G2", "federatable": True},
        ]
        # Entity union rows — same entity in both graphs
        entity_rows = [
            {"entity_id": "e1", "name": "Alice", "type": "Person", "source_graph_id": _GRAPH_1},
            {"entity_id": "e2", "name": "Alice", "type": "Person", "source_graph_id": _GRAPH_2},
        ]

        # Driver returns validation_rows for the permission check, then entity_rows for the union
        call_count = 0

        class _MultiRowDriver:
            def session(self, **_kw: Any) -> _FakeSession:
                nonlocal call_count
                rows = validation_rows if call_count == 0 else entity_rows
                call_count += 1
                return _FakeSession(rows)

        driver = _MultiRowDriver()
        svc = FederationService(driver)
        opts = FederatedQueryOptions(
            deduplicate_entities=True,
            include_cross_graph_links=True,
        )

        result = await svc.federated_query(_USER_A, [_GRAPH_1, _GRAPH_2], "Alice", options=opts)

        assert result["cross_graph_links"], (
            "Expected at least one CrossGraphLink for same-name entities"
        )
        link = result["cross_graph_links"][0]
        assert link.link_type == "SAME_AS"
        assert {link.graph_a, link.graph_b} == {_GRAPH_1, _GRAPH_2}
