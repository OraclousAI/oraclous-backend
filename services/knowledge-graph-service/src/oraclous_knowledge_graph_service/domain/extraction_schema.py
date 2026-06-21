"""Ontology → free-text extraction schema compiler (domain layer — pure, no I/O).

The bridge that lets a graph's TYPED ontology drive free-text LLM extraction, restoring the legacy
instruction-driven extraction — made better: instead of injecting the schema as plain text in the
prompt (the legacy `build_schema_block`), the typed defs compile to a NATIVE neo4j-graphrag
`GraphSchema` that the `EntityExtractor` ENFORCES (hard schema), plus a prompt PREFIX for the soft
steering the schema can't express (domain context, density, focus, ignore).

Two pure functions, both fed into the extractor's `extract_for_chunk(schema, prompt_prefix, chunk)`:

  - `to_graph_schema(ontology)` -> `GraphSchema | None`
      Builds node types from `entity_types` (label + optional STRING property definitions) and
      relationship types from `relationship_types`. A `patterns` triple `(source, rel, target)` is
      emitted ONLY when source, rel and target are all declared (the library validates that patterns
      reference declared node/relationship types and would raise otherwise). Returns None when there
      are no entity_types — the graph stays OPEN exactly as before Slice B (the extractor then uses
      its default open `GraphSchema`, and the LLM discovers types freely).

      Hardness comes for free from the library: `GraphSchema(node_types=())` defaults
      `additional_node_types=True` (open), but as soon as node_types are supplied the defaults flip
      to closed — the extractor will not admit a node/rel/pattern outside the declared set.

  - `to_prompt_prefix(ontology)` -> `str`
      The soft-steering prefix the schema can't carry: a domain-context line, the entity +
      relationship types + descriptions, and the density/focus/ignore rules. Empty string when there
      are no hints, so free-form ontologies leave the base extraction prompt untouched.
"""

from __future__ import annotations

from neo4j_graphrag.experimental.components.schema import (
    GraphSchema,
    NodeType,
    PropertyType,
    RelationshipType,
)

from oraclous_knowledge_graph_service.domain.ontology import Ontology

_DENSITY_GUIDANCE = {
    "sparse": "sparse — extract only the major entities; prefer precision over recall.",
    "balanced": "balanced — extract meaningful entities; include supporting ones when relevant.",
    "dense": "dense — extract every entity mention; maximise recall.",
}


def to_graph_schema(ontology: Ontology | None) -> GraphSchema | None:
    """Compile the ontology's typed defs to a hard neo4j-graphrag `GraphSchema`, or None when open.

    None (no entity_types) means "stay open" — the extractor falls back to its default open schema.
    """
    if ontology is None or not ontology.entity_types:
        return None

    node_types = tuple(
        NodeType(
            label=et.name,
            description=et.description or "",
            properties=[PropertyType(name=p, type="STRING") for p in et.properties],
        )
        for et in ontology.entity_types
    )
    relationship_types = tuple(
        RelationshipType(label=rt.name, description="") for rt in ontology.relationship_types
    )

    declared_nodes = {et.name for et in ontology.entity_types}
    declared_rels = {rt.name for rt in ontology.relationship_types}
    # A pattern is only valid if its source, rel AND target are all declared (the library rejects a
    # pattern that references an undeclared node/relationship type). Skip partial defs silently.
    patterns = tuple(
        (rt.source, rt.name, rt.target)
        for rt in ontology.relationship_types
        if rt.source in declared_nodes and rt.target in declared_nodes and rt.name in declared_rels
    )

    return GraphSchema(
        node_types=node_types,
        relationship_types=relationship_types,
        patterns=patterns,
    )


def from_graph_schema(schema: GraphSchema, *, mode: str = "strict") -> dict:
    """Inverse of ``to_graph_schema``: a neo4j-graphrag ``GraphSchema`` → the Slice-B Ontology dict.

    Used by schema synthesis (``POST /ontology/suggest``): an LLM infers a ``GraphSchema`` from a
    text sample and this projects it into the SAME ``{mode, entity_types, relationship_types}``
    shape the ontology PUT accepts, so a suggestion round-trips straight into a saved ontology. Node
    property names are surfaced as ``entity_types[].properties``; ``patterns`` triples fill each
    relationship's ``source``/``target`` (the first pattern that names the relationship), so the
    suggestion carries the directed shape, not just bare labels.
    """
    pattern_by_rel: dict[str, tuple[str, str]] = {}
    for pattern_source, pattern_rel, pattern_target in schema.patterns:
        pattern_by_rel.setdefault(pattern_rel, (pattern_source, pattern_target))

    entity_types: list[dict] = [
        {
            "name": node.label,
            **({"description": node.description} if node.description else {}),
            **({"properties": [p.name for p in node.properties]} if node.properties else {}),
        }
        for node in schema.node_types
    ]
    relationship_types: list[dict] = []
    for rel in schema.relationship_types:
        entry: dict = {"name": rel.label}
        endpoints = pattern_by_rel.get(rel.label)
        if endpoints is not None:
            entry["source"], entry["target"] = endpoints
        relationship_types.append(entry)

    return {
        "mode": mode,
        "entity_types": entity_types,
        "relationship_types": relationship_types,
    }


def to_prompt_prefix(ontology: Ontology | None) -> str:
    """Build the soft-steering prompt prefix from the ontology's hints. Empty string when none."""
    if ontology is None or not ontology.has_hints:
        return ""

    blocks: list[str] = []

    if ontology.domain:
        blocks.append(
            "## Extraction Context\n"
            f"This text is from the domain: {ontology.domain}. "
            "Apply domain-specific extraction conventions."
        )

    if ontology.entity_types:
        lines = ["## Entity Types", "Extract ONLY entities of the following types:"]
        for et in ontology.entity_types:
            desc = f": {et.description}" if et.description else ""
            props = f" Properties to capture: {', '.join(et.properties)}." if et.properties else ""
            lines.append(f"- {et.name}{desc}.{props}")
        lines.append("Do NOT create entity types not listed above.")
        blocks.append("\n".join(lines))

    if ontology.relationship_types:
        lines = ["## Relationship Types", "Use ONLY these relationship types:"]
        for rt in ontology.relationship_types:
            src = rt.source or "Entity"
            tgt = rt.target or "Entity"
            lines.append(f"- ({src})-[{rt.name}]->({tgt})")
        blocks.append("\n".join(lines))

    rules: list[str] = []
    if ontology.density:
        guidance = _DENSITY_GUIDANCE.get(ontology.density, ontology.density)
        rules.append(f"- Extraction density: {guidance}")
    if ontology.ignore:
        rules.append(f"- Ignore text matching: {', '.join(ontology.ignore)}")
    if rules:
        blocks.append("\n".join(["## Extraction Rules", *rules]))

    if ontology.focus:
        lines = ["## Focus Areas", "Pay special attention to:"]
        lines.extend(f"- {area}" for area in ontology.focus)
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
