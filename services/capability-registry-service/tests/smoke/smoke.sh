#!/usr/bin/env bash
# R3.5 service #5 — capability-registry acceptance smoke.
# Proves the service boots, migrates its own schema (own alembic version_table), serves /health, and
# runs real org-scoped capability-descriptor CRUD + capability matching over the running stack.
# Key-free: the dev INTERNAL_SERVICE_KEY + dev bearer are baked into the compose dev values.
#
# Usage (from repo root):  bash services/capability-registry-service/tests/smoke/smoke.sh
set -euo pipefail

CR="${CR_SMOKE_URL:-http://localhost:8001}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
jget() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

if [[ "${CR_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  step "1. bring up postgres + migrate + the capability-registry"
  ${COMPOSE} up -d --build postgres
  ${COMPOSE} build capability-registry-service
  ${COMPOSE} up capreg-migrate
  ${COMPOSE} up -d capability-registry-service
fi

step "2. wait for healthy"
for i in $(seq 1 30); do curl -fsS "${CR}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "not healthy: ${CR}"; sleep 2; done
body=$(curl -fsS "${CR}/health")
echo "$body" | grep -q '"status":"healthy"' && pass "/health -> healthy ($body)" \
  || fail "unexpected /health: $body"

step "3. the migration created the registry table (own version_table)"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -c "\dt capability_descriptors" 2>/dev/null \
  | grep -q capability_descriptors && pass "capability_descriptors table exists" \
  || fail "capability_descriptors missing"
${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -c "\dt alembic_version_capability_registry" \
  2>/dev/null | grep -q alembic_version_capability_registry \
  && pass "own alembic version_table (no shared-DB collision)" || fail "version_table missing"

step "4. S1: org-scoped capability descriptor CRUD (dev-auth bearer binds the org)"
AUTH=(-H "Authorization: Bearer dev-token" -H "Content-Type: application/json")
DESC='{"kind":"tool","descriptor":{"kind":"tool","metadata":{"name":"Smoke Drive Reader","category":"INGESTION"},"spec":{"type":"INTERNAL","capabilities":[{"name":"read_drive_files","description":"x"}],"credential_requirements":[{"type":"oauth_token","provider":"google","scopes":["drive.readonly"]}]}}}'
created=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/capabilities" -d "${DESC}")
cid=$(echo "$created" | jget "['id']")
chash=$(echo "$created" | jget "['content_hash']")
[[ -n "$cid" ]] && pass "registered descriptor ${cid}" || fail "register failed: $created"
[[ ${#chash} -eq 64 ]] && pass "server computed a 64-char content_hash" || fail "bad hash: $chash"
got=$(curl -fsS "${AUTH[@]}" -X GET "${CR}/api/v1/capabilities/${cid}")
echo "$got" | grep -q "$chash" && pass "GET returns the descriptor with its hash" || fail "GET wrong: $got"

step "5. S1: capability matching + descriptor search"
m=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/capabilities/match" -d '{"capabilities":["read_drive_files"]}')
echo "$m" | grep -q '"Smoke Drive Reader"' && pass "match by capability name finds the tool" \
  || fail "match wrong: $m"

step "6. S1: validation + auth boundaries"
# malformed: oauth requirement with empty scopes -> 422
BAD='{"kind":"tool","descriptor":{"kind":"tool","metadata":{"name":"bad"},"spec":{"type":"INTERNAL","capabilities":[],"credential_requirements":[{"type":"oauth_token","provider":"google","scopes":[]}]}}}'
c=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X POST "${CR}/api/v1/capabilities" -d "${BAD}")
[[ "$c" == "422" ]] && pass "oauth-without-scopes -> 422 (fail-closed validation)" || fail "expected 422, got $c"
# no bearer -> 401
c=$(curl -s -o /dev/null -w '%{http_code}' -X GET "${CR}/api/v1/capabilities")
[[ "$c" == "401" ]] && pass "no bearer -> 401" || fail "expected 401, got $c"
# unknown id -> 404 (org-scoped mask)
c=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X GET "${CR}/api/v1/capabilities/33333333-3333-3333-3333-333333333333")
[[ "$c" == "404" ]] && pass "unknown id -> 404 (org-scoped)" || fail "expected 404, got $c"

step "7. S2: startup plugin discovery seeded the built-in tool catalogue"
tools=$(curl -fsS "${AUTH[@]}" -X GET "${CR}/api/v1/tools")
echo "$tools" | grep -q '"PostgreSQL Reader"' && echo "$tools" | grep -q '"Google Drive Reader"' \
  && pass "GET /api/v1/tools lists the seeded connector tools" || fail "tools not seeded: $tools"
total=$(echo "$tools" | jget "['total']")
[[ "$total" -ge 5 ]] && pass "catalogue has >=5 built-in tools (total=$total)" || fail "too few tools: $total"

step "8. S2: register a tool -> deterministic id, idempotent re-register"
TDESC='{"descriptor":{"kind":"tool","metadata":{"name":"Smoke Echo Tool","category":"UTILITY"},"version":{"semver":"1.0.0"},"spec":{"type":"INTERNAL","capabilities":[{"name":"echo","description":"echo"}],"credential_requirements":[]}}}'
t1=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/tools" -d "${TDESC}" | jget "['id']")
t2=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/tools" -d "${TDESC}" | jget "['id']")
[[ -n "$t1" && "$t1" == "$t2" ]] && pass "re-register yields the same deterministic id ($t1)" \
  || fail "tool ids not deterministic: $t1 vs $t2"

step "9. S3: create a tool instance -> CONFIGURATION_REQUIRED (oauth credential not mapped)"
gid=$(curl -fsS "${AUTH[@]}" -X GET "${CR}/api/v1/tools" \
  | python3 -c "import sys,json;ts=json.load(sys.stdin)['capabilities'];print(next(t['id'] for t in ts if t['name']=='Google Drive Reader'))")
[[ -n "$gid" ]] && pass "resolved seeded Google Drive Reader id ($gid)" || fail "drive tool not found"
inst=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances" \
  -d "{\"capability_id\":\"${gid}\",\"name\":\"my drive\"}")
iid=$(echo "$inst" | jget "['id']"); istatus=$(echo "$inst" | jget "['status']")
[[ "$istatus" == "CONFIGURATION_REQUIRED" ]] && pass "new instance is CONFIGURATION_REQUIRED" \
  || fail "unexpected instance status: $istatus"
rep=$(curl -fsS "${AUTH[@]}" -X GET "${CR}/api/v1/instances/${iid}/validate-execution")
echo "$rep" | grep -q '"is_ready":false' && echo "$rep" | grep -q '"oauth_token"' \
  && pass "validate-execution reports the missing oauth credential" || fail "validate wrong: $rep"

step "10. S3: configure credentials -> READY -> validate is_ready"
conf=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances/${iid}/configure-credentials" \
  -d '{"credential_mappings":{"oauth_token":"cred-smoke-123"}}')
echo "$conf" | grep -q '"status":"READY"' && pass "instance is READY after mapping the credential" \
  || fail "configure didn't make it READY: $conf"
rep2=$(curl -fsS "${AUTH[@]}" -X GET "${CR}/api/v1/instances/${iid}/validate-execution")
echo "$rep2" | grep -q '"is_ready":true' && pass "validate-execution is_ready=true" \
  || fail "still not ready: $rep2"

step "11. S4: execute a PostgreSQL Reader instance end-to-end (fake broker -> real query)"
pgid=$(curl -fsS "${AUTH[@]}" -X GET "${CR}/api/v1/tools" \
  | python3 -c "import sys,json;ts=json.load(sys.stdin)['capabilities'];print(next(t['id'] for t in ts if t['name']=='PostgreSQL Reader'))")
piid=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances" \
  -d "{\"capability_id\":\"${pgid}\",\"name\":\"pg\"}" | jget "['id']")
curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances/${piid}/configure-credentials" \
  -d '{"credential_mappings":{"connection_string":"cred-smoke"}}' >/dev/null
exec_out=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances/${piid}/execute" \
  -d '{"input_data":{"operation":"list_tables"}}')
echo "$exec_out" | grep -q '"status":"SUCCESS"' && pass "execution SUCCESS (real list_tables over the DB)" \
  || fail "execution failed: $exec_out"
echo "$exec_out" | grep -q 'capability_descriptors' \
  && pass "real rows returned (capability_descriptors among the tables)" || fail "no real rows: $exec_out"
echo "$exec_out" | grep -q '"type":"connection_string"' \
  && pass "executions row records credential_refs (type only)" || fail "no credential_refs: $exec_out"
echo "$exec_out" | grep -q 'postgresql://' && fail "SECRET LEAK: a DSN appears in the execution output" \
  || pass "no resolved secret (DSN) echoed in the execution output"

step "12. S4: a tool without an executor is not executable (409)"
nid=$(curl -fsS "${AUTH[@]}" -X GET "${CR}/api/v1/tools" \
  | python3 -c "import sys,json;ts=json.load(sys.stdin)['capabilities'];print(next(t['id'] for t in ts if t['name']=='Notion Reader'))")
niid=$(curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances" -d "{\"capability_id\":\"${nid}\",\"name\":\"n\"}" | jget "['id']")
curl -fsS "${AUTH[@]}" -X POST "${CR}/api/v1/instances/${niid}/configure-credentials" -d '{"credential_mappings":{"api_key":"k"}}' >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X POST "${CR}/api/v1/instances/${niid}/execute" -d '{"input_data":{}}')
[[ "$code" == "409" ]] && pass "no-executor tool -> 409 (fail-closed, no silent no-op)" || fail "expected 409, got $code"

printf '\n\033[32mcapability-registry S1-S4 smoke passed.\033[0m  boot + migrate + descriptor CRUD + '
printf 'matching + validation + plugin catalogue + instance lifecycle + REAL sync execution '
printf '(PostgreSQL connector via the credential-broker seam, provenance, no secret leak), over the stack.\n'
