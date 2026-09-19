"""#1163 — migration ``0033`` and the ``EngineTeamRun`` columns it adds.

A team run records which team draft (and which version of it) it was started from, so the
console can offer "run again" against a specific draft snapshot and list a draft's succeeded
versions (R1/R2/R17). Two nullable columns, a pair CHECK (one without the other is impossible at
the database level) and a partial index that serves the succeeded-versions read.

Pure file-scan of the migration (no alembic runtime / DB, mirroring
``test_migrations_single_head.py``) plus a read of the mapped ``EngineTeamRun.__table__`` — no DB
needed for either, so this stays a fast unit guardrail. RED until migration ``0033`` exists and the
model carries the columns/constraint/index (I1).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_VERSIONS = pathlib.Path(__file__).parents[2] / "migrations" / "versions"
_MIGRATION = _VERSIONS / "0033_team_run_team_draft.py"
_REV = re.compile(r'^revision\s*(?::[^=]*)?=\s*["\']([^"\']+)["\']', re.M)
_DOWN = re.compile(r'^down_revision\s*(?::[^=]*)?=\s*["\']([^"\']+)["\']', re.M)


def test_migration_file_exists_with_the_right_revision_chain() -> None:
    assert _MIGRATION.exists(), f"expected {_MIGRATION} to exist"
    text = _MIGRATION.read_text()

    rev_match = _REV.search(text)
    assert rev_match is not None, "no `revision = ...` assignment found"
    revision = rev_match.group(1)
    assert revision == "0033_team_run_team_draft"
    assert len(revision) <= 32, "alembic's version_num column caps revision ids at 32 characters"

    down_match = _DOWN.search(text)
    assert down_match is not None, "no `down_revision = ...` assignment found"
    assert down_match.group(1) == "0032_team_run_skip_reasons"


def test_migration_file_mentions_the_new_names() -> None:
    text = _MIGRATION.read_text()
    for token in (
        "team_draft_id",
        "team_draft_version",
        "ix_engine_team_runs_org_draft_succeeded",
        "ck_engine_team_runs_team_draft_pair",
        "SUCCEEDED",
    ):
        assert token in text, f"migration text is missing {token!r}"


def test_model_has_the_team_draft_id_column() -> None:
    from oraclous_execution_engine_service.models.team_run import EngineTeamRun

    col = EngineTeamRun.__table__.columns["team_draft_id"]
    assert col.nullable is True
    assert not col.foreign_keys
    # UUID columns render through sqlalchemy's dialect-generic UUID type name.
    assert col.type.__class__.__name__.upper() == "UUID"


def test_model_has_the_team_draft_version_column() -> None:
    from oraclous_execution_engine_service.models.team_run import EngineTeamRun

    col = EngineTeamRun.__table__.columns["team_draft_version"]
    assert col.nullable is True
    assert col.type.python_type is int


def test_model_has_the_succeeded_versions_partial_index() -> None:
    from oraclous_execution_engine_service.models.team_run import EngineTeamRun

    indexes = {ix.name: ix for ix in EngineTeamRun.__table__.indexes}
    ix = indexes.get("ix_engine_team_runs_org_draft_succeeded")
    assert ix is not None, "expected an index named ix_engine_team_runs_org_draft_succeeded"

    columns = [c.name for c in ix.columns]
    assert columns == ["organisation_id", "team_draft_id", "team_draft_version", "updated_at"]

    where_clause = ix.dialect_options["postgresql"]["where"]
    assert where_clause is not None
    assert "SUCCEEDED" in str(where_clause)


def test_model_has_the_team_draft_pair_check_constraint() -> None:
    from oraclous_execution_engine_service.models.team_run import EngineTeamRun

    names = {
        c.name for c in EngineTeamRun.__table__.constraints if getattr(c, "name", None) is not None
    }
    assert "ck_engine_team_runs_team_draft_pair" in names
