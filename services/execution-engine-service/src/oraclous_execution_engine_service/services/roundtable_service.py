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

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.roundtable import EngineRoundtable
from oraclous_execution_engine_service.repositories.maintenance_repository import (
    EngineMaintenanceRepository,
)
from oraclous_execution_engine_service.repositories.roundtable_repository import (
    RoundtableRepository,
)
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
)

_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
# Hard ceiling on total turns (max_rounds × actors) so one driver task can't fan out into thousands
# of harness calls and get SIGKILLed by the Celery time limit, stranding the round-table RUNNING.
_MAX_TOTAL_TURNS = 64
_MAX_ENTRY_OUTPUT = 4000  # bound each transcript entry so the replayed context can't grow unbounded
# A driver hand-off: (roundtable_id, organisation_id, user_id) → fire the worker task. Injected.
EnqueueFn = Callable[[uuid.UUID, uuid.UUID, uuid.UUID], None]


class RoundtableError(Exception):
    """A round-table could not be created/advanced (missing, bad shape, wrong state). Carries the
    HTTP status: 400 bad request (default), 404 not found, 409 wrong state."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class RoundtableService:
    def __init__(
        self,
        *,
        roundtables: RoundtableRepository,
        provenance: ProvenanceCollector,
        harness: HarnessClient | None = None,
        enqueue: EnqueueFn | None = None,
        maintenance: EngineMaintenanceRepository | None = None,
    ) -> None:
        self._roundtables = roundtables
        self._provenance = provenance
        self._harness = harness  # the worker (drive) needs this
        self._enqueue = enqueue  # the request path (create/respond) needs this
        # ADR-030 §3: the reaper path injects the OWNER-engine cross-org reader. The stale-RUNNING
        # enumeration reads ACROSS orgs on it (FORCE'd RLS on the org-bound engine would fail it
        # closed); each stranded round-table is re-queued on the ORG-BOUND `roundtables` repo under
        # org_scope(rt.org). None on the request/drive path (those are org-bound).
        self._maintenance = maintenance

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
        if max_rounds * len(actors) > _MAX_TOTAL_TURNS:
            raise RoundtableError(
                f"max_rounds × actors must not exceed {_MAX_TOTAL_TURNS} total turns"
            )
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
            raise RoundtableError("round-table not found", 404)
        # Single-driver claim: only a QUEUED round-table may start driving. A concurrent or
        # redelivered driver (acks_late) that finds it already RUNNING/ESCALATED/terminal no-ops —
        # so turns are never double-run and the transcript can't suffer a lost update.
        claimed, ok = await self._roundtables.transition(
            rt.id, org_id, new_state="RUNNING", allowed_from=frozenset({"QUEUED"})
        )
        if not ok:
            return claimed or rt
        rt = claimed

        actors = rt.actors
        n = len(actors)
        total_turns = rt.max_rounds * n
        transcript = list(rt.transcript or [])
        turn = rt.current_turn

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
                    "output": _bounded(result.get("output")),
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
            raise RoundtableError("round-table not found", 404)
        if rt.state != "ESCALATED":
            raise RoundtableError("round-table is not awaiting a human turn", 409)
        actor = rt.actors[rt.current_turn % len(rt.actors)]
        if actor.get("kind") != "human":
            raise RoundtableError("the current turn is not a human turn", 409)
        transcript = list(rt.transcript or [])
        transcript.append(
            {
                "turn": rt.current_turn,
                "role": actor.get("role"),
                "kind": "human",
                "output": _bounded(output),
            }
        )
        # CAS ESCALATED→QUEUED so a concurrent double-respond applies exactly once (the loser 409s).
        updated, ok = await self._roundtables.transition(
            rt.id,
            org_id,
            new_state="QUEUED",
            allowed_from=frozenset({"ESCALATED"}),
            current_turn=rt.current_turn + 1,
            transcript=transcript,
        )
        if not ok:
            raise RoundtableError("round-table is not awaiting a human turn", 409)
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

    async def reap_stale(self, *, older_than: object) -> int:
        """System sweep (with the job reaper): re-queue round-tables stuck RUNNING past the
        lease — a driver that died mid-turn — so a fresh driver re-claims them. The CAS claim makes
        the re-drive idempotent. Each row is re-queued + re-enqueued under its OWN org.

        ADR-030 §3 two-engine carve: the stale-RUNNING ENUMERATION reads across orgs on the OWNER
        engine (``self._maintenance``) — FORCE'd RLS on the org-bound engine would fail it closed to
        zero rows. Each row's RUNNING→QUEUED CAS is then applied on the ORG-BOUND
        ``self._roundtables`` repo INSIDE ``org_scope(rt.org)`` (RLS WITH CHECK admits it; a
        cross-org write is denied 42501). The row's org comes from the trusted maintenance read,
        never request input."""
        reader = self._maintenance
        if reader is None:  # the reaper path always injects it; fail loud if mis-wired
            raise RoundtableError("reap_stale requires the maintenance (cross-org) reader")
        reaped = 0
        for rt in await reader.list_stale_roundtables(older_than):
            try:
                with org_scope(rt.organisation_id):
                    _, ok = await self._roundtables.transition(
                        rt.id,
                        rt.organisation_id,
                        new_state="QUEUED",
                        allowed_from=frozenset({"RUNNING"}),
                    )
                if ok and self._enqueue is not None:
                    self._enqueue(rt.id, rt.organisation_id, rt.user_id)
                    reaped += 1
            except Exception:  # noqa: BLE001, S112 — best-effort maintenance; skip the row, continue
                continue
        return reaped

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


def _bounded(value: object) -> str | None:
    """Bound a transcript entry's output so the replayed context (and the JSONB row) can't grow
    without bound across turns."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= _MAX_ENTRY_OUTPUT else text[:_MAX_ENTRY_OUTPUT] + "…"


def _render_context(topic: str, transcript: list[dict[str, Any]]) -> str:
    """The shared context fed to each agent turn: the topic + the transcript so far."""
    lines = [f"Topic: {topic}", ""]
    for entry in transcript:
        lines.append(f"[{entry.get('role')} ({entry.get('kind')})]: {entry.get('output')}")
    lines.append("")
    lines.append("Contribute the next turn of the discussion.")
    return "\n".join(lines)
