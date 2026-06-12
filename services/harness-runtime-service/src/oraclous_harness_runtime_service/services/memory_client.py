"""Post-run agent-memory writer (ORAA-4 §21 services layer) — issue #332 / ADR-027 §5.

FIRE-AND-FORGET + FAIL-SOFT — the zero-risk constraint, MANDATORY: a memory write must NEVER fail,
block, or slow a run. ``schedule_*`` spawns a detached asyncio task (strong-ref'd so it is not
GC'd) and returns immediately; the task POSTs ``/internal/v1/memories`` on the knowledge-graph
service over the internal-key trust path (ADR-018) with a SHORT timeout (~2s) and swallows + logs
EVERY failure — transport faults, timeouts, non-2xx, serialization, scheduling. Nothing here can
propagate into the run path.

The writer exists at all only when ``HARNESS_MEMORY_WRITES`` is true (default FALSE in code; the
deploy env opts in) — flag off means the writer is never constructed and zero calls happen.

After a run completes the hook writes ONE episodic memory (the run-outcome summary: agent,
goal/task, result status, key tool usage); when explicit human feedback exists (a HITL decision
reason) it also writes a procedural memory. Which graph a run's memory lands in is the harness's
single graph context when the manifest binds exactly one ``config.graph_id`` across its
capabilities; otherwise the body omits ``graph_id`` and the KGS falls back to the lazily-created
org-default memory graph.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MEMORIES_PATH = "/internal/v1/memories"
_INPUT_TRUNC = 200
_OUTPUT_TRUNC = 300
_FEEDBACK_TRUNC = 500

# Strong references to in-flight fire-and-forget writes: an un-referenced asyncio.Task can be
# garbage-collected mid-flight. Done tasks remove themselves.
_pending: set[asyncio.Task[None]] = set()


async def drain_pending_writes() -> None:
    """Await every in-flight write (tests + graceful shutdown; the run path NEVER calls this)."""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


def _clip(text: str | None, limit: int) -> str:
    cleaned = (text or "").strip().replace("\n", " ")
    return cleaned[:limit] if cleaned else "(none)"


class MemoryWriter:
    """Schedules fail-soft memory writes against the KGS internal memory endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: dict[str, str],
        timeout: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = headers
        self._timeout = timeout
        self._transport = transport  # injectable for tests (None → real network)

    # ------------------------------------------------------------- payloads

    def schedule_run_outcome(
        self,
        *,
        harness_id: str,
        harness_name: str,
        status: str,
        user_input: str,
        output: str | None,
        tool_names: list[str],
        execution_id: uuid.UUID,
        graph_id: str | None,
    ) -> None:
        """ONE episodic memory per completed run: agent, task, result status, key tool usage."""
        tools = ", ".join(dict.fromkeys(tool_names)) or "none"
        content = (
            f"Agent '{harness_name}' run {status}. "
            f"Task: {_clip(user_input, _INPUT_TRUNC)}. "
            f"Outcome: {_clip(output, _OUTPUT_TRUNC)}. "
            f"Tools used: {tools}."
        )
        self._schedule(
            {
                "type": "episodic",
                "content": content,
                "source": "agent",
                "scope": "agent",
                "agent_id": harness_id,
                "session_id": str(execution_id),
                "event_type": "harness_run",
                **({"graph_id": graph_id} if graph_id else {}),
            }
        )

    def schedule_human_feedback(
        self,
        *,
        harness_id: str,
        harness_name: str,
        feedback: str,
        execution_id: uuid.UUID,
        graph_id: str | None,
    ) -> None:
        """A procedural memory when explicit human feedback exists (a HITL decision reason)."""
        self._schedule(
            {
                "type": "procedural",
                "content": (
                    f"Human feedback on agent '{harness_name}': {_clip(feedback, _FEEDBACK_TRUNC)}"
                ),
                "source": "user_feedback",
                "scope": "agent",
                "agent_id": harness_id,
                "session_id": str(execution_id),
                "category": "feedback",
                **({"graph_id": graph_id} if graph_id else {}),
            }
        )

    # ------------------------------------------------------------- mechanics

    def _schedule(self, payload: dict[str, Any]) -> None:
        """Detach the POST onto the running loop. NEVER raises — a scheduling fault is logged."""
        try:
            task = asyncio.get_running_loop().create_task(self._post(payload))
            _pending.add(task)
            task.add_done_callback(_pending.discard)
        except Exception as exc:  # noqa: BLE001 — fail-soft: a memory write can never hurt a run
            logger.warning("memory write skipped (could not schedule): %s", exc)

    async def _post(self, payload: dict[str, Any]) -> None:
        """The detached write: short timeout, every failure swallowed + logged."""
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                resp = await client.post(_MEMORIES_PATH, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "memory write dropped: KGS returned %s for a %s memory",
                    resp.status_code,
                    payload.get("type"),
                )
        except Exception as exc:  # noqa: BLE001 — fail-soft by contract (ADR-027 §5)
            logger.warning("memory write dropped (%s): %s", type(exc).__name__, exc)
