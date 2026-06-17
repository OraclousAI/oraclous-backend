"""Data-layer proof that the Postgres RLS backstop isolates the capability-registry-service's
org-scoped tables on its own — the app-layer ``WHERE organisation_id`` removed (ADR-030 / #353).

Distinct from the API/unit tests (which prove the app-layer scoping + the repository's widened-read
behave): here we go under the repositories and prove the *backstop* — that even with the app-layer
predicate gone, RLS alone scopes reads and denies cross-org writes. This is the real point of the
backstop: a bug that drops a ``WHERE`` clause no longer leaks cross-org rows.

Two policy shapes are proven (matching 0006_enable_rls):

* The three STRICT tables (``tool_instances``, ``executions``, ``harness_graph_binding``):
  ``USING`` == ``WITH CHECK`` == strict caller-org equality — a cross-org read returns zero rows, a
  cross-org write raises 42501, and an unbound GUC fails closed to zero rows (T1-M1).

* ``capability_descriptors`` — WIDENED READ, STRICT WRITE (the platform tool-catalogue case): a
  tenant READS its own rows AND the PLATFORM_ORG's built-in catalogue, but NOT another tenant's
  rows; a tenant WRITE stays strict (a cross-org INSERT — including stamping the platform org —
  raises 42501). This is the load-bearing capreg-specific behaviour: the shared catalogue must stay
  readable by every tenant while no tenant can mutate it.

Run as the NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` role (the deployed runtime role) — RLS only
bites a non-superuser, so the isolation proven here is real. The org GUC is bound exactly as runtime
binds it (``use_organisation_context`` + the engine ``begin`` guard installed by
``install_org_guc_guard``, the same guard ``build_rls_engine`` installs), never via a hand-written
WHERE. Mirrors the credential-broker / knowledge-graph ``test_rls_backstop_isolation``.

The policy-level tests above hand-bind the GUC (``use_organisation_context``) around raw SQL — they
prove the *policy* is shaped right, but they cannot catch a repository that never binds the GUC at
all. :class:`TestRealRepoPathBindsTheGuc` closes that gap: it drives the **actual repository
methods** under the ``oraclous_app`` engine and the repos bind the GUC themselves (via their own
``org_scope``) — nothing in the test hand-binds ``app.current_organisation_id``. It proves a tenant
creates a descriptor + a tool_instance and reads its OWN rows back (NOT zero rows — the regression
that shipped when the repos opened sessions on the GUC-guarded engine without binding), still reads
the platform catalogue (widened), gets nothing for a cross-org read, and is denied a cross-org write
(42501). This is the test that FAILS against the pre-fix repos (empty GUC → zero rows / 42501) and
PASSES once every repo op is wrapped in ``org_scope(organisation_id)``.

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
PLATFORM_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a0")

# --- STRICT table: executions. Chosen for the strict proof because it carries NO cross-table FK
# (tool_instances / harness_graph_binding FK capability_descriptors, which is itself now RLS'd —
# using executions keeps the strict-policy proof self-contained). Direct SQL with NO organisation_id
# predicate — RLS alone scopes it. user_id/instance_id/capability_id are plain UUIDs (no FK), and
# credits_consumed is NOT NULL with a Python-side default so the raw INSERT supplies it; status is
# the `executionstatus` PG enum.
_SELECT_EXECUTION_USERS = "SELECT user_id FROM executions"
_COUNT_EXECUTIONS = "SELECT count(*) FROM executions"
_INSERT_EXECUTION = (
    "INSERT INTO executions "
    "(id, organisation_id, instance_id, capability_id, user_id, status, credits_consumed) "
    "VALUES (:id, :org, :instance, :cap, :user, 'QUEUED'::executionstatus, 0)"
)

# --- WIDENED-READ table: capability_descriptors. kind is the PG enum `descriptorkind` (lowercase
# values); descriptor is JSONB NOT NULL; status/created_at/updated_at have server defaults.
_SELECT_DESCRIPTOR_NAMES = "SELECT name FROM capability_descriptors ORDER BY name"
_COUNT_DESCRIPTORS = "SELECT count(*) FROM capability_descriptors"
_INSERT_DESCRIPTOR = (
    "INSERT INTO capability_descriptors (id, organisation_id, kind, name, descriptor) "
    "VALUES (:id, :org, 'tool'::descriptorkind, :name, '{}'::jsonb)"
)


@pytest.fixture
async def app_engine(capreg_dsns) -> AsyncIterator[AsyncEngine]:  # noqa: ANN001
    """An AsyncEngine on the oraclous_app role with the org-GUC guard installed (so a transaction
    under a bound ``use_organisation_context`` sets the GUC), schema+RLS+role already provisioned by
    ``capreg_dsns`` as the owner. Uses the SAME ``install_org_guc_guard`` the runtime factory."""
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
    """The strict org-isolation policy on ``executions``: read filtered to the bound org, a
    cross-org write denied (42501), an unbound GUC fails closed to zero rows."""
    from oraclous_governance import use_organisation_context

    user_a = uuid.uuid4()

    # WRITE org A's execution, org A bound (the engine begin-guard sets the GUC; WITH CHECK admits).
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_EXECUTION),
                {
                    "id": uuid.uuid4(),
                    "org": ORG_A,
                    "instance": uuid.uuid4(),
                    "cap": uuid.uuid4(),
                    "user": user_a,
                },
            )

    # READ under org A: visible — the SELECT carries NO organisation_id WHERE, so RLS alone scopes.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            a_rows = [r[0] for r in (await conn.execute(text(_SELECT_EXECUTION_USERS))).all()]
    assert a_rows == [user_a]

    # READ under org B: org A's row is INVISIBLE (RLS USING filters it) — the backstop proof.
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            b_rows = [r[0] for r in (await conn.execute(text(_SELECT_EXECUTION_USERS))).all()]
    assert b_rows == []

    # CROSS-ORG WRITE: org B bound, inserting a row stamped for org A violates WITH CHECK → 42501.
    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_EXECUTION),
                    {
                        "id": uuid.uuid4(),
                        "org": ORG_A,  # smuggled — not the bound org
                        "instance": uuid.uuid4(),
                        "cap": uuid.uuid4(),
                        "user": uuid.uuid4(),
                    },
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    # FAIL-CLOSED: with NO org context bound, the guard binds the empty GUC → zero rows (T1-M1).
    async with app_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT_EXECUTIONS))).scalar_one() == 0


async def test_capability_descriptors_widened_read_admits_platform_org_not_other_tenants(
    app_engine: AsyncEngine,
) -> None:
    """``capability_descriptors`` widened-read policy: a tenant reads its OWN rows AND the
    PLATFORM_ORG's shared catalogue, but NEVER another tenant's rows. Writes stay strict."""
    from oraclous_governance import use_organisation_context

    # Seed the PLATFORM catalogue (org-scope bound to the platform org so WITH CHECK admits it).
    with use_organisation_context(_ctx(PLATFORM_ORG)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_DESCRIPTOR),
                {"id": uuid.uuid4(), "org": PLATFORM_ORG, "name": "platform-builtin"},
            )
    # Org A writes its own descriptor.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_DESCRIPTOR),
                {"id": uuid.uuid4(), "org": ORG_A, "name": "org-a-tool"},
            )
    # Org B writes its own descriptor.
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT_DESCRIPTOR),
                {"id": uuid.uuid4(), "org": ORG_B, "name": "org-b-tool"},
            )

    # READ as org A: sees its OWN row + the PLATFORM catalogue, NOT org B's. The load-bearing
    # widened-read: the shared catalogue stays readable while another tenant's rows stay hidden.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            a_names = [r[0] for r in (await conn.execute(text(_SELECT_DESCRIPTOR_NAMES))).all()]
    assert a_names == ["org-a-tool", "platform-builtin"]
    assert "org-b-tool" not in a_names

    # READ as org B: its OWN row + the PLATFORM catalogue, NOT org A's.
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            b_names = [r[0] for r in (await conn.execute(text(_SELECT_DESCRIPTOR_NAMES))).all()]
    assert b_names == ["org-b-tool", "platform-builtin"]
    assert "org-a-tool" not in b_names

    # READ as the PLATFORM org: only the platform catalogue (the widened branch is caller==extra,
    # which collapses to strict equality — a tenant's rows are NOT visible to the platform org).
    with use_organisation_context(_ctx(PLATFORM_ORG)):
        async with app_engine.begin() as conn:
            p_names = [r[0] for r in (await conn.execute(text(_SELECT_DESCRIPTOR_NAMES))).all()]
    assert p_names == ["platform-builtin"]

    # FAIL-CLOSED: with NO org context bound, the guard binds the empty GUC. The widened USING still
    # admits the PLATFORM_ORG branch (a fixed literal, GUC-independent) but never a tenant row — so
    # an unscoped read leaks at most the already-public platform catalogue, never tenant data.
    async with app_engine.begin() as conn:
        unscoped = [r[0] for r in (await conn.execute(text(_SELECT_DESCRIPTOR_NAMES))).all()]
    assert unscoped == ["platform-builtin"]


async def test_capability_descriptors_cross_org_write_is_denied(app_engine: AsyncEngine) -> None:
    """Writes to ``capability_descriptors`` stay STRICT despite the widened read: a tenant cannot
    write into another org's scope NOR into the PLATFORM org (it can only READ the platform
    catalogue, never mutate it). Both violate the strict WITH CHECK → 42501."""
    from oraclous_governance import use_organisation_context

    # org A stamping org B → 42501.
    with pytest.raises(ProgrammingError) as exc_b:
        with use_organisation_context(_ctx(ORG_A)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_DESCRIPTOR),
                    {"id": uuid.uuid4(), "org": ORG_B, "name": "cross-tenant-write"},
                )
    assert getattr(exc_b.value.orig, "sqlstate", None) == "42501"

    # org A stamping the PLATFORM org → 42501 (a tenant cannot inject into the shared catalogue).
    with pytest.raises(ProgrammingError) as exc_p:
        with use_organisation_context(_ctx(ORG_A)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_DESCRIPTOR),
                    {"id": uuid.uuid4(), "org": PLATFORM_ORG, "name": "catalogue-poison"},
                )
    assert getattr(exc_p.value.orig, "sqlstate", None) == "42501"


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


def _tool_descriptor(name: str) -> dict:
    """A minimal valid kind=tool descriptor — ``metadata.name`` drives the denormalised name col."""
    return {
        "metadata": {"name": name, "description": f"{name} (rls real-path test)"},
        "spec": {"type": "function", "capabilities": [{"name": "noop"}]},
    }


class TestRealRepoPathBindsTheGuc:
    """The real-path proof (the gap the policy-level tests above could not catch): drive the ACTUAL
    repository methods under the ``oraclous_app`` engine. The repositories bind the org GUC
    themselves via their own ``org_scope(organisation_id)`` — this test NEVER hand-binds
    ``app.current_organisation_id`` (no ``use_organisation_context`` around the repo calls). So if a
    repo opens a session on the GUC-guarded engine without binding the org, the GUC is empty and RLS
    returns zero rows / denies the write — exactly the fail-closed bug this slice introduced and
    this class regresses against.

    FAILS pre-fix (repos never bound the GUC → a tenant read its OWN row back as zero rows, and a
    tenant INSERT would 42501 under the runtime role). PASSES once every repo op is wrapped in
    ``org_scope(organisation_id)``.
    """

    @pytest.fixture
    async def repos(self, capreg_dsns):  # noqa: ANN001, ANN201
        """Real ``CapabilityRepository`` (platform-widened) + ``InstanceRepository`` built on the
        **app** asyncpg DSN — the NOSUPERUSER runtime role. Each repo constructs its own
        ``build_rls_engine`` (installing the GUC guard) and binds the GUC via its own ``org_scope``,
        so this is the genuine request-path wiring, not a hand-bound engine."""
        from oraclous_capability_registry_service.repositories.capability_repository import (
            CapabilityRepository,
        )
        from oraclous_capability_registry_service.repositories.instance_repository import (
            InstanceRepository,
        )

        _owner_async_dsn, app_async_dsn = capreg_dsns
        cap_repo = CapabilityRepository(app_async_dsn, platform_org_id=PLATFORM_ORG)
        inst_repo = InstanceRepository(app_async_dsn)
        try:
            yield cap_repo, inst_repo
        finally:
            await cap_repo.close()
            await inst_repo.close()

    async def test_tenant_reads_own_rows_and_platform_catalogue_via_real_repo(self, repos) -> None:  # noqa: ANN001
        """A tenant creates a descriptor + a tool_instance through the real repos and reads its OWN
        rows back (NOT zero), still sees the platform catalogue (widened read), and never sees
        another tenant's rows — all with the repo binding the GUC itself."""
        from oraclous_capability_registry_service.models.enums import DescriptorKind, InstanceStatus

        cap_repo, inst_repo = repos

        # PLATFORM catalogue entry — the repo binds org_scope(PLATFORM) and the strict WITH CHECK
        # admits the platform-stamped write (mirrors the startup seed path).
        platform_tool = await cap_repo.create(
            organisation_id=PLATFORM_ORG,
            kind=DescriptorKind.TOOL,
            descriptor=_tool_descriptor("platform-builtin"),
        )

        # ORG_A registers its OWN tool through the real repo (repo binds org_scope(ORG_A)).
        org_a_tool = await cap_repo.create(
            organisation_id=ORG_A,
            kind=DescriptorKind.TOOL,
            descriptor=_tool_descriptor("org-a-tool"),
        )
        # ORG_B registers its own tool too (to prove A never sees it).
        org_b_tool = await cap_repo.create(
            organisation_id=ORG_B,
            kind=DescriptorKind.TOOL,
            descriptor=_tool_descriptor("org-b-tool"),
        )

        # READ-BACK (the core regression): ORG_A reads its OWN descriptor by id — must be the row,
        # NOT None. Pre-fix the empty GUC made RLS hide it and this returned None.
        fetched = await cap_repo.get_by_id(org_a_tool.id, ORG_A)
        assert fetched is not None
        assert fetched.id == org_a_tool.id
        assert fetched.organisation_id == ORG_A

        # WIDENED read: ORG_A's catalogue listing shows its OWN tool AND the platform built-in, but
        # NOT ORG_B's — proving the GUC bound to ORG_A drives the (org=GUC OR org=PLATFORM) policy.
        names_a = {d.name for d in await cap_repo.list_by_org(ORG_A)}
        assert "org-a-tool" in names_a
        assert "platform-builtin" in names_a
        assert "org-b-tool" not in names_a

        # The platform built-in is reachable by id from ORG_A (widened read), ORG_B's is not.
        assert await cap_repo.get_by_id(platform_tool.id, ORG_A) is not None
        assert await cap_repo.get_by_id(org_b_tool.id, ORG_A) is None

        # tool_instances (a STRICT table): ORG_A creates an instance of its own tool and reads it
        # back. Pre-fix the INSERT would 42501 (empty GUC vs strict WITH CHECK); the read would be
        # zero rows. The FK needs the descriptor to exist in a readable scope (it does — ORG_A's).
        instance = await inst_repo.create(
            organisation_id=ORG_A,
            capability_id=org_a_tool.id,
            user_id=uuid.uuid4(),
            name="org-a-instance",
            description=None,
            configuration={},
            settings={},
            required_credentials=[],
            status=InstanceStatus.PENDING,
        )
        got = await inst_repo.get_by_id(instance.id, ORG_A)
        assert got is not None and got.id == instance.id
        assert [i.id for i in await inst_repo.list_by_org(ORG_A)] == [instance.id]

        # CROSS-ORG read on the strict table: ORG_B sees none of ORG_A's instances.
        assert await inst_repo.get_by_id(instance.id, ORG_B) is None
        assert await inst_repo.list_by_org(ORG_B) == []

    async def test_cross_org_write_denied_through_repo_binding(self, repos) -> None:  # noqa: ANN001
        """A write whose stamped org differs from the bound GUC is denied 42501 — proven through the
        repo's OWN binding seam (``org_scope`` from ``core.rls``, the exact mechanism the fix adds)
        and the repo's RLS engine, not a hand-built engine. Binds ORG_B but inserts a row stamped
        ORG_A into the strict ``executions`` table: the strict WITH CHECK rejects it."""
        from oraclous_capability_registry_service.core.rls import org_scope

        cap_repo, _inst_repo = repos

        # Use the repository's own engine + its own org_scope seam (what the fix wired in). Binding
        # ORG_B then INSERTing a row stamped ORG_A violates the strict WITH CHECK on executions.
        with pytest.raises(ProgrammingError) as exc_info:
            with org_scope(ORG_B):
                async with cap_repo.engine.begin() as conn:
                    await conn.execute(
                        text(_INSERT_EXECUTION),
                        {
                            "id": uuid.uuid4(),
                            "org": ORG_A,  # smuggled — not the bound (ORG_B) GUC
                            "instance": uuid.uuid4(),
                            "cap": uuid.uuid4(),
                            "user": uuid.uuid4(),
                        },
                    )
        assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

        # And a tenant cannot mutate the PLATFORM catalogue through the real write path: a row
        # stamped PLATFORM while ORG_A is bound is rejected by the strict WITH CHECK (42501).
        with pytest.raises(ProgrammingError) as exc_platform:
            with org_scope(ORG_A):
                async with cap_repo.engine.begin() as conn:
                    await conn.execute(
                        text(_INSERT_DESCRIPTOR),
                        {"id": uuid.uuid4(), "org": PLATFORM_ORG, "name": "catalogue-poison"},
                    )
        assert getattr(exc_platform.value.orig, "sqlstate", None) == "42501"
