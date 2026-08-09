"""Retrieval returns ``citation`` as a typed sibling, never a key inside ``properties`` (#742).

``NodeResultModel`` and ``FederatedNodeResultModel`` each gain ``citation: Citation | None = None``.
``FederatedNodeResultModel`` already sets the precedent: ``source_graph_id`` and
``source_graph_name`` are siblings, not property-bag entries.

Why a sibling and not a property: the console renders the citation and the user clicks it, and
UC-E1 makes the citation a **gate**. A gate cannot run against an untyped bag whose contents vary
by modality — and ``properties`` is exactly that bag (score, text, relationship, precedence_tier
all live there). A key inside it would also be indistinguishable from a caller-planted one.

The projection is asserted at ``_to_node_result`` / ``_labeled_node`` rather than over a live
Neo4j: the lift-and-strip is pure dict work over a row, so a real substrate would prove nothing
extra here. That the stored node genuinely round-trips is pinned in ``packages/citation``, and that
the whole path works end to end is the ``[impl]`` PR's deployed-stack e2e (criteria 15-20).

``oraclous_citation`` is imported **function-locally** (TST001); the tests hard-fail RED with
``ModuleNotFoundError`` until the paired ``[impl]`` lands, and never skip.

RED until backend-implementer adds ``citation`` to the retrieval envelopes.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit

_GITHUB_SOURCE = {
    "source_system": "github",
    "source_id": "OraclousAI/oraclous-backend:README.md",
    "revision": "9f2a4c81b3d7e5061c8a4f2b6d90e37c15ab8e42",
    "revision_kind": "blob_sha",
    "url": "https://github.com/OraclousAI/oraclous-backend/blob/9f2a4c81/README.md",
    "title": "README.md",
    "author": None,
    "permission_ref": None,
}


def _citation():
    from oraclous_citation import SourceRef, mint_citation  # RED

    return mint_citation(SourceRef(**_GITHUB_SOURCE))


def _stored_chunk_row(citation=None) -> dict[str, Any]:
    """A repository row: ``props`` is ``properties(c)`` straight out of Cypher, so a stored
    citation arrives flattened among the chunk's own properties."""
    from oraclous_citation import citation_properties  # RED

    props: dict[str, Any] = {
        "text": "the chunk body",
        "graph_id": "graph-A",
        "organisation_id": "org-1",
        "ingestion_source": "README.md",
    }
    if citation is not None:
        props |= citation_properties(citation)
    return {"id": "4:abc:1", "labels": ["Chunk"], "props": props, "score": 0.91}


class TestTheEnvelopeCarriesCitationAsASibling:
    def test_node_result_model_accepts_a_typed_citation(self) -> None:
        from oraclous_citation import Citation  # RED
        from oraclous_knowledge_retriever_service.schema.search_schemas import NodeResultModel

        model = NodeResultModel(
            id="4:abc:1", type="Chunk", properties={"text": "x"}, citation=_citation()
        )
        assert isinstance(model.citation, Citation)

    def test_federated_node_result_model_accepts_a_typed_citation(self) -> None:
        from oraclous_citation import Citation  # RED
        from oraclous_knowledge_retriever_service.schema.federated_schemas import (
            FederatedNodeResultModel,
        )

        model = FederatedNodeResultModel(
            id="4:abc:1",
            type="Chunk",
            properties={"text": "x"},
            source_graph_id="g1",
            source_graph_name="Acme KB",
            citation=_citation(),
        )
        assert isinstance(model.citation, Citation)

    def test_citation_serialises_at_the_top_level_of_the_response(self) -> None:
        """What the console reads. A nested citation would make the FE dig through a bag whose
        other keys change per modality."""
        from oraclous_knowledge_retriever_service.schema.search_schemas import NodeResultModel

        citation = _citation()
        dumped = NodeResultModel(
            id="4:abc:1", type="Chunk", properties={"text": "x"}, citation=citation
        ).model_dump(mode="json")

        assert dumped["citation"]["citation_id"] == citation.citation_id
        assert "citation" not in dumped["properties"]

    def test_a_pre_contract_record_round_trips_as_citation_null(self) -> None:
        """Criterion 9. Existing records stay ``citation: null`` — no backfill, no migration.
        The field is present and explicitly null, so the console can tell "no source identity"
        from "this response predates the field"."""
        from oraclous_knowledge_retriever_service.schema.search_schemas import NodeResultModel

        dumped = NodeResultModel(id="4:abc:1", type="Chunk", properties={"text": "x"}).model_dump(
            mode="json"
        )

        assert "citation" in dumped
        assert dumped["citation"] is None


class TestTheProjectionLiftsCitationOutOfProperties:
    """``_to_node_result`` is the one place a stored node becomes a response envelope."""

    def test_a_stored_citation_becomes_the_typed_sibling(self) -> None:
        from oraclous_knowledge_retriever_service.services.retrieval_service import (
            _to_node_result,
        )

        citation = _citation()
        result = _to_node_result(_stored_chunk_row(citation))

        assert result["citation"] == citation

    def test_no_citation_key_survives_inside_properties(self) -> None:
        """Both halves of the rule, asserted together: it appears at the top level, and nothing
        citation-shaped is left in the bag underneath."""
        from oraclous_citation import citation_properties  # RED
        from oraclous_knowledge_retriever_service.services.retrieval_service import (
            _to_node_result,
        )

        citation = _citation()
        result = _to_node_result(_stored_chunk_row(citation))
        properties = result["properties"]

        assert not (set(citation_properties(citation)) & set(properties))
        assert not [key for key in properties if key == "citation" or key.startswith("source_")]

    def test_the_rest_of_the_node_still_reaches_the_caller(self) -> None:
        """The strip is surgical. Lifting the citation must not take the chunk's own data or the
        modality fields with it."""
        from oraclous_knowledge_retriever_service.services.retrieval_service import (
            _to_node_result,
        )

        result = _to_node_result(_stored_chunk_row(_citation()))

        assert result["properties"]["text"] == "the chunk body"
        assert result["properties"]["ingestion_source"] == "README.md"
        assert result["properties"]["score"] == 0.91

    def test_a_pre_contract_row_projects_to_a_null_citation(self) -> None:
        from oraclous_knowledge_retriever_service.services.retrieval_service import (
            _to_node_result,
        )

        result = _to_node_result(_stored_chunk_row(citation=None))

        assert result["citation"] is None

    def test_a_federated_hit_keeps_its_citation_alongside_its_source_graph(self) -> None:
        """A federated answer spans organisationally distinct graphs, so "which document" matters
        more there, not less."""
        from oraclous_knowledge_retriever_service.services.federated_service import _labeled_node
        from oraclous_knowledge_retriever_service.services.graph_registry_client import GraphInfo

        citation = _citation()
        labeled = _labeled_node(_stored_chunk_row(citation), GraphInfo(id="g1", name="Acme KB"))

        assert labeled["citation"] == citation
        assert labeled["source_graph_id"] == "g1"
        assert "citation" not in labeled["properties"]


class TestTheReadPathContract:
    """``NodeResult`` is the TypedDict every read modality returns — the citation belongs on it,
    not only on the Pydantic response model, or the two shapes drift."""

    def test_node_result_declares_a_citation_field(self) -> None:
        from oraclous_knowledge_retriever_service.contracts import NodeResult

        assert "citation" in NodeResult.__annotations__

    def test_federated_node_result_declares_a_citation_field(self) -> None:
        from oraclous_knowledge_retriever_service.contracts import FederatedNodeResult

        assert "citation" in FederatedNodeResult.__annotations__
