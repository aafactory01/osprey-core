"""Shared fakes for the pipeline status tests: a routing fake transport, a manual clock, and payload builders.

No network: every outbound call is answered by FakeTransport from in-memory routes. Expected values are
derived from config/cicd.json and config/pipeline_status.json so stage ids are never hardcoded here.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from app.config_store import ConfigStore
from app.pipeline_status import PipelineStatusService
from tests.helpers import load_json

CICD = load_json("config/cicd.json")
CFG = load_json("config/pipeline_status.json")
STAGES = CICD["stage_ids"]
ENV_NAMES = CICD["provenance_env"]
REPO = CICD["ci"]["image_repository"]
PIPELINE = CICD["cd"]["delivery_pipeline"]
TARGET = CICD["cd"]["target"]
TOKEN_URL_PART, BUILD_URL_PART, ROLLOUTS_URL_PART = "computeMetadata", "cloudbuild.googleapis.com", "clouddeploy.googleapis.com"
FAKE_TOKEN = "fake-access-token-for-tests"
DIGEST = "sha256:" + "ab" * 32
BUILD_ID = "6f1d2c3b-aaaa-4bbb-8ccc-1234567890ab"
COMMIT = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
SHORT_SHA = COMMIT[:7]
RELEASE = "rel-0a1b2c3"
REVISION = "osprey-core-00042-xyz"


def provenance_values(**overrides: Any) -> dict[str, Any]:
    values = {"commit_sha": COMMIT, "short_sha": SHORT_SHA, "build_id": BUILD_ID, "image_digest": DIGEST, "release": RELEASE,
              "pipeline": PIPELINE, "target": TARGET, "revision": REVISION, "service": CICD["service"]}
    values.update(overrides)
    return values


def provenance_env(**overrides: Any) -> dict[str, str]:
    """Environment as Cloud Deploy + Cloud Run would set it; pass key=None to leave a variable unset."""
    return {ENV_NAMES[key]: value for key, value in provenance_values(**overrides).items() if value is not None}


def ok(doc: Any, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(doc).encode("utf-8")


def google_error(status: int, message: str) -> tuple[int, bytes]:
    return ok({"error": {"code": status, "message": message, "status": "ERROR"}}, status)


TOKEN_OK = ok({"access_token": FAKE_TOKEN, "expires_in": 3599, "token_type": "Bearer"})


def build_doc(status: str = "SUCCESS", **extra: Any) -> dict[str, Any]:
    doc = {
        "id": BUILD_ID, "status": status, "createTime": "2026-11-14T20:00:00.123456789Z",
        "startTime": "2026-11-14T20:00:05Z", "finishTime": "2026-11-14T20:03:17.5Z", "statusDetail": "",
        "steps": [
            {"id": "pytest", "status": "SUCCESS",
             "timing": {"startTime": "2026-11-14T20:00:05Z", "endTime": "2026-11-14T20:01:05Z"}},
            {"name": "gcr.io/cloud-builders/docker", "status": "SUCCESS",
             "timing": {"startTime": "2026-11-14T20:01:05Z", "endTime": "2026-11-14T20:03:05Z"}},
            {"status": "QUEUED"},
        ],
        "results": {"images": [{"name": f"{REPO}:{SHORT_SHA}", "digest": DIGEST}]},
    }
    doc.update(extra)
    return doc


def rollout(rollout_id: str, created: str, target: str = TARGET, state: str = "SUCCEEDED", **extra: Any) -> dict[str, Any]:
    name = f"projects/p/locations/r/deliveryPipelines/{PIPELINE}/releases/{RELEASE}/rollouts/{rollout_id}"
    return {"name": name, "targetId": target, "state": state, "createTime": created,
            "deployStartTime": "2026-11-14T20:05:00Z", "deployEndTime": "2026-11-14T20:06:30.25Z", **extra}


def rollouts_doc() -> dict[str, Any]:
    """Several rollouts: the newest one for TARGET is `rollout-new`; a newer one targets another target."""
    return {"rollouts": [
        rollout("rollout-old", "2026-11-14T19:00:00Z", state="FAILED"),
        rollout("rollout-new", "2026-11-14T20:04:00.5Z"),
        rollout("rollout-other", "2026-11-14T21:00:00Z", target="some-other-target", state="IN_PROGRESS"),
        rollout("rollout-undated", "not-a-time", state="FAILED"),
    ]}


class FakeClock:
    """Manual monotonic clock in seconds."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTransport:
    """Answers by URL substring: a (status, body) tuple, a list of them (served in order), an exception, or a callable."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = dict(routes)
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, headers: Any, timeout_s: float, max_bytes: int) -> tuple[int, bytes]:
        with self._lock:
            self.calls.append({"url": url, "headers": dict(headers), "timeout_s": timeout_s, "max_bytes": max_bytes})
            for part, action in self.routes.items():
                if part in url:
                    if isinstance(action, list):
                        action = action.pop(0) if len(action) > 1 else action[0]
                    break
            else:
                raise AssertionError(f"unexpected URL {url}")
        if isinstance(action, BaseException):
            raise action
        return action(url) if callable(action) else action

    def count(self, part: str) -> int:
        return sum(part in call["url"] for call in self.calls)


def default_routes(**overrides: Any) -> dict[str, Any]:
    routes = {TOKEN_URL_PART: TOKEN_OK, BUILD_URL_PART: ok(build_doc()), ROLLOUTS_URL_PART: ok(rollouts_doc())}
    routes.update({{"token": TOKEN_URL_PART, "build": BUILD_URL_PART, "rollouts": ROLLOUTS_URL_PART}[k]: v
                   for k, v in overrides.items()})
    return routes


def make_service(routes: dict[str, Any] | None = None, env: dict[str, str] | None = None, clock: Callable[[], float] | None = None,
                 store: ConfigStore | None = None) -> tuple[PipelineStatusService, FakeTransport, Any]:
    transport = FakeTransport(default_routes() if routes is None else routes)
    fake_clock = clock or FakeClock()
    service = PipelineStatusService(store or ConfigStore(), transport=transport, clock=fake_clock,
                                    env=provenance_env() if env is None else env)
    return service, transport, fake_clock
