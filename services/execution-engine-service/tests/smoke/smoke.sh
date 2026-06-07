#!/usr/bin/env bash
# R5 service — execution-engine acceptance smoke (slice 1).
# Proves the engine boots, migrates its own schema (own version_table — no shared-DB collision),
# serves /health, and runs a durable harness job END TO END THROUGH THE GATEWAY: submit → the engine
# calls the harness /execute over HTTP → the harness runs the OHM → the engine checkpoints the
# terminal state + writes provenance. Two paths: a human-actor OHM (→ ESCALATED + a captured
# assignment, no creds) and the PostgreSQL-Reader OHM (→ SUCCEEDED + a harness_execution_id, real
# tables). Full stack in gateway mode (ADR-018), the realistic path.
#
# Usage (from repo root):  bash services/execution-engine-service/tests/smoke/smoke.sh
set -euo pipefail

GW="${ENGINE_SMOKE_GW:-http://localhost:8006}"
EE="${ENGINE_SMOKE_URL:-http://localhost:8008}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# Gateway mode across the stack; the registry resolves credentials through the REAL broker so the
# PostgreSQL Reader runs against the stack's own Postgres (no external API key needed).
export GATEWAY_AUTH_MODE=jwt ENGINE_AUTH_MODE=gateway HARNESS_AUTH_MODE=gateway \
       CAPABILITY_REGISTRY_AUTH_MODE=gateway CRED_BROKER_AUTH_MODE=gateway \
       KGS_AUTH_MODE=gateway KRS_AUTH_MODE=gateway \
       CAPABILITY_REGISTRY_BROKER_MODE=real HARNESS_LLM_MODE=fake

if [[ "${ENGINE_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  step "1. bring up the full stack (gateway mode, real broker)"
  ${COMPOSE} --profile services up -d --build
  ${COMPOSE} up harness-migrate engine-migrate
  ${COMPOSE} up -d harness-runtime-service execution-engine-service application-gateway-service
fi

step "2. wait for the engine + gateway to be healthy"
for i in $(seq 1 40); do curl -fsS "${EE}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 40 ]] && fail "engine not healthy: ${EE}"; sleep 2; done
curl -fsS "${EE}/health" | grep -q '"status":"ok"' && pass "/health -> ok" || fail "bad /health"
for i in $(seq 1 30); do curl -fsS "${GW}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "gateway not healthy"; sleep 2; done
pass "gateway healthy"

step "3. the migration created the engine tables (own version_table)"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -c "\dt engine_jobs" 2>/dev/null \
  | grep -q engine_jobs && pass "engine_jobs exists" || fail "engine_jobs missing"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -c "\dt alembic_version_execution_engine" \
  2>/dev/null | grep -q alembic_version_execution_engine \
  && pass "own alembic version_table (no shared-DB collision)" || fail "version_table missing"

step "4. register a user through the gateway -> JWT"
EMAIL="engine-smoke-$(date +%s)@oraclous.ai"
TOK=$(curl -fsS "${GW}/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"Sup3rSecret!\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
read -r SUB ORG < <(python3 -c "import base64,json,sys;p=sys.argv[1].split('.')[1];p+='='*(-len(p)%4);c=json.loads(base64.urlsafe_b64decode(p));print(c['sub'],c['organisation_id'])" "$TOK")
AUTH=(-H "Authorization: Bearer ${TOK}" -H "Content-Type: application/json")
pass "registered ${EMAIL}"

step "5. a HUMAN-actor OHM job -> the engine job ESCALATED with a captured assignment"
HUMAN=$(python3 -c "import json,sys;print(json.dumps({'manifest':{'ohm_version':'1.0','metadata':{'id':'01976e3a-7c9b-7b00-9c45-aaa000000001','name':'Eng Review','owner_organization_id':sys.argv[1]},'capabilities':[],'models':[{'role':'primary','binding':'openrouter/x','protocol_shape':'openai-compatible'}],'prompts':[{'role':'primary','source':'inline','body':'r'}],'actors':[{'role':'reviewer','kind':'human','human_role':'admin'}],'runtime':{'entrypoint':'reviewer'}},'input':'review via engine'}))" "$ORG")
hb=$(curl -fsS "${GW}/v1/engine/jobs" "${AUTH[@]}" -d "$HUMAN")
echo "$hb" | grep -q '"state":"ESCALATED"' && [[ "$(echo "$hb" | python3 -c 'import sys,json;print(json.load(sys.stdin)["assignment_id"] or "")')" != "" ]] \
  && pass "human job -> ESCALATED + assignment captured" || fail "human job: $hb"

step "6. the PostgreSQL-Reader OHM job -> SUCCEEDED + a harness_execution_id (real tables)"
CRED=$(curl -fsS "${AUTH[@]}" -X POST "${GW}/credentials/" -d "{\"tool_id\":\"00000000-0000-0000-0000-0000000000a0\",\"user_id\":\"${SUB}\",\"name\":\"pg\",\"provider\":\"postgresql\",\"cred_type\":\"raw\",\"credential\":{\"connection_string\":\"postgresql://oraclous:oraclous@postgres:5432/oraclous\"}}" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
PG=$(python3 -c "import json,sys;print(json.dumps({'manifest':{'ohm_version':'1.0','metadata':{'id':'01976e3a-7c9b-7b00-9c45-aaa000000002','name':'Eng PG','owner_organization_id':sys.argv[1]},'capabilities':[{'ref':'core/postgresql-reader@1.0.0','binding':'pg','config':{'credential_mappings':{'connection_string':sys.argv[2]}}}],'models':[{'role':'primary','binding':'openrouter/x','protocol_shape':'openai-compatible'}],'prompts':[{'role':'primary','source':'inline','body':'List the tables using the tool.'}],'runtime':{'entrypoint':'pg'}},'input':'tables?'}))" "$ORG" "$CRED")
pb=$(curl -fsS "${GW}/v1/engine/jobs" "${AUTH[@]}" -d "$PG")
echo "$pb" | grep -q '"state":"SUCCEEDED"' && [[ "$(echo "$pb" | python3 -c 'import sys,json;print(json.load(sys.stdin)["harness_execution_id"] or "")')" != "" ]] \
  && pass "PG job -> SUCCEEDED + harness_execution_id" || fail "PG job: $pb"
JID=$(echo "$pb" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

step "7. read surfaces + edge auth"
[[ "$(curl -s -o /dev/null -w '%{http_code}' "${GW}/v1/engine/jobs/${JID}" "${AUTH[@]}")" == "200" ]] \
  && pass "GET /jobs/{id} -> 200" || fail "GET by id failed"
curl -fsS "${GW}/v1/engine/jobs" "${AUTH[@]}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);assert d['total']>=2,d" \
  && pass "GET /jobs lists the org's jobs" || fail "list failed"
[[ "$(curl -s -o /dev/null -w '%{http_code}' "${GW}/v1/engine/jobs")" == "401" ]] \
  && pass "no-auth -> 401 (edge-gated)" || fail "expected 401"

step "8. provenance was written for the job"
c=$(${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -tAc \
  "select count(*) from engine_provenance where resource='engine_job:${JID}' and action='engine.job.run'")
[[ "${c//[[:space:]]/}" -ge 1 ]] && pass "engine.job.run provenance recorded" || fail "no provenance"

printf '\n\033[32mAll execution-engine slice-1 smoke checks passed.\033[0m\n'
