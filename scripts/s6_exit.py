#!/usr/bin/env python3
"""S6_DEPLOY_CLOSE (exit gate): prove the serving revision is live behind IAP with the expected provenance.

Modes (config/cicd.json exit_probe.modes):
  verify     after s6_deploy: (a) every service URL answers an anonymous GET / with a 302 to accounts.google.com
             or a 401; (b) with `Proxy-Authorization: Bearer <ID token>`, GET /, /health, /ready and /api/pipeline
             answer 200 and /api/pipeline provenance matches the packet (commit_sha, image_digest, build_id) and
             the deploy report (release, revision); (c) the service keeps IAP on, ingress all, min/max scale 1,
             the runtime service account, and 100% of traffic on the reported revision running the packet image;
             (d) IAM still grants the onboard SA run.invoker + iap.httpsResourceAccessor and the IAP agent invoker.
  reprobe    after s6_rollback: (a), (b) on /, /health and /ready (the restored revision may predate
             /api/pipeline), (c) with the serving revision no longer running the rejected packet image, (d).
  preflight  before a release: (a), (b) on /health only, (c) without image checks, (d).

The ID token comes from `--token-command` (default: $IAP_TOKEN_COMMAND), a command that prints one token on
stdout; it stays in memory and is never logged or written. URLs come from `gcloud run services describe`
(status.url + run.googleapis.com/urls); none are committed. Cold-start retries come from config.

  IAP_TOKEN_COMMAND='<operator token command>' python3 scripts/s6_exit.py --packet <packet> --report <report>

Exit codes: 0 pass; 1 fail; 75 transient (429/5xx/timeouts after the configured cold-start retries).
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import telemetry  # noqa: E402
from app.config_store import REPO_ROOT, ConfigError  # noqa: E402
from scripts.cicd_common import (  # noqa: E402
    CicdError, Deps, call, call_json, command, emit_call, error_fields, exit_code_for, first_container, load_config,
    load_packet, parse_json, read_json, resolve, run_cli, serving_revision, service_urls, utc_iso, write_json,
)

VERIFY, REPROBE, PREFLIGHT = "verify", "reprobe", "preflight"


@dataclass(frozen=True)
class HttpResponse:
    """One HTTP exchange (redirects are never followed): status, lower-cased headers, bounded body."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand 3xx responses back to the caller instead of following them (the IAP gate inspects Location)."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _lowered(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in (headers.items() if headers else ())}


def urllib_client(url: str, headers: Mapping[str, str], timeout: float, max_bytes: int) -> HttpResponse:
    """Default HTTP client: one GET, no redirects, bounded body; network failures are transient errors."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with opener.open(request, timeout=timeout) as resp:
            return HttpResponse(resp.status, _lowered(resp.headers), resp.read(max_bytes))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(max_bytes) if exc.fp is not None else b""
        finally:
            exc.close()
        return HttpResponse(exc.code, _lowered(exc.headers), body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CicdError("HTTP_UNREACHABLE", f"{type(exc).__name__}: {reason}", transient=True) from exc


def http_get(deps: Deps, url: str, headers: Mapping[str, str], probe: Mapping[str, Any], op: str) -> HttpResponse:
    """One GET with telemetry (the URL and headers are never logged: they carry hostnames and the token)."""
    client = deps.http or urllib_client
    started = time.perf_counter()
    try:
        response = client(url, headers, probe["http_timeout_seconds"], probe["max_body_bytes"])
    except CicdError as exc:
        emit_call(op, started, telemetry.STATUS_ERROR, exc.taxonomy)
        raise
    emit_call(op, started, telemetry.STATUS_OK, telemetry.NO_ERROR, http_status=response.status)
    return response


@dataclass
class Probe:
    """One probe run: config, side effects, and the ordered list of check results."""

    cfg: Mapping[str, Any]
    deps: Deps
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def probe(self) -> Mapping[str, Any]:
        return self.cfg["exit_probe"]

    def record(self, check: str, ok: bool, detail: str, **fields: Any) -> None:
        self.checks.append({"check": check, "ok": bool(ok), "detail": detail, **fields})


def fetch_token(deps: Deps, cfg: Mapping[str, Any], command_text: str | None) -> str:
    probe = cfg["exit_probe"]
    if not command_text:
        raise CicdError("TOKEN_COMMAND_MISSING", f"pass --token-command or set {probe['token_env_var']}")
    try:
        argv = shlex.split(command_text)
    except ValueError as exc:
        raise CicdError("TOKEN_COMMAND_INVALID", "token command is not valid shell syntax") from exc
    result = call(deps.runner, argv, cfg, "iap.token", probe["token_timeout_seconds"], ("TOKEN_UNAVAILABLE", True))
    token = result.stdout.strip()
    if not re.fullmatch(cfg["formats"]["id_token"], token):
        raise CicdError("TOKEN_MALFORMED", "token command did not print exactly one ID token")
    return token


def get(state: Probe, url: str, headers: Mapping[str, str], op: str) -> HttpResponse:
    """GET with cold-start retries on retry_statuses and transient network errors (config-driven)."""
    probe = state.probe
    for attempt in range(1, probe["cold_start_attempts"] + 1):
        last = attempt == probe["cold_start_attempts"]
        try:
            response = http_get(state.deps, url, headers, probe, op)
        except CicdError as exc:
            if last or not exc.transient:
                raise
        else:
            if response.status not in probe["retry_statuses"]:
                return response
            if last:
                raise CicdError("COLD_START_EXHAUSTED", f"{op} still answered {response.status}", transient=True)
        state.deps.sleep(probe["cold_start_delay_seconds"])
    raise CicdError("COLD_START_EXHAUSTED", f"{op} made no attempt", transient=True)


def check_unauthenticated(state: Probe, urls: list[str]) -> None:
    probe = state.probe
    for index, url in enumerate(urls):
        response = get(state, url + probe["unauth_path"], {}, f"http.unauth[{index}]")
        host = urllib.parse.urlsplit(response.headers.get("location", "")).hostname or ""
        redirect = response.status in probe["redirect_statuses"]
        ok = response.status in probe["unauth_allowed_statuses"] and (not redirect or host in probe["unauth_redirect_hosts"])
        state.record("unauthenticated_gate", ok, f"{response.status} -> {host or '-'}", url_index=index)


def check_provenance(state: Probe, response: HttpResponse, expected: Mapping[str, Any], index: int) -> None:
    try:
        doc = parse_json(response.body.decode("utf-8"), "provenance endpoint", "PROVENANCE_MALFORMED")
    except (CicdError, UnicodeDecodeError):
        state.record("provenance", False, "response is not JSON", url_index=index)
        return
    found = doc.get(state.probe["provenance_key"]) if isinstance(doc, dict) else None
    if not isinstance(found, dict):
        state.record("provenance", False, "no provenance object", url_index=index)
        return
    wrong = sorted(key for key, value in expected.items() if found.get(key) != value)
    state.record("provenance", not wrong, "match" if not wrong else f"mismatch: {', '.join(wrong)}", url_index=index,
                 observed={key: found.get(key) for key in expected})


def check_authenticated(state: Probe, urls: list[str], paths: list[str], token: str,
                        expected: Mapping[str, Any]) -> None:
    probe = state.probe
    headers = {probe["auth_header"]: f"{probe['auth_scheme']} {token}"}
    for index, url in enumerate(urls):
        for path in paths:
            response = get(state, url + path, headers, f"http.auth[{index}]{path}")
            ok = response.status == probe["ok_status"]
            state.record("authenticated_get", ok, str(response.status), url_index=index, path=path)
            if ok and path == probe["provenance_path"] and expected:
                check_provenance(state, response, expected, index)


def check_service(state: Probe, service: Mapping[str, Any], mode: str, packet: Mapping[str, Any] | None,
                  report: Mapping[str, Any] | None) -> str | None:
    """(c): IAP/ingress/scale annotations, runtime SA, single 100% target, and per-mode image expectations."""
    cfg, probe = state.cfg, state.probe
    meta = service.get("metadata") or {}
    template = (service.get("spec") or {}).get("template") or {}
    for scope, found, wanted in (("service", meta.get("annotations") or {}, probe["expected_service_annotations"]),
                                 ("template", (template.get("metadata") or {}).get("annotations") or {},
                                  probe["expected_template_annotations"])):
        for key, value in wanted.items():
            state.record(f"{scope}_annotation", found.get(key) == value, f"{key}={found.get(key)!r}")
    account = (template.get("spec") or {}).get("serviceAccountName")
    state.record("runtime_service_account", account == cfg["runtime_service_account"], str(account))
    try:
        revision = serving_revision(service, cfg["deploy"]["full_traffic_percent"])
    except CicdError as exc:
        state.record("full_traffic", False, exc.detail)
        return None
    state.record("full_traffic", True, revision)
    if mode == PREFLIGHT:
        return revision
    doc = call_json(state.deps.runner, command(cfg, "revision_describe", revision=revision), cfg,
                    "gcloud.run.revisions.describe")
    image = first_container(doc).get("image")
    if mode == VERIFY:
        state.record("template_image", first_container(service).get("image") == packet["image_ref"], "template")
        state.record("serving_revision", revision == report.get("revision"), revision)
        state.record("serving_image", image == packet["image_ref"], str(image))
    else:
        state.record("rejected_image_gone", image != packet["image_ref"], str(image))
    return revision


def check_iam(state: Probe) -> None:
    cfg, probe, runner = state.cfg, state.probe, state.deps.runner
    invokers = _members(call_json(runner, command(cfg, "run_iam_policy"), cfg, "gcloud.run.get-iam-policy"),
                        probe["invoker_role"])
    accessors = _members(call_json(runner, command(cfg, "iap_iam_policy"), cfg, "gcloud.iap.web.get-iam-policy"),
                         probe["accessor_role"])
    for member in probe["required_invoker_members"]:
        state.record("iam_run_invoker", member in invokers, member)
    for member in probe["required_accessor_members"]:
        state.record("iam_iap_accessor", member in accessors, member)
    agent = any(re.fullmatch(probe["iap_agent_member_regex"], member) for member in invokers)
    state.record("iam_iap_agent_invoker", agent, "IAP service agent holds run.invoker" if agent else "missing")


def _members(policy: Any, role: str) -> set[str]:
    bindings = policy.get("bindings") if isinstance(policy, dict) else None
    if not isinstance(bindings, list):
        return set()
    return {str(m) for b in bindings if isinstance(b, dict) and b.get("role") == role for m in b.get("members") or []}


def _probe(state: Probe, mode: str, packet: Mapping[str, Any] | None, report: Mapping[str, Any] | None,
           token_command: str | None, result: dict[str, Any]) -> None:
    cfg, probe = state.cfg, state.probe
    service = call_json(state.deps.runner, command(cfg, "service_describe"), cfg, "gcloud.run.services.describe")
    if not isinstance(service, dict):
        raise CicdError("MALFORMED_GCLOUD_JSON", "service description is not a JSON object")
    urls = service_urls(service, cfg["deploy"]["urls_annotation"])
    result.update(urls=urls, serving_revision=check_service(state, service, mode, packet, report))
    check_unauthenticated(state, urls)
    token = fetch_token(state.deps, cfg, token_command or state.deps.environ.get(probe["token_env_var"]))
    expected: dict[str, Any] = {}
    if mode == VERIFY:
        expected = {key: packet.get(key) for key in probe["packet_provenance_fields"]}
        expected.update({key: report.get(key) for key in probe["report_provenance_fields"]})
    paths = {VERIFY: probe["auth_paths"], REPROBE: probe["reprobe_paths"], PREFLIGHT: probe["preflight_paths"]}[mode]
    check_authenticated(state, urls, paths, token, expected)
    check_iam(state)


def run_exit(packet_path: str | Path | None = None, report_path: str | Path | None = None, mode: str = VERIFY,
             token_command: str | None = None, out_path: str | Path | None = None, config_root: str | Path = REPO_ROOT,
             deps: Deps | None = None) -> tuple[int, dict[str, Any]]:
    """Library entry point for workflow_ctl and tests: returns (exit code, summary dict); never raises."""
    deps = deps or Deps()
    try:
        cfg = load_config(config_root)
    except ConfigError as exc:
        return exit_code_for(None, exc), {"ok": False, **error_fields(exc)}
    state = Probe(cfg, deps)
    result: dict[str, Any] = {"schema": cfg["exit_probe"]["report_schema"], "stage": cfg["stage_ids"]["deploy_close"],
                              "mode": mode, "started_at": utc_iso(deps)}
    error: CicdError | None = None
    try:
        if mode not in cfg["exit_probe"]["modes"]:
            raise CicdError("BAD_MODE", f"mode {mode!r} is not one of {cfg['exit_probe']['modes']}")
        evidence = cfg["evidence"]
        packet = load_packet(cfg, resolve(packet_path or evidence["s5_packet"])) if mode != PREFLIGHT else None
        report = read_json(resolve(report_path or evidence["s6_deploy_report"]), "REPORT") if mode == VERIFY else None
        if mode == VERIFY and not (isinstance(report, dict) and report.get("ok") and report.get("revision")):
            raise CicdError("REPORT_NOT_OK", "the S6 deploy report does not record a verified deploy")
        _probe(state, mode, packet, report, token_command, result)
    except CicdError as exc:
        error = exc
    failed = sorted({check["check"] for check in state.checks if not check["ok"]})
    if error is None and failed:
        error = CicdError("EXIT_GATE_FAILED", f"failed checks: {', '.join(failed)}")
    result.update(ok=error is None, checks=state.checks, failed_checks=failed, finished_at=utc_iso(deps),
                  **(error_fields(error) if error else {}))
    target = resolve(out_path or cfg["evidence"]["s6_exit_reports"][mode if mode in cfg["exit_probe"]["modes"] else VERIFY])
    write_json(target, result, cfg)
    passed = sum(1 for check in state.checks if check["ok"])
    summary = {key: value for key, value in result.items() if key not in ("checks", "urls")}
    return exit_code_for(cfg, error), {**summary, "checks_passed": passed, "checks_total": len(state.checks),
                                       "report": str(target)}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--packet", help="S5 release packet (default: config/cicd.json evidence.s5_packet)")
    parser.add_argument("--report", help="S6 deploy report (default: config/cicd.json evidence.s6_deploy_report)")
    parser.add_argument("--mode", default=VERIFY, help="verify | reprobe | preflight (config exit_probe.modes)")
    parser.add_argument("--token-command", help="command printing an IAP ID token (default: $IAP_TOKEN_COMMAND)")
    parser.add_argument("--out", help="exit report path (default: config/cicd.json evidence.s6_exit_reports[mode])")
    parser.add_argument("--config-root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, deps: Deps | None = None, stdout: IO[str] | None = None) -> int:
    args = parse_args(argv)
    return run_cli(lambda: run_exit(args.packet, args.report, args.mode, args.token_command, args.out,
                                    args.config_root, deps), stdout)


if __name__ == "__main__":
    raise SystemExit(main())
