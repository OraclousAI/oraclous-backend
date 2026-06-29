"""#585 — the budget-halt is a GOVERNED terminal on the engine surface, distinct from a failure.

When ``run_team`` halts a run on the team-pooled ceiling it returns ``status="cost_budget"``; that
must map to a governed terminal run state (``COST_BUDGET``), NOT ``FAILED`` (a budget halt is policy
not an error — re-run targeting per ADR-042 must not treat it like a member fault), and the team-run
API row must surface the ``partial`` flag so a caller (and the deployed e2e) can see the run halted
partially. RED until the [impl] adds the status-map entry + the ``TeamRunOut.partial`` field.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_cost_budget_status_maps_to_a_governed_terminal_not_failed() -> None:
    from oraclous_execution_engine_service.services.team_run_service import _STATUS_TO_STATE

    assert _STATUS_TO_STATE["cost_budget"] == "COST_BUDGET"  # a governed budget-halt terminal
    assert _STATUS_TO_STATE["cost_budget"] != "FAILED"  # NOT routed through the failure branch


def test_team_run_out_exposes_the_partial_flag() -> None:
    from oraclous_execution_engine_service.schema.engine_schemas import TeamRunOut

    # the API row (GET /v1/engine/team-runs/{id}) surfaces the budget-partial flag so a caller can
    # tell a partial budget-halt from a full success — the deployed e2e asserts it on the row.
    assert "partial" in TeamRunOut.model_fields
