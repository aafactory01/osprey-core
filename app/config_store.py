"""Fail-closed JSON configuration store.

Each logical config name is declared in config/config_schema.json with its path and a small type
spec. `ConfigStore.get(name)` stats the file on every call, re-reads and re-validates it when the
(mtime, size) signature changes, and otherwise serves a deep copy of the cached document. A file
that disappears, becomes unreadable, or stops validating raises `ConfigError` immediately: callers
never receive stale or invented defaults.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app import telemetry

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_RELPATH = "config/config_schema.json"

CONFIG_MISSING = "CONFIG_MISSING"
CONFIG_UNREADABLE = "CONFIG_UNREADABLE"
CONFIG_MALFORMED = "CONFIG_MALFORMED"
CONFIG_SCHEMA = "CONFIG_SCHEMA"
CONFIG_UNKNOWN = "CONFIG_UNKNOWN"


class ConfigError(Exception):
    """Typed configuration failure; `taxonomy` is one of the CONFIG_* constants."""

    def __init__(self, taxonomy: str, file: str, detail: str) -> None:
        super().__init__(f"{taxonomy}: {file}: {detail}")
        self.taxonomy = taxonomy
        self.file = file
        self.detail = detail

    def to_dict(self) -> dict[str, str]:
        return {"taxonomy": self.taxonomy, "file": self.file, "detail": self.detail}


def reject_json_constant(token: str) -> Any:
    """json.loads hook: NaN / Infinity are not valid JSON and are rejected."""
    raise ValueError(f"non-finite JSON constant {token!r} is not allowed")


def _type_ok(value: Any, type_name: str) -> bool:
    if type_name == "any":
        return True
    if type_name == "str":
        return isinstance(value, str)
    if type_name == "bool":
        return isinstance(value, bool)
    if type_name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "list":
        return isinstance(value, list)
    if type_name == "dict":
        return isinstance(value, dict)
    raise ConfigError(CONFIG_SCHEMA, SCHEMA_RELPATH, f"unknown type name {type_name!r}")


def _check_bounds(value: Any, spec: dict[str, Any], path: str, errors: list[str]) -> None:
    if "enum" in spec and value not in spec["enum"]:
        errors.append(f"{path}: {value!r} is not one of {spec['enum']}")
    if isinstance(value, str) and len(value) < spec.get("min_length", 0):
        errors.append(f"{path}: string shorter than {spec['min_length']}")
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if is_number and "min" in spec and value < spec["min"]:
        errors.append(f"{path}: {value} is below minimum {spec['min']}")
    if is_number and "max" in spec and value > spec["max"]:
        errors.append(f"{path}: {value} is above maximum {spec['max']}")
    if isinstance(value, (list, dict)) and len(value) < spec.get("min_items", 0):
        errors.append(f"{path}: needs at least {spec['min_items']} item(s)")


def validate(value: Any, spec: Any, path: str, errors: list[str]) -> None:
    """Validate `value` against a schema spec, appending human-readable errors."""
    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
        errors.append(f"{path}: invalid schema spec")
        return
    if not _type_ok(value, spec["type"]):
        errors.append(f"{path}: expected {spec['type']}, got {type(value).__name__}")
        return
    _check_bounds(value, spec, path, errors)
    if isinstance(value, list) and "items" in spec:
        for index, item in enumerate(value):
            validate(item, spec["items"], f"{path}[{index}]", errors)
    if isinstance(value, dict):
        for key, sub_spec in spec.get("required", {}).items():
            if key not in value:
                errors.append(f"{path}.{key}: required key missing")
            else:
                validate(value[key], sub_spec, f"{path}.{key}", errors)
        for key, sub_spec in spec.get("optional", {}).items():
            if key in value:
                validate(value[key], sub_spec, f"{path}.{key}", errors)
        if "values" in spec:
            for key, item in value.items():
                validate(item, spec["values"], f"{path}.{key}", errors)


def _check_schema_document(doc: Any) -> list[str]:
    """Procedural meta-check of config/config_schema.json (it cannot validate itself)."""
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, dict) or not files:
        return ["$.files: must be a non-empty object"]
    errors: list[str] = []
    for name, entry in files.items():
        if not isinstance(entry, dict):
            errors.append(f"$.files.{name}: must be an object")
        elif not isinstance(entry.get("path"), str) or not entry["path"]:
            errors.append(f"$.files.{name}.path: must be a non-empty string")
        elif not isinstance(entry.get("spec"), (dict, str)):
            errors.append(f"$.files.{name}.spec: must be an object or type name")
    return errors


@dataclass(frozen=True)
class _CacheEntry:
    signature: tuple[int, int]
    data: Any


class ConfigStore:
    """Loads, validates, and caches the JSON files declared in config/config_schema.json."""

    def __init__(self, root: Path | str = REPO_ROOT, schema_relpath: str = SCHEMA_RELPATH) -> None:
        self._root = Path(root)
        self._schema_relpath = schema_relpath
        self._lock = threading.Lock()
        self._cache: dict[str, _CacheEntry] = {}

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, name: str) -> Path:
        """Absolute path of a declared config file."""
        return self._root / self._entry(name)["path"]

    def get(self, name: str) -> Any:
        """Return a validated deep copy of config `name` or raise ConfigError (fail closed)."""
        entry = self._entry(name)
        spec = entry["spec"]

        def check(data: Any) -> list[str]:
            errors: list[str] = []
            validate(data, spec, "$", errors)
            return errors

        return copy.deepcopy(self._load(entry["path"], check))

    def names(self) -> list[str]:
        return sorted(self._schema()["files"])

    def check_all(self) -> dict[str, dict[str, Any]]:
        """Validate every declared file; used by /readyz."""
        try:
            names = self.names()
        except ConfigError as exc:
            return {"config_schema": {"ok": False, **exc.to_dict()}}
        report: dict[str, dict[str, Any]] = {}
        for name in names:
            try:
                self.get(name)
            except ConfigError as exc:
                report[name] = {"ok": False, **exc.to_dict()}
            else:
                report[name] = {"ok": True}
        return report

    def _entry(self, name: str) -> dict[str, Any]:
        files = self._schema()["files"]
        entry = files.get(name)
        if entry is None:
            raise ConfigError(CONFIG_UNKNOWN, name, "not declared in config/config_schema.json")
        return entry

    def _schema(self) -> dict[str, Any]:
        return self._load(self._schema_relpath, _check_schema_document)

    def _load(self, relpath: str, check: Callable[[Any], list[str]]) -> Any:
        started = time.perf_counter()
        path = self._root / relpath
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            self._fail(started, relpath, ConfigError(CONFIG_MISSING, relpath, "file not found"), exc)
        except OSError as exc:
            self._fail(started, relpath, ConfigError(CONFIG_UNREADABLE, relpath, exc.strerror or type(exc).__name__), exc)
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._cache.get(relpath)
        if cached is not None and cached.signature == signature:
            return cached.data
        data = self._read_and_validate(started, relpath, path, check)
        with self._lock:
            self._cache[relpath] = _CacheEntry(signature=signature, data=data)
        telemetry.record_core("config.load", started, telemetry.STATUS_OK, file=relpath)
        return data

    def _read_and_validate(self, started: float, relpath: str, path: Path, check: Callable[[Any], list[str]]) -> Any:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            self._fail(started, relpath, ConfigError(CONFIG_MISSING, relpath, "file disappeared during read"), exc)
        except (OSError, UnicodeDecodeError) as exc:
            detail = getattr(exc, "strerror", None) or type(exc).__name__
            self._fail(started, relpath, ConfigError(CONFIG_UNREADABLE, relpath, detail), exc)
        try:
            data = json.loads(text, parse_constant=reject_json_constant)
        except (ValueError, RecursionError) as exc:
            self._fail(started, relpath, ConfigError(CONFIG_MALFORMED, relpath, str(exc)), exc)
        errors = check(data)
        if errors:
            extra = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            self._fail(started, relpath, ConfigError(CONFIG_SCHEMA, relpath, errors[0] + extra), None)
        return data

    def _fail(self, started: float, relpath: str, error: ConfigError, cause: BaseException | None) -> None:
        with self._lock:
            self._cache.pop(relpath, None)
        telemetry.record_core("config.load", started, telemetry.STATUS_ERROR, error.taxonomy, file=relpath, detail=error.detail)
        raise error from cause
