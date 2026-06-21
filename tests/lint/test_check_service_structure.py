"""Tests for the canonical service-architecture guardrail (STR001-006, R3.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.lint.check_service_structure import check_package, main

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


def test_str004_allows_driver_in_core_connection_layer(tmp_path: Path) -> None:
    # §21 rule 3: the whole core/ connection layer may open connections, not just lifespan.py.
    pkg = _make_service(tmp_path)
    (pkg / "core" / "database.py").write_text("import sqlalchemy\n")
    (pkg / "core" / "dependencies.py").write_text(
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
    )
    assert "STR004" not in _codes(pkg)


def test_str004_allows_orm_import_in_models_layer(tmp_path: Path) -> None:
    # a dedicated models/ layer may import the ORM for declarations (not driver/connection access).
    pkg = _make_service(tmp_path)
    (pkg / "models").mkdir(exist_ok=True)
    (pkg / "models" / "user_model.py").write_text(
        "from sqlalchemy.orm import Mapped, mapped_column\n"
    )
    assert "STR004" not in _codes(pkg)


def test_str004_allows_external_driver_in_connectors_layer(tmp_path: Path) -> None:
    # a connectors/ layer holds tool executors that speak a DB protocol to an EXTERNAL data source;
    # that outbound driver use is the tool's payload, not the service's own persistence.
    pkg = _make_service(tmp_path)
    (pkg / "domain" / "connectors").mkdir(parents=True, exist_ok=True)
    (pkg / "domain" / "connectors" / "postgresql.py").write_text("import asyncpg\n")
    assert "STR004" not in _codes(pkg)


def test_str005_scattered_service_module_at_package_root(tmp_path: Path) -> None:
    pkg = _make_service(tmp_path)
    (pkg / "federation_service.py").write_text("X = 1\n")
    assert "STR005" in _codes(pkg)


# --- documented structure exceptions / STR006 (#301, #302) ----------------------------------------


def test_documented_exception_for_absent_optional_layer_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A read-only service legitimately has no models/; recording it in structure_exceptions
    # surfaces the absence as ACCEPTED and keeps the gate green (exit 0).
    _make_service(tmp_path)  # builds services/svc/... with no models/ dir
    status = tmp_path / "status.yaml"
    status.write_text(
        "services:\n"
        "  svc:\n"
        "    structure_exceptions:\n"
        "      - layer: models\n"
        "        reason: read-only; no relational schema\n"
    )
    rc = main([str(tmp_path / "services"), "--status", str(status)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no 'models/' — accepted" in out


def test_stale_exception_for_present_layer_fails_str006(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If the exception names a layer that now EXISTS, the deviation was resolved and the note is
    # stale — re-flag it as STR006 (exit 1) so the recorded exception can't go silently wrong.
    pkg = _make_service(tmp_path)
    (pkg / "models").mkdir()
    status = tmp_path / "status.yaml"
    status.write_text(
        "services:\n  svc:\n    structure_exceptions:\n      - layer: models\n        reason: x\n"
    )
    rc = main([str(tmp_path / "services"), "--status", str(status)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "STR006" in err
