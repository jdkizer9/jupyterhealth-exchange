#!/usr/bin/env bash
# Run the backend test suite against a disposable, RAM-backed Postgres
# (docker-compose.test.yml). Your dev stack and its data are never touched.
#
#   scripts/dev/test.sh                                  all of tests/backend/
#   scripts/dev/test.sh tests/backend/test_study_viewset.py -k scope
#   scripts/dev/test.sh --fresh                          recreate the test DB first
#
# The test DB container stays up between runs and pytest reuses the database, so warm
# runs skip migrations. Use --fresh after switching branches or changing migrations.
# Any other arguments go straight to pytest.
set -euo pipefail
cd "$(dirname "$0")/../.."

fresh=0
has_path=0
args=()
for arg in "$@"; do
    case "$arg" in
        --fresh) fresh=1 ;;
        *)
            args+=("$arg")
            # A file/dir (optionally with ::test_name) means the caller chose what to run.
            [ -e "${arg%%::*}" ] && has_path=1
            ;;
    esac
done
# Default to the suite CI runs. A bare `pytest` from the repo root also collects
# mcp_server/ and tests/contract/, which need their own environments.
[ "$has_path" = 1 ] || args+=(tests/backend/)

compose=(docker compose -f docker-compose.test.yml)
if [ "$fresh" = 1 ]; then
    "${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
fi
"${compose[@]}" up -d --wait test-db >/dev/null

# Same environment CI uses, pointed at the test container.
# shellcheck disable=SC1091
source ci/test_env.sh
export DB_HOST=127.0.0.1
export DB_PORT="${JHE_TEST_DB_PORT:-5433}"

db_args=(--reuse-db)
[ "$fresh" = 1 ] && db_args+=(--create-db)

exec uv run pytest "${db_args[@]}" "${args[@]}"
