"""Registry client (slice 3 hardening): ref↔capability_id binding closes the allocation bypass."""

from __future__ import annotations

import httpx
import pytest
from oraclous_harness_runtime_service.services.registry_client import RegistryClient, RegistryError

pytestmark = pytest.mark.unit

# Two registry tools: a benign echo and a (governance-forbidden) shell-exec.
_TOOLS = {
    "capabilities": [
        {"id": "11111111-1111-1111-1111-111111111111", "name": "Echo", "descriptor": {}},
        {"id": "22222222-2222-2222-2222-222222222222", "name": "Shell Exec", "descriptor": {}},
    ]
}


def _client() -> RegistryClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/tools":
            return httpx.Response(200, json=_TOOLS)
        return httpx.Response(404, json={"detail": "not found"})

    return RegistryClient("http://registry", headers={}, transport=httpx.MockTransport(handler))


async def test_resolve_by_ref_name() -> None:
    item = await (_client()).resolve_capability("core/echo@1.0.0")
    assert item["id"] == "11111111-1111-1111-1111-111111111111"


async def test_capability_id_must_match_the_ref_name() -> None:
    # benign ref "echo" but capability_id points at shell-exec → rejected (no allocation bypass).
    with pytest.raises(RegistryError):
        await (_client()).resolve_capability(
            "core/echo@1.0.0", explicit_id="22222222-2222-2222-2222-222222222222"
        )


async def test_capability_id_matching_ref_is_accepted() -> None:
    item = await (_client()).resolve_capability(
        "core/echo@1.0.0", explicit_id="11111111-1111-1111-1111-111111111111"
    )
    assert item["name"] == "Echo"


# ── #698 D4: an imported MCP tool must have a ref shape that actually resolves ────────────────────
#
# ``_ref_slug`` keeps the tail after the LAST "/" and slugifies it. A descriptor stored as
# "github-mcp/pull_request_read" therefore slugifies to "github-mcp-pull-request-read" while any
# ref naming it resolves to just "pull-request-read" — they never match and the run fails closed at
# capability resolution. Removing the "/" from the stored name makes the two agree.
# Shape confirmation is tracked as a Contract on #699.

_ORG_ID = "00000000-0000-0000-0000-0000000000aa"
_MCP_ID = "33333333-3333-3333-3333-333333333333"


def _mcp_client(stored_name: str) -> RegistryClient:
    tools = {
        "capabilities": [
            {"id": _MCP_ID, "kind": "tool", "name": stored_name, "descriptor": {}},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/tools":
            return httpx.Response(200, json=tools)
        return httpx.Response(404, json={"detail": "not found"})

    return RegistryClient("http://registry", headers={}, transport=httpx.MockTransport(handler))


async def test_the_documented_mcp_ref_resolves_to_the_imported_tool() -> None:
    """The shape a manifest author is told to write, against the name the importer stores."""
    client = _mcp_client("github-mcp-pull_request_read")
    item = await client.resolve_capability(f"org:{_ORG_ID}/github-mcp-pull_request_read")
    assert item["id"] == _MCP_ID


async def test_the_documented_mcp_ref_resolves_with_a_version_suffix() -> None:
    client = _mcp_client("github-mcp-pull_request_read")
    item = await client.resolve_capability(f"org:{_ORG_ID}/github-mcp-pull_request_read@1.0.0")
    assert item["id"] == _MCP_ID


async def test_the_old_slashed_name_is_pinned_as_unresolvable() -> None:
    """The regression itself. This is what shipped, and it is why no imported tool was callable —
    if a future change makes this pass, the two matching rules have silently drifted again."""
    client = _mcp_client("github-mcp/pull_request_read")
    with pytest.raises(RegistryError):
        await client.resolve_capability(f"org:{_ORG_ID}/github-mcp/pull_request_read")


async def test_two_servers_exposing_the_same_tool_name_stay_distinguishable() -> None:
    """The label is what separates ``github-mcp/read`` from ``jira-mcp/read``. Folding it into the
    name must keep both resolvable to their own descriptor, not collapse them."""
    tools = {
        "capabilities": [
            {"id": _MCP_ID, "kind": "tool", "name": "github-mcp-read", "descriptor": {}},
            {"id": "44444444-4444-4444-4444-444444444444", "name": "jira-mcp-read", "kind": "tool"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=tools)

    client = RegistryClient("http://r", headers={}, transport=httpx.MockTransport(handler))
    assert (await client.resolve_capability(f"org:{_ORG_ID}/github-mcp-read"))["id"] == _MCP_ID
    jira = await client.resolve_capability(f"org:{_ORG_ID}/jira-mcp-read")
    assert jira["id"] == "44444444-4444-4444-4444-444444444444"


# ── #731: ``_slug``/``capability_slug`` repoint at the primitive; ``_ref_slug`` stays as-is ───────
#
# ``_slug`` is one of the five plain copies #731 collapses onto ``oraclous_ohm._slug.basic_slug``.
# ``_ref_slug`` is deliberately DIFFERENT (it keeps only the TAIL after the last "/" — the shape
# that matches a registry row's own server-validated name) and must NOT be touched; it is pinned
# here as "the primitive applied to the already-split tail" so a future change that folds the
# tail-split INTO the primitive itself would be caught.

_TAIL_CORPUS = [
    "core/postgresql-reader@1.0.0",
    f"org:{_ORG_ID}/github-mcp-pull_request_read@1.0.0",
    "evil/web-research",
    "  Spaced Name  ",
    "@",
    "",
]


@pytest.mark.parametrize("ref", _TAIL_CORPUS)
def test_ref_slug_is_the_shared_primitive_applied_to_the_tail(ref: str) -> None:
    """RED until the [impl] adds ``oraclous_ohm._slug.basic_slug`` — function-local import."""
    from oraclous_harness_runtime_service.services.registry_client import _ref_slug
    from oraclous_ohm._slug import basic_slug

    tail = ref.split("/")[-1].split("@")[0]
    assert _ref_slug(ref) == basic_slug(tail)


def test_ref_slug_still_gives_web_research_for_evil_web_research() -> None:
    """The value the harness-runtime CLAUDE.md map pins explicitly: repointing ``_slug`` must not
    change what ``_ref_slug`` returns for a foreign-namespace ref."""
    from oraclous_harness_runtime_service.services.registry_client import _ref_slug

    assert _ref_slug("evil/web-research") == "web-research"


_PLAIN_SLUG_CORPUS = [
    "  Web Research  ",
    "Echo",
    "Shell Exec",
    "😈/web-research",
    "core//x",
    "",
    "@",
]


@pytest.mark.parametrize("value", _PLAIN_SLUG_CORPUS)
def test_slug_and_capability_slug_are_the_shared_primitive(value: str) -> None:
    """``_slug`` and its public wrapper ``capability_slug`` (used to match a registry row's OWN
    name) must both give the shared primitive's own answer."""
    from oraclous_harness_runtime_service.services.registry_client import _slug, capability_slug
    from oraclous_ohm._slug import basic_slug

    expected = basic_slug(value)
    assert _slug(value) == expected
    assert capability_slug(value) == expected
