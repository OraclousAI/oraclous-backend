"""#714 — a compiled team declares the per-run task it expects, and that declaration survives.

Evidence (deployed stack, same draft / same models / same tools, one manifest field apart):
run ``538ab1fa`` without ``task_input`` FAILED with four ``MCP_TOOL_ERROR`` rows, its first member
having invented ``my-org/my-repo#123``; run ``3ce47d5e`` with ``task_input`` SUCCEEDED and posted a
real comment. The drafter never emitted the field, so no compiled team has ever had one.

Two seams carry the fix on this side of the boundary. ``DRAFTER_PROMPT`` has to ask for the block,
and ``assemble_and_report`` has to keep it — today the assembler rebuilds a team from ``members``
alone, so even a drafter that emitted ``task_input`` perfectly would lose it at the peel.

RED-by-design until the ``[impl]`` lands: ``assemble_and_report`` exists but takes no ``task_input``
argument, so those tests fail at runtime on the unexpected keyword.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.compiler.validate import validate_draft
from oraclous_ohm.import_ import assemble_and_report
from oraclous_ohm.manifest import OHMMember

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_CATALOG = ["web-research", "github-sink"]


def _members() -> list[OHMMember]:
    return [
        OHMMember(role="reviewer", kind="agent", manifest_ref="org:compiled/reviewer@1"),
        OHMMember(
            role="poster",
            kind="agent",
            manifest_ref="org:compiled/poster@1",
            depends_on=["reviewer"],
        ),
    ]


def _draft(task_input: Any = ...) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "members": [
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:compiled/reviewer@1",
                "outputs_schema": {"required": ["summary"]},  # #697
            },
        ],
        "orchestration": {"style": "pipeline"},
    }
    if task_input is not ...:
        draft["task_input"] = task_input
    return draft


# ── the drafter is told to declare it ────────────────────────────────────────


def test_the_drafter_prompt_asks_for_a_task_input_block() -> None:
    """The prompt lists exactly the keys the drafter emits — members, orchestration, governance,
    budget. ``task_input`` was not among them, which is the whole of defect (a): a team the user
    cannot address, for an objective ("review a pull request") that is meaningless without one."""
    from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT

    assert "task_input" in DRAFTER_PROMPT
    assert "required" in DRAFTER_PROMPT
    assert "description" in DRAFTER_PROMPT


def test_the_drafter_prompt_asks_for_it_on_every_team() -> None:
    """The decision is ALWAYS emit, ``required: false`` by default — not "emit it when the objective
    seems to need one". Judging that is harder than it looks, and a wrong judgement ships a team
    that cannot be told anything. Always declaring it also makes the silent-drop path of defect (b)
    unreachable for a compiled team, so the two halves reinforce each other."""
    from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT

    assert "ALWAYS emit" in DRAFTER_PROMPT  # not a conditional judgement call
    # the description is a question shown to a USER (the console renders it as the field label),
    # never a schema note — the prompt has to say so or the drafter writes "string, optional".
    assert "label" in DRAFTER_PROMPT.lower() or "user" in DRAFTER_PROMPT.lower()


# ── the assembler keeps it ───────────────────────────────────────────────────


def test_assemble_keeps_the_declared_task_input() -> None:
    """``create_from_run`` rebuilds the stored manifest through this call, passing ``members`` and
    nothing else. Whatever the drafter declared beside them is dropped here unless the assembler is
    told to carry it."""
    result = assemble_and_report(
        "compiled-team",
        _members(),
        owner_organization_id=_ORG,
        shape="compiled",
        task_input={"required": False, "key": "task", "description": "the pull request to review"},
    )
    assert result.manifest is not None
    declared = result.manifest.task_input
    assert declared is not None
    assert declared.key == "task"
    assert declared.required is False
    assert declared.description == "the pull request to review"


def test_assemble_carries_a_required_task_input() -> None:
    # the drafter may still mark it required when the objective names a target it cannot know.
    result = assemble_and_report(
        "compiled-team",
        _members(),
        owner_organization_id=_ORG,
        shape="compiled",
        task_input={"required": True, "key": "pr_url", "description": "which pull request?"},
    )
    assert result.manifest is not None
    assert result.manifest.task_input is not None
    assert result.manifest.task_input.required is True
    assert result.manifest.task_input.key == "pr_url"


def test_assemble_without_a_task_input_is_unchanged() -> None:
    """Back-compat: every team compiled before this change has no ``task_input`` and must still
    assemble and run exactly as it does today."""
    result = assemble_and_report(
        "compiled-team", _members(), owner_organization_id=_ORG, shape="compiled"
    )
    assert result.manifest is not None
    assert result.manifest.task_input is None
    assert result.report.would_block is False


def test_a_malformed_task_input_does_not_crash_the_assembler() -> None:
    """Fail-closed, never a 500: the drafter is a language model, so the block can arrive as a
    string or with a junk key. The assembler either drops it or blocks — it never raises."""
    result = assemble_and_report(
        "compiled-team",
        _members(),
        owner_organization_id=_ORG,
        shape="compiled",
        task_input="the pull request to review",  # a string, not the block
    )
    assert result.manifest is None or result.manifest.task_input is None


# ── the gate sees it, and a malformed one blocks rather than vanishing ───────


def test_validate_draft_admits_a_well_formed_task_input() -> None:
    verdict = validate_draft(
        _draft({"required": False, "key": "task", "description": "the pull request to review"}),
        _CATALOG,
        owner_organization_id=_ORG,
    )
    assert verdict["would_block"] is False


def test_validate_draft_admits_a_draft_without_one() -> None:
    verdict = validate_draft(_draft(), _CATALOG, owner_organization_id=_ORG)
    assert verdict["would_block"] is False


@pytest.mark.parametrize(
    "bad",
    [
        "the pull request to review",  # a bare string where the block belongs
        {"key": ""},  # an empty key — nothing can ever be supplied under it
        ["task"],
    ],
)
def test_validate_draft_blocks_a_malformed_task_input(bad: Any) -> None:
    """Same treatment as a malformed ``orchestration``: it blocks, so the reviewer's bounded repair
    loop gets a named reason to fix. Silently dropping it would ship exactly the team #714 reports
    — one that looks compiled and cannot be told what to work on."""
    verdict = validate_draft(_draft(bad), _CATALOG, owner_organization_id=_ORG)
    assert verdict["would_block"] is True
    assert any(
        "task_input" in str(b).lower() or "F-DRAFT-INVALID" in str(b) for b in verdict["blocking"]
    )
