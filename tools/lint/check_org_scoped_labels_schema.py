"""Validator for the canonical org-scoped-labels YAML.

Pins the YAML's shape so neither side (substrate import-time read or lint-time
read) can be silently poisoned by a malformed update. Each failure mode
produces a specific error string naming what is wrong; the CI step at
``.github/workflows/ci.yml`` runs this validator on the canonical file.

Run:  uv run python -m tools.lint.check_org_scoped_labels_schema [<path> ...]
With no arguments, validates the canonical
``packages/substrate/src/oraclous_substrate/schema/org_scoped_labels.yaml``.
Exits non-zero (1) on any validation error; 0 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
NEO4J_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_yaml(text: str) -> list[str]:
    """Return a list of error strings; empty list means the YAML is well-formed."""
    errors: list[str] = []

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]

    if not isinstance(data, dict):
        return ["top-level YAML must be a mapping"]

    if "schema_version" not in data:
        errors.append("missing required key 'schema_version'")
    elif data["schema_version"] != SCHEMA_VERSION:
        errors.append(
            f"unsupported schema_version {data['schema_version']!r}; expected {SCHEMA_VERSION}"
        )

    errors.extend(_validate_token_list(data, "labels", required=True))
    errors.extend(_validate_token_list(data, "relationship_types", required=False))

    return errors


def _validate_token_list(data: dict[str, Any], key: str, *, required: bool) -> list[str]:
    """Validate that ``data[key]`` is a list of unique valid Neo4j tokens."""
    errors: list[str] = []

    if key not in data:
        if required:
            errors.append(f"missing required key '{key}'")
        return errors

    value = data[key]
    if value is None:
        if required:
            errors.append(f"'{key}' must be a list, got null")
        return errors

    if not isinstance(value, list):
        errors.append(f"'{key}' must be a list, got {type(value).__name__}")
        return errors

    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            errors.append(f"'{key}' entries must be strings, got {type(entry).__name__}: {entry!r}")
            continue
        if not entry:
            errors.append(
                f"'{key}' contains an empty string; Neo4j token syntax requires a non-empty name"
            )
            continue
        if not NEO4J_TOKEN_RE.match(entry):
            errors.append(
                f"'{key}' entry {entry!r} is not a valid Neo4j token "
                "(syntax: [A-Za-z_][A-Za-z0-9_]*)"
            )
            continue
        if entry in seen:
            errors.append(f"'{key}' entry {entry!r} is a duplicate")
            continue
        seen.add(entry)

    return errors


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args:
        paths = [Path(a) for a in args]
    else:
        from oraclous_substrate.schema.org_scoped_labels import CANONICAL_YAML_PATH

        paths = [CANONICAL_YAML_PATH]

    rc = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"{path}: cannot read: {exc}")
            rc = 1
            continue
        errors = validate_yaml(text)
        for err in errors:
            print(f"{path}: {err}")
        if errors:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
