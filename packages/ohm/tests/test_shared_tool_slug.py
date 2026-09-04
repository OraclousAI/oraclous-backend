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


def test_the_old_copies_are_gone_and_re_export_the_shared_one() -> None:
    """The copies are DELETED, not left beside the new module to drift again. ``validate`` and
    ``seeds`` keep their public names — callers and the existing tests still import those — but
    both must now be the shared function itself, not a look-alike."""
    from oraclous_ohm._slug import tool_slug
    from oraclous_ohm.compiler.validate import _tool_slug
    from oraclous_ohm.seeds import catalog_slug

    assert _tool_slug is tool_slug
    assert catalog_slug is tool_slug


def test_both_on_ramps_answer_identically_on_the_cases_that_used_to_differ() -> None:
    """The identity check above passes for a re-export; this one would still catch a re-inlined
    copy that happens to be assigned the same public name. Every case here is one where a
    hand-written twin could plausibly diverge."""
    from oraclous_ohm.compiler.validate import _tool_slug
    from oraclous_ohm.seeds import catalog_slug

    for value in (
        "Write",
        "write",
        "core/graph-ingest@1.0.0",
        "Graph Ingest",
        "evil/web-research",
        "😈/web-research",
        "core//x",
        "",
        "@",
    ):
        assert _tool_slug(value) == catalog_slug(value), value


def test_the_leaf_module_imports_nothing_from_the_package() -> None:
    """This is what makes the shared home possible at all. ``compiler.validate`` imports
    ``import_``, so ``import_.mapping`` cannot import ``compiler.validate`` back — that cycle is
    why three copies existed. A leaf with no intra-package imports has no cycle to close."""
    import ast
    import pathlib

    import oraclous_ohm._slug as slug_module

    source = pathlib.Path(slug_module.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        module = getattr(node, "module", None) or ""
        if isinstance(node, ast.ImportFrom):
            assert not module.startswith("oraclous_ohm"), module
            assert node.level == 0, "no relative import either"
        if isinstance(node, ast.Import):
            assert all(not a.name.startswith("oraclous_ohm") for a in node.names)


# ── the membership sets moved to the leaf too (#694, [impl] review M2) ────────────────────────
#
# The normaliser's consolidation fixed callers disagreeing about what a tool is CALLED. The same
# slice then wrote the file-tool set out in three modules and stated the same partition a fourth
# time in the remap table, which is the identical failure one layer along: callers disagreeing
# about what a file tool IS.


def test_the_membership_sets_live_beside_the_normaliser() -> None:
    from oraclous_ohm._slug import (
        FILE_SUBSTRATE_READ_TOOLS,
        FILE_SUBSTRATE_TOOLS,
        FILE_SUBSTRATE_WRITE_TOOLS,
        GRAPH_READ_TOOLS,
        GRAPH_WRITE_TOOLS,
    )

    assert FILE_SUBSTRATE_WRITE_TOOLS == {"write", "edit"}
    assert FILE_SUBSTRATE_READ_TOOLS == {"read", "grep", "glob"}
    assert FILE_SUBSTRATE_TOOLS == FILE_SUBSTRATE_WRITE_TOOLS | FILE_SUBSTRATE_READ_TOOLS
    assert GRAPH_WRITE_TOOLS == {"graph-ingest"}
    assert GRAPH_READ_TOOLS == {"knowledge-retriever", "find-similar", "recall-memory"}
    assert "bash" not in FILE_SUBSTRATE_TOOLS  # the exec fallback is not a deliverable sink (#507)
    assert not FILE_SUBSTRATE_TOOLS & (GRAPH_WRITE_TOOLS | GRAPH_READ_TOOLS)


def test_every_consumer_reads_the_leaf_rather_than_its_own_copy() -> None:
    """Identity, not equality: an equal-but-separate copy is exactly what drifts."""
    from oraclous_execution_engine_service.domain import compiler_onramp
    from oraclous_execution_engine_service.services import team_run
    from oraclous_ohm._slug import (
        FILE_SUBSTRATE_READ_TOOLS,
        FILE_SUBSTRATE_TOOLS,
        FILE_SUBSTRATE_WRITE_TOOLS,
        GRAPH_READ_TOOLS,
        GRAPH_WRITE_TOOLS,
    )
    from oraclous_ohm.compiler import validate

    assert validate._FILE_SUBSTRATE_TOOLS is FILE_SUBSTRATE_TOOLS
    assert compiler_onramp._FILE_SUBSTRATE_TOOLS is FILE_SUBSTRATE_TOOLS
    assert team_run._SANDBOX_WRITE_TOOLS is FILE_SUBSTRATE_WRITE_TOOLS
    assert team_run._SANDBOX_READ_TOOLS is FILE_SUBSTRATE_READ_TOOLS
    assert team_run._GRAPH_WRITE_TOOLS is GRAPH_WRITE_TOOLS
    assert team_run._GRAPH_READ_TOOLS is GRAPH_READ_TOOLS


def test_the_remap_table_covers_every_file_tool_the_leaf_names() -> None:
    """The fourth statement of the partition. A file tool the remap does not carry falls through
    to a provisional ``core/<slug>@1`` — which is #694's own failure, silently."""
    from oraclous_ohm._slug import FILE_SUBSTRATE_TOOLS, GRAPH_READ_TOOLS, GRAPH_WRITE_TOOLS
    from oraclous_ohm.import_.mapping import _GRAPH_REMAP

    assert FILE_SUBSTRATE_TOOLS <= set(_GRAPH_REMAP)
    assert GRAPH_WRITE_TOOLS | GRAPH_READ_TOOLS <= set(_GRAPH_REMAP)
    # and every target is a SEEDED ref, never a provisional @1
    assert all(not ref.endswith("@1") for ref in _GRAPH_REMAP.values()), _GRAPH_REMAP


# ── #731: the FIVE remaining plain copies collapse onto one shared primitive ──────────────────────
#
# #694 already collapsed three copies of ``tool_slug`` onto this module. #731 is the same fix one
# rung down: ``mapping.slugify``, ``registry_client._slug``/``capability_slug``,
# ``mcp_descriptor_shape.resolution_slug`` and auth's ``organisations.slugify`` are five more
# hand-written twins of the same ``_basic_slug`` body. The owner ruling (2026-08-24) is: one shared
# implementation, every current call site repointed at it, stored names NOT rewritten.
#
# The issue's own acceptance criterion ("a foreign namespace normalises identically in every
# module... pinned across all readers") is FALSE against the design: three readers
# (``tool_slug``, ``registry_client._ref_slug``, ``policy._registry_of``) are DELIBERATELY
# different from the plain primitive and from each other, and are already test-pinned that way
# (``test_a_foreign_namespace_can_never_masquerade_as_a_bare_surveyed_tool`` above,
# ``test_registry_client.py``, ``test_policy.py``). So this slice pins three corrected properties
# instead: (1) every PLAIN reader is one function, (2) the deliberate differences are DECLARED, not
# silently three-way inconsistent, (3) sharing the primitive never widens the #594 masquerade gate.


def test_the_shared_module_exports_basic_slug() -> None:
    """The promoted-public primitive #731's five copies repoint at. Function-local: ``basic_slug``
    does not exist yet, only the private ``_basic_slug``."""
    import oraclous_ohm._slug as slug_module
    from oraclous_ohm._slug import basic_slug

    assert callable(basic_slug)
    assert "basic_slug" in slug_module.__all__


# A corpus chosen to plausibly diverge between five independently hand-written twins: leading/
# trailing whitespace, an existing separator, mixed case + spaces, an emoji segment, a doubled
# separator, the empty string, an all-punctuation string, an all-separator string, an
# already-collapsed run, and other whitespace (tab/newline).
_PLAIN_CORPUS = [
    "  Web Research  ",
    "github-mcp/read",
    "Google Drive Reader",
    "😈/web-research",
    "core//x",
    "",
    "@",
    "___",
    "a--b",
    "\tfoo\n",
]


@pytest.mark.parametrize("value", _PLAIN_CORPUS)
def test_every_plain_reader_agrees_with_the_shared_primitive(value: str) -> None:
    """``mapping.slugify``, ``registry_client._slug``, ``registry_client.capability_slug`` and
    ``mcp_descriptor_shape.resolution_slug`` are all extensionally identical to ``_basic_slug``
    today (five hand-written copies of one function). #731 repoints all five at ``basic_slug`` —
    proved here by comparing each to the shared primitive's OWN answer, not a hand-derived literal,
    so this test cannot itself be wrong about what the primitive computes."""
    from oraclous_capability_registry_service.domain.mcp_descriptor_shape import resolution_slug
    from oraclous_harness_runtime_service.services.registry_client import _slug, capability_slug
    from oraclous_ohm._slug import basic_slug
    from oraclous_ohm.import_.mapping import slugify as mapping_slugify

    expected = basic_slug(value)
    assert mapping_slugify(value) == expected, "import_.mapping.slugify"
    assert _slug(value) == expected, "registry_client._slug"
    assert capability_slug(value) == expected, "registry_client.capability_slug"
    assert resolution_slug(value) == expected, "mcp_descriptor_shape.resolution_slug"


@pytest.mark.security
def test_the_deliberately_different_readers_are_declared_not_unified() -> None:
    """The corrected form of #731's stated acceptance criterion. ``evil/web-research`` answers FOUR
    different questions today, on purpose:

    * ``basic_slug``      — what does this text look like, slugified?           (no gate)
    * ``tool_slug``       — is this a bare surveyed tool, or a namespace? (#594 masquerade gate)
    * ``_ref_slug``       — which registry ROW does this ref's TAIL name?      (server-side match)
    * ``_registry_of``    — which registry does this ref's HEAD belong to?     (policy allow-list)

    A test asserting these are equal would have to break three already-shipped, already-pinned
    behaviours (see this module's own masquerade test above, ``test_registry_client.py``'s
    ``test_the_old_slashed_name_is_pinned_as_unresolvable``-adjacent tail semantics, and
    ``test_policy.py``'s allow-list matching). Declaring them here means a future change to any one
    of the four has to explain itself against this table, not slip through silently."""
    from oraclous_harness_runtime_service.domain.policy import _registry_of
    from oraclous_harness_runtime_service.services.registry_client import _ref_slug
    from oraclous_ohm._slug import basic_slug, tool_slug

    value = "evil/web-research"
    readers = {
        "basic_slug — plain text, no gate": (basic_slug, "evil-web-research"),
        "tool_slug — #594 masquerade gate, foreign namespace marked": (
            tool_slug,
            "ns--evil--web-research",
        ),
        "_ref_slug — registry TAIL match (server-validated row name)": (_ref_slug, "web-research"),
        "_registry_of — registry HEAD match (policy allow-list), never hyphenated": (
            _registry_of,
            "evil",
        ),
    }
    for description, (reader, expected) in readers.items():
        assert reader(value) == expected, description
    answers = {expected for _, expected in readers.values()}
    assert len(answers) == 4, "the four readers must answer four DIFFERENT questions, not converge"


@pytest.mark.security
def test_sharing_the_primitive_never_widens_the_594_masquerade_gate() -> None:
    """#731 widens WHERE ``basic_slug`` is called from (five more sites); it must never widen WHAT
    ``tool_slug``'s own #594 gate protects. Carried over unchanged by the collapse."""
    from oraclous_ohm._slug import tool_slug

    result = tool_slug("evil/web-research")
    assert result.startswith("ns--")
    assert result != "web-research"
    for degenerate in ("./web-research", "😈/web-research", "core//web-research"):
        assert tool_slug(degenerate) == ""
