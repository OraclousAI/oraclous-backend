#!/usr/bin/env bash
# Test runner for oraclous-backend (mirrors the legacy test.sh ergonomics).
#   scripts/test.sh              # unit tests (fast, no Docker)
#   scripts/test.sh integration  # integration tests (real substrate via testcontainers; needs Docker)
#   scripts/test.sh security     # security-marked tests
#   scripts/test.sh isolation    # cross-org / organisation-isolation tests
#   scripts/test.sh all          # the whole suite
set -euo pipefail
cd "$(dirname "$0")/.."

TYPE="${1:-unit}"
case "$TYPE" in
  unit)        uv run pytest -m unit ;;
  integration) uv run pytest -m integration ;;
  security)    uv run pytest -m security ;;
  isolation)   uv run pytest -m "isolation or organization_isolation" ;;
  all)         uv run pytest ;;
  *) echo "usage: $0 {unit|integration|security|isolation|all}" >&2; exit 2 ;;
esac
