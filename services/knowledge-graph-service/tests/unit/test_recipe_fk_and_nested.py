"""Slice-A engine tests (oraclous-backend #258): the two gaps that block the EURail
evidence/conflicts use case, plus the shipped recipe templates.

  G2 — `_read_field` walks a dotted nested path (`field:source.url`), keeps reading top-level keys,
       and returns None for a missing / non-dict path.
  G1 — a recipe-declared `foreign_key` edge resolves a scalar OR list `from_field` to the TARGET
       node's deterministic id (the FK value IS the target's identity), MERGE-ing a stub target when
       absent so a cross-job/cross-file ingest still links; a later ingest of the target record
       enriches the SAME id (no duplicate); a composite target identity is a clear validation error.

Engine-level only (no Neo4j): a stateful in-memory fake writer models the writer's MERGE-by-id
semantics so the stub/enrich behaviour is observable.
"""

from __future__ import annotations

import json

import pytest
from oraclous_knowledge_graph_service.domain.structural import (
    ExtractionMode,
    StructuralUnit,
    UnitKind,
)
from oraclous_knowledge_graph_service.services.recipes.engine import (
    RecipeExecutionEngine,
    RecipeValidationError,
    get_recipe_engine,
)
from oraclous_knowledge_graph_service.services.recipes.templates import (
    build_conflicts_recipe,
    build_evidence_recipe,
)
from oraclous_knowledge_graph_service.services.structured.primitives import JsonPrimitive

pytestmark = pytest.mark.unit


# --- a stateful fake writer (models MERGE-by-id) ------------------------------
class _StatefulWriter:
    """Records nodes/edges keyed by deterministic id so re-MERGEs collapse (idempotent), mirroring
    the real writer's `MERGE ... {id}` + `ON CREATE` semantics. `merge_edge_to_stub` MERGEs the
    target as a stub (stub=true, no props); `merge_node` enriches the same id (stub=false)."""

    graph_id = "g-test"

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}  # id -> {label, props, stub, identity_key}
        self.edges: list[tuple[str, str, str]] = []  # (rel_type, from_id, to_id)

    def write_source(self, *, source_id, source_type, shape_signature, meta) -> None:
        pass

    def write_containers(self, *, label, rows, source_id, meta) -> None:
        pass

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
        node = self.nodes.setdefault(
            entity_id, {"label": label, "props": {}, "stub": False, "identity_key": identity_key}
        )
        node["label"] = label
        node["identity_key"] = identity_key
        node["stub"] = False  # a real record ingest clears any prior stub flag
        node["props"].update(properties)

    def set_property(self, *, prop_name, targets) -> int:
        for t in targets:
            if t["id"] in self.nodes:
                self.nodes[t["id"]]["props"][prop_name] = t["value"]
        return len(targets)

    def merge_edge(self, *, rel_type, edges, source_id, provenance, meta) -> int:
        for e in edges:
            self.edges.append((rel_type, e["from"], e["to"]))
        return len(edges)

    def merge_edge_to_stub(
        self, *, rel_type, target_label, edges, source_id, provenance, meta
    ) -> int:
        for e in edges:
            # MERGE the target by id — create as a stub only if absent (don't clobber a real node).
            self.nodes.setdefault(
                e["to"],
                {
                    "label": target_label,
                    "props": {},
                    "stub": True,
                    "identity_key": e["target_identity_key"],
                },
            )
            self.edges.append((rel_type, e["from"], e["to"]))
        return len(edges)

    def labels(self) -> list[str]:
        return [n["label"] for n in self.nodes.values()]

    def rels(self, rel_type: str) -> list[tuple[str, str]]:
        return [(f, t) for (rt, f, t) in self.edges if rt == rel_type]


def _json_rep(records: list[dict], name: str):
    return JsonPrimitive().decompose(json.dumps(records), ExtractionMode.FULL, name=name)


# --- G2: dotted nested-path reads ---------------------------------------------
def test_read_field_walks_dotted_nested_path() -> None:
    unit = StructuralUnit(kind=UnitKind.RECORD, unit_id="record:0")
    payload = {"id": "e1", "source": {"url": "http://a", "name": "A"}}
    read = RecipeExecutionEngine._read_field
    assert read(payload, unit, "field:source.url") == "http://a"
    assert read(payload, unit, "field:source.name") == "A"


def test_read_field_still_reads_top_level() -> None:
    unit = StructuralUnit(kind=UnitKind.RECORD, unit_id="record:0")
    payload = {"id": "e1", "source": {"url": "http://a"}}
    read = RecipeExecutionEngine._read_field
    assert read(payload, unit, "field:id") == "e1"
    assert read(payload, unit, "column:id") == "e1"  # prefix-agnostic, as before


def test_read_field_missing_or_non_dict_path_returns_none() -> None:
    unit = StructuralUnit(kind=UnitKind.RECORD, unit_id="record:0")
    read = RecipeExecutionEngine._read_field
    # missing nested segment
    assert read({"source": {"url": "http://a"}}, unit, "field:source.missing") is None
    # parent key absent entirely
    assert read({"id": "e1"}, unit, "field:source.url") is None
    # intermediate segment is not a dict (a scalar can't be walked)
    assert read({"source": "scalar"}, unit, "field:source.url") is None


# --- G1: foreign_key edges ----------------------------------------------------
def _fk_recipe(shape_signature: str) -> dict:
    """A minimal conflicts recipe: a Conflict node + a foreign_key CONTRADICTS edge to Evidence."""
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_fk-test",
        "version": 1,
        "status": "promoted",
        "concern": "test",
        "applies_to": {"source_type": "json", "shape_signature": shape_signature},
        "defaults": {"provenance": "EXTRACTED"},
        "mappings": [
            {
                "id": "conflict",
                "project_to": "node",
                "label": "Conflict",
                "match": {"unit_kind": "record"},
                "identity": {"scheme": "deterministic", "from": ["field:id"]},
            },
            {
                "id": "evidence",
                "project_to": "node",
                "label": "Evidence",
                "match": {"unit_kind": "evidence_record"},  # selects nothing here
                "identity": {"scheme": "deterministic", "from": ["field:id"]},
            },
            {
                "id": "contradicts",
                "project_to": "edge",
                "type": "CONTRADICTS",
                "match": {"unit_kind": "record"},
                "from": {"node_rule": "conflict"},
                "to": {
                    "node_rule": "evidence",
                    "resolve_by": "foreign_key",
                    "from_field": "field:evidence_ids",
                },
            },
        ],
    }


def _evidence_id(graph_id: str, value: str) -> str:
    """The deterministic id the engine assigns an Evidence node whose identity is `value`."""
    from oraclous_knowledge_graph_service.services.recipes.engine import _deterministic_id

    return _deterministic_id(graph_id, "Evidence", value)


def test_foreign_key_list_value_makes_one_edge_per_id() -> None:
    records = [{"id": "cf1", "evidence_ids": ["ev1", "ev2", "ev3"]}]
    rep = _json_rep(records, "conflicts.json")
    writer = _StatefulWriter()
    get_recipe_engine().execute(_fk_recipe(rep.shape_signature), rep, writer)
    contradicts = writer.rels("CONTRADICTS")
    assert len(contradicts) == 3
    targets = {t for _, t in contradicts}
    assert targets == {_evidence_id("g-test", v) for v in ("ev1", "ev2", "ev3")}


def test_foreign_key_scalar_value_makes_one_edge() -> None:
    records = [{"id": "cf1", "evidence_ids": "ev1"}]  # scalar, not a list
    rep = _json_rep(records, "conflicts.json")
    writer = _StatefulWriter()
    get_recipe_engine().execute(_fk_recipe(rep.shape_signature), rep, writer)
    contradicts = writer.rels("CONTRADICTS")
    assert len(contradicts) == 1
    assert contradicts[0][1] == _evidence_id("g-test", "ev1")


def test_foreign_key_creates_stub_target_when_absent() -> None:
    records = [{"id": "cf1", "evidence_ids": ["ev1"]}]
    rep = _json_rep(records, "conflicts.json")
    writer = _StatefulWriter()
    get_recipe_engine().execute(_fk_recipe(rep.shape_signature), rep, writer)
    target_id = _evidence_id("g-test", "ev1")
    # The target Evidence node was not ingested yet: the FK edge created it as a stub.
    assert target_id in writer.nodes
    assert writer.nodes[target_id]["stub"] is True
    assert writer.nodes[target_id]["label"] == "Evidence"
    # The edge runs from the (real) Conflict node to that stub target.
    conflict_id = next(i for i, n in writer.nodes.items() if n["label"] == "Conflict")
    assert writer.rels("CONTRADICTS") == [(conflict_id, target_id)]


def test_foreign_key_stub_then_real_ingest_enriches_same_node_no_duplicate() -> None:
    # Run 1: conflicts ingested first → Evidence stub created by the FK edge.
    writer = _StatefulWriter()
    cf_records = [{"id": "cf1", "evidence_ids": ["ev1"]}]
    cf_rep = _json_rep(cf_records, "conflicts.json")
    get_recipe_engine().execute(_fk_recipe(cf_rep.shape_signature), cf_rep, writer)
    target_id = _evidence_id("g-test", "ev1")
    assert writer.nodes[target_id]["stub"] is True
    assert writer.nodes[target_id]["props"] == {}
    evidence_count_before = sum(1 for n in writer.nodes.values() if n["label"] == "Evidence")
    assert evidence_count_before == 1

    # Run 2 (separate job/file): the evidence record is ingested → SAME deterministic id.
    ev_recipe = {
        "recipe_format_version": "0.2",
        "id": "rcp_ev-test",
        "version": 1,
        "status": "promoted",
        "concern": "test",
        "applies_to": {"source_type": "json", "shape_signature": "x"},
        "defaults": {"provenance": "EXTRACTED"},
        "mappings": [
            {
                "id": "evidence",
                "project_to": "node",
                "label": "Evidence",
                "match": {"unit_kind": "record"},
                "identity": {"scheme": "deterministic", "from": ["field:id"]},
                "properties": [{"name": "claim", "value_from": "field:claim"}],
            }
        ],
    }
    ev_records = [{"id": "ev1", "claim": "the claim"}]
    ev_rep = _json_rep(ev_records, "evidence.json")
    ev_recipe["applies_to"]["shape_signature"] = ev_rep.shape_signature
    get_recipe_engine().execute(ev_recipe, ev_rep, writer)

    # No duplicate: still exactly one Evidence node, the same id, now enriched + un-stubbed.
    evidence_ids = [i for i, n in writer.nodes.items() if n["label"] == "Evidence"]
    assert evidence_ids == [target_id]
    assert writer.nodes[target_id]["stub"] is False
    assert writer.nodes[target_id]["props"]["claim"] == "the claim"
    # The CONTRADICTS edge formed in run 1 still points at that (now enriched) node.
    assert writer.rels("CONTRADICTS")[0][1] == target_id


def test_foreign_key_composite_target_identity_is_validation_error() -> None:
    recipe = _fk_recipe("json(evidence_ids:array,id:string)")
    # Make the Evidence (target) identity composite — no single FK value can stand in for it.
    recipe["mappings"][1]["identity"]["from"] = ["field:id", "field:kind"]
    with pytest.raises(RecipeValidationError, match="single-field identity"):
        get_recipe_engine().validate(recipe)


def test_foreign_key_missing_from_field_is_validation_error() -> None:
    recipe = _fk_recipe("json(evidence_ids:array,id:string)")
    del recipe["mappings"][2]["to"]["from_field"]
    with pytest.raises(RecipeValidationError, match="from_field"):
        get_recipe_engine().validate(recipe)


# --- EURail-shaped end-to-end (templates) -------------------------------------
def test_eurail_evidence_and_conflicts_end_to_end() -> None:
    evidence = [
        {
            "id": "ev1",
            "claim": "Punctuality improved",
            "confidence": 0.8,
            "label": "CLAIM",
            "dimensions": ["ops"],
            "source": {
                "url": "http://eurail/a",
                "name": "EURail Report",
                "publication_date": "2024-01-01",
            },
        },
        {
            "id": "ev2",
            "claim": "Punctuality declined",
            "confidence": 0.6,
            "label": "CLAIM",
            "dimensions": ["ops"],
            "source": {
                "url": "http://news/b",
                "name": "News B",
                "publication_date": "2024-03-01",
            },
        },
    ]
    conflicts = [
        {
            "id": "cf1",
            "topic": "punctuality",
            "resolution": "unresolved",
            "synthesis_note": "mixed signals",
            "evidence_ids": ["ev1", "ev2"],
        }
    ]
    engine = get_recipe_engine()
    writer = _StatefulWriter()
    ev_rep = _json_rep(evidence, "evidence.json")
    cf_rep = _json_rep(conflicts, "conflicts.json")

    engine.execute(build_evidence_recipe(ev_rep.shape_signature), ev_rep, writer)
    engine.execute(build_conflicts_recipe(cf_rep.shape_signature), cf_rep, writer)

    labels = writer.labels()
    assert labels.count("Evidence") == 2
    assert labels.count("ClaimSource") == 2  # nested source.url identity (G2)
    assert labels.count("Conflict") == 1

    # FROM_SOURCE: one per evidence record (same-record identity edge over nested source).
    assert len(writer.rels("FROM_SOURCE")) == 2
    # CONTRADICTS: one per evidence id on the conflict's list-valued FK (G1).
    contradicts = writer.rels("CONTRADICTS")
    assert len(contradicts) == 2
    # Every CONTRADICTS target is a REAL Evidence node id — proving the cross-file deterministic
    # link (the conflicts recipe never saw the evidence records, only their ids).
    evidence_ids = {i for i, n in writer.nodes.items() if n["label"] == "Evidence"}
    assert {t for _, t in contradicts} == evidence_ids
    assert all(n["stub"] is False for n in writer.nodes.values() if n["label"] == "Evidence")


def test_eurail_templates_validate() -> None:
    engine = get_recipe_engine()
    engine.validate(build_evidence_recipe())
    engine.validate(build_conflicts_recipe())
