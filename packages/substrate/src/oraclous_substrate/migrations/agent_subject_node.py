"""Context-free explicit-org Neo4j writer for Agent ReBAC subject nodes (ORA-36 / R1-D1).

A migration is context-less by definition: there is no bound governance
``organisation_id`` to source from. Yet the substrate still has to write
``organisation_id`` onto every legacy ``(:Agent:__Platform__)`` node so the
C2 delegation traversal (ORA-35) can resolve it. This helper accepts the
``organisation_id`` as an explicit parameter — sibling to the
``org_backfill`` primitives that stamp an explicit ``SEED_ORGANISATION_ID``
— and stamps it idempotently via ``MERGE … ON CREATE/ON MATCH`` plus
``COALESCE`` (a node that already carries ``organisation_id`` keeps it on
re-run; a node missing it gets the value supplied here).

Per security-architect R2 (ORA-36 comment 10346): this writer lives in the
substrate ``migrations`` namespace, NOT on ``oraclous_substrate.access``. A
caller-chooses-org writer at the request-path access seam is a T1
cross-organisation-write primitive — the migration needs the capability,
the request path must never have it. The canonical ``organisation_id``
property name is single-sourced from ``oraclous_substrate.schema.neo4j``
(``ORG_PROPERTY``).

The Agent label literal (``Agent:__Platform__``) matches the ORA-35 ReBAC
engine's MERGE/MATCH patterns; ``Agent`` is deliberately NOT in
``oraclous_substrate.schema.neo4j.ORG_SCOPED_LABELS`` (the YAML registry
that ``org_backfill`` iterates over), so the legacy Agent corpus is
invisible to the ORA-24 org backfill — ORA-36 is the migration that brings
it under the org scope.
"""

from __future__ import annotations

from oraclous_substrate.schema.neo4j import ORG_PROPERTY

# The Agent ReBAC subject-node label, mirroring the ORA-35 engine's MERGE/MATCH
# pattern in ``oraclous_rebac.engine``. Treated as a trusted module constant —
# never request input — so safe to compose into Cypher.
_AGENT_LABEL = "Agent:__Platform__"


def _require(value: str, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")


def stamp_agent_subject_node(
    driver,
    *,
    agent_id: str,
    organisation_id: str,
) -> bool:
    """Idempotently ensure the ``(:Agent:__Platform__ {agent_id})`` node carries
    ``organisation_id``.

    Returns ``True`` if the node was created or ``organisation_id`` was newly
    set on this call, ``False`` if the node already carried it (no-op).

    Idempotency: a re-run for a node that already has ``organisation_id`` is
    a no-op — the ``COALESCE`` in ``ON MATCH SET`` preserves the existing
    value. This preserves the legacy-``org_id``-preservation invariant the
    test suite pins (a second run with a different ``organisation_id``
    argument keeps the original scope).
    """
    _require(agent_id, "agent_id")
    _require(organisation_id, "organisation_id")

    # Probe first so we can report whether this call newly stamped the node.
    # The MERGE below is what actually does the work; the probe only informs
    # the return value, not the state mutation, so a concurrent stamp between
    # probe and merge is harmless — it just under-counts in the summary.
    probe, _, _ = driver.execute_query(
        f"MATCH (a:{_AGENT_LABEL} {{agent_id: $agent_id}}) RETURN a.{ORG_PROPERTY} AS existing",
        agent_id=agent_id,
    )
    already_had_org = bool(probe) and probe[0]["existing"] is not None

    driver.execute_query(
        f"MERGE (a:{_AGENT_LABEL} {{agent_id: $agent_id}}) "
        f"ON CREATE SET a.{ORG_PROPERTY} = $org "
        f"ON MATCH SET a.{ORG_PROPERTY} = COALESCE(a.{ORG_PROPERTY}, $org)",
        agent_id=agent_id,
        org=organisation_id,
    )
    return not already_had_org


def unstamp_agent_subject_node(driver, *, agent_id: str) -> bool:
    """Remove ``organisation_id`` from the Agent subject node, preserving the node.

    Returns ``True`` if the property was removed, ``False`` if no Agent
    node with that ``agent_id`` exists or the property was already absent.

    The Agent node itself is **never** deleted — legacy nodes predate the
    migration; the rollback's contract is to revert the property the
    migration set, not to destroy the data the migration found.
    """
    _require(agent_id, "agent_id")
    records, _, _ = driver.execute_query(
        f"MATCH (a:{_AGENT_LABEL} {{agent_id: $agent_id}}) "
        f"WHERE a.{ORG_PROPERTY} IS NOT NULL "
        f"REMOVE a.{ORG_PROPERTY} "
        f"RETURN count(a) AS removed",
        agent_id=agent_id,
    )
    if not records:
        return False
    return int(records[0]["removed"]) > 0
