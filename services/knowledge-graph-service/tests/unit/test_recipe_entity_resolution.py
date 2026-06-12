"""Recipe enrichment Slice 4 (oraclous-backend #269): entity resolution / canonicalization.

RESOLVE-ON-WRITE — an extraction rule's `resolution` block canonicalizes the mined entities DURING
ingestion so a company's surface variants (`Eurail` / `eurail.com` / `Eurail B.V.`) become ONE node
keyed by the canonical key, with the original forms kept in an `aliases` audit trail. A conservative
SEMANTIC pass then clusters the distinct canonical names per label, folds near-duplicates (cosine >=
merge_threshold) onto one representative, and flags the ambiguous band ([candidate_threshold,
merge_threshold)) with a `SAME_AS_CANDIDATE {score}` edge — never merging across labels.

No real LLM / embedding API: a real `EntityExtractor` wraps a fake `LLMInterface` returning fixed
entity JSON (so the chunk-grouping the pass relies on is exercised genuinely), and a FAKE embedder
hands back hand-chosen vectors keyed by canonical name, so the cosine clustering / thresholds / fold
/ candidate-band are deterministic. The same stateful in-memory fake writer the Slice-1/2/3 tests
use (extended with an `aliases` set-union) makes MERGE-by-id + the alias trail observable.
"""

from __future__ import annotations

import json

import pytest
from neo4j_graphrag.llm import LLMInterface
from neo4j_graphrag.llm.types import LLMResponse
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.recipes.transforms import (
    apply_transform,
    canonical,
    is_known_transform,
)
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.services import recipes
from oraclous_knowledge_graph_service.services.entity_extractor import EntityExtractor
from oraclous_knowledge_graph_service.services.recipes import resolution_pass
from oraclous_knowledge_graph_service.services.recipes.engine import (
    RecipeValidationError,
    _deterministic_id,
    get_recipe_engine,
)
from oraclous_knowledge_graph_service.services.recipes.extraction_pass import run_extraction_pass
from oraclous_knowledge_graph_service.services.recipes.templates import build_evidence_recipe
from oraclous_knowledge_graph_service.services.structured.primitives import JsonPrimitive

pytestmark = pytest.mark.unit

assert recipes  # keep the namespace import tidy for the monkeypatch targets


# --- a stateful fake writer (MERGE-by-id + aliases set-union + per-edge score) --------------------
class _StatefulWriter:
    graph_id = "g-test"

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        # (rel_type, from, to, score) — score is a candidate edge's `properties.score`, else None.
        self.edges: list[tuple[str, str, str, float | None]] = []

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
        for a in aliases or []:  # model the writer's set-union of the alias audit trail
            if a not in node["aliases"]:
                node["aliases"].append(a)

    def set_property(self, *, prop_name, targets) -> int:
        for t in targets:
            if t["id"] in self.nodes:
                self.nodes[t["id"]]["props"][prop_name] = t["value"]
        return len(targets)

    def merge_edge(self, *, rel_type, edges, source_id, provenance, meta) -> int:
        for e in edges:
            score = (e.get("properties") or {}).get("score")
            self.edges.append((rel_type, e["from"], e["to"], score))
        return len(edges)

    def labels(self) -> list[str]:
        return [n["label"] for n in self.nodes.values()]

    def rels(self, rel_type: str) -> list[tuple[str, str]]:
        return [(f, t) for (rt, f, t, _s) in self.edges if rt == rel_type]

    def scored_rels(self, rel_type: str) -> list[tuple[str, str, float | None]]:
        return [(f, t, s) for (rt, f, t, s) in self.edges if rt == rel_type]


class _FakeLLM(LLMInterface):
    """An LLMInterface whose every call returns the same fixed extraction JSON."""

    def __init__(self, *, nodes: list[dict], relationships: list[dict]) -> None:
        super().__init__(model_name="fake")
        self._payload = json.dumps({"nodes": nodes, "relationships": relationships})

    def invoke(self, *args, **kwargs) -> LLMResponse:  # pragma: no cover - async path is used
        return LLMResponse(content=self._payload)

    async def ainvoke(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content=self._payload)


def _fake_extractor(nodes: list[dict], relationships: list[dict] | None = None) -> EntityExtractor:
    return EntityExtractor(llm=_FakeLLM(nodes=nodes, relationships=relationships or []))


class _PerRecordLLM(LLMInterface):
    """An LLMInterface that returns a DIFFERENT entity set per chunk text (keyed by the prose)."""

    def __init__(self, by_text: dict[str, list[dict]]) -> None:
        super().__init__(model_name="fake")
        self._by_text = by_text

    def invoke(self, *args, **kwargs) -> LLMResponse:  # pragma: no cover - async path is used
        return self._respond(*args, **kwargs)

    async def ainvoke(self, *args, **kwargs) -> LLMResponse:
        return self._respond(*args, **kwargs)

    def _respond(self, input_text="", *args, **kwargs) -> LLMResponse:
        # The chunk text is embedded in the extractor's prompt (first positional arg); scan it.
        # Match the LONGEST registered key first so an overlapping prefix ("Eurail"/"Eurailways")
        # never shadows the more specific record text.
        text = input_text if isinstance(input_text, str) else ""
        for key in sorted(self._by_text, key=len, reverse=True):
            if key in text:
                return LLMResponse(
                    content=json.dumps({"nodes": self._by_text[key], "relationships": []})
                )
        return LLMResponse(content=json.dumps({"nodes": [], "relationships": []}))


class _FakeEmbedder:
    dim = 3

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Unknown text → a far-away unit vector so it never accidentally clusters.
        return [list(self._vectors.get(t, [0.0, 0.0, 1.0])) for t in texts]


class _BoomEmbedder:
    dim = 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("simulated embedder failure")


def _patch(monkeypatch: pytest.MonkeyPatch, *, extractor, embedder) -> None:
    """Hand the extraction pass our fake extractor and the resolution pass our fake embedder."""
    from oraclous_knowledge_graph_service.services.recipes import extraction_pass

    monkeypatch.setattr(extraction_pass, "make_extractor", lambda *a, **k: extractor)
    monkeypatch.setattr(resolution_pass, "make_embedder", lambda *a, **k: embedder)


def _json_rep(records: list[dict], name: str):
    return JsonPrimitive().decompose(json.dumps(records), ExtractionMode.FULL, name=name)


_META = {"recipe_id": "rcp_x", "recipe_version": 1, "ingestion_time": "2024-01-01T00:00:00+00:00"}


def _recipe(
    records_shape: str,
    *,
    resolution: dict,
    ontology: dict | None = None,
) -> dict:
    """A minimal recipe: an `Item` node per record + a hybrid extraction over `field:text` with a
    Slice-4 `resolution` block."""
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_resolution-test",
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
                "ontology": ontology
                or {"entity_types": [{"name": "Organization"}, {"name": "Product"}]},
                "resolution": resolution,
                "link": {"type": "MENTIONS", "from_node_rule": "item"},
            }
        ],
    }


def _project_and_extract(recipe, rep, writer, monkeypatch, *, extractor, embedder, settings=None):
    engine = get_recipe_engine()
    result = engine.execute(recipe, rep, writer)
    _patch(monkeypatch, extractor=extractor, embedder=embedder)
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


# === A. the `canonical` transform: a big table ===================================================
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # the three eurail variants the brief calls out → one canonical key
        ("Eurail B.V.", "eurail"),
        ("eurail.com", "eurail"),
        ("Eurail Group", "eurail"),
        ("www.eurail.com", "eurail"),
        ("EURAIL", "eurail"),
        ("  Eurail   B.V.  ", "eurail"),  # whitespace collapsed
        ("eurail.co.uk", "eurail"),  # multi-label TLD stem
        # stacked legal suffixes stripped repeatedly
        ("Eurail Group Holding", "eurail"),
        ("Acme Corporation", "acme"),
        ("Acme, Inc.", "acme"),  # punctuation around the suffix token
        ("Foo Bar Ltd", "foo bar"),
        ("Rail Europe Inc", "rail europe"),
        ("DB AG", "db"),
        # distinct names stay distinct (the over-merge guard at the keying layer)
        ("Interrail", "interrail"),
        ("SNCF", "sncf"),
        ("SBB", "sbb"),
        ("Trenitalia", "trenitalia"),
        # all-suffix / empty → empty (an empty identity, skipped by the caller)
        ("Ltd", ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_canonical_transform_table(name: str, expected: str) -> None:
    assert canonical(name) == expected
    assert apply_transform("canonical", name) == expected  # registered + reachable by name


def test_canonical_is_registered() -> None:
    assert is_known_transform("canonical") is True


def test_canonical_distinct_names_never_collide() -> None:
    # The brief's explicit non-collisions: Interrail ↛ eurail, SNCF ↛ SBB.
    assert canonical("Interrail") != canonical("Eurail B.V.")
    assert canonical("SNCF") != canonical("SBB")


# === B. canonical keying (resolve-on-write): two surface forms → ONE node, aliases=both ===========
def test_two_surface_forms_collapse_to_one_node_with_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two records name the SAME org two different ways → one canonical `eurail` Organization node,
    # aliases = both surface forms, canonical_name = the longest surface form, name = the key.
    records = [
        {"id": "r1", "text": "about eurail.com"},
        {"id": "r2", "text": "about Eurail B.V."},
    ]
    rep = _json_rep(records, "items.json")
    extractor = _PerRecordLLM(
        {
            "eurail.com": [
                {"id": "0", "label": "Organization", "properties": {"name": "eurail.com"}}
            ],
            "Eurail B.V.": [
                {"id": "0", "label": "Organization", "properties": {"name": "Eurail B.V."}}
            ],
        }
    )
    writer = _StatefulWriter()
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature, resolution={"canonical": True}),
        rep,
        writer,
        monkeypatch,
        extractor=EntityExtractor(llm=extractor),
        embedder=_FakeEmbedder({}),
    )
    assert writer.labels().count("Organization") == 1  # ONE node, not two
    eurail_id = _deterministic_id("g-test", "Organization", "eurail")
    node = writer.nodes[eurail_id]
    assert node["identity_key"] == "eurail"
    assert node["props"]["name"] == "eurail"
    assert node["props"]["canonical_name"] == "Eurail B.V."  # longest surface form seen
    assert sorted(node["aliases"]) == ["Eurail B.V.", "eurail.com"]  # both forms in the audit trail
    # One MENTIONS from each record's Item to the single canonical node.
    mentions = writer.rels("MENTIONS")
    assert len(mentions) == 2
    assert all(t == eurail_id for _, t in mentions)
    assert stats.entities_extracted == 1  # ONE canonical node written


# === B. explicit alias map applied BEFORE the transform ===========================================
def test_explicit_alias_map_applied_before_transform(monkeypatch: pytest.MonkeyPatch) -> None:
    # The transform alone would NOT fold "Global Pass" onto "eurailpass"; the explicit map does.
    records = [
        {"id": "r1", "text": "Eurailpass deal"},
        {"id": "r2", "text": "Global Pass deal"},
    ]
    rep = _json_rep(records, "items.json")
    extractor = _PerRecordLLM(
        {
            "Eurailpass": [{"id": "0", "label": "Product", "properties": {"name": "Eurailpass"}}],
            "Global Pass": [{"id": "0", "label": "Product", "properties": {"name": "Global Pass"}}],
        }
    )
    writer = _StatefulWriter()
    resolution = {"canonical": True, "aliases": {"eurailpass": ["Global Pass"]}}
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature, resolution=resolution),
        rep,
        writer,
        monkeypatch,
        extractor=EntityExtractor(llm=extractor),
        embedder=_FakeEmbedder({}),
    )
    # Both map onto the `eurailpass` canonical key: "Eurailpass" via the transform, "Global Pass"
    # via the explicit map (applied first) → ONE Product node.
    assert writer.labels().count("Product") == 1
    pass_id = _deterministic_id("g-test", "Product", "eurailpass")
    assert pass_id in writer.nodes
    assert sorted(writer.nodes[pass_id]["aliases"]) == ["Eurailpass", "Global Pass"]
    assert stats.entities_extracted == 1


# === C. semantic merge: two canonical nodes >= merge_threshold → one survivor =====================
def test_semantic_merge_folds_near_duplicate_canonical_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two DISTINCT canonical keys (`eurail`, `eurailways`) the transform cannot fold, but whose
    # embeddings are near-identical (cosine ~1 >= 0.92) → the semantic pass folds them onto ONE
    # representative; MENTIONS from BOTH records re-point to it, aliases unioned.
    records = [
        {"id": "r1", "text": "Eurail"},
        {"id": "r2", "text": "Eurailways"},
    ]
    rep = _json_rep(records, "items.json")
    extractor = _PerRecordLLM(
        {
            "Eurail": [{"id": "0", "label": "Organization", "properties": {"name": "Eurail"}}],
            "Eurailways": [
                {"id": "0", "label": "Organization", "properties": {"name": "Eurailways"}}
            ],
        }
    )
    embedder = _FakeEmbedder({"eurail": [1.0, 0.0, 0.0], "eurailways": [0.999, 0.04, 0.0]})
    writer = _StatefulWriter()
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature, resolution={"canonical": True, "merge_threshold": 0.92}),
        rep,
        writer,
        monkeypatch,
        extractor=EntityExtractor(llm=extractor),
        embedder=embedder,
    )
    # ONE survivor Organization (the two near-identical canonical names folded).
    assert writer.labels().count("Organization") == 1
    survivor = next(i for i, n in writer.nodes.items() if n["label"] == "Organization")
    # The representative is the longest canonical key (`eurailways`); both forms in aliases.
    assert sorted(writer.nodes[survivor]["aliases"]) == ["Eurail", "Eurailways"]
    # MENTIONS from BOTH records re-pointed onto the single survivor.
    mentions = writer.rels("MENTIONS")
    assert len(mentions) == 2
    assert all(t == survivor for _, t in mentions)
    assert stats.entities_merged == 1  # one canonical name folded onto the representative
    assert writer.rels("SAME_AS_CANDIDATE") == []  # a clean merge, not an ambiguous candidate


# === C. candidate band: in [candidate, merge) → SAME_AS_CANDIDATE edge, NOT merged ================
def test_ambiguous_band_flags_candidate_edge_not_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two canonical names whose cosine ~0.88 sits in [0.85, 0.92): NOT merged — a SAME_AS_CANDIDATE
    # {score} edge is MERGEd between the two canonical nodes for review; both nodes survive.
    records = [
        {"id": "r1", "text": "Eurail"},
        {"id": "r2", "text": "Interrail"},
    ]
    rep = _json_rep(records, "items.json")
    extractor = _PerRecordLLM(
        {
            "Eurail": [{"id": "0", "label": "Organization", "properties": {"name": "Eurail"}}],
            "Interrail": [
                {"id": "0", "label": "Organization", "properties": {"name": "Interrail"}}
            ],
        }
    )
    # cosine(eurail, interrail) = 0.88 → ambiguous band.
    embedder = _FakeEmbedder({"eurail": [1.0, 0.0, 0.0], "interrail": [0.88, 0.4750, 0.0]})
    writer = _StatefulWriter()
    resolution = {"canonical": True, "merge_threshold": 0.92, "candidate_threshold": 0.85}
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature, resolution=resolution),
        rep,
        writer,
        monkeypatch,
        extractor=EntityExtractor(llm=extractor),
        embedder=embedder,
    )
    # BOTH canonical nodes survive (NOT merged).
    assert writer.labels().count("Organization") == 2
    eurail_id = _deterministic_id("g-test", "Organization", "eurail")
    interrail_id = _deterministic_id("g-test", "Organization", "interrail")
    assert {eurail_id, interrail_id} <= set(writer.nodes)
    # A single SAME_AS_CANDIDATE edge between them, carrying the cosine score.
    candidate = writer.scored_rels("SAME_AS_CANDIDATE")
    assert len(candidate) == 1
    f, t, score = candidate[0]
    assert {f, t} == {eurail_id, interrail_id}
    assert score == pytest.approx(0.88, abs=1e-3)
    assert stats.entities_merged == 0  # nothing folded
    assert stats.resolution_candidates == 1


# === C. candidate-write routes through the suppression-aware merge when available (#279) ==========
def test_candidate_edges_use_suppression_aware_writer_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A writer that exposes `merge_candidate_edges` (the real RecipeGraphWriter, which skips
    # NOT_SAME_AS-rejected pairs) must receive the candidate edges through THAT method, not the
    # generic `merge_edge` — so a previously-rejected pair stops resurfacing on re-ingest.
    class _SuppressionAwareWriter(_StatefulWriter):
        def __init__(self) -> None:
            super().__init__()
            self.candidate_calls = 0

        def merge_candidate_edges(self, *, edges, source_id, provenance, meta) -> int:
            self.candidate_calls += 1
            for e in edges:
                score = (e.get("properties") or {}).get("score")
                self.edges.append(("SAME_AS_CANDIDATE", e["from"], e["to"], score))
            return len(edges)

    records = [{"id": "r1", "text": "Eurail"}, {"id": "r2", "text": "Interrail"}]
    rep = _json_rep(records, "items.json")
    extractor = _PerRecordLLM(
        {
            "Eurail": [{"id": "0", "label": "Organization", "properties": {"name": "Eurail"}}],
            "Interrail": [
                {"id": "0", "label": "Organization", "properties": {"name": "Interrail"}}
            ],
        }
    )
    embedder = _FakeEmbedder({"eurail": [1.0, 0.0, 0.0], "interrail": [0.88, 0.4750, 0.0]})
    writer = _SuppressionAwareWriter()
    resolution = {"canonical": True, "merge_threshold": 0.92, "candidate_threshold": 0.85}
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature, resolution=resolution),
        rep,
        writer,
        monkeypatch,
        extractor=EntityExtractor(llm=extractor),
        embedder=embedder,
    )
    assert writer.candidate_calls == 1  # routed through the suppression-aware path
    assert len(writer.scored_rels("SAME_AS_CANDIDATE")) == 1
    assert stats.resolution_candidates == 1


# === C. over-merge guard: different labels never merge ============================================
def test_different_labels_never_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    # An Organization and a Product with IDENTICAL embeddings (cosine 1) must NOT merge — clustering
    # is strictly per-label. Two nodes survive; no candidate edge across the label boundary.
    records = [
        {"id": "r1", "text": "Eurail org"},
        {"id": "r2", "text": "Eurail product"},
    ]
    rep = _json_rep(records, "items.json")
    extractor = _PerRecordLLM(
        {
            "Eurail org": [{"id": "0", "label": "Organization", "properties": {"name": "Eurail"}}],
            "Eurail product": [{"id": "0", "label": "Product", "properties": {"name": "Eurail"}}],
        }
    )
    embedder = _FakeEmbedder({"eurail": [1.0, 0.0, 0.0]})  # same key, same vector, but two labels
    writer = _StatefulWriter()
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature, resolution={"canonical": True}),
        rep,
        writer,
        monkeypatch,
        extractor=EntityExtractor(llm=extractor),
        embedder=embedder,
    )
    assert writer.labels().count("Organization") == 1
    assert writer.labels().count("Product") == 1  # never folded into the Organization
    assert stats.entities_merged == 0
    assert writer.rels("SAME_AS_CANDIDATE") == []  # no cross-label candidate either


# === C. fail-soft: embedder raises → semantic pass skipped, entities intact (keying stands) ===
def test_embedder_failure_skips_semantic_pass_entities_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        {"id": "r1", "text": "Eurail"},
        {"id": "r2", "text": "Eurailways"},
    ]
    rep = _json_rep(records, "items.json")
    extractor = _PerRecordLLM(
        {
            "Eurail": [{"id": "0", "label": "Organization", "properties": {"name": "Eurail"}}],
            "Eurailways": [
                {"id": "0", "label": "Organization", "properties": {"name": "Eurailways"}}
            ],
        }
    )
    writer = _StatefulWriter()
    _result, stats = _project_and_extract(
        _recipe(rep.shape_signature, resolution={"canonical": True}),
        rep,
        writer,
        monkeypatch,
        extractor=EntityExtractor(llm=extractor),
        embedder=_BoomEmbedder(),
    )
    # The deterministic canonical keying still stands: the two distinct canonical names are intact
    # (NOT folded, since the semantic pass was skipped), no candidate edge, a warning explains it.
    assert writer.labels().count("Organization") == 2
    assert stats.entities_merged == 0
    assert writer.rels("SAME_AS_CANDIDATE") == []
    assert any("embedder failed" in w for w in stats.warnings)
    assert stats.entities_extracted == 2  # both canonical nodes written


# === B. validation: thresholds in (0,1], merge >= candidate =======================================
def test_validation_merge_below_candidate_raises() -> None:
    rep = _json_rep([{"id": "r1", "text": "x"}], "items.json")
    recipe = _recipe(
        rep.shape_signature,
        resolution={"canonical": True, "merge_threshold": 0.80, "candidate_threshold": 0.90},
    )
    with pytest.raises(RecipeValidationError, match="must be >= candidate_threshold"):
        get_recipe_engine().validate(recipe)


def test_validation_threshold_out_of_range_raises() -> None:
    rep = _json_rep([{"id": "r1", "text": "x"}], "items.json")
    recipe = _recipe(rep.shape_signature, resolution={"canonical": True, "merge_threshold": 1.5})
    with pytest.raises(RecipeValidationError):  # JSON-schema maximum:1
        get_recipe_engine().validate(recipe)


def test_resolution_defaults_validate_without_optional_fields() -> None:
    rep = _json_rep([{"id": "r1", "text": "x"}], "items.json")
    recipe = _recipe(rep.shape_signature, resolution={"canonical": True})
    get_recipe_engine().validate(recipe)  # must not raise (defaults 0.92/0.85 satisfy merge>=cand)


# === F. EURail e2e: 3 evidence (Eurail / eurail.com / Eurail B.V.) → ONE Organization ===
def test_eurail_resolution_end_to_end_one_org_three_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Three evidence whose claims name the SAME org three different ways → ONE Organization with all
    # three surface forms in `aliases` + a MENTIONS from each of the three evidence records.
    evidence = [
        {
            "id": "ev1",
            "claim": "Eurail expanded the Global Pass",
            "confidence": 0.8,
            "label": "CLAIM",
            "dimensions": ["ops"],
            "source": {"url": "https://www.eurail.com/a", "name": "A", "publication_date": "2024"},
        },
        {
            "id": "ev2",
            "claim": "eurail.com published new fares",
            "confidence": 0.7,
            "label": "CLAIM",
            "dimensions": ["ops"],
            "source": {"url": "https://eurail.com/b", "name": "B", "publication_date": "2024"},
        },
        {
            "id": "ev3",
            "claim": "Eurail B.V. filed its annual report",
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
    get_recipe_engine().validate(recipe)  # the Slice-4 enriched template still validates

    # The fake extractor names the org by a DIFFERENT surface form per claim — all canonicalize to
    # `eurail`, so resolve-on-write collapses them to ONE node before any semantic step.
    extractor = _PerRecordLLM(
        {
            "Eurail expanded": [
                {"id": "0", "label": "Organization", "properties": {"name": "Eurail"}}
            ],
            "eurail.com published": [
                {"id": "0", "label": "Organization", "properties": {"name": "eurail.com"}}
            ],
            "Eurail B.V. filed": [
                {"id": "0", "label": "Organization", "properties": {"name": "Eurail B.V."}}
            ],
        }
    )
    writer = _StatefulWriter()
    _result, stats = _project_and_extract(
        recipe,
        rep,
        writer,
        monkeypatch,
        extractor=EntityExtractor(llm=extractor),
        embedder=_FakeEmbedder({}),
    )

    # The deterministic structured projection still produced its graph.
    assert writer.labels().count("Evidence") == 3
    # ONE Organization keyed by the canonical `eurail`, with all three surface forms as aliases.
    assert writer.labels().count("Organization") == 1
    eurail_id = _deterministic_id("g-test", "Organization", "eurail")
    assert sorted(writer.nodes[eurail_id]["aliases"]) == ["Eurail", "Eurail B.V.", "eurail.com"]
    assert writer.nodes[eurail_id]["props"]["name"] == "eurail"
    # A MENTIONS from each of the three evidence records to the single canonical node.
    evidence_ids = {_deterministic_id("g-test", "Evidence", f"ev{n}") for n in (1, 2, 3)}
    mentions = writer.rels("MENTIONS")
    assert len(mentions) == 3
    assert {f for f, _ in mentions} == evidence_ids
    assert all(t == eurail_id for _, t in mentions)
    assert stats.entities_extracted == 1  # one canonical node, not three occurrences
