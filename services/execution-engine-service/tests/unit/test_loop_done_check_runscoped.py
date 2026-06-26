"""ADR-043 #552 fast-follow (CTO concern 2) — the loop done-check's landed-artifacts gate must be
RUN-scoped, not graph-scoped: it requires NEW artifacts beyond the pre-run baseline, so a warm /
adopted graph's pre-existing artifacts cannot vacuously satisfy convergence. RED until the [impl]
adds the ``artifacts_baseline`` parameter.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.manifest import (
    OHMLoop,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
    OHMTermination,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_LOOP = OHMLoop(members=["writer", "critic"], routing={})


def _team():
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=[
            OHMMember(role="writer", kind="agent", manifest_ref="o:x/writer@1"),
            OHMMember(role="critic", kind="agent", manifest_ref="o:x/critic@1"),
        ],
        orchestration=OHMOrchestration(
            loops=[_LOOP],
            success_criteria="an accurate draft",
            termination=OHMTermination(convergence="evaluator>=0.8"),
        ),
        runtime=OHMRuntime(entrypoint="writer"),
    )


class _Artifacts:
    def __init__(self, n: int) -> None:
        self.n = n

    async def list_artifacts(self, graph_id: Any) -> list[dict[str, Any]]:
        return [{"id": str(uuid.uuid4())} for _ in range(self.n)]


class _Evaluate:
    async def evaluate(self, **kw: Any) -> dict[str, Any]:
        return {"score": 0.9, "pass": True}


def _svc(*, artifacts: Any):
    from oraclous_execution_engine_service.services.team_run_service import TeamRunService

    return TeamRunService(team_runs=object(), evaluate=_Evaluate(), artifacts=artifacts)


def _both() -> dict[str, Any]:
    return {"writer": {"out": "draft"}, "critic": {"out": "review"}}


async def test_no_new_artifacts_over_baseline_blocks_convergence() -> None:
    # a warm graph already had 2 artifacts; this loop added none → NOT converged
    svc = _svc(artifacts=_Artifacts(2))
    done = svc._make_loop_done_check(
        _team(), uuid.uuid4(), str(uuid.uuid4()), _LOOP, artifacts_baseline=2
    )
    assert await done(_both()) is False


async def test_new_artifacts_over_baseline_allow_convergence() -> None:
    # the loop added a 3rd artifact beyond the baseline of 2 → the landed-artifacts floor clears
    svc = _svc(artifacts=_Artifacts(3))
    done = svc._make_loop_done_check(
        _team(), uuid.uuid4(), str(uuid.uuid4()), _LOOP, artifacts_baseline=2
    )
    assert await done(_both()) is True


async def test_default_baseline_zero_keeps_fresh_graph_behaviour() -> None:
    # the common fresh-per-run graph: baseline 0, ≥1 landed artifact converges (back-compat)
    svc = _svc(artifacts=_Artifacts(1))
    done = svc._make_loop_done_check(_team(), uuid.uuid4(), str(uuid.uuid4()), _LOOP)
    assert await done(_both()) is True
