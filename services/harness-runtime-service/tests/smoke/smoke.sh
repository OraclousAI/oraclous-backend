#!/usr/bin/env bash
# R4 service — harness-runtime acceptance smoke (slice 1).
# Proves the runtime boots, migrates its own schema, serves /health, and runs a real harness end to
# end: load an OHM → the (fake, key-free) agent tool-use loop dispatches the OHM's PostgreSQL Reader
# capability to the REAL capability-registry execute → real tables come back → a provenance trail is
# written. Runs the full stack in gateway mode (ADR-018), the realistic path; the registry uses the
# REAL credential-broker against the stack's OWN Postgres, so no external API key is needed.
#
# Usage (from repo root):  bash services/harness-runtime-service/tests/smoke/smoke.sh
set -euo pipefail

GW="${HARNESS_SMOKE_GW:-http://localhost:8006}"
HR="${HARNESS_SMOKE_URL:-http://localhost:8007}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
jget() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

# Gateway mode across the stack; the registry resolves credentials through the REAL broker.
export GATEWAY_AUTH_MODE=jwt HARNESS_AUTH_MODE=gateway CAPABILITY_REGISTRY_AUTH_MODE=gateway \
       CRED_BROKER_AUTH_MODE=gateway KGS_AUTH_MODE=gateway KRS_AUTH_MODE=gateway \
       CAPABILITY_REGISTRY_BROKER_MODE=real HARNESS_LLM_MODE=fake

if [[ "${HARNESS_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  step "1. bring up the full stack (gateway mode, real broker) + migrate the harness schema"
  ${COMPOSE} --profile services up -d --build
  ${COMPOSE} up harness-migrate
  ${COMPOSE} up -d harness-runtime-service application-gateway-service
fi

step "2. wait for the harness + gateway to be healthy"
for i in $(seq 1 40); do curl -fsS "${HR}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 40 ]] && fail "harness not healthy: ${HR}"; sleep 2; done
body=$(curl -fsS "${HR}/health")
echo "$body" | grep -q '"status":"ok"' && pass "/health -> ok ($body)" || fail "bad /health: $body"
for i in $(seq 1 30); do curl -fsS "${GW}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "gateway not healthy"; sleep 2; done
pass "gateway healthy"

step "3. the migration created the harness tables (own version_table)"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -c "\dt harness_executions" 2>/dev/null \
  | grep -q harness_executions && pass "harness_executions exists" || fail "harness_executions missing"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -c "\dt alembic_version_harness_runtime" \
  2>/dev/null | grep -q alembic_version_harness_runtime \
  && pass "own alembic version_table (no shared-DB collision)" || fail "version_table missing"

step "4. register a user through the gateway -> JWT"
EMAIL="harness-smoke-$(date +%s)@oraclous.ai"
reg=$(curl -fsS -X POST "${GW}/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"Sup3rSecret!\"}")
TOK=$(echo "$reg" | jget "['access_token']")
[[ -n "$TOK" ]] && pass "registered + got an access token" || fail "register failed: $reg"
AUTH=(-H "Authorization: Bearer ${TOK}" -H "Content-Type: application/json")

step "5. store a PostgreSQL connection_string credential (points at the stack's own Postgres)"
UID=$(curl -fsS "${AUTH[@]}" "${GW}/v1/auth/me" | jget "['id']")
cred=$(curl -fsS "${AUTH[@]}" -X POST "${GW}/credentials/" -d "{\"tool_id\":\"00000000-0000-0000-0000-0000000000a0\",\"user_id\":\"${UID}\",\"name\":\"smoke pg\",\"provider\":\"postgresql\",\"cred_type\":\"raw\",\"credential\":{\"connection_string\":\"postgresql://oraclous:oraclous@postgres:5432/oraclous\"}}")
CRED=$(echo "$cred" | jget "['id']")
[[ -n "$CRED" ]] && pass "stored credential ${CRED}" || fail "credential store failed: $cred"

step "6. run an OHM whose agent calls the real PostgreSQL Reader (fake LLM drives the loop)"
OHM=$(cat <<YAML
ohm_version: "1.0"
metadata:
  id: "01976e3a-7c9b-7b00-9c45-aaaaaaaaaaaa"
  name: "PG Lister"
  owner_organization_id: "01976e3a-0000-7000-9c45-000000000000"
capabilities:
  - ref: "core/postgresql-reader@1.0.0"
    binding: "pg"
    config:
      credential_mappings:
        connection_string: "${CRED}"
models:
  - role: "primary"
    binding: "anthropic/claude-opus-4-8"
    protocol_shape: "native"
prompts:
  - role: "primary"
    source: "inline"
    body: "List the database tables using the available tool, then report them."
runtime:
  entrypoint: "pg"
YAML
)
BODY=$(python3 -c "import json,sys;print(json.dumps({'manifest_yaml':sys.argv[1],'input':'List the tables.'}))" "$OHM")
run=$(curl -fsS "${AUTH[@]}" -X POST "${GW}/v1/harnesses/execute" -d "$BODY")
EXID=$(echo "$run" | jget "['id']")
STATUS=$(echo "$run" | jget "['status']")
[[ "$STATUS" == "SUCCEEDED" ]] && pass "harness execution SUCCEEDED (${EXID})" || fail "status=$STATUS: $run"
echo "$run" | python3 -c "import sys,json;s=json.load(sys.stdin)['steps'];assert any(x['kind']=='tool' and x['name'].startswith('pg.') and x['status']=='ok' for x in s),s" \
  && pass "trace shows a successful pg.* tool dispatch" || fail "no successful tool step: $run"
echo "$run" | grep -q 'capability_descriptors' \
  && pass "the agent observed REAL tables (capability_descriptors among them)" \
  || fail "no real tables in the output: $run"

step "7. GET the execution back (org-scoped)"
got=$(curl -fsS "${AUTH[@]}" "${GW}/v1/harnesses/executions/${EXID}")
echo "$got" | grep -q "\"id\":\"${EXID}\"" && pass "GET /executions/{id} returns the run" || fail "GET wrong: $got"

step "8. a provenance trail was written (one closure event + per-step events)"
n=$(${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -tAc \
  "select count(*) from harness_provenance where resource = 'harness_execution:${EXID}'")
[[ "${n//[[:space:]]/}" -ge 2 ]] && pass "harness_provenance has ${n//[[:space:]]/} events for this run" \
  || fail "expected >=2 provenance events, got ${n}"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -tAc \
  "select count(*) from harness_provenance where resource = 'harness_execution:${EXID}' and action='harness.execute'" \
  | grep -q 1 && pass "closure event (action=harness.execute) present" || fail "no closure event"

step "9. auth boundary: execute without a token -> 401 at the edge"
c=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${GW}/v1/harnesses/execute" \
  -H 'Content-Type: application/json' -d '{"manifest":{},"input":"x"}')
[[ "$c" == "401" ]] && pass "unauthenticated execute -> 401" || fail "expected 401, got $c"

printf '\n\033[32mAll harness-runtime slice-1 smoke checks passed.\033[0m\n'
