"""Tests proving the organisation-scoping guardrails fire (ORA-10 / 0b)."""

import pytest
from tools.lint.check_org_scoping import check_source

pytestmark = pytest.mark.unit


def _rules(src: str) -> set[str]:
    return {v.rule for v in check_source(src)}


def test_org001_subscript_from_body() -> None:
    src = "def handler(body):\n    org = body['organisation_id']\n    return org\n"
    assert "ORG001" in _rules(src)


def test_org001_attribute_from_payload() -> None:
    src = "def handler(payload):\n    return payload.organization_id\n"
    assert "ORG001" in _rules(src)


def test_org001_request_model_input_field() -> None:
    src = "class CreateThingRequest:\n    name: str\n    organisation_id: str\n"
    assert "ORG001" in _rules(src)


def test_org002_storage_model_without_org_is_flagged() -> None:
    src = "class Thing(Base):\n    __tablename__ = 'things'\n    id = Column(Integer)\n"
    assert "ORG002" in _rules(src)


def test_org002_storage_model_with_org_passes() -> None:
    src = (
        "class Thing(Base):\n"
        "    __tablename__ = 'things'\n"
        "    organisation_id = Column(UUID)\n"
        "    id = Column(Integer)\n"
    )
    assert "ORG002" not in _rules(src)


def test_clean_source_has_no_violations() -> None:
    assert _rules("def add(a, b):\n    return a + b\n") == set()
