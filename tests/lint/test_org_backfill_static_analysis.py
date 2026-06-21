"""The organisation backfill migration passes the 0b org-scoping gate (D1, AC#2).

RED until ``backend-implementer`` adds ``oraclous_substrate.migrations.org_backfill``.

AC#2 requires the 0b static-analysis (``tools.lint.check_org_scoping``) to pass
*post-migration*: the migration source must not introduce a storage declaration
without ``organisation_id`` (ORG002) or read ``organisation_id`` from an untrusted
request body (ORG001). This pins two things:

  1. the migration module exists (so the gate is not vacuously clean against an
     empty package — keeps the test RED until implementation), and
  2. running the 0b rule over the migration source reports zero violations.

NB (as on the A1 schema-clean test): the 0b AST rule covers SQLAlchemy
``__tablename__`` models and request-body reads. The authoritative org-scoping
proof for the Neo4j/Redis/Postgres *data* lives in the real-substrate
``organization_isolation`` backfill tests, not here.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from tools.lint.check_org_scoping import check_paths

pytestmark = [pytest.mark.unit, pytest.mark.security]

_MIGRATIONS_SRC = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "substrate"
    / "src"
    / "oraclous_substrate"
    / "migrations"
)


def test_migration_module_exists() -> None:
    """The 0b gate is only meaningful once the migration actually exists."""
    assert importlib.import_module("oraclous_substrate.migrations.org_backfill") is not None


def test_migration_source_has_no_org_scoping_violations() -> None:
    importlib.import_module("oraclous_substrate.migrations.org_backfill")
    violations = check_paths([str(_MIGRATIONS_SRC)])
    assert violations == [], "\n".join(str(v) for v in violations)
