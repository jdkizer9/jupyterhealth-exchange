#!/usr/bin/env bash
# Run every quick check, without changing any files. Exits non-zero if any check fails.
#
#   scripts/dev/check.sh           lint, format, migrations, backend tests, frontend tests
#   scripts/dev/check.sh --quick   skip the test suites
#
# To auto-fix lint and formatting instead, run the pre-commit hooks:
#   uv run pre-commit run --all-files
set -uo pipefail
cd "$(dirname "$0")/../.."

quick=0
[ "${1:-}" = "--quick" ] && quick=1

failed=()
run() {
    local name="$1"
    shift
    echo
    echo "==> $name"
    if "$@"; then
        echo "--- $name: ok"
    else
        echo "--- $name: FAILED"
        failed+=("$name")
    fi
}

# Use the ruff version pinned for pre-commit, so local checks match CI.
ruff_version=$(awk '/ruff-pre-commit/ {getline; if (match($0, /v[0-9]+\.[0-9]+\.[0-9]+/)) print substr($0, RSTART + 1, RLENGTH - 1)}' .pre-commit-config.yaml)
ruff=(uvx "ruff@${ruff_version:?could not read ruff version from .pre-commit-config.yaml}")

# Same file set as the pre-commit hooks: tracked Python and notebook files only (a bare
# `ruff format .` also formats code blocks in Markdown, which pre-commit doesn't), with
# pyproject's excludes still applied to the explicit file list.
ruff_on_python_files() {
    git ls-files -z -- '*.py' '*.ipynb' ':!:data/schema/**' | xargs -0 "${ruff[@]}" "$@" --force-exclude
}
run "ruff check" ruff_on_python_files check
run "ruff format" ruff_on_python_files format --check

# makemigrations needs settings and a database for its history check; borrow the test DB.
migrations_check() {
    docker compose -f docker-compose.test.yml up -d --wait test-db >/dev/null &&
        (
            # shellcheck disable=SC1091
            source ci/test_env.sh
            export DB_HOST=127.0.0.1 DB_PORT="${JHE_TEST_DB_PORT:-5433}"
            uv run python manage.py makemigrations --check --dry-run
        )
}
run "missing migrations" migrations_check

if [ "$quick" = 0 ]; then
    run "backend tests" scripts/dev/test.sh
    if [ -d tests/frontend/node_modules ]; then
        run "frontend tests" bash -c "cd tests/frontend && npm test --silent"
    else
        echo
        echo "==> frontend tests: skipped (run 'cd tests/frontend && npm ci' once to enable)"
    fi
fi

echo
if [ ${#failed[@]} -gt 0 ]; then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
echo "All checks passed."
