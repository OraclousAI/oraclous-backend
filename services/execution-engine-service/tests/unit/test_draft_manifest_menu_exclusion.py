"""#900 — does ``draft-manifest`` (and its precedent, ``manifest-validate``) stay out of the
drafter's OWN surveyed menu?

INVESTIGATION FINDING (read in full before doubting this: ``services/capability-registry-service/
src/oraclous_capability_registry_service/services/plugin_sync.py``, ``.../routes/
capability_routes.py``, ``packages/ohm/src/oraclous_ohm/seeds.py::survey_catalog``, and
``services/execution-engine-service/src/oraclous_execution_engine_service/domain/
compiler_onramp.py::draft_catalog``/``draft_catalog_described``):

**THERE IS NO EXISTING EXCLUSION MECHANISM.** ``sync_plugins`` seeds EVERY ``plugin_registry.
discover()`` class — ``ManifestValidatePlugin`` included — into an org's registry as a ``kind=tool``
row with no category/tag/"system"/"internal" flag distinguishing it from an ordinary member-
selectable tool; ``CapabilityDescriptor.status`` server-defaults to ``"active"`` and
``plugin_sync.py`` applies no filter at all. ``GET /api/v1/capabilities?kind=tool``
(``capability_routes.py::list_capabilities``) returns it unfiltered — there is no
category/tag/system-flag query param at all. ``RegistryClient.list_capability_rows``/
``list_capabilities`` (execution-engine-service) keep only ``status == "active"`` rows, which
``manifest-validate`` already satisfies. ``draft_catalog``/``draft_catalog_described``
(``compiler_onramp.py``) union that live list wholesale into the seed catalog, filtering ONLY the
graph-substrate file tools (``_FILE_SUBSTRATE_TOOLS`` from ``oraclous_ohm._slug``) — nothing else.

So **``manifest-validate`` already, TODAY, on ``main``, silently appears in the drafter's own
surveyed menu** whenever an org's registry has been synced with the built-in plugins — and nothing
has broken because nothing NEEDS to choose it from that menu: the compiler's reviewer is given
``tools=["manifest-validate"]`` UNCONDITIONALLY, hardcoded in
``packages/ohm/src/oraclous_ohm/compiler/team.py``, regardless of what the survey contains (see
this branch's own ``packages/ohm/tests/compiler/test_team.py::
test_planner_and_reviewer_are_unaffected_when_a_catalog_is_given``). ``draft-manifest`` will
inherit the IDENTICAL silent leak the moment its plugin registers itself, for the identical reason,
unless ``[impl]`` adds a real filter somewhere in this chain.

This is a genuine, surprising, PRE-EXISTING gap — not a hypothetical about unbuilt #900 code. The
tests below pin the CORRECT behaviour (absence from the surveyed menu) for both tool names; the
``manifest-validate`` test is RED against code that has been on ``main`` since #594/#705, which is
the load-bearing, flag-loudly part of this finding.
"""

from __future__ import annotations

import pytest
from oraclous_execution_engine_service.domain.compiler_onramp import draft_catalog

pytestmark = pytest.mark.unit


class _FakeRowRegistry:
    """The registry seam as the described catalog consumes it — rows, not bare names. Mirrors
    ``test_surveyed_catalog_union.py``'s own ``_FakeRowRegistry`` fixture exactly."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    async def list_capability_rows(self) -> list[dict[str, str]]:
        return list(self._rows)


async def test_manifest_validate_currently_leaks_into_the_surveyed_menu_a_pre_existing_gap() -> (
    None
):
    """RED against code already on ``main`` (#594/#705), not against #900's new code — see the
    module docstring's investigation. Pins the CORRECT behaviour: a first-party compiler-internal
    tool the reviewer holds unconditionally must never be offered to an ordinary drafted-team
    MEMBER to pick from its own tools[] menu."""
    from oraclous_execution_engine_service.services.compiler_run_service import (
        surveyed_catalog_described,
    )

    row = {"name": "manifest-validate", "description": "compiler validate gate"}
    described = await surveyed_catalog_described(_FakeRowRegistry([row]))
    names = {e["name"] for e in described}
    assert "manifest-validate" not in names, (
        "manifest-validate is a compiler-internal tool the reviewer holds unconditionally — it "
        "must never appear in the menu an ordinary drafted TEAM MEMBER can choose a tool from; "
        "today it leaks in through the plain live-registry union with no filter at all"
    )


async def test_draft_manifest_must_not_leak_into_its_own_surveyed_menu_either() -> None:
    """The tool #900 is building would inherit the identical leak the moment its plugin registers
    itself — ``sync_plugins`` seeds every ``plugin_registry.discover()`` class unfiltered — unless
    ``[impl]`` adds a real exclusion somewhere in this chain. RED for the same structural reason as
    the ``manifest-validate`` test above."""
    from oraclous_execution_engine_service.services.compiler_run_service import (
        surveyed_catalog_described,
    )

    row = {"name": "draft-manifest", "description": "compiler drafter answer tool"}
    described = await surveyed_catalog_described(_FakeRowRegistry([row]))
    names = {e["name"] for e in described}
    assert "draft-manifest" not in names, (
        "draft-manifest is the manifest-drafter's own hardcoded answer tool — it must never be "
        "offered to a drafted team's ordinary MEMBER as something it can pick from the menu"
    )


def test_draft_catalog_the_synchronous_slug_view_has_the_same_gap() -> None:
    """``draft_catalog`` (the bare-slug view the capability-absence gate itself diffs against) has
    the identical gap, proven synchronously with no fake registry needed — the union in
    ``compiler_onramp.py`` filters only the graph-substrate file tools, nothing else."""
    united = draft_catalog(["manifest-validate", "draft-manifest"])
    assert "manifest-validate" not in united
    assert "draft-manifest" not in united
