"""Structured API errors: one JSON shape for every failure, never a fake default payload."""

from __future__ import annotations

import json
import math
from http import HTTPStatus
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from app import telemetry
from app.config_store import ConfigError, reject_json_constant

AUDIT_KEY_MISSING = "AUDIT_KEY_MISSING"


class ApiError(Exception):
    """An expected request failure with an HTTP status and an error taxonomy."""

    def __init__(self, status: HTTPStatus, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.taxonomy = code
        self.message = message
        self.details = details


class AuditKeyError(Exception):
    """AUDIT_HMAC_KEY is absent or too short: receipts fail closed instead of inventing signatures."""

    taxonomy = AUDIT_KEY_MISSING


def error_body(code: str, message: str, taxonomy: str, **details: Any) -> dict[str, Any]:
    """The single error envelope used by every endpoint."""
    return {"error": {"code": code, "taxonomy": taxonomy, "message": message, "trace_id": telemetry.current_trace_id(), **details}}


def error_response(status: HTTPStatus, code: str, message: str, taxonomy: str, **details: Any) -> tuple[Response, int]:
    response = jsonify(error_body(code, message, taxonomy, **details))
    return response, int(status)


def _ensure_finite(value: Any) -> None:
    """Reject floats that overflowed to inf (e.g. 1e400) anywhere in a decoded JSON document."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ApiError(HTTPStatus.BAD_REQUEST, "NON_FINITE_NUMBER", "JSON numbers must be finite")
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def read_json_object() -> dict[str, Any]:
    """Decode the request body as a JSON object (UTF-8, finite numbers only) or raise ApiError."""
    raw = request.get_data(cache=False)
    if not raw or not raw.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "EMPTY_BODY", "request body must be a JSON object")
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=reject_json_constant)
    except RecursionError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "JSON_TOO_DEEP", "JSON nesting is too deep") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "MALFORMED_JSON", "request body is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "BODY_NOT_OBJECT", "request body must be a JSON object")
    _ensure_finite(data)
    return data


def register_error_handlers(app: Flask) -> None:
    """Map every failure class to the structured envelope (and to a telemetry taxonomy)."""

    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError) -> tuple[Response, int]:
        request.environ["console.error_taxonomy"] = exc.taxonomy
        return error_response(exc.status, exc.code, exc.message, exc.taxonomy, **exc.details)

    @app.errorhandler(ConfigError)
    def _config_error(exc: ConfigError) -> tuple[Response, int]:
        request.environ["console.error_taxonomy"] = exc.taxonomy
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE, "CONFIG_INVALID", "configuration is missing or invalid; failing closed",
            exc.taxonomy, file=exc.file, detail=exc.detail,
        )

    @app.errorhandler(AuditKeyError)
    def _audit_key_error(exc: AuditKeyError) -> tuple[Response, int]:
        request.environ["console.error_taxonomy"] = AUDIT_KEY_MISSING
        return error_response(HTTPStatus.SERVICE_UNAVAILABLE, "AUDIT_KEY_NOT_CONFIGURED", str(exc), AUDIT_KEY_MISSING)

    @app.errorhandler(telemetry.TelemetryUnavailable)
    def _telemetry_unavailable(exc: telemetry.TelemetryUnavailable) -> tuple[Response, int]:
        request.environ["console.error_taxonomy"] = exc.taxonomy
        return error_response(HTTPStatus.SERVICE_UNAVAILABLE, exc.taxonomy, str(exc), exc.taxonomy)

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException) -> tuple[Response, int]:
        status = HTTPStatus(exc.code or HTTPStatus.INTERNAL_SERVER_ERROR)
        code = status.name
        request.environ["console.error_taxonomy"] = code
        return error_response(status, code, exc.description or status.phrase, code)

    @app.errorhandler(Exception)
    def _unexpected(exc: Exception) -> tuple[Response, int]:
        request.environ["console.error_taxonomy"] = telemetry.INTERNAL_ERROR
        telemetry.emit({
            "kind": "core", "op": "unhandled_exception", "trace_id": telemetry.current_trace_id(),
            "status": telemetry.STATUS_ERROR, "error_taxonomy": telemetry.INTERNAL_ERROR,
            "error_type": type(exc).__name__, "detail": str(exc),
        })
        return error_response(
            HTTPStatus.INTERNAL_SERVER_ERROR, telemetry.INTERNAL_ERROR, "unexpected server error", telemetry.INTERNAL_ERROR,
        )
