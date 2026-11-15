"""Liveness/readiness, console shell, and read-only operations endpoints."""

from __future__ import annotations

import os
from http import HTTPStatus
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, render_template, request

from app import telemetry
from app.config_store import CONFIG_SCHEMA, ConfigError, ConfigStore
from app.errors import ApiError
from app.health import compute_health
from app.workflow_service import workflow_view

core_bp = Blueprint("core", __name__)


def store() -> ConfigStore:
    return current_app.extensions["console.store"]


def _audit_key_configured(config_store: ConfigStore) -> bool:
    """Boolean only: whether AUDIT_HMAC_KEY is present and long enough (the key is never exposed)."""
    try:
        cfg = config_store.get("audit_receipts")
    except ConfigError:
        return False
    raw = os.environ.get(cfg["key_env_var"], "")
    return len(raw.encode("utf-8")) >= cfg["min_key_bytes"]


@core_bp.get("/")
def index() -> str:
    return render_template("index.html")


@core_bp.get("/healthz")
@core_bp.get("/health")
def healthz() -> Response:
    """Liveness (/healthz, alias /health): the process is serving. Deliberately independent of config."""
    return jsonify({"status": "ok"})


@core_bp.get("/readyz")
@core_bp.get("/ready")
def readyz() -> tuple[Response, int]:
    """Readiness (/readyz, alias /ready for Cloud Run URLs): every config file validates.

    Reports audit key presence as a boolean only.
    """
    report = store().check_all()
    ready = all(entry["ok"] for entry in report.values())
    body = {"ready": ready, "config": report, "audit_key_configured": _audit_key_configured(store())}
    return jsonify(body), int(HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE)


@core_bp.get("/api/ui-config")
def ui_config() -> Response:
    settings = store().get("app_settings")
    cicd = store().get("cicd")
    runtime = settings["runtime"]
    short_sha_env = cicd["provenance_env"]["short_sha"]
    return jsonify({
        "service_name": settings["service_name"],
        "console_title": settings["console_title"],
        "cloud_run_service": os.environ.get(runtime["service_env_var"]) or runtime["unset_label"],
        "revision": os.environ.get(runtime["revision_env_var"]) or runtime["unset_label"],
        "short_sha": os.environ.get(short_sha_env) or runtime["unset_label"],
        "ui": settings["ui"],
    })


@core_bp.get("/api/health")
def health() -> Response:
    config_store = store()
    cfg = config_store.get("health")
    service = config_store.get("app_settings")["service_name"]
    return jsonify(compute_health(cfg, telemetry.RING.snapshot(), service))


@core_bp.get("/api/rollouts")
def rollouts() -> Response:
    cfg = store().get("rollouts")
    active_total = sum(item["traffic_percent"] for item in cfg["rollouts"] if item["status"] == "active")
    if active_total != cfg["traffic_total_percent"]:
        detail = f"active rollouts carry {active_total}% of traffic, expected {cfg['traffic_total_percent']}%"
        raise ConfigError(CONFIG_SCHEMA, "config/rollouts.json", detail)
    return jsonify({"source_label": cfg["source_label"], "scenario": cfg["scenario"],
                    "traffic_total_percent": cfg["traffic_total_percent"],
                    "active_traffic_percent": active_total, "rollouts": cfg["rollouts"]})


@core_bp.get("/api/workflow")
def workflow() -> Response:
    return jsonify(workflow_view(store()))


@core_bp.get("/api/agents")
def agents() -> Response:
    cfg = store().get("agents")
    return jsonify({"roster_id": cfg["roster_id"], "count": len(cfg["agents"]), "agents": cfg["agents"]})


def _limit(settings: dict[str, Any]) -> int:
    raw = request.args.get("limit")
    if raw is None:
        return int(settings["default_limit"])
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_LIMIT", "limit must be an integer") from exc
    if value < 1 or value > settings["max_limit"]:
        raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_LIMIT", f"limit must be between 1 and {settings['max_limit']}")
    return value


@core_bp.get("/api/telemetry")
def telemetry_events() -> Response:
    settings = store().get("app_settings")["telemetry"]
    limit = _limit(settings)
    events = telemetry.RING.recent(limit)
    return jsonify({"count": len(events), "limit": limit, "capacity": telemetry.RING.capacity, "ring_configured": telemetry.RING.configured, "events": events})
