#!/usr/bin/env bash
# Bring up an ephemeral isolated substrate stack for a given ticket.
# Usage: ./scripts/stack-up.sh <ticket>  (e.g. ./scripts/stack-up.sh ora-42)
#
# Discovers OS-assigned host ports, writes .stack-env in the repo root,
# and registers the stack in .stack-registry.json.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TICKET="${1:-}"
if [[ -z "$TICKET" ]]; then
  echo "usage: $0 <ticket>" >&2
  exit 2
fi

# Normalise ticket to lowercase (e.g. ORA-42 → ora-42)
TICKET="$(echo "$TICKET" | tr '[:upper:]' '[:lower:]')"
PROJECT="oraclous-${TICKET}"

COMPOSE_FILES="-f $REPO_ROOT/deploy/docker-compose.yml -f $REPO_ROOT/deploy/docker-compose.agent.yml"

echo "Starting stack: $PROJECT"
COMPOSE_PROJECT_NAME="$PROJECT" docker compose $COMPOSE_FILES up -d

# Discover host ports
discover_port() {
  local service="$1"
  local container_port="$2"
  docker compose -p "$PROJECT" $COMPOSE_FILES port "$service" "$container_port" | cut -d: -f2
}

echo "Discovering ports..."
NEO4J_BOLT_PORT="$(discover_port neo4j 7687)"
NEO4J_HTTP_PORT="$(discover_port neo4j 7474)"
POSTGRES_PORT="$(discover_port postgres 5432)"
REDIS_PORT="$(discover_port redis 6379)"
JAEGER_UI_PORT="$(discover_port jaeger 16686)"
OTLP_GRPC_PORT="$(discover_port jaeger 4317)"
OTLP_HTTP_PORT="$(discover_port jaeger 4318)"

STACK_ENV_FILE="$REPO_ROOT/.stack-env"
cat > "$STACK_ENV_FILE" <<EOF
export STACK_PROJECT=${PROJECT}
export STACK_TICKET=${TICKET}
export NEO4J_BOLT_PORT=${NEO4J_BOLT_PORT}
export NEO4J_HTTP_PORT=${NEO4J_HTTP_PORT}
export POSTGRES_PORT=${POSTGRES_PORT}
export REDIS_PORT=${REDIS_PORT}
export JAEGER_UI_PORT=${JAEGER_UI_PORT}
export OTLP_GRPC_PORT=${OTLP_GRPC_PORT}
export OTLP_HTTP_PORT=${OTLP_HTTP_PORT}
EOF

BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
REGISTRY_FILE="$REPO_ROOT/.stack-registry.json"

python3 - <<PYEOF
import json, os

registry_path = "$REGISTRY_FILE"
registry = {}
if os.path.exists(registry_path):
    with open(registry_path) as f:
        try:
            registry = json.load(f)
        except json.JSONDecodeError:
            pass

registry["$PROJECT"] = {
    "ticket": "$TICKET",
    "branch": "$BRANCH",
    "ports": {
        "neo4j_bolt": int("$NEO4J_BOLT_PORT"),
        "neo4j_http": int("$NEO4J_HTTP_PORT"),
        "postgres": int("$POSTGRES_PORT"),
        "redis": int("$REDIS_PORT"),
        "jaeger_ui": int("$JAEGER_UI_PORT"),
        "otlp_grpc": int("$OTLP_GRPC_PORT"),
        "otlp_http": int("$OTLP_HTTP_PORT"),
    },
    "started_at": "$STARTED_AT",
}

with open(registry_path, "w") as f:
    json.dump(registry, f, indent=2)
    f.write("\n")
PYEOF

echo ""
echo "Stack: $PROJECT"
echo "Source ports: . .stack-env"
