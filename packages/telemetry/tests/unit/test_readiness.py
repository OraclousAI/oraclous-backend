"""Unit tests for the readiness verdict helper (ADR-021 startup-degradation policy, ORAA-297)."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from oraclous_telemetry import (
    EXIT_ON_DEGRADE_ENV,
    STATUS_DEGRADED,
    STATUS_OK,
    evaluate_readiness,
    exit_on_degrade_enabled,
)


def test_all_stores_bound_is_ok():
    verdict = evaluate_readiness({"postgres": object(), "neo4j": object()})
    assert verdict.status == STATUS_OK
    assert verdict.is_degraded is False
    assert verdict.degraded_stores == ()
    assert verdict.readyz_status_code == HTTPStatus.OK


def test_a_none_store_is_degraded():
    verdict = evaluate_readiness({"postgres": None})
    assert verdict.status == STATUS_DEGRADED
    assert verdict.is_degraded is True
    assert verdict.degraded_stores == ("postgres",)
    assert verdict.readyz_status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_degraded_stores_preserves_order_and_lists_only_none():
    verdict = evaluate_readiness({"postgres": None, "neo4j": object(), "redis": None})
    assert verdict.degraded_stores == ("postgres", "redis")


def test_no_critical_stores_is_ok():
    verdict = evaluate_readiness({})
    assert verdict.status == STATUS_OK
    assert verdict.readyz_status_code == HTTPStatus.OK


def test_exit_on_degrade_default_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(EXIT_ON_DEGRADE_ENV, raising=False)
    assert exit_on_degrade_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_exit_on_degrade_truthy(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv(EXIT_ON_DEGRADE_ENV, value)
    assert exit_on_degrade_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_exit_on_degrade_falsy(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv(EXIT_ON_DEGRADE_ENV, value)
    assert exit_on_degrade_enabled() is False
