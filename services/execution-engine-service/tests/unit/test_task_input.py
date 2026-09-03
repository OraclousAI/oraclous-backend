"""Unit: the per-run task input reaches every member; required-and-missing fails at create (#674).

Contract §TASK (oraclous-knowledge, flows/interface-contracts.md): a standing team's per-run task
rides ``TeamRunCreate.inputs`` under the manifest-declared key (default ``"task"``, additive OHM
v1.1 ``task_input`` block) and ``render_member_input`` delivers it **verbatim to every member** as
a ``Task:`` block. A manifest that declares ``required: true`` refuses to run without it — 422 at
create, never a self-chosen target. Evidence for the gap: UC-D7 run ``9ddf00f3`` — with no way to
pass the PR URL, the Fetcher picked a random public React PR, its receipts passed the grounding
grade, and the team confidently reviewed the wrong thing.

RED-by-design until the #674 ``[impl]`` lands. ``validate_task_input`` is a not-yet-built seam —
imported function-locally (§4.1) so collection never aborts; those tests fail at runtime on the
missing attribute, on their own marker only.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_run import (
    render_member_input,
    run_team_harness,
)
from oraclous_ohm.manifest import OHMMember
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_TASK = "Review https://github.com/parhamdavari/coderabbit-poc/pull/1 and flag the seeded bugs."


def _member(role: str, depends_on: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"x/{role}@1",
        subgoal=f"{role} work",
        depends_on=depends_on or [],
    )


def _team_document(task_input: dict[str, Any] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "review-team",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [
            {"role": "fetcher", "kind": "agent", "manifest_ref": "x/fetcher@1", "subgoal": "fetch"},
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "x/reviewer@1",
                "subgoal": "review",
                "depends_on": ["fetcher"],
            },
        ],
        "runtime": {"entrypoint": "fetcher"},
    }
    if task_input is not None:
        doc["task_input"] = task_input
    return doc


class _FakeHarness:
    """Records every execute() call's input and always succeeds."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def execute(self, *, input_text: str, **_: Any) -> dict[str, Any]:
        self.inputs.append(input_text)
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran", "steps": []}


# ── OHM declaration (additive v1.1 team block) ─────────────────────────────────────────────


def test_ohm_parses_the_task_input_block() -> None:
    manifest = load_ohm(
        _team_document({"required": True, "description": "The PR URL to review", "key": "task"})
    )
    declared = manifest.task_input
    assert declared is not None
    assert declared.required is True
    assert declared.description == "The PR URL to review"
    assert declared.key == "task"


def test_ohm_task_input_defaults() -> None:
    """key defaults to "task"; required defaults to False — declaring the block is enough to get
    the Run-dialog field without making the team refuse legacy runs."""
    manifest = load_ohm(_team_document({"description": "optional focus"}))
    declared = manifest.task_input
    assert declared is not None
    assert declared.required is False
    assert declared.key == "task"


def test_ohm_without_the_block_stays_none() -> None:
    assert load_ohm(_team_document()).task_input is None  # absent → today's behaviour


# ── delivery: the task reaches members verbatim ────────────────────────────────────────────


def test_render_member_input_includes_the_task_verbatim() -> None:
    rendered = render_member_input(_member("fetcher"), [], task=_TASK)
    assert f"Task: {_TASK}" in rendered


def test_no_task_renders_byte_identical_to_today() -> None:
    """Default-OFF: a run without a task renders exactly the pre-#674 input (no empty Task: block,
    no reordering) — the same additive discipline as refresh_records (#602)."""
    assert render_member_input(_member("fetcher"), [], task=None) == render_member_input(
        _member("fetcher"), []
    )


async def test_every_member_receives_the_task() -> None:
    """Acceptance: EVERY member's rendered input carries the task — the reviewer must not have to
    reconstruct the target from the fetcher's hand-off."""
    harness = _FakeHarness()
    manifest = load_ohm(_team_document({"required": True}))
    await run_team_harness(manifest, harness, inputs={"task": _TASK})
    assert len(harness.inputs) == 2
    for rendered in harness.inputs:
        assert _TASK in rendered  # verbatim, in both the fetcher's and the reviewer's input


async def test_an_undeclared_stray_task_is_ignored() -> None:
    """Back-compat: a team with NO task_input block accepts and ignores a stray inputs.task —
    members' inputs stay byte-for-byte unchanged (inputs remains fan-out/refresh seed only)."""
    harness = _FakeHarness()
    manifest = load_ohm(_team_document())
    await run_team_harness(manifest, harness, inputs={"task": _TASK})
    for rendered in harness.inputs:
        assert _TASK not in rendered


# ── fail-closed at create ──────────────────────────────────────────────────────────────────


def test_required_and_missing_task_is_a_422_at_create() -> None:
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_task_input,
    )

    manifest = load_ohm(_team_document({"required": True, "description": "The PR URL"}))
    with pytest.raises(TeamRunError) as err:
        validate_task_input(manifest, None)
    assert err.value.status_code == 422
    assert err.value.error_type == "missing_task_input"


@pytest.mark.parametrize("bad", [{}, {"task": ""}, {"task": "   "}, {"task": 42}])
def test_empty_or_non_string_task_fails_the_same_way(bad: dict[str, Any]) -> None:
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_task_input,
    )

    manifest = load_ohm(_team_document({"required": True}))
    with pytest.raises(TeamRunError) as err:
        validate_task_input(manifest, bad)
    assert err.value.status_code == 422


def test_optional_task_input_permits_a_taskless_run() -> None:
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_task_input,
    )

    manifest = load_ohm(_team_document({"required": False}))
    validate_task_input(manifest, None)  # declared-but-optional → runs without a task


def test_a_declared_custom_key_is_honoured() -> None:
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_task_input,
    )

    manifest = load_ohm(_team_document({"required": True, "key": "pr_url"}))
    with pytest.raises(TeamRunError):
        validate_task_input(manifest, {"task": _TASK})  # wrong key — the declared one is empty
    validate_task_input(manifest, {"pr_url": _TASK})  # the declared key satisfies it


# --- #696: the user's task text is HANDED to every member, so a path in it is never a claim ---


async def test_a_tool_less_member_repeating_a_path_from_the_task_is_not_grading_a_claim() -> None:
    """The entrypoint usually receives the user's own text, which routinely names files and URLs.
    A tool-less member that talks about the file the TASK named is reasoning over its input, and
    must not fail #696's claim grade (the engine hands ``inputs`` to the orchestrator as state)."""
    from oraclous_ohm.parse import load_ohm

    class _EchoHarness:
        async def execute(self, *, input_text: str, **_: Any) -> dict[str, Any]:
            return {
                "id": str(uuid.uuid4()),
                "status": "SUCCEEDED",
                "output": "docs/brief.md asks for a pricing review; start with the deck.",
                "steps": [],
            }

    manifest = load_ohm(
        _team_document({"required": True, "description": "what to review", "key": "task"})
    )
    res = await run_team_harness(
        manifest, _EchoHarness(), inputs={"task": "Review the brief at docs/brief.md"}
    )
    assert res.member_status["fetcher"] == "succeeded"
    assert res.member_status["reviewer"] == "succeeded"
