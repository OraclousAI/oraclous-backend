#!/usr/bin/env bash
# R3.5-P1 acceptance smoke — knowledge-graph-service against the real docker stack.
#
# S1: graph CRUD (Postgres). S2: async text/document ingestion -> real :Document/:Chunk nodes in
# Neo4j (org + graph stamped), key-free (dev-auth + hashing embedder + null extractor — no API key).
# This is the runbook Reza runs to sign off (ORAA-4 §22 gate 6).
#
# Usage (from repo root):  bash services/knowledge-graph-service/tests/smoke/smoke.sh
# Env: KGS_SMOKE_URL (default http://localhost:8003); KGS_SMOKE_NO_COMPOSE=1 to skip bring-up.
set -euo pipefail

BASE="${KGS_SMOKE_URL:-http://localhost:8003}"
AUTH=(-H "Authorization: Bearer dev-token" -H "Content-Type: application/json")
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/deploy/docker-compose.yml"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
jget() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
cypher() { ${COMPOSE} exec -T neo4j cypher-shell -u neo4j -p password --format plain "$1"; }

if [[ "${KGS_SMOKE_NO_COMPOSE:-0}" != "1" ]]; then
  step "1. bring up postgres + neo4j + redis -> migrate -> seed -> service + worker"
  ${COMPOSE} up -d --build postgres neo4j redis
  ${COMPOSE} up kgs-migrate kgs-seed
  ${COMPOSE} up -d knowledge-graph-service knowledge-graph-worker
fi

step "2. wait for /health"
for i in $(seq 1 30); do
  curl -fsS "${BASE}/health" >/dev/null 2>&1 && break
  [[ $i -eq 30 ]] && fail "service not healthy at ${BASE}/health"
  sleep 2
done
curl -fsS "${BASE}/health" | grep -q '"status":"ok"' && pass "health ok" || fail "health body"

step "3. auth seam"
code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/api/v1/graphs")
[[ "$code" == "401" ]] && pass "no token -> 401" || fail "expected 401, got $code"

step "4. graph CRUD (S1)"
gid=$(curl -fsS "${AUTH[@]}" -X POST "${BASE}/api/v1/graphs" -d '{"name":"crud"}' | jget "['id']")
curl -fsS "${AUTH[@]}" "${BASE}/api/v1/graphs/${gid}" | grep -q '"crud"' && pass "create+get" || fail "get"
code=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X DELETE "${BASE}/api/v1/graphs/${gid}")
[[ "$code" == "204" ]] && pass "delete -> 204" || fail "delete $code"

step "5. ingest inline text -> async job"
IGID=$(curl -fsS "${AUTH[@]}" -X POST "${BASE}/api/v1/graphs" -d '{"name":"ingest"}' | jget "['id']")
pass "ingestion graph=${IGID}"
job=$(curl -fsS "${AUTH[@]}" -X POST "${BASE}/api/v1/graphs/${IGID}/ingest" \
  -d '{"content":"Ada Lovelace wrote the first algorithm.\n\nCharles Babbage designed the Analytical Engine.\n\nThey collaborated in the 1840s.","filename":"history.txt"}')
jid=$(echo "$job" | jget "['id']")
[[ "$(echo "$job" | jget "['status']")" == "pending" ]] && pass "accepted (202) job=${jid}" || fail "submit: $job"

step "6. poll job to completion"
status=""
for i in $(seq 1 30); do
  body=$(curl -fsS "${AUTH[@]}" "${BASE}/api/v1/graphs/${IGID}/jobs/${jid}")
  status=$(echo "$body" | jget "['status']")
  [[ "$status" == "completed" || "$status" == "failed" ]] && break
  sleep 2
done
[[ "$status" == "completed" ]] && pass "job completed: $body" || fail "job $status: $body"
nodes=$(echo "$body" | jget "['extracted_entities']")
[[ "$nodes" -ge 3 ]] && pass "nodes written=${nodes}" || fail "expected >=3 nodes, got $nodes"

step "7. Neo4j: real :Document/:Chunk nodes, org + graph stamped, key-free"
doc=$(cypher "MATCH (d:Document {graph_id:'${IGID}'}) RETURN count(d)" | tail -1 | tr -d ' ')
chunks=$(cypher "MATCH (c:Chunk {graph_id:'${IGID}'}) RETURN count(c)" | tail -1 | tr -d ' ')
[[ "$doc" == "1" ]] && pass ":Document count=1" || fail ":Document count=$doc"
[[ "$chunks" -ge 3 ]] && pass ":Chunk count=${chunks}" || fail ":Chunk count=$chunks"
org=$(cypher "MATCH (c:Chunk {graph_id:'${IGID}'}) RETURN c.organisation_id LIMIT 1" | tail -1 | tr -d ' "')
[[ "$org" == "00000000-0000-0000-0000-00000000050a" ]] && pass "organisation_id stamped (${org})" || fail "org stamp=$org"
emb=$(cypher "MATCH (c:Chunk {graph_id:'${IGID}'}) WHERE c.embedding IS NOT NULL RETURN count(c)" | tail -1 | tr -d ' ')
[[ "$emb" -ge 3 ]] && pass "chunks carry embeddings (${emb})" || fail "embeddings missing ($emb)"

step "8. idempotent re-ingest (deterministic ids -> MERGE, no duplicate nodes)"
job2=$(curl -fsS "${AUTH[@]}" -X POST "${BASE}/api/v1/graphs/${IGID}/ingest" \
  -d '{"content":"Ada Lovelace wrote the first algorithm.\n\nCharles Babbage designed the Analytical Engine.\n\nThey collaborated in the 1840s.","filename":"history.txt"}')
jid2=$(echo "$job2" | jget "['id']")
for i in $(seq 1 30); do
  s=$(curl -fsS "${AUTH[@]}" "${BASE}/api/v1/graphs/${IGID}/jobs/${jid2}" | jget "['status']")
  [[ "$s" == "completed" || "$s" == "failed" ]] && break; sleep 2
done
chunks2=$(cypher "MATCH (c:Chunk {graph_id:'${IGID}'}) RETURN count(c)" | tail -1 | tr -d ' ')
[[ "$chunks2" == "$chunks" ]] && pass "re-ingest idempotent (still ${chunks2} chunks)" || fail "duplicated: $chunks -> $chunks2"

step "9. file upload (.md)"
printf '# Title\n\nFirst paragraph.\n\nSecond paragraph.\n' > /tmp/kgs_smoke.md
up=$(curl -fsS -H "Authorization: Bearer dev-token" -F "file=@/tmp/kgs_smoke.md;type=text/markdown" \
  "${BASE}/api/v1/graphs/${IGID}/upload")
upjid=$(echo "$up" | jget "['id']")
[[ "$(echo "$up" | jget "['source_type']")" == "md" ]] && pass "md upload accepted" || fail "upload: $up"

step "10. documents list + internal schema"
docs=$(curl -fsS "${AUTH[@]}" "${BASE}/api/v1/graphs/${IGID}/documents" | jget "[0]['status']" || true)
pass "documents listed"
schema=$(curl -fsS "${AUTH[@]}" "${BASE}/internal/v1/schema/${IGID}")
echo "  schema -> ${schema}"
echo "$schema" | grep -q '"Document"' && pass "schema has :Document" || fail "no :Document in schema"
echo "$schema" | grep -q '"Chunk"' && pass "schema has :Chunk" || fail "no :Chunk in schema"

# ---------------------------------------------------------------------------
# S3 — structured (CSV / JSON) ingestion via the recipe engine (key-free, default recipe)
# ---------------------------------------------------------------------------
poll_done() {  # $1=graph $2=job
  for _ in $(seq 1 30); do
    s=$(curl -fsS "${AUTH[@]}" "${BASE}/api/v1/graphs/$1/jobs/$2" | jget "['status']")
    [[ "$s" == "completed" || "$s" == "failed" ]] && { echo "$s"; return; }; sleep 2
  done; echo "timeout"
}

step "11. ingest a CSV (default recipe) -> :Table + :Record:__Entity__"
SGID=$(curl -fsS "${AUTH[@]}" -X POST "${BASE}/api/v1/graphs" -d '{"name":"structured"}' | jget "['id']")
printf 'name,age,city\nAda,36,London\nCharles,49,Devon\nGrace,53,NY\n' > /tmp/kgs_people.csv
csvjob=$(curl -fsS -H "Authorization: Bearer dev-token" -F "file=@/tmp/kgs_people.csv;type=text/csv" \
  "${BASE}/api/v1/graphs/${SGID}/upload")
cjid=$(echo "$csvjob" | jget "['id']")
[[ "$(echo "$csvjob" | jget "['source_type']")" == "csv" ]] && pass "csv accepted" || fail "csv: $csvjob"
[[ "$(poll_done "$SGID" "$cjid")" == "completed" ]] && pass "csv job completed" || fail "csv job failed"
recs=$(cypher "MATCH (r:Record {graph_id:'${SGID}'}) RETURN count(r)" | tail -1 | tr -d ' ')
tables=$(cypher "MATCH (t:Table {graph_id:'${SGID}'}) RETURN count(t)" | tail -1 | tr -d ' ')
[[ "$recs" == "3" ]] && pass ":Record count=3" || fail ":Record count=$recs"
[[ "$tables" == "1" ]] && pass ":Table count=1" || fail ":Table count=$tables"
props=$(cypher "MATCH (r:Record {graph_id:'${SGID}'}) WHERE r.name='Ada' RETURN r.age, r.city" | tail -1)
echo "  Ada -> ${props}"
echo "$props" | grep -q "36" && pass "row properties projected (age/city)" || fail "props missing: $props"
org=$(cypher "MATCH (r:Record {graph_id:'${SGID}'}) RETURN r.organisation_id LIMIT 1" | tail -1 | tr -d ' "')
[[ "$org" == "00000000-0000-0000-0000-00000000050a" ]] && pass "organisation_id stamped" || fail "org=$org"

step "12. idempotent CSV re-ingest (deterministic recipe ids -> MERGE)"
rj=$(curl -fsS -H "Authorization: Bearer dev-token" -F "file=@/tmp/kgs_people.csv;type=text/csv" \
  "${BASE}/api/v1/graphs/${SGID}/upload" | jget "['id']")
poll_done "$SGID" "$rj" >/dev/null
recs2=$(cypher "MATCH (r:Record {graph_id:'${SGID}'}) RETURN count(r)" | tail -1 | tr -d ' ')
[[ "$recs2" == "3" ]] && pass "re-ingest idempotent (still 3 records)" || fail "duplicated: 3 -> $recs2"

step "13. custom recipe -> custom :Person label"
RECIPE='{"recipe":{"recipe_format_version":"0.2","id":"rcp_people","version":1,"status":"draft","concern":"people","applies_to":{"source_type":"csv","shape_signature":"any"},"mappings":[{"id":"p","project_to":"node","label":"Person","match":{"unit_kind":"record"},"identity":{"scheme":"deterministic","from":["column:name"]},"properties":[{"name":"age","value_from":"column:age"}]}]}}'
stored=$(curl -fsS "${AUTH[@]}" -X POST "${BASE}/api/v1/recipes" -d "$RECIPE")
[[ "$(echo "$stored" | jget "['id']")" == "rcp_people" ]] && pass "recipe stored" || fail "store: $stored"
pj=$(curl -fsS -H "Authorization: Bearer dev-token" -F "file=@/tmp/kgs_people.csv;type=text/csv" \
  -F "recipe_id=rcp_people" "${BASE}/api/v1/graphs/${SGID}/upload" | jget "['id']")
[[ "$(poll_done "$SGID" "$pj")" == "completed" ]] && pass "custom-recipe job completed" || fail "custom job failed"
persons=$(cypher "MATCH (p:Person {graph_id:'${SGID}'}) RETURN count(p)" | tail -1 | tr -d ' ')
[[ "$persons" == "3" ]] && pass ":Person count=3 (custom label)" || fail ":Person count=$persons"

step "14. inline JSON ingestion -> :Record"
JGID=$(curl -fsS "${AUTH[@]}" -X POST "${BASE}/api/v1/graphs" -d '{"name":"json"}' | jget "['id']")
jjob=$(curl -fsS "${AUTH[@]}" -X POST "${BASE}/api/v1/graphs/${JGID}/ingest" \
  -d '{"content":"[{\"name\":\"Ada\",\"age\":36},{\"name\":\"Grace\",\"age\":53}]","source_type":"json","filename":"people.json"}')
jjid=$(echo "$jjob" | jget "['id']")
[[ "$(poll_done "$JGID" "$jjid")" == "completed" ]] && pass "json job completed" || fail "json failed: $jjob"
jrecs=$(cypher "MATCH (r:Record {graph_id:'${JGID}'}) RETURN count(r)" | tail -1 | tr -d ' ')
[[ "$jrecs" == "2" ]] && pass "json :Record count=2" || fail "json records=$jrecs"

printf '\n\033[32mS1+S2+S3 smoke passed.\033[0m  text/doc + CSV/JSON (recipe engine) -> real org-scoped graph, key-free, idempotent.\n'
