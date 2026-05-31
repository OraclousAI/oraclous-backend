"""Idempotent pre-R1 agent-identity backfill (ORA-36 / R1-D1).

RED until ``backend-implementer`` adds
``oraclous_auth_service.migrations.agent_identity_backfill``.

Reshape (lift-tag **Reshape**) of the legacy
``knowledge-graph-builder/scripts/backfill_default_orgs.py`` Neo4j-traversal
pattern and the idempotent alembic add-column+backfill style — refit to
issue, for every agent that existed pre-R1 (a legacy
``(:Agent:__Platform__)`` node carrying only the legacy ``org_id`` property),
the three artifacts R1 declares for an agent principal:

1.  a Postgres ``agents`` row keyed on the legacy ``agent_id``, carrying
    ``organisation_id`` per ADR-006 (ORA-30 / R1-A1);
2.  at least one Postgres ``agent_credentials`` row tied to that agent and
    carrying the same ``organisation_id`` (the raw credential is **not**
    reconstructable — the row exists so the principal has a credential of
    record; operators rotate before first authentication);
3.  the ReBAC-traversable subject node shape — ``(:Agent:__Platform__
    {agent_id, organisation_id, …})`` — that the C2 delegation traversal
    (``oraclous_rebac.ReBACEngine.check_graph_permission`` with
    ``subject={"type": "agent", …}``) reads from.

**Module decomposition (ratified by solution-architect comment 10345 +
security-architect co-sign comment 10346):** this is a two-domain migration
— Postgres principal + credential are written by *auth-service*, the Neo4j
subject-node ``organisation_id`` stamp is written by *substrate* — each
store written by its owner (ADR-001 / ADR-012 §1a / §1b). Concretely:

* The orchestrator the tests import (``backfill_agent_identity`` /
  ``rollback_agent_identity``) lives in **auth-service**:
  ``oraclous_auth_service.migrations.agent_identity_backfill``.
* The Postgres writes use auth-service-owned helpers (hash / prefix /
  active-prefix-unique convention from ORA-30 / ADR-012 §1a, kept in one
  home) over the caller's ``postgres_conn`` (caller-controlled txn —
  mirrors ``org_backfill``).
* The Neo4j stamp composes a new context-free, explicit-org node-writer
  living in the *substrate migrations namespace*
  (``oraclous_substrate.migrations.*`` — sibling of ``org_backfill``,
  alongside the explicit-``SEED_ORGANISATION_ID`` pattern that namespace
  already uses). Per security-architect R2: this writer is **never**
  exposed on the request-path access seam beside ``scoped_write_node`` —
  a caller-chooses-org writer at the request boundary is a T1 cross-org-
  write primitive. The canonical ``organisation_id`` / label spelling is
  single-sourced from ``oraclous_substrate.access`` (§1b); the dangerous
  capability stays out of the seam.

The migration is asserted against the real ORA-12 substrate harness
(``neo4j_driver`` + ``postgres_dsn``). Per the brief the migration is
*authored and rehearsed* now; this suite is the staging-rehearsal contract.

Migration contract under test (to be implemented):
  ``backfill_agent_identity(*, postgres_conn, neo4j_driver,
      organisation_id=SEED_ORGANISATION_ID) -> dict[str, int]``
  ``rollback_agent_identity(*, postgres_conn, neo4j_driver) -> None``

Both operate on the caller's connection/driver (mirroring ``org_backfill``);
the Postgres side does not commit (the caller controls the transaction).

T2 (the brief's threat tag) is the through-line: "no agent without a
correctly-scoped principal = no implicit-escalation gap". A migrated
principal must (a) exist in every store, (b) carry the same
``organisation_id`` everywhere, and (c) carry **no** authority — no
``DELEGATED_TO`` edge, no role grant, and a **structurally inert
credential** (security-architect R1): ``validate_credential`` returns
``None`` for any input, and the row is excluded from
``active_credentials_by_prefix`` (keeping the partial-unique active-prefix
slot free for the first real credential an admin later issues — defense
in depth, so inertness does not rest solely on bcrypt preimage resistance).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

# Per-suite marker scoping cleanup so legacy seeds don't bleed into the
# session-shared Neo4j container (mirrors the ORA-24 Neo4j suite pattern).
_NEO4J_MARKER_PROP = "_ora36_marker"

# Per-test agent identifiers. The migration must accept arbitrary legacy
# ``agent_id``s — these strings are not magic.
_AGENT_A = "ora36-agent-a"
_AGENT_B = "ora36-agent-b"
_AGENT_C_NO_ORG = "ora36-agent-c-no-org"

# Two distinct legacy organisations: A and B. The third agent has no legacy
# ``org_id`` at all — the migration must fall back to the seed org (T2: no
# agent left without a correctly-scoped principal, even if legacy data was
# incomplete).
_LEGACY_ORG_A = "ora36-legacy-org-aaaa"
_LEGACY_ORG_B = "ora36-legacy-org-bbbb"


# ── Helpers ────────────────────────────────────────────────────────────────


def _neo4j_clean(driver, marker: str) -> None:
    """Remove every node carrying our per-suite marker (covers seed + backfill)."""
    driver.execute_query(f"MATCH (n) WHERE n.{_NEO4J_MARKER_PROP} = $m DETACH DELETE n", m=marker)


def _seed_legacy_agents(driver, marker: str) -> None:
    """Create the three legacy ``(:Agent:__Platform__)`` nodes.

    Shape mirrors the legacy ``knowledge-graph-builder`` agent nodes: the
    ``agent_id`` property holds the identifier; ``org_id`` is the legacy
    org name (TASK-202 / TASK-203); ``organisation_id`` is **not** set
    (R1's name; the migration's job to add). Agent C has no legacy
    ``org_id`` at all — a truly orphaned legacy agent.
    """
    driver.execute_query(
        f"CREATE (a:Agent:__Platform__ {{agent_id: $aid, org_id: $org, {_NEO4J_MARKER_PROP}: $m}})",
        aid=_AGENT_A,
        org=_LEGACY_ORG_A,
        m=marker,
    )
    driver.execute_query(
        f"CREATE (a:Agent:__Platform__ {{agent_id: $aid, org_id: $org, {_NEO4J_MARKER_PROP}: $m}})",
        aid=_AGENT_B,
        org=_LEGACY_ORG_B,
        m=marker,
    )
    driver.execute_query(
        f"CREATE (a:Agent:__Platform__ {{agent_id: $aid, {_NEO4J_MARKER_PROP}: $m}})",
        aid=_AGENT_C_NO_ORG,
        m=marker,
    )


def _create_agent_tables(conn) -> None:
    """Create the ORA-30 ``agents`` + ``agent_credentials`` tables in legacy shape.

    DDL mirrors ``oraclous_auth_service.models.agent_model`` exactly — the
    suite seeds the schema R1-A1 deployed (ORA-30) so the migration runs
    against the same table shape as production. The partial unique index
    on ``credential_prefix WHERE status='active'`` (ADR-012 §1a) is
    preserved so a migration that issues duplicate active prefixes will
    fail loudly here instead of silently in production.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.agents (
                id text PRIMARY KEY,
                organisation_id text NOT NULL,
                created_by_user_id text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_agents_organisation_id "
            "ON public.agents (organisation_id)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.agent_credentials (
                id text PRIMARY KEY,
                agent_id text NOT NULL,
                organisation_id text NOT NULL,
                credential_hash text NOT NULL,
                credential_prefix text NOT NULL,
                status text NOT NULL DEFAULT 'active',
                created_at timestamptz NOT NULL DEFAULT now(),
                expires_at timestamptz,
                revoked_at timestamptz
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_agent_credentials_agent_id "
            "ON public.agent_credentials (agent_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_agent_credentials_organisation_id "
            "ON public.agent_credentials (organisation_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_agent_credentials_credential_prefix "
            "ON public.agent_credentials (credential_prefix)"
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
              ix_agent_credentials_active_prefix_unique
              ON public.agent_credentials (credential_prefix)
              WHERE status = 'active'
            """
        )
    conn.commit()


def _drop_agent_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.agent_credentials CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.agents CASCADE")
    conn.commit()


def _count(cur, sql: str, params: tuple[Any, ...] = ()) -> int:
    cur.execute(sql, params)
    return cur.fetchone()[0]


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def marker() -> str:
    """A per-test marker tag — keeps the session-shared Neo4j container clean."""
    return f"ora36-{uuid.uuid4()}"


@pytest.fixture
def legacy_neo4j(neo4j_driver, marker: str):
    """A Neo4j driver pre-seeded with the three legacy agent nodes."""
    _neo4j_clean(neo4j_driver, marker)
    _seed_legacy_agents(neo4j_driver, marker)
    try:
        yield neo4j_driver
    finally:
        _neo4j_clean(neo4j_driver, marker)


@pytest.fixture
def fresh_pg(postgres_dsn: str) -> Iterator[psycopg.Connection]:
    """A Postgres connection with the ORA-30 agent tables freshly created.

    Drop-then-create per test so the session-shared container's accumulated
    rows from neighbouring suites cannot bleed into orphan-count assertions.
    """
    with psycopg.connect(postgres_dsn) as conn:
        _drop_agent_tables(conn)
        _create_agent_tables(conn)
        try:
            yield conn
        finally:
            _drop_agent_tables(conn)


# ── Module-import surface ──────────────────────────────────────────────────


class TestMigrationContractSurface:
    """The migration exposes two entry points named exactly as in the brief.

    Names + keyword-only signatures are part of the contract; the body
    shape is the implementer's call.
    """

    def test_backfill_agent_identity_is_importable(self) -> None:
        """The orchestrator the tests import lives in **auth-service**, per
        the solution-architect ruling (comment 10345). The substrate
        migrations namespace owns the Neo4j explicit-org helper that this
        orchestrator composes; it is never imported here directly.
        """
        from oraclous_auth_service.migrations import agent_identity_backfill  # noqa: F401

        assert callable(getattr(agent_identity_backfill, "backfill_agent_identity", None)), (
            "agent_identity_backfill.backfill_agent_identity must exist"
        )

    def test_rollback_agent_identity_is_importable(self) -> None:
        from oraclous_auth_service.migrations import agent_identity_backfill

        assert callable(getattr(agent_identity_backfill, "rollback_agent_identity", None)), (
            "agent_identity_backfill.rollback_agent_identity must exist"
        )

    def test_backfill_accepts_keyword_only_postgres_and_neo4j(self) -> None:
        """The brief calls out *both* stores — ``postgres_conn`` and
        ``neo4j_driver`` must be passable as kwargs; positional binding is
        not pinned (mirrors the kw-only convention of ``org_backfill``).
        """
        import inspect

        from oraclous_auth_service.migrations import agent_identity_backfill

        sig = inspect.signature(agent_identity_backfill.backfill_agent_identity)
        assert "postgres_conn" in sig.parameters, sig
        assert "neo4j_driver" in sig.parameters, sig

    def test_orchestrator_does_not_live_in_substrate_migrations(self) -> None:
        """Solution-architect ruling Q1 (comment 10345): the orchestrator
        lives in **auth-service**, not substrate. The substrate
        ``migrations`` namespace owns the Neo4j explicit-org node-writer
        that the orchestrator composes, but the orchestrator itself
        crossing into substrate would invert the layer dependency
        (auth-service depends on substrate, never the reverse — ADR-001 +
        ADR-012 §1a).
        """
        from oraclous_substrate import migrations as substrate_migrations

        assert not hasattr(substrate_migrations, "agent_identity_backfill"), (
            "the agent-identity orchestrator must NOT be exposed from "
            "oraclous_substrate.migrations — solution-architect Q1 ruling: "
            "the orchestrator lives in auth-service. The substrate "
            "migrations namespace owns only the Neo4j explicit-org helper "
            "the orchestrator composes."
        )


# ── AC#1: zero orphans across every store ──────────────────────────────────


class TestZeroOrphansAcrossEveryStore:
    """A1: "Every pre-existing agent gets a principal + credential + ReBAC
    subject node; none left without (query proves zero orphans)."

    Asserted on the real harness via three deterministic counts after the
    backfill: legacy Neo4j agents without a matching Postgres principal,
    Postgres principals without a credential, Postgres principals whose
    Neo4j subject node carries no ``organisation_id``.
    """

    def test_every_legacy_neo4j_agent_has_a_postgres_principal(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            principals = {row[0] for row in cur.execute("SELECT id FROM public.agents").fetchall()}
        assert {_AGENT_A, _AGENT_B, _AGENT_C_NO_ORG} <= principals, (
            "missing principals after backfill: "
            f"{ {_AGENT_A, _AGENT_B, _AGENT_C_NO_ORG} - principals }"
        )

    def test_every_postgres_principal_has_at_least_one_credential(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            orphans = _count(
                cur,
                "SELECT count(*) FROM public.agents a "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM public.agent_credentials c "
                "  WHERE c.agent_id = a.id"
                ")",
            )
        assert orphans == 0, (
            f"{orphans} principal(s) have no agent_credentials row — "
            "violates AC#1 (a principal without a credential of record)"
        )

    def test_every_legacy_neo4j_agent_has_an_organisation_id_stamped(
        self, legacy_neo4j, marker: str, fresh_pg: psycopg.Connection
    ) -> None:
        """The Neo4j subject node must carry ``organisation_id`` after the
        backfill, regardless of whether it originally had ``org_id`` or
        nothing at all (Agent-C). The C2 delegation traversal filters on
        ``organisation_id`` — a missing value is a silent permission deny."""
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        records, _, _ = legacy_neo4j.execute_query(
            f"MATCH (a:Agent:__Platform__) WHERE a.{_NEO4J_MARKER_PROP} = $m "
            "AND a.organisation_id IS NULL RETURN count(a) AS c",
            m=marker,
        )
        assert records[0]["c"] == 0, (
            f"{records[0]['c']} legacy Agent node(s) still lack "
            "organisation_id after backfill — violates AC#1 / T2"
        )

    def test_returns_summary_dict(self, legacy_neo4j, fresh_pg: psycopg.Connection) -> None:
        """The return value reports counts so an operator running the
        migration has a concrete ledger of what changed (the brief's
        staging-rehearsal expectation)."""
        from oraclous_auth_service.migrations import agent_identity_backfill

        summary = agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        assert isinstance(summary, dict), (
            f"backfill must return a summary dict; got {type(summary).__name__}"
        )
        assert sum(summary.values()) >= 3, (
            f"summary should account for all three migrated agents; got {summary}"
        )


# ── AC#1 cross-store invariant: organisation_id matches everywhere ─────────


class TestOrganisationIdMatchesEverywhere:
    """An agent's ``organisation_id`` must be the *same* value across all
    three stores (Postgres ``agents`` row, every ``agent_credentials`` row,
    Neo4j subject node). A divergence would be a worse failure mode than a
    missing row — a query in one store would mis-classify the principal's
    tenancy.
    """

    @pytest.mark.security
    def test_principal_credential_and_subject_node_share_organisation_id(
        self, legacy_neo4j, marker: str, fresh_pg: psycopg.Connection
    ) -> None:
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        for agent_id in (_AGENT_A, _AGENT_B, _AGENT_C_NO_ORG):
            with fresh_pg.cursor() as cur:
                cur.execute(
                    "SELECT organisation_id FROM public.agents WHERE id = %s",
                    (agent_id,),
                )
                pg_principal_org = cur.fetchone()[0]
                cur.execute(
                    "SELECT array_agg(DISTINCT organisation_id::text) "
                    "FROM public.agent_credentials WHERE agent_id = %s",
                    (agent_id,),
                )
                cred_orgs = cur.fetchone()[0] or []
            records, _, _ = legacy_neo4j.execute_query(
                f"MATCH (a:Agent:__Platform__) WHERE a.{_NEO4J_MARKER_PROP} = $m "
                "AND a.agent_id = $aid RETURN a.organisation_id AS org",
                m=marker,
                aid=agent_id,
            )
            neo4j_org = records[0]["org"]
            assert cred_orgs == [pg_principal_org], (
                f"{agent_id}: credential org(s) {cred_orgs} != principal org "
                f"{pg_principal_org} — cross-store divergence (T2)"
            )
            assert neo4j_org == pg_principal_org, (
                f"{agent_id}: Neo4j subject org {neo4j_org!r} != "
                f"Postgres principal org {pg_principal_org!r}"
            )

    @pytest.mark.security
    def test_legacy_org_id_is_preserved_not_overwritten(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        """Agent A and Agent B had distinct legacy ``org_id`` values. The
        migration must preserve them — overwriting both to the seed org
        would be a cross-org bleed (T1) and a violation of ADR-006 (the
        outermost tenancy scope is the one the data already carries)."""
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            cur.execute(
                "SELECT id, organisation_id FROM public.agents WHERE id IN (%s, %s)",
                (_AGENT_A, _AGENT_B),
            )
            orgs = dict(cur.fetchall())
        assert orgs[_AGENT_A] == _LEGACY_ORG_A, (
            f"Agent A's legacy org_id was not preserved: {orgs[_AGENT_A]!r}"
        )
        assert orgs[_AGENT_B] == _LEGACY_ORG_B, (
            f"Agent B's legacy org_id was not preserved: {orgs[_AGENT_B]!r}"
        )
        assert orgs[_AGENT_A] != orgs[_AGENT_B], (
            "two distinct legacy orgs were collapsed into one — T1 violation"
        )

    def test_truly_orphaned_legacy_agent_falls_back_to_seed_org(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        """Agent C has no legacy ``org_id`` at all. The migration must
        still produce a *correctly-scoped* principal — the seed org is
        the documented fallback (ADR-006 + ORA-24 precedent). A backfill
        that left this agent without a principal would be the exact T2
        gap the brief calls out."""
        from oraclous_auth_service.migrations import agent_identity_backfill
        from oraclous_substrate.organisation import SEED_ORGANISATION_ID

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            cur.execute(
                "SELECT organisation_id FROM public.agents WHERE id = %s",
                (_AGENT_C_NO_ORG,),
            )
            (org,) = cur.fetchone()
        assert org == str(SEED_ORGANISATION_ID), (
            f"orphaned legacy agent did not fall back to the seed org; got {org!r}"
        )


# ── AC#2: idempotency ──────────────────────────────────────────────────────


class TestIdempotency:
    """A2: "Idempotent (re-run is a no-op)". The migration must be safe to
    re-run after a partial / completed run — no duplicate principals,
    no duplicate active credentials, no double-stamped Neo4j properties.
    """

    def test_second_backfill_does_not_duplicate_principals(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            first = _count(cur, "SELECT count(*) FROM public.agents")
        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            second = _count(cur, "SELECT count(*) FROM public.agents")
        assert first == second, f"re-run duplicated principals: {first} -> {second}"

    def test_second_backfill_does_not_duplicate_credentials(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        """A second run must not insert another credential per agent —
        the partial UNIQUE INDEX on active credential prefixes would
        outright reject a duplicate active row, but the migration should
        guard *before* the index does so the rerun is a clean no-op.
        """
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            first = _count(cur, "SELECT count(*) FROM public.agent_credentials")
        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            second = _count(cur, "SELECT count(*) FROM public.agent_credentials")
        assert first == second, f"re-run duplicated credentials: {first} -> {second}"

    def test_second_backfill_is_a_no_op_on_neo4j_subject_nodes(
        self, legacy_neo4j, marker: str, fresh_pg: psycopg.Connection
    ) -> None:
        """The Neo4j Agent count + ``organisation_id`` distribution must
        be byte-identical after a second run."""
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )

        def _snapshot() -> tuple[int, list[str]]:
            records, _, _ = legacy_neo4j.execute_query(
                f"MATCH (a:Agent:__Platform__) WHERE a.{_NEO4J_MARKER_PROP} = $m "
                "RETURN count(a) AS c, collect(a.organisation_id) AS orgs",
                m=marker,
            )
            return records[0]["c"], sorted(records[0]["orgs"])

        before = _snapshot()
        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        after = _snapshot()
        assert before == after, (
            f"backfill_agent_identity is not idempotent on Neo4j: before={before}, after={after}"
        )

    def test_partial_state_resume_does_not_double_insert(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        """Simulate a partial prior run: Agent A's principal already
        exists (operator-injected from a half-completed migration), then
        run the migration. A re-run must complete the missing rows but
        not duplicate Agent A's principal.
        """
        from oraclous_auth_service.migrations import agent_identity_backfill

        with fresh_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO public.agents (id, organisation_id, created_by_user_id) "
                "VALUES (%s, %s, %s)",
                (_AGENT_A, _LEGACY_ORG_A, "ora36-backfill"),
            )
        fresh_pg.commit()
        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            agent_a_count = _count(
                cur, "SELECT count(*) FROM public.agents WHERE id = %s", (_AGENT_A,)
            )
            all_present = {row[0] for row in cur.execute("SELECT id FROM public.agents").fetchall()}
        assert agent_a_count == 1, (
            f"partial-state resume duplicated Agent A's principal: {agent_a_count}"
        )
        assert {_AGENT_B, _AGENT_C_NO_ORG} <= all_present, (
            "partial-state resume failed to complete the missing principals: "
            f"{ {_AGENT_B, _AGENT_C_NO_ORG} - all_present }"
        )


# ── AC#3: rollback ─────────────────────────────────────────────────────────


class TestRollback:
    """A3: "Documented + tested rollback". Rollback must remove the
    Postgres rows the migration inserted and revert any property the
    migration stamped on the Neo4j subject nodes — *without* deleting the
    legacy Agent nodes themselves (they predate the migration; deleting
    them would lose data the migration never owned).
    """

    def test_rollback_removes_backfilled_postgres_principals(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        agent_identity_backfill.rollback_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            principals = _count(
                cur,
                "SELECT count(*) FROM public.agents WHERE id IN (%s, %s, %s)",
                (_AGENT_A, _AGENT_B, _AGENT_C_NO_ORG),
            )
        assert principals == 0, f"rollback left {principals} backfilled principal(s) in Postgres"

    def test_rollback_removes_backfilled_postgres_credentials(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        agent_identity_backfill.rollback_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        with fresh_pg.cursor() as cur:
            creds = _count(
                cur,
                "SELECT count(*) FROM public.agent_credentials WHERE agent_id IN (%s, %s, %s)",
                (_AGENT_A, _AGENT_B, _AGENT_C_NO_ORG),
            )
        assert creds == 0, f"rollback left {creds} backfilled credential(s) in Postgres"

    def test_rollback_preserves_legacy_neo4j_agent_nodes(
        self, legacy_neo4j, marker: str, fresh_pg: psycopg.Connection
    ) -> None:
        """The legacy ``(:Agent:__Platform__)`` nodes predate the migration
        — rollback must not delete them. (It may revert the
        ``organisation_id`` property it stamped, but the nodes themselves
        and their ``agent_id`` must remain readable so a subsequent
        re-attempt has a source to migrate again.)"""
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        agent_identity_backfill.rollback_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        records, _, _ = legacy_neo4j.execute_query(
            f"MATCH (a:Agent:__Platform__) WHERE a.{_NEO4J_MARKER_PROP} = $m "
            "RETURN collect(a.agent_id) AS ids",
            m=marker,
        )
        surviving_ids = set(records[0]["ids"])
        assert {_AGENT_A, _AGENT_B, _AGENT_C_NO_ORG} == surviving_ids, (
            f"rollback deleted (or fabricated) legacy Agent node(s); survivors: {surviving_ids}"
        )

    def test_rollback_is_idempotent(self, legacy_neo4j, fresh_pg: psycopg.Connection) -> None:
        """A second rollback after the first must be a clean no-op (the
        rollback operator's safety net — re-running the rollback after a
        crash must not raise)."""
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        agent_identity_backfill.rollback_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        # Second call must not raise.
        agent_identity_backfill.rollback_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()

    def test_rollback_on_unbackfilled_state_is_a_safe_no_op(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        """Operator may invoke rollback defensively before discovering the
        migration never ran. That call must not raise and must not delete
        the legacy Neo4j Agent nodes."""
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.rollback_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()


# ── T2: no implicit-escalation gap ─────────────────────────────────────────


class TestNoImplicitEscalation:
    """The brief's threat tag: "T2 — no agent without a correctly-scoped
    principal = no implicit-escalation gap". The complementary half is the
    converse: the migration must produce a *bare* principal — one that
    exists, is correctly-scoped, but has **no** authority. A migration
    that silently created a delegation, role grant, or known-good
    credential would invert the threat, granting access to every legacy
    agent at once.
    """

    @pytest.mark.security
    def test_backfill_creates_no_delegated_to_edges(
        self, legacy_neo4j, marker: str, fresh_pg: psycopg.Connection
    ) -> None:
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        records, _, _ = legacy_neo4j.execute_query(
            "MATCH ()-[d:DELEGATED_TO]->(a:Agent:__Platform__) "
            f"WHERE a.{_NEO4J_MARKER_PROP} = $m "
            "RETURN count(d) AS c",
            m=marker,
        )
        assert records[0]["c"] == 0, (
            f"backfill created {records[0]['c']} DELEGATED_TO edge(s) — "
            "T2 implicit-escalation violation (a backfilled agent must "
            "have NO authority until explicitly delegated)"
        )

    @pytest.mark.security
    def test_backfill_creates_no_role_grants_to_backfilled_agents(
        self, legacy_neo4j, marker: str, fresh_pg: psycopg.Connection
    ) -> None:
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        records, _, _ = legacy_neo4j.execute_query(
            "MATCH (a:Agent:__Platform__)-[r:HAS_ROLE]->() "
            f"WHERE a.{_NEO4J_MARKER_PROP} = $m "
            "RETURN count(r) AS c",
            m=marker,
        )
        assert records[0]["c"] == 0, (
            "backfill granted a role to a backfilled agent — T2 implicit-escalation violation"
        )

    @pytest.mark.security
    def test_backfilled_credential_is_structurally_inert(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        """Security-architect R1 (comment 10346): a backfilled principal
        closes the T2 gap only if the credential is *structurally inert*.
        Pin the behaviour — ``validate_credential`` returns ``None`` for
        *any* input — not a specific sentinel-hash spelling.

        Mechanism is the implementer's call: a bcrypt hash of a freshly-
        generated random secret that is discarded *plus* a status that
        excludes the row from the active-prefix lookup (the partial
        UNIQUE INDEX on ``credential_prefix WHERE status='active'`` stays
        free for the first real credential an admin later issues —
        defense in depth, so inertness does not rest solely on bcrypt
        preimage resistance).

        Inputs below are illustrative (empty string, bare credential
        prefix, agent id, fresh random ``oag_…`` tokens). They do not
        lock down a credential-format spelling — they sample the space
        where an honest backfill MUST refuse and a known-shortcut
        backfill (T2 mistake) WOULD authenticate.
        """
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()
        repo, creds = self._repo_over_backfilled_rows(fresh_pg, _AGENT_A)
        assert creds, "no agent_credentials row for Agent A after backfill"

        import asyncio
        import secrets

        candidates = [
            "",
            creds[0].credential_prefix,
            _AGENT_A,
            f"oag_{_AGENT_A}",
            f"oag_{secrets.token_urlsafe(32)}",
            f"oag_{secrets.token_urlsafe(32)}",
        ]
        for candidate in candidates:
            assert asyncio.run(repo.validate_credential(candidate)) is None, (
                f"backfilled credential authenticated against {candidate!r} "
                "— T2 violation: an honest backfill cannot produce a hash "
                "that matches any input the implementer did not retain"
            )

    @pytest.mark.security
    def test_backfilled_credential_is_excluded_from_active_prefix_lookup(
        self, legacy_neo4j, fresh_pg: psycopg.Connection
    ) -> None:
        """Security-architect R1 (comment 10346) — defense in depth: even
        before the bcrypt verify gets a chance, the backfilled row must
        not be returned by ``active_credentials_by_prefix``. The status
        excludes the row from the active set so the partial-unique
        ``credential_prefix WHERE status='active'`` slot stays free for
        the first real credential an admin later issues, *and* a misused
        prefix-injection attack on the lookup path finds no row to
        challenge bcrypt against.
        """
        from oraclous_auth_service.migrations import agent_identity_backfill

        agent_identity_backfill.backfill_agent_identity(
            postgres_conn=fresh_pg, neo4j_driver=legacy_neo4j
        )
        fresh_pg.commit()

        with fresh_pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM public.agent_credentials "
                "WHERE agent_id = %s AND status = 'active'",
                (_AGENT_A,),
            )
            active_count = cur.fetchone()[0]
        assert active_count == 0, (
            "backfilled credential is `status='active'` — would occupy the "
            "partial-unique active-prefix slot and reach the bcrypt verify "
            "path on every prefix-lookup attempt (R1 defense-in-depth "
            "violation)"
        )

    # ── helpers (shared between R1 tests) ──────────────────────────────────

    @staticmethod
    def _repo_over_backfilled_rows(
        conn: psycopg.Connection, agent_id: str
    ) -> tuple[Any, list[Any]]:
        """Build a live ``AgentRepository`` backed by an in-memory snapshot
        of the backfilled rows. Lets the R1 tests exercise the real
        repository ``validate_credential`` path without needing the
        async-SQLAlchemy ``PostgresCredentialStore`` — the SQL
        ``status='active'`` filter is faithfully reproduced in the
        in-memory ``active_credentials_by_prefix``.
        """
        from oraclous_auth_service.models.agent_model import AgentCredential
        from oraclous_auth_service.repositories.agent_repository import (
            AgentRepository,
            CredentialStore,
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, agent_id, organisation_id, credential_hash, "
                "credential_prefix, status, created_at, expires_at, revoked_at "
                "FROM public.agent_credentials WHERE agent_id = %s",
                (agent_id,),
            )
            rows = cur.fetchall()

        def _row_to_cred(row: tuple[Any, ...]) -> AgentCredential:
            cred = AgentCredential(
                id=row[0],
                agent_id=row[1],
                organisation_id=row[2],
                credential_hash=row[3],
                credential_prefix=row[4],
                status=row[5],
            )
            cred.created_at = row[6]
            cred.expires_at = row[7]
            cred.revoked_at = row[8]
            return cred

        creds = [_row_to_cred(r) for r in rows]

        class _SnapshotStore:
            async def persist(self, *_a, **_k) -> None:
                raise AssertionError("validate-only fixture")

            async def active_credentials_by_prefix(self, prefix: str) -> list[AgentCredential]:
                return [c for c in creds if c.status == "active" and c.credential_prefix == prefix]

            async def revoke_agent_credentials(self, _agent_id: str) -> int:
                raise AssertionError("validate-only fixture")

        store: CredentialStore = _SnapshotStore()
        return AgentRepository(store=store), creds


# ── R2: the request-path access seam admits no caller-supplied-org write ──


class TestAccessSeamRemainsCallerOrgFree:
    """Security-architect R2 (comment 10346): the new context-free,
    explicit-``organisation_id`` Neo4j node-writer that the agent-identity
    migration composes lives in ``oraclous_substrate.migrations``
    (alongside ``org_backfill``) — NOT on the request-path access seam
    beside ``scoped_write_node``.

    A caller-chooses-org writer at the seam is a T1 cross-org-write
    primitive the moment it reaches a request path: any handler holding
    a stray ``organisation_id`` value (a user-supplied id, a deserialized
    body field, a confused-deputy parameter) could write into another
    organisation's substrate. The migration needs the capability; the
    request path must never have it. The seam continues to source
    ``organisation_id`` from the bound governance context only (ADR-006 /
    T1-M1 / ADR-012 §1b).
    """

    def test_scoped_write_node_takes_no_caller_supplied_organisation_id(self) -> None:
        """The seam's writer signature stays caller-org-free. A regression
        that added ``organisation_id`` as a parameter (even with a
        ``None`` default) would let a request handler override the bound
        context — the exact T1 surface R2 forbids.
        """
        import inspect

        from oraclous_substrate import access

        sig = inspect.signature(access.scoped_write_node)
        assert "organisation_id" not in sig.parameters, (
            "oraclous_substrate.access.scoped_write_node must NOT accept "
            "an `organisation_id` parameter — the seam takes org from the "
            "bound governance context only (ADR-006 / ADR-012 §1b). The "
            "explicit-org writer the agent-identity migration composes "
            "lives in oraclous_substrate.migrations, never on the seam. "
            f"Current signature: {sig}"
        )

    def test_access_seam_neo4j_callable_takes_no_caller_supplied_organisation_id(
        self,
    ) -> None:
        """The seam's Neo4j-touching callables (those taking a ``driver``
        argument — the seam's writer/reader convention) must all source
        ``organisation_id`` from the bound context, never from a caller
        parameter. The agent-identity migration's explicit-org node-writer
        lives in ``oraclous_substrate.migrations``, never on this seam
        (security-architect R2).

        Restricted to ``driver``-taking callables on purpose: pure data
        containers (``AccessRequest``) and cache-key builders
        (``query_cache_key``) are re-exported through the access module
        but are not Neo4j surfaces; they legitimately accept
        ``organisation_id`` as a value-carrying argument.
        """
        import inspect

        from oraclous_substrate import access

        offenders: list[str] = []
        for name in dir(access):
            if name.startswith("_"):
                continue
            obj = getattr(access, name)
            if not callable(obj):
                continue
            try:
                sig = inspect.signature(obj)
            except (TypeError, ValueError):
                continue
            if "driver" not in sig.parameters:
                continue
            if "organisation_id" in sig.parameters:
                offenders.append(f"{name}{sig}")
        assert not offenders, (
            "oraclous_substrate.access exposes a Neo4j-touching callable "
            "that accepts `organisation_id` as a parameter — a T1 cross-"
            "org-write primitive at the request boundary (security-"
            f"architect R2 / ORA-36). Offenders: {offenders}. The new "
            "explicit-org writer the agent-identity migration composes "
            "MUST live in oraclous_substrate.migrations, never on the seam."
        )

    def test_substrate_migrations_namespace_is_where_the_explicit_org_writer_lives(
        self,
    ) -> None:
        """The new explicit-org Neo4j node-writer is housed in
        ``oraclous_substrate.migrations`` (sibling of ``org_backfill``).
        The orchestrator in auth-service composes it; the seam never
        sees it.

        Asserted constructively: at least one public callable in
        ``oraclous_substrate.migrations`` accepts an ``organisation_id``
        parameter — ``org_backfill`` already satisfies this for the
        existing reshape, and the new agent-identity helper joins the
        same namespace. The negative test above pins the seam-side
        invariant; this one anchors the positive side so the namespace
        cannot quietly drift to housing the writer somewhere illegal.
        """
        import inspect
        import pkgutil

        import oraclous_substrate.migrations as migrations_pkg

        found_explicit_org_writer = False
        for module_info in pkgutil.iter_modules(migrations_pkg.__path__):
            module = __import__(
                f"{migrations_pkg.__name__}.{module_info.name}",
                fromlist=["*"],
            )
            for name in dir(module):
                if name.startswith("_"):
                    continue
                obj = getattr(module, name)
                if not callable(obj):
                    continue
                try:
                    sig = inspect.signature(obj)
                except (TypeError, ValueError):
                    continue
                if "organisation_id" in sig.parameters:
                    found_explicit_org_writer = True
                    break
            if found_explicit_org_writer:
                break
        assert found_explicit_org_writer, (
            "oraclous_substrate.migrations must house at least one public "
            "callable that accepts `organisation_id` as a parameter — the "
            "context-free, explicit-org writer the agent-identity migration "
            "composes (security-architect R2). The seam is intentionally "
            "off-limits for this capability; the migrations namespace is "
            "its only home."
        )
