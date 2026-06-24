"""Unit: the post-run memory hook writes TEAM scope to the BOUND (adopted) graph (#513, ADR-027).

Graph-adopt team-scope blackboard (E6): a team member's run-outcome memory is written
  * with ``scope="team"`` (not the hardcoded ``"agent"``) and the run's ``team_id`` — so concurrent
    members + future runs of the SAME team share one blackboard (the bitcoin/DoefinGPT world-model);
  * into the run's BOUND ``graph_id`` (the user's existing/adopted graph, threaded via #524) — NOT
    ``_manifest_graph_context`` and NEVER ``resolve_default_graph`` (no second graph is stood up).
A run WITHOUT a team_id keeps the legacy ``scope="agent"`` (back-compat, zero-risk).

The ZERO-RISK contract is preserved: the write is still fire-and-forget — a team write that fails or
is slow can never block/slow/fail the run (asserted in test_memory_hook.py; unchanged here).

RED until #513 [impl] threads ``team_id`` to the memory hook, writes ``MemoryScope.TEAM``, and
points the write at the bound graph_id.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_harness_runtime_service.services.memory_client import (
    MemoryWriter,
    drain_pending_writes,
)
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_DESCRIPTOR = {
    "id": "cap-1",
    "metadata": {"name": "Echo"},
    "spec": {"capabilities": [{"name": "run", "description": "Echo back", "parameters": {}}]},
}


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _manifest() -> dict[str, Any]:
    """A team-member sub-harness with NO manifest graph context — the graph comes from the run."""
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
            "name": "Researcher",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": [{"ref": "core/echo@1.0.0", "binding": "echo"}],
        "prompts": [{"role": "primary", "source": "inline", "body": "You are helpful."}],
        "runtime": {"entrypoint": "echo"},
    }


class _FakeRegistry:
    async def list_tools(self) -> list[dict]:
        return [{"id": "cap-1", "name": "Echo", "descriptor": _DESCRIPTOR}]

    async def resolve_capability(self, ref: str, *, explicit_id: str | None = None) -> dict:
        return {"id": "cap-1", "name": "Echo", "descriptor": _DESCRIPTOR}

    async def list_instances(self) -> list[dict]:
        return []

    async def create_instance(self, *, capability_id: str, name: str, configuration: dict) -> dict:
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(self, instance_id: uuid.UUID, mappings: dict) -> dict:
        return {}

    async def execute(self, instance_id: uuid.UUID, input_data: dict) -> dict:
        return {"status": "SUCCESS", "output_data": {"echo": "ok"}}


class _FakeExecutions:
    async def create(self, **fields: Any) -> SimpleNamespace:
        return SimpleNamespace(id=fields["execution_id"], **fields)


class _FakeProv:
    async def emit(self, record: Any) -> None:
        pass


def _service(memory: MemoryWriter) -> HarnessExecutionService:
    return HarnessExecutionService(
        registry=_FakeRegistry(),
        broker=None,
        executions=_FakeExecutions(),
        assignments=None,
        checkpoints=None,
        provenance=_FakeProv(),
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
        memory=memory,
    )


def _capture_writer(seen: list[dict]) -> MemoryWriter:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({"path": req.url.path, "body": json.loads(req.content.decode())})
        return httpx.Response(201, json={"memory_id": "m1", "importance_score": 0.4})

    return MemoryWriter(
        base_url="http://knowledge-graph-service:8000",
        headers={"X-Internal-Key": "k", "X-Organisation-Id": str(_ORG)},
        timeout=2.0,
        transport=httpx.MockTransport(handler),
    )


async def test_team_run_writes_team_scope_to_the_bound_graph() -> None:
    """A member run bound to a team + an adopted graph writes ``scope=team`` + ``team_id`` into THAT
    graph — never the agent scope, never a second (org-default) graph."""
    seen: list[dict] = []
    svc = _service(_capture_writer(seen))
    graph_id = str(uuid.uuid4())  # the user's EXISTING adopted graph (run binding, #524)
    team_id = str(uuid.uuid4())  # the stable team-manifest id
    await svc.execute(
        manifest_inline=_manifest(),
        manifest_ref=None,
        user_input="gather evidence",
        principal=_principal(),
        graph_id=graph_id,
        team_id=team_id,
    )
    await drain_pending_writes()

    assert len(seen) == 1
    body = seen[0]["body"]
    assert body["scope"] == "team"  # NOT the hardcoded "agent"
    assert body["team_id"] == team_id  # the team identity, so the team's reads find it
    assert body["graph_id"] == graph_id  # the BOUND adopted graph — not the manifest / org-default


async def test_without_a_team_id_the_write_stays_agent_scope() -> None:
    """Back-compat: a non-team run (no team_id) keeps the legacy ``scope=agent`` — zero behaviour
    change for single-agent runs."""
    seen: list[dict] = []
    svc = _service(_capture_writer(seen))
    await svc.execute(
        manifest_inline=_manifest(),
        manifest_ref=None,
        user_input="go",
        principal=_principal(),
    )
    await drain_pending_writes()

    assert len(seen) == 1
    assert seen[0]["body"]["scope"] == "agent"
    assert "team_id" not in seen[0]["body"]
