"""Tests for the RLS request-binding presence guardrail (RLSBIND001/002).

A realized service that builds a GUC-guarded RLS engine must reference an org-binding seam
somewhere in src; a guarded engine with zero binding (the capreg pre-fix state) is flagged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tools.lint.check_rls_request_binding import check

pytestmark = pytest.mark.unit


def _make_service(repo_root: Path, service: str, *, src_files: dict[str, str]) -> None:
    """Lay down ``services/<svc>/src/pkg`` with the given relative files + contents."""
    pkg = repo_root / "services" / service / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for rel, content in src_files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _manifest(tmp_path: Path, services: list[str]) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(
        yaml.safe_dump({"schema_version": 1, "services": {s: {"tables": []} for s in services}}),
        encoding="utf-8",
    )
    return path


def _codes(manifest: Path, repo_root: Path) -> list[str]:
    return [v.code for v in check(manifest, repo_root)]


def test_engine_with_binding_passes(tmp_path: Path) -> None:
    _make_service(
        tmp_path,
        "svc",
        src_files={
            "rls.py": "from oraclous_substrate.access_async import build_rls_engine\n",
            "repo.py": "from oraclous_substrate.access_async import org_scope\n",
        },
    )
    assert _codes(_manifest(tmp_path, ["svc"]), tmp_path) == []


def test_engine_with_zero_binding_fires_rlsbind001(tmp_path: Path) -> None:
    # The capreg pre-fix state: guarded engine, no org binding anywhere.
    _make_service(
        tmp_path,
        "svc",
        src_files={
            "rls.py": "from oraclous_substrate.access_async import build_rls_engine\n"
            "engine = build_rls_engine(dsn)\n",
            "repo.py": "async def list_all(session):\n    return await session.execute(q)\n",
        },
    )
    assert _codes(_manifest(tmp_path, ["svc"]), tmp_path) == ["RLSBIND001"]


def test_install_guc_guard_also_counts_as_engine(tmp_path: Path) -> None:
    _make_service(
        tmp_path,
        "svc",
        src_files={"rls.py": "install_org_guc_guard(engine)\n"},
    )
    assert _codes(_manifest(tmp_path, ["svc"]), tmp_path) == ["RLSBIND001"]


def test_no_guarded_engine_is_not_asserted(tmp_path: Path) -> None:
    # A service with neither engine token nor binding is silent (nothing to assert).
    _make_service(
        tmp_path,
        "svc",
        src_files={"repo.py": "async def go(session):\n    return 1\n"},
    )
    assert _codes(_manifest(tmp_path, ["svc"]), tmp_path) == []


def test_each_binding_token_satisfies(tmp_path: Path) -> None:
    for token in (
        "org_scope",
        "use_organisation_context",
        "enforced_organisation_id",
        "bind_org_context",
    ):
        root = tmp_path / token
        root.mkdir()
        _make_service(
            root,
            "svc",
            src_files={
                "rls.py": "build_rls_engine(dsn)\n",
                "bind.py": f"x = {token}\n",
            },
        )
        assert _codes(_manifest(root, ["svc"]), root) == [], token


def test_binding_only_in_tests_does_not_satisfy(tmp_path: Path) -> None:
    # A test-only binding reference must NOT satisfy the runtime requirement.
    pkg = tmp_path / "services" / "svc" / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "rls.py").write_text("build_rls_engine(dsn)\n", encoding="utf-8")
    tests_dir = pkg / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from oraclous_substrate import org_scope\n", encoding="utf-8"
    )
    assert _codes(_manifest(tmp_path, ["svc"]), tmp_path) == ["RLSBIND001"]


def test_missing_service_dir_fires_rlsbind002(tmp_path: Path) -> None:
    assert _codes(_manifest(tmp_path, ["ghost"]), tmp_path) == ["RLSBIND002"]
