"""Legacy un-org-scoped Redis query-cache keys do not survive the migration (D1).

RED until ``backend-implementer`` adds ``oraclous_substrate.migrations.org_backfill``.

The legacy query cache keyed entries as ``qcache:{graph_id}:{sha256}`` — graph the
only tenant scope. A1 reshaped the key to
``qcache:{organisation_id}:{graph_id}:{digest}`` (``oraclous_substrate.cache_keys``).
A legacy entry **cannot** be backfilled in place: its key carries only the query
*hash*, so the new org-then-graph key (which hashes the query *text*) cannot be
recomputed from it. The migration therefore takes the cold-start/flush route —
it removes the legacy un-scoped ``qcache`` entries so nothing stale can be read
across the new prefix (AC#4). It must be scoped to the cache namespace: a blanket
flush of unrelated keys (sessions, provenance, …) is a bug.

Asserted on the real Redis harness. ``redis_client`` is created with
``decode_responses=True`` (keys come back as ``str``).

Migration contract under test (to be implemented):
  ``migrate_redis_cache(redis_client) -> None``
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]


def _migrate(redis_client) -> None:
    from oraclous_substrate.migrations import org_backfill

    org_backfill.migrate_redis_cache(redis_client)


@pytest.fixture
def seeded_redis(redis_client):
    """Seed legacy ``qcache:{graph}:{sha}`` entries + a control non-cache key.

    Yields ``(redis_client, graph, legacy_keys, control_key)``. The graph id is
    unique per test so assertions are deterministic on the session-shared server.
    """
    graph = str(uuid.uuid4())
    legacy_keys = [f"qcache:{graph}:{'a' * 64}", f"qcache:{graph}:{'b' * 64}"]
    control_key = f"ora24-keep:{uuid.uuid4()}"
    for key in legacy_keys:
        redis_client.set(key, "stale-answer")
    redis_client.set(control_key, "keep-me")
    try:
        yield redis_client, graph, legacy_keys, control_key
    finally:
        redis_client.delete(control_key, *legacy_keys)
        for key in redis_client.keys(f"qcache:{graph}:*"):
            redis_client.delete(key)


def test_migration_removes_legacy_unscoped_cache_keys(seeded_redis) -> None:
    """AC#4: no legacy ``qcache:{graph}:{sha}`` entry survives (no stale cross-prefix read)."""
    redis_client, graph, legacy_keys, _control = seeded_redis
    _migrate(redis_client)
    assert redis_client.exists(*legacy_keys) == 0, "legacy un-org-scoped cache keys survived"
    leftover = redis_client.keys(f"qcache:{graph}:*")
    assert leftover == [], f"legacy-format qcache keys remain after migration: {leftover}"


def test_migration_does_not_touch_non_cache_keys(seeded_redis) -> None:
    """The flush is scoped to the cache namespace — unrelated keys are preserved."""
    redis_client, _graph, _legacy_keys, control_key = seeded_redis
    _migrate(redis_client)
    assert redis_client.get(control_key) == "keep-me", "migration clobbered a non-cache key"


def test_migration_is_idempotent(seeded_redis) -> None:
    """AC#1: re-running the cache migration neither raises nor resurrects legacy keys."""
    redis_client, graph, legacy_keys, control_key = seeded_redis
    _migrate(redis_client)
    _migrate(redis_client)  # second run must be a safe no-op
    assert redis_client.exists(*legacy_keys) == 0
    assert redis_client.keys(f"qcache:{graph}:*") == []
    assert redis_client.get(control_key) == "keep-me"
