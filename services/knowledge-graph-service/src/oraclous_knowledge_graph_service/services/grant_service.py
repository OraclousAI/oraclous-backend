"""Cross-organisation graph grants (services layer) — the ReBAC GATE (ADR-004, #446).

A graph's owner shares a READ on it with another organisation's user. RLS stays the wall (this
writes NO data and does not widen any row-read predicate); it records a ReBAC ``HAS_ROLE`` relation
so the retriever may later ADMIT the granted graph into a federated scope. The relation is keyed by
the GRANTEE organisation (``check_graph_permission`` scopes by the caller's org), and the ReBAC
vocabulary is prefixed (``user-<id>`` / ``graph-<id>``).
"""

from __future__ import annotations

import uuid

from oraclous_rebac import ReBACEngine
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_graph_service.services.auth_client import AuthMembershipPort
from oraclous_knowledge_graph_service.services.graph_service import GraphService

# The async Neo4j driver is opaque to this service layer (§21: DB drivers live in repositories). It
# is only forwarded to the ReBAC engine — the ReBAC data-access layer — so we keep it untyped here.


class GrantUnavailable(Exception):
    """The ReBAC store (async Neo4j) is unavailable — the grant cannot be recorded (→ 503)."""


class GranteeNotInOrg(Exception):
    """#464: the grantee user is NOT a member of the grantee organisation — reject (→ 422). An
    unverified grantee-org would leave a spoofable value on the audit/grant edge."""


class GraphGrantService:
    def __init__(
        self,
        *,
        graphs: GraphService,
        engine: ReBACEngine,
        async_driver: object | None,
        auth: AuthMembershipPort | None = None,
    ) -> None:
        self._graphs = graphs
        self._engine = engine
        self._driver = async_driver
        self._auth = auth

    async def grant_read(
        self,
        *,
        graph_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        grantee_organisation_id: uuid.UUID,
        grantee_user_id: uuid.UUID,
    ) -> None:
        if self._driver is None:
            raise GrantUnavailable("ReBAC store unavailable")
        # Owner gate: the caller must own the graph in their bound org (else 404, no leak).
        await self._graphs.assert_owned(graph_id=graph_id, user_id=owner_user_id)

        # #464: the grantee user MUST belong to the grantee org before we key the cross-org edge by
        # it. FAIL-CLOSED — ``verify_membership`` raises AuthServiceUnavailable on error (→ 503),
        # so a cross-org write is never granted un-validated; a genuine non-member → GranteeNotInOrg
        # (422). Skipped only when no auth client is wired (an unconfigured deploy).
        if self._auth is not None and not await self._auth.verify_membership(
            organisation_id=str(grantee_organisation_id), user_id=str(grantee_user_id)
        ):
            raise GranteeNotInOrg("grantee user is not a member of the grantee organisation")

        gid = f"graph-{graph_id}"
        owner = f"user-{owner_user_id}"
        org = str(grantee_organisation_id)
        # ADR-036 / Contract G2: the OWNER org — the org that owns this graph — is the owner's bound
        # org, which `assert_owned` just verified. Server-derived here (NEVER from a request body);
        # recorded on the grant edge so federation can bind it to read the owner's rows.
        owner_org = enforced_organisation_id()
        # Seed the system roles for (graph, GRANTEE org) so grant_role can MATCH the viewer role,
        # then grant the grantee user a read-level (viewer) role under the grantee org.
        await self._engine.bootstrap_graph_roles(
            self._driver, organisation_id=org, graph_id=gid, owner_user_id=owner
        )
        await self._engine.grant_role(
            self._driver,
            organisation_id=org,
            graph_id=gid,
            target_user_id=f"user-{grantee_user_id}",
            role_name="viewer",
            granted_by=owner,
            owner_organisation_id=owner_org,
        )
        try:
            await self._engine.invalidate_permission_cache(
                organisation_id=org, user_id=f"user-{grantee_user_id}", graph_id=gid
            )
        except Exception:  # noqa: BLE001, S110 — cache invalidation is best-effort; relation written
            pass
