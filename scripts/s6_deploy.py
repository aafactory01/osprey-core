#!/usr/bin/env python3
"""S6_DEPLOY_CLOSE (deploy half): roll one verified S5 release packet out through Google Cloud Deploy.

Validates the packet (schema, formats, packet_sha256), re-checks the Cloud Build result (SUCCESS, same digest)
and the Artifact Registry tag (same digest), requires the release source to be a clean checkout of the packet
commit, then runs `gcloud deploy releases create rel-<short>-<UTC %Y%m%d-%H%M%S> --images=osprey-core=<repo>@sha256:..
--deploy-parameters=commit_sha=..,short_sha=..,build_id=..,image_digest=..,release=..` (templates in
config/cicd.json), polls `gcloud deploy rollouts list` to SUCCEEDED/FAILED, and confirms the serving Cloud Run
revision (100% of traffic) runs exactly <repo>@<digest> with the rendered provenance env. The report is written
in every outcome (new_revision_serving tells workflow_ctl whether a rollback is needed).

  python3 scripts/s6_deploy.py --packet .workflow/evidence/s5_release_packet.json
  python3 scripts/s6_deploy.py --packet <packet> --resume-release <rel>    # keep polling an existing release

Exit codes: 0 deployed and verified; 1 failed; 75 transient (429/503/timeouts/poll deadline).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import IO, Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import telemetry  # noqa: E402
from app.config_store import REPO_ROOT, ConfigError  # noqa: E402
from scripts.cicd_common import (  # noqa: E402
    CicdError, Deps, call, call_json, command, container_env, error_fields, exit_code_for, first_container,
    load_config, load_packet, poll, resolve, resource_name, run_cli, serving_revision, service_urls, utc_iso,
    verify_checkout, write_json,
)


def recheck_build(deps: Deps, cfg: Mapping[str, Any], packet: Mapping[str, Any]) -> None:
    """The packet's Cloud Build must still read SUCCESS with the same pushed digest."""
    build = call_json(deps.runner, command(cfg, "build_describe", build_id=packet["build_id"]), cfg,
                      "gcloud.builds.describe")
    if not isinstance(build, dict) or build.get("status") != cfg["release_gate"]["success_status"]:
        status = build.get("status") if isinstance(build, dict) else None
        raise CicdError("BUILD_NOT_SUCCESS", f"build {packet['build_id']} status is {status!r}")
    images = (build.get("results") or {}).get("images") or []
    digests = {image.get("digest") for image in images if isinstance(image, dict)
               and image.get("name") == packet["image_tag_ref"]}
    if digests != {packet["image_digest"]}:
        raise CicdError("BUILD_DIGEST_MISMATCH", f"build results digest {sorted(map(str, digests))} != packet digest")


def recheck_tag(deps: Deps, cfg: Mapping[str, Any], packet: Mapping[str, Any]) -> None:
    """The Artifact Registry tag :<short> must resolve to the packet digest (no re-tagging since S5)."""
    doc = call_json(deps.runner, command(cfg, "tag_describe", image_tag_ref=packet["image_tag_ref"]), cfg,
                    "gcloud.artifacts.docker.images.describe")
    digest = ((doc.get("image_summary") or {}) if isinstance(doc, dict) else {}).get("digest")
    if digest != packet["image_digest"]:
        raise CicdError("TAG_DIGEST_MISMATCH", f"{packet['image_tag_ref']} resolves to {digest!r}, not the packet digest")


def create_release(deps: Deps, cfg: Mapping[str, Any], packet: Mapping[str, Any], source: Path, release: str) -> None:
    cmd = command(cfg, "release_create", release=release, source=str(source), image_ref=packet["image_ref"],
                  commit_sha=packet["commit_sha"], short_sha=packet["short_sha"], build_id=packet["build_id"],
                  image_digest=packet["image_digest"])
    call(deps.runner, cmd, cfg, "gcloud.deploy.releases.create", cfg["deploy"]["create_timeout_seconds"])


def target_rollout(rollouts: Any, target: str) -> dict[str, Any] | None:
    """Newest rollout of this release to `target` (None while Cloud Deploy has not created it yet)."""
    if not isinstance(rollouts, list):
        raise CicdError("MALFORMED_GCLOUD_JSON", "rollouts list is not a JSON array")
    mine = [item for item in rollouts if isinstance(item, dict) and item.get("targetId") == target]
    return max(mine, key=lambda item: str(item.get("createTime") or "")) if mine else None


def wait_for_rollout(deps: Deps, cfg: Mapping[str, Any], release: str) -> dict[str, Any]:
    dep = cfg["deploy"]
    terminal = {dep["success_state"], *dep["failed_states"]}
    cmd = command(cfg, "rollouts_list", release=release)

    def done(rollout: dict[str, Any] | None) -> bool:
        return rollout is not None and rollout.get("state") in terminal

    def fetch() -> dict[str, Any] | None:
        return target_rollout(call_json(deps.runner, cmd, cfg, "gcloud.deploy.rollouts.list"), cfg["cd"]["target"])

    return poll(fetch, done, dep["poll_interval_seconds"], dep["max_wait_seconds"], deps, f"rollout of {release}")


def observe_serving(deps: Deps, cfg: Mapping[str, Any], packet: Mapping[str, Any],
                    report: dict[str, Any]) -> dict[str, Any]:
    """Record the revision holding 100% of traffic, its image, env, and URLs; returns the revision env."""
    service = call_json(deps.runner, command(cfg, "service_describe"), cfg, "gcloud.run.services.describe")
    revision = serving_revision(service, cfg["deploy"]["full_traffic_percent"])
    doc = call_json(deps.runner, command(cfg, "revision_describe", revision=revision), cfg,
                    "gcloud.run.revisions.describe")
    container = first_container(doc)
    image = container.get("image")
    report.update(revision=revision, image=image, new_revision_serving=image == packet["image_ref"],
                  urls=service_urls(service, cfg["deploy"]["urls_annotation"]))
    return container_env(container)


def verify_provenance(cfg: Mapping[str, Any], packet: Mapping[str, Any], release: str, env: Mapping[str, Any]) -> None:
    cd = cfg["cd"]
    expected = {"commit_sha": packet["commit_sha"], "short_sha": packet["short_sha"], "build_id": packet["build_id"],
                "image_digest": packet["image_digest"], "release": release, "pipeline": cd["delivery_pipeline"],
                "target": cd["target"]}
    names = cfg["provenance_env"]
    wrong = sorted(names[key] for key, value in expected.items() if env.get(names[key]) != value)
    if wrong:
        raise CicdError("PROVENANCE_ENV_MISMATCH", f"serving revision env differs from the release on: {', '.join(wrong)}")


def _deploy(deps: Deps, cfg: Mapping[str, Any], packet_path: Path, source: Path, resume: str | None,
            report: dict[str, Any]) -> None:
    packet = load_packet(cfg, packet_path)
    report.update(commit_sha=packet["commit_sha"], short_sha=packet["short_sha"], build_id=packet["build_id"],
                  image_digest=packet["image_digest"], packet_sha256=packet["packet_sha256"])
    verify_checkout(deps, cfg, source, packet["commit_sha"])
    recheck_build(deps, cfg, packet)
    recheck_tag(deps, cfg, packet)
    timings = report["timings"]
    release = resume or resource_name(cfg, cfg["deploy"]["release_name_template"], deps, short_sha=packet["short_sha"])
    report["release"] = release
    if resume is None:
        started = time.perf_counter()
        create_release(deps, cfg, packet, source, release)
        timings["create_release_ms"] = telemetry.elapsed_ms(started)
    started = time.perf_counter()
    rollout = wait_for_rollout(deps, cfg, release)
    timings["rollout_wait_ms"] = telemetry.elapsed_ms(started)
    report.update(rollout=str(rollout.get("name") or "").rsplit("/", 1)[-1], state=rollout.get("state"),
                  failure_reason=rollout.get("failureReason"))
    env = observe_serving(deps, cfg, packet, report)
    if rollout.get("state") != cfg["deploy"]["success_state"]:
        raise CicdError(f"ROLLOUT_{rollout.get('state')}", f"rollout {report['rollout']} ended {rollout.get('state')}")
    if not report["new_revision_serving"]:
        raise CicdError("IMAGE_MISMATCH", f"serving revision {report['revision']} does not run {packet['image_ref']}")
    verify_provenance(cfg, packet, release, env)


def run_deploy(packet_path: str | Path, report_path: str | Path | None = None, source: str | Path | None = None,
               resume_release: str | None = None, config_root: str | Path = REPO_ROOT,
               deps: Deps | None = None) -> tuple[int, dict[str, Any]]:
    """Library entry point for workflow_ctl and tests: returns (exit code, summary dict); never raises."""
    deps = deps or Deps()
    try:
        cfg = load_config(config_root)
    except ConfigError as exc:
        return exit_code_for(None, exc), {"ok": False, **error_fields(exc)}
    started = time.perf_counter()
    report: dict[str, Any] = {"schema": cfg["deploy"]["report_schema"], "stage": cfg["stage_ids"]["deploy_close"],
                              "ok": False, "release": resume_release, "new_revision_serving": None,
                              "started_at": utc_iso(deps), "timings": {}}
    error: CicdError | None = None
    try:
        _deploy(deps, cfg, resolve(packet_path), Path(source or REPO_ROOT).resolve(), resume_release, report)
    except CicdError as exc:
        error = exc
    report.update(ok=error is None, finished_at=utc_iso(deps), **(error_fields(error) if error else {}))
    report["timings"]["total_ms"] = telemetry.elapsed_ms(started)
    target = resolve(report_path or cfg["evidence"]["s6_deploy_report"])
    write_json(target, report, cfg)
    summary = {key: value for key, value in report.items() if key != "urls"}  # URLs stay in the git-ignored report
    return exit_code_for(cfg, error), {**summary, "report": str(target)}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--packet", required=True, help="verified S5 release packet (.workflow/evidence/...)")
    parser.add_argument("--report", help="deploy report path (default: config/cicd.json evidence.s6_deploy_report)")
    parser.add_argument("--source", default=str(REPO_ROOT), help="clean checkout of the packet commit (release source)")
    parser.add_argument("--resume-release", help="poll and verify this existing release instead of creating one")
    parser.add_argument("--config-root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, deps: Deps | None = None, stdout: IO[str] | None = None) -> int:
    args = parse_args(argv)
    return run_cli(lambda: run_deploy(args.packet, args.report, args.source, args.resume_release, args.config_root,
                                      deps), stdout)


if __name__ == "__main__":
    raise SystemExit(main())
