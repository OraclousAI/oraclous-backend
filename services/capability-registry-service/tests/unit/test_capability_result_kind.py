"""Unit: #804 — a capability declares what its result is (`result_kind`; §CITE rev6, #776).

rev3 of §CITE required a tool to declare "whether it returns *assertable content* or only a
*status*, so that action tools are never graded", and then never gave that declaration a home, a
name or a set of values. rev6 (`oraclous-knowledge` `flows/interface-contracts.md`, §CITE-QUAL
"`result_kind` — the declaration itself") names it and moves it from the tool to the **capability**:
one tool exposes a ``read_file`` that cites next to a ``list_files`` that does not.

Three values, because the declaration has three consumers and a boolean serves only the first:

* ``status``     — an outcome, not content. Never graded, never minted for.
* ``single``     — the content of ONE identified document. One citation per result.
* ``collection`` — MANY content items, each with its own identity. One citation PER ITEM.

Absent is **undeclared**, and rev6 decision 16 is explicit that it is never read as ``status``.
That direction is fail-open: an ungraded content-returning tool would declare itself an action tool,
skip §CITE-QUAL grading entirely, and let a member read from it and assert uncited.

**Every operation is pinned, and that is the point of the table below.** An earlier revision of this
file pinned only the values §CITE quotes and left the rest to a closed-set check. `be-test-reviewer`
rejected it: twelve content-returning operations could all have been declared ``status`` with the
suite green, which is exactly the one direction the Contract calls dangerous. The table now carries
every operation, from the ruling on #804.

The criterion behind the table, restated so the next connector needs no new ruling — §CITE-QUAL
Limit 1's own words are that action tools "have no source to cite", not that they return no bytes:

    A capability returns content when its result names something that exists independently of
    the call, which a reader could be pointed at. Otherwise it returns a status.

Two corollaries carry most of the list. **A listing is a status** — a list of names, paths or ids
supports only "these things exist", and that claim's source is the connector call, not a document.
**An operation is classified by what it is, not by what it currently emits** — a content-returning
operation with no ``SourceRef`` is a defect in that connector (tracked as #808, and #786 for
``Read.read``), never grounds to downgrade it to ``status``.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_capability_registry_service.domain.libraries import registry as library_registry
from oraclous_capability_registry_service.domain.plugins import plugin_registry

pytestmark = pytest.mark.unit

#: The closed value set (§CITE-QUAL, rev6). Absent is a fourth state and is NOT a member.
RESULT_KINDS = {"status", "single", "collection"}

#: Every operation in the first-party catalogue, keyed by (tool display name, operation name).
#: 33 operations across 25 plugins. Values ruled on #804 on 2026-08-15.
RULED: dict[tuple[str, str], str] = {
    # --- status: the result reports what the platform did, or lists identifiers without content --
    # §CITE-QUAL Limit 1 names these four acts verbatim: "open a pull request, send a message,
    # write a file, deliver a report".
    ("GitHub Sink", "deliver"): "status",
    ("Send to Drafts", "send"): "status",
    ("Write", "write"): "status",
    ("Edit", "edit"): "status",
    # Listings. A list of names carries no content to assert from.
    ("GitHub Reader", "list_files"): "status",
    ("PostgreSQL Reader", "list_tables"): "status",
    ("MySQL Reader", "list_tables"): "status",
    ("Notion Reader", "search"): "status",
    ("Glob", "glob"): "status",
    # The platform reporting on its own work.
    ("Bash", "bash"): "status",
    ("Graph Ingest", "ingest"): "status",
    ("Script Ingestion", "run"): "status",
    ("Manifest Validate", "validate_manifest"): "status",
    ("Manifest Refine", "refine_manifest"): "status",
    # Transforms of input the caller already holds.
    ("Text Tools", "word_count"): "status",
    ("Text Tools", "to_upper"): "status",
    ("Text Tools", "extract_emails"): "status",
    # --- single: one identified document ------------------------------------------------------
    ("GitHub Reader", "read_file"): "single",
    ("Notion Reader", "read_page"): "single",
    ("Read", "read"): "single",
    ("Web Research", "read"): "single",
    ("Web Research", "fetch"): "single",
    ("WebFetch", "fetch"): "single",
    ("REST Connector", "fetch"): "single",
    # --- collection: many content items, each with its own identity -----------------------------
    ("Web Research", "search"): "collection",
    ("WebSearch", "search"): "collection",
    ("Knowledge Retriever", "search"): "collection",
    ("Find Similar", "find_similar"): "collection",
    ("Federated Search", "federated_search"): "collection",
    ("Recall Memory", "recall_memory"): "collection",
    ("PostgreSQL Reader", "query"): "collection",
    ("MySQL Reader", "query"): "collection",
    ("Grep", "grep"): "collection",
}


def _operations() -> dict[tuple[str, str], dict[str, Any]]:
    """Every first-party capability entry, keyed by (tool display name, operation name).

    The key is asserted unique. Two plugins sharing a display name would otherwise let one
    silently overwrite the other, and an operation that never reaches the dict is an operation
    the completeness check below cannot see.
    """
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for plugin in plugin_registry.discover():
        descriptor = plugin.descriptor()
        tool = descriptor["metadata"]["name"]
        for operation in descriptor["spec"]["capabilities"]:
            key = (tool, operation["name"])
            assert key not in found, f"two plugins expose {key}; the catalogue key is not unique"
            found[key] = operation
    return found


def _kind_of(tool: str, operation: str) -> Any:
    operations = _operations()
    assert (tool, operation) in operations, f"{tool}.{operation} is not in the catalogue"
    return operations[(tool, operation)].get("result_kind")


# --------------------------------------------------------------------------------------
# The table is the specification
# --------------------------------------------------------------------------------------


def test_the_ruled_table_covers_the_catalogue_exactly() -> None:
    """The table and the catalogue must agree in both directions.

    A new connector arriving with no ruled value fails here rather than passing on a permissive
    closed-set check, and a table entry for an operation that no longer exists fails too. This is
    the guard that keeps the pinning honest as the catalogue grows.
    """
    catalogue = set(_operations())
    assert catalogue - set(RULED) == set(), "catalogue operations with no ruled result_kind"
    assert set(RULED) - catalogue == set(), "ruled operations that are not in the catalogue"
    assert len(catalogue) == 33, f"expected 33 operations, found {len(catalogue)}"


@pytest.mark.parametrize(("tool", "operation"), sorted(RULED), ids=lambda p: p)
def test_each_capability_declares_its_ruled_result_kind(tool: str, operation: str) -> None:
    assert _kind_of(tool, operation) == RULED[(tool, operation)]


def test_every_declared_result_kind_is_in_the_closed_set() -> None:
    """Cheap backstop. The table above is the real constraint; this catches a typo'd value."""
    wrong = {
        f"{tool}.{operation}": entry.get("result_kind")
        for (tool, operation), entry in _operations().items()
        if entry.get("result_kind") not in RESULT_KINDS
    }
    assert wrong == {}, f"result_kind outside {sorted(RESULT_KINDS)}: {wrong}"


def test_the_generated_library_group_operations_declare_one_too() -> None:
    """``LibraryGroupPlugin`` builds its capabilities from the library registry, not a literal.

    The generated site is a second place the field has to be added; a fix applied only to
    ``builtin.py`` leaves this plugin undeclared while every other one passes.
    """
    generated = library_registry.capabilities()
    assert generated, "the library registry declares no operations at all"
    for entry in generated:
        assert entry.get("result_kind") in RESULT_KINDS, entry


def test_a_tool_declares_differently_across_its_own_operations() -> None:
    """The property that makes this per-capability rather than per-tool (#776).

    GitHub Reader exposes a read that reports its source and a listing that does not. A
    tool-level declaration cannot express that, which is why §CITE-QUAL Limit 1's "a tool
    declares" wording was amended in rev6. Redundant against the table, and kept because the
    table alone does not say *why* two entries for one tool differ.
    """
    assert _kind_of("GitHub Reader", "read_file") == "single"
    assert _kind_of("GitHub Reader", "list_files") == "status"


def test_no_content_returning_operation_is_declared_a_status() -> None:
    """The fail-open direction, asserted as its own named test rather than left implied.

    Under-claiming skips §CITE-QUAL grading *and* skips minting, so a member reads content and
    asserts from it uncited. `Read.read` is the sharpest case: an agent writes a file, a later
    member reads it back off the org-shared sandbox, and nothing is recorded.
    """
    content_returning = [key for key, kind in RULED.items() if kind != "status"]
    assert len(content_returning) == 16
    downgraded = [
        f"{tool}.{operation}"
        for (tool, operation) in content_returning
        if _kind_of(tool, operation) == "status"
    ]
    assert downgraded == [], f"content-returning operations declared as a status: {downgraded}"


# --------------------------------------------------------------------------------------
# An MCP import declares nothing, and nothing fills it in
# --------------------------------------------------------------------------------------


def _imported_operation(**tool_extra: Any) -> dict[str, Any]:
    from oraclous_capability_registry_service.services.mcp_import_service import _operation_for

    tool = {"name": "pull_request_read", "description": "Read a PR", **tool_extra}
    return _operation_for(tool, "pull_request_read")


def test_an_mcp_import_declares_no_result_kind() -> None:
    """An MCP server tells us nothing about its result shape, and we do not invent one.

    Inventing a value here is the exact move §CITE forbids everywhere else: the platform
    declares what it knows, and discovers the rest. What an imported tool actually returns is
    determined at connect time by the §CITE-QUAL grade (#744), not asserted at import.
    """
    assert "result_kind" not in _imported_operation()


def test_an_mcp_import_declares_nothing_even_when_the_server_offers_one() -> None:
    """A hostile or merely confused server cannot smuggle a declaration into our catalogue."""
    assert "result_kind" not in _imported_operation(result_kind="single")


def test_an_absent_result_kind_survives_the_descriptor_dtos_uncoerced() -> None:
    """Decision 16: absent is undeclared. It must not become ``status`` on any read or write.

    The DTOs carry the descriptor as an untyped ``dict[str, Any]``, so today nothing could
    default it. This pins that, because the obvious implementation of "make mypy happy" is a
    typed model with ``result_kind: str = "status"``, and that single default is fail-open.
    """
    from oraclous_capability_registry_service.models.enums import DescriptorKind
    from oraclous_capability_registry_service.schema.capability_schema import (
        CreateCapability,
        UpdateCapability,
    )

    descriptor = {
        "kind": "tool",
        "metadata": {"name": "github-mcp-pull_request_read", "description": "Read a PR"},
        "spec": {
            "type": "mcp",
            "server_url": "https://api.githubcopilot.com/mcp/",
            "tool_name": "pull_request_read",
            "label": "github-mcp",
            "capabilities": [_imported_operation()],
        },
    }

    created = CreateCapability(kind=DescriptorKind.TOOL, descriptor=descriptor)
    updated = UpdateCapability(descriptor=descriptor)
    for dto in (created, updated):
        entry = dto.descriptor["spec"]["capabilities"][0]
        assert "result_kind" not in entry, f"{type(dto).__name__} filled in a result_kind"


def test_validation_does_not_invent_a_result_kind() -> None:
    """``validate_descriptor`` is the one place every descriptor passes through on write."""
    from oraclous_capability_registry_service.domain.manifest import validate_descriptor
    from oraclous_capability_registry_service.models.enums import DescriptorKind

    descriptor: dict[str, Any] = {
        "kind": "tool",
        "metadata": {"name": "github-mcp-pull_request_read", "description": "Read a PR"},
        "spec": {"type": "mcp", "capabilities": [_imported_operation()]},
    }
    validate_descriptor(DescriptorKind.TOOL, descriptor)
    assert "result_kind" not in descriptor["spec"]["capabilities"][0]
