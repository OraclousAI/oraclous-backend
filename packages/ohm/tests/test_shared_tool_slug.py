"""One canonical tool-name normaliser, shared by all three on-ramps (#694).

The same function is written out THREE times today: ``compiler.validate._tool_slug`` (canonical),
``seeds._slug`` (its inlined twin), and — after this slice — a third copy would be needed in
``import_.mapping``. The copies exist because ``compiler.validate`` imports ``import_``, so
``import_.mapping`` importing ``compiler.validate`` would close an import cycle.

That duplication is not cosmetic: it IS #694. ``_GRAPH_REMAP`` is keyed on the Claude-Code tool
names (``"Write"``) while the compiler catalog is lower-cased slugs (``write``), so every compiled
member fell through the remap to ``core/write@1`` and wrote into the per-org tmp sandbox on a
graph-bound run (``fe548aac``: ~10 KB off-graph). Two on-ramps disagreed about what a tool is
called because neither could see the other's answer.

So the function moves DOWN to a leaf module every layer may import, and the copies are deleted.
``oraclous_ohm._slug`` imports nothing from ``oraclous_ohm``, which is what makes it cycle-free.

RED until the [impl] adds ``packages/ohm/src/oraclous_ohm/_slug.py``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_the_shared_module_exists_and_exports_tool_slug() -> None:
    """The new leaf seam. Function-local import: at module level this would break collection."""
    from oraclous_ohm._slug import tool_slug

    assert callable(tool_slug)


def test_a_tool_name_and_its_canonical_ref_normalise_to_the_same_slug() -> None:
    from oraclous_ohm._slug import tool_slug

    assert tool_slug("Web Research") == "web-research"
    assert tool_slug("core/web-research@1.0.0") == "web-research"
    assert tool_slug("web-research") == "web-research"


def test_case_is_erased_which_is_the_defect_694_reports() -> None:
    """``Write`` and ``write`` are the SAME tool. The remap could not see that."""
    from oraclous_ohm._slug import tool_slug

    for name in ("Write", "write", "WRITE", " Write "):
        assert tool_slug(name) == "write"
    assert tool_slug("Read") == tool_slug("read") == "read"
    assert tool_slug("Glob") == tool_slug("glob") == "glob"


@pytest.mark.security
def test_a_foreign_namespace_can_never_masquerade_as_a_bare_surveyed_tool() -> None:
    """#594's masquerade gate, carried over UNCHANGED by the move. ONLY the canonical ``core/``
    namespace is stripped; anything else keeps an ``ns--`` marker a bare slug can never contain."""
    from oraclous_ohm._slug import tool_slug

    assert tool_slug("evil/web-research") != "web-research"
    assert tool_slug("evil/web-research").startswith("ns--")
    assert tool_slug("core/web-search@1.0.0") == "web-search"  # a DIFFERENT tool stays different


@pytest.mark.security
def test_a_namespace_that_slugs_to_nothing_is_degenerate_and_drops() -> None:
    """``./x``, ``/x``, ``😈/x`` would all bare-slug to ``x`` and slip the gate, so an erasing
    segment returns ``""`` — dropped from a catalog, blocking as a drafted tool. Never a
    wildcard."""
    from oraclous_ohm._slug import tool_slug

    for degenerate in ("./web-research", "/web-research", "😈/web-research", "core//web-research"):
        assert tool_slug(degenerate) == ""


def test_the_three_old_copies_are_gone_and_re_export_the_shared_one() -> None:
    """The copies are DELETED, not left beside the new module to drift again. ``validate`` and
    ``seeds`` keep their public names — callers and the existing tests still import those — but
    both must now be the shared function itself, not a look-alike."""
    from oraclous_ohm._slug import tool_slug
    from oraclous_ohm.compiler.validate import _tool_slug
    from oraclous_ohm.seeds import catalog_slug

    assert _tool_slug is tool_slug
    assert catalog_slug is tool_slug
