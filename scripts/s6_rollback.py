#!/usr/bin/env python3
"""S6 rollback: return osprey-core to its previous good Cloud Deploy release or Cloud Run revision.

Modes (--mode; default config/cicd.json cd_rollback.default_mode = auto):
  auto     Cloud Deploy mode when the target has a previous SUCCEEDED release, else traffic mode.
  deploy   `gcloud deploy targets rollback osprey-core-prod --delivery-pipeline=osprey-core-cd --region --project
           --release=<previous SUCCEEDED release> --rollout-id=<id> --quiet`, then poll that rollout to SUCCEEDED.
           The current release is the one whose SUCCEEDED rollout to the target is newest; previous is the next.
  traffic  `gcloud run services update-traffic --to-revisions=<previous ready revision>=<percent>` (percent from
           config/rollback.json); the serving revision comes from `run services describe`, candidates from
           `run revisions list`.
Guards (workflow_ctl passes both): --bad-release skips the Cloud Deploy rollback when the target is not on that
release; --bad-image skips the traffic shift when the serving revision does not run that image.

Dry-run by default (prints the plan); `--execute` acts. Fail closed: any gcloud error (401/403/429/503, timeout,
missing binary, malformed JSON) or an ambiguous history exits 1 without touching traffic.

  python3 scripts/s6_rollback.py --project "$GOOGLE_CLOUD_PROJECT"            # dry-run (auto)
  WORKFLOW_STAGE=S6_DEPLOY_CLOSE python3 scripts/s6_rollback.py --execute    # roll back
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import telemetry  # noqa: E402
from app.config_store import REPO_ROOT, ConfigError, ConfigStore  # noqa: E402
from scripts.cicd_common import RunResult, emit_call  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
TRAFFIC, DEPLOY = "traffic", "deploy"
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


def run_command(runner: Runner, cmd: list[str], cfg: Mapping[str, Any], op: str = "rollback.gcloud") -> RunResult:
    """Run one gcloud command with telemetry (trace_id, perf_counter latency, status, taxonomy)."""
    timeout = cfg["command_timeout_seconds"]
    started = time.perf_counter()
    try:
        result = runner(cmd, timeout)
    except subprocess.TimeoutExpired as exc:
        emit_call(op, started, telemetry.STATUS_ERROR, "TIMEOUT")
        raise RollbackError("TIMEOUT", f"{' '.join(cmd)} timed out after {timeout}s") from exc
    except RollbackError as exc:
        emit_call(op, started, telemetry.STATUS_ERROR, exc.taxonomy)
        raise
    except OSError as exc:
        emit_call(op, started, telemetry.STATUS_ERROR, "GCLOUD_ERROR")
        raise RollbackError("GCLOUD_ERROR", f"{type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
        taxonomy = classify(result.stderr, cfg)
        emit_call(op, started, telemetry.STATUS_ERROR, taxonomy, returncode=result.returncode)
        raise RollbackError(taxonomy, lines[-1] if lines else f"exit code {result.returncode}")
    emit_call(op, started, telemetry.STATUS_OK, telemetry.NO_ERROR, returncode=result.returncode)
    return result


def run_json(runner: Runner, cmd: list[str], cfg: Mapping[str, Any], op: str = "rollback.gcloud") -> Any:
    result = run_command(runner, cmd, cfg, op)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RollbackError("MALFORMED_GCLOUD_JSON", f"{' '.join(cmd)} did not return JSON") from exc


def _parse_time(raw: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise RollbackError("MALFORMED_GCLOUD_JSON", f"bad timestamp {raw!r}") from exc


def _timestamp(revision: Mapping[str, Any]) -> datetime:
    return _parse_time(revision.get("metadata", {}).get("creationTimestamp"))


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


def _revision_image(revisions: list[Any], name: str) -> Any:
    for rev in revisions:
        if isinstance(rev, dict) and rev.get("metadata", {}).get("name") == name:
            containers = rev.get("spec", {}).get("containers") or [{}]
            return containers[0].get("image") if isinstance(containers[0], dict) else None
    return None


def plan_and_execute(args: argparse.Namespace, cfg: Mapping[str, Any], runner: Runner, project: str) -> dict[str, Any]:
    """Traffic mode: shift 100% back to the newest Ready revision older than the serving one."""
    values = {"gcloud": cfg["gcloud_bin"], "service": args.service or cfg["service"],
              "region": args.region or cfg["region"], "project": project}
    commands = cfg["commands"]
    current = current_revision(run_json(runner, render(commands["describe_service"], values), cfg, "rollback.describe"))
    revisions = run_json(runner, render(commands["list_revisions"], values), cfg, "rollback.revisions")
    previous = previous_revision(revisions, current)
    update = render(commands["update_traffic"], {**values, "revision": previous, "percent": cfg["rollback_traffic_percent"]})
    plan = {"rollback_mode": TRAFFIC, "service": values["service"], "region": values["region"], "project": project,
            "current_revision": current, "previous_revision": previous, "command": update, "executed": False}
    if args.bad_image and _revision_image(revisions, current) != args.bad_image:
        return {**plan, "skipped": f"serving revision {current} does not run the rejected image"}
    if args.execute:
        run_command(runner, update, cfg, "rollback.update_traffic")
    return {**plan, "executed": bool(args.execute)}


def release_history(runner: Runner, cfg: Mapping[str, Any], cicd: Mapping[str, Any],
                    values: Mapping[str, Any]) -> list[tuple[datetime, str]]:
    """(newest SUCCEEDED rollout time, release id) for releases deployed to the target, newest first."""
    commands, success = cicd["commands"], cicd["cd_rollback"]["success_state"]
    releases = run_json(runner, render(commands["releases_list"], values), cfg, "rollback.releases")
    if not isinstance(releases, list):
        raise RollbackError("MALFORMED_GCLOUD_JSON", "releases list is not a JSON array")
    history = []
    for release in releases:
        name = str(release.get("name") or "").rsplit("/", 1)[-1] if isinstance(release, dict) else ""
        if not name:
            raise RollbackError("MALFORMED_GCLOUD_JSON", "release entry without a name")
        if release.get("abandoned"):
            continue
        listed = run_json(runner, render(commands["rollouts_list"], {**values, "release": name}), cfg, "rollback.rollouts")
        if not isinstance(listed, list):
            raise RollbackError("MALFORMED_GCLOUD_JSON", f"rollouts of {name} are not a JSON array")
        times = [_parse_time(item.get("deployEndTime") or item.get("createTime")) for item in listed
                 if isinstance(item, dict) and item.get("targetId") == values["target"] and item.get("state") == success]
        if times:
            history.append((max(times), name))
    return sorted(history, reverse=True)


def plan_deploy(args: argparse.Namespace, cfg: Mapping[str, Any], cicd: Mapping[str, Any], runner: Runner,
                project: str) -> dict[str, Any] | None:
    """Cloud Deploy rollback plan, or None when the target has no previous SUCCEEDED release."""
    cd, rb = cicd["cd"], cicd["cd_rollback"]
    values = {"gcloud": cfg["gcloud_bin"], "project": project, "region": args.region or cfg["region"],
              "pipeline": cd["delivery_pipeline"], "target": cd["target"], "limit": rb["release_scan_limit"]}
    history = release_history(runner, cfg, cicd, values)
    if not history:
        return None
    current = history[0][1]
    base = {"rollback_mode": DEPLOY, "project": project, "region": values["region"], "target": cd["target"],
            "current_release": current, "executed": False}
    if args.bad_release and current != args.bad_release:
        return {**base, "skipped": f"target is on {current}, not the rejected release {args.bad_release}"}
    previous = next((name for _, name in history[1:] if name != args.bad_release), None)
    if previous is None:
        return None
    stamp = datetime.now(timezone.utc).strftime(cicd["formats"]["name_stamp"])
    rollout = rb["rollout_id_template"].format(stamp=stamp)
    named = {**values, "release": previous, "rollout": rollout}
    return {**base, "previous_release": previous, "rollout": rollout,
            "command": render(cicd["commands"]["target_rollback"], named),
            "describe": render(cicd["commands"]["rollout_describe"], named)}


def execute_deploy(plan: dict[str, Any], cfg: Mapping[str, Any], cicd: Mapping[str, Any], runner: Runner,
                   sleep: Callable[[float], None], clock: Callable[[], float]) -> dict[str, Any]:
    """Run the Cloud Deploy rollback and poll its rollout to a terminal state (SUCCEEDED or fail closed)."""
    rb = cicd["cd_rollback"]
    run_command(runner, plan["command"], {**cfg, "command_timeout_seconds": cicd["deploy"]["create_timeout_seconds"]},
                "rollback.targets_rollback")
    terminal = {rb["success_state"], *cicd["deploy"]["failed_states"]}
    deadline = clock() + rb["max_wait_seconds"]
    while True:
        doc = run_json(runner, plan["describe"], cfg, "rollback.rollout_describe")
        state = doc.get("state") if isinstance(doc, dict) else None
        if state in terminal:
            break
        if clock() + rb["poll_interval_seconds"] > deadline:
            raise RollbackError("POLL_DEADLINE", f"rollback rollout {plan['rollout']} still {state} at the deadline")
        sleep(rb["poll_interval_seconds"])
    if state != rb["success_state"]:
        raise RollbackError(f"ROLLBACK_ROLLOUT_{state}", f"rollback rollout {plan['rollout']} ended {state}")
    return {**plan, "executed": True, "rollout_state": state}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--service", help="Cloud Run service (default: config/rollback.json)")
    parser.add_argument("--region", help="Cloud Run region (default: config/rollback.json)")
    parser.add_argument("--project", help="GCP project (default: $GOOGLE_CLOUD_PROJECT)")
    parser.add_argument("--execute", action="store_true", help="roll back (default is a dry-run)")
    parser.add_argument("--mode", help="auto | deploy | traffic (default: config/cicd.json cd_rollback.default_mode)")
    parser.add_argument("--bad-release", help="only roll back Cloud Deploy if the target is on this release")
    parser.add_argument("--bad-image", help="only shift traffic if the serving revision runs this image")
    parser.add_argument("--config-root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _rollback(args: argparse.Namespace, store: ConfigStore, cfg: Mapping[str, Any], runner: Runner, project: str,
              sleep: Callable[[float], None], clock: Callable[[], float]) -> dict[str, Any]:
    cicd = store.get("cicd")
    mode = args.mode or cicd["cd_rollback"]["default_mode"]
    if mode not in cicd["cd_rollback"]["modes"]:
        raise RollbackError("BAD_MODE", f"mode {mode!r} is not one of {cicd['cd_rollback']['modes']}")
    if mode == TRAFFIC:
        return plan_and_execute(args, cfg, runner, project)
    plan = plan_deploy(args, cfg, cicd, runner, project)
    if plan is None and mode == DEPLOY:
        raise RollbackError("NO_PREVIOUS_RELEASE", "the target has no previous SUCCEEDED Cloud Deploy release")
    if plan is None:
        return {**plan_and_execute(args, cfg, runner, project), "selected_by": "no previous SUCCEEDED release"}
    if args.execute and "skipped" not in plan:
        plan = execute_deploy(plan, cfg, cicd, runner, sleep, clock)
    return {key: value for key, value in plan.items() if key != "describe"}


def run_rollback(argv: list[str] | None = None, runner: Runner | None = None, environ: Mapping[str, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> tuple[int, dict[str, Any]]:
    """Library entry point for workflow_ctl and tests: returns (exit code, summary dict); never raises."""
    args = parse_args(argv)
    env = os.environ if environ is None else environ
    mode = "execute" if args.execute else "dry-run"
    try:
        store = ConfigStore(args.config_root)
        cfg = store.get("rollback")
        project = args.project or env.get(cfg["project_env_var"])
        if not project:
            raise RollbackError("MISSING_PROJECT", f"pass --project or set {cfg['project_env_var']}")
        summary = _rollback(args, store, cfg, runner or subprocess_runner, project, sleep, clock)
        return EXIT_OK, {"status": "ok", "mode": mode, **summary}
    except (RollbackError, ConfigError) as exc:
        return EXIT_FAIL, {"status": "error", "mode": mode, "executed": False, "error_taxonomy": exc.taxonomy,
                           "detail": getattr(exc, "detail", str(exc))}


def main(argv: list[str] | None = None, runner: Runner | None = None, environ: Mapping[str, str] | None = None,
         stdout: IO[str] | None = None, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> int:
    started = time.perf_counter()
    trace_id = telemetry.new_trace_id()
    token = telemetry.bind_trace_id(trace_id)
    out = stdout if stdout is not None else sys.stdout
    previous_stream = telemetry.use_stream(sys.stderr)
    try:
        code, summary = run_rollback(argv, runner, environ, sleep, clock)
    finally:
        telemetry.use_stream(previous_stream)
        telemetry.reset_trace_id(token)
    summary.update({"trace_id": trace_id, "latency_ms": telemetry.elapsed_ms(started)})
    out.write(json.dumps(summary, sort_keys=True) + "\n")
    out.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
