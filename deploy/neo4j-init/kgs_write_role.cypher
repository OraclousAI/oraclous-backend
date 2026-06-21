// Neo4j write-role initialisation for knowledge-graph-service.
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
// Execution — supply the password via cypher-shell's --param flag:
//
//   cypher-shell -a bolt://<host>:7687 -u neo4j -p <admin-password> \
//     --param 'kgs_writer_password => "<strong-password>"' \
//     -f deploy/neo4j-init/kgs_write_role.cypher
//
// Local dev: docker-compose provisions roles with a hardcoded dev password via
// the `neo4j-role-setup` service; this file is not used there.
//
// Production: the future Helm neo4jRoleInit Job will mount this file and inject
// the password from a K8s Secret via --param.  See deploy/README.md § Production.

CREATE USER kgs_writer IF NOT EXISTS
  SET PASSWORD $kgs_writer_password
  CHANGE NOT REQUIRED;

GRANT ROLE publisher TO kgs_writer;
