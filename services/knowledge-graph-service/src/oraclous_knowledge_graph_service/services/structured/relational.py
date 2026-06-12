"""Relational structural primitive + default relational recipe (ORAA-4 §21 services layer).

Turns an introspected SQL :class:`SchemaSnapshot` (+ its fetched rows) into the SAME
:class:`StructuralRepresentation` vocabulary the recipe engine already projects for CSV/JSON, so a
SQL source rides the existing deterministic engine + org-scoped writer with NO new graph-write path.
This is the relational extractor the recipe schema's ``source_type: "relational"`` declared but had
no implementation for (#307).

Decomposition:
  * one SOURCE unit (the database);
  * one TABLE container unit per table (→ a ``:Table`` node);
  * one COLUMN unit per column under its table, carrying ``role`` (``primary_key``/``foreign_key``)
    and — for an FK — the ``fk_target`` table + ``fk_target_column`` in ``metadata`` (the keys the
    engine's ``fk_target`` edge resolver reads to wire cross-row references);
  * one RECORD unit per row (``full_snapshot``), payload in ``sample_values=[row_dict]`` (the same
    shape the engine reads for node identity + properties). ``schema_only`` emits NO records.

The default recipe projects each row to a ``{table}``-labelled node keyed by its PK, one property
per non-PK column, and one ``fk_target`` edge rule per FK column → a typed ``REFERENCES_{TARGET}``
edge to the referenced table's node (resolved by matching the FK value to the target row's PK — the
engine's ``resolve_by: fk_target`` path). Deterministic, no LLM. A caller may supply a recipe.
"""

from __future__ import annotations

import re

from oraclous_knowledge_graph_service.domain.connectors.sql_connector import (
    SchemaSnapshot,
    TableMeta,
    map_fk_relationship_type,
)
from oraclous_knowledge_graph_service.domain.structural import (
    ExtractionMode,
    StructuralRepresentation,
    StructuralUnit,
    UnitKind,
)

_SAFE_IDENTIFIER = re.compile(r"^(?!__)[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = frozenset({"__Platform__", "__Entity__", "__KGBuilder__", "__Rebac__", "__System__"})
_CONTAINER_LABELS = frozenset({"Source", "Table", "Sheet", "File", "Chunk"})


def _table_unit_id(table: str) -> str:
    return f"table:{table}"


def _to_pascal_case(name: str) -> str:
    parts = re.sub(r"[^0-9A-Za-z_]", "_", name).split("_")
    label = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if (
        not label
        or not _SAFE_IDENTIFIER.match(label)
        or label in _RESERVED
        or label in _CONTAINER_LABELS
    ):
        label = "Tbl_" + re.sub(r"[^0-9A-Za-z_]", "_", name).strip("_")
    if not _SAFE_IDENTIFIER.match(label) or label in _CONTAINER_LABELS:
        label = "Tbl"
    return label


def _sanitize_key(name: str, used: set[str]) -> str:
    candidate = re.sub(r"[^0-9A-Za-z_]", "_", name.strip())
    candidate = re.sub(r"^_+", "", candidate)
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


def _first_pk(table: TableMeta) -> str | None:
    for col in table.columns:
        if col.is_pk:
            return col.name
    return None


def decompose_relational(
    snapshot: SchemaSnapshot,
    rows_by_table: dict[str, list[dict]],
    mode: ExtractionMode,
) -> StructuralRepresentation:
    """Build the StructuralRepresentation from an introspected snapshot + its fetched rows.

    ``rows_by_table`` is keyed by table name (empty for ``schema_only``). The shape signature is a
    deterministic descriptor of the schema (the recipe-lookup key)."""
    source_id = "source"
    units: list[StructuralUnit] = [
        StructuralUnit(
            kind=UnitKind.SOURCE,
            unit_id=source_id,
            name=snapshot.database,
            metadata={
                "dialect": snapshot.dialect.value,
                "schema": snapshot.schema_name,
                "table_count": len(snapshot.tables),
            },
        )
    ]
    for table in snapshot.tables:
        tid = _table_unit_id(table.name)
        units.append(
            StructuralUnit(kind=UnitKind.TABLE, unit_id=tid, name=table.name, parent_id=source_id)
        )
        for col in table.columns:
            role = "primary_key" if col.is_pk else ("foreign_key" if col.is_fk else None)
            metadata: dict = {}
            if col.is_fk and col.fk_table:
                metadata = {
                    "fk_target": _table_unit_id(col.fk_table),
                    "fk_target_table": col.fk_table,
                    "fk_target_column": col.fk_column or "id",
                    "fk_target_present": True,
                }
            units.append(
                StructuralUnit(
                    kind=UnitKind.COLUMN,
                    unit_id=f"{tid}:column:{col.name}",
                    name=col.name,
                    data_type=col.data_type,
                    role=role,
                    parent_id=tid,
                    metadata=metadata,
                )
            )
        rows = rows_by_table.get(table.name, []) if mode == ExtractionMode.FULL else []
        for idx, row in enumerate(rows):
            units.append(
                StructuralUnit(
                    kind=UnitKind.RECORD,
                    unit_id=f"{tid}:record:{idx}",
                    name=f"{table.name} row {idx}",
                    # `role` carries the table name so a per-table node rule matches ONLY this
                    # table's rows (the engine `_matches` discriminates on role) — distinct tables
                    # never collapse into one label.
                    role=f"table:{table.name}",
                    parent_id=tid,
                    sample_values=[dict(row)],
                )
            )
    return StructuralRepresentation(
        source_type="relational",
        shape_signature=_shape_signature(snapshot),
        mode=mode,
        units=units,
    )


def _shape_signature(snapshot: SchemaSnapshot) -> str:
    parts = []
    for table in sorted(snapshot.tables, key=lambda t: t.name):
        cols = ",".join(f"{c.name}:{c.data_type}" for c in table.columns)
        parts.append(f"{table.name}({cols})")
    return f"relational[{snapshot.dialect.value}](" + ";".join(parts) + ")"


def build_default_relational_recipe(snapshot: SchemaSnapshot) -> dict:
    """Synthesise a valid format-0.2 recipe from the snapshot: one node rule per table (identity =
    PK, properties = non-PK columns) + one ``fk_target`` edge rule per FK column → a typed
    ``REFERENCES_{TARGET}`` edge to the referenced table's node. A table with no PK is skipped for
    node projection (we cannot key its rows uniquely)."""
    mappings: list[dict] = []
    label_by_table: dict[str, str] = {}
    node_rule_by_table: dict[str, str] = {}

    for table in snapshot.tables:
        pk = _first_pk(table)
        if pk is None:
            continue  # cannot uniquely key rows without a PK
        label = _to_pascal_case(table.name)
        label_by_table[table.name] = label
        node_rule_id = f"node_{table.name}"
        node_rule_by_table[table.name] = node_rule_id
        used: set[str] = set()
        properties = [
            {"name": _sanitize_key(c.name, used), "value_from": f"column:{c.name}"}
            for c in table.columns
            if not c.is_pk
        ]
        mappings.append(
            {
                "id": node_rule_id,
                "project_to": "node",
                "label": label,
                # Match ONLY this table's RECORD units (role carries the table name) so each table
                # projects under its own label and the fk_target resolver's parent_id filtering
                # selects the right source/target rows.
                "match": {"unit_kind": "record", "role": f"table:{table.name}"},
                "identity": {
                    "scheme": "deterministic",
                    "from": [f"column:{pk}"],
                    "normalize": ["trim"],
                },
                "properties": properties,
            }
        )

    # FK edges: a `fk_target` edge resolver matches each source row's FK value to the target row's
    # referenced column. The COLUMN unit carries `fk_target`/`fk_target_column` metadata (set by
    # decompose_relational), which the engine reads — so the edge rule matches the FK COLUMN unit.
    for table in snapshot.tables:
        if table.name not in node_rule_by_table:
            continue
        for col in table.columns:
            if not (col.is_fk and col.fk_table):
                continue
            target_rule = node_rule_by_table.get(col.fk_table)
            if target_rule is None:
                continue  # the referenced table had no PK / was skipped — no node to link to
            mappings.append(
                {
                    "id": f"edge_{table.name}_{col.name}",
                    "project_to": "edge",
                    "type": map_fk_relationship_type(col.fk_table),
                    "match": {"unit_kind": "column", "name": col.name},
                    "from": {"node_rule": node_rule_by_table[table.name]},
                    "to": {"node_rule": target_rule, "resolve_by": "fk_target"},
                }
            )

    if not mappings:
        # No table had a PK — still emit a minimal node rule so the recipe is valid (records project
        # by their full payload identity, mirroring the CSV/JSON default's fallback).
        mappings.append(
            {
                "id": "records",
                "project_to": "node",
                "label": "Record",
                "match": {"unit_kind": "record"},
                "identity": {"scheme": "deterministic", "from": ["name"], "normalize": ["trim"]},
            }
        )

    return {
        "recipe_format_version": "0.2",
        "id": f"rcp_default-relational-{snapshot.dialect.value}",
        "version": 1,
        "status": "promoted",
        "concern": "relational",
        "applies_to": {
            "source_type": "relational",
            "shape_signature": _shape_signature(snapshot),
        },
        "defaults": {"provenance": "EXTRACTED"},
        "mappings": mappings,
    }
