"""ReBAC engine — extracted from the legacy ``knowledge-graph-builder`` and
reshaped to scope every relation edge by ``organisation_id`` (ORA-34, ADR-006).

Behavioural reference: ``app/services/rebac_service.py`` in the legacy
codebase. Preserved here: cache→Phase B→Phase A resolution order, fail-closed
on any backend error, the 60-second permission cache, soft-revoke (no DELETE),
live-checked grant expiry, parameterised Cypher (injection-safe).

Reshape (the new contract pinned by the merged ORA-34 ``[tests]`` PR):

* ``organisation_id`` is the outermost scope on every entry point — passed as a
  keyword argument, validated non-blank, and bound into every Cypher query as
  ``$organisation_id`` so HAS_ROLE / CAN_ACCESS edges are written and filtered
  by it (Threat T1 — the tenant loop).
* the permission cache key is namespaced by ``organisation_id`` so a cached
  decision in one org can never satisfy a check in another.

This module deliberately does not adapt the engine into ``oraclous_substrate``'s
``resolve(AccessRequest) -> bool | None`` resolver protocol. The substrate seam
(ORA-15) consumes a resolver via dependency injection and is tested with a
test-double; whether to wire ``ReBACEngine`` as the production resolver is an
open question for the coordinator ``solution-architect`` and is deferred.

Out of scope for ORA-34 (not in the merged test suite, not in this impl):
schema initialisation / system-permission seeding / legacy Phase A sync,
``register_new_graph``, ``get_user_access_filter``, ``list_subgraphs``,
agent-delegation relations (C2). Add when their consuming stories land.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from neo4j import AsyncDriver

logger = logging.getLogger(__name__)

# ── Phase A acceptable-level hierarchy ──────────────────────────────────────
_ACCEPTABLE_LEVELS: dict[str, list[str]] = {
    "read": ["read", "write", "admin"],
    "write": ["write", "admin"],
    "admin": ["admin"],
}

_PERM_CACHE_TTL = 60  # seconds

# ── Phase B constants — built-in roles, permissions, inheritance ────────────
_SYSTEM_ROLES = ["owner", "admin", "editor", "viewer", "restricted_viewer"]

_SYSTEM_PERMISSIONS = [
    {"name": "graph:read", "resource_type": "graph", "action": "read"},
    {"name": "graph:write", "resource_type": "graph", "action": "write"},
    {"name": "graph:delete", "resource_type": "graph", "action": "delete"},
    {"name": "graph:manage_access", "resource_type": "graph", "action": "manage"},
    {"name": "entity:read", "resource_type": "entity", "action": "read"},
    {"name": "entity:write", "resource_type": "entity", "action": "write"},
    {"name": "entity:delete", "resource_type": "entity", "action": "delete"},
    {"name": "chunk:read", "resource_type": "chunk", "action": "read"},
    {"name": "document:read", "resource_type": "document", "action": "read"},
    {"name": "document:write", "resource_type": "document", "action": "write"},
    {"name": "session:read", "resource_type": "session", "action": "read"},
    {"name": "session:write", "resource_type": "session", "action": "write"},
    {"name": "pii:read", "resource_type": "entity", "action": "read"},
]

_ROLE_PERMISSIONS: dict[str, list[str]] = {
    "owner": [p["name"] for p in _SYSTEM_PERMISSIONS],
    "admin": [
        "graph:read",
        "graph:write",
        "graph:manage_access",
        "entity:read",
        "entity:write",
        "entity:delete",
        "chunk:read",
        "document:read",
        "document:write",
        "session:read",
        "session:write",
        "pii:read",
    ],
    "editor": [
        "graph:read",
        "graph:write",
        "entity:read",
        "entity:write",
        "chunk:read",
        "document:read",
        "document:write",
        "session:read",
        "session:write",
    ],
    "viewer": [
        "graph:read",
        "entity:read",
        "chunk:read",
        "document:read",
        "session:read",
        "pii:read",
    ],
    "restricted_viewer": [
        "graph:read",
        "entity:read",
        "chunk:read",
        "document:read",
        "session:read",
    ],
}

_LEVEL_TO_PERM: dict[str, str] = {
    "read": "graph:read",
    "write": "graph:write",
    "admin": "graph:manage_access",
}

_INHERITANCE: list[tuple[str, str]] = [
    ("owner", "admin"),
    ("admin", "editor"),
    ("editor", "viewer"),
    ("owner", "restricted_viewer"),
    ("admin", "restricted_viewer"),
]

_ROLE_DESCRIPTIONS: dict[str, str] = {
    "owner": "Full control including deletion and role management",
    "admin": "Read/write + manage members, cannot delete graph",
    "editor": "Read + write entities/relationships, cannot manage access",
    "viewer": "Read-only on all nodes/edges",
    "restricted_viewer": "Read-only on non-PII nodes only",
}


# ── Cypher templates ────────────────────────────────────────────────────────

_PHASE_B_PERM_QUERY = """
MATCH (u:User:__Platform__ {user_id: $user_id})
  -[hr:HAS_ROLE]->(r:Role:__System__ {graph_id: $graph_id, organisation_id: $organisation_id})
WHERE hr.graph_id = $graph_id
  AND hr.organisation_id = $organisation_id
  AND hr.is_active = true
  AND (hr.expires_at IS NULL OR hr.expires_at > datetime())

OPTIONAL MATCH (r)-[:HAS_PERMISSION|INHERITS_FROM*0..5]->
               (:Role)-[:HAS_PERMISSION]->(p1:Permission {name: $perm})

OPTIONAL MATCH (r)-[:HAS_PERMISSION]->(p2:Permission:__System__ {name: $perm})

WITH count(p1) + count(p2) AS perm_count
RETURN perm_count > 0 AS authorized
"""

_ROLE_EXISTS_QUERY = (
    "MATCH (r:Role:__System__ {graph_id: $graph_id, organisation_id: $organisation_id}) "
    "RETURN count(r) AS cnt LIMIT 1"
)

_PHASE_A_QUERY = """
WITH $user_id AS uid, $graph_id AS gid, $acceptable AS ok, $organisation_id AS oid

OPTIONAL MATCH (u:User:__Platform__ {user_id: uid, graph_id: "__system__"})
  -[r1:CAN_ACCESS]->(g1:Graph:__Rebac__ {graph_id: gid, namespace: "__system__"})
WHERE r1.level IN ok
  AND r1.organisation_id = oid
  AND (r1.expires_at IS NULL OR r1.expires_at > datetime())

OPTIONAL MATCH (u2:User:__Platform__ {user_id: uid, graph_id: "__system__"})
  -[:MEMBER_OF]->(:Team {graph_id: "__system__"})
  -[r2:CAN_ACCESS]->(g2:Graph:__Rebac__ {graph_id: gid, namespace: "__system__"})
WHERE r2.level IN ok
  AND r2.organisation_id = oid
  AND (r2.expires_at IS NULL OR r2.expires_at > datetime())

OPTIONAL MATCH (u3:User:__Platform__ {user_id: uid, graph_id: "__system__"})
  -[bt:BELONGS_TO {role: "owner"}]->(:Organization {graph_id: "__system__"})
  -[:OWNS]->(g3:Graph:__Rebac__ {graph_id: gid, namespace: "__system__"})
WHERE bt.organisation_id = oid

RETURN (u IS NOT NULL OR u2 IS NOT NULL OR u3 IS NOT NULL) AS authorized
"""

_GRANT_ROLE_QUERY = """
MERGE (u:User:__Platform__ {user_id: $user_id})
ON CREATE SET u.created_at = $now, u.is_service_account = false, u.email = $email
ON MATCH SET u.email = CASE WHEN $email IS NOT NULL THEN $email ELSE u.email END
WITH u
MATCH (r:Role:__System__ {graph_id: $graph_id, organisation_id: $organisation_id, name: $role_name})
MERGE (u)-[hr:HAS_ROLE {graph_id: $graph_id, organisation_id: $organisation_id}]->(r)
ON CREATE SET hr.granted_at = $now, hr.granted_by = $granted_by,
              hr.expires_at = $expires_at, hr.is_active = true
ON MATCH SET hr.granted_at = $now, hr.granted_by = $granted_by,
             hr.expires_at = $expires_at, hr.is_active = true
"""

_REVOKE_ROLE_QUERY = """
MATCH (u:User:__Platform__ {user_id: $user_id})
  -[hr:HAS_ROLE {graph_id: $graph_id, organisation_id: $organisation_id}]->
  (r:Role:__System__ {graph_id: $graph_id, organisation_id: $organisation_id, name: $role_name})
SET hr.is_active = false
RETURN count(hr) AS revoked_count
"""

_BOOTSTRAP_ROLE_QUERY = """
MERGE (r:Role:__System__ {graph_id: $graph_id, organisation_id: $organisation_id, name: $name})
ON CREATE SET r.role_id = $role_id, r.description = $description,
              r.is_system_role = true, r.created_at = $now,
              r.created_by = 'system', r.organisation_id = $organisation_id
ON MATCH SET r.role_id = CASE WHEN r.role_id IS NULL THEN $role_id ELSE r.role_id END
RETURN r.role_id AS role_id
"""

_BOOTSTRAP_PERM_EDGE_QUERY = """
MATCH (r:Role:__System__ {graph_id: $graph_id, organisation_id: $organisation_id, name: $role_name})
MATCH (p:Permission:__System__ {name: $perm_name})
MERGE (r)-[hp:HAS_PERMISSION]->(p)
ON CREATE SET hp.graph_id = $graph_id, hp.organisation_id = $organisation_id, hp.granted_at = $now
"""

_BOOTSTRAP_INHERIT_QUERY = """
MATCH (parent:Role:__System__ {
    graph_id: $graph_id, organisation_id: $organisation_id, name: $parent
})
MATCH (child:Role:__System__ {
    graph_id: $graph_id, organisation_id: $organisation_id, name: $child
})
MERGE (parent)-[i:INHERITS_FROM]->(child)
ON CREATE SET i.graph_id = $graph_id, i.organisation_id = $organisation_id, i.created_at = $now
"""

_BOOTSTRAP_OWNER_GRANT_QUERY = """
MERGE (u:User:__Platform__ {user_id: $user_id})
ON CREATE SET u.created_at = $now, u.is_service_account = false
WITH u
MATCH (r:Role:__System__ {graph_id: $graph_id, organisation_id: $organisation_id, name: 'owner'})
MERGE (u)-[hr:HAS_ROLE {graph_id: $graph_id, organisation_id: $organisation_id}]->(r)
ON CREATE SET hr.granted_at = $now, hr.granted_by = 'system',
              hr.expires_at = null, hr.is_active = true
ON MATCH SET hr.is_active = true
"""

_LIST_MEMBERS_QUERY = """
MATCH (u:User:__Platform__)
  -[hr:HAS_ROLE {graph_id: $graph_id, organisation_id: $organisation_id}]->
  (r:Role:__System__ {graph_id: $graph_id, organisation_id: $organisation_id})
WHERE hr.is_active = true
  AND (hr.expires_at IS NULL OR hr.expires_at > datetime())
RETURN u.user_id AS user_id, u.email AS email, r.name AS role,
       hr.granted_at AS granted_at, hr.expires_at AS expires_at
ORDER BY r.name, u.user_id
"""

_CREATE_SUBGRAPH_QUERY = """
MERGE (sg:SubGraph:__Platform__ {
    graph_id: $graph_id, organisation_id: $organisation_id, name: $name
})
ON CREATE SET sg.subgraph_id = $subgraph_id, sg.description = $description,
              sg.created_at = $now, sg.created_by = $created_by
RETURN sg.subgraph_id AS subgraph_id, sg.name AS name,
       sg.description AS description, sg.created_at AS created_at
"""


def _require_org(organisation_id: str) -> None:
    if not organisation_id or not organisation_id.strip():
        raise ValueError("organisation_id is required (ADR-006)")


def _require_graph(graph_id: str) -> None:
    if not graph_id:
        raise ValueError("graph_id is required")


class ReBACEngine:
    """The reshaped ReBAC engine. Async, scoped by ``organisation_id`` on every
    operation, backed by Neo4j (graph traversal) and Redis (60s permission
    cache). Callers inject a Neo4j ``AsyncDriver`` per call; a Redis client may
    be injected via ``self._redis`` or lazily constructed from ``REDIS_URL``.
    """

    def __init__(self, redis: Any | None = None) -> None:
        self._redis: Any | None = redis

    async def _get_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis  # local: production path only

            self._redis = await aioredis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
            )
        return self._redis

    # ── Permission resolution ────────────────────────────────────────────

    async def check_graph_permission(
        self,
        driver: AsyncDriver,
        *,
        organisation_id: str,
        user_id: str,
        graph_id: str,
        required_level: str,
    ) -> bool:
        """Return True iff ``user_id`` has at least ``required_level`` access to
        ``graph_id`` *within* ``organisation_id``. Resolution order: Redis
        cache → Phase B HAS_ROLE traversal → Phase A CAN_ACCESS fallback.
        Fail-closed on any backend error.
        """
        _require_org(organisation_id)
        _require_graph(graph_id)

        acceptable = _ACCEPTABLE_LEVELS.get(required_level, ["admin"])
        required_perm = _LEVEL_TO_PERM.get(required_level, "graph:manage_access")
        cache_key = f"perm:{organisation_id}:{user_id}:{graph_id}:{required_level}"

        redis: Any | None = None
        try:
            redis = await self._get_redis()
            cached = await redis.get(cache_key)
            if cached is not None:
                return cached == "1"
        except Exception as exc:  # pragma: no cover — covered behaviourally
            logger.warning("Redis permission cache read error: %s", exc)

        authorized = False

        # Phase B — HAS_ROLE traversal (ORA-48 model, org-scoped).
        try:
            async with driver.session() as session:
                result = await session.run(
                    _PHASE_B_PERM_QUERY,
                    {
                        "user_id": user_id,
                        "graph_id": graph_id,
                        "organisation_id": organisation_id,
                        "perm": required_perm,
                    },
                )
                record = await result.single()
                if record is not None:
                    authorized = bool(record["authorized"])
                    if redis is not None:
                        try:
                            await redis.set(
                                cache_key, "1" if authorized else "0", ex=_PERM_CACHE_TTL
                            )
                        except Exception as exc:  # cache write is best-effort
                            logger.debug("Redis cache write skipped: %s", exc)
                    role_check = await session.run(
                        _ROLE_EXISTS_QUERY,
                        {"graph_id": graph_id, "organisation_id": organisation_id},
                    )
                    role_record = await role_check.single()
                    if role_record and role_record["cnt"] > 0:
                        return authorized
        except Exception as exc:
            logger.error("ReBAC Phase B permission check error: %s", exc)

        # Phase A — CAN_ACCESS fallback, fail-closed on backend error.
        try:
            async with driver.session() as session:
                result = await session.run(
                    _PHASE_A_QUERY,
                    {
                        "user_id": user_id,
                        "graph_id": graph_id,
                        "organisation_id": organisation_id,
                        "acceptable": acceptable,
                    },
                )
                record = await result.single()
                authorized = bool(record and record["authorized"])
        except Exception as exc:
            logger.error("ReBAC Phase A permission check Neo4j error: %s", exc)
            return False

        if redis is not None:
            try:
                await redis.set(cache_key, "1" if authorized else "0", ex=_PERM_CACHE_TTL)
            except Exception as exc:  # cache write is best-effort
                logger.debug("Redis cache write skipped: %s", exc)

        return authorized

    async def invalidate_permission_cache(
        self, *, organisation_id: str, user_id: str, graph_id: str
    ) -> None:
        """Drop all cached permission entries for this (org, user, graph) triple."""
        try:
            redis = await self._get_redis()
            for level in ("read", "write", "admin"):
                await redis.delete(f"perm:{organisation_id}:{user_id}:{graph_id}:{level}")
        except Exception as exc:  # pragma: no cover
            logger.warning("Redis permission cache invalidation error: %s", exc)

    # ── Role management ──────────────────────────────────────────────────

    async def grant_role(
        self,
        driver: AsyncDriver,
        *,
        organisation_id: str,
        graph_id: str,
        target_user_id: str,
        role_name: str,
        granted_by: str,
        expires_at: str | None = None,
        email: str | None = None,
    ) -> None:
        """MERGE a HAS_ROLE edge scoped by ``organisation_id`` + ``graph_id``;
        invalidate the cache so a freshly granted role is not masked by a stale
        deny (AC#3).
        """
        _require_org(organisation_id)
        _require_graph(graph_id)

        async with driver.session() as session:
            await session.run(
                _GRANT_ROLE_QUERY,
                {
                    "user_id": target_user_id,
                    "graph_id": graph_id,
                    "organisation_id": organisation_id,
                    "role_name": role_name,
                    "granted_by": granted_by,
                    "expires_at": expires_at,
                    "email": email,
                    "now": _now(),
                },
            )
        await self.invalidate_permission_cache(
            organisation_id=organisation_id, user_id=target_user_id, graph_id=graph_id
        )

    async def revoke_role(
        self,
        driver: AsyncDriver,
        *,
        organisation_id: str,
        graph_id: str,
        target_user_id: str,
        role_name: str,
    ) -> int:
        """Soft-revoke: flip ``hr.is_active`` to false (never DELETE) so a
        revoked grant is preserved for audit. Returns the count of edges
        revoked (0 = no matching edge). Invalidates the cache.
        """
        _require_org(organisation_id)
        _require_graph(graph_id)

        async with driver.session() as session:
            result = await session.run(
                _REVOKE_ROLE_QUERY,
                {
                    "user_id": target_user_id,
                    "graph_id": graph_id,
                    "organisation_id": organisation_id,
                    "role_name": role_name,
                },
            )
            record = await result.single()
            count = int(record["revoked_count"]) if record else 0

        await self.invalidate_permission_cache(
            organisation_id=organisation_id, user_id=target_user_id, graph_id=graph_id
        )
        return count

    # ── Bootstrap a graph's role topology ────────────────────────────────

    async def bootstrap_graph_roles(
        self,
        driver: AsyncDriver,
        *,
        organisation_id: str,
        graph_id: str,
        owner_user_id: str,
    ) -> None:
        """Create the 5 built-in Role nodes for ``graph_id`` inside
        ``organisation_id``, wire HAS_PERMISSION edges to system Permission
        nodes, set up INHERITS_FROM chain, and grant ``owner`` to
        ``owner_user_id``. Idempotent (all MERGE). Every Cypher query carries
        ``organisation_id``.
        """
        _require_org(organisation_id)
        _require_graph(graph_id)

        now = _now()
        async with driver.session() as session:
            for role_name in _SYSTEM_ROLES:
                await session.run(
                    _BOOTSTRAP_ROLE_QUERY,
                    {
                        "graph_id": graph_id,
                        "organisation_id": organisation_id,
                        "name": role_name,
                        "role_id": str(uuid4()),
                        "description": _ROLE_DESCRIPTIONS[role_name],
                        "now": now,
                    },
                )

            for role_name, perms in _ROLE_PERMISSIONS.items():
                for perm_name in perms:
                    await session.run(
                        _BOOTSTRAP_PERM_EDGE_QUERY,
                        {
                            "graph_id": graph_id,
                            "organisation_id": organisation_id,
                            "role_name": role_name,
                            "perm_name": perm_name,
                            "now": now,
                        },
                    )

            for parent, child in _INHERITANCE:
                await session.run(
                    _BOOTSTRAP_INHERIT_QUERY,
                    {
                        "graph_id": graph_id,
                        "organisation_id": organisation_id,
                        "parent": parent,
                        "child": child,
                        "now": now,
                    },
                )

            await session.run(
                _BOOTSTRAP_OWNER_GRANT_QUERY,
                {
                    "user_id": owner_user_id,
                    "graph_id": graph_id,
                    "organisation_id": organisation_id,
                    "now": now,
                },
            )

        await self.invalidate_permission_cache(
            organisation_id=organisation_id, user_id=owner_user_id, graph_id=graph_id
        )

    # ── Member + SubGraph queries ────────────────────────────────────────

    async def list_graph_members(
        self, driver: AsyncDriver, *, organisation_id: str, graph_id: str
    ) -> list[dict]:
        _require_org(organisation_id)
        _require_graph(graph_id)

        async with driver.session() as session:
            result = await session.run(
                _LIST_MEMBERS_QUERY,
                {"graph_id": graph_id, "organisation_id": organisation_id},
            )
            return [
                {
                    "user_id": r["user_id"],
                    "email": r["email"],
                    "role": r["role"],
                    "granted_at": str(r["granted_at"]) if r["granted_at"] else None,
                    "expires_at": str(r["expires_at"]) if r["expires_at"] else None,
                }
                async for r in result
            ]

    async def create_subgraph(
        self,
        driver: AsyncDriver,
        *,
        organisation_id: str,
        graph_id: str,
        name: str,
        description: str | None = None,
        created_by: str | None = None,
    ) -> dict:
        _require_org(organisation_id)
        _require_graph(graph_id)

        now = _now()
        async with driver.session() as session:
            result = await session.run(
                _CREATE_SUBGRAPH_QUERY,
                {
                    "graph_id": graph_id,
                    "organisation_id": organisation_id,
                    "name": name,
                    "subgraph_id": str(uuid4()),
                    "description": description,
                    "created_by": created_by,
                    "now": now,
                },
            )
            record = await result.single()
            return {
                "subgraph_id": record["subgraph_id"],
                "graph_id": graph_id,
                "name": record["name"],
                "description": record["description"],
                "created_at": str(record["created_at"]) if record["created_at"] else now,
            }


def _now() -> str:
    return datetime.now(UTC).isoformat()
