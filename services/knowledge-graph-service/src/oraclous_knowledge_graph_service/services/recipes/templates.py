"""Reusable recipe templates (ORAA-4 §21 services layer — pure data, no I/O).

A *template* is an author-ready format-0.2 recipe built for a recurring concern, so a caller can
ingest a known JSON shape into a typed subgraph without hand-writing the recipe. These are plain
`dict`s (a recipe is data, ADR-022); they validate against the same schema the engine enforces and
exercise the two Slice-A capabilities:

  * G2 nested-object reads — `ClaimSource` identity/props are read from a nested `source{}` object
    (`field:source.url`, `field:source.name`, ...), which the engine's dotted-path `_read_field`
    walks.
  * G1 foreign_key edges — `Conflict-[:CONTRADICTS]->Evidence` resolves by the conflict record's
    list-valued `evidence_ids` foreign key: each id is hashed to the Evidence node's deterministic
    id, so a conflict links to its evidence even across separate ingest runs/files.

The pair is deliberately split into two recipes (evidence, conflicts) so each source file is
ingested on its own; the deterministic id is what stitches them together, not a shared job.
"""

from __future__ import annotations

from typing import Any

EVIDENCE_CONCERN = "evidence-and-conflicts"

# Source shape signatures the engine matches on (recipe-lookup key only). The JSON primitive derives
# the live signature from the record fields + inferred types — these are the canonical shapes the
# templates were authored against (fields sorted, `name:type` per JsonPrimitive.decompose).
EVIDENCE_SHAPE_SIGNATURE = (
    "json(claim:string,confidence:number,dimensions:array,id:string,label:string,source:object)"
)
CONFLICTS_SHAPE_SIGNATURE = (
    "json(evidence_ids:array,id:string,resolution:string,synthesis_note:string,topic:string)"
)


def build_evidence_recipe(shape_signature: str = EVIDENCE_SHAPE_SIGNATURE) -> dict[str, Any]:
    """Evidence records → `Evidence` nodes + a `ClaimSource` node read from the nested `source{}`
    object (G2), linked `Evidence-[:FROM_SOURCE]->ClaimSource` (same-record identity). NB: the label
    is `ClaimSource`, NOT `Source` — `Source` is a reserved platform container label.

    Enriched (Slice 1, #269) to ALSO project two derived/finer entities:

      * a `Publisher` node whose identity is `field:source.url` with `transform: host` — the URL's
        hostname — so different article URLs from the SAME publisher (e.g. `www.eurail.com/a` and
        `eurail.com/b`) collapse to one `eurail.com` Publisher; `ClaimSource-[:PUBLISHED_BY]->
        Publisher` (same-record identity).
      * `Tag` nodes fanned out of the list-valued `field:dimensions` (one node per dimension,
        MERGE-shared across records), each linked `Evidence-[:HAS_DIMENSION]->Tag`.

    Enriched again (Slice 2, #269) with a HYBRID free-text-on-a-field extraction: the LLM entity
    extractor runs over each record's prose `field:claim` under an inline ontology (Person /
    Organization / Product / Place), so a claim's named entities are mined into the graph and a
    `Evidence-[:MENTIONS]->`entity edge is MERGEd from the record's Evidence node to each. Entities
    MERGE-dedup across records (the same org named in two claims → one node + two MENTIONS), so the
    evidence records interconnect by the entities they share. Fail-soft: with no LLM extractor
    (`KGS_EXTRACTOR=null`, the CI default) the extraction is skipped and only the deterministic
    structured projection above runs.

    Enriched once more (Slice 3, #269) with a CONTENT-SIMILARITY pass: each record's prose
    `field:claim` is embedded and a cosine kNN MERGEs an `Evidence-[:SIMILAR_TO {score}]->Evidence`
    edge between records whose claims are close (top_k=5, min_score=0.5; one edge per unordered
    pair). So evidence that says similar things connects even when it shares no source/entity.
    Fail-soft: an embedder failure skips the similarity pass, leaving the projection intact.

    Enriched finally (Slice 4, #269) with ENTITY RESOLUTION (resolve-on-write): the extraction
    rule's `resolution` block canonicalizes the mined entities during ingestion, so `Eurail` /
    `eurail.com` / `Eurail B.V.` collapse to ONE Organization with an `aliases` audit trail (rather
    than three separate nodes), and a conservative semantic pass folds near-duplicate canonical
    names + flags an ambiguous band as `SAME_AS_CANDIDATE` edges. Fail-soft: an embedder failure
    skips the semantic merge, leaving the deterministic canonical keying in place.
    """
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_eurail-evidence",
        "version": 1,
        "status": "promoted",
        "concern": EVIDENCE_CONCERN,
        "applies_to": {"source_type": "json", "shape_signature": shape_signature},
        "defaults": {"provenance": "EXTRACTED"},
        "authoring": {
            "authored_by": "data-specialist",
            "sample_basis": "EURail evidence ledger records (nested source{} object).",
        },
        "mappings": [
            {
                "id": "evidence",
                "project_to": "node",
                "label": "Evidence",
                "match": {"unit_kind": "record"},
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:id"],
                    "normalize": ["trim"],
                },
                "properties": [
                    {"name": "claim", "value_from": "field:claim"},
                    {"name": "confidence", "value_from": "field:confidence"},
                    {"name": "label", "value_from": "field:label"},
                    {"name": "dimensions", "value_from": "field:dimensions"},
                ],
            },
            {
                "id": "claim_source",
                "project_to": "node",
                "label": "ClaimSource",
                "match": {"unit_kind": "record"},
                # G2: nested-object identity + props read via dotted paths into source{}.
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:source.url"],
                    "normalize": ["trim"],
                },
                "properties": [
                    {"name": "name", "value_from": "field:source.name"},
                    {"name": "publication_date", "value_from": "field:source.publication_date"},
                ],
            },
            {
                "id": "from_source",
                "project_to": "edge",
                "type": "FROM_SOURCE",
                "match": {"unit_kind": "record"},
                "from": {"node_rule": "evidence"},
                "to": {"node_rule": "claim_source", "resolve_by": "identity"},
            },
            # Slice 1 — a Publisher derived from the source URL's HOST (transform: host), so claims
            # from the same domain dedup onto one Publisher node regardless of the article path.
            {
                "id": "publisher",
                "project_to": "node",
                "label": "Publisher",
                "match": {"unit_kind": "record"},
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:source.url"],
                    "normalize": ["trim"],
                    "transform": "host",
                },
            },
            {
                "id": "published_by",
                "project_to": "edge",
                "type": "PUBLISHED_BY",
                "match": {"unit_kind": "record"},
                "from": {"node_rule": "claim_source"},
                "to": {"node_rule": "publisher", "resolve_by": "identity"},
            },
            # Slice 1 — fan the list-valued `dimensions` field into one Tag node per dimension,
            # MERGE-shared across records, with an Evidence-[:HAS_DIMENSION]->Tag edge per element.
            {
                "id": "tag",
                "project_to": "node",
                "label": "Tag",
                "match": {"unit_kind": "record"},
                "from_each": "field:dimensions",
                # With from_each the element value IS the identity; identity.from is ignored (it
                # echoes the fan-out field for readability), only normalize/transform apply.
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:dimensions"],
                    "normalize": ["trim", "casefold"],
                },
                "edge_to_each": {"type": "HAS_DIMENSION", "from_node_rule": "evidence"},
            },
        ],
        # Slice 2 — hybrid free-text-on-a-field: mine named entities from each record's prose
        # `field:claim` and MERGE Evidence-[:MENTIONS]->entity. Entities dedup across records, so
        # two claims naming the same org share one node (interconnecting their Evidence records).
        #
        # Slice 4 — entity resolution (resolve-on-write): the `resolution` block canonicalizes each
        # extracted entity DURING ingestion, so an organisation's surface variants (`Eurail`,
        # `eurail.com`, `Eurail B.V.`, `Eurail Group`) collapse to ONE Organization node keyed by
        # the canonical key (`eurail`), with the original forms kept in its `aliases` set + a
        # `canonical_name` display form. A conservative semantic pass then folds near-duplicate
        # canonical names (cosine >= 0.92) and flags the ambiguous band (0.85–0.92) with
        # SAME_AS_CANDIDATE edges. Relation-type canonicalization needs no separate step: the closed
        # `relationship_types` ontology below yields canonical relation roots by construction.
        "extractions": [
            {
                "id": "claim_entities",
                "from": "field:claim",
                "ontology": {
                    "mode": "strict",
                    "domain": "rail travel and tourism",
                    "entity_types": [
                        {"name": "Person", "description": "A named individual."},
                        {"name": "Organization", "description": "A company, agency, or operator."},
                        {"name": "Product", "description": "A named product, pass, or service."},
                        {"name": "Place", "description": "A named city, country, or location."},
                    ],
                    "relationship_types": [
                        {"name": "OPERATES", "source": "Organization", "target": "Product"},
                        {"name": "LOCATED_IN", "source": "Organization", "target": "Place"},
                    ],
                },
                "resolution": {
                    "canonical": True,
                    "merge_threshold": 0.92,
                    "candidate_threshold": 0.85,
                },
                "link": {"type": "MENTIONS", "from_node_rule": "evidence"},
            }
        ],
        # Slice 3 — content similarity: embed each record's prose `field:claim`, run a cosine kNN,
        # and MERGE an Evidence-[:SIMILAR_TO {score}]->Evidence edge between records whose claims
        # are close (one edge per unordered pair). So evidence that says similar things connects
        # even when it shares no source/publisher/entity. Fail-soft: an embedder failure skips it.
        "similarities": [
            {
                "id": "claim_similarity",
                "from": "field:claim",
                "node_rule": "evidence",
                "edge_type": "SIMILAR_TO",
                "top_k": 5,
                "min_score": 0.5,
            }
        ],
    }


def build_conflicts_recipe(shape_signature: str = CONFLICTS_SHAPE_SIGNATURE) -> dict[str, Any]:
    """Conflict records → `Conflict` nodes + a list-valued foreign-key edge
    `Conflict-[:CONTRADICTS]->Evidence` (G1): each id in the record's `evidence_ids[]` is the
    Evidence node's identity, so the edge links to evidence ingested by the evidence recipe — even
    in a separate run (deterministic id; a missing target is MERGEd as a stub the evidence ingest
    later enriches).
    """
    return {
        "recipe_format_version": "0.2",
        "id": "rcp_eurail-conflicts",
        "version": 1,
        "status": "promoted",
        "concern": EVIDENCE_CONCERN,
        "applies_to": {"source_type": "json", "shape_signature": shape_signature},
        "defaults": {"provenance": "EXTRACTED"},
        "authoring": {
            "authored_by": "data-specialist",
            "sample_basis": "EURail conflict-log records (list-valued evidence_ids).",
        },
        "mappings": [
            {
                "id": "conflict",
                "project_to": "node",
                "label": "Conflict",
                "match": {"unit_kind": "record"},
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:id"],
                    "normalize": ["trim"],
                },
                "properties": [
                    {"name": "topic", "value_from": "field:topic"},
                    {"name": "resolution", "value_from": "field:resolution"},
                    {"name": "synthesis_note", "value_from": "field:synthesis_note"},
                ],
            },
            # The Evidence node_rule is declared here too so the conflicts recipe knows the target's
            # label + identity (single-field) for the foreign_key id computation. Its match selects
            # no unit in a conflicts file (no `evidence_record` unit kind exists), so it projects no
            # node here — it is purely the resolver's identity template, hashed with the SAME id
            # function the evidence recipe used, which is why the ids line up cross-file.
            {
                "id": "evidence",
                "project_to": "node",
                "label": "Evidence",
                "match": {"unit_kind": "evidence_record"},
                "identity": {
                    "scheme": "deterministic",
                    "from": ["field:id"],
                    "normalize": ["trim"],
                },
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


def build_evidence_and_conflicts_recipes() -> tuple[dict[str, Any], dict[str, Any]]:
    """The evidence/conflicts template pair (evidence first, then conflicts)."""
    return build_evidence_recipe(), build_conflicts_recipe()


__all__ = [
    "EVIDENCE_CONCERN",
    "EVIDENCE_SHAPE_SIGNATURE",
    "CONFLICTS_SHAPE_SIGNATURE",
    "build_evidence_recipe",
    "build_conflicts_recipe",
    "build_evidence_and_conflicts_recipes",
]
