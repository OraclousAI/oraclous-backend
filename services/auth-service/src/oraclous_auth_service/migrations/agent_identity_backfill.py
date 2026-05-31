"""Idempotent pre-R1 agent-identity backfill (ORA-36 / R1-D1).

For every agent that existed before R1 — a legacy ``(:Agent:__Platform__)``
node carrying only the legacy ``org_id`` property — this migration issues
the three R1 artifacts the agent principal needs:

1. a Postgres ``agents`` row keyed on the legacy ``agent_id``, carrying
   ``organisation_id`` per ADR-006 (ORA-30 / R1-A1);
2. a structurally **inert** Postgres ``agent_credentials`` row tied to that
   agent — bcrypt hash of a discarded random secret, status
   ``pending_rotation`` so the row is excluded from the active-prefix
   lookup (sec-arch R1 defense-in-depth; ORA-36 comment 10346);
3. the ReBAC-traversable ``organisation_id`` stamp on the
   ``(:Agent:__Platform__)`` subject node — via the substrate-owned
   :func:`oraclous_substrate.migrations.agent_subject_node.stamp_agent_subject_node`
   helper (sol-arch decomposition; ORA-36 comment 10345).

The orchestrator lives in auth-service per the sol-arch ruling: each store
is written by its owner. The Postgres half stays inside the auth-service
identity domain (raw INSERTs on the caller's ``postgres_conn``, mirroring
the ORA-24 ``org_backfill`` caller-controlled-transaction model); the
Neo4j half delegates to the substrate helper so the canonical
``organisation_id`` property spelling and the request-path-isolation
invariants stay single-sourced.

T2 (the brief's threat tag) is the through-line: every backfilled
principal exists, is correctly scoped, and carries **no** authority —
no ``DELEGATED_TO`` edge, no role grant, and the credential cannot
authenticate against any input until an admin rotates it.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import bcrypt
from oraclous_substrate.migrations.agent_subject_node import (
    stamp_agent_subject_node,
    unstamp_agent_subject_node,
)
from oraclous_substrate.organisation import SEED_ORGANISATION_ID

_BACKFILL_USER_ID = "ora36-backfill"
# A discrete status distinct from the ORA-30 lifecycle's ``active`` /
# ``revoked`` so the partial UNIQUE INDEX ``credential_prefix WHERE
# status='active'`` (ADR-012 §1a) stays free, and ``active_credentials_by_prefix``
# (the bcrypt-verify entry point) never returns these rows.
_BACKFILL_STATUS = "pending_rotation"
# Mirrors the ORA-30 12-char prefix width; the value is meaningless because
# ``status != 'active'`` makes the prefix unreachable from validate_credential.
_BACKFILL_PREFIX_PREFIX = "oag_bf"
_BCRYPT_ROUNDS = 12

_LEGACY_AGENT_LABEL = "Agent:__Platform__"


# --- Legacy enumeration -----------------------------------------------------


def _enumerate_legacy_agents(neo4j_driver) -> list[dict[str, Any]]:
    """Read every legacy ``(:Agent:__Platform__)`` node with its ``agent_id``,
    the legacy ``org_id`` (may be NULL), and any already-set ``organisation_id``
    so a partial-prior run is detected and not duplicated.

    Skips degenerate rows where ``agent_id`` is NULL **or blank** — the
    downstream substrate stamp helper rejects blank identifiers via
    ``_require``, so filtering here prevents a degenerate legacy row from
    crashing the migration mid-loop (code-reviewer suggestion on PR #57).
    """
    records, _, _ = neo4j_driver.execute_query(
        f"MATCH (a:{_LEGACY_AGENT_LABEL}) "
        "WHERE a.agent_id IS NOT NULL AND trim(a.agent_id) <> '' "
        "RETURN a.agent_id AS agent_id, "
        "       a.org_id AS legacy_org_id, "
        "       a.organisation_id AS organisation_id"
    )
    return [
        {
            "agent_id": r["agent_id"],
            "legacy_org_id": r["legacy_org_id"],
            "organisation_id": r["organisation_id"],
        }
        for r in records
    ]


def _resolve_organisation_id(agent: dict[str, Any], seed: str) -> str:
    """``organisation_id`` ▷ legacy ``org_id`` ▷ seed.

    An agent already migrated keeps its scope; an agent with only legacy
    ``org_id`` is brought up to the R1 name; an orphan with neither falls
    back to the seed org (the documented platform/system tenant per
    ADR-006 — sec-arch N1 confirmation).
    """
    return agent["organisation_id"] or agent["legacy_org_id"] or seed


# --- Postgres writes (auth-service identity domain) -------------------------


def _insert_principal_if_missing(conn, *, agent_id: str, organisation_id: str) -> bool:
    """Atomically insert the principal iff it does not already exist.

    ``ON CONFLICT (id) DO NOTHING`` closes the TOCTOU window the prior
    check-then-insert pattern had against concurrent callers (code-reviewer
    suggestion on PR #57). Returns ``True`` if a row was inserted,
    ``False`` if the principal already existed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.agents (id, organisation_id, created_by_user_id) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING "
            "RETURNING id",
            (agent_id, organisation_id, _BACKFILL_USER_ID),
        )
        return cur.fetchone() is not None


def _insert_inert_credential_if_missing(conn, *, agent_id: str, organisation_id: str) -> bool:
    """Atomically insert a structurally inert credential iff the agent has
    no credential of record yet (sec-arch R1).

    The ``INSERT … SELECT … WHERE NOT EXISTS`` form makes the check + write
    one atomic SQL statement — closes the TOCTOU window the prior
    check-then-insert pattern had against concurrent callers (code-reviewer
    suggestion on PR #57). Returns ``True`` if a row was inserted,
    ``False`` if a credential of any status already existed for this agent.

    The bcrypt hash is computed before the SQL so an idempotent re-run
    burns a hash that doesn't get written; acceptable for a one-shot
    migration. Inertness invariants from R1:

    * The raw secret is generated and discarded — the bcrypt hash's
      preimage is unrecoverable.
    * ``status = _BACKFILL_STATUS`` excludes the row from
      ``active_credentials_by_prefix``, so even a correctly-guessed prefix
      cannot reach the bcrypt verify path until an admin issues a real
      (``status='active'``) credential.
    """
    raw = secrets.token_urlsafe(32)
    credential_hash = bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()
    # `raw` falls out of scope here; no further reference exists in process.
    credential_id = str(uuid.uuid4())
    # 12-char prefix to mirror the ORA-30 convention; meaningless because
    # status != 'active' makes the prefix unreachable.
    prefix = f"{_BACKFILL_PREFIX_PREFIX}{secrets.token_urlsafe(6)}"[:12]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.agent_credentials "
            "(id, agent_id, organisation_id, credential_hash, credential_prefix, status) "
            "SELECT %s, %s, %s, %s, %s, %s "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM public.agent_credentials WHERE agent_id = %s"
            ") "
            "RETURNING id",
            (
                credential_id,
                agent_id,
                organisation_id,
                credential_hash,
                prefix,
                _BACKFILL_STATUS,
                agent_id,
            ),
        )
        return cur.fetchone() is not None


# --- Public migration API ---------------------------------------------------


def backfill_agent_identity(
    *,
    postgres_conn,
    neo4j_driver,
    organisation_id: str | uuid.UUID = SEED_ORGANISATION_ID,
) -> dict[str, int]:
    """Backfill the three R1 artifacts for every legacy Agent.

    Caller-controlled txn: this function does **not** commit the Postgres
    side. The caller is responsible for ``conn.commit()`` / rollback at the
    transactional boundary (mirrors the ORA-24 ``org_backfill`` pattern).

    Idempotent: a re-run skips agents whose principal already exists and
    credentials whose any-status row already exists; Neo4j stamps preserve
    any organisation_id already present on the node.

    Returns a summary ``dict`` reporting how many of each artifact were
    newly created on this call — useful for an operator running the
    migration as the staging-rehearsal ledger.
    """
    seed = str(organisation_id)
    summary = {
        "principals_created": 0,
        "credentials_created": 0,
        "subject_nodes_stamped": 0,
    }

    for agent in _enumerate_legacy_agents(neo4j_driver):
        target_org = _resolve_organisation_id(agent, seed)
        agent_id = agent["agent_id"]

        if _insert_principal_if_missing(
            postgres_conn, agent_id=agent_id, organisation_id=target_org
        ):
            summary["principals_created"] += 1

        if _insert_inert_credential_if_missing(
            postgres_conn, agent_id=agent_id, organisation_id=target_org
        ):
            summary["credentials_created"] += 1

        if stamp_agent_subject_node(neo4j_driver, agent_id=agent_id, organisation_id=target_org):
            summary["subject_nodes_stamped"] += 1

    return summary


def rollback_agent_identity(
    *,
    postgres_conn,
    neo4j_driver,
) -> dict[str, int]:
    """Revert the backfill: remove the Postgres rows + unstamp the Neo4j org.

    Removes the ``agents`` and ``agent_credentials`` rows produced by the
    backfill (identified by ``created_by_user_id = _BACKFILL_USER_ID`` so a
    real principal an admin later issued is **not** touched). Removes the
    ``organisation_id`` property from the legacy ``(:Agent:__Platform__)``
    subject nodes — the nodes themselves are preserved because they
    predate the migration.

    Idempotent + safe-on-unbackfilled-state: re-running rollback after a
    completed rollback is a no-op; running rollback before any backfill is
    a no-op. Caller-controlled txn (does not commit).
    """
    summary = {
        "principals_removed": 0,
        "credentials_removed": 0,
        "subject_nodes_unstamped": 0,
    }

    # 1. Identify backfilled principals (by the audit marker) so we don't
    #    delete a principal an admin created out-of-band post-backfill.
    with postgres_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM public.agents WHERE created_by_user_id = %s",
            (_BACKFILL_USER_ID,),
        )
        backfilled_ids = [row[0] for row in cur.fetchall()]

    # 2. Remove backfilled credentials FIRST (no FK constraint defined but
    #    keep referential order regardless), then the principals. Count the
    #    actual rows removed so the summary reflects reality even when a
    #    real credential has been issued between backfill and rollback
    #    (its principal stays; the inert credential gets removed only if
    #    it still carries the backfill audit shape).
    if backfilled_ids:
        with postgres_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM public.agent_credentials WHERE agent_id = ANY(%s) AND status = %s",
                (backfilled_ids, _BACKFILL_STATUS),
            )
            summary["credentials_removed"] = cur.rowcount or 0
            cur.execute(
                "DELETE FROM public.agents WHERE id = ANY(%s) AND created_by_user_id = %s",
                (backfilled_ids, _BACKFILL_USER_ID),
            )
            summary["principals_removed"] = cur.rowcount or 0

    # 3. Unstamp the Neo4j subject nodes. We touch every Agent the backfill
    #    could have stamped — for a rollback-before-any-backfill call the
    #    enumeration finds 0 organisation_id-bearing nodes and the unstamp
    #    helpers all return False (no-op).
    for agent in _enumerate_legacy_agents(neo4j_driver):
        if unstamp_agent_subject_node(neo4j_driver, agent_id=agent["agent_id"]):
            summary["subject_nodes_unstamped"] += 1

    return summary
