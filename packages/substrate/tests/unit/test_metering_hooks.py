"""Failing tests for the substrate metering emission hooks (ORA-22, story C2).

C1 (ORA-21) built the append-only usage-event stream primitive
(``oraclous_substrate.usage.UsageEventStream.emit``). C2 wires *emission* into
the metered actions: it turns each metered op into a well-formed usage event and
emits it through that single stream. Per the R0.5 release page deliverable 5 and
ADR-009, metering is enforced **at the substrate level, not the application
level** — so the testable C2 surface is a substrate-level metering-hook layer
(``oraclous_substrate.metering``) that the future harness-runtime /
capability-registry / substrate-write call sites will invoke. Those services are
still empty scaffolds (R2/R4/R5), so these tests pin the hook layer, not the
service wiring.

The four metered actions (R0.5 deliverable 5 / ADR-009 "metered actions"):

* **model tokens** — LLM token consumption, split by kind (input/output). Lifts
  the prompt/completion **token capture** from
  ``knowledge-graph-builder/app/services/chat_history_service.py`` /
  ``llm_pricing.py``.
* **tool invocation** — capability invocation credits. Lifts the per-tool
  ``calculate_credits`` signal from
  ``oraclous-core-service/app/tools/base/internal_tool.py`` (L57-62) and the
  ``credits_consumed`` accumulation in ``instance_repository``.
* **storage write** — bytes written (net-new).
* **cross-workspace traversal** — traversal count + bytes (net-new).

Two deliberate interpretations are pinned here and flagged for the Tests Review
gate (be-test-reviewer / solution-architect):

1. **Tokens/credits are emitted as the raw metered *signal*, never as priced
   USD.** ADR-009 makes billing a *separable downstream consumer*; pricing
   (legacy ``estimate_cost_usd``'s USD table) belongs to billing, not the
   substrate. So the lift is the token *counts* and the *credits* unit — not the
   currency cost. The hooks emit ``unit="tokens"`` / ``unit="credits"``, and
   never a currency unit. If the gate wants USD emitted at the substrate, these
   tests change.
2. **Failure classes are distinguished.** A *pipeline* failure (the durable
   write / queue is unavailable) is non-blocking: the metered op completes and
   the event is captured for replay (R0.5 Risks: "if the metering pipeline
   fails, the operation still completes but the metering record is logged for
   replay"). A *fail-closed tenancy* failure (no organisation context bound) is
   NOT a replayable pipeline error — it must propagate, never be swallowed
   (CLAUDE.md §3.5 fail-closed; Threat Catalogue T1-M1). A blanket
   ``except Exception`` around emit would wrongly swallow the latter; these tests
   forbid that.

Threats pinned: **T7-M1** (every substrate state change emits a structured
event — the single validated emit path), **T7-M3** (append-only/tamper-evident,
inherited from C1), and **T1-M1** (identity from the bound context, never a
caller-supplied ``organisation_id``).

RED until backend-implementer creates ``oraclous_substrate.metering``.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable, Coroutine
from decimal import Decimal
from typing import Any

import pytest
from oraclous_governance import (
    MissingOrganisationContextError,
    OrganisationContext,
    PrincipalType,
    use_organisation_context,
)
from oraclous_substrate.metering import (
    CROSS_WORKSPACE_TRAVERSAL,
    MODEL_TOKENS,
    STORAGE_WRITE,
    TOOL_INVOCATION,
    MeteringHook,
)
from oraclous_substrate.usage import UsageEvent, UsageEventStream

pytestmark = [pytest.mark.unit]

WORKSPACE_ID = "ws-alpha"
TARGET_WORKSPACE_ID = "ws-beta"

# The four canonical metered action types C2 emits. The literal action_type
# strings are owned by the implementer (these are imported constants, not
# hard-coded literals); the *set* of metered actions is the contract.
_ALL_ACTION_TYPES = frozenset(
    {MODEL_TOKENS, TOOL_INVOCATION, STORAGE_WRITE, CROSS_WORKSPACE_TRAVERSAL}
)

# Units must be raw metering signal, never a priced currency (ADR-009).
_CURRENCY_UNITS = frozenset({"usd", "USD", "$", "dollars", "cents"})


class _RecordingStore:
    """In-memory usage-event store double — records every successful append."""

    def __init__(self) -> None:
        self.writes: list[UsageEvent] = []

    async def write(self, event: UsageEvent) -> None:
        self.writes.append(event)

    async def read(self, organisation_id: uuid.UUID) -> list[UsageEvent]:
        return [e for e in self.writes if e.organisation_id == organisation_id]


class _PipelineDownStore:
    """Store whose durable write always fails — stands in for a metering
    pipeline / queue outage (R0.5 Risks). Emission through this store raises an
    *operational* error, which the hook must absorb (non-blocking) and route to
    the replay log."""

    def __init__(self) -> None:
        self.attempts = 0

    async def write(self, event: UsageEvent) -> None:
        self.attempts += 1
        raise RuntimeError("usage pipeline unavailable")

    async def read(self, organisation_id: uuid.UUID) -> list[UsageEvent]:
        raise RuntimeError("usage pipeline unavailable")


class _RecordingReplayLog:
    """Replay sink double. The real replay queue (backpressure-safe, per R0.5
    Risks) is the implementer's choice; the contract pinned here is only that a
    failed emission is handed to ``record`` carrying the four emit fields so it
    can be replayed later."""

    def __init__(self) -> None:
        self.recorded: list[Any] = []

    async def record(self, pending: Any) -> None:
        self.recorded.append(pending)


def _context(organisation_id: uuid.UUID | None = None) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=organisation_id or uuid.uuid4(),
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


def _hook() -> tuple[MeteringHook, _RecordingStore, _RecordingReplayLog]:
    store = _RecordingStore()
    replay = _RecordingReplayLog()
    hook = MeteringHook(stream=UsageEventStream(store), replay=replay)
    return hook, store, replay


def _meter_calls(hook: MeteringHook) -> dict[str, Callable[[], Coroutine[Any, Any, Any]]]:
    """One representative invocation per metered action, for cross-cutting AC1
    assertions that must hold for *every* metered op."""
    return {
        MODEL_TOKENS: lambda: hook.meter_model_tokens(
            workspace_id=WORKSPACE_ID,
            model="claude-opus-4-7",
            prompt_tokens=100,
            completion_tokens=20,
        ),
        TOOL_INVOCATION: lambda: hook.meter_tool_invocation(
            workspace_id=WORKSPACE_ID,
            capability_id="notion-reader",
            credits=0.1,
        ),
        STORAGE_WRITE: lambda: hook.meter_storage_write(
            workspace_id=WORKSPACE_ID,
            byte_count=2048,
        ),
        CROSS_WORKSPACE_TRAVERSAL: lambda: hook.meter_cross_workspace_traversal(
            workspace_id=WORKSPACE_ID,
            target_workspace_id=TARGET_WORKSPACE_ID,
            count=3,
            byte_count=4096,
        ),
    }


# --------------------------------------------------------------------------- #
# AC1 — every metered op emits a usage event scoped to org-context + workspace
# --------------------------------------------------------------------------- #


@pytest.mark.audit
async def test_every_metered_action_emits_org_scoped_event_with_workspace() -> None:
    """AC1 invariant: each metered op emits at least one usage event whose
    organisation_id is the bound context's and whose dimensions carry the
    workspace_id (T7-M1 emission; the event has no workspace_id field of its own,
    so it rides in dimensions)."""
    ctx = _context()
    for action_type, call in _meter_calls(_hook()[0]).items():
        store = _RecordingStore()
        replay = _RecordingReplayLog()
        hook = MeteringHook(stream=UsageEventStream(store), replay=replay)
        with use_organisation_context(ctx):
            await _meter_calls(hook)[action_type]()

        assert store.writes, f"{action_type} emitted no usage event"
        for event in store.writes:
            assert event.organisation_id == ctx.organisation_id
            assert event.dimensions.get("workspace_id") == WORKSPACE_ID
        assert replay.recorded == []


@pytest.mark.audit
async def test_model_tokens_emits_one_event_per_captured_kind() -> None:
    """Lift fidelity: the prompt/completion split from legacy chat capture
    becomes one token event per kind (input/output), quantity = the token count,
    unit = tokens, model carried as a dimension."""
    hook, store, _ = _hook()
    with use_organisation_context(_context()):
        await hook.meter_model_tokens(
            workspace_id=WORKSPACE_ID,
            model="claude-opus-4-7",
            prompt_tokens=100,
            completion_tokens=20,
        )

    by_kind = {e.dimensions.get("token_kind"): e for e in store.writes}
    assert set(by_kind) == {"input", "output"}
    assert all(e.unit == "tokens" for e in store.writes)
    assert all(e.action_type == MODEL_TOKENS for e in store.writes)
    assert all(e.dimensions.get("model") == "claude-opus-4-7" for e in store.writes)
    assert by_kind["input"].quantity == 100
    assert by_kind["output"].quantity == 20


@pytest.mark.audit
async def test_model_tokens_skips_uncaptured_kind() -> None:
    """A token kind that was not captured (None, mirroring legacy unknown
    counts) emits no event for that kind — metering records what was measured,
    not a fabricated zero."""
    hook, store, _ = _hook()
    with use_organisation_context(_context()):
        await hook.meter_model_tokens(
            workspace_id=WORKSPACE_ID,
            model="claude-opus-4-7",
            prompt_tokens=100,
            completion_tokens=None,
        )

    assert len(store.writes) == 1
    assert store.writes[0].dimensions.get("token_kind") == "input"
    assert store.writes[0].quantity == 100


@pytest.mark.audit
async def test_tool_invocation_emits_credits_event() -> None:
    """Lift fidelity: the per-tool ``calculate_credits`` signal becomes one
    credits usage event keyed to the capability, quantity = credits, unit =
    credits."""
    hook, store, _ = _hook()
    with use_organisation_context(_context()):
        await hook.meter_tool_invocation(
            workspace_id=WORKSPACE_ID, capability_id="notion-reader", credits=0.1
        )

    assert len(store.writes) == 1
    event = store.writes[0]
    assert event.action_type == TOOL_INVOCATION
    assert event.unit == "credits"
    assert event.quantity == pytest.approx(0.1)
    assert event.dimensions.get("capability_id") == "notion-reader"


@pytest.mark.audit
async def test_tool_invocation_accepts_legacy_decimal_credits() -> None:
    """The legacy credit signal is a ``Decimal``; the stream's quantity contract
    is numeric (int/float). The reshape converts the lifted ``Decimal`` to a
    numeric quantity rather than re-deriving credits at the substrate."""
    hook, store, _ = _hook()
    with use_organisation_context(_context()):
        await hook.meter_tool_invocation(
            workspace_id=WORKSPACE_ID,
            capability_id="postgresql-reader",
            credits=Decimal("0.1"),
        )

    assert len(store.writes) == 1
    assert store.writes[0].quantity == pytest.approx(0.1)


@pytest.mark.audit
async def test_storage_write_emits_bytes_event() -> None:
    """Net-new: a substrate write emits a bytes usage event (ADR-009 storage
    writes are 'rated by bytes')."""
    hook, store, _ = _hook()
    with use_organisation_context(_context()):
        await hook.meter_storage_write(workspace_id=WORKSPACE_ID, byte_count=2048)

    assert len(store.writes) == 1
    event = store.writes[0]
    assert event.action_type == STORAGE_WRITE
    assert event.unit == "bytes"
    assert event.quantity == 2048


@pytest.mark.audit
async def test_cross_workspace_traversal_emits_count_and_bytes() -> None:
    """Net-new: a cross-workspace traversal emits a traversal event recording
    the traversal count (quantity) and the bytes traversed (dimension), naming
    both the acting (source) workspace and the target workspace."""
    hook, store, _ = _hook()
    with use_organisation_context(_context()):
        await hook.meter_cross_workspace_traversal(
            workspace_id=WORKSPACE_ID,
            target_workspace_id=TARGET_WORKSPACE_ID,
            count=3,
            byte_count=4096,
        )

    assert len(store.writes) == 1
    event = store.writes[0]
    assert event.action_type == CROSS_WORKSPACE_TRAVERSAL
    assert event.unit == "traversals"
    assert event.quantity == 3
    assert event.dimensions.get("workspace_id") == WORKSPACE_ID
    assert event.dimensions.get("target_workspace_id") == TARGET_WORKSPACE_ID
    assert event.dimensions.get("bytes") == 4096


# --------------------------------------------------------------------------- #
# ADR-009 — substrate emits raw signal, billing prices it downstream
# --------------------------------------------------------------------------- #


@pytest.mark.audit
async def test_no_metered_action_emits_a_priced_currency_unit() -> None:
    """ADR-009 (billing separable): the substrate emits the raw metered signal
    (tokens/credits/bytes/traversals), never a priced currency. No emitted unit
    is a currency, and no dimension smuggles a USD cost in."""
    ctx = _context()
    store = _RecordingStore()
    hook = MeteringHook(stream=UsageEventStream(store), replay=_RecordingReplayLog())
    with use_organisation_context(ctx):
        for call in _meter_calls(hook).values():
            await call()

    assert store.writes
    for event in store.writes:
        assert event.unit not in _CURRENCY_UNITS
        assert not any(
            "usd" in str(k).lower() or "cost" in str(k).lower() for k in event.dimensions
        )


# --------------------------------------------------------------------------- #
# T1-M1 — identity from the bound context, never a caller-supplied org id
# --------------------------------------------------------------------------- #


@pytest.mark.security
@pytest.mark.parametrize(
    "method_name",
    [
        "meter_model_tokens",
        "meter_tool_invocation",
        "meter_storage_write",
        "meter_cross_workspace_traversal",
    ],
)
def test_no_hook_method_accepts_an_organisation_id(method_name: str) -> None:
    """T1-M1: no metering hook accepts an ``organisation_id`` parameter. The
    organisation is sourced from the bound context (via the C1 stream), so there
    is no caller channel through which a tenant scope could be smuggled."""
    method = getattr(MeteringHook, method_name)
    params = set(inspect.signature(method).parameters)
    assert "organisation_id" not in params
    assert "organization_id" not in params


@pytest.mark.security
async def test_emitted_org_is_the_bound_context_org() -> None:
    """T1-M1: the metered event is scoped to the bound context's organisation,
    proving the hook does not (and cannot) attribute usage to another org."""
    ctx = _context()
    other_org = uuid.uuid4()
    hook, store, _ = _hook()
    with use_organisation_context(ctx):
        await hook.meter_storage_write(workspace_id=WORKSPACE_ID, byte_count=64)

    assert store.writes[0].organisation_id == ctx.organisation_id
    assert store.writes[0].organisation_id != other_org


# --------------------------------------------------------------------------- #
# AC4 — pipeline failure is non-blocking; the event is captured for replay
# --------------------------------------------------------------------------- #


@pytest.mark.audit
async def test_pipeline_failure_does_not_propagate() -> None:
    """AC4: if the metering pipeline fails, the hook absorbs it (does not raise),
    so the already-completed metered operation is never broken by metering."""
    pipeline_down = _PipelineDownStore()
    replay = _RecordingReplayLog()
    hook = MeteringHook(stream=UsageEventStream(pipeline_down), replay=replay)
    with use_organisation_context(_context()):
        await hook.meter_storage_write(workspace_id=WORKSPACE_ID, byte_count=2048)

    assert pipeline_down.attempts == 1  # emission was attempted


@pytest.mark.audit
async def test_pipeline_failure_is_captured_for_replay() -> None:
    """AC4: a failed emission is handed to the replay log carrying the four emit
    fields, so it can be replayed once the pipeline recovers."""
    pipeline_down = _PipelineDownStore()
    replay = _RecordingReplayLog()
    hook = MeteringHook(stream=UsageEventStream(pipeline_down), replay=replay)
    with use_organisation_context(_context()):
        await hook.meter_storage_write(workspace_id=WORKSPACE_ID, byte_count=2048)

    assert len(replay.recorded) == 1
    pending = replay.recorded[0]
    assert pending.action_type == STORAGE_WRITE
    assert pending.quantity == 2048
    assert pending.unit == "bytes"
    assert pending.dimensions.get("workspace_id") == WORKSPACE_ID


@pytest.mark.audit
async def test_each_failed_facet_is_replayed_independently() -> None:
    """AC4: model-token metering emits one event per kind; if the pipeline is
    down, *each* failed facet is captured for replay (none is silently lost)."""
    pipeline_down = _PipelineDownStore()
    replay = _RecordingReplayLog()
    hook = MeteringHook(stream=UsageEventStream(pipeline_down), replay=replay)
    with use_organisation_context(_context()):
        await hook.meter_model_tokens(
            workspace_id=WORKSPACE_ID,
            model="claude-opus-4-7",
            prompt_tokens=100,
            completion_tokens=20,
        )

    kinds = {p.dimensions.get("token_kind") for p in replay.recorded}
    assert kinds == {"input", "output"}


@pytest.mark.audit
async def test_successful_emit_does_not_touch_replay_log() -> None:
    """AC4 guard: the replay path is only for failures. A successful emission
    must not enqueue a replay (else every event would be double-counted)."""
    hook, store, replay = _hook()
    with use_organisation_context(_context()):
        await hook.meter_storage_write(workspace_id=WORKSPACE_ID, byte_count=2048)

    assert len(store.writes) == 1
    assert replay.recorded == []


@pytest.mark.security
async def test_missing_context_is_not_swallowed_as_a_pipeline_failure() -> None:
    """Fail-closed (CLAUDE.md §3.5, T1-M1): a missing organisation context is a
    tenancy violation, not a replayable pipeline error. It must propagate — never
    be swallowed by the non-blocking path — and nothing is written or replayed.
    This forbids a blanket ``except Exception`` around emit."""
    store = _RecordingStore()
    replay = _RecordingReplayLog()
    hook = MeteringHook(stream=UsageEventStream(store), replay=replay)
    with pytest.raises(MissingOrganisationContextError):
        await hook.meter_storage_write(workspace_id=WORKSPACE_ID, byte_count=2048)

    assert store.writes == []
    assert replay.recorded == []
