# oraclous-application-gateway-service

R3.5 service #6 — the single **reverse-proxy edge** fronting the backend services. R6 hardens it: it
now also owns Redis (the edge rate limiter, Slice 2) and a small Postgres (the integration-key store,
Slice 3 / ADR-019) alongside the shared `httpx` client to the upstreams. Layered per the
service-architecture standard: `routes → services → domain → repositories → core`.

Until the gateway fronts everything, the services also stay reachable directly by host port. The
platform-internal `/internal/*` plane (X-Internal-Key, service-to-service) is **never** edge-exposed.

## Slices

- **GW-1 (this slice)** — runnable §21 skeleton + dependency-free `GET /health` (answers even when
  every upstream is down). Config carries the upstream base URLs, the identity seam
  (`GATEWAY_AUTH_MODE` dev|jwt), and the CORS allow-list; `lifespan` opens the shared upstream HTTP
  client. Compose block on host port **8006**.
- **GW-2 (this slice)** — config-driven **route table** (longest-prefix match) + reverse-proxy
  passthrough. `domain/route_table` resolves a path-prefix to an upstream base URL;
  `repositories/upstream_client` streams the request/response over the shared httpx client with
  bounded timeouts; `services/proxy_service` resolves + applies the forward header policy (drops
  `host` + hop-by-hop); a catch-all route streams the upstream response back. Fail-closed: unknown
  prefix → 404, upstream down → 502, timeout → 504. No edge auth yet (GW-3).
- **GW-3 (this slice)** — edge JWT termination + identity forwarding. `core/auth` verifies the bearer
  ONCE (reusing `oraclous-governance` + the substrate claim contract; `dev`/`jwt` modes); a
  closed public allow-list (`/v1/auth/*`, `/oauth/*`) proxies through unauthenticated. On a verified
  request the gateway forwards `X-Principal-Id`/`X-Principal-Type`/`X-Organisation-Id` downstream
  (keeping the original Bearer) and **strips any client-forged identity headers** (anti-spoof).
  Fail-closed: missing/invalid/expired → 401 before any upstream call.
- **GW-4 (this slice)** — all seven upstreams routed (verified each prefix → its upstream) + **CORS
  termination** at the edge (Starlette `CORSMiddleware`, `GATEWAY_CORS_ORIGINS`), so upstreams don't
  each carry CORS. The platform-internal `/internal/*` plane is confirmed **not** edge-routed
  (gateway 404, never forwarded).
- **GW-5 (this slice)** — aggregated upstream health + gateway own-error envelope.
  `GET /health/upstreams` fans out to each upstream's `/health` (per-service `{name,status,latency_ms}`
  + an overall rollup; always HTTP 200, body reflects degraded). The gateway's **own** errors
  (401/404/502/503/504) return the forward-compatible envelope `{error_code, message, request_id}`
  (id echoed in `X-Request-Id`); upstream errors still pass through verbatim (full cross-service
  normalization → R6). Carries the **§22 sign-off** (`needs-human`).

## Run / smoke

```bash
uv run pytest services/application-gateway-service/tests -q
bash services/application-gateway-service/tests/smoke/smoke.sh   # docker stack, port 8006
```

Config (`core/config.py`): all settings have dev defaults (the gateway boots with no env). `*_URL`
settings are the upstream base URLs; `GATEWAY_AUTH_MODE`/`JWT_SECRET` drive the identity seam;
`GATEWAY_CORS_ORIGINS` (comma-separated) the CORS allow-list.
