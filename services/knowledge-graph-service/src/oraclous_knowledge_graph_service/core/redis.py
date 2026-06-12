"""Redis client factory + advisory per-(org,graph) lock (ORAA-4 §21 core layer) — connection setup
and the SET-NX-EX primitive, no business logic.

A SYNC ``redis.Redis`` used for the per-(org,graph) locks (community-detect #303, code-ingest +
stale-sweep #305). The repositories/services/tasks are sync (sync Neo4j driver, called via
``asyncio.to_thread``; the Celery worker is sync), so the lock client is sync too — a sync
``SET NX EX`` is the natural mutual-exclusion every path shares. The lock is ADVISORY: a ``None``
client (Redis unconfigured / unreachable) means the work still runs, just without cross-run mutual
exclusion (logged at acquire), exactly like the retriever's advisory query cache (#308).

``RedisLock`` is the shared SET-NX-EX helper so the community detect (#303), the code-ingest
critical section, and the code-stale sweep (#305) all serialise the same way without each
re-implementing the token-matched acquire/release. It takes the (already-built) client by duck-type
— the redis driver import lives ONLY here in core/, so the services/tasks layers never import a
driver (STR004).
"""

from __future__ import annotations

import logging
import uuid

from oraclous_knowledge_graph_service.core.config import Settings

logger = logging.getLogger(__name__)

# Sentinel token returned when there is no Redis (or acquire degraded): the caller "holds" an
# unlocked lock so the work still proceeds, and release is a no-op for it.
_NO_LOCK = "no-lock"


def make_redis_lock_client(settings: Settings) -> object | None:
    """Build a sync Redis client for the advisory locks, or ``None`` when Redis is unreachable.

    Never raises: a connection that cannot be established degrades to ``None`` (lock disabled) so a
    Redis outage never takes the locked work down — it only loses the concurrency guard.
    """
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 — advisory: a Redis fault disables the lock, not work
        logger.warning("advisory Redis lock disabled: Redis unavailable (%s)", exc)
        return None


class RedisLock:
    """Advisory SET-NX-EX lock over a duck-typed sync Redis client (``set``/``get``/``delete``).

    ``acquire`` returns a token (the random owner id, or ``_NO_LOCK`` when there is no client / a
    Redis fault) on success, or ``None`` when the key is already held by someone else. ``release``
    only deletes the key when WE still own it (token match), so a TTL-expired-then-retaken lock is
    never released out from under another holder. Every Redis call is wrapped: the lock is advisory,
    so a fault degrades to "unlocked", never an exception into the caller.
    """

    def __init__(self, client: object | None, *, key: str, ttl_seconds: int) -> None:
        self._client = client
        self._key = key
        self._ttl = ttl_seconds

    def acquire(self) -> str | None:
        if self._client is None:
            return _NO_LOCK
        token = uuid.uuid4().hex
        try:
            acquired = self._client.set(self._key, token, nx=True, ex=self._ttl)
        except Exception as exc:  # noqa: BLE001 — advisory; a Redis fault must not block the work
            logger.warning("advisory lock acquire failed (%s) — proceeding unlocked", exc)
            return _NO_LOCK
        return token if acquired else None

    def is_held(self) -> bool:
        """True iff the key is currently set (the sweep uses it to skip a mid-ingest graph).

        A ``None`` client or a Redis fault reports "not held" so the work degrades to lock-off."""
        if self._client is None:
            return False
        try:
            return self._client.get(self._key) is not None
        except Exception as exc:  # noqa: BLE001 — advisory: a read fault degrades to "not held"
            logger.warning("advisory lock probe failed (%s) — treating as not held", exc)
            return False

    def release(self, token: str | None) -> None:
        if self._client is None or token in (None, _NO_LOCK):
            return
        try:
            current = self._client.get(self._key)
            held = current.decode() if isinstance(current, bytes) else current
            if held == token:
                self._client.delete(self._key)
        except Exception as exc:  # noqa: BLE001 — release is cleanup; let the TTL reap it otherwise
            logger.warning("advisory lock release skipped (%s)", exc)
