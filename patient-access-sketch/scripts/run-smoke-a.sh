#!/bin/sh
# Scenario A end to end: deep link -> sandbox (skip-login brand) -> fetch -> Composition Bundle in
# the configured destination (PA_DESTINATION, default s3) -> receipt. Prints the job JSON.
#   scripts/run-smoke-a.sh ["Express symmetric"]
set -eu
cd "$(dirname "$0")/.."
exec docker compose exec -T web python scripts/smoke.py --brand "${1:-Express (scripted}" --until done
