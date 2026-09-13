"""#900 — per-run ``OHMCapability.resolved_schema`` threading into the model-facing tool spec
(ADR-053 decision 1), its single-operation gate, and that it never reaches the database.

ADR-053 decision 1: ``OHMCapability.resolved_schema`` (``dict[str, Any] | None = None``) carries a
per-run, per-organisation JSON Schema for a capability's tool-call parameters, computed by the
compiler, which OVERRIDES the descriptor's own schema for that run when present. The field itself
is pinned in ``packages/ohm/tests/test_resolved_schema_manifest.py`` (a sibling #900 commit) — this
file is about what happens to it inside ``harness-runtime-service``, which owns none of that
pinning.

Two rules given directly alongside the ADR, not yet built anywhere:

* the override applies only when the descriptor declares EXACTLY ONE operation in
  ``spec.capabilities``; two or more operations means the override is ignored (with a warning),
  never applied to "the first one" or to all of them;
* it must never be persisted — creating/reusing a registry instance must never write
  ``resolved_schema`` (or the schema's contents) into the stored ``configuration`` row.

The mechanism is presumably: ``_materialise`` (``harness_execution_service.py``) injects
``cap.resolved_schema`` as the existing per-operation ``descriptor["spec"]["capabilities"][i]
["parameters_schema"]`` key — a DIFFERENT, already-implemented override that
``_parameters_for``'s priority-1 branch (``tool_schemas.py:148-150``) already returns unchanged,
outright, over everything else. That existing priority-1 behaviour ("a per-operation
``parameters_schema`` wins outright, closed, with its own ``required``") is already exhaustively
pinned in ``test_tool_schemas.py`` (``test_bound_config_does_not_alter_a_per_op_parameters_schema_
override``, ``test_a_per_op_parameters_schema_still_wins_outright_over_the_plugin_level_
projection``, and others) — this file does not duplicate that coverage. What is NOT pinned
anywhere yet is the wiring from ``cap.resolved_schema`` (the manifest-level, per-run field) INTO
that descriptor-level key, which is entirely #900's gap and this file's subject.

Follows the fake-registry / fake-provenance / ``_service()`` idiom of
``test_tool_schema_bound_config.py`` verbatim (itself copied from ``test_org_instance_reuse.py``).
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

# Same real shape as ``test_tool_schema_bound_config.py``'s ``_RECALL_DESCRIPTOR``: ONE operation,
# ``graph_id`` + ``query`` both declared required on ``spec.input_schema``.
_SINGLE_OP_DESCRIPTOR = {
    "id": "core-recall-memory-single",
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

# The EXACT schema ``_SINGLE_OP_DESCRIPTOR`` projects today with nothing bound — the byte-identical
# baseline test 2 pins against. Computed by hand from ``_project_input_schema``'s rules: both
# hint-map keys are declared on ``input_schema``, both are required and neither is bound, so both
# properties travel verbatim and both stay in ``required``.
_SINGLE_OP_PROJECTED_TODAY = {
    "type": "object",
    "properties": {
        "graph_id": {"type": "string", "format": "uuid"},
        "query": {"type": "string", "minLength": 1},
    },
    "required": ["graph_id", "query"],
    "additionalProperties": False,
}

# Two operations declared on ``spec.capabilities`` — the single-operation gate must refuse to apply
# an override here.
_MULTI_OP_DESCRIPTOR = {
    "id": "core-recall-memory-multi",
    "metadata": {"name": "Recall Memory (multi-op)"},
    "spec": {
        "type": "MEMORY",
        "capabilities": [
            {
                "name": "recall_memory",
                "description": "Recall from a knowledge graph",
                "parameters": {"graph_id": "str", "query": "str"},
            },
            {
                "name": "forget_memory",
                "description": "Forget from a knowledge graph",
                "parameters": {"graph_id": "str"},
            },
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

_RESOLVED_SINGLE = {
    "recall-memory": {"id": _SINGLE_OP_DESCRIPTOR["id"], "descriptor": _SINGLE_OP_DESCRIPTOR}
}
_RESOLVED_MULTI = {
    "recall-memory": {"id": _MULTI_OP_DESCRIPTOR["id"], "descriptor": _MULTI_OP_DESCRIPTOR}
}

# A per-run override that disagrees with everything the descriptor would otherwise project — so an
# equality assertion against it can never pass by coincidence.
_OVERRIDE_SCHEMA = {
    "type": "object",
    "properties": {"compiled_arg": {"type": "string"}},
    "required": ["compiled_arg"],
    "additionalProperties": False,
}

# A nontrivial, catalogue-shaped schema for the "never reaches the database" test — several
# properties, so a leak of even a fragment of it (not just the whole dict) would be visible.
_CATALOGUE_SHAPED_SCHEMA = {
    "type": "object",
    "properties": {
        "graph_id": {"type": "string"},
        "query": {"type": "string"},
        "web_search__search": {"type": "object"},
        "file_write__write": {"type": "object"},
    },
    "required": ["graph_id", "query"],
    "additionalProperties": False,
}


class _Registry:
    """A registry fake: canned ``list_instances`` rows + recording create/configure calls.

    Copied verbatim (shape-for-shape) from ``test_tool_schema_bound_config.py``'s ``_Registry``,
    itself copied from ``test_org_instance_reuse.py``.
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
    """#826 cleanup double: a real recording no-op instead of ``None`` against a non-optional
    ``provenance: ProvenanceCollector`` parameter. This test never inspects emissions."""

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


def _manifest_with_resolved_schema(resolved_schema: dict[str, Any] | None) -> OHMManifest:
    """A one-capability manifest built the NEW way — passing ``resolved_schema`` through to
    ``OHMCapability``. Today (``resolved_schema`` not yet a declared field, pydantic
    ``extra="ignore"``) construction succeeds but silently drops the kwarg — see
    ``packages/ohm/tests/test_resolved_schema_manifest.py`` for that field-level pin. This helper
    exists only so the RUNTIME-level tests below read the same way they will once the field lands.
    """
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[
            OHMCapability(
                ref="core/recall-memory@1.0.0",
                binding="recall-memory",
                resolved_schema=resolved_schema,
            )
        ],
        runtime=OHMRuntime(entrypoint="recall-memory"),
    )


def _manifest_old_style() -> OHMManifest:
    """Exactly how every existing call site builds a capability today — no ``resolved_schema``
    kwarg at all."""
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[OHMCapability(ref="core/recall-memory@1.0.0", binding="recall-memory")],
        runtime=OHMRuntime(entrypoint="recall-memory"),
    )


def _spec_for(tool_specs: list[ToolSpec], operation: str) -> ToolSpec:
    for spec in tool_specs:
        if spec.operation == operation:
            return spec
    raise AssertionError(f"no ToolSpec for operation {operation!r} in {tool_specs!r}")


# ── 1. single-operation gate: the override IS threaded when the descriptor declares one op ──────


async def test_a_resolved_schema_override_reaches_the_toolspec_on_one_operation() -> None:
    """RED today: nothing in ``_materialise`` reads ``cap.resolved_schema`` at all, so the
    resulting ``ToolSpec.parameters`` is ``_SINGLE_OP_PROJECTED_TODAY`` (the ordinary
    ``spec.input_schema`` projection), never ``_OVERRIDE_SCHEMA`` — the two are built to disagree
    on every key so this cannot pass by coincidence.

    ``[impl]`` must inject ``cap.resolved_schema`` as this op's ``parameters_schema`` key on the
    descriptor BEFORE ``tool_specs_for`` runs, so ``_parameters_for``'s existing, unmodified
    priority-1 branch (``tool_schemas.py:148-150``) returns it unchanged."""
    registry = _Registry([])
    manifest = _manifest_with_resolved_schema(_OVERRIDE_SCHEMA)
    _, tool_specs = await _service(registry)._materialise(manifest, _RESOLVED_SINGLE)
    spec = _spec_for(tool_specs, "recall_memory")
    assert spec.parameters == _OVERRIDE_SCHEMA


# ── 2. byte-identical when absent (regression pin) ───────────────────────────────────────────


async def test_no_resolved_schema_is_byte_identical_to_todays_projection() -> None:
    """GREEN today: a capability built the OLD way carries no ``resolved_schema`` at all (the
    field doesn't exist yet), so nothing changes — this pins the EXACT dict
    ``_SINGLE_OP_DESCRIPTOR`` produces today so a future implementer cannot regress the "absent
    means unchanged" half of ADR-053 decision 1 while building the threading in test 1."""
    registry = _Registry([])
    manifest = _manifest_old_style()
    _, tool_specs = await _service(registry)._materialise(manifest, _RESOLVED_SINGLE)
    spec = _spec_for(tool_specs, "recall_memory")
    assert spec.parameters == _SINGLE_OP_PROJECTED_TODAY


# ── 3. refused (not silently mis-applied) on a multi-operation descriptor ───────────────────


async def test_a_resolved_schema_is_ignored_when_the_descriptor_declares_two_operations() -> None:
    """Pins the INTENDED refusal (CLAUDE.md §3.5 fail-closed default: ambiguous → ignore, not
    guess). ``[impl]`` must not apply ``cap.resolved_schema`` to "the first operation" or to every
    operation when ``spec.capabilities`` has more than one entry.

    GREEN today, but VACUOUSLY: nothing threads ``resolved_schema`` in regardless of operation
    count today, so both operations already use the descriptor's own per-operation schemas
    unconditionally. This test cannot yet distinguish "correctly refused" from "never attempted" —
    that distinction only becomes real once test 1's single-operation case goes green; until then,
    this test's job is purely to catch a NAIVE ``[impl]`` that applies the override to
    ``spec.capabilities[0]`` unconditionally (which would flip ``recall_memory``'s parameters to
    ``_OVERRIDE_SCHEMA`` and turn this red)."""
    registry = _Registry([])
    manifest = _manifest_with_resolved_schema(_OVERRIDE_SCHEMA)
    _, tool_specs = await _service(registry)._materialise(manifest, _RESOLVED_MULTI)
    recall_spec = _spec_for(tool_specs, "recall_memory")
    forget_spec = _spec_for(tool_specs, "forget_memory")
    assert recall_spec.parameters != _OVERRIDE_SCHEMA
    assert forget_spec.parameters != _OVERRIDE_SCHEMA
    assert recall_spec.parameters == _SINGLE_OP_PROJECTED_TODAY
    assert forget_spec.parameters == {
        "type": "object",
        "properties": {"graph_id": {"type": "string", "format": "uuid"}},
        "required": ["graph_id"],
        "additionalProperties": False,
    }


# ── 4. never reaches the database ────────────────────────────────────────────────────────────


async def test_resolved_schema_never_reaches_the_persisted_configuration() -> None:
    """ADR-053's rejected-alternative rationale for ``OHMCapability.config`` verbatim: 'every
    compile would write a copy of the calling organisation's whole tool catalogue into the
    database'. Asserts the payload actually sent to ``create_instance(configuration=...)`` never
    carries ``resolved_schema`` or any fragment of the catalogue-shaped schema.

    GREEN today: ``cap_config`` is built exclusively from ``cap.config`` (``harness_execution_
    service.py``'s fresh-mint branch), which never contains ``resolved_schema`` regardless of what
    is set on the manifest capability object. This pins the guarantee so a future implementer
    cannot silently start merging ``cap.resolved_schema`` into ``cap_config`` while wiring test 1's
    threading — the injection point is the DESCRIPTOR's op dict, never the persisted
    configuration."""
    registry = _Registry([])
    manifest = _manifest_with_resolved_schema(_CATALOGUE_SHAPED_SCHEMA)
    await _service(registry)._materialise(manifest, _RESOLVED_SINGLE)
    assert len(registry.created) == 1
    configuration = registry.created[0]["configuration"]
    assert "resolved_schema" not in configuration
    for leaked_key in _CATALOGUE_SHAPED_SCHEMA["properties"]:
        assert leaked_key not in configuration
    assert configuration == {}


async def test_resolved_schema_never_reaches_configure_credentials_either() -> None:
    """Companion to the test above: when the manifest ALSO carries credential mappings (so
    ``configure_credentials`` fires), the schema still never rides along in that call either —
    it is a two-argument call (``instance_id``, ``mappings``) with no schema-shaped slot at all,
    so this is a structural guarantee, not a behavioural one; pinned anyway so the "never reaches
    the database" claim covers both registry-mutating calls ``_materialise`` makes, not just one.

    GREEN today for the same reason as the test above."""
    registry = _Registry([])
    manifest = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[
            OHMCapability(
                ref="core/recall-memory@1.0.0",
                binding="recall-memory",
                config={"credential_mappings": {"api_key": "cred-1"}},
                resolved_schema=_CATALOGUE_SHAPED_SCHEMA,
            )
        ],
        runtime=OHMRuntime(entrypoint="recall-memory"),
    )
    await _service(registry)._materialise(manifest, _RESOLVED_SINGLE)
    assert len(registry.configured) == 1
    _, mappings = registry.configured[0]
    assert mappings == {"api_key": "cred-1"}
    assert "resolved_schema" not in mappings


# ── 5. a resolved-schema-derived tool spec is marked strict ─────────────────────────────────
#
# UPDATED 2026-09-13 against #898's real, now-known mechanism (its [tests] PRs #1057/#1060 merged
# to `main` at 3b124cac; its [impl] PR #1059 has not). #898 does NOT add a bare ``strict=True``
# kwarg straight onto ``ToolSpec`` inferred from a schema's shape — strictness is carried
# EXPLICITLY, on the DESCRIPTOR'S OP, as a sibling key to ``parameters_schema`` named
# ``parameters_schema_strict`` (confirmed verbatim in #898's own
# ``test_first_party_declared_schema.py``: ``test_the_explicit_marker_makes_a_first_party_declared_
# override_strict``, ``test_a_declared_schema_with_no_explicit_strict_marker_stays_non_strict``).
# ``tool_specs_for``/``_parameters_for`` reading that marker and setting the resulting
# ``ToolSpec.strict`` is #898's own scope and is pinned in #898's own suite, not duplicated here.
#
# What is uniquely #900's gap, and what this test pins: ``_materialise``'s threading of
# ``cap.resolved_schema`` onto the descriptor's op (test 1, above) must ALSO set
# ``parameters_schema_strict: True`` on that SAME op — never just the schema alone. Skipping the
# marker would silently leave #900's authored override NON-strict (per #898's own "no explicit
# marker → stays non-strict, regardless of how closed the schema looks" rule), which is exactly the
# fail-open #898 was built to close. This is the reason the issue brief calls out explicitly: "use
# it rather than inventing a parallel mechanism" — there is no separate #900-owned carrier.


async def test_the_threaded_override_also_carries_the_explicit_strict_marker() -> None:
    """RED for two independent, converging reasons — neither is #900 inventing new machinery:

    1. #900's own gap (this file's whole subject): nothing in ``_materialise`` threads
       ``cap.resolved_schema`` onto the descriptor's op at all yet (test 1).
    2. Even once it does, ``[impl]`` must remember to ALSO set the sibling
       ``parameters_schema_strict: True`` key #898 defines — the schema alone is not enough.

    Asserting ``spec.strict is True`` additionally depends on #898's own ``[impl]`` (PR #1059,
    not yet merged) actually reading that marker inside ``tool_specs_for`` — today ``ToolSpec`` has
    no ``strict`` field at all (confirmed: ``grep -n strict`` on this branch's ``llm/base.py`` finds
    nothing), so the attribute access itself raises ``AttributeError`` before the marker-check can
    even matter. Whichever of the two lands first, this test only goes green once BOTH #900's
    threading AND #898's marker-reading are in place together — which is the correct, narrow claim
    the issue brief actually makes ("a specification carrying a resolved schema is marked strict"),
    not a claim #900 can satisfy alone."""
    registry = _Registry([])
    manifest = _manifest_with_resolved_schema(_OVERRIDE_SCHEMA)
    _, tool_specs = await _service(registry)._materialise(manifest, _RESOLVED_SINGLE)
    spec = _spec_for(tool_specs, "recall_memory")
    assert spec.parameters == _OVERRIDE_SCHEMA
    assert spec.strict is True  # AttributeError today — ToolSpec has no `strict` field (#898)


def test_a_first_party_override_with_no_marker_stays_non_strict_the_898_way() -> None:
    """Regression guard, using #898's OWN already-real mechanism directly (no #900 involved): a
    descriptor op that carries a ``parameters_schema`` override but NO ``parameters_schema_strict``
    key stays non-strict, however closed/required the schema looks — mirrors #898's own
    ``test_a_declared_schema_with_no_explicit_strict_marker_stays_non_strict`` verbatim, run again
    here so a reader of THIS file sees the contrast with the test above without having to cross-
    reference another service's test suite. RED today for the same root reason: ``ToolSpec`` has no
    ``strict`` field to read at all yet (#898's own [impl], PR #1059, not merged)."""
    from oraclous_harness_runtime_service.domain.tool_schemas import tool_specs_for

    descriptor = {
        "kind": "tool",
        "metadata": {"name": "Recall Memory"},
        "spec": {
            "type": "MEMORY",
            "capabilities": [
                {
                    "name": "recall_memory",
                    "description": "Recall from a knowledge graph",
                    "parameters": {"graph_id": "str", "query": "str"},
                    "parameters_schema": _OVERRIDE_SCHEMA,
                    # deliberately no "parameters_schema_strict" key
                }
            ],
        },
    }
    spec = tool_specs_for("recall-memory", descriptor)[0]
    assert spec.parameters == _OVERRIDE_SCHEMA
    assert spec.strict is False
