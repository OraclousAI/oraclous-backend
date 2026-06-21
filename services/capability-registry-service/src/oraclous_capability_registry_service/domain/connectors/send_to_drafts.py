"""Send-to-drafts connector (domain layer) — the delivery SINK (#489 / ADR-039 D1).

The structural delivery boundary: a generator agent can only *deliver* through a **declared,
ceiling-gated** sink, and this sink **only records a DRAFT — never sends or publishes**. The
draft is returned as ``output_data`` and persisted on the org-scoped (RLS) Execution row, so org
drafts are the send-to-drafts executions (a dedicated ``/deliveries`` list view is a follow-up). A
real external send (email/Slack/webhook) is deliberately NOT here — that is a separate, human-gated
publish step, never an ambient capability. Keyless; content is size-capped (the #488 lesson).
"""

from __future__ import annotations

from typing import Any

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

_CHANNELS = frozenset({"email", "slack", "notification", "webhook"})
_MAX_CONTENT_CHARS = 100_000


class SendToDraftsConnector(InternalTool):
    """Records a delivery as a DRAFT (never sent); the draft lands on the org Execution row."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        channel = input_data.get("channel")
        if not isinstance(channel, str) or channel not in _CHANNELS:
            return ExecutionResult(
                success=False,
                error_message=f"'channel' must be one of {sorted(_CHANNELS)}",
                error_type="INVALID_INPUT",
            )
        content = input_data.get("content")
        if not isinstance(content, str) or not content.strip():
            return ExecutionResult(
                success=False, error_message="'content' is required", error_type="INVALID_INPUT"
            )
        if len(content) > _MAX_CONTENT_CHARS:
            return ExecutionResult(
                success=False,
                error_message=f"'content' exceeds the {_MAX_CONTENT_CHARS}-character limit",
                error_type="INVALID_INPUT",
            )
        recipient = input_data.get("recipient")
        return ExecutionResult(
            success=True,
            data={
                # ALWAYS a draft — this sink structurally cannot send/publish (ADR-039 D1).
                "status": "DRAFT",
                "channel": channel,
                "recipient": recipient if isinstance(recipient, str) else None,
                "content": content,
            },
            metadata={"sink": "drafts", "channel": channel},
        )
