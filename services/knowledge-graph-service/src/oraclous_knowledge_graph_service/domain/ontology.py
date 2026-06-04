"""Graph ontology (ORAA-4 §21 domain layer — pure, no I/O).

An ontology constrains which entity labels a graph admits, with a mode:
  - open   : no constraint (default).
  - strict : a node whose label is not allowed is rejected (skipped, counted as a violation).
  - coerce : an unknown label is mapped to the nearest allowed label (difflib ratio >= cutoff,
             ported from legacy `ontology_tasks`); below the cutoff it is rejected.
Enforced INLINE in the recipe engine at projection time (better than the legacy retroactive sweep):
the resolved label is what gets written, so a strict graph never gains an off-ontology node.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

_COERCE_CUTOFF = 0.7
MODES = ("open", "strict", "coerce")


@dataclass(frozen=True)
class Ontology:
    allowed_labels: tuple[str, ...]
    mode: str

    @classmethod
    def of(cls, data: dict | None) -> Ontology | None:
        if not data:
            return None
        return cls(
            allowed_labels=tuple(data.get("allowed_labels", [])),
            mode=data.get("mode", "open"),
        )

    def as_dict(self) -> dict:
        return {"allowed_labels": list(self.allowed_labels), "mode": self.mode}


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
