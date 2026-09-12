r"""#1043 — auditing the six duplicated greedy `re.search(r"\{.*\}", ..., re.DOTALL)` JSON-peel
sites for the same bug. `_extract_answer_object` (tool_use.py:382) is its own copy of the SAME
regex ("the same lenient 'widest {...}' peel the engine's `_parse_member_object` uses ... duplicated
rather than imported" — its own docstring), so it has the identical failure mode: a reply carrying
two JSON objects back to back (e.g. a member's declared-key answer followed by a separate
`driving_signals` receipt object, the REVIEWER_PROMPT-mandated shape) makes the regex span both,
`json.loads` raises, and `{}` comes back instead of the real answer.

RED until the [impl] decodes the FIRST JSON value out of the text instead of the widest match.
"""

from __future__ import annotations

import json

import pytest
from oraclous_harness_runtime_service.domain.loop.tool_use import _extract_answer_object

pytestmark = pytest.mark.unit

_ANSWER = {"members": ["a", "b"]}
_RECEIPT = {"driving_signals": [{"signal": "ok", "value": True, "source_tool_call_id": "c1"}]}


def test_the_answer_object_is_still_found_when_a_receipt_object_follows_it() -> None:
    text = json.dumps(_ANSWER) + "\n\n" + json.dumps(_RECEIPT)
    assert _extract_answer_object(text) == _ANSWER


def test_a_single_object_reply_is_unchanged() -> None:
    # regression guard — the one-object case must keep working exactly as it does today
    assert _extract_answer_object(json.dumps(_ANSWER)) == _ANSWER


def test_no_json_at_all_returns_an_empty_dict() -> None:
    # regression guard
    assert _extract_answer_object("nothing here") == {}
