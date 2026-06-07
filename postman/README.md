# Oraclous API — Postman collection

A complete Postman collection for the Oraclous backend: **108 endpoints across all 8 services**,
callable through the **application-gateway** (the real entrypoint) or directly per service.

| File | What |
| --- | --- |
| `oraclous.postman_collection.json` | The collection (import into Postman / `newman`). |
| `oraclous.postman_environment.json` | The **Oraclous (local)** environment (service URLs + runtime vars). |

## Quick start

1. Import both files into Postman; select the **Oraclous (local)** environment.
2. **Auth & Identity → `POST /v1/auth/register`** (or `login`) — a test script captures
   `{{access_token}}` + `{{refresh_token}}` automatically.
3. **`GET /v1/auth/me`** — captures `{{org_id}}` and `{{user_id}}`.
4. Every other request inherits the bearer token and targets `{{base_url}}` (defaults to the
   gateway, `http://localhost:8006`). You're ready to call anything.

CLI: `newman run oraclous.postman_collection.json -e oraclous.postman_environment.json`.

## How it's organised

- **One folder per service** (Auth & Identity, Credentials, Capability Registry, Knowledge Graph,
  Knowledge Retriever, Harness Runtime, **Execution Engine**, Gateway) — business routes that go
  **through the gateway** via `{{base_url}}`.
- **Execution Engine (R5)** — durable orchestration above the harness (`/v1/engine`): async jobs
  (submit/get/list/cancel), the human task board (`/tasks` + `/complete` for entrypoint tasks,
  `/approve` for mid-loop HITL), cron schedules, and the round-table multi-actor primitive. POST
  `/jobs`, `/schedules`, and `/roundtables` capture `{{job_id}}` / `{{schedule_id}}` /
  `{{roundtable_id}}` for the follow-up requests. The new harness HITL routes
  (`/v1/harnesses/{id}/resume`, `assignments/{id}/{claim,complete}`) live in the Harness Runtime
  folder.
- **Health checks** — each service's `/health`, hit **directly** on its own port.
- **Internal (service-to-service)** — `/internal/*` routes that are **never** exposed through the
  gateway; called directly with `X-Internal-Key: {{internal_service_key}}`. Debugging only.

## Routing model (important)

`{{base_url}}` defaults to `{{gateway_url}}`. The gateway is the single edge: it verifies the bearer,
then injects `X-Principal-*` + `X-Internal-Key` to the upstream service (ADR-018 trusted gateway).
So **normal use = through the gateway** — you only send a bearer token.

To hit a service **directly** (bypass the gateway), point `{{base_url}}` at a service var
(`{{capreg_url}}`, `{{harness_url}}`, …). Note: in the full stack the services run in *gateway auth
mode*, so a direct call needs the `X-Principal-*`/`X-Internal-Key` headers the gateway would have
injected — direct access is for debugging, not the happy path.

## Service ports (direct)

| Service | Var | Port |
| --- | --- | --- |
| application-gateway | `{{gateway_url}}` | 8006 |
| capability-registry | `{{capreg_url}}` | 8001 |
| credential-broker | `{{cred_broker_url}}` | 8002 |
| knowledge-graph | `{{kg_url}}` | 8003 |
| knowledge-retriever | `{{kr_url}}` | 8004 |
| auth | `{{auth_url}}` | 8005 |
| harness-runtime | `{{harness_url}}` | 8007 |
| execution-engine | `{{engine_url}}` | 8008 |

## Keeping it current

This collection was generated from the services' FastAPI route definitions. When routes change,
regenerate or hand-edit and keep the folder/variable conventions above. The **Harness Runtime →
`POST /v1/harnesses/execute`** request carries a ready-to-run inline OHM (a human-actor review
harness that escalates to a task-board assignment — needs no credentials).
