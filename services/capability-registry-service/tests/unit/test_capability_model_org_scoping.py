"""Unit: the capability descriptor model is org-scoped (ADR-006, ORG002)."""

from __future__ import annotations

import pytest
from oraclous_capability_registry_service.models.capability_descriptor import CapabilityDescriptor

pytestmark = pytest.mark.unit


def test_table_declares_organisation_id_not_null_and_indexed() -> None:
    cols = CapabilityDescriptor.__table__.columns
    assert "organisation_id" in cols
    org = cols["organisation_id"]
    assert org.nullable is False
    assert org.index is True


def test_descriptor_payload_is_not_null() -> None:
    assert CapabilityDescriptor.__table__.columns["descriptor"].nullable is False
