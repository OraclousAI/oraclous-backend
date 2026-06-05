# oraclous-capability-registry-service

R3.5 service #5 — the unified **capability + tool registry** (ported from the legacy
`oraclous-core-service`). A *tool* is a capability descriptor of `kind=tool`; the registry is the
sole authority for capability lookups, matching, and (later slices) tool execution.

Layered per ORAA-4 §21: `routes → services → domain → repositories → core`. Every descriptor is
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
- S3 — tool instances + execution-readiness validation.
- S4 — execution engine (sync) + credential-broker seam + PostgreSQL connector.
- S5 — connector breadth + real-broker integration smoke (Reza sign-off).

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
