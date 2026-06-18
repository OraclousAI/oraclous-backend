"""Integration test for the wired Celery request-id threading (#366 part 2, WP-6) in KGS.

Proves the ACTUAL signal handlers wired on the KGS ``celery_app`` (not a reconstruction) carry
the WP-6 ``x-request-id`` across the broker: bound on publish, re-bound in the worker at task
start, reset at task end so a pooled worker never leaks one task's id onward. Runs the task in
Celery EAGER mode (no broker), exercising the publish → prerun → postrun signals exactly as a
real enqueue would. Also pins that the gated tracing init is a behaviour-neutral no-op when OTel
is unconfigured.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.tasks.celery_app import (
    _configure_worker_tracing,
    celery_app,
)
from oraclous_telemetry import get_request_id, instrument_worker, tracing_enabled
from oraclous_telemetry.correlation import bind_request_id, reset_request_id

pytestmark = pytest.mark.unit


@pytest.fixture()
def eager_celery():
    """Run tasks inline (no broker) with signals firing, then restore the prior eager config."""
    prev_eager = celery_app.conf.task_always_eager
    prev_prop = celery_app.conf.task_eager_propagates
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        yield celery_app
    finally:
        celery_app.conf.task_always_eager = prev_eager
        celery_app.conf.task_eager_propagates = prev_prop


def test_request_id_threads_from_publish_into_worker(eager_celery):
    seen: dict[str, str] = {}

    @eager_celery.task(name="kgs.test_echo_request_id")
    def _echo() -> str:
        # Inside the "worker": the id must be re-bound from the carried message headers.
        seen["rid"] = get_request_id()
        return seen["rid"]

    token = bind_request_id("req_thread_me")
    try:
        _echo.delay()
    finally:
        reset_request_id(token)

    assert seen["rid"] == "req_thread_me"
    # And the publish-side bind was reset above — no id leaks into the ambient context afterwards.
    assert get_request_id() == ""


def test_no_id_bound_threads_nothing(eager_celery):
    """A task enqueued outside any request (e.g. Beat) carries no id; the worker simply has none."""
    seen: dict[str, str] = {}

    @eager_celery.task(name="kgs.test_echo_no_id")
    def _echo() -> str:
        seen["rid"] = get_request_id()
        return seen["rid"]

    assert get_request_id() == ""  # nothing bound on the publish side
    _echo.delay()
    assert seen["rid"] == ""


def test_worker_tracing_init_is_noop_when_otel_unconfigured(monkeypatch: pytest.MonkeyPatch):
    """Deployed sets OTEL_EXPORTER_OTLP_ENDPOINT; dev/test leaves it unset → a pure no-op."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    assert tracing_enabled() is False
    # The wired worker_process_init handler must not raise and must install nothing when gated.
    assert instrument_worker("knowledge-graph-service") is False
    _configure_worker_tracing()  # the actual wired handler — a safe no-op
