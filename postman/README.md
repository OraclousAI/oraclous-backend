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

## Gateway edge behavior (R6)

The gateway publishes its own contract and enforces two edge controls (no new requests to import —
they apply to every call):

- **Published OpenAPI contract** (R6 Slice 1) — `GET {{gateway_url}}/v1/openapi.json` (or
  `/v1/openapi.yaml`) is the canonical API contract; `GET {{gateway_url}}/docs` is a live Swagger UI.
- **Rate limit** (R6 Slice 2) — any request may return **429 `RATE_LIMITED`** with a `Retry-After`
  header (seconds). It's a per-client-IP window; back off and retry after `Retry-After`. (Liveness +
  the OpenAPI/`/docs` probes are exempt.)
- **Request-size cap** (R6 Slice 2) — a body over the gateway's cap returns **413 `PAYLOAD_TOO_LARGE`**.
- **Integration-key auth** (R6 Slice 3) — the gateway also accepts an **`oak-`/`oag-` integration-key bearer** (in addition to a member JWT): `Authorization: Bearer oak-…`. It resolves to an org-scoped service-account; an unknown/revoked/expired/wrong key is rejected `401` at the edge.
- **Published agents + integration keys** (R6 Slice 4) — the **`Published Agents & Integration Keys (R6 S4)`** folder. A member publishes an agent (`POST /v1/agents`) and mints a key bound to it (`POST /v1/integration-keys` — the plaintext is returned **once**, captured into `{{integration_key}}`); the public **`GET /v1/agents/{slug}`** + **`POST /v1/agents/{slug}/invoke`** then use that key (a key may only reach the agent it is bound to — otherwise `403`). Run the folder top-to-bottom: publish → mint → GET/invoke → rotate → revoke.
- **Per-key CORS** (R6 Slice 5) — set `cors_origins` (a list of exact `scheme://host[:port]` origins) when minting a key to scope which **browser** origins may embed that published agent. Postman is server-side (no `Origin` header), so it isn't CORS-gated; in a browser, a preflight from a listed origin is reflected (no credentials) and the response carries `Access-Control-Allow-Origin` only for a listed origin (a key with no `cors_origins` allows none). Only the two `/v1/agents/{slug}` widget routes are per-key-scoped; everything else uses the gateway-wide CORS.
- **Member chat** (R6 Slice 6) — the **`Member Chat (R6 S6)`** folder. A member starts a thread bound to a published agent (`POST /v1/chat/threads`, captured into `{{thread_id}}`), then sends messages (`POST .../messages` runs the agent on the harness + persists the turn; the response `status` is `succeeded` / `pending` (a HITL escalation) / `failed`), reads the transcript, and soft-deletes. A thread is **private to its creator** — another member's thread reads `404`. Run after the publish step (it sets `{{pub_slug}}`).
- **Webhooks** (R6 Slice 7) — the **`Webhooks (R6 S7)`** folder. A member registers a webhook for a published agent (`POST /v1/webhook-subscriptions`, capturing `{{webhook_sub_id}}` + `{{webhook_signing_secret}}` — the secret is shown **once**), then the **public** inbound request (`POST /v1/webhooks/{{webhook_sub_id}}`) fires it: a **pre-request script** signs the raw body with the captured secret into `X-Hub-Signature-256` (HMAC-SHA256), so a correct signature → `202` and a tampered one → `404`. The inbound door uses **no bearer** (it authenticates by the signature). Run after the publish step (it sets `{{pub_slug}}`).
- **MCP server** (R6 Slice 8) — the **`MCP Server (R6 S8)`** folder. An MCP (Model Context Protocol) JSON-RPC server (`POST /v1/mcp`) exposing the org's published agents as tools to external MCP clients. Auth is your **integration key** (`{{integration_key}}` as a Bearer — a member JWT is `403`), so mint a bound key first. `initialize` → the handshake; `tools/list` → the agents your key may invoke (scoped by its binding); `tools/call` (name = the agent slug) → runs the agent through the invoke path and returns `{content, isError}` (the harness internals never leak; a tool outside the binding → a JSON-RPC `unknown tool` error).

Both 429 and 413 are the standard ORA-37 error envelope (`{error:{code,message,requestId,retryable}}`).

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
