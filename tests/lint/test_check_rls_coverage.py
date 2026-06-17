"""Tests for the RLS-coverage guardrail (RLS001-003 + documented exclusions, ADR-030 §4).

Focus on the Slice-1 addition: a realized service may declare ``exclusions`` (org-scoped tables
deliberately NOT RLS-d, each with a reason) so RLS002 does not fire on a table that is intentionally
read without a bound org (e.g. auth's ``org_members``, enumerated across a user's orgs at login).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from tools.lint.check_rls_coverage import check

pytestmark = pytest.mark.unit


def _make_service(repo_root: Path, service: str, *, models: str, migration: str = "") -> None:
    """Lay down a minimal services/<svc>/src/<pkg> tree + an optional migrations/ file."""
    pkg = repo_root / "services" / service / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text(textwrap.dedent(models), encoding="utf-8")
    if migration:
        migs = repo_root / "services" / service / "migrations"
        migs.mkdir(parents=True)
        (migs / "0001_rls.py").write_text(textwrap.dedent(migration), encoding="utf-8")


def _manifest(tmp_path: Path, spec: dict) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "services": spec}), encoding="utf-8")
    return path


# Two org-scoped models: one is RLS-enabled, the other we vary (excluded / not).
_MODELS = """
    class Scoped:
        __tablename__ = "scoped"
        organisation_id = 1
    class LoginEdge:
        __tablename__ = "login_edge"
        organisation_id = 1
"""
_MIGRATION = """
    _T = ("scoped",)
    def upgrade():
        for t in _T:
            enable_rls_on(conn, t)
"""


def _codes(manifest: Path, repo_root: Path) -> list[str]:
    return [v.code for v in check(manifest, repo_root)]


def test_excluded_org_scoped_table_does_not_trip_rls002(tmp_path: Path) -> None:
    _make_service(tmp_path, "svc", models=_MODELS, migration=_MIGRATION)
    manifest = _manifest(
        tmp_path,
        {"svc": {"tables": ["scoped"], "exclusions": [{"table": "login_edge", "reason": "login"}]}},
    )
    assert _codes(manifest, tmp_path) == []


def test_unexcluded_org_scoped_table_trips_rls002(tmp_path: Path) -> None:
    _make_service(tmp_path, "svc", models=_MODELS, migration=_MIGRATION)
    # login_edge is neither RLS-d nor excluded → must fail RLS002.
    manifest = _manifest(tmp_path, {"svc": {"tables": ["scoped"]}})
    assert "RLS002" in _codes(manifest, tmp_path)


def test_table_in_both_tables_and_exclusions_is_contradictory_rls003(tmp_path: Path) -> None:
    _make_service(tmp_path, "svc", models=_MODELS, migration=_MIGRATION)
    manifest = _manifest(
        tmp_path,
        {
            "svc": {
                "tables": ["scoped", "login_edge"],
                "exclusions": [{"table": "login_edge", "reason": "x"}],
            }
        },
    )
    # login_edge would also need an enable_rls_on call (RLS001) — the contradiction is the point.
    codes = _codes(manifest, tmp_path)
    assert "RLS003" in codes


def test_exclusion_for_nonexistent_model_is_stale_rls003(tmp_path: Path) -> None:
    _make_service(tmp_path, "svc", models=_MODELS, migration=_MIGRATION)
    manifest = _manifest(
        tmp_path,
        {
            "svc": {
                "tables": ["scoped"],
                "exclusions": [
                    {"table": "login_edge", "reason": "ok"},
                    {"table": "ghost", "reason": "typo — no such org-scoped model"},
                ],
            }
        },
    )
    assert "RLS003" in _codes(manifest, tmp_path)


def test_mapping_form_exclusions_also_supported(tmp_path: Path) -> None:
    """``exclusions`` accepts a {table: reason} mapping as well as a list of dicts."""
    _make_service(tmp_path, "svc", models=_MODELS, migration=_MIGRATION)
    manifest = _manifest(
        tmp_path, {"svc": {"tables": ["scoped"], "exclusions": {"login_edge": "login flow"}}}
    )
    assert _codes(manifest, tmp_path) == []
