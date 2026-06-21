# deploy/

Deployment scaffolding for oraclous-backend.

| Path | Purpose |
| --- | --- |
| `docker-compose.yml` | Local self-hosted **substrate stack**: Neo4j, Postgres, Redis, Jaeger. App-service containers are added per release. |
| `docker-compose.agent.yml` | Concurrency override — run multiple isolated stacks on one host via `COMPOSE_PROJECT_NAME` + OS-assigned ports. |
| `docker-compose.fe-target.yml` | Fixed-port overlay for the long-lived shared **fe-target** stack used by frontend-implementer. |
| `helm/` | **Production Helm chart** for the full backend — all nine services + workers, migrate/seed hook Jobs, the gateway Ingress, Neo4j role provisioning. Encodes the prod security contract (RLS DSN split, `RUN_MODE=prod`, secrets-as-secretKeyRef). Substrate is operator-managed/external. See [Production (Kubernetes / Helm)](#production-kubernetes--helm). |
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

The production Helm chart (`deploy/helm`) provisions the `kgs_writer` + `krs_reader` Neo4j
roles automatically via the `neo4j-role-init` pre-install/pre-upgrade Job (passwords injected
from Secrets), then wires KGS/KRS to their role-scoped credentials. See the consolidated
[Production (Kubernetes / Helm)](#production-kubernetes--helm) section below for the full
chart, the Secret list, and the deploy commands.

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

## Postgres RLS roles (ADR-030)

The Postgres row-level-security backstop (ADR-012 §2, realized by ADR-030) needs the **runtime**
services to connect as a `NOSUPERUSER NOBYPASSRLS` role — a superuser/`BYPASSRLS` role bypasses RLS
unconditionally, leaving the policy inert. The realized services therefore split their DB access:

| Role | Attributes | Used for |
|---|---|---|
| `oraclous` | superuser (owner) | Alembic migrations + DDL + the operator envelope backfill (it must see every org's rows, which a superuser does by bypassing RLS) |
| `oraclous_app` | `NOSUPERUSER NOBYPASSRLS`, DML grants only | the **runtime** service connections — the FORCE'd RLS policy bites them, scoping every row to the GUC-bound org |

**Slice 0 realizes `credential-broker-service`** (its four org-scoped tables: `user_credentials`,
`webhook_secrets`, `delegated_tokens`, `org_data_keys`). Other services follow in later slices.

### Local development

`oraclous_app` is provisioned two ways (both idempotent), so a fresh **or** an existing `pgdata`
volume is covered:

- **Fresh volume:** `deploy/postgres-init/01_rls_app_role.sql` is mounted at
  `/docker-entrypoint-initdb.d/` and creates the role on first DB init.
- **Every deploy:** the `credbroker-migrate` one-shot runs, as the owner,
  `alembic upgrade head && python -m oraclous_credential_broker_service.core.bootstrap_rls_role`
  — the migration enables RLS (0004) and the bootstrap (re-)creates the role + grants DML on the
  broker tables.

The runtime `credential-broker-service` then connects as `oraclous_app`
(`DATABASE_URL=postgresql+asyncpg://oraclous_app:app@postgres:5432/oraclous`) and refuses to start
if its role can bypass RLS (`RLS_ASSERT_RUNTIME_ROLE: "true"` → fail closed, ADR-030 §3). Dev
password is `app`; production overrides the runtime DSN with a managed credential.

> **Operator note — the envelope backfill must run as the OWNER, not `oraclous_app`.** The ADR-020
> backfill (`python -m oraclous_credential_broker_service.tasks.backfill_envelope`) sweeps *every*
> org's ciphertext, which RLS would hide under `oraclous_app`. Run it with `DATABASE_URL` pointed at
> the owner (`oraclous`) DSN — it bypasses RLS as a superuser and sees all rows.

### Static analysis

A CI guardrail (`tools/lint/check_rls_coverage.py`, wired into the `lint` job and `.githooks/pre-push`)
enforces that every org-scoped table of a **realized** service (listed in `tools/lint/rls_coverage.yaml`)
has an `enable_rls_on(...)` call in that service's migrations, and that no realized service ships a
new org-scoped storage model absent from the manifest. It is scope-aware: a not-yet-realized service
is ignored, so the phased rollout never reddens the build before its slice lands. The data-layer
proof that RLS actually isolates (app-`WHERE` removed; cross-org write denied 42501) is each service's
integration isolation test (e.g. `test_rls_backstop_isolation.py`).

```bash
uv run python -m tools.lint.check_rls_coverage
# exits 0 — every realized org-scoped table has RLS applied
```

## Production (Kubernetes / Helm)

`deploy/helm` is the production chart for the **whole backend**. It templates all nine services
(Deployments + ClusterIP Services on port 8000, `/health` liveness + `/readyz` readiness — the
gateway uses `/health` for both since it has no `/readyz`), the KGS + execution-engine workers and
the engine beat (no Service), the schema-migration / seed Jobs (Helm pre-install/pre-upgrade
hooks), the gateway **Ingress** (the single external surface), and the Neo4j role-provisioning Job.

The **source of truth** for every service's image, port, and env is `deploy/docker-compose.yml`
(the dev stack); the chart encodes the same contract for production.

### What the chart does NOT deploy

Postgres, Neo4j, and Redis are **operator-managed / external** in production. The chart only wires
the app services to them via `substrate.{postgres,neo4j,redis}` and the `neo4jRoles.*` values — it
never deploys an in-cluster datastore.

### Security contract encoded by the chart

1. **RLS DSN split (ADR-030).** Every *runtime* service connects to Postgres as the
   `NOSUPERUSER`/`NOBYPASSRLS` `oraclous_app` role and sets its `*_RLS_ASSERT_RUNTIME_ROLE=true`
   (so a pod refuses to start if its role can bypass RLS). Every *migrate/seed* Job connects as the
   `oraclous` **owner** role. This is structural: a workload declares only a `postgres.role`
   (`app`/`owner`), the migrate-Job template **forces** `owner`, and the two passwords come from
   two distinct Secret keys — a runtime pod can never receive the owner DSN and a migrate can never
   receive `oraclous_app`. The gateway additionally carries `OWNER_DATABASE_URL` (owner) for its two
   pre-auth producer reads; the execution-engine carries `ENGINE_MAINTENANCE_DATABASE_URL` (owner)
   for cross-org sweeps.
2. **`RUN_MODE=prod`** on every app workload (grade-A WP-1 fail-closed: a missing security secret
   raises at boot instead of falling back to a dev default).
3. **Secrets as `secretKeyRef` only.** No sensitive value is ever a literal in a manifest. DB
   passwords are delivered as their own env vars from Secrets and the DSN is composed with
   Kubernetes dependent-env-var (`$(VAR)`) expansion, so the secret lives only in the Secret object.
   `GATEWAY_CORS_ORIGINS` must be a real origin allow-list (never `*`); left empty it fail-closes
   under `RUN_MODE=prod`.

The chart is **fail-closed**: rendering against the bare `values.yaml` errors out (`... secretName
is required`) until the operator supplies every Secret — you cannot accidentally deploy without
them.

### Deploy

1. **Create the Kubernetes Secrets** the chart references. The full list (names + keys) is
   documented at the top of `deploy/helm/values-prod.example.yaml`:
   the owner + `oraclous_app` DB passwords, the Neo4j admin / `kgs_writer` / `krs_reader`
   passwords, the shared `JWT_SECRET` + `INTERNAL_SERVICE_KEY`, `OAUTH_ENC_KEY`, the
   credential-broker `ENCRYPTION_KEY`, the OpenRouter/BYOM LLM key, the OAuth provider client
   id/secret pairs you enable, and the gateway TLS Secret.

2. **Copy + fill the values:**
   ```bash
   cp deploy/helm/values-prod.example.yaml values-prod.yaml
   # edit values-prod.yaml: registry, substrate endpoints, ingress host, Secret names, CORS origins
   ```

3. **Install / upgrade** (the migrate + neo4j-role-init hook Jobs run first, in weight order:
   `neo4j-role-init` → migrates → `kgs-seed`):
   ```bash
   helm upgrade --install oraclous deploy/helm -f values-prod.yaml -n oraclous --create-namespace
   ```

### Validate the chart locally

```bash
helm lint deploy/helm -f deploy/helm/values-prod.example.yaml
helm template oraclous deploy/helm -f deploy/helm/values-prod.example.yaml | kubeconform -kubernetes-version 1.29.0 -summary -
```

CI runs `helm lint` + `helm template` (with the prod example values) on every PR via the
`helm-chart` job in `.github/workflows/ci.yml`, so the chart stays deployable as services evolve.

### Chart layout

| Path | Purpose |
| --- | --- |
| `Chart.yaml` | Chart + app version. |
| `values.yaml` | Full structure + safe **non-secret** prod defaults; every sensitive value is an empty `secretKeyRef` the operator fills. |
| `values-prod.example.yaml` | Fully-worked operator example — documents every Secret name + key and every value to fill. |
| `templates/workloads.yaml` | One generic Deployment(+Service) ranging over `.Values.services`. |
| `templates/migrate-jobs.yaml` | One generic Job (pre-install/pre-upgrade hook, OWNER DSN) ranging over `.Values.migrations`. |
| `templates/neo4j-role-init-job.yaml` | Provisions `kgs_writer` + `krs_reader` on the external Neo4j. |
| `templates/ingress.yaml` | The gateway Ingress (the only external surface). |
| `templates/_helpers.tpl` | Naming, labels, image ref, and the RLS-split DSN + secretKeyRef helpers. |
