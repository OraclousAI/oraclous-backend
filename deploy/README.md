# deploy/

Deployment scaffolding for oraclous-backend.

| Path | Purpose |
| --- | --- |
| `docker-compose.yml` | Local self-hosted **substrate stack**: Neo4j, Postgres, Redis, Jaeger. App-service containers are added per release. |
| `docker-compose.agent.yml` | Concurrency override — run multiple isolated stacks on one host via `COMPOSE_PROJECT_NAME` + OS-assigned ports. |
| `docker-compose.fe-target.yml` | Fixed-port overlay for the long-lived shared **fe-target** stack used by frontend-implementer. |
| `helm/` | Cloud-hosted chart **skeleton**; service templates added per release (R1–R6). |
| `observability/` | Tracing config (Jaeger + an OpenTelemetry Collector scaffold). |

## Local stack

```bash
docker compose -f deploy/docker-compose.yml up -d      # start substrate
docker compose -f deploy/docker-compose.yml ps         # check health
docker compose -f deploy/docker-compose.yml down       # stop (add -v to drop volumes)
```

Dev defaults: Neo4j `neo4j/password` (bolt :7687, http :7474), Postgres `oraclous/oraclous` db `oraclous` (:5432), Redis (:6379), Jaeger UI (:16686). **Dev-only credentials.**

## Concurrent stacks (parallel agents / tickets)

Each workstream gets an isolated stack — no port or data collisions.

### Automated helpers (recommended)

```bash
./scripts/stack-up.sh ora-42     # boots stack, discovers ports, writes .stack-env
. .stack-env                     # load port vars into your shell
# ... run tests / app ...
./scripts/stack-down.sh ora-42   # tear down stack, remove .stack-env entry
```

`stack-up.sh` sets `COMPOSE_PROJECT_NAME=oraclous-<ticket>`, brings up both compose files with OS-assigned ports, discovers all 7 host ports, and writes `.stack-env` to the repo root. `stack-down.sh` accepts the ticket as an argument or reads `$STACK_TICKET` from the environment.

### Manual alternative

```bash
export COMPOSE_PROJECT_NAME=oraclous-ora-14
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.agent.yml up -d
docker compose -p oraclous-ora-14 port neo4j 7687      # find the assigned host port
docker compose -p oraclous-ora-14 down -v              # tear down when the ticket is done
```

### Port and stack registry

`stack-up.sh` writes discovered ports to two places:

- **`.stack-env`** — sourceable shell file with `NEO4J_BOLT_PORT`, `POSTGRES_PORT`, etc. Gitignored; overwritten each time `stack-up.sh` runs.
- **`.stack-registry.json`** — JSON map of all running stacks keyed by project name, recording ticket, branch, ports, and `started_at`. Gitignored; inspect it to see what is currently running.

```json
{
  "oraclous-ora-42": {
    "ticket": "ora-42",
    "branch": "backend-implementer/ora-42-something",
    "ports": { "neo4j_bolt": 32768, "postgres": 32770, ... },
    "started_at": "2026-06-01T10:00:00Z"
  }
}
```

## fe-target stack (frontend-implementer)

The **fe-target** stack is a long-lived shared backend stack with fixed host ports that frontend-implementer always connects to. It is **not** managed by `stack-up`/`stack-down`.

**Who uses it:** frontend-implementer only. Backend agents use per-ticket ephemeral stacks.

**Bring up / tear down manually:**

```bash
COMPOSE_PROJECT_NAME=oraclous-fe-target \
  docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.fe-target.yml up -d

COMPOSE_PROJECT_NAME=oraclous-fe-target \
  docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.fe-target.yml down -v
```

**Env file:** `.stack-env.fe-target` is checked into git (ports are stable). Source it before running the frontend:

```bash
. .stack-env.fe-target
```

**Fixed ports:**

| Service | Host port |
| --- | --- |
| Neo4j bolt | 17687 |
| Neo4j http | 17474 |
| Postgres | 15432 |
| Redis | 16379 |
| Jaeger UI | 26686 |
| OTLP gRPC | 14317 |
| OTLP HTTP | 14318 |

Contact the CTO if the fe-target stack is down.

## Neo4j roles (ORAA-53 / ORAA-58)

The knowledge-graph-service (write path) connects to Neo4j as a dedicated
`kgs_writer` user with the `publisher` role (read + write + schema-element
creation).  The knowledge-retriever-service (read path) connects as `krs_reader`
with the `reader` role.  Neither service uses the `neo4j` admin account.  This
follows principle of least privilege (Threat T6): a compromised KGS or KRS
credential does not grant admin access to Neo4j.

### Role summary

| User | Neo4j role | Capabilities | Service |
|---|---|---|---|
| `neo4j` | `admin` | Full admin | dev/ops only |
| `kgs_writer` | `publisher` | Read + write + CREATE INDEX/CONSTRAINT | knowledge-graph-service |
| `krs_reader` | `reader` | Read-only | knowledge-retriever-service |

### Local development

The `neo4j-role-setup` service in `docker-compose.yml` creates both users
automatically when the substrate stack starts:

```bash
docker compose -f deploy/docker-compose.yml up -d
# neo4j-role-setup runs after neo4j is healthy and creates kgs_writer + krs_reader
```

Dev defaults (never used in production):
- `kgs_writer` / `kgs-writer-pass`
- `krs_reader` / `krs-reader-pass`

The knowledge-graph-service reads these env vars:

| Env var | Dev value | Description |
|---|---|---|
| `KGS_NEO4J_URI` | `bolt://neo4j:7687` | Bolt URI for the KGS Neo4j connection |
| `KGS_NEO4J_USER` | `kgs_writer` | Write-capable Neo4j user |
| `KGS_NEO4J_PASSWORD` | `kgs-writer-pass` | Dev-only; K8s secret in production |

The knowledge-retriever-service reads these env vars:

| Env var | Dev value | Description |
|---|---|---|
| `KRS_NEO4J_URI` | `bolt://neo4j:7687` | Bolt URI for the KRS Neo4j connection |
| `KRS_NEO4J_USER` | `krs_reader` | Read-only Neo4j user |
| `KRS_NEO4J_PASSWORD` | `krs-reader-pass` | Dev-only; K8s secret in production |

### Production (Kubernetes / Helm)

1. Create a K8s Secret containing the `kgs_writer` password:
   ```bash
   kubectl create secret generic neo4j-kgs-writer \
     --from-literal=password=<strong-password>
   ```

2. Create a K8s Secret containing the `krs_reader` password:
   ```bash
   kubectl create secret generic neo4j-krs-reader \
     --from-literal=password=<strong-password>
   ```

3. Create a K8s Secret for the Neo4j admin password (used by the init Job):
   ```bash
   kubectl create secret generic neo4j-admin \
     --from-literal=password=<admin-password>
   ```

4. Set Helm values (see `deploy/helm/values.yaml` `neo4jRoles.kgsWriter`):
   ```yaml
   neo4jRoles:
     kgsWriter:
       uri: bolt://<neo4j-host>:7687
       user: kgs_writer
       secretName: neo4j-kgs-writer
       secretKey: password
   neo4jRoleInit:
     enabled: true
     adminSecretName: neo4j-admin
   ```

5. **The `neo4jRoleInit` block in `values.yaml` is scaffolding for a future Helm Job
   template** (`deploy/helm/templates/neo4j-role-init-job.yaml`). That template does not
   exist yet — `deploy/helm/templates/` currently contains only `NOTES.txt` and
   `_helpers.tpl`. Setting `neo4jRoleInit.enabled: true` and running `helm upgrade` has
   no effect until the Job template is added in a future release.

   Until then, provision roles manually from a pod that can reach the Neo4j bolt port:
   ```bash
   # KGS write role
   kubectl run neo4j-init --rm -it --image=neo4j:5.23-community --restart=Never -- \
     cypher-shell -a bolt://<neo4j-host>:7687 -u neo4j -p <admin-password> \
       --param 'kgs_writer_password => "<strong-password>"' \
       -f /dev/stdin < deploy/neo4j-init/kgs_write_role.cypher

   # KRS read role — $krs_reader_password is a required parameter, no baked-in default
   kubectl run neo4j-init --rm -it --image=neo4j:5.23-community --restart=Never -- \
     cypher-shell -a bolt://<neo4j-host>:7687 -u neo4j -p <admin-password> \
       --param 'krs_reader_password => "<strong-password>"' \
       -f /dev/stdin < deploy/neo4j-init/krs_read_role.cypher
   ```

### Static analysis

A CI guardrail (`tools/lint/check_neo4j_write_role.py`) enforces that no code in
`services/knowledge-graph-service/` uses the generic `NEO4J_URI` / `NEO4J_USER` /
`NEO4J_PASSWORD` admin env vars or hardcodes a `bolt://` URI.  Any bypass is
flagged as a NEO4J001/NEO4J002 violation and blocks the CI `quality` job.

A second guardrail (`tools/lint/check_neo4j_read_role.py`) enforces the same for
`services/knowledge-retriever-service/` — no write Cypher (CREATE/MERGE/SET/DELETE)
and no admin env var usage (NEO4J002/NEO4J003).

```bash
uv run python -m tools.lint.check_neo4j_write_role
uv run python -m tools.lint.check_neo4j_read_role
# both exit 0 — no violations found
```
