"""The shared allowed-tool-catalog reader (#705, #708) — READ, never relayed.

Both ``ManifestValidateConnector`` (the compiler reviewer's compile-time gate),
``ManifestRefineConnector`` (the NL-refine applier's edit-time gate), and ``DraftManifestConnector``
(#900, the drafter's own answer-tool gate) need the SAME answer to "what tools may this org's
drafted/edited team actually draw from" — the org's registered TOOL descriptors (where an imported
MCP tool lives, a ``capability_repository`` ROW) unioned with the in-process plugin registry (the
built-in plugin classes compiled into this service). #705 fixed this for ``manifest-validate``;
#708 found the identical relay bug in ``manifest-refine`` — it read a caller-supplied
``input_data["catalog"]`` and unioned it with only the plugin registry, so an imported tool could
never be added to an existing team via refine. This module is the ONE place that answer is
computed, so the gates can never drift apart again.

#900 review finding (security + craft, independently): the compiler's OWN instruments
(``oraclous_ohm._slug.COMPILER_INTERNAL_TOOLS`` — ``manifest-validate``/``manifest-refine``/
``draft-manifest``) were excluded from the DRAFTER'S PROMPT MENU (``compiler_onramp.py``) but not
from THIS function — the one that actually decides what an ordinary drafted member may hold and
have dispatched. An ordinary member could carry one of the compiler's own tools, pass this gate
cleanly, and have it dispatched at runtime — exactly the drift this module's own docstring says it
exists to prevent, now between the menu and the gate rather than between two gates. Filtered here,
reading the SAME canonical set the menu filter reads, never a second copy of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oraclous_ohm._slug import COMPILER_INTERNAL_TOOLS, tool_slug

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


def _not_compiler_internal(name: str) -> bool:
    """#900: exclude the compiler's own instruments by the ONE canonical set, slug-compared (a
    plugin's descriptor name is Title Case, e.g. ``"Draft Manifest"``; the set is slugs)."""
    return tool_slug(name) not in COMPILER_INTERNAL_TOOLS


async def read_allowed_catalog(
    capability_repo: CapabilityRepository | None, organisation_id: uuid.UUID
) -> list[str]:
    """The tools the calling org may actually draw from — READ, never relayed.

    Two sources, both code: the org's registered TOOL descriptors (which is where an imported MCP
    tool lives) and the in-process plugin registry (the built-in plugin classes compiled into this
    service, which are registered by construction). A registered harness row is NOT admissible — a
    member's ``tools[]`` names tools. The compiler's OWN instruments
    (``COMPILER_INTERNAL_TOOLS``) are excluded from both sources — they are tools the compiler's
    reviewer/drafter hold unconditionally, hardcoded, never something an ordinary drafted member
    may pick or dispatch.

    The degrade is fail-CLOSED and mirrors the engine's ``surveyed_catalog`` policy upstream
    (seed-only on a registry outage): with no repository, or a read that fails, the allowed set
    NARROWS to the built-ins. A tool the gate cannot confirm is blocked, never waved through.
    """
    from oraclous_capability_registry_service.domain.plugins import plugin_registry

    # use the public descriptor() contract (metadata.name) — discover() is typed to the base
    registered = [
        str(p.descriptor()["metadata"]["name"])
        for p in plugin_registry.discover()
        if _not_compiler_internal(str(p.descriptor()["metadata"]["name"]))
    ]
    if capability_repo is None:
        return registered
    try:
        rows = await capability_repo.list_by_kind(organisation_id, DescriptorKind.TOOL)
    except Exception:  # noqa: BLE001 — a registry read failure narrows the gate, never widens it
        return registered
    owned = [
        str(row.name)
        for row in rows
        if row.name and row.status == _AVAILABLE and _not_compiler_internal(str(row.name))
    ]
    return [*owned, *registered]
