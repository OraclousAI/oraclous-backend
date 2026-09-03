"""The shared allowed-tool-catalog reader (#705, #708) — READ, never relayed.

Both ``ManifestValidateConnector`` (the compiler reviewer's compile-time gate) and
``ManifestRefineConnector`` (the NL-refine applier's edit-time gate) need the SAME answer to "what
tools may this org's drafted/edited team actually draw from" — the org's registered TOOL
descriptors (where an imported MCP tool lives, a ``capability_repository`` ROW) unioned with the
in-process plugin registry (the built-in plugin classes compiled into this service). #705 fixed
this for ``manifest-validate``; #708 found the identical relay bug in ``manifest-refine`` — it read
a caller-supplied ``input_data["catalog"]`` and unioned it with only the plugin registry, so an
imported tool could never be added to an existing team via refine. This module is the ONE place
that answer is computed, so the two gates can never drift apart again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oraclous_capability_registry_service.models.enums import DescriptorKind

if TYPE_CHECKING:
    import uuid

    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )

#: the supply-chain status a descriptor must carry to count as available to a gate. Only ``active``
#: — a ``pending_approval`` tool is refused at dispatch by the HITL gate, so admitting it here would
#: compile/refine a team that is guaranteed to fail later. The failure belongs at gate time, where
#: it costs one verdict instead of a run.
_AVAILABLE = "active"


async def read_allowed_catalog(
    capability_repo: CapabilityRepository | None, organisation_id: uuid.UUID
) -> list[str]:
    """The tools the calling org may actually draw from — READ, never relayed.

    Two sources, both code: the org's registered TOOL descriptors (which is where an imported MCP
    tool lives) and the in-process plugin registry (the built-in plugin classes compiled into this
    service, which are registered by construction). A registered harness row is NOT admissible — a
    member's ``tools[]`` names tools.

    The degrade is fail-CLOSED and mirrors the engine's ``surveyed_catalog`` policy upstream
    (seed-only on a registry outage): with no repository, or a read that fails, the allowed set
    NARROWS to the built-ins. A tool the gate cannot confirm is blocked, never waved through.
    """
    from oraclous_capability_registry_service.domain.plugins import plugin_registry

    # use the public descriptor() contract (metadata.name) — discover() is typed to the base
    registered = [str(p.descriptor()["metadata"]["name"]) for p in plugin_registry.discover()]
    if capability_repo is None:
        return registered
    try:
        rows = await capability_repo.list_by_kind(organisation_id, DescriptorKind.TOOL)
    except Exception:  # noqa: BLE001 — a registry read failure narrows the gate, never widens it
        return registered
    owned = [str(row.name) for row in rows if row.name and row.status == _AVAILABLE]
    return [*owned, *registered]
