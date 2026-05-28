"""The substrate schema package passes the 0b organisation-scoping gate (ORA-16 / A1, AC#1).

RED until `backend-implementer` adds `oraclous_substrate.schema.postgres`.

AC#1 requires the static-analysis pass (the 0b rule, `tools.lint.check_org_scoping`)
to report no substrate storage declaration without ``organisation_id``. This test
pins two things:

  1. the substrate's Postgres schema module exists (so the gate has something to
     check — without it the gate would be vacuously clean), and
  2. running the 0b rule over the whole substrate package source reports zero
     violations.

NB (flagged for the architect at Tests Review): the 0b rule today detects
SQLAlchemy ``__tablename__`` models (Postgres tables) and request-body reads.
AC#1 also names Neo4j labels, Redis key prefixes, and index DDL. Extending the
0b AST rule to those is an open interpretation — the *authoritative* org-scoping
proof for Neo4j/Redis lives in the real-substrate ``organization_isolation``
tests, not here.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from tools.lint.check_org_scoping import check_paths

pytestmark = pytest.mark.unit

_SUBSTRATE_SRC = (
    Path(__file__).resolve().parents[2] / "packages" / "substrate" / "src" / "oraclous_substrate"
)


def test_substrate_declares_a_postgres_schema_module() -> None:
    """The 0b gate is only meaningful once the substrate actually declares tables."""
    assert importlib.import_module("oraclous_substrate.schema.postgres") is not None


def test_substrate_package_has_no_org_scoping_violations() -> None:
    """The 0b rule reports zero ORG001/ORG002 violations across the substrate source.

    Gated on the schema module existing so the check is not vacuously clean
    against an empty package (this keeps the test RED until implementation).
    """
    importlib.import_module("oraclous_substrate.schema.postgres")
    violations = check_paths([str(_SUBSTRATE_SRC)])
    assert violations == [], "\n".join(str(v) for v in violations)
