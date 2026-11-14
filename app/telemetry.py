"""Structured telemetry: uuid4 trace ids, perf_counter latencies, JSON lines on stdout, bounded ring.

Every HTTP request and core function emits one JSON object per line on stdout (Cloud Run / Cloud
Logging parse these as structured logs). The same records are kept in a bounded in-memory ring
exposed at GET /api/telemetry. The ring stays disabled until its size is read from
config/app_settings.json: no invented default size (fail closed, logs still flow to stdout).
"""

from __future__ import annotations

import collections
import contextvars
import functools
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import IO, Any, Callable, Iterable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
_ONE_MS = timedelta(milliseconds=1)
_ONE_US = timedelta(microseconds=1)
_WRITE_LOCK = threading.Lock()
_STREAM: list[IO[str]] = []

STATUS_OK = "ok"
STATUS_ERROR = "error"
NO_ERROR = "NONE"
INTERNAL_ERROR = "INTERNAL_ERROR"


class TelemetryUnavailable(RuntimeError):
    """Raised when the ring is read before its configured size is known."""

    taxonomy = "TELEMETRY_UNAVAILABLE"


def new_trace_id() -> str:
    """Return a fresh uuid4 trace id (hex, no dashes)."""
    return uuid.uuid4().hex


def current_trace_id() -> str:
    """Trace id bound to the current request/context, or a new one for background work."""
    bound = _TRACE_ID.get()
    return bound if bound else new_trace_id()


def bind_trace_id(trace_id: str) -> contextvars.Token[str | None]:
    """Bind a trace id to the current context; pair with reset_trace_id()."""
    return _TRACE_ID.set(trace_id)


def reset_trace_id(token: contextvars.Token[str | None]) -> None:
    """Restore the trace id that was bound before bind_trace_id()."""
    _TRACE_ID.reset(token)


def elapsed_ms(started: float) -> int:
    """Real wall-clock latency since `started` (a time.perf_counter() value), rounded to int ms."""
    return round(timedelta(seconds=time.perf_counter() - started) / _ONE_MS)


def elapsed_us(started: float) -> int:
    """Real wall-clock latency since `started`, rounded to int microseconds."""
    return round(timedelta(seconds=time.perf_counter() - started) / _ONE_US)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with millisecond precision and a Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TelemetryRing:
    """Thread-safe bounded ring of recent telemetry records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: collections.deque[dict[str, Any]] | None = None

    @property
    def configured(self) -> bool:
        with self._lock:
            return self._events is not None

    @property
    def capacity(self) -> int | None:
        with self._lock:
            return None if self._events is None else self._events.maxlen

    def configure(self, size: int) -> None:
        """Enable (or resize) the ring, keeping the newest records."""
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError("telemetry ring size must be a positive integer")
        with self._lock:
            if self._events is None or self._events.maxlen != size:
                previous: Iterable[dict[str, Any]] = self._events or ()
                self._events = collections.deque(previous, maxlen=size)

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            if self._events is not None:
                self._events.append(record)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._events is None:
                raise TelemetryUnavailable("telemetry ring not configured (config/app_settings.json unreadable)")
            return list(self._events)

    def recent(self, limit: int) -> list[dict[str, Any]]:
        events = self.snapshot()
        if limit < 1:
            return []
        return events[-limit:]

    def clear(self) -> None:
        with self._lock:
            if self._events is not None:
                self._events.clear()


RING = TelemetryRing()


def use_stream(stream: IO[str] | None) -> IO[str] | None:
    """Redirect telemetry lines (CLI scripts use stderr so stdout stays pure JSON); returns the previous target."""
    with _WRITE_LOCK:
        previous = _STREAM[0] if _STREAM else None
        _STREAM.clear()
        if stream is not None:
            _STREAM.append(stream)
    return previous


def emit(event: dict[str, Any], stream: IO[str] | None = None) -> dict[str, Any]:
    """Write one structured JSON line (stdout by default) and append it to the ring."""
    is_error = event.get("status") == STATUS_ERROR
    record = {"ts": utc_now_iso(), "severity": "ERROR" if is_error else "INFO", **event}
    line = json.dumps(record, sort_keys=True, default=str, ensure_ascii=False)
    target = stream if stream is not None else (_STREAM[0] if _STREAM else sys.stdout)
    with _WRITE_LOCK:
        target.write(line + "\n")
        target.flush()
    RING.append(record)
    return record


def record_core(op: str, started: float, status: str, taxonomy: str = NO_ERROR, **fields: Any) -> dict[str, Any]:
    """Emit a `core` telemetry record for a named operation started at `started`."""
    return emit(
        {
            "kind": "core",
            "op": op,
            "trace_id": current_trace_id(),
            "latency_ms": elapsed_ms(started),
            "status": status,
            "error_taxonomy": taxonomy,
            **fields,
        }
    )


def traced(op: str) -> Callable[[F], F]:
    """Decorator: measure a core function with perf_counter and emit ok/error telemetry."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                record_core(op, started, STATUS_ERROR, getattr(exc, "taxonomy", INTERNAL_ERROR), error_type=type(exc).__name__)
                raise
            record_core(op, started, STATUS_OK)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
