"""Data-layer proof for the substrate ASYNC org-GUC seam (ADR-030 §2) on the testcontainers harness.

The sync ``bind_organisation_guc`` / ``scoped_pg_connection`` are proven by
``test_query_path_org_enforcement.py``; this is their async equivalent — the load-bearing new piece
the async SQLAlchemy services use. It exercises both forms under the NOSUPERUSER ``oraclous_app``
role against a **dedicated, RLS-forced probe table this test owns** (created + RLS-enabled +
granted as the owner in the ``_probe_table`` fixture). It deliberately does NOT reuse a service
table like ``knowledge_graphs``: in the shared CI integration DB that table's schema/defaults vary
by cross-service test-creation order, which is irrelevant to what this test proves. The probe table
is fully self-contained, so the proof is deterministic regardless of suite ordering.

* ``bind_org_guc_async(conn, organisation_id=…)`` — explicit-org bind: a read bound to org A sees
  only org A's row; a cross-org write under org B's bind is denied (WITH CHECK → 42501).
* ``install_org_guc_guard(engine)`` — the engine ``begin`` event: a transaction opened inside a
  ``use_organisation_context`` block is org-bound automatically; with no context bound it fails
  closed to zero rows.

Threats: T1-M1, T1-M3. ADR-006; ADR-012 §2; ADR-030.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_PROBE = "rls_async_probe"
# Plain literals (not f-strings) so ruff's S608 doesn't false-positive on a hardcoded table name.
_INSERT = "INSERT INTO public.rls_async_probe (id, organisation_id, name) VALUES (:id, :org, :n)"
_SELECT_NAMES = "SELECT name FROM public.rls_async_probe"  # noqa: S608 — constant table name
_COUNT = "SELECT count(*) FROM public.rls_async_probe"  # noqa: S608 — constant table name


@pytest.fixture
def _probe_table(postgres_dsn: str) -> str:
    """Own a dedicated org-scoped, RLS-FORCED probe table (created + policy + DML grant to
    oraclous_app, as the owner) — so this test is independent of any service's ambient schema."""
    import psycopg
    from oraclous_substrate.schema.postgres import enable_rls_on

    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'CREATE TABLE IF NOT EXISTS public."{_PROBE}" '
                "(id uuid PRIMARY KEY, organisation_id uuid NOT NULL, name text NOT NULL)"
            )
            cur.execute(f'TRUNCATE public."{_PROBE}"')
        enable_rls_on(conn, _PROBE)
        with conn.cursor() as cur:
            cur.execute(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON public."{_PROBE}" TO oraclous_app'
            )
    return _PROBE


def _ctx(org: uuid.UUID):  # noqa: ANN202
    from oraclous_governance.context import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


@pytest.fixture
async def app_async_engine(app_dsn: str, _probe_table: str) -> AsyncIterator[AsyncEngine]:
    """An asyncpg AsyncEngine on the ``oraclous_app`` role (derived from the psycopg ``app_dsn``
    fixture, which already created the NOSUPERUSER role + granted DML); the probe table is owned by
    the ``_probe_table`` fixture."""
    parts = urlsplit(app_dsn)
    async_dsn = urlunsplit(
        ("postgresql+asyncpg", parts.netloc, parts.path, parts.query, parts.fragment)
    )
    engine = create_async_engine(async_dsn)
    yield engine
    await engine.dispose()


async def test_bind_org_guc_async_explicit_org_isolates(app_async_engine: AsyncEngine) -> None:
    from oraclous_substrate.access_async import bind_org_guc_async

    name = f"async-guc-{uuid.uuid4()}"

    # WRITE org A's row, binding the GUC explicitly to org A (WITH CHECK admits it).
    async with app_async_engine.begin() as conn:
        await bind_org_guc_async(conn, organisation_id=str(ORG_A))
        await conn.execute(text(_INSERT), {"id": uuid.uuid4(), "org": ORG_A, "n": name})

    # READ bound to org A → visible; bound to org B → invisible (no app-WHERE; RLS alone scopes).
    async with app_async_engine.begin() as conn:
        await bind_org_guc_async(conn, organisation_id=str(ORG_A))
        a = [r[0] for r in (await conn.execute(text(_SELECT_NAMES))).all()]
    assert name in a

    async with app_async_engine.begin() as conn:
        await bind_org_guc_async(conn, organisation_id=str(ORG_B))
        b = [r[0] for r in (await conn.execute(text(_SELECT_NAMES))).all()]
    assert name not in b

    # CROSS-ORG WRITE: bound to org B, inserting an org-A-stamped row → 42501 (WITH CHECK).
    with pytest.raises(ProgrammingError) as exc_info:
        async with app_async_engine.begin() as conn:
            await bind_org_guc_async(conn, organisation_id=str(ORG_B))
            await conn.execute(text(_INSERT), {"id": uuid.uuid4(), "org": ORG_A, "n": "smuggled"})
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


async def test_install_org_guc_guard_binds_from_context(app_async_engine: AsyncEngine) -> None:
    from oraclous_governance.propagation import use_organisation_context
    from oraclous_substrate.access_async import install_org_guc_guard

    install_org_guc_guard(app_async_engine)
    name = f"guard-{uuid.uuid4()}"

    # the begin-event binds the GUC from the bound context — no explicit bind call here.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_async_engine.begin() as conn:
            await conn.execute(text(_INSERT), {"id": uuid.uuid4(), "org": ORG_A, "n": name})
        async with app_async_engine.begin() as conn:
            a = [r[0] for r in (await conn.execute(text(_SELECT_NAMES))).all()]
    assert name in a

    with use_organisation_context(_ctx(ORG_B)):
        async with app_async_engine.begin() as conn:
            b = [r[0] for r in (await conn.execute(text(_SELECT_NAMES))).all()]
    assert name not in b

    # FAIL-CLOSED: with no context bound the guard sets the empty GUC → zero rows (never widen).
    async with app_async_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT))).scalar_one() == 0
