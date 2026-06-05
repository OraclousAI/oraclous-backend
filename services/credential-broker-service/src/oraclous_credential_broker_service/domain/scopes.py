"""OAuth scope reasoning (ORAA-4 §21 domain layer). Pure, no I/O."""

from __future__ import annotations


def missing_scopes(required: list[str] | None, granted: list[str] | None) -> list[str]:
    """The required scopes not present in the granted set (order-stable)."""
    granted_set = set(granted or [])
    return [s for s in (required or []) if s not in granted_set]


def has_required(required: list[str] | None, granted: list[str] | None) -> bool:
    """True iff every required scope is granted (required ⊆ granted)."""
    return not missing_scopes(required, granted)
