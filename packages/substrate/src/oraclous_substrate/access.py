"""Substrate tenancy-enforcement seam (Layer 1, A2 / ADR-012).

The single, canonical boundary through which every substrate read and write is
scoped to an organisation. ``organisation_id`` is always sourced from the bound
governance org-context (0f / ORA-14), never from a caller/request-body argument,
and an absent context fails closed (Threat Catalogue T1-M1; ADR-006).

Reshape of the organisation-blind tenant scoping in ``knowledge-graph-builder``:
``_inject_graph_id_filter`` (→ ``org_scoped_cypher``), ``get_db()``
(→ ``bind_organisation_guc``), and ``federation_service::_validate_and_filter``
(→ ``authorise_cross_org_traversal`` over the substrate ReBAC client).

This module is the one A2 enforcement surface (ADR-012 §Decision-1): enforcement
primitives plus the scoped store operations composed over them. Neo4j community
has no RLS/WITH-CHECK backstop, so the Neo4j write/read enforcement here is the
*primary* control, not a convenience layer. Postgres has A1's row-level-security
as a defense-in-depth backstop, activated by ``bind_organisation_guc``.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Any

from oraclous_governance import current_organisation_context

from oraclous_substrate.cache_keys import query_cache_key
from oraclous_substrate.rebac import AccessRequest
from oraclous_substrate.schema.postgres import ORG_GUC

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from oraclous_substrate.rebac import AccessDecisionClient

# The canonical tenancy property/parameter name (ADR-006).
_ORG = "organisation_id"

# A Neo4j label is an identifier, never request input: validate before it is
# composed into Cypher (labels cannot be passed as a bound parameter).
_SAFE_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CrossOrganisationDenied(Exception):
    """A cross-organisation traversal was not authorised (fail-closed)."""


def enforced_organisation_id() -> str:
    """The organisation scope for the current request, from the bound context.

    Fail-closed: raises ``MissingOrganisationContextError`` when no context is
    bound — never returns ``None``, ``""``, or a default (T1-M1). Read live, so it
    tracks the active scope.
    """
    return str(current_organisation_context().organisation_id)


def org_scoped_cypher(query: str, *, alias: str = "node") -> tuple[str, dict[str, str]]:
    """Scope a Cypher read to the bound organisation (reshape of
    ``_inject_graph_id_filter``).

    Injects ``<alias>.organisation_id = $organisation_id`` against the first
    clause and returns the rewritten query plus the bound params. The org value
    travels only as the ``$organisation_id`` parameter — never interpolated into
    the query text (injection-safe; T1). Idempotent: a query already carrying
    ``$organisation_id`` is returned unchanged. Fail-closed without a context.
    """
    params = {_ORG: enforced_organisation_id()}
    if f"${_ORG}" in query:
        return query, params

    predicate = f"{alias}.{_ORG} = ${_ORG}"
    lines = query.split("\n")
    # Prefer merging into the first existing WHERE; else add a WHERE after MATCH.
    for idx, line in enumerate(lines):
        if line.lstrip().upper().startswith("WHERE"):
            lines[idx] = f"{line.rstrip()} AND {predicate}"
            return "\n".join(lines), params
    for idx, line in enumerate(lines):
        if line.lstrip().upper().startswith("MATCH"):
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(idx + 1, f"{indent}WHERE {predicate}")
            return "\n".join(lines), params
    # Nothing to anchor the scope to — refuse rather than emit an unscoped query.
    raise ValueError("org_scoped_cypher: no MATCH/WHERE clause to scope")


def bind_organisation_guc(cursor: object) -> None:
    """Bind the Postgres RLS GUC (``app.current_organisation_id``) from the
    context, activating A1's row-level-security policy for this transaction
    (reshape of ``get_db()``).

    Transaction-local (``set_config(..., is_local=true)``) so the scope never
    leaks across a pooled connection's transactions. The org value is a bound
    parameter, never interpolated (injection-safe; T1). Fail-closed: with no
    bound context it raises before issuing any statement.
    """
    org_id = enforced_organisation_id()
    cursor.execute("SELECT set_config(%s, %s, true)", (ORG_GUC, org_id))  # type: ignore[attr-defined]


async def authorise_cross_org_traversal(
    client: AccessDecisionClient, *, resource: str, relation: str
) -> None:
    """Authorise a cross-organisation traversal through the ReBAC client
    (reshape of ``federation_service::_validate_and_filter``).

    Subject and ``organisation_id`` are taken from the bound context (ADR-006),
    never from arguments. Fail-closed (ADR-004; T1-M2): an absent, ambiguous, or
    errored decision denies. The denial does not echo the target resource id
    (enumeration-prevention).
    """
    context = current_organisation_context()
    request = AccessRequest(
        organisation_id=str(context.organisation_id),
        subject=str(context.principal_id),
        resource=resource,
        relation=relation,
    )
    decision = await client.check(request)
    if not decision.allowed:
        raise CrossOrganisationDenied("cross-organisation traversal denied")


def scoped_write_node(driver: object, *, label: str, properties: Mapping[str, object]) -> None:
    """Create a Neo4j node stamped with the bound-context organisation.

    The ``organisation_id`` is taken from the context and overrides any
    caller-supplied ``organisation_id`` in ``properties`` — so a write can never
    be tagged for another organisation (T1-M1). Neo4j community has no WITH-CHECK
    backstop, so this stamping is the primary write-isolation control.
    """
    org_id = enforced_organisation_id()
    if not _SAFE_LABEL.match(label):
        raise ValueError(f"unsafe Neo4j label: {label!r}")
    props = dict(properties)
    props[_ORG] = org_id  # context wins; any body-supplied organisation_id is ignored
    driver.execute_query(f"CREATE (n:`{label}`) SET n = $props", props=props)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Scoped store operations (ADR-012 ratified surface, ORA-20 gate's call sites).
# Each composes the enforcement primitives above. ``scoped_pg_connection``
# centralises the NOSUPERUSER/NOBYPASSRLS role precondition (ADR-012 §3.3.4(a) /
# Threat Catalogue T1-M3) as a structural chokepoint — never push it to callers.
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def scoped_pg_connection(dsn: str) -> Iterator[Any]:
    """Open a Postgres connection bound to the ambient organisation scope.

    Fail-closed: the bound org is required *before* any connection is opened.
    Asserts the connecting role is **NOSUPERUSER and NOBYPASSRLS** — a role with
    either attribute silently voids the A1 RLS backstop (T1-M3), so this seam
    refuses to bind under one rather than letting it through. Then binds the
    org GUC ``transaction-locally`` on the connection's first transaction via
    ``bind_organisation_guc``; the caller's subsequent statements run inside
    that transaction and therefore under the bound scope. The caller owns
    ``commit()`` / rollback.
    """
    enforced_organisation_id()  # fail-closed BEFORE opening any connection
    import psycopg

    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            # T1-M3 chokepoint: a superuser / BYPASSRLS session silently voids
            # the A1 RLS backstop. Refuse to bind under such a role.
            cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("scoped_pg_connection: current_user has no pg_roles entry")
            rolsuper, rolbypassrls = row
            if rolsuper or rolbypassrls:
                raise RuntimeError(
                    "scoped_pg_connection refuses to bind under a role that bypasses RLS "
                    f"(rolsuper={rolsuper}, rolbypassrls={rolbypassrls}) — "
                    "ADR-012 §3.3.4(a) requires NOSUPERUSER NOBYPASSRLS"
                )
            # Bind the org GUC locally to the implicit transaction now open; the
            # caller's subsequent cursor work runs in the same transaction and
            # sees the GUC. The caller's commit/rollback ends the transaction
            # and resets the local GUC.
            bind_organisation_guc(cur)
        yield conn
    finally:
        conn.close()


def scoped_traverse(driver: object, *, label: str, marker: str) -> list[dict[str, Any]]:
    """Org-scoped Cypher traversal: return every ``(:label {marker})`` node that
    the bound organisation owns, as a list of ``{"name": ...}``-shaped dicts.

    The org filter is injected via ``org_scoped_cypher`` (bound parameter, never
    interpolated). Fail-closed without a bound context.
    """
    if not _SAFE_LABEL.match(label):
        raise ValueError(f"unsafe Neo4j label: {label!r}")
    # Match variable matches ``org_scoped_cypher``'s default alias so the injected
    # ``WHERE node.organisation_id = ...`` predicate binds correctly.
    base = f"MATCH (node:`{label}` {{marker: $marker}})\nRETURN node.name AS name"
    query, params = org_scoped_cypher(base)
    records, _, _ = driver.execute_query(query, marker=marker, **params)  # type: ignore[attr-defined]
    return [dict(r) for r in records]


def scoped_fulltext_search(driver: object, *, index_name: str, query: str) -> list[dict[str, Any]]:
    """Org-scoped Neo4j fulltext search: returns hits (property dicts) whose
    ``organisation_id`` matches the bound context.

    ``index_name`` travels as a bound parameter to ``db.index.fulltext.queryNodes``;
    the org filter is post-applied on the YIELD'd node. Fail-closed without a
    bound context.
    """
    org_id = enforced_organisation_id()
    cypher = (
        "CALL db.index.fulltext.queryNodes($index_name, $query) YIELD node, score "
        "WHERE node.organisation_id = $organisation_id "
        "RETURN properties(node) AS props, score "
        "ORDER BY score DESC"
    )
    records, _, _ = driver.execute_query(  # type: ignore[attr-defined]
        cypher,
        index_name=index_name,
        query=query,
        organisation_id=org_id,
    )
    return [dict(r["props"]) for r in records]


def scoped_cache_get(
    redis: object, *, graph_id: str, query_text: str, retriever_type: str
) -> str | None:
    """Org-scoped Redis cache lookup using A1's ``query_cache_key`` (organisation
    is the outermost scope). Returns the cached value, or ``None`` on miss.
    Fail-closed without a bound context.
    """
    org_id = enforced_organisation_id()
    key = query_cache_key(org_id, graph_id, query_text, retriever_type)
    return redis.get(key)  # type: ignore[attr-defined]


def scoped_cache_set(
    redis: object,
    *,
    graph_id: str,
    query_text: str,
    retriever_type: str,
    value: str,
) -> None:
    """Org-scoped Redis cache write using A1's ``query_cache_key``. Fail-closed
    without a bound context.
    """
    org_id = enforced_organisation_id()
    key = query_cache_key(org_id, graph_id, query_text, retriever_type)
    redis.set(key, value)  # type: ignore[attr-defined]
