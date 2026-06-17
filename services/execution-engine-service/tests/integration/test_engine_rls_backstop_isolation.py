"""Data-layer proof of the execution-engine-service RLS SPLIT (ADR-030 / #353).

Two halves, both load-bearing for the engine's two-engine carve (ADR-030 §3):

1. The ORG-BOUND engine (the NOSUPERUSER ``oraclous_app`` role + the org-GUC guard) isolates the
   engine's org-scoped tables ON ITS OWN — the app-layer ``WHERE organisation_id`` removed. Even
   with the app predicate gone, RLS alone returns only the bound org's rows, denies a cross-org
   write (SQLSTATE 42501), and fails closed to zero rows when no org is bound (T1-M1). This is the
   real point of the backstop: a bug that drops a ``WHERE`` no longer leaks cross-org rows. Proven
   on ``engine_jobs`` AND ``engine_provenance`` (the table that "rides along").

2. The MAINTENANCE engine (the OWNER role) STILL READS CROSS-ORG — the carve's other half. The
   three cross-org sweeps (the reaper ``list_stale_running`` + Beat ``list_enabled_cron``) MUST keep
   reading across orgs; under FORCE'd RLS the org-bound engine fails them closed, so they run on the
   owner engine which bypasses RLS. This proves the maintenance reader sees BOTH orgs' rows — i.e.
   RLS does NOT fail-close maintenance (the HARD RULE), while the org-bound engine above proves it
   DOES bite the request/driver path.

The org GUC is bound exactly as the runtime binds it (``use_organisation_context`` + ``org_scope`` →
the engine ``begin`` guard installed by ``install_org_guc_guard``), never via a hand-written WHERE.
Mirrors the KGS / credential-broker ``test_rls_backstop_isolation``.

Threats: T1-M1, T1-M3. ADR-006; ADR-012 §1a/§2; ADR-030.
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

# Direct SQL against the table with NO organisation_id predicate — RLS is the only thing scoping it.
_INSERT_JOB = (
    "INSERT INTO engine_jobs "
    "(id, organisation_id, user_id, state, input_text, retry_count, max_retries, progress) "
    "VALUES (:id, :org, :user, 'QUEUED', :input, 0, 0, 0)"
)
_SELECT_JOB_INPUTS = "SELECT input_text FROM engine_jobs"
_COUNT_JOBS = "SELECT count(*) FROM engine_jobs"

# engine_provenance — the table that rides along on the org-bound engine (clean).
_INSERT_PROV = (
    "INSERT INTO engine_provenance (id, organisation_id, principal, action, resource, outcome) "
    "VALUES (:id, :org, 'p', 'engine.job.run', 'engine_job:x', 'SUCCEEDED')"
)
_COUNT_PROV = "SELECT count(*) FROM engine_provenance"


@pytest.fixture
async def app_engine(engine_dsns) -> AsyncIterator[AsyncEngine]:  # noqa: ANN001
    """The ORG-BOUND AsyncEngine on the oraclous_app role with the org-GUC guard installed (so a
    transaction under a bound ``use_organisation_context`` / ``org_scope`` sets the GUC), schema +
    RLS + role already provisioned by ``engine_dsns`` as the owner. Uses the SAME
    ``install_org_guc_guard`` the runtime repositories install."""
    from oraclous_substrate.access_async import install_org_guc_guard

    _owner_async_dsn, app_async_dsn = engine_dsns
    engine = create_async_engine(app_async_dsn)
    install_org_guc_guard(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
async def owner_engine(engine_dsns) -> AsyncIterator[AsyncEngine]:  # noqa: ANN001
    """The MAINTENANCE AsyncEngine on the OWNER role — NO org-GUC guard (mirrors the maintenance
    reader on the owner DSN). The owner bypasses RLS, so it reads cross-org (the sweep reads)."""
    owner_async_dsn, _app_async_dsn = engine_dsns
    engine = create_async_engine(owner_async_dsn)
    yield engine
    await engine.dispose()


def _ctx(org: uuid.UUID):  # noqa: ANN202
    from oraclous_governance import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


async def test_org_bound_engine_isolates_reads_and_denies_cross_org_writes(
    app_engine: AsyncEngine,
) -> None:
    from oraclous_governance import use_organisation_context

    # WRITE org A's job, org A bound (the engine begin-guard sets the GUC; WITH CHECK admits it).
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_JOB),
                {"id": uuid.uuid4(), "org": ORG_A, "user": uuid.uuid4(), "input": "org-a-job"},
            )

    # READ under org A's GUC: the row is visible — and the SELECT carries NO organisation_id WHERE,
    # so RLS alone is what scopes it.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            a_rows = [r[0] for r in (await conn.execute(text(_SELECT_JOB_INPUTS))).all()]
    assert a_rows == ["org-a-job"]

    # READ under org B's GUC: org A's row is INVISIBLE (RLS USING filters it) — the backstop proof
    # with the app-WHERE removed.
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            b_rows = [r[0] for r in (await conn.execute(text(_SELECT_JOB_INPUTS))).all()]
    assert b_rows == []

    # CROSS-ORG WRITE: with org B bound, inserting a job stamped for org A violates the RLS WITH
    # CHECK → SQLSTATE 42501 (InsufficientPrivilege). org B cannot write into org A's scope.
    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_JOB),
                    {
                        "id": uuid.uuid4(),
                        "org": ORG_A,  # smuggled — not the bound org
                        "user": uuid.uuid4(),
                        "input": "smuggled",
                    },
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    # FAIL-CLOSED: with NO org context bound, the guard binds the empty GUC → RLS exposes zero rows
    # (an absent scope denies, never defaults — T1-M1). org A's real row stays hidden.
    async with app_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT_JOBS))).scalar_one() == 0


async def test_org_bound_engine_isolates_provenance_ridealong(app_engine: AsyncEngine) -> None:
    """engine_provenance rides along on the org-bound engine — RLS isolates it too: a cross-org read
    is empty and a cross-org write is denied 42501."""
    from oraclous_governance import use_organisation_context

    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(text(_INSERT_PROV), {"id": uuid.uuid4(), "org": ORG_A})

    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            assert (await conn.execute(text(_COUNT_PROV))).scalar_one() == 0

    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with app_engine.begin() as conn:
                await conn.execute(text(_INSERT_PROV), {"id": uuid.uuid4(), "org": ORG_A})
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


async def test_org_scope_binds_the_per_row_settle(app_engine: AsyncEngine) -> None:
    """The sweeps settle each row under ``org_scope(row.org)`` (not use_organisation_context). Prove
    org_scope binds the GUC identically: a write under org_scope(ORG_A) is admitted, and a write
    smuggling ORG_A under org_scope(ORG_B) is denied 42501 — exactly the per-row settle path."""
    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(ORG_A):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_JOB),
                {"id": uuid.uuid4(), "org": ORG_A, "user": uuid.uuid4(), "input": "settled"},
            )
    with org_scope(ORG_A):
        async with app_engine.begin() as conn:
            assert (await conn.execute(text(_COUNT_JOBS))).scalar_one() == 1

    with pytest.raises(ProgrammingError) as exc_info:
        with org_scope(ORG_B):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_JOB),
                    {"id": uuid.uuid4(), "org": ORG_A, "user": uuid.uuid4(), "input": "x"},
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


async def test_maintenance_owner_engine_reads_cross_org(
    app_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    """The carve's other half (the HARD RULE): the MAINTENANCE (owner) engine still reads ACROSS
    orgs, so the reaper + Beat sweeps are NOT failed closed by RLS. Two orgs' jobs are written via
    the org-bound engine (each under its own org); the owner engine — NO org bound, NO GUC guard —
    sees BOTH, while the org-bound engine with no bound org sees NEITHER (fail-closed)."""
    from oraclous_governance import use_organisation_context

    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_JOB),
                {"id": uuid.uuid4(), "org": ORG_A, "user": uuid.uuid4(), "input": "a"},
            )
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_JOB),
                {"id": uuid.uuid4(), "org": ORG_B, "user": uuid.uuid4(), "input": "b"},
            )

    # The MAINTENANCE owner engine (no GUC bound) sees BOTH orgs — the cross-org sweep read works.
    async with owner_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT_JOBS))).scalar_one() == 2

    # …while the ORG-BOUND engine with NO org bound sees NEITHER (fail-closed) — the contrast that
    # makes the split meaningful: the request path bites, the maintenance path does not.
    async with app_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT_JOBS))).scalar_one() == 0


async def test_runtime_role_is_non_bypassing(app_engine: AsyncEngine) -> None:
    """The role the ORG-BOUND runtime (api + worker) connects as must be NOSUPERUSER/NOBYPASSRLS,
    else the policy is inert (T1-M3) — the precondition the isolation above depends on, and what
    ``assert_runtime_role_isolates`` enforces at web startup + worker_process_init."""
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


async def test_owner_engine_is_bypassing(owner_engine: AsyncEngine) -> None:
    """The MAINTENANCE engine is the OWNER (superuser in the dev stack) — it MUST bypass RLS so the
    cross-org sweep reads pass. The assertion that guards the org-bound role would (correctly) FAIL
    on it, which is why the maintenance engine is deliberately never asserted (ADR-030 §3)."""
    from oraclous_substrate.access_async import RlsBypassingRoleError, assert_non_bypassing_role

    with pytest.raises(RlsBypassingRoleError):
        await assert_non_bypassing_role(owner_engine)
