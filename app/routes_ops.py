"""Task backend selection, Night Dreaming shadow replay, and HMAC audit receipt endpoints."""

from __future__ import annotations

import json
import os
import time
from http import HTTPStatus
from typing import Any

from flask import Blueprint, Response, current_app, jsonify

from app import telemetry
from app.config_store import CONFIG_SCHEMA, ConfigError, ConfigStore
from app.errors import ApiError, read_json_object
from app.receipts import describe_chain, load_key, verify_chain, verify_receipt
from app.workflow_service import TaskBackendService, load_task_backend_module
from dreamstore.hooks.dp001_policy import PolicyError
from dreamstore.replay import ReplayError, run_shadow_replay

ops_bp = Blueprint("ops", __name__)


def store() -> ConfigStore:
    return current_app.extensions["console.store"]


def backends() -> TaskBackendService:
    return current_app.extensions["console.backends"]


@ops_bp.get("/api/task-backends")
def task_backends() -> Response:
    registry = backends().registry()
    return jsonify({
        "backends": registry.describe_all(),
        "default_backend": registry.default_backend,
        "selected": backends().selector().selected(registry),
        "source_label": store().get("task_backends")["source_label"],
    })


@ops_bp.get("/api/task-backend")
def task_backend() -> Response:
    return jsonify({**backends().view(), "scope": "process"})


@ops_bp.post("/api/task-backend")
@telemetry.traced("task_backend.select")
def select_task_backend() -> Response:
    body = read_json_object()
    requested = body.get("backend")
    module = load_task_backend_module()
    try:
        view = backends().select(requested)
    except module.UnknownBackendError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "UNKNOWN_BACKEND", str(exc), allowed=backends().registry().ids()) from exc
    return jsonify({**view, "scope": "process", "requested": requested, "selected_at": telemetry.utc_now_iso()})


@telemetry.traced("dreams.shadow_replay")
def _replay(config_store: ConfigStore) -> tuple[dict[str, Any], dict[str, Any]]:
    dreaming = config_store.get("night_dreaming")
    if config_store.path_for("shadow_replay_evals") != config_store.root / dreaming["eval_set_path"]:
        raise ConfigError(CONFIG_SCHEMA, "config/night_dreaming.json", "eval_set_path does not match config_schema.json")
    policy_doc = config_store.get("dp001_guard_policy")
    evals = config_store.get("shadow_replay_evals")
    try:
        report = run_shadow_replay(policy_doc, evals, dreaming)
    except PolicyError as exc:
        raise ConfigError(CONFIG_SCHEMA, "config/dp001_guard_policy.json", str(exc)) from exc
    except ReplayError as exc:
        raise ConfigError(CONFIG_SCHEMA, dreaming["eval_set_path"], str(exc)) from exc
    return dreaming, report


def _nightly(config_store: ConfigStore, dreaming: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Cross-check the last committed nightly scorecard against the live computation."""
    relpath = dreaming["outputs"]["scorecard"]
    try:
        doc = json.loads((config_store.root / relpath).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"present": False, "file": relpath, "detail": "no evolver run has written a scorecard yet"}
    except (OSError, ValueError) as exc:
        return {"present": True, "valid": False, "file": relpath, "detail": f"unreadable: {type(exc).__name__}"}
    try:
        consistent = all(doc[side]["passed"] == report[side]["passed"] for side in ("baseline", "candidate"))
        labels = {side: doc[side]["pass_rate_label"] for side in ("baseline", "candidate")}
        generated_at = str(doc["generated_at"])
    except (KeyError, TypeError) as exc:
        return {"present": True, "valid": False, "file": relpath, "detail": f"missing field {exc}"}
    return {"present": True, "valid": True, "file": relpath, "generated_at": generated_at,
            "labels": labels, "consistent_with_live": consistent}


def _dreams_payload(started: float) -> dict[str, Any]:
    config_store = store()
    dreaming, report = _replay(config_store)
    return {
        "proposal": dreaming["proposal"],
        "replay": report,
        "nightly": _nightly(config_store, dreaming, report),
        "generated_at": telemetry.utc_now_iso(),
        "trace_id": telemetry.current_trace_id(),
        "latency_ms": telemetry.elapsed_ms(started),
        "latency_us": telemetry.elapsed_us(started),
    }


@ops_bp.get("/api/dreams")
def dreams() -> Response:
    return jsonify(_dreams_payload(time.perf_counter()))


@ops_bp.post("/api/dreams/replay")
def dreams_replay() -> Response:
    return jsonify(_dreams_payload(time.perf_counter()))


@ops_bp.get("/api/receipts")
@telemetry.traced("receipts.describe_chain")
def receipts() -> Response:
    started = time.perf_counter()
    cfg = store().get("audit_receipts")
    key = load_key(cfg, os.environ)
    view = describe_chain(cfg, key)
    return jsonify({**view, "short_signature_chars": cfg["short_signature_chars"], "latency_ms": telemetry.elapsed_ms(started)})


@ops_bp.post("/api/receipts/verify")
@telemetry.traced("receipts.verify")
def verify() -> Response:
    started = time.perf_counter()
    cfg = store().get("audit_receipts")
    key = load_key(cfg, os.environ)
    body = read_json_object()
    if "chain" in body:
        result = verify_chain(cfg, key, body["chain"])
        mode = "chain"
    elif "receipt" in body and "signature" in body:
        result = verify_receipt(cfg, key, body["receipt"], body["signature"])
        mode = "receipt"
    else:
        raise ApiError(HTTPStatus.BAD_REQUEST, "VERIFY_BODY_INVALID", "send {receipt, signature} or {chain}")
    return jsonify({**result, "mode": mode, "trace_id": telemetry.current_trace_id(), "latency_ms": telemetry.elapsed_ms(started)})
