"""Failing tests for the substrate provenance collector seam (story 0g).

Behavioural reference: legacy ``knowledge-graph-builder/app/services/audit_service.py``
(``log_public_call``), reshaped into a single-write-path collector.

These tests pin the Structured Threat Catalogue T7-M1 ("every substrate state
change emits a structured audit event with principal, action, resource,
outcome, and organisation_id") and the architecture invariant that provenance
writes go through one collector, never a direct DB write (CLAUDE.md §3.7).

RED until backend-implementer creates ``oraclous_substrate.provenance``.
"""

from __future__ import annotations

import pytest
from oraclous_substrate.provenance import ProvenanceCollector, ProvenanceRecord

pytestmark = [pytest.mark.unit, pytest.mark.audit]


# The structured fields T7-M1 requires on every provenance event.
_REQUIRED_FIELDS = ("organisation_id", "principal", "action", "resource", "outcome")


class _RecordingSink:
    """Test double for the provenance store; records every write."""

    def __init__(self) -> None:
        self.writes: list[ProvenanceRecord] = []

    async def write(self, record: ProvenanceRecord) -> None:
        self.writes.append(record)


def _record(**overrides: str) -> ProvenanceRecord:
    fields = {
        "organisation_id": "org-aaaa",
        "principal": "user-1234",
        "action": "capability.invoke",
        "resource": "capability-xyz",
        "outcome": "success",
    }
    fields.update(overrides)
    return ProvenanceRecord(**fields)


async def test_emit_writes_through_the_sink_once() -> None:
    """A valid record is written exactly once via the single emit path."""
    sink = _RecordingSink()
    collector = ProvenanceCollector(sink=sink)
    await collector.emit(_record())
    assert len(sink.writes) == 1


async def test_emitted_record_carries_all_required_fields() -> None:
    """T7-M1: the event carries principal, action, resource, outcome, organisation_id."""
    sink = _RecordingSink()
    collector = ProvenanceCollector(sink=sink)
    await collector.emit(_record())
    written = sink.writes[0]
    for field in _REQUIRED_FIELDS:
        assert getattr(written, field)  # present and non-empty


async def test_emit_is_the_only_public_write_method() -> None:
    """AC#2: the collector exposes a *single* public write entrypoint — ``emit``.

    "Single emit path" is an API-surface contract, so this is asserted
    structurally: ``emit`` exists, and no alternative public mutator does.
    """
    public_methods = {
        name
        for name in dir(ProvenanceCollector)
        if not name.startswith("_") and callable(getattr(ProvenanceCollector, name))
    }
    assert "emit" in public_methods
    forbidden = {"write", "insert", "save", "log", "persist", "store", "write_to_db"}
    assert public_methods.isdisjoint(forbidden)


async def test_sink_is_not_exposed_for_direct_writes() -> None:
    """Callers cannot bypass ``emit`` to write provenance directly to the store.

    The sink is an internal collaborator and must not be reachable through any
    public attribute of the collector, so the only way to write provenance is
    ``emit`` (which enforces the required-field contract).
    """
    sink = _RecordingSink()
    collector = ProvenanceCollector(sink=sink)
    public_attrs = [getattr(collector, n) for n in dir(collector) if not n.startswith("_")]
    assert sink not in public_attrs


@pytest.mark.parametrize("missing", _REQUIRED_FIELDS)
async def test_emit_rejects_record_missing_a_required_field(missing: str) -> None:
    """Fail-closed: an incomplete event is rejected and nothing is written.

    organisation_id in particular is mandatory on every substrate operation
    (CLAUDE.md §3.3 / ADR-006); a silent audit gap is itself a threat (T7). The
    rejection may surface at record construction or at ``emit`` — either is
    acceptable, so long as no partial write reaches the sink.
    """
    sink = _RecordingSink()
    collector = ProvenanceCollector(sink=sink)
    with pytest.raises(ValueError):
        await collector.emit(_record(**{missing: ""}))
    assert sink.writes == []
