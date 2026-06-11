"""Graph ontology (ORAA-4 §21 domain layer — pure, no I/O).

An ontology constrains which entity labels a graph admits, with a mode:
  - open   : no constraint (default).
  - strict : a node whose label is not allowed is rejected (skipped, counted as a violation).
  - coerce : an unknown label is mapped to the nearest allowed label (difflib ratio >= cutoff,
             ported from legacy `ontology_tasks`); below the cutoff it is rejected.
Enforced INLINE in the recipe engine at projection time (better than the legacy retroactive sweep):
the resolved label is what gets written, so a strict graph never gains an off-ontology node.

Slice B adds an optional TYPED layer on top of the labels-only shape: entity + relationship type
definitions (with descriptions/properties) plus free-text extraction hints (domain, density, focus,
ignore). The typed defs compile (in `domain/extraction_schema.py`) to a NATIVE neo4j-graphrag
`GraphSchema` (hard — the extractor enforces it) and a prompt prefix (soft steering), so the SAME
ontology that gates the structured path now also drives free-text LLM extraction. When
`entity_types` is set, `allowed_labels` is DERIVED from their names — so `resolve_label` enforcement
is unchanged and applies to both paths. The shape is fully back-compatible: a legacy labels-only row
parses exactly as before, and `as_dict` round-trips either shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

_COERCE_CUTOFF = 0.7
MODES = ("open", "strict", "coerce")
DENSITIES = ("sparse", "balanced", "dense")


@dataclass(frozen=True)
class EntityType:
    """A typed entity definition: a node label, an optional description, optional property names."""

    name: str
    description: str | None = None
    properties: tuple[str, ...] = ()

    @classmethod
    def of(cls, data: dict) -> EntityType:
        return cls(
            name=data["name"],
            description=data.get("description") or None,
            properties=tuple(data.get("properties") or ()),
        )

    def as_dict(self) -> dict:
        out: dict = {"name": self.name}
        if self.description:
            out["description"] = self.description
        if self.properties:
            out["properties"] = list(self.properties)
        return out


@dataclass(frozen=True)
class RelType:
    """A typed relationship definition: a rel label + optional source/target entity names."""

    name: str
    source: str | None = None
    target: str | None = None

    @classmethod
    def of(cls, data: dict) -> RelType:
        return cls(
            name=data["name"],
            source=data.get("source") or None,
            target=data.get("target") or None,
        )

    def as_dict(self) -> dict:
        out: dict = {"name": self.name}
        if self.source:
            out["source"] = self.source
        if self.target:
            out["target"] = self.target
        return out


@dataclass(frozen=True)
class Ontology:
    allowed_labels: tuple[str, ...]
    mode: str
    entity_types: tuple[EntityType, ...] = ()
    relationship_types: tuple[RelType, ...] = ()
    # Free-text extraction hints (soft steering for the LLM; ignored on the structured path).
    domain: str | None = None
    density: str | None = None  # one of DENSITIES, or None
    focus: tuple[str, ...] = field(default=())
    ignore: tuple[str, ...] = field(default=())

    @classmethod
    def of(cls, data: dict | None) -> Ontology | None:
        """Parse BOTH the typed shape and the legacy labels-only shape (back-compatible).

        When `entity_types` is present, `allowed_labels` is derived from their names (the typed defs
        are authoritative); otherwise the legacy `allowed_labels` list is used verbatim.
        """
        if not data:
            return None
        entity_types = tuple(EntityType.of(e) for e in data.get("entity_types") or ())
        relationship_types = tuple(RelType.of(r) for r in data.get("relationship_types") or ())
        allowed_labels = (
            tuple(e.name for e in entity_types)
            if entity_types
            else tuple(data.get("allowed_labels", []))
        )
        density = data.get("density") or None
        return cls(
            allowed_labels=allowed_labels,
            mode=data.get("mode", "open"),
            entity_types=entity_types,
            relationship_types=relationship_types,
            domain=data.get("domain") or None,
            density=density if density in DENSITIES else None,
            focus=tuple(data.get("focus") or ()),
            ignore=tuple(data.get("ignore") or ()),
        )

    def as_dict(self) -> dict:
        """Round-trip JSON. Always carries `allowed_labels`+`mode`; the typed/hint keys appear only
        when set, so a legacy labels-only ontology round-trips to the legacy shape unchanged."""
        out: dict = {"allowed_labels": list(self.allowed_labels), "mode": self.mode}
        if self.entity_types:
            out["entity_types"] = [e.as_dict() for e in self.entity_types]
        if self.relationship_types:
            out["relationship_types"] = [r.as_dict() for r in self.relationship_types]
        if self.domain:
            out["domain"] = self.domain
        if self.density:
            out["density"] = self.density
        if self.focus:
            out["focus"] = list(self.focus)
        if self.ignore:
            out["ignore"] = list(self.ignore)
        return out

    @property
    def has_hints(self) -> bool:
        """True when any soft free-text steering is configured (drives `to_prompt_prefix`)."""
        return bool(
            self.entity_types
            or self.relationship_types
            or self.domain
            or self.density
            or self.focus
            or self.ignore
        )


def resolve_label(ontology: Ontology | None, label: str) -> tuple[str | None, bool]:
    """Return (resolved_label_or_None, was_coerced). None => the node is rejected."""
    if ontology is None or ontology.mode == "open" or not ontology.allowed_labels:
        return label, False
    if label in ontology.allowed_labels:
        return label, False
    if ontology.mode == "coerce":
        best = max(
            ontology.allowed_labels,
            key=lambda a: SequenceMatcher(None, label.lower(), a.lower()).ratio(),
        )
        ratio = SequenceMatcher(None, label.lower(), best.lower()).ratio()
        return (best, True) if ratio >= _COERCE_CUTOFF else (None, False)
    return None, False  # strict
