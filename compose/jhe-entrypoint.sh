#!/bin/sh
# Dev-only entrypoint for the `jhe` service in docker-compose.yml: migrate, seed if the
# database is empty, then run the command (runserver). The production image
# (Dockerfile / fly.toml) does not use this.
set -eu
cd /code

python manage.py migrate --noinput

# `seed` only works on an empty database, so run it once. "Has any user" is the marker:
# seed's first step creates the superuser. Re-seed with scripts/dev/reset.sh.
if python manage.py shell -v 0 -c "import sys; from core.models import JheUser; sys.exit(0 if JheUser.objects.exists() else 1)"; then
    echo "jhe-entrypoint: database already seeded, skipping seed"
else
    echo "jhe-entrypoint: empty database, running seed"
    python manage.py seed
    # Sketch: the SMART Health IT sandbox hospitals for the USCDI Patient Access picker. Only
    # here, with the seed: the command rebuilds those brands (their base URLs encode launch
    # options), so re-running it on a live database would detach earlier pulls' locations.
    echo "jhe-entrypoint: seeding the SMART sandbox hospitals for USCDI Patient Access"
    python manage.py seed_patient_access_sandbox
fi

exec "$@"
