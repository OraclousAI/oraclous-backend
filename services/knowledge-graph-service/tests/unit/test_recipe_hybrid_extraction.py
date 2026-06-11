"""Recipe enrichment Slice 2 (oraclous-backend #269): hybrid free-text-on-a-field.

The `extractions[]` rule runs the LLM entity extractor over a designated PROSE field within a
structured recipe ingest, so structured records gain entities mined from their text + MENTIONS edges
from the record's primary node, and records interconnect by the entities they share.

No real LLM: a real `EntityExtractor` wraps a fake `LLMInterface` returning fixed entity JSON (so
the chunk-namespacing + FROM_CHUNK grouping the pass relies on is exercised genuinely), and the
deterministic projection runs through the same stateful in-memory fake writer the Slice-1 tests use,
so MERGE-by-id (entities shared across records) is observable. The factory `make_extractor` is
monkeypatched to hand back that fake extractor (or None, for the fail-soft path).
"""

from __future__ import annotations

import json

import pytest
from neo4j_graphrag.llm import LLMInterface
from neo4j_graphrag.llm.types import LLMResponse
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.services import recipes
from oraclous_knowledge_graph_service.services.entity_extractor import EntityExtractor
from oraclous_knowledge_graph_service.services.recipes import extraction_pass
from oraclous_knowledge_graph_service.services.recipes.engine import (
    RecipeValidationError,
    _deterministic_id,
    get_recipe_engine,
)
from oraclous_knowledge_graph_service.services.recipes.extraction_pass import run_extraction_pass
from oraclous_knowledge_graph_service.services.recipes.templates import build_evidence_recipe
from oraclous_knowledge_graph_service.services.structured.primitives import JsonPrimitive

pytestmark = pytest.mark.unit

assert recipes  # the package import keeps the namespace tidy for the monkeypatch target


# --- a stateful fake writer (models MERGE-by-id) — same shape the Slice-1 tests use ----
class _StatefulWriter:
    graph_id = "g-test"

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[tuple[str, str, str]] = []

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
            entity_id,
            {
                "label": label,
                "props": {},
                "identity_key": identity_key,
                "provenance": provenance,
                "aliases": [],
            },
        )
        node["label"] = label
        node["identity_key"] = identity_key
        node["provenance"] = provenance
        node["props"].update(properties)
        # Model the writer's set-union of the alias audit trail (Slice 4 resolve-on-write).
        for a in aliases or []:
            if a not in node["aliases"]:
                node["aliases"].append(a)

    def set_property(self, *, prop_name, targets) -> int:
        for t in targets:
            if t["id"] in self.nodes:
                self.nodes[t["id"]]["props"][prop_name] = t["value"]
        return len(targets)

    def merge_edge(self, *, rel_type, edges, source_id, provenance, meta) -> int:
        for e in edges:
            self.edges.append((rel_type, e["from"], e["to"]))
        return len(edges)

    def labels(self) -> list[str]:
        return [n["label"] for n in self.nodes.values()]

    def rels(self, rel_type: str) -> list[tuple[str, str]]:
        return [(f, t) for (rt, f, t) in self.edges if rt == rel_type]


class _FakeLLM(LLMInterface):
    """An LLMInterface whose every call returns the same fixed extraction JSON (entity-rich)."""

    def __init__(self, *, nodes: list[dict], relationships: list[dict]) -> None:
        super().__init__(model_name="fake")
        self._payload = json.dumps({"nodes": nodes, "relationships": relationships})

    def invoke(self, *args, **kwargs) -> LLMResponse:  # pragma: no cover - async path is used
        return LLMResponse(content=self._payload)

    async def ainvoke(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content=self._payload)


def _fake_extractor(nodes: list[dict], relationships: list[dict] | None = None) -> EntityExtractor:
    return EntityExtractor(llm=_FakeLLM(nodes=nodes, relationships=relationships or []))


def _patch_extractor(monkeypatch: pytest.MonkeyPatch, extractor) -> None:
    """Make the pass's `make_extractor(...)` hand back our fake (or None)."""
    monkeypatch.setattr(extraction_pass, "make_extractor", lambda *a, **k: extractor)


def _json_rep(records: list[dict], name: str):
    return JsonPrimitive().decompose(json.dumps(records), ExtractionMode.FULL, name=name)


_META = {"recipe_id": "rcp_x", "recipe_version": 1, "ingestion_time": "2024-01-01T00:00:00+00:00"}


def _recipe(
    records_shape: str, *, ontology: dict | None = None, link_type: str = "MENTIONS"
) -> dict:
    """A minimal recipe: an `Item` node per record + a hybrid extraction over `field:text`."""
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_hybrid-test",
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
        "extractions": [
            {
                "id": "ents",
                "from": "field:text",
                "ontology": ontology or {"entity_types": [{"name": "Person"}, {"name": "Company"}]},
                "link": {"type": link_type, "from_node_rule": "item"},
            }
        ],
    }


def _project_and_extract(recipe, rep, writer, monkeypatch, extractor, *, settings=None):
    """Run the deterministic projection then the hybrid extraction pass over the same writer."""
    from oraclous_knowledge_graph_service.core.config import Settings

    engine = get_recipe_engine()
    result = engine.execute(recipe, rep, writer)
    _patch_extractor(monkeypatch, extractor)
    stats = run_extraction_pass(
        recipe=recipe,
        representation=rep,
        writer=writer,
        node_index_by_rule=result.node_index_by_rule,
        settings=settings or Settings(extractor="openai", openai_api_key="sk-test"),
        engine=engine,
        meta=_META,
        source_id=result.source_id,
    )
    return result, stats


_PERSON_COMPANY = [
    {"id": "0", "label": "Person", "properties": {"name": "Ada"}},
    {"id": "1", "label": "Company", "properties": {"name": "Acme"}},
]


# --- 1. the extraction rule writes entities + MENTIONS from the primary node ----------------------
def test_extraction_writes_entities_and_mentions_from_primary_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rep = _json_rep([{"id": "i1", "text": "Ada works at Acme"}], "items.json")
    writer = _StatefulWriter()
    extractor = _fake_extractor(_PERSON_COMPANY)
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature), rep, writer, monkeypatch, extractor
    )

    # One Item (the deterministic primary) + the two mined entities.
    item_id = _deterministic_id("g-test", "Item", "i1")
    assert writer.nodes[item_id]["label"] == "Item"
    person_id = _deterministic_id("g-test", "Person", "ada")
    company_id = _deterministic_id("g-test", "Company", "acme")
    assert writer.nodes[person_id]["label"] == "Person"
    assert writer.nodes[company_id]["label"] == "Company"
    # The mined entities are INFERRED provenance (the LLM inferred them); name preserved on props.
    assert writer.nodes[person_id]["provenance"] == "INFERRED"
    assert writer.nodes[person_id]["props"]["name"] == "Ada"
    # MENTIONS runs primary -> entity, one per mined entity.
    mentions = writer.rels("MENTIONS")
    assert sorted(mentions) == sorted([(item_id, person_id), (item_id, company_id)])
    assert stats.entities_extracted == 2
    assert stats.mentions == 2


def test_extraction_writes_entity_inter_relationships(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = _json_rep([{"id": "i1", "text": "Ada works at Acme"}], "items.json")
    writer = _StatefulWriter()
    extractor = _fake_extractor(
        _PERSON_COMPANY,
        relationships=[{"start_node_id": "0", "end_node_id": "1", "type": "WORKS_AT"}],
    )
    _project_and_extract(_recipe(rep.shape_signature), rep, writer, monkeypatch, extractor)
    person_id = _deterministic_id("g-test", "Person", "ada")
    company_id = _deterministic_id("g-test", "Company", "acme")
    # The entity↔entity rel is translated onto the deterministic entity ids.
    assert writer.rels("WORKS_AT") == [(person_id, company_id)]


# --- 2. entity nodes MERGE-dedup across records ---------------------------------------------------
def test_entities_merge_dedup_across_records(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fake LLM returns the SAME entities for every record, so "Acme" named in three records
    # collapses to ONE node with three MENTIONS (one from each record's Item).
    records = [
        {"id": "i1", "text": "Ada at Acme"},
        {"id": "i2", "text": "Ada at Acme again"},
        {"id": "i3", "text": "Acme grows"},
    ]
    rep = _json_rep(records, "items.json")
    writer = _StatefulWriter()
    extractor = _fake_extractor(_PERSON_COMPANY)
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature), rep, writer, monkeypatch, extractor
    )

    # Two distinct entity nodes total (Ada + Acme), NOT 2-per-record.
    assert writer.labels().count("Person") == 1
    assert writer.labels().count("Company") == 1
    acme_id = _deterministic_id("g-test", "Company", "acme")
    # One MENTIONS from each of the three records' Items to the shared Acme node.
    item_ids = {_deterministic_id("g-test", "Item", f"i{n}") for n in (1, 2, 3)}
    acme_mentions = [(f, t) for f, t in writer.rels("MENTIONS") if t == acme_id]
    assert len(acme_mentions) == 3
    assert {f for f, _ in acme_mentions} == item_ids
    # entities_extracted counts every mined entity occurrence (3 records × 2), mentions likewise.
    assert stats.entities_extracted == 6
    assert stats.mentions == 6


# --- 3. extractor unavailable (None) → skipped + warned, projection unaffected --------------------
def test_extractor_none_skips_pass_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    from oraclous_knowledge_graph_service.core.config import Settings

    rep = _json_rep([{"id": "i1", "text": "Ada at Acme"}], "items.json")
    writer = _StatefulWriter()
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature),
        rep,
        writer,
        monkeypatch,
        extractor=None,  # KGS_EXTRACTOR=null
        settings=Settings(extractor="null"),
    )
    # The deterministic projection still completed: the Item node exists.
    assert writer.labels().count("Item") == 1
    # No mined entities, no MENTIONS — and a warning explains why.
    assert writer.labels().count("Person") == 0
    assert writer.rels("MENTIONS") == []
    assert stats.entities_extracted == 0
    assert any("KGS_EXTRACTOR=null" in w for w in stats.warnings)


# --- 4. a per-record extractor exception is skipped; other records still processed ----------------
def test_per_record_error_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        {"id": "good1", "text": "Ada at Acme"},
        {"id": "bad", "text": "boom"},
        {"id": "good2", "text": "Ada at Acme"},
    ]
    rep = _json_rep(records, "items.json")
    writer = _StatefulWriter()
    extractor = _fake_extractor(_PERSON_COMPANY)
    good_item_ids = {_deterministic_id("g-test", "Item", f"good{n}") for n in (1, 2)}
    bad_item_id = _deterministic_id("g-test", "Item", "bad")

    # Make writing the `bad` record's MENTIONS raise (its primary node is the edge source); the
    # other records succeed, proving the per-record failure is isolated (like on_error=IGNORE).
    def _flaky_merge_edge(self, *, rel_type, edges, source_id, provenance, meta):
        if rel_type == "MENTIONS" and any(e["from"] == bad_item_id for e in edges):
            raise RuntimeError("simulated per-record write failure")
        for e in edges:
            self.edges.append((rel_type, e["from"], e["to"]))
        return len(edges)

    monkeypatch.setattr(_StatefulWriter, "merge_edge", _flaky_merge_edge)

    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature), rep, writer, monkeypatch, extractor
    )

    # The two good records produced MENTIONS; the bad one was skipped (its failure isolated).
    mention_sources = {f for f, _ in writer.rels("MENTIONS")}
    assert mention_sources == good_item_ids
    assert bad_item_id not in mention_sources
    assert any("failed" in w for w in stats.warnings)


# --- 5. validation: from_node_rule must reference an existing node rule ---------------------------
def test_validation_unknown_from_node_rule_raises() -> None:
    rep = _json_rep([{"id": "i1", "text": "x"}], "items.json")
    recipe = _recipe(rep.shape_signature)
    recipe["extractions"][0]["link"]["from_node_rule"] = "does_not_exist"
    with pytest.raises(RecipeValidationError, match="is not a node rule"):
        get_recipe_engine().validate(recipe)


def test_validation_unsafe_link_type_raises() -> None:
    rep = _json_rep([{"id": "i1", "text": "x"}], "items.json")
    recipe = _recipe(rep.shape_signature)
    # An unsafe link type is rejected by the JSON schema (safe_identifier) before the cross-check.
    recipe["extractions"][0]["link"]["type"] = "__Evil__"
    with pytest.raises(RecipeValidationError):
        get_recipe_engine().validate(recipe)


def test_validation_strict_ontology_drops_off_type_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    # The LLM returns a Person + an off-ontology Gadget; a strict inline ontology drops the Gadget.
    rep = _json_rep([{"id": "i1", "text": "Ada and a Gadget"}], "items.json")
    writer = _StatefulWriter()
    extractor = _fake_extractor(
        [
            {"id": "0", "label": "Person", "properties": {"name": "Ada"}},
            {"id": "1", "label": "Gadget", "properties": {"name": "Widget"}},
        ]
    )
    ontology = {"mode": "strict", "entity_types": [{"name": "Person"}]}
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature, ontology=ontology), rep, writer, monkeypatch, extractor
    )
    assert writer.labels().count("Person") == 1
    assert writer.labels().count("Gadget") == 0  # off-ontology entity rejected (strict)
    assert stats.entities_extracted == 1  # only the Person counted


# --- 6. EURail-shape end-to-end over the enriched evidence template -------------------------------
def test_eurail_enriched_template_mentions_and_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three evidence records with entity-rich claims; the fake extractor returns a known entity set
    # per claim. Eurail (Organization) is mentioned in every claim -> ONE node + three MENTIONS.
    evidence = [
        {
            "id": "ev1",
            "claim": "Eurail expanded the Global Pass across Europe",
            "confidence": 0.8,
            "label": "CLAIM",
            "dimensions": ["ops"],
            "source": {"url": "https://www.eurail.com/a", "name": "A", "publication_date": "2024"},
        },
        {
            "id": "ev2",
            "claim": "Eurail partnered with Trenitalia in Italy",
            "confidence": 0.7,
            "label": "CLAIM",
            "dimensions": ["ops"],
            "source": {"url": "https://eurail.com/b", "name": "B", "publication_date": "2024"},
        },
        {
            "id": "ev3",
            "claim": "Eurail improved punctuality",
            "confidence": 0.6,
            "label": "CLAIM",
            "dimensions": ["sentiment"],
            "source": {
                "url": "https://news.example.org/x",
                "name": "X",
                "publication_date": "2024",
            },
        },
    ]
    rep = _json_rep(evidence, "evidence.json")
    recipe = build_evidence_recipe(rep.shape_signature)
    get_recipe_engine().validate(recipe)  # the Slice-2 enriched template still validates

    writer = _StatefulWriter()
    # Eurail (Organization) in every claim; ev2 also names Trenitalia + Italy.
    extractor = _fake_extractor(
        [
            {"id": "0", "label": "Organization", "properties": {"name": "Eurail"}},
        ]
    )
    _result, stats = _project_and_extract(recipe, rep, writer, monkeypatch, extractor)

    # Deterministic projection still produced the structured graph (Evidence + ClaimSource + ...).
    assert writer.labels().count("Evidence") == 3
    # The mined Organization dedups to one node across the three claims.
    assert writer.labels().count("Organization") == 1
    eurail_id = _deterministic_id("g-test", "Organization", "eurail")
    # MENTIONS runs from each Evidence node to the shared Eurail node (three records → three edges).
    evidence_ids = {_deterministic_id("g-test", "Evidence", f"ev{n}") for n in (1, 2, 3)}
    mentions = writer.rels("MENTIONS")
    assert len(mentions) == 3
    assert {f for f, _ in mentions} == evidence_ids
    assert all(t == eurail_id for _, t in mentions)
    # Slice 4 resolve-on-write: the template now carries `resolution`, so `entities_extracted` is
    # the count of CANONICAL entity NODES written (one `eurail` Organization), not per-record
    # occurrences — the dedup happens at the canonical-key level. The node carries `name`=eurail +
    # the surface form in its `aliases` audit trail. MENTIONS still runs one per source record.
    assert stats.entities_extracted == 1
    assert stats.mentions == 3
    assert writer.nodes[eurail_id]["props"]["name"] == "eurail"
    assert writer.nodes[eurail_id]["aliases"] == ["Eurail"]
