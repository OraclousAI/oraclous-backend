"""Unit tests for Celery request-id threading (#366 part 2, WP-6) — the celery-free pure functions.

These prove the broker-crossing contract WITHOUT a broker: the publish side copies the bound
``x-request-id`` onto a headers dict; the worker side re-binds it from those headers and resets it
at task end. Together they let a worker's logs + spans join the web request that enqueued the task.
"""

from __future__ import annotations

import pytest
from oraclous_telemetry import (
    REQUEST_ID_HEADER_KEY,
    attach_request_id,
    bind_request_id_from_headers,
    clear_request_id,
    request_id_from_headers,
)
from oraclous_telemetry.correlation import bind_request_id, get_request_id, reset_request_id

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _unbound_request_id():
    """Each test starts with no request id bound, and leaves none bound."""
    token = bind_request_id("")
    try:
        yield
    finally:
        reset_request_id(token)


def test_header_key_is_x_request_id():
    # Same wire name the gateway forwards on the HTTP path → one id across HTTP + broker hops.
    assert REQUEST_ID_HEADER_KEY == "x-request-id"


# --- attach (publish side) -----------------------------------------------------------------------


def test_attach_copies_bound_id_into_headers():
    token = bind_request_id("req_pub_1")
    try:
        headers: dict[str, object] = {}
        attach_request_id(headers)
        assert headers[REQUEST_ID_HEADER_KEY] == "req_pub_1"
    finally:
        reset_request_id(token)


def test_attach_is_noop_when_no_id_bound():
    headers: dict[str, object] = {}
    attach_request_id(headers)  # nothing bound (e.g. a Celery Beat task)
    assert REQUEST_ID_HEADER_KEY not in headers


def test_attach_never_overwrites_existing_header():
    token = bind_request_id("req_new")
    try:
        headers: dict[str, object] = {REQUEST_ID_HEADER_KEY: "req_already_set"}
        attach_request_id(headers)
        assert headers[REQUEST_ID_HEADER_KEY] == "req_already_set"
    finally:
        reset_request_id(token)


def test_attach_tolerates_non_dict_headers():
    # A None/odd headers payload must never break publishing.
    attach_request_id(None)
    attach_request_id("not-a-dict")  # type: ignore[arg-type]


# --- read + bind (worker side) -------------------------------------------------------------------


def test_request_id_from_headers_reads_value():
    assert request_id_from_headers({REQUEST_ID_HEADER_KEY: "req_w"}) == "req_w"


@pytest.mark.parametrize("headers", [None, {}, {REQUEST_ID_HEADER_KEY: ""}, {"other": "x"}, 5])
def test_request_id_from_headers_absent_is_empty(headers: object):
    assert request_id_from_headers(headers) == ""


def test_bind_from_headers_binds_and_returns_token():
    token = bind_request_id_from_headers({REQUEST_ID_HEADER_KEY: "req_bound"})
    try:
        assert token is not None
        assert get_request_id() == "req_bound"
    finally:
        clear_request_id(token)


def test_bind_from_headers_returns_none_when_absent():
    # No id in headers → nothing bound, nothing to reset.
    assert bind_request_id_from_headers({}) is None
    assert get_request_id() == ""


def test_clear_resets_the_bind():
    token = bind_request_id_from_headers({REQUEST_ID_HEADER_KEY: "req_clear"})
    assert get_request_id() == "req_clear"
    clear_request_id(token)
    # After clear the worker context no longer carries the previous task's id (no leak).
    assert get_request_id() == ""


def test_clear_tolerates_none_token():
    clear_request_id(None)  # the no-id-bound case — must be a safe no-op


def test_publish_then_worker_round_trip():
    """End-to-end (no broker): bind → attach → (cross the 'wire') → re-bind in a fresh context."""
    publish_token = bind_request_id("req_round_trip")
    headers: dict[str, object] = {}
    try:
        attach_request_id(headers)
    finally:
        reset_request_id(publish_token)

    # The worker process has no request bound until it re-binds from the carried headers.
    assert get_request_id() == ""
    worker_token = bind_request_id_from_headers(headers)
    try:
        assert get_request_id() == "req_round_trip"
    finally:
        clear_request_id(worker_token)
    assert get_request_id() == ""
