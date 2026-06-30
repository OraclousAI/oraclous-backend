"""RegisterScheduleRequest validator — the conditional exactly-one-manifest rule (#489).

The exactly-one-manifest rule is CONDITIONAL on target_kind: harness_job (default) keeps it;
adopted_tool_run forbids both manifests and requires instance_id. All four shape combos are covered
so the relaxation never weakens the harness case.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_execution_engine_service.schema.engine_schemas import RegisterScheduleRequest
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_IID = uuid.uuid4()


def test_harness_job_with_one_manifest_is_valid() -> None:
    req = RegisterScheduleRequest(cron="* * * * *", manifest_ref="h", input="go")
    assert req.target_kind.value == "harness_job"  # default
    assert req.instance_id is None


def test_harness_job_with_no_manifest_is_rejected() -> None:
    with pytest.raises(ValidationError):  # the harness case still requires exactly one manifest
        RegisterScheduleRequest(cron="* * * * *", input="go")


def test_harness_job_with_both_manifests_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RegisterScheduleRequest(cron="* * * * *", manifest={"x": 1}, manifest_ref="h", input="go")


def test_harness_job_with_instance_id_is_rejected() -> None:
    with pytest.raises(ValidationError):  # instance_id is only for adopted_tool_run
        RegisterScheduleRequest(cron="* * * * *", manifest_ref="h", instance_id=_IID, input="go")


def test_adopted_tool_run_with_instance_id_is_valid() -> None:
    req = RegisterScheduleRequest(
        cron="* * * * *",
        target_kind="adopted_tool_run",
        instance_id=_IID,
        input_data={"channel": "email"},
        input="scheduled",
    )
    assert req.target_kind.value == "adopted_tool_run"
    assert req.instance_id == _IID and req.manifest is None and req.manifest_ref is None


def test_adopted_tool_run_without_instance_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RegisterScheduleRequest(cron="* * * * *", target_kind="adopted_tool_run", input="scheduled")


def test_adopted_tool_run_with_a_manifest_is_rejected() -> None:
    with pytest.raises(ValidationError):  # adopted_tool_run takes no manifest
        RegisterScheduleRequest(
            cron="* * * * *",
            target_kind="adopted_tool_run",
            instance_id=_IID,
            manifest_ref="h",
            input="scheduled",
        )


# ── #598 — the L3 per-period budget cap shape (edge 422; the service re-validates as the gate) ──
def _team_kw() -> dict:
    return dict(
        cron="* * * * *", target_kind="team", manifest={"members": []}, graph_id="g", input="x"
    )


def test_team_with_a_valid_period_cap_is_valid() -> None:
    req = RegisterScheduleRequest(**_team_kw(), budget_period="daily", budget_allowance_tokens=5000)
    assert req.budget_period is not None and req.budget_period.value == "daily"
    assert req.budget_allowance_tokens == 5000


def test_team_with_no_budget_is_valid_cap_off() -> None:
    req = RegisterScheduleRequest(**_team_kw())
    assert req.budget_period is None and req.budget_allowance_tokens is None


def test_period_without_allowance_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RegisterScheduleRequest(**_team_kw(), budget_period="daily")


def test_allowance_without_period_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RegisterScheduleRequest(**_team_kw(), budget_allowance_tokens=100)


def test_nonpositive_allowance_is_rejected() -> None:
    with pytest.raises(ValidationError):  # Field(gt=0)
        RegisterScheduleRequest(**_team_kw(), budget_period="daily", budget_allowance_tokens=0)


def test_unknown_period_is_rejected() -> None:
    with pytest.raises(ValidationError):  # BudgetPeriod enum
        RegisterScheduleRequest(**_team_kw(), budget_period="hourly", budget_allowance_tokens=100)


def test_period_cap_on_a_harness_job_is_rejected() -> None:
    with pytest.raises(ValidationError):  # the cap is team-only
        RegisterScheduleRequest(
            cron="* * * * *",
            manifest_ref="h",
            input="go",
            budget_period="daily",
            budget_allowance_tokens=100,
        )


def test_period_cap_on_a_manual_team_is_rejected() -> None:
    with pytest.raises(ValidationError):  # the cap needs a recurring (cron) cadence
        RegisterScheduleRequest(
            type="manual",
            target_kind="team",
            manifest={"members": []},
            graph_id="g",
            input="x",
            budget_period="daily",
            budget_allowance_tokens=100,
        )
