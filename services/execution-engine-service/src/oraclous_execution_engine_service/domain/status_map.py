"""Harness → engine status mapping (domain layer) — pure, no I/O.

A synchronous harness run returns ``HarnessExecutionOut.status`` ∈ {SUCCEEDED, PARTIAL, FAILED,
ESCALATED}. This maps that terminal/wait outcome onto the engine's job state. TIMED_OUT is purely
an engine concern (a wall-clock budget) and is never produced here. CANCELLED, unlike the module
docstring once claimed, CAN now be reported by the harness (#1072): a CONFIRMED
``HarnessClient.cancel()`` response carries ``status: "CANCELLED"`` in the same shape ``execute()``
returns, so a caller that feeds a cancel result through this map (none does today — job_service.py
does not yet call ``cancel()`` on its own TIMED_OUT path, a documented follow-up) gets the correct
terminal instead of the fail-closed FAILED default.
"""

from __future__ import annotations

from oraclous_execution_engine_service.models.enums import EngineJobState

_MAP: dict[str, EngineJobState] = {
    "SUCCEEDED": EngineJobState.SUCCEEDED,
    # #580/#587: a degrade-completed harness run is a real terminal — NOT a fail-closed FAILED.
    "PARTIAL": EngineJobState.PARTIAL,
    "FAILED": EngineJobState.FAILED,
    "ESCALATED": EngineJobState.ESCALATED,
    # #1072: a CONFIRMED cancel is a real terminal, not an unknown status falling to FAILED.
    "CANCELLED": EngineJobState.CANCELLED,
}


def map_harness_status(harness_status: str) -> EngineJobState:
    """Map a harness run status to an engine job state; an unknown status fails closed → FAILED."""
    return _MAP.get(harness_status, EngineJobState.FAILED)
