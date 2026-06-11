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
