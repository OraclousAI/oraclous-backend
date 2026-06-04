"""Unit tests for the S3 structured (CSV/JSON) ingestion: extractors, primitives, default recipe,
the recipe engine (via a recording fake writer — no Neo4j), and the write-boundary safety check.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.repositories.recipe_write_repository import (
    UnsafeIdentifierError,
    _safe,
)
from oraclous_knowledge_graph_service.services.recipes.engine import (
    RecipeValidationError,
    get_recipe_engine,
)
from oraclous_knowledge_graph_service.services.structured.default_recipe import build_default_recipe
from oraclous_knowledge_graph_service.services.structured.extractors import (
    extract_csv,
    extract_json,
)
from oraclous_knowledge_graph_service.services.structured.primitives import (
    CsvPrimitive,
    JsonPrimitive,
)

pytestmark = pytest.mark.unit

_CSV = "name,age,active\nAda,36,true\nCharles,49,false"
_JSON = '[{"name": "Ada", "age": 36}, {"name": "Charles", "age": 49}]'


# --- extractors ---------------------------------------------------------------
def test_extract_csv_infers_types() -> None:
    out = extract_csv(_CSV)
    assert out["columns"] == ["name", "age", "active"]
    assert out["row_count"] == 2
    assert out["schema"] == {"name": "str", "age": "int", "active": "bool"}


def test_extract_json_schema() -> None:
    out = extract_json(_JSON)
    assert out["record_count"] == 2
    assert out["field_schema"] == {"name": "string", "age": "integer"}


# --- primitives ---------------------------------------------------------------
def test_csv_primitive_emits_source_table_columns_records() -> None:
    rep = CsvPrimitive().decompose(_CSV, ExtractionMode.FULL, name="people.csv")
    kinds = [u.kind.value for u in rep.units]
    assert kinds.count("source") == 1
    assert kinds.count("table") == 1
    assert kinds.count("column") == 3
    assert kinds.count("record") == 2
    record = next(u for u in rep.units if u.kind.value == "record")
    assert record.sample_values[0] == {"name": "Ada", "age": "36", "active": "true"}
    assert rep.shape_signature == "csv(name:str,age:int,active:bool)"


def test_json_primitive_emits_fields_and_records() -> None:
    rep = JsonPrimitive().decompose(_JSON, ExtractionMode.FULL, name="people.json")
    assert [u.kind.value for u in rep.units].count("record") == 2
    assert [u.kind.value for u in rep.units].count("field") == 2


# --- default recipe -----------------------------------------------------------
def test_default_recipe_is_valid_and_maps_columns() -> None:
    rep = CsvPrimitive().decompose(_CSV, ExtractionMode.FULL, name="p.csv")
    recipe = build_default_recipe(rep)
    get_recipe_engine().validate(recipe)  # raises on invalid
    rule = recipe["mappings"][0]
    assert rule["label"] == "Record"
    assert {p["name"] for p in rule["properties"]} == {"name", "age", "active"}


def test_default_recipe_sanitises_unsafe_column_names() -> None:
    rep = CsvPrimitive().decompose("First Name,2nd\nA,B", ExtractionMode.FULL, name="p.csv")
    recipe = build_default_recipe(rep)
    get_recipe_engine().validate(recipe)  # must still validate (keys sanitised)
    names = {p["name"] for p in recipe["mappings"][0]["properties"]}
    assert all(n.replace("_", "a").isalnum() for n in names)  # safe identifiers


# --- engine (recording fake writer) ------------------------------------------
class _FakeWriter:
    graph_id = "g-test"

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.containers: list[tuple[str, list]] = []
        self.nodes: list[dict] = []
        self.edges: list[tuple] = []

    def write_source(self, *, source_id, source_type, shape_signature, meta) -> None:
        self.sources.append(source_id)

    def write_containers(self, *, label, rows, source_id, meta) -> None:
        self.containers.append((label, [r["id"] for r in rows]))

    def link_containers(self, *, pairs) -> None:
        pass

    def merge_node(
        self,
        *,
        label,
        entity_id,
        identity_key,
        properties,
        provenance,
        source_id,
        meta,
        confidence,
        container_id,
    ) -> None:
        self.nodes.append(
            {"label": label, "id": entity_id, "props": properties, "container": container_id}
        )

    def set_property(self, *, prop_name, targets) -> int:
        return len(targets)

    def merge_edge(self, *, rel_type, edges, source_id, provenance, meta) -> int:
        self.edges.append((rel_type, edges))
        return len(edges)


def test_engine_csv_default_recipe_writes_records() -> None:
    rep = CsvPrimitive().decompose(_CSV, ExtractionMode.FULL, name="p.csv")
    recipe = build_default_recipe(rep)
    writer = _FakeWriter()
    result = get_recipe_engine().execute(recipe, rep, writer)
    assert result.nodes_written == 2
    assert result.containers_written == 1
    assert [n["label"] for n in writer.nodes] == ["Record", "Record"]
    assert writer.nodes[0]["props"]["name"] == "Ada"
    assert writer.nodes[0]["props"]["age"] == "36"
    assert writer.containers[0][0] == "Table"
    # records derive from the :Table container (their parent), not the bare source
    assert all(n["container"] is not None for n in writer.nodes)


def test_engine_ids_are_deterministic() -> None:
    rep = CsvPrimitive().decompose(_CSV, ExtractionMode.FULL, name="p.csv")
    recipe = build_default_recipe(rep)
    a, b = _FakeWriter(), _FakeWriter()
    get_recipe_engine().execute(recipe, rep, a)
    get_recipe_engine().execute(recipe, rep, b)
    assert [n["id"] for n in a.nodes] == [n["id"] for n in b.nodes]  # idempotent MERGE keys


# --- security -----------------------------------------------------------------
def test_write_boundary_rejects_unsafe_identifiers() -> None:
    assert _safe("Person") == "Person"
    for bad in ["__Evil__", "1bad", "drop table", "a-b", "__Entity__"]:
        with pytest.raises(UnsafeIdentifierError):
            _safe(bad)


def test_engine_rejects_recipe_with_container_label_collision() -> None:
    rep = CsvPrimitive().decompose(_CSV, ExtractionMode.FULL, name="p.csv")
    recipe = build_default_recipe(rep)
    recipe["mappings"][0]["label"] = "Table"  # collides with a platform container label
    with pytest.raises(RecipeValidationError):
        get_recipe_engine().execute(recipe, rep, _FakeWriter())
