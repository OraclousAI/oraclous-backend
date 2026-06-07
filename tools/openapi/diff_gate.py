"""openapi-diff-gate (ADR-015 §3) — block a PR that breaks the published stable contract.

Validates ``openapi/v1.yaml`` as an OpenAPI 3.x document and, when a base revision is supplied,
fails on a BREAKING change to any ``x-stability: stable`` operation: a stable operation
**removed** or **demoted** from stable. Additive operations and any change to a ``provisional`` are
allowed (ADR-015 §2). This is the oasdiff-equivalent for the removal/demotion breaking-change class
that matters at R6 open; richer field-level breaking detection is a later enhancement.

Usage::

    python -m tools.openapi.diff_gate REVISION.yaml [BASE.yaml]

Exit 0 = ok, 1 = breaking change / invalid spec, 2 = bad invocation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def stable_operations(spec: dict) -> set[tuple[str, str]]:
    """The ``(method, path)`` set of stable operations (stable is the default when absent)."""
    ops: set[tuple[str, str]] = set()
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            if operation.get("x-stability", "stable") == "stable":
                ops.add((method.lower(), path))
    return ops


def _validate(spec: dict, where: str) -> list[str]:
    problems: list[str] = []
    if not str(spec.get("openapi", "")).startswith("3."):
        problems.append(f"{where}: not an OpenAPI 3.x document")
        return problems
    try:
        from openapi_spec_validator import validate
    except ImportError:
        return problems  # validator optional locally; CI installs it
    try:
        validate(spec)
    except Exception as exc:  # noqa: BLE001 — surface any validation failure as a gate problem
        problems.append(f"{where}: invalid OpenAPI: {exc}")
    return problems


def run(revision_path: Path, base_path: Path | None) -> list[str]:
    revision = yaml.safe_load(revision_path.read_text(encoding="utf-8"))
    problems = _validate(revision, revision_path.name)
    if problems:
        return problems  # don't diff against an invalid revision

    base: object = None
    if base_path is not None and base_path.is_file():
        # an empty file is how CI signals "no base on this branch" (first publication) — safe_load
        # returns None for it; treat any falsy/non-dict base as no-base rather than crashing.
        base = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    if isinstance(base, dict) and base:
        broken = stable_operations(base) - stable_operations(revision)
        for method, path in sorted(broken):
            problems.append(
                f"BREAKING: stable operation removed or demoted: {method.upper()} {path}"
            )
    else:
        print("openapi-diff-gate: no base spec (first publication) — validating revision only")
    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: diff_gate.py REVISION.yaml [BASE.yaml]", file=sys.stderr)
        return 2
    revision = Path(argv[0])
    base = Path(argv[1]) if len(argv) > 1 and argv[1] else None
    problems = run(revision, base)
    if problems:
        print("openapi-diff-gate: FAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("openapi-diff-gate: ok (spec valid; no breaking change to a stable operation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
