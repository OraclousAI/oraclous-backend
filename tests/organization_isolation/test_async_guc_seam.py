"""Data-layer proof for the substrate ASYNC org-GUC seam (ADR-030 §2) on the testcontainers harness.

The sync ``bind_organisation_guc`` / ``scoped_pg_connection`` are proven by
``test_query_path_org_enforcement.py``; this is their async equivalent — the load-bearing new piece
the async SQLAlchemy services use. It exercises, against the real KGS ``knowledge_graphs`` table
(``apply()``-created, RLS forced) under the NOSUPERUSER ``oraclous_app`` role both forms:

* ``bind_org_guc_async(conn, organisation_id=…)`` — the explicit-org bind: a read bound to org A
  sees only org A's row; a cross-org write under org B's bind is denied (WITH CHECK → 42501).
* ``install_org_guc_guard(engine)`` — the engine ``begin`` event: a transaction opened inside a
  ``use_organisation_context`` block is org-bound automatically (no explicit bind call), and with no
  context bound it fails closed to zero rows.

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

_INSERT = (
    "INSERT INTO public.knowledge_graphs (id, organisation_id, user_id, name) "
    "VALUES (:id, :org, :u, :n)"
)
_SELECT_NAMES = "SELECT name FROM public.knowledge_graphs"
_COUNT = "SELECT count(*) FROM public.knowledge_graphs"


def _ctx(org: uuid.UUID):  # noqa: ANN202
    from oraclous_governance.context import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


@pytest.fixture
async def app_async_engine(app_dsn: str) -> AsyncIterator[AsyncEngine]:
    """An asyncpg AsyncEngine on the ``oraclous_app`` role (derived from the psycopg ``app_dsn``
    fixture, which already applied the A1 schema + created the NOSUPERUSER role + granted DML)."""
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
        await conn.execute(
            text(_INSERT), {"id": uuid.uuid4(), "org": ORG_A, "u": uuid.uuid4(), "n": name}
        )

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
            await conn.execute(
                text(_INSERT),
                {"id": uuid.uuid4(), "org": ORG_A, "u": uuid.uuid4(), "n": "smuggled"},
            )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


async def test_install_org_guc_guard_binds_from_context(app_async_engine: AsyncEngine) -> None:
    from oraclous_governance.propagation import use_organisation_context
    from oraclous_substrate.access_async import install_org_guc_guard

    install_org_guc_guard(app_async_engine)
    name = f"guard-{uuid.uuid4()}"

    # the begin-event binds the GUC from the bound context — no explicit bind call here.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_async_engine.begin() as conn:
            await conn.execute(
                text(_INSERT), {"id": uuid.uuid4(), "org": ORG_A, "u": uuid.uuid4(), "n": name}
            )
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
