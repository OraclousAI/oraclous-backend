"""ReBAC engine — extracted from the legacy ``knowledge-graph-builder`` and
reshaped to scope every relation edge by ``organisation_id`` (ORA-34, ADR-006),
then extended with agent-as-subject delegation (ORA-35 / R1-C2, ADR-013 §3).

Behavioural reference: ``app/services/rebac_service.py`` in the legacy
codebase. Preserved here: cache→Phase B→Phase A resolution order, fail-closed
on any backend error, the 60-second permission cache, soft-revoke (no DELETE),
live-checked grant expiry, parameterised Cypher (injection-safe).

Reshape (the contract pinned by the merged ORA-34 + ORA-35 ``[tests]`` PRs):

* ``organisation_id`` is the outermost scope on every entry point — passed as a
  keyword argument, validated non-blank, and bound into every Cypher query as
  ``$organisation_id`` so HAS_ROLE / CAN_ACCESS / DELEGATED_TO edges are
  written and filtered by it (Threat T1 — the tenant loop).
* the permission cache key is namespaced by ``organisation_id`` so a cached
  decision in one org can never satisfy a check in another.
* ``check_graph_permission`` is **polymorphic** over the principal type
  (ADR-013 §3 — Bounds on adapter logic). It accepts a ``subject``
  discriminator of shape ``{"type": "user" | "agent", "id": "…"}`` and
  dispatches internally to the user path (Phase B → Phase A) or the agent
  path (delegation traversal). The closed type set fails closed on any
  other value so a new principal type cannot accidentally inherit
  user-style resolution.
* Agent subjects resolve through a single-shot **delegation traversal**:
  find an active ``(member:User)-[:DELEGATED_TO]->(:Agent)`` edge
  scoped by org + graph + (graph | subgraph), then verify the delegating
  member has the required role on the graph. Transitive ``Agent→Agent``
  edges are invisible to the traversal (T2 mitigation — the ``:User``
  label on the delegator side is the structural guard).
* Delegation CRUD is its own surface (``delegate_to_agent`` /
  ``revoke_agent_delegation``) — separate per C1 precedent. Both
  invalidate the org-scoped delegation cache so the next check sees the
  new state (T2-M2 revocation propagation; 60s bounded stale-relation
  tolerance, same TTL as the permission cache).
* Transitive ``Agent→Agent`` delegation is rejected at the API boundary
  by a prefix heuristic on ``member_user_id`` (AC#3); the Cypher-level
  ``:User`` guard is the second line of defence.

This module deliberately does not adapt the engine into ``oraclous_substrate``'s
``resolve(AccessRequest) -> bool | None`` resolver protocol. The substrate seam
(ORA-15) consumes a resolver via dependency injection and is tested with a
test-double; whether to wire ``ReBACEngine`` as the production resolver is an
open question for the coordinator ``solution-architect`` and is deferred.

Out of scope (not in the merged test suites, not in this impl):
schema initialisation / system-permission seeding / legacy Phase A sync,
``register_new_graph``, ``get_user_access_filter``, ``list_subgraphs``,
cross-organisation delegation (R5 federation), an explicit-scope-narrowing
agent→agent variant (architect: defer until R4 surfaces a real need —
schema accepts the relation; no migration needed to add later).
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


# ── R1-C2 (ORA-35) delegation surface ──────────────────────────────────────
# Polymorphic subject discriminator per ADR-013 §3 (Bounds on adapter logic):
# the substrate seam's AccessRequest carries a polymorphic subject, so the
# engine's check is polymorphic too. The CRUD methods (delegate_to_agent /
# revoke_agent_delegation) stay separate per CRUD-separation precedent.

_SUBJECT_TYPES = ("user", "agent")
# Heuristic agent-id prefixes used to reject transitive agent→agent
# delegation at the delegate_to_agent boundary (T2). The Cypher-level guard
# (``MATCH (m:User …)``) is the second line of defence.
_AGENT_ID_PREFIXES = ("agent-", "agt-", "agt_", "oag_")
_DELEGATION_SCOPES = ("graph", "subgraph")
_DELEGATION_CACHE_TTL = 60  # seconds — mirrors _PERM_CACHE_TTL (T2-M2 budget)

# Single-shot traversal: (member User)-[:DELEGATED_TO]->(:Agent), restricted
# by org + graph + scope, joined to the member's HAS_ROLE permission check.
# Returns one row with ``authorized: bool``. The :User label on the
# delegator is the structural guard against transitive Agent→Agent
# authorisation (T2 mitigation, AC#3) — even if an Agent-sourced
# DELEGATED_TO edge exists in the graph, this MATCH never sees it.
_DELEGATION_TRAVERSAL_QUERY = """
MATCH (m:User:__Platform__)
  -[d:DELEGATED_TO]->(:Agent:__Platform__ {agent_id: $agent_id})
WHERE d.organisation_id = $organisation_id
  AND d.graph_id = $graph_id
  AND d.is_active = true
  AND (d.expires_at IS NULL OR d.expires_at > datetime())
  AND (d.scope = 'graph'
       OR (d.scope = 'subgraph' AND d.subgraph_id = $subgraph_id))
MATCH (m)-[hr:HAS_ROLE]->(r:Role:__System__ {
    graph_id: $graph_id, organisation_id: $organisation_id
})
WHERE hr.graph_id = $graph_id
  AND hr.organisation_id = $organisation_id
  AND hr.is_active = true
  AND (hr.expires_at IS NULL OR hr.expires_at > datetime())

OPTIONAL MATCH (r)-[:HAS_PERMISSION|INHERITS_FROM*0..5]->
               (:Role)-[:HAS_PERMISSION]->(p1:Permission {name: $perm})

OPTIONAL MATCH (r)-[:HAS_PERMISSION]->(p2:Permission:__System__ {name: $perm})

WITH count(p1) + count(p2) AS perm_count
RETURN perm_count > 0 AS authorized
LIMIT 1
"""

# Grant queries are dispatched by scope (ORA-37 R1-gate discovery): Neo4j
# rejects MERGE on a relationship whose key map contains a null property
# value (``SemanticError: Cannot merge … because of null property value
# for 'subgraph_id'``). The graph-scope variant therefore omits
# ``subgraph_id`` from the MERGE key entirely; the subgraph-scope variant
# keeps it (subgraph_id is non-null on that path — guarded by
# ``_require_scope``). Both queries take the same parameter set, minus
# ``$subgraph_id`` for the graph-scope query.

_DELEGATION_GRANT_QUERY_GRAPH = """
MERGE (m:User:__Platform__ {user_id: $member_user_id})
ON CREATE SET m.created_at = $now, m.is_service_account = false
MERGE (a:Agent:__Platform__ {agent_id: $agent_id})
ON CREATE SET a.created_at = $now
MERGE (m)-[d:DELEGATED_TO {
    graph_id: $graph_id,
    organisation_id: $organisation_id,
    scope: $scope
}]->(a)
ON CREATE SET d.granted_at = $now, d.granted_by = $granted_by,
              d.expires_at = $expires_at, d.is_active = true
ON MATCH SET d.granted_at = $now, d.granted_by = $granted_by,
             d.expires_at = $expires_at, d.is_active = true
"""

_DELEGATION_GRANT_QUERY_SUBGRAPH = """
MERGE (m:User:__Platform__ {user_id: $member_user_id})
ON CREATE SET m.created_at = $now, m.is_service_account = false
MERGE (a:Agent:__Platform__ {agent_id: $agent_id})
ON CREATE SET a.created_at = $now
MERGE (m)-[d:DELEGATED_TO {
    graph_id: $graph_id,
    organisation_id: $organisation_id,
    scope: $scope,
    subgraph_id: $subgraph_id
}]->(a)
ON CREATE SET d.granted_at = $now, d.granted_by = $granted_by,
              d.expires_at = $expires_at, d.is_active = true
ON MATCH SET d.granted_at = $now, d.granted_by = $granted_by,
             d.expires_at = $expires_at, d.is_active = true
"""

# Revoke is a single query with the scope/subgraph_id check in WHERE rather
# than in the relationship pattern key — the ORA-37 R1-gate discovery: a
# pattern like ``{subgraph_id: $subgraph_id}`` with ``$subgraph_id=null``
# matches **zero** rows under Cypher three-valued logic, so the pre-fix
# revoke silently no-op'd for every graph-scope delegation and the edge
# stayed active (T2-M2 defeat). The ``$scope = 'graph' OR …`` short-circuit
# means the ``d.subgraph_id = $subgraph_id`` arm is only evaluated when
# scope is 'subgraph' (where subgraph_id is guaranteed non-null by
# ``_require_scope``).

_DELEGATION_REVOKE_QUERY = """
MATCH (m:User:__Platform__ {user_id: $member_user_id})
  -[d:DELEGATED_TO]->(a:Agent:__Platform__ {agent_id: $agent_id})
WHERE d.graph_id = $graph_id
  AND d.organisation_id = $organisation_id
  AND d.scope = $scope
  AND ($scope = 'graph'
       OR ($scope = 'subgraph' AND d.subgraph_id = $subgraph_id))
SET d.is_active = false
RETURN count(d) AS revoked_count
"""


def _require_org(organisation_id: str) -> None:
    if not organisation_id or not organisation_id.strip():
        raise ValueError("organisation_id is required (ADR-006)")


def _require_graph(graph_id: str) -> None:
    if not graph_id:
        raise ValueError("graph_id is required")


def _require_subject(subject: Any) -> tuple[str, str]:
    """Validate the polymorphic subject discriminator (ADR-013 §3).

    Returns ``(type, id)`` for a well-formed subject. Fail-closed on any
    deviation — unknown type, missing field, or blank id — mirrors the
    substrate seam's unknown-relation rejection and the C1 ``_require_org``
    guard. A silent fallback (e.g. treating an unknown type as ``"user"``)
    would be a privilege-escalation bug.
    """
    if not isinstance(subject, dict):
        raise ValueError(
            "subject must be a dict with 'type' and 'id' fields "
            "(polymorphic principal type per ADR-013 §3)"
        )
    if "type" not in subject:
        raise ValueError("subject.type is required")
    if "id" not in subject:
        raise ValueError("subject.id is required")
    subject_type = subject["type"]
    subject_id = subject["id"]
    if subject_type not in _SUBJECT_TYPES:
        raise ValueError(
            f"subject.type must be one of {_SUBJECT_TYPES}, got {subject_type!r} "
            "(closed type set; fail-closed on unknown principal type)"
        )
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise ValueError("subject.id must be a non-blank string")
    return subject_type, subject_id


def _require_scope(scope: str, subgraph_id: str | None) -> None:
    """Validate the delegation scope discriminator. Closed set, fail-closed."""
    if scope not in _DELEGATION_SCOPES:
        raise ValueError(f"scope must be one of {_DELEGATION_SCOPES}, got {scope!r}")
    if scope == "subgraph" and not subgraph_id:
        raise ValueError(
            "scope='subgraph' requires a non-blank subgraph_id "
            "(silent fallback to graph-scope would widen the grant)"
        )


def _looks_like_agent(identifier: str) -> bool:
    """Heuristic API-level guard against transitive agent→agent delegation
    (T2, AC#3). Conventional agent identifiers start with one of
    ``_AGENT_ID_PREFIXES``; passing such an id as ``member_user_id`` to
    ``delegate_to_agent`` is rejected here. The Cypher traversal's
    ``MATCH (m:User …)`` is the second line of defence — even if a
    misformatted id slipped past this check, the User-typed match would
    refuse to authorise through it.
    """
    return identifier.startswith(_AGENT_ID_PREFIXES)


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
        subject: dict[str, str],
        graph_id: str,
        required_level: str,
        subgraph_id: str | None = None,
    ) -> bool:
        """Polymorphic permission check (ADR-013 §3).

        ``subject`` is the discriminator ``{"type": "user" | "agent", "id":
        "…"}``. Closed type set: any other value is fail-closed (``ValueError``).
        For ``user`` subjects, runs the C1 Phase B HAS_ROLE → Phase A
        CAN_ACCESS resolution. For ``agent`` subjects, runs the delegation
        traversal phase (R1-C2): find an active (member→agent)
        ``DELEGATED_TO`` edge in (org, graph, scope) and check the
        delegating member's role. ``subgraph_id`` narrows the agent-path
        check to a specific subgraph (graph-scope delegations still match).

        Fail-closed on any backend error.
        """
        _require_org(organisation_id)
        _require_graph(graph_id)
        subject_type, subject_id = _require_subject(subject)

        if subject_type == "user":
            return await self._check_user_graph_permission(
                driver,
                organisation_id=organisation_id,
                user_id=subject_id,
                graph_id=graph_id,
                required_level=required_level,
            )
        # subject_type == "agent" — closed set, no other branch is reachable
        return await self._check_agent_graph_permission(
            driver,
            organisation_id=organisation_id,
            agent_id=subject_id,
            graph_id=graph_id,
            required_level=required_level,
            subgraph_id=subgraph_id,
        )

    async def _check_user_graph_permission(
        self,
        driver: AsyncDriver,
        *,
        organisation_id: str,
        user_id: str,
        graph_id: str,
        required_level: str,
    ) -> bool:
        """The C1 user-subject permission resolution. Pre-existing behaviour
        — cache → Phase B HAS_ROLE → Phase A CAN_ACCESS, fail-closed,
        60s Redis cache, parameterised queries.
        """
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

    # ── Agent delegation (R1-C2, ORA-35) ─────────────────────────────────

    async def _check_agent_graph_permission(
        self,
        driver: AsyncDriver,
        *,
        organisation_id: str,
        agent_id: str,
        graph_id: str,
        required_level: str,
        subgraph_id: str | None = None,
    ) -> bool:
        """The agent-subject branch of ``check_graph_permission`` (R1-C2).
        Runs a single-shot delegation traversal: find an active
        (member→agent) ``DELEGATED_TO`` edge restricted by org / graph /
        scope and check that the delegating member has the required role.
        Fail-closed on any backend error. 60s org-namespaced cache (T2-M2).
        """
        required_perm = _LEVEL_TO_PERM.get(required_level, "graph:manage_access")
        cache_key = f"del:{organisation_id}:{agent_id}:{graph_id}:{required_level}"

        redis: Any | None = None
        try:
            redis = await self._get_redis()
            cached = await redis.get(cache_key)
            if cached is not None:
                return cached == "1"
        except Exception as exc:  # pragma: no cover — covered behaviourally
            logger.warning("Redis delegation cache read error: %s", exc)

        authorized = False
        try:
            async with driver.session() as session:
                result = await session.run(
                    _DELEGATION_TRAVERSAL_QUERY,
                    {
                        "organisation_id": organisation_id,
                        "agent_id": agent_id,
                        "graph_id": graph_id,
                        "subgraph_id": subgraph_id,
                        "perm": required_perm,
                    },
                )
                record = await result.single()
                authorized = bool(record and record["authorized"])
        except Exception as exc:
            logger.error("ReBAC delegation traversal Neo4j error: %s", exc)
            return False

        if redis is not None:
            try:
                await redis.set(cache_key, "1" if authorized else "0", ex=_DELEGATION_CACHE_TTL)
            except Exception as exc:  # cache write is best-effort
                logger.debug("Redis delegation-cache write skipped: %s", exc)
        return authorized

    async def delegate_to_agent(
        self,
        driver: AsyncDriver,
        *,
        organisation_id: str,
        member_user_id: str,
        agent_id: str,
        graph_id: str,
        scope: str,
        granted_by: str,
        subgraph_id: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        """Grant ``agent_id`` an effective scope-bounded delegation from
        ``member_user_id`` on ``graph_id`` within ``organisation_id`` (AC#1).
        Closed scope set (``graph`` / ``subgraph``); subgraph requires a
        ``subgraph_id``. Transitive agent→agent delegation is rejected at
        the API boundary (T2 / AC#3). Idempotent (MERGE). Invalidates the
        org-scoped delegation cache so a freshly granted scope is not
        masked by a stale deny (the same stale-deny mitigation C1's
        ``grant_role`` applies).
        """
        _require_org(organisation_id)
        _require_graph(graph_id)
        _require_scope(scope, subgraph_id)
        if _looks_like_agent(member_user_id):
            raise ValueError(
                f"transitive agent→agent delegation is forbidden (T2 / AC#3): "
                f"member_user_id={member_user_id!r} looks like an agent identifier"
            )

        # Dispatch by scope (ORA-37 fix): the graph-scope query omits
        # ``subgraph_id`` from the MERGE relationship-pattern key so Neo4j
        # does not crash on the null value. ``scope`` is still passed in
        # params under both branches — kept for symmetry with the revoke
        # query and to satisfy the C2 unit suite's "delegate binds $scope"
        # contract.
        params = {
            "member_user_id": member_user_id,
            "agent_id": agent_id,
            "graph_id": graph_id,
            "organisation_id": organisation_id,
            "scope": scope,
            "granted_by": granted_by,
            "expires_at": expires_at,
            "now": _now(),
        }
        if scope == "subgraph":
            params["subgraph_id"] = subgraph_id
            query = _DELEGATION_GRANT_QUERY_SUBGRAPH
        else:
            query = _DELEGATION_GRANT_QUERY_GRAPH

        async with driver.session() as session:
            await session.run(query, params)
        await self.invalidate_agent_delegation_cache(
            organisation_id=organisation_id, agent_id=agent_id, graph_id=graph_id
        )

    async def revoke_agent_delegation(
        self,
        driver: AsyncDriver,
        *,
        organisation_id: str,
        member_user_id: str,
        agent_id: str,
        graph_id: str,
        scope: str,
        subgraph_id: str | None = None,
    ) -> int:
        """Soft-revoke (``is_active = false``, never DELETE) the delegation
        edge matching (org, member, agent, graph, scope, subgraph_id).
        Returns the count of edges revoked (0 = no matching edge).
        Invalidates the org-scoped delegation cache so the **next**
        invocation fails (T2-M2 / AC#2).
        """
        _require_org(organisation_id)
        _require_graph(graph_id)
        _require_scope(scope, subgraph_id)

        async with driver.session() as session:
            result = await session.run(
                _DELEGATION_REVOKE_QUERY,
                {
                    "member_user_id": member_user_id,
                    "agent_id": agent_id,
                    "graph_id": graph_id,
                    "organisation_id": organisation_id,
                    "scope": scope,
                    "subgraph_id": subgraph_id,
                },
            )
            record = await result.single()
            count = int(record["revoked_count"]) if record else 0

        await self.invalidate_agent_delegation_cache(
            organisation_id=organisation_id, agent_id=agent_id, graph_id=graph_id
        )
        return count

    async def invalidate_agent_delegation_cache(
        self, *, organisation_id: str, agent_id: str, graph_id: str
    ) -> None:
        """Drop all cached delegation entries for this (org, agent, graph)
        triple. Iterates the three acceptable-level keys (``read`` /
        ``write`` / ``admin``) the agent-check populates; the cache key is
        org-namespaced so this never touches another org's keys.
        """
        try:
            redis = await self._get_redis()
            for level in ("read", "write", "admin"):
                await redis.delete(f"del:{organisation_id}:{agent_id}:{graph_id}:{level}")
        except Exception as exc:  # pragma: no cover
            logger.warning("Redis delegation cache invalidation error: %s", exc)

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
