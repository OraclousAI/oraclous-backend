"""Substrate metering emission hooks (Layer 1, ADR-009).

Each metered action is turned into a ``UsageEvent`` and emitted through the
``UsageEventStream`` — the single, validated emit path for the substrate's
usage stream. Substrate-level per ADR-009 ("Metering at Substrate, Billing as
Separable"): the substrate emits the raw cost-driving signal (token counts,
raw tool invocations, bytes, traversal counts), never priced amounts. A
downstream rater applies the rate book to that signal. A price book living in
the metering path would couple the substrate to pricing and diverge between
self-hosted (no billing) and cloud-hosted modes.

Identity (``organisation_id``, ``principal``) is sourced from the ambient
organisation-context by the stream itself — no metering hook accepts
``organisation_id`` as a parameter, so there is no caller channel through
which a tenant scope could be smuggled (Structured Threat Catalogue T1-M1).
``workspace_id`` is a sub-organisation scope and rides on each event as a
dimension; the ``UsageEvent`` schema has no ``workspace_id`` field of its own
(ADR-009 §4).

Failure handling — two distinct classes, handled oppositely:

* **Pipeline failure** — the durable write / queue is unavailable. The metered
  operation has already completed; metering itself must not take it down. The
  failed event is absorbed and handed to the injected ``UsageReplayLog`` to be
  replayed once the pipeline recovers.
* **Missing organisation-context** — a metered op reaching the hook with no
  bound tenancy scope is a fail-closed violation (T1-M1) and must propagate.
  Swallowing it would let an op complete with unattributed usage and would
  hide a tenancy gap inside the replay queue. The pipeline catch is therefore
  ordered after an explicit re-raise of ``MissingOrganisationContextError``;
  collapsing the two classes into a blanket ``except`` is forbidden.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from oraclous_governance import MissingOrganisationContextError

from oraclous_substrate.usage import DimensionValue, UsageEventStream

# Canonical action types this hook layer emits. ADR-009 §4 enumerates units as
# ``{tokens, count, bytes}`` — every event below stays inside that vocabulary.
MODEL_TOKENS = "model.tokens"
CAPABILITY_INVOCATION = "capability.invocation"
STORAGE_WRITE = "storage.write"
CROSS_WORKSPACE_TRAVERSAL = "substrate.traversal"


@dataclass(frozen=True, slots=True)
class PendingUsageEvent:
    """A usage event whose durable emission failed, captured for replay.

    Carries the four emit fields so a replay consumer can resubmit it without
    losing attribution. Frozen so a queued event cannot be mutated in flight.
    """

    action_type: str
    quantity: float
    unit: str
    dimensions: Mapping[str, DimensionValue]


class UsageReplayLog(Protocol):
    """Sink for usage events whose durable emission failed.

    The concrete implementation (a backpressure-safe queue, persistent buffer,
    dead-letter topic, etc.) is the operator's choice; this seam pins only that
    a failed emission is handed off here with the four emit fields intact.
    """

    async def record(self, pending: PendingUsageEvent) -> None: ...


class MeteringHook:
    """The substrate-level entry point wiring metered actions into the stream.

    Each ``meter_*`` method computes the right ``(action_type, quantity, unit,
    dimensions)`` for one metered action and emits one usage event per facet
    (model-token metering emits per kind). The stream's ``emit`` is the single
    validated write path; no hook bypasses it.
    """

    def __init__(self, *, stream: UsageEventStream, replay: UsageReplayLog) -> None:
        self._stream = stream
        self._replay = replay

    async def _emit(
        self,
        *,
        action_type: str,
        quantity: float,
        unit: str,
        dimensions: Mapping[str, DimensionValue],
    ) -> None:
        try:
            await self._stream.emit(
                action_type=action_type,
                quantity=quantity,
                unit=unit,
                dimensions=dimensions,
            )
        except MissingOrganisationContextError:
            # T1-M1 fail-closed: a missing org scope must halt, never widen
            # into the replay path. MissingOrganisationContextError is a
            # RuntimeError subclass, so this re-raise must come before any
            # broader catch below or it would be silently swallowed.
            raise
        except Exception:
            # Pipeline / durable-write failure — absorb and queue for replay
            # so the metered operation, which has already completed, is not
            # broken by an emission outage (R0.5 Risks).
            await self._replay.record(
                PendingUsageEvent(
                    action_type=action_type,
                    quantity=float(quantity),
                    unit=unit,
                    dimensions=dict(dimensions),
                )
            )

    async def meter_model_tokens(
        self,
        *,
        workspace_id: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        """Emit one usage event per captured token kind for an LLM call.

        ``unit = tokens``, ``quantity = token count``; the model identifier and
        the token kind ride as dimensions. Uncaptured kinds (``None``) emit
        nothing — metering records what was measured, not a fabricated zero.
        """
        for kind, count in (("input", prompt_tokens), ("output", completion_tokens)):
            if count is None:
                continue
            await self._emit(
                action_type=MODEL_TOKENS,
                quantity=count,
                unit="tokens",
                dimensions={
                    "workspace_id": workspace_id,
                    "model": model,
                    "token_kind": kind,
                },
            )

    async def meter_capability_invocation(
        self,
        *,
        workspace_id: str,
        tool_id: str,
        operation: str,
        row_count: int | None = None,
        byte_count: int | None = None,
    ) -> None:
        """Emit one usage event for a tool / capability invocation.

        ``quantity`` is always 1 — the invocation itself — and ``unit`` is
        ``count``. The raw rating signals (``tool_id``, ``operation``,
        ``row_count``, ``bytes``) ride as dimensions; a downstream rater
        applies the rate book to them. No rated quantity is ever emitted from
        the substrate.
        """
        dimensions: dict[str, DimensionValue] = {
            "workspace_id": workspace_id,
            "tool_id": tool_id,
            "operation": operation,
        }
        if row_count is not None:
            dimensions["row_count"] = row_count
        if byte_count is not None:
            dimensions["bytes"] = byte_count
        await self._emit(
            action_type=CAPABILITY_INVOCATION,
            quantity=1,
            unit="count",
            dimensions=dimensions,
        )

    async def meter_storage_write(self, *, workspace_id: str, byte_count: int) -> None:
        """Emit one usage event for a substrate write, rated by bytes."""
        await self._emit(
            action_type=STORAGE_WRITE,
            quantity=byte_count,
            unit="bytes",
            dimensions={"workspace_id": workspace_id},
        )

    async def meter_cross_workspace_traversal(
        self,
        *,
        workspace_id: str,
        target_workspace_id: str,
        count: int,
        byte_count: int,
    ) -> None:
        """Emit one usage event for a cross-workspace traversal.

        ``unit = count`` — substrate operations are rated by count (ADR-009 §4).
        Names both the acting (source) workspace and the target; bytes
        traversed ride as a dimension so a downstream consumer can rate either
        facet.
        """
        await self._emit(
            action_type=CROSS_WORKSPACE_TRAVERSAL,
            quantity=count,
            unit="count",
            dimensions={
                "workspace_id": workspace_id,
                "target_workspace_id": target_workspace_id,
                "bytes": byte_count,
            },
        )
