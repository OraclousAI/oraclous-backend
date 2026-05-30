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

The four metered actions (ADR-009 Decision §5):

* **model tokens** — LLM token consumption, split by kind (input/output).
  ``quantity = token count``, ``unit = tokens``, model carried as a dimension.
  Lifts the prompt/completion **token capture** from
  ``knowledge-graph-builder/app/services/chat_history_service.py`` /
  ``llm_pricing.py`` (the *signal*, not the USD rate table).
* **capability invocation** — a tool / capability call.
  ``action_type = capability_invocation``, ``quantity = 1``, ``unit = count``;
  the **rating signals** (``tool_id``, ``operation``, ``row_count``, ``bytes``)
  ride as dimensions, never as the emitted quantity. Lifts the *cost-driver
  capture* from ``oraclous-core-service`` — explicitly **not** the per-tool
  ``calculate_credits`` rate arithmetic.
* **storage write** — bytes written (net-new). ``quantity = byte_count``,
  ``unit = bytes``.
* **cross-workspace traversal** — traversal count + bytes traversed (net-new).
  ``quantity = count``, ``unit = count``, bytes as a dimension.

Two architect rulings are **settled** on this story and pinned by this suite:

1. **No priced/rated unit at the substrate.** Per ADR-009 ruling
   (solution-architect, ORA-22 comments 10133 + 10167), the substrate emits the
   raw cost-driving signal only — never USD and never *credits*. ``credits`` is
   a per-operation rated unit (the legacy ``calculate_credits`` is a rate table,
   not a count), categorically identical to USD for ADR-009 purposes: a price
   book that lives in the metering path makes a pricing change a substrate
   change. The rate arithmetic lives in the downstream billing/rater, not here.
   Pinned by ``test_no_metered_action_emits_a_priced_or_rated_unit`` and
   ``test_capability_invocation_emits_raw_cost_driver_dimensions``.
2. **Failure classes are distinguished** (security-architect ruling, ORA-22
   comment 10134). A *pipeline* failure (the durable write / queue is
   unavailable) is non-blocking: the metered op completes, the event is captured
   for replay. A *missing org-context* is a T1-M1 fail-closed tenancy violation
   and must propagate — never be swallowed by the replay path. A blanket
   ``except Exception`` around emit collapses the two and is forbidden. Pinned
   by ``test_missing_context_is_not_swallowed_as_a_pipeline_failure``.

Threats pinned: **T7-M1** (every substrate state change emits via the single
validated emit path), **T7-M3** (append-only / tamper-evident, inherited from
C1), and **T1-M1** (identity from the bound context, never a caller-supplied
``organisation_id``; metered ops with no org scope halt rather than emit
unattributed events).

RED until backend-implementer creates ``oraclous_substrate.metering``.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from oraclous_governance import (
    MissingOrganisationContextError,
    OrganisationContext,
    PrincipalType,
    use_organisation_context,
)
from oraclous_substrate.metering import (
    CAPABILITY_INVOCATION,
    CROSS_WORKSPACE_TRAVERSAL,
    MODEL_TOKENS,
    STORAGE_WRITE,
    MeteringHook,
)
from oraclous_substrate.usage import UsageEvent, UsageEventStream

pytestmark = [pytest.mark.unit]

WORKSPACE_ID = "ws-alpha"
TARGET_WORKSPACE_ID = "ws-beta"

# The four canonical metered actions C2 emits. The literal action_type strings
# are owned by the implementer (these are imported constants, not hard-coded
# literals); the *set* of metered actions is the contract.
_ALL_ACTION_TYPES = frozenset(
    {MODEL_TOKENS, CAPABILITY_INVOCATION, STORAGE_WRITE, CROSS_WORKSPACE_TRAVERSAL}
)

# Priced or rated units the substrate must NOT emit (ADR-009 ruling 10167).
# USD is a currency; credits is the platform's rated billing unit. Both belong
# downstream of the substrate, not on a usage event.
_FORBIDDEN_PRICED_UNITS = frozenset({"usd", "USD", "$", "dollars", "cents", "credits", "credit"})

# Dimension-key fragments that would smuggle a priced/rated number onto a usage
# event (cost, price, credits). The substrate emits raw cost-driver signal only.
_FORBIDDEN_DIMENSION_KEY_FRAGMENTS = ("usd", "cost", "price", "credit")


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
        CAPABILITY_INVOCATION: lambda: hook.meter_capability_invocation(
            workspace_id=WORKSPACE_ID,
            tool_id="postgresql-reader",
            operation="query",
            row_count=42,
            byte_count=1024,
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
    for action_type in _meter_calls(_hook()[0]):
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
async def test_capability_invocation_emits_raw_cost_driver_dimensions() -> None:
    """Lift fidelity (ADR-009 ruling 10167): the *signal* lifted from legacy
    tool execution is the raw cost-driver capture — ``tool_id``, ``operation``,
    and the per-operation rating inputs (``row_count``, ``bytes``) — carried as
    dimensions. The substrate does NOT lift the per-tool ``calculate_credits``
    rate arithmetic and does NOT emit a credit-rated quantity; the downstream
    rater (billing) applies the rate table to these raw dimensions.

    Mirrors a representative legacy case
    (``postgresql_reader.calculate_credits`` for ``operation=query`` with
    ``row_count=N``): the substrate emits the dimensions; the
    ``0.1 + N*0.001`` arithmetic does not live here."""
    hook, store, _ = _hook()
    with use_organisation_context(_context()):
        await hook.meter_capability_invocation(
            workspace_id=WORKSPACE_ID,
            tool_id="postgresql-reader",
            operation="query",
            row_count=42,
            byte_count=1024,
        )

    assert len(store.writes) == 1
    event = store.writes[0]
    assert event.action_type == CAPABILITY_INVOCATION
    assert event.unit == "count"
    assert event.quantity == 1  # one invocation
    assert event.dimensions.get("tool_id") == "postgresql-reader"
    assert event.dimensions.get("operation") == "query"
    assert event.dimensions.get("row_count") == 42
    assert event.dimensions.get("bytes") == 1024


@pytest.mark.audit
async def test_capability_invocation_omits_unsupplied_cost_drivers() -> None:
    """Optional cost-driver dimensions (``row_count``, ``bytes``) are emitted
    only when supplied — they are not invented as zeros. A minimal invocation
    (``tool_id`` + ``operation`` only) still emits the action with
    ``quantity = 1`` and carries no fabricated rating signal."""
    hook, store, _ = _hook()
    with use_organisation_context(_context()):
        await hook.meter_capability_invocation(
            workspace_id=WORKSPACE_ID,
            tool_id="list-tables-tool",
            operation="list_tables",
        )

    assert len(store.writes) == 1
    event = store.writes[0]
    assert event.action_type == CAPABILITY_INVOCATION
    assert event.unit == "count"
    assert event.quantity == 1
    assert event.dimensions.get("tool_id") == "list-tables-tool"
    assert event.dimensions.get("operation") == "list_tables"
    assert "row_count" not in event.dimensions
    assert "bytes" not in event.dimensions


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
    """Net-new: a cross-workspace traversal emits a substrate-operations event
    (ADR-009 "substrate operations rated by count") with ``quantity = count`` and
    ``unit = count``, naming both the acting (source) workspace and the target,
    and carrying bytes traversed as a dimension."""
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
    assert event.unit == "count"
    assert event.quantity == 3
    assert event.dimensions.get("workspace_id") == WORKSPACE_ID
    assert event.dimensions.get("target_workspace_id") == TARGET_WORKSPACE_ID
    assert event.dimensions.get("bytes") == 4096


# --------------------------------------------------------------------------- #
# ADR-009 ruling 10167 — substrate emits raw signal; no priced/rated units
# --------------------------------------------------------------------------- #


@pytest.mark.audit
async def test_no_metered_action_emits_a_priced_or_rated_unit() -> None:
    """ADR-009 ruling (comments 10133 + 10167): the substrate emits the raw
    metered signal only — never a priced currency (USD) and never the rated
    ``credits`` unit (the legacy ``calculate_credits`` rate table is billing,
    not metering). No emitted unit is currency or credits, and no dimension
    smuggles a cost / price / credits number in. All units fall in ADR-009's
    canonical ``{tokens, count, bytes}`` vocabulary."""
    ctx = _context()
    store = _RecordingStore()
    hook = MeteringHook(stream=UsageEventStream(store), replay=_RecordingReplayLog())
    with use_organisation_context(ctx):
        for call in _meter_calls(hook).values():
            await call()

    assert store.writes
    for event in store.writes:
        assert event.unit not in _FORBIDDEN_PRICED_UNITS
        assert event.unit in {"tokens", "count", "bytes"}
        for key in event.dimensions:
            assert not any(
                fragment in str(key).lower() for fragment in _FORBIDDEN_DIMENSION_KEY_FRAGMENTS
            )


@pytest.mark.security
def test_capability_invocation_does_not_accept_a_rated_quantity_parameter() -> None:
    """ADR-009 ruling 10167: the rate arithmetic (``calculate_credits``) lives
    in the downstream rater, not the substrate hook. So the capability-invocation
    method must not accept a pre-rated quantity (``credits``, ``cost``,
    ``price``, ``rate``) as a parameter — the only inputs it takes are the raw
    cost-driver signals (``tool_id``, ``operation``, ``row_count``,
    ``byte_count``). This forbids reintroducing the rate table by the back door."""
    params = set(inspect.signature(MeteringHook.meter_capability_invocation).parameters)
    for forbidden in ("credits", "credit", "cost", "cost_usd", "price", "rate", "rated_quantity"):
        assert forbidden not in params, (
            f"meter_capability_invocation must not accept a rated/priced parameter "
            f"({forbidden!r}); ratings belong downstream of the substrate"
        )


# --------------------------------------------------------------------------- #
# T1-M1 — identity from the bound context, never a caller-supplied org id
# --------------------------------------------------------------------------- #


@pytest.mark.security
@pytest.mark.parametrize(
    "method_name",
    [
        "meter_model_tokens",
        "meter_capability_invocation",
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
    """Fail-closed (CLAUDE.md §3.5, T1-M1; security-architect ruling 10134): a
    missing organisation context is a tenancy violation, not a replayable
    pipeline error. It must propagate — never be swallowed by the non-blocking
    path — and nothing is written or replayed. This forbids a blanket
    ``except Exception`` around emit; transport/durable-write failures must be
    caught narrowly so ``MissingOrganisationContextError`` (and any other
    tenancy/authorization error) propagates."""
    store = _RecordingStore()
    replay = _RecordingReplayLog()
    hook = MeteringHook(stream=UsageEventStream(store), replay=replay)
    with pytest.raises(MissingOrganisationContextError):
        await hook.meter_storage_write(workspace_id=WORKSPACE_ID, byte_count=2048)

    assert store.writes == []
    assert replay.recorded == []
