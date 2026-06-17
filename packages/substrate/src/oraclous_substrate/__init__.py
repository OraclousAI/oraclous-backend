"""oraclous-substrate — Layer 1 seams: ReBAC, provenance, usage metering."""

from __future__ import annotations

from oraclous_substrate.access import (
    CrossOrganisationDenied,
    authorise_cross_org_traversal,
    bind_organisation_guc,
    enforced_organisation_id,
    org_scoped_cypher,
    scoped_cache_get,
    scoped_cache_set,
    scoped_fulltext_search,
    scoped_pg_connection,
    scoped_traverse,
    scoped_write_node,
)
from oraclous_substrate.access_async import (
    RlsBypassingRoleError,
    assert_non_bypassing_role,
    bind_org_guc_async,
    build_rls_engine,
    install_org_guc_guard,
    org_scope,
    provision_app_role,
    provision_app_role_ddl,
)
from oraclous_substrate.aggregation import (
    ORG_ADMIN_RELATION,
    UsageAggregate,
    UsageAggregationDenied,
    UsageAggregator,
)
from oraclous_substrate.metering import (
    CAPABILITY_INVOCATION,
    CROSS_WORKSPACE_TRAVERSAL,
    MODEL_TOKENS,
    STORAGE_WRITE,
    MeteringHook,
    PendingUsageEvent,
    UsageReplayLog,
)
from oraclous_substrate.provenance import (
    ProvenanceCollector,
    ProvenanceRecord,
    ProvenanceSink,
)
from oraclous_substrate.rebac import (
    AccessDecision,
    AccessDecisionClient,
    AccessRequest,
    RelationResolver,
)
from oraclous_substrate.usage import (
    UsageEvent,
    UsageEventStore,
    UsageEventStream,
)

__all__ = [
    "CAPABILITY_INVOCATION",
    "CROSS_WORKSPACE_TRAVERSAL",
    "MODEL_TOKENS",
    "ORG_ADMIN_RELATION",
    "STORAGE_WRITE",
    "AccessDecision",
    "AccessDecisionClient",
    "AccessRequest",
    "CrossOrganisationDenied",
    "MeteringHook",
    "RlsBypassingRoleError",
    "PendingUsageEvent",
    "ProvenanceCollector",
    "ProvenanceRecord",
    "ProvenanceSink",
    "RelationResolver",
    "UsageAggregate",
    "UsageAggregationDenied",
    "UsageAggregator",
    "UsageEvent",
    "UsageEventStore",
    "UsageEventStream",
    "UsageReplayLog",
    "assert_non_bypassing_role",
    "authorise_cross_org_traversal",
    "bind_org_guc_async",
    "bind_organisation_guc",
    "build_rls_engine",
    "enforced_organisation_id",
    "install_org_guc_guard",
    "org_scope",
    "org_scoped_cypher",
    "provision_app_role",
    "provision_app_role_ddl",
    "scoped_cache_get",
    "scoped_cache_set",
    "scoped_fulltext_search",
    "scoped_pg_connection",
    "scoped_traverse",
    "scoped_write_node",
]
