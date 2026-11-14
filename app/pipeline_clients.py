"""Outbound HTTP for the pipeline status: urllib transport, metadata-server token, and JSON GETs.

Every call emits one telemetry record (trace id, perf_counter latency, status, error taxonomy, HTTP status,
host). Failures raise PipelineError: HTTP statuses map through config/pipeline_status.json
`taxonomy.by_http_status`; the transport reports TIMEOUT, UNAVAILABLE, or MALFORMED_RESPONSE (oversized
body); undecodable JSON is MALFORMED_RESPONSE; the metadata token call maps every failure to
NO_CREDENTIALS. Stdlib only, HTTP proxies bypassed, every call bounded by a shared deadline.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from typing import Any, Callable, Mapping

from app import telemetry
from app.config_store import reject_json_constant
from app.pipeline_model import MALFORMED_RESPONSE, NO_CREDENTIALS, TIMEOUT, UNAVAILABLE, PipelineError, clean_str, fmt, seconds

TOKEN_OP = "pipeline.metadata_token"
Transport = Callable[[str, Mapping[str, str], float, int], tuple[int, bytes]]
Clock = Callable[[], float]


def _read_capped(stream: Any, max_bytes: int) -> bytes:
    body = stream.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise PipelineError(MALFORMED_RESPONSE, f"response is larger than {max_bytes} bytes")
    return body


def urllib_transport(url: str, headers: Mapping[str, str], timeout_s: float, max_bytes: int) -> tuple[int, bytes]:
    """Default transport: one GET, no proxies, bounded body. HTTP error statuses are returned, not raised."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout_s) as response:
            return int(response.status), _read_capped(response, max_bytes)
    except urllib.error.HTTPError as exc:
        if exc.fp is None:
            return int(exc.code), b""
        with exc:
            return int(exc.code), _read_capped(exc, max_bytes)
    except urllib.error.URLError as exc:
        taxonomy = TIMEOUT if isinstance(exc.reason, TimeoutError) else UNAVAILABLE
        raise PipelineError(taxonomy, f"connection failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise PipelineError(TIMEOUT, "timed out while reading the response") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise PipelineError(UNAVAILABLE, f"{type(exc).__name__}: {exc}") from exc


def _decode(body: bytes, status: int) -> Any:
    try:
        return json.loads(body.decode("utf-8"), parse_constant=reject_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PipelineError(MALFORMED_RESPONSE, f"response is not valid JSON ({type(exc).__name__})", status) from exc


def _google_message(body: bytes, status: int, limit: int) -> str:
    """The `error.message` of a Google API error body (truncated), else `HTTP <status>`."""
    try:
        payload = json.loads(body.decode("utf-8"), parse_constant=reject_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    message = clean_str(error.get("message")) if isinstance(error, dict) else None
    return (message or f"HTTP {status}")[:limit]


def _emit(op: str, url: str, started: float, trace_id: str, http_status: int | None, error: PipelineError | None) -> None:
    telemetry.emit({
        "kind": "outbound",
        "op": op,
        "trace_id": trace_id,
        "latency_ms": telemetry.elapsed_ms(started),
        "status": telemetry.STATUS_ERROR if error else telemetry.STATUS_OK,
        "error_taxonomy": error.taxonomy if error else telemetry.NO_ERROR,
        "http_status": http_status,
        "host": urllib.parse.urlsplit(url).hostname,
        "detail": error.detail if error else None,
    })


class ApiClient:
    """JSON GETs through an injectable transport within a deadline, plus a cached metadata-server token."""

    def __init__(self, transport: Transport, clock: Clock) -> None:
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None

    def get_json(self, op: str, url: str, headers: Mapping[str, str], cfg: Mapping[str, Any], deadline: float,
                 trace_id: str, failure_taxonomy: str | None = None) -> Any:
        """One GET; returns decoded JSON or raises PipelineError. Always emits one telemetry record."""
        settings, started, status = cfg["http"], time.perf_counter(), None
        try:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise PipelineError(TIMEOUT, "request budget exhausted before the call")
            timeout_s = min(seconds(settings["call_timeout_ms"]), remaining)
            status, body = self._transport(url, headers, timeout_s, settings["max_response_bytes"])
            if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
                taxonomy = cfg["taxonomy"]["by_http_status"].get(str(status), cfg["taxonomy"]["default_http"])
                raise PipelineError(taxonomy, _google_message(body, status, settings["max_detail_chars"]), status)
            payload = _decode(body, status)
        except PipelineError as exc:
            error = exc
        except Exception as exc:  # a broken transport or response object is an outage, never a 500
            error = PipelineError(UNAVAILABLE, f"transport failure: {type(exc).__name__}")
        else:
            _emit(op, url, started, trace_id, status, None)
            return payload
        if failure_taxonomy:
            error = PipelineError(failure_taxonomy, f"{error.taxonomy}: {error.detail}", error.http_status)
        _emit(op, url, started, trace_id, status, error)
        raise error

    def access_token(self, cfg: Mapping[str, Any], env: Mapping[str, str], deadline: float, trace_id: str) -> str:
        """Default service account token from the metadata server, reused until expiry minus the refresh margin."""
        now = self._clock()
        with self._lock:
            cached = self._token
        if cached is not None and cached[1] > now:
            return cached[0]
        meta = cfg["metadata"]
        token_url = fmt(meta["token_url_template"], host=clean_str(env.get(meta["host_env_var"])) or meta["default_host"])
        payload = self.get_json(TOKEN_OP, token_url, {meta["header_name"]: meta["header_value"]}, cfg, deadline, trace_id,
                                failure_taxonomy=NO_CREDENTIALS)
        token = clean_str(payload.get("access_token")) if isinstance(payload, dict) else None
        if token is None:
            raise PipelineError(NO_CREDENTIALS, "metadata token response has no access_token")
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, int) and not isinstance(expires_in, bool):
            expires_at = now + expires_in - seconds(meta["refresh_margin_ms"])
            if expires_at > now:
                with self._lock:
                    self._token = (token, expires_at)
        return token

    def auth_headers(self, cfg: Mapping[str, Any], token: str) -> dict[str, str]:
        settings = cfg["http"]
        return {"Authorization": f"{settings['authorization_scheme']} {token}", "Accept": settings["accept"]}
