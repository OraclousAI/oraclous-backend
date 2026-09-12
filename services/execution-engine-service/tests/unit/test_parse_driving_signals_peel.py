"""#1043 — auditing the six duplicated greedy `re.search(r"\{.*\}", ..., re.DOTALL)` JSON-peel
sites for the same bug (see `test_parse_member_object_peel.py` for the live-broken one,
`_parse_member_object`). `parse_driving_signals` (team_run.py:520) is a SIBLING peel in the same
file, but it already carries a SECOND fallback regex — `r'"driving_signals"\s*:\s*(\[.*?\])'` —
that extracts just the array, independent of anything else in the text. So unlike
`_parse_member_object`, this one already survives the two-object reply shape today.

This is a regression guard, not a RED test: it pins that `parse_driving_signals` keeps working on
the exact reply shape that broke `_parse_member_object`, so a future change to the shared peel
helper does not accidentally regress this sibling's own working fallback.
"""

from __future__ import annotations

import json

import pytest
from oraclous_execution_engine_service.services.team_run import parse_driving_signals

pytestmark = pytest.mark.unit

_TEAM = {
    "members": [{"role": "researcher", "kind": "agent", "manifest_ref": "org:x/researcher@1"}]
}
_SIGNALS = [{"signal": "validated", "value": True, "source_tool_call_id": "c1"}]
_RECEIPT = {"driving_signals": _SIGNALS}


def test_the_receipt_after_the_team_json_is_still_found() -> None:
    # passes today — parse_driving_signals's own fallback regex already handles this shape
    text = json.dumps(_TEAM) + "\n\n" + json.dumps(_RECEIPT)
    assert parse_driving_signals(text) == _SIGNALS
