"""Round-table coordination (ORAA-4 §21 services layer).

Drives N actors (agents + humans) over ONE shared transcript, turn by turn — no new execution
primitive. ``create`` records the round-table + enqueues the driver; the worker ``drive`` runs each
agent turn through the harness (feeding the accumulated transcript as the turn's input) and appends
the result, until it hits a HUMAN turn (pause ESCALATED) or completes ``max_rounds`` (SUCCEEDED). A
human answers via ``respond``, which appends their output and re-enqueues the driver. Org from the
principal only (ADR-006); a provenance event per turn + lifecycle change (§3.7).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_execution_engine_service.models.roundtable import EngineRoundtable
from oraclous_execution_engine_service.repositories.roundtable_repository import (
    RoundtableRepository,
)
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
)

_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
# A driver hand-off: (roundtable_id, organisation_id, user_id) → fire the worker task. Injected.
EnqueueFn = Callable[[uuid.UUID, uuid.UUID, uuid.UUID], None]


class RoundtableError(Exception):
    """A round-table could not be created/advanced (missing, bad shape, wrong state). HTTP 4xx."""


class RoundtableService:
    def __init__(
        self,
        *,
        roundtables: RoundtableRepository,
        provenance: ProvenanceCollector,
        harness: HarnessClient | None = None,
        enqueue: EnqueueFn | None = None,
    ) -> None:
        self._roundtables = roundtables
        self._provenance = provenance
        self._harness = harness  # the worker (drive) needs this
        self._enqueue = enqueue  # the request path (create/respond) needs this

    async def create(
        self,
        principal: Principal,
        *,
        topic: str,
        actors: list[dict[str, Any]],
        max_rounds: int,
    ) -> EngineRoundtable:
        org_id = self._require_org(principal)
        if not actors:
            raise RoundtableError("a round-table needs at least one actor")
        for actor in actors:
            kind = actor.get("kind")
            if kind not in ("agent", "human"):
                raise RoundtableError("each actor needs kind 'agent' or 'human'")
            if kind == "agent" and not (actor.get("manifest") or actor.get("manifest_ref")):
                raise RoundtableError("an agent actor needs a manifest or manifest_ref")
        row = await self._roundtables.create(
            organisation_id=org_id,
            user_id=principal.principal_id,
            topic=topic,
            actors=actors,
            max_rounds=max_rounds,
        )
        await self._emit(
            org_id, principal.principal_id, row.id, "engine.roundtable.create", "QUEUED"
        )
        if self._enqueue is not None:
            self._enqueue(row.id, org_id, principal.principal_id)
        return row

    async def get(self, roundtable_id: uuid.UUID, principal: Principal) -> EngineRoundtable | None:
        return await self._roundtables.get(roundtable_id, self._require_org(principal))

    async def drive(self, roundtable_id: uuid.UUID, principal: Principal) -> EngineRoundtable:
        """Worker entrypoint: run agent turns until a human turn pauses it or it completes."""
        org_id = self._require_org(principal)
        if self._harness is None:
            raise RoundtableError("no harness client configured")
        rt = await self._roundtables.get(roundtable_id, org_id)
        if rt is None:
            raise RoundtableError("round-table not found")
        if rt.state in _TERMINAL:
            return rt

        actors = rt.actors
        n = len(actors)
        total_turns = rt.max_rounds * n
        transcript = list(rt.transcript or [])
        turn = rt.current_turn
        await self._roundtables.update(rt.id, org_id, state="RUNNING")

        while turn < total_turns:
            actor = actors[turn % n]
            if actor.get("kind") == "human":  # pause for the human to respond
                await self._roundtables.update(
                    rt.id, org_id, state="ESCALATED", current_turn=turn, transcript=transcript
                )
                await self._emit(
                    org_id, rt.user_id, rt.id, "engine.roundtable.turn", f"{turn}:human:awaiting"
                )
                return await self._roundtables.get(rt.id, org_id) or rt

            context = _render_context(rt.topic, transcript)
            try:
                result = await self._harness.execute(
                    input_text=context,
                    manifest_inline=actor.get("manifest"),
                    manifest_ref=actor.get("manifest_ref"),
                )
            except HarnessClientError as exc:
                return await self._fail(rt, org_id, transcript, turn, f"agent turn failed: {exc}")
            if result.get("status") != "SUCCEEDED":
                detail = result.get("error_message") or result.get("status")
                return await self._fail(rt, org_id, transcript, turn, f"agent turn: {detail}")
            transcript.append(
                {
                    "turn": turn,
                    "role": actor.get("role"),
                    "kind": "agent",
                    "output": result.get("output"),
                }
            )
            turn += 1
            await self._roundtables.update(rt.id, org_id, current_turn=turn, transcript=transcript)
            await self._emit(
                org_id, rt.user_id, rt.id, "engine.roundtable.turn", f"{turn - 1}:agent:done"
            )

        final = transcript[-1]["output"] if transcript else None
        await self._emit(org_id, rt.user_id, rt.id, "engine.roundtable.complete", "SUCCEEDED")
        return await self._roundtables.update(
            rt.id,
            org_id,
            state="SUCCEEDED",
            current_turn=turn,
            transcript=transcript,
            final_output=final,
        )

    async def respond(
        self, roundtable_id: uuid.UUID, principal: Principal, output: str
    ) -> EngineRoundtable:
        """A human answers the paused turn; append it + re-enqueue the driver to continue."""
        org_id = self._require_org(principal)
        rt = await self._roundtables.get(roundtable_id, org_id)
        if rt is None:
            raise RoundtableError("round-table not found")
        if rt.state != "ESCALATED":
            raise RoundtableError("round-table is not awaiting a human turn")
        actor = rt.actors[rt.current_turn % len(rt.actors)]
        if actor.get("kind") != "human":
            raise RoundtableError("the current turn is not a human turn")
        transcript = list(rt.transcript or [])
        transcript.append(
            {"turn": rt.current_turn, "role": actor.get("role"), "kind": "human", "output": output}
        )
        updated = await self._roundtables.update(
            rt.id,
            org_id,
            current_turn=rt.current_turn + 1,
            transcript=transcript,
            state="QUEUED",
        )
        await self._emit(
            org_id,
            principal.principal_id,
            rt.id,
            "engine.roundtable.turn",
            f"{rt.current_turn}:human:done",
        )
        if self._enqueue is not None:
            self._enqueue(rt.id, org_id, principal.principal_id)
        return updated or rt

    async def _fail(
        self,
        rt: EngineRoundtable,
        org_id: uuid.UUID,
        transcript: list[dict[str, Any]],
        turn: int,
        message: str,
    ) -> EngineRoundtable:
        await self._emit(org_id, rt.user_id, rt.id, "engine.roundtable.fail", "FAILED")
        return await self._roundtables.update(
            rt.id,
            org_id,
            state="FAILED",
            current_turn=turn,
            transcript=transcript,
            error_message=message[:2000],
        )

    @staticmethod
    def _require_org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:
            raise RoundtableError("authenticated principal has no organisation scope")
        return principal.organisation_id

    async def _emit(
        self,
        org_id: uuid.UUID,
        principal_id: uuid.UUID,
        roundtable_id: uuid.UUID,
        action: str,
        outcome: str,
    ) -> None:
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=str(principal_id),
                action=action,
                resource=f"engine_roundtable:{roundtable_id}",
                outcome=outcome,
            )
        )


def _render_context(topic: str, transcript: list[dict[str, Any]]) -> str:
    """The shared context fed to each agent turn: the topic + the transcript so far."""
    lines = [f"Topic: {topic}", ""]
    for entry in transcript:
        lines.append(f"[{entry.get('role')} ({entry.get('kind')})]: {entry.get('output')}")
    lines.append("")
    lines.append("Contribute the next turn of the discussion.")
    return "\n".join(lines)
