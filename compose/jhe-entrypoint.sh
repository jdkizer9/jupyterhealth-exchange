#!/bin/sh
# Dev-only entrypoint for the `jhe` and `cron` services in docker-compose.yml.
# The production image (Dockerfile / fly.toml) does not use this.
#
#   JHE_SETUP=1 (default)  migrate, then seed if the database has no users yet
#   JHE_SETUP=0            skip both (cron: it waits for `jhe` to be healthy instead,
#                          so the two containers never race on an empty database)
set -eu
cd /code

if [ "${JHE_SETUP:-1}" = "1" ]; then
    python manage.py migrate --noinput

    # `seed` only works on an empty database, so run it once. "Has any user" is the
    # marker: seed's first step creates the superuser. Re-seed with scripts/dev/reset.sh.
    if python manage.py shell -v 0 -c "import sys; from core.models import JheUser; sys.exit(0 if JheUser.objects.exists() else 1)"; then
        echo "jhe-entrypoint: database already seeded, skipping seed"
    else
        echo "jhe-entrypoint: empty database, running seed"
        python manage.py seed
    fi
fi

exec "$@"
