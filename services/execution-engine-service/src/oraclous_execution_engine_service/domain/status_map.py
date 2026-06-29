"""Harness → engine status mapping (domain layer) — pure, no I/O.

A synchronous harness run returns ``HarnessExecutionOut.status`` ∈ {SUCCEEDED, PARTIAL, FAILED,
ESCALATED}. This maps that terminal/wait outcome onto the engine's job state. (TIMED_OUT/CANCELLED
are engine concerns — a wall-clock budget or a caller cancel — never reported by the harness, so
they are not produced here.)
"""

from __future__ import annotations

from oraclous_execution_engine_service.models.enums import EngineJobState

_MAP: dict[str, EngineJobState] = {
    "SUCCEEDED": EngineJobState.SUCCEEDED,
    # #580/#587: a degrade-completed harness run is a real terminal — NOT a fail-closed FAILED.
    "PARTIAL": EngineJobState.PARTIAL,
    "FAILED": EngineJobState.FAILED,
    "ESCALATED": EngineJobState.ESCALATED,
}


def map_harness_status(harness_status: str) -> EngineJobState:
    """Map a harness run status to an engine job state; an unknown status fails closed → FAILED."""
    return _MAP.get(harness_status, EngineJobState.FAILED)
