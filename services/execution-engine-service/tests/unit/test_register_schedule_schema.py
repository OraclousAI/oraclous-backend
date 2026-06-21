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
