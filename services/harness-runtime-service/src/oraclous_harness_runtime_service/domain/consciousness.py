"""ADR-043 #554 (Flow-6 Learn) — the coded, single-run consciousness pattern classifier.

Deterministic + NEVER a model self-grade (the CTO's never-self-grade posture): a small CODED read of
ONE completed run into a within-run family. The genuinely cross-run families (hand-off friction,
recurring ambiguity, repetitive-solutions-across-stories) need the consciousness doc's nightly
sweep across runs — a DEFERRED follow-up, not this single-run path.
"""

from __future__ import annotations

from collections import Counter

#: an over-long single run (many loop rounds) — a velocity/process signal worth recording
_VELOCITY_ROUNDS = 20


def classify_consciousness_pattern(
    *,
    status: str,
    tool_names: list[str],
    tool_errors: list[str],
    rounds: int = 0,
) -> str | None:
    """Classify a completed run into ONE within-run consciousness family, or ``None``.

    Priority: a SUCCESS is the most APPLICABLE compounding lesson (a future run retrieves the
    approach), so it wins even over length; then a recurring in-run failure; then an over-long run.
    """
    if status == "SUCCEEDED":
        return "solution"  # the reusable working approach — the compounding lesson
    if tool_errors and max(Counter(tool_errors).values()) >= 2:
        return "repetitive_failures"  # the same error recurred within this run
    if rounds >= _VELOCITY_ROUNDS:
        return "velocity_anomaly"  # an over-long run (a process/velocity signal)
    return None  # no within-run pattern (a cross-run sweep would surface one across many runs)
