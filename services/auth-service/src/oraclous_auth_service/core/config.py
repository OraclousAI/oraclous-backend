"""Auth-service settings loader (ORA-31).

Reads JWT secret + algorithm from the environment. Kept deliberately small —
the tests pin behaviour, not config wiring, and they use environment variables
to override the JWT secret (see ``test_agent_token_issuance.py::_read_token_settings``).

The settings object is recomputed on every access via :func:`get_settings` so
tests can mutate ``os.environ`` between calls. Production usage should treat
``get_settings()`` as cheap and call it where the value is needed rather than
caching it at module load (which would freeze the test secret on first import).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """Auth-service configuration resolved from the environment."""

    jwt_secret: str
    jwt_algorithm: str
    agent_token_ttl_minutes: int


def get_settings() -> Settings:
    """Return a freshly resolved :class:`Settings` snapshot."""
    return Settings(
        jwt_secret=os.environ.get("JWT_SECRET", "change-me-in-production"),
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
        # 15-minute cap mirrors the legacy ``_SA_TOKEN_EXPIRE_MINUTES`` and is
        # pinned by ``test_agent_token_is_short_lived_capped_at_fifteen_minutes``.
        agent_token_ttl_minutes=int(os.environ.get("AGENT_TOKEN_TTL_MINUTES", "15")),
    )
