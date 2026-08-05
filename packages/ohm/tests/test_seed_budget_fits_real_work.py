"""The governed-by-default per-member ceiling has to fit one member's real work (#714).

Measured on the deployed stack while proving #714: a compiled Reviewer reading
``OraclousAI/oraclous-backend#716`` through the GitHub MCP ``pull_request_read`` tool spent 105,917
tokens over seven calls — one call per method, no loop, the payload simply accumulating turn over
turn. A second run on the smaller #710 spent 107,804. Both escalated at the 100,000 ceiling the
seed policy handed every compiled team, a few thousand tokens short of posting anything.

A ceiling that rejects ordinary work is not a safety bound, it is a papercut that reads like a
budget failure. The POOL is deliberately untouched: a team still cannot exceed ``max_tokens_total``
however its members divide it.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.seeds import seed_policy_template

pytestmark = pytest.mark.unit

#: the highest single-member spend observed on the deployed stack reading one real pull request
_OBSERVED_PR_REVIEW_TOKENS = 107_804


def test_the_per_member_ceiling_clears_a_real_pull_request_read() -> None:
    budget = seed_policy_template().budget
    assert budget.max_tokens_per_member is not None
    assert budget.max_tokens_per_member > _OBSERVED_PR_REVIEW_TOKENS, (
        "a compiled member reading one real pull request spends ~105k tokens; a ceiling at or "
        "below that escalates on normal work"
    )


def test_the_team_pool_still_bounds_the_whole_team() -> None:
    """Raising what ONE member may spend must not raise what a TEAM may. The pool is the bound
    that actually stops a runaway; the per-member cap only stops one member taking all of it."""
    budget = seed_policy_template().budget
    assert budget.max_tokens_total == 500_000
    assert budget.max_tokens_per_member is not None
    assert budget.max_tokens_per_member <= budget.max_tokens_total


def test_a_three_member_team_cannot_spend_three_ceilings() -> None:
    budget = seed_policy_template().budget
    assert budget.max_tokens_per_member is not None
    assert 3 * budget.max_tokens_per_member > budget.max_tokens_total  # the pool binds first
