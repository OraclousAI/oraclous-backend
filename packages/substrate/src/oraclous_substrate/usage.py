"""Substrate usage-event-stream seam (Layer 1, ADR-009).

The append-only metering stream. Every metered action emits one structured
usage event ``{organisation_id, principal, action_type, quantity, unit,
dimensions, timestamp}`` through ``UsageEventStream.emit`` — the single write
path. Identity (``organisation_id``, ``principal``) is taken from the ambient
organisation-context (0f / ORA-14), never from the caller's metering payload, so
there is no request-body channel through which a tenant scope could be smuggled
(Structured Threat Catalogue T1-M1). There is no update or delete path:
corrections are compensating events, not edits (ADR-009).

``dimensions`` is bounded scalar metering metadata — bounded key, bounded value,
bounded cardinality, no nested structures — so usage events stay
operator-readable *metadata, not customer state* (T6 / ADR-008). Billing is a
separable downstream consumer; this seam only emits and reads the stream.

The store is injected (a ``UsageEventStore``): this seam owns the schema,
validation, and context-sourcing, not the storage backend (whose data-layer
row-level-security backstop is A1 / ORA-16). Wiring emission into call sites is
C2 and out of scope here.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol

from oraclous_governance import current_organisation_context

# dimensions bounds — usage events are metadata, so dimensions carries short
# scalar metering labels, never customer payload (T6). Exceeding any bound is
# rejected fail-closed before anything is written.
MAX_DIMENSION_KEY_LENGTH = 64
MAX_DIMENSION_VALUE_LENGTH = 256
MAX_DIMENSIONS = 16

# A dimension value is a scalar metering datum, never a nested structure.
DimensionValue = str | int | float | bool


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """An append-only, immutable usage event — the seven ADR-009 fields."""

    organisation_id: uuid.UUID
    principal: uuid.UUID
    action_type: str
    quantity: float
    unit: str
    dimensions: Mapping[str, DimensionValue]
    timestamp: datetime


class UsageEventStore(Protocol):
    """Persists and serves usage events, scoped by organisation.

    The stream's only collaborator. The data-layer row-level-security backstop
    (T1-M3) lives in the concrete store (A1 / ORA-16), not in this seam.
    """

    async def write(self, event: UsageEvent) -> None: ...

    async def read(self, organisation_id: uuid.UUID) -> list[UsageEvent]: ...


class UsageEventStream:
    """The single, validated, append-only emit path for usage events."""

    def __init__(self, store: UsageEventStore) -> None:
        # Private on purpose: emit() is the only write path — there is no
        # update/delete/bypass (ADR-009 append-only).
        self._store = store

    async def emit(
        self,
        *,
        action_type: str,
        quantity: float,
        unit: str,
        dimensions: Mapping[str, DimensionValue],
    ) -> None:
        """Emit one usage event, scoped to the ambient organisation-context.

        Fails closed: a missing context, a blank required field, a non-numeric
        quantity, or non-conforming ``dimensions`` all raise before anything is
        written.
        """
        # Identity from the authenticated context, never the payload. Raises
        # MissingOrganisationContextError (fail-closed) when none is bound.
        context = current_organisation_context()
        _require_nonempty("action_type", action_type)
        _require_nonempty("unit", unit)
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            raise ValueError("usage event quantity must be a number")
        event = UsageEvent(
            organisation_id=context.organisation_id,
            principal=context.principal_id,
            action_type=action_type,
            quantity=quantity,
            unit=unit,
            dimensions=_validate_dimensions(dimensions),
            timestamp=datetime.now(UTC),
        )
        await self._store.write(event)

    async def read(self) -> list[UsageEvent]:
        """Return the current organisation's usage events.

        Scoped to the ambient context's organisation: there is no parameter
        through which another organisation's events could be requested (T1).
        Fails closed when no context is bound.
        """
        context = current_organisation_context()
        return await self._store.read(context.organisation_id)


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"usage event {name} must be a non-empty string")


def _validate_dimensions(
    dimensions: Mapping[str, DimensionValue],
) -> Mapping[str, DimensionValue]:
    if not isinstance(dimensions, Mapping):
        raise ValueError("usage event dimensions must be a mapping")
    if len(dimensions) > MAX_DIMENSIONS:
        raise ValueError(f"usage event dimensions exceeds {MAX_DIMENSIONS} entries")
    cleaned: dict[str, DimensionValue] = {}
    for key, value in dimensions.items():
        _validate_dimension_key(key)
        _validate_dimension_value(key, value)
        cleaned[key] = value
    # Frozen mapping so a stored event's dimensions cannot be mutated after emit.
    return MappingProxyType(cleaned)


def _validate_dimension_key(key: object) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError("usage event dimension keys must be non-empty strings")
    if len(key) > MAX_DIMENSION_KEY_LENGTH:
        raise ValueError(f"usage event dimension key exceeds {MAX_DIMENSION_KEY_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        raise ValueError(
            "usage event dimension key must not contain whitespace or control characters"
        )


def _validate_dimension_value(key: str, value: object) -> None:
    # bool is a scalar metering datum (and a subclass of int) — allow it.
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        return
    if isinstance(value, str):
        if len(value) > MAX_DIMENSION_VALUE_LENGTH:
            raise ValueError(
                f"usage event dimension '{key}' value exceeds "
                f"{MAX_DIMENSION_VALUE_LENGTH} characters"
            )
        return
    raise ValueError(f"usage event dimension '{key}' must be a scalar (str, int, float, or bool)")
