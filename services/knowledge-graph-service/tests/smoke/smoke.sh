#!/usr/bin/env bash
# R3.5-P1-S1 acceptance smoke — knowledge-graph-service graph CRUD against the real stack.
#
# This is the runbook Reza runs to sign off S1 (ORAA-4 §22 gate 6). It needs NO API keys: the
# dev-auth seam (Bearer dev-token) and Postgres are all that S1 exercises. Ingestion (Neo4j nodes,
# CSV/code, the §22 gate-5 "real substrate" assertions) arrives in S2 and extends this script.
#
# Usage (from repo root):
#   bash services/knowledge-graph-service/tests/smoke/smoke.sh
# Override the base URL with KGS_SMOKE_URL (default http://localhost:8003).
set -euo pipefail

BASE="${KGS_SMOKE_URL:-http://localhost:8003}"
AUTH=(-H "Authorization: Bearer dev-token" -H "Content-Type: application/json")
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml --profile services"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

if [[ "${KGS_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  step "1. bring the stack up (postgres -> migrate -> seed -> service)"
  ${COMPOSE} up -d --build postgres kgs-migrate kgs-seed knowledge-graph-service
fi

step "2. wait for /health"
for i in $(seq 1 30); do
  if curl -fsS "${BASE}/health" >/dev/null 2>&1; then break; fi
  [[ $i -eq 30 ]] && fail "service did not become healthy at ${BASE}/health"
  sleep 2
done
curl -fsS "${BASE}/health" | grep -q '"status":"ok"' && pass "health ok" || fail "health body"

step "3. auth seam"
code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/api/v1/graphs")
[[ "$code" == "401" ]] && pass "no token -> 401" || fail "expected 401, got $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer wrong" "${BASE}/api/v1/graphs")
[[ "$code" == "401" ]] && pass "bad token -> 401" || fail "expected 401, got $code"

step "4. create a graph"
created=$(curl -fsS "${AUTH[@]}" -X POST "${BASE}/api/v1/graphs" -d '{"name":"smoke-graph","description":"s1"}')
echo "  -> ${created}"
echo "$created" | grep -q '"organisation_id"' && fail "organisation_id leaked into response" || pass "no org leak"
GID=$(echo "$created" | sed -n 's/.*"id":"\([0-9a-f-]*\)".*/\1/p')
[[ -n "$GID" ]] && pass "created id=$GID" || fail "no id in create response"

step "5. list includes it (plus the seeded dev-demo-graph)"
list=$(curl -fsS "${AUTH[@]}" "${BASE}/api/v1/graphs")
echo "$list" | grep -q "smoke-graph" && pass "smoke-graph listed" || fail "smoke-graph not in list"
echo "$list" | grep -q "dev-demo-graph" && pass "seeded dev-demo-graph present" || pass "(seed graph optional)"

step "6. get / update / delete lifecycle"
curl -fsS "${AUTH[@]}" "${BASE}/api/v1/graphs/${GID}" | grep -q "smoke-graph" && pass "get ok" || fail "get"
upd=$(curl -fsS "${AUTH[@]}" -X PATCH "${BASE}/api/v1/graphs/${GID}" -d '{"name":"smoke-graph-2"}')
echo "$upd" | grep -q "smoke-graph-2" && pass "update ok" || fail "update"
code=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X DELETE "${BASE}/api/v1/graphs/${GID}")
[[ "$code" == "204" ]] && pass "delete -> 204" || fail "expected 204, got $code"
code=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "${BASE}/api/v1/graphs/${GID}")
[[ "$code" == "404" ]] && pass "get deleted -> 404" || fail "expected 404, got $code"

step "7. unknown graph -> 404"
code=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "${BASE}/api/v1/graphs/00000000-0000-0000-0000-0000000000ff")
[[ "$code" == "404" ]] && pass "unknown -> 404" || fail "expected 404, got $code"

printf '\n\033[32mS1 smoke passed.\033[0m  (cross-org isolation against two live principals arrives with the identity service, R3.5-P3.)\n'
