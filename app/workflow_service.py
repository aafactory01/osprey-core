"""Bridges the API to .workflow/task_backend.py and computes stage statuses from config."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

from app.config_store import REPO_ROOT, ConfigError, ConfigStore

_MODULE_NAME = "osprey_core_workflow_task_backend"
_MODULE_PATH = REPO_ROOT / ".workflow" / "task_backend.py"
_LOAD_LOCK = threading.Lock()

STAGE_COMPLETED = "completed"
STAGE_ACTIVE = "active"
STAGE_PENDING = "pending"


def load_task_backend_module(path: Path = _MODULE_PATH) -> ModuleType:
    """Import .workflow/task_backend.py (a dot-directory, so not importable as a package)."""
    with _LOAD_LOCK:
        cached = sys.modules.get(_MODULE_NAME)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
        if spec is None or spec.loader is None or not path.is_file():
            raise ConfigError("CONFIG_MISSING", ".workflow/task_backend.py", "task backend module not found")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_MODULE_NAME, None)
            raise
        return module


def workflow_view(store: ConfigStore) -> dict[str, Any]:
    """Workflow stages with status derived from `current_stage` (completed / active / pending)."""
    cfg = store.get("workflow_stages")
    ids = [stage["id"] for stage in cfg["stages"]]
    if len(set(ids)) != len(ids):
        raise ConfigError("CONFIG_SCHEMA", "config/workflow_stages.json", "duplicate stage ids")
    if cfg["current_stage"] not in ids:
        raise ConfigError("CONFIG_SCHEMA", "config/workflow_stages.json", f"current_stage {cfg['current_stage']!r} is not a stage id")
    current = ids.index(cfg["current_stage"])
    stages = []
    for index, stage in enumerate(cfg["stages"]):
        status = STAGE_COMPLETED if index < current else STAGE_ACTIVE if index == current else STAGE_PENDING
        stages.append({**stage, "position": index + 1, "status": status,
                       "mode_description": cfg["mode_descriptions"][stage["mode"]]})
    return {
        "workflow_id": cfg["workflow_id"],
        "version": cfg["version"],
        "mode_label": cfg["mode_label"],
        "scenario": cfg["scenario"],
        "source_label": cfg["source_label"],
        "current_stage": cfg["current_stage"],
        "stages": stages,
    }


class TaskBackendService:
    """Registry construction (from config) plus the per-process selection."""

    def __init__(self, store: ConfigStore) -> None:
        self._store = store
        self._selector: Any = None
        self._selector_lock = threading.Lock()

    def _module(self) -> ModuleType:
        return load_task_backend_module()

    def registry(self) -> Any:
        module = self._module()
        stage_ids = [stage["id"] for stage in self._store.get("workflow_stages")["stages"]]
        try:
            return module.TaskBackendRegistry(self._store.get("task_backends"), stage_ids)
        except module.TaskBackendError as exc:
            raise ConfigError("CONFIG_SCHEMA", "config/task_backends.json", str(exc)) from exc

    def selector(self) -> Any:
        with self._selector_lock:
            if self._selector is None:
                self._selector = self._module().TaskBackendSelector()
            return self._selector

    def view(self, backend_id: str | None = None) -> dict[str, Any]:
        registry = self.registry()
        chosen = backend_id or self.selector().selected(registry)
        backend = registry.get(chosen)
        return {
            "selected": chosen,
            "default_backend": registry.default_backend,
            "source_label": self._store.get("task_backends")["source_label"],
            "backend": backend.describe(),
            "mapping": registry.map_workflow(chosen),
        }

    def select(self, backend_id: Any) -> dict[str, Any]:
        registry = self.registry()
        chosen = self.selector().select(registry, backend_id)
        return self.view(chosen)
