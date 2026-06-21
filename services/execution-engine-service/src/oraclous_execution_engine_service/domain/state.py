"""Engine job state machine (domain layer) — pure, no I/O.

The allowed transitions for an ``EngineJob``. Terminal states never transition again; ``ESCALATED``
is a wait state resolved by complete/approve/cancel; ``FAILED``/``TIMED_OUT`` may re-enter QUEUED
on a retry (S3). Centralised here so the service/worker enforce one transition table.
"""

from __future__ import annotations

from oraclous_execution_engine_service.models.enums import EngineJobState

TERMINAL: frozenset[EngineJobState] = frozenset(
    {
        EngineJobState.SUCCEEDED,
        EngineJobState.FAILED,
        EngineJobState.TIMED_OUT,
        EngineJobState.CANCELLED,
    }
)

_ALLOWED: dict[EngineJobState, frozenset[EngineJobState]] = {
    # QUEUED → FAILED covers a submit that couldn't enqueue (no orphaned QUEUED row).
    EngineJobState.QUEUED: frozenset(
        {EngineJobState.RUNNING, EngineJobState.FAILED, EngineJobState.CANCELLED}
    ),
    EngineJobState.RUNNING: frozenset(
        {
            EngineJobState.SUCCEEDED,
            EngineJobState.FAILED,
            EngineJobState.ESCALATED,
            EngineJobState.TIMED_OUT,
            EngineJobState.CANCELLED,
        }
    ),
    # ESCALATED resolves to a terminal outcome, or re-queues for a resumed run.
    EngineJobState.ESCALATED: frozenset(
        {
            EngineJobState.SUCCEEDED,
            EngineJobState.FAILED,
            EngineJobState.CANCELLED,
            EngineJobState.QUEUED,
        }
    ),
    # FAILED / TIMED_OUT may re-enter QUEUED on a retry (S3).
    EngineJobState.FAILED: frozenset({EngineJobState.QUEUED}),
    EngineJobState.TIMED_OUT: frozenset({EngineJobState.QUEUED}),
    EngineJobState.SUCCEEDED: frozenset(),
    EngineJobState.CANCELLED: frozenset(),
}


def is_terminal(state: EngineJobState) -> bool:
    return state in TERMINAL


def can_transition(current: EngineJobState, target: EngineJobState) -> bool:
    return target in _ALLOWED.get(current, frozenset())


def sources_for(target: EngineJobState) -> frozenset[EngineJobState]:
    """States that may transition INTO ``target`` — the allowed-from set for a CAS guard."""
    return frozenset(s for s in EngineJobState if can_transition(s, target))
