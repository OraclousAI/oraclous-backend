#!/usr/bin/env bash
# R3.5 service #3 Slice 1 acceptance smoke — CROSS-SERVICE identity keystone.
# Proves a human can register/login at the auth-service (:8005) and the resulting JWT authorises
# against knowledge-graph-service (:8003) in jwt-mode — i.e. KGS/KRS jwt-mode is now REAL, not a 401
# stub. The whole stack shares one HS256 secret (AUTH_JWT_SECRET); KGS/KRS run with *_AUTH_MODE=jwt.
#
# Usage (from repo root):  bash services/auth-service/tests/smoke/smoke.sh
set -euo pipefail

export AUTH_JWT_SECRET="${AUTH_JWT_SECRET:-smoke-shared-jwt-secret}"
export KGS_AUTH_MODE="jwt"
export KRS_AUTH_MODE="jwt"

AUTH="${AUTH_SMOKE_URL:-http://localhost:8005}"
KGS="${KGS_SMOKE_URL:-http://localhost:8003}"
KRS="${KRS_SMOKE_URL:-http://localhost:8004}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
jget() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

if [[ "${AUTH_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  step "1. bring up substrate + migrations + auth-service + KGS (jwt-mode)"
  ${COMPOSE} up -d --build postgres neo4j redis
  # Build the app images first so the migrate one-shots (which share them) run fresh code.
  ${COMPOSE} build auth-service knowledge-graph-service knowledge-retriever-service
  ${COMPOSE} up auth-migrate kgs-migrate kgs-seed
  ${COMPOSE} up -d auth-service knowledge-graph-service knowledge-graph-worker \
    knowledge-retriever-service
fi

step "2. wait for auth-service + KGS healthy"
for url in "${AUTH}" "${KGS}"; do
  for i in $(seq 1 30); do curl -fsS "${url}/health" >/dev/null 2>&1 && break; \
    [[ $i -eq 30 ]] && fail "not healthy: ${url}"; sleep 2; done
done
pass "auth-service + KGS healthy"

step "3. register a human user"
reg=$(curl -fsS -H "Content-Type: application/json" -X POST "${AUTH}/v1/auth/register" \
  -d '{"email":"smoke@oraclous.dev","password":"SmokePass1"}')
ACCESS=$(echo "$reg" | jget "['access_token']")
REFRESH=$(echo "$reg" | jget "['refresh_token']")
[[ -n "$ACCESS" && -n "$REFRESH" ]] && pass "registered -> access + refresh tokens issued" \
  || fail "register did not return tokens: $reg"

step "4. KGS jwt-mode authorises the user token (was a 401 stub before R3.5-P3)"
c=$(code -X GET "${KGS}/api/v1/graphs")
[[ "$c" == "401" ]] && pass "KGS no-token -> 401" || fail "KGS no-token expected 401, got $c"
c=$(code -H "Authorization: Bearer ${ACCESS}" -X GET "${KGS}/api/v1/graphs")
[[ "$c" == "200" ]] && pass "KGS + user access token -> 200 (jwt-mode REAL)" \
  || fail "KGS + user token expected 200, got $c"
c=$(code -H "Authorization: Bearer ${REFRESH}" -X GET "${KGS}/api/v1/graphs")
[[ "$c" == "401" ]] && pass "KGS rejects a refresh token as access (type claim enforced)" \
  || fail "KGS should reject refresh token, got $c"

step "5. the user token actually writes (org-scoped to the user's org)"
gid=$(curl -fsS -H "Authorization: Bearer ${ACCESS}" -H "Content-Type: application/json" \
  -X POST "${KGS}/api/v1/graphs" -d '{"name":"identity-smoke"}' | jget "['id']")
[[ -n "$gid" ]] && pass "created graph ${gid} under the user's organisation" || fail "graph create failed"
listed=$(curl -fsS -H "Authorization: Bearer ${ACCESS}" -X GET "${KGS}/api/v1/graphs")
echo "$listed" | grep -q "$gid" && pass "the user sees their own graph back" || fail "graph not listed"

step "6. login issues a fresh token that also authorises against KGS"
log=$(curl -fsS -H "Content-Type: application/json" -X POST "${AUTH}/v1/auth/login" \
  -d '{"email":"smoke@oraclous.dev","password":"SmokePass1"}')
LACCESS=$(echo "$log" | jget "['access_token']")
c=$(code -H "Authorization: Bearer ${LACCESS}" -X GET "${KGS}/api/v1/graphs")
[[ "$c" == "200" ]] && pass "login token -> KGS 200" || fail "login token rejected by KGS: $c"

step "7. KRS jwt-mode accepts the same identity (read side)"
c=$(code -H "Authorization: Bearer ${ACCESS}" -H "Content-Type: application/json" \
  -X POST "${KRS}/v1/search/semantic" -d "{\"query\":\"x\",\"graph_id\":\"${gid}\"}")
[[ "$c" != "401" ]] && pass "KRS accepts the user token (HTTP $c, not 401)" \
  || fail "KRS rejected the user token (401) — jwt-mode not wired"

printf '\n\033[32mIdentity smoke passed.\033[0m  auth-service issues tokens that KGS+KRS jwt-mode '
printf 'authorise end-to-end, org-scoped to the registering user.\n'
