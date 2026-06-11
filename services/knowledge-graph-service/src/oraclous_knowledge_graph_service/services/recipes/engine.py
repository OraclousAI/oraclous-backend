"""Recipe execution engine (ORAA-4 §21 services layer — planning only, no driver).

Faithful port of legacy `develop@84152635 knowledge-graph-builder/app/recipes/engine.py` (ADR-022):
deterministic, no-LLM interpretation of a validated recipe over a StructuralRepresentation into the
unified graph (:Source / container / :__Entity__ nodes + edges). Every inline `driver.session().run`
is reshaped into a call on the injected org-scoped `RecipeGraphWriter` (the only Neo4j access); the
engine is pure planning. Identity hashes keep `graph_id` as the leading segment (unchanged ids); the
writer adds organisation_id to the MERGE keys. The `text_extraction` rule kind is the LLM null seam.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from oraclous_knowledge_graph_service.domain.ontology import Ontology, resolve_label
from oraclous_knowledge_graph_service.domain.structural import (
    StructuralRepresentation,
    StructuralUnit,
)
from oraclous_knowledge_graph_service.repositories.recipe_write_repository import RecipeGraphWriter

_SAFE_IDENTIFIER = re.compile(r"^(?!__)[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_LABELS = frozenset(
    {"__Platform__", "__Entity__", "__KGBuilder__", "__Rebac__", "__System__"}
)
_CONTAINER_LABELS = frozenset({"Source", "Table", "Sheet", "File", "Chunk"})
_CONTAINER_KIND_TO_LABEL: dict[str, str] = {
    "table": "Table",
    "sheet": "Sheet",
    "file": "File",
    "chunk": "Chunk",
}
_RECORD_KINDS = frozenset({"record", "row"})
_SUPPORTED_FORMAT_VERSIONS = frozenset({"0.2"})
_SCHEMA_PATH = Path(__file__).parent / "recipe.schema.json"


class RecipeValidationError(ValueError):
    """A recipe failed schema or identifier validation."""


def _is_safe_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(_SAFE_IDENTIFIER.match(value))
        and value not in _RESERVED_LABELS
    )


def _normalize_identity(value: Any, ops: list[str]) -> str:
    text = "" if value is None else str(value)
    for op in ops:
        if op == "casefold":
            text = text.casefold()
        elif op == "trim":
            text = text.strip()
        elif op == "collapse_whitespace":
            text = re.sub(r"\s+", " ", text)
    return text


def _deterministic_id(graph_id: str, label: str, identity_key: str) -> str:
    return hashlib.sha256(f"{graph_id}|{label}|{identity_key}".encode()).hexdigest()[:32]


def _source_id(graph_id: str, source_descriptor: str) -> str:
    return hashlib.sha256(f"{graph_id}|source|{source_descriptor}".encode()).hexdigest()[:32]


def _container_id(graph_id: str, unit_id: str) -> str:
    return hashlib.sha256(f"{graph_id}|container|{unit_id}".encode()).hexdigest()[:32]


def _coerce_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str, type(None))):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_coerce_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _coerce_value(v) for k, v in value.items()}
    return str(value)


@dataclass
class ExecutionResult:
    recipe_id: str
    recipe_version: int
    graph_id: str
    source_id: str
    containers_written: int = 0
    nodes_written: int = 0
    edges_written: int = 0
    properties_written: int = 0
    units_skipped: int = 0
    ontology_violations: int = 0
    ontology_coercions: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "recipe_version": self.recipe_version,
            "graph_id": self.graph_id,
            "source_id": self.source_id,
            "containers_written": self.containers_written,
            "nodes_written": self.nodes_written,
            "edges_written": self.edges_written,
            "properties_written": self.properties_written,
            "units_skipped": self.units_skipped,
            "ontology_violations": self.ontology_violations,
            "ontology_coercions": self.ontology_coercions,
            "warnings": self.warnings,
        }


class RecipeExecutionEngine:
    SUPPORTED_FORMAT_VERSIONS = _SUPPORTED_FORMAT_VERSIONS

    def __init__(self) -> None:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._validator = Draft202012Validator(schema)

    # --- validation ---------------------------------------------------------
    def validate(self, recipe: dict[str, Any]) -> None:
        """Public: validate a recipe (schema + format version + identifiers). Raises on failure."""
        self._validate_recipe(recipe)

    def _validate_recipe(self, recipe: dict[str, Any]) -> None:
        if not isinstance(recipe, dict):
            raise RecipeValidationError("recipe must be a JSON object")
        errors = sorted(self._validator.iter_errors(recipe), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            path = "/".join(str(p) for p in first.path) or "<root>"
            raise RecipeValidationError(
                f"recipe failed schema validation at {path}: {first.message}"
            )
        fmt = recipe.get("recipe_format_version")
        if fmt not in self.SUPPORTED_FORMAT_VERSIONS:
            raise RecipeValidationError(f"unsupported recipe_format_version {fmt!r}")
        for rule in recipe["mappings"]:
            self._check_rule_identifiers(rule)
        self._check_foreign_key_edges(recipe["mappings"])

    def _check_foreign_key_edges(self, mappings: list[dict[str, Any]]) -> None:
        """A `foreign_key` edge needs a `to.from_field`, and its target node_rule must exist and
        have a single-field identity (the FK value stands in for that one identity field). Rejected
        at validate time so a malformed recipe fails at store/POST, not mid-ingest.
        """
        by_id = {r["id"]: r for r in mappings}
        for rule in mappings:
            if rule.get("project_to") != "edge":
                continue
            if rule.get("to", {}).get("resolve_by") != "foreign_key":
                continue
            if not rule["to"].get("from_field"):
                raise RecipeValidationError(
                    f"foreign_key edge rule {rule['id']!r}: missing required to.from_field"
                )
            target = by_id.get(rule["to"]["node_rule"])
            if target is None or target.get("project_to") != "node":
                raise RecipeValidationError(
                    f"foreign_key edge rule {rule['id']!r}: target node_rule "
                    f"{rule['to']['node_rule']!r} is not a node rule"
                )
            if len(target["identity"]["from"]) != 1:
                raise RecipeValidationError(
                    f"foreign_key edge rule {rule['id']!r}: target node_rule "
                    f"{target['id']!r} must have a single-field identity "
                    f"(got {len(target['identity']['from'])} fields); a composite identity has no "
                    f"single FK value to stand in for it"
                )

    def _check_rule_identifiers(self, rule: dict[str, Any]) -> None:
        kind = rule["project_to"]
        if kind == "node":
            label = rule["label"]
            if not _is_safe_identifier(label):
                raise RecipeValidationError(f"node rule {rule['id']!r}: unsafe label {label!r}")
            if label in _CONTAINER_LABELS:
                raise RecipeValidationError(
                    f"node rule {rule['id']!r}: label {label!r} collides with a container label"
                )
            for prop in rule.get("properties", []):
                if not _is_safe_identifier(prop["name"]):
                    raise RecipeValidationError(
                        f"node rule {rule['id']!r}: unsafe property key {prop['name']!r}"
                    )
        elif kind == "edge":
            if not _is_safe_identifier(rule["type"]):
                raise RecipeValidationError(
                    f"edge rule {rule['id']!r}: unsafe type {rule['type']!r}"
                )
            for prop in rule.get("properties", []):
                if not _is_safe_identifier(prop["name"]):
                    raise RecipeValidationError(f"edge rule {rule['id']!r}: unsafe property key")
        elif kind == "property":
            if not _is_safe_identifier(rule["name"]):
                raise RecipeValidationError(f"property rule {rule['id']!r}: unsafe key")
        elif kind == "text_extraction":
            if not _is_safe_identifier(rule["link_to"]["edge_type"]):
                raise RecipeValidationError(
                    f"text_extraction rule {rule['id']!r}: unsafe edge_type"
                )

    # --- matching + field reads --------------------------------------------
    @staticmethod
    def _matches(match: dict[str, Any], unit: StructuralUnit) -> bool:
        if match["unit_kind"] != unit.kind.value:
            return False
        if "name" in match and match["name"] != unit.name:
            return False
        if "role" in match and match["role"] != unit.role:
            return False
        return True

    def _first_match(self, mappings: list[dict], unit: StructuralUnit) -> dict | None:
        for rule in mappings:
            if self._matches(rule["match"], unit):
                return rule
        return None

    def _all_matches(self, mappings: list[dict], unit: StructuralUnit) -> list[dict]:
        """Every rule whose match selects this unit, in recipe order. A single record can drive
        several rules — e.g. an Evidence node + a ClaimSource node + the FROM_SOURCE edge between
        them all match `unit_kind: record`. (The default recipe still has one rule per unit; this
        is a superset of _first_match.)"""
        return [rule for rule in mappings if self._matches(rule["match"], unit)]

    @staticmethod
    def _read_field(payload: dict, unit: StructuralUnit, ref: str) -> Any:
        if ref in ("name", "unit:name"):
            return unit.name
        key = ref.partition(":")[2] if ":" in ref else ref
        # G2: a dotted key (e.g. `source.url`) is a path into a nested object — walk it segment
        # by segment. A top-level key (no dot) keeps the original `payload.get(key)` behaviour.
        # Any missing or non-dict segment short-circuits to None (a value never resolved).
        if "." not in key:
            return payload.get(key)
        current: Any = payload
        for segment in key.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(segment)
            if current is None:
                return None
        return current

    @staticmethod
    def _resolve_unit_payload(unit: StructuralUnit) -> dict[str, Any]:
        if unit.kind.value in _RECORD_KINDS and unit.sample_values:
            first = unit.sample_values[0]
            if isinstance(first, dict):
                return first
        return {}

    # --- execution ----------------------------------------------------------
    def execute(
        self,
        recipe: dict[str, Any],
        representation: StructuralRepresentation,
        writer: RecipeGraphWriter,
        *,
        ontology: Ontology | None = None,
        temporal: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        self._validate_recipe(recipe)
        graph_id = writer.graph_id
        recipe_id, recipe_version = recipe["id"], recipe["version"]
        ingestion_time = datetime.now(UTC).isoformat()
        source_descriptor = f"{representation.source_type}:{representation.shape_signature}"
        source_id = _source_id(graph_id, source_descriptor)
        result = ExecutionResult(recipe_id, recipe_version, graph_id, source_id)
        meta = {
            "recipe_id": recipe_id,
            "recipe_version": recipe_version,
            "ingestion_time": ingestion_time,
        }
        self._ontology = ontology
        self._temporal = {k: v for k, v in (temporal or {}).items() if v is not None}
        # Stashed for the foreign_key resolver: it computes a target node's deterministic id from
        # the FK value, which needs the target node_rule (its label + identity) and the graph_id.
        self._mappings_by_id = {r["id"]: r for r in recipe["mappings"]}
        self._graph_id = graph_id
        payload_cache = {u.unit_id: self._resolve_unit_payload(u) for u in representation.units}
        defaults = recipe.get("defaults", {})

        writer.write_source(
            source_id=source_id,
            source_type=representation.source_type,
            shape_signature=representation.shape_signature,
            meta=meta,
        )
        unit_to_container = self._write_containers(writer, representation, source_id, meta, result)

        node_index: dict[tuple[str, str], str] = {}
        for unit in representation.units:
            matched = self._all_matches(recipe["mappings"], unit)
            if not matched:
                result.units_skipped += 1
                continue
            for rule in matched:
                if rule["project_to"] == "node":
                    eid = self._project_node(
                        writer,
                        rule,
                        unit,
                        graph_id,
                        meta,
                        defaults,
                        unit_to_container,
                        source_id,
                        payload_cache,
                        result,
                    )
                    if eid is not None:
                        node_index[(rule["id"], unit.unit_id)] = eid
                elif rule["project_to"] == "skip":
                    result.units_skipped += 1

        # The identity/self/foreign_key edge resolvers and the property rule fan out over the whole
        # node_index (unit-independent), so they must run exactly ONCE per rule — applying a
        # same-record edge per matching record would re-emit the identical edge batch N times. The
        # legacy fk_target resolver and text_extraction are still per-unit (they read the matched
        # unit), so they keep firing per matching unit. Dedupe the once-per-rule kinds by rule id.
        applied_once: set[str] = set()
        for unit in representation.units:
            for rule in self._all_matches(recipe["mappings"], unit):
                kind = rule["project_to"]
                if kind in ("node", "skip"):
                    continue
                once_per_rule = kind == "property" or (
                    kind == "edge" and rule["to"].get("resolve_by", "identity") != "fk_target"
                )
                if once_per_rule:
                    if rule["id"] in applied_once:
                        continue
                    applied_once.add(rule["id"])
                if kind == "property":
                    self._apply_property(writer, rule, unit, node_index, payload_cache, result)
                elif kind == "edge":
                    self._apply_edge(
                        writer,
                        rule,
                        unit,
                        meta,
                        defaults,
                        node_index,
                        representation,
                        payload_cache,
                        source_id,
                        result,
                    )
                elif kind == "text_extraction":
                    self._apply_text_extraction(rule, unit, result)
        return result

    def _write_containers(
        self, writer: RecipeGraphWriter, representation, source_id, meta, result
    ) -> dict[str, str]:
        graph_id = writer.graph_id
        unit_to_container: dict[str, str] = {}
        by_label: dict[str, list[dict]] = {}
        for unit in representation.units:
            label = _CONTAINER_KIND_TO_LABEL.get(unit.kind.value)
            if label is None:
                continue
            cid = _container_id(graph_id, unit.unit_id)
            unit_to_container[unit.unit_id] = cid
            by_label.setdefault(label, []).append(
                {"id": cid, "unit_id": unit.unit_id, "name": unit.name}
            )
        for label, rows in by_label.items():
            writer.write_containers(label=label, rows=rows, source_id=source_id, meta=meta)
            result.containers_written += len(rows)
        pairs = [
            {"child": unit_to_container[u.unit_id], "parent": unit_to_container[u.parent_id]}
            for u in representation.units
            if u.unit_id in unit_to_container and (u.parent_id or "") in unit_to_container
        ]
        if pairs:
            writer.link_containers(pairs=pairs)
        return unit_to_container

    def _project_node(
        self,
        writer,
        rule,
        unit,
        graph_id,
        meta,
        defaults,
        unit_to_container,
        source_id,
        payload_cache,
        result,
    ) -> str | None:
        label = rule["label"]
        # Ontology enforcement (inline): resolve the label against the graph's ontology.
        resolved_label, coerced = resolve_label(getattr(self, "_ontology", None), label)
        if resolved_label is None:
            result.ontology_violations += 1
            result.units_skipped += 1
            return None
        if coerced:
            result.ontology_coercions += 1
        label = resolved_label
        payload = payload_cache.get(unit.unit_id, {})
        identity = rule["identity"]
        normalize_ops = identity.get("normalize", [])
        parts = [
            _normalize_identity(self._read_field(payload, unit, ref), normalize_ops)
            for ref in identity["from"]
        ]
        identity_key = "|".join(parts)
        if not identity_key.strip("|"):
            result.warnings.append(
                f"node rule {rule['id']!r}: empty identity for unit {unit.unit_id!r}"
            )
            result.units_skipped += 1
            return None
        entity_id = _deterministic_id(graph_id, label, identity_key)
        props: dict[str, Any] = dict(
            getattr(self, "_temporal", {})
        )  # valid_from/valid_to/event_time
        for prop in rule.get("properties", []):
            value = self._read_field(payload, unit, prop["value_from"])
            if value is not None:
                props[prop["name"]] = _coerce_value(value)
        provenance = rule.get("provenance") or defaults.get("provenance", "EXTRACTED")
        confidence = rule.get("confidence", 0.5) if provenance == "INFERRED" else None
        container_id = unit_to_container.get(unit.parent_id or "")
        writer.merge_node(
            label=label,
            entity_id=entity_id,
            identity_key=identity_key,
            properties=props,
            provenance=provenance,
            source_id=source_id,
            meta=meta,
            confidence=confidence,
            container_id=container_id,
        )
        result.nodes_written += 1
        return entity_id

    def _apply_property(self, writer, rule, unit, node_index, payload_cache, result) -> None:
        on_rule_id = rule["on"]
        value_ref = rule.get("value_from") or f"column:{rule['name']}"
        targets = []
        for (rid, unit_id), entity_id in node_index.items():
            if rid != on_rule_id:
                continue
            value = self._read_field(payload_cache.get(unit_id, {}), unit, value_ref)
            if value is not None:
                targets.append({"id": entity_id, "value": _coerce_value(value)})
        result.properties_written += writer.set_property(prop_name=rule["name"], targets=targets)

    def _apply_edge(
        self,
        writer,
        rule,
        unit,
        meta,
        defaults,
        node_index,
        representation,
        payload_cache,
        source_id,
        result,
    ) -> None:
        from_rule_id, to_rule_id = rule["from"]["node_rule"], rule["to"]["node_rule"]
        resolve_by = rule["to"].get("resolve_by", "identity")
        provenance = rule.get("provenance") or defaults.get("provenance", "EXTRACTED")
        if resolve_by == "foreign_key":
            self._apply_foreign_key_edge(
                writer,
                rule,
                from_rule_id,
                to_rule_id,
                node_index,
                payload_cache,
                source_id,
                provenance,
                meta,
                result,
            )
            return
        edges: list[dict] = []
        if resolve_by == "self":
            edges = [
                {"from": eid, "to": eid}
                for (rid, _u), eid in node_index.items()
                if rid == from_rule_id
            ]
        elif resolve_by == "identity":
            to_by_unit = {u: e for (rid, u), e in node_index.items() if rid == to_rule_id}
            for (rid, unit_id), eid in node_index.items():
                if rid != from_rule_id:
                    continue
                target = to_by_unit.get(unit_id)
                if target is not None:
                    edges.append({"from": eid, "to": target})
        else:  # fk_target
            edges = self._resolve_fk_target_edges(
                rule,
                unit,
                from_rule_id,
                to_rule_id,
                node_index,
                representation,
                payload_cache,
                result,
            )
        result.edges_written += writer.merge_edge(
            rel_type=rule["type"],
            edges=edges,
            source_id=source_id,
            provenance=provenance,
            meta=meta,
        )

    def _resolve_fk_target_edges(
        self,
        rule,
        unit,
        from_rule_id,
        to_rule_id,
        node_index,
        representation,
        payload_cache,
        result,
    ) -> list[dict]:
        fk_target_table = unit.metadata.get("fk_target")
        if not fk_target_table or not unit.metadata.get("fk_target_present", True):
            return []
        fk_column_name = unit.name
        ref_column = unit.metadata.get("fk_target_column", "id")
        source_table_id = unit.parent_id
        unit_by_id = {u.unit_id: u for u in representation.units}
        to_by_ref_value: dict[Any, str] = {}
        for (rid, target_unit_id), eid in node_index.items():
            if rid != to_rule_id:
                continue
            target_unit = unit_by_id.get(target_unit_id)
            if target_unit is None or target_unit.parent_id != fk_target_table:
                continue
            ref_value = payload_cache.get(target_unit_id, {}).get(ref_column)
            if ref_value is not None:
                to_by_ref_value[ref_value] = eid
        edges, unmatched = [], 0
        for (rid, source_unit_id), eid in node_index.items():
            if rid != from_rule_id:
                continue
            source_unit = unit_by_id.get(source_unit_id)
            if source_unit is None or (
                source_table_id is not None and source_unit.parent_id != source_table_id
            ):
                continue
            fk_value = payload_cache.get(source_unit_id, {}).get(fk_column_name)
            if fk_value is None:
                continue
            target = to_by_ref_value.get(fk_value)
            if target is None:
                unmatched += 1
                continue
            edges.append({"from": eid, "to": target})
        if unmatched:
            result.warnings.append(f"edge rule {rule['id']!r}: {unmatched} unmatched fk value(s)")
        return edges

    def _target_node_identity(self, to_rule_id: str) -> tuple[str, list[str]]:
        """Resolve the target node_rule's write-time (label, normalize-ops) for FK id computation.

        The deterministic id MUST be computed exactly as the target record's own ingest would
        compute it (same id function, same resolved label, same normalize chain) so a value linked
        here matches the node materialised by a later/separate target ingest. The target identity
        must be a SINGLE field for foreign_key — a composite has no single FK value to stand in.
        """
        to_rule = self._mappings_by_id.get(to_rule_id)
        if to_rule is None or to_rule.get("project_to") != "node":
            raise RecipeValidationError(
                f"foreign_key edge target node_rule {to_rule_id!r} is not a node rule"
            )
        identity = to_rule["identity"]
        if len(identity["from"]) != 1:
            raise RecipeValidationError(
                f"foreign_key edge target node_rule {to_rule_id!r} must have a single-field "
                f"identity (got {len(identity['from'])} fields); a composite has no single FK value"
            )
        resolved_label, _coerced = resolve_label(getattr(self, "_ontology", None), to_rule["label"])
        if resolved_label is None:
            raise RecipeValidationError(
                f"foreign_key edge target node_rule {to_rule_id!r}: label "
                f"{to_rule['label']!r} is not permitted by the ontology"
            )
        return resolved_label, identity.get("normalize", [])

    def _apply_foreign_key_edge(
        self,
        writer,
        rule,
        from_rule_id,
        to_rule_id,
        node_index,
        payload_cache,
        source_id,
        provenance,
        meta,
        result,
    ) -> None:
        """G1: a recipe-declared, list-capable, cross-record/cross-file foreign-key edge.

        For each matched SOURCE node, read its `to.from_field` (dotted-aware via _read_field) — a
        scalar or a list of FK values. Each value IS the target's identity: normalise it with the
        target rule's chain and hash it with the engine's own deterministic id
        (sha256(graph_id|target_label|value)) — the SAME id the target's record ingest will produce.
        MERGE (source)-[type]->(target_id), creating the target as a stub if it is not present yet.
        No graph query, so a target ingested in a separate run/file still links (cross-job/
        cross-file). Edges are stamped with the recipe provenance default.
        """
        from_field = rule["to"].get("from_field")
        if not from_field:
            raise RecipeValidationError(
                f"foreign_key edge rule {rule['id']!r}: missing required to.from_field"
            )
        target_label, normalize_ops = self._target_node_identity(to_rule_id)
        graph_id = self._graph_id
        edges: list[dict] = []
        for (rid, source_unit_id), source_eid in node_index.items():
            if rid != from_rule_id:
                continue
            payload = payload_cache.get(source_unit_id, {})
            raw = self._read_field(payload, None, from_field)
            if raw is None:
                continue
            fk_values = raw if isinstance(raw, list) else [raw]
            for value in fk_values:
                if value is None:
                    continue
                identity_key = _normalize_identity(value, normalize_ops)
                if not identity_key.strip():
                    continue
                target_id = _deterministic_id(graph_id, target_label, identity_key)
                edges.append(
                    {"from": source_eid, "to": target_id, "target_identity_key": identity_key}
                )
        result.edges_written += writer.merge_edge_to_stub(
            rel_type=rule["type"],
            target_label=target_label,
            edges=edges,
            source_id=source_id,
            provenance=provenance,
            meta=meta,
        )

    def _apply_text_extraction(self, rule, unit, result) -> None:
        result.units_skipped += 1
        result.warnings.append(
            f"text_extraction rule {rule['id']!r} (primitive {rule['primitive']!r}) skipped for "
            f"unit {unit.unit_id!r}: the LLM-backed text-extraction primitive is the null seam."
        )


@lru_cache(maxsize=1)
def get_recipe_engine() -> RecipeExecutionEngine:
    """Shared engine (loads + compiles the recipe schema once)."""
    return RecipeExecutionEngine()
