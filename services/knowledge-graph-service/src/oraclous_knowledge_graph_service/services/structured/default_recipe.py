"""Default-recipe synthesis (services layer).

So a user can ingest a CSV/JSON and immediately get graph nodes WITHOUT authoring a recipe: build a
valid format-0.2 recipe from the structural representation that maps every RECORD unit to a
`:Record:__Entity__` node, identity = all column/field values (distinct rows -> distinct nodes;
identical re-ingest -> idempotent MERGE), properties = one per column/field (keys sanitised to safe
identifiers). No edges, no LLM. A caller may instead supply a stored recipe for richer projections.
"""

from __future__ import annotations

import re

from oraclous_knowledge_graph_service.domain.structural import StructuralRepresentation

_SAFE_IDENTIFIER = re.compile(r"^(?!__)[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = frozenset({"__Platform__", "__Entity__", "__KGBuilder__", "__Rebac__", "__System__"})


def _sanitize_key(name: str, used: set[str]) -> str:
    candidate = re.sub(r"[^0-9A-Za-z_]", "_", name.strip())
    candidate = re.sub(r"^_+", "", candidate)  # never an __wrapped__ / leading-underscore key
    if not candidate or not _SAFE_IDENTIFIER.match(candidate) or candidate in _RESERVED:
        candidate = "col_" + re.sub(r"[^0-9A-Za-z_]", "_", name.strip()).strip("_")
    if not _SAFE_IDENTIFIER.match(candidate):
        candidate = "col"
    base, i = candidate, 1
    while candidate in used:
        candidate = f"{base}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def build_default_recipe(representation: StructuralRepresentation) -> dict:
    source_type = representation.source_type  # "csv" | "json"
    ref_prefix = "column" if source_type == "csv" else "field"
    names = [u.name for u in representation.units if u.kind.value in ("column", "field") and u.name]
    used: set[str] = set()
    properties = []
    identity_from = []
    for name in names:
        ref = f"{ref_prefix}:{name}"
        identity_from.append(ref)
        properties.append({"name": _sanitize_key(name, used), "value_from": ref})
    if not identity_from:
        identity_from = ["name"]

    return {
        "recipe_format_version": "0.2",
        "id": f"rcp_default-{source_type}-structural",
        "version": 1,
        "status": "promoted",
        "concern": "structural",
        "applies_to": {
            "source_type": source_type,
            "shape_signature": representation.shape_signature,
        },
        "defaults": {"provenance": "EXTRACTED"},
        "mappings": [
            {
                "id": "records",
                "project_to": "node",
                "label": "Record",
                "match": {"unit_kind": "record"},
                "identity": {
                    "scheme": "deterministic",
                    "from": identity_from,
                    "normalize": ["trim"],
                },
                "properties": properties,
            }
        ],
    }
