#!/usr/bin/env bash
# Run the deployed-stack e2e suite THROUGH THE GATEWAY and print a PASS/FAIL banner to paste into
# the PR. GitHub CI cannot run these (it has no deployed stack), so the implementer runs this LOCALLY
# before opening the PR, and the CTO re-runs it at merge (FUCK_CLAUDE_FUCK_PAPERCLIP.md rules 3 & 4).
#
#   scripts/e2e.sh            # run against an already-up stack
#   scripts/e2e.sh --up       # bring the stack up (fake LLM) first, then run
#
# The suite auto-skips when the gateway (:8006) is unreachable, so a green run means it really ran.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev-ports.yml"

if [[ "${1:-}" == "--up" ]]; then
  echo ">> bringing the stack up (HARNESS_LLM_MODE=fake)…"
  HARNESS_LLM_MODE=fake $COMPOSE up -d --wait
fi

if ! curl -fsS http://localhost:8006/health >/dev/null 2>&1; then
  echo "!! gateway :8006 is NOT reachable — bring the stack up first (scripts/e2e.sh --up)." >&2
  echo "   The e2e suite would otherwise SKIP, which does NOT count as run (rules 3 & 4)." >&2
  exit 2
fi

echo ">> running e2e through the gateway…"
if uv run pytest tests/e2e -m e2e -v -p no:cacheprovider; then
  echo ""
  echo "========================================================"
  echo "  DEPLOYED-STACK E2E: PASS — paste this into the PR body"
  echo "  (gateway :8006, real services, no fakes)"
  echo "========================================================"
else
  echo ""
  echo "!! DEPLOYED-STACK E2E: FAIL — do NOT open/merge the PR." >&2
  exit 1
fi
