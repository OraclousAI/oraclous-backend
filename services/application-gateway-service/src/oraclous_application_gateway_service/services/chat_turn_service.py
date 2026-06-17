"""Chat turn (ORAA-4 §21 services layer) — run a chat turn through the harness, persist both sides.

Resolve the thread (org + member) + its bound published agent, fold the capped prior-turn history +
the new user message into the SINGLE harness ``input`` (the harness is stateless one-in-one-out),
run the agent (execute, ADR-018 headers, org from the principal), and persist the user
+ assistant messages. ESCALATED surfaces as a ``pending`` turn (NOT stored as a completed answer); a
stale binding / non-2xx upstream is a 502; a FAILED run stores a GENERIC assistant turn — the raw
harness ``error_message`` (un-redacted exception text) never reaches the client.
"""

from __future__ import annotations

import json
import uuid

from oraclous_governance import Principal

from oraclous_application_gateway_service.domain.chat_context import (
    MAX_HISTORY_MESSAGES,
    build_turn_input,
    derive_title,
)
from oraclous_application_gateway_service.repositories.chat_repository import ChatRepository
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)
from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
from oraclous_application_gateway_service.schema.chat_schemas import ChatTurnOut, MessageOut
from oraclous_application_gateway_service.services.proxy_service import forward_request_headers

_PUBLIC_STATUS = {"SUCCEEDED": "succeeded", "ESCALATED": "pending"}
_PUBLIC_FAIL = "The agent could not complete this request."


def _coarse_status(raw: str) -> str:
    return _PUBLIC_STATUS.get(raw.upper(), "failed")


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


class ThreadNotFound(Exception):
    """No such live thread for this member (-> 404)."""


class UnknownAgent(Exception):
    """The thread's bound published agent is gone/inactive (-> 404)."""


class UpstreamChatError(Exception):
    """The harness could not run the agent (e.g. a stale ref) (-> 502)."""


class ChatTurnService:
    def __init__(
        self,
        *,
        threads: ChatRepository,
        agents: PublishedAgentRepository,
        upstream_client: UpstreamClient,
        harness_base_url: str,
        internal_key: str,
    ) -> None:
        self._threads = threads
        self._agents = agents
        self._upstream = upstream_client
        self._base_url = harness_base_url.rstrip("/")
        self._internal_key = internal_key

    async def send_message(
        self, *, thread_id: uuid.UUID, content: str, principal: Principal
    ) -> ChatTurnOut:
        org = principal.organisation_id
        user_id = principal.principal_id
        thread = await self._threads.get_thread(
            thread_id=thread_id, organisation_id=org, user_id=user_id
        )
        if thread is None:
            raise ThreadNotFound(thread_id)
        agent = await self._agents.get_by_slug(organisation_id=org, slug=thread.bound_agent_slug)
        if agent is None or agent.status != "active":
            raise UnknownAgent(thread.bound_agent_slug)
        # load the capped history BEFORE writing the new user turn — the MOST-RECENT window only
        # (build_turn_input keeps the tail), bounded so a long thread never triggers an unbounded
        # read (WP-10).
        prior = await self._threads.recent_messages(
            thread_id=thread_id, organisation_id=org, limit=MAX_HISTORY_MESSAGES
        )
        history = [(m.role, m.content) for m in prior if m.role in ("user", "assistant")]
        agent_input = build_turn_input(history, content)
        # persist the user turn (derive the title on the very first message)
        await self._threads.add_message(
            thread_id=thread_id,
            organisation_id=org,
            role="user",
            content=content,
            new_title=derive_title(content) if not history else None,
        )
        data = await self._execute(
            manifest_ref=agent.bound_capability_ref, agent_input=agent_input, principal=principal
        )
        status = _coarse_status(str(data.get("status", "")))
        execution_id = _uuid_or_none(data.get("id"))
        if status == "pending":  # ESCALATED — a human/HITL step is pending; not a completed answer
            return ChatTurnOut(status="pending", execution_id=execution_id)
        if status == "succeeded":
            msg = await self._threads.add_message(
                thread_id=thread_id,
                organisation_id=org,
                role="assistant",
                content=data.get("output") or "",
                execution_id=execution_id,
                total_tokens=data.get("total_tokens"),
            )
        else:  # failed — a GENERIC assistant turn (never the raw harness error_message)
            msg = await self._threads.add_message(
                thread_id=thread_id,
                organisation_id=org,
                role="assistant",
                content=_PUBLIC_FAIL,
                execution_id=execution_id,
            )
        return ChatTurnOut(
            status=status, message=MessageOut.model_validate(msg), execution_id=execution_id
        )

    async def _execute(self, *, manifest_ref: str, agent_input: str, principal: Principal) -> dict:
        body = json.dumps({"manifest_ref": manifest_ref, "input": agent_input}).encode()
        headers = forward_request_headers(
            [(b"content-type", b"application/json")], principal, internal_key=self._internal_key
        )
        resp = await self._upstream.open(
            method="POST",
            url=f"{self._base_url}/v1/harnesses/execute",
            headers=headers,
            params=None,
            content=body,
        )
        try:
            code, raw = resp.status_code, await resp.aread()
        finally:
            await resp.aclose()
        if code not in (200, 201):
            raise UpstreamChatError(f"harness returned {code}")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise UpstreamChatError("harness returned a malformed body") from exc
        if not isinstance(data, dict):
            raise UpstreamChatError("harness returned a non-object body")
        return data
