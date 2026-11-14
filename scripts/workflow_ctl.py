#!/usr/bin/env python3
"""Deterministic S5 -> S6 control loop for one commit (config/cicd.json decides every transition).

  python3 scripts/workflow_ctl.py release --commit <40 hex> [--source DIR] [--token-command CMD]
  python3 scripts/workflow_ctl.py status

release:
  PREFLIGHT        s6_exit --mode preflight (token, IAP gate, IAM on the live service); fail -> HALT, nothing built.
  S5_RELEASE_GATE  s5_release_gate; fail -> "BOUNCE S5_RELEASE_GATE -> <route from config>", exit 1.
  S6_DEPLOY_CLOSE  s6_deploy, then s6_exit --mode verify; pass -> "CLOSE S6_DEPLOY_CLOSE", exit 0.
                   Exit-gate failure (or a failed deploy whose image may be serving) -> s6_rollback --execute
                   (auto mode, guarded by the rejected release and image) -> s6_exit --mode reprobe ->
                   "ROLLBACK & BOUNCE S6_DEPLOY_CLOSE -> S3_BUILD", exit 1. A failed deploy whose image never
                   served needs no rollback -> "BOUNCE S6_DEPLOY_CLOSE -> S3_BUILD", exit 1.
Transient results (exit 75) are retried with config backoff and do not count as a strike; once
workflow.max_attempts is spent the stage counts as failed (PREFLIGHT and S5 halt with exit 75).
Every attempt and verdict is appended to .workflow/evidence/workflow_ledger.jsonl (transition, rc, trace_id,
latency_ms); `status` prints the packet, report, and ledger summaries.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import telemetry  # noqa: E402
from app.config_store import REPO_ROOT, ConfigError  # noqa: E402
from scripts import s5_release_gate, s6_deploy, s6_exit, s6_rollback  # noqa: E402
from scripts.cicd_common import (  # noqa: E402
    CicdError, Deps, append_jsonl, error_fields, is_sealed, load_config, read_json, resolve, utc_iso,
)

StageResult = tuple[int, dict[str, Any]]


@dataclass
class Stages:
    """Injectable stage callables (tests replace them; production runs the real scripts in-process)."""

    gate: Callable[..., StageResult] = s5_release_gate.run_release_gate
    deploy: Callable[..., StageResult] = s6_deploy.run_deploy
    probe: Callable[..., StageResult] = s6_exit.run_exit
    rollback: Callable[..., StageResult] = s6_rollback.run_rollback


class Controller:
    """One `release` run: stage calls with transient retries, ledger writes, and the verdict lines."""

    def __init__(self, cfg: Mapping[str, Any], deps: Deps, stages: Stages, config_root: str | Path,
                 out: IO[str], trace_id: str) -> None:
        self.cfg, self.deps, self.stages, self.out, self.trace_id = cfg, deps, stages, out, trace_id
        self.config_root = str(config_root)
        self.codes = cfg["exit_codes"]
        self.ledger = resolve(cfg["evidence"]["ledger"])

    def record(self, transition: str, rc: int | None, started: float, **fields: Any) -> None:
        append_jsonl(self.ledger, {"ts": utc_iso(self.deps), "trace_id": self.trace_id, "transition": transition,
                                   "rc": rc, "latency_ms": telemetry.elapsed_ms(started), **fields})

    def say(self, key: str, rc: int, **values: Any) -> int:
        line = self.cfg["workflow"]["messages"][key].format(**values)
        self.out.write(line + "\n")
        self.out.flush()
        self.record(f"verdict:{key}", rc, time.perf_counter(), message=line)
        return rc

    def run_stage(self, transition: str, call: Callable[..., StageResult],
                  resume: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> StageResult:
        """Call a stage; exit 75 is retried with config backoff (no strike) up to workflow.max_attempts."""
        workflow, extra = self.cfg["workflow"], {}
        rc, summary = self.codes["transient"], {}
        for attempt in range(1, workflow["max_attempts"] + 1):
            started = time.perf_counter()
            rc, summary = call(**extra)
            self.record(transition, rc, started, attempt=attempt, ok=summary.get("ok"),
                        error_taxonomy=summary.get("error_taxonomy"), detail=summary.get("detail"))
            if rc != self.codes["transient"]:
                return rc, summary
            extra = resume(summary) if resume else extra
            if attempt < workflow["max_attempts"]:
                backoff = workflow["backoff_seconds"]
                self.deps.sleep(backoff[min(attempt, len(backoff)) - 1])
        return rc, summary

    def _rollback(self, argv: list[str]) -> StageResult:
        """s6_rollback keeps its 0/1 CLI contract; transient taxonomies are mapped to exit 75 for retries."""
        transient = {entry["taxonomy"] for entry in self.cfg["error_taxonomy"] if entry["transient"]}
        transient.update(self.cfg["cd_rollback"]["transient_taxonomies"])
        rc, summary = self.stages.rollback(argv, runner=self.deps.runner, environ=self.deps.environ,
                                           sleep=self.deps.sleep, clock=self.deps.clock)
        if rc != self.codes["ok"] and summary.get("error_taxonomy") in transient:
            rc = self.codes["transient"]
        return rc, summary

    def rollback_and_bounce(self, s6: str, image_ref: str | None, deploy: Mapping[str, Any],
                            probe_args: dict[str, Any]) -> int:
        """Guarded rollback, re-probe, then the verdict (an INCIDENT line first if either step failed)."""
        argv = ["--execute", "--project", str(self.cfg["project_id"])]
        argv += ["--config-root", self.config_root]
        if deploy.get("release"):
            argv += ["--bad-release", str(deploy["release"])]
        if image_ref:
            argv += ["--bad-image", image_ref]
        rc_rollback, rolled = self.run_stage(f"{s6}:rollback", lambda **_: self._rollback(argv))
        rc_probe, _ = self.run_stage(f"{s6}:reprobe", lambda **kw: self.stages.probe(
            mode=s6_exit.REPROBE, **probe_args, **kw))
        fail = self.codes["fail"]
        if rc_rollback != self.codes["ok"] or rc_probe != self.codes["ok"]:
            self.say("incident", fail, stage=s6, reason=f"rollback rc={rc_rollback}, reprobe rc={rc_probe}")
        key = "rollback_bounce" if rolled.get("executed") else "bounce"
        return self.say(key, fail, stage=s6, route=self.cfg["deploy"]["bounce_to"])

    def release(self, commit: str, source: str | Path, token_command: str | None) -> int:
        cfg, codes, ids = self.cfg, self.codes, self.cfg["stage_ids"]
        s5, s6, pre = ids["release_gate"], ids["deploy_close"], cfg["workflow"]["preflight_stage"]
        packet, report = resolve(cfg["evidence"]["s5_packet"]), resolve(cfg["evidence"]["s6_deploy_report"])
        common = {"config_root": self.config_root, "deps": self.deps}
        probe_args = {"packet_path": packet, "report_path": report, "token_command": token_command, **common}
        rc, pre_summary = self.run_stage(f"{pre}:probe", lambda **kw: self.stages.probe(
            mode=s6_exit.PREFLIGHT, token_command=token_command, **common, **kw))
        if rc != codes["ok"]:
            return self.say("halt", rc, stage=pre, reason=pre_summary.get("error_taxonomy"))
        rc, gate = self.run_stage(f"{s5}:gate", lambda **kw: self.stages.gate(
            commit=commit, source=source, packet_path=packet, **common, **kw),
            resume=lambda summary: {"build_id": summary["build_id"]} if summary.get("build_id") else {})
        if rc == codes["transient"]:
            return self.say("halt", rc, stage=s5, reason=gate.get("error_taxonomy"))
        if rc != codes["ok"]:
            route = gate.get("bounce_to") or cfg["release_gate"]["default_bounce"]
            return self.say("bounce", codes["fail"], stage=s5, route=route)
        rc, deploy = self.run_stage(f"{s6}:deploy", lambda **kw: self.stages.deploy(
            packet_path=packet, report_path=report, source=source, **common, **kw),
            resume=lambda summary: {"resume_release": summary["release"]} if summary.get("release") else {})
        if rc == codes["ok"]:
            rc, _ = self.run_stage(f"{s6}:exit", lambda **kw: self.stages.probe(
                mode=s6_exit.VERIFY, **probe_args, **kw))
            if rc == codes["ok"]:
                return self.say("close", codes["ok"], stage=s6)
        elif deploy.get("new_revision_serving") is False:
            return self.say("bounce", codes["fail"], stage=s6, route=cfg["deploy"]["bounce_to"])
        return self.rollback_and_bounce(s6, gate.get("image_ref"), deploy, probe_args)


def status(cfg: Mapping[str, Any], out: IO[str]) -> int:
    """Print one JSON line per evidence artifact plus the ledger tail; exit fail if any artifact is malformed."""
    evidence, fields = cfg["evidence"], cfg["workflow"]["status_fields"]
    entries = [("s5_release_packet", evidence["s5_packet"], fields["packet"]),
               ("s6_deploy_report", evidence["s6_deploy_report"], fields["deploy"])]
    entries += [(f"s6_exit_{mode}", path, fields["exit"]) for mode, path in evidence["s6_exit_reports"].items()]
    rc = cfg["exit_codes"]["ok"]
    for name, path, keys in entries:
        line: dict[str, Any] = {"artifact": name, "path": str(resolve(path))}
        try:
            doc = read_json(resolve(path), "EVIDENCE")
            if not isinstance(doc, dict):
                raise CicdError("EVIDENCE_MALFORMED", f"{path} is not a JSON object")
            line.update(present=True, sealed=is_sealed(doc), **{key: doc.get(key) for key in keys})
        except CicdError as exc:
            missing = exc.taxonomy == "EVIDENCE_MISSING"
            line.update(present=not missing, **({} if missing else error_fields(exc)))
            rc = rc if missing else cfg["exit_codes"]["fail"]
        out.write(json.dumps(line, sort_keys=True, default=str) + "\n")
    ledger = resolve(evidence["ledger"])
    lines = ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []
    tail = deque(lines, cfg["workflow"]["status_ledger_tail"])
    out.write(json.dumps({"artifact": "workflow_ledger", "path": str(ledger), "tail": list(tail)}, sort_keys=True) + "\n")
    return int(rc)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config-root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    release = sub.add_parser("release", help="run PREFLIGHT -> S5_RELEASE_GATE -> S6_DEPLOY_CLOSE for one commit")
    release.add_argument("--commit", required=True, help="40-hex commit (HEAD of --source)")
    release.add_argument("--source", default=str(REPO_ROOT), help="clean checkout of --commit (default: repo root)")
    release.add_argument("--token-command", help="IAP ID token command (default: $IAP_TOKEN_COMMAND)")
    sub.add_parser("status", help="print the packet, report, and ledger summaries")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, deps: Deps | None = None, stages: Stages | None = None,
         stdout: IO[str] | None = None) -> int:
    args = parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    try:
        cfg = load_config(args.config_root)
    except ConfigError as exc:
        out.write(f"HALT {exc.taxonomy}: {exc.file}: {exc.detail}\n")
        return 1
    if args.command == "status":
        return status(cfg, out)
    trace_id = telemetry.new_trace_id()
    token = telemetry.bind_trace_id(trace_id)
    previous = telemetry.use_stream(sys.stderr)
    try:
        controller = Controller(cfg, deps or Deps(), stages or Stages(), args.config_root, out, trace_id)
        return controller.release(args.commit, args.source, args.token_command)
    finally:
        telemetry.use_stream(previous)
        telemetry.reset_trace_id(token)


if __name__ == "__main__":
    raise SystemExit(main())
