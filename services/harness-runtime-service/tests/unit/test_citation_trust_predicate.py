"""#780 item 1 (security) — citation trust must key on an IDENTITY, not on a display name.

``_build_runnable`` decides which of a manifest's binding aliases the loop may believe when a tool
result carries a reserved key. #779 moved that decision off ``ToolSpec.binding`` — the manifest
author's free choice — and onto the resolved registry row's own ``name``, slugified, matched against
``_CITATION_MINTING_CAPABILITIES``. That was the right direction and it is not far enough.

**A registry row's name is a display string, not an identity.** For an imported MCP tool it is
``mcp_capability_name(label, tool_name)`` = ``<admin label>-<server tool name>``, and both halves
are chosen outside the platform. An org admin importing a server under the label ``knowledge`` with
a tool named ``retriever`` stores the name ``knowledge-retriever``, whose slug is exactly the
first-party retriever's. ``resolve_capability`` matches by name slug with first-match-wins, so such
a row can in principle resolve ``core/knowledge-retriever@1.0.0`` — and would then be believed.

**Why the tests below must not lean on ordering.** The collision does not bite today, for two
reasons that are incidental rather than stated: ``list_by_kind`` orders by ``created_at`` ascending,
so a seeded row precedes a later import, and ``_dedupe_prefer_caller`` dedupes by descriptor id
rather than by name. Both were verified at the security gate on #779. A test that arranges the rows
so the first-party one comes first would pass on the accident and prove nothing, so
``test_a_name_colliding_mcp_row_is_untrusted_when_it_wins_the_resolution`` hands the collider to the
service **as the resolved row** — the state the ordering accident is currently preventing.

The fix is #780's own option 1: require ``descriptor["spec"]["type"] == "INTERNAL"`` alongside the
name slug. MCP rows carry ``spec.type == "mcp"``, so the collision closes outright, and the runtime
gains no registry UUIDs (option 2's cost).

**Second reason this file exists.** ``security-architect`` noted on #780 that ``citation_bindings``
is derived by code that no test in the repository covers — the loop's behaviour given a set is well
covered, the computation of the set is not, and the deployed e2e exercises the positive half only
because the member happens to bind the retriever under an alias. Both halves are pinned here.

The service returns the two trust sets as one named value (``.citation`` / ``.data_absence``, the
#781 split), which does not exist yet — so every test here hard-fails RED until the ``[impl]``
lands. Module-level imports are shipped seams, so collection stays clean
(``.claude/rules/tests-seam-imports.md``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.policy import resolve_policy_set
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = [pytest.mark.unit, pytest.mark.security]

_ORG = uuid.uuid4()

_SEARCH_OPERATION = {
    "name": "search",
    "description": "Search a knowledge graph and return the matching nodes.",
    "parameters": {"query": "str"},
}


def _row(*, row_id: str, name: str, spec_type: str) -> dict[str, Any]:
    """A registry tool item as ``resolve_capability`` returns it: id + display name + descriptor."""
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


# The seeded first-party retriever: `KnowledgeRetrieverPlugin.NAME` is "Knowledge Retriever" and its
# TYPE is "INTERNAL" (capability-registry-service/domain/plugins/builtin.py).
_FIRST_PARTY = _row(row_id="cap-kr", name="Knowledge Retriever", spec_type="INTERNAL")
# The collision #780 describes, built the way the registry would build it: an admin imports a server
# under the label "knowledge" carrying a tool named "retriever", and `mcp_capability_name` stores
# `knowledge-retriever`. The slug is identical to the first-party row's; only `spec.type` differs.
_MCP_COLLIDER = _row(row_id="cap-mcp", name="knowledge-retriever", spec_type="mcp")
# A first-party row that mints nothing — the ordinary case a trust set must exclude on its name.
_INGEST = _row(row_id="cap-ingest", name="Graph Ingest", spec_type="INTERNAL")


class _Registry:
    """A registry fake: no pre-existing instances, recording the mints ``_materialise`` makes."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def list_instances(self) -> list[dict[str, Any]]:
        return []

    async def create_instance(
        self, *, capability_id: str, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        self.created.append({"capability_id": capability_id, "name": name})
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(
        self, instance_id: uuid.UUID, mappings: dict[str, str]
    ) -> dict[str, Any]:
        return {}


def _manifest(*bindings: tuple[str, str]) -> OHMManifest:
    """One capability per (ref, binding) pair, in the order given."""
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[OHMCapability(ref=ref, binding=binding) for ref, binding in bindings],
        runtime=OHMRuntime(entrypoint=bindings[0][1]),
    )


def _service() -> HarnessExecutionService:
    return HarnessExecutionService(
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


async def _trust(manifest: OHMManifest, resolved: dict[str, dict[str, Any]]) -> Any:
    """Run the real ``_build_runnable`` over a canned resolution and return its trust sets.

    ``_resolve_all`` is the registry round-trip and ``_build_llm`` costs a client, so both are
    replaced; everything between them — including the trust derivation under test — is the shipped
    code path, not a re-implementation of it.
    """
    service = _service()

    async def _resolve_all(_manifest: Any) -> dict[str, dict[str, Any]]:  # noqa: ANN401
        return resolved

    async def _build_llm(_manifest: Any, _org_id: Any) -> Any:  # noqa: ANN401
        return object()

    service._resolve_all = _resolve_all  # type: ignore[method-assign]
    service._build_llm = _build_llm  # type: ignore[method-assign]
    *_, trust = await service._build_runnable(manifest, resolve_policy_set(None), _ORG)
    return trust


# --- the positive half: an arbitrary alias over a real retriever row IS trusted --------------


async def test_a_first_party_retriever_is_trusted_under_whatever_alias_the_manifest_chose() -> None:
    # The property #779 was built for, and the one the derivation has never had a test for. The
    # manifest binds the retriever as "Read" — which is what the deployed harness actually does —
    # so a trust set keyed on the literal capability name would be empty and citations would stop
    # minting. Trust names the ALIAS, derived from the resolved row's own name.
    manifest = _manifest(("core/knowledge-retriever@1.0.0", "Read"))
    trust = await _trust(manifest, {"Read": _FIRST_PARTY})
    assert trust.citation == frozenset({"Read"})
    assert trust.data_absence == frozenset({"Read"})  # #781: the retriever emits `data_absent`


async def test_a_first_party_row_that_mints_nothing_is_absent_from_both_sets() -> None:
    # The ordinary negative. Being INTERNAL is necessary, never sufficient: the name slug still has
    # to be one of the minting capabilities, or every first-party tool in a manifest would be
    # believed about citations it never serves.
    manifest = _manifest(("core/graph-ingest@1.0.0", "Write"))
    trust = await _trust(manifest, {"Write": _INGEST})
    assert trust.citation == frozenset()
    assert trust.data_absence == frozenset()


# --- the negative half: a name-colliding MCP row is NOT trusted ------------------------------


async def test_a_name_colliding_mcp_row_is_untrusted_when_it_wins_the_resolution() -> None:
    # #780 item 1, and the regression test for it. The row's stored name slugs to exactly
    # `knowledge-retriever`, so the name predicate alone accepts it. It is an MCP import
    # (`spec.type == "mcp"`), whose result is whatever a remote server returned — the reserved keys
    # would be attacker-controlled.
    #
    # Handed in AS the resolved row on purpose. Today's defence is that `list_by_kind` orders by
    # `created_at` so the seeded row resolves first; that is an accident of ordering, not an
    # invariant, and #746 removes the "seeded rows are always oldest" property it rests on. The
    # trust predicate has to hold when the accident does not.
    manifest = _manifest(("core/knowledge-retriever@1.0.0", "retriever"))
    trust = await _trust(manifest, {"retriever": _MCP_COLLIDER})
    assert trust.citation == frozenset()
    assert trust.data_absence == frozenset()


async def test_the_collider_is_untrusted_even_beside_a_genuine_retriever() -> None:
    # Both rows in one run, the collider bound FIRST so no iteration order can rescue the result.
    # The genuine binding keeps its trust and the collider gets none — the predicate discriminates
    # per row, rather than deciding once for the manifest.
    manifest = _manifest(
        ("core/knowledge-retriever@1.0.0", "notes"),
        ("core/knowledge-retriever@1.0.0", "Read"),
    )
    trust = await _trust(manifest, {"notes": _MCP_COLLIDER, "Read": _FIRST_PARTY})
    assert trust.citation == frozenset({"Read"})
    assert trust.data_absence == frozenset({"Read"})
