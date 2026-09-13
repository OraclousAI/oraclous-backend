"""#1063 AC3/AC4, found on #900's own PR #1065 review (security-architect BLOCKING-2, code-reviewer
BLOCKING-1, independently) — the exclusion of the compiler's own instruments must bind at the
ENFORCING check, not only at the drafter's prompt MENU.

``execution-engine-service``'s ``test_draft_manifest_menu_exclusion.py`` already pins the MENU half
of #1063 (AC1/AC2): ``compiler_onramp.draft_catalog``/``draft_catalog_described`` exclude
``COMPILER_INTERNAL_TOOLS``. That fix touches a completely different function in a different
service from the one that actually decides whether a NAMED tool is admissible —
``read_allowed_catalog`` (this module's own docstring: "the ONE place that answer is computed"),
which feeds ``oraclous_ohm.compiler.validate.validate_draft``'s fail-closed F-CAPABILITY-MISSING
gate via ``ManifestValidateConnector``/``ManifestRefineConnector``/``DraftManifestConnector``. Both
reviewers proved, independently, that the menu fix left this function untouched: an ordinary team
member could carry ``"tools": ["manifest-validate"]`` (or ``"draft-manifest"``/``"manifest-
refine"``), pass ``validate_draft`` cleanly because the slug WAS in the ``allowed`` set that
function computes, and reach dispatch as ordinary work — directly contradicting #900's own PR
description, which claimed "a tool absent from the menu is still blocked if a model names it".

RED today for a real, measured reason, not a not-yet-built seam: ``manifest-validate`` and
``manifest-refine`` are ALREADY registered, active, in-process, credential-free plugins on ``main``
(#594/#705/#708, long before #900) — ``read_allowed_catalog(None, org)`` already returns
``"Manifest Validate"``/``"Manifest Refine"`` today, unfiltered. ``COMPILER_INTERNAL_TOOLS`` itself
does not exist on ``oraclous_ohm._slug`` on ``main`` yet, so it is imported function-locally per
``.claude/rules/tests-seam-imports.md``.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000c0901")


async def test_read_allowed_catalog_excludes_every_compiler_internal_tool() -> None:
    """The org-scoped catalogue read itself — the function both the compile-time gate and the
    drafter's own answer-tool gate source their ``allowed`` set from — must exclude
    ``COMPILER_INTERNAL_TOOLS``, with no repository needed: ``capability_repo=None`` degrades to
    the built-in plugin registry alone, which is exactly where ``manifest-validate``/``manifest-
    refine`` live (discovered, active, in-process, no credential requirement — always present).
    Measured directly against ``main``: this list already carries "Manifest Validate"/"Manifest
    Refine" (Title Case descriptor names) before this fix; slug-compared here because the read
    itself never normalises casing, and neither should this assertion pretend it does."""
    from oraclous_capability_registry_service.domain.connectors._catalog import (
        read_allowed_catalog,
    )
    from oraclous_ohm._slug import COMPILER_INTERNAL_TOOLS, tool_slug

    allowed = await read_allowed_catalog(None, _ORG)
    leaked = COMPILER_INTERNAL_TOOLS & {tool_slug(name) for name in allowed}
    assert not leaked, (
        f"compiler-internal tool(s) {sorted(leaked)} leaked into the catalogue the fail-closed "
        "capability check treats as legitimate for an ordinary drafted team member"
    )


async def test_a_member_naming_a_compiler_instrument_is_still_blocked_by_the_gate() -> None:
    """Ties the two halves together per #1063 AC4 ("a test proves a compiler instrument is absent
    from the menu AND still refused by the check, so the two halves cannot drift apart"): feed
    ``validate_draft`` the SAME catalogue ``read_allowed_catalog`` actually produces in production,
    and confirm a member naming ``manifest-validate`` in its own ``tools[]`` is blocked — not
    because the drafter chose not to offer it, but because the enforcing check itself still
    refuses it. Measured RED against ``main`` today: ``would_block`` comes back ``False`` for this
    exact draft, because ``read_allowed_catalog`` still includes "Manifest Validate" unfiltered."""
    from oraclous_capability_registry_service.domain.connectors._catalog import (
        read_allowed_catalog,
    )
    from oraclous_ohm.compiler.validate import validate_draft

    allowed = await read_allowed_catalog(None, _ORG)
    draft = {
        "members": [
            {
                "role": "scout",
                "kind": "agent",
                "tools": ["manifest-validate"],
                "tool_rationale": {"manifest-validate": "needs it to validate its own output"},
                "outputs_schema": {"required": ["summary"]},
            }
        ]
    }
    v = validate_draft(draft, allowed, owner_organization_id=_ORG)
    assert v["would_block"] is True, (
        "a compiler-internal tool absent from the menu must still be refused by the fail-closed "
        "capability check when a member names it directly (#1063 AC3/AC4)"
    )
    assert any("F-CAPABILITY-MISSING" in b for b in v["blocking"])


def test_an_ordinary_tool_is_unaffected_by_the_exclusion() -> None:
    """Nothing else leaves the catalogue — a legitimately registered, non-compiler-internal
    plugin (``web-research``, an ordinary builtin) survives the same filter untouched. Guards
    against an over-broad fix that excludes more than ``COMPILER_INTERNAL_TOOLS`` names."""
    from oraclous_capability_registry_service.domain.plugins import plugin_registry
    from oraclous_ohm._slug import tool_slug

    names = {tool_slug(str(p.descriptor()["metadata"]["name"])) for p in plugin_registry.discover()}
    assert "web-research" in names, "expected the ordinary web-research builtin to be registered"


async def test_an_ordinary_registered_tool_still_passes_the_gate() -> None:
    """The companion regression pin to the two tests above: excluding the compiler's own
    instruments from ``read_allowed_catalog`` must not touch an ordinary tool's admissibility."""
    from oraclous_capability_registry_service.domain.connectors._catalog import (
        read_allowed_catalog,
    )
    from oraclous_ohm.compiler.validate import validate_draft

    allowed = await read_allowed_catalog(None, _ORG)
    draft = {
        "members": [
            {
                "role": "scout",
                "kind": "agent",
                "tools": ["web-research"],
                "tool_rationale": {"web-research": "needs it to search the web"},
                "outputs_schema": {"required": ["summary"]},
            }
        ]
    }
    v = validate_draft(draft, allowed, owner_organization_id=_ORG)
    assert v["would_block"] is False, (
        f"an ordinary registered tool must still pass the gate; blocked with: {v['blocking']}"
    )
