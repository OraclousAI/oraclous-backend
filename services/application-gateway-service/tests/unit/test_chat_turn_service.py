"""Unit: ChatTurnService — history reaches the input, both sides persist, ESCALATED/FAILED."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from oraclous_application_gateway_service.services.chat_turn_service import (
    ChatTurnService,
    ThreadNotFound,
    UnknownAgent,
    UpstreamChatError,
)
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_PRINCIPAL = Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)
_TID = uuid.uuid4()


class _FakeThreads:
    def __init__(self, *, thread, prior=None) -> None:
        self._thread = thread
        self.messages = list(prior or [])
        self.added: list = []

    async def get_thread(self, *, thread_id, organisation_id, user_id):  # noqa: ANN001
        # the fake enforces org+user scoping like the real repo
        if self._thread is None or organisation_id != _ORG or user_id != _USER:
            return None
        return self._thread

    async def list_messages(self, *, thread_id, limit=100, offset=0):  # noqa: ANN001
        return self.messages

    async def recent_messages(self, *, thread_id, organisation_id, limit):  # noqa: ANN001
        # the turn service loads the most-recent window (WP-10); the fake's prior history is
        # small, so the bounded read returns it unchanged (oldest->newest).
        return self.messages[-limit:]

    async def add_message(
        self,
        *,
        thread_id,
        organisation_id,
        role,
        content,
        execution_id=None,
        total_tokens=None,
        sources=None,
        new_title=None,
    ):  # noqa: ANN001
        m = SimpleNamespace(
            id=uuid.uuid4(),
            role=role,
            content=content,
            execution_id=execution_id,
            total_tokens=total_tokens,
            created_at=datetime.now(UTC),
        )
        self.added.append(m)
        if role in ("user", "assistant"):
            self.messages.append(SimpleNamespace(role=role, content=content))
        return m


class _FakeAgents:
    def __init__(self, agent) -> None:
        self._agent = agent

    async def get_by_slug(self, *, organisation_id, slug):  # noqa: ANN001
        return self._agent


class _FakeResp:
    def __init__(self, code, body: bytes) -> None:
        self.status_code = code
        self._body = body

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        return None


class _FakeUpstream:
    def __init__(self, resp) -> None:
        self._resp = resp
        self.sent: dict | None = None

    async def open(self, *, method, url, headers, params, content):  # noqa: ANN001
        self.sent = {"content": content, "headers": headers, "url": url}
        return self._resp


def _thread(slug="weather"):
    return SimpleNamespace(id=_TID, bound_agent_slug=slug)


def _agent(ref="cap-1", status="active"):
    return SimpleNamespace(bound_capability_ref=ref, status=status)


def _svc(threads, agents, upstream):
    return ChatTurnService(
        threads=threads,
        agents=agents,
        upstream_client=upstream,
        harness_base_url="http://harness:8000",
        internal_key="k",
    )


def _ok(status="SUCCEEDED", output="the reply", error=None):
    return json.dumps(
        {
            "id": str(uuid.uuid4()),
            "status": status,
            "output": output,
            "error_message": error,
            "total_tokens": 7,
        }
    ).encode()


async def test_turn_folds_history_and_persists_both_sides() -> None:
    threads = _FakeThreads(
        thread=_thread(), prior=[SimpleNamespace(role="user", content="hi earlier")]
    )
    up = _FakeUpstream(_FakeResp(201, _ok()))
    out = await _svc(threads, _FakeAgents(_agent()), up).send_message(
        thread_id=_TID, content="what did I say?", principal=_PRINCIPAL
    )
    assert (
        out.status == "succeeded"
        and out.message.content == "the reply"
        and out.message.total_tokens == 7
    )
    # the prior turn + the new message reached the SINGLE harness input
    sent = up.sent["content"].decode()
    assert "hi earlier" in sent and "what did I say?" in sent and "Conversation so far" in sent
    # both the user turn and the assistant turn were persisted, in order
    roles = [m.role for m in threads.added]
    assert roles == ["user", "assistant"]


async def test_failed_run_persists_a_generic_assistant_no_raw_leak() -> None:
    leak = "sk-or-SECRET provider 401"
    threads = _FakeThreads(thread=_thread())
    out = await _svc(
        threads,
        _FakeAgents(_agent()),
        _FakeUpstream(_FakeResp(201, _ok(status="FAILED", output=None, error=leak))),
    ).send_message(thread_id=_TID, content="hi", principal=_PRINCIPAL)
    assert out.status == "failed"
    assert (
        "SECRET" not in out.message.content and out.message.content
    )  # a generic message, not the raw error


async def test_escalated_is_pending_and_stores_no_assistant() -> None:
    threads = _FakeThreads(thread=_thread())
    out = await _svc(
        threads,
        _FakeAgents(_agent()),
        _FakeUpstream(_FakeResp(201, _ok(status="ESCALATED", output=None))),
    ).send_message(thread_id=_TID, content="hi", principal=_PRINCIPAL)
    assert out.status == "pending" and out.message is None
    assert [m.role for m in threads.added] == ["user"]  # only the user turn persisted


async def test_unknown_thread_raises_not_found() -> None:
    with pytest.raises(ThreadNotFound):
        await _svc(
            _FakeThreads(thread=None), _FakeAgents(_agent()), _FakeUpstream(_FakeResp(201, _ok()))
        ).send_message(thread_id=_TID, content="hi", principal=_PRINCIPAL)


async def test_stale_bound_agent_raises_unknown_agent() -> None:
    with pytest.raises(UnknownAgent):
        await _svc(
            _FakeThreads(thread=_thread()), _FakeAgents(None), _FakeUpstream(_FakeResp(201, _ok()))
        ).send_message(thread_id=_TID, content="hi", principal=_PRINCIPAL)


async def test_harness_non_2xx_is_upstream_error() -> None:
    with pytest.raises(UpstreamChatError):
        await _svc(
            _FakeThreads(thread=_thread()),
            _FakeAgents(_agent()),
            _FakeUpstream(_FakeResp(502, b"x")),
        ).send_message(thread_id=_TID, content="hi", principal=_PRINCIPAL)
