#!/usr/bin/env bash
# R5 service — execution-engine acceptance smoke (slice 4 — task board + resume).
# Proves the engine boots, migrates its own schema (own version_table — no shared-DB collision),
# serves /health, and runs durable harness jobs ASYNC THROUGH THE GATEWAY: submit returns 202 +
# QUEUED, a Celery worker calls the harness /execute over HTTP → the engine checkpoints the terminal
# state + writes provenance; poll GET /jobs/{id} for the outcome. Covers a human-actor OHM (→
# ESCALATED + a captured assignment), the PostgreSQL-Reader OHM (→ SUCCEEDED + harness_execution_id,
# real tables), and cancel (ESCALATED → CANCELLED). Full stack in gateway mode (ADR-018).
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
  step "1. bring up the full stack (gateway mode, real broker) + the engine worker"
  ${COMPOSE} --profile services up -d --build
  ${COMPOSE} up harness-migrate engine-migrate
  ${COMPOSE} up -d harness-runtime-service execution-engine-service execution-engine-worker \
    application-gateway-service
fi

# Submit a job (202 + QUEUED) and poll until it reaches the wanted state; fail fast on a wrong terminal.
job_state() { curl -fsS "${GW}/v1/engine/jobs/$1" "${AUTH[@]:-}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])'; }
poll_job() {  # $1=job_id $2=wanted_state
  for _ in $(seq 1 40); do
    s=$(job_state "$1")
    [[ "$s" == "$2" ]] && return 0
    case "$s" in SUCCEEDED|FAILED|ESCALATED|TIMED_OUT|CANCELLED) return 1 ;; esac
    sleep 2
  done; return 1
}

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

step "5. submit a HUMAN-actor OHM job (202 + QUEUED) -> the worker drives it to ESCALATED"
HUMAN=$(python3 -c "import json,sys;print(json.dumps({'manifest':{'ohm_version':'1.0','metadata':{'id':'01976e3a-7c9b-7b00-9c45-aaa000000001','name':'Eng Review','owner_organization_id':sys.argv[1]},'capabilities':[],'models':[{'role':'primary','binding':'openrouter/x','protocol_shape':'openai-compatible'}],'prompts':[{'role':'primary','source':'inline','body':'r'}],'actors':[{'role':'reviewer','kind':'human','human_role':'admin'}],'runtime':{'entrypoint':'reviewer'}},'input':'review via engine'}))" "$ORG")
hb=$(curl -fsS -o /dev/null -w '%{http_code}' "${GW}/v1/engine/jobs" "${AUTH[@]}" -d "$HUMAN")
[[ "$hb" == "202" ]] && pass "submit -> 202 (accepted, async)" || fail "expected 202, got $hb"
HJID=$(curl -fsS "${GW}/v1/engine/jobs" "${AUTH[@]}" -d "$HUMAN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
poll_job "$HJID" ESCALATED && pass "worker ran it -> ESCALATED" || fail "human job did not reach ESCALATED"
[[ "$(curl -fsS "${GW}/v1/engine/jobs/${HJID}" "${AUTH[@]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["assignment_id"] or "")')" != "" ]] \
  && pass "assignment captured" || fail "no assignment_id"

step "6. submit the PostgreSQL-Reader OHM job -> the worker drives it to SUCCEEDED (real tables)"
CRED=$(curl -fsS "${AUTH[@]}" -X POST "${GW}/credentials/" -d "{\"tool_id\":\"00000000-0000-0000-0000-0000000000a0\",\"user_id\":\"${SUB}\",\"name\":\"pg\",\"provider\":\"postgresql\",\"cred_type\":\"raw\",\"credential\":{\"connection_string\":\"postgresql://oraclous:oraclous@postgres:5432/oraclous\"}}" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
PG=$(python3 -c "import json,sys;print(json.dumps({'manifest':{'ohm_version':'1.0','metadata':{'id':'01976e3a-7c9b-7b00-9c45-aaa000000002','name':'Eng PG','owner_organization_id':sys.argv[1]},'capabilities':[{'ref':'core/postgresql-reader@1.0.0','binding':'pg','config':{'credential_mappings':{'connection_string':sys.argv[2]}}}],'models':[{'role':'primary','binding':'openrouter/x','protocol_shape':'openai-compatible'}],'prompts':[{'role':'primary','source':'inline','body':'List the tables using the tool.'}],'runtime':{'entrypoint':'pg'}},'input':'tables?'}))" "$ORG" "$CRED")
JID=$(curl -fsS "${GW}/v1/engine/jobs" "${AUTH[@]}" -d "$PG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
poll_job "$JID" SUCCEEDED && pass "worker ran it -> SUCCEEDED" || fail "PG job did not reach SUCCEEDED"
[[ "$(curl -fsS "${GW}/v1/engine/jobs/${JID}" "${AUTH[@]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["harness_execution_id"] or "")')" != "" ]] \
  && pass "harness_execution_id set" || fail "no harness_execution_id"

step "6b. S4: the task board lists the ESCALATED human job; completing it flips it SUCCEEDED"
TB=$(python3 -c "import json,sys;print(json.dumps({'manifest':{'ohm_version':'1.0','metadata':{'id':'01976e3a-7c9b-7b00-9c45-aaa000000004','name':'Eng Task','owner_organization_id':sys.argv[1]},'capabilities':[],'models':[{'role':'primary','binding':'openrouter/x','protocol_shape':'openai-compatible'}],'prompts':[{'role':'primary','source':'inline','body':'r'}],'actors':[{'role':'reviewer','kind':'human','human_role':'admin'}],'runtime':{'entrypoint':'reviewer'}},'input':'review'}))" "$ORG")
TJID=$(curl -fsS "${GW}/v1/engine/jobs" "${AUTH[@]}" -d "$TB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
poll_job "$TJID" ESCALATED && pass "task job -> ESCALATED" || fail "task job did not reach ESCALATED"
curl -fsS "${AUTH[@]}" "${GW}/v1/engine/tasks" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);assert any(t['id']=='${TJID}' for t in d['tasks']),d" \
  && pass "GET /tasks lists the ESCALATED job" || fail "task not on the board"
curl -fsS "${AUTH[@]}" -X POST "${GW}/v1/engine/tasks/${TJID}/complete" -d '{"output":"approved by human"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);assert d['state']=='SUCCEEDED' and d['output']=='approved by human',d" \
  && pass "complete -> the engine job is SUCCEEDED with the human output" || fail "task complete failed"

step "7. S3: a failing job with max_retries=2 retries then ends FAILED with retry_count=2"
# an OHM whose runtime.entrypoint matches no capability binding -> the harness rejects it (422) ->
# the engine marks the attempt FAILED and re-queues until the retry budget is spent.
BAD=$(python3 -c "import json,sys;print(json.dumps({'manifest':{'ohm_version':'1.0','metadata':{'id':'01976e3a-7c9b-7b00-9c45-aaa000000003','name':'Eng Bad','owner_organization_id':sys.argv[1]},'capabilities':[{'ref':'core/echo@1.0.0','binding':'echo'}],'models':[{'role':'primary','binding':'openrouter/x','protocol_shape':'openai-compatible'}],'prompts':[{'role':'primary','source':'inline','body':'x'}],'runtime':{'entrypoint':'does-not-exist'}},'input':'x','max_retries':2}))" "$ORG")
BJID=$(curl -fsS "${GW}/v1/engine/jobs" "${AUTH[@]}" -d "$BAD" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
poll_job "$BJID" FAILED && pass "exhausted retries -> FAILED" || fail "bad job did not reach FAILED"
[[ "$(curl -fsS "${GW}/v1/engine/jobs/${BJID}" "${AUTH[@]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["retry_count"])')" == "2" ]] \
  && pass "retry_count == 2 (retries were attempted)" || fail "retry_count wrong"

step "8. cancel the (ESCALATED) human job -> CANCELLED"
curl -fsS "${GW}/v1/engine/jobs/${HJID}/cancel" "${AUTH[@]}" -X POST \
  | grep -q '"state":"CANCELLED"' && pass "cancel -> CANCELLED" || fail "cancel failed"

step "9. read surfaces + edge auth"
[[ "$(curl -s -o /dev/null -w '%{http_code}' "${GW}/v1/engine/jobs/${JID}" "${AUTH[@]}")" == "200" ]] \
  && pass "GET /jobs/{id} -> 200" || fail "GET by id failed"
curl -fsS "${GW}/v1/engine/jobs" "${AUTH[@]}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);assert d['total']>=2,d" \
  && pass "GET /jobs lists the org's jobs" || fail "list failed"
[[ "$(curl -s -o /dev/null -w '%{http_code}' "${GW}/v1/engine/jobs")" == "401" ]] \
  && pass "no-auth -> 401 (edge-gated)" || fail "expected 401"

step "10. provenance was written for the job (submit + run)"
c=$(${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -tAc \
  "select count(*) from engine_provenance where resource='engine_job:${JID}' and action in ('engine.job.submit','engine.job.run')")
[[ "${c//[[:space:]]/}" -ge 2 ]] && pass "engine.job.submit + run provenance recorded" || fail "no provenance"

printf '\n\033[32mAll execution-engine slice-4 smoke checks passed.\033[0m\n'
