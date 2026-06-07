"""Unit: the openapi-diff-gate flags a broken stable contract; passes additive change."""

from __future__ import annotations

import pytest
from tools.openapi.diff_gate import run, stable_operations

pytestmark = pytest.mark.unit


def _spec(paths: dict) -> dict:
    return {"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "paths": paths}


def test_stable_operations_defaults_to_stable_and_honours_provisional() -> None:
    spec = _spec(
        {
            "/a": {"get": {"x-stability": "stable"}},
            "/b": {"post": {}},  # default = stable
            "/c": {"get": {"x-stability": "provisional"}},
        }
    )
    assert stable_operations(spec) == {("get", "/a"), ("post", "/b")}


def test_removing_a_stable_operation_is_breaking(tmp_path) -> None:
    base = tmp_path / "base.yaml"
    rev = tmp_path / "rev.yaml"
    import yaml

    base.write_text(yaml.safe_dump(_spec({"/a": {"get": {"x-stability": "stable"}}})))
    rev.write_text(yaml.safe_dump(_spec({})))  # /a get removed
    problems = run(rev, base)
    assert any("BREAKING" in p and "GET /a" in p for p in problems), problems


def test_demoting_stable_to_provisional_is_breaking(tmp_path) -> None:
    import yaml

    base = tmp_path / "base.yaml"
    rev = tmp_path / "rev.yaml"
    base.write_text(yaml.safe_dump(_spec({"/a": {"get": {"x-stability": "stable"}}})))
    rev.write_text(yaml.safe_dump(_spec({"/a": {"get": {"x-stability": "provisional"}}})))
    assert any("BREAKING" in p for p in run(rev, base))


def test_adding_an_operation_is_not_breaking(tmp_path) -> None:
    import yaml

    base = tmp_path / "base.yaml"
    rev = tmp_path / "rev.yaml"
    base.write_text(yaml.safe_dump(_spec({"/a": {"get": {}}})))
    rev.write_text(yaml.safe_dump(_spec({"/a": {"get": {}}, "/b": {"post": {}}})))
    assert run(rev, base) == []


def test_no_base_validates_only(tmp_path) -> None:
    import yaml

    rev = tmp_path / "rev.yaml"
    rev.write_text(yaml.safe_dump(_spec({"/a": {"get": {}}})))
    assert run(rev, None) == []


def test_empty_base_file_is_treated_as_no_base(tmp_path) -> None:
    # CI writes an EMPTY base file on first publication; the gate must treat it as no-base, not
    # crash on yaml.safe_load("") -> None. This is exactly the first-publication CI invocation.
    import yaml

    rev = tmp_path / "rev.yaml"
    empty_base = tmp_path / "empty.yaml"
    rev.write_text(yaml.safe_dump(_spec({"/a": {"get": {"x-stability": "stable"}}})))
    empty_base.write_text("")
    assert run(rev, empty_base) == []
