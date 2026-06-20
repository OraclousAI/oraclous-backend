"""Strict judge-response parsing (lifted from the KRS evaluation posture, #331/#333).

A malformed or out-of-range judge response fails THAT dimension (fail-soft null + warning) rather
than fabricating a score. Never clamp-fabricate from NaN/Infinity, and never accept a score outside
[0, 1].
"""

from __future__ import annotations

import json
import math


class JudgeResponseError(ValueError):
    """The judge returned output a dimension step could not parse → that dimension nulls."""


def parse_json_object(raw: str) -> dict:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise JudgeResponseError(f"judge response was not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise JudgeResponseError("judge response was not a JSON object")
    return obj


def parse_score(raw: str, *, key: str = "score") -> float:
    """The judged score in [0, 1]. Rejects non-numeric, NaN/Inf, and out-of-range (no clamp-fab)."""
    value = parse_json_object(raw).get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise JudgeResponseError(f"judge {key!r} was not a number: {value!r}")
    score = float(value)
    if math.isnan(score) or math.isinf(score):
        raise JudgeResponseError(f"judge {key!r} was NaN/Inf")
    if not 0.0 <= score <= 1.0:
        raise JudgeResponseError(f"judge {key!r} {score} out of range [0, 1]")
    return round(score, 4)


def parse_reason(raw: str, *, key: str = "reason", default: str = "") -> str:
    """A short rationale string, or the default. Truncated defensively; callers must still ensure
    it carries no verbatim customer text (ADR-037 H5 / §3.7)."""
    value = parse_json_object(raw).get(key)
    return str(value)[:500] if isinstance(value, str) else default
