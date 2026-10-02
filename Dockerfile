FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 1000 app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY frontend ./frontend
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# SQLite on a volume. Railway mounts it at /data (set DATABASE_URL=sqlite:////data/schreduler.db there);
# docker compose mounts /app/data. Postgres: just change DATABASE_URL (docker-compose.postgres.yml).
ENV DATABASE_URL=sqlite:////app/data/schreduler.db

EXPOSE 8000

# Railway ignores this and uses its own healthcheck path (/health); it's for docker run / compose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=3)"

# Starts as root only to prepare the volume, then runs as the "app" user (see docker/entrypoint.sh).
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
