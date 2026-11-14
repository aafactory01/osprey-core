"""S6 rollback: revision math, dry-run vs execute, and fail-closed chaos (401/403/429/503, timeouts, bad JSON)."""

from __future__ import annotations

import io
import json
import re
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
    """Injectable gcloud: scripted results per subcommand, records every call.

    Cloud Deploy calls (`gcloud deploy ...`, used by the auto/deploy modes) are answered from `releases` /
    `rollouts` (default: no Cloud Deploy history, so auto mode falls back to traffic mode) and are recorded as
    kind "cd"; `kinds()` keeps reporting only the Cloud Run calls the traffic-mode tests assert on.
    """

    def __init__(self, service=SERVICE, revisions=REVISIONS, fail=None, raise_on=None, releases=None, rollouts=None,
                 rollout_states=None):
        self.calls = []
        self.service, self.revisions, self.fail, self.raise_on = service, revisions, fail or {}, raise_on or {}
        self.releases, self.rollouts = releases if releases is not None else [], rollouts or {}
        self.rollout_states = list(rollout_states or ["SUCCEEDED"])

    def _cd(self, cmd):
        verb = cmd[2:4]
        if verb == ["releases", "list"]:
            return self.releases
        if verb == ["rollouts", "list"]:
            release = next(part.split("=", 1)[1] for part in cmd if part.startswith("--release="))
            return self.rollouts.get(release, [])
        if verb == ["rollouts", "describe"]:
            state = self.rollout_states.pop(0) if len(self.rollout_states) > 1 else self.rollout_states[0]
            return {"name": cmd[4], "state": state}
        return {"name": "rollback"}

    def __call__(self, cmd, timeout):
        if "deploy" in cmd:
            key = "cd_" + "_".join(cmd[2:4])
            self.calls.append(("cd", cmd, timeout))
            if key in self.raise_on:
                raise self.raise_on[key]
            if key in self.fail:
                return rollback.RunResult(1, "", self.fail[key])
            body = self._cd(cmd)
            return rollback.RunResult(0, body if isinstance(body, str) else json.dumps(body), "")
        kind = "describe" if "describe" in cmd else "list" if "list" in cmd else "update"
        self.calls.append((kind, cmd, timeout))
        if kind in self.raise_on:
            raise self.raise_on[kind]
        if kind in self.fail:
            return rollback.RunResult(1, "", self.fail[kind])
        body = {"describe": self.service, "list": self.revisions, "update": {"ok": True}}[kind]
        return rollback.RunResult(0, body if isinstance(body, str) else json.dumps(body), "")

    def kinds(self):
        return [kind for kind, _, _ in self.calls if kind != "cd"]

    def cd_calls(self):
        return [cmd[2:4] for kind, cmd, _ in self.calls if kind == "cd"]


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


# --- Cloud Deploy mode (auto / deploy) and the workflow_ctl guards -------------------------------------------------

def cd_release(name, created, abandoned=False):
    doc = {"name": f"projects/gement02/locations/us-central1/deliveryPipelines/osprey-core-cd/releases/{name}",
           "createTime": created}
    return {**doc, "abandoned": True} if abandoned else doc


def cd_rollout(state, end, target="osprey-core-prod"):
    return {"targetId": target, "state": state, "deployEndTime": end, "createTime": end}


REL_A, REL_B, REL_C = "rel-aaaaaaa-20261113-100000", "rel-bbbbbbb-20261114-200000", "rel-ccccccc-20261114-230000"
RELEASES = [cd_release(REL_C, "2026-11-14T23:00:00Z"), cd_release(REL_B, "2026-11-14T20:00:00Z"),
            cd_release(REL_A, "2026-11-13T10:00:00Z")]
ROLLOUTS = {REL_C: [cd_rollout("SUCCEEDED", "2026-11-14T23:05:00Z")],
            REL_B: [cd_rollout("SUCCEEDED", "2026-11-14T20:05:00.123456789Z")],
            REL_A: [cd_rollout("SUCCEEDED", "2026-11-13T10:05:00Z")]}


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def run_cd(args, runner, clock=None):
    out, clock = io.StringIO(), clock or FakeClock()
    code = rollback.main(args, runner=runner, environ={"GOOGLE_CLOUD_PROJECT": "demo-project"}, stdout=out,
                         sleep=clock.sleep, clock=lambda: clock.now)
    return code, json.loads(out.getvalue())


def test_auto_mode_plans_a_cloud_deploy_rollback_to_the_previous_release():
    runner = FakeRunner(releases=RELEASES, rollouts=ROLLOUTS)
    code, summary = run_cd([], runner)
    assert code == 0 and summary["rollback_mode"] == "deploy" and summary["executed"] is False
    assert summary["current_release"] == REL_C and summary["previous_release"] == REL_B
    command = summary["command"]
    assert command[:5] == ["gcloud", "deploy", "targets", "rollback", "osprey-core-prod"]
    assert "--delivery-pipeline=osprey-core-cd" in command and f"--release={REL_B}" in command and "--quiet" in command
    assert "--region=us-central1" in command and "--project=demo-project" in command
    assert re.fullmatch(r"rollback-\d{8}-\d{6}", summary["rollout"]) and f"--rollout-id={summary['rollout']}" in command
    assert runner.kinds() == [] and runner.cd_calls() == [["releases", "list"]] + [["rollouts", "list"]] * 3
    assert "--limit=10" in runner.calls[0][1] and "--sort-by=~createTime" in runner.calls[0][1]


def test_execute_runs_the_rollback_and_polls_its_rollout_to_success():
    runner, clock = FakeRunner(releases=RELEASES, rollouts=ROLLOUTS, rollout_states=["IN_PROGRESS", "SUCCEEDED"]), FakeClock()
    code, summary = run_cd(["--execute"], runner, clock)
    assert code == 0 and summary["executed"] is True and summary["rollout_state"] == "SUCCEEDED"
    assert runner.cd_calls()[-3:] == [["targets", "rollback"], ["rollouts", "describe"], ["rollouts", "describe"]]
    rollback_call = next(call for call in runner.calls if call[1][2:4] == ["targets", "rollback"])
    assert rollback_call[2] == 900 and clock.sleeps == [15] and runner.kinds() == []


def test_rollback_redeploys_count_as_the_live_release():
    rollouts = {REL_C: [cd_rollout("SUCCEEDED", "2026-11-14T23:05:00Z")],
                REL_B: [cd_rollout("SUCCEEDED", "2026-11-14T20:05:00Z")],
                REL_A: [cd_rollout("SUCCEEDED", "2026-11-13T10:05:00Z"), cd_rollout("SUCCEEDED", "2026-11-14T21:00:00Z")]}
    code, summary = run_cd([], FakeRunner(releases=RELEASES, rollouts=rollouts))
    assert code == 0 and summary["current_release"] == REL_C and summary["previous_release"] == REL_A


def test_abandoned_failed_and_other_target_releases_are_never_targets():
    releases = [cd_release(REL_C, "c"), cd_release(REL_B, "b", abandoned=True), cd_release(REL_A, "a"),
                cd_release("rel-0000000-20261101-000000", "z")]
    rollouts = {REL_C: [cd_rollout("SUCCEEDED", "2026-11-14T23:05:00Z")],
                REL_A: [cd_rollout("FAILED", "2026-11-13T10:05:00Z"),
                        cd_rollout("SUCCEEDED", "2026-11-13T11:00:00Z", target="osprey-core-staging")],
                "rel-0000000-20261101-000000": [cd_rollout("SUCCEEDED", "2026-11-01T00:05:00Z")]}
    code, summary = run_cd([], FakeRunner(releases=releases, rollouts=rollouts))
    assert code == 0 and summary["previous_release"] == "rel-0000000-20261101-000000"


@pytest.mark.parametrize("releases,rollouts", [([], {}), ([RELEASES[0]], {REL_C: ROLLOUTS[REL_C]}),
                                               ([RELEASES[0]], {REL_C: [cd_rollout("FAILED", "2026-11-14T23:05:00Z")]})])
def test_auto_falls_back_to_traffic_without_a_previous_release(releases, rollouts):
    runner = FakeRunner(releases=releases, rollouts=rollouts)
    code, summary = run_cd(["--execute"], runner)
    assert code == 0 and summary["rollback_mode"] == "traffic" and summary["selected_by"]
    assert runner.kinds() == ["describe", "list", "update"] and summary["executed"] is True


def test_forced_deploy_mode_without_history_fails_closed():
    runner = FakeRunner(releases=[RELEASES[0]], rollouts={REL_C: ROLLOUTS[REL_C]})
    code, summary = run_cd(["--mode", "deploy", "--execute"], runner)
    assert code == 1 and summary["error_taxonomy"] == "NO_PREVIOUS_RELEASE" and summary["executed"] is False
    assert ["targets", "rollback"] not in runner.cd_calls() and runner.kinds() == []


def test_bad_release_guard_skips_when_the_target_already_left_it():
    runner = FakeRunner(releases=RELEASES, rollouts=ROLLOUTS)
    code, summary = run_cd(["--execute", "--bad-release", "rel-ddddddd-20261115-000000"], runner)
    assert code == 0 and summary["executed"] is False and REL_C in summary["skipped"]
    assert ["targets", "rollback"] not in runner.cd_calls() and runner.kinds() == []


def test_bad_release_guard_rolls_back_the_rejected_release():
    code, summary = run_cd(["--execute", "--bad-release", REL_C], FakeRunner(releases=RELEASES, rollouts=ROLLOUTS))
    assert code == 0 and summary["executed"] is True and summary["previous_release"] == REL_B


IMAGED = [{**rev, "spec": {"containers": [{"image": f"repo@sha256:{name[-1] * 64}"}]}}
          for rev, name in ((rev, rev["metadata"]["name"]) for rev in REVISIONS)]


@pytest.mark.parametrize("bad_image,executed", [("repo@sha256:" + "3" * 64, True), ("repo@sha256:" + "9" * 64, False)])
def test_traffic_bad_image_guard(bad_image, executed):
    runner = FakeRunner(revisions=IMAGED)
    code, summary = run_cd(["--mode", "traffic", "--execute", "--bad-image", bad_image], runner)
    assert code == 0 and summary["executed"] is executed and ("update" in runner.kinds()) is executed
    assert ("skipped" in summary) is not executed and runner.cd_calls() == []


@pytest.mark.parametrize("states,taxonomy", [(["FAILED"], "ROLLBACK_ROLLOUT_FAILED"), (["IN_PROGRESS"], "POLL_DEADLINE")])
def test_rollback_rollout_that_does_not_succeed_fails_closed(states, taxonomy):
    runner = FakeRunner(releases=RELEASES, rollouts=ROLLOUTS, rollout_states=states)
    code, summary = run_cd(["--execute"], runner)
    assert code == 1 and summary["error_taxonomy"] == taxonomy and summary["executed"] is False


@pytest.mark.parametrize("fail,taxonomy", [
    ({"cd_releases_list": "ERROR: (gcloud.deploy.releases.list) PERMISSION_DENIED: 403"}, "PERMISSION_DENIED"),
    ({"cd_rollouts_list": "ERROR: HTTPError 503: Service Unavailable"}, "UNAVAILABLE"),
    ({"cd_targets_rollback": "ERROR: HTTPError 429: Too Many Requests"}, "RATE_LIMITED"),
    ({"cd_targets_rollback": "ERROR: FAILED_PRECONDITION: a rollout is in progress"}, "GCLOUD_ERROR"),
])
def test_cloud_deploy_errors_fail_closed_without_traffic_changes(fail, taxonomy):
    runner = FakeRunner(releases=RELEASES, rollouts=ROLLOUTS, fail=fail)
    code, summary = run_cd(["--execute"], runner)
    assert code == 1 and summary["error_taxonomy"] == taxonomy and runner.kinds() == []


@pytest.mark.parametrize("releases,rollouts", [
    ([{"createTime": "2026-11-14T23:00:00Z"}], {}),
    ({"not": "a list"}, {}),
    ("not json", {}),
    (RELEASES, {REL_C: {"not": "a list"}}),
    (RELEASES, {REL_C: [cd_rollout("SUCCEEDED", "yesterday")]}),
])
def test_malformed_cloud_deploy_history_fails_closed(releases, rollouts):
    runner = FakeRunner(releases=releases, rollouts=rollouts)
    code, summary = run_cd(["--execute"], runner)
    assert code == 1 and summary["error_taxonomy"] == "MALFORMED_GCLOUD_JSON" and runner.kinds() == []


def test_unknown_mode_fails_closed():
    code, summary = run_cd(["--mode", "sideways"], FakeRunner())
    assert code == 1 and summary["error_taxonomy"] == "BAD_MODE"


def test_cloud_deploy_timeout_fails_closed():
    runner = FakeRunner(releases=RELEASES, rollouts=ROLLOUTS,
                        raise_on={"cd_releases_list": subprocess.TimeoutExpired(["gcloud"], 60)})
    code, summary = run_cd([], runner)
    assert code == 1 and summary["error_taxonomy"] == "TIMEOUT"
