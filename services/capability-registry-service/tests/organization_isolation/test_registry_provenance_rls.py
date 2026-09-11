"""Data-layer proof that the Postgres RLS backstop isolates ``registry_provenance`` (#826, the 24
August + 11 September rulings) — the new strict table this issue adds alongside the collector emit.

Mirrors ``test_capreg_rls_backstop_isolation.py``'s strict-table proof (``executions``): direct SQL
with NO ``organisation_id`` predicate — RLS alone must scope it — plus a real-repository-path class
that drives the ACTUAL (read-only) repository under the ``oraclous_app`` role, never hand-binding
the GUC, so a repo that opens a session without binding the org cannot hide behind a hand-bound
test.

RED until the ``registry_provenance`` table/migration exist (every raw SQL statement here fails
against relation "registry_provenance") and until the read-only repository exists (function-local
import — TST001).

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

ORG_A = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
ORG_B = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

_SELECT_ACTIONS = "SELECT action FROM registry_provenance"
_COUNT = "SELECT count(*) FROM registry_provenance"
_INSERT = (
    "INSERT INTO registry_provenance "
    "(id, organisation_id, principal, action, resource, outcome) "
    "VALUES (:id, :org, :principal, :action, :resource, :outcome)"
)


@pytest.fixture
async def app_engine(capreg_dsns) -> AsyncIterator[AsyncEngine]:  # noqa: ANN001
    """An AsyncEngine on the ``oraclous_app`` role with the org-GUC guard installed — the same
    seam the runtime factory installs (mirrors ``test_capreg_rls_backstop_isolation.app_engine``).
    """
    from oraclous_substrate.access_async import install_org_guc_guard

    _owner_async_dsn, app_async_dsn = capreg_dsns
    engine = create_async_engine(app_async_dsn)
    install_org_guc_guard(engine)
    yield engine
    await engine.dispose()


def _ctx(org: uuid.UUID):  # noqa: ANN202
    from oraclous_governance import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


async def test_strict_table_isolates_reads_and_denies_cross_org_writes(
    app_engine: AsyncEngine,
) -> None:
    """The strict org-isolation policy on ``registry_provenance``: a tenant reads only its own
    events, a cross-org write is denied (42501), and an unbound GUC fails closed to zero rows."""
    from oraclous_governance import use_organisation_context

    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT),
                {
                    "id": uuid.uuid4(),
                    "org": ORG_A,
                    "principal": "user-a",
                    "action": "capability.invoke",
                    "resource": "tool_instance:1",
                    "outcome": "succeeded",
                },
            )

    # READ under org A: visible — the SELECT carries NO organisation_id WHERE, RLS alone scopes it.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            a_rows = [r[0] for r in (await conn.execute(text(_SELECT_ACTIONS))).all()]
    assert a_rows == ["capability.invoke"]

    # READ under org B: org A's row is INVISIBLE — the backstop proof.
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            b_rows = [r[0] for r in (await conn.execute(text(_SELECT_ACTIONS))).all()]
    assert b_rows == []

    # CROSS-ORG WRITE: org B bound, inserting a row stamped for org A violates WITH CHECK → 42501.
    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT),
                    {
                        "id": uuid.uuid4(),
                        "org": ORG_A,  # smuggled — not the bound (ORG_B) GUC
                        "principal": "user-b",
                        "action": "capability.invoke",
                        "resource": "tool_instance:2",
                        "outcome": "succeeded",
                    },
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    # FAIL-CLOSED: with NO org context bound, the guard binds the empty GUC → zero rows (T1-M1).
    async with app_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT))).scalar_one() == 0


class TestRealRepoPathBindsTheGuc:
    """The real-path proof: drive the ACTUAL read-only repository under the ``oraclous_app``
    engine — the repo binds the org GUC itself via its own ``org_scope``, never hand-bound here.
    FAILS pre-impl (the repository module does not exist); PASSES once every repo read is wrapped
    in ``org_scope(organisation_id)``."""

    @pytest.fixture
    async def repo(self, capreg_dsns):  # noqa: ANN001, ANN201
        from oraclous_capability_registry_service.repositories.registry_provenance_repository import (  # noqa: E501
            RegistryProvenanceRepository,
        )

        _owner_async_dsn, app_async_dsn = capreg_dsns
        repo = RegistryProvenanceRepository(app_async_dsn)
        try:
            yield repo
        finally:
            await repo.close()

    async def test_org_b_never_reads_org_as_events_through_the_real_repo(
        self, app_engine: AsyncEngine, repo
    ) -> None:  # noqa: ANN001
        from oraclous_governance import use_organisation_context

        with use_organisation_context(_ctx(ORG_A)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT),
                    {
                        "id": uuid.uuid4(),
                        "org": ORG_A,
                        "principal": "user-a",
                        "action": "capability.invoke",
                        "resource": "tool_instance:3",
                        "outcome": "succeeded",
                    },
                )

        # ORG_A reads its OWN row back through the real repo (repo binds org_scope(ORG_A) itself).
        a_events = await repo.list_by_org(ORG_A, limit=10)
        assert len(a_events) == 1
        assert a_events[0].resource == "tool_instance:3"

        # ORG_B never sees ORG_A's row through the same repo.
        b_events = await repo.list_by_org(ORG_B, limit=10)
        assert b_events == []
