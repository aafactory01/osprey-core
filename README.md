# osprey-core: Service Release & Operations Console

A Flask console for releasing and operating a Cloud Run service behind IAP. Every number on screen
comes from code that ran: SLOs from live request telemetry, the DP-001 scorecard from a shadow replay
through the real PreToolUse guard, and audit receipts from HMAC-SHA256 over canonical JSON.

## Run locally

```bash
python3 -m venv ../.venv && ../.venv/bin/pip install -r requirements-dev.txt
export AUDIT_HMAC_KEY="$(openssl rand -hex 32)"
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider
PORT=8080 ../.venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 8 app.app:app
```

## Container

`docker build -t osprey-core .` then `docker run -p 8080:8080 -e AUDIT_HMAC_KEY=... osprey-core`.
The image runs as a non-root user and binds `0.0.0.0:${PORT:-8080}`. Gunicorn settings come from
`GUNICORN_WORKERS` (default 1), `GUNICORN_THREADS` (default 8), and `GUNICORN_TIMEOUT` (default 0).

## Environment

- `AUDIT_HMAC_KEY` (required for receipts): HMAC key of at least 32 bytes; store it in Secret Manager. Without it the receipts API returns 503.
- `PORT` (set by Cloud Run): listen port, default 8080.
- `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT` (optional): server tuning.
- `K_SERVICE`, `K_REVISION` (set by Cloud Run): shown in the console header.
- `WORKFLOW_STAGE`, `DP001_POLICY_VERSION`, `DP001_POLICY_PATH` (hook only): inputs for the DP-001 guard.
- `GOOGLE_CLOUD_PROJECT` (rollback only): project for `scripts/s6_rollback.py`.

## Endpoints

- `GET /healthz`, `GET /health`: liveness (no config needed). `GET /readyz`: config validation plus `audit_key_configured`.
- `GET /api/ui-config`, `/api/health`, `/api/rollouts`, `/api/workflow`, `/api/agents`, `/api/telemetry?limit=N`
- `GET /api/task-backends`, `GET|POST /api/task-backend` (`{"backend": "jira"}`; 400 on unknown)
- `GET /api/dreams`, `POST /api/dreams/replay`: DP-001 proposal and the live shadow replay.
- `GET /api/receipts`, `POST /api/receipts/verify` (`{"receipt", "signature"}` or `{"chain": [...]}`)

Errors always use `{"error": {"code", "taxonomy", "message", "trace_id", ...}}`; every response
carries `X-Trace-Id`, and each request logs one JSON line to stdout.

## Layout

- `app/`: Flask app, config store, telemetry, health, receipts, UI (templates and static).
- `config/`: every tunable and demo record, validated by `config/config_schema.json`.
- `dreamstore/`: DP-001 guard (`hooks/`), shadow replay, eval set, computed scorecard and proposal.
- `scripts/`: `nightly_dream_evolver.py` (S5) and `s6_rollback.py` (S6).
- `.workflow/`: hybrid workflow definition and the pluggable task backend registry.
- `tests/`: happy-path, edge-case, chaos, AST zero-hardcoding, and hygiene tests.
