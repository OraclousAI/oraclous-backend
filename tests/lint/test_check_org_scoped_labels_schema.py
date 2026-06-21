"""Validator for the canonical org-scoped-labels YAML.

RED until ``backend-implementer`` adds:
  * ``packages/substrate/src/oraclous_substrate/schema/org_scoped_labels.yaml``
    (the single source of truth for the substrate + lint),
  * ``tools/lint/check_org_scoped_labels_schema.py`` exposing
    ``validate_yaml(text: str) -> list[str]`` (returns one error string per
    rejected condition; empty list = the YAML is well-formed).

The validator is the CI gatekeeper for the v2 single-source-of-truth file. The
v1 mirror constant in ``tools/lint/check_org_scoping.py`` is being replaced
because the *substrate* loads the YAML at module-import time and the *linter*
loads it at lint time — both derive from the same file, so structural drift
is impossible. The validator pins the YAML's shape so neither side can be
silently poisoned by a malformed update.

Each failure-mode test names the *specific* error the validator must report so
the human author of a broken YAML sees what is wrong, not just that something
is. Generic ``invalid YAML`` errors are explicitly insufficient.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_YAML = (
    _REPO_ROOT
    / "packages"
    / "substrate"
    / "src"
    / "oraclous_substrate"
    / "schema"
    / "org_scoped_labels.yaml"
)


def _validate(text: str) -> list[str]:
    """Call the validator under test.

    Importing inside the function keeps collection green when the module does
    not yet exist (the test then fails with a clear ``ModuleNotFoundError``
    inside the body rather than at collection time).
    """
    from tools.lint.check_org_scoped_labels_schema import validate_yaml

    return validate_yaml(text)


# --- happy path ----------------------------------------------------------------


def test_canonical_yaml_exists_at_expected_path() -> None:
    """The single source of truth must live at the canonical path or the design fails."""
    assert _CANONICAL_YAML.exists(), (
        f"canonical YAML missing at {_CANONICAL_YAML.relative_to(_REPO_ROOT)}"
    )


def test_canonical_yaml_validates() -> None:
    errors = _validate(_CANONICAL_YAML.read_text(encoding="utf-8"))
    assert errors == [], "\n".join(errors)


def test_canonical_yaml_contains_currently_org_scoped_labels() -> None:
    """Behavioural-preservation guard: the v2 swap is mechanism-only, not semantics.

    Before the YAML swap the substrate's ``ORG_SCOPED_LABELS`` was
    ``("__Entity__", "__Community__", "__Contradiction__", "Chunk")`` (see
    ``packages/substrate/.../schema/neo4j.py`` on origin/main). The YAML
    must carry exactly that set in that order or the substrate's ``apply()``
    silently changes what it indexes.
    """
    import yaml

    spec = yaml.safe_load(_CANONICAL_YAML.read_text(encoding="utf-8"))
    assert spec["labels"] == [
        "__Entity__",
        "__Community__",
        "__Contradiction__",
        "Chunk",
    ]


# --- failure modes: each must produce a *specific* error -----------------------


def test_missing_schema_version_is_rejected() -> None:
    text = 'labels:\n  - "Chunk"\nrelationship_types: []\n'
    errors = _validate(text)
    assert errors, "expected an error for missing schema_version"
    assert any("schema_version" in e for e in errors), errors


def test_missing_labels_is_rejected() -> None:
    text = "schema_version: 1\nrelationship_types: []\n"
    errors = _validate(text)
    assert errors and any("labels" in e for e in errors), errors


def test_non_list_labels_is_rejected() -> None:
    text = 'schema_version: 1\nlabels: "Chunk"\nrelationship_types: []\n'
    errors = _validate(text)
    assert errors, "expected an error for non-list labels"
    assert any("labels" in e and "list" in e.lower() for e in errors), errors


def test_non_string_label_entry_is_rejected() -> None:
    text = "schema_version: 1\nlabels:\n  - 42\nrelationship_types: []\n"
    errors = _validate(text)
    assert errors and any("string" in e.lower() for e in errors), errors


def test_duplicate_label_entry_is_rejected() -> None:
    text = 'schema_version: 1\nlabels:\n  - "Chunk"\n  - "Chunk"\nrelationship_types: []\n'
    errors = _validate(text)
    assert errors and any("duplicate" in e.lower() for e in errors), errors


def test_label_starting_with_digit_is_rejected() -> None:
    # Neo4j tokens: [A-Za-z_][A-Za-z0-9_]* — a leading digit is invalid syntax.
    text = 'schema_version: 1\nlabels:\n  - "1Bad"\nrelationship_types: []\n'
    errors = _validate(text)
    assert errors, "expected an error for an invalid Neo4j token"
    assert any(
        "token" in e.lower() or "neo4j" in e.lower() or "syntax" in e.lower() for e in errors
    ), errors


def test_label_with_hyphen_is_rejected() -> None:
    text = 'schema_version: 1\nlabels:\n  - "has-dash"\nrelationship_types: []\n'
    errors = _validate(text)
    assert errors, "expected an error for an invalid Neo4j token"
    assert any(
        "token" in e.lower() or "neo4j" in e.lower() or "syntax" in e.lower() for e in errors
    ), errors


def test_empty_string_label_is_rejected() -> None:
    text = 'schema_version: 1\nlabels:\n  - ""\nrelationship_types: []\n'
    errors = _validate(text)
    assert errors and any(
        "token" in e.lower() or "empty" in e.lower() or "syntax" in e.lower() for e in errors
    ), errors


# --- relationship_types follows the same shape rules --------------------------


def test_non_list_relationship_types_is_rejected() -> None:
    text = 'schema_version: 1\nlabels:\n  - "Chunk"\nrelationship_types: "IN_COMMUNITY"\n'
    errors = _validate(text)
    assert errors and any("relationship_types" in e and "list" in e.lower() for e in errors), errors


def test_invalid_relationship_type_token_is_rejected() -> None:
    text = 'schema_version: 1\nlabels:\n  - "Chunk"\nrelationship_types:\n  - "1BAD_REL"\n'
    errors = _validate(text)
    assert errors and any(
        "token" in e.lower() or "neo4j" in e.lower() or "syntax" in e.lower() for e in errors
    ), errors


def test_duplicate_relationship_type_is_rejected() -> None:
    text = (
        'schema_version: 1\nlabels:\n  - "Chunk"\n'
        'relationship_types:\n  - "IN_COMMUNITY"\n  - "IN_COMMUNITY"\n'
    )
    errors = _validate(text)
    assert errors and any("duplicate" in e.lower() for e in errors), errors
