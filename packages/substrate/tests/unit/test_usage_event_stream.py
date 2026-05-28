"""Failing tests for the substrate usage-event-stream seam (ORA-21, story C1).

Behavioural reference (Reshape): legacy
``knowledge-graph-builder/app/services/audit_service.py`` (audit emission) and
the metered-quantity capture in ``app/services/llm_pricing.py`` /
``oraclous-core-service`` credits, reshaped to the ADR-009 substrate usage stream.

These tests pin ADR-009 ("Metering at Substrate, Billing as Separable"): the
substrate emits an append-only usage event
``{organisation_id, principal, action_type, quantity, unit, dimensions, timestamp}``
through the audit pipeline (a separate stream). They also pin two Structured
Threat Catalogue contracts the brief tags:

* **T7-M1** — every substrate state change emits a structured event; emission is
  the single, validated write path (mirrors the provenance collector, CLAUDE.md
  §3.7).
* **T6** (operator-separation) — usage events are *metadata, not customer state*
  (that is precisely why ADR-009 lets an operator read them for billing without
  breaching ADR-008). The metadata property is load-bearing, so ``dimensions``
  must structurally exclude plaintext customer payload.

Identity (``organisation_id``/``principal``) is taken from the **ambient**
organisation-context (0f / ORA-14), never from the caller's metering payload —
the emit path reads ``current_organisation_context()`` rather than accepting an
``organisation_id`` argument, so there is no body-supplied channel to smuggle one
through. Wiring emission into call sites is C2 (out of scope here); this seam is
the primitive those hooks will call.

RED until backend-implementer creates ``oraclous_substrate.usage``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from oraclous_governance import (
    MissingOrganisationContextError,
    OrganisationContext,
    PrincipalType,
    use_organisation_context,
)
from oraclous_substrate.usage import (
    MAX_DIMENSION_KEY_LENGTH,
    MAX_DIMENSION_VALUE_LENGTH,
    MAX_DIMENSIONS,
    UsageEvent,
    UsageEventStream,
)

pytestmark = [pytest.mark.unit]


# The seven ADR-009 fields every usage event carries.
_ADR009_FIELDS = (
    "organisation_id",
    "principal",
    "action_type",
    "quantity",
    "unit",
    "dimensions",
    "timestamp",
)

# Public methods that would betray a mutable / non-append-only store. A usage
# stream is append-only: corrections are compensating events, never edits
# (ADR-009). ``read`` is legitimate; mutators and deletes are not.
_FORBIDDEN_METHODS = frozenset(
    {"update", "delete", "modify", "overwrite", "remove", "edit", "patch", "replace", "amend"}
)


class _RecordingStore:
    """In-memory test double for the usage-event store.

    Records every append and serves organisation-scoped reads — standing in for
    the real substrate store whose data-layer row-level-security backstop is
    A1 / ORA-16. The double filters by ``organisation_id`` so a leak would have
    to come from the stream handing it the wrong scope.
    """

    def __init__(self) -> None:
        self.writes: list[UsageEvent] = []

    async def write(self, event: UsageEvent) -> None:
        self.writes.append(event)

    async def read(self, organisation_id: uuid.UUID) -> list[UsageEvent]:
        return [e for e in self.writes if str(e.organisation_id) == str(organisation_id)]


def _context(organisation_id: uuid.UUID | None = None) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=organisation_id or uuid.uuid4(),
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


async def _emit(stream: UsageEventStream, **overrides: object) -> None:
    """Emit one well-formed usage event; ``overrides`` tweak a single field."""
    kwargs: dict[str, object] = {
        "action_type": "model.tokens",
        "quantity": 128,
        "unit": "tokens",
        "dimensions": {"model": "claude-x", "token_kind": "input"},
    }
    kwargs.update(overrides)
    await stream.emit(**kwargs)


# --------------------------------------------------------------------------- #
# AC1 — a usage event with the ADR-009 fields emits, and the stream is append-only
# --------------------------------------------------------------------------- #


@pytest.mark.audit
async def test_emit_writes_one_event_with_all_adr009_fields() -> None:
    """A valid emit writes exactly one event carrying every ADR-009 field."""
    store = _RecordingStore()
    stream = UsageEventStream(store)
    ctx = _context()
    with use_organisation_context(ctx):
        await _emit(stream, action_type="model.tokens", quantity=128, unit="tokens")

    assert len(store.writes) == 1
    event = store.writes[0]
    for field in _ADR009_FIELDS:
        assert hasattr(event, field), f"usage event missing ADR-009 field: {field}"
    assert event.action_type == "model.tokens"
    assert event.quantity == 128
    assert event.unit == "tokens"
    assert isinstance(event.timestamp, datetime)


@pytest.mark.audit
async def test_stream_exposes_no_mutation_or_delete_path() -> None:
    """AC1 append-only: the stream offers emit (+read) but no edit/delete path."""
    public = {
        name
        for name in dir(UsageEventStream)
        if not name.startswith("_") and callable(getattr(UsageEventStream, name))
    }
    assert "emit" in public
    assert public.isdisjoint(_FORBIDDEN_METHODS)


@pytest.mark.audit
async def test_usage_event_is_immutable() -> None:
    """A written event cannot be mutated after the fact (tamper-evidence)."""
    store = _RecordingStore()
    stream = UsageEventStream(store)
    with use_organisation_context(_context()):
        await _emit(stream)
    event = store.writes[0]
    with pytest.raises(AttributeError):
        event.quantity = 999  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# AC2 — organisation_id (+ principal) come from the org-context, never the body
# --------------------------------------------------------------------------- #


@pytest.mark.audit
async def test_identity_is_taken_from_the_bound_org_context() -> None:
    """T1-M1: the event's organisation_id and principal come from 0f's context."""
    store = _RecordingStore()
    stream = UsageEventStream(store)
    ctx = _context()
    with use_organisation_context(ctx):
        await _emit(stream)

    event = store.writes[0]
    assert str(event.organisation_id) == str(ctx.organisation_id)
    assert str(event.principal) == str(ctx.principal_id)


@pytest.mark.security
async def test_body_supplied_organisation_id_cannot_override_context() -> None:
    """AC2: a smuggled organisation_id in the metering payload never wins.

    The authoritative organisation is the bound context's. A ``dimensions`` key
    naming a different organisation is either rejected (stricter) or ignored —
    either way the stored event is scoped to the context, never the body.
    """
    store = _RecordingStore()
    stream = UsageEventStream(store)
    ctx = _context()
    other_org = uuid.uuid4()
    with use_organisation_context(ctx):
        try:
            await _emit(stream, dimensions={"organisation_id": str(other_org)})
        except ValueError:
            assert store.writes == []  # rejecting a body-supplied org is acceptable
            return

    event = store.writes[-1]
    assert str(event.organisation_id) == str(ctx.organisation_id)
    assert str(event.organisation_id) != str(other_org)


@pytest.mark.audit
async def test_emit_without_bound_context_fails_closed() -> None:
    """T1-M1 fail-closed: no bound context means no event is emitted at all.

    There is no organisation to scope to, so emit must halt rather than default
    or read an attacker-influenced value — and nothing is written.
    """
    store = _RecordingStore()
    stream = UsageEventStream(store)
    with pytest.raises(MissingOrganisationContextError):
        await _emit(stream)
    assert store.writes == []


# --------------------------------------------------------------------------- #
# AC1 fail-closed — incomplete / malformed metering payloads are rejected
# --------------------------------------------------------------------------- #


@pytest.mark.audit
@pytest.mark.parametrize("field", ["action_type", "unit"])
async def test_emit_rejects_blank_required_field(field: str) -> None:
    """A blank required metering field is rejected and nothing is written."""
    store = _RecordingStore()
    stream = UsageEventStream(store)
    with use_organisation_context(_context()), pytest.raises(ValueError):
        await _emit(stream, **{field: ""})
    assert store.writes == []


@pytest.mark.audit
async def test_emit_rejects_non_numeric_quantity() -> None:
    """``quantity`` is a metered amount; a non-numeric quantity is rejected."""
    store = _RecordingStore()
    stream = UsageEventStream(store)
    with use_organisation_context(_context()), pytest.raises((ValueError, TypeError)):
        await _emit(stream, quantity="lots")
    assert store.writes == []


# --------------------------------------------------------------------------- #
# AC3 — no plaintext customer payload in dimensions (T6 operator-separation)
# --------------------------------------------------------------------------- #


@pytest.mark.security
@pytest.mark.operator_separation
@pytest.mark.parametrize(
    "payload",
    [
        {"blob": {"prompt": "a customer's private prompt text"}},  # nested mapping
        {"history": ["customer message one", "customer message two"]},  # nested list
    ],
)
async def test_dimensions_rejects_nested_customer_payload(payload: dict) -> None:
    """T6: dimensions is flat scalar metadata; a nested blob (where customer
    payload would ride along) is rejected and nothing is written.
    """
    store = _RecordingStore()
    stream = UsageEventStream(store)
    with use_organisation_context(_context()), pytest.raises(ValueError):
        await _emit(stream, dimensions=payload)
    assert store.writes == []


@pytest.mark.security
@pytest.mark.operator_separation
async def test_dimension_value_length_is_bounded() -> None:
    """T6: dimension values are bounded metering labels, not unbounded customer
    text. A value longer than the published bound is rejected (nothing written).
    """
    store = _RecordingStore()
    stream = UsageEventStream(store)
    oversized = "x" * (MAX_DIMENSION_VALUE_LENGTH + 1)
    with use_organisation_context(_context()), pytest.raises(ValueError):
        await _emit(stream, dimensions={"label": oversized})
    assert store.writes == []


@pytest.mark.security
@pytest.mark.operator_separation
async def test_dimension_key_length_is_bounded() -> None:
    """T6: a dimension *key* is a bounded metering label, not a payload carrier.

    A key longer than the published bound is rejected (nothing written), closing
    the smuggle path where customer text rides in the key instead of the value.
    """
    store = _RecordingStore()
    stream = UsageEventStream(store)
    oversized_key = "k" * (MAX_DIMENSION_KEY_LENGTH + 1)
    with use_organisation_context(_context()), pytest.raises(ValueError):
        await _emit(stream, dimensions={oversized_key: 1})
    assert store.writes == []


@pytest.mark.security
@pytest.mark.operator_separation
@pytest.mark.parametrize(
    "key",
    [
        "has space",  # whitespace
        "new\nline",  # newline
        "tab\there",  # tab
        "null\x00byte",  # NUL control char
        "bell\x07char",  # control char
    ],
)
async def test_dimension_key_rejects_whitespace_and_control_characters(key: str) -> None:
    """T6: dimension keys are constrained metering labels; whitespace / newline /
    control characters (how prose payload would ride in a key) are rejected and
    nothing is written.
    """
    store = _RecordingStore()
    stream = UsageEventStream(store)
    with use_organisation_context(_context()), pytest.raises(ValueError):
        await _emit(stream, dimensions={key: 1})
    assert store.writes == []


@pytest.mark.security
@pytest.mark.operator_separation
async def test_dimensions_entry_count_is_capped() -> None:
    """T6: dimensions cardinality is bounded — real metering dimensions are a
    handful of labels. A mapping exceeding the published cap is rejected
    (nothing written), closing the smuggle path of payload spread across many
    entries.
    """
    store = _RecordingStore()
    stream = UsageEventStream(store)
    too_many = {f"k{i}": 1 for i in range(MAX_DIMENSIONS + 1)}
    with use_organisation_context(_context()), pytest.raises(ValueError):
        await _emit(stream, dimensions=too_many)
    assert store.writes == []


@pytest.mark.audit
async def test_well_formed_scalar_dimensions_are_accepted() -> None:
    """The structural constraint is not over-broad: scalar metering metadata
    (str / int / float / bool) is accepted.
    """
    store = _RecordingStore()
    stream = UsageEventStream(store)
    with use_organisation_context(_context()):
        await _emit(
            stream,
            dimensions={"model": "claude-x", "tokens": 128, "ratio": 0.5, "cached": True},
        )
    assert len(store.writes) == 1
