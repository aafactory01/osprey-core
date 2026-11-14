"""Shared plumbing for the deterministic S5/S6 scripts: config, gcloud runner, telemetry, evidence, HTTP.

Every external call (gcloud, git, the IAP token command, HTTP probes) goes through `call()` or `http_get()`,
which emit one structured telemetry line (trace_id, perf_counter latency_ms, status, error taxonomy) on the
telemetry stream (stderr for the CLIs, so stdout stays one JSON summary line). Failures raise `CicdError`
with an error taxonomy and a transient flag (429/503/timeouts -> exit code 75); nothing is retried silently
and nothing falls back to invented values (fail closed). All templates and tunables live in config/cicd.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Mapping

from app import telemetry
from app.config_store import REPO_ROOT, ConfigError, ConfigStore, reject_json_constant

CONFIG_NAME = "cicd"
SEAL_KEY = "packet_sha256"
EXIT_FAIL = 1


@dataclass(frozen=True)
class RunResult:
    """One finished command: exit code plus captured text streams."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], float], RunResult]


class CicdError(Exception):
    """Fail-closed stop: `taxonomy` names the error class; `transient` errors map to the transient exit code."""

    def __init__(self, taxonomy: str, detail: str, transient: bool = False) -> None:
        super().__init__(detail)
        self.taxonomy = taxonomy
        self.detail = detail
        self.transient = transient


def subprocess_runner(cmd: list[str], timeout: float) -> RunResult:
    """Default runner: real subprocess with captured text, closed stdin, and a hard timeout."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise CicdError("BINARY_NOT_FOUND", f"{cmd[0]} is not on PATH") from exc
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Deps:
    """Injectable side effects (tests pass fakes; production uses the real subprocess/HTTP/clock)."""

    runner: Runner = subprocess_runner
    http: Callable[..., Any] | None = None  # s6_exit.urllib_client when None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    utcnow: Callable[[], datetime] = _utcnow
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))


def emit_call(op: str, started: float, status: str, taxonomy: str, **fields: Any) -> None:
    """One telemetry line per external call (never includes stdout, tokens, or response bodies)."""
    telemetry.emit({"kind": "external_call", "op": op, "trace_id": telemetry.current_trace_id(),
                    "latency_ms": telemetry.elapsed_ms(started), "status": status, "error_taxonomy": taxonomy, **fields})


def classify(text: str, cfg: Mapping[str, Any]) -> tuple[str, bool]:
    """Map gcloud stderr to (taxonomy, transient) with the ordered regexes in config/cicd.json."""
    for entry in cfg["error_taxonomy"]:
        if any(re.search(pattern, text) for pattern in entry["patterns"]):
            return str(entry["taxonomy"]), bool(entry["transient"])
    return str(cfg["default_error_taxonomy"]), False


def render(template: list[str], values: Mapping[str, Any]) -> list[str]:
    try:
        return [part.format_map(values) for part in template]
    except (KeyError, ValueError, IndexError) as exc:
        raise CicdError("CONFIG_SCHEMA", f"bad command template field: {exc}") from exc


def base_values(cfg: Mapping[str, Any]) -> dict[str, Any]:
    cd = cfg["cd"]
    return {"gcloud": cfg["gcloud_bin"], "git": cfg["git_bin"], "project": cfg["project_id"], "region": cfg["region"],
            "service": cfg["service"], "pipeline": cd["delivery_pipeline"], "target": cd["target"],
            "image_name": cd["image_name"], "skaffold_file": cd["skaffold_file"]}


def command(cfg: Mapping[str, Any], name: str, **values: Any) -> list[str]:
    """Render the named command template from config/cicd.json `commands`."""
    return render(cfg["commands"][name], {**base_values(cfg), **values})


def call(runner: Runner, cmd: list[str], cfg: Mapping[str, Any], op: str, timeout: float | None = None,
         default: tuple[str, bool] | None = None) -> RunResult:
    """Run one command with telemetry; non-zero exit raises a classified CicdError (stdout never logged)."""
    limit = cfg["command_timeout_seconds"] if timeout is None else timeout
    started = time.perf_counter()
    try:
        result = runner(cmd, limit)
    except subprocess.TimeoutExpired as exc:
        emit_call(op, started, telemetry.STATUS_ERROR, "TIMEOUT")
        raise CicdError("TIMEOUT", f"{op} timed out after {limit}s", transient=True) from exc
    except CicdError as exc:
        emit_call(op, started, telemetry.STATUS_ERROR, exc.taxonomy)
        raise
    except OSError as exc:
        emit_call(op, started, telemetry.STATUS_ERROR, "EXEC_ERROR")
        raise CicdError("EXEC_ERROR", f"{op}: {type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        taxonomy, transient = classify(result.stderr, cfg)
        if default is not None and taxonomy == cfg["default_error_taxonomy"]:
            taxonomy, transient = default
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        emit_call(op, started, telemetry.STATUS_ERROR, taxonomy, returncode=result.returncode)
        raise CicdError(taxonomy, f"{op}: {lines[-1] if lines else f'exit code {result.returncode}'}", transient)
    emit_call(op, started, telemetry.STATUS_OK, telemetry.NO_ERROR, returncode=result.returncode)
    return result


def parse_json(text: str, what: str, taxonomy: str = "MALFORMED_GCLOUD_JSON") -> Any:
    try:
        return json.loads(text, parse_constant=reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise CicdError(taxonomy, f"{what} did not return valid JSON") from exc


def call_json(runner: Runner, cmd: list[str], cfg: Mapping[str, Any], op: str, timeout: float | None = None) -> Any:
    return parse_json(call(runner, cmd, cfg, op, timeout).stdout, op)


def load_config(config_root: Path | str = REPO_ROOT) -> dict[str, Any]:
    return ConfigStore(config_root).get(CONFIG_NAME)


def require_format(cfg: Mapping[str, Any], kind: str, value: Any) -> str:
    """Validate `value` against config/cicd.json formats[kind] (fail closed with BAD_<KIND>)."""
    pattern = cfg["formats"][kind]
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise CicdError(f"BAD_{kind.upper()}", f"{kind} {value!r} does not match {pattern}")
    return value


def short_sha(cfg: Mapping[str, Any], commit: str) -> str:
    return commit[: cfg["formats"]["short_sha_length"]]


def resource_name(cfg: Mapping[str, Any], template: str, deps: Deps, **values: Any) -> str:
    """Render a Cloud Deploy resource id (release / rollout) with a UTC stamp and validate it."""
    name = template.format(stamp=deps.utcnow().strftime(cfg["formats"]["name_stamp"]), **values)
    if len(name) > cfg["formats"]["resource_name_max_length"]:
        raise CicdError("BAD_RESOURCE_NAME", f"{name} is longer than {cfg['formats']['resource_name_max_length']}")
    return require_format(cfg, "resource_name", name)


def resolve(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def utc_iso(deps: Deps) -> str:
    return deps.utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_sha256(doc: Mapping[str, Any]) -> str:
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seal(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Return `doc` plus SEAL_KEY: the sha256 of the canonical JSON of every other field."""
    body = {key: value for key, value in doc.items() if key != SEAL_KEY}
    return {**body, SEAL_KEY: canonical_sha256(body)}


def is_sealed(doc: Mapping[str, Any]) -> bool:
    return doc.get(SEAL_KEY) == canonical_sha256({key: value for key, value in doc.items() if key != SEAL_KEY})


def write_json(path: Path, doc: Mapping[str, Any], cfg: Mapping[str, Any]) -> None:
    """Atomic write (temp file + os.replace) so readers never see a half-written packet or report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=cfg["evidence"]["json_indent"], sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, prefix: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CicdError(f"{prefix}_MISSING", f"{path} not found") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise CicdError(f"{prefix}_UNREADABLE", f"{path}: {type(exc).__name__}") from exc
    return parse_json(text, str(path), f"{prefix}_MALFORMED")


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    """Append-only ledger line, flushed and fsynced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_packet(cfg: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Read and validate an S5 release packet: schema, formats, repo/tag/digest coherence, packet_sha256."""
    packet = read_json(path, "PACKET")
    if not isinstance(packet, dict):
        raise CicdError("PACKET_MALFORMED", f"{path} is not a JSON object")
    if packet.get("schema") != cfg["release_gate"]["packet_schema"]:
        raise CicdError("PACKET_SCHEMA", f"schema {packet.get('schema')!r} != {cfg['release_gate']['packet_schema']}")
    for kind in cfg["release_gate"]["packet_format_fields"]:
        require_format(cfg, kind, packet.get(kind))
    repo = cfg["ci"]["image_repository"]
    coherent = (packet["commit_sha"].startswith(packet["short_sha"]) and packet.get("project_id") == cfg["project_id"]
                and packet.get("image_ref") == f"{repo}@{packet['image_digest']}"
                and packet.get("image_tag_ref") == f"{repo}:{packet['short_sha']}")
    if not coherent:
        raise CicdError("PACKET_MISMATCH", "packet commit/project/image fields are not coherent with config/cicd.json")
    if not is_sealed(packet):
        raise CicdError("PACKET_TAMPERED", f"{SEAL_KEY} does not match the canonical JSON of the packet")
    return packet


def verify_checkout(deps: Deps, cfg: Mapping[str, Any], source: Path, commit: str) -> None:
    """The source must be a clean git checkout whose HEAD is exactly `commit` (fail closed otherwise)."""
    git_error = ("GIT_ERROR", False)
    status = call(deps.runner, command(cfg, "git_status", source=str(source)), cfg, "git.status", default=git_error)
    if status.stdout.strip():
        raise CicdError("DIRTY_SOURCE", f"{source} has uncommitted or untracked changes")
    head = call(deps.runner, command(cfg, "git_head", source=str(source)), cfg, "git.head", default=git_error).stdout.strip()
    if head != commit:
        raise CicdError("HEAD_MISMATCH", f"{source} HEAD {head or '<none>'} is not {commit}")


def poll(fetch: Callable[[], Any], done: Callable[[Any], bool], interval: float, max_wait: float, deps: Deps,
         what: str) -> Any:
    """Call `fetch` until `done(result)`; past `max_wait` (monotonic clock) raise a transient POLL_DEADLINE."""
    deadline = deps.clock() + max_wait
    while True:
        result = fetch()
        if done(result):
            return result
        if deps.clock() + interval > deadline:
            raise CicdError("POLL_DEADLINE", f"{what} still pending after {max_wait}s", transient=True)
        deps.sleep(interval)


def serving_revision(service: Any, full_percent: int) -> str:
    """The single revision holding `full_percent` of traffic (latestRevision targets resolved)."""
    status = service.get("status") if isinstance(service, dict) else None
    if not isinstance(status, dict):
        raise CicdError("MALFORMED_GCLOUD_JSON", "service description has no status")
    targets = [entry for entry in status.get("traffic") or [] if isinstance(entry, dict) and entry.get("percent")]
    if len(targets) != 1 or targets[0].get("percent") != full_percent:
        shares = [entry.get("percent") for entry in targets]
        raise CicdError("TRAFFIC_SPLIT", f"expected one traffic target at {full_percent}%, found {shares}")
    entry = targets[0]
    name = entry.get("revisionName") or (status.get("latestReadyRevisionName") if entry.get("latestRevision") else None)
    if not name:
        raise CicdError("NO_SERVING_REVISION", "traffic target names no revision")
    return str(name)


def service_urls(service: Mapping[str, Any], annotation: str) -> list[str]:
    """status.url plus the JSON list in the run.googleapis.com/urls annotation, de-duplicated, https only."""
    urls = [str((service.get("status") or {}).get("url") or "")]
    raw = ((service.get("metadata") or {}).get("annotations") or {}).get(annotation)
    if raw:
        extra = parse_json(raw, annotation)
        if not isinstance(extra, list):
            raise CicdError("MALFORMED_GCLOUD_JSON", f"{annotation} is not a JSON list")
        urls += [str(url) for url in extra]
    unique = list(dict.fromkeys(url.rstrip("/") for url in urls if url))
    if not unique or not all(url.startswith("https://") for url in unique):
        raise CicdError("NO_SERVICE_URL", "service exposes no https URL")
    return unique


def first_container(doc: Any) -> dict[str, Any]:
    """spec.containers[0] of a revision, or spec.template.spec.containers[0] of a service."""
    spec = doc.get("spec") if isinstance(doc, dict) else None
    spec = (spec.get("template") or {}).get("spec", spec) if isinstance(spec, dict) and "template" in spec else spec
    containers = spec.get("containers") if isinstance(spec, dict) else None
    if not isinstance(containers, list) or not containers or not isinstance(containers[0], dict):
        raise CicdError("MALFORMED_GCLOUD_JSON", "no container in the description")
    return containers[0]


def container_env(container: Mapping[str, Any]) -> dict[str, Any]:
    return {item.get("name"): item.get("value") for item in container.get("env") or [] if isinstance(item, dict)}


def exit_code_for(cfg: Mapping[str, Any] | None, exc: Exception | None) -> int:
    """ok when exc is None, transient for transient CicdErrors, fail otherwise (fail when config is unknown)."""
    if cfg is None:
        return EXIT_FAIL
    codes = cfg["exit_codes"]
    if exc is None:
        return int(codes["ok"])
    return int(codes["transient"] if getattr(exc, "transient", False) else codes["fail"])


def error_fields(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ConfigError):
        return {"error_taxonomy": exc.taxonomy, "detail": f"{exc.file}: {exc.detail}", "transient": False}
    return {"error_taxonomy": getattr(exc, "taxonomy", type(exc).__name__), "detail": getattr(exc, "detail", str(exc)),
            "transient": bool(getattr(exc, "transient", False))}


def run_cli(body: Callable[[], tuple[int, dict[str, Any]]], stdout: IO[str] | None) -> int:
    """CLI wrapper: bind a trace id, route telemetry to stderr, print one JSON summary line on stdout."""
    started = time.perf_counter()
    trace_id = telemetry.new_trace_id()
    token = telemetry.bind_trace_id(trace_id)
    previous = telemetry.use_stream(sys.stderr)
    try:
        code, summary = body()
    finally:
        telemetry.use_stream(previous)
        telemetry.reset_trace_id(token)
    summary.update(trace_id=trace_id, latency_ms=telemetry.elapsed_ms(started))
    out = stdout if stdout is not None else sys.stdout
    out.write(json.dumps(summary, sort_keys=True, default=str) + "\n")
    out.flush()
    return code
