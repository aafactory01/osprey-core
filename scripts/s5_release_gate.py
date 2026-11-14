#!/usr/bin/env python3
"""S5_RELEASE_GATE: run Cloud Build CI for one clean commit and verify its immutable @sha256 release packet.

Submits `gcloud builds submit <source> --config=<source>/cloudbuild.yaml --project --region
--substitutions=COMMIT_SHA=<40 hex>,SHORT_SHA=<7 hex> --async` (templates in config/cicd.json), polls
`gcloud builds describe` to a terminal status and, on SUCCESS, cross-checks the digest Cloud Build recorded in
results.images against the release packet the build uploaded to GCS (`gcloud storage cat`). The verified packet
is written locally with a packet_sha256 over its canonical JSON; stdout carries one JSON summary line.

  python3 scripts/s5_release_gate.py --commit "$(git rev-parse HEAD)"
  python3 scripts/s5_release_gate.py --commit <sha> --build-id <id>     # re-verify an existing build

Exit codes (config/cicd.json exit_codes): 0 verified; 1 failed (summary names failed_step and bounce_to);
75 transient (429/503/timeouts/poll deadline: rerun with --build-id when the summary carries one).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import IO, Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config_store import REPO_ROOT, ConfigError  # noqa: E402
from scripts.cicd_common import (  # noqa: E402
    SEAL_KEY, CicdError, Deps, call, call_json, command, error_fields, exit_code_for, load_config, poll,
    require_format, resolve, run_cli, seal, short_sha, utc_iso, verify_checkout, write_json,
)


class CrosscheckError(CicdError):
    """The build succeeded but its evidence disagrees (routes to the verification bounce, not the build bounce)."""


def console_url(cfg: Mapping[str, Any], build_id: str | None) -> str | None:
    if not build_id:
        return None
    return cfg["release_gate"]["console_url_template"].format(region=cfg["region"], build_id=build_id,
                                                             project=cfg["project_id"])


def submit_build(deps: Deps, cfg: Mapping[str, Any], source: Path, commit: str, short: str) -> str:
    config_path = source / cfg["ci"]["config_file"]
    if not config_path.is_file():
        raise CicdError("MISSING_BUILD_CONFIG", f"{config_path} not found")
    cmd = command(cfg, "build_submit", source=str(source), config_path=str(config_path), commit_sha=commit,
                  short_sha=short)
    result = call(deps.runner, cmd, cfg, "gcloud.builds.submit", cfg["release_gate"]["submit_timeout_seconds"])
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return require_format(cfg, "build_id", lines[-1] if lines else "")


def wait_for_build(deps: Deps, cfg: Mapping[str, Any], build_id: str) -> dict[str, Any]:
    gate = cfg["release_gate"]
    cmd = command(cfg, "build_describe", build_id=build_id)

    def fetch() -> dict[str, Any]:
        doc = call_json(deps.runner, cmd, cfg, "gcloud.builds.describe")
        if not isinstance(doc, dict):
            raise CicdError("MALFORMED_GCLOUD_JSON", "build description is not a JSON object")
        return doc

    return poll(fetch, lambda doc: doc.get("status") in gate["terminal_statuses"], gate["poll_interval_seconds"],
                gate["max_wait_seconds"], deps, f"build {build_id}")


def first_failed_step(build: Mapping[str, Any], gate: Mapping[str, Any]) -> str | None:
    for step in build.get("steps") or []:
        if isinstance(step, dict) and step.get("status") in gate["failed_step_statuses"]:
            return str(step.get("id") or step.get("name") or "")
    return None


def _results_digest(cfg: Mapping[str, Any], build: Mapping[str, Any], tag_ref: str) -> str:
    images = (build.get("results") or {}).get("images") or []
    digests = [image.get("digest") for image in images if isinstance(image, dict) and image.get("name") == tag_ref]
    if len(digests) != 1:
        raise CrosscheckError("RESULTS_IMAGE_MISSING", f"results.images has no single entry for {tag_ref}")
    return require_format(cfg, "image_digest", digests[0])


def crosscheck(deps: Deps, cfg: Mapping[str, Any], build: Mapping[str, Any], commit: str, short: str,
               build_id: str) -> tuple[dict[str, Any], str, int]:
    """results.images digest == GCS packet digest, and the packet names this commit, build, and project."""
    gate = cfg["release_gate"]
    repo = cfg["ci"]["image_repository"]
    tag_ref = f"{repo}:{short}"
    keys = gate["substitution_keys"]
    subs = build.get("substitutions") or {}
    if subs.get(keys["commit_sha"]) != commit or subs.get(keys["short_sha"]) != short:
        raise CrosscheckError("SUBSTITUTION_MISMATCH", f"build {build_id} was not built from commit {commit}")
    try:
        digest = _results_digest(cfg, build, tag_ref)
        artifact_uri = f"{gate['artifact_prefix']}/{short}/{gate['packet_artifact']}"
        cloud = call_json(deps.runner, command(cfg, "artifact_cat", artifact_uri=artifact_uri), cfg, "gcloud.storage.cat")
    except CicdError as exc:
        if exc.transient or isinstance(exc, CrosscheckError):
            raise
        raise CrosscheckError(exc.taxonomy, exc.detail) from exc
    expected = {"schema": gate["packet_schema"], "commit_sha": commit, "short_sha": short, "build_id": build_id,
                "project_id": cfg["project_id"], "image_tag_ref": tag_ref, "image_ref": f"{repo}@{digest}",
                "image_digest": digest}
    if not isinstance(cloud, dict):
        raise CrosscheckError("PACKET_MALFORMED", f"{artifact_uri} is not a JSON object")
    mismatched = sorted(key for key, value in expected.items() if cloud.get(key) != value)
    if mismatched:
        raise CrosscheckError("PACKET_MISMATCH", f"cloud packet disagrees with the build on: {', '.join(mismatched)}")
    summary = cloud.get("pytest_summary")
    passed = re.search(gate["pytest_passed_regex"], summary) if isinstance(summary, str) else None
    if passed is None or re.search(gate["pytest_failure_regex"], summary or ""):
        raise CrosscheckError("PYTEST_SUMMARY", f"in-cloud pytest summary is not a clean pass: {summary!r}")
    return cloud, artifact_uri, int(passed.group(1))


def _gate(deps: Deps, cfg: Mapping[str, Any], commit: str, source: Path, packet_path: Path,
          state: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    gate = cfg["release_gate"]
    require_format(cfg, "commit_sha", commit)
    short = short_sha(cfg, commit)
    if state["build_id"] is None:
        verify_checkout(deps, cfg, source, commit)
        state["build_id"] = submit_build(deps, cfg, source, commit, short)
    build_id = require_format(cfg, "build_id", state["build_id"])
    build = wait_for_build(deps, cfg, build_id)
    status = build.get("status")
    base = {"stage": cfg["stage_ids"]["release_gate"], "build_id": build_id, "status": status,
            "commit_sha": commit, "console_url": console_url(cfg, build_id)}
    if status in gate["transient_statuses"]:
        raise CicdError(f"BUILD_{status}", f"Cloud Build ended {status}", transient=True)
    if status != gate["success_status"]:
        step = first_failed_step(build, gate)
        route = gate["bounce_routes"].get(step, gate["default_bounce"]) if step else gate["default_bounce"]
        info = build.get("failureInfo") or {}
        return int(cfg["exit_codes"]["fail"]), {
            **base, "ok": False, "failed_step": step, "bounce_to": route, "error_taxonomy": f"BUILD_{status}",
            "failure_type": info.get("type"), "detail": info.get("detail") or build.get("statusDetail")}
    cloud, artifact_uri, passed = crosscheck(deps, cfg, build, commit, short, build_id)
    local = {key: value for key, value in cloud.items() if key != SEAL_KEY}
    local.update(build_status=status, region=cfg["region"], artifact_uri=artifact_uri, pytest_passed=passed,
                 console_url=base["console_url"], verified_at=utc_iso(deps))
    packet = seal(local)
    write_json(packet_path, packet, cfg)
    return exit_code_for(cfg, None), {
        **base, "ok": True, "image_ref": packet["image_ref"], "image_digest": packet["image_digest"],
        "pytest_summary": packet["pytest_summary"], "pytest_passed": passed, "packet": str(packet_path),
        "packet_sha256": packet[SEAL_KEY]}


def run_release_gate(commit: str, source: str | Path | None = None, packet_path: str | Path | None = None,
                     build_id: str | None = None, config_root: str | Path = REPO_ROOT,
                     deps: Deps | None = None) -> tuple[int, dict[str, Any]]:
    """Library entry point for workflow_ctl and tests: returns (exit code, summary dict); never raises."""
    deps = deps or Deps()
    try:
        cfg = load_config(config_root)
    except ConfigError as exc:
        return exit_code_for(None, exc), {"ok": False, **error_fields(exc)}
    state: dict[str, Any] = {"build_id": build_id}
    target = resolve(packet_path or cfg["evidence"]["s5_packet"])
    try:
        return _gate(deps, cfg, commit, Path(source or REPO_ROOT).resolve(), target, state)
    except CicdError as exc:
        gate = cfg["release_gate"]
        summary = {"ok": False, "stage": cfg["stage_ids"]["release_gate"], "build_id": state["build_id"],
                   "commit_sha": commit, "console_url": console_url(cfg, state["build_id"]), **error_fields(exc)}
        if not exc.transient:
            summary["bounce_to"] = gate["crosscheck_bounce"] if isinstance(exc, CrosscheckError) else gate["default_bounce"]
        return exit_code_for(cfg, exc), summary


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--commit", required=True, help="40-hex commit to build (must equal HEAD of --source)")
    parser.add_argument("--source", default=str(REPO_ROOT), help="clean git checkout to upload (default: repo root)")
    parser.add_argument("--packet", help="local packet path (default: config/cicd.json evidence.s5_packet)")
    parser.add_argument("--build-id", help="re-verify this existing Cloud Build instead of submitting")
    parser.add_argument("--config-root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, deps: Deps | None = None, stdout: IO[str] | None = None) -> int:
    args = parse_args(argv)
    return run_cli(lambda: run_release_gate(args.commit, args.source, args.packet, args.build_id, args.config_root,
                                            deps), stdout)


if __name__ == "__main__":
    raise SystemExit(main())
