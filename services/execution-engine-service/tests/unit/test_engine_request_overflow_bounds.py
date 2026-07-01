"""Request-DTO length bounds on fixed-width columns (CTO review #622 sibling-sweep).

The closed-loop fingerprint overflow (a growing value written to a fixed-width VARCHAR) prompted a
sweep for the SAME class on the request path. These fields feed fixed-width columns but were missing
the ``max_length`` guard their sibling fields already carry — so an over-long value would reach the
INSERT and raise Postgres StringDataRightTruncation (a 500), instead of a clean edge 422. The guards
reject at validation time; a bound value is unchanged.
"""

from __future__ import annotations

import pytest
from oraclous_execution_engine_service.schema.engine_schemas import (
    EngineEventRequest,
    RegisterScheduleRequest,
    SubmitJobRequest,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_submit_job_manifest_ref_over_512_is_rejected() -> None:
    # EngineJob.manifest_ref is VARCHAR(512) — an over-long ref must 422 at the edge, not overflow.
    SubmitJobRequest(manifest_ref="r" * 512, input="go")  # exactly at the bound is fine
    with pytest.raises(ValidationError):
        SubmitJobRequest(manifest_ref="r" * 513, input="go")


def test_engine_event_manifest_ref_over_512_is_rejected() -> None:
    ok = EngineEventRequest(manifest_ref="r" * 512, input="go", idempotency_key="k")
    assert ok.manifest_ref is not None and len(ok.manifest_ref) == 512
    with pytest.raises(ValidationError):
        EngineEventRequest(manifest_ref="r" * 513, input="go", idempotency_key="k")


def test_register_schedule_valid_but_long_cron_is_rejected() -> None:
    # EngineSchedule.cron is VARCHAR(128). A minute-enumeration cron is VALID (passes croniter) yet
    # ~177 chars — it must be rejected at the edge, not overflow the column on INSERT.
    long_cron = ",".join(str(i) for i in range(60)) + " * * * *"
    assert len(long_cron) > 128
    with pytest.raises(ValidationError):
        RegisterScheduleRequest(cron=long_cron, manifest_ref="h", input="go")
    # a normal cron is unaffected
    RegisterScheduleRequest(cron="*/5 * * * *", manifest_ref="h", input="go")
