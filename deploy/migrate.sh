#!/usr/bin/env bash
# #528 — apply pending DB migrations for one (or all) DB-owning service(s) WITHOUT a full image
# rebuild, by running that service's OWNER migrate one-shot (`alembic upgrade head` as the owner DSN).
#
# Why this exists: a fast TARGETED recreate (`docker compose up -d --no-deps <svc>` — the shortcut we
# use to redeploy one rebuilt service in the e2e loop) SKIPS the service's `*-migrate` depends_on, so a
# newly added migration is not applied and the app starts on a STALE schema (the #527/#528 footgun).
# The app cannot self-migrate: it connects as the NOBYPASSRLS `oraclous_app` runtime role, not the DB
# owner (ADR-030). This helper runs the owner migrate one-shot directly. Idempotent — a no-op when the
# schema is already at head.
#
# A NORMAL deploy already auto-migrates and needs no helper: a fresh `up`, a whole-stack
# `up -d --force-recreate`, a RULE-7 `up -d --build --wait` (the migrate image changes → it re-runs),
# and the Helm `pre-install,pre-upgrade` hook Jobs (every `helm upgrade`). This is ONLY the dev-loop
# convenience for the `--no-deps` fast path.
#
# Usage:
#   deploy/migrate.sh                 # migrate ALL DB-owning services
#   deploy/migrate.sh engine          # just the engine (friendly alias)
#   deploy/migrate.sh engine-migrate  # or the raw migrate one-shot service name
#
# Portable to macOS's bash 3.2 (no associative arrays).
set -euo pipefail
cd "$(dirname "$0")/.."

# Override COMPOSE to point at a specific stack (e.g. add -f deploy/docker-compose.dev-ports.yml).
# The default references deploy/.env — present from the normal stack setup; docker compose errors
# loudly if it is missing.
COMPOSE="${COMPOSE:-docker compose --env-file deploy/.env -f deploy/docker-compose.yml}"

# every owner migrate one-shot in deploy/docker-compose.yml (the 7 DB-owning services)
ALL_MIGRATES="auth-migrate credbroker-migrate capreg-migrate kgs-migrate gateway-migrate harness-migrate engine-migrate"

# friendly alias -> the owner migrate one-shot (a raw *-migrate name passes through unchanged)
migrate_service() {
  case "$1" in
    auth) echo auth-migrate ;;
    credential-broker | broker | credbroker) echo credbroker-migrate ;;
    capability-registry | capreg) echo capreg-migrate ;;
    knowledge-graph | kgs) echo kgs-migrate ;;
    gateway) echo gateway-migrate ;;
    harness) echo harness-migrate ;;
    engine) echo engine-migrate ;;
    *) echo "$1" ;;
  esac
}

run_migrate() {
  # fail closed on a typo/unknown name with a helpful list (rather than leaning on docker's
  # terser "no such service"). Every valid target resolves to one of the 7 ALL_MIGRATES names.
  case " ${ALL_MIGRATES} " in
    *" ${1} "*) ;;
    *)
      echo "migrate.sh: unknown service '${1}'." >&2
      echo "  valid: all | auth | credential-broker | capability-registry | knowledge-graph | gateway | harness | engine" >&2
      echo "  (or a raw *-migrate service name)" >&2
      exit 2
      ;;
  esac
  echo ">> ${1}: alembic upgrade head (owner DSN)…"
  # the migrate one-shots carry `profiles: [services]`; `run` targets the service explicitly.
  COMPOSE_PROFILES=services ${COMPOSE} run --rm "${1}"
}

arg="${1:-all}"
if [ "${arg}" = "all" ]; then
  for m in ${ALL_MIGRATES}; do run_migrate "${m}"; done
else
  run_migrate "$(migrate_service "${arg}")"
fi
echo ">> migrations applied (at head)."
