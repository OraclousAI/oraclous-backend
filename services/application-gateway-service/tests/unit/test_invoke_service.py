"""Unit: InvokeService resolves the bound agent, calls the harness, and projects the result.

A fake UpstreamClient stands in for the harness; asserts the narrow projection (only id/status/
output/error survive — never org/harness_id/steps/tokens) and the error mapping (missing agent ->
AgentNotFound; harness non-2xx / non-JSON -> UpstreamInvokeError).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from oraclous_application_gateway_service.services.invoke_service import (
    AgentNotFound,
    InvokeService,
    UpstreamInvokeError,
)
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_PRINCIPAL = Principal(
    principal_id=uuid.uuid4(), principal_type=PrincipalType.SERVICE_ACCOUNT, organisation_id=_ORG
)


class _FakeAgents:
    def __init__(self, row=None) -> None:
        self._row = row
        self.asked: tuple | None = None

    async def get_by_slug(self, *, organisation_id, slug):  # noqa: ANN001
        self.asked = (organisation_id, slug)
        return self._row


class _FakeResp:
    def __init__(self, status_code, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        return None


class _FakeUpstream:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.sent: dict | None = None

    async def open(self, *, method, url, headers, params, content):  # noqa: ANN001
        self.sent = {"method": method, "url": url, "headers": headers, "content": content}
        return self._resp


def _agent(slug="weather", ref="cap-1", status="active"):
    return SimpleNamespace(slug=slug, bound_capability_ref=ref, status=status)


def _svc(agents, upstream) -> InvokeService:
    return InvokeService(
        agents=agents,
        upstream_client=upstream,
        harness_base_url="http://harness:8000",
        internal_key="k",
    )


_HARNESS_OK = json.dumps(
    {
        "id": str(uuid.uuid4()),
        "organisation_id": str(uuid.uuid4()),  # must NOT surface
        "harness_id": str(uuid.uuid4()),  # must NOT surface
        "status": "SUCCEEDED",
        "output": "the answer",
        "error_message": None,
        "steps": [{"index": 0}],  # must NOT surface
        "total_tokens": 999,  # must NOT surface
    }
).encode()


async def test_invoke_projects_only_safe_fields() -> None:
    agents = _FakeAgents(_agent())
    up = _FakeUpstream(_FakeResp(201, _HARNESS_OK))
    out = await _svc(agents, up).invoke(slug="weather", agent_input="hi", principal=_PRINCIPAL)
    assert out.status == "succeeded" and out.output == "the answer" and out.error is None
    # the projection is narrow — the model has no org/harness/steps/token fields at all
    assert set(out.model_dump().keys()) == {"execution_id", "status", "output", "error"}
    # resolution was org-scoped (the key's org), and the harness ran the agent's bound ref
    assert agents.asked == (_ORG, "weather")
    assert b"cap-1" in up.sent["content"] and up.sent["url"].endswith("/v1/harnesses/execute")


async def test_invoke_sends_adr018_trusted_headers_only() -> None:
    up = _FakeUpstream(_FakeResp(201, _HARNESS_OK))
    await _svc(_FakeAgents(_agent()), up).invoke(
        slug="weather", agent_input="hi", principal=_PRINCIPAL
    )
    sent = {k.decode(): v.decode() for k, v in up.sent["headers"]}
    assert sent["x-internal-key"] == "k"  # the gateway attests the call (ADR-018)
    assert sent["x-principal-id"] == str(_PRINCIPAL.principal_id)
    assert sent["x-principal-type"] == "service_account"
    assert sent["x-organisation-id"] == str(_ORG)  # the verified org, never the caller's


async def test_failed_run_does_not_leak_raw_error_message() -> None:
    # the harness 201s on an in-loop failure with a RAW error_message (provider/secret text) — the
    # public projection must surface only a generic 'failed' + a generic error, never that string.
    leaky = "sk-or-v1-SECRET provider 401 at https://internal.host"  # noqa: S105 — fake
    body = json.dumps(
        {"id": str(uuid.uuid4()), "status": "FAILED", "output": None, "error_message": leaky}
    ).encode()
    out = await _svc(_FakeAgents(_agent()), _FakeUpstream(_FakeResp(201, body))).invoke(
        slug="weather", agent_input="hi", principal=_PRINCIPAL
    )
    assert out.status == "failed" and out.output is None
    assert out.error and "SECRET" not in out.error and "sk-or" not in out.error


async def test_escalated_maps_to_pending_not_an_internal_state() -> None:
    body = json.dumps({"id": str(uuid.uuid4()), "status": "ESCALATED", "output": None}).encode()
    out = await _svc(_FakeAgents(_agent()), _FakeUpstream(_FakeResp(201, body))).invoke(
        slug="weather", agent_input="hi", principal=_PRINCIPAL
    )
    assert out.status == "pending"


async def test_malformed_2xx_body_is_502_not_500() -> None:
    # a 2xx body that is valid JSON but missing 'id' must be UpstreamInvokeError (502), not 500
    body = json.dumps({"status": "SUCCEEDED"}).encode()
    with pytest.raises(UpstreamInvokeError):
        await _svc(_FakeAgents(_agent()), _FakeUpstream(_FakeResp(201, body))).invoke(
            slug="weather", agent_input="hi", principal=_PRINCIPAL
        )


async def test_missing_agent_raises_not_found() -> None:
    with pytest.raises(AgentNotFound):
        await _svc(_FakeAgents(None), _FakeUpstream(_FakeResp(201, _HARNESS_OK))).invoke(
            slug="nope", agent_input="hi", principal=_PRINCIPAL
        )


async def test_inactive_agent_raises_not_found() -> None:
    with pytest.raises(AgentNotFound):
        await _svc(
            _FakeAgents(_agent(status="unpublished")), _FakeUpstream(_FakeResp(201, _HARNESS_OK))
        ).invoke(slug="weather", agent_input="hi", principal=_PRINCIPAL)


async def test_harness_non_2xx_is_upstream_error() -> None:
    with pytest.raises(UpstreamInvokeError):
        await _svc(_FakeAgents(_agent()), _FakeUpstream(_FakeResp(404, b'{"x":1}'))).invoke(
            slug="weather", agent_input="hi", principal=_PRINCIPAL
        )


async def test_harness_non_json_is_upstream_error() -> None:
    with pytest.raises(UpstreamInvokeError):
        await _svc(_FakeAgents(_agent()), _FakeUpstream(_FakeResp(201, b"not json"))).invoke(
            slug="weather", agent_input="hi", principal=_PRINCIPAL
        )
