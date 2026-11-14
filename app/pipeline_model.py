"""Pure mapping for the /api/pipeline document: provenance and Google API payloads -> ci / cd blocks.

No I/O here. Status tones, labels, URL templates, and stage roles come from config/pipeline_status.json
and config/cicd.json. Values come from the environment or from API payloads, otherwise they are null:
nothing is invented. Degraded blocks always carry status UNKNOWN and a typed error.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from app import telemetry
from app.config_store import CONFIG_SCHEMA, ConfigError

UNKNOWN = "UNKNOWN"
SOURCE_LIVE, SOURCE_UNAVAILABLE, SOURCE_NOT_CONFIGURED = "live", "unavailable", "not_configured"
NO_CREDENTIALS, NOT_FOUND, UNAVAILABLE = "NO_CREDENTIALS", "NOT_FOUND", "UNAVAILABLE"
TIMEOUT, MALFORMED_RESPONSE = "TIMEOUT", "MALFORMED_RESPONSE"
PIPELINE_FILE = "config/pipeline_status.json"
CD_PROVENANCE_KEYS = ("release", "pipeline", "target")
STATE_KEY = {"ci": "status", "cd": "rollout_state"}
_ONE_MS = timedelta(milliseconds=1)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class PipelineError(Exception):
    """Typed failure (taxonomy, detail, optional HTTP status); it becomes a degraded block's `error`."""

    def __init__(self, taxonomy: str, detail: str, http_status: int | None = None) -> None:
        super().__init__(f"{taxonomy}: {detail}")
        self.taxonomy = taxonomy
        self.detail = detail
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        return {"taxonomy": self.taxonomy, "http_status": self.http_status, "detail": self.detail}


def seconds(ms: int) -> float:
    """Milliseconds from config -> float seconds (no inline conversion constants)."""
    return timedelta(milliseconds=ms).total_seconds()


def clean_str(value: Any) -> str | None:
    """Stripped string, or None for blanks and non-strings."""
    return (value.strip() or None) if isinstance(value, str) else None


def parse_time(value: Any) -> datetime | None:
    """RFC 3339 timestamp (Google APIs use a Z suffix and up to nanoseconds) -> aware datetime, else None."""
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc) if parsed else None


def iso_ms(value: Any) -> str | None:
    """Normalize a timestamp to UTC ISO-8601 with millisecond precision and a Z suffix."""
    parsed = parse_time(value)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if parsed else None


def duration_ms(start: Any, end: Any) -> int | None:
    """Whole milliseconds between two timestamps; None when either is missing or the interval is negative."""
    begin, finish = parse_time(start), parse_time(end)
    return round((finish - begin) / _ONE_MS) if begin and finish and finish >= begin else None


def fmt(template: str, **values: Any) -> str:
    """Fill a config template; a template naming an unknown field is a configuration error (503)."""
    try:
        return template.format_map(values)
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(CONFIG_SCHEMA, PIPELINE_FILE, f"bad template field in {template!r}: {exc}") from exc


def url(template: str, **values: Any) -> str:
    """Fill a URL template with every value percent-encoded as one path or query component."""
    return fmt(template, **{key: urllib.parse.quote(str(value), safe="") for key, value in values.items()})


def stage_for(stage_ids: Mapping[str, Any], role: str) -> str:
    if role not in stage_ids:
        raise ConfigError(CONFIG_SCHEMA, PIPELINE_FILE, f"stage role {role!r} is not in config/cicd.json stage_ids")
    return str(stage_ids[role])


def control_loop(items: list[dict[str, Any]], stage_ids: Mapping[str, Any]) -> list[str]:
    """cd.control_loop items are either a fixed label or a stage role resolved through config/cicd.json."""
    steps = []
    for item in items:
        if ("label" in item) == ("stage_role" in item):
            raise ConfigError(CONFIG_SCHEMA, PIPELINE_FILE, "each cd.control_loop item needs exactly one of label, stage_role")
        steps.append(str(item["label"]) if "label" in item else stage_for(stage_ids, item["stage_role"]))
    return steps


def read_provenance(cicd: Mapping[str, Any], env: Mapping[str, str], unset_values: Iterable[str] = ()) -> dict[str, Any]:
    """Provenance from the env vars named in config/cicd.json `provenance_env`; absent, blank, or sentinel values
    (config `provenance_unset_values`, e.g. a manifest placeholder) are null.

    deployed_by_cloud_deploy is true only when the Cloud Deploy parameters (release, pipeline, target) are all set.
    """
    unset = {value.lower() for value in unset_values}
    values: dict[str, Any] = {}
    for key, var in cicd["provenance_env"].items():
        value = clean_str(env.get(var))
        values[key] = None if value is None or value.lower() in unset else value
    values["deployed_by_cloud_deploy"] = all(values.get(key) for key in CD_PROVENANCE_KEYS)
    return values


def _image_for(build: Mapping[str, Any], repository: str) -> tuple[str | None, str | None]:
    results = build.get("results") if isinstance(build.get("results"), dict) else {}
    images = results.get("images") if isinstance(results.get("images"), list) else []
    prefix = f"{repository}:"
    for image in images:
        name = clean_str(image.get("name")) if isinstance(image, dict) else None
        if name and (name == repository or name.startswith(prefix)):
            tag = (name[len(prefix):] or None) if name.startswith(prefix) else None
            return tag, clean_str(image.get("digest"))
    return None, None


def _step(index: int, step: Mapping[str, Any], tones: Mapping[str, str], default_tone: str) -> dict[str, Any]:
    status = clean_str(step.get("status")) or UNKNOWN
    timing = step.get("timing") if isinstance(step.get("timing"), dict) else {}
    return {
        "id": clean_str(step.get("id")) or clean_str(step.get("name")) or f"step-{index}",
        "status": status,
        "tone": tones.get(status, default_tone),
        "duration_ms": duration_ms(timing.get("startTime"), timing.get("endTime")),
    }


def map_build(build: Any, cfg: Mapping[str, Any], cicd: Mapping[str, Any], provenance: Mapping[str, Any]) -> dict[str, Any]:
    """Cloud Build v1 Build resource -> the live fields of the ci block."""
    status = clean_str(build.get("status")) if isinstance(build, dict) else None
    if status is None:
        raise PipelineError(MALFORMED_RESPONSE, "Cloud Build response has no build status")
    tones, default_tone = cfg["ci"]["status_tones"], cfg["default_tone"]
    tag, digest = _image_for(build, cicd["ci"]["image_repository"])
    raw_steps = build.get("steps") if isinstance(build.get("steps"), list) else []
    expected = provenance.get("image_digest")
    return {
        "status": status,
        "tone": tones.get(status, default_tone),
        "build_id": clean_str(build.get("id")) or provenance.get("build_id"),
        "image_tag": tag,
        "image_digest": digest,
        "create_time": iso_ms(build.get("createTime")),
        "finish_time": iso_ms(build.get("finishTime")),
        "duration_ms": duration_ms(build.get("startTime"), build.get("finishTime")),
        "steps": [_step(index, step, tones, default_tone) for index, step in enumerate(raw_steps, start=1) if isinstance(step, dict)],
        "status_detail": clean_str(build.get("statusDetail")),
        "digest_matches_provenance": (digest == expected) if digest and expected else None,
    }


def map_rollouts(payload: Any, cfg: Mapping[str, Any], cicd: Mapping[str, Any], release: Any) -> dict[str, Any]:
    """Cloud Deploy ListRollouts response -> the newest rollout (by createTime) on the configured target."""
    rollouts = payload.get("rollouts", []) if isinstance(payload, dict) else None
    if not isinstance(rollouts, list):
        raise PipelineError(MALFORMED_RESPONSE, "Cloud Deploy response has no rollouts list")
    target = cicd["cd"]["target"]
    mine = [item for item in rollouts if isinstance(item, dict) and item.get("targetId") == target]
    if not mine:
        raise PipelineError(NOT_FOUND, f"release {release} has no rollout for target {target}")
    newest = max(mine, key=lambda item: parse_time(item.get("createTime")) or _EPOCH)
    state = clean_str(newest.get("state"))
    if state is None:
        raise PipelineError(MALFORMED_RESPONSE, "newest rollout has no state")
    return {
        "rollout": (clean_str(newest.get("name")) or "").rsplit("/", 1)[-1] or None,
        "rollout_state": state,
        "tone": cfg["cd"]["status_tones"].get(state, cfg["default_tone"]),
        "deploy_start_time": iso_ms(newest.get("deployStartTime")),
        "deploy_end_time": iso_ms(newest.get("deployEndTime")),
        "failure_reason": clean_str(newest.get("failureReason")),
    }


def ci_base(cfg: Mapping[str, Any], cicd: Mapping[str, Any], provenance: Mapping[str, Any]) -> dict[str, Any]:
    """The ci block before any live data: status UNKNOWN, source not_configured, honest label."""
    stage_ids, build_id = cicd["stage_ids"], provenance.get("build_id")
    missing = fmt(cfg["labels"]["missing_env"], env_var=cicd["provenance_env"]["build_id"])
    console = url(cfg["ci"]["console_url_template"], build_id=build_id, project=cicd["project_id"], region=cicd["region"])
    return {
        "provider": cicd["ci"]["provider"],
        "stage": stage_for(stage_ids, "release_gate"),
        "status": UNKNOWN,
        "tone": cfg["ci"]["status_tones"].get(UNKNOWN, cfg["default_tone"]),
        "build_id": build_id,
        "image_tag": None,
        "image_digest": None,
        "create_time": None,
        "finish_time": None,
        "duration_ms": None,
        "steps": [],
        "status_detail": None,
        "digest_matches_provenance": None,
        "console_url": console if build_id else None,
        "source": SOURCE_NOT_CONFIGURED,
        "source_label": missing if provenance["deployed_by_cloud_deploy"] else fmt(cfg["labels"]["not_deployed"]),
        "error": None,
        "latency_ms": None,
        "bounce_to": [stage_for(stage_ids, role) for role in cfg["ci"]["bounce_to_roles"]],
    }


def cd_base(cfg: Mapping[str, Any], cicd: Mapping[str, Any], provenance: Mapping[str, Any]) -> dict[str, Any]:
    """The cd block before any live data: rollout_state UNKNOWN, source not_configured, honest label."""
    pipeline, release = cicd["cd"]["delivery_pipeline"], provenance.get("release")
    console = url(cfg["cd"]["console_url_template"], pipeline=pipeline, release=release, project=cicd["project_id"],
                  region=cicd["region"])
    return {
        "provider": cicd["cd"]["provider"],
        "stage": stage_for(cicd["stage_ids"], "deploy_close"),
        "pipeline": pipeline,
        "target": cicd["cd"]["target"],
        "release": release,
        "rollout": None,
        "rollout_state": UNKNOWN,
        "tone": cfg["cd"]["status_tones"].get(UNKNOWN, cfg["default_tone"]),
        "deploy_start_time": None,
        "deploy_end_time": None,
        "failure_reason": None,
        "console_url": console if provenance["deployed_by_cloud_deploy"] else None,
        "source": SOURCE_NOT_CONFIGURED,
        "source_label": fmt(cfg["labels"]["not_deployed"]),
        "error": None,
        "latency_ms": None,
        "control_loop": control_loop(cfg["cd"]["control_loop"], cicd["stage_ids"]),
    }


def degraded(cfg: Mapping[str, Any], name: str, base: dict[str, Any], error: PipelineError, started: float) -> dict[str, Any]:
    """A block whose live read failed: status UNKNOWN, source unavailable, typed error, measured latency."""
    return {
        **base,
        STATE_KEY[name]: UNKNOWN,
        "tone": cfg[name]["status_tones"].get(UNKNOWN, cfg["default_tone"]),
        "source": SOURCE_UNAVAILABLE,
        "source_label": fmt(cfg["labels"]["unavailable"], taxonomy=error.taxonomy),
        "error": error.to_dict(),
        "latency_ms": telemetry.elapsed_ms(started),
    }


def live(cfg: Mapping[str, Any], name: str, base: dict[str, Any], fields: dict[str, Any], started: float) -> dict[str, Any]:
    """A block filled from a successful API read."""
    return {**base, **fields, "source": SOURCE_LIVE, "source_label": cfg[name]["live_label"], "error": None,
            "latency_ms": telemetry.elapsed_ms(started)}
