"""ADR-043 #554 (slice 3/3) PR-2 — wiring the consciousness write into the real run path (the CTO
carry-list): the post-run hook threads the run's tool ERRORS + ROUNDS into the classifier (so the
within-run families ``repetitive_failures`` / ``velocity_anomaly`` go LIVE, not just ``solution``),
and reads the manifest's ``consciousness.permissions`` posture — None → the harness did not opt into
Flow-6 Learn → no enrichment; set → enrich (``can_auto_apply=False``, never auto-apply).

RED until the [impl] adds ``_tool_step_errors`` + threads errors/rounds/posture from ``execute``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_harness_runtime_service.services.memory_client import (
    MemoryWriter,
    drain_pending_writes,
)
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


# ── _tool_step_errors — the coded error extractor for the classifier (fail-soft) ────────────────


def test_tool_step_errors_returns_errored_tool_step_names() -> None:
    from oraclous_harness_runtime_service.domain.loop.tool_use import LoopStep
    from oraclous_harness_runtime_service.models.enums import StepKind
    from oraclous_harness_runtime_service.services.harness_execution_service import (
        _tool_step_errors,
    )

    steps = [
        LoopStep(0, StepKind.TOOL, "read", "error", '{"error": "boom"}'),
        LoopStep(1, StepKind.TOOL, "read", "error", '{"error": "boom"}'),
        LoopStep(2, StepKind.TOOL, "write", "ok", "{}"),
        LoopStep(3, StepKind.LLM, "think", "ok", None),
    ]
    errs = _tool_step_errors(steps)
    # the two errored 'read' steps surface (→ the classifier sees a recurring failure); ok excluded
    assert errs.count("read") == 2
    assert "write" not in errs and "think" not in errs


def test_tool_step_errors_is_fail_soft_on_a_bad_shape() -> None:
    from oraclous_harness_runtime_service.services.harness_execution_service import (
        _tool_step_errors,
    )

    assert _tool_step_errors(["not-a-step"]) == []  # never raises into the run path


# ── the posture gate — consciousness.permissions decides whether the write is enriched ──────────


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _manifest(*, consciousness: str | None) -> dict[str, Any]:
    gov = {"consciousness_permissions": consciousness} if consciousness else {}
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
            "name": "Researcher",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": [{"ref": "core/echo@1.0.0", "binding": "echo"}],
        "prompts": [{"role": "primary", "source": "inline", "body": "You are helpful."}],
        "governance": gov,
        "runtime": {"entrypoint": "echo"},
    }


_DESCRIPTOR = {
    "id": "cap-1",
    "metadata": {"name": "Echo"},
    "spec": {"capabilities": [{"name": "run", "description": "Echo", "parameters": {}}]},
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


def _service(memory: MemoryWriter):
    from oraclous_harness_runtime_service.services.harness_execution_service import (
        HarnessExecutionService,
    )

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
        seen.append(json.loads(req.content.decode()))
        return httpx.Response(201, json={"memory_id": "m1", "importance_score": 0.4})

    return MemoryWriter(
        base_url="http://knowledge-graph-service:8000",
        headers={"X-Internal-Key": "k", "X-Organisation-Id": str(_ORG)},
        timeout=2.0,
        transport=httpx.MockTransport(handler),
    )


async def test_a_harness_that_opts_into_consciousness_records_the_pattern() -> None:
    seen: list[dict] = []
    svc = _service(_capture_writer(seen))
    await svc.execute(
        manifest_inline=_manifest(consciousness="never_auto_apply"),
        manifest_ref=None,
        user_input="gather evidence",
        principal=_principal(),
        graph_id=str(uuid.uuid4()),
        team_id=str(uuid.uuid4()),
    )
    await drain_pending_writes()
    assert len(seen) == 1
    body = seen[0]
    assert body.get("consciousness_pattern") == "solution"  # a SUCCEEDED run → the reusable lesson
    assert body.get("can_auto_apply") is False  # never auto-apply


async def test_a_harness_without_the_posture_writes_no_consciousness() -> None:
    # no consciousness.permissions → the harness did not opt in → a bare run-outcome (back-compat)
    seen: list[dict] = []
    svc = _service(_capture_writer(seen))
    await svc.execute(
        manifest_inline=_manifest(consciousness=None),
        manifest_ref=None,
        user_input="go",
        principal=_principal(),
        graph_id=str(uuid.uuid4()),
        team_id=str(uuid.uuid4()),
    )
    await drain_pending_writes()
    assert len(seen) == 1
    assert seen[0].get("consciousness_pattern") is None  # no pattern recorded
