"""Flask application factory for the Service Release & Operations Console.

Gunicorn entrypoint: `app.app:app`. Every request gets a uuid4 trace id (echoed in X-Trace-Id),
a perf_counter latency, and one structured JSON telemetry line; security headers are always set.
"""

from __future__ import annotations

import os
import time
from http import HTTPStatus
from pathlib import Path

from flask import Flask, Response, request

from app import telemetry
from app.config_store import REPO_ROOT, ConfigError, ConfigStore
from app.errors import register_error_handlers
from app.routes_core import core_bp
from app.routes_ops import ops_bp
from app.workflow_service import TaskBackendService

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
NO_STORE_PREFIXES = ("/api/", "/health", "/readyz")


def _security_headers(response: Response) -> None:
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith(NO_STORE_PREFIXES):
        response.headers["Cache-Control"] = "no-store"


def _severity(status_code: int) -> str:
    if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return "ERROR"
    if status_code >= HTTPStatus.BAD_REQUEST:
        return "WARNING"
    return "INFO"


def create_app(root: Path | str | None = None) -> Flask:
    """Build the app around a ConfigStore rooted at `root` (the repository by default)."""
    store = ConfigStore(root or REPO_ROOT)
    app = Flask(__name__)
    app.extensions["console.store"] = store
    app.extensions["console.backends"] = TaskBackendService(store)

    @app.before_request
    def _start_request() -> None:
        request.environ["console.started"] = time.perf_counter()
        trace_id = telemetry.new_trace_id()
        request.environ["console.trace_id"] = trace_id
        request.environ["console.trace_token"] = telemetry.bind_trace_id(trace_id)
        try:
            settings = store.get("app_settings")
        except ConfigError:
            # Already logged by ConfigStore; each handler re-raises it and answers 503 (fail closed).
            return
        telemetry.RING.configure(settings["telemetry"]["ring_size"])
        request.max_content_length = settings["server"]["max_request_bytes"]

    @app.after_request
    def _finish_request(response: Response) -> Response:
        trace_id = request.environ.get("console.trace_id") or telemetry.current_trace_id()
        response.headers["X-Trace-Id"] = trace_id
        _security_headers(response)
        if not request.path.startswith("/static/"):
            started = request.environ.get("console.started", time.perf_counter())
            failed = response.status_code >= HTTPStatus.BAD_REQUEST
            telemetry.emit({
                "kind": "http_request",
                "op": f"{request.method} {request.path}",
                "method": request.method,
                "path": request.path,
                "http_status": response.status_code,
                "latency_ms": telemetry.elapsed_ms(started),
                "trace_id": trace_id,
                "status": telemetry.STATUS_ERROR if failed else telemetry.STATUS_OK,
                "severity": _severity(response.status_code),
                "error_taxonomy": request.environ.get("console.error_taxonomy", telemetry.NO_ERROR),
            })
        return response

    @app.teardown_request
    def _end_request(_exc: BaseException | None) -> None:
        token = request.environ.pop("console.trace_token", None)
        if token is not None:
            telemetry.reset_trace_id(token)

    register_error_handlers(app)
    app.register_blueprint(core_bp)
    app.register_blueprint(ops_bp)
    return app


app = create_app()


if __name__ == "__main__":
    server = ConfigStore().get("app_settings")["server"]
    app.run(host=server["bind_host"], port=int(os.environ.get(server["port_env_var"], server["default_port"])))
