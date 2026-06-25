#!/usr/bin/env bash
# Arm the deliver-back (#515) gitea forge for a deployed-stack e2e: mint the forge admin + a scoped
# token and emit the GITEA_* env as KEY=value on STDOUT (diagnostics go to stderr). SHARED by
# scripts/e2e.sh (local) and the ci.yml deployed-stack-e2e step so the gitea setup never drifts —
# without it `requires_gitea` SKIPS and the O7 proof never actually runs (a skip is not a pass).
#
# Exits non-zero when gitea is unreachable / the token cannot be minted, so CI fails LOUD (the local
# runner treats a non-zero as "skip"). The registry's egress relaxation
# (CAPABILITY_REGISTRY_ALLOW_PRIVATE_EGRESS — needed so the sink reaches gitea:3000, a single-label
# private host) is the CALLER's job: CI sets it as a job env at `up`; scripts/e2e.sh force-recreates
# the registry with it after this script. Gitea is reached on the host at :3001 (dev-ports overlay);
# both callers publish it there.
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev-ports.yml}"
GITEA_HOST="${GITEA_HOST:-http://localhost:3001}"
log() { echo ">> [gitea-setup] $*" >&2; }

curl -fsS "${GITEA_HOST}/api/healthz" >/dev/null 2>&1 || {
  log "gitea (${GITEA_HOST}) not reachable — is the 'gitea' service up (profiles: services + dev-ports)?"
  exit 1
}

# idempotent admin (ignore 'user already exists'); the gitea CLI runs as the git user
$COMPOSE exec -u git -T gitea gitea admin user create \
  --admin --username oraclous --password oraclous-e2e \
  --email oraclous@example.com --must-change-password=false >/dev/null 2>&1 || true

tok=$(curl -fsS -u oraclous:oraclous-e2e -X POST "${GITEA_HOST}/api/v1/users/oraclous/tokens" \
        -H 'Content-Type: application/json' \
        -d "{\"name\":\"e2e-$$-${RANDOM}\",\"scopes\":[\"write:repository\",\"write:user\"]}" \
        2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("sha1",""))' \
        2>/dev/null) || true
[[ -n "${tok:-}" ]] || { log "could not mint a gitea access token"; exit 1; }

log "minted admin + scoped token; emitting GITEA_* for the deliver-back e2e"
printf 'GITEA_API_BASE=%s/api/v1\n' "$GITEA_HOST"
printf 'GITEA_INTERNAL_BASE=http://gitea:3000/api/v1\n'
printf 'GITEA_TOKEN=%s\n' "$tok"
