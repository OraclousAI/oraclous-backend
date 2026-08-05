"""A compiled team keeps the governance, budget and orchestration its drafter emitted.

Found while proving #714 on the deployed stack, and folded in because it BLOCKS #714's acceptance
criterion (CLAUDE.md governance gates, "small conflicts fold in"). The console route reached GO,
the task was delivered, every tool call hit the real ``OraclousAI/oraclous-backend#716`` — and the
run still FAILED, because the first member re-read the pull request's comments a dozen times and
escalated on token budget.

The drafter had emitted exactly the bound that would have stopped it:

    "budget": {"max_tokens_total": 500000, "max_tool_calls_total": 200, "max_sub_runs": 20,
               "max_tokens_per_member": 100000, "max_tool_calls_per_member": 50,
               "on_exhaustion": "escalate"}

and the stored manifest carried ``budget: null``. It spent 219,124 tokens. ``assemble_and_report``
rebuilds the team from ``members`` alone, so governance and budget were dropped at the same seam
``task_input`` was — which also means every team compiled to date has shipped UNGOVERNED: no policy
set, no redact patterns, no cost ceiling, against #596's governed-by-default promise.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.import_ import assemble_and_report
from oraclous_ohm.manifest import OHMMember

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")

_BUDGET = {
    "max_tokens_total": 500_000,
    "max_tool_calls_total": 200,
    "max_sub_runs": 20,
    "max_tokens_per_member": 100_000,
    "max_tool_calls_per_member": 50,
    "on_exhaustion": "escalate",
}
_GOVERNANCE = {
    "policy_set_ref": "policy-set:development-default@1.0.0",
    "rebac_bindings": [],
    "redact_patterns": [r"\b(?:\d[ -]?){13,16}\b"],
}


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


def test_the_budget_survives_the_rebuild() -> None:
    """The per-member ceiling is what stops a member looping on a tool. Dropping it is the
    difference between a bounded run and 219k tokens spent re-reading the same comments."""
    result = assemble_and_report(
        "compiled-team",
        _members(),
        owner_organization_id=_ORG,
        shape="compiled",
        budget=_BUDGET,
    )
    assert result.manifest is not None
    budget = result.manifest.budget
    assert budget is not None
    assert budget.max_tokens_per_member == 100_000
    assert budget.max_tool_calls_per_member == 50
    assert budget.max_tokens_total == 500_000


def test_the_governance_survives_the_rebuild() -> None:
    """#596 governed-by-default: a compiled team ships with a known policy set and the seed redact
    patterns. Losing them means every compiled team has been running unredacted."""
    result = assemble_and_report(
        "compiled-team",
        _members(),
        owner_organization_id=_ORG,
        shape="compiled",
        governance=_GOVERNANCE,
    )
    assert result.manifest is not None
    assert result.manifest.governance.policy_set_ref == "policy-set:development-default@1.0.0"
    assert result.manifest.governance.redact_patterns == _GOVERNANCE["redact_patterns"]


def test_neither_is_required() -> None:
    """Back-compat: a caller that passes neither gets exactly today's manifest — the filesystem
    importer supplies neither and must not change."""
    result = assemble_and_report(
        "compiled-team", _members(), owner_organization_id=_ORG, shape="compiled"
    )
    assert result.manifest is not None
    assert result.manifest.budget is None
    assert result.manifest.governance.policy_set_ref is None


@pytest.mark.parametrize("bad", ["500000", ["max_tokens_total"], {"max_tokens_total": "lots"}])
def test_a_malformed_budget_is_dropped_not_raised(bad: object) -> None:
    """The drafter is a language model. A junk budget must degrade to no budget, never to a 500 —
    same discipline as the malformed ``task_input``."""
    result = assemble_and_report(
        "compiled-team",
        _members(),
        owner_organization_id=_ORG,
        shape="compiled",
        budget=bad,
    )
    assert result.manifest is None or result.manifest.budget is None


@pytest.mark.parametrize("bad", ["a policy", ["policy-set:x@1"]])
def test_a_malformed_governance_is_dropped_not_raised(bad: object) -> None:
    result = assemble_and_report(
        "compiled-team",
        _members(),
        owner_organization_id=_ORG,
        shape="compiled",
        governance=bad,
    )
    assert result.manifest is None or result.manifest.governance.policy_set_ref is None
