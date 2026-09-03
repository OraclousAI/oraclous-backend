"""The drafter is told where a member's output persists (#694, defect 2).

The surveyor handed the drafter a menu of bare names and a positional index:

    [{"name": "bash", "ref": "1"}, {"name": "edit", "ref": "2"}, ...]

``WriteToolPlugin.DESCRIPTION`` already says "Write text to a file in the agent's sandbox
workspace", and the catalog discarded it. ``DRAFTER_PROMPT`` then said nothing at all about
persistence, so a name-only menu was read as a persistence promise and the model picked the two
names it has the strongest prior for.

Descriptions now ride the menu (#713 built that seam), and one explicit rule tells the drafter to
say where each member's output lands. That makes the manifest read honestly rather than by
accident — a model choosing correctly for a stated reason, not by luck.

RED until the [impl] adds the rule.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT

pytestmark = pytest.mark.unit


def test_the_drafter_is_told_to_give_every_producing_member_a_persistence_tool() -> None:
    text = DRAFTER_PROMPT.lower()
    assert "persist" in text


def test_the_rule_names_where_the_output_goes() -> None:
    """Not just "persist your output" — the drafter has to know the destination is the graph, or
    the rule is satisfiable by ``write`` again."""
    assert "knowledge graph" in DRAFTER_PROMPT.lower()


def test_the_existing_rules_are_untouched() -> None:
    """The prompt is load-bearing for four other gates. Adding a rule must not displace one — each
    of these has its own issue behind it (#714 task_input, #594 the catalog gate, #596 governance).
    """
    text = DRAFTER_PROMPT
    assert "task_input" in text
    assert "the surveyed catalog listed" in text  # #709 dropped the dead surveyor step
    assert "ACYCLIC" in text
    assert "GOVERNED-BY-DEFAULT" in text
