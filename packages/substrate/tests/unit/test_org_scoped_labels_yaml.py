"""ORG_SCOPED_LABELS derives from the canonical YAML at substrate import (ORA-51).

RED until ``backend-implementer`` makes
``packages/substrate/src/oraclous_substrate/schema/neo4j.py`` derive its
``ORG_SCOPED_LABELS`` / ``ORG_SCOPED_RELATIONSHIP_TYPES`` constants from
``packages/substrate/src/oraclous_substrate/schema/org_scoped_labels.yaml`` at
module-import time, and adds a ``load(path)`` loader so the derivation can be
exercised against a fixture.

This is the v2 single-source-of-truth swap: the v1 mirror constant in
``tools/lint/check_org_scoping.py`` is replaced by both sides (substrate +
lint) deriving from the same YAML file. The substrate side is pinned here;
the lint side is pinned in ``tests/lint/test_check_org_scoping.py``.

The pre-ORA-51 behaviour the substrate must preserve (see ``neo4j.py`` on
origin/main):

  ORG_SCOPED_LABELS = ("__Entity__", "__Community__", "__Contradiction__", "Chunk")
  ORG_SCOPED_RELATIONSHIP_TYPES = ("IN_COMMUNITY",)

The YAML must carry exactly these so ``apply()`` continues to index the same
labels and relationship types. The brief's example YAML (see ORA-51
description) shows ``relationship_types: []``, which — if taken literally —
would drop ``IN_COMMUNITY`` from the org-scoped relationship-index loop.
That contradicts the brief's own "No application behaviour change at runtime"
clause. ``test_yaml_preserves_in_community_relationship_type`` below pins the
preservation; see the [tests] PR description for the flag to product-planner.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


_YAML_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "oraclous_substrate"
    / "schema"
    / "org_scoped_labels.yaml"
)


def _yaml_spec() -> dict[str, object]:
    return yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8"))


# --- canonical YAML exists + carries the preserved set ------------------------


def test_canonical_yaml_exists() -> None:
    assert _YAML_PATH.exists(), f"canonical YAML missing at {_YAML_PATH}"


def test_yaml_preserves_currently_org_scoped_labels() -> None:
    """Behavioural preservation: same labels, same order, as v1."""
    spec = _yaml_spec()
    assert list(spec["labels"]) == [
        "__Entity__",
        "__Community__",
        "__Contradiction__",
        "Chunk",
    ]


def test_yaml_preserves_in_community_relationship_type() -> None:
    """Behavioural preservation: ``IN_COMMUNITY`` must remain org-scoped.

    On origin/main, ``apply()`` iterates ``ORG_SCOPED_RELATIONSHIP_TYPES =
    ("IN_COMMUNITY",)`` to create the org-scoped relationship index. Dropping
    it from the YAML changes runtime behaviour. (Brief inconsistency flagged
    in the [tests] PR description.)
    """
    spec = _yaml_spec()
    rels = list(spec.get("relationship_types") or [])
    assert "IN_COMMUNITY" in rels, (
        "IN_COMMUNITY must remain in the YAML or apply() stops creating its "
        "org-scoped relationship index — silent behavioural regression"
    )


# --- substrate module-level constants are derived from the YAML ---------------


def test_org_scoped_labels_constant_equals_yaml_labels() -> None:
    spec = _yaml_spec()
    neo4j = importlib.import_module("oraclous_substrate.schema.neo4j")
    assert neo4j.ORG_SCOPED_LABELS == tuple(spec["labels"])


def test_org_scoped_relationship_types_constant_equals_yaml() -> None:
    spec = _yaml_spec()
    neo4j = importlib.import_module("oraclous_substrate.schema.neo4j")
    assert neo4j.ORG_SCOPED_RELATIONSHIP_TYPES == tuple(spec.get("relationship_types") or ())


def test_constants_are_typed_string_tuples() -> None:
    neo4j = importlib.import_module("oraclous_substrate.schema.neo4j")
    assert isinstance(neo4j.ORG_SCOPED_LABELS, tuple)
    assert all(isinstance(x, str) for x in neo4j.ORG_SCOPED_LABELS)
    assert isinstance(neo4j.ORG_SCOPED_RELATIONSHIP_TYPES, tuple)
    assert all(isinstance(x, str) for x in neo4j.ORG_SCOPED_RELATIONSHIP_TYPES)


# --- loader derives from arbitrary YAML (proves no hard-coding) ---------------


def test_loader_derives_labels_from_arbitrary_yaml(tmp_path: Path) -> None:
    """Verifies derivation, not coincidence.

    A fixture YAML with a different label list produces a spec matching that
    list when fed to the loader. If the substrate were hard-coding the
    canonical values, this would fail. The loader exposes the substrate's
    YAML-derivation seam so this property is testable.
    """
    loader_mod = importlib.import_module("oraclous_substrate.schema.org_scoped_labels")
    fixture = tmp_path / "org_scoped_labels.yaml"
    fixture.write_text(
        'schema_version: 1\nlabels:\n  - "Alpha"\n  - "Beta"\nrelationship_types:\n  - "REL_X"\n',
        encoding="utf-8",
    )
    spec = loader_mod.load(fixture)
    assert tuple(spec.labels) == ("Alpha", "Beta")
    assert tuple(spec.relationship_types) == ("REL_X",)


def test_loader_canonical_path_matches_yaml(tmp_path: Path) -> None:
    """The loader's canonical-path read returns the same content as a direct
    ``yaml.safe_load`` of the file. Pins that the loader does not transform
    the YAML on the way through (no implicit reordering, no implicit
    additions / removals).
    """
    loader_mod = importlib.import_module("oraclous_substrate.schema.org_scoped_labels")
    spec_loader = loader_mod.load(_YAML_PATH)
    spec_direct = _yaml_spec()
    assert tuple(spec_loader.labels) == tuple(spec_direct["labels"])
    assert tuple(spec_loader.relationship_types) == tuple(
        spec_direct.get("relationship_types") or ()
    )
