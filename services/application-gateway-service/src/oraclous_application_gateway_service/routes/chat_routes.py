"""Chat routes (ORAA-4 §21 routes layer) — the member console chat plane, org + member scoped.

A member starts a thread bound to a published agent, sends messages (each runs the agent via the
harness + persists the reply), reads the transcript, lists their own threads, and soft-deletes. All
routes require a member (user) credential and resolve the org + user from the verified principal;
registered before the proxy catch-all.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_application_gateway_service.core.dependencies import (
    ChatServiceDep,
    ChatTurnServiceDep,
    MemberDep,
)
from oraclous_application_gateway_service.schema.chat_schemas import (
    ChatTurnOut,
    MessageOut,
    SendMessageRequest,
    StartThreadRequest,
    ThreadOut,
)
from oraclous_application_gateway_service.services.chat_service import UnknownAgent
from oraclous_application_gateway_service.services.chat_turn_service import (
    ThreadNotFound,
    UpstreamChatError,
)
from oraclous_application_gateway_service.services.chat_turn_service import (
    UnknownAgent as TurnUnknownAgent,
)

router = APIRouter(prefix="/v1/chat", tags=["chat"])


@router.post("/threads", response_model=ThreadOut, status_code=status.HTTP_201_CREATED)
async def start_thread(
    body: StartThreadRequest, member: MemberDep, svc: ChatServiceDep
) -> ThreadOut:
    try:
        return await svc.start_thread(
            organisation_id=member.organisation_id,
            user_id=member.principal_id,
            agent_slug=body.agent_slug,
            title=body.title,
        )
    except UnknownAgent as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such published agent"
        ) from exc


@router.get("/threads", response_model=list[ThreadOut])
async def list_threads(member: MemberDep, svc: ChatServiceDep) -> list[ThreadOut]:
    return await svc.list_threads(
        organisation_id=member.organisation_id, user_id=member.principal_id
    )


@router.post("/threads/{thread_id}/messages", response_model=ChatTurnOut)
async def send_message(
    thread_id: uuid.UUID, body: SendMessageRequest, member: MemberDep, svc: ChatTurnServiceDep
) -> ChatTurnOut:
    try:
        return await svc.send_message(thread_id=thread_id, content=body.content, principal=member)
    except (ThreadNotFound, TurnUnknownAgent) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such chat thread"
        ) from exc
    except UpstreamChatError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="the agent could not be run"
        ) from exc


@router.get("/threads/{thread_id}/messages", response_model=list[MessageOut])
async def list_messages(
    thread_id: uuid.UUID, member: MemberDep, svc: ChatServiceDep
) -> list[MessageOut]:
    messages = await svc.get_messages(
        thread_id=thread_id, organisation_id=member.organisation_id, user_id=member.principal_id
    )
    if messages is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such chat thread")
    return messages


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(thread_id: uuid.UUID, member: MemberDep, svc: ChatServiceDep) -> None:
    deleted = await svc.delete_thread(
        thread_id=thread_id, organisation_id=member.organisation_id, user_id=member.principal_id
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such chat thread")
