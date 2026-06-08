#!/usr/bin/env bash
# R3.5 service #6 — application-gateway acceptance smoke.
# GW-1: the gateway boots as a real §21 service and serves /health.
# GW-2: it reverse-proxies a routed request to a real upstream (capability-registry), streams a
#       successful response through, normalises an upstream error into the ORA-37 envelope, 404s an
#       unknown prefix, and 502s when the upstream is down.
#
# Usage (from repo root):  bash services/application-gateway-service/tests/smoke/smoke.sh
set -euo pipefail

GW="${GW_SMOKE_URL:-http://localhost:8006}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

if [[ "${GW_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  step "1. build + bring up two real upstreams (capability-registry + credential-broker) + the gateway"
  ${COMPOSE} up -d --build postgres
  ${COMPOSE} build capability-registry-service credential-broker-service application-gateway-service
  ${COMPOSE} up capreg-migrate credbroker-migrate
  # bring the gateway up with a small body cap (1 KiB) so the GW-7 size-guard checks are cheap; the
  # gateway's depends_on pulls Redis up for the edge rate limiter.
  MAX_REQUEST_BODY_BYTES=1024 ${COMPOSE} up -d \
    capability-registry-service credential-broker-service application-gateway-service
fi

step "2. wait for healthy"
for i in $(seq 1 30); do curl -fsS "${GW}/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "not healthy: ${GW}"; sleep 2; done
body=$(curl -fsS "${GW}/health")
echo "$body" | grep -q '"status":"ok"' && pass "/health -> ok ($body)" || fail "unexpected /health: $body"
echo "$body" | grep -q '"service":"application-gateway"' && pass "identifies as application-gateway" \
  || fail "wrong service id: $body"

step "2b. wait for the upstream (capability-registry) to be ready"
for i in $(seq 1 30); do curl -fsS "http://localhost:8001/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "upstream capability-registry not healthy"; sleep 2; done
pass "capability-registry upstream is healthy"

step "3. GW-2: forward a routed request to the real upstream (capability-registry)"
# /api/v1/tools routes to capability-registry; with the dev bearer it returns the seeded catalogue.
tools=$(curl -fsS -H "Authorization: Bearer dev-token" "${GW}/api/v1/tools")
echo "$tools" | grep -q '"PostgreSQL Reader"' \
  && pass "gateway forwarded /api/v1/tools -> capability-registry returned real data (catalogue)" \
  || fail "forward returned no real data: $tools"

step "4. GW-3: edge JWT termination rejects unauthenticated requests (before any upstream call)"
code=$(curl -s -o /dev/null -w '%{http_code}' "${GW}/api/v1/tools")  # no bearer
[[ "$code" == "401" ]] && pass "no bearer -> gateway 401 (edge auth)" || fail "expected 401, got $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer bogus" "${GW}/api/v1/tools")
[[ "$code" == "401" ]] && pass "invalid bearer -> gateway 401" || fail "expected 401, got $code"

step "5. GW-2: unknown prefix is a gateway 404 (closed allow-list, not forwarded)"
nope=$(curl -s -w '\n%{http_code}' -H "Authorization: Bearer dev-token" "${GW}/totally/unknown")
echo "$nope" | grep -q '"code":"NOT_FOUND"' && echo "$nope" | tail -1 | grep -q 404 \
  && pass "unknown prefix -> gateway 404 NOT_FOUND envelope" || fail "expected gateway 404: $nope"

step "6. GW-4: route to a SECOND real upstream (credential-broker) — multi-upstream live"
for i in $(seq 1 30); do curl -fsS "http://localhost:8002/health" >/dev/null 2>&1 && break; \
  [[ $i -eq 30 ]] && fail "credential-broker upstream not healthy"; sleep 2; done
provs=$(curl -fsS -H "Authorization: Bearer dev-token" "${GW}/credentials/providers?user_id=00000000-0000-0000-0000-0000000000c5")
echo "$provs" | grep -q '"providers"' \
  && pass "gateway routed /credentials/* -> credential-broker (2nd live upstream)" \
  || fail "credential-broker route failed: $provs"

step "7. GW-4: CORS preflight is answered at the edge"
hdrs=$(curl -s -D - -o /dev/null -X OPTIONS \
  -H "Origin: https://app.test" -H "Access-Control-Request-Method: GET" "${GW}/api/v1/tools")
echo "$hdrs" | tr -d '\r' | grep -qi '^access-control-allow-origin:' \
  && pass "OPTIONS preflight -> access-control-allow-origin header set by the edge" \
  || fail "no CORS header on preflight: $hdrs"

step "8. GW-4: the platform-internal /internal plane is NOT edge-routed"
nope=$(curl -s -w '\n%{http_code}' -H "Authorization: Bearer dev-token" "${GW}/internal/agent-credentials")
echo "$nope" | grep -q '"code":"NOT_FOUND"' && echo "$nope" | tail -1 | grep -q 404 \
  && pass "/internal/* -> gateway 404 (never forwarded)" || fail "expected gateway 404: $nope"

step "9. GW-5: aggregated upstream health (/health/upstreams rolls up per-service + overall)"
agg=$(curl -fsS "${GW}/health/upstreams")
echo "$agg" | python3 -c "
import sys,json
d=json.load(sys.stdin); ups={u['name']:u['status'] for u in d['upstreams']}
assert len(d['upstreams'])==5, d
# the two upstreams this smoke launched must be ok
assert ups['capability-registry']=='ok', ups
assert ups['credential-broker']=='ok', ups
# overall is a correct rollup of the per-service statuses (ok iff every upstream is ok)
expected = 'ok' if all(s=='ok' for s in ups.values()) else 'degraded'
assert d['overall']==expected, d
print(f'  rollup consistent: overall={d[\"overall\"]} statuses={ups}')
" && pass "/health/upstreams aggregates per-service health + consistent overall rollup (HTTP 200)" \
  || fail "aggregated health wrong: $agg"

step "10. GW-5: the gateway own-error is the canonical ORA-37 envelope"
hdrs=$(curl -s -D - -o /tmp/gw_404.json -H "Authorization: Bearer dev-token" "${GW}/totally/unknown")
python3 -c "
import json, re
d = json.load(open('/tmp/gw_404.json'))['error']
assert d['code'] == 'NOT_FOUND', d
assert d['message'] and d['retryable'] is False, d
assert re.match(r'^req_[0-9A-Za-z]+\$', d['requestId']), d
assert set(d) == {'code', 'message', 'requestId', 'retryable'}, d
" && echo "$hdrs" | tr -d '\r' | grep -qi '^x-request-id:' \
  && pass "gateway 404 -> ORA-37 envelope {error:{code,message,requestId,retryable}} + X-Request-Id" \
  || fail "envelope missing/non-conformant: $(cat /tmp/gw_404.json)"

step "11. GW-2: upstream down -> 502 envelope (fail-closed, no hang)"
${COMPOSE} stop capability-registry-service >/dev/null 2>&1
down=$(curl -s -w '\n%{http_code}' -H "Authorization: Bearer dev-token" "${GW}/api/v1/tools")
code=$(echo "$down" | tail -1)
{ [[ "$code" == "502" || "$code" == "504" ]] \
  && echo "$down" | grep -qE '"code":"(SERVICE_UNAVAILABLE|GATEWAY_TIMEOUT)"'; } \
  && pass "upstream down -> ${code} ORA-37 envelope (gateway did not hang)" \
  || fail "expected 502/504 envelope, got: $down"
${COMPOSE} up -d capability-registry-service >/dev/null 2>&1

step "12. GW-6: the published OpenAPI v1 contract is served at the edge (ADR-015) + matches the ORA-37 taxonomy"
curl -fsS "${GW}/v1/openapi.json" -o /tmp/gw_openapi.json
ROOT_DIR="$ROOT" python3 -c "
import json, os
spec = json.load(open('/tmp/gw_openapi.json'))
assert str(spec['openapi']).startswith('3.'), spec.get('openapi')
assert spec['info']['title'] == 'Oraclous Platform API', spec['info']
# the published error component's code enum must equal the cross-repo ORA-37 taxonomy, byte-for-byte
published = spec['components']['schemas']['ErrorEnvelope']['properties']['error']['properties']['code']['enum']
canonical = json.load(open(os.path.join(os.environ['ROOT_DIR'],
    'packages/errors/contract/error-envelope.schema.json')))['properties']['error']['properties']['code']['enum']
assert published == canonical, (published, canonical)
# the contract documents real proxied operations and never the internal plane
assert '/v1/engine/jobs' in spec['paths'] and '/api/v1/tools' in spec['paths'], list(spec['paths'])
assert not any(p.startswith('/internal') for p in spec['paths']), 'internal plane disclosed'
print(f'  served OpenAPI {spec[\"openapi\"]} — {len(spec[\"paths\"])} paths, error enum == ORA-37 taxonomy')
" && pass "/v1/openapi.json -> valid OpenAPI 3.x, error component == ORA-37 taxonomy, no /internal" \
  || fail "published contract missing/inconsistent: $(head -c 200 /tmp/gw_openapi.json)"

# the reverse-proxy catch-all must NOT shadow the served contract (it is the edge route, public)
sc=$(curl -s -o /dev/null -w '%{http_code}' "${GW}/v1/openapi.json")  # no bearer — public
[[ "$sc" == "200" ]] && pass "catch-all does not shadow /v1/openapi.json (served public, 200)" \
  || fail "expected 200 for the served contract, got $sc"
sc=$(curl -s -o /dev/null -w '%{http_code}' "${GW}/docs")
[[ "$sc" == "200" ]] && pass "/docs (Swagger UI) served at the edge" || fail "expected 200 for /docs, got $sc"

# the gateway's actual error body conforms to the published contract: code is in the published enum
python3 -c "
import json
spec = json.load(open('/tmp/gw_openapi.json'))
enum = spec['components']['schemas']['ErrorEnvelope']['properties']['error']['properties']['code']['enum']
err = json.load(open('/tmp/gw_404.json'))['error']  # captured in step 10
assert err['code'] in enum, (err['code'], enum)
assert set(err) == {'code','message','requestId','retryable'}, err
" && pass "the gateway's emitted error body conforms to its own published ORA-37 contract" \
  || fail "emitted error does not match the published contract"

if [[ "${GW_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  AUTH=(-H "Authorization: Bearer dev-token")
  BIG="$(head -c 2000 </dev/zero | tr '\0' 'x')"  # 2000 bytes > the 1 KiB smoke cap

  step "13. GW-7a/b: request-size guard (FAIL-CLOSED) -> 413 PAYLOAD_TOO_LARGE, conforms to the contract"
  curl -s -D /tmp/gw_413h.txt -o /tmp/gw_413.json "${AUTH[@]}" --data-binary "$BIG" "${GW}/v1/search" >/dev/null
  python3 -c "
import json
enum = json.load(open('/tmp/gw_openapi.json'))['components']['schemas']['ErrorEnvelope']['properties']['error']['properties']['code']['enum']
e = json.load(open('/tmp/gw_413.json'))['error']
assert e['code'] == 'PAYLOAD_TOO_LARGE' and e['retryable'] is False, e
assert e['code'] in enum and set(e) == {'code','message','requestId','retryable'}, e
" && grep -qi '^x-request-id:' /tmp/gw_413h.txt \
    && pass "oversize POST -> 413 PAYLOAD_TOO_LARGE ORA-37 envelope + X-Request-Id (in the published enum)" \
    || fail "size guard 413 failed: $(cat /tmp/gw_413.json)"
  # chunked / omitted-length: the byte counter (not the header) must catch it
  cc=$(printf '%s' "$BIG" | curl -s -o /tmp/gw_413c.json -w '%{http_code}' "${AUTH[@]}" \
        -H "Transfer-Encoding: chunked" --data-binary @- "${GW}/v1/search")
  { [[ "$cc" == "413" ]] && grep -q '"code":"PAYLOAD_TOO_LARGE"' /tmp/gw_413c.json; } \
    && pass "chunked oversize -> 413 (byte counter caught it; the gateway did not buffer past the cap)" \
    || fail "chunked size-guard failed: $cc $(cat /tmp/gw_413c.json)"

  step "14. GW-7d: edge rate limit LIVE -> 429 RATE_LIMITED + Retry-After (recreate gateway, limit=5)"
  EDGE_RATE_LIMIT=5 MAX_REQUEST_BODY_BYTES=1024 ${COMPOSE} up -d --force-recreate \
    application-gateway-service >/dev/null 2>&1
  for i in $(seq 1 30); do curl -fsS "${GW}/health" >/dev/null 2>&1 && break; \
    [[ $i -eq 30 ]] && fail "gateway not healthy after recreate"; sleep 1; done
  saw429=0; retry=""
  for i in $(seq 1 10); do
    rc=$(curl -s -D /tmp/gw_rlh.txt -o /tmp/gw_rl.json -w '%{http_code}' "${AUTH[@]}" "${GW}/api/v1/tools")
    if [[ "$rc" == "429" ]]; then
      saw429=1; retry=$(grep -i '^retry-after:' /tmp/gw_rlh.txt | tr -d '\r' | awk '{print $2}'); break
    fi
  done
  { [[ "$saw429" == "1" && -n "$retry" ]] && grep -q '"code":"RATE_LIMITED"' /tmp/gw_rl.json; } \
    && pass "burst over EDGE_RATE_LIMIT -> 429 RATE_LIMITED + Retry-After=${retry}s (limiter live vs real Redis)" \
    || fail "rate limit did not trip: saw429=$saw429 retry=$retry $(cat /tmp/gw_rl.json)"
  hc=$(curl -s -o /dev/null -w '%{http_code}' "${GW}/health")
  [[ "$hc" == "200" ]] && pass "/health stays exempt from the limiter (200 while the bucket is tripped)" \
    || fail "/health was throttled: $hc"

  step "15. GW-7e/g: limiter FAILS OPEN when Redis is down; the size guard stays FAIL-CLOSED"
  ${COMPOSE} stop redis >/dev/null 2>&1; sleep 1
  oc=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "${GW}/api/v1/tools")
  [[ "$oc" != "429" ]] && pass "Redis down -> rate limit FAILS OPEN (status $oc, not 429 — sole ingress not self-DoSed)" \
    || fail "limiter did not fail open with redis down: $oc"
  sc=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" --data-binary "$BIG" "${GW}/v1/search")
  [[ "$sc" == "413" ]] && pass "Redis down -> size guard STILL 413 (fail-closed, independent of Redis)" \
    || fail "size guard broke with redis down: $sc"
  ${COMPOSE} start redis >/dev/null 2>&1
  # restore the gateway to default limits so the stack is left clean
  ${COMPOSE} up -d --force-recreate application-gateway-service >/dev/null 2>&1
  for i in $(seq 1 30); do curl -fsS "${GW}/health" >/dev/null 2>&1 && break; sleep 1; done
  pass "restored the gateway to default limits"

  step "16. GW-8: integration-key validator (seed a real key in the gateway DB, validate live, ADR-019)"
  DEV_ORG="00000000-0000-0000-0000-00000000050a"
  PSQL=(${COMPOSE} exec -T postgres psql -U oraclous -d oraclous -tAc)
  # A valid key is authenticated AT THE GATEWAY then forwarded; we probe /v1/engine (a routed prefix
  # whose upstream the smoke does NOT start), so a RESOLVED key gets a non-401 (forwarded past auth →
  # 5xx upstream-down, or 2xx if the full stack is up), while every BAD key is rejected at the edge
  # with 401 BEFORE any forward. That difference is the live proof of the validator. (The integration
  # key's authorised business routes — published-agent invoke — arrive in Slice 4.)
  PROBE="${GW}/v1/engine/jobs"
  mint_key() { python3 -c "
import secrets, hashlib, uuid
prefix = secrets.token_hex(8); secret = secrets.token_urlsafe(32)
tok = f'oak-{prefix}-{secret}'
print(uuid.uuid4(), tok, prefix, hashlib.sha256(tok.encode()).hexdigest(), secret[-4:])"; }
  read -r OAK_ID OAK_TOKEN OAK_PREFIX OAK_HASH OAK_LAST4 < <(mint_key)
  "${PSQL[@]}" "INSERT INTO integration_keys (id, organisation_id, key_prefix, key_hash, last4, bound_agent_slug, status) VALUES ('${OAK_ID}', '${DEV_ORG}', '${OAK_PREFIX}', '${OAK_HASH}', '${OAK_LAST4}', 'smoke-agent', 'active');" >/dev/null
  # valid key -> resolved + forwarded past edge auth (NOT a 401/403 edge rejection)
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${OAK_TOKEN}" "${PROBE}")
  { [[ "$code" != "401" && "$code" != "403" ]]; } \
    && pass "valid integration key -> authenticated + forwarded past the edge (HTTP ${code}, not 401)" \
    || fail "valid key was edge-rejected: HTTP ${code}"
  # unknown prefix -> 401 fail-closed (rejected at the edge, never forwarded)
  rnd=$(python3 -c "import secrets;print(secrets.token_hex(8))")
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer oak-${rnd}-nope" "${PROBE}")
  [[ "$code" == "401" ]] && pass "unknown integration key -> 401 fail-closed" || fail "expected 401, got $code"
  # right prefix + wrong secret -> 401 via constant-time compare (never a 500 / timing oracle)
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer oak-${OAK_PREFIX}-wrongsecretpadded000" "${PROBE}")
  [[ "$code" == "401" ]] && pass "right prefix + wrong secret -> 401 (constant-time, no 500)" || fail "expected 401, got $code"
  # revoke -> 401
  "${PSQL[@]}" "UPDATE integration_keys SET status='revoked' WHERE key_prefix='${OAK_PREFIX}';" >/dev/null
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${OAK_TOKEN}" "${PROBE}")
  [[ "$code" == "401" ]] && pass "revoked key -> 401 fail-closed" || fail "expected 401 after revoke, got $code"
  # expired key -> 401
  read -r EXP_ID EXP_TOKEN EXP_PREFIX EXP_HASH EXP_LAST4 < <(mint_key)
  "${PSQL[@]}" "INSERT INTO integration_keys (id, organisation_id, key_prefix, key_hash, last4, bound_agent_slug, status, expires_at) VALUES ('${EXP_ID}', '${DEV_ORG}', '${EXP_PREFIX}', '${EXP_HASH}', '${EXP_LAST4}', 'smoke-agent', 'active', now() - interval '1 day');" >/dev/null
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${EXP_TOKEN}" "${PROBE}")
  [[ "$code" == "401" ]] && pass "expired key (TTL in the past) -> 401 fail-closed" || fail "expected 401 for expired, got $code"
  # clean up the seeded keys
  "${PSQL[@]}" "DELETE FROM integration_keys WHERE bound_agent_slug='smoke-agent';" >/dev/null
  pass "cleaned up the seeded smoke keys"

  step "17. GW-9: the management plane (publish agents + integration-key CRUD), member-org-scoped (Slice 4)"
  JSON=(-H "Content-Type: application/json")
  MEMBER=(-H "Authorization: Bearer dev-token")  # a member (USER) JWT in the dev org
  # publish an agent (member)
  pub=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"slug":"smoke-pub","bound_capability_ref":"cap-smoke"}' "${GW}/v1/agents")
  echo "$pub" | grep -q '"slug":"smoke-pub"' \
    && pass "publish agent -> 201 (member)" || fail "publish failed: $pub"
  # mint a key bound to it -> plaintext ONCE
  mint=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"bound_agent_slug":"smoke-pub"}' "${GW}/v1/integration-keys")
  oak=$(echo "$mint" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  kid=$(echo "$mint" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  [[ "$oak" == oak-* ]] && pass "mint a key bound to the agent -> plaintext once" || fail "mint failed: $mint"
  # list -> redacted (never the hash or the plaintext)
  lst=$(curl -s "${MEMBER[@]}" "${GW}/v1/integration-keys")
  { echo "$lst" | grep -q '"last4"' && ! echo "$lst" | grep -q 'key_hash' && ! echo "$lst" | grep -q '"key"'; } \
    && pass "list -> redacted (no hash, no plaintext)" || fail "list not redacted: $lst"
  # the minted key authenticates at the edge (the S3 validator, live against the just-minted row)
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${oak}" "${PROBE}")
  [[ "$code" != "401" ]] && pass "the minted key authenticates at the edge (HTTP ${code})" || fail "minted key edge-rejected: $code"
  # a key bearer cannot manage keys -> 403 (member-only)
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${oak}" "${GW}/v1/integration-keys")
  [[ "$code" == "403" ]] && pass "a key bearer cannot manage keys -> 403" || fail "expected 403, got $code"
  # rotate -> a NEW plaintext; the OLD key dies (401 at the edge)
  rot=$(curl -s "${MEMBER[@]}" -X POST "${GW}/v1/integration-keys/${kid}/rotate")
  newoak=$(echo "$rot" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  { [[ "$newoak" == oak-* && "$newoak" != "$oak" ]]; } && pass "rotate -> a new plaintext" || fail "rotate failed: $rot"
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${oak}" "${PROBE}")
  [[ "$code" == "401" ]] && pass "the OLD key 401s after rotate" || fail "old key still works after rotate: $code"
  # revoke -> 204; the rotated key now 401s
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER[@]}" -X DELETE "${GW}/v1/integration-keys/${kid}")
  [[ "$code" == "204" ]] && pass "revoke -> 204" || fail "revoke failed: $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${newoak}" "${PROBE}")
  [[ "$code" == "401" ]] && pass "the rotated key 401s after revoke (fail-closed)" || fail "revoked key still works: $code"
  # cleanup
  "${PSQL[@]}" "DELETE FROM integration_keys WHERE bound_agent_slug='smoke-pub';" >/dev/null
  "${PSQL[@]}" "DELETE FROM published_agents WHERE slug='smoke-pub';" >/dev/null
  pass "cleaned up the smoke published agent + keys"

  step "18. GW-10: published-agent invoke surface — key-auth + binding enforcement (Slice 4 PR2)"
  # Publish two agents; a key is bound to the first. The agent's bound ref is one the harness can't
  # run, so a VALID invoke still reaches the harness (-> 502) — which proves the gateway authenticated,
  # binding-checked, and FORWARDED; a binding violation is 403 BEFORE any forward. (The agent actually
  # executing -> SUCCEEDED is the harness's own R4 smoke + the gateway unit tests with a fake harness.)
  curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"slug":"smoke-inv","bound_capability_ref":"cap-unrunnable"}' "${GW}/v1/agents" >/dev/null
  curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"slug":"smoke-inv2","bound_capability_ref":"cap-unrunnable"}' "${GW}/v1/agents" >/dev/null
  im=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"bound_agent_slug":"smoke-inv"}' "${GW}/v1/integration-keys")
  ik=$(echo "$im" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  ikid=$(echo "$im" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  cm=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"capability_allow_list":["cap:read"]}' "${GW}/v1/integration-keys")
  ck=$(echo "$cm" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  ckid=$(echo "$cm" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  # bound key -> GET metadata = the narrow public projection (slug/display/description, no internal ids)
  meta=$(curl -s -H "Authorization: Bearer ${ik}" "${GW}/v1/agents/smoke-inv")
  { echo "$meta" | grep -q '"slug":"smoke-inv"' && ! echo "$meta" | grep -q bound_capability_ref; } \
    && pass "GET /v1/agents/{slug} -> public metadata only (no internal ids)" || fail "metadata leaked/wrong: $meta"
  # bound key -> invoke -> forwarded PAST auth+binding to the harness (not a 401/403 edge rejection)
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${ik}" "${JSON[@]}" -d '{"input":"hi"}' "${GW}/v1/agents/smoke-inv/invoke")
  { [[ "$code" != "401" && "$code" != "403" ]]; } \
    && pass "bound key invoke -> authenticated + forwarded past binding (HTTP ${code})" || fail "invoke edge-rejected: $code"
  # the SAME key on a DIFFERENT agent -> 403, fail-closed BEFORE any forward (the binding is enforced)
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${ik}" "${JSON[@]}" -d '{"input":"hi"}' "${GW}/v1/agents/smoke-inv2/invoke")
  [[ "$code" == "403" ]] && pass "key bound to A cannot invoke B -> 403 (binding enforced)" || fail "expected 403, got $code"
  # a member JWT cannot invoke (the public surface requires a key) -> 403
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER[@]}" "${JSON[@]}" -d '{"input":"hi"}' "${GW}/v1/agents/smoke-inv/invoke")
  [[ "$code" == "403" ]] && pass "a member JWT cannot invoke (needs a key) -> 403" || fail "expected 403, got $code"
  # a capability-only key (bound to no agent) cannot invoke a published agent -> 403
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${ck}" "${JSON[@]}" -d '{"input":"hi"}' "${GW}/v1/agents/smoke-inv/invoke")
  [[ "$code" == "403" ]] && pass "a capability-only key cannot invoke a published agent -> 403" || fail "expected 403, got $code"
  # cleanup
  "${PSQL[@]}" "DELETE FROM integration_keys WHERE id IN ('${ikid}','${ckid}');" >/dev/null
  "${PSQL[@]}" "DELETE FROM published_agents WHERE slug IN ('smoke-inv','smoke-inv2');" >/dev/null
  pass "cleaned up the invoke smoke agents + keys"
else
  step "13. GW-7/GW-8: edge-protection + key-validator LIVE checks SKIPPED in NO_COMPOSE mode (unit-covered)"
fi

printf '\n\033[32mapplication-gateway GW-1..GW-5 smoke passed.\033[0m  edge JWT termination, '
printf 'reverse-proxy to TWO real upstreams (capability-registry + credential-broker), CORS '
printf 'termination, /internal not edge-routed, aggregated upstream health, own-error envelope, '
printf 'unknown-prefix 404, and downed-upstream 502 — over the stack.\n'
printf '%s\n' "" "(For the full §22 sign-off, run the whole stack: 'docker compose --profile services up -d --build' — then /health/upstreams rolls up all five as ok.)"
