"""Auto-trigger similarity-rule synthesis (ORAA-4 §21 services layer — planning, no driver).

Issue #310. The legacy `similarity_service` ran similarity on EVERY ingest (gated by
`SIMILARITY_AUTO_TRIGGER_ON_INGEST`), not only when an author declared it. The shipped #269
recipe path runs the similarity pass only when a recipe carries an explicit `similarities[]` block.
This module restores the auto-trigger by SYNTHESISING default `similarities[]` rules from a recipe's
node mappings when the operator opts in (`KGS_SIMILARITY_AUTO_TRIGGER=true`) and the recipe declared
none — so records connect by content with no authoring, while an explicit block always wins.

Design (lift-and-reshape, not a parallel pipeline):
  - For each `project_to: node` rule, pick the node's BEST text field to embed: among the rule's
    property `value_from` fields, the one whose sampled record values are, on average, the longest
    free text (longer text embeds more meaningfully than a short code/id). Ties break on recipe
    order (the first such field). A node rule with no usable text field is skipped (no rule).
  - Emit a normal `similarities[]` rule per such node rule (`edge_type: SIMILAR_TO`, the operator's
    `min_score` floor). These rules then flow through the SAME validated `run_similarity_pass` — no
    new code path, no new edge writer.

The synthesis NEVER overrides an authored `similarities[]` block; the caller only invokes it when
the active recipe has none. The synthesised rules are validated by the engine before use, so a bad
synthesis fails closed rather than writing a bad graph.
"""

from __future__ import annotations

from typing import Any

from oraclous_knowledge_graph_service.domain.structural import StructuralRepresentation

# Don't scan every record to choose a field — a small head sample is enough to rank text length.
_SAMPLE_SIZE = 25
# A field whose sampled values are shorter than this (avg chars) is treated as a code/id/number,
# not free text worth embedding for similarity.
_MIN_AVG_TEXT_LEN = 8


def synthesize_similarity_rules(
    *,
    recipe: dict[str, Any],
    representation: StructuralRepresentation,
    engine: Any,
    min_score: float,
) -> list[dict[str, Any]]:
    """Build default `similarities[]` rules from the recipe's node mappings (#310 auto-trigger).

    Returns one rule per node rule that has a usable text field; an empty list when none do (so the
    caller simply runs no similarity pass). `min_score` is the operator's auto floor.
    """
    record_units = [u for u in representation.units if u.kind.value == "record"]
    if len(record_units) < 2:  # a similarity needs at least one PAIR.
        return []
    sample = record_units[:_SAMPLE_SIZE]

    rules: list[dict[str, Any]] = []
    for mapping in recipe.get("mappings", []):
        if mapping.get("project_to") != "node":
            continue
        from_ref = _best_text_field(mapping, sample, engine)
        if from_ref is None:
            continue
        rules.append(
            {
                "id": f"auto_sim_{mapping['id']}",
                "from": from_ref,
                "node_rule": mapping["id"],
                "edge_type": "SIMILAR_TO",
                "min_score": min_score,
            }
        )
    return rules


def _best_text_field(mapping: dict[str, Any], sample: list, engine: Any) -> str | None:
    """The node rule's longest-average-text `value_from` field over the sample, or None.

    Considers only the rule's declared property fields (the identity fields are often short codes);
    a field whose sampled values average below `_MIN_AVG_TEXT_LEN` chars is skipped as non-prose.
    Deterministic: among fields tied on average length, the first in recipe order wins.
    """
    candidates = [
        prop["value_from"]
        for prop in mapping.get("properties", [])
        if isinstance(prop.get("value_from"), str)
    ]
    best_ref: str | None = None
    best_len = -1.0
    for ref in candidates:
        avg = _avg_text_len(ref, sample, engine)
        if avg >= _MIN_AVG_TEXT_LEN and avg > best_len:
            best_len = avg
            best_ref = ref
    return best_ref


def _avg_text_len(ref: str, sample: list, engine: Any) -> float:
    """Mean char length of a field's non-empty values across the sampled records (0 if none)."""
    lengths: list[int] = []
    for unit in sample:
        value = _read_or_none(engine, unit, ref)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            lengths.append(len(text))
    if not lengths:
        return 0.0
    return sum(lengths) / len(lengths)


def _read_or_none(engine: Any, unit: Any, ref: str) -> Any:
    """Read a field off a record, returning None when the field is absent (a missing field on a
    record simply contributes nothing to the length sample — never sinks the synthesis)."""
    try:
        return engine.read_record_field(unit, ref)
    except Exception:  # noqa: BLE001 — a field absent on a record contributes nothing.
        return None
