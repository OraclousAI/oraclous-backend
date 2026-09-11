"""Unit: the registry's provenance read-surface response models (#826, the 11 September ruling).

ONE event shape, additive to the engine's ``ActivityEvent``/``ActivityResponse``
(``execution-engine-service/schema/engine_schemas.py``) — ``principal``, ``context``,
``input_hash``, ``output_hash`` are newly exposed here.

RED until ``oraclous_capability_registry_service.schema.provenance_schema`` exists (function-local
import — TST001: the module does not exist on the current tree).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.unit


def _schema() -> tuple[type, type]:
    from oraclous_capability_registry_service.schema.provenance_schema import (
        ProvenanceEvent,
        ProvenanceListResponse,
    )

    return ProvenanceEvent, ProvenanceListResponse


def _event(**overrides: object) -> object:
    ProvenanceEvent, _ = _schema()
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "action": "capability.invoke",
        "resource": "tool_instance:1234",
        "outcome": "succeeded",
        "created_at": datetime.now(UTC),
        "principal": "user-1",
        "context": {"error_code": "no_executor"},
        "input_hash": "sha256:" + "a" * 64,
        "output_hash": "sha256:" + "b" * 64,
    }
    fields.update(overrides)
    return ProvenanceEvent(**fields)


def test_provenance_event_has_exactly_the_nine_ruled_fields() -> None:
    ProvenanceEvent, _ = _schema()
    event = _event()
    assert set(type(event).model_fields) == {
        "id",
        "action",
        "resource",
        "outcome",
        "created_at",
        "principal",
        "context",
        "input_hash",
        "output_hash",
    }


def test_provenance_event_nullable_fields_accept_none() -> None:
    event = _event(created_at=None, context=None, input_hash=None, output_hash=None)
    assert event.created_at is None
    assert event.context is None
    assert event.input_hash is None
    assert event.output_hash is None


def test_provenance_event_principal_is_a_plain_string() -> None:
    """``principal`` is NEWLY exposed (11 Sep ruling) — a plain identifier, not nested/opaque."""
    event = _event(principal="00000000-0000-0000-0000-000000000042")
    assert event.principal == "00000000-0000-0000-0000-000000000042"


def test_provenance_list_response_shape() -> None:
    _, ProvenanceListResponse = _schema()
    event = _event()
    resp = ProvenanceListResponse(events=[event], total=1)
    assert set(type(resp).model_fields) == {"events", "total"}
    assert resp.events == [event]
    assert resp.total == 1


def test_provenance_list_response_total_is_independent_of_page_size() -> None:
    """``total`` reflects the org's full count, not merely ``len(events)`` on this page."""
    _, ProvenanceListResponse = _schema()
    event = _event()
    resp = ProvenanceListResponse(events=[event], total=57)
    assert len(resp.events) == 1
    assert resp.total == 57
