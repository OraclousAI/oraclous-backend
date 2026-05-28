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


def test_org001_pydantic_request_model_input_field() -> None:
    # A genuine inbound Pydantic request schema declaring organisation_id is flagged.
    src = "class CreateThingRequest(BaseModel):\n    name: str\n    organisation_id: str\n"
    assert "ORG001" in _rules(src)


def test_org001_non_pydantic_request_dataclass_not_flagged() -> None:
    # A plain domain value object named *Request that carries organisation_id
    # through a seam is not an inbound body schema, so it is not flagged (ORA-40).
    src = (
        "@dataclass(frozen=True, slots=True)\n"
        "class AccessRequest:\n"
        "    organisation_id: str\n"
        "    subject: str\n"
    )
    assert "ORG001" not in _rules(src)


def test_org001_attribute_from_body_still_flagged() -> None:
    src = "def handler(body):\n    return body.organisation_id\n"
    assert "ORG001" in _rules(src)


def test_org001_attribute_from_request_domain_object_not_flagged() -> None:
    # `request`/`req`/`data` routinely name domain objects; an attribute read off
    # them is not body trust (the rebac.py:64 pattern). ORA-40 / security-architect.
    src = "def check(request):\n    return request.organisation_id\n"
    assert "ORG001" not in _rules(src)


def test_org001_subscript_from_request_still_flagged() -> None:
    # Dict-style extraction stays broad — subscripting a domain object is not a real
    # pattern, so request["organisation_id"] is still treated as untrusted body trust.
    src = "def handler(request):\n    return request['organisation_id']\n"
    assert "ORG001" in _rules(src)


def test_org001_substrate_rebac_patterns_clean() -> None:
    # Mirrors packages/substrate/.../rebac.py:23 (AccessRequest dataclass) and
    # :64 (the organisation_id presence-validation that enforces ADR-006).
    src = (
        "@dataclass(frozen=True, slots=True)\n"
        "class AccessRequest:\n"
        "    organisation_id: str\n"
        "    subject: str\n"
        "    resource: str\n"
        "    relation: str\n"
        "\n"
        "def check(request):\n"
        "    if not request.organisation_id or not request.organisation_id.strip():\n"
        "        raise ValueError('organisation_id is required')\n"
    )
    assert "ORG001" not in _rules(src)


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
