"""What an app tells you before you press Run (#932 follow-up).

The app read deliberately carries no team documents: opening an app is not opening the plan behind
it. But "no documents" was read as "nothing at all", and that is too far. A person about to spend
their own model key is owed a straight answer to two questions — what is going to happen, and what
is the most this can cost — and neither is available anywhere else once the frontend stops reading
the team document directly.

So the read gains a plan summary that is STRUCTURE ONLY: which steps run, what each waits on, what
tools each may use, and the run's ceilings. Deliberately NOT the members' prompts. A member's
subgoal is the plan's content rather than its shape, it is the thing a shared app should not
publish, and for an app someone made themselves it can carry their own words.

RED until ``plan_summary`` lands; the seam is imported function-locally
(`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = [pytest.mark.unit]

#: A member's prompt, standing in for anything an author would not want republished.
_PRIVATE_PROMPT = "You are the researcher. Follow these house instructions exactly, and privately."


def _team() -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "desk-research-team",
            "owner_organization_id": "00000000-0000-0000-0000-0000000000a0",
            "kind": "team",
        },
        "task_input": {"required": True, "key": "task", "description": "The idea to validate."},
        "budget": {
            "max_tokens_total": 4_000_000,
            "max_tool_calls_total": 80,
            "max_sub_runs": 20,
            "max_tokens_per_member": 1_000_000,
        },
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:desk/researcher@1",
                "subgoal": _PRIVATE_PROMPT,
                "depends_on": [],
                "tools": ["web-research", "graph-ingest"],
                "tool_rationale": {
                    "web-research": "searching is its whole job",
                    "graph-ingest": "x",
                },
                "outputs_schema": {"required": ["summary"]},
            },
            {
                "role": "synthesizer",
                "kind": "agent",
                "manifest_ref": "org:desk/synthesizer@1",
                "subgoal": "Write one decision brief.",
                "depends_on": ["researcher"],
                "tools": ["graph-ingest"],
                "tool_rationale": {"graph-ingest": "the brief is written exactly once"},
                "outputs_schema": {"required": ["posture", "headline"]},
            },
        ],
        "runtime": {"entrypoint": "researcher"},
    }


def test_the_plan_lists_each_step_in_the_order_it_will_run() -> None:
    """The screen shows what is about to happen. Order matters, and so does what each step waits
    on — that is the difference between a list of names and a plan."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    plan = plan_summary(_team())

    assert [s["role"] for s in plan["steps"]] == ["researcher", "synthesizer"]
    assert plan["steps"][0]["depends_on"] == []
    assert plan["steps"][1]["depends_on"] == ["researcher"]
    assert plan["ordered"] is True


def test_the_order_is_the_run_order_not_the_order_members_were_typed() -> None:
    """The claim above is only worth anything if it survives a manifest written back-to-front.

    The first version of this feature iterated members in declaration order and called it execution
    order. It passed its own test because the fixture happened to be typed in dependency order —
    exactly the accident this test removes. Here the dependent member is declared FIRST, and the
    plan must still put the one it waits on ahead of it.
    """
    from oraclous_execution_engine_service.domain.apps import plan_summary

    manifest = _team()
    manifest["members"] = list(reversed(manifest["members"]))
    assert manifest["members"][0]["role"] == "synthesizer"  # declared first, runs last

    plan = plan_summary(manifest)

    assert [s["role"] for s in plan["steps"]] == ["researcher", "synthesizer"]


def test_steps_that_run_together_share_a_stage() -> None:
    """A fan-out is the plan's shape as much as a sequence is. Two members that wait on nothing run
    at the same time, and a screen that stacked them vertically would misdescribe the run."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    manifest = _team()
    manifest["members"].insert(
        1,
        {
            "role": "scout",
            "kind": "agent",
            "manifest_ref": "org:desk/scout@1",
            "subgoal": "look in what we already have",
            "depends_on": [],
            "tools": ["knowledge-retriever"],
            "tool_rationale": {"knowledge-retriever": "reads what the team already gathered"},
            "outputs_schema": {"required": ["summary"]},
        },
    )
    manifest["members"][-1]["depends_on"] = ["researcher", "scout"]

    stages = {s["role"]: s["stage"] for s in plan_summary(manifest)["steps"]}

    assert stages["researcher"] == stages["scout"] == 0
    assert stages["synthesizer"] == 1


def test_a_plan_that_cannot_be_ordered_says_so_instead_of_guessing() -> None:
    """A dependency naming a member that does not exist makes the run fail the moment someone
    presses Run. Inventing a sequence for it would show a confident picture of something that
    cannot happen; refusing to render would tell the reader less than they had before. So the steps
    come back with the order marked unknown."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    manifest = _team()
    manifest["members"][1]["depends_on"] = ["nobody-by-that-name"]

    plan = plan_summary(manifest)

    assert plan["ordered"] is False
    assert {s["role"] for s in plan["steps"]} == {"researcher", "synthesizer"}
    assert all(s["stage"] is None for s in plan["steps"])


def test_each_step_names_the_tools_it_may_use() -> None:
    """A person deciding whether to run this is entitled to know it will search the web and write
    to their knowledge graph, before it does."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    plan = plan_summary(_team())

    assert plan["steps"][0]["tools"] == ["web-research", "graph-ingest"]
    assert plan["steps"][1]["tools"] == ["graph-ingest"]


def test_the_plan_carries_the_ceilings_this_run_cannot_exceed() -> None:
    """The other half of informed consent: the most this can cost. The caller is spending their own
    key, so the ceiling is theirs to see before they agree to it."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    limits = plan_summary(_team())["limits"]

    assert limits["max_tokens_total"] == 4_000_000
    assert limits["max_tool_calls_total"] == 80
    assert limits["max_sub_runs"] == 20


def test_no_members_prompt_reaches_the_plan() -> None:
    """The line this whole shape is drawn around. A subgoal is the plan's CONTENT — the thing a
    shared app must not publish, and the thing that can carry an author's own words in an app
    someone made themselves. Asserted over the serialized plan rather than field by field, so a
    future field cannot quietly start leaking it."""
    import json

    from oraclous_execution_engine_service.domain.apps import plan_summary

    serialized = json.dumps(plan_summary(_team()))

    assert _PRIVATE_PROMPT not in serialized
    assert "subgoal" not in serialized


def test_a_team_with_no_declared_budget_reports_no_limits_rather_than_zeroes() -> None:
    """A missing ceiling is unlimited, not zero. Rendering "0 tokens" for a team that declared
    nothing would tell the reader the exact opposite of the truth."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    manifest = _team()
    del manifest["budget"]

    limits = plan_summary(manifest)["limits"]

    assert limits["max_tokens_total"] is None
    assert limits["max_tool_calls_total"] is None
    assert limits["max_sub_runs"] is None


def test_a_members_declared_description_appears_on_its_step_matched_by_role() -> None:
    """#1085 (CTO ruling on the app-read plan contract): a member's own one-sentence, declared
    ``description`` is exactly what a reader is owed — distinct from the ``subgoal`` this plan
    otherwise withholds. Matched by role, not position, and the steps still come back in run
    order."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    manifest = _team()
    manifest["members"][0]["description"] = "Searches the web for competitor signals."
    manifest["members"][1]["description"] = "Writes one decision brief."

    plan = plan_summary(manifest)
    by_role = {s["role"]: s for s in plan["steps"]}

    assert by_role["researcher"]["description"] == "Searches the web for competitor signals."
    assert by_role["synthesizer"]["description"] == "Writes one decision brief."
    assert [s["role"] for s in plan["steps"]] == ["researcher", "synthesizer"]


def test_a_member_with_no_description_and_one_with_a_blank_description_both_read_none() -> None:
    """Absence and a whitespace-only value are the same claim: nothing was declared. Neither
    should read back as an empty string a screen would render as a blank line."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    manifest = _team()
    # researcher declares no description key at all; synthesizer declares whitespace only.
    manifest["members"][1]["description"] = "   "

    plan = plan_summary(manifest)
    by_role = {s["role"]: s for s in plan["steps"]}

    assert by_role["researcher"]["description"] is None
    assert by_role["synthesizer"]["description"] is None


def test_description_is_never_derived_from_subgoal_when_absent() -> None:
    """A screen must not backfill a step's blurb from the member's private ``subgoal`` — that
    would leak exactly the content this plan is designed to withhold. With no description
    declared, the subgoal text must not surface anywhere in the plan, including as a stand-in
    description value."""
    import json

    from oraclous_execution_engine_service.domain.apps import plan_summary

    plan = plan_summary(_team())
    researcher_step = next(s for s in plan["steps"] if s["role"] == "researcher")

    assert researcher_step["description"] is None
    assert _PRIVATE_PROMPT not in json.dumps(plan)


def test_limits_max_wall_seconds_equals_the_declared_termination_ceiling() -> None:
    """The other ceiling the ruling adds: copied straight from
    ``orchestration.termination.max_wall_seconds``, the same field the runtime's own termination
    check already reads."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    manifest = _team()
    manifest["orchestration"] = {"termination": {"max_wall_seconds": 900}}

    limits = plan_summary(manifest)["limits"]

    assert limits["max_wall_seconds"] == 900


def test_limits_max_wall_seconds_is_none_not_zero_when_undeclared() -> None:
    """A missing ceiling is unlimited, not zero — the same rule the file's other limits already
    follow. The key must still be present on the read, just with a ``None`` value."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    manifest = _team()
    assert "orchestration" not in manifest

    limits = plan_summary(manifest)["limits"]

    assert "max_wall_seconds" in limits
    assert limits["max_wall_seconds"] is None


def test_a_human_step_is_shown_as_a_step_a_person_has_to_take() -> None:
    """A team can pause for a person. Hiding that would let someone start a run expecting it to
    finish on its own."""
    from oraclous_execution_engine_service.domain.apps import plan_summary

    manifest = _team()
    manifest["members"].append(
        {
            "role": "approver",
            "kind": "human",
            # a human member must name the role a person is playing — the manifest refuses one
            # that does not, so the fixture carries it to stay a VALID team
            "human_role": "reviewer",
            "subgoal": "Approve or reject.",
            "depends_on": ["synthesizer"],
            "outputs_schema": {"required": ["summary"]},
        }
    )

    plan = plan_summary(manifest)
    approver = next(s for s in plan["steps"] if s["role"] == "approver")

    assert approver["kind"] == "human"
    assert plan["steps"][0]["kind"] == "agent"
