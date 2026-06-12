"""Recipe enrichment #310: auto-trigger similarity on ingest.

Two layers:
  - `synthesize_similarity_rules` (pure): from a recipe's node mappings + the sampled records, build
    one default SIMILAR_TO rule per node rule over the node's best (longest) text field; short
    code/id fields and recordless/single-record sources yield no rule.
  - the `StructuredIngestionService` wiring: when `KGS_SIMILARITY_AUTO_TRIGGER` is on AND the recipe
    declared no `similarities[]`, the synthesised rules are injected into the active recipe so the
    SAME similarity pass runs them; an explicit `similarities[]` block is never overridden, and the
    auto-trigger is off by default.
"""

from __future__ import annotations

import json

import pytest
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.services import structured_ingestion_service as svc_mod
from oraclous_knowledge_graph_service.services.recipes.auto_similarity import (
    synthesize_similarity_rules,
)
from oraclous_knowledge_graph_service.services.recipes.engine import get_recipe_engine
from oraclous_knowledge_graph_service.services.structured.primitives import JsonPrimitive
from oraclous_knowledge_graph_service.services.structured_ingestion_service import (
    StructuredIngestionService,
)

pytestmark = pytest.mark.unit


def _rep(records: list[dict], name: str = "items.json"):
    return JsonPrimitive().decompose(json.dumps(records), ExtractionMode.FULL, name=name)


def _node_recipe(properties: list[dict], shape: str, *, rule_id: str = "item") -> dict:
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_auto-test",
        "version": 1,
        "status": "promoted",
        "concern": "test",
        "applies_to": {"source_type": "json", "shape_signature": shape},
        "defaults": {"provenance": "EXTRACTED"},
        "mappings": [
            {
                "id": rule_id,
                "project_to": "node",
                "label": "Item",
                "match": {"unit_kind": "record"},
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:id"],
                    "normalize": ["trim"],
                },
                "properties": properties,
            }
        ],
    }


# --- synthesizer ----------------------------------------------------------------------------------
def test_synthesizes_a_rule_over_the_longest_text_field() -> None:
    records = [
        {"id": "1", "code": "AB", "claim": "Eurail expanded the Global Pass across new operators"},
        {"id": "2", "code": "CD", "claim": "Eurail grew the Global Pass to more rail operators"},
    ]
    rep = _rep(records)
    recipe = _node_recipe(
        [
            {"name": "code", "value_from": "field:code"},  # short → not chosen
            {"name": "claim", "value_from": "field:claim"},  # long free text → chosen
        ],
        rep.shape_signature,
    )
    rules = synthesize_similarity_rules(
        recipe=recipe, representation=rep, engine=get_recipe_engine(), min_score=0.85
    )
    assert len(rules) == 1
    rule = rules[0]
    assert rule["from"] == "field:claim"  # the longest-average-text field, not the short code
    assert rule["node_rule"] == "item"
    assert rule["edge_type"] == "SIMILAR_TO"
    assert rule["min_score"] == 0.85
    # the synthesised rule is a valid similarities[] entry.
    get_recipe_engine().validate({**recipe, "similarities": rules})


def test_no_rule_when_only_short_code_fields() -> None:
    # every field is a short code/number → nothing worth embedding → no rule.
    records = [{"id": "1", "code": "AB"}, {"id": "2", "code": "CD"}]
    rep = _rep(records)
    recipe = _node_recipe([{"name": "code", "value_from": "field:code"}], rep.shape_signature)
    rules = synthesize_similarity_rules(
        recipe=recipe, representation=rep, engine=get_recipe_engine(), min_score=0.85
    )
    assert rules == []


def test_no_rule_for_fewer_than_two_records() -> None:
    records = [{"id": "1", "claim": "a long enough free-text claim to embed"}]
    rep = _rep(records)
    recipe = _node_recipe([{"name": "claim", "value_from": "field:claim"}], rep.shape_signature)
    rules = synthesize_similarity_rules(
        recipe=recipe, representation=rep, engine=get_recipe_engine(), min_score=0.85
    )
    assert rules == []  # a similarity needs at least one pair


# --- StructuredIngestionService wiring ------------------------------------------------------------
class _RecordingEngine:
    """A stand-in engine that records the recipe handed to execute() and returns a tiny result."""

    def __init__(self) -> None:
        self.executed_recipe: dict | None = None
        self._real = get_recipe_engine()

    def execute(self, recipe, representation, writer, *, ontology=None, temporal=None):
        self.executed_recipe = recipe

        class _R:
            recipe_id = recipe["id"]
            recipe_version = recipe["version"]
            graph_id = "g"
            source_id = "s"
            node_index_by_rule: dict = {}
            similarity_edges = 0
            warnings: list = []

            def as_dict(self):
                return {"similarity_edges": self.similarity_edges}

        return _R()

    def read_record_field(self, unit, ref):
        return self._real.read_record_field(unit, ref)


def _service(settings: Settings) -> tuple[StructuredIngestionService, _RecordingEngine]:
    service = StructuredIngestionService(driver=object(), organisation_id="org", settings=settings)
    engine = _RecordingEngine()
    service._engine = engine  # noqa: SLF001 — inject the recorder for the wiring assertion
    return service, engine


_RECORDS = json.dumps(
    [
        {"id": "1", "claim": "Eurail expanded the Global Pass across new operators"},
        {"id": "2", "claim": "Eurail grew the Global Pass to more rail operators"},
    ]
)


def test_auto_trigger_injects_rules_when_on_and_recipe_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _capture(**kw):
        captured["recipe"] = kw["recipe"]
        return _ZeroStats()

    monkeypatch.setattr(svc_mod, "run_similarity_pass", _capture)
    settings = Settings(similarity_auto_trigger=True, similarity_auto_min_score=0.8)
    service, engine = _service(settings)
    service.ingest(graph_id="g", document="d", text=_RECORDS, source_type="json")
    # the engine saw a recipe carrying the synthesised similarities[] (validated by execute path).
    assert engine.executed_recipe is not None
    auto = engine.executed_recipe.get("similarities")
    assert auto and auto[0]["from"] == "field:claim" and auto[0]["min_score"] == 0.8
    # and the similarity pass ran over that same enriched recipe.
    assert captured["recipe"]["similarities"] == auto


def test_auto_trigger_off_by_default_runs_no_similarity(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def _count(**kw):
        called["n"] += 1
        return _ZeroStats()

    monkeypatch.setattr(svc_mod, "run_similarity_pass", _count)
    service, engine = _service(Settings())  # default: similarity_auto_trigger=False
    service.ingest(graph_id="g", document="d", text=_RECORDS, source_type="json")
    assert "similarities" not in (engine.executed_recipe or {})
    assert called["n"] == 0  # no similarity pass when auto-trigger is off and no authored rules


def test_explicit_similarities_block_is_never_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def _capture(**kw):
        captured["recipe"] = kw["recipe"]
        return _ZeroStats()

    monkeypatch.setattr(svc_mod, "run_similarity_pass", _capture)
    settings = Settings(similarity_auto_trigger=True)
    service, engine = _service(settings)
    authored = _node_recipe([{"name": "claim", "value_from": "field:claim"}], "shape")
    authored["similarities"] = [
        {"id": "authored", "from": "field:claim", "node_rule": "item", "min_score": 0.42}
    ]
    service.ingest(graph_id="g", document="d", text=_RECORDS, source_type="json", recipe=authored)
    # the operator's explicit rule survives untouched — no auto rule is injected over it.
    sims = engine.executed_recipe["similarities"]
    assert len(sims) == 1 and sims[0]["id"] == "authored" and sims[0]["min_score"] == 0.42


class _ZeroStats:
    similarity_edges = 0
    warnings: list = []
