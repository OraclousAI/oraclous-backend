"""BYOM-judge plumbing (ADR-037 / BYOM-judge): the credential-broker client + the model-binding
split KRS owns. No network — a mock transport for the broker; the split is pure."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from oraclous_knowledge_retriever_service.services.broker_client import BrokerClient, BrokerError
from oraclous_knowledge_retriever_service.services.eval_judge import _model_from_binding

pytestmark = pytest.mark.unit


def _broker(handler) -> BrokerClient:  # noqa: ANN001
    return BrokerClient("http://broker", internal_key="k", transport=httpx.MockTransport(handler))


async def test_resolve_credential_returns_the_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/resolve-credential"
        body = json.loads(request.content)
        assert body["credential_id"] == "cred-1" and "organisation_id" in body
        assert request.headers["X-Internal-Key"] == "k"
        return httpx.Response(200, json={"credential": {"api_key": "sk-or-xyz"}})

    out = await _broker(handler).resolve_credential(
        credential_id="cred-1", organisation_id=uuid.uuid4()
    )
    assert out == {"api_key": "sk-or-xyz"}


async def test_resolve_credential_404_raises_broker_error() -> None:
    with pytest.raises(BrokerError):
        await _broker(lambda r: httpx.Response(404)).resolve_credential(
            credential_id="x", organisation_id=uuid.uuid4()
        )


@pytest.mark.parametrize(
    "binding,expected",
    [
        ("openrouter/openai/gpt-4o-mini", "openai/gpt-4o-mini"),  # split on the FIRST '/'
        ("openai/gpt-4o-mini", "gpt-4o-mini"),
        ("gpt-4o-mini", "gpt-4o-mini"),
        (None, None),
    ],
)
def test_model_from_binding(binding, expected) -> None:  # noqa: ANN001
    assert _model_from_binding(binding) == expected
