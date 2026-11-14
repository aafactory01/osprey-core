"""Pluggable TaskBackend registry for the hybrid S1..S6 workflow.

Backends (farmtable, github, jira, gitlab, beads, linear) are declared in config/task_backends.json:
display name, issue-key format, stage -> native status/label mapping, and capabilities. The registry
validates that every backend maps every workflow stage and that issue-key formats only use the
allowed fields, then turns the six stages into backend-native task keys and statuses. Selection is
held per process by `TaskBackendSelector` (thread-safe).
"""

from __future__ import annotations

import hashlib
import string
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

ALLOWED_FORMAT_FIELDS = frozenset({"project_key", "seq", "hash"})


class TaskBackendError(Exception):
    """The backend registry configuration is inconsistent (fails closed)."""

    taxonomy = "TASK_BACKEND_INVALID"


class UnknownBackendError(TaskBackendError):
    """A caller asked for a backend id that is not registered."""

    taxonomy = "UNKNOWN_BACKEND"


def _check_format(backend_id: str, fmt: str) -> None:
    try:
        fields = [field for _, field, spec, conv in string.Formatter().parse(fmt) if field is not None or spec or conv]
    except ValueError as exc:
        raise TaskBackendError(f"{backend_id}: invalid issue_key_format {fmt!r}: {exc}") from exc
    for field in fields:
        if field not in ALLOWED_FORMAT_FIELDS:
            raise TaskBackendError(f"{backend_id}: issue_key_format field {field!r} is not allowed")


@dataclass(frozen=True)
class TaskBackend:
    """One task tracker and how the six workflow stages land in it."""

    backend_id: str
    display_name: str
    description: str
    issue_key_format: str
    project_key: str
    seq_start: int
    hash_chars: int
    capabilities: tuple[str, ...]
    stage_map: Mapping[str, Mapping[str, str]]

    def task_key(self, stage_id: str, index: int) -> str:
        """Deterministic backend-native key: sequence numbers or a content hash, per the format."""
        seed = f"{self.backend_id}:{self.project_key}:{stage_id}".encode("utf-8")
        digest = hashlib.sha256(seed).hexdigest()[: self.hash_chars]
        return self.issue_key_format.format(project_key=self.project_key, seq=self.seq_start + index, hash=digest)

    def map_stage(self, stage_id: str, index: int) -> dict[str, str]:
        native = self.stage_map.get(stage_id)
        if native is None:
            raise TaskBackendError(f"{self.backend_id}: no mapping for stage {stage_id}")
        return {"stage": stage_id, "task_key": self.task_key(stage_id, index), "status": native["status"], "label": native["label"]}

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.backend_id,
            "display_name": self.display_name,
            "description": self.description,
            "issue_key_format": self.issue_key_format,
            "project_key": self.project_key,
            "capabilities": list(self.capabilities),
        }


class TaskBackendRegistry:
    """Validated registry built from config/task_backends.json and the workflow stage ids."""

    def __init__(self, config: Mapping[str, Any], stage_ids: Sequence[str]) -> None:
        if not stage_ids:
            raise TaskBackendError("workflow has no stages to map")
        self._stage_ids = list(stage_ids)
        self._backends: dict[str, TaskBackend] = {}
        for backend_id, spec in config["backends"].items():
            _check_format(backend_id, spec["issue_key_format"])
            missing = [stage for stage in self._stage_ids if stage not in spec["stage_map"]]
            if missing:
                raise TaskBackendError(f"{backend_id}: stage_map is missing {', '.join(missing)}")
            self._backends[backend_id] = TaskBackend(
                backend_id=backend_id,
                display_name=spec["display_name"],
                description=spec["description"],
                issue_key_format=spec["issue_key_format"],
                project_key=spec["project_key"],
                seq_start=spec["seq_start"],
                hash_chars=spec["hash_chars"],
                capabilities=tuple(spec["capabilities"]),
                stage_map=dict(spec["stage_map"]),
            )
        self.default_backend = config["default_backend"]
        if self.default_backend not in self._backends:
            raise TaskBackendError(f"default_backend {self.default_backend!r} is not registered")

    def ids(self) -> list[str]:
        return list(self._backends)

    def get(self, backend_id: Any) -> TaskBackend:
        if not isinstance(backend_id, str) or backend_id not in self._backends:
            raise UnknownBackendError(f"unknown task backend {backend_id!r}; choose one of {', '.join(self._backends)}")
        return self._backends[backend_id]

    def describe_all(self) -> list[dict[str, Any]]:
        return [backend.describe() for backend in self._backends.values()]

    def map_workflow(self, backend_id: str) -> list[dict[str, str]]:
        """How S1..S6 land in the chosen backend: native task key, status, and label per stage."""
        backend = self.get(backend_id)
        return [backend.map_stage(stage_id, index) for index, stage_id in enumerate(self._stage_ids)]


class TaskBackendSelector:
    """Per-process, thread-safe selection of the active backend (echoed back by the API)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._selected: str | None = None

    def selected(self, registry: TaskBackendRegistry) -> str:
        with self._lock:
            chosen = self._selected
        if chosen is not None and chosen in registry.ids():
            return chosen
        return registry.default_backend

    def select(self, registry: TaskBackendRegistry, backend_id: Any) -> str:
        backend = registry.get(backend_id)
        with self._lock:
            self._selected = backend.backend_id
        return backend.backend_id
