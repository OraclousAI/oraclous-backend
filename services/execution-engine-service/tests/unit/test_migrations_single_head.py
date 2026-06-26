"""Guardrail: the engine's alembic chain has exactly ONE head (no divergent migration branches).

A second head makes ``alembic upgrade head`` ambiguous on deploy (it would error or leave the DB
behind the ORM). ADR-042 added migration 0012 — this keeps the chain linear as more land. Pure
file-scan of ``migrations/versions`` (no alembic runtime / DB), so it is a fast unit guardrail.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_VERSIONS = pathlib.Path(__file__).parents[2] / "migrations" / "versions"
# match both the plain (`revision = "x"`) and the type-annotated (`revision: str = "x"`) forms
_REV = re.compile(r'^revision\s*(?::[^=]*)?=\s*["\']([^"\']+)["\']', re.M)
_DOWN = re.compile(r'^down_revision\s*(?::[^=]*)?=\s*["\']([^"\']+)["\']', re.M)


def test_engine_migrations_have_a_single_head() -> None:
    revisions: set[str] = set()
    down_revisions: set[str] = set()
    for f in _VERSIONS.glob("*.py"):
        if f.name == "__init__.py":
            continue
        text = f.read_text()
        if m := _REV.search(text):
            revisions.add(m.group(1))
        down_revisions.update(_DOWN.findall(text))
    heads = revisions - down_revisions
    assert revisions, f"no migrations found under {_VERSIONS}"
    assert len(heads) == 1, f"expected a single alembic head, found {sorted(heads)}"
