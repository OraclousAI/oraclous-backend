"""Unit: #698 D4 — the rewrite that migration 0008 applies to already-imported MCP descriptors.

Existing rows were stored as ``metadata.name = "<label>/<tool>"``, a shape no reference can name
(see ``test_registry_client.py``). Migration ``0008_mcp_descriptor_shape`` moves them to
``"<label>-<tool>"`` with the label preserved in ``spec.label``.

The rewrite is pinned here as a PURE function so it can be tested without a database, and so the
migration and the importer cannot drift apart. The seam is
``oraclous_capability_registry_service.domain.mcp_descriptor_shape`` — imported function-locally
(`.claude/rules/tests-seam-imports.md`) because the ``[impl]`` PR has not built it yet.

Three fields, not one. A descriptor rewrite that touches only the JSONB leaves the migration
looking successful while resolution stays broken:

* ``descriptor.metadata.name`` — what the importer writes;
* the denormalised ``name`` COLUMN (``descriptor_name()``) — what ``resolve_capability`` reads;
* ``content_hash`` — auto-computed from the descriptor on every write, and stale afterwards.

Measured on the deployed stack before writing this: 44 imported descriptors, all in one org, none
carrying a stored ``inputSchema``. So the migration is RENAME-ONLY. It must NOT invent
``spec.capabilities``, because a descriptor with a fabricated empty schema looks callable and is
not — those rows have to be re-imported to gain a real schema.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _rewrite(descriptor: dict[str, Any]) -> dict[str, Any]:
    """The seam under test. Function-local import: RED until the ``[impl]`` lands, never a skip."""
    from oraclous_capability_registry_service.domain.mcp_descriptor_shape import (
        rewrite_mcp_descriptor,
    )

    return rewrite_mcp_descriptor(descriptor)


def _legacy(name: str = "github-mcp/pull_request_read", **spec_extra: Any) -> dict[str, Any]:
    return {
        "kind": "tool",
        "metadata": {"name": name, "description": "Read a PR"},
        "spec": {
            "type": "mcp",
            "server_url": "https://api.githubcopilot.com/mcp/",
            "tool_name": "pull_request_read",
            "credential_id": "cred-1",
            **spec_extra,
        },
    }


def test_the_slash_becomes_a_hyphen() -> None:
    assert _rewrite(_legacy())["metadata"]["name"] == "github-mcp-pull_request_read"


def test_the_label_is_recovered_into_the_spec() -> None:
    assert _rewrite(_legacy())["spec"]["label"] == "github-mcp"


def test_a_tool_name_containing_a_slash_splits_on_the_FIRST_one_only() -> None:
    """``<label>/<tool>`` where the tool itself holds a slash. The label is everything before the
    first separator; the rest is the tool name, and every remaining slash must still go."""
    rewritten = _rewrite(_legacy(name="acme/a/b"))
    assert rewritten["spec"]["label"] == "acme"
    assert "/" not in rewritten["metadata"]["name"]


def test_the_rewrite_is_idempotent() -> None:
    """Alembic can be re-run, and a partially applied migration must be safe to replay."""
    once = _rewrite(_legacy())
    assert _rewrite(once) == once


def test_an_already_migrated_descriptor_keeps_its_label() -> None:
    already = _legacy(name="github-mcp-pull_request_read", label="github-mcp")
    assert _rewrite(already)["spec"]["label"] == "github-mcp"


def test_a_name_with_no_slash_and_no_label_is_left_alone() -> None:
    """A row with nothing to split must not gain a fabricated label or a mangled name."""
    odd = _legacy(name="loose_name")
    rewritten = _rewrite(odd)
    assert rewritten["metadata"]["name"] == "loose_name"
    assert rewritten["spec"].get("label") in (None, "", "loose_name")


def test_a_non_mcp_descriptor_is_returned_untouched() -> None:
    builtin = {
        "kind": "tool",
        "metadata": {"name": "core/web-research"},
        "spec": {"type": "web_research", "capabilities": [{"name": "search"}]},
    }
    assert _rewrite(builtin) == builtin


def test_the_rewrite_never_invents_capabilities() -> None:
    """The measured reality: no stored ``inputSchema`` exists to backfill from. A fabricated empty
    schema would make a dead tool look callable, so these rows stay schema-less and re-import."""
    rewritten = _rewrite(_legacy())
    assert "capabilities" not in rewritten["spec"]


def test_the_rewrite_preserves_every_other_spec_field() -> None:
    rewritten = _rewrite(_legacy())
    assert rewritten["spec"]["server_url"] == "https://api.githubcopilot.com/mcp/"
    assert rewritten["spec"]["tool_name"] == "pull_request_read"
    assert rewritten["spec"]["credential_id"] == "cred-1"
    assert rewritten["kind"] == "tool"
    assert rewritten["metadata"]["description"] == "Read a PR"


def test_the_rewrite_does_not_mutate_its_input() -> None:
    """The migration reads a row, rewrites it, and writes it back. An in-place mutation would make
    a mid-batch failure leave half-rewritten dictionaries behind."""
    original = _legacy()
    _rewrite(original)
    assert original["metadata"]["name"] == "github-mcp/pull_request_read"


def test_the_rewritten_name_still_fits_the_column() -> None:
    long_name = "l" * 200 + "/" + "t" * 200
    assert len(_rewrite(_legacy(name=long_name))["metadata"]["name"]) <= 255


# ── The two derived fields the migration must also rewrite ───────────────────────────────────────


def test_the_denormalised_name_column_is_derived_from_the_rewritten_descriptor() -> None:
    """``resolve_capability`` matches against the COLUMN, not the JSONB. If the migration updates
    only the descriptor, every migrated tool stays unresolvable and the migration looks fine."""
    from oraclous_capability_registry_service.domain.manifest import descriptor_name

    assert descriptor_name(_rewrite(_legacy())) == "github-mcp-pull_request_read"


def test_the_content_hash_changes_with_the_rewrite() -> None:
    """``content_hash`` is auto-computed on write. A migration that leaves the old hash in place
    breaks the content-hash-mismatch check (CLAUDE.md §3.5) for every migrated row."""
    from oraclous_capability_registry_service.domain.hashing import compute_content_hash

    legacy = _legacy()
    assert compute_content_hash(_rewrite(legacy)) != compute_content_hash(legacy)


# ── Collision detection ──────────────────────────────────────────────────────────────────────────
#
# There is no unique constraint or unique index on ``name``, so the database cannot catch a
# post-rewrite duplicate. Two labels differing only by a separator collapse to the same name, and
# ``resolve_capability`` would then silently pick whichever row it saw first. Measured as zero on
# the deployed stack, but nothing prevents it from arising later.


def test_two_legacy_names_that_collapse_to_one_are_detectable() -> None:
    a = _rewrite(_legacy(name="acme-prod/read"))
    b = _rewrite(_legacy(name="acme/prod-read"))
    assert a["metadata"]["name"] == b["metadata"]["name"]  # the collision is real and reachable


def test_the_collision_helper_reports_duplicates_within_an_org() -> None:
    from oraclous_capability_registry_service.domain.mcp_descriptor_shape import (
        find_name_collisions,
    )

    names = ["acme-prod-read", "acme-prod-read", "github-mcp-read"]
    assert find_name_collisions(names) == {"acme-prod-read"}


def test_the_collision_helper_is_quiet_when_names_are_unique() -> None:
    from oraclous_capability_registry_service.domain.mcp_descriptor_shape import (
        find_name_collisions,
    )

    assert find_name_collisions(["a-b", "c-d"]) == set()
