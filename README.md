# osprey-core: Service Release & Operations Console

A Flask console for releasing and operating a Cloud Run service behind IAP. Computed values come
from code that ran: SLOs from live request telemetry, the DP-001 scorecard from a shadow replay of
curated scenario traces through the real PreToolUse guard, and audit receipts from HMAC-SHA256 over
canonical JSON. Illustrative content is labelled as such: the workflow progress and receipts are a
sample release scenario, the rollout ledger is sample data (not live Cloud Run traffic), and task
keys are a projection. Every label is returned by the API (`source_label`) and shown in the UI.
Acceptance criteria and the tests that prove them are in `SPEC.md`.

## Run locally

```bash
python3 -m venv ../.venv && ../.venv/bin/pip install -r requirements-dev.txt
export AUDIT_HMAC_KEY="$(openssl rand -hex 32)"
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider
PORT=8080 ../.venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 8 app.app:app
```

Browser and HTTP sweeps against a running server (Playwright is a separate install):

```bash
python3 qa/smoke_endpoints.py http://127.0.0.1:8080 /tmp/smoke.json yes
python3 qa/e2e_console.py http://127.0.0.1:8080 /tmp/e2e local
```

## Container

`docker build -t osprey-core .` then `docker run -p 8080:8080 -e AUDIT_HMAC_KEY=... osprey-core`.
The image runs as a non-root user and binds `0.0.0.0:${PORT:-8080}`. Gunicorn settings come from
`GUNICORN_WORKERS` (default 1), `GUNICORN_THREADS` (default 8), and `GUNICORN_TIMEOUT` (default 0).

## CI/CD

Releases follow the hybrid workflow in `WORKFLOW.md`. S5 and S6 are deterministic scripts; agents do not
improvise them. The DP-001 guard blocks these commands before their stage.

- CI at S5_RELEASE_GATE: `python3 scripts/s5_release_gate.py --commit <HEAD sha>` runs
  `gcloud builds submit --config=cloudbuild.yaml`. Cloud Build runs pytest, builds and smoke-tests the
  image, and pushes `us-central1-docker.pkg.dev/gement02/osprey-scion/osprey-core:$SHORT_SHA`. The script
  writes the immutable `@sha256` release packet to `.workflow/evidence/s5_release_packet.json`. A failed
  step bounces the release to S3_BUILD or S4_VERIFY_QA.
- Register the Cloud Deploy pipeline once: `gcloud deploy apply --file=clouddeploy.yaml --region=us-central1 --project=gement02`
  (delivery pipeline osprey-core-cd, target osprey-core-prod).
- CD at S6_DEPLOY_CLOSE: `python3 scripts/s6_deploy.py --packet .workflow/evidence/s5_release_packet.json`
  runs `gcloud deploy releases create` with the packet image. The service stays on Cloud Run us-central1
  behind IAP (ingress all, min and max 1 instance, runtime service account osprey-core-run, execution
  service account osprey-onboard-sa).
- Exit gate: `scripts/s6_exit.py` probes IAP (unauthenticated 302 or 401, authenticated 200) and checks
  that GET `/api/pipeline` provenance matches the packet.
- Rollback: `python3 scripts/s6_rollback.py --execute` runs `gcloud deploy targets rollback` with a
  traffic fallback, then the release bounces to S3_BUILD. Without `--execute` the script is a dry-run.
- Whole loop: `python3 scripts/workflow_ctl.py release --commit <sha>`.
- IAM for the pipeline: the execution service account osprey-onboard-sa has `roles/clouddeploy.jobRunner`
  (plus its existing Cloud Run deploy roles). The runtime service account osprey-core-run has read-only
  `roles/cloudbuild.builds.viewer` and `roles/clouddeploy.viewer` (on osprey-core-cd only), so GET
  `/api/pipeline` can read live status. A release never changes the service IAM policy.
- Agent templates: the 13 agent templates of the team live in `templates/`.

## Health checks on Cloud Run

Cloud Run's front end reserves some URL paths ending in `z` (for example `/healthz`): requests to
them can be answered by Google with a 404 and never reach the container. Through the service URL,
and for Cloud Run startup and liveness probes, use:

- `GET /health`: liveness (no config needed).
- `GET /ready`: readiness (every config file validates; `audit_key_configured` as a boolean). `GET /readyz` is the same handler.
- `GET /healthz`: kept for container-local checks such as the Docker HEALTHCHECK.

## Environment

- `AUDIT_HMAC_KEY` (required for receipts): HMAC key of at least 32 bytes; store it in Secret Manager. Without it the receipts API returns 503.
- `PORT` (set by Cloud Run): listen port, default 8080.
- `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT` (optional): server tuning.
- `K_SERVICE`, `K_REVISION` (set by Cloud Run): shown in the console header (live values).
- Provenance variables named in `config/cicd.json` (`provenance_env`, set by the CD release): commit, build id, image digest, release, pipeline, and target for GET `/api/pipeline`.
- `WORKFLOW_STAGE`, `DP001_POLICY_VERSION`, `DP001_POLICY_PATH` (hook only): inputs for the DP-001 guard.
- `GOOGLE_CLOUD_PROJECT` (rollback only): project for `scripts/s6_rollback.py`.

## Endpoints

- `GET /health`, `GET /healthz`: liveness. `GET /ready`, `GET /readyz`: readiness.
- `GET /api/ui-config`, `/api/health`, `/api/rollouts`, `/api/workflow`, `/api/agents`, `/api/telemetry?limit=N`
- `GET /api/pipeline`: live Cloud Build and Cloud Deploy status and provenance for the serving revision.
- `GET /api/task-backends`, `GET|POST /api/task-backend` (`{"backend": "jira"}`; 400 on unknown)
- `GET /api/dreams`, `POST /api/dreams/replay`: DP-001 proposal and the live shadow replay.
- `GET /api/receipts`, `POST /api/receipts/verify` (`{"receipt", "signature"}` or `{"chain": [...]}`)

Errors always use `{"error": {"code", "taxonomy", "message", "trace_id", ...}}`; every response
carries `X-Trace-Id`, and each request logs one JSON line to stdout.

## Layout

- `app/`: Flask app, config store, telemetry, health, receipts, UI (templates and static).
- `config/`: every tunable and labelled sample record, validated by `config/config_schema.json`.
- `dreamstore/`: DP-001 guard (`hooks/`), shadow replay, curated eval set, computed scorecard and proposal.
- `scripts/`: the deterministic S5 and S6 scripts (`scripts/s5_release_gate.py`, `scripts/s6_deploy.py`,
  `scripts/s6_exit.py`, `scripts/s6_rollback.py`, `scripts/workflow_ctl.py`) and `scripts/nightly_dream_evolver.py`.
- `.workflow/`: hybrid workflow definition and the pluggable task backend registry.
- `templates/`: the 13 agent templates exported from the Hub.
- `qa/`: HTTP smoke and Playwright browser sweep run against a live server.
- `tests/`: happy-path, edge-case, chaos, honesty, AST zero-hardcoding, wiring, and hygiene tests.
