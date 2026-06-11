"""Unit: the Slice-C authoring aids — schema synthesis, recipe dry-run, and the from_graph_schema
projection. No Neo4j, no LLM network (the synthesizer is driven by an injected inference fn / a
fake that returns a fixed GraphSchema).
"""

from __future__ import annotations

import pytest
from neo4j_graphrag.experimental.components.schema import (
    GraphSchema,
    NodeType,
    PropertyType,
    RelationshipType,
)
from oraclous_knowledge_graph_service.domain.extraction_schema import from_graph_schema
from oraclous_knowledge_graph_service.services.dry_run_service import (
    DryRunError,
    DryRunService,
    _RecordingNoOpWriter,
)
from oraclous_knowledge_graph_service.services.recipes.templates import build_evidence_recipe
from oraclous_knowledge_graph_service.services.schema_synthesis_service import (
    SchemaSynthesisService,
)

pytestmark = pytest.mark.unit

_JSON = '[{"name": "Ada", "age": 36}, {"name": "Charles", "age": 49}]'


# --- from_graph_schema (GraphSchema -> Ontology dict) -------------------------
def test_from_graph_schema_projects_ontology_shape() -> None:
    schema = GraphSchema(
        node_types=(
            NodeType(
                label="Person",
                description="a person",
                properties=[PropertyType(name="age", type="STRING")],
            ),
            NodeType(label="Company"),
        ),
        relationship_types=(RelationshipType(label="WORKS_AT", description=""),),
        patterns=(("Person", "WORKS_AT", "Company"),),
    )
    out = from_graph_schema(schema, mode="strict")
    assert out["mode"] == "strict"
    person = next(e for e in out["entity_types"] if e["name"] == "Person")
    assert person["description"] == "a person"
    assert person["properties"] == ["age"]
    rel = out["relationship_types"][0]
    assert rel == {"name": "WORKS_AT", "source": "Person", "target": "Company"}


# --- schema synthesis service ------------------------------------------------
async def test_schema_synthesis_returns_ontology_shaped_suggestion() -> None:
    fixed = GraphSchema(
        node_types=(NodeType(label="Station"), NodeType(label="Operator")),
        relationship_types=(RelationshipType(label="OPERATED_BY", description=""),),
        patterns=(("Station", "OPERATED_BY", "Operator"),),
    )
    # inject a fake inference fn that returns a ready GraphSchema (no LLM, no await needed)
    service = SchemaSynthesisService(lambda _text: fixed)
    out = await service.suggest(sample="Stations are operated by operators.", mode="coerce")
    assert out["mode"] == "coerce"
    assert {e["name"] for e in out["entity_types"]} == {"Station", "Operator"}
    assert out["relationship_types"][0]["name"] == "OPERATED_BY"


async def test_schema_synthesis_awaits_a_coroutine_inference() -> None:
    fixed = GraphSchema(node_types=(NodeType(label="Doc"),))

    async def _async_infer(_text: str) -> GraphSchema:
        return fixed

    service = SchemaSynthesisService(lambda text: _async_infer(text))
    out = await service.suggest(sample="x")
    assert out["mode"] == "strict"  # the service default
    assert out["entity_types"] == [{"name": "Doc"}]


# --- dry-run: writes NOTHING -------------------------------------------------
def test_dry_run_writes_nothing_to_neo4j() -> None:
    # The recording no-op writer is what the dry-run runs the engine against — assert it issued no
    # Cypher (it holds no driver) and only RECORDED intent.
    writer = _RecordingNoOpWriter("dry-run")
    assert not hasattr(writer, "_driver")  # no Neo4j driver is ever opened

    out = DryRunService().preview(sample=_JSON, source_type="json")
    assert out["source_type"] == "json"
    # the default recipe maps every record to a :Record node — 2 records → 2 nodes, no writes
    assert out["node_labels"] == {"Record": 2}
    assert out["counts"]["nodes"] == 2
    assert out["counts"]["edges"] == 0


def test_dry_run_preview_reports_relationship_types_for_a_recipe() -> None:
    # the evidence recipe projects Evidence + ClaimSource nodes and a FROM_SOURCE edge
    sample = (
        '[{"id": "e1", "claim": "x", "confidence": 0.9, "label": "L", "dimensions": [],'
        ' "source": {"url": "http://a", "name": "A", "publication_date": "2020"}}]'
    )
    out = DryRunService().preview(sample=sample, source_type="json", recipe=build_evidence_recipe())
    assert set(out["node_labels"]) == {"Evidence", "ClaimSource"}
    assert "FROM_SOURCE" in out["relationship_types"]


def test_dry_run_reports_ontology_violations() -> None:
    # a strict ontology that does NOT allow :Record rejects every node (violation + skip)
    ontology = {"mode": "strict", "allowed_labels": ["Person"]}
    out = DryRunService().preview(sample=_JSON, source_type="json", ontology=ontology)
    assert out["ontology_violations"] == 2
    assert out["counts"]["nodes"] == 0


def test_dry_run_free_text_requires_llm() -> None:
    out = DryRunService().preview(sample="some prose", source_type="text")
    assert out["requires_llm"] is True


def test_dry_run_empty_sample_raises() -> None:
    with pytest.raises(DryRunError):
        DryRunService().preview(sample="[]", source_type="json")
