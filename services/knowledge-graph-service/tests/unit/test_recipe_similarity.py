"""Recipe enrichment Slice 3 (oraclous-backend #269): content-similarity edges.

The `similarities[]` rule runs AFTER the deterministic projection: it embeds a designated `from`
field per record, runs a cosine kNN over the embeddings, and MERGEs a `SIMILAR_TO {score}` edge
between records whose text is close — so records that say similar things connect even when they
share no identity/entity.

No real embedding API: a FAKE embedder hands back hand-chosen vectors keyed by text, so the cosine
kNN, the top_k/min_score filters, the self-exclusion and the canonical-direction dedup are all
exercised deterministically. The deterministic projection runs through the same stateful in-memory
fake writer the Slice-1/2 tests use, so MERGE-by-id + per-edge `score` properties are observable.
"""

from __future__ import annotations

import json

import pytest
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.services import recipes
from oraclous_knowledge_graph_service.services.recipes import similarity_pass
from oraclous_knowledge_graph_service.services.recipes.engine import (
    RecipeValidationError,
    _deterministic_id,
    get_recipe_engine,
)
from oraclous_knowledge_graph_service.services.recipes.similarity_pass import run_similarity_pass
from oraclous_knowledge_graph_service.services.recipes.templates import build_evidence_recipe
from oraclous_knowledge_graph_service.services.structured.primitives import JsonPrimitive

pytestmark = pytest.mark.unit

assert recipes  # keep the namespace import for the monkeypatch target


# --- a stateful fake writer (models MERGE-by-id + per-edge properties) ---------------------------
class _StatefulWriter:
    graph_id = "g-test"

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        # (rel_type, from, to, score) — score is a SIMILAR_TO edge's `properties.score`, else None.
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
    ) -> None:
        node = self.nodes.setdefault(
            entity_id, {"label": label, "props": {}, "identity_key": identity_key}
        )
        node["label"] = label
        node["identity_key"] = identity_key
        node["props"].update(properties)

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


# --- a fake embedder returning hand-chosen vectors keyed by text ----------------------------------
class _FakeEmbedder:
    dim = 3

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vectors[t]) for t in texts]


class _BoomEmbedder:
    dim = 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("simulated embedder failure")


def _patch_embedder(monkeypatch: pytest.MonkeyPatch, embedder) -> None:
    """Make the pass's `make_embedder(...)` hand back our fake."""
    monkeypatch.setattr(similarity_pass, "make_embedder", lambda *a, **k: embedder)


def _json_rep(records: list[dict], name: str):
    return JsonPrimitive().decompose(json.dumps(records), ExtractionMode.FULL, name=name)


_META = {"recipe_id": "rcp_x", "recipe_version": 1, "ingestion_time": "2024-01-01T00:00:00+00:00"}


def _recipe(
    records_shape: str,
    *,
    edge_type: str = "SIMILAR_TO",
    top_k: int = 5,
    min_score: float = 0.5,
) -> dict:
    """A minimal recipe: an `Item` node per record + a similarity over `field:text`."""
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_similarity-test",
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
        "similarities": [
            {
                "id": "sim",
                "from": "field:text",
                "node_rule": "item",
                "edge_type": edge_type,
                "top_k": top_k,
                "min_score": min_score,
            }
        ],
    }


def _project_and_similarity(recipe, rep, writer, monkeypatch, embedder):
    """Run the deterministic projection then the similarity pass over the same writer."""
    engine = get_recipe_engine()
    result = engine.execute(recipe, rep, writer)
    _patch_embedder(monkeypatch, embedder)
    stats = run_similarity_pass(
        recipe=recipe,
        representation=rep,
        writer=writer,
        node_index_by_rule=result.node_index_by_rule,
        settings=Settings(embedder="hashing"),
        engine=engine,
        meta=_META,
        source_id=result.source_id,
    )
    return result, stats


def _item_id(rid: str) -> str:
    return _deterministic_id("g-test", "Item", rid)


# --- 1. cosine kNN correctness: right neighbours, top_k caps, min_score filters, self excluded ----
def test_knn_links_similar_excludes_dissimilar_and_self(monkeypatch: pytest.MonkeyPatch) -> None:
    # a,b near-identical (cosine ~1); c orthogonal to both (cosine 0). min_score 0.5 ⇒ only a–b.
    records = [
        {"id": "a", "text": "alpha"},
        {"id": "b", "text": "beta"},
        {"id": "c", "text": "gamma"},
    ]
    rep = _json_rep(records, "items.json")
    embedder = _FakeEmbedder(
        {"alpha": [1.0, 0.0, 0.0], "beta": [0.99, 0.01, 0.0], "gamma": [0.0, 0.0, 1.0]}
    )
    writer = _StatefulWriter()
    _result, stats = _project_and_similarity(
        _recipe(rep.shape_signature), rep, writer, monkeypatch, embedder
    )
    sim = writer.rels("SIMILAR_TO")
    a, b, c = _item_id("a"), _item_id("b"), _item_id("c")
    # Exactly one edge, between a and b (canonical min->max). c (orthogonal) and self are excluded.
    assert len(sim) == 1
    pair = sim[0]
    assert set(pair) == {a, b}
    assert c not in (pair[0], pair[1])
    assert stats.similarity_edges == 1


def test_top_k_caps_neighbour_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # Four records ALL mutually similar (every pairwise cosine >= min_score=0.5: in-cluster ~0.999,
    # cross-cluster ~0.76-0.79). Without a cap that is 6 unordered pairs; top_k=1 caps EACH record
    # to its single best neighbour, which is its tight-cluster partner — so after canonical dedup
    # exactly two edges survive ({a,b}, {c,d}). The four cross-cluster pairs are dropped by the cap.
    records = [
        {"id": "a", "text": "a"},
        {"id": "b", "text": "b"},
        {"id": "c", "text": "c"},
        {"id": "d", "text": "d"},
    ]
    rep = _json_rep(records, "items.json")
    embedder = _FakeEmbedder(
        {
            "a": [1.0, 0.7, 0.6, 0.0],  # a,b cluster tightly (cosine ~0.999) ...
            "b": [1.0, 0.7, 0.6, 0.05],
            "c": [0.7, 1.0, 0.0, 0.6],  # c,d cluster tightly ...
            "d": [0.7, 1.0, 0.05, 0.6],
        }
    )
    writer = _StatefulWriter()
    _result, stats = _project_and_similarity(
        _recipe(rep.shape_signature, top_k=1, min_score=0.5), rep, writer, monkeypatch, embedder
    )
    sim = [set(p) for p in writer.rels("SIMILAR_TO")]
    a, b, c, d = _item_id("a"), _item_id("b"), _item_id("c"), _item_id("d")
    # top_k=1 → each record keeps only its best partner; cross-cluster pairs are dropped by the cap.
    assert len(sim) == 2
    assert {a, b} in sim
    assert {c, d} in sim
    assert stats.similarity_edges == 2


def test_min_score_filters_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    # a–b cosine ~0.6: above a 0.5 threshold (linked), below a 0.8 threshold (filtered).
    records = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]
    rep = _json_rep(records, "items.json")
    embedder = _FakeEmbedder({"alpha": [1.0, 0.0, 0.0], "beta": [0.6, 0.8, 0.0]})  # cosine 0.6

    writer_low = _StatefulWriter()
    _r, stats_low = _project_and_similarity(
        _recipe(rep.shape_signature, min_score=0.5), rep, writer_low, monkeypatch, embedder
    )
    assert stats_low.similarity_edges == 1  # 0.6 >= 0.5 → linked

    writer_high = _StatefulWriter()
    _r2, stats_high = _project_and_similarity(
        _recipe(rep.shape_signature, min_score=0.8), rep, writer_high, monkeypatch, embedder
    )
    assert stats_high.similarity_edges == 0  # 0.6 < 0.8 → filtered
    assert writer_high.rels("SIMILAR_TO") == []


# --- 2. canonical-direction dedup: a symmetric pair → exactly ONE edge, with the score property ---
def test_symmetric_pair_yields_one_edge_with_score(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]
    rep = _json_rep(records, "items.json")
    # Identical vectors → cosine exactly 1.0 from BOTH sides; canonical dedup must emit ONE edge.
    embedder = _FakeEmbedder({"alpha": [1.0, 0.0, 0.0], "beta": [1.0, 0.0, 0.0]})
    writer = _StatefulWriter()
    _result, stats = _project_and_similarity(
        _recipe(rep.shape_signature), rep, writer, monkeypatch, embedder
    )
    scored = writer.scored_rels("SIMILAR_TO")
    a, b = _item_id("a"), _item_id("b")
    assert len(scored) == 1  # not two (one per direction)
    f, t, score = scored[0]
    # Canonical direction min(id) -> max(id).
    assert (f, t) == ((a, b) if a < b else (b, a))
    assert score == pytest.approx(1.0)  # the cosine carried as a rounded float property
    assert stats.similarity_edges == 1


# --- 3. fail-soft: an embedder whose embed() raises → pass skipped + warned, prior work intact ----
def test_embedder_failure_skips_pass_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]
    rep = _json_rep(records, "items.json")
    writer = _StatefulWriter()
    _result, stats = _project_and_similarity(
        _recipe(rep.shape_signature), rep, writer, monkeypatch, _BoomEmbedder()
    )
    # The deterministic projection is intact: both Item nodes exist.
    assert writer.labels().count("Item") == 2
    # No SIMILAR_TO edges; a warning explains the skip.
    assert writer.rels("SIMILAR_TO") == []
    assert stats.similarity_edges == 0
    assert any("embedder failed" in w for w in stats.warnings)


# --- 4. fewer than two texts → no edges -----------------------------------------------------------
def test_single_record_yields_no_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = _json_rep([{"id": "a", "text": "alpha"}], "items.json")
    embedder = _FakeEmbedder({"alpha": [1.0, 0.0, 0.0]})
    writer = _StatefulWriter()
    _result, stats = _project_and_similarity(
        _recipe(rep.shape_signature), rep, writer, monkeypatch, embedder
    )
    assert stats.similarity_edges == 0
    assert writer.rels("SIMILAR_TO") == []


def test_empty_text_records_skipped_leaving_under_two(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two records but one has blank text → only one embeddable record → no pair → no edges.
    records = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "   "}]
    rep = _json_rep(records, "items.json")
    embedder = _FakeEmbedder({"alpha": [1.0, 0.0, 0.0]})
    writer = _StatefulWriter()
    _result, stats = _project_and_similarity(
        _recipe(rep.shape_signature), rep, writer, monkeypatch, embedder
    )
    assert stats.similarity_edges == 0


# --- 5. EURail-shape end-to-end over the enriched evidence template -------------------------------
def test_eurail_enriched_template_similarity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three evidence; ev1 and ev2 claims are near-identical vectors, ev3 is orthogonal. The
    # SIMILAR_TO edge links ev1–ev2 only (none to the orthogonal ev3).
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
            "claim": "Eurail grew the Global Pass",
            "confidence": 0.7,
            "label": "CLAIM",
            "dimensions": ["ops"],
            "source": {"url": "https://eurail.com/b", "name": "B", "publication_date": "2024"},
        },
        {
            "id": "ev3",
            "claim": "Punctuality unrelated topic",
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
    get_recipe_engine().validate(recipe)  # the Slice-3 enriched template still validates

    embedder = _FakeEmbedder(
        {
            "Eurail expanded the Global Pass": [1.0, 0.0, 0.0],
            "Eurail grew the Global Pass": [0.98, 0.02, 0.0],  # near-identical to ev1
            "Punctuality unrelated topic": [0.0, 0.0, 1.0],  # orthogonal to both
        }
    )
    writer = _StatefulWriter()
    _result, stats = _project_and_similarity(recipe, rep, writer, monkeypatch, embedder)

    ev1 = _deterministic_id("g-test", "Evidence", "ev1")
    ev2 = _deterministic_id("g-test", "Evidence", "ev2")
    ev3 = _deterministic_id("g-test", "Evidence", "ev3")
    sim = writer.rels("SIMILAR_TO")
    assert len(sim) == 1
    assert set(sim[0]) == {ev1, ev2}
    assert ev3 not in {x for pair in sim for x in pair}  # orthogonal record links to nothing
    assert stats.similarity_edges == 1


# --- 6. validation: node_rule must reference an existing node rule; edge_type must be safe --------
def test_validation_unknown_node_rule_raises() -> None:
    rep = _json_rep([{"id": "a", "text": "x"}], "items.json")
    recipe = _recipe(rep.shape_signature)
    recipe["similarities"][0]["node_rule"] = "does_not_exist"
    with pytest.raises(RecipeValidationError, match="is not a node rule"):
        get_recipe_engine().validate(recipe)


def test_validation_unsafe_edge_type_raises() -> None:
    rep = _json_rep([{"id": "a", "text": "x"}], "items.json")
    recipe = _recipe(rep.shape_signature)
    # An unsafe edge_type is rejected by the JSON schema (safe_identifier) before the cross-check.
    recipe["similarities"][0]["edge_type"] = "__Evil__"
    with pytest.raises(RecipeValidationError):
        get_recipe_engine().validate(recipe)


def test_similarity_defaults_validate_without_optional_fields() -> None:
    # edge_type/top_k/min_score are optional (run-time defaults SIMILAR_TO/5/0.5).
    rep = _json_rep([{"id": "a", "text": "x"}], "items.json")
    recipe = _recipe(rep.shape_signature)
    recipe["similarities"][0] = {"id": "sim", "from": "field:text", "node_rule": "item"}
    get_recipe_engine().validate(recipe)  # must not raise
