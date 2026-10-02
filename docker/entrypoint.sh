#!/bin/sh
# Container start: prepare the volume (as root), drop to the app user, migrate, then serve.
# Any failing step stops the container, so the server never starts on an unmigrated database.
set -eu

if [ "$(id -u)" = "0" ]; then
    python -m app.scripts.prepare_volume
    exec setpriv --reuid=app --regid=app --init-groups "$0" "$@"
fi

python -m app.scripts.prepare_volume
alembic upgrade head

# One worker only: APScheduler runs inside the process, so a second worker would send every reminder twice.
# --proxy-headers only trusts X-Forwarded-Proto/For from FORWARDED_ALLOW_IPS (default: none but localhost).
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
