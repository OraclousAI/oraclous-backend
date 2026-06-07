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
else
  step "13. GW-7: edge-protection LIVE checks SKIPPED in NO_COMPOSE mode (size + rate-limit unit-covered)"
fi

printf '\n\033[32mapplication-gateway GW-1..GW-5 smoke passed.\033[0m  edge JWT termination, '
printf 'reverse-proxy to TWO real upstreams (capability-registry + credential-broker), CORS '
printf 'termination, /internal not edge-routed, aggregated upstream health, own-error envelope, '
printf 'unknown-prefix 404, and downed-upstream 502 — over the stack.\n'
printf '%s\n' "" "(For the full §22 sign-off, run the whole stack: 'docker compose --profile services up -d --build' — then /health/upstreams rolls up all five as ok.)"
