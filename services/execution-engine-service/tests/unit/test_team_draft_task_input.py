"""#714 — the drafter's ``task_input`` survives the peel into the stored draft.

``create_from_run`` peels the compiler reviewer's JSON, validates it, and then rebuilds the manifest
through ``assemble_and_report(draft_name, members, …)``. It passes ``members`` and nothing else, so
a ``task_input`` the drafter emitted is dropped right there — the console GO path would still have
nowhere to put the user's task even with the prompt fixed. This is the "confirm it survives the
peel, do not assume" half of #714.

RED-by-design until the ``[impl]`` lands: nothing threads the block today, so the stored manifest
comes back without it.
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


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _compiled(task_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the compiler reviewer emits — the drafted team JSON, verbatim."""
    team: dict[str, Any] = {
        "members": [
            {
                "role": "Reviewer",
                "kind": "agent",
                "manifest_ref": "org:compiled/reviewer@1",
                "subgoal": "Retrieve the pull request changes",
                "tools": [],
                "outputs_schema": {"required": ["summary"]},  # #697
            },
            {
                "role": "CommentPoster",
                "kind": "agent",
                "manifest_ref": "org:compiled/poster@1",
                "subgoal": "Post the review back as a comment",
                "depends_on": ["Reviewer"],
                "tools": [],
                "outputs_schema": {"required": ["summary"]},  # #697
            },
        ],
        "orchestration": {"style": "pipeline"},
    }
    if task_input is not None:
        team["task_input"] = task_input
    return team


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
    """Drive ``create_from_run`` over a reviewer output and return the STORED manifest."""
    repo = _FakeDraftRepo()
    runs = _FakeTeamRuns(json.dumps(compiled))
    svc = TeamDraftService(
        drafts=repo,  # type: ignore[arg-type] — duck-typed seam in unit tests
        team_runs=runs,  # type: ignore[arg-type]
    )
    row, _verdict, created = await svc.create_from_run(_principal(), team_run_id=runs.row.id)
    assert created is True
    return dict(row.manifest)


async def test_the_compiled_task_input_reaches_the_stored_draft() -> None:
    manifest = await _from_run(
        _compiled({"required": False, "key": "task", "description": "the pull request to review"})
    )
    declared = manifest.get("task_input")
    assert declared is not None, "the drafter declared a task_input and the peel dropped it"
    assert declared["key"] == "task"
    assert declared["required"] is False
    # the console renders this as the Run-dialog field label, so it has to survive verbatim
    assert declared["description"] == "the pull request to review"


async def test_a_required_task_input_reaches_the_stored_draft() -> None:
    manifest = await _from_run(
        _compiled({"required": True, "key": "pr_url", "description": "which pull request?"})
    )
    assert manifest["task_input"] == {
        "required": True,
        "key": "pr_url",
        "description": "which pull request?",
    }


async def test_a_compiled_team_without_one_still_drafts() -> None:
    """Back-compat: every draft compiled before this change has no ``task_input`` and must still
    peel, validate and store."""
    manifest = await _from_run(_compiled())
    assert manifest.get("task_input") is None
    assert [m["role"] for m in manifest["members"]] == ["Reviewer", "CommentPoster"]
