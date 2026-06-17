"""Data-layer proof that the Postgres RLS backstop isolates the knowledge-graph-service's org-scoped
tables on its own — the app-layer ``WHERE organisation_id`` removed (ADR-030 / #353).

Distinct from the API/unit tests (which prove the app-layer scoping behaves): here we go under the
repositories and prove the *backstop* — that even with the app-layer predicate gone, RLS alone
returns only the bound org's rows and denies a cross-org write. This is the real point of the
backstop: a bug that drops a ``WHERE`` clause no longer leaks cross-org rows.

Run as the NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` role (the deployed web + worker runtime role) —
RLS only bites a non-superuser, so the isolation proven here is real. The org GUC is bound exactly
as the runtime binds it (``use_organisation_context`` + the engine ``begin`` guard installed by
``install_org_guc_guard``, the same guard ``core/database.make_engine`` / ``make_worker_engine``
use), never via a hand-written WHERE. Mirrors the credential-broker ``test_rls_backstop_isolation``.

The policy is proven on the composite-PK ``recipes`` table too (PK
``(id, version, organisation_id)``) — RLS is table-level and PK-agnostic, so it applies cleanly
regardless of key shape. Neo4j is out of scope (RLS is Postgres-only).

Threats: T1-M1, T1-M3. ADR-006; ADR-012 §2; ADR-030.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
    pytest.mark.isolation,
]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# Direct SQL against the table with NO organisation_id predicate — RLS is the only thing scoping it.
_SELECT_ALL_NAMES = "SELECT name FROM knowledge_graphs"
_COUNT_ALL = "SELECT count(*) FROM knowledge_graphs"
_INSERT_GRAPH = (
    "INSERT INTO knowledge_graphs "
    "(id, organisation_id, user_id, name, status, node_count, relationship_count) "
    "VALUES (:id, :org, :user, :name, 'active', 0, 0)"
)
# recipes carries a COMPOSITE PK (id, version, organisation_id) — table-level RLS is PK-agnostic.
_INSERT_RECIPE = (
    "INSERT INTO recipes "
    "(id, version, organisation_id, status, source_type, shape_signature, concern, recipe_json) "
    "VALUES (:id, 1, :org, 'draft', 'csv', 'sig', 'c', '{}'::json)"
)
_COUNT_RECIPES = "SELECT count(*) FROM recipes"


@pytest.fixture
async def app_engine(kgs_dsns) -> AsyncIterator[AsyncEngine]:  # noqa: ANN001
    """An AsyncEngine on the oraclous_app role with the org-GUC guard installed (so a transaction
    under a bound ``use_organisation_context`` sets the GUC), schema+RLS+role already provisioned by
    ``kgs_dsns`` as the owner. Uses the SAME ``install_org_guc_guard`` the runtime factories use."""
    from oraclous_substrate.access_async import install_org_guc_guard
    from sqlalchemy.ext.asyncio import create_async_engine

    _owner_async_dsn, app_async_dsn = kgs_dsns
    engine = create_async_engine(app_async_dsn)
    install_org_guc_guard(engine)
    yield engine
    await engine.dispose()


def _ctx(org: uuid.UUID):  # noqa: ANN202
    from oraclous_governance import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


async def test_rls_alone_isolates_reads_and_denies_cross_org_writes(
    app_engine: AsyncEngine,
) -> None:
    from oraclous_governance import use_organisation_context

    # WRITE org A's graph, org A bound (the engine begin-guard sets the GUC; WITH CHECK admits it)
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_GRAPH),
                {"id": uuid.uuid4(), "org": ORG_A, "user": uuid.uuid4(), "name": "org-a-graph"},
            )

    # READ under org A's GUC: the row is visible — and the SELECT carries NO organisation_id WHERE,
    # so RLS alone is what scopes it.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            a_rows = [r[0] for r in (await conn.execute(text(_SELECT_ALL_NAMES))).all()]
    assert a_rows == ["org-a-graph"]

    # READ under org B's GUC: org A's row is INVISIBLE (RLS USING filters it) — the backstop proof
    # with the app-WHERE removed.
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            b_rows = [r[0] for r in (await conn.execute(text(_SELECT_ALL_NAMES))).all()]
    assert b_rows == []

    # CROSS-ORG WRITE: with org B bound, inserting a graph stamped for org A violates the RLS WITH
    # CHECK → SQLSTATE 42501 (InsufficientPrivilege). org B cannot write into org A's scope.
    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_GRAPH),
                    {
                        "id": uuid.uuid4(),
                        "org": ORG_A,  # smuggled — not the bound org
                        "user": uuid.uuid4(),
                        "name": "smuggled",
                    },
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    # FAIL-CLOSED: with NO org context bound, the guard binds the empty GUC → RLS exposes zero rows
    # (an absent scope denies, never defaults — T1-M1). org A's real row stays hidden.
    async with app_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT_ALL))).scalar_one() == 0


async def test_rls_applies_to_composite_pk_table_recipes(app_engine: AsyncEngine) -> None:
    """The recipes table has a COMPOSITE PK (id, version, organisation_id); table-level RLS is
    PK-agnostic, so the org-isolation policy applies cleanly — a cross-org write is still denied and
    a cross-org read still returns zero rows."""
    from oraclous_governance import use_organisation_context

    recipe_id = f"recipe-{uuid.uuid4()}"
    # WRITE org A's recipe under org A — admitted by WITH CHECK.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(text(_INSERT_RECIPE), {"id": recipe_id, "org": ORG_A})

    # READ under org B: invisible (RLS USING on the composite-PK table).
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            assert (await conn.execute(text(_COUNT_RECIPES))).scalar_one() == 0

    # CROSS-ORG WRITE under org B stamping org A → 42501.
    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_RECIPE), {"id": f"recipe-{uuid.uuid4()}", "org": ORG_A}
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


async def test_runtime_role_is_non_bypassing(app_engine: AsyncEngine) -> None:
    """The role the runtime (web + worker) connects as must be NOSUPERUSER/NOBYPASSRLS, else the
    policy is inert (T1-M3) — the precondition the isolation above depends on, and what
    ``assert_runtime_role_isolates`` enforces at web startup."""
    from oraclous_substrate.access_async import assert_non_bypassing_role

    # passes silently for oraclous_app; would raise RlsBypassingRoleError for a superuser.
    await assert_non_bypassing_role(app_engine)

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).first()
    assert row is not None
    assert row[0] is False and row[1] is False  # NOSUPERUSER, NOBYPASSRLS
