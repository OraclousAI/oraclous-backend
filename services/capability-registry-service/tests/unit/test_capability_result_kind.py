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

**What these tests do and do not pin.** They pin the invariants (every first-party capability
declares one; the value is in the closed set; an MCP import declares nothing; nothing coerces an
absent value) and the classifications the Contract itself states. They deliberately do NOT pin a
value for every operation in the catalogue — several are genuine judgment calls, and #804 says an
operation you cannot classify is a question for the issue rather than a default. The completeness
test below is what forces each one to be decided.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_capability_registry_service.domain.libraries import registry as library_registry
from oraclous_capability_registry_service.domain.plugins import plugin_registry

pytestmark = pytest.mark.unit

#: The closed value set (§CITE-QUAL, rev6). Absent is a fourth state and is NOT a member.
RESULT_KINDS = {"status", "single", "collection"}


def _operations() -> dict[tuple[str, str], dict[str, Any]]:
    """Every first-party capability entry, keyed by (tool display name, operation name)."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for plugin in plugin_registry.discover():
        descriptor = plugin.descriptor()
        tool = descriptor["metadata"]["name"]
        for operation in descriptor["spec"]["capabilities"]:
            found[(tool, operation["name"])] = operation
    return found


def _kind_of(tool: str, operation: str) -> Any:
    operations = _operations()
    assert (tool, operation) in operations, f"{tool}.{operation} is not in the catalogue"
    return operations[(tool, operation)].get("result_kind")


# --------------------------------------------------------------------------------------
# The invariants
# --------------------------------------------------------------------------------------


def test_every_first_party_capability_declares_a_result_kind() -> None:
    """Completeness. This is the test that forces all 31 operations to be classified.

    An operation with no declaration is refused by the §CITE-QUAL gate and hidden from the
    console's ingest picker, so leaving one out silently retires it rather than shipping it.
    """
    undeclared = [
        f"{tool}.{operation}"
        for (tool, operation), entry in _operations().items()
        if "result_kind" not in entry
    ]
    assert undeclared == [], f"first-party capabilities with no result_kind: {undeclared}"


def test_every_declared_result_kind_is_in_the_closed_set() -> None:
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


# --------------------------------------------------------------------------------------
# The classifications the Contract itself states
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "operation"),
    [
        # The two working readers that emit a SourceRef today (#770; Notion since PR #772).
        # These are the operations the console's ingest picker exists to offer.
        ("GitHub Reader", "read_file"),
        ("Notion Reader", "read_page"),
    ],
)
def test_a_document_read_declares_single(tool: str, operation: str) -> None:
    assert _kind_of(tool, operation) == "single"


@pytest.mark.parametrize(
    ("tool", "operation"),
    [
        # §CITE-QUAL Limit 1 names these four acts verbatim: "open a pull request, send a
        # message, write a file, deliver a report". They have no source to cite.
        ("GitHub Sink", "deliver"),
        ("Send to Drafts", "send"),
        ("Write", "write"),
        ("Edit", "edit"),
    ],
)
def test_an_action_tool_declares_status(tool: str, operation: str) -> None:
    assert _kind_of(tool, operation) == "status"


@pytest.mark.parametrize(
    ("tool", "operation"),
    [
        # §CITE rev6 names `core/web-research.search` as the collection case: one tool result,
        # ten hits, ten sources. WebSearch is the same act under a different plugin.
        ("Web Research", "search"),
        ("WebSearch", "search"),
    ],
)
def test_a_live_web_search_declares_collection(tool: str, operation: str) -> None:
    assert _kind_of(tool, operation) == "collection"


def test_a_tool_may_mix_kinds_across_its_own_operations() -> None:
    """The property that makes this per-capability rather than per-tool (#776).

    GitHub Reader exposes a read that reports its source and a listing that does not. A
    tool-level declaration cannot express that, which is why §CITE-QUAL Limit 1's "a tool
    declares" wording was amended in rev6.
    """
    assert _kind_of("GitHub Reader", "read_file") != _kind_of("GitHub Reader", "list_files")


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
