"""Domain errors (domain layer)."""

from __future__ import annotations


class CapabilityNotFoundError(Exception):
    """Descriptor absent in the caller's org — maps to HTTP 404 (cross-org / unknown indistinct)."""


class InvalidDescriptorError(Exception):
    """The supplied OHM descriptor failed validation — maps to HTTP 422."""


class ConfigurationConflictError(Exception):
    """A configuration replace was built on a read another writer has since superseded (#1130).

    Maps to HTTP 409 with ``error_code: configuration_conflict``. The write is REFUSED, not merged
    and not applied: two runs of the same seeded app share one instance row, so a blind replace
    silently resurrects whatever the stale read contained. The caller re-reads and decides again.
    """
