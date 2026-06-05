#!/usr/bin/env bash
# R3.5 service #6 — application-gateway acceptance smoke.
# GW-1: the gateway boots as a real §21 service and serves /health.
# GW-2: it reverse-proxies a routed request to a real upstream (capability-registry), passes the
#       upstream's response/status through, 404s an unknown prefix, and 502s when the upstream is down.
#
# Usage (from repo root):  bash services/application-gateway-service/tests/smoke/smoke.sh
set -euo pipefail

GW="${GW_SMOKE_URL:-http://localhost:8006}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

if [[ "${GW_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  step "1. build + bring up two real upstreams (capability-registry + credential-broker) + the gateway"
  ${COMPOSE} up -d --build postgres
  ${COMPOSE} build capability-registry-service credential-broker-service application-gateway-service
  ${COMPOSE} up capreg-migrate credbroker-migrate
  ${COMPOSE} up -d capability-registry-service credential-broker-service application-gateway-service
fi

step "2. wait for healthy"
for i in $(seq 1 30); do curl -fsS "${GW}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "not healthy: ${GW}"; sleep 2; done
body=$(curl -fsS "${GW}/health")
echo "$body" | grep -q '"status":"ok"' && pass "/health -> ok ($body)" || fail "unexpected /health: $body"
echo "$body" | grep -q '"service":"application-gateway"' && pass "identifies as application-gateway" \
  || fail "wrong service id: $body"

step "2b. wait for the upstream (capability-registry) to be ready"
for i in $(seq 1 30); do curl -fsS "http://localhost:8001/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "upstream capability-registry not healthy"; sleep 2; done
pass "capability-registry upstream is healthy"

step "3. GW-2: forward a routed request to the real upstream (capability-registry)"
# /api/v1/tools routes to capability-registry; with the dev bearer it returns the seeded catalogue.
tools=$(curl -fsS -H "Authorization: Bearer dev-token" "${GW}/api/v1/tools")
echo "$tools" | grep -q '"PostgreSQL Reader"' \
  && pass "gateway forwarded /api/v1/tools -> capability-registry returned real data (catalogue)" \
  || fail "forward returned no real data: $tools"

step "4. GW-3: edge JWT termination rejects unauthenticated requests (before any upstream call)"
code=$(curl -s -o /dev/null -w '%{http_code}' "${GW}/api/v1/tools")  # no bearer
[[ "$code" == "401" ]] && pass "no bearer -> gateway 401 (edge auth)" || fail "expected 401, got $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer bogus" "${GW}/api/v1/tools")
[[ "$code" == "401" ]] && pass "invalid bearer -> gateway 401" || fail "expected 401, got $code"

step "5. GW-2: unknown prefix is a gateway 404 (closed allow-list, not forwarded)"
nope=$(curl -s -w '\n%{http_code}' -H "Authorization: Bearer dev-token" "${GW}/totally/unknown")
echo "$nope" | grep -q '"route_not_found"' && echo "$nope" | tail -1 | grep -q 404 \
  && pass "unknown prefix -> gateway 404 route_not_found" || fail "expected gateway 404: $nope"

step "6. GW-4: route to a SECOND real upstream (credential-broker) — multi-upstream live"
for i in $(seq 1 30); do curl -fsS "http://localhost:8002/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "credential-broker upstream not healthy"; sleep 2; done
provs=$(curl -fsS -H "Authorization: Bearer dev-token" "${GW}/credentials/providers?user_id=00000000-0000-0000-0000-0000000000c5")
echo "$provs" | grep -q '"providers"' \
  && pass "gateway routed /credentials/* -> credential-broker (2nd live upstream)" \
  || fail "credential-broker route failed: $provs"

step "7. GW-4: CORS preflight is answered at the edge"
hdrs=$(curl -s -D - -o /dev/null -X OPTIONS \
  -H "Origin: https://app.test" -H "Access-Control-Request-Method: GET" "${GW}/api/v1/tools")
echo "$hdrs" | tr -d '\r' | grep -qi '^access-control-allow-origin:' \
  && pass "OPTIONS preflight -> access-control-allow-origin header set by the edge" \
  || fail "no CORS header on preflight: $hdrs"

step "8. GW-4: the platform-internal /internal plane is NOT edge-routed"
nope=$(curl -s -w '\n%{http_code}' -H "Authorization: Bearer dev-token" "${GW}/internal/agent-credentials")
echo "$nope" | grep -q '"route_not_found"' && echo "$nope" | tail -1 | grep -q 404 \
  && pass "/internal/* -> gateway 404 (never forwarded)" || fail "expected gateway 404: $nope"

step "9. GW-5: aggregated upstream health (/health/upstreams rolls up per-service + overall)"
agg=$(curl -fsS "${GW}/health/upstreams")
echo "$agg" | python3 -c "
import sys,json
d=json.load(sys.stdin); ups={u['name']:u['status'] for u in d['upstreams']}
assert len(d['upstreams'])==5, d
# the two upstreams this smoke launched must be ok
assert ups['capability-registry']=='ok', ups
assert ups['credential-broker']=='ok', ups
# overall is a correct rollup of the per-service statuses (ok iff every upstream is ok)
expected = 'ok' if all(s=='ok' for s in ups.values()) else 'degraded'
assert d['overall']==expected, d
print(f'  rollup consistent: overall={d[\"overall\"]} statuses={ups}')
" && pass "/health/upstreams aggregates per-service health + consistent overall rollup (HTTP 200)" \
  || fail "aggregated health wrong: $agg"

step "10. GW-5: the gateway own-error envelope (request_id) on its own errors"
env=$(curl -s -D - -H "Authorization: Bearer dev-token" "${GW}/totally/unknown")
echo "$env" | grep -q '"error_code":"route_not_found"' && echo "$env" | grep -q '"request_id"' \
  && echo "$env" | tr -d '\r' | grep -qi '^x-request-id:' \
  && pass "gateway 404 -> envelope {error_code,message,request_id} + X-Request-Id header" \
  || fail "envelope missing: $env"

step "11. GW-2: upstream down -> 502 (fail-closed, no hang)"
${COMPOSE} stop capability-registry-service >/dev/null 2>&1
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer dev-token" "${GW}/api/v1/tools")
[[ "$code" == "502" || "$code" == "504" ]] && pass "upstream down -> ${code} (gateway did not hang)" \
  || fail "expected 502/504, got $code"
${COMPOSE} up -d capability-registry-service >/dev/null 2>&1

printf '\n\033[32mapplication-gateway GW-1..GW-5 smoke passed.\033[0m  edge JWT termination, '
printf 'reverse-proxy to TWO real upstreams (capability-registry + credential-broker), CORS '
printf 'termination, /internal not edge-routed, aggregated upstream health, own-error envelope, '
printf 'unknown-prefix 404, and downed-upstream 502 — over the stack.\n'
printf '%s\n' "" "(For the full §22 sign-off, run the whole stack: 'docker compose --profile services up -d --build' — then /health/upstreams rolls up all five as ok.)"
