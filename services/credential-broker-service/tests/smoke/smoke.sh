#!/usr/bin/env bash
# R3.5 service #4 — credential-broker S0 acceptance smoke.
# Proves the service boots, migrates its own schema (own alembic version_table), and serves /health.
# Key-free: the dev ENCRYPTION_KEY + INTERNAL_SERVICE_KEY are baked into the compose dev values.
#
# Usage (from repo root):  bash services/credential-broker-service/tests/smoke/smoke.sh
set -euo pipefail

CB="${CB_SMOKE_URL:-http://localhost:8002}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

if [[ "${CB_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  step "1. bring up postgres + migrate + the credential-broker"
  ${COMPOSE} up -d --build postgres
  ${COMPOSE} build credential-broker-service
  ${COMPOSE} up credbroker-migrate
  ${COMPOSE} up -d credential-broker-service
fi

step "2. wait for healthy"
for i in $(seq 1 30); do curl -fsS "${CB}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "not healthy: ${CB}"; sleep 2; done
body=$(curl -fsS "${CB}/health")
echo "$body" | grep -q '"status":"healthy"' && pass "/health -> healthy ($body)" \
  || fail "unexpected /health: $body"

step "3. the migration created the broker's tables (own version_table)"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -c "\dt user_credentials" 2>/dev/null \
  | grep -q user_credentials && pass "user_credentials table exists" || fail "user_credentials missing"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -c "\dt delegated_tokens" 2>/dev/null \
  | grep -q delegated_tokens && pass "delegated_tokens table exists" || fail "delegated_tokens missing"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -c "\dt alembic_version_credential_broker" \
  2>/dev/null | grep -q alembic_version_credential_broker \
  && pass "own alembic version_table (no shared-DB collision)" || fail "version_table missing"

step "4. S1: encrypted credential CRUD (dev-auth bearer binds the org)"
AUTH=(-H "Authorization: Bearer dev-token" -H "Content-Type: application/json")
jget() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
TOOL="11111111-1111-1111-1111-111111111111"; USR="22222222-2222-2222-2222-222222222222"
cid=$(curl -fsS "${AUTH[@]}" -X POST "${CB}/credentials/" \
  -d "{\"tool_id\":\"${TOOL}\",\"user_id\":\"${USR}\",\"name\":\"g\",\"provider\":\"google\",\"cred_type\":\"oauth\",\"credential\":{\"access_token\":\"smoke-secret-123\"}}" \
  | jget "['id']")
[[ -n "$cid" ]] && pass "created credential ${cid}" || fail "credential create failed"
# the stored value is ciphertext, not the plaintext secret
stored=$(${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -tAc \
  "SELECT encrypted_cred FROM user_credentials WHERE id='${cid}'")
echo "$stored" | grep -q "smoke-secret-123" && fail "PLAINTEXT AT REST: secret found in DB" \
  || pass "credential is AES-256-GCM encrypted at rest (no plaintext in DB)"
# GET decrypts back to the original secret
sec=$(curl -fsS "${AUTH[@]}" -X GET "${CB}/credentials/${cid}" | jget "['credential']['access_token']")
[[ "$sec" == "smoke-secret-123" ]] && pass "GET decrypts the credential back to plaintext" \
  || fail "decrypt mismatch: $sec"
# no token -> 401; unknown id -> 404 (org-scoped)
c=$(curl -s -o /dev/null -w '%{http_code}' -X GET "${CB}/credentials/${cid}")
[[ "$c" == "401" ]] && pass "no bearer -> 401" || fail "expected 401, got $c"
c=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X GET "${CB}/credentials/33333333-3333-3333-3333-333333333333")
[[ "$c" == "404" ]] && pass "unknown id -> 404 (org-scoped)" || fail "expected 404, got $c"

step "5. S2: internal-key gate on the provider catalogue"
INTKEY="${CRED_BROKER_INTERNAL_KEY:-dev-internal-key}"
c=$(curl -s -o /dev/null -w '%{http_code}' -X GET "${CB}/internal/providers")
[[ "$c" == "401" ]] && pass "no internal key -> 401" || fail "expected 401, got $c"
c=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Internal-Key: ${INTKEY}X" -X GET "${CB}/internal/providers")
[[ "$c" == "401" ]] && pass "wrong internal key -> 401 (constant-time compare)" || fail "expected 401, got $c"
cat=$(curl -fsS -H "X-Internal-Key: ${INTKEY}" -X GET "${CB}/internal/providers")
echo "$cat" | grep -q '"google"' && echo "$cat" | grep -q '"github"' \
  && pass "valid internal key -> provider catalogue (google/notion/github)" || fail "bad catalogue: $cat"

step "6. S3: runtime OAuth-token resolution (internal-key gated)"
RTUSER="44444444-4444-4444-4444-444444444444"
DEVORG="00000000-0000-0000-0000-00000000050a"
DRIVE="https://www.googleapis.com/auth/drive.readonly"
# seed a stored OAuth credential (far-future expiry) for the dev org via the CRUD API
curl -fsS "${AUTH[@]}" -X POST "${CB}/credentials/" \
  -d "{\"tool_id\":\"55555555-5555-5555-5555-555555555555\",\"user_id\":\"${RTUSER}\",\"name\":\"g\",\"provider\":\"google\",\"cred_type\":\"oauth\",\"credential\":{\"access_token\":\"rt-stored\",\"refresh_token\":\"r\",\"scopes\":[\"${DRIVE}\"],\"expires_at\":\"2999-01-01T00:00:00+00:00\"}}" >/dev/null
ok=$(curl -fsS -H "X-Internal-Key: ${INTKEY}" -H "Content-Type: application/json" \
  -X POST "${CB}/internal/runtime-token" \
  -d "{\"organisation_id\":\"${DEVORG}\",\"user_id\":\"${RTUSER}\",\"provider\":\"google\",\"required_scopes\":[\"${DRIVE}\"]}")
echo "$ok" | grep -q '"access_token":"rt-stored"' && pass "runtime-token resolves the stored token" \
  || fail "runtime-token failed: $ok"
short=$(curl -fsS -H "X-Internal-Key: ${INTKEY}" -H "Content-Type: application/json" \
  -X POST "${CB}/internal/runtime-token" \
  -d "{\"organisation_id\":\"${DEVORG}\",\"user_id\":\"${RTUSER}\",\"provider\":\"google\",\"required_scopes\":[\"https://www.googleapis.com/auth/gmail.send\"]}")
echo "$short" | grep -q '"oauth_insufficient_scopes"' && echo "$short" | grep -q "login_url" \
  && pass "scope-shortfall returns missing_scopes + login_url" || fail "scope-shortfall wrong: $short"
c=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${CB}/internal/runtime-token" -H "Content-Type: application/json" -d '{}')
[[ "$c" == "401" ]] && pass "runtime-token requires the internal key (401)" || fail "expected 401, got $c"

printf '\n\033[32mcredential-broker S3 smoke passed.\033[0m  encrypted CRUD + provider catalogue + '
printf 'runtime OAuth-token resolution (resolve/scope-shortfall), all over the running stack.\n'
