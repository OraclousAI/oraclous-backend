"""#961 — WHICH tool the site-restriction gate fires on is decided by the registry, never by a name.

The gate refuses a web search that leaves a person's site restriction out (ruling 2). Deciding what
counts as "a web search" is therefore a security question, and it has exactly the shape #780 already
settled for citations:

* A ``ToolSpec.binding`` is the manifest author's own alias. An author who called the first-party
  search anything else would dodge the gate outright — the restriction stops binding the moment
  somebody renames their tool.
* A registry row's ``name`` is a DISPLAY string. For an imported MCP tool it is
  ``<admin label>-<server tool name>``, both halves chosen outside the platform, so an admin can
  store a name whose slug is exactly a first-party one. Trusting the slug alone would fire the gate
  on a remote tool that shares a name — refusing searches on a tool the restriction was never about.

So the same two predicates #780 landed apply here: the resolved row's slugified name is one of the
platform's own web-search capabilities, AND the row is first-party (``spec.type == "INTERNAL"``),
which an imported MCP row can never satisfy.

**Two capabilities, not one.** The platform ships the search under two first-party tools that share
one code path — ``Web Research``'s ``search`` operation and the standard ``WebSearch`` tool. A set
naming only the first would leave every team built from the standard toolset unenforced, which is
the majority case, and nothing would say why.

This mirrors ``test_citation_trust_predicate.py`` deliberately, including its warning about
ordering: a test that arranges the rows so the first-party one wins proves nothing, because the
resolution's ordering is incidental. The collider is handed in AS the resolved row.

RED-by-design until ``TrustedBindings`` carries a ``web_search`` set; the module-level imports are
all shipped seams, so collection stays clean (`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.policy import resolve_policy_set
from oraclous_harness_runtime_service.services.harness_execution_service import (
    _WEB_SEARCH_CAPABILITIES,
    HarnessExecutionService,
)
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = [pytest.mark.unit, pytest.mark.security]

_ORG = uuid.uuid4()

_SEARCH_OPERATION = {
    "name": "search",
    "description": "Search the live web and return ranked hits.",
    "parameters": {"query": "str", "sites": "list"},
}


def _row(*, row_id: str, name: str, spec_type: str) -> dict[str, Any]:
    return {
        "id": row_id,
        "name": name,
        "descriptor": {
            "kind": "tool",
            "id": row_id,
            "metadata": {"name": name},
            "spec": {
                "type": spec_type,
                "capabilities": [_SEARCH_OPERATION],
                "credential_requirements": [],
            },
        },
    }


# #968: the type both search plugins ACTUALLY declare (`builtin.py`: `WebResearchPlugin.TYPE` and
# `WebSearchToolPlugin.TYPE`). This file originally wrote "INTERNAL" here — borrowed from the
# citation predicate's own row — and that one wrong literal is the whole of #968: the positive case
# proved only that the predicate agreed with itself, while the gate was dead on every real run and
# a person's list of websites was ignored. A hand-built row is only worth as much as its fidelity
# to the one the registry really stores.
_REAL_SEARCH_SPEC_TYPE = "API"

# `WebResearchPlugin.NAME` is "Web Research" (slug `web-research`).
_WEB_RESEARCH = _row(row_id="cap-wr", name="Web Research", spec_type=_REAL_SEARCH_SPEC_TYPE)
# `WebSearchToolPlugin.NAME` is the single word "WebSearch" (slug `websearch`) — the standard
# toolset's search, sharing Web Research's code path. Both must be enforced or a whole family of
# teams silently escapes the gate.
_WEBSEARCH_TOOL = _row(row_id="cap-ws", name="WebSearch", spec_type=_REAL_SEARCH_SPEC_TYPE)
# The collision, built the way the registry would: an admin imports a server labelled "web" with a
# tool named "research". Same slug as the first-party row; only `spec.type` differs.
_MCP_COLLIDER = _row(row_id="cap-mcp", name="web-research", spec_type="mcp")
# A first-party row that is not a search at all — the ordinary case the set must exclude.
_FETCH = _row(row_id="cap-wf", name="WebFetch", spec_type=_REAL_SEARCH_SPEC_TYPE)


class _Registry:
    """The minimum registry the trust derivation needs: nothing resolvable, nothing created."""

    async def list_tools(self) -> list[dict[str, Any]]:
        return []

    async def list_instances(self) -> list[dict[str, Any]]:
        return []

    async def create_instance(
        self, *, capability_id: str, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(
        self, instance_id: uuid.UUID, mappings: dict[str, str]
    ) -> dict[str, Any]:
        return {}


def _manifest(*bindings: tuple[str, str]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[OHMCapability(ref=ref, binding=binding) for ref, binding in bindings],
        runtime=OHMRuntime(entrypoint=bindings[0][1]),
    )


async def _trust(manifest: OHMManifest, resolved: dict[str, dict[str, Any]]) -> Any:
    """Run the real ``_build_runnable`` over a canned resolution and return its trust sets."""
    service = HarnessExecutionService(
        registry=_Registry(),
        broker=None,
        executions=None,
        assignments=None,
        checkpoints=None,
        provenance=None,
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
        memory=None,
    )

    async def _resolve_all(_manifest: Any) -> dict[str, dict[str, Any]]:  # noqa: ANN401
        return resolved

    async def _build_llm(_manifest: Any, _org_id: Any) -> Any:  # noqa: ANN401
        return object()

    service._resolve_all = _resolve_all  # type: ignore[method-assign]
    service._build_llm = _build_llm  # type: ignore[method-assign]
    *_, trust = await service._build_runnable(manifest, resolve_policy_set(None), _ORG)
    return trust


async def test_the_web_research_search_is_enforced_under_whatever_alias_it_was_bound() -> None:
    """The positive half. A manifest binding the search as "research" — or as anything else — is
    still the platform's own web search, and the restriction still binds it."""
    trust = await _trust(
        _manifest(("core/web-research@1.0.0", "research")), {"research": _WEB_RESEARCH}
    )

    assert trust.web_search == frozenset({"research"})


async def test_the_standard_websearch_tool_is_enforced_too() -> None:
    """The majority case. Teams built from the standard toolset bind ``WebSearch``, which runs the
    same search path; a set naming only Web Research would leave all of them unenforced."""
    trust = await _trust(
        _manifest(("core/websearch@1", "WebSearch")), {"WebSearch": _WEBSEARCH_TOOL}
    )

    assert trust.web_search == frozenset({"WebSearch"})


async def test_an_mcp_row_that_borrowed_the_name_is_not_enforced() -> None:
    """#780's collision, on this set. The row is handed in AS the resolution, because the ordering
    that hides the collision today is incidental and a test resting on it proves nothing.

    Not being enforced is the right answer for an imported tool, not a loophole: an MCP server's
    search takes whatever arguments that server defines, so a ``sites`` argument the platform
    demanded of it might not exist at all. The gate covers the tools the platform ships.
    """
    trust = await _trust(
        _manifest(("core/web-research@1.0.0", "research")), {"research": _MCP_COLLIDER}
    )

    assert trust.web_search == frozenset()


async def test_a_first_party_tool_that_is_not_a_search_is_not_enforced() -> None:
    """Being first-party is necessary, never sufficient. ``WebFetch`` reads a page the run already
    found; demanding a site restriction of it would refuse the member's own follow-up reads."""
    trust = await _trust(_manifest(("core/webfetch@1", "fetch")), {"fetch": _FETCH})

    assert trust.web_search == frozenset()


async def test_a_first_party_type_this_gate_did_not_expect_is_still_enforced() -> None:
    """#968, the defect in one line: the platform has several first-party descriptor types, and
    this gate must not depend on which one a given plugin picked.

    An allow-list of one type is how the gate shipped dead — the searches are ``API`` and the list
    said ``INTERNAL``. Any non-imported type is enforced now, so a plugin that changes its type, or
    a fourth first-party type nobody has written yet, cannot silently switch the restriction off.
    """
    for spec_type in ("API", "INTERNAL", "CONNECTOR"):
        trust = await _trust(
            _manifest(("core/web-research@1.0.0", "research")),
            {"research": _row(row_id="cap-wr", name="Web Research", spec_type=spec_type)},
        )
        assert trust.web_search == frozenset({"research"}), (
            f"a first-party row typed {spec_type!r} is not enforced, so a person's list of "
            "websites would be ignored on every run using it — #968 exactly"
        )


async def test_the_citation_set_keeps_its_narrower_predicate() -> None:
    """The two sets guard risks pointing in opposite directions, and #968's fix must not level them.

    Believing a row that should not be believed MINTS a forged citation, so that set stays on the
    positive ``INTERNAL`` allow-list. Not believing a row here merely fails to enforce, which is the
    defect being fixed. A retriever row typed anything else is still trusted for nothing.
    """
    trust = await _trust(
        _manifest(("core/knowledge-retriever@1.0.0", "Read")),
        {"Read": _row(row_id="cap-kr", name="Knowledge Retriever", spec_type="API")},
    )

    assert trust.citation == frozenset()
    assert trust.data_absence == frozenset()


async def test_the_three_trust_sets_stay_separate() -> None:
    """#781's rule, extended rather than diluted: each reserved behaviour trusts the capabilities
    that actually carry it. The web search neither mints citations nor flags graph data-absence, and
    a shape that folded the three sets together would silently grant it both."""
    trust = await _trust(
        _manifest(("core/web-research@1.0.0", "research")), {"research": _WEB_RESEARCH}
    )

    assert trust.web_search == frozenset({"research"})
    assert trust.data_absence == frozenset()


def test_the_plugin_names_slugify_to_what_this_gate_matches() -> None:
    """The other half of #968's blind spot, asserted where the slugifier lives.

    The registry suite pins the two plugin NAMES; this pins what they become. Split across the two
    services on purpose: each side asserts the half it can actually change, and neither can drift
    without a red test somewhere.
    """
    from oraclous_harness_runtime_service.services.registry_client import capability_slug

    assert capability_slug("Web Research") in _WEB_SEARCH_CAPABILITIES
    assert capability_slug("WebSearch") in _WEB_SEARCH_CAPABILITIES


def test_the_capability_set_is_exactly_the_two_search_tools() -> None:
    """A third name added here would start enforcing a restriction on a tool nobody decided about.
    Small and explicit, so widening it is a deliberate act."""
    assert _WEB_SEARCH_CAPABILITIES == frozenset({"web-research", "websearch"})
