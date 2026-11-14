"""GET /api/pipeline: live Cloud Build (release gate) and Cloud Deploy (deploy close) status.

The blueprint installs one PipelineStatusService per app when it is registered; tests may replace
`app.extensions[EXTENSION_KEY]` with a service built on a fake transport and clock. Upstream failures come
back as a 200 degraded document (status UNKNOWN plus a typed error); only invalid configuration answers the
structured 503 from app/errors.py. The app-level hooks add the trace id, telemetry, and no-store.
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, jsonify
from flask.blueprints import BlueprintSetupState

from app.pipeline_status import PipelineStatusService

EXTENSION_KEY = "console.pipeline"
pipeline_bp = Blueprint("pipeline", __name__)


@pipeline_bp.record_once
def _install(state: BlueprintSetupState) -> None:
    state.app.extensions.setdefault(EXTENSION_KEY, PipelineStatusService(state.app.extensions["console.store"]))


def service() -> PipelineStatusService:
    return current_app.extensions[EXTENSION_KEY]


@pipeline_bp.get("/api/pipeline")
def pipeline_status() -> Response:
    return jsonify(service().status())
