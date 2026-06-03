// Neo4j read-role initialisation for knowledge-retriever-service (ORAA-58).
//
// Creates a dedicated `krs_reader` user with the `reader` role (read-only;
// no write, schema-change, or admin capabilities).  Principle of least
// privilege: the knowledge-retriever-service (read path) connects as
// `krs_reader`, not as the Neo4j admin or as `kgs_writer`, limiting blast
// radius if the KRS credential is compromised (Threat T6).
//
// The `reader` native role grants:
//   - ACCESS, MATCH on any user database
//   - No CREATE / MERGE / SET / DELETE / REMOVE capabilities
//   - No schema-element creation (labels, relationship types, property keys)
//   - No ADMIN / role-management / user-management capabilities
//
// This script is idempotent: `CREATE USER … IF NOT EXISTS` and
// `GRANT ROLE … TO` are safe to re-run on an already-initialised database.
//
// The password here is the dev-only default.  Production deployments inject
// KRS_NEO4J_PASSWORD via K8s secrets; see deploy/README.md § Neo4j roles.

CREATE USER krs_reader IF NOT EXISTS
  SET PASSWORD $krs_reader_password
  CHANGE NOT REQUIRED;

GRANT ROLE reader TO krs_reader;
