"""Credential requirements declared by a tool descriptor (domain layer).

One reading of ``spec.credential_requirements``, shared by the execution spine (which resolves each
requirement before dispatch) and the readiness check (which resolves them to decide whether the
instance is actually usable). Keeping it in one place is what makes "healthy" and "executable" mean
the same thing — #692 was, in part, the two disagreeing.
"""

from __future__ import annotations

from typing import Any


def required_credentials(descriptor: dict[str, Any]) -> list[dict[str, Any]]:
    """The descriptor's REQUIRED credential requirements (an optional one never blocks a run)."""
    spec = descriptor.get("spec") or {}
    return [
        r
        for r in (spec.get("credential_requirements") or [])
        if isinstance(r, dict) and r.get("required", True)
    ]
