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
  step "1. build + bring up an upstream (capability-registry) + the application-gateway"
  ${COMPOSE} up -d --build postgres
  ${COMPOSE} build capability-registry-service application-gateway-service
  ${COMPOSE} up capreg-migrate
  ${COMPOSE} up -d capability-registry-service application-gateway-service
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

step "4. GW-2: the upstream's own status passes through (no edge auth yet)"
code=$(curl -s -o /dev/null -w '%{http_code}' "${GW}/api/v1/tools")  # no bearer
[[ "$code" == "401" ]] && pass "no bearer -> upstream 401 passed through verbatim" \
  || fail "expected upstream 401, got $code"

step "5. GW-2: unknown prefix is a gateway 404 (closed allow-list, not forwarded)"
nope=$(curl -s -w '\n%{http_code}' "${GW}/totally/unknown")
echo "$nope" | grep -q '"route_not_found"' && echo "$nope" | tail -1 | grep -q 404 \
  && pass "unknown prefix -> gateway 404 route_not_found" || fail "expected gateway 404: $nope"

step "6. GW-2: upstream down -> 502 (fail-closed, no hang)"
${COMPOSE} stop capability-registry-service >/dev/null 2>&1
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer dev-token" "${GW}/api/v1/tools")
[[ "$code" == "502" || "$code" == "504" ]] && pass "upstream down -> ${code} (gateway did not hang)" \
  || fail "expected 502/504, got $code"
${COMPOSE} up -d capability-registry-service >/dev/null 2>&1

printf '\n\033[32mapplication-gateway GW-1+GW-2 smoke passed.\033[0m  the gateway reverse-proxies a '
printf 'routed request to a real upstream (real data through the edge), passes upstream status '
printf 'through, 404s unknown prefixes, and 502s a downed upstream — over the stack.\n'
