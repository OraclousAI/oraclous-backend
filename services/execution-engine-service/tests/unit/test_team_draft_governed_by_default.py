"""``create_from_run`` stores the governance, budget and orchestration the drafter emitted.

The engine half of the same defect: even with ``assemble_and_report`` willing to carry them, the
peel has to hand them over. Found while proving #714 on the deployed stack — the compiled team ran
with ``budget: null``, its first member looped on ``pull_request_read`` and escalated at 219,124
tokens, while the reviewer's own output had declared ``max_tool_calls_per_member: 50``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_draft_service import TeamDraftService
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()

_BUDGET = {
    "max_tokens_total": 500_000,
    "max_tool_calls_total": 200,
    "max_sub_runs": 20,
    "max_tokens_per_member": 100_000,
    "max_tool_calls_per_member": 50,
    "on_exhaustion": "escalate",
}
_GOVERNANCE = {
    "policy_set_ref": "policy-set:development-default@1.0.0",
    "rebac_bindings": [],
    "redact_patterns": [r"\b(?:\d[ -]?){13,16}\b"],
}
_ORCHESTRATION = {
    "style": "linear",
    "success_criteria": "All members complete their tasks in order",
}


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _compiled(**extra: Any) -> dict[str, Any]:
    """The compiler reviewer's output, as it really comes back off the deployed stack."""
    return {
        "members": [
            {
                "role": "Reviewer",
                "kind": "agent",
                "manifest_ref": "org:compiled/reviewer@1",
                "subgoal": "fetch pull request changes",
                "tools": [],
            },
            {
                "role": "Poster",
                "kind": "agent",
                "manifest_ref": "org:compiled/poster@1",
                "subgoal": "post the review",
                "depends_on": ["Reviewer"],
                "tools": [],
            },
        ],
        **extra,
    }


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.id = kw.get("id", uuid.uuid4())
        self.organisation_id = kw["organisation_id"]
        self.user_id = kw.get("user_id", _USER)
        self.name = kw["name"]
        self.manifest = kw["manifest"]
        self.sub_harnesses = kw.get("sub_harnesses", {})
        self.version = kw.get("version", 1)
        self.team_run_id = kw.get("team_run_id")
        self.created_at = None
        self.updated_at = None


class _FakeDraftRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _Row] = {}

    async def get_by_team_run(self, organisation_id: uuid.UUID, team_run_id: uuid.UUID) -> None:
        return None

    async def create_from_run(self, **kw: Any) -> tuple[_Row, bool]:
        row = _Row(**kw)
        self.rows[row.id] = row
        return row, True


class _RunRow:
    def __init__(self, output: str) -> None:
        self.id = uuid.uuid4()
        self.state = "SUCCEEDED"
        self.results = {"reviewer": {"output": output, "status": "SUCCEEDED"}}
        self.manifest = {"metadata": {"name": "harness-compiler"}}


class _FakeTeamRuns:
    def __init__(self, output: str) -> None:
        self.row = _RunRow(output)

    async def get(self, run_id: uuid.UUID, principal: Principal) -> _RunRow:
        if run_id != self.row.id:
            raise TeamRunError("team run not found", 404)
        return self.row


async def _from_run(compiled: dict[str, Any]) -> dict[str, Any]:
    repo = _FakeDraftRepo()
    runs = _FakeTeamRuns(json.dumps(compiled))
    svc = TeamDraftService(
        drafts=repo,  # type: ignore[arg-type] — duck-typed seam in unit tests
        team_runs=runs,  # type: ignore[arg-type]
    )
    row, _verdict, _created = await svc.create_from_run(_principal(), team_run_id=runs.row.id)
    return dict(row.manifest)


async def test_the_drafted_budget_reaches_the_stored_team() -> None:
    manifest = await _from_run(_compiled(budget=_BUDGET))
    budget = manifest.get("budget")
    assert budget is not None, "the drafter declared a budget and the peel dropped it"
    assert budget["max_tool_calls_per_member"] == 50  # the bound that stops a looping member
    assert budget["max_tokens_per_member"] == 100_000


async def test_the_drafted_governance_reaches_the_stored_team() -> None:
    manifest = await _from_run(_compiled(governance=_GOVERNANCE))
    governance = manifest.get("governance") or {}
    assert governance.get("policy_set_ref") == "policy-set:development-default@1.0.0"
    assert governance.get("redact_patterns") == _GOVERNANCE["redact_patterns"]


async def test_the_drafted_orchestration_reaches_the_stored_team() -> None:
    manifest = await _from_run(_compiled(orchestration=_ORCHESTRATION))
    orchestration = manifest.get("orchestration") or {}
    assert orchestration.get("success_criteria") == _ORCHESTRATION["success_criteria"]


async def test_a_compiled_team_declaring_none_of_them_still_drafts() -> None:
    """Back-compat: nothing about the peel becomes mandatory."""
    manifest = await _from_run(_compiled())
    assert [m["role"] for m in manifest["members"]] == ["Reviewer", "Poster"]
    assert manifest.get("budget") is None
