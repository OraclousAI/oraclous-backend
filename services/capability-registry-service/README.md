# oraclous-capability-registry-service

R3.5 service #5 — the unified **capability + tool registry** (ported from the legacy
`oraclous-core-service`). A *tool* is a capability descriptor of `kind=tool`; the registry is the
sole authority for capability lookups, matching, and (later slices) tool execution.

Layered per the service-architecture standard: `routes → services → domain → repositories → core`. Every descriptor is
org-scoped (ADR-006); the organisation is resolved from the authenticated principal, never a request
body (ORG001). Identity is a pluggable seam (`AUTH_MODE=dev` binds a fixed dev principal/org from a
bearer; `AUTH_MODE=jwt` decodes the real auth-service HS256 token).

## Slices

- **S1 (this slice)** — persistence + registry CRUD. `capability_descriptors` (own Alembic
  `version_table`), org-scoped repository, OHM-v1 descriptor validation at persist
  (`oauth_token` requires non-empty scopes → 422), canonical SHA-256 `content_hash`, and capability
  matching via JSONB containment.
  - `GET/POST /api/v1/capabilities`, `GET/PUT/DELETE /api/v1/capabilities/{id}`,
    `POST /api/v1/capabilities/match`.
- **S2 (this slice)** — tool registry + plugin discovery + manifest seeding. A tool is a
  `kind=tool` descriptor with a deterministic UUIDv5 id (`generate_tool_id`). Built-in connector
  plugins (PostgreSQL/MySQL/Notion/GitHub/Google Drive readers) register at import and are seeded
  into the dev org at startup (idempotent — re-seeding is a no-op). `GET/POST /api/v1/tools`,
  `GET /api/v1/tools/{id}`.
- **S3 (this slice)** — tool instances + execution-readiness validation. `tool_instances`
  (org-scoped, `capability_id` FK, no `workflow_id`); creating an instance derives its required
  credential types from the descriptor and sets `READY`/`CONFIGURATION_REQUIRED`; mapping
  credentials re-derives the status; `validate-execution` returns a readiness report (descriptor
  exists, required credentials present, config). Live token resolution lands in S4.
  `POST /api/v1/instances`, `GET /{id}`, `POST /{id}/configure-credentials`,
  `GET /{id}/validate-execution`, `GET /{id}/health`.
- **S4 (this slice)** — synchronous execution engine + credential-broker seam + PostgreSQL
  connector. `executions` provenance table; `BaseToolExecutor`/`InternalTool`/`DatabaseTool` (hard
  timeout, credential redaction); `ToolFactory` maps a descriptor → executor; `CredentialBrokerPort`
  with `FakeCredentialBroker` (key-free dev/CI default) + `RealCredentialBroker` (`/internal/*`);
  `execute_sync` validates → resolves credentials → records QUEUED → dispatches → persists outcome
  with `credential_refs` (never the secret) → scrubs the in-memory credentials → bumps instance
  counters. `POST /api/v1/instances/{id}/execute`, `GET /api/v1/executions/{id}`. The PostgreSQL
  connector runs **parameterized** queries only.
- **S5a (this slice)** — real credential-broker integration. The broker gains
  `POST /internal/resolve-credential` (X-Internal-Key) returning a stored credential's decrypted
  payload by id; `RealCredentialBroker` resolves OAuth via `/internal/runtime-token` and non-OAuth
  via `/internal/resolve-credential` (the instance's mapped `credential_id`). With
  `CREDENTIAL_BROKER_MODE=real` the PostgreSQL connector executes against a credential resolved by
  the live broker (`tests/smoke/smoke_real_broker.sh`).
- **S5b (this slice)** — connector breadth. MySQL connector (aiomysql, real, parameterized,
  MySQL-testcontainer verified) + Notion & GitHub HTTP connectors (real httpx, api_key; live call
  key-gated, the resolution+dispatch seam verified via a mocked transport). The Google Drive Reader's
  live OAuth connector is deferred (no key-free smoke); its descriptor stays registered → executing it
  returns 409 `no_executor`. **Carries `needs-human` for Reza's §22 8-gate sign-off.**

## Run / smoke

```bash
# unit + integration (testcontainer Postgres)
uv run pytest services/capability-registry-service/tests -q

# end-to-end smoke over the docker stack (port 8001)
bash services/capability-registry-service/tests/smoke/smoke.sh
```

Config (`core/config.py`): `DATABASE_URL` and `INTERNAL_SERVICE_KEY` are required (the service fails
closed without the internal key — ADR-008). `AUTH_MODE`/`DEV_BEARER`/`JWT_SECRET` drive the identity
seam. Alembic uses its own `version_table` (`alembic_version_capability_registry`) so it shares the
dev Postgres without colliding with the other services' migration lineages.
