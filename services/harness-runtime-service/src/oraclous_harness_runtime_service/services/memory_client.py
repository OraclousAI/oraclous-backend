"""Post-run agent-memory writer (services layer) — issue #332 / ADR-027 §5.

FIRE-AND-FORGET + FAIL-SOFT — the zero-risk constraint, MANDATORY: a memory write must NEVER fail,
block, or slow a run. ``schedule_*`` spawns a detached asyncio task (strong-ref'd so it is not
GC'd) and returns immediately; the task POSTs ``/internal/v1/memories`` on the knowledge-graph
service over the internal-key trust path (ADR-018) under an OVERALL per-write deadline and swallows
+ logs EVERY failure — transport faults, timeouts, non-2xx, serialization, scheduling. Nothing here
can propagate into the run path.

Two bounds keep a detached write from outliving its purpose or leaking resources, both fail-soft:

* **Whole-write deadline** — ``httpx``'s ``timeout`` is PER-PHASE (connect/read/write each get the
  budget), so a byte-trickling responder could keep a detached task + socket alive far longer than
  intended. Each ``_post`` is therefore wrapped in ``asyncio.wait_for`` with an OVERALL budget
  (``timeout`` × a small factor): no single write outlives that bound, after which it is cancelled,
  swallowed + logged.
* **Bounded pending set** — ``_pending`` is capped (``_MAX_IN_FLIGHT``). When saturated a new write
  is SKIPPED (logged), never queued — a slow/stuck KGS can never make the in-flight set grow without
  limit. The run path is unaffected either way: it awaits nothing.

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

from oraclous_harness_runtime_service.domain.consciousness import (
    classify_consciousness_pattern,
)

logger = logging.getLogger(__name__)

_MEMORIES_PATH = "/internal/v1/memories"
_INPUT_TRUNC = 200
_OUTPUT_TRUNC = 300
_FEEDBACK_TRUNC = 500

# Max concurrent in-flight writes. A post-run hook schedules at most 2 writes per run; this cap is
# comfortably above normal fan-in yet bounds the worst case (a stuck KGS) so the detached set can
# never grow without limit. When saturated a new write is SKIPPED + logged, never queued.
_MAX_IN_FLIGHT = 64
# The per-write OVERALL deadline is the per-phase httpx timeout times this factor — enough headroom
# for connect+write+read in the normal case, but a hard ceiling no single write can exceed (so a
# byte-trickling responder cannot keep a task/socket alive indefinitely). Min floor keeps a tiny
# test timeout (e.g. 0.2s) from being unreasonably short for the whole round-trip.
_DEADLINE_FACTOR = 3.0
_DEADLINE_FLOOR_S = 1.0

# Strong references to in-flight fire-and-forget writes: an un-referenced asyncio.Task can be
# garbage-collected mid-flight. Done tasks remove themselves.
_pending: set[asyncio.Task[None]] = set()


async def drain_pending_writes(timeout: float | None = None) -> None:  # noqa: ASYNC109 — an OPTIONAL bound; None awaits to completion (tests), a float caps the wait (shutdown grace)
    """Await every in-flight write, optionally under a SHORT overall bound.

    Used by tests (no bound — await to completion) and by lifespan shutdown (a small bounded grace
    so in-flight memories get a brief chance to land without delaying teardown). The run path NEVER
    calls this. Always fail-soft: a write that errors or is still running when the bound elapses is
    swallowed — draining can never raise.
    """
    if not _pending:
        return
    gather = asyncio.gather(*list(_pending), return_exceptions=True)
    if timeout is None:
        await gather
        return
    try:
        await asyncio.wait_for(gather, timeout)
    except TimeoutError:
        # Bounded grace elapsed with writes still in flight; let them be cancelled at teardown.
        logger.info("memory drain: %d write(s) unfinished within the grace bound", len(_pending))
    except Exception as exc:  # noqa: BLE001 — fail-soft: draining never hurts shutdown
        logger.warning("memory drain swallowed an error: %s", exc)


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
        team_id: str | None = None,
        tool_errors: list[str] | None = None,
        rounds: int = 0,
        can_auto_apply: bool = False,
        record_pattern: bool = True,
    ) -> None:
        """ONE episodic memory per completed run: agent, task, result status, key tool usage.

        Team run (#513): ``team_id`` set → ``scope=team`` under the team identity, so concurrent
        members + future runs of the team share the blackboard; else legacy ``scope=agent``.

        Flow-6 Learn (#554): the run's CODED, within-run consciousness PATTERN
        (``classify_consciousness_pattern`` — a SUCCESS → reusable ``solution``, a recurring in-run
        error → ``repetitive_failures``, an over-long run → ``velocity_anomaly``) is recorded so a
        future run recalls a LESSON, not a bare outcome. ``can_auto_apply`` carries the harness's
        ``consciousness.permissions`` posture — ``False`` under ``never_auto_apply`` (advisory-only;
        a recalled lesson biases a turn but a human must approve any behaviour change).

        ``record_pattern`` is the consciousness GATE: True (the harness declared
        ``consciousness.permissions``) → classify + record the lesson; False → a bare run-outcome
        (no consciousness — the opt-in default for a harness without the posture)."""
        pattern = (
            classify_consciousness_pattern(
                status=status, tool_names=tool_names, tool_errors=tool_errors or [], rounds=rounds
            )
            if record_pattern
            else None
        )
        tools = ", ".join(dict.fromkeys(tool_names)) or "none"
        lesson = f"Lesson ({pattern}): " if pattern else ""
        content = (
            f"{lesson}Agent '{harness_name}' run {status}. "
            f"Task: {_clip(user_input, _INPUT_TRUNC)}. "
            f"Outcome: {_clip(output, _OUTPUT_TRUNC)}. "
            f"Tools used: {tools}."
        )
        self._schedule(
            {
                "type": "episodic",
                "content": content,
                "source": "agent",
                "scope": "team" if team_id else "agent",
                "agent_id": harness_id,
                "session_id": str(execution_id),
                "event_type": "harness_run",
                # #554: the consciousness props ride ONLY a consciousness write (record_pattern);
                # a bare run-outcome (no posture) stays byte-identical to the legacy memory.
                **(
                    {"consciousness_pattern": pattern, "can_auto_apply": can_auto_apply}
                    if record_pattern
                    else {}
                ),
                **({"graph_id": graph_id} if graph_id else {}),
                **({"team_id": team_id} if team_id else {}),
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
        team_id: str | None = None,
    ) -> None:
        """A procedural memory when explicit human feedback exists (a HITL decision reason).

        Team run (#513): ``team_id`` → ``scope=team`` (the team's blackboard); else ``agent``."""
        self._schedule(
            {
                "type": "procedural",
                "content": (
                    f"Human feedback on agent '{harness_name}': {_clip(feedback, _FEEDBACK_TRUNC)}"
                ),
                "source": "user_feedback",
                "scope": "team" if team_id else "agent",
                "agent_id": harness_id,
                "session_id": str(execution_id),
                "category": "feedback",
                **({"graph_id": graph_id} if graph_id else {}),
                **({"team_id": team_id} if team_id else {}),
            }
        )

    # ------------------------------------------------------------- mechanics

    def _schedule(self, payload: dict[str, Any]) -> None:
        """Detach the POST onto the running loop. NEVER raises — a scheduling fault is logged.

        Bounded: when ``_MAX_IN_FLIGHT`` writes are already detached the new write is SKIPPED (a
        slow/stuck KGS can never make the in-flight set grow without limit). The run path awaits
        nothing either way.
        """
        try:
            if len(_pending) >= _MAX_IN_FLIGHT:
                logger.warning(
                    "memory write skipped: %d writes already in flight (KGS slow?)", len(_pending)
                )
                return
            task = asyncio.get_running_loop().create_task(self._post(payload))
            _pending.add(task)
            task.add_done_callback(_pending.discard)
        except Exception as exc:  # noqa: BLE001 — fail-soft: a memory write can never hurt a run
            logger.warning("memory write skipped (could not schedule): %s", exc)

    @property
    def _deadline(self) -> float:
        """The OVERALL per-write budget — a hard ceiling on how long one detached write may live."""
        return max(self._timeout * _DEADLINE_FACTOR, _DEADLINE_FLOOR_S)

    async def _post(self, payload: dict[str, Any]) -> None:
        """The detached write: an OVERALL deadline (not just per-phase), every failure swallowed.

        ``httpx``'s ``timeout`` bounds each phase; ``asyncio.wait_for`` bounds the whole write so a
        byte-trickling responder cannot keep the task + socket alive past ``_deadline``. On the
        deadline the inner coroutine is cancelled (the ``AsyncClient`` context closes the socket).
        """
        try:
            await asyncio.wait_for(self._post_once(payload), self._deadline)
        except TimeoutError:
            logger.warning(
                "memory write dropped: exceeded the %.1fs whole-write deadline for a %s memory",
                self._deadline,
                payload.get("type"),
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft by contract (ADR-027 §5)
            logger.warning("memory write dropped (%s): %s", type(exc).__name__, exc)

    async def _post_once(self, payload: dict[str, Any]) -> None:
        """One POST under the per-phase httpx timeout, bounded overall by the caller's wait_for."""
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


class MemoryReader:
    """The team-scope blackboard READ (#513, ADR-027) — fetch the team's current memory block
    from the adopted graph for in-loop injection (``scope=team`` for THIS team).

    Fail-soft, mirroring the writer's zero-risk property: EVERY failure (unreachable KGS,
    non-200, malformed body, timeout) returns ``None``, so a memory read can never block, slow past
    its short timeout, or fail a run. Unlike the writer it is AWAITED (the loop needs it before
    reasoning), but awaited under a bounded timeout and degrades to no-context on any fault."""

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
        self._transport = transport

    async def team_context(
        self, *, graph_id: str, team_id: str, query: str, max_tokens: int = 2000
    ) -> str | None:
        """The ``## Relevant Memory`` block for this team in the graph, or None (fail-soft)."""
        params: dict[str, str | int] = {
            "query": (query or "team")[:_INPUT_TRUNC],
            "scope": "team",
            "team_id": team_id,
            "max_tokens": max_tokens,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                resp = await client.get(
                    f"/api/v1/graphs/{graph_id}/memories/context", params=params
                )
            if resp.status_code != 200:
                logger.warning("team memory read skipped: KGS returned %s", resp.status_code)
                return None
            block = resp.json().get("context_block")
            return block or None
        except Exception as exc:  # noqa: BLE001 — fail-soft: a read can never hurt a run
            logger.warning("team memory read skipped (%s): %s", type(exc).__name__, exc)
            return None
