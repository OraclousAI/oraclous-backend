#!/usr/bin/env bash
# R3.5 service #5 — S5a REAL credential-broker integration smoke (docker-required, §9.3).
# Proves the cross-service contract end-to-end: a connection_string credential is stored in the REAL
# credential-broker, the capability-registry runs with CREDENTIAL_BROKER_MODE=real, and executing a
# PostgreSQL Reader instance resolves that secret over the broker's /internal/resolve-credential
# (X-Internal-Key) and runs a real query. Key-free (the connection_string targets the dev Postgres).
#
# Usage (from repo root):  bash services/capability-registry-service/tests/smoke/smoke_real_broker.sh
set -euo pipefail

CR="${CR_SMOKE_URL:-http://localhost:8001}"
CB="${CB_SMOKE_URL:-http://localhost:8002}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
jget() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

DEVORG="00000000-0000-0000-0000-00000000050a"
PG_DSN="postgresql://oraclous:oraclous@postgres:5432/oraclous"
AUTH=(-H "Authorization: Bearer dev-token" -H "Content-Type: application/json")
INTKEY="${CRED_BROKER_INTERNAL_KEY:-dev-internal-key}"

step "1. bring up postgres + the REAL credential-broker + capability-registry (broker mode=real)"
${COMPOSE} up -d --build postgres
${COMPOSE} build credential-broker-service capability-registry-service
${COMPOSE} up credbroker-migrate capreg-migrate
${COMPOSE} up -d credential-broker-service
CAPABILITY_REGISTRY_BROKER_MODE=real ${COMPOSE} up -d capability-registry-service

step "2. wait for both healthy"
for i in $(seq 1 30); do curl -fsS "${CB}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "broker not healthy"; sleep 2; done
for i in $(seq 1 30); do curl -fsS "${CR}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "registry not healthy"; sleep 2; done
pass "credential-broker + capability-registry are healthy"

step "3. store a connection_string credential in the REAL broker"
cred=$(curl -fsS "${AUTH[@]}" -X POST "${CB}/credentials/" -d "{
  \"tool_id\":\"$(uuidgen)\",\"user_id\":\"$(uuidgen)\",\"name\":\"pg\",\"provider\":\"postgresql\",
  \"cred_type\":\"raw\",\"credential\":{\"connection_string\":\"${PG_DSN}\"}}")
cid=$(echo "$cred" | jget "['id']")
[[ -n "$cid" ]] && pass "stored connection_string credential ${cid}" || fail "credential create failed: $cred"

step "4. resolve it over the internal endpoint (the contract capability-registry uses)"
res=$(curl -fsS -H "X-Internal-Key: ${INTKEY}" -H "Content-Type: application/json" \
  -X POST "${CB}/internal/resolve-credential" -d "{\"organisation_id\":\"${DEVORG}\",\"credential_id\":\"${cid}\"}")
echo "$res" | grep -q "${PG_DSN}" && pass "broker /internal/resolve-credential returns the decrypted DSN" \
  || fail "resolve-credential wrong: $res"

step "5. execute a PostgreSQL Reader instance THROUGH the real broker (mode=real)"
pgid=$(curl -fsS "${AUTH[@]}" -X GET "${CR}/api/v1/tools" \
  | python3 -c "import sys,json;ts=json.load(sys.stdin)['capabilities'];print(next(t['id'] for t in ts if t['name']=='PostgreSQL Reader'))")
iid=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances" -d "{\"capability_id\":\"${pgid}\",\"name\":\"pg-real\"}" | jget "['id']")
# map the instance's connection_string to the broker credential id
curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances/${iid}/configure-credentials" \
  -d "{\"credential_mappings\":{\"connection_string\":\"${cid}\"}}" >/dev/null
out=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances/${iid}/execute" \
  -d '{"input_data":{"operation":"list_tables"}}')
echo "$out" | grep -q '"status":"SUCCESS"' \
  && pass "execution SUCCESS — secret resolved via the REAL broker, real query ran" || fail "exec failed: $out"
echo "$out" | grep -q 'capability_descriptors' && pass "real rows returned over the real-broker path" \
  || fail "no real rows: $out"
echo "$out" | grep -q "${PG_DSN}" && fail "SECRET LEAK: the DSN appears in the execution output" \
  || pass "no resolved secret echoed in the execution output"

printf '\n\033[32mS5a real-broker integration smoke passed.\033[0m  capability-registry resolved a '
printf 'stored credential over the credential-broker /internal/resolve-credential contract and '
printf 'executed a real PostgreSQL query — the cross-service seam works end-to-end.\n'
