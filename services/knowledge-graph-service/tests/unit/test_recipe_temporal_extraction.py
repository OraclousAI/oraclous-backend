"""LLM temporal extraction on the recipe hybrid-extraction pass (oraclous-backend #311).

Restores the legacy `pipeline_service.py` temporal capability — lift-and-reshaped onto the shipped
#269 recipe extraction pass. A `temporal: true` extraction rule (1) appends the relationship-
temporal steering to the extractor's prompt prefix and (2) carries the LLM-mined temporal properties
(`valid_from`/`valid_to`/`event_time`/`event_time_end`) onto the written inter-entity edges, after
normalising them (year-only -> full ISO date, blanks dropped). A rule WITHOUT `temporal` behaves
exactly as before (no temporal properties written).

No real LLM: a real `EntityExtractor` wraps a fake `LLMInterface` returning a fixed entity +
relationship JSON, exercised through the genuine chunk-namespacing/grouping the pass relies on; the
stateful fake writer records each edge's properties so the temporal passthrough is observable. Pure
domain-layer helpers (`normalize_date`/`normalize_temporal_properties`/`temporal_prompt_steering`)
are unit-tested directly.
"""

from __future__ import annotations

import json

import pytest
from neo4j_graphrag.llm import LLMInterface
from neo4j_graphrag.llm.types import LLMResponse
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.domain.temporal import (
    TEMPORAL_KEYS,
    normalize_date,
    normalize_temporal_properties,
    temporal_prompt_steering,
)
from oraclous_knowledge_graph_service.services import recipes
from oraclous_knowledge_graph_service.services.entity_extractor import EntityExtractor
from oraclous_knowledge_graph_service.services.recipes import extraction_pass, resolution_pass
from oraclous_knowledge_graph_service.services.recipes.engine import get_recipe_engine
from oraclous_knowledge_graph_service.services.recipes.extraction_pass import run_extraction_pass
from oraclous_knowledge_graph_service.services.structured.primitives import JsonPrimitive

pytestmark = pytest.mark.unit

assert recipes  # keep the namespace import tidy for the monkeypatch targets


# --- a stateful fake writer that records each edge's properties -----------------------------------
class _PropWriter:
    graph_id = "g-test"

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        # (rel_type, from, to, properties-dict)
        self.edges: list[tuple[str, str, str, dict]] = []

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
        aliases=None,
    ) -> None:
        node = self.nodes.setdefault(
            entity_id, {"label": label, "props": {}, "identity_key": identity_key, "aliases": []}
        )
        node["label"] = label
        node["identity_key"] = identity_key
        node["props"].update(properties)
        for a in aliases or []:
            if a not in node["aliases"]:
                node["aliases"].append(a)

    def set_property(self, *, prop_name, targets) -> int:
        return len(targets)

    def merge_edge(self, *, rel_type, edges, source_id, provenance, meta) -> int:
        for e in edges:
            self.edges.append((rel_type, e["from"], e["to"], dict(e.get("properties") or {})))
        return len(edges)

    def edge_props(self, rel_type: str) -> list[dict]:
        return [p for (rt, _f, _t, p) in self.edges if rt == rel_type]


class _FixedLLM(LLMInterface):
    """An LLMInterface whose every call returns the same fixed extraction JSON."""

    def __init__(self, *, nodes: list[dict], relationships: list[dict]) -> None:
        super().__init__(model_name="fake")
        self._payload = json.dumps({"nodes": nodes, "relationships": relationships})

    def invoke(self, *args, **kwargs) -> LLMResponse:  # pragma: no cover - async path is used
        return LLMResponse(content=self._payload)

    async def ainvoke(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content=self._payload)


class _NoEmbedder:
    dim = 3

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - never reached
        raise AssertionError("no resolution block → embedder must not be called")


def _patch(monkeypatch: pytest.MonkeyPatch, *, extractor) -> None:
    monkeypatch.setattr(extraction_pass, "make_extractor", lambda *a, **k: extractor)
    monkeypatch.setattr(resolution_pass, "make_embedder", lambda *a, **k: _NoEmbedder())


def _json_rep(records: list[dict], name: str):
    return JsonPrimitive().decompose(json.dumps(records), ExtractionMode.FULL, name=name)


_META = {"recipe_id": "rcp_x", "recipe_version": 1, "ingestion_time": "2024-01-01T00:00:00+00:00"}


def _recipe(records_shape: str, *, temporal: bool) -> dict:
    rule: dict = {
        "id": "ents",
        "from": "field:text",
        "ontology": {
            "entity_types": [{"name": "Person"}, {"name": "Company"}],
            "relationship_types": [{"name": "WORKS_FOR", "source": "Person", "target": "Company"}],
        },
        "link": {"type": "MENTIONS", "from_node_rule": "item"},
    }
    if temporal:
        rule["temporal"] = True
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_temporal-test",
        "version": 1,
        "status": "promoted",
        "concern": "test",
        "applies_to": {"source_type": "json", "shape_signature": records_shape},
        "defaults": {"provenance": "EXTRACTED"},
        "mappings": [
            {
                "id": "item",
                "project_to": "node",
                "label": "Item",
                "match": {"unit_kind": "record"},
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:id"],
                    "normalize": ["trim"],
                },
                "properties": [{"name": "text", "value_from": "field:text"}],
            }
        ],
        "extractions": [rule],
    }


def _run(recipe, rep, writer, monkeypatch, *, extractor):
    engine = get_recipe_engine()
    engine.validate(recipe)  # the new `temporal` field must validate
    result = engine.execute(recipe, rep, writer)
    _patch(monkeypatch, extractor=extractor)
    return run_extraction_pass(
        recipe=recipe,
        representation=rep,
        writer=writer,
        node_index_by_rule=result.node_index_by_rule,
        settings=Settings(extractor="openai", openai_api_key="sk-test"),
        engine=engine,
        meta=_META,
        source_id=result.source_id,
    )


# === A. pure helpers ==============================================================================
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2023", "2023-01-01"),  # year-only -> start of year
        ("2021-03-01", "2021-03-01"),  # already ISO -> unchanged
        ("  2020-06  ", "2020-06"),  # trimmed, otherwise unchanged
        ("", None),  # empty -> None
        ("   ", None),  # blank -> None
        (None, None),  # None -> None
        (2024, None),  # non-string -> None (never coerced)
        ("not a date", "not a date"),  # passthrough (the LLM is trusted to emit ISO)
    ],
)
def test_normalize_date(value, expected) -> None:
    assert normalize_date(value) == expected


def test_normalize_temporal_properties_coerces_and_drops() -> None:
    props = {
        "valid_from": "2018",  # year -> full date
        "valid_to": "",  # blank -> dropped
        "event_time": "2018-01-01",  # ISO -> kept
        "event_time_end": None,  # None -> dropped
        "position": "CFO",  # non-temporal -> passthrough
    }
    out = normalize_temporal_properties(props)
    assert out == {"valid_from": "2018-01-01", "event_time": "2018-01-01", "position": "CFO"}
    assert props["valid_to"] == ""  # input not mutated


def test_temporal_keys_and_steering() -> None:
    assert TEMPORAL_KEYS == ("valid_from", "valid_to", "event_time", "event_time_end")
    steering = temporal_prompt_steering()
    assert "valid_from" in steering and "event_time" in steering
    assert "RELATIONSHIP" in steering  # the legacy placement rule survives


# === B. temporal ON: the mined+normalised temporal props land on the inter-entity edge ============
def test_temporal_rule_writes_normalised_temporal_props_on_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [{"id": "r1", "text": "Bob served as CFO of Acme from 2018 to 2022"}]
    rep = _json_rep(records, "items.json")
    # The fake extractor mines a Person->Company WORKS_FOR with a year-only valid_from + a blank
    # valid_to + an ISO event_time; normalization should coerce/drop accordingly.
    extractor = EntityExtractor(
        llm=_FixedLLM(
            nodes=[
                {"id": "p", "label": "Person", "properties": {"name": "Bob"}},
                {"id": "c", "label": "Company", "properties": {"name": "Acme"}},
            ],
            relationships=[
                {
                    "type": "WORKS_FOR",
                    "start_node_id": "p",
                    "end_node_id": "c",
                    "properties": {
                        "valid_from": "2018",
                        "valid_to": "",
                        "event_time": "2018-01-01",
                        "position": "CFO",
                    },
                }
            ],
        )
    )
    writer = _PropWriter()
    _run(_recipe(rep.shape_signature, temporal=True), rep, writer, monkeypatch, extractor=extractor)
    works_for = writer.edge_props("WORKS_FOR")
    assert len(works_for) == 1
    # year coerced to a full date, blank dropped, ISO kept; the non-temporal `position` is NOT
    # carried (only the four temporal keys are passed through).
    assert works_for[0] == {"valid_from": "2018-01-01", "event_time": "2018-01-01"}


# === C. temporal OFF: no temporal properties written (unchanged behaviour) ========================
def test_no_temporal_flag_writes_bare_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [{"id": "r1", "text": "Bob worked for Acme since 2018"}]
    rep = _json_rep(records, "items.json")
    extractor = EntityExtractor(
        llm=_FixedLLM(
            nodes=[
                {"id": "p", "label": "Person", "properties": {"name": "Bob"}},
                {"id": "c", "label": "Company", "properties": {"name": "Acme"}},
            ],
            relationships=[
                {
                    "type": "WORKS_FOR",
                    "start_node_id": "p",
                    "end_node_id": "c",
                    "properties": {"valid_from": "2018"},
                }
            ],
        )
    )
    writer = _PropWriter()
    _run(
        _recipe(rep.shape_signature, temporal=False), rep, writer, monkeypatch, extractor=extractor
    )
    works_for = writer.edge_props("WORKS_FOR")
    assert len(works_for) == 1
    assert works_for[0] == {}  # no `temporal` flag → temporal props are NOT carried onto the edge


# === D. temporal ON but the LLM emitted no temporal field: bare edge (no empty props) =============
def test_temporal_rule_with_no_dates_writes_bare_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [{"id": "r1", "text": "Bob knows Acme"}]
    rep = _json_rep(records, "items.json")
    extractor = EntityExtractor(
        llm=_FixedLLM(
            nodes=[
                {"id": "p", "label": "Person", "properties": {"name": "Bob"}},
                {"id": "c", "label": "Company", "properties": {"name": "Acme"}},
            ],
            relationships=[
                {"type": "WORKS_FOR", "start_node_id": "p", "end_node_id": "c", "properties": {}}
            ],
        )
    )
    writer = _PropWriter()
    _run(_recipe(rep.shape_signature, temporal=True), rep, writer, monkeypatch, extractor=extractor)
    works_for = writer.edge_props("WORKS_FOR")
    assert len(works_for) == 1
    assert works_for[0] == {}  # nothing to carry → no `properties` key set on the edge


# === E. temporal steering reaches the extractor's prompt prefix ===================================
def test_temporal_flag_appends_steering_to_prompt_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def _capture_make_extractor(settings, *, schema=None, prompt_prefix=""):  # noqa: ARG001
        captured["prefix"] = prompt_prefix
        return EntityExtractor(
            llm=_FixedLLM(
                nodes=[{"id": "p", "label": "Person", "properties": {"name": "Bob"}}],
                relationships=[],
            )
        )

    monkeypatch.setattr(extraction_pass, "make_extractor", _capture_make_extractor)
    monkeypatch.setattr(resolution_pass, "make_embedder", lambda *a, **k: _NoEmbedder())

    records = [{"id": "r1", "text": "Bob worked somewhere since 2018"}]
    rep = _json_rep(records, "items.json")
    recipe = _recipe(rep.shape_signature, temporal=True)
    engine = get_recipe_engine()
    result = engine.execute(recipe, rep, writer := _PropWriter())
    run_extraction_pass(
        recipe=recipe,
        representation=rep,
        writer=writer,
        node_index_by_rule=result.node_index_by_rule,
        settings=Settings(extractor="openai", openai_api_key="sk-test"),
        engine=engine,
        meta=_META,
        source_id=result.source_id,
    )
    # The ontology's own prefix (entity/rel types) AND the temporal steering are both present.
    assert "Temporal Extraction" in captured["prefix"]
    assert "valid_from" in captured["prefix"]
    assert "WORKS_FOR" in captured["prefix"]  # the ontology prefix is preserved, not replaced
