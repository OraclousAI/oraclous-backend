# oraclous-application-gateway-service

R3.5 service #6 — the single **reverse-proxy edge** fronting the backend services. Stateless (no
database); its only external substrate is the upstream services, reached over a shared `httpx`
client. Layered per ORAA-4 §21: `routes → services → domain → repositories → core`.

Until the gateway fronts everything, the services also stay reachable directly by host port. The
platform-internal `/internal/*` plane (X-Internal-Key, service-to-service) is **never** edge-exposed.

## Slices

- **GW-1 (this slice)** — runnable §21 skeleton + dependency-free `GET /health` (answers even when
  every upstream is down). Config carries the upstream base URLs, the identity seam
  (`GATEWAY_AUTH_MODE` dev|jwt), and the CORS allow-list; `lifespan` opens the shared upstream HTTP
  client. Compose block on host port **8006**.
- GW-2 — config-driven route table + single-upstream proxy passthrough (timeouts, 404/502/504).
- GW-3 — edge JWT termination + identity forwarding (`X-Principal-*`), public auth allow-list.
- GW-4 — all five upstreams routed (longest-prefix match) + CORS termination.
- GW-5 — aggregated upstream health + gateway own-error envelope subset + §22 sign-off.

## Run / smoke

```bash
uv run pytest services/application-gateway-service/tests -q
bash services/application-gateway-service/tests/smoke/smoke.sh   # docker stack, port 8006
```

Config (`core/config.py`): all settings have dev defaults (the gateway boots with no env). `*_URL`
settings are the upstream base URLs; `GATEWAY_AUTH_MODE`/`JWT_SECRET` drive the identity seam;
`GATEWAY_CORS_ORIGINS` (comma-separated) the CORS allow-list.
