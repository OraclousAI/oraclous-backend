"""#853 — one bounded repair turn for a malformed structured document.

A five-member team run finished; every member delivered; the synthesising member's decision brief
had one bracket missing and `JSON.parse` failed at character 2,540. Twenty to thirty minutes and
five members' tool work were discarded for one character (run `e3a6af87-eefb-4b6e-8c1a-
d59639af9e35`). Ruled 2026-08-21 (issue comment): a member declares "my output must parse" with a
new field, separate from `outputs_schema` (which checks required keys of an ALREADY-parsed payload,
only at a hand-off — never for a terminal member with no consumer, and never for a syntax failure).

RED until the [impl] adds `OHMMember.requires_valid_json`.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_member_accepts_requires_valid_json() -> None:
    from oraclous_ohm.manifest import OHMMember

    m = OHMMember(
        role="synthesizer", kind="agent", manifest_ref="org:x/a@1", requires_valid_json=True
    )
    assert m.requires_valid_json is True


def test_requires_valid_json_defaults_to_false_back_compat() -> None:
    from oraclous_ohm.manifest import OHMMember

    m = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/a@1")
    assert m.requires_valid_json is False  # every manifest written before #853 parses unchanged


def test_requires_valid_json_is_independent_of_outputs_schema() -> None:
    # A member can declare either, both, or neither — they check different things (syntax vs shape).
    from oraclous_ohm.manifest import OHMMember

    m = OHMMember(
        role="synthesizer",
        kind="agent",
        manifest_ref="org:x/a@1",
        requires_valid_json=True,
        outputs_schema={"required": ["posture"]},
    )
    assert m.requires_valid_json is True
    assert m.outputs_schema == {"required": ["posture"]}
