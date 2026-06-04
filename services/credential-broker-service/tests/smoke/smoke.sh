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

printf '\n\033[32mcredential-broker S0 smoke passed.\033[0m  boots + migrates its own schema + serves /health.\n'
