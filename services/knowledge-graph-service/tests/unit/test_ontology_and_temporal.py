"""Unit tests for S5: ontology resolution (open/strict/coerce) + the recipe engine applying
ontology + temporal at projection time (via a recording fake writer — no Neo4j).
"""

from __future__ import annotations

import pytest
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
