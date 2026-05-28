"""Substrate query/write-path organisation enforcement — unit contracts (ORA-17 / A2).

RED until ``backend-implementer`` adds ``oraclous_substrate.query_scoping``.

Reshape (lift-tag **Reshape**) of the organisation-blind tenant scoping in
``knowledge-graph-builder``:

* ``app/components/multi_tenant_components.py::_inject_graph_id_filter`` (also
  called from ``app/services/retriever_factory.py:149``) — a query mutator that
  appended ``WHERE node.graph_id = $graph_id`` after the first ``MATCH`` with the
  value supplied by the *caller*. A2 makes it organisation-aware: the filter value
  is taken from the bound governance org-context (ORA-14 / 0f), never from a
  caller/request-body argument, and absent-context fails closed.
* ``app/core/database.py::get_db()`` — the DB session seam. A2 binds the Postgres
  RLS GUC (``app.current_organisation_id``, ORA-16) from the org-context so A1's
  row-level-security policy actually isolates at runtime.

These are the unit-level contracts (pure logic + a fake cursor). The data-layer
proof against real Neo4j/Postgres lives in
``tests/organization_isolation/test_query_path_org_enforcement.py``.

Threats: T1-M1 — ``organisation_id`` enforced at the substrate boundary, never
from a request body; an absent scope fails closed. ADR-006.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    use_organisation_context,
)
from oraclous_substrate.query_scoping import (
    bind_organisation_guc,
    enforced_organisation_id,
    org_scoped_cypher,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _ctx(org: uuid.UUID) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=org,
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


# ── enforced_organisation_id: the single context-sourced chokepoint ──────────


def test_enforced_organisation_id_returns_bound_context_org() -> None:
    with use_organisation_context(_ctx(ORG_A)):
        assert enforced_organisation_id() == str(ORG_A)


def test_enforced_organisation_id_fails_closed_without_context() -> None:
    # No context bound: must raise, never return None / "" / a default (T1-M1).
    with pytest.raises(MissingOrganisationContextError):
        enforced_organisation_id()


def test_enforced_organisation_id_tracks_the_active_scope() -> None:
    # Switching the bound context switches the enforced scope — proving the value
    # is read live from the context, not memoised or caller-pinned.
    with use_organisation_context(_ctx(ORG_A)):
        assert enforced_organisation_id() == str(ORG_A)
    with use_organisation_context(_ctx(ORG_B)):
        assert enforced_organisation_id() == str(ORG_B)


# ── org_scoped_cypher: reshape of _inject_graph_id_filter ────────────────────

_BASE_QUERY = "MATCH (node:__Entity__)\nRETURN node"


def test_org_scoped_cypher_injects_org_filter_from_context() -> None:
    with use_organisation_context(_ctx(ORG_A)):
        query, params = org_scoped_cypher(_BASE_QUERY)
    assert "$organisation_id" in query
    assert params["organisation_id"] == str(ORG_A)


def test_org_scoped_cypher_never_interpolates_the_org_value() -> None:
    """Legacy invariant ('never interpolates values directly into Cypher'): the org
    value travels as a bound ``$organisation_id`` parameter, never spliced into the
    query text (injection-safe; T1)."""
    with use_organisation_context(_ctx(ORG_A)):
        query, params = org_scoped_cypher(_BASE_QUERY)
    assert str(ORG_A) not in query
    assert params["organisation_id"] == str(ORG_A)


def test_org_scoped_cypher_sources_org_only_from_context_not_caller() -> None:
    """AC#1: ``organisation_id`` is never taken from a caller/request-body value.

    ``org_scoped_cypher`` takes no ``organisation_id`` argument; its sole source is
    the bound context, and the returned params dict is authoritative — so a caller
    cannot smuggle a different organisation in. Under ORG_A's context the bound
    param is ORG_A regardless of a planted foreign-org literal in the query text.
    """
    planted = f"MATCH (node:__Entity__)\nWHERE node.note = '{ORG_B}'\nRETURN node"
    with use_organisation_context(_ctx(ORG_A)):
        _query, params = org_scoped_cypher(planted)
    assert params["organisation_id"] == str(ORG_A)


def test_org_scoped_cypher_fails_closed_without_context() -> None:
    with pytest.raises(MissingOrganisationContextError):
        org_scoped_cypher(_BASE_QUERY)


def test_org_scoped_cypher_is_idempotent_when_already_scoped() -> None:
    """Mirrors the legacy guard (``if "$graph_id" in query: return query``): a query
    already carrying ``$organisation_id`` is not double-filtered."""
    with use_organisation_context(_ctx(ORG_A)):
        once, _ = org_scoped_cypher(_BASE_QUERY)
        twice, _ = org_scoped_cypher(once)
    assert twice.count("$organisation_id") == 1
    assert once == twice


def test_org_scoped_cypher_respects_a_custom_alias() -> None:
    with use_organisation_context(_ctx(ORG_A)):
        query, _ = org_scoped_cypher("MATCH (e:__Entity__)\nRETURN e", alias="e")
    assert "e.organisation_id = $organisation_id" in query


# ── bind_organisation_guc: reshape of get_db() (activates A1 Postgres RLS) ────


def test_bind_organisation_guc_sets_the_rls_guc_from_context() -> None:
    """A1's RLS policy filters on ``current_setting('app.current_organisation_id')``.
    A2 binds that GUC from the org-context so the backstop isolates at runtime. The
    org value must be a *bound parameter* to ``set_config`` — never interpolated
    into the SQL text (injection-safe; T1)."""
    from oraclous_substrate.schema.postgres import ORG_GUC

    cur = MagicMock()
    with use_organisation_context(_ctx(ORG_A)):
        bind_organisation_guc(cur)

    assert cur.execute.call_count == 1
    args = cur.execute.call_args.args
    assert len(args) >= 2, "org value must be passed as a bound parameter, not interpolated"
    sql, params = args[0], args[1]
    assert "set_config" in sql.lower()
    assert ORG_GUC in (sql, *params)  # the GUC name is present (in SQL or as a param)
    assert str(ORG_A) in params  # the org value is a bound parameter ...
    assert str(ORG_A) not in sql  # ... and never interpolated into the statement


def test_bind_organisation_guc_fails_closed_without_context() -> None:
    cur = MagicMock()
    with pytest.raises(MissingOrganisationContextError):
        bind_organisation_guc(cur)
    cur.execute.assert_not_called()  # no un-scoped GUC is ever set
