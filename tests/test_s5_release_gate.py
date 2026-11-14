"""S5_RELEASE_GATE: submit/poll/cross-check happy path, edge cases, bounce routing, and gcloud chaos (no network)."""

from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timezone

import pytest

from scripts import s5_release_gate as s5
from scripts.cicd_common import Deps, RunResult, is_sealed
from tests.helpers import edit_json, load_json

CICD = load_json("config/cicd.json")
COMMIT = "3f1c2a9b8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a"
SHORT = COMMIT[:7]
BUILD_ID = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
DIGEST = "sha256:" + "4d" * 32
REPO = CICD["ci"]["image_repository"]
TAG = f"{REPO}:{SHORT}"
STEPS = list(CICD["release_gate"]["bounce_routes"])


def build_doc(status="SUCCESS", steps=None, digest=DIGEST, subs=None, results=True, **extra):
    subs = {"COMMIT_SHA": COMMIT, "SHORT_SHA": SHORT} if subs is None else subs
    doc = {"id": BUILD_ID, "status": status, "substitutions": subs,
           "steps": steps or [{"id": step, "status": "SUCCESS"} for step in STEPS], **extra}
    if results:
        doc["results"] = {"images": [{"name": TAG, "digest": digest}]}
    return doc


def cloud_packet(**override):
    doc = {"schema": CICD["release_gate"]["packet_schema"], "commit_sha": COMMIT, "short_sha": SHORT,
           "build_id": BUILD_ID, "project_id": "gement02", "image_tag_ref": TAG, "image_ref": f"{REPO}@{DIGEST}",
           "image_digest": DIGEST, "pytest_summary": "561 passed in 9.84s", "created_at": "2026-11-14T22:40:00Z"}
    doc.update(override)
    return doc


class FakeGcloud:
    """Scripted git/gcloud: one queue of describe documents, a cloud packet, and per-call failures."""

    def __init__(self, describes=None, packet=None, status="", head=COMMIT, fail=None, raise_on=None,
                 submit_out=BUILD_ID + "\n"):
        self.calls, self.status, self.head, self.submit_out = [], status, head, submit_out
        self.describes = list(describes or [build_doc("WORKING"), build_doc()])
        self.packet = cloud_packet() if packet is None else packet
        self.fail, self.raise_on = fail or {}, raise_on or {}

    def key(self, cmd):
        if cmd[0] == "git":
            return "git_status" if "status" in cmd else "git_head"
        return {"submit": "submit", "describe": "describe", "cat": "cat"}[cmd[2]]

    def __call__(self, cmd, timeout):
        key = self.key(cmd)
        self.calls.append((key, cmd, timeout))
        if key in self.raise_on:
            raise self.raise_on[key]
        if key in self.fail:
            return RunResult(1, "", self.fail[key])
        if key == "describe":
            doc = self.describes.pop(0) if len(self.describes) > 1 else self.describes[0]
        else:
            doc = {"git_status": self.status, "git_head": self.head + "\n", "submit": self.submit_out,
                   "cat": self.packet}[key]
        return RunResult(0, doc if isinstance(doc, str) else json.dumps(doc), "uploading tarball")

    def keys(self):
        return [key for key, _, _ in self.calls]


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "cloudbuild.yaml").write_text("steps: []\n", encoding="utf-8")
    return src


def gate(runner, source, tmp_path, build_id=None, config_root=None, clock=None):
    clock = clock or Clock()
    deps = Deps(runner=runner, sleep=clock.sleep, clock=lambda: clock.now,
                utcnow=lambda: datetime(2026, 11, 14, 22, 45, tzinfo=timezone.utc))
    kwargs = {"config_root": config_root} if config_root else {}
    return s5.run_release_gate(COMMIT, source, tmp_path / "packet.json", build_id=build_id, deps=deps, **kwargs)


def test_happy_path_submits_polls_crosschecks_and_writes_a_sealed_packet(source, tmp_path):
    runner, clock = FakeGcloud(), Clock()
    rc, summary = gate(runner, source, tmp_path, clock=clock)
    assert rc == 0 and summary["ok"] is True and summary["status"] == "SUCCESS"
    assert runner.keys() == ["git_status", "git_head", "submit", "describe", "describe", "cat"]
    submit = runner.calls[2][1]
    assert submit[:4] == ["gcloud", "builds", "submit", str(source.resolve())]
    assert f"--substitutions=COMMIT_SHA={COMMIT},SHORT_SHA={SHORT}" in submit and "--async" in submit
    assert f"--config={source.resolve() / 'cloudbuild.yaml'}" in submit and "--region=us-central1" in submit
    assert runner.calls[2][2] == CICD["release_gate"]["submit_timeout_seconds"]
    assert clock.sleeps == [CICD["release_gate"]["poll_interval_seconds"]]
    assert runner.calls[-1][1][3] == f"{CICD['release_gate']['artifact_prefix']}/{SHORT}/release_packet.json"
    packet = json.loads((tmp_path / "packet.json").read_text())
    assert is_sealed(packet) and packet["packet_sha256"] == summary["packet_sha256"]
    assert packet["image_ref"] == f"{REPO}@{DIGEST}" == summary["image_ref"] and packet["build_status"] == "SUCCESS"
    assert packet["pytest_passed"] == summary["pytest_passed"] == 561 and packet["verified_at"] == "2026-11-14T22:45:00Z"
    assert BUILD_ID in summary["console_url"] and "project=gement02" in summary["console_url"]


def test_build_id_reverifies_without_git_or_submit(source, tmp_path):
    runner = FakeGcloud(describes=[build_doc()])
    rc, summary = gate(runner, source, tmp_path, build_id=BUILD_ID)
    assert rc == 0 and summary["build_id"] == BUILD_ID and runner.keys() == ["describe", "cat"]


def test_main_prints_one_json_line_and_telemetry_goes_to_stderr(source, tmp_path, capsys):
    out = io.StringIO()
    deps = Deps(runner=FakeGcloud(describes=[build_doc()]), sleep=lambda _: None, clock=lambda: 0.0)
    code = s5.main(["--commit", COMMIT, "--source", str(source), "--packet", str(tmp_path / "p.json"),
                    "--build-id", BUILD_ID], deps=deps, stdout=out)
    lines = out.getvalue().splitlines()
    assert code == 0 and len(lines) == 1
    summary = json.loads(lines[0])
    assert len(summary["trace_id"]) == 32 and isinstance(summary["latency_ms"], int)
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    calls = [event for event in events if event.get("kind") == "external_call"]
    assert [event["op"] for event in calls] == ["gcloud.builds.describe", "gcloud.storage.cat"]
    assert all(event["trace_id"] == summary["trace_id"] and isinstance(event["latency_ms"], int) for event in calls)


def test_commit_is_required_on_the_cli():
    with pytest.raises(SystemExit):
        s5.parse_args(["--source", "."])


@pytest.mark.parametrize("status,head,taxonomy", [
    (" M app/app.py\n", COMMIT, "DIRTY_SOURCE"),
    ("?? scratch.txt\n", COMMIT, "DIRTY_SOURCE"),
    ("", "f" * 40, "HEAD_MISMATCH"),
])
def test_unclean_checkout_fails_before_submit(source, tmp_path, status, head, taxonomy):
    runner = FakeGcloud(status=status, head=head)
    rc, summary = gate(runner, source, tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == taxonomy and summary["bounce_to"] == "S3_BUILD"
    assert "submit" not in runner.keys() and not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize("commit", [COMMIT.upper(), COMMIT[:39], COMMIT[:-1] + "g", "", COMMIT + "0"])
def test_malformed_commit_never_reaches_gcloud(source, tmp_path, commit):
    runner = FakeGcloud()
    rc, summary = s5.run_release_gate(commit, source, tmp_path / "p.json", deps=Deps(runner=runner))
    assert rc == 1 and summary["error_taxonomy"] == "BAD_COMMIT_SHA" and runner.calls == []


def test_missing_build_config_in_source(tmp_path):
    runner = FakeGcloud()
    (tmp_path / "empty").mkdir()
    rc, summary = gate(runner, tmp_path / "empty", tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == "MISSING_BUILD_CONFIG" and "submit" not in runner.keys()


def test_submit_without_a_build_id_fails_closed(source, tmp_path):
    rc, summary = gate(FakeGcloud(submit_out="\n"), source, tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == "BAD_BUILD_ID"


@pytest.mark.parametrize("step,route", [
    ("guard-substitutions", "S4_VERIFY_QA"), ("unit-tests", "S3_BUILD"), ("docker-build", "S3_BUILD"),
    ("container-smoke", "S4_VERIFY_QA"), ("docker-push", "S3_BUILD"), ("release-packet", "S4_VERIFY_QA"),
])
def test_first_failed_step_picks_the_bounce_route(source, tmp_path, step, route):
    statuses = []
    for name in STEPS:
        statuses.append({"id": name, "status": "FAILURE" if name == step else ("QUEUED" if statuses and
                         statuses[-1]["status"] != "SUCCESS" else "SUCCESS")})
    runner = FakeGcloud(describes=[build_doc("FAILURE", steps=statuses, results=False)])
    rc, summary = gate(runner, source, tmp_path)
    assert rc == 1 and summary["ok"] is False and summary["failed_step"] == step and summary["bounce_to"] == route
    assert summary["build_id"] == BUILD_ID and BUILD_ID in summary["console_url"] and "cat" not in runner.keys()
    assert not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize("steps,expected_step,route", [
    ([{"id": "unit-tests", "status": "TIMEOUT"}], "unit-tests", "S3_BUILD"),
    ([{"id": "mystery-step", "status": "FAILURE"}], "mystery-step", "S3_BUILD"),
    ([{"id": step, "status": "SUCCESS"} for step in STEPS], None, "S3_BUILD"),
    ([{"id": "release-packet", "status": "CANCELLED"}, {"id": "unit-tests", "status": "FAILURE"}], "release-packet",
     "S4_VERIFY_QA"),
])
def test_unusual_failures_route_deterministically(source, tmp_path, steps, expected_step, route):
    doc = build_doc("FAILURE", steps=steps, failureInfo={"type": "PUSH_FAILED", "detail": "push denied"})
    rc, summary = gate(FakeGcloud(describes=[doc]), source, tmp_path)
    assert rc == 1 and summary["failed_step"] == expected_step and summary["bounce_to"] == route
    assert summary["failure_type"] == "PUSH_FAILED" and summary["error_taxonomy"] == "BUILD_FAILURE"


@pytest.mark.parametrize("describes,packet,taxonomy", [
    ([build_doc(results=False)], None, "RESULTS_IMAGE_MISSING"),
    ([build_doc(digest="sha256:short")], None, "BAD_IMAGE_DIGEST"),
    ([build_doc()], cloud_packet(image_digest="sha256:" + "0" * 64), "PACKET_MISMATCH"),
    ([build_doc()], cloud_packet(build_id="ffffffff-4f50-4a6b-8c7d-9e0f1a2b3c4d"), "PACKET_MISMATCH"),
    ([build_doc()], cloud_packet(schema="other/v9"), "PACKET_MISMATCH"),
    ([build_doc(subs={"COMMIT_SHA": "a" * 40, "SHORT_SHA": "aaaaaaa"})], None, "SUBSTITUTION_MISMATCH"),
    ([build_doc(subs={})], None, "SUBSTITUTION_MISMATCH"),
    ([build_doc()], cloud_packet(pytest_summary="3 failed, 558 passed in 9s"), "PYTEST_SUMMARY"),
    ([build_doc()], cloud_packet(pytest_summary=None), "PYTEST_SUMMARY"),
    ([build_doc()], "{not json", "MALFORMED_GCLOUD_JSON"),
    ([build_doc()], [1, 2], "PACKET_MALFORMED"),
])
def test_crosscheck_disagreements_bounce_to_verify(source, tmp_path, describes, packet, taxonomy):
    rc, summary = gate(FakeGcloud(describes=describes, packet=packet), source, tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == taxonomy and summary["bounce_to"] == "S4_VERIFY_QA"
    assert not (tmp_path / "packet.json").exists()


def test_artifact_permission_denied_is_a_verification_failure(source, tmp_path):
    runner = FakeGcloud(describes=[build_doc()], fail={"cat": "ERROR: HTTPError 403: Permission denied"})
    rc, summary = gate(runner, source, tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == "PERMISSION_DENIED" and summary["bounce_to"] == "S4_VERIFY_QA"


@pytest.mark.parametrize("fail,raise_on,taxonomy,build_known", [
    ({"submit": "ERROR: (gcloud.builds.submit) HTTPError 429: Too Many Requests"}, {}, "RATE_LIMITED", False),
    ({"submit": "ERROR: HTTPError 503: Service Unavailable"}, {}, "UNAVAILABLE", False),
    ({"describe": "ERROR: (gcloud.builds.describe) HTTPError 503: Service Unavailable"}, {}, "UNAVAILABLE", True),
    ({"cat": "ERROR: 429 Too Many Requests"}, {}, "RATE_LIMITED", True),
    ({}, {"describe": subprocess.TimeoutExpired(["gcloud"], 120)}, "TIMEOUT", True),
    ({}, {"submit": subprocess.TimeoutExpired(["gcloud"], 900)}, "TIMEOUT", False),
])
def test_transient_faults_exit_75_with_resumable_build_id(source, tmp_path, fail, raise_on, taxonomy, build_known):
    runner = FakeGcloud(describes=[build_doc()], fail=fail, raise_on=raise_on)
    rc, summary = gate(runner, source, tmp_path)
    assert rc == 75 and summary["transient"] is True and summary["error_taxonomy"] == taxonomy
    assert "bounce_to" not in summary and (summary["build_id"] == BUILD_ID) is build_known


@pytest.mark.parametrize("fail,taxonomy", [
    ({"submit": "ERROR: (gcloud.builds.submit) HTTPError 401: Request had invalid authentication credentials."},
     "UNAUTHENTICATED"),
    ({"submit": "ERROR: PERMISSION_DENIED: The caller does not have permission (403)"}, "PERMISSION_DENIED"),
    ({"describe": "ERROR: NOT_FOUND: build could not be found (404)"}, "NOT_FOUND"),
    ({"git_status": "fatal: not a git repository (or any of the parent directories): .git"}, "GIT_ERROR"),
])
def test_hard_gcloud_errors_fail_closed(source, tmp_path, fail, taxonomy):
    rc, summary = gate(FakeGcloud(fail=fail), source, tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == taxonomy and summary["bounce_to"] == "S3_BUILD"


@pytest.mark.parametrize("describes,rc,taxonomy", [
    (["not json"], 1, "MALFORMED_GCLOUD_JSON"),
    ([["a", "list"]], 1, "MALFORMED_GCLOUD_JSON"),
    ([build_doc("INTERNAL_ERROR")], 75, "BUILD_INTERNAL_ERROR"),
    ([build_doc("EXPIRED")], 75, "BUILD_EXPIRED"),
    ([build_doc("WORKING")], 75, "POLL_DEADLINE"),
    ([{"id": BUILD_ID}], 75, "POLL_DEADLINE"),
])
def test_odd_build_states(source, tmp_path, describes, rc, taxonomy):
    clock = Clock()
    code, summary = gate(FakeGcloud(describes=describes), source, tmp_path, clock=clock)
    assert code == rc and summary["error_taxonomy"] == taxonomy
    if taxonomy == "POLL_DEADLINE":
        assert sum(clock.sleeps) <= CICD["release_gate"]["max_wait_seconds"] and summary["build_id"] == BUILD_ID


def test_missing_gcloud_binary_with_the_real_runner(source, tmp_path, temp_root):
    edit_json(temp_root / "config" / "cicd.json", lambda doc: doc.update(gcloud_bin="/nonexistent/gcloud-xyz"))
    rc, summary = s5.run_release_gate(COMMIT, source, tmp_path / "p.json", build_id=BUILD_ID, config_root=temp_root)
    assert rc == 1 and summary["error_taxonomy"] == "BINARY_NOT_FOUND"


@pytest.mark.parametrize("mutate,taxonomy", [
    (lambda doc: doc.pop("release_gate"), "CONFIG_SCHEMA"),
    (lambda doc: doc["exit_codes"].update(transient="75"), "CONFIG_SCHEMA"),
    (lambda doc: doc["commands"]["build_submit"].append("{nope}"), "CONFIG_SCHEMA"),
])
def test_broken_config_fails_closed(source, tmp_path, temp_root, mutate, taxonomy):
    edit_json(temp_root / "config" / "cicd.json", mutate)
    rc, summary = gate(FakeGcloud(), source, tmp_path, config_root=temp_root)
    assert rc == 1 and summary["ok"] is False and summary["error_taxonomy"] == taxonomy


def test_missing_config_file(source, tmp_path, temp_root):
    (temp_root / "config" / "cicd.json").unlink()
    rc, summary = gate(FakeGcloud(), source, tmp_path, config_root=temp_root)
    assert rc == 1 and summary["error_taxonomy"] == "CONFIG_MISSING"
