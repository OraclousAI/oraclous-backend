"""Unit: the dispatching instance's effective configuration is threaded into the model-facing
schema, so a bound argument is never asked of the model twice (#911 call-site half).

``_materialise`` has exactly three branches that decide which registry instance a capability
binds to, and each has a different source for "the dispatching instance's effective
configuration":

1. a reused DETERMINISTIC prior instance (``prior is not None`` branch) —
   ``prior["configuration"]``;
2. a reused ORG SIBLING instance (#663 ``elif needed and not all(...)`` branch) —
   ``sibling["configuration"]``;
3. a FRESH MINT (``else`` branch) — the inline ``cap_config`` dict, already carrying
   ``working_dir``/``graph_id``/``precedence``/producer keys by the time ``create_instance`` runs.

The fix under test (not written here) makes all three call
``tool_specs_for(cap.binding, descriptor, bound_config=<that branch's effective configuration>)``
so a declared-required argument that is ALREADY bound on the instance drops out of the model-facing
``required`` list — while the property itself stays, so the model still understands the tool's
shape. Today (as of `main`) none of the three branches pass ``bound_config`` at all, and
``tool_specs_for`` does not accept the keyword yet, and ALSO does not yet project
``spec.input_schema`` onto the operation at all (#911's other half, covered in
``test_tool_schemas.py``) — so ``required`` is unconditionally ``[]`` regardless of what the
descriptor declares. Every assertion below that expects a NON-empty ``required`` is RED against
today's code for that reason: it needs BOTH #911 halves (the input_schema projection AND this
call-site threading) to land together before it can go green. See each test's docstring for the
precise failure this predicts.

Follows the fake-registry / fake-provenance / ``_service()`` idiom of
``test_org_instance_reuse.py`` and ``test_precedence_instance_binding.py`` verbatim.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()

# RecallMemoryPlugin's real shape (confirmed): one operation, ``graph_id`` + ``query``, both
# declared required on ``spec.input_schema`` — the plugin's real ``INPUT_SCHEMA`` on the wire.
_RECALL_DESCRIPTOR = {
    "id": "core-recall-memory",
    "metadata": {"name": "Recall Memory"},
    "spec": {
        "type": "MEMORY",
        "capabilities": [
            {
                "name": "recall_memory",
                "description": "Recall from a knowledge graph",
                "parameters": {"graph_id": "str", "query": "str"},
            }
        ],
        "input_schema": {
            "type": "object",
            "required": ["graph_id", "query"],
            "properties": {
                "graph_id": {"type": "string", "format": "uuid"},
                "query": {"type": "string", "minLength": 1},
            },
        },
    },
}

# A keyed variant of the same shape, used only to force the #663 org-sibling branch (``needed`` has
# to be non-empty for that branch to fire at all). This credential-requirements + graph_id
# combination is not a real production plugin shape — a keyless first-party connector like
# RecallMemoryPlugin never declares ``credential_requirements`` in production — but the mechanism
# under test (config threading into the projection) does not care which branch minted or found the
# instance, only that its ``configuration``/``cap_config`` reaches ``tool_specs_for``.
_KEYED_RECALL_DESCRIPTOR = {
    "id": "core-recall-memory-keyed",
    "metadata": {"name": "Recall Memory (keyed)"},
    "spec": {
        "type": "MEMORY",
        "capabilities": [
            {
                "name": "recall_memory",
                "description": "Recall from a knowledge graph",
                "parameters": {"graph_id": "str", "query": "str"},
            }
        ],
        "credential_requirements": [{"type": "api_key", "provider": "test", "required": True}],
        "input_schema": {
            "type": "object",
            "required": ["graph_id", "query"],
            "properties": {
                "graph_id": {"type": "string", "format": "uuid"},
                "query": {"type": "string", "minLength": 1},
            },
        },
    },
}

_RESOLVED = {"recall-memory": {"id": _RECALL_DESCRIPTOR["id"], "descriptor": _RECALL_DESCRIPTOR}}
_RESOLVED_KEYED = {
    "recall-memory": {"id": _KEYED_RECALL_DESCRIPTOR["id"], "descriptor": _KEYED_RECALL_DESCRIPTOR}
}


class _Registry:
    """A registry fake: canned ``list_instances`` rows + recording create/configure calls.

    Copied verbatim (shape-for-shape) from ``test_org_instance_reuse.py``'s ``_Registry``.
    """

    def __init__(self, instances: list[dict[str, Any]] | None = None) -> None:
        self.instances = list(instances or [])
        self.created: list[dict[str, Any]] = []
        self.configured: list[tuple[uuid.UUID, dict[str, str]]] = []

    async def list_instances(self) -> list[dict[str, Any]]:
        return list(self.instances)

    async def create_instance(
        self, *, capability_id: str, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        self.created.append(
            {"capability_id": capability_id, "name": name, "configuration": configuration}
        )
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(
        self, instance_id: uuid.UUID, mappings: dict[str, str]
    ) -> dict[str, Any]:
        self.configured.append((instance_id, mappings))
        return {}


class _FakeProvenance:
    """#826 cleanup: a real recording double instead of `None` against a non-optional
    ``provenance: ProvenanceCollector`` parameter — this test never inspects emissions."""

    async def emit(self, record: Any) -> None:
        return None


def _service(registry: _Registry) -> HarnessExecutionService:
    return HarnessExecutionService(
        registry=registry,
        broker=None,
        executions=None,
        assignments=None,
        checkpoints=None,
        provenance=_FakeProvenance(),
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


def _manifest(binding: str = "recall-memory", config: dict[str, Any] | None = None) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[
            OHMCapability(ref="core/recall-memory@1.0.0", binding=binding, config=config or {})
        ],
        runtime=OHMRuntime(entrypoint=binding),
    )


def _spec_for(tool_specs: list[ToolSpec], operation: str) -> ToolSpec:
    for spec in tool_specs:
        if spec.operation == operation:
            return spec
    raise AssertionError(f"no ToolSpec for operation {operation!r} in {tool_specs!r}")


async def test_a_fresh_mint_drops_the_bound_graph_id_from_required_but_keeps_the_property() -> None:
    """The observable end-state (#911 brief, verbatim framing): a capability whose declared schema
    marks ``graph_id`` required, run with a harness that binds ``graph_id``, produces a ``ToolSpec``
    that does not advertise ``graph_id`` as required.

    Fresh-mint branch: no prior/sibling row exists, so ``_materialise`` takes the ``else`` mint
    path and builds ``cap_config`` inline, merging in ``graph_id="g-123"`` (the #524 bind) before
    calling ``create_instance`` — that merged dict is the branch's effective configuration and must
    reach ``tool_specs_for`` as ``bound_config``.

    RED today for two compounding reasons: (1) ``_materialise`` does not pass ``bound_config`` to
    ``tool_specs_for`` at this call site at all, and (2) even if it did, ``tool_specs_for`` does not
    yet project ``spec.input_schema`` onto the operation — so ``required`` is unconditionally ``[]``
    today and this assertion cannot yet distinguish "subtracted" from "never projected". Both
    ``query in required`` (never bound) and the negative ``graph_id not in required`` currently pass
    only by coincidence of the empty list; the real signal is
    ``test_the_same_descriptor_with_nothing_bound_keeps_graph_id_required`` below, which pins the
    contrast this test alone cannot.
    """
    registry = _Registry([])
    manifest = _manifest()
    _, tool_specs = await _service(registry)._materialise(manifest, _RESOLVED, graph_id="g-123")
    spec = _spec_for(tool_specs, "recall_memory")
    assert "graph_id" not in spec.parameters["required"]
    assert "query" in spec.parameters["required"]
    assert "graph_id" in spec.parameters["properties"]  # the property is not removed, only required


async def test_a_fresh_mint_bound_config_is_the_merged_cap_config_sent_to_create_instance() -> None:
    """Companion assertion: the SAME dict the branch sends to ``create_instance`` (carrying
    ``graph_id`` merged in) is what must have reached ``tool_specs_for`` as ``bound_config`` — pins
    that the threading uses the branch's actual effective configuration, not some other value.

    NOTE: unlike every other test in this file, this one is GREEN today — it pins pre-existing
    ``create_instance`` wiring that this change does not touch, not the new ``bound_config``
    threading itself. It lives in this file (rather than a differently-labelled one) because it is
    a direct companion to the RED test right above it and the two are easiest to read together."""
    registry = _Registry([])
    manifest = _manifest()
    await _service(registry)._materialise(manifest, _RESOLVED, graph_id="g-123")
    assert len(registry.created) == 1
    assert registry.created[0]["configuration"]["graph_id"] == "g-123"


async def test_reused_deterministic_prior_instance_drops_its_bound_key_from_required() -> None:
    """Reuse branch 1 (the ``prior is not None`` branch): a prior instance already exists at the
    deterministic name for this manifest+binding, its ``configuration`` (the registry's
    ``InstanceOut.configuration`` field) already carries ``graph_id`` — bound by an earlier run —
    and this capability needs no credential (``recall_memory`` is keyless, so ``needed`` is empty
    and the reuse condition is trivially satisfied). The prior's ``configuration`` must reach
    ``tool_specs_for`` as ``bound_config``.

    RED today: ``_materialise`` never reads ``prior.get("configuration")`` into ``bound_config`` at
    all, compounded by ``tool_specs_for`` not yet projecting ``spec.input_schema`` (see the module
    docstring)."""
    manifest = _manifest()
    prior_row = {
        "id": str(uuid.uuid4()),
        "name": f"harness:{manifest.metadata.id}:recall-memory",
        "capability_id": _RECALL_DESCRIPTOR["id"],
        "status": "READY",
        "required_credentials": [],
        "credential_mappings": {},
        "configuration": {"graph_id": "already-bound-uuid"},
    }
    registry = _Registry([prior_row])
    _, tool_specs = await _service(registry)._materialise(manifest, _RESOLVED)
    spec = _spec_for(tool_specs, "recall_memory")
    assert "graph_id" not in spec.parameters["required"]
    assert "query" in spec.parameters["required"]
    assert "graph_id" in spec.parameters["properties"]
    assert registry.created == []  # confirms the reuse branch fired, not a fresh mint


async def test_reused_org_sibling_instance_drops_its_bound_key_from_required() -> None:
    """Reuse branch 2 (#663, the org-sibling branch): no deterministic prior row exists, so the
    first branch is skipped; the capability is KEYED (``credential_requirements`` set) so ``needed``
    is non-empty and the #663 sibling lookup fires; one sibling row matches on ``capability_id``
    and covers ``needed`` via ``credential_mappings``, and its own ``configuration`` carries
    ``graph_id``. That sibling's ``configuration`` must reach ``tool_specs_for`` as
    ``bound_config``.

    The credential-requirements + ``graph_id`` combination is artificial (see
    ``_KEYED_RECALL_DESCRIPTOR``'s comment) — it exists only to force this specific branch, not to
    model a real plugin.

    RED today for the same two compounding reasons as the other two branches."""
    manifest = _manifest()
    sibling_row = {
        "id": str(uuid.uuid4()),
        "name": "harness:some-other-manifest:recall-memory",  # deliberately NOT this manifest's
        "capability_id": _KEYED_RECALL_DESCRIPTOR["id"],
        "status": "READY",
        "required_credentials": ["api_key"],
        "credential_mappings": {"api_key": "cred-sibling"},
        "configuration": {"graph_id": "sibling-bound-uuid"},
    }
    registry = _Registry([sibling_row])
    _, tool_specs = await _service(registry)._materialise(manifest, _RESOLVED_KEYED)
    spec = _spec_for(tool_specs, "recall_memory")
    assert "graph_id" not in spec.parameters["required"]
    assert "query" in spec.parameters["required"]
    assert "graph_id" in spec.parameters["properties"]
    assert registry.created == []  # confirms the reuse branch fired, not a fresh mint


async def test_the_same_descriptor_with_nothing_bound_keeps_graph_id_required() -> None:
    """Control / contrast case: the identical descriptor, fresh-minted with NO ``graph_id`` bound
    anywhere (no kwarg to ``_materialise``, no prior/sibling row carrying it) — ``required`` must
    still contain ``graph_id``. This proves the subtraction only happens when something is actually
    bound, not a blanket clearing of ``required``.

    RED today, but for a DIFFERENT reason than the other four tests: today ``required`` is always
    ``[]`` regardless of binding (``tool_specs_for`` does not yet project ``spec.input_schema`` at
    all), so this assertion — which needs ``graph_id`` to be PRESENT in ``required`` — fails now for
    the opposite reason it will need to fail for later if the subtraction is ever implemented too
    eagerly. Once the #911 projection lands (with no config-threading yet), this test alone would
    go green; it only stays meaningfully RED-then-GREEN alongside the other four once both halves
    land together."""
    registry = _Registry([])
    manifest = _manifest()
    _, tool_specs = await _service(registry)._materialise(manifest, _RESOLVED)
    spec = _spec_for(tool_specs, "recall_memory")
    assert "graph_id" in spec.parameters["required"]
    assert "query" in spec.parameters["required"]
