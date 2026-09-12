"""#1043 — `_parse_member_object`'s greedy `re.search(r"\{.*\}", ..., re.DOTALL)` spans from the
model's FIRST `{` to its LAST `}`. The reviewer's real reply (`REVIEWER_PROMPT`,
packages/ohm/src/oraclous_ohm/compiler/prompts.py) is REQUIRED to carry the team JSON followed by a
SEPARATE `driving_signals` receipt object — "The team JSON and the receipt are BOTH required" — so
on every real reviewer run the regex captures BOTH objects concatenated, `json.loads` raises, the
exception is swallowed, and `{}` comes back. Live evidence: run
`cf50f9ea-13b7-4529-bd91-24d13d5eaacb`, terminal FAILED — "member 'reviewer' declared an output
contract it did not deliver: missing required output 'members'".

RED until the [impl] decodes the FIRST JSON value out of the text (e.g.
`json.JSONDecoder().raw_decode` from the first `{`) instead of taking the widest `re.search` match.

Two of the five cases below already pass today (regression guards for the format-tolerance the
function already has); they are marked as such. The rest are RED.
"""

from __future__ import annotations

import json

import pytest
from oraclous_execution_engine_service.services.team_run import _parse_member_object

pytestmark = pytest.mark.unit

_TEAM = {
    "members": [{"role": "researcher", "kind": "agent", "manifest_ref": "org:x/researcher@1"}]
}
_RECEIPT = {"driving_signals": [{"signal": "validated", "value": True, "source_tool_call_id": "c1"}]}


def test_team_json_followed_by_a_separate_receipt_object_is_peeled() -> None:
    # RED today — the REVIEWER_PROMPT-mandated shape ("[the receipt] goes AFTER the team JSON, as a
    # separate object"): the exact reply shape that failed run cf50f9ea-13b7-4529-bd91-24d13d5eaacb.
    text = json.dumps(_TEAM) + "\n\n" + json.dumps(_RECEIPT)
    assert _parse_member_object(text) == _TEAM


def test_prose_before_the_json_is_peeled() -> None:
    # passes today (regression guard) — a single embedded object with prose around it already works
    text = "Here is the finished team:\n" + json.dumps(_TEAM)
    assert _parse_member_object(text) == _TEAM


def test_a_fenced_json_code_block_is_peeled() -> None:
    # passes today (regression guard) — a single fenced object already works
    text = "```json\n" + json.dumps(_TEAM) + "\n```"
    assert _parse_member_object(text) == _TEAM


def test_the_receipt_first_and_the_team_second_is_still_peeled_by_declared_key() -> None:
    # RED today — a real model is free to reorder its reply; the intended object must be chosen by
    # the DECLARED key it is supposed to carry, not by which object happens to come first. This
    # pins a `declared_keys` parameter on `_parse_member_object` as the mechanism.
    text = json.dumps(_RECEIPT) + "\n\n" + json.dumps(_TEAM)
    assert _parse_member_object(text, declared_keys=["members"]) == _TEAM


def test_a_single_object_reply_is_unchanged() -> None:
    # passes today (regression guard) — the one-object case must keep working exactly as it does
    assert _parse_member_object(json.dumps(_TEAM)) == _TEAM


def test_no_json_at_all_returns_an_empty_dict() -> None:
    # passes today (regression guard)
    assert _parse_member_object("I could not complete this task.") == {}
