"""Harness → engine status mapping (pure)."""

from __future__ import annotations

import pytest
from oraclous_execution_engine_service.domain.status_map import map_harness_status
from oraclous_execution_engine_service.models.enums import EngineJobState as S

pytestmark = pytest.mark.unit


def test_known_statuses() -> None:
    assert map_harness_status("SUCCEEDED") is S.SUCCEEDED
    assert map_harness_status("FAILED") is S.FAILED
    assert map_harness_status("ESCALATED") is S.ESCALATED


def test_unknown_status_fails_closed() -> None:
    assert map_harness_status("WAT") is S.FAILED
    assert map_harness_status("") is S.FAILED
