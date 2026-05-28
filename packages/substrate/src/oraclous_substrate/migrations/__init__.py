"""Substrate data migrations (ORA-24 / D1).

One-time, idempotent migrations that bring an existing deployment's substrate data
up to the organisation-scoped shape A1 declares. Each migration is paired with a
rollback. See ``org_backfill`` for the organisation backfill across Postgres,
Neo4j and Redis.
"""

from __future__ import annotations
