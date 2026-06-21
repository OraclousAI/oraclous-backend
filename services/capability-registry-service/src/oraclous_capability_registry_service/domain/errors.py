"""Domain errors (domain layer)."""

from __future__ import annotations


class CapabilityNotFoundError(Exception):
    """Descriptor absent in the caller's org — maps to HTTP 404 (cross-org / unknown indistinct)."""


class InvalidDescriptorError(Exception):
    """The supplied OHM descriptor failed validation — maps to HTTP 422."""
