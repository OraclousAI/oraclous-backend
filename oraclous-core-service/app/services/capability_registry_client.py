"""Client interface for the capability registry.

ORAA-76: The KGB agent toolkit reads tool descriptors from the capability registry
via this client, rather than from a static in-module schema map.
"""

from __future__ import annotations

from typing import Any


class RemoteCapabilityRegistryClient:
    """HTTP client for the capability registry service.

    Fetches OHM v1.0 tool descriptors by name.  Concrete deployments
    sub-class or configure this with a base URL; tests inject a stub.
    """

    async def get_tool_descriptor(self, tool_name: str) -> dict[str, Any] | None:
        """Return the OHM descriptor for *tool_name*, or None if not registered."""
        raise NotImplementedError


# ORAA-196: alias kept for test backward-compat; update tests to use RemoteCapabilityRegistryClient
CapabilityRegistryClient = RemoteCapabilityRegistryClient
