#!/bin/sh
# Prepare the running JHE dev stack for scenario B and print a fresh invitation link.
#   eval "$(scripts/jhe-setup.sh | grep DATA_SOURCE_ID | sed 's/^/export PA_JHE_/')"
set -eu
cd "$(dirname "$0")/../.."   # the JHE repo root, where JHE's docker-compose.yml lives
docker compose exec -T jhe python manage.py shell -v 0 < patient-access-sketch/scripts/jhe_setup.py
