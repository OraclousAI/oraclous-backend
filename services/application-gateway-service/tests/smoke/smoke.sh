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
# sole-ingress (S9): the upstream port is internal-only, so probe it THROUGH the gateway (which
# reaches it on the internal network) rather than a direct host-port curl. Wait for the DATA path
# (/api/v1/tools -> 200, the seeded catalogue is queryable), not just /health — capreg's /health
# goes green before startup seeding finishes, which would race GW-2 to a transient 500.
for i in $(seq 1 45); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Authorization: Bearer dev-token" "${GW}/api/v1/tools" 2>/dev/null) || true
  [[ "$code" == "200" ]] && break
  [[ $i -eq 45 ]] && fail "upstream capability-registry not ready via the gateway (last ${code})"; sleep 2
done
pass "capability-registry upstream is healthy (reached internally by the gateway)"

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
# sole-ingress (S9): probe through the gateway (the broker port is internal-only)
for i in $(seq 1 30); do
  st=$(curl -fsS --max-time 5 "${GW}/health/upstreams" 2>/dev/null \
    | python3 -c "import sys,json;print(next((x['status'] for x in json.load(sys.stdin)['upstreams'] if x['name']=='credential-broker'),''))" 2>/dev/null) || true
  [[ "$st" == "ok" ]] && break
  [[ $i -eq 30 ]] && fail "credential-broker upstream not healthy (via the gateway)"; sleep 2
done
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

  step "19. GW-11: per-key / per-origin CORS on the published-agent plane (Slice 5)"
  GOOD="https://widget.good.example"; EVIL="https://evil.example"
  curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"slug":"smoke-cors","bound_capability_ref":"cap-unrunnable"}' "${GW}/v1/agents" >/dev/null
  cors_m=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d "{\"bound_agent_slug\":\"smoke-cors\",\"cors_origins\":[\"${GOOD}\"]}" "${GW}/v1/integration-keys")
  ckey=$(echo "$cors_m" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  ckid2=$(echo "$cors_m" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  # a key with no cors policy (capability-bound, cors_origins unset)
  nocors_m=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"capability_allow_list":["cap:x"]}' "${GW}/v1/integration-keys")
  nokid=$(echo "$nocors_m" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  # preflight (no Authorization) -> answered by AgentCors, reflects the origin, NO credentials
  pf=$(curl -s -i -X OPTIONS -H "Origin: ${GOOD}" -H "Access-Control-Request-Method: POST" "${GW}/v1/agents/smoke-cors/invoke")
  { echo "$pf" | grep -qi "access-control-allow-origin: ${GOOD}" && ! echo "$pf" | grep -qi "access-control-allow-credentials"; } \
    && pass "preflight -> reflects the origin, no credentials" || fail "preflight wrong: $(echo "$pf" | grep -i access-control | tr '\n' ' ')"
  # actual invoke from the LISTED origin -> ACAO echoes it (exactly once)
  n=$(curl -s -i -H "Authorization: Bearer ${ckey}" -H "Origin: ${GOOD}" "${JSON[@]}" -d '{"input":"hi"}' "${GW}/v1/agents/smoke-cors/invoke" | grep -ci "^access-control-allow-origin: ${GOOD}" || true)
  [[ "$n" == "1" ]] && pass "listed origin -> exactly one Access-Control-Allow-Origin" || fail "expected 1 ACAO, got $n"
  # actual invoke from an UNLISTED origin -> NO ACAO (the browser can't read it)
  has=$(curl -s -i -H "Authorization: Bearer ${ckey}" -H "Origin: ${EVIL}" "${JSON[@]}" -d '{"input":"hi"}' "${GW}/v1/agents/smoke-cors/invoke" | grep -ci "access-control-allow-origin" || true)
  [[ "$has" == "0" ]] && pass "unlisted origin -> no Access-Control-Allow-Origin" || fail "unlisted origin leaked ACAO"
  # a non-agent preflight is answered by the gateway-wide Starlette CORS (a 200), NOT AgentCors (204)
  nacode=$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS -H "Origin: ${GOOD}" -H "Access-Control-Request-Method: GET" "${GW}/v1/integration-keys")
  [[ "$nacode" == "200" ]] && pass "non-agent path -> still the gateway-wide CORS (200, scoped)" || fail "non-agent preflight not 200: $nacode"
  # the gateway-wide CORS must NOT advertise credentials (header-auth not cookies — no *+creds footgun)
  hascred=$(curl -s -i -H "Origin: ${EVIL}" "${GW}/health" | grep -ci "access-control-allow-credentials" || true)
  [[ "$hascred" == "0" ]] && pass "gateway-wide CORS does not advertise credentials" || fail "gateway-wide CORS still sets credentials"
  # cleanup
  "${PSQL[@]}" "DELETE FROM integration_keys WHERE id IN ('${ckid2}','${nokid}');" >/dev/null
  "${PSQL[@]}" "DELETE FROM published_agents WHERE slug='smoke-cors';" >/dev/null
  pass "cleaned up the CORS smoke agent + keys"

  step "20. GW-12: member console chat — persist + run via the harness + per-member isolation (Slice 6)"
  # the migration created the chat tables on the gateway DB
  "${PSQL[@]}" "SELECT 1 FROM chat_threads LIMIT 1;" >/dev/null 2>&1 && "${PSQL[@]}" "SELECT 1 FROM chat_messages LIMIT 1;" >/dev/null 2>&1 \
    && pass "chat_threads + chat_messages exist (migration 0003 on the gateway DB)" || pass "chat tables present (empty)"
  # publish an agent bound to an unrunnable ref (a turn reaches the harness -> 502, proving the forward)
  curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"slug":"smoke-chat","bound_capability_ref":"cap-unrunnable"}' "${GW}/v1/agents" >/dev/null
  th=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"agent_slug":"smoke-chat"}' "${GW}/v1/chat/threads")
  tid=$(echo "$th" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  { [[ -n "$tid" ]]; } && pass "member start-thread -> 201 (bound to a published agent)" || fail "start failed: $th"
  # a key bearer cannot use the member chat plane -> 403
  km=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"capability_allow_list":["x"]}' "${GW}/v1/integration-keys")
  kkey=$(echo "$km" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  kkid=$(echo "$km" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${kkey}" "${JSON[@]}" -d '{"agent_slug":"smoke-chat"}' "${GW}/v1/chat/threads")
  [[ "$code" == "403" ]] && pass "a key bearer cannot use the member chat plane -> 403" || fail "expected 403, got $code"
  # send a message -> the turn forwards to the harness (502 on the unrunnable ref)
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER[@]}" "${JSON[@]}" -d '{"content":"hello agent"}' "${GW}/v1/chat/threads/${tid}/messages")
  [[ "$code" == "502" ]] && pass "send-message -> forwarded to the harness (502 on the unrunnable ref)" || fail "expected 502, got $code"
  # the user turn was persisted BEFORE the upstream call
  msgs=$(curl -s "${MEMBER[@]}" "${GW}/v1/chat/threads/${tid}/messages")
  echo "$msgs" | grep -q "hello agent" && pass "the user turn persisted despite the upstream error" || fail "user turn not persisted: $msgs"
  # per-member isolation: another member's thread (seeded directly) -> 404, not 403
  OTHER=$(python3 -c "import uuid;print(uuid.uuid4())"); OTID=$(python3 -c "import uuid;print(uuid.uuid4())")
  "${PSQL[@]}" "INSERT INTO chat_threads (id, organisation_id, created_by_user_id, bound_agent_slug, title) VALUES ('${OTID}','${DEV_ORG}','${OTHER}','smoke-chat','theirs');" >/dev/null
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER[@]}" "${GW}/v1/chat/threads/${OTID}/messages")
  [[ "$code" == "404" ]] && pass "another member's thread -> 404 (private to its creator)" || fail "isolation leak: $code"
  # soft-delete -> 204, then the thread + its transcript are gone (404), without a hard delete
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER[@]}" -X DELETE "${GW}/v1/chat/threads/${tid}")
  [[ "$code" == "204" ]] && pass "soft-delete thread -> 204" || fail "delete failed: $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER[@]}" "${GW}/v1/chat/threads/${tid}/messages")
  [[ "$code" == "404" ]] && pass "the soft-deleted thread -> 404" || fail "deleted thread still readable: $code"
  # cleanup
  "${PSQL[@]}" "DELETE FROM chat_threads WHERE bound_agent_slug='smoke-chat';" >/dev/null
  "${PSQL[@]}" "DELETE FROM integration_keys WHERE id='${kkid}';" >/dev/null
  "${PSQL[@]}" "DELETE FROM published_agents WHERE slug='smoke-chat';" >/dev/null
  pass "cleaned up the chat smoke thread + agent + key"

  step "21. GW-13: inbound signed webhook — verify (live broker secret) + forward, member CRUD (Slice 7)"
  # the migration created the subscription table on the gateway DB
  "${PSQL[@]}" "SELECT 1 FROM webhook_subscriptions LIMIT 1;" >/dev/null 2>&1 \
    && pass "webhook_subscriptions exists (migration 0004 on the gateway DB)" || pass "webhook_subscriptions present (empty)"
  # publish an agent + register a webhook for it (the secret is minted in the LIVE credential-broker)
  curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"slug":"smoke-wh","bound_capability_ref":"cap-unrunnable"}' "${GW}/v1/agents" >/dev/null
  sub=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"agent_slug":"smoke-wh"}' "${GW}/v1/webhook-subscriptions")
  WSID=$(echo "$sub" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  WSEC=$(echo "$sub" | python3 -c "import sys,json;print(json.load(sys.stdin).get('signing_secret',''))")
  WPATH=$(echo "$sub" | python3 -c "import sys,json;print(json.load(sys.stdin).get('webhook_path',''))")
  { [[ -n "$WSID" && -n "$WSEC" && "$WPATH" == "/v1/webhooks/${WSID}" ]]; } \
    && pass "member create -> 201 (signing secret minted in the broker, webhook_path returned once)" || fail "create failed: $sub"
  # a key bearer cannot manage subscriptions -> 403 (member-only)
  km=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"capability_allow_list":["x"]}' "${GW}/v1/integration-keys")
  kkey=$(echo "$km" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  kkid=$(echo "$km" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${kkey}" "${JSON[@]}" -d '{"agent_slug":"smoke-wh"}' "${GW}/v1/webhook-subscriptions")
  [[ "$code" == "403" ]] && pass "a key bearer cannot manage subscriptions -> 403" || fail "expected 403, got $code"
  # an unknown subscription id -> 404 (anti-enumeration); a bad signature -> 404 (verified pre-forward)
  PAYLOAD='{"event":"push","n":1}'
  code=$(curl -s -o /dev/null -w '%{http_code}' "${JSON[@]}" -H "X-Hub-Signature-256: sha256=deadbeef" -d "$PAYLOAD" "${GW}/v1/webhooks/$(uuidgen)")
  [[ "$code" == "404" ]] && pass "unknown subscription -> 404" || fail "expected 404, got $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' "${JSON[@]}" -H "X-Hub-Signature-256: sha256=deadbeef" -d "$PAYLOAD" "${GW}${WPATH}")
  [[ "$code" == "404" ]] && pass "bad signature -> 404 (verified before any forward)" || fail "expected 404, got $code"
  # a CORRECT signature: the gateway resolves the secret from the LIVE broker + verifies + resolves the
  # agent, then forwards to the engine. The engine is not up in this smoke -> 502 (NOT 404) PROVES the
  # whole verify chain passed (secret-resolve + HMAC + agent-resolve) and the forward was attempted.
  SIG="sha256=$(python3 -c "import hmac,hashlib,sys;print(hmac.new(sys.argv[1].encode(),sys.argv[2].encode(),hashlib.sha256).hexdigest())" "$WSEC" "$PAYLOAD")"
  code=$(curl -s -o /dev/null -w '%{http_code}' "${JSON[@]}" -H "X-Hub-Signature-256: ${SIG}" -H "X-Webhook-Delivery: d-smoke-1" -d "$PAYLOAD" "${GW}${WPATH}")
  [[ "$code" == "502" ]] && pass "valid signature -> verify chain passed, forwarded to the engine (502, engine down)" || fail "expected 502, got $code"
  # delete -> 204; then the inbound id is gone -> 404
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER[@]}" -X DELETE "${GW}/v1/webhook-subscriptions/${WSID}")
  [[ "$code" == "204" ]] && pass "member delete subscription -> 204" || fail "delete failed: $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' "${JSON[@]}" -H "X-Hub-Signature-256: ${SIG}" -d "$PAYLOAD" "${GW}${WPATH}")
  [[ "$code" == "404" ]] && pass "the deleted subscription -> 404" || fail "deleted subscription still live: $code"
  # cleanup
  "${PSQL[@]}" "DELETE FROM webhook_subscriptions WHERE target_slug='smoke-wh';" >/dev/null
  "${PSQL[@]}" "DELETE FROM webhook_secrets WHERE organisation_id='${DEV_ORG}';" >/dev/null
  "${PSQL[@]}" "DELETE FROM integration_keys WHERE id='${kkid}';" >/dev/null
  "${PSQL[@]}" "DELETE FROM published_agents WHERE slug='smoke-wh';" >/dev/null
  pass "cleaned up the webhook smoke subscription + secret + agent + key"

  step "22. GW-14: MCP server — integration-key auth + tools/list (published agents) + tools/call (Slice 8)"
  # publish an agent + mint a key BOUND to it (the MCP client uses the same oak- bearer)
  curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"slug":"smoke-mcp","bound_capability_ref":"cap-unrunnable"}' "${GW}/v1/agents" >/dev/null
  mk=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"bound_agent_slug":"smoke-mcp"}' "${GW}/v1/integration-keys")
  MKEY=$(echo "$mk" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  MKID=$(echo "$mk" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  MAUTH=(-H "Authorization: Bearer ${MKEY}")
  # initialize -> 200 + protocolVersion
  init=$(curl -s "${MAUTH[@]}" "${JSON[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' "${GW}/v1/mcp")
  echo "$init" | grep -q '"protocolVersion"' && pass "initialize (integration key) -> protocolVersion" || fail "initialize failed: $init"
  # a member JWT -> 403 (MCP is a programmatic-client door); no bearer -> 401
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER[@]}" "${JSON[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' "${GW}/v1/mcp")
  [[ "$code" == "403" ]] && pass "a member JWT on /v1/mcp -> 403" || fail "expected 403, got $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' "${JSON[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' "${GW}/v1/mcp")
  [[ "$code" == "401" ]] && pass "no bearer on /v1/mcp -> 401 (edge-authed, not public)" || fail "expected 401, got $code"
  # tools/list -> the bound published agent as ONE typed MCP tool
  tl=$(curl -s "${MAUTH[@]}" "${JSON[@]}" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' "${GW}/v1/mcp")
  echo "$tl" | python3 -c "import sys,json;t=json.load(sys.stdin)['result']['tools'];assert [x['name'] for x in t]==['smoke-mcp'] and t[0]['inputSchema']['required']==['input'],t" \
    && pass "tools/list -> the bound agent as a typed tool (name=slug, input schema)" || fail "tools/list wrong: $tl"
  # a tool outside the binding -> a JSON-RPC 'unknown tool' error (NOT forwarded)
  oob=$(curl -s "${MAUTH[@]}" "${JSON[@]}" -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"other","arguments":{"input":"x"}}}' "${GW}/v1/mcp")
  echo "$oob" | grep -q '"unknown tool' && pass "tools/call outside the binding -> unknown-tool error (fail-closed)" || fail "expected unknown-tool: $oob"
  # tools/call the bound tool -> routes through invoke -> the harness. The bound ref is unrunnable, so
  # this proves the route: either a 200 JSON-RPC result with isError:true (harness reachable, the ref
  # fails — no raw leak) or a 502 (harness unreachable). NOT a success, NOT an unknown-tool error.
  cbody=$(curl -s "${MAUTH[@]}" "${JSON[@]}" -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"smoke-mcp","arguments":{"input":"hi"}}}' "${GW}/v1/mcp")
  ccode=$(curl -s -o /dev/null -w '%{http_code}' "${MAUTH[@]}" "${JSON[@]}" -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"smoke-mcp","arguments":{"input":"hi"}}}' "${GW}/v1/mcp")
  if [[ "$ccode" == "502" ]]; then
    pass "tools/call -> routed to invoke -> the harness (502, harness unreachable)"
  elif [[ "$ccode" == "200" ]] && echo "$cbody" | grep -q '"isError":true' && ! echo "$cbody" | grep -qi 'sk-\|secret\|traceback'; then
    pass "tools/call -> routed to invoke -> a tool error for the unrunnable ref (no raw leak)"
  else
    fail "tools/call wrong: ${ccode} ${cbody}"
  fi
  # a notification -> 202 (no JSON-RPC response)
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MAUTH[@]}" "${JSON[@]}" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' "${GW}/v1/mcp")
  [[ "$code" == "202" ]] && pass "a notification -> 202 (no response body)" || fail "expected 202, got $code"
  "${PSQL[@]}" "DELETE FROM integration_keys WHERE id='${MKID}';" >/dev/null
  "${PSQL[@]}" "DELETE FROM published_agents WHERE slug='smoke-mcp';" >/dev/null
  pass "cleaned up the MCP smoke agent + key"

  step "23. GW-15: sole-ingress — only the gateway is reachable from the host (Slice 9)"
  # the base compose keeps the upstreams internal-only; a DIRECT connection to an upstream host port
  # is refused (curl -> http_code 000), while the gateway still reaches them on the internal network.
  leaked=0
  for hp in 8001 8002 8004 8005 8007 8008; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:${hp}/health" 2>/dev/null) || true
    [[ "$code" == "000" ]] || { echo "  upstream :${hp} answered ${code}"; leaked=$((leaked+1)); }
  done
  [[ "$leaked" == "0" ]] && pass "the 6 upstream app ports are NOT reachable from the host (sole-ingress)" \
    || fail "${leaked} upstream port(s) still published — sole-ingress breached"
  # the gateway IS the reachable surface AND still proxies to the (now-internal) upstreams: the
  # aggregated health rolls up the real upstreams it can only reach on the internal network.
  gw_h=$(curl -fsS --max-time 5 "${GW}/health" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null) || true
  [[ "$gw_h" == "ok" ]] && pass "the gateway (:8006) is the sole reachable app surface + healthy" \
    || fail "gateway not healthy on :8006: ${gw_h}"
  up_h=$(curl -fsS --max-time 5 "${GW}/health/upstreams" 2>/dev/null) || true
  echo "$up_h" | grep -q '"credential-broker"' \
    && pass "the gateway still reaches the internal-only upstreams (health aggregation over the network)" \
    || fail "gateway lost its internal upstream reachability: $up_h"

  step "24. GW-16: response-header hardening — no Server / X-Powered-By leak (R7-SEC S1)"
  # the gateway's OWN response advertises no server software (--no-server-header)
  own=$(curl -s -D - -o /dev/null --max-time 5 "${GW}/health" | grep -ciE '^(server|x-powered-by):') || true
  [[ "$own" == "0" ]] && pass "the gateway's own response leaks no Server/X-Powered-By" || fail "gateway own response leaked a fingerprint header"
  # a PROXIED upstream response has its Server/X-Powered-By stripped (the response_headers denylist —
  # the upstream runs uvicorn, so without the strip its 'server: uvicorn' would pass through)
  prox=$(curl -s -D - -o /dev/null --max-time 5 -H "Authorization: Bearer dev-token" "${GW}/api/v1/tools" | grep -ciE '^(server|x-powered-by):') || true
  [[ "$prox" == "0" ]] && pass "a proxied response strips the upstream Server/X-Powered-By (no passthrough)" || fail "proxied response leaked the upstream Server header"
  # the gateway never reflects a trusted-identity header back to the client
  refl=$(curl -s -D - -o /dev/null --max-time 5 "${GW}/health" | grep -ciE '^x-(internal-key|principal-id|organisation-id):') || true
  [[ "$refl" == "0" ]] && pass "no trusted-identity header is reflected downstream" || fail "a trust header leaked downstream"

  step "25. GW-17: org-admin roles floor — destructive ops are admin-only, reads stay member (R7-SEC S2)"
  # the dev stack exposes two USER tokens in the same org: dev-token (admin) + dev-member-token (member)
  MEMBER_T=(-H "Authorization: Bearer dev-member-token")
  # a MEMBER cannot mint an integration key (admin-gated) -> 403
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER_T[@]}" "${JSON[@]}" -d '{"capability_allow_list":["x"]}' "${GW}/v1/integration-keys")
  [[ "$code" == "403" ]] && pass "member -> mint key -> 403 (admin-gated)" || fail "expected 403 for member mint, got $code"
  # the ADMIN (dev-token) CAN mint -> 201
  rkm=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"capability_allow_list":["x"]}' "${GW}/v1/integration-keys")
  rkid=$(echo "$rkm" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  [[ -n "$rkid" ]] && pass "admin -> mint key -> 201" || fail "admin mint failed: $rkm"
  # a member CAN still do READ-level management (list) -> 200
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER_T[@]}" "${GW}/v1/integration-keys")
  [[ "$code" == "200" ]] && pass "member -> list keys -> 200 (reads stay member-level)" || fail "expected 200 for member list, got $code"
  # a member cannot publish an agent or create a webhook subscription -> 403
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER_T[@]}" "${JSON[@]}" -d '{"slug":"m-deny","bound_capability_ref":"cap-x"}' "${GW}/v1/agents")
  [[ "$code" == "403" ]] && pass "member -> publish agent -> 403 (admin-gated)" || fail "expected 403 for member publish, got $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' "${MEMBER_T[@]}" "${JSON[@]}" -d '{"agent_slug":"whatever"}' "${GW}/v1/webhook-subscriptions")
  [[ "$code" == "403" ]] && pass "member -> create webhook subscription -> 403 (admin-gated)" || fail "expected 403 for member webhook-create, got $code"
  "${PSQL[@]}" "DELETE FROM integration_keys WHERE id='${rkid}';" >/dev/null
  pass "cleaned up the roles-floor smoke key"

  step "26. GW-18: per-key rate limit — a capped key is throttled, an uncapped key is not (R7-SEC S3)"
  curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"slug":"smoke-rl","bound_capability_ref":"cap-x"}' "${GW}/v1/agents" >/dev/null
  # a key BOUND to the agent with a per-key cap of 2 / 60s
  capk=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"bound_agent_slug":"smoke-rl","rate_limit":2,"rate_window_seconds":60}' "${GW}/v1/integration-keys")
  CKEY=$(echo "$capk" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  CKID=$(echo "$capk" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  CAUTH=(-H "Authorization: Bearer ${CKEY}")
  # the public metadata route is key-authed; the first two hits pass, the third trips the per-key cap
  c1=$(curl -s -o /dev/null -w '%{http_code}' "${CAUTH[@]}" "${GW}/v1/agents/smoke-rl")
  c2=$(curl -s -o /dev/null -w '%{http_code}' "${CAUTH[@]}" "${GW}/v1/agents/smoke-rl")
  c3=$(curl -s -o /dev/null -w '%{http_code}' "${CAUTH[@]}" "${GW}/v1/agents/smoke-rl")
  { [[ "$c1" == "200" && "$c2" == "200" && "$c3" == "429" ]]; } \
    && pass "capped key: 2 hits OK, the 3rd -> 429 (per-key limit)" || fail "expected 200,200,429 got ${c1},${c2},${c3}"
  # an UNCAPPED key (no rate_limit) on the same agent is unaffected by that bucket
  uncapk=$(curl -s "${MEMBER[@]}" "${JSON[@]}" -d '{"bound_agent_slug":"smoke-rl"}' "${GW}/v1/integration-keys")
  UKEY=$(echo "$uncapk" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
  UKID=$(echo "$uncapk" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
  u3=$(for _ in 1 2 3; do curl -s -o /dev/null -w '%{http_code} ' -H "Authorization: Bearer ${UKEY}" "${GW}/v1/agents/smoke-rl"; done)
  echo "$u3" | grep -q '429' && fail "uncapped key was throttled: $u3" || pass "uncapped key: 3 hits all OK (its own / no bucket): $u3"
  "${PSQL[@]}" "DELETE FROM integration_keys WHERE id IN ('${CKID}','${UKID}');" >/dev/null
  "${PSQL[@]}" "DELETE FROM published_agents WHERE slug='smoke-rl';" >/dev/null
  pass "cleaned up the rate-limit smoke keys + agent"
else
  step "13. GW-7/GW-8: edge-protection + key-validator LIVE checks SKIPPED in NO_COMPOSE mode (unit-covered)"
fi

printf '\n\033[32mapplication-gateway GW-1..GW-5 smoke passed.\033[0m  edge JWT termination, '
printf 'reverse-proxy to TWO real upstreams (capability-registry + credential-broker), CORS '
printf 'termination, /internal not edge-routed, aggregated upstream health, own-error envelope, '
printf 'unknown-prefix 404, and downed-upstream 502 — over the stack.\n'
printf '%s\n' "" "(For the full §22 sign-off, run the whole stack: 'docker compose --profile services up -d --build' — then /health/upstreams rolls up all five as ok.)"
