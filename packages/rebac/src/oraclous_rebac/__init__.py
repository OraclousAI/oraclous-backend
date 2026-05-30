"""oraclous-rebac — Layer 1 ReBAC engine (ORA-34) + substrate-seam adapter
(ORA-46).

Extracted from the legacy ``knowledge-graph-builder`` ReBAC service and
reshaped so every relation edge carries ``organisation_id`` (ADR-006). See
``engine.py`` for the engine and ``adapter.py`` for the seam adapter; this
module re-exports the public surface the ``packages/substrate`` seam and other
Layer-1 callers consume.
"""

from oraclous_rebac.adapter import ReBACEngineResolver
from oraclous_rebac.engine import (
    _ACCEPTABLE_LEVELS,
    _PERM_CACHE_TTL,
    _SYSTEM_ROLES,
    ReBACEngine,
)

__all__ = [
    "ReBACEngine",
    "ReBACEngineResolver",
    "_ACCEPTABLE_LEVELS",
    "_PERM_CACHE_TTL",
    "_SYSTEM_ROLES",
]
