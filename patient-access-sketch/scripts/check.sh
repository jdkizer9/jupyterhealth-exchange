#!/bin/sh
# Tests, import-linter contracts and ruff, all in the web container. Usage: scripts/check.sh [--fix]
set -eu
cd "$(dirname "$0")/.."
RUFF="uv tool run ruff@0.16.9"
if [ "${1:-}" = "--fix" ]; then
  exec docker compose run --rm -T web sh -c "$RUFF check --fix . && $RUFF format ."
fi
exec docker compose run --rm -T web sh -c "pytest && $RUFF check . && $RUFF format --check ."
