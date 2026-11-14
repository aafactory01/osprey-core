# Service Release & Operations Console (osprey-core) - Cloud Run image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=8 \
    GUNICORN_TIMEOUT=0

WORKDIR /srv/osprey-core

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Code and config stay root-owned and read-only for the runtime user.
COPY . .

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=4)"

# One worker keeps the per-process task-backend selection consistent; threads serve concurrency.
# GUNICORN_TIMEOUT=0 defers request timeouts to Cloud Run.
CMD exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --workers "${GUNICORN_WORKERS:-1}" --threads "${GUNICORN_THREADS:-8}" \
    --timeout "${GUNICORN_TIMEOUT:-0}" --error-logfile - app.app:app
