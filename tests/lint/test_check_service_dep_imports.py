"""Tests for the service first-party-dependency guardrail (DEP001, the #375 class).

A service that imports a first-party workspace package must declare it in BOTH
``[project.dependencies]`` and ``[tool.uv.sources] (workspace = true)``; otherwise the built
image ModuleNotFounds it even though CI's workspace venv stays green.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from tools.lint.check_service_dep_imports import check, first_party_index

pytestmark = pytest.mark.unit


def _make_packages(repo_root: Path, *names: str) -> None:
    """Lay down ``packages/<name>/`` distributions; dist name is ``oraclous-<name>`` with the
    importable package ``oraclous_<name>`` under ``src/``."""
    for name in names:
        dist = f"oraclous-{name}"
        imp = f"oraclous_{name}"
        pkg = repo_root / "packages" / name
        (pkg / "src" / imp).mkdir(parents=True)
        (pkg / "src" / imp / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "pyproject.toml").write_text(
            f'[project]\nname = "{dist}"\nversion = "0.0.0"\n', encoding="utf-8"
        )


def _make_service(
    repo_root: Path,
    service: str,
    *,
    src_module: str,
    dependencies: list[str],
    sources: dict[str, bool],
) -> Path:
    """Lay down ``services/<service>/src/oraclous_<service>_service`` + a pyproject."""
    own = f"oraclous_{service.replace('-', '_')}"
    pkg = repo_root / "services" / service / "src" / own
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text(textwrap.dedent(src_module), encoding="utf-8")
    deps = "".join(f'    "{d}",\n' for d in dependencies)
    src_lines = "".join(
        f"{dist} = {{ workspace = {str(ws).lower()} }}\n" for dist, ws in sources.items()
    )
    (repo_root / "services" / service / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""\
            [project]
            name = "oraclous-{service}"
            version = "0.0.0"
            dependencies = [
            {deps}]

            [tool.uv.sources]
            {src_lines}"""
        ),
        encoding="utf-8",
    )
    return repo_root / "services" / service


def _codes(repo_root: Path, service_dir: Path) -> list[str]:
    return [v.code for v in check(repo_root, [service_dir])]


def test_first_party_index_is_derived_not_hardcoded(tmp_path: Path) -> None:
    _make_packages(tmp_path, "substrate", "telemetry")
    index = first_party_index(tmp_path)
    assert index == {
        "oraclous_substrate": "oraclous-substrate",
        "oraclous_telemetry": "oraclous-telemetry",
    }


def test_declared_in_both_passes(tmp_path: Path) -> None:
    _make_packages(tmp_path, "substrate")
    svc = _make_service(
        tmp_path,
        "gw",
        src_module="from oraclous_substrate.access_async import build_rls_engine\n",
        dependencies=["oraclous-substrate"],
        sources={"oraclous-substrate": True},
    )
    assert _codes(tmp_path, svc) == []


def test_imported_but_undeclared_fires_dep001(tmp_path: Path) -> None:
    # The #375 shape: imported in src, declared in NEITHER half.
    _make_packages(tmp_path, "substrate")
    svc = _make_service(
        tmp_path,
        "gw",
        src_module="from oraclous_substrate import access_async\n",
        dependencies=[],
        sources={},
    )
    assert _codes(tmp_path, svc) == ["DEP001"]


def test_in_deps_but_missing_uv_source_fires(tmp_path: Path) -> None:
    _make_packages(tmp_path, "substrate")
    svc = _make_service(
        tmp_path,
        "gw",
        src_module="import oraclous_substrate\n",
        dependencies=["oraclous-substrate"],
        sources={},
    )
    assert _codes(tmp_path, svc) == ["DEP001"]


def test_uv_source_present_but_not_workspace_true_fires(tmp_path: Path) -> None:
    _make_packages(tmp_path, "substrate")
    svc = _make_service(
        tmp_path,
        "gw",
        src_module="import oraclous_substrate\n",
        dependencies=["oraclous-substrate"],
        sources={"oraclous-substrate": False},
    )
    assert _codes(tmp_path, svc) == ["DEP001"]


def test_own_package_import_is_not_a_dependency(tmp_path: Path) -> None:
    # A service importing its OWN src package must not be flagged.
    _make_packages(tmp_path, "substrate")
    pkg = tmp_path / "services" / "gw" / "src" / "oraclous_gw"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text("from oraclous_gw import other\n", encoding="utf-8")
    (tmp_path / "services" / "gw" / "pyproject.toml").write_text(
        '[project]\nname = "oraclous-gw"\nversion = "0.0.0"\ndependencies = []\n', encoding="utf-8"
    )
    # oraclous_gw is not a packages/* dist, so it is not in the first-party index → not flagged.
    assert _codes(tmp_path, tmp_path / "services" / "gw") == []


def test_comment_or_string_mention_does_not_fire(tmp_path: Path) -> None:
    # AST-based: a package named only in a comment/docstring is not an import (the capreg
    # `oraclous_errors`-in-a-comment false-positive guard).
    _make_packages(tmp_path, "errors")
    svc = _make_service(
        tmp_path,
        "gw",
        src_module='"""Normalise to the oraclous_errors envelope."""\nX = 1  # oraclous_errors\n',
        dependencies=[],
        sources={},
    )
    assert _codes(tmp_path, svc) == []


def test_third_party_import_ignored(tmp_path: Path) -> None:
    _make_packages(tmp_path, "substrate")
    svc = _make_service(
        tmp_path,
        "gw",
        src_module="import fastapi\nfrom sqlalchemy import text\n",
        dependencies=[],
        sources={},
    )
    assert _codes(tmp_path, svc) == []
