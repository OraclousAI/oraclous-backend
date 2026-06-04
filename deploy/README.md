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

## Neo4j roles

`deploy/neo4j-init/krs_read_role.cypher` creates the `krs_reader` user and grants it the built-in `reader` role (read-only: ACCESS + MATCH only; no write/schema/admin capabilities). It must be applied once against any new Neo4j instance before the knowledge-retriever-service starts.

`$krs_reader_password` is a **required Cypher parameter** — there is no baked-in default. Pass it explicitly via `--param`.

### Dev (local stack)

The dev default password is `krs_reader_dev` (matches `KRS_NEO4J_PASSWORD` in `.env.dev`).

```bash
# From repo root, with the local stack running:
cypher-shell -a bolt://localhost:7687 \
  -u neo4j -p password \
  --param 'krs_reader_password => "krs_reader_dev"' \
  --file deploy/neo4j-init/krs_read_role.cypher
```

The script is idempotent — safe to re-run on an already-initialised database.

### Production (K8s)

The password is injected at deploy time from the `krs-neo4j-credentials` Kubernetes Secret:

```yaml
# helm/templates/jobs/neo4j-init-job.yaml (excerpt)
env:
  - name: KRS_READER_PASSWORD
    valueFrom:
      secretKeyRef:
        name: krs-neo4j-credentials
        key: krs_reader_password
```

The init Job runs `cypher-shell --param "krs_reader_password => \"$KRS_READER_PASSWORD\""` against the cluster Neo4j endpoint before the KRS deployment rolls out. See `helm/values.yaml` for the secret reference and `helm/templates/jobs/` for the full Job spec.
