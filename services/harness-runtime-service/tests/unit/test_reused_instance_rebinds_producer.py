"""Unit: a reused deterministic tool instance rebinds the CURRENT run's per-run keys (#1130).

A seeded app's graph-ingest sub-harness is named deterministically
(``uuid.uuid5(app_id, role)``, ``execution-engine-service/.../domain/seed_apps/build.py:86``), so
every run of the same organisation's seeded app resolves ``_materialise``'s deterministic-reuse
branch (``prior is not None``, ~1748-1765) onto the SAME registry instance. That branch inherits
``prior["configuration"]`` verbatim and never rebinds ``producer``/``graph_id``/``working_dir``/
``precedence`` to the CURRENT run — so a run's artifacts (graph-ingest reads producer from the
instance's PERSISTED configuration, never from a tool call's own arguments; see
``capability-registry-service/.../domain/connectors/graph_ingest.py``'s ``_producer_config``) are
still stamped with whichever run happened to mint the instance first.

The fix pins here: the reuse branch must MERGE the current call's per-run keys on top of the
prior's stored configuration (keeping everything else, e.g. a manifest-authored config key) and
PUSH that merged configuration back to the registry before dispatch — the schema-facing
``bound_config`` alone is not enough, because the actual dispatch reads the registry's PERSISTED
row, not anything ``_materialise`` computes locally (confirmed:
``capability-registry-service/.../services/tool_execution_service.py``'s
``instance_config = dict(instance.configuration or {})``).

RED today for the plain reason that ``_materialise`` calls no such update on the reuse branch at
all — ``registry.updated`` stays empty. Follows the fake-registry / fake-provenance / ``_service()``
idiom of ``test_org_instance_reuse.py`` and ``test_precedence_instance_binding.py`` verbatim; adds
one new recording method, ``update_configuration``, that a real ``RegistryClient`` does not have
yet either (a new intra-repo seam the ``[impl]`` PR must add, mirroring ``configure_credentials``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()

# A keyless, credential-less descriptor — like the real graph-ingest connector (#728's own
# "internal trust path", `credential_requirements: []`) — so `needed` is empty and the
# deterministic-reuse branch fires unconditionally on a name match.
_DESCRIPTOR = {
    "id": "cap-graph-ingest",
    "metadata": {"name": "Graph Ingest"},
    "spec": {"capabilities": [], "credential_requirements": []},
}
_RESOLVED = {"graph-ingest": {"id": _DESCRIPTOR["id"], "descriptor": _DESCRIPTOR}}

#: A stable id, like a seeded app's `uuid.uuid5(app_id, role)` sub-harness — the same across every
#: run of the same seeded app, which is exactly what makes the deterministic-reuse branch fire on
#: every later run rather than only occasionally.
_STABLE_SUBHARNESS_ID = uuid.uuid5(uuid.NAMESPACE_URL, "validation-desk/synthesizer")


class _Registry:
    """A registry fake: canned ``list_instances`` rows + recording create/configure/update calls.

    Unlike the read-only fakes in ``test_org_instance_reuse.py``/``test_precedence_instance_binding.
    py``, ``create_instance`` and ``update_configuration`` here MUTATE ``self.instances`` — the real
    registry persists both, and the multi-run test below depends on a later ``_materialise`` call
    seeing the PREVIOUS call's write when it re-lists instances, the same way two calls through the
    real HTTP registry would.
    """

    def __init__(self, instances: list[dict[str, Any]] | None = None) -> None:
        self.instances = list(instances or [])
        self.created: list[dict[str, Any]] = []
        self.configured: list[tuple[uuid.UUID, dict[str, str]]] = []
        self.updated: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def list_instances(self) -> list[dict[str, Any]]:
        return list(self.instances)

    async def create_instance(
        self, *, capability_id: str, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        instance_id = str(uuid.uuid4())
        self.instances.append(
            {
                "id": instance_id,
                "name": name,
                "capability_id": capability_id,
                "status": "READY",
                "required_credentials": [],
                "credential_mappings": {},
                "configuration": dict(configuration),
            }
        )
        self.created.append(
            {"capability_id": capability_id, "name": name, "configuration": configuration}
        )
        return {"id": instance_id}

    async def configure_credentials(
        self, instance_id: uuid.UUID, mappings: dict[str, str]
    ) -> dict[str, Any]:
        self.configured.append((instance_id, mappings))
        return {}

    async def update_configuration(
        self, instance_id: uuid.UUID, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        """The seam this fix needs and ``RegistryClient`` does not have yet (RED by absence: the
        production ``_materialise`` calls nothing named this today, so ``self.updated`` never
        grows)."""
        self.updated.append((instance_id, dict(configuration)))
        for row in self.instances:
            if row["id"] == str(instance_id):
                row["configuration"] = dict(configuration)
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


def _manifest(binding: str = "graph-ingest") -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(
            id=_STABLE_SUBHARNESS_ID, name="synthesizer", owner_organization_id=_ORG, kind="agent"
        ),
        capabilities=[OHMCapability(ref="core/graph-ingest@1.0.0", binding=binding, config={})],
        runtime=OHMRuntime(entrypoint=binding),
    )


def _producer(run: str) -> dict[str, Any]:
    return {
        "producer_kind": "team-member",
        "member_role": "Synthesizer",
        "team_run_id": f"run-{run}",
        "execution_id": f"exec-{run}",
    }


async def test_second_run_rebinds_producer_and_every_per_run_key_on_the_reused_instance() -> None:
    """Acceptance 1: reusing the deterministic instance for a NEW run rebinds `producer`,
    `graph_id`, `working_dir` and `precedence` to the CURRENT call's values, keeps a
    manifest-authored config key intact, and drops every trace of the FIRST run's values.

    RED today: `_materialise`'s reuse branch never calls anything to update the registry's stored
    configuration, so `registry.updated` stays empty and the first assert below fails."""
    manifest = _manifest()
    name = f"harness:{manifest.metadata.id}:graph-ingest"
    instance_id = str(uuid.uuid4())
    registry = _Registry(
        [
            {
                "id": instance_id,
                "name": name,
                "capability_id": _DESCRIPTOR["id"],
                "status": "READY",
                "required_credentials": [],
                "credential_mappings": {},
                "configuration": {
                    # producer keys are FLAT on configuration — read that way by the connector's
                    # own `_producer_config` (graph_ingest.py:74-83), never nested.
                    **_producer("1"),
                    "graph_id": "graph-run-1",
                    "working_dir": "/ws/run-1",
                    "precedence": {"order": ["rules"], "graph_authoritative": False},
                    "custom_manifest_key": "keep-me",
                },
            }
        ]
    )

    run2_producer = _producer("2")
    await _service(registry)._materialise(
        manifest,
        _RESOLVED,
        workspace_root="/ws/run-2",
        graph_id="graph-run-2",
        precedence_order=["fresh_rules"],
        graph_authoritative=True,
        producer=run2_producer,
    )

    assert registry.created == []  # the reuse branch fired — no fresh mint
    assert len(registry.updated) == 1, "the reused instance's configuration was never re-pushed"
    updated_id, updated_config = registry.updated[0]
    assert str(updated_id) == instance_id

    for key, value in run2_producer.items():
        assert updated_config[key] == value, f"{key} was not rebound to the second run's value"
    assert updated_config["graph_id"] == "graph-run-2"
    assert updated_config["working_dir"] == "/ws/run-2"
    assert updated_config["precedence"] == {"order": ["fresh_rules"], "graph_authoritative": True}
    assert updated_config["custom_manifest_key"] == "keep-me"  # a merge, not a wholesale replace

    # nothing from the first run survives onto the second run's pushed configuration
    serialized = str(updated_config)
    for stale in ("run-1", "exec-1", "graph-run-1", "/ws/run-1"):
        assert stale not in serialized, f"{stale!r} leaked from the first run into the second"


async def test_a_freshly_minted_instance_still_binds_producer_directly_as_today() -> None:
    """Regression guard: with no prior instance at the deterministic name, `_materialise` takes the
    mint branch — unchanged by this fix — which already binds producer/graph_id/working_dir onto
    the newly created instance's configuration (#728/#524/#518). It must never call the new update
    seam: there is nothing to update."""
    registry = _Registry([])
    manifest = _manifest()
    producer = _producer("1")

    await _service(registry)._materialise(
        manifest,
        _RESOLVED,
        workspace_root="/ws/run-1",
        graph_id="graph-run-1",
        producer=producer,
    )

    assert len(registry.created) == 1
    created_config = registry.created[0]["configuration"]
    for key, value in producer.items():
        assert created_config[key] == value
    assert created_config["graph_id"] == "graph-run-1"
    assert created_config["working_dir"] == "/ws/run-1"
    assert registry.updated == []


async def test_a_third_run_rebinds_again_and_drops_every_earlier_runs_values() -> None:
    """The class recurs on every later run of a seeded app, not just the second (#1130's own title
    says "a later run", not "the second run"). Three successive `_materialise` calls against the
    SAME fake registry (which persists an update the way the real one would) must each push only
    that run's own values — the second run's values must not linger into the third's push either."""
    registry = _Registry([])
    manifest = _manifest()

    async def _run(n: int) -> None:
        await _service(registry)._materialise(
            manifest,
            _RESOLVED,
            workspace_root=f"/ws/run-{n}",
            graph_id=f"graph-run-{n}",
            producer=_producer(str(n)),
        )

    await _run(1)  # fresh mint
    await _run(2)  # reuse — first rebind
    await _run(3)  # reuse — second rebind

    assert len(registry.created) == 1, "only the first run should ever mint an instance"
    assert len(registry.updated) == 2, "runs 2 and 3 must each rebind the same reused instance"

    _, run2_config = registry.updated[0]
    assert run2_config["team_run_id"] == "run-2"

    _, run3_config = registry.updated[1]
    assert run3_config["team_run_id"] == "run-3"
    assert run3_config["execution_id"] == "exec-3"
    assert run3_config["graph_id"] == "graph-run-3"
    assert run3_config["working_dir"] == "/ws/run-3"

    serialized = str(run3_config)
    for stale in ("run-1", "run-2", "exec-1", "exec-2", "graph-run-1", "graph-run-2"):
        assert stale not in serialized, f"{stale!r} leaked into the third run's push"
