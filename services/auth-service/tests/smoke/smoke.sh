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

step "3. register a human user (fall back to login if the smoke DB already has them)"
CRED='{"email":"smoke@oraclous.dev","password":"SmokePass1"}'
reg=$(curl -s -H "Content-Type: application/json" -X POST "${AUTH}/v1/auth/register" -d "$CRED")
ACCESS=$(echo "$reg" | jget "['access_token']" 2>/dev/null || echo "")
if [[ -z "$ACCESS" ]]; then
  reg=$(curl -fsS -H "Content-Type: application/json" -X POST "${AUTH}/v1/auth/login" -d "$CRED")
  ACCESS=$(echo "$reg" | jget "['access_token']")
fi
REFRESH=$(echo "$reg" | jget "['refresh_token']")
[[ -n "$ACCESS" && -n "$REFRESH" ]] && pass "user has access + refresh tokens" \
  || fail "no tokens from register/login: $reg"

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

step "7. S2: multi-org — active-org selection flows to KGS scoping + cross-org isolation"
ORG2=$(curl -fsS -H "Authorization: Bearer ${ACCESS}" -H "Content-Type: application/json" \
  -X POST "${AUTH}/v1/orgs" -d '{"name":"Second Workspace"}' | jget "['id']")
[[ -n "$ORG2" ]] && pass "created a second organisation ${ORG2}" || fail "org create failed"
# log in selecting org2 as the active org -> the token is scoped to org2
T2=$(curl -fsS -H "Content-Type: application/json" -H "X-Organisation-Id: ${ORG2}" \
  -X POST "${AUTH}/v1/auth/login" -d '{"email":"smoke@oraclous.dev","password":"SmokePass1"}' \
  | jget "['access_token']")
g2=$(curl -fsS -H "Authorization: Bearer ${T2}" -H "Content-Type: application/json" \
  -X POST "${KGS}/api/v1/graphs" -d '{"name":"org2-graph"}' | jget "['id']")
[[ -n "$g2" ]] && pass "org2 token wrote a graph ${g2} in KGS" || fail "org2 graph create failed"
# the personal-org token must NOT see org2's graph (org isolation at the data layer)
listed=$(curl -fsS -H "Authorization: Bearer ${ACCESS}" -X GET "${KGS}/api/v1/graphs")
echo "$listed" | grep -q "$g2" && fail "ISOLATION BREACH: personal-org token saw org2's graph" \
  || pass "personal-org token cannot see org2's graph (active-org scoping holds)"
echo "$listed" | grep -q "$gid" && pass "personal-org token still sees its own graph" \
  || fail "personal-org token lost its own graph"

step "8. S3: invite an email into org2, accept it, and confirm the new membership"
INVTOK=$(curl -fsS -H "Authorization: Bearer ${T2}" -H "Content-Type: application/json" \
  -X POST "${AUTH}/v1/orgs/${ORG2}/invitations" -d '{"email":"invitee@oraclous.dev"}' \
  | jget "['token']")
[[ -n "$INVTOK" ]] && pass "owner invited invitee@oraclous.dev (hashed token issued)" \
  || fail "invitation create failed"
peekrole=$(curl -fsS -H "Content-Type: application/json" -X POST "${AUTH}/v1/invitations/peek" \
  -d "{\"token\":\"${INVTOK}\"}" | jget "['role']")
[[ "$peekrole" == "member" ]] && pass "public peek resolves the invitation (role=member)" \
  || fail "peek failed: $peekrole"
# the invitee registers (fresh) or logs in, then accepts
ireg=$(curl -s -H "Content-Type: application/json" -X POST "${AUTH}/v1/auth/register" \
  -d '{"email":"invitee@oraclous.dev","password":"InviteePass1"}')
IACCESS=$(echo "$ireg" | jget "['access_token']" 2>/dev/null || echo "")
[[ -z "$IACCESS" ]] && IACCESS=$(curl -fsS -H "Content-Type: application/json" \
  -X POST "${AUTH}/v1/auth/login" -d '{"email":"invitee@oraclous.dev","password":"InviteePass1"}' \
  | jget "['access_token']")
acceptedorg=$(curl -fsS -H "Authorization: Bearer ${IACCESS}" -H "Content-Type: application/json" \
  -X POST "${AUTH}/v1/invitations/accept" -d "{\"token\":\"${INVTOK}\"}" | jget "['organisation_id']")
[[ "$acceptedorg" == "$ORG2" ]] && pass "invitee accepted -> member of org2" \
  || fail "accept did not join org2: $acceptedorg"
# replay the (now accepted) token -> generic 400
c=$(code -H "Authorization: Bearer ${IACCESS}" -H "Content-Type: application/json" \
  -X POST "${AUTH}/v1/invitations/accept" -d "{\"token\":\"${INVTOK}\"}")
[[ "$c" == "400" ]] && pass "replayed invitation token rejected (generic 400)" \
  || fail "replayed token expected 400, got $c"

step "9. S4: a service-account credential mints a service_account token that KGS authorises"
INTKEY="${AUTH_INTERNAL_KEY:-dev-internal-key}"
sacred=$(curl -fsS -H "X-Internal-Key: ${INTKEY}" -H "Content-Type: application/json" \
  -X POST "${AUTH}/internal/agent-credentials" \
  -d "{\"organisation_id\":\"${ORG2}\",\"created_by_user_id\":\"smoke\",\"principal_type\":\"service_account\"}")
SARAW=$(echo "$sacred" | jget "['credential']")
[[ "$(echo "$sacred" | jget "['principal_type']")" == "service_account" ]] \
  && pass "created a service_account credential (internal-key gated)" || fail "SA create failed: $sacred"
saexch=$(curl -fsS -H "Content-Type: application/json" -X POST "${AUTH}/agent-token" \
  -d "{\"credential\":\"${SARAW}\"}")
SATOK=$(echo "$saexch" | jget "['access_token']")
[[ "$(echo "$saexch" | jget "['principal_type']")" == "service_account" ]] \
  && pass "exchange minted a service_account JWT" || fail "SA exchange wrong type: $saexch"
c=$(code -H "Authorization: Bearer ${SATOK}" -X GET "${KGS}/api/v1/graphs")
[[ "$c" == "200" ]] && pass "KGS jwt-mode authorises the service_account token (200)" \
  || fail "KGS rejected the service_account token: $c"
# wrong internal key is rejected (no SA minted)
c=$(code -H "X-Internal-Key: ${INTKEY}X" -H "Content-Type: application/json" \
  -X POST "${AUTH}/internal/agent-credentials" -d "{\"organisation_id\":\"${ORG2}\",\"created_by_user_id\":\"x\"}")
[[ "$c" == "401" ]] && pass "wrong internal key rejected (401)" || fail "internal-key gate weak: $c"

step "10. KRS jwt-mode accepts the same identity (read side)"
c=$(code -H "Authorization: Bearer ${ACCESS}" -H "Content-Type: application/json" \
  -X POST "${KRS}/v1/search/semantic" -d "{\"query\":\"x\",\"graph_id\":\"${gid}\"}")
[[ "$c" != "401" ]] && pass "KRS accepts the user token (HTTP $c, not 401)" \
  || fail "KRS rejected the user token (401) — jwt-mode not wired"

printf '\n\033[32mIdentity smoke passed.\033[0m  auth issues tokens that KGS+KRS jwt-mode authorise '
printf 'end-to-end; multi-org active-org selection scopes KGS writes + isolation holds.\n'
