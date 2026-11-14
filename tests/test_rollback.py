"""S6 rollback: revision math, dry-run vs execute, and fail-closed chaos (401/403/429/503, timeouts, bad JSON)."""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from scripts import s6_rollback as rollback
from tests.helpers import edit_json

READY = {"type": "Ready", "status": "True"}
NOT_READY = {"type": "Ready", "status": "False"}


def revision(name, created, ready=True):
    return {"metadata": {"name": name, "creationTimestamp": created}, "status": {"conditions": [READY if ready else NOT_READY]}}


REVISIONS = [
    revision("osprey-core-00004", "2026-11-14T18:00:00Z", ready=False),
    revision("osprey-core-00003", "2026-11-14T16:20:00Z"),
    revision("osprey-core-00002", "2026-11-13T18:05:00Z"),
    revision("osprey-core-00001", "2026-11-11T17:42:00Z"),
]
SERVICE = {"status": {"traffic": [{"revisionName": "osprey-core-00003", "percent": 100}],
                      "latestReadyRevisionName": "osprey-core-00003"}}


class FakeRunner:
    """Injectable gcloud: scripted results per subcommand, records every call."""

    def __init__(self, service=SERVICE, revisions=REVISIONS, fail=None, raise_on=None):
        self.calls = []
        self.service, self.revisions, self.fail, self.raise_on = service, revisions, fail or {}, raise_on or {}

    def __call__(self, cmd, timeout):
        kind = "describe" if "describe" in cmd else "list" if "list" in cmd else "update"
        self.calls.append((kind, cmd, timeout))
        if kind in self.raise_on:
            raise self.raise_on[kind]
        if kind in self.fail:
            return rollback.RunResult(1, "", self.fail[kind])
        body = {"describe": self.service, "list": self.revisions, "update": {"ok": True}}[kind]
        return rollback.RunResult(0, body if isinstance(body, str) else json.dumps(body), "")

    def kinds(self):
        return [kind for kind, _, _ in self.calls]


def run(args, runner, env=None):
    out = io.StringIO()
    code = rollback.main(args, runner=runner, environ=env if env is not None else {"GOOGLE_CLOUD_PROJECT": "demo-project"},
                         stdout=out)
    return code, json.loads(out.getvalue())


def test_dry_run_plans_without_touching_traffic():
    runner = FakeRunner()
    code, summary = run([], runner)
    assert code == 0 and summary["status"] == "ok" and summary["mode"] == "dry-run" and summary["executed"] is False
    assert summary["current_revision"] == "osprey-core-00003" and summary["previous_revision"] == "osprey-core-00002"
    assert "--to-revisions=osprey-core-00002=100" in summary["command"] and "--quiet" in summary["command"]
    assert runner.kinds() == ["describe", "list"]
    assert isinstance(summary["latency_ms"], int)


def test_execute_shifts_traffic_with_planned_command():
    runner = FakeRunner()
    code, summary = run(["--execute", "--project", "explicit-project"], runner)
    assert code == 0 and summary["executed"] is True and summary["project"] == "explicit-project"
    assert runner.kinds() == ["describe", "list", "update"]
    assert runner.calls[-1][1] == summary["command"]
    assert runner.calls[-1][2] == 60


def test_latest_revision_traffic_target_is_resolved():
    service = {"status": {"traffic": [{"latestRevision": True, "percent": 100}], "latestReadyRevisionName": "osprey-core-00002"}}
    code, summary = run([], FakeRunner(service=service))
    assert code == 0 and summary["current_revision"] == "osprey-core-00002" and summary["previous_revision"] == "osprey-core-00001"


def test_split_traffic_uses_the_majority_revision():
    service = {"status": {"traffic": [{"revisionName": "osprey-core-00002", "percent": 85},
                                      {"revisionName": "osprey-core-00003", "percent": 15}]}}
    assert run([], FakeRunner(service=service))[1]["previous_revision"] == "osprey-core-00001"


def test_not_ready_revisions_are_skipped():
    revisions = [REVISIONS[1], revision("osprey-core-00002", "2026-11-13T18:05:00Z", ready=False), REVISIONS[3]]
    assert run([], FakeRunner(revisions=revisions))[1]["previous_revision"] == "osprey-core-00001"


@pytest.mark.parametrize("service,revisions,taxonomy", [
    (SERVICE, [REVISIONS[1]], "NO_PREVIOUS_REVISION"),
    ({"status": {"traffic": []}}, REVISIONS, "NO_SERVING_REVISION"),
    ({"status": {"traffic": [{"revisionName": "ghost", "percent": 100}]}}, REVISIONS, "UNKNOWN_SERVING_REVISION"),
    (SERVICE, {"not": "a list"}, "MALFORMED_GCLOUD_JSON"),
    (SERVICE, [revision("osprey-core-00003", "yesterday"), REVISIONS[2]], "MALFORMED_GCLOUD_JSON"),
    (SERVICE, "this is not json", "MALFORMED_GCLOUD_JSON"),
])
def test_ambiguous_history_fails_closed(service, revisions, taxonomy):
    runner = FakeRunner(service=service, revisions=revisions)
    code, summary = run(["--execute"], runner)
    assert code == 1 and summary["error_taxonomy"] == taxonomy and summary["executed"] is False
    assert "update" not in runner.kinds()


def test_missing_project_fails_before_any_gcloud_call():
    runner = FakeRunner()
    code, summary = run([], runner, env={})
    assert code == 1 and summary["error_taxonomy"] == "MISSING_PROJECT" and runner.calls == []


@pytest.mark.parametrize("stderr,taxonomy", [
    ("ERROR: (gcloud.run.services.describe) HTTPError 401: Request had invalid authentication credentials.", "UNAUTHENTICATED"),
    ("ERROR: (gcloud.run.services.describe) You do not currently have an active account selected.", "UNAUTHENTICATED"),
    ("ERROR: (gcloud.run.services.describe) PERMISSION_DENIED: Permission denied on resource (or it may not exist). 403",
     "PERMISSION_DENIED"),
    ("ERROR: (gcloud.run.services.describe) HTTPError 429: Too Many Requests", "RATE_LIMITED"),
    ("ERROR: RESOURCE_EXHAUSTED: Quota exceeded for quota metric", "RATE_LIMITED"),
    ("ERROR: (gcloud.run.services.describe) HTTPError 503: Service Unavailable", "UNAVAILABLE"),
    ("ERROR: (gcloud.run.services.describe) Something odd happened", "GCLOUD_ERROR"),
])
def test_gcloud_http_failures_fail_closed(stderr, taxonomy):
    runner = FakeRunner(fail={"describe": stderr})
    code, summary = run(["--execute"], runner)
    assert code == 1 and summary["error_taxonomy"] == taxonomy and summary["executed"] is False
    assert runner.kinds() == ["describe"] and summary["detail"]


def test_failure_during_listing_fails_closed():
    runner = FakeRunner(fail={"list": "ERROR: HTTPError 503: Service Unavailable"})
    code, summary = run(["--execute"], runner)
    assert code == 1 and summary["error_taxonomy"] == "UNAVAILABLE" and "update" not in runner.kinds()


def test_failure_of_the_traffic_shift_is_reported():
    runner = FakeRunner(fail={"update": "ERROR: HTTPError 429: Too Many Requests"})
    code, summary = run(["--execute"], runner)
    assert code == 1 and summary["error_taxonomy"] == "RATE_LIMITED" and summary["executed"] is False


def test_timeout_fails_closed():
    runner = FakeRunner(raise_on={"describe": subprocess.TimeoutExpired(["gcloud"], 60)})
    code, summary = run([], runner)
    assert code == 1 and summary["error_taxonomy"] == "TIMEOUT"


def test_os_error_from_runner_fails_closed():
    runner = FakeRunner(raise_on={"list": PermissionError(13, "Permission denied")})
    code, summary = run([], runner)
    assert code == 1 and summary["error_taxonomy"] == "GCLOUD_ERROR"


def test_real_subprocess_runner_enforces_timeout():
    cfg = {"command_timeout_seconds": 0.2, "error_taxonomy": [], "default_error_taxonomy": "GCLOUD_ERROR"}
    with pytest.raises(rollback.RollbackError) as info:
        rollback.run_command(rollback.subprocess_runner, [sys.executable, "-c", "import time; time.sleep(5)"], cfg)
    assert info.value.taxonomy == "TIMEOUT"


def test_real_subprocess_runner_missing_binary(temp_root):
    edit_json(temp_root / "config" / "rollback.json", lambda doc: doc.update(gcloud_bin="/nonexistent/gcloud-xyz"))
    out = io.StringIO()
    code = rollback.main(["--config-root", str(temp_root)], environ={"GOOGLE_CLOUD_PROJECT": "p"}, stdout=out)
    assert code == 1 and json.loads(out.getvalue())["error_taxonomy"] == "GCLOUD_NOT_FOUND"


def test_missing_config_fails_closed(temp_root):
    (temp_root / "config" / "rollback.json").unlink()
    out = io.StringIO()
    code = rollback.main(["--config-root", str(temp_root)], runner=FakeRunner(), environ={"GOOGLE_CLOUD_PROJECT": "p"}, stdout=out)
    assert code == 1 and json.loads(out.getvalue())["error_taxonomy"] == "CONFIG_MISSING"


def test_bad_command_template_fails_closed(temp_root):
    edit_json(temp_root / "config" / "rollback.json", lambda doc: doc["commands"]["describe_service"].append("{nope}"))
    out = io.StringIO()
    code = rollback.main(["--config-root", str(temp_root)], runner=FakeRunner(), environ={"GOOGLE_CLOUD_PROJECT": "p"}, stdout=out)
    assert code == 1 and json.loads(out.getvalue())["error_taxonomy"] == "CONFIG_SCHEMA"
