"""NodeResult TypedDict — failing tests for envelope shape (ORAA-227).

[R3-KGS-4] Acceptance criteria:
1. ``NodeResult`` TypedDict exists and is importable from
   ``oraclous_knowledge_graph_service.contracts``
2. ``NodeResult`` has the correct OHM-shaped fields: ``id`` (str), ``type``
   (str), ``properties`` (dict) at minimum
3. Write-path endpoints return data serialisable to ``NodeResult`` shape

All imports of ``oraclous_knowledge_graph_service.contracts`` are
function-local (TST001 / ORA-48) so this file collects cleanly during the
TDD window before the contracts module exists.  Tests fail at runtime with
``ModuleNotFoundError`` until the paired ``[impl]`` PR lands.

RED until backend-implementer creates
``oraclous_knowledge_graph_service.contracts.NodeResult``.
"""

from __future__ import annotations

import json
import typing

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Importability (AC1)
# ---------------------------------------------------------------------------


class TestNodeResultImportability:
    """NodeResult TypedDict exists and is importable from the contracts module."""

    def test_node_result_importable_from_contracts(self) -> None:
        """NodeResult can be imported from oraclous_knowledge_graph_service.contracts."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        assert NodeResult is not None

    def test_node_result_is_a_typed_dict(self) -> None:
        """NodeResult is a TypedDict subclass, not a plain dict or dataclass.

        TypedDicts carry ``__annotations__`` and are instances of ``type``.
        This prevents an implementation from satisfying the import AC with a
        non-TypedDict class.
        """
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        assert hasattr(NodeResult, "__annotations__"), (
            "NodeResult must be a TypedDict — it has no __annotations__"
        )
        assert isinstance(NodeResult, type), (
            "NodeResult must be a type (TypedDict class), not an instance"
        )


# ---------------------------------------------------------------------------
# 2. Field shape — OHM envelope (AC2)
# ---------------------------------------------------------------------------


class TestNodeResultFieldShape:
    """NodeResult has the correct OHM-shaped fields at minimum: id, type, properties."""

    def test_node_result_has_id_field(self) -> None:
        """NodeResult declares an 'id' field."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        assert "id" in NodeResult.__annotations__, "NodeResult must declare an 'id' field"

    def test_node_result_has_type_field(self) -> None:
        """NodeResult declares a 'type' field."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        assert "type" in NodeResult.__annotations__, "NodeResult must declare a 'type' field"

    def test_node_result_has_properties_field(self) -> None:
        """NodeResult declares a 'properties' field."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        assert "properties" in NodeResult.__annotations__, (
            "NodeResult must declare a 'properties' field"
        )

    def test_node_result_id_resolves_to_str(self) -> None:
        """NodeResult.id resolves to str (not Optional[str] or Any)."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        hints = typing.get_type_hints(NodeResult)
        assert hints.get("id") is str, (
            f"NodeResult.id must be annotated as str; got {hints.get('id')}"
        )

    def test_node_result_type_resolves_to_str(self) -> None:
        """NodeResult.type resolves to str (not Optional[str] or Any)."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        hints = typing.get_type_hints(NodeResult)
        assert hints.get("type") is str, (
            f"NodeResult.type must be annotated as str; got {hints.get('type')}"
        )

    def test_node_result_id_is_required_not_optional(self) -> None:
        """NodeResult.id is a required TypedDict field, not Optional.

        A missing 'id' breaks client-side node identity.  The TypedDict must
        not declare it as ``str | None``.
        """
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        required_keys = getattr(NodeResult, "__required_keys__", frozenset())
        assert "id" in required_keys, (
            "NodeResult.id must be a required TypedDict field, not optional"
        )

    def test_node_result_type_is_required_not_optional(self) -> None:
        """NodeResult.type is a required field — absent type breaks dispatch."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        required_keys = getattr(NodeResult, "__required_keys__", frozenset())
        assert "type" in required_keys, (
            "NodeResult.type must be a required TypedDict field, not optional"
        )

    def test_node_result_properties_is_required_not_optional(self) -> None:
        """NodeResult.properties is a required field."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        required_keys = getattr(NodeResult, "__required_keys__", frozenset())
        assert "properties" in required_keys, (
            "NodeResult.properties must be a required TypedDict field, not optional"
        )


# ---------------------------------------------------------------------------
# 3. Instantiation and JSON serialisability
# ---------------------------------------------------------------------------


class TestNodeResultInstantiation:
    """NodeResult instances can be constructed and serialised to JSON.

    JSON serialisability is the write-path contract: endpoints must be able
    to return NodeResult dicts directly as HTTP response bodies.
    """

    def test_node_result_can_be_constructed_with_required_fields(self) -> None:
        """NodeResult can be constructed as a dict with id, type, properties."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        instance: NodeResult = {
            "id": "node-123",
            "type": "Person",
            "properties": {"name": "Alice"},
        }
        assert instance["id"] == "node-123"
        assert instance["type"] == "Person"
        assert instance["properties"]["name"] == "Alice"

    def test_node_result_with_scalar_properties_is_json_serialisable(self) -> None:
        """NodeResult carrying scalar properties serialises to JSON without error."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        instance: NodeResult = {
            "id": "node-abc",
            "type": "Company",
            "properties": {"name": "Acme Corp", "founded": 1990},
        }
        serialised = json.dumps(instance)
        round_tripped = json.loads(serialised)
        assert round_tripped["id"] == "node-abc"
        assert round_tripped["type"] == "Company"
        assert round_tripped["properties"]["founded"] == 1990

    def test_node_result_with_empty_properties_is_valid(self) -> None:
        """A NodeResult with an empty properties dict is valid (e.g. stub nodes)."""
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        instance: NodeResult = {
            "id": "node-xyz",
            "type": "Chunk",
            "properties": {},
        }
        assert isinstance(instance["properties"], dict)

    def test_node_result_list_is_json_serialisable(self) -> None:
        """A list of NodeResult dicts (typical endpoint payload) serialises to JSON.

        Write-path endpoints return a collection of persisted nodes, not a
        singleton — the envelope is list[NodeResult].
        """
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        nodes: list[NodeResult] = [
            {"id": "n1", "type": "Person", "properties": {"name": "Alice"}},
            {"id": "n2", "type": "Person", "properties": {"name": "Bob"}},
        ]
        serialised = json.dumps(nodes)
        parsed = json.loads(serialised)
        assert len(parsed) == 2
        assert parsed[0]["type"] == "Person"


# ---------------------------------------------------------------------------
# 4. Write-path endpoint data compatibility (AC3)
# ---------------------------------------------------------------------------


class TestWritePathEndpointCompatibility:
    """Write-path endpoint responses are serialisable to NodeResult shape (AC3).

    These tests verify the TypedDict contract is expressive enough to represent
    node data emitted by ingest and upload endpoints.  They exercise the shape,
    not the HTTP transport — the HTTP transport layer is covered in integration
    tests at the Tests Review gate.
    """

    def test_ingest_response_node_satisfies_node_result_shape(self) -> None:
        """POST /api/v1/graphs/{id}/ingest node entries satisfy NodeResult.

        An ingest response carries a list of persisted entity nodes; each node
        dict must carry id, type, and properties so callers can identify,
        classify, and inspect the ingested data.
        """
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        ingest_node: NodeResult = {
            "id": "urn:node:alice-123",
            "type": "Person",
            "properties": {
                "name": "Alice",
                "graph_id": "graph-001",
                "transaction_time": "2026-06-04T00:00:00+00:00",
            },
        }
        assert ingest_node["type"] == "Person"
        assert "graph_id" in ingest_node["properties"]
        # id must be non-empty string
        assert ingest_node["id"]

    def test_upload_document_node_satisfies_node_result_shape(self) -> None:
        """POST /api/v1/graphs/{id}/upload Document nodes satisfy NodeResult.

        File upload creates Document nodes; each must carry id,
        type='Document', and properties including the file path.
        """
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        document_node: NodeResult = {
            "id": "urn:node:doc-456",
            "type": "Document",
            "properties": {
                "path": "/uploads/report.pdf",
                "graph_id": "graph-001",
            },
        }
        assert document_node["type"] == "Document"
        assert "path" in document_node["properties"]

    def test_upload_chunk_node_satisfies_node_result_shape(self) -> None:
        """POST /api/v1/graphs/{id}/upload Chunk nodes satisfy NodeResult.

        Upload also creates Chunk nodes (text fragments); each must carry id,
        type='Chunk', and properties including the chunk text.
        """
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        chunk_node: NodeResult = {
            "id": "urn:node:chunk-789",
            "type": "Chunk",
            "properties": {
                "text": "First paragraph of the uploaded document.",
                "graph_id": "graph-001",
            },
        }
        assert chunk_node["type"] == "Chunk"
        assert "text" in chunk_node["properties"]

    def test_ingest_and_upload_nodes_share_same_envelope(self) -> None:
        """Ingest and upload nodes use the same NodeResult envelope.

        The write-path contract is unified: whether data enters via text
        ingest or file upload, the node envelope is always NodeResult.  There
        must be no separate IngestNodeResult / UploadNodeResult types.
        """
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        nodes: list[NodeResult] = [
            {
                "id": "urn:node:entity-1",
                "type": "Person",
                "properties": {"name": "Alice", "graph_id": "g1"},
            },
            {
                "id": "urn:node:doc-2",
                "type": "Document",
                "properties": {"path": "/a.pdf", "graph_id": "g1"},
            },
            {
                "id": "urn:node:chunk-3",
                "type": "Chunk",
                "properties": {"text": "...", "graph_id": "g1"},
            },
        ]
        types = {n["type"] for n in nodes}
        assert types == {"Person", "Document", "Chunk"}

    def test_write_path_provenance_properties_are_node_result_compatible(self) -> None:
        """Provenance stamps injected by the write path are NodeResult-compatible.

        MultiTenantKGWriter injects graph_id, transaction_time, and
        ingestion_time onto every persisted node.  A NodeResult carrying these
        provenance fields must still be valid and JSON-serialisable.
        """
        from oraclous_knowledge_graph_service.contracts import NodeResult  # RED

        node_with_provenance: NodeResult = {
            "id": "urn:node:entity-999",
            "type": "Organisation",
            "properties": {
                "name": "Acme Ltd",
                "graph_id": "graph-XYZ",
                "transaction_time": "2026-06-04T00:00:00+00:00",
                "ingestion_time": "2026-06-04T00:00:00+00:00",
                "ingestion_source": "annual_report.pdf",
            },
        }
        serialised = json.dumps(node_with_provenance)
        parsed = json.loads(serialised)
        assert parsed["properties"]["graph_id"] == "graph-XYZ"
        assert parsed["properties"]["transaction_time"] is not None
