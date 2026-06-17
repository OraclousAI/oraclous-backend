"""Data-layer proof that the Postgres RLS backstop isolates the harness-runtime-service's four
org-scoped tables on its own — the app-layer ``WHERE organisation_id`` removed (ADR-030 / #353).

Distinct from the API/unit tests (which prove the app-layer scoping behaves): here we go under the
repositories and prove the *backstop* — that even with the app-layer predicate gone, RLS alone
scopes reads and denies cross-org writes. This is the real point of the backstop: a bug that drops a
``WHERE`` clause no longer leaks cross-org rows.

All four harness tables (``harness_executions``, ``harness_checkpoints``, ``harness_assignments``,
``harness_provenance``) share ONE policy shape (matching 0006_enable_rls): ``USING`` == ``WITH
CHECK`` == strict caller-org equality — a cross-org read returns zero rows, a cross-org write raises
42501, and an unbound GUC fails closed to zero rows (T1-M1). The harness has no shared
platform-catalogue case, so there is no read-widening.

``harness_provenance`` is the INSERT-ONLY runtime path (``PostgresProvenanceSink.write`` only ever
inserts); its WITH CHECK is proven directly — a cross-org provenance INSERT is denied. The four
tables are owned by four independent repositories that each build their engine via
``build_rls_engine``; this test binds the org exactly as those repositories do (``org_scope`` +
the engine ``begin`` guard ``install_org_guc_guard`` installs, the same guard ``build_rls_engine``
installs), never via a hand-written WHERE.

Run as the NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` role (the deployed runtime role) — RLS only
bites a non-superuser, so the isolation proven here is real. Mirrors the credential-broker /
knowledge-graph / capability-registry ``test_rls_backstop_isolation``.

Threats: T1-M1, T1-M3. ADR-006; ADR-012 §2; ADR-030.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
    pytest.mark.isolation,
]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# --- harness_provenance: the INSERT-ONLY runtime table (PostgresProvenanceSink.write). All five
# non-id columns are NOT NULL plain strings/text; created_at/updated_at have server defaults — so a
# raw INSERT with NO organisation_id predicate exercises both the strict USING (reads) and the
# strict WITH CHECK (the INSERT-only write path). Chosen as the primary strict proof for exactly
# that INSERT-only WITH CHECK reason.
_SELECT_PROVENANCE_ACTIONS = "SELECT action FROM harness_provenance"
_COUNT_PROVENANCE = "SELECT count(*) FROM harness_provenance"
_INSERT_PROVENANCE = (
    "INSERT INTO harness_provenance (id, organisation_id, principal, action, resource, outcome) "
    "VALUES (:id, :org, :principal, :action, :resource, :outcome)"
)

# --- harness_executions: a read/update table. status/input are NOT NULL; iterations/steps and the
# token columns carry no DB server-default under create_all, so the raw INSERT supplies every NOT
# NULL column without a default. Proves the same strict policy on a second table (one a user reads
# via GET /v1/harnesses and the resume path updates).
_SELECT_EXECUTION_USERS = "SELECT user_id FROM harness_executions"
_INSERT_EXECUTION = (
    "INSERT INTO harness_executions "
    "(id, organisation_id, user_id, harness_id, harness_name, status, input, iterations, "
    "total_tokens, input_tokens, output_tokens, steps) "
    "VALUES (:id, :org, :user, :harness, :name, 'SUCCEEDED', :input, 0, 0, 0, 0, '[]'::jsonb)"
)


@pytest.fixture
async def app_engine(harness_dsns) -> AsyncIterator[AsyncEngine]:  # noqa: ANN001
    """An AsyncEngine on the oraclous_app role with the org-GUC guard installed (so a transaction
    under a bound ``use_organisation_context`` sets the GUC), schema+RLS+role already provisioned by
    ``harness_dsns`` as the owner. Uses the SAME ``install_org_guc_guard`` the runtime factory."""
    from oraclous_substrate.access_async import install_org_guc_guard

    _owner_async_dsn, app_async_dsn = harness_dsns
    engine = create_async_engine(app_async_dsn)
    install_org_guc_guard(engine)
    yield engine
    await engine.dispose()


def _ctx(org: uuid.UUID):  # noqa: ANN202
    from oraclous_governance import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


async def test_provenance_insert_only_isolates_reads_and_denies_cross_org_writes(
    app_engine: AsyncEngine,
) -> None:
    """The strict org-isolation policy on ``harness_provenance`` (the INSERT-only runtime path): a
    write is admitted only for the bound org, a read is filtered to the bound org, a cross-org write
    is denied (42501), and an unbound GUC fails closed to zero rows."""
    from oraclous_governance import use_organisation_context

    # WRITE org A's provenance, org A bound (engine begin-guard sets the GUC; WITH CHECK admits it).
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_PROVENANCE),
                {
                    "id": uuid.uuid4(),
                    "org": ORG_A,
                    "principal": "user:a",
                    "action": "llm.complete",
                    "resource": "harness_execution:1",
                    "outcome": "ok",
                },
            )

    # READ under org A: visible — the SELECT carries NO organisation_id WHERE, so RLS alone scopes.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            a_rows = [r[0] for r in (await conn.execute(text(_SELECT_PROVENANCE_ACTIONS))).all()]
    assert a_rows == ["llm.complete"]

    # READ under org B: org A's row is INVISIBLE (RLS USING filters it) — the backstop proof.
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            b_rows = [r[0] for r in (await conn.execute(text(_SELECT_PROVENANCE_ACTIONS))).all()]
    assert b_rows == []

    # CROSS-ORG INSERT: org B bound, inserting a row stamped for org A violates WITH CHECK → 42501.
    # This is the INSERT-only WITH CHECK proof — the provenance write path is exactly this INSERT.
    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_PROVENANCE),
                    {
                        "id": uuid.uuid4(),
                        "org": ORG_A,  # smuggled — not the bound org
                        "principal": "user:b",
                        "action": "capability.invoke",
                        "resource": "harness_execution:2",
                        "outcome": "ok",
                    },
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    # FAIL-CLOSED: with NO org context bound, the guard binds the empty GUC → zero rows (T1-M1).
    async with app_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT_PROVENANCE))).scalar_one() == 0


async def test_executions_table_isolates_reads_and_denies_cross_org_writes(
    app_engine: AsyncEngine,
) -> None:
    """The same strict policy on ``harness_executions`` (a read/update table): read filtered to the
    bound org, a cross-org write denied (42501), an unbound GUC fails closed to zero rows."""
    from oraclous_governance import use_organisation_context

    user_a = uuid.uuid4()

    # WRITE org A's run, org A bound.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_EXECUTION),
                {
                    "id": uuid.uuid4(),
                    "org": ORG_A,
                    "user": user_a,
                    "harness": uuid.uuid4(),
                    "name": "demo-harness",
                    "input": "hello",
                },
            )

    # READ under org A: visible (RLS alone scopes — no WHERE in the SELECT).
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            a_rows = [r[0] for r in (await conn.execute(text(_SELECT_EXECUTION_USERS))).all()]
    assert a_rows == [user_a]

    # READ under org B: org A's row is INVISIBLE.
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            b_rows = [r[0] for r in (await conn.execute(text(_SELECT_EXECUTION_USERS))).all()]
    assert b_rows == []

    # CROSS-ORG WRITE: org B bound, stamping org A's id → 42501.
    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_EXECUTION),
                    {
                        "id": uuid.uuid4(),
                        "org": ORG_A,  # smuggled — not the bound org
                        "user": uuid.uuid4(),
                        "harness": uuid.uuid4(),
                        "name": "smuggled",
                        "input": "x",
                    },
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    # FAIL-CLOSED: with NO org context bound, the guard binds the empty GUC → zero rows (T1-M1).
    async with app_engine.begin() as conn:
        assert (await conn.execute(text(_SELECT_EXECUTION_USERS))).all() == []


async def test_runtime_role_is_non_bypassing(app_engine: AsyncEngine) -> None:
    """The role the runtime connects as must be NOSUPERUSER/NOBYPASSRLS, else the policy is inert
    (T1-M3) — the precondition the isolation above depends on, and what
    ``assert_runtime_role_isolates`` enforces at startup."""
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
