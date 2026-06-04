"""Tests for the canonical service-architecture guardrail (STR001-005, ORAA-4 §21, R3.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.lint.check_service_structure import check_package

pytestmark = pytest.mark.unit


def _make_service(root: Path, *, full: bool = True) -> Path:
    """Build a minimal canonical service package under root; return the package root."""
    pkg = root / "services" / "svc" / "src" / "oraclous_svc_service"
    (pkg / "app").mkdir(parents=True)
    (pkg / "main.py").write_text("from .app.factory import create_app\napp = create_app()\n")
    (pkg / "app" / "factory.py").write_text("def create_app():\n    return object()\n")
    for d in ("routes", "services", "repositories", "schema", "core"):
        (pkg / d).mkdir()
        (pkg / d / "__init__.py").write_text("")
    return pkg


def _codes(pkg: Path) -> set[str]:
    return {v.code for v in check_package(pkg)}


def test_conformant_service_has_no_violations(tmp_path: Path) -> None:
    pkg = _make_service(tmp_path)
    (pkg / "routes" / "graph_routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter()\n"
        "class CreateGraph(BaseModel):\n    name: str\n"
        "@router.post('/graphs')\n"
        "def create(body: CreateGraph, svc=None):\n    return svc.create(body.name)\n"
    )
    (pkg / "repositories" / "graph_repository.py").write_text(
        "import neo4j\n"
        "class GraphRepository:\n    def __init__(self, driver): self.driver = driver\n"
    )
    assert _codes(pkg) == set()


def test_str001_missing_layer_dir(tmp_path: Path) -> None:
    pkg = _make_service(tmp_path)
    import shutil

    shutil.rmtree(pkg / "repositories")
    assert "STR001" in _codes(pkg)


def test_str002_non_basemodel_class_in_routes(tmp_path: Path) -> None:
    pkg = _make_service(tmp_path)
    # the GraphNodeService-inside-a-route anti-pattern
    (pkg / "routes" / "graph_routes.py").write_text(
        "class GraphNodeService:\n    def get_graph(self, gid): return None\n"
    )
    assert "STR002" in _codes(pkg)


def test_str003_db_driver_in_routes(tmp_path: Path) -> None:
    pkg = _make_service(tmp_path)
    (pkg / "routes" / "graph_routes.py").write_text("import asyncpg\nrouter = None\n")
    assert "STR003" in _codes(pkg)


def test_str004_db_driver_outside_repositories(tmp_path: Path) -> None:
    pkg = _make_service(tmp_path)
    (pkg / "services" / "graph_service.py").write_text("import neo4j\n")
    assert "STR004" in _codes(pkg)


def test_str004_allows_driver_in_lifespan(tmp_path: Path) -> None:
    pkg = _make_service(tmp_path)
    (pkg / "core" / "lifespan.py").write_text("import neo4j\n")
    assert "STR004" not in _codes(pkg)


def test_str005_scattered_service_module_at_package_root(tmp_path: Path) -> None:
    pkg = _make_service(tmp_path)
    (pkg / "federation_service.py").write_text("X = 1\n")
    assert "STR005" in _codes(pkg)
