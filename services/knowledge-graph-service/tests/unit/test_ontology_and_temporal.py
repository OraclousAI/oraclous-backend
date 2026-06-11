"""Unit tests for S5: ontology resolution (open/strict/coerce) + the recipe engine applying
ontology + temporal at projection time (via a recording fake writer — no Neo4j).
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.domain.extraction_schema import (
    to_graph_schema,
    to_prompt_prefix,
)
from oraclous_knowledge_graph_service.domain.ontology import Ontology, resolve_label
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.services.recipes.engine import get_recipe_engine
from oraclous_knowledge_graph_service.services.structured.default_recipe import build_default_recipe
from oraclous_knowledge_graph_service.services.structured.primitives import CsvPrimitive

pytestmark = pytest.mark.unit


# --- domain ontology ----------------------------------------------------------
def test_resolve_open_or_none_passthrough() -> None:
    assert resolve_label(None, "Anything") == ("Anything", False)
    assert resolve_label(Ontology((), "open"), "Anything") == ("Anything", False)


def test_resolve_strict() -> None:
    onto = Ontology(("Person", "Org"), "strict")
    assert resolve_label(onto, "Person") == ("Person", False)
    assert resolve_label(onto, "Record") == (None, False)  # rejected


def test_resolve_coerce_near_and_far() -> None:
    onto = Ontology(("Person",), "coerce")
    assert resolve_label(onto, "Persons") == ("Person", True)  # close -> coerced
    assert resolve_label(onto, "Zzz") == (None, False)  # far -> rejected


# --- Slice B: typed ontology shape (back-compat + round-trip) -----------------
def test_ontology_of_parses_legacy_labels_only_shape() -> None:
    onto = Ontology.of({"allowed_labels": ["Person", "Org"], "mode": "strict"})
    assert onto.allowed_labels == ("Person", "Org")
    assert onto.mode == "strict"
    assert onto.entity_types == ()
    # legacy shape round-trips back to the legacy shape (no typed keys)
    assert onto.as_dict() == {"allowed_labels": ["Person", "Org"], "mode": "strict"}


def test_ontology_of_parses_typed_shape_and_derives_allowed_labels() -> None:
    onto = Ontology.of(
        {
            "mode": "strict",
            "entity_types": [
                {"name": "Person", "description": "a human", "properties": ["name", "age"]},
                {"name": "Company"},
            ],
            "relationship_types": [{"name": "WORKS_AT", "source": "Person", "target": "Company"}],
            "domain": "HR",
            "density": "dense",
            "focus": ["org charts"],
            "ignore": ["footers"],
        }
    )
    # allowed_labels DERIVED from entity_types (not from any allowed_labels key)
    assert onto.allowed_labels == ("Person", "Company")
    assert onto.entity_types[0].properties == ("name", "age")
    assert onto.relationship_types[0].source == "Person"
    assert onto.domain == "HR" and onto.density == "dense"
    assert onto.focus == ("org charts",) and onto.ignore == ("footers",)


def test_ontology_typed_shape_round_trips() -> None:
    data = {
        "allowed_labels": ["Person", "Company"],
        "mode": "strict",
        "entity_types": [
            {"name": "Person", "description": "a human", "properties": ["name", "age"]},
            {"name": "Company"},
        ],
        "relationship_types": [{"name": "WORKS_AT", "source": "Person", "target": "Company"}],
        "domain": "HR",
        "density": "dense",
        "focus": ["org charts"],
        "ignore": ["footers"],
    }
    assert Ontology.of(data).as_dict() == data


def test_ontology_density_must_be_known_else_dropped() -> None:
    onto = Ontology.of({"mode": "open", "allowed_labels": [], "density": "nonsense"})
    assert onto.density is None  # unknown density is not carried


# --- Slice B: the extraction-schema compiler ----------------------------------
def test_to_graph_schema_returns_none_when_no_entity_types() -> None:
    # legacy labels-only ontology -> stay OPEN (None), the extractor uses its default open schema
    assert to_graph_schema(Ontology(("Person",), "strict")) is None
    assert to_graph_schema(None) is None


def test_to_graph_schema_builds_node_rel_types_and_patterns() -> None:
    onto = Ontology.of(
        {
            "mode": "strict",
            "entity_types": [
                {"name": "Person", "properties": ["name"]},
                {"name": "Company"},
            ],
            "relationship_types": [
                {"name": "WORKS_AT", "source": "Person", "target": "Company"},
                {"name": "KNOWS"},  # no endpoints -> no pattern, but still a declared rel type
            ],
        }
    )
    schema = to_graph_schema(onto)
    assert {n.label for n in schema.node_types} == {"Person", "Company"}
    assert {r.label for r in schema.relationship_types} == {"WORKS_AT", "KNOWS"}
    # a pattern is emitted only for the fully-specified relationship
    assert schema.patterns == (("Person", "WORKS_AT", "Company"),)
    # node_types supplied -> the library makes the schema HARD (closed)
    assert schema.additional_node_types is False
    person = next(n for n in schema.node_types if n.label == "Person")
    assert [(p.name, p.type) for p in person.properties] == [("name", "STRING")]


def test_to_graph_schema_skips_pattern_referencing_undeclared_type() -> None:
    # WORKS_AT targets "Ghost" which is not a declared entity type -> the pattern is skipped,
    # so GraphSchema validation does not reject the whole schema.
    onto = Ontology.of(
        {
            "mode": "strict",
            "entity_types": [{"name": "Person"}],
            "relationship_types": [{"name": "WORKS_AT", "source": "Person", "target": "Ghost"}],
        }
    )
    schema = to_graph_schema(onto)
    assert schema.patterns == ()  # no invalid pattern emitted


def test_to_prompt_prefix_includes_types_density_focus() -> None:
    onto = Ontology.of(
        {
            "mode": "strict",
            "entity_types": [{"name": "Person", "description": "a human"}],
            "relationship_types": [{"name": "WORKS_AT", "source": "Person", "target": "Company"}],
            "domain": "HR",
            "density": "dense",
            "focus": ["org charts"],
            "ignore": ["footers"],
        }
    )
    prefix = to_prompt_prefix(onto)
    assert "Person" in prefix and "a human" in prefix
    assert "WORKS_AT" in prefix
    assert "HR" in prefix and "dense" in prefix
    assert "org charts" in prefix and "footers" in prefix


def test_to_prompt_prefix_empty_when_no_hints() -> None:
    # labels-only ontology carries no typed defs / hints -> empty prefix (free-form prompt)
    assert to_prompt_prefix(Ontology(("Person",), "strict")) == ""
    assert to_prompt_prefix(None) == ""


# --- engine applies ontology + temporal --------------------------------------
class _FakeWriter:
    graph_id = "g-test"

    def __init__(self) -> None:
        self.nodes: list[dict] = []

    def write_source(self, **_):  # noqa: ANN003
        pass

    def write_containers(self, **_):  # noqa: ANN003
        pass

    def link_containers(self, **_):  # noqa: ANN003
        pass

    def merge_node(self, *, label, properties, **_):  # noqa: ANN003
        self.nodes.append({"label": label, "props": properties})

    def set_property(self, **_):  # noqa: ANN003
        return 0

    def merge_edge(self, **_):  # noqa: ANN003
        return 0


def _csv_recipe():
    rep = CsvPrimitive().decompose("name\nAda\nGrace", ExtractionMode.FULL, name="p.csv")
    return rep, build_default_recipe(rep)  # default label is "Record"


def test_engine_strict_ontology_rejects_off_label() -> None:
    rep, recipe = _csv_recipe()
    writer = _FakeWriter()
    result = get_recipe_engine().execute(
        recipe, rep, writer, ontology=Ontology(("Person",), "strict")
    )
    assert result.nodes_written == 0
    assert result.ontology_violations == 2
    assert writer.nodes == []  # nothing written


def test_engine_coerce_ontology_maps_label() -> None:
    rep, recipe = _csv_recipe()
    writer = _FakeWriter()
    result = get_recipe_engine().execute(
        recipe, rep, writer, ontology=Ontology(("Recordd",), "coerce")
    )
    assert result.ontology_coercions == 2
    assert {n["label"] for n in writer.nodes} == {"Recordd"}  # Record -> Recordd


def test_engine_stamps_temporal_props() -> None:
    rep, recipe = _csv_recipe()
    writer = _FakeWriter()
    get_recipe_engine().execute(
        recipe, rep, writer, temporal={"valid_from": "2020-01-01", "event_time": None}
    )
    assert writer.nodes[0]["props"]["valid_from"] == "2020-01-01"
    assert "event_time" not in writer.nodes[0]["props"]  # None temporal values are dropped
