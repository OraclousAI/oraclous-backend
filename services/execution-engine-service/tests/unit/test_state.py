"""Engine job state machine — the allowed transition table + terminality (pure)."""

from __future__ import annotations

import pytest
from oraclous_execution_engine_service.domain.state import (
    can_transition,
    is_terminal,
    sources_for,
)
from oraclous_execution_engine_service.models.enums import EngineJobState as S

pytestmark = pytest.mark.unit


def test_sources_for_is_the_inverse_allowed_set() -> None:
    assert sources_for(S.RUNNING) == frozenset({S.QUEUED})
    assert S.RUNNING in sources_for(S.SUCCEEDED)
    assert sources_for(S.CANCELLED) == frozenset({S.QUEUED, S.RUNNING, S.ESCALATED})
    assert sources_for(S.QUEUED) == frozenset({S.FAILED, S.TIMED_OUT, S.ESCALATED})  # retry/resume


def test_terminal_states() -> None:
    assert is_terminal(S.SUCCEEDED) and is_terminal(S.FAILED)
    assert is_terminal(S.TIMED_OUT) and is_terminal(S.CANCELLED)
    assert not is_terminal(S.QUEUED) and not is_terminal(S.RUNNING) and not is_terminal(S.ESCALATED)


def test_happy_path_transitions() -> None:
    assert can_transition(S.QUEUED, S.RUNNING)
    assert can_transition(S.RUNNING, S.SUCCEEDED)
    assert can_transition(S.RUNNING, S.ESCALATED)
    assert can_transition(S.RUNNING, S.TIMED_OUT)


def test_retry_and_resume_transitions() -> None:
    assert can_transition(S.FAILED, S.QUEUED)  # retry
    assert can_transition(S.TIMED_OUT, S.QUEUED)  # retry
    assert can_transition(S.ESCALATED, S.QUEUED)  # resume
    assert can_transition(S.ESCALATED, S.SUCCEEDED)  # human complete


def test_terminal_never_transitions() -> None:
    assert not can_transition(S.SUCCEEDED, S.RUNNING)
    assert not can_transition(S.CANCELLED, S.QUEUED)
    assert not can_transition(S.QUEUED, S.SUCCEEDED)  # must run first
