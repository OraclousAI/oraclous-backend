#!/usr/bin/env bash
# Tear down an ephemeral isolated substrate stack.
# Usage: ./scripts/stack-down.sh <ticket>
#        ./scripts/stack-down.sh          (uses $STACK_TICKET env var)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TICKET="${1:-${STACK_TICKET:-}}"
if [[ -z "$TICKET" ]]; then
  echo "usage: $0 <ticket>   or set STACK_TICKET" >&2
  exit 2
fi

TICKET="$(echo "$TICKET" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]-')"
if [[ -z "$TICKET" ]]; then
  echo "error: ticket normalised to empty string" >&2
  exit 2
fi
PROJECT="oraclous-${TICKET}"

COMPOSE_FILES="-f $REPO_ROOT/deploy/docker-compose.yml -f $REPO_ROOT/deploy/docker-compose.agent.yml"

echo "Stopping stack: $PROJECT"
docker compose -p "$PROJECT" $COMPOSE_FILES down -v

# Remove registry entry
REGISTRY_FILE="$REPO_ROOT/.stack-registry.json"
if [[ -f "$REGISTRY_FILE" ]]; then
  python3 - <<PYEOF
import json, os

registry_path = "$REGISTRY_FILE"
with open(registry_path) as f:
    try:
        registry = json.load(f)
    except json.JSONDecodeError:
        registry = {}

registry.pop("$PROJECT", None)

with open(registry_path, "w") as f:
    json.dump(registry, f, indent=2)
    f.write("\n")
PYEOF
fi

# Delete .stack-env if it belongs to this ticket
STACK_ENV_FILE="$REPO_ROOT/.stack-env"
if [[ -f "$STACK_ENV_FILE" ]]; then
  if grep -q "STACK_TICKET=${TICKET}" "$STACK_ENV_FILE" 2>/dev/null; then
    rm "$STACK_ENV_FILE"
    echo "Removed .stack-env"
  fi
fi

echo "Stack $PROJECT is down."
