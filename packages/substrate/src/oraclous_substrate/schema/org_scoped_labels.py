"""Loader for the canonical org-scoped-labels YAML (ORA-51).

This module is the single source of truth for which Neo4j labels and
relationship types carry the organisation_id tenancy seam. The substrate's
``schema.neo4j`` constants derive from ``load(CANONICAL_YAML_PATH)`` at
module-import time; ``tools.lint.check_org_scoping`` reads the same YAML at
lint time. No second source — drift is structurally impossible.

The YAML format is pinned by the validator in
``tools.lint.check_org_scoped_labels_schema``; this module assumes a
schema-valid file and performs minimal defensive parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

CANONICAL_YAML_PATH: Path = Path(__file__).resolve().parent / "org_scoped_labels.yaml"


@dataclass(frozen=True, slots=True)
class Spec:
    """Parsed org-scoped-labels spec — immutable, safe to publish as module constants."""

    labels: tuple[str, ...]
    relationship_types: tuple[str, ...]


def load(path: Path) -> Spec:
    """Parse the YAML at ``path`` into a Spec.

    Callers should validate the file in CI via
    ``tools.lint.check_org_scoped_labels_schema.validate_yaml``; this loader
    coerces missing optional keys to empty tuples but does not enforce shape.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Spec(
        labels=tuple(data.get("labels") or ()),
        relationship_types=tuple(data.get("relationship_types") or ()),
    )
