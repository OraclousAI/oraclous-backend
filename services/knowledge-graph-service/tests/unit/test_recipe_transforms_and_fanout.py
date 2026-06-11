"""Recipe enrichment Slice 1 engine tests (oraclous-backend #269): deterministic primitives.

  1. transforms — the pure `domain/recipes/transforms.py` registry (`host`/`lower`/`strip_www`) and
     `apply_transform`; an unknown transform name fails at recipe validation; a property and an
     identity carrying a `transform` produce the transformed value end-to-end.
  2. fan-out — a node rule with `from_each` fans a LIST field into one node per element (+ one edge
     per element via `edge_to_each`): scalar → 1, empty/missing → 0 + warning, duplicates deduped
     within a record, equal elements MERGE-shared across records.
  3. an EURail-shape end-to-end over the enriched template: Evidence + ClaimSource + Publisher
     (deduped by URL host) + Tag (deduped) with FROM_SOURCE + PUBLISHED_BY + HAS_DIMENSION counts.

Engine-level only (no Neo4j): the same stateful in-memory fake writer the Slice-A tests use, so
MERGE-by-id (shared nodes across records/runs) is observable.
"""

from __future__ import annotations

import json

import pytest
from oraclous_knowledge_graph_service.domain.recipes.transforms import (
    RecipeTransformError,
    apply_transform,
    is_known_transform,
)
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.services.recipes.engine import (
    RecipeValidationError,
    _deterministic_id,
    get_recipe_engine,
)
from oraclous_knowledge_graph_service.services.recipes.templates import build_evidence_recipe
from oraclous_knowledge_graph_service.services.structured.primitives import JsonPrimitive

pytestmark = pytest.mark.unit


# --- a stateful fake writer (models MERGE-by-id) ------------------------------
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
                "stub": False,
                "identity_key": identity_key,
                "aliases": [],
            },
        )
        node["label"] = label
        node["identity_key"] = identity_key
        node["stub"] = False
        node["props"].update(properties)
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

    def merge_edge_to_stub(
        self, *, rel_type, target_label, edges, source_id, provenance, meta
    ) -> int:
        for e in edges:
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


# --- 1. transforms: the pure registry -----------------------------------------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.eurail.com/en/about-us", "eurail.com"),
        ("https://eurail.com/en/about-us", "eurail.com"),
        ("http://www.example.org", "example.org"),
        (
            "https://news.example.org/2024/a/b",
            "news.example.org",
        ),  # subdomain kept (host, not eTLD+1)
        ("https://example.com:8443/path", "example.com"),  # port stripped
        ("eurail.com/en/about-us", "eurail.com"),  # no scheme
        ("www.eurail.com", "eurail.com"),  # no scheme, bare host
        ("HTTPS://WWW.EURAIL.COM/X", "eurail.com"),  # lowercased
        ("", ""),  # empty
        ("   ", ""),  # whitespace only
        ("not a url", ""),  # no host (bare text, no dot-before-slash authority)
        ("/just/a/path", ""),  # no authority
    ],
)
def test_host_transform(url: str, expected: str) -> None:
    assert apply_transform("host", url) == expected


def test_lower_and_strip_www_transforms() -> None:
    assert apply_transform("lower", "EuRail") == "eurail"
    assert apply_transform("strip_www", "www.eurail.com") == "eurail.com"
    assert apply_transform("strip_www", "WWW.Eurail.com") == "Eurail.com"  # case-insensitive prefix
    assert apply_transform("strip_www", "eurail.com") == "eurail.com"  # no www → unchanged


def test_apply_transform_coerces_non_str_and_none() -> None:
    assert apply_transform("lower", None) == ""
    assert apply_transform("lower", 123) == "123"


def test_unknown_transform_raises_transform_error() -> None:
    assert is_known_transform("host") is True
    assert is_known_transform("nope") is False
    with pytest.raises(RecipeTransformError, match="unknown transform"):
        apply_transform("nope", "x")


# --- 1. transforms: recipe validation + end-to-end ----------------------------
def _evidence_with_transforms(shape_signature: str, *, identity_transform, prop_transform) -> dict:
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_transform-test",
        "version": 1,
        "status": "promoted",
        "concern": "test",
        "applies_to": {"source_type": "json", "shape_signature": shape_signature},
        "defaults": {"provenance": "EXTRACTED"},
        "mappings": [
            {
                "id": "thing",
                "project_to": "node",
                "label": "Thing",
                "match": {"unit_kind": "record"},
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:url"],
                    "normalize": ["trim"],
                    "transform": identity_transform,
                },
                "properties": [
                    {"name": "site", "value_from": "field:url", "transform": prop_transform},
                ],
            }
        ],
    }


def test_unknown_transform_on_identity_is_validation_error() -> None:
    rep = _json_rep([{"url": "https://www.eurail.com/a"}], "things.json")
    recipe = _evidence_with_transforms(
        rep.shape_signature, identity_transform="bogus", prop_transform="host"
    )
    with pytest.raises(RecipeValidationError, match="unknown transform"):
        get_recipe_engine().validate(recipe)


def test_unknown_transform_on_property_is_validation_error() -> None:
    rep = _json_rep([{"url": "https://www.eurail.com/a"}], "things.json")
    recipe = _evidence_with_transforms(
        rep.shape_signature, identity_transform="host", prop_transform="bogus"
    )
    with pytest.raises(RecipeValidationError, match="unknown transform"):
        get_recipe_engine().validate(recipe)


def test_identity_and_property_transform_applied_end_to_end() -> None:
    # Two records, different paths under the same host → ONE node (identity transformed to host).
    records = [
        {"url": "https://www.eurail.com/a"},
        {"url": "https://eurail.com/b"},
    ]
    rep = _json_rep(records, "things.json")
    recipe = _evidence_with_transforms(
        rep.shape_signature, identity_transform="host", prop_transform="host"
    )
    writer = _StatefulWriter()
    get_recipe_engine().execute(recipe, rep, writer)
    things = [n for n in writer.nodes.values() if n["label"] == "Thing"]
    assert len(things) == 1  # both records collapse onto the transformed identity
    assert things[0]["identity_key"] == "eurail.com"
    assert things[0]["props"]["site"] == "eurail.com"  # property transform applied too
    # The id is the deterministic id of the TRANSFORMED identity, not the raw URL.
    assert _deterministic_id("g-test", "Thing", "eurail.com") in writer.nodes


def test_property_rule_transform_applied() -> None:
    """A standalone `project_to: property` rule may also carry a transform."""
    records = [{"url": "https://www.eurail.com/a"}]
    rep = _json_rep(records, "things.json")
    recipe = {
        "recipe_format_version": "0.2",
        "id": "rcp_prop-transform",
        "version": 1,
        "status": "promoted",
        "concern": "test",
        "applies_to": {"source_type": "json", "shape_signature": rep.shape_signature},
        "defaults": {"provenance": "EXTRACTED"},
        "mappings": [
            {
                "id": "thing",
                "project_to": "node",
                "label": "Thing",
                "match": {"unit_kind": "record"},
                "identity": {"scheme": "deterministic", "from": ["field:url"]},
            },
            {
                "id": "site_prop",
                "project_to": "property",
                "match": {"unit_kind": "record"},
                "on": "thing",
                "name": "site",
                "value_from": "field:url",
                "transform": "host",
            },
        ],
    }
    writer = _StatefulWriter()
    get_recipe_engine().execute(recipe, rep, writer)
    thing = next(n for n in writer.nodes.values() if n["label"] == "Thing")
    assert thing["props"]["site"] == "eurail.com"


# --- 2. fan-out ---------------------------------------------------------------
def _fanout_recipe(shape_signature: str) -> dict:
    """A primary `Item` node + a `Tag` fan-out of `field:tags` with an Item-[:HAS_TAG]->Tag edge."""
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_fanout-test",
        "version": 1,
        "status": "promoted",
        "concern": "test",
        "applies_to": {"source_type": "json", "shape_signature": shape_signature},
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
            },
            {
                "id": "tag",
                "project_to": "node",
                "label": "Tag",
                "match": {"unit_kind": "record"},
                "from_each": "field:tags",
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:tags"],
                    "normalize": ["trim", "casefold"],
                },
                "edge_to_each": {"type": "HAS_TAG", "from_node_rule": "item"},
            },
        ],
    }


def test_fanout_list_makes_one_node_and_edge_per_element() -> None:
    records = [{"id": "i1", "tags": ["alpha", "beta", "gamma"]}]
    rep = _json_rep(records, "items.json")
    writer = _StatefulWriter()
    get_recipe_engine().execute(_fanout_recipe(rep.shape_signature), rep, writer)
    tags = [n for n in writer.nodes.values() if n["label"] == "Tag"]
    assert len(tags) == 3
    assert {n["identity_key"] for n in tags} == {"alpha", "beta", "gamma"}
    has_tag = writer.rels("HAS_TAG")
    assert len(has_tag) == 3
    item_id = next(i for i, n in writer.nodes.items() if n["label"] == "Item")
    assert all(src == item_id for src, _ in has_tag)
    assert {t for _, t in has_tag} == {i for i, n in writer.nodes.items() if n["label"] == "Tag"}


def test_fanout_scalar_value_makes_one_node() -> None:
    records = [{"id": "i1", "tags": "solo"}]  # scalar, not a list
    rep = _json_rep(records, "items.json")
    writer = _StatefulWriter()
    get_recipe_engine().execute(_fanout_recipe(rep.shape_signature), rep, writer)
    tags = [n for n in writer.nodes.values() if n["label"] == "Tag"]
    assert len(tags) == 1
    assert tags[0]["identity_key"] == "solo"
    assert len(writer.rels("HAS_TAG")) == 1


def test_fanout_empty_or_missing_field_projects_zero_and_warns() -> None:
    for records in ([{"id": "i1", "tags": []}], [{"id": "i1"}]):
        rep = _json_rep(records, "items.json")
        writer = _StatefulWriter()
        result = get_recipe_engine().execute(_fanout_recipe(rep.shape_signature), rep, writer)
        assert [n for n in writer.nodes.values() if n["label"] == "Tag"] == []
        assert writer.rels("HAS_TAG") == []
        assert any("empty from_each" in w for w in result.warnings)


def test_fanout_duplicate_elements_deduped_within_record() -> None:
    # "alpha" repeated (incl. a case/whitespace variant the normalize chain folds together).
    records = [{"id": "i1", "tags": ["alpha", "Alpha", " alpha ", "beta"]}]
    rep = _json_rep(records, "items.json")
    writer = _StatefulWriter()
    get_recipe_engine().execute(_fanout_recipe(rep.shape_signature), rep, writer)
    tags = [n for n in writer.nodes.values() if n["label"] == "Tag"]
    assert len(tags) == 2  # alpha (folded) + beta
    assert {n["identity_key"] for n in tags} == {"alpha", "beta"}
    # One edge per DISTINCT element, not per raw element.
    assert len(writer.rels("HAS_TAG")) == 2


def test_fanout_equal_elements_merge_shared_across_records() -> None:
    records = [
        {"id": "i1", "tags": ["shared", "only1"]},
        {"id": "i2", "tags": ["shared", "only2"]},
    ]
    rep = _json_rep(records, "items.json")
    writer = _StatefulWriter()
    get_recipe_engine().execute(_fanout_recipe(rep.shape_signature), rep, writer)
    tags = [n for n in writer.nodes.values() if n["label"] == "Tag"]
    # shared collapses to one node across the two records → 3 distinct tags, not 4.
    assert len(tags) == 3
    assert {n["identity_key"] for n in tags} == {"shared", "only1", "only2"}
    shared_id = _deterministic_id("g-test", "Tag", "shared")
    # Both items link to the SAME shared Tag node.
    has_tag = writer.rels("HAS_TAG")
    assert sum(1 for _, t in has_tag if t == shared_id) == 2
    assert len(has_tag) == 4  # 2 items × 2 tags each


def test_fanout_validate_edge_to_each_requires_from_each() -> None:
    rep = _json_rep([{"id": "i1", "tags": ["a"]}], "items.json")
    recipe = _fanout_recipe(rep.shape_signature)
    del recipe["mappings"][1]["from_each"]  # edge_to_each without from_each
    with pytest.raises(RecipeValidationError, match="edge_to_each requires from_each"):
        get_recipe_engine().validate(recipe)


# --- 3. enriched EURail template end-to-end -----------------------------------
def test_enriched_eurail_recipe_publisher_and_tags() -> None:
    evidence = [
        {
            "id": "ev1",
            "claim": "Punctuality improved",
            "confidence": 0.8,
            "label": "CLAIM",
            "dimensions": ["ops", "punctuality"],
            "source": {
                "url": "https://www.eurail.com/en/report-a",
                "name": "EURail Report A",
                "publication_date": "2024-01-01",
            },
        },
        {
            "id": "ev2",
            "claim": "Punctuality stable",
            "confidence": 0.7,
            "label": "CLAIM",
            "dimensions": ["ops", "OPS"],  # dup within record (case-folds to one)
            # SAME host as ev1 (no www) → both collapse onto one eurail.com Publisher.
            "source": {
                "url": "https://eurail.com/en/report-b",
                "name": "EURail Report B",
                "publication_date": "2024-02-01",
            },
        },
        {
            "id": "ev3",
            "claim": "News take",
            "confidence": 0.5,
            "label": "CLAIM",
            "dimensions": ["sentiment"],
            "source": {
                "url": "https://news.example.org/x",  # different host → its own Publisher
                "name": "News X",
                "publication_date": "2024-03-01",
            },
        },
    ]
    rep = _json_rep(evidence, "evidence.json")
    recipe = build_evidence_recipe(rep.shape_signature)
    get_recipe_engine().validate(recipe)  # the enriched template still validates
    writer = _StatefulWriter()
    get_recipe_engine().execute(recipe, rep, writer)

    labels = writer.labels()
    assert labels.count("Evidence") == 3
    assert labels.count("ClaimSource") == 3  # three distinct source.url values
    # Publisher dedups by HOST: eurail.com (ev1+ev2) + news.example.org (ev3) = 2.
    assert labels.count("Publisher") == 2
    publisher_keys = {n["identity_key"] for n in writer.nodes.values() if n["label"] == "Publisher"}
    assert publisher_keys == {"eurail.com", "news.example.org"}
    # Tag dedups across records: ops, punctuality, sentiment = 3 (ev2's OPS folds onto ops).
    assert labels.count("Tag") == 3
    tag_keys = {n["identity_key"] for n in writer.nodes.values() if n["label"] == "Tag"}
    assert tag_keys == {"ops", "punctuality", "sentiment"}

    # Edges: FROM_SOURCE one per evidence; PUBLISHED_BY one per claim_source; HAS_DIMENSION one per
    # DISTINCT dimension per record (ev1:2, ev2:1 folded, ev3:1 = 4).
    assert len(writer.rels("FROM_SOURCE")) == 3
    assert len(writer.rels("PUBLISHED_BY")) == 3
    assert len(writer.rels("HAS_DIMENSION")) == 4

    # PUBLISHED_BY runs ClaimSource -> Publisher; both ev1/ev2 sources point at the one eurail.com.
    eurail_pub_id = _deterministic_id("g-test", "Publisher", "eurail.com")
    published_by = writer.rels("PUBLISHED_BY")
    assert sum(1 for _, t in published_by if t == eurail_pub_id) == 2

    # HAS_DIMENSION runs Evidence -> Tag; every target is a real Tag node.
    tag_ids = {i for i, n in writer.nodes.items() if n["label"] == "Tag"}
    assert {t for _, t in writer.rels("HAS_DIMENSION")} == tag_ids
