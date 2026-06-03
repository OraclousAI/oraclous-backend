// Neo4j write-role initialisation for knowledge-graph-service (ORAA-53).
//
// Creates a dedicated `kgs_writer` user with the `publisher` role (read +
// write + schema-element creation; no admin/RBAC capabilities).  Principle of
// least privilege: the knowledge-graph-service (write path) connects as
// `kgs_writer`, not as the Neo4j admin, limiting blast radius if the KGS
// credential is compromised (Threat T6).
//
// The `publisher` native role grants:
//   - ACCESS, MATCH, CREATE, MERGE, SET, DELETE on any user database
//   - CREATE new labels / relationship types / property keys (needed for
//     index creation via oraclous_substrate.schema.neo4j.apply())
//   - No ADMIN / role-management / user-management capabilities
//
// This script is idempotent: `CREATE USER … IF NOT EXISTS` and
// `GRANT ROLE … TO` are safe to re-run on an already-initialised database.
//
// The password here is the dev-only default.  Production deployments inject
// KGS_NEO4J_PASSWORD via K8s secrets; see deploy/README.md § Neo4j write role.

CREATE USER kgs_writer IF NOT EXISTS
  SET PASSWORD $kgs_writer_password
  CHANGE NOT REQUIRED;

GRANT ROLE publisher TO kgs_writer;
