#!/usr/bin/env bash
# Run the deployed-stack e2e suite THROUGH THE GATEWAY and print a PASS/FAIL banner to paste into
# the PR. GitHub CI cannot run these (it has no deployed stack), so the implementer runs this LOCALLY
# before opening the PR, and the CTO re-runs it at merge (FUCK_CLAUDE_FUCK_PAPERCLIP.md rules 3 & 4).
#
#   scripts/e2e.sh            # deterministic suite (fake LLM): -m "e2e and not byom"
#   scripts/e2e.sh --up       # bring the stack up (fake LLM) first, then run the deterministic suite
#   scripts/e2e.sh --byom     # BYOM real-LLM run: harness -> LIVE, -m byom (needs OPENROUTER_API_KEY)
#   scripts/e2e.sh --oauth    # OAuth login: bring up a real dex OIDC provider, -m oauth
#   scripts/e2e.sh --github   # real-github.com deliver-back O7 proof (#542), -m github (deploy/.env PAT)
#   scripts/e2e.sh --all      # deterministic (fake) THEN BYOM (live), restoring fake at the end
#
# Two LLM modes are mutually exclusive in one stack: the deterministic team-run asserts scripted
# transitions (fake), while the BYOM test asserts a real completion (live). The suite auto-skips when
# the gateway (:8006) is unreachable, so a green run means it really ran.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose --env-file deploy/.env -f deploy/docker-compose.yml -f deploy/docker-compose.dev-ports.yml"
OAUTH_COMPOSE="$COMPOSE -f deploy/docker-compose.e2e-oauth.yml"

run_oauth() {
  echo ">> bringing up a real dex OIDC provider + configuring the auth-service…"
  $OAUTH_COMPOSE up -d --wait dex
  $OAUTH_COMPOSE up -d --force-recreate --no-deps auth-service
  # wait for the RECREATED auth-service to be back AND configured (dex listed), not just the gateway
  for _ in $(seq 1 30); do
    curl -fsS http://localhost:8006/oauth/providers 2>/dev/null | grep -q '"dex"' && break
    sleep 1
  done
  echo ">> OAuth login e2e through the gateway (real dex, real password)…"
  uv run pytest tests/e2e -m oauth -v -p no:cacheprovider && _banner "OAuth (real dex)"
}

_recreate_harness() {  # $1 = fake|live
  echo ">> harness -> HARNESS_LLM_MODE=$1"
  HARNESS_LLM_MODE="$1" $COMPOSE up -d --force-recreate --no-deps harness-runtime-service >/dev/null
  for _ in $(seq 1 20); do curl -fsS http://localhost:8007/health >/dev/null 2>&1 && return 0; sleep 1; done
  echo "!! harness did not become healthy" >&2; return 1
}

_setup_gitea() {  # arm the deliver-back (#515) forge: mint admin/token (shared script) + relax egress
  # the admin/token mint + GITEA_* shape lives in ONE shared script (scripts/setup-gitea-e2e.sh) the
  # CI deployed-stack-e2e step also calls, so the gitea setup never drifts (the CTO's no-drift fix).
  local kv
  if ! kv=$(COMPOSE="$COMPOSE" bash scripts/setup-gitea-e2e.sh); then
    echo ">> deliver-back e2e (#515) will SKIP (gitea setup unavailable)"; return 0
  fi
  while IFS= read -r line; do [[ -n "$line" ]] && export "${line?}"; done <<< "$kv"
  # the github-sink must reach gitea:3000 (a single-label private host) → recreate the registry with
  # the single-tenant egress knob on (default off; IMDS/metadata stay blocked in either mode). CI
  # sets this knob as a job env at `up` instead, so it does not need this recreate.
  CAPABILITY_REGISTRY_ALLOW_PRIVATE_EGRESS=true $COMPOSE up -d --force-recreate --no-deps \
    capability-registry-service >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do curl -fsS http://localhost:8001/health >/dev/null 2>&1 && break; sleep 1; done
  echo ">> gitea armed + registry egress-relaxed — deliver-back e2e (#515) will RUN"
}

_require_gateway() {
  curl -fsS http://localhost:8006/health >/dev/null 2>&1 && return 0
  echo "!! gateway :8006 is NOT reachable — bring the stack up first (scripts/e2e.sh --up)." >&2
  echo "   The e2e suite would otherwise SKIP, which does NOT count as run (rules 3 & 4)." >&2
  exit 2
}

_banner() {  # $1 = label
  echo ""; echo "========================================================"
  echo "  DEPLOYED-STACK E2E ($1): PASS — paste this into the PR body"
  echo "  (gateway :8006, real services, no fakes)"
  echo "========================================================"
}

MODE="${1:-}"
[[ "$MODE" == "--up" ]] && { echo ">> bringing the stack up (fake LLM)…"; HARNESS_LLM_MODE=fake $COMPOSE up -d --wait; }
_require_gateway

run_deterministic() {
  _recreate_harness fake
  _setup_gitea
  echo ">> deterministic e2e through the gateway (fake LLM)…"
  uv run pytest tests/e2e -m "e2e and not byom and not oauth" -v -p no:cacheprovider \
    && _banner "deterministic"
}

run_byom() {
  : "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY to the BYOM model key for --byom}"
  _recreate_harness live
  echo ">> BYOM real-LLM e2e through the gateway (live LLM, user-supplied key)…"
  uv run pytest tests/e2e -m byom -v -p no:cacheprovider && _banner "BYOM real-LLM"
}

run_github() {  # the real-github.com keyed O7 proof (#542) — human-gated, deselected in CI like byom
  # creds from deploy/.env (Reza-provided, local only; the PAT needs Contents + Pull-requests write
  # on the test repo). The sink reaches api.github.com (public) — no egress knob / harness needed.
  local pat repo
  pat=$(grep -E '^GITHUB_DELIVER_PAT=' deploy/.env 2>/dev/null | head -1 | cut -d= -f2-)
  repo=$(grep -E '^GITHUB_DELIVER_REPO=' deploy/.env 2>/dev/null | head -1 | cut -d= -f2-)
  export GITHUB_DELIVER_PAT="${GITHUB_DELIVER_PAT:-$pat}"
  export GITHUB_DELIVER_REPO="${GITHUB_DELIVER_REPO:-$repo}"
  : "${GITHUB_DELIVER_PAT:?set GITHUB_DELIVER_PAT in deploy/.env (or env) for --github}"
  : "${GITHUB_DELIVER_REPO:?set GITHUB_DELIVER_REPO in deploy/.env (or env) for --github}"
  echo ">> real-github deliver-back O7 e2e through the gateway (configured-not-passed; real PR)…"
  uv run pytest tests/e2e -m github -v -p no:cacheprovider && _banner "real-github O7 (#542)"
}

case "$MODE" in
  --byom)   run_byom ;;
  --oauth)  run_oauth ;;
  --github) run_github ;;
  --all)    run_deterministic; run_byom; _recreate_harness fake ;;  # leave the stack deterministic
  *)        run_deterministic ;;
esac
