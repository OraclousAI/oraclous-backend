"""Tests for the no-hollow guardrail (HOL001-005, ORAA-4 §22, R3.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.lint.check_no_stubs import check_source

pytestmark = pytest.mark.unit


def _codes(src: str) -> set[str]:
    return {v.code for v in check_source(Path("services/x/src/oraclous_x_service/m.py"), src)}


def test_hol001_not_implemented_error_fires() -> None:
    assert "HOL001" in _codes("def f():\n    raise NotImplementedError('later')\n")


def test_hol002_stub_marker_fires() -> None:
    assert "HOL002" in _codes("def f():\n    return _stub_result('x')\n")


def test_hol003_deferral_marker_fires() -> None:
    assert "HOL003" in _codes("def f():\n    pass  # TODO: implement this\n")
    assert "HOL003" in _codes("# deferred to R5\nX = 1\n")


def test_hol004_stub_body_fires_for_return_none_and_pass() -> None:
    assert "HOL004" in _codes("def get_graph(gid):\n    return None\n")
    assert "HOL004" in _codes("def delete_graph(gid):\n    return False\n")
    assert "HOL004" in _codes("def noop():\n    pass\n")


def test_hol004_excludes_abstractmethod_and_protocol() -> None:
    abstract = (
        "from abc import abstractmethod\nclass P:\n    @abstractmethod\n    def f(self): ...\n"
    )
    assert "HOL004" not in _codes(abstract)
    protocol = (
        "from typing import Protocol\n"
        "class Repo(Protocol):\n"
        "    def get(self, x): ...\n"
        "    def put(self, x): ...\n"
    )
    assert "HOL004" not in _codes(protocol)


def test_hol005_route_returns_501() -> None:
    assert "HOL005" in _codes("def handler():\n    return JSONResponse(status_code=501)\n")


def test_real_function_is_clean() -> None:
    real = (
        "def create_graph(name, user_id):\n"
        "    row = self.repo.insert(name=name, owner=user_id)\n"
        "    return {'id': row.id, 'name': row.name}\n"
    )
    assert _codes(real) == set()
