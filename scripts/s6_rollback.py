#!/usr/bin/env python3
"""S6 rollback: shift Cloud Run traffic back to the previous ready revision.

Dry-run by default (prints the plan); `--execute` runs `gcloud run services update-traffic` with
`--to-revisions=<previous>=<percent>` (percent from config/rollback.json). The serving revision comes
from `gcloud run services describe --format=json` and the candidates from
`gcloud run revisions list --format=json`. Fail closed: any gcloud error (401/403/429/503, timeout,
missing binary, malformed JSON) or an ambiguous revision history exits 1 without touching traffic.

  python3 scripts/s6_rollback.py --project "$GOOGLE_CLOUD_PROJECT"            # dry-run
  WORKFLOW_STAGE=S6_DEPLOY_CLOSE python3 scripts/s6_rollback.py --execute    # shift traffic
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Callable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import telemetry  # noqa: E402
from app.config_store import REPO_ROOT, ConfigError, ConfigStore  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], float], RunResult]


class RollbackError(Exception):
    """A fail-closed stop with an error taxonomy (UNAUTHENTICATED, TIMEOUT, NO_PREVIOUS_REVISION, ...)."""

    def __init__(self, taxonomy: str, detail: str) -> None:
        super().__init__(detail)
        self.taxonomy = taxonomy
        self.detail = detail


def subprocess_runner(cmd: list[str], timeout: float) -> RunResult:
    """Default runner: real gcloud subprocess with a timeout and captured output."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise RollbackError("GCLOUD_NOT_FOUND", f"{cmd[0]} is not on PATH") from exc
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def classify(stderr: str, cfg: Mapping[str, Any]) -> str:
    for entry in cfg["error_taxonomy"]:
        if any(re.search(pattern, stderr) for pattern in entry["patterns"]):
            return str(entry["taxonomy"])
    return str(cfg["default_error_taxonomy"])


def render(template: list[str], values: Mapping[str, Any]) -> list[str]:
    try:
        return [part.format_map(values) for part in template]
    except (KeyError, ValueError, IndexError) as exc:
        raise RollbackError("CONFIG_SCHEMA", f"bad command template field: {exc}") from exc


def run_command(runner: Runner, cmd: list[str], cfg: Mapping[str, Any]) -> RunResult:
    timeout = cfg["command_timeout_seconds"]
    try:
        result = runner(cmd, timeout)
    except subprocess.TimeoutExpired as exc:
        raise RollbackError("TIMEOUT", f"{' '.join(cmd)} timed out after {timeout}s") from exc
    except OSError as exc:
        raise RollbackError("GCLOUD_ERROR", f"{type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
        raise RollbackError(classify(result.stderr, cfg), lines[-1] if lines else f"exit code {result.returncode}")
    return result


def run_json(runner: Runner, cmd: list[str], cfg: Mapping[str, Any]) -> Any:
    result = run_command(runner, cmd, cfg)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RollbackError("MALFORMED_GCLOUD_JSON", f"{' '.join(cmd)} did not return JSON") from exc


def _timestamp(revision: Mapping[str, Any]) -> datetime:
    raw = revision.get("metadata", {}).get("creationTimestamp")
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise RollbackError("MALFORMED_GCLOUD_JSON", f"bad creationTimestamp {raw!r}") from exc


def _is_ready(revision: Mapping[str, Any]) -> bool:
    conditions = revision.get("status", {}).get("conditions", [])
    return any(cond.get("type") == "Ready" and cond.get("status") == "True" for cond in conditions)


def current_revision(service_doc: Any) -> str:
    """Revision receiving the largest traffic share (resolving latestRevision targets)."""
    status = service_doc.get("status", {}) if isinstance(service_doc, dict) else {}
    best_name, best_percent = None, -1
    for entry in status.get("traffic") or []:
        name = entry.get("revisionName") or (status.get("latestReadyRevisionName") if entry.get("latestRevision") else None)
        percent = entry.get("percent", 0)
        if name and isinstance(percent, int) and percent > best_percent:
            best_name, best_percent = name, percent
    if not best_name:
        raise RollbackError("NO_SERVING_REVISION", "service has no traffic targets")
    return str(best_name)


def previous_revision(revisions: Any, current: str) -> str:
    """Newest Ready revision created before the serving one."""
    if not isinstance(revisions, list):
        raise RollbackError("MALFORMED_GCLOUD_JSON", "revisions list is not a JSON array")
    by_name = {rev.get("metadata", {}).get("name"): rev for rev in revisions if isinstance(rev, dict)}
    if current not in by_name:
        raise RollbackError("UNKNOWN_SERVING_REVISION", f"serving revision {current} is not in the revisions list")
    cutoff = _timestamp(by_name[current])
    older = [rev for name, rev in by_name.items() if name != current and _is_ready(rev) and _timestamp(rev) < cutoff]
    if not older:
        raise RollbackError("NO_PREVIOUS_REVISION", f"no Ready revision older than {current}")
    return str(max(older, key=_timestamp)["metadata"]["name"])


def plan_and_execute(args: argparse.Namespace, cfg: Mapping[str, Any], runner: Runner, project: str) -> dict[str, Any]:
    values = {"gcloud": cfg["gcloud_bin"], "service": args.service or cfg["service"],
              "region": args.region or cfg["region"], "project": project}
    commands = cfg["commands"]
    current = current_revision(run_json(runner, render(commands["describe_service"], values), cfg))
    previous = previous_revision(run_json(runner, render(commands["list_revisions"], values), cfg), current)
    update = render(commands["update_traffic"], {**values, "revision": previous, "percent": cfg["rollback_traffic_percent"]})
    if args.execute:
        run_command(runner, update, cfg)
    return {"service": values["service"], "region": values["region"], "project": project,
            "current_revision": current, "previous_revision": previous, "command": update, "executed": bool(args.execute)}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", help="Cloud Run service (default: config/rollback.json)")
    parser.add_argument("--region", help="Cloud Run region (default: config/rollback.json)")
    parser.add_argument("--project", help="GCP project (default: $GOOGLE_CLOUD_PROJECT)")
    parser.add_argument("--execute", action="store_true", help="shift traffic (default is a dry-run)")
    parser.add_argument("--config-root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _run(args: argparse.Namespace, runner: Runner, env: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    mode = "execute" if args.execute else "dry-run"
    try:
        cfg = ConfigStore(args.config_root).get("rollback")
        project = args.project or env.get(cfg["project_env_var"])
        if not project:
            raise RollbackError("MISSING_PROJECT", f"pass --project or set {cfg['project_env_var']}")
        return EXIT_OK, {"status": "ok", "mode": mode, **plan_and_execute(args, cfg, runner, project)}
    except (RollbackError, ConfigError) as exc:
        return EXIT_FAIL, {"status": "error", "mode": mode, "executed": False, "error_taxonomy": exc.taxonomy,
                           "detail": getattr(exc, "detail", str(exc))}


def main(argv: list[str] | None = None, runner: Runner | None = None, environ: Mapping[str, str] | None = None,
         stdout: IO[str] | None = None) -> int:
    started = time.perf_counter()
    trace_id = telemetry.new_trace_id()
    args = parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    previous_stream = telemetry.use_stream(sys.stderr)
    try:
        code, summary = _run(args, runner or subprocess_runner, os.environ if environ is None else environ)
    finally:
        telemetry.use_stream(previous_stream)
    summary.update({"trace_id": trace_id, "latency_ms": telemetry.elapsed_ms(started)})
    out.write(json.dumps(summary, sort_keys=True) + "\n")
    out.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
