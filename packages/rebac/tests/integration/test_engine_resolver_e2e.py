"""End-to-end tests: the ReBAC engine resolver adapter, wired through the
substrate seam, against a real Neo4j ("End-to-end through
AccessDecisionClient + real adapter").

These assert what the unit suite cannot — that the *real* ``ReBACEngine``
actually returns True when an org-scoped CAN_ACCESS edge exists, returns
False when it does not, and returns 0 (deny) on a cross-org check. The
engine's full traversal (Phase B HAS_ROLE → Phase A CAN_ACCESS fallback) is
exercised behaviourally: tests seed Phase A edges directly, because the
Phase B HAS_ROLE traversal needs system-Permission nodes the engine does not
seed itself (that seeding is intentionally out of the engine's scope and lands
with the first real consumer).

Markers: ``integration`` (real substrate via testcontainers), ``rebac``,
``organization_isolation`` (the cross-org test pins ADR-006's tenant-loop
invariant at the data layer).

RED until ``backend-implementer`` creates ``oraclous_rebac.ReBACEngineResolver``.
"""

from __future__ import annotations

import pytest
from oraclous_rebac import ReBACEngine
from oraclous_substrate.rebac import AccessDecisionClient, AccessRequest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rebac,
    pytest.mark.organization_isolation,
]


_ORG_A = "org-aaaa"
_ORG_B = "org-bbbb"
_USER = "user-alice"
_GRAPH = "graph-roadmap"

# Phase A seed: the engine's ``_PHASE_A_QUERY`` matches a User in the
# ``__system__`` namespace, a Graph in the ``__system__`` namespace, and a
# CAN_ACCESS edge whose ``level`` is in the acceptable hierarchy AND whose
# ``organisation_id`` equals the requested org. We seed exactly that, so a
# matching org_id allows; a mismatched org_id falls off the WHERE clause and
# returns 0 (deny) — the tenant-loop invariant from ADR-006.
_SEED_PHASE_A = """
MERGE (u:User:__Platform__ {user_id: $user_id, graph_id: "__system__"})
MERGE (g:Graph:__Rebac__ {graph_id: $graph_id, namespace: "__system__"})
MERGE (u)-[r:CAN_ACCESS]->(g)
SET r.level = $level, r.organisation_id = $organisation_id
"""


async def _seed_can_access(
    driver,
    *,
    organisation_id: str,
    user_id: str = _USER,
    graph_id: str = _GRAPH,
    level: str = "read",
) -> None:
    async with driver.session() as session:
        await session.run(
            _SEED_PHASE_A,
            {
                "user_id": user_id,
                "graph_id": graph_id,
                "level": level,
                "organisation_id": organisation_id,
            },
        )


def _make_client(driver, null_async_redis) -> AccessDecisionClient:
    """Wire the real engine through the not-yet-built adapter into the seam.

    Import is function-local so the module collects cleanly before
    ``[impl]`` lands (TST001 / TDD-window collection guardrail).
    """
    from oraclous_rebac import ReBACEngineResolver  # noqa: PLC0415

    engine = ReBACEngine(redis=null_async_redis)
    resolver = ReBACEngineResolver(
        permission_check=engine.check_graph_permission,
        driver=driver,
    )
    return AccessDecisionClient(resolver=resolver)


# ── Allow on a granted org-scoped relation ──────────────────────────────────


async def test_allow_for_granted_org_scoped_relation(
    rebac_async_driver,
    null_async_redis,
) -> None:
    """A user with an org-scoped CAN_ACCESS edge of acceptable level → ALLOW
    through the full ``AccessDecisionClient`` → adapter → engine → Neo4j path.
    """
    await _seed_can_access(rebac_async_driver, organisation_id=_ORG_A, level="read")
    client = _make_client(rebac_async_driver, null_async_redis)

    decision = await client.check(
        AccessRequest(
            organisation_id=_ORG_A,
            subject=_USER,
            resource=_GRAPH,
            relation="read",
        )
    )

    assert decision.allowed is True


# ── Deny on an absent relation ──────────────────────────────────────────────


async def test_deny_when_no_relation_exists(
    rebac_async_driver,
    null_async_redis,
) -> None:
    """No edge for (org_A, user, graph) → DENY with the *absent* reason — not
    the error reason. The graph is empty (the conftest wipes it per test).
    """
    client = _make_client(rebac_async_driver, null_async_redis)

    decision = await client.check(
        AccessRequest(
            organisation_id=_ORG_A,
            subject=_USER,
            resource=_GRAPH,
            relation="read",
        )
    )

    assert decision.allowed is False
    # The absent / ambiguous reason — distinct from the error reason. If a
    # Neo4j outage masquerades as "absent" the next test catches it.
    assert "absent" in decision.reason or "ambiguous" in decision.reason


# ── Cross-org returns 0 (the tenant-loop invariant) ─────────────────────────


async def test_cross_organisation_check_returns_zero(
    rebac_async_driver,
    null_async_redis,
) -> None:
    """ADR-006: an identical (user, graph) under a different organisation
    must resolve to deny — even though the CAN_ACCESS edge exists, it carries
    a different ``organisation_id`` so the Phase A query's WHERE clause drops
    it. This is the cross-org-returns-0 invariant the engine data-layer test
    proves at the Cypher level; this suite proves it end-to-end through the
    adapter + seam.
    """
    # Seeded only for ORG_A.
    await _seed_can_access(rebac_async_driver, organisation_id=_ORG_A, level="read")
    client = _make_client(rebac_async_driver, null_async_redis)

    decision = await client.check(
        AccessRequest(
            organisation_id=_ORG_B,  # the *other* org
            subject=_USER,
            resource=_GRAPH,
            relation="read",
        )
    )

    assert decision.allowed is False
    assert "absent" in decision.reason or "ambiguous" in decision.reason


# ── On the "deny+reason on engine error" e2e AC bullet ─────────────────────
#
# The AC's "deny+reason on engine error" end-to-end is *not* asserted here,
# deliberately. The current ``ReBACEngine.check_graph_permission``
# swallows Neo4j exceptions internally and returns ``False`` — by
# design (it self-denies fail-closed). The adapter's no-pre-collapse contract
# is pinned at unit level (``test_engine_error_propagates_not_collapsed_to_false``
# and ``test_seam_end_to_end_allow_definitive_deny_error_deny_reasons_distinguishable``
# in the unit suite), where a stub permission check is forced to raise.
#
# Flagging this as a contract gap for be-test-reviewer / coordinator
# solution-architect: if the SA ruling on fail-closed authority placement is
# "seam-owns-fail-closed", the engine should be refactored to surface
# exceptions (a small engine follow-up), at which point a real-engine e2e
# error test becomes writable — drop a closed driver in, observe the error
# reason at the seam. Until then, asserting it end-to-end here would pin
# behaviour the engine does not currently produce.


# ── Phase B (HAS_ROLE) — the REAL grant flow, against real Neo4j (#456) ─────
#
# Every test above seeds CAN_ACCESS directly (Phase A). The role-based path
# bootstrap_graph_roles -> grant_role -> check (Phase B HAS_ROLE -> Role ->
# Permission) was NEVER exercised end-to-end (the substrate federation test
# mocks the client) — which hid a bug: the global :Permission:__System__ nodes
# were never seeded, so _BOOTSTRAP_PERM_EDGE_QUERY's MATCH found nothing, no
# HAS_PERMISSION edge was wired, and every role-based grant failed closed.
# This drives the REAL chain (client.check -> resolver -> engine, Phase B) so
# that bug cannot regress.
import uuid  # noqa: E402


async def test_grant_role_authorizes_a_read_through_the_real_phase_b_chain(
    rebac_async_driver, null_async_redis
) -> None:
    engine = ReBACEngine(redis=null_async_redis)
    org = str(uuid.uuid4())
    graph = "graph-" + str(uuid.uuid4())
    granted = "user-" + str(uuid.uuid4())
    other = "user-" + str(uuid.uuid4())
    owner = "user-" + str(uuid.uuid4())
    client = _make_client(rebac_async_driver, null_async_redis)

    def _req(subject: str) -> AccessRequest:
        return AccessRequest(organisation_id=org, subject=subject, resource=graph, relation="read")

    # deny BEFORE any grant (no role, no permission)
    assert (await client.check(_req(granted))).allowed is False

    # seed the system roles+permissions for (graph, org), then grant viewer
    await engine.bootstrap_graph_roles(
        rebac_async_driver, organisation_id=org, graph_id=graph, owner_user_id=owner
    )
    await engine.grant_role(
        rebac_async_driver,
        organisation_id=org,
        graph_id=graph,
        target_user_id=granted,
        role_name="viewer",
        granted_by=owner,
    )

    # ALLOW after grant (viewer -> graph:read satisfies a read), and an ungranted
    # user is still denied (the grant is subject-scoped, not graph-wide).
    assert (await client.check(_req(granted))).allowed is True
    assert (await client.check(_req(other))).allowed is False
