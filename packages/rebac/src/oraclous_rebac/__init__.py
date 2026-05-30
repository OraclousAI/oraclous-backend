"""oraclous-rebac — Layer 1 ReBAC engine (ORA-34).

Extracted from the legacy ``knowledge-graph-builder`` ReBAC service and
reshaped so every relation edge carries ``organisation_id`` (ADR-006). See
``engine.py`` for the implementation; this module re-exports the public surface
the ``packages/substrate`` seam and other Layer-1 callers consume.
"""

from oraclous_rebac.engine import (
    _ACCEPTABLE_LEVELS,
    _PERM_CACHE_TTL,
    _SYSTEM_ROLES,
    ReBACEngine,
)

__all__ = [
    "ReBACEngine",
    "_ACCEPTABLE_LEVELS",
    "_PERM_CACHE_TTL",
    "_SYSTEM_ROLES",
]
