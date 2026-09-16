"""HarnessClient — request marshalling + error mapping, via a mock transport."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
    HarnessRejected,
)

pytestmark = pytest.mark.unit


def _client(handler) -> HarnessClient:  # noqa: ANN001
    return HarnessClient(
        "http://harness", headers={"X-Internal-Key": "k"}, transport=httpx.MockTransport(handler)
    )


async def test_inline_manifest_marshalled_and_status_returned() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["internal"] = request.headers.get("X-Internal-Key")
        return httpx.Response(200, json={"id": "x", "status": "SUCCEEDED", "output": "done"})

    out = await _client(handler).execute(input_text="go", manifest_inline={"ohm_version": "1.0"})
    assert captured["path"] == "/v1/harnesses/execute"
    assert captured["body"] == {
        "input": "go",
        "manifest": {"ohm_version": "1.0"},
        # #975: NEVER omitted (ruling 6/S7) — an absent kwarg still sends the empty default.
        "prior_fetched_urls": [],
        "person_supplied_text": "",
    }
    assert captured["internal"] == "k"
    assert out["status"] == "SUCCEEDED"


async def test_manifest_ref_marshalled() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x", "status": "SUCCEEDED"})

    await _client(handler).execute(input_text="go", manifest_ref="cap-123")
    assert captured["body"] == {
        "input": "go",
        "manifest_ref": "cap-123",
        # #975: NEVER omitted (ruling 6/S7) — an absent kwarg still sends the empty default.
        "prior_fetched_urls": [],
        "person_supplied_text": "",
    }


async def test_no_manifest_raises() -> None:
    with pytest.raises(HarnessClientError):
        await _client(lambda r: httpx.Response(200)).execute(input_text="go")


async def test_non_2xx_raises_harness_rejected_with_status_and_detail() -> None:
    # A reachable-but-rejecting harness surfaces as HarnessRejected (a HarnessClientError subclass)
    # carrying the upstream status + bounded detail — distinct from a transport failure (#251).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(HarnessRejected) as exc_info:
        await _client(handler).execute(input_text="go", manifest_inline={})
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "bad gateway"
    assert isinstance(exc_info.value, HarnessClientError)  # still a HarnessClientError


async def test_rejected_prefers_structured_ohm_detail() -> None:
    # A 422 with a structured OHM/FastAPI body renders the `detail`, not the raw envelope.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "manifest.ohm_version is required"})

    with pytest.raises(HarnessRejected) as exc_info:
        await _client(handler).execute(input_text="go", manifest_inline={})
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "manifest.ohm_version is required"


async def test_transport_error_becomes_client_error() -> None:
    # harness down / timeout must surface as HarnessClientError (→ a clean FAILED job, never a 500).
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(HarnessClientError):
        await _client(handler).execute(input_text="go", manifest_inline={})


async def test_complete_assignment_marshals_and_wraps_transport() -> None:
    import uuid

    captured: dict = {}

    def ok(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "COMPLETED"})

    aid = uuid.uuid4()
    out = await _client(ok).complete_assignment(aid, "approved")
    assert captured["path"] == f"/v1/harnesses/assignments/{aid}/complete"
    assert captured["body"] == {"output": "approved"}
    assert out["status"] == "COMPLETED"

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(HarnessClientError):  # harness down → clean error, not a 500
        await _client(down).complete_assignment(aid, "x")


async def test_precedence_marshalled_into_the_execute_body() -> None:
    """#538: the team's precedence is marshalled into the engine→harness POST body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x", "status": "SUCCEEDED", "output": "done"})

    await _client(handler).execute(
        input_text="go",
        manifest_inline={"ohm_version": "1.0"},
        precedence_order=["rules", "bible"],
        graph_authoritative=True,
    )
    assert captured["body"]["precedence_order"] == ["rules", "bible"]
    assert captured["body"]["graph_authoritative"] is True


async def test_default_precedence_is_omitted_from_the_execute_body() -> None:
    """Additive: no precedence + the default graph_authoritative=False → neither key is sent."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x", "status": "SUCCEEDED", "output": "done"})

    await _client(handler).execute(input_text="go", manifest_inline={"ohm_version": "1.0"})
    assert "precedence_order" not in captured["body"]
    assert "graph_authoritative" not in captured["body"]


async def test_execute_sends_execution_id() -> None:
    """#1072: the engine mints ``execution_id`` per dispatch so it can cancel before any response
    arrives; ``execute`` marshals it into the POST body as a string (matching the existing
    ``parent_execution_id``/``trace_id`` UUID-serialisation convention)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x", "status": "SUCCEEDED"})

    eid = uuid.uuid4()
    await _client(handler).execute(
        input_text="go", manifest_inline={"ohm_version": "1.0"}, execution_id=eid
    )
    assert captured["body"]["execution_id"] == str(eid)


async def test_cancel_posts_cancel_path_returns_row() -> None:
    """#1072: a 200 cancel response is the same ``HarnessExecutionOut`` shape ``execute`` returns
    (carrying ``total_tokens``, the true spend), and the request carries the same auth/principal
    headers ``execute`` sends so the harness sees the same tenant (ADR-018)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["internal"] = request.headers.get("X-Internal-Key")
        return httpx.Response(200, json={"id": "x", "status": "CANCELLED", "total_tokens": 100})

    eid = uuid.uuid4()
    out = await _client(handler).cancel(eid, timeout=5.0)
    assert captured["path"] == f"/v1/harnesses/{eid}/cancel"
    assert captured["internal"] == "k"
    assert out == {"id": "x", "status": "CANCELLED", "total_tokens": 100}


async def test_cancel_202_returns_none() -> None:
    """#1072: a 202 (harness still winding the loop down) → ``cancel`` returns ``None`` rather
    than a partial/synthetic row, so the caller knows to fall back to its own budget handling."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"execution_id": "x", "status": "CANCEL_REQUESTED"})

    out = await _client(handler).cancel(uuid.uuid4(), timeout=5.0)
    assert out is None


async def test_cancel_404_raises_harness_rejected() -> None:
    """#1072: an unknown execution id or another org's id → ``HarnessRejected`` (#251) — reachable
    but refused, distinct from a transport failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    with pytest.raises(HarnessRejected) as exc_info:
        await _client(handler).cancel(uuid.uuid4(), timeout=5.0)
    assert exc_info.value.status_code == 404


async def test_cancel_transport_error_raises_client_error() -> None:
    """#1072: the harness unreachable during cancel → ``HarnessClientError``, matching ``execute``
    so the engine's fail-closed budget path (charge ``member_max_tokens``) can key off one type."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(HarnessClientError):
        await _client(handler).cancel(uuid.uuid4(), timeout=5.0)


async def test_cancel_uses_own_timeout() -> None:
    """#1072: ``cancel``'s own ``timeout`` argument governs the request — never the client's
    member-``execute`` timeout — so a fast cancel is never held hostage by a long-running job's
    wall-clock budget."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"id": "x", "status": "CANCELLED", "total_tokens": 0})

    client = HarnessClient(
        "http://harness",
        headers={"X-Internal-Key": "k"},
        timeout=600.0,  # the member-execute default — cancel must not inherit this
        transport=httpx.MockTransport(handler),
    )
    await client.cancel(uuid.uuid4(), timeout=3.0)
    assert captured["timeout"] == {"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}
