"""OHM atomic reference resolution (domain layer; OHM v1.0 spec §3).

"Every reference resolves at harness load time, atomically. If any reference fails, the entire load
fails. Partial loads are never permitted." Resolves **all** of an OHM's capability references up
front (read-only) — so the agent's full toolset is known and a single bad ref aborts the load before
any instance is created or any tool runs. The resolver is injected (the registry client), keeping
this pure of transport.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from oraclous_ohm.errors import OHMReferenceError
from oraclous_ohm.manifest import OHMManifest

# (ref, explicit_id) -> the resolved registry tool item (carries id + descriptor).
CapabilityResolver = Callable[[str, str | None], Awaitable[dict[str, Any]]]


async def resolve_capabilities(
    manifest: OHMManifest, resolve: CapabilityResolver
) -> dict[str, dict[str, Any]]:
    """Resolve every ``capabilities[].ref`` to a registry item, keyed by binding. All-or-nothing."""
    resolved: dict[str, dict[str, Any]] = {}
    for cap in manifest.capabilities:
        try:
            resolved[cap.binding] = await resolve(cap.ref, cap.config.get("capability_id"))
        except OHMReferenceError:
            raise
        except Exception as exc:  # noqa: BLE001 — any resolver failure aborts the whole load
            raise OHMReferenceError(
                f"capability {cap.binding!r} (ref {cap.ref!r}) failed to resolve: {exc}"
            ) from exc
    return resolved
