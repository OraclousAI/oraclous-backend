"""LINKED_TO read paths for KRS — ReBAC-enforced graph and entity link listing (ORAA-59).

Write operations (create/delete) stay in KGS. This module exposes read paths only.
"""

from __future__ import annotations

from typing import Any

_SYSTEM_ROLES: list[str] = ["owner", "editor", "viewer", "restricted_viewer", "denied"]
_VALID_ROLES: list[str] = list(_SYSTEM_ROLES)

# Sentinel: caller has no role on the graph
_NO_ACCESS_LEVEL: int = len(_SYSTEM_ROLES)


async def _user_role_level(driver: Any, graph_id: str, user_id: str) -> int:
    """Return the caller's most-privileged role level on graph_id (0=owner; higher=less privileged).

    Returns _NO_ACCESS_LEVEL when the caller has no active role.
    """
    async with driver.session() as session:
        result = await session.run(
            "MATCH (u:User:__Platform__ {user_id: $user_id})"
            "-[:HAS_ROLE {graph_id: $graph_id, is_active: true}]->"
            "(r:Role:__System__ {graph_id: $graph_id}) "
            "RETURN collect(r.name) AS role_names",
            {"user_id": user_id, "graph_id": graph_id},
        )
        rows = await result.data()
    if not rows:
        return _NO_ACCESS_LEVEL
    role_names: list[str] = rows[0].get("role_names") or []
    best = _NO_ACCESS_LEVEL
    for name in role_names:
        if name in _SYSTEM_ROLES:
            idx = _SYSTEM_ROLES.index(name)
            if idx < best:
                best = idx
    return best


async def list_graph_links(
    driver: Any,
    source_graph_id: str,
    user_id: str,
) -> list[dict[str, Any]]:
    """Return LINKED_TO edges from source_graph_id visible to user_id (ADR-021 §4).

    A link is visible when the caller's role level is <= the link's min_role level
    (i.e. the caller is at least as privileged as the minimum required role).
    Links with an unrecognised min_role are hidden (fail-closed).
    """
    user_level = await _user_role_level(driver, source_graph_id, user_id)
    if user_level >= _NO_ACCESS_LEVEL:
        return []

    async with driver.session() as session:
        result = await session.run(
            "MATCH (ga:Graph:__Platform__ {graph_id: $source_graph_id})"
            "-[l:LINKED_TO]->(gb:Graph:__Platform__) "
            "RETURN ga.graph_id AS source_graph_id, "
            "gb.graph_id AS target_graph_id, "
            "l.min_role AS min_role, "
            "l.created_by AS created_by, "
            "l.created_at AS created_at",
            {"source_graph_id": source_graph_id},
        )
        rows = await result.data()

    out: list[dict[str, Any]] = []
    for row in rows:
        min_role = row.get("min_role")
        if min_role not in _SYSTEM_ROLES:
            continue  # fail-closed: unknown min_role → hidden
        min_level = _SYSTEM_ROLES.index(min_role)
        if user_level <= min_level:
            out.append(dict(row))
    return out


async def list_entity_links(
    driver: Any,
    source_graph_id: str,
    source_entity_id: str,
    user_id: str,
) -> list[dict[str, Any]]:
    """Return entity-level LINKED_TO edges visible to user_id (ADR-021 §4).

    Visibility is enforced against the source graph role, using the same
    min_role hierarchy as list_graph_links.
    """
    user_level = await _user_role_level(driver, source_graph_id, user_id)
    if user_level >= _NO_ACCESS_LEVEL:
        return []

    async with driver.session() as session:
        result = await session.run(
            "MATCH (ea:__Entity__ {graph_id: $source_graph_id, id: $source_entity_id})"
            "-[l:LINKED_TO]->(eb:__Entity__) "
            "RETURN $source_graph_id AS source_graph_id, "
            "$source_entity_id AS source_entity_id, "
            "eb.graph_id AS target_graph_id, "
            "eb.id AS target_entity_id, "
            "l.min_role AS min_role, "
            "l.created_by AS created_by, "
            "l.created_at AS created_at",
            {
                "source_graph_id": source_graph_id,
                "source_entity_id": source_entity_id,
            },
        )
        rows = await result.data()

    out: list[dict[str, Any]] = []
    for row in rows:
        min_role = row.get("min_role")
        if min_role not in _SYSTEM_ROLES:
            continue  # fail-closed: unknown min_role → hidden
        min_level = _SYSTEM_ROLES.index(min_role)
        if user_level <= min_level:
            out.append(dict(row))
    return out
