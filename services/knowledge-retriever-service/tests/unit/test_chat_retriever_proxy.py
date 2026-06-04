"""Chat-path regression tests — KRS proxy (ORAA-61).

[R3-KRS-4] Chat APIs proxy through knowledge-retriever-service.

In R3, application-gateway-service is not yet extracted (R6). The chat
retrieval path routes through KRS. These tests verify:

AC1. All five KRS retrieval endpoints handle chat-style (natural-language)
     queries and return NodeResult-shaped payloads suitable for LLM grounding.
AC2. p95 round-trip latency for the in-process ASGI path is within 5 % of the
     pre-R3 baseline (500 ms wall-clock budget; tested at N=50 requests).
AC3. The NodeResult response shape is backwards-compatible with the legacy
     ``SourceInfo`` / provenance contract used by the chat service:
     ``id``, ``type``, and ``properties`` are present and of the correct types.

All imports are function-local (ORA-48 / TST001) so this file collects cleanly
during the TDD window before any chat-proxy routing layer exists.

RED until:
  * ``oraclous_knowledge_retriever_service.app.factory.create_app`` wires a
    chat-compatible router (ORAA-61 impl).
  * KRS returns a non-empty list for natural-language queries.
  * Latency budget confirmed by the implementation under test.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CHAT_QUERY = "What do we know about the organisation's strategic goals?"
_NODE_ID = "node-strategy-001"
_TIMESTAMP = "2026-06-04T00:00:00Z"
_LATENCY_BUDGET_MS = 500.0  # pre-R3 baseline (ASGI in-process)
_LATENCY_TOLERANCE = 0.05  # 5 %


async def _make_client():
    from httpx import ASGITransport, AsyncClient
    from oraclous_knowledge_retriever_service.app.factory import (  # ORA-48
        create_app,
    )

    app = create_app()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _assert_chat_grounding_compatible(items: list[Any]) -> None:
    """Assert that a list of NodeResult items can be used for LLM grounding.

    AC1 / AC3: the chat service maps NodeResult → SourceInfo.  The minimum
    usable shape requires id, type, and a properties dict with at least one
    key so the LLM has something to quote.
    """
    assert isinstance(items, list), "Chat retrieval must return a JSON list"
    assert len(items) > 0, (
        "Chat retrieval must return at least one node for LLM grounding; "
        "empty list means the chat service has nothing to ground on"
    )
    for item in items:
        assert isinstance(item, dict), f"Each NodeResult must be a dict; got {type(item)}"
        assert "id" in item, "NodeResult missing 'id' — chat service cannot identify the source"
        assert "type" in item, "NodeResult missing 'type' — chat service cannot classify source"
        assert "properties" in item, "NodeResult missing 'properties' — no data for grounding"
        assert isinstance(item["id"], str) and item["id"], (
            "NodeResult.id must be a non-empty string"
        )
        assert isinstance(item["type"], str) and item["type"], (
            "NodeResult.type must be a non-empty string"
        )
        assert isinstance(item["properties"], dict), "NodeResult.properties must be a dict"


# ---------------------------------------------------------------------------
# AC1 — Chat-path queries succeed on all five endpoints
# ---------------------------------------------------------------------------


class TestChatPathQueriesSucceed:
    """All five KRS endpoints accept chat-style natural-language queries (AC1).

    The chat service calls each retrieval modality depending on the user's
    chosen ChatMode (ENHANCED, PRECISE, EXPLORATORY, TEMPORAL).  Every
    endpoint must respond 200 with a non-empty NodeResult list.
    """

    async def test_semantic_search_handles_chat_query(self) -> None:
        """POST /v1/search/semantic accepts a natural-language chat query."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/semantic", json={"query": _CHAT_QUERY})
        assert response.status_code == 200
        _assert_chat_grounding_compatible(response.json())

    async def test_fulltext_search_handles_chat_query(self) -> None:
        """POST /v1/search/fulltext accepts a natural-language chat query."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/fulltext", json={"query": _CHAT_QUERY})
        assert response.status_code == 200
        _assert_chat_grounding_compatible(response.json())

    async def test_hybrid_search_handles_chat_query(self) -> None:
        """POST /v1/search/hybrid accepts a natural-language chat query."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/hybrid", json={"query": _CHAT_QUERY})
        assert response.status_code == 200
        _assert_chat_grounding_compatible(response.json())

    async def test_graph_traverse_handles_chat_node(self) -> None:
        """GET /v1/graph/traverse handles a node_id arising from a chat context."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/traverse", params={"node_id": _NODE_ID})
        assert response.status_code == 200
        _assert_chat_grounding_compatible(response.json())

    async def test_temporal_slice_handles_chat_timestamp(self) -> None:
        """GET /v1/graph/temporal accepts a ISO-8601 timestamp from a chat session."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/temporal", params={"ts": _TIMESTAMP})
        assert response.status_code == 200
        _assert_chat_grounding_compatible(response.json())

    async def test_all_five_endpoints_are_routable(self) -> None:
        """None of the five chat-path endpoints returns 404 (routing is complete)."""
        async with await _make_client() as client:
            routes = [
                ("POST", "/v1/search/semantic", {"query": _CHAT_QUERY}),
                ("POST", "/v1/search/fulltext", {"query": _CHAT_QUERY}),
                ("POST", "/v1/search/hybrid", {"query": _CHAT_QUERY}),
                ("GET", "/v1/graph/traverse", None),
                ("GET", "/v1/graph/temporal", None),
            ]
            for method, path, body in routes:
                if method == "POST":
                    r = await client.post(path, json=body)
                else:
                    params = {"node_id": _NODE_ID} if "traverse" in path else {"ts": _TIMESTAMP}
                    r = await client.get(path, params=params)
                assert r.status_code != 404, (
                    f"Chat-path endpoint {method} {path} returned 404 — not routed"
                )


# ---------------------------------------------------------------------------
# AC2 — Latency budget (p95 ≤ pre-R3 baseline + 5 %)
# ---------------------------------------------------------------------------


class TestChatProxyLatencyBudget:
    """p95 ASGI round-trip latency must not exceed the pre-R3 baseline + 5 % (AC2).

    Pre-R3 baseline: 500 ms (KGB direct-Neo4j path, ASGI in-process measure).
    Tolerance: 5 % → ceiling = 525 ms.

    Measurement: 50 sequential POST /v1/search/semantic requests; p95 derived
    from the sample.  Runs in-process via ASGITransport (no network hop), so
    any overhead beyond the stub handler is pure Python / framework cost.

    RED until the chat router is wired and the handler stays under budget.
    """

    _SAMPLE_SIZE = 50
    _BASELINE_MS = _LATENCY_BUDGET_MS
    _CEILING_MS = _BASELINE_MS * (1 + _LATENCY_TOLERANCE)  # 525.0 ms

    async def test_semantic_p95_within_budget(self) -> None:
        """p95 of 50 POST /v1/search/semantic calls must be ≤ 525 ms."""
        latencies: list[float] = []
        async with await _make_client() as client:
            for _ in range(self._SAMPLE_SIZE):
                t0 = time.perf_counter()
                await client.post("/v1/search/semantic", json={"query": _CHAT_QUERY})
                latencies.append((time.perf_counter() - t0) * 1000)

        p95 = statistics.quantiles(latencies, n=100)[94]
        assert p95 <= self._CEILING_MS, (
            f"Semantic search p95 latency {p95:.1f} ms exceeds "
            f"pre-R3 ceiling {self._CEILING_MS:.1f} ms "
            f"(baseline {self._BASELINE_MS:.1f} ms + {_LATENCY_TOLERANCE * 100:.0f} %)"
        )

    async def test_hybrid_p95_within_budget(self) -> None:
        """p95 of 50 POST /v1/search/hybrid calls must be ≤ 525 ms."""
        latencies: list[float] = []
        async with await _make_client() as client:
            for _ in range(self._SAMPLE_SIZE):
                t0 = time.perf_counter()
                await client.post("/v1/search/hybrid", json={"query": _CHAT_QUERY})
                latencies.append((time.perf_counter() - t0) * 1000)

        p95 = statistics.quantiles(latencies, n=100)[94]
        assert p95 <= self._CEILING_MS, (
            f"Hybrid search p95 latency {p95:.1f} ms exceeds ceiling {self._CEILING_MS:.1f} ms"
        )

    async def test_graph_traverse_p95_within_budget(self) -> None:
        """p95 of 50 GET /v1/graph/traverse calls must be ≤ 525 ms."""
        latencies: list[float] = []
        async with await _make_client() as client:
            for _ in range(self._SAMPLE_SIZE):
                t0 = time.perf_counter()
                await client.get("/v1/graph/traverse", params={"node_id": _NODE_ID})
                latencies.append((time.perf_counter() - t0) * 1000)

        p95 = statistics.quantiles(latencies, n=100)[94]
        assert p95 <= self._CEILING_MS, (
            f"Graph traverse p95 latency {p95:.1f} ms exceeds ceiling {self._CEILING_MS:.1f} ms"
        )


# ---------------------------------------------------------------------------
# AC3 — NodeResult is backwards-compatible with legacy SourceInfo contract
# ---------------------------------------------------------------------------


class TestNodeResultSourceInfoCompatibility:
    """NodeResult fields map cleanly onto the legacy SourceInfo / provenance contract (AC3).

    The legacy chat service built SourceInfo from Neo4j provenance nodes:
      ``node_id``     ← NodeResult.id
      ``content``     ← NodeResult.properties value (label / text)
      ``node_labels`` ← NodeResult.type

    No breaking change means these fields are present and correctly typed so
    the chat-path adapter can construct SourceInfo without a schema migration.
    """

    async def test_semantic_result_maps_to_source_info(self) -> None:
        """POST /v1/search/semantic result can be mapped to SourceInfo fields."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/semantic", json={"query": _CHAT_QUERY})
        items = response.json()
        assert len(items) > 0
        first = items[0]
        assert isinstance(first.get("id"), str), "id must be str (maps to SourceInfo.node_id)"
        assert isinstance(first.get("type"), str), (
            "type must be str (maps to SourceInfo.node_labels)"
        )
        assert isinstance(first.get("properties"), dict), (
            "properties must be dict (maps to SourceInfo.content/properties)"
        )

    async def test_hybrid_result_maps_to_source_info(self) -> None:
        """POST /v1/search/hybrid result can be mapped to SourceInfo fields."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/hybrid", json={"query": _CHAT_QUERY})
        items = response.json()
        first = items[0]
        assert isinstance(first.get("id"), str)
        assert isinstance(first.get("type"), str)
        assert isinstance(first.get("properties"), dict)

    async def test_traverse_result_maps_to_source_info(self) -> None:
        """GET /v1/graph/traverse result can be mapped to SourceInfo fields."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/traverse", params={"node_id": _NODE_ID})
        items = response.json()
        first = items[0]
        assert isinstance(first.get("id"), str)
        assert isinstance(first.get("type"), str)
        assert isinstance(first.get("properties"), dict)

    async def test_node_result_top_level_keys_are_exactly_envelope_fields(self) -> None:
        """NodeResult has only id, type, properties at root — no extraneous fields.

        Extra root-level keys would break the chat adapter's exact-match
        SourceInfo mapping.  All modality-specific data (scores, vectors,
        graph depth) must live inside properties.
        """
        async with await _make_client() as client:
            response = await client.post("/v1/search/semantic", json={"query": _CHAT_QUERY})
        for item in response.json():
            extra = set(item.keys()) - {"id", "type", "properties"}
            assert not extra, (
                f"NodeResult has extra root-level keys {extra!r}; "
                "all modality-specific fields must be inside properties "
                "(AC3: no breaking changes to chat API shapes)"
            )

    async def test_modality_data_lives_inside_properties_not_at_root(self) -> None:
        """Modality-specific fields (query, scores, depth) are inside properties.

        This is the inverse of the above: we confirm that at least one
        modality-specific field IS present somewhere in properties, proving
        the envelope carries useful data for grounding rather than being empty.
        """
        async with await _make_client() as client:
            response = await client.post("/v1/search/hybrid", json={"query": _CHAT_QUERY})
        for item in response.json():
            props = item.get("properties", {})
            assert isinstance(props, dict), "properties must be a dict"
            assert len(props) > 0, (
                "NodeResult.properties must contain at least one field so the "
                "chat service has grounding data to surface to the LLM"
            )

    async def test_result_ids_are_unique_within_response(self) -> None:
        """Each item in a retrieval response must have a unique id.

        Duplicate node ids would cause the chat service to de-duplicate
        sources incorrectly, leading to hallucination-risk gaps in grounding.
        """
        async with await _make_client() as client:
            response = await client.post("/v1/search/semantic", json={"query": _CHAT_QUERY})
        items = response.json()
        ids = [item["id"] for item in items]
        assert len(ids) == len(set(ids)), (
            f"NodeResult ids must be unique within a response; "
            f"duplicates found: {[x for x in ids if ids.count(x) > 1]}"
        )

    async def test_chat_query_is_reflected_in_properties(self) -> None:
        """Properties carry enough context to let the chat service attribute answers.

        The pre-R3 SourceInfo tracked which query produced which nodes; the
        R3 proxy must carry the originating query inside properties so the
        chat attribution chain is unbroken.
        """
        query = "What is the strategic vision for 2027?"
        async with await _make_client() as client:
            response = await client.post("/v1/search/semantic", json={"query": query})
        items = response.json()
        assert len(items) > 0
        first_props = items[0]["properties"]
        assert any(isinstance(v, str) and query in v for v in first_props.values()), (
            "At least one property value should carry the originating query "
            "so the chat service can attribute grounding back to the user input; "
            "this ensures AC3 compatibility with the pre-R3 provenance contract"
        )


# ---------------------------------------------------------------------------
# KGS → KRS shim: chat-path proxy verification
# ---------------------------------------------------------------------------
