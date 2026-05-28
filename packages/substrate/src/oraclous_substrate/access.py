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

import re
from typing import TYPE_CHECKING

from oraclous_governance import current_organisation_context

from oraclous_substrate.rebac import AccessRequest
from oraclous_substrate.schema.postgres import ORG_GUC

if TYPE_CHECKING:
    from collections.abc import Mapping

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
