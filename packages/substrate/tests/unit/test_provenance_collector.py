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


def _record(**overrides: object) -> ProvenanceRecord:
    fields: dict[str, object] = {
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


# ── #826 (24 August + 11 September rulings): the record gains an extension point ───────────────
#
# ``ProvenanceRecord`` gains three OPTIONAL fields — ``context``, ``input_hash``, ``output_hash`` —
# so a record can attest what a call returned, not merely that it happened, and later carry
# claim-to-evidence relationships (#827 Decision E). ``emit()`` keeps validating only the original
# five required fields; the three new ones are nullable BY DESIGN and must never be swept into the
# non-empty check (a regression this suite pins directly). RED until
# ``oraclous_substrate.provenance`` grows these fields and ``hash_payload``.


def test_record_accepts_the_three_new_optional_fields_defaulting_to_none() -> None:
    """Constructing with only the original five fields still works; the new ones default to None."""
    record = ProvenanceRecord(
        organisation_id="org-aaaa",
        principal="user-1234",
        action="capability.invoke",
        resource="capability-xyz",
        outcome="success",
    )
    assert record.context is None
    assert record.input_hash is None
    assert record.output_hash is None


async def test_emit_accepts_a_record_with_the_new_fields_unset() -> None:
    """A record with context=None, input_hash=None, output_hash=None emits fine — the three new
    fields are not swept into the required-field non-emptiness check."""
    sink = _RecordingSink()
    collector = ProvenanceCollector(sink=sink)
    await collector.emit(_record(context=None, input_hash=None, output_hash=None))
    assert len(sink.writes) == 1


async def test_emit_accepts_an_empty_context_mapping() -> None:
    """A record with context={} emits fine — falsy-but-present is not the same as blank/missing."""
    sink = _RecordingSink()
    collector = ProvenanceCollector(sink=sink)
    await collector.emit(_record(context={}))
    assert len(sink.writes) == 1


async def test_new_fields_reach_the_sink_unchanged() -> None:
    """The three new fields round-trip through the sink untouched."""
    sink = _RecordingSink()
    collector = ProvenanceCollector(sink=sink)
    ctx = {"error_code": "no_executor"}
    in_hash = "sha256:" + "a" * 64
    out_hash = "sha256:" + "b" * 64
    await collector.emit(_record(context=ctx, input_hash=in_hash, output_hash=out_hash))
    written = sink.writes[0]
    assert written.context == ctx
    assert written.input_hash == in_hash
    assert written.output_hash == out_hash


def test_record_stays_frozen() -> None:
    """The widened record is still immutable (dataclasses.FrozenInstanceError on mutation)."""
    import dataclasses

    record = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.context = {"x": 1}  # type: ignore[misc]


def test_hash_payload_is_deterministic_and_order_independent() -> None:
    from oraclous_substrate.provenance import hash_payload

    payload_a = {"b": 2, "a": 1}
    payload_b = {"a": 1, "b": 2}  # same content, different key order
    assert hash_payload(payload_a) == hash_payload(payload_a)
    assert hash_payload(payload_a) == hash_payload(payload_b)


def test_hash_payload_differs_for_different_payloads() -> None:
    from oraclous_substrate.provenance import hash_payload

    assert hash_payload({"a": 1}) != hash_payload({"a": 2})


def test_hash_payload_shape_is_sha256_hex() -> None:
    from oraclous_substrate.provenance import hash_payload

    digest = hash_payload({"a": 1})
    assert digest is not None
    prefix, _, hex_part = digest.partition(":")
    assert prefix == "sha256"
    assert len(hex_part) == 64
    assert all(c in "0123456789abcdef" for c in hex_part)


def test_hash_payload_of_none_is_none() -> None:
    from oraclous_substrate.provenance import hash_payload

    assert hash_payload(None) is None


async def test_record_never_carries_the_raw_payload() -> None:
    """SECURITY (CLAUDE.md §11): the record attests a HASH, never the payload text itself — a
    customer's manifest/prompt/output must never be reproducible from a provenance record."""
    from oraclous_substrate.provenance import hash_payload

    secret_payload = {"prompt": "the customer's actual private prompt text"}
    record = _record(input_hash=hash_payload(secret_payload), context={"note": "no payload here"})
    assert record.input_hash != secret_payload
    assert "customer's actual private prompt" not in str(record.input_hash)
    assert "customer's actual private prompt" not in str(record.context)
    # structurally: there is no field that could hold the raw payload object/text
    assert not hasattr(record, "input_payload")
    assert not hasattr(record, "output_payload")
    assert not hasattr(record, "payload")
